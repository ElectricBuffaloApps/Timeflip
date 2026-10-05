import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from tftrack.api import TimeFlipClient
from tftrack.limits import alerts_due, evaluate, parse_limits, period_window
from tftrack.store import Store, parse_started_at

UTC = timezone.utc


class ParseStartedAtTest(unittest.TestCase):
    def test_formats(self):
        expected = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
        for value in ["2026-09-30T09:00:00Z", "2026-09-30T09:00:00", "2026-09-30T10:00:00+01:00",
                      "2026-09-30 09:00:00", "2026-09-30T09:00:00.1234567Z", "2026-09-30T10:00:00.000+01", "2026-09-30T10:00:00+0100",
                      1790758800, "1790758800000"]:
            self.assertEqual(parse_started_at(value).replace(microsecond=0), expected, value)


def make_store(intervals, tasks=None, unit="seconds"):
    tmp = tempfile.mkdtemp()
    store = Store(Path(tmp) / "t.sqlite")
    tasks = tasks or [
        {"id": 1, "name": "Admin", "tag": None},
        {"id": 2, "name": "Design", "tag": "Acme Ltd"},
        {"id": 3, "name": "Build", "tag": "Acme Ltd"},
    ]
    store.replace_all({"tasks": tasks, "timeIntervals": intervals}, unit)
    return store


class StoreTest(unittest.TestCase):
    def test_real_timeflip_shape_links_by_local_id(self):
        # Shape copied from a real /api/sync/all response.
        store = make_store(
            [
                {"id": 179118691200000, "startedAt": "2026-10-05T08:55:12.000+01", "duration": 5,
                 "taskId": None, "taskLocalId": "C35C", "extId": 6204897, "delDate": None},
                {"id": 179118691700000, "startedAt": "2026-10-05T08:55:17.000+01", "duration": 16501,
                 "taskId": None, "taskLocalId": "5679", "extId": 6204893, "delDate": None},
                {"id": 1, "startedAt": "2026-10-05T09:00:00.000+01", "duration": 60,
                 "taskId": None, "taskLocalId": "gone", "delDate": None},
            ],
            tasks=[
                {"id": 163705, "name": "FYPT Clients", "localId": "C35C", "tag": "FYPT1", "isBillable": False},
                {"id": 163706, "name": "Admin", "localId": "5679", "tag": None},
            ],
        )
        start = datetime(2026, 10, 5, tzinfo=UTC)
        self.assertEqual(store.seconds_by_task(start, start + timedelta(days=1)), {163705: 5, 163706: 16501})
        self.assertEqual(store.get_meta("unmatched_intervals"), "1")
        seg = store.segments(start, start + timedelta(days=1))[0]
        self.assertEqual(seg["start"], datetime(2026, 10, 5, 7, 55, 12, tzinfo=UTC))

    def test_clips_at_window_edges_and_skips_deleted(self):
        store = make_store([
            # 23:00 -> 01:00 across midnight: only 1h falls on the 30th.
            {"id": 1, "taskId": 1, "startedAt": "2026-09-29T23:00:00Z", "duration": 7200},
            {"id": 2, "taskId": 1, "startedAt": "2026-09-30T10:00:00Z", "duration": 1800},
            {"id": 3, "taskId": 1, "startedAt": "2026-09-30T12:00:00Z", "duration": 999, "delDate": "x"},
        ])
        start = datetime(2026, 9, 30, tzinfo=UTC)
        self.assertEqual(store.seconds_by_task(start, start + timedelta(days=1)), {1: 3600 + 1800})

    def test_millisecond_unit(self):
        store = make_store([{"id": 1, "taskId": 1, "startedAt": "2026-09-30T10:00:00Z", "duration": 60000}],
                           unit="milliseconds")
        start = datetime(2026, 9, 30, tzinfo=UTC)
        self.assertEqual(store.seconds_by_task(start, start + timedelta(days=1)), {1: 60})

    def test_resync_replaces_data(self):
        store = make_store([{"id": 1, "taskId": 1, "startedAt": "2026-09-30T10:00:00Z", "duration": 60}])
        store.replace_all({"tasks": [{"id": 1, "name": "Admin"}], "timeIntervals": []})
        start = datetime(2026, 9, 30, tzinfo=UTC)
        self.assertEqual(store.seconds_by_task(start, start + timedelta(days=1)), {})


class LimitsTest(unittest.TestCase):
    def test_parse_requires_one_target_and_a_period(self):
        with self.assertRaises(ValueError):
            parse_limits({"limits": [{"task": "A", "client": "B", "daily_hours": 1}]})
        with self.assertRaises(ValueError):
            parse_limits({"limits": [{"task": "A"}]})
        both = parse_limits({"limits": [{"task": "A", "daily_hours": 1, "weekly_hours": 5}]})
        self.assertEqual([l.period for l in both], ["daily", "weekly"])

    def test_week_window(self):
        now = datetime(2026, 10, 1, 15, 0).astimezone()  # Thursday, local time
        start, end = period_window("weekly", now, "monday")
        self.assertEqual((start.day, start.hour, (end - start).days), (28, 0, 7))
        start, _ = period_window("weekly", now, "sunday")
        self.assertEqual(start.day, 27)

    def test_client_limit_sums_tagged_tasks_and_alerts_once(self):
        now = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
        store = make_store([
            {"id": 1, "taskId": 2, "startedAt": "2026-09-29T09:00:00Z", "duration": 4 * 3600},
            {"id": 2, "taskId": 3, "startedAt": "2026-10-01T09:00:00Z", "duration": 4 * 3600},
            {"id": 3, "taskId": 1, "startedAt": "2026-10-01T09:00:00Z", "duration": 3600},
        ])
        limits = parse_limits({"limits": [
            {"client": "acme ltd", "weekly_hours": 10},
            {"task": "Admin", "daily_hours": 2},
        ]})
        statuses = evaluate(limits, store.tasks(), store.seconds_by_task, now)
        self.assertEqual([s.seconds for s in statuses], [8 * 3600, 3600])

        due = alerts_due(statuses, [80, 100], store.alert_already_sent)
        self.assertEqual([(d[0].limit.label, d[1]) for d in due], [("acme ltd", 80)])
        store.mark_alert_sent(due[0][0].limit.key, due[0][0].period_start.isoformat(), 80)
        self.assertEqual(alerts_due(statuses, [80, 100], store.alert_already_sent), [])


