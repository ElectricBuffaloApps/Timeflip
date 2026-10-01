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
                      "2026-09-30 09:00:00", "2026-09-30T09:00:00.1234567Z",
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