class FakeApi(BaseHTTPRequestHandler):
    logins = 0
    valid_token = "t1"

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/auth/email/sign-in" and body["password"] == "pw":
            FakeApi.logins += 1
            FakeApi.valid_token = f"t{FakeApi.logins}"
            return self._send(200, {"token": FakeApi.valid_token})
        self._send(401, {})

    def do_GET(self):
        if self.headers.get("Authorization") != f"Bearer {FakeApi.valid_token}":
            return self._send(401, {})
        self._send(200, {"tasks": [], "timeIntervals": []})


class ApiTest(unittest.TestCase):
    def test_login_and_relogin_on_expired_token(self):
        server = HTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = TimeFlipClient("a@b.c", "pw", token="stale",
                                    base_url=f"http://127.0.0.1:{server.server_port}")
            self.assertEqual(client.sync_all(), {"tasks": [], "timeIntervals": []})
            self.assertEqual(FakeApi.logins, 1)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()


class ReportsTest(unittest.TestCase):
    def test_report_and_timesheet(self):
        from datetime import date
        from tftrack.reports import build_report, timesheet_csv, timesheet_rows
        tasks = [
            {"id": 1, "name": "Admin", "tag": None},
            {"id": 2, "name": "Design", "tag": "Acme Ltd", "isBillable": True, "hourlyRate": 60, "currency": "POUND"},
        ]
        noon = datetime(2026, 9, 30, 12, 0).astimezone()
        store = make_store([
            {"id": 1, "taskId": 2, "startedAt": noon.astimezone(UTC).isoformat(), "duration": 5400},
            {"id": 2, "taskId": 1, "startedAt": (noon + timedelta(days=1)).astimezone(UTC).isoformat(), "duration": 1800},
        ], tasks=tasks)
        r = build_report(store, date(2026, 9, 30), date(2026, 10, 1), "client")
        self.assertEqual(r["days"], ["2026-09-30", "2026-10-01"])
        by_name = {s["name"]: s for s in r["series"]}
        self.assertEqual(by_name["Acme Ltd"]["values"], [5400, 0])
        self.assertEqual(by_name["No client"]["values"], [0, 1800])
        self.assertEqual(r["amounts"], {"£": 90.0})

        rows = timesheet_rows(store, date(2026, 9, 30), date(2026, 10, 1), "acme ltd")
        self.assertEqual([(x["task"], x["hours"], x["amount"]) for x in rows], [("Design", 1.5, 90.0)])
        self.assertIn("Total,,,,,1.50,,,£,90.00", timesheet_csv(rows))


class WebTest(unittest.TestCase):
    def test_host_check_and_saving_limits(self):
        import os
        import urllib.error
        import urllib.request
        from tftrack import config as cfg, web
        tmp = Path(tempfile.mkdtemp())
        old = (cfg.APP_DIR, cfg.CONFIG_PATH, cfg.DB_PATH, cfg.PORT)
        cfg.APP_DIR, cfg.CONFIG_PATH, cfg.DB_PATH = tmp, tmp / "config.json", tmp / "db.sqlite"
        server = HTTPServer(("127.0.0.1", 0), web.Handler)
        cfg.PORT = server.server_port
        threading.Thread(target=server.serve_forever, daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f"http://127.0.0.1:{cfg.PORT}"
        try:
            def post(path, body, headers):
                req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST", headers=headers)
                return opener.open(req)
            with self.assertRaises(urllib.error.HTTPError) as e:
                post("/api/limits", {"limits": []}, {})
            self.assertEqual(e.exception.code, 403)
            resp = post("/api/limits", {"limits": [{"client": "Acme", "weekly_hours": "10", "daily_hours": ""}]},
                        {"X-TFTrack": "1", "Content-Type": "application/json"})
            self.assertEqual(json.load(resp)["limits"], [{"client": "Acme", "weekly_hours": 10.0}])
            with self.assertRaises(urllib.error.HTTPError) as e:
                post("/api/limits", {"limits": [{"task": "A", "daily_hours": "-1"}]}, {"X-TFTrack": "1"})
            self.assertEqual(e.exception.code, 400)
            self.assertIn(b"TimeFlip Tracker", opener.open(base + "/").read())
        finally:
            server.shutdown()
            cfg.APP_DIR, cfg.CONFIG_PATH, cfg.DB_PATH, cfg.PORT = old
