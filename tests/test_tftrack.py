import io
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


class InvoiceTest(unittest.TestCase):
    def test_week_months_use_mondays_in_month(self):
        from datetime import date
        from tftrack.reports import month_range
        self.assertEqual(month_range(2026, 9, "week"), (date(2026, 9, 7), date(2026, 10, 4)))
        self.assertEqual(month_range(2026, 10, "week"), (date(2026, 10, 5), date(2026, 11, 1)))
        self.assertEqual(month_range(2026, 12, "day"), (date(2026, 12, 1), date(2026, 12, 31)))

    def test_rounding(self):
        from tftrack.reports import round_seconds
        m = 60
        self.assertEqual(round_seconds(52 * m, 15, "nearest"), 45 * m)
        self.assertEqual(round_seconds(53 * m, 15, "nearest"), 60 * m)
        self.assertEqual(round_seconds(46 * m, 15, "up"), 60 * m)
        self.assertEqual(round_seconds(59 * m, 15, "down"), 45 * m)
        self.assertEqual(round_seconds(59 * m, 15, "none"), 59 * m)

    def test_day_invoice_rounds_days_and_skips_excluded(self):
        from tftrack.reports import invoice
        tasks = [{"id": 1, "name": "Build", "tag": "HTB"}, {"id": 2, "name": "Other", "tag": "FCY"}]
        at = lambda d, h: datetime(2026, 9, d, h).astimezone().astimezone(UTC).isoformat()
        store = make_store([
            {"id": 10, "taskId": 1, "startedAt": at(3, 9), "duration": 50 * 60},
            {"id": 11, "taskId": 1, "startedAt": at(3, 11), "duration": 20 * 60},
            {"id": 12, "taskId": 1, "startedAt": at(4, 9), "duration": 3600},
            {"id": 13, "taskId": 2, "startedAt": at(4, 9), "duration": 3600},
        ], tasks=tasks)
        store.set_excluded(11, True)
        inv = invoice(store, "htb", 2026, 9, "day", 15, "nearest")
        days = inv["groups"][0]["days"]
        self.assertEqual([d["date"] for d in days], ["2026-09-03", "2026-09-04"])
        self.assertEqual([d["billed_s"] for d in days], [45 * 60, 3600])   # 50m -> 45m; excluded 20m ignored
        self.assertEqual(len(days[0]["entries"]), 2)                        # excluded entry still listed
        self.assertEqual((inv["days_worked"], inv["total_billed_s"]), (2, 105 * 60))
        store.set_excluded(11, False)
        self.assertEqual(invoice(store, "HTB", 2026, 9, "day", 15, "nearest")["total_billed_s"], 135 * 60)

    def test_week_invoice_groups_by_monday(self):
        from tftrack.reports import invoice
        at = lambda m, d: datetime(2026, m, d, 9).astimezone().astimezone(UTC).isoformat()
        store = make_store([
            {"id": 1, "taskId": 1, "startedAt": at(9, 1), "duration": 600},    # before first Monday: August's
            {"id": 2, "taskId": 1, "startedAt": at(9, 28), "duration": 600},
            {"id": 3, "taskId": 1, "startedAt": at(10, 2), "duration": 600},   # belongs to w/c 28 Sep
        ], tasks=[{"id": 1, "name": "Prog", "tag": "FCY"}])
        inv = invoice(store, "FCY", 2026, 9, "week")
        self.assertEqual(len(inv["groups"]), 4)
        self.assertEqual(inv["groups"][-1]["label"], "Week commencing Mon 28 Sep 2026")
        self.assertEqual([d["date"] for d in inv["groups"][-1]["days"]], ["2026-09-28", "2026-10-02"])
        self.assertEqual(inv["total_billed_s"], 1200)


class UpdaterTest(unittest.TestCase):
    def test_install_zip_swaps_code_folder(self):
        import io
        import os
        import zipfile
        from tftrack import updater
        install = Path(tempfile.mkdtemp()) / "Application Support" / "TimeFlip Tracker"
        (install / "tftrack").mkdir(parents=True)
        (install / "tftrack" / "VERSION").write_text("1\n")
        (install / "tftrack" / "stale.py").write_text("")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:   # same layout as a GitHub branch download
            root = "Timeflip-claude-branch/"
            zf.writestr(root + "tftrack/__main__.py", "print('hi')\n")
            zf.writestr(root + "tftrack/VERSION", "7\n")
            zf.writestr(root + "tftrack/static/index.html", "<html></html>")
            zf.writestr(root + "Uninstall TimeFlip Tracker.command", "#!/bin/bash\n")
            zf.writestr(root + "README.md", "ignored")
        self.assertEqual(updater.install_zip(buf.getvalue(), install), 7)
        self.assertEqual((install / "tftrack" / "VERSION").read_text().strip(), "7")
        self.assertFalse((install / "tftrack" / "stale.py").exists())
        self.assertTrue((install / "tftrack" / "static" / "index.html").exists())
        self.assertFalse((install / "README.md").exists())
        self.assertTrue(os.access(install / "Uninstall TimeFlip Tracker.command", os.X_OK))
        self.assertEqual(sorted(p.name for p in install.iterdir()), ["Uninstall TimeFlip Tracker.command", "tftrack"])

    def test_version_file_is_a_number(self):
        from tftrack import updater
        self.assertGreater(updater.current_version(), 0)


class AutoUpdateTest(unittest.TestCase):
    def test_due_once_a_day_after_3am(self):
        from tftrack.updater import auto_update_due
        self.assertFalse(auto_update_due(None, datetime(2026, 10, 6, 2, 55)))
        self.assertTrue(auto_update_due(None, datetime(2026, 10, 6, 3, 0)))
        self.assertTrue(auto_update_due("2026-10-05", datetime(2026, 10, 6, 9, 0)))   # asleep overnight
        self.assertFalse(auto_update_due("2026-10-06", datetime(2026, 10, 6, 15, 0)))

    def test_changes_since(self):
        from tftrack import updater
        cur = updater.current_version()
        self.assertEqual([n["version"] for n in updater.changes_since(cur)], [])
        self.assertEqual([n["version"] for n in updater.changes_since(None)], [cur])
        self.assertTrue(all(n["changes"] for n in updater.changes_since(cur - 2)))
        self.assertEqual(updater.changes_since(None)[0]["version"], cur, "CHANGELOG.json needs an entry for VERSION")

    def test_maybe_auto_update(self):
        from unittest import mock
        from tftrack import updater
        store = make_store([])
        now = datetime(2026, 10, 6, 3, 2)
        with mock.patch.object(updater, "is_installed_copy", return_value=True), \
             mock.patch.object(updater, "latest_version", return_value=99), \
             mock.patch.object(updater, "update", return_value=99) as upd, \
             mock.patch.object(updater, "restart_web_agent") as restart:
            self.assertIsNone(updater.maybe_auto_update({"auto_update": False}, store, now))
            self.assertEqual(updater.maybe_auto_update({}, store, now), 99)
            self.assertIsNone(updater.maybe_auto_update({}, store, now))   # already done today
            self.assertEqual((upd.call_count, restart.call_count), (1, 1))
        store2 = make_store([])
        with mock.patch.object(updater, "is_installed_copy", return_value=True), \
             mock.patch.object(updater, "latest_version", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                updater.maybe_auto_update({}, store2, now)
        self.assertIsNone(store2.get_meta("auto_update_checked"))           # will retry at the next check


class InvoiceLinesTest(unittest.TestCase):
    def test_lines_per_day_and_per_week(self):
        from tftrack.reports import invoice, invoice_lines
        at = lambda m, d, h=9: datetime(2026, m, d, h).astimezone().astimezone(UTC).isoformat()
        store = make_store([
            {"id": 1, "taskId": 1, "startedAt": at(9, 28), "duration": 50 * 60},
            {"id": 2, "taskId": 1, "startedAt": at(10, 2), "duration": 70 * 60},
            {"id": 3, "taskId": 2, "startedAt": at(9, 29), "duration": 6 * 60},
        ], tasks=[{"id": 1, "name": "Prog", "tag": "FCY"}, {"id": 2, "name": "Build", "tag": "HTB"}])
        fcy = invoice_lines(invoice(store, "FCY", 2026, 9, "week"), 40)
        self.assertEqual(fcy, [{"description": "FCY – week commencing Mon 28 Sep 2026", "hours": 2.0,
                                "rate": 40, "amount": 80.0}])
        htb = invoice_lines(invoice(store, "HTB", 2026, 9, "day", 15, "up"))
        self.assertEqual(htb, [{"description": "HTB – Tue 29 Sep 2026", "hours": 0.25, "rate": None, "amount": None}])


class MacAppTest(unittest.TestCase):
    def test_build_app_bundle(self):
        import os
        import plistlib
        import subprocess
        from tftrack.macapp import build_app
        app = build_app(Path(tempfile.mkdtemp()))
        self.assertEqual(app.name, "TimeFlip Tracker.app")
        info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
        self.assertEqual((info["CFBundleExecutable"], info["CFBundleIconFile"]), ("launcher", "AppIcon"))
        launcher = app / "Contents" / "MacOS" / "launcher"
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertEqual(subprocess.run(["bash", "-n", str(launcher)]).returncode, 0)
        self.assertIn("http://127.0.0.1:8765/", launcher.read_text())
        self.assertGreater((app / "Contents" / "Resources" / "AppIcon.icns").stat().st_size, 1000)


class AdjustmentTest(unittest.TestCase):
    def test_adjust_move_add_and_reset(self):
        from tftrack.reports import invoice, timesheet_rows
        from datetime import date
        at = lambda d, h, m=0: datetime(2026, 9, d, h, m).astimezone()
        tasks = [{"id": 1, "name": "Build", "tag": "HTB"}, {"id": 2, "name": "Prog", "tag": "FCY"}]
        store = make_store([{"id": 5, "taskId": 1, "startedAt": at(3, 9).astimezone(UTC).isoformat(),
                             "duration": 3600}], tasks=tasks)
        # shorten to 09:00-09:40 and keep it on HTB
        store.set_adjustment(5, at(3, 9), 40 * 60, 1)
        row = timesheet_rows(store, date(2026, 9, 3), date(2026, 9, 3), "HTB")[0]
        self.assertEqual((row["start"], row["end"], row["adjusted"], row["note"]), ("09:00", "09:40", True, "was 09:00–10:00"))
        # survives a resync
        store.replace_all({"tasks": tasks, "timeIntervals": [{"id": 5, "taskId": 1,
                           "startedAt": at(3, 9).astimezone(UTC).isoformat(), "duration": 3600}]})
        self.assertEqual(invoice(store, "HTB", 2026, 9, "day")["total_billed_s"], 40 * 60)
        # move it to FCY
        store.set_adjustment(5, at(3, 9), 40 * 60, 2)
        self.assertEqual(timesheet_rows(store, date(2026, 9, 3), date(2026, 9, 3), "HTB"), [])
        moved = timesheet_rows(store, date(2026, 9, 3), date(2026, 9, 3), "FCY")[0]
        self.assertEqual(moved["note"], "was 09:00–10:00, Build")
        # add a forgotten call, then delete it
        mid = store.add_manual(at(3, 14), 30 * 60, 2)
        rows = timesheet_rows(store, date(2026, 9, 3), date(2026, 9, 3), "FCY")
        self.assertEqual([(r["start"], r["manual"]) for r in rows], [("09:00", False), ("14:00", True)])
        store.set_adjustment(mid, at(3, 14), 45 * 60, 2)   # editing an added entry changes it in place
        self.assertEqual(timesheet_rows(store, date(2026, 9, 3), date(2026, 9, 3), "FCY")[1]["end"], "14:45")
        store.delete_manual(mid)
        store.clear_adjustment(5)
        rows = timesheet_rows(store, date(2026, 9, 3), date(2026, 9, 3))
        self.assertEqual([(r["task"], r["end"], r["adjusted"]) for r in rows], [("Build", "10:00", False)])


class MoneyTest(unittest.TestCase):
    def test_earned_and_imagined_value_kept_apart(self):
        from datetime import date
        from tftrack.reports import money_summary
        at = lambda h, m=0: datetime(2026, 10, 7, h, m).astimezone().astimezone(UTC).isoformat()
        tasks = [{"id": 1, "name": "Build", "tag": "HTB"}, {"id": 2, "name": "Coaching", "tag": "FYPT1"},
                 {"id": 3, "name": "Admin", "tag": None}]
        store = make_store([
            {"id": 1, "taskId": 1, "startedAt": at(9), "duration": 50 * 60},      # HTB 50m -> 1h rounded up
            {"id": 2, "taskId": 1, "startedAt": at(11), "duration": 20 * 60},     # excluded below
            {"id": 3, "taskId": 2, "startedAt": at(13), "duration": 90 * 60},     # FYPT1 1.5h imagined
            {"id": 4, "taskId": 3, "startedAt": at(15), "duration": 3600},        # untagged: not counted
        ], tasks=tasks)
        store.set_excluded(2, True)
        conf = {"HTB": {"rate": 40, "rounding": "up", "block_minutes": 15},
                "FYPT1": {"rate": 50, "notional": True}}
        m = money_summary(store, date(2026, 10, 7), date(2026, 10, 7), conf)
        self.assertEqual((m["earned"], m["value"]), (40.0, 75.0))
        self.assertEqual([(c["client"], c["notional"]) for c in m["by_client"]], [("HTB", False), ("FYPT1", True)])
        self.assertNotIn("Admin", m["by_task"])


class MonthlyIncomeTest(unittest.TestCase):
    def test_effective_rate_and_carry_forward(self):
        from tftrack.reports import income_for_month, month_summary
        at = lambda m, d, h: datetime(2026, m, d, h).astimezone().astimezone(UTC).isoformat()
        tasks = [{"id": 1, "name": "Coaching", "tag": "FYPT1"}, {"id": 2, "name": "Dev", "tag": "FYPTD"},
                 {"id": 3, "name": "Build", "tag": "HTB"}]
        store = make_store([
            {"id": 1, "taskId": 1, "startedAt": at(10, 6, 9), "duration": 10 * 3600},
            {"id": 2, "taskId": 1, "startedAt": at(10, 7, 9), "duration": 5 * 3600},
            {"id": 3, "taskId": 2, "startedAt": at(10, 8, 9), "duration": 2 * 3600},
            {"id": 4, "taskId": 3, "startedAt": at(10, 9, 9), "duration": 50 * 60},
        ], tasks=tasks)
        conf = {"FYPT1": {"billing": "monthly"}, "FYPTD": {"rate": 50, "notional": True},
                "HTB": {"rate": 40, "rounding": "up", "block_minutes": 15}}
        incomes = {"FYPT1": {"2026-09": 600}}
        self.assertEqual(income_for_month(incomes, "fypt1", "2026-10"), (600.0, True))
        self.assertEqual(income_for_month(incomes, "FYPT1", "2026-08"), (None, False))
        m = month_summary(store, 2026, 10, conf, incomes)
        rows = {r["client"]: r for r in m["clients"]}
        self.assertEqual((rows["FYPT1"]["kind"], rows["FYPT1"]["amount"], rows["FYPT1"]["rate"]), ("monthly", 600.0, 40.0))
        self.assertEqual((rows["FYPTD"]["kind"], rows["FYPTD"]["amount"]), ("imagined", 100.0))
        self.assertEqual((rows["HTB"]["kind"], rows["HTB"]["amount"]), ("hourly", 40.0))
        self.assertEqual((m["earned"], m["value"]), (640.0, 100.0))


class IgnoredTasksTest(unittest.TestCase):
    def test_ignored_tasks_left_out(self):
        from tftrack.store import Store
        tmp = Path(tempfile.mkdtemp()) / "t.sqlite"
        base = Store(tmp)
        base.replace_all({"tasks": [{"id": 1, "name": "Work"}, {"id": 2, "name": "Break"}], "timeIntervals": [
            {"id": 1, "taskId": 1, "startedAt": "2026-10-07T09:00:00Z", "duration": 3600},
            {"id": 2, "taskId": 2, "startedAt": "2026-10-07T10:00:00Z", "duration": 900}]})
        start = datetime(2026, 10, 7, tzinfo=UTC)
        end = start + timedelta(days=1)
        self.assertEqual(Store(tmp).seconds_by_task(start, end), {1: 3600, 2: 900})
        self.assertEqual(Store(tmp, ignored_task_ids=[2]).seconds_by_task(start, end), {1: 3600})


class TargetsTest(unittest.TestCase):
    def test_income_and_time_targets_with_estimates(self):
        from datetime import date
        from tftrack.reports import targets_summary
        at = lambda m, d, h: datetime(2026, m, d, h).astimezone().astimezone(UTC).isoformat()
        tasks = [{"id": 1, "name": "Coaching", "tag": "FYPT1"}, {"id": 2, "name": "Dev", "tag": "FYPTD"},
                 {"id": 3, "name": "Build", "tag": "HTB"}]
        store = make_store([
            {"id": 1, "taskId": 1, "startedAt": at(9, 10, 9), "duration": 20 * 3600},   # Sept: 20h FYPT1
            {"id": 2, "taskId": 1, "startedAt": at(10, 7, 9), "duration": 2 * 3600},    # today: 2h FYPT1
            {"id": 3, "taskId": 3, "startedAt": at(10, 7, 12), "duration": 3600},       # today: 1h HTB
            {"id": 4, "taskId": 2, "startedAt": at(10, 6, 9), "duration": 3 * 3600},    # this week: 3h FYPTD
        ], tasks=tasks)
        conf = {"week_starts": "monday",
                "clients": {"FYPT1": {"billing": "monthly"}, "FYPTD": {"rate": 50, "notional": True},
                            "HTB": {"rate": 40}},
                "monthly_income": {"FYPT1": {"2026-09": 600}},
                "targets": {"income": {"day": 200, "month": 2000}, "hours": {"FYPTD": {"week": 5}}}}
        t = targets_summary(store, date(2026, 10, 7), conf)   # a Wednesday
        inc = {r["period"]: r for r in t["income"]}
        # today: HTB £40 real + FYPT1 2h at last month's £30/h = £60 estimated; FYPTD imagined value not counted
        self.assertEqual((inc["day"]["amount"], inc["day"]["estimated"], inc["day"]["rest_day"]), (100.0, 60.0, False))
        # month: HTB £40 + FYPT1 £600 carried as an estimate
        self.assertEqual((inc["month"]["amount"], inc["month"]["estimated"]), (640.0, 600.0))
        self.assertEqual([(r["client"], r["period"], r["seconds"]) for r in t["time"]], [("FYPTD", "week", 3 * 3600)])
        # Saturday: no daily target unless weekends are on
        self.assertTrue({r["period"]: r for r in targets_summary(store, date(2026, 10, 10), conf)["income"]}["day"]["rest_day"])


class AllowanceTest(unittest.TestCase):
    def setUp(self):
        at = lambda m, d, h: datetime(2026, m, d, h).astimezone().astimezone(UTC).isoformat()
        tasks = [{"id": 1, "name": "Justin", "tag": "HTB"}, {"id": 2, "name": "Build", "tag": "HTB"}]
        self.store = make_store([
            {"id": 1, "taskId": 1, "startedAt": at(9, 3, 9), "duration": 3 * 3600},
            {"id": 2, "taskId": 1, "startedAt": at(9, 4, 9), "duration": 2 * 3600 - 300},  # rounds up to 2h
            {"id": 3, "taskId": 2, "startedAt": at(9, 4, 13), "duration": 30 * 60},
            {"id": 4, "taskId": 1, "startedAt": at(10, 5, 9), "duration": 3600},
        ], tasks=tasks)
        self.conf = {"clients": {"HTB": {"billing": "day", "rounding": "up", "block_minutes": 15}},
                     "limits": [{"task": "Justin", "weekly_hours": 1, "allowance": True, "allowance_start": "2026-09"}]}
        self.a = {"scope": "task", "name": "Justin", "hours_per_week": 1, "start": "2026-09", "opening_hours": 0}

    def _inv(self, month):
        from tftrack import allowance
        from tftrack.reports import invoice
        inv = invoice(self.store, "HTB", 2026, month, "day", 15, "up")
        inv = allowance.apply(inv, allowance.for_invoice(self.store, self.conf, inv, set()))
        if inv["allowances"]:   # the first allowance, as most of these tests have one
            inv["allowance"], inv["carry"] = inv["allowances"][0], inv["allowances"][0]["carry"]
        return inv

    def test_monthly_max_and_week_maxes(self):
        sep = self._inv(9)["allowance"]
        # 22 weekdays x 12 minutes = 4h 24m, rounded down to whole 15-minute blocks
        self.assertEqual((sep["working_days"], sep["max_s"]), (22, 255 * 60))
        self.assertEqual((sep["worked_s"], sep["charging_s"], sep["rolling_s"]), (5 * 3600, 5 * 3600, 0))
        self.assertEqual(sep["carry"]["available_s"], 0)
        w = sep["weeks"][0]   # w/c 31 Aug: only Tue 1 – Sun 6 Sep are September's (4 weekdays)
        self.assertEqual((w["monday"], w["from"], w["working_days"], w["max_s"], w["worked_s"]),
                         ("2026-08-31", "2026-09-01", 4, 45 * 60, 5 * 3600))

    def test_roll_over_ticks_build_a_balance_added_with_buttons(self):
        from tftrack import web
        from tftrack.reports import invoice_lines
        self.store.set_rolled(2, "carried", "2026-09-04")    # Justin's 4 Sept entry (rounds to 2h)
        sep = self._inv(9)
        days = {d["date"]: d["billed_s"] for d in sep["groups"][0]["days"]}
        self.assertEqual(days, {"2026-09-03": 3 * 3600, "2026-09-04": 30 * 60})   # only the other HTB work is left
        self.assertEqual((sep["rolled_s"], sep["allowance"]["charging_s"]), (2 * 3600, 3 * 3600))
        # October: 2h carried, nothing added until you press a button
        oct_ = self._inv(10)
        c = oct_["carry"]
        self.assertEqual((c["available_s"], c["added_s"], oct_["total_billed_s"]), (2 * 3600, 0, 3600))
        # October's weeks have spare room under the 1h-a-week max: 5 Oct week has 1h worked of its 1h max,
        # so added hours go to the other weeks first
        self.store.set_carry_added("HTB", "2026-10", 30 * 60)
        oct_ = self._inv(10)
        c = oct_["carry"]
        self.assertEqual((c["added_s"], c["remaining_s"], oct_["total_billed_s"]), (30 * 60, 90 * 60, 90 * 60))
        self.assertEqual(sum(w["seconds"] for w in c["by_week"]) + c["unplaced_s"], 30 * 60)
        lines = invoice_lines(oct_, 40)
        self.assertTrue(lines[0]["description"].startswith("HTB – Justin, carried over (week of "))
        self.assertEqual(sum(l["hours"] for l in lines), 1.5)
        # November: what's left (1h 30m) is still available
        self.assertEqual(self._inv(11)["carry"]["available_s"], 90 * 60)
        # asking for more than is available is capped
        self.store.set_carry_added("HTB", "2026-11", 10 * 3600)
        self.assertEqual(self._inv(11)["carry"]["added_s"], 90 * 60)
        orig, web._task_options = web._task_options, lambda: ""
        try:
            page = web.invoice_page(sep)
            self.assertIn("rolled over: carried to a later invoice", page)
            self.assertIn("Week of Mon 31 Aug total", page)
            page = web.invoice_page(self._inv(10))
            self.assertIn("Justin: carried over to charge: 2h 00m", page)
            self.assertIn('data-seconds="2700"', page)   # +15m from the 30m already added
        finally:
            web._task_options = orig
        # hours brought in at the start join the balance from the first month
        conf = dict(self.conf, limits=[dict(self.conf["limits"][0], opening_hours=1)])
        from tftrack import allowance
        from tftrack.reports import invoice
        inv = invoice(self.store, "HTB", 2026, 9, "day", 15, "up")
        self.assertEqual(allowance.for_invoice(self.store, conf, inv, set())[0]["carry"]["available_s"], 3600)

    def test_allowance_for_a_whole_client(self):
        # all of HTB's work counts, so Build entries can be rolled over too
        from tftrack import web
        self.conf["limits"] = [{"client": "HTB", "weekly_hours": 1, "allowance": True, "allowance_start": "2026-09"}]
        sep = self._inv(9)
        al = sep["allowance"]
        self.assertEqual((al["scope"], al["name"], al["key"]), ("client", "HTB", ""))
        self.assertEqual(al["worked_s"], 3 * 3600 + 2 * 3600 + 30 * 60)   # 3 Sept 3h; 4 Sept 1h55 + 30m rounds to 2h30
        self.store.set_rolled(3, "carried", "2026-09-04")                 # the Build entry
        sep = self._inv(9)
        self.assertEqual((sep["allowance"]["worked_s"], sep["allowance"]["rolling_s"]), (5 * 3600, 30 * 60))
        self.assertEqual(self._inv(10)["carry"]["available_s"], 30 * 60)
        orig, web._task_options = web._task_options, lambda: ""
        try:
            page = web.invoice_page(sep)
        finally:
            web._task_options = orig
        self.assertEqual(page.count("data-roll="), 3)   # every HTB entry in September has a Roll over tick
        from tftrack.reports import invoice_lines
        from tftrack import allowance
        allowance.set_added(self.store, self.conf, "HTB", "", "2026-10", 30 * 60)
        self.assertTrue(invoice_lines(self._inv(10), 40)[0]["description"].startswith("HTB, carried over (week of "))

    def test_two_allowances_for_one_client_keep_separate_balances(self):
        from tftrack import allowance
        self.conf["limits"].append({"task": "Build", "weekly_hours": 2, "allowance": True, "allowance_start": "2026-09",
                                    "opening_hours": 1})
        self.store.set_carry_added("HTB", "2026-10", 15 * 60)   # added before allowances had names: stays with Justin
        self.store.set_rolled(2, "carried", "2026-09-04")       # Justin, 1h55
        self.store.set_rolled(3, "carried", "2026-09-04")       # Build, 30m
        sep = self._inv(9)
        justin, build = sep["allowances"]
        # the day's charge was 2h30 (1h55 + 30m, rounded up); Justin's share is the first drop, Build's the rest
        self.assertEqual((justin["rolling_s"], build["rolling_s"], sep["rolled_s"]), (2 * 3600, 30 * 60, 150 * 60))
        self.assertEqual(build["carry"]["available_s"], 3600)   # the hour brought in
        justin, build = self._inv(10)["allowances"]
        self.assertEqual((justin["carry"]["available_s"], justin["carry"]["added_s"]), (2 * 3600, 15 * 60))
        self.assertEqual((build["carry"]["available_s"], build["carry"]["added_s"]), (90 * 60, 0))
        allowance.set_added(self.store, self.conf, "HTB", "Build", "2026-10", 3600)
        allowance.set_added(self.store, self.conf, "HTB", "justin", "2026-10", 30 * 60)   # replaces the unnamed 15m
        oct_ = self._inv(10)
        justin, build = oct_["allowances"]
        self.assertEqual((justin["carry"]["added_s"], build["carry"]["added_s"]), (30 * 60, 3600))
        self.assertEqual(oct_["total_billed_s"], 3600 + 90 * 60)
        self.assertEqual(sorted((r["task"], r["seconds"]) for r in self.store.carry_rows("HTB")),
                         [("Build", 3600), ("Justin", 1800)])
        justin, build = self._inv(11)["allowances"]
        self.assertEqual((justin["carry"]["available_s"], build["carry"]["available_s"]), (90 * 60, 30 * 60))

    def test_a_client_cannot_have_a_whole_client_allowance_and_a_task_one(self):
        from tftrack import allowance
        tasks = self.store.tasks()
        whole = {"client": "HTB", "weekly_hours": 6, "allowance": True, "allowance_start": "2026-09"}
        allowance.validate(self.conf["limits"] + [{"task": "Build", "weekly_hours": 2, "allowance": True,
                                                   "allowance_start": "2026-09"}], tasks)   # two tasks: fine
        allowance.validate(self.conf["limits"] + [{"client": "HTB", "weekly_hours": 20}], tasks)  # a plain limit: fine
        with self.assertRaisesRegex(ValueError, "HTB has an allowance for all its work and one for Justin"):
            allowance.validate(self.conf["limits"] + [whole], tasks)
        with self.assertRaisesRegex(ValueError, "more than one"):
            allowance.validate(self.conf["limits"] * 2, tasks)
        # if both ever arrive (from another Mac, say), the whole-client one is used alone
        both = dict(self.conf, limits=self.conf["limits"] + [whole])
        self.assertEqual([a["scope"] for a in allowance.for_client(self.store, both, "htb")], ["client"])

    def test_ledger_of_carried_hours_month_by_month(self):
        from datetime import date
        from tftrack import allowance, web
        self.conf["clients"]["HTB"]["rate"] = 40
        self.store.set_rolled(2, "carried", "2026-09-04")
        self.store.set_carry_added("HTB", "2026-10", 30 * 60)
        self.store.set_invoiced("HTB", "2026-09", 3 * 3600, 120.0)
        (book,) = allowance.ledger(self.store, self.conf, date(2026, 11, 12), set())
        self.assertEqual((book["client"], book["name"], book["balance_s"], book["balance_value"]),
                         ("HTB", "Justin", 90 * 60, 60.0))
        rows = {r["month"]: r for r in book["rows"]}
        self.assertEqual(list(rows), ["2026-09", "2026-10", "2026-11"])
        self.assertEqual([(r["worked_s"], r["rolled_s"], r["added_s"], r["balance_s"], r["invoiced"])
                          for r in book["rows"]],
                         [(5 * 3600, 2 * 3600, 0, 2 * 3600, True), (3600, 0, 30 * 60, 90 * 60, False),
                          (0, 0, 0, 90 * 60, False)])
        # the ledger and the timesheets agree
        self.assertEqual(self._inv(11)["carry"]["remaining_s"], book["balance_s"])
        page = web.ledger_page([book], date(2026, 11, 12))
        for text in ("HTB – Justin", "September 2026", "1h 30m", "£60.00", "As at 12/11/2026", "/invoice?client=HTB&month=2026-10"):
            self.assertIn(text, page)
        self.assertIn("No monthly allowances yet", web.ledger_page([], date(2026, 11, 12)))

    def test_older_carried_hours_table_is_moved_across(self):
        import sqlite3
        path = Path(tempfile.mkdtemp()) / "old.sqlite"
        db = sqlite3.connect(str(path))
        db.execute("CREATE TABLE carry_added (client TEXT NOT NULL, month TEXT NOT NULL, seconds INTEGER NOT NULL, "
                   "PRIMARY KEY (client, month))")
        db.execute("INSERT INTO carry_added VALUES ('HTB', '2026-10', 2700)")
        db.commit()
        db.close()
        store = Store(path)
        self.assertEqual(store.carry_rows("htb"), [{"month": "2026-10", "task": "", "seconds": 2700}])
        store.set_carry_added("HTB", "2026-10", 900, "Build")   # a second allowance in the same month now fits
        self.assertEqual(len(Store(path).carry_rows("HTB")), 2)

    def test_allowance_is_a_ticked_task_limit(self):
        from tftrack import allowance, config, web
        self.assertEqual(allowance.configured(self.conf), [self.a])
        # older configs kept allowances separately: they move onto the task's limit
        old = {"limits": [{"task": "Justin", "daily_hours": 2, "weekly_hours": 8}, {"client": "HTB", "weekly_hours": 20}],
               "allowances": [{"task": "justin", "hours_per_week": 6, "start": "2026-09", "opening_hours": 1.5}]}
        new = config.migrate(old)
        self.assertNotIn("allowances", new)
        self.assertEqual(new["limits"][0], {"task": "Justin", "daily_hours": 2, "weekly_hours": 6.0, "allowance": True,
                                            "allowance_start": "2026-09", "opening_hours": 1.5})
        self.assertEqual(allowance.configured(old)[0]["hours_per_week"], 6.0)
        with self.assertRaises(ValueError):   # an allowance needs hours per week
            web._clean_limit({"client": "HTB", "daily_hours": 6, "allowance": True, "allowance_start": "2026-09"})
        self.assertEqual(web._clean_limit({"client": "HTB", "weekly_hours": 6, "allowance": True,
                                           "allowance_start": "2026-09"})["allowance_start"], "2026-09")
        self.assertEqual(web._clean_limit({"task": "Justin", "weekly_hours": "6", "allowance": True,
                                           "allowance_start": "2026-09", "opening_hours": ""}),
                         {"task": "Justin", "weekly_hours": 6.0, "allowance": True, "allowance_start": "2026-09",
                          "opening_hours": 0.0})

    def test_bank_holidays_download_and_copy(self):
        from unittest import mock
        from tftrack import allowance, config
        data = {"england-and-wales": {"division": "england-and-wales",
                                      "events": [{"title": "Christmas Day", "date": "2026-12-25"}]}}

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): pass

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, "APP_DIR", Path(tmp)), \
                mock.patch.object(allowance, "_last_try", 0.0), \
                mock.patch("urllib.request.urlopen", return_value=Resp(json.dumps(data).encode())):
            days, updated = allowance.bank_holidays()
            self.assertEqual({d.isoformat() for d in days}, {"2026-12-25"})
            self.assertTrue((Path(tmp) / "bank-holidays.json").exists())
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, "APP_DIR", Path(tmp)):
            self.assertEqual(allowance.bank_holidays(fetch=False), (None, None))



class PdfTest(unittest.TestCase):
    def _check(self, raw: bytes) -> str:
        """A well-formed PDF: every xref offset points at its object. Returns the pages' text operators."""
        import re
        import zlib
        self.assertTrue(raw.startswith(b"%PDF-1.4"))
        xref = int(raw.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
        self.assertTrue(raw[xref:].startswith(b"xref"))
        offsets = [int(l[:10]) for l in raw[xref:].split(b"\n")[3:] if re.match(rb"^\d{10} 00000 n", l)]
        for n, off in enumerate(offsets, 1):
            self.assertTrue(raw[off:].startswith(f"{n} 0 obj".encode()), n)
        streams = re.findall(rb"stream\n(.*?)\nendstream", raw, re.S)
        return "\n".join(zlib.decompress(s).decode("latin-1") for s in streams)

    def test_invoice_and_timesheet_pdfs(self):
        from tftrack import pdf
        from tftrack.reports import invoice, timesheet_rows
        from datetime import date
        at = lambda d, h: datetime(2026, 9, d, h).astimezone().astimezone(UTC).isoformat()
        tasks = [{"id": 1, "name": "Build (phase 2)", "tag": "HTB"}]
        store = make_store([{"id": n, "taskId": 1, "startedAt": at(1 + n % 28, 8 + n % 9), "duration": 1500}
                            for n in range(1, 90)], tasks=tasks)
        store.set_excluded(5, True)
        inv = invoice(store, "HTB", 2026, 9, "day", 15, "up")
        text = self._check(pdf.invoice_pdf(inv))
        self.assertIn("Page 2 of", text)                        # long months flow over pages
        self.assertIn("(Total for September 2026)", text)
        self.assertIn("(Build \\(phase 2\\))", text)            # brackets escaped
        self.assertIn("(not charged)", text)
        rows = timesheet_rows(store, date(2026, 9, 1), date(2026, 9, 30), "HTB")
        text = self._check(pdf.timesheet_pdf(rows, date(2026, 9, 1), date(2026, 9, 30), "HTB"))
        self.assertIn("(Timesheet: HTB)", text)
        self.assertEqual(pdf.text_width("Hello", 10), 22.78)   # Adobe Helvetica metrics
        self.assertEqual(pdf.fit("A very long task name", 50, 9)[-1], "…")


class SmoothWeeksTest(unittest.TestCase):
    def test_spreads_the_total_evenly_in_blocks(self):
        from tftrack.reports import invoice_lines, smooth_weeks
        h = 3600
        # FCY September 2026's real weeks: 4.00, 2.75, 3.50, 13.00 = 23.25 h
        inv = {"client": "FCY", "style": "week", "month": "2026-09", "block_minutes": 15,
               "groups": [{"label": f"Week commencing Mon {d} Sep 2026", "billed_s": int(x * h), "days": []}
                          for d, x in (("07", 4), ("14", 2.75), ("21", 3.5), ("28", 13))]}
        self.assertEqual([s / h for s in smooth_weeks(inv)], [5.75, 5.75, 5.75, 6.0])   # extra block on the busiest
        lines = invoice_lines(inv, 26.5, smooth=True)
        self.assertEqual(sum(l["hours"] for l in lines), 23.25)                          # same total as the timesheet
        self.assertEqual([l["hours"] for l in invoice_lines(inv, 26.5)], [4.0, 2.75, 3.5, 13.0])
        # no rounding: whatever isn't a whole block stays on the busiest week, so the total still matches
        inv["groups"][3]["billed_s"] += 7 * 60
        self.assertEqual(sum(smooth_weeks(inv)), int(23.25 * h) + 7 * 60)


class OutstandingTest(unittest.TestCase):
    def test_to_invoice_and_carried_per_client(self):
        from datetime import date
        from unittest import mock
        from tftrack import web, allowance
        at = lambda d, h: datetime(2026, 10, d, h).astimezone().astimezone(UTC).isoformat()
        tasks = [{"id": 1, "name": "Justin", "tag": "HTB"}, {"id": 2, "name": "Prog", "tag": "FCY"},
                 {"id": 3, "name": "Dev", "tag": "FYPTD"}]
        store = make_store([
            {"id": 1, "taskId": 1, "startedAt": at(5, 9), "duration": 2 * 3600},
            {"id": 2, "taskId": 1, "startedAt": at(6, 9), "duration": 3600},
            {"id": 3, "taskId": 2, "startedAt": at(7, 9), "duration": 50 * 60},
            {"id": 4, "taskId": 3, "startedAt": at(7, 12), "duration": 3600},   # own business: not invoiced
        ], tasks=tasks)
        store.set_rolled(2, "carried", "2026-10-06")
        conf = {"clients": {"HTB": {"billing": "day", "rounding": "up", "block_minutes": 15, "rate": 40},
                            "FCY": {"billing": "week", "rounding": "up", "block_minutes": 15, "rate": 26.5},
                            "FYPTD": {"billing": "hourly", "rate": 50, "notional": True}},
                "limits": [{"task": "Justin", "weekly_hours": 6, "allowance": True, "allowance_start": "2026-10"}]}
        with mock.patch.object(web.service, "open_store", return_value=store), \
                mock.patch.object(web.cfg, "load_config", return_value=conf), \
                mock.patch.object(allowance, "bank_holidays", return_value=(set(), None)):
            o = web.outstanding(date(2026, 10, 23))
        by = {c["client"]: c for c in o["clients"]}
        self.assertEqual(sorted(by), ["FCY", "HTB"])
        self.assertEqual((by["HTB"]["due_s"], by["HTB"]["due_value"]), (2 * 3600, 80.0))
        self.assertEqual((by["HTB"]["carried_s"], by["HTB"]["carried_value"]), (3600, 40.0))
        self.assertEqual((by["FCY"]["due_s"], by["FCY"]["due_value"]), (3600, 26.5))   # 50m rounds up to 1h
        self.assertEqual((o["due_value"], o["carried_value"]), (106.5, 40.0))
        self.assertEqual([m["month"] for m in by["HTB"]["months"]], ["2026-10"])
        # in November, October shows as not invoiced until it's marked
        with mock.patch.object(web.service, "open_store", return_value=store), \
                mock.patch.object(web.cfg, "load_config", return_value=conf), \
                mock.patch.object(allowance, "bank_holidays", return_value=(set(), None)):
            nov = {c["client"]: c for c in web.outstanding(date(2026, 11, 5))["clients"]}
            self.assertEqual([(m["month"], m["current"]) for m in nov["HTB"]["months"]], [("2026-10", False)])
            secs, amount, _ = web._month_due(store, conf, "HTB", "2026-10")
            self.assertEqual((secs, amount), (2 * 3600, 80.0))
            store.set_invoiced("HTB", "2026-10", secs, amount)
            nov = {c["client"]: c for c in web.outstanding(date(2026, 11, 5))["clients"]}
            self.assertEqual(nov["HTB"]["months"], [])          # invoiced: gone, but carried hours still show
            self.assertEqual(nov["HTB"]["carried_s"], 3600)
            self.assertEqual(store.invoiced("htb")["2026-10"]["amount"], 80.0)
            store.set_invoiced("HTB", "2026-10", None)
            self.assertEqual(store.invoiced("HTB"), {})


class RemoteTest(unittest.TestCase):
    def test_tailscale_serve_and_allowed_hosts(self):
        import os, stat
        from unittest import mock
        from tftrack import config, remote
        with tempfile.TemporaryDirectory() as tmp:
            # A stand-in for the Tailscale command line: answers status, then serve status/serve.
            fake = Path(tmp) / "tailscale"
            log = Path(tmp) / "calls"
            fake.write_text(f"""#!/bin/sh
echo "$@" >> {log}
case "$1 $2" in
  "status --json") echo '{{"Self": {{"DNSName": "daves-mac.tail1234.ts.net."}}}}' ;;
  "serve status") [ -f {tmp}/on ] && echo '{{"Web": {{"daves-mac.tail1234.ts.net:8765": {{"Handlers": {{"/": {{"Proxy": "http://127.0.0.1:8765"}}}}}}}}}}' || echo '{{}}' ;;
  "serve --bg") touch {tmp}/on ;;
  "serve --https=8765") rm -f {tmp}/on ;;
esac
""")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            with mock.patch.object(config, "APP_DIR", Path(tmp)), mock.patch.object(config, "CONFIG_PATH", Path(tmp) / "c.json"), \
                    mock.patch.object(remote, "cli", return_value=str(fake)):
                st = remote.status()
                self.assertEqual((st["running"], st["serving"], st["name"]), (True, False, "daves-mac.tail1234.ts.net"))
                self.assertEqual(remote.enable(), {"url": "https://daves-mac.tail1234.ts.net:8765/"})
                self.assertIn("serve --bg --https=8765 http://127.0.0.1:8765", log.read_text())
                self.assertTrue(remote.status()["serving"])
                hosts = remote.allowed_hosts()
                self.assertIn("daves-mac.tail1234.ts.net:8765", hosts)
                self.assertIn("127.0.0.1:8765", hosts)
                self.assertNotIn("evil.example.com:8765", hosts)
                remote.disable()
                self.assertFalse(remote.status()["serving"])
            with mock.patch.object(remote, "cli", return_value=None):
                self.assertFalse(remote.status()["installed"])

    def test_approval_link_and_bad_viewer_address(self):
        from unittest import mock
        from tftrack import remote
        out = "Serve is not enabled on your tailnet.\nTo enable, visit:\n\n         https://login.tailscale.com/f/serve?node=abc123\n"
        with mock.patch.object(remote, "dns_name", return_value="m.tail1.ts.net"), \
                mock.patch.object(remote, "_run", return_value=(124, out)):
            self.assertEqual(remote.enable()["needs_approval"], "https://login.tailscale.com/f/serve?node=abc123")
        with self.assertRaises(ValueError):
            remote.check_main("http://192.168.1.5:8765/")   # only a Tailscale https address


class MergeTest(unittest.TestCase):
    def test_changes_from_another_mac_are_added_not_overwritten(self):
        from tftrack import merge
        at = lambda d, h: datetime(2026, 10, d, h).astimezone().astimezone(UTC).isoformat()
        tasks = [{"id": 1, "name": "Justin", "tag": "HTB"}]
        intervals = [{"id": n, "taskId": 1, "startedAt": at(n, 9), "duration": 3600} for n in range(1, 6)]
        other, mine = make_store(intervals, tasks=tasks), make_store(intervals, tasks=tasks)
        # On the other Mac
        other.set_excluded(1, True)
        other.set_adjustment(2, datetime(2026, 10, 2, 10).astimezone(), 1800, 1)
        other.set_adjustment(3, datetime(2026, 10, 3, 10).astimezone(), 1800, 1)
        added = other.add_manual(datetime(2026, 10, 20, 9).astimezone(), 900, 1)
        other.set_rolled(added, "carried", "2026-10-20")
        other.set_invoiced("HTB", "2026-09", 3600, 40.0)
        other.set_carry_added("HTB", "2026-10", 1800)
        other_conf = {"clients": {"HTB": {"billing": "day", "rate": 40}, "FCY": {"billing": "week", "rate": 26.5}},
                      "monthly_income": {"FYPT1": {"2026-09": 600}}, "smooth_weeks": ["FCY"],
                      "limits": [{"task": "Justin", "weekly_hours": 6, "allowance": True, "allowance_start": "2026-09"}]}
        # On this Mac: one thing done differently, plus its own manual entry
        mine.set_adjustment(3, datetime(2026, 10, 3, 11).astimezone(), 2700, 1)
        mine_conf = {"clients": {"HTB": {"billing": "day", "rate": 45}}, "limits": []}
        data = json.loads(json.dumps(merge.export(other, other_conf)))   # as it would travel in the file
        conf, report = merge.import_changes(mine, mine_conf, data)
        text = " ".join(report)
        self.assertIn("1 added entry", text)
        self.assertIn("1 exclusion", text)
        self.assertIn("1 edited entry", text)
        self.assertIn("entry 3 was edited differently", text)
        self.assertIn("invoicing settings for HTB differ", text)
        self.assertEqual(conf["clients"]["HTB"]["rate"], 45)           # this Mac's kept
        self.assertEqual(conf["clients"]["FCY"]["rate"], 26.5)         # missing one added
        self.assertEqual(conf["monthly_income"]["FYPT1"]["2026-09"], 600)
        self.assertEqual(conf["smooth_weeks"], ["FCY"])
        self.assertEqual(conf["limits"][0]["task"], "Justin")
        self.assertIn(1, mine.excluded_ids())
        self.assertEqual(mine.invoiced("HTB")["2026-09"]["amount"], 40.0)
        self.assertEqual(mine.carry_rows("HTB"), [{"month": "2026-10", "task": "", "seconds": 1800}])
        rolled = list(mine.rolled())
        self.assertEqual(len(rolled), 1)
        # the rolled-over added entry points at this Mac's own copy of it
        row = mine.db.execute("SELECT started_at FROM manual_entry WHERE id=?", (-rolled[0] - 10**12,)).fetchone()
        self.assertIsNotNone(row)
        # importing the same file again adds nothing
        _, again = merge.import_changes(mine, conf, data)
        self.assertFalse(any(r.startswith(("1 added", "1 exclusion")) for r in again))
        with self.assertRaises(ValueError):
            merge.import_changes(mine, conf, {"hello": 1})


class ViewerTest(unittest.TestCase):
    def test_viewer_skips_sync_and_redirects(self):
        from unittest import mock
        from tftrack import service, updater
        store = make_store([])
        with mock.patch.object(service, "sync") as sync, mock.patch.object(updater, "maybe_auto_update", return_value=None):
            service.run_check({"viewer_of": "https://m.tail1.ts.net:8765/"}, store)
            sync.assert_not_called()


class CollectTest(unittest.TestCase):
    def test_prompts_for_money_to_collect(self):
        from datetime import date
        from unittest import mock
        from tftrack import web, allowance
        at = lambda mo, d, h: datetime(2026, mo, d, h).astimezone().astimezone(UTC).isoformat()
        tasks = [{"id": 1, "name": "Justin", "tag": "HTB"}, {"id": 2, "name": "Prog", "tag": "FCY"}]
        store = make_store([
            {"id": 1, "taskId": 1, "startedAt": at(9, 7, 9), "duration": 3 * 3600},    # September, HTB
            {"id": 2, "taskId": 1, "startedAt": at(10, 5, 9), "duration": 2 * 3600},   # October, HTB
            {"id": 3, "taskId": 1, "startedAt": at(10, 6, 9), "duration": 3600},       # October, HTB, excluded
            {"id": 4, "taskId": 2, "startedAt": at(10, 7, 9), "duration": 3600},       # October, FCY
            {"id": 5, "taskId": 1, "startedAt": at(11, 2, 9), "duration": 3600},       # Mon 2 Nov
            {"id": 6, "taskId": 1, "startedAt": at(11, 4, 9), "duration": 3600},       # Wed 4 Nov: Tue 3 is a gap
        ], tasks=tasks)
        store.set_excluded(3, True)
        conf = {"clients": {"HTB": {"billing": "day", "rounding": "none", "block_minutes": 15, "rate": 40},
                            "FCY": {"billing": "hourly"}},
                "payment_terms_days": 14}
        patches = (mock.patch.object(web.service, "open_store", return_value=store),
                   mock.patch.object(web.cfg, "load_config", return_value=conf),
                   mock.patch.object(allowance, "bank_holidays", return_value=(set(), None)))
        with patches[0], patches[1], patches[2]:
            # September invoiced 40 days ago and never paid; October not invoiced yet
            store.set_invoiced("HTB", "2026-09", 3 * 3600, 120.0)
            store.db.execute("UPDATE invoiced SET marked_at = ?", (datetime(2026, 9, 26, 12, tzinfo=UTC).isoformat(),))
            store.db.commit()
            c = web.money_to_collect(date(2026, 11, 5))
            by = {p["key"]: p for p in c["prompts"]}
            self.assertEqual((c["to_invoice"], c["overdue"], c["awaiting"]), (80.0, 120.0, 0.0))
            self.assertEqual(by["invoice:htb:2026-10"]["value"], 80.0)
            self.assertEqual(by["unpaid:htb:2026-09"]["level"], "critical")
            self.assertIn("26 days overdue", by["unpaid:htb:2026-09"]["title"])      # due 10/10/2026
            self.assertEqual(by["excluded:htb:2026-10:3600"]["value"], 40.0)
            self.assertIn("rate:fcy", by)
            self.assertIn("Tue 03/11", by["gap:2026-11-03"]["detail"])
            self.assertEqual(c["prompts"][0]["level"], "critical")                    # most urgent first
            # hiding a judgement call; it comes back if the figure changes
            store.set_dismissed("excluded:htb:2026-10:3600")
            c = web.money_to_collect(date(2026, 11, 5))
            self.assertNotIn("excluded:htb:2026-10:3600", {p["key"] for p in c["prompts"]})
            self.assertEqual(c["hidden"], 1)
            # paid: no longer chased; time added after invoicing is flagged with what it's worth
            store.set_paid("HTB", "2026-09")
            store.add_manual(datetime(2026, 9, 8, 9).astimezone(), 1800, 1)
            c = web.money_to_collect(date(2026, 11, 5))
            by = {p["key"]: p for p in c["prompts"]}
            self.assertEqual(c["overdue"], 0.0)
            self.assertNotIn("unpaid:htb:2026-09", by)
            self.assertEqual(by[f"changed:htb:2026-09:{3 * 3600 + 1800}"]["value"], 20.0)
            # un-invoicing clears the paid mark
            store.set_invoiced("HTB", "2026-09", None)
            self.assertEqual(store.paid("HTB"), {})
            store.set_invoiced("HTB", "2026-09", 3600, 40.0)
            self.assertEqual(store.mark_all_paid(), 1)
            self.assertIn("2026-09", store.paid("htb"))


class BillingTest(unittest.TestCase):
    def test_create_number_vat_void_and_pdf(self):
        from datetime import date
        from unittest import mock
        from tftrack import web, allowance, billing, collect
        at = lambda d, h: datetime(2026, 10, d, h).astimezone().astimezone(UTC).isoformat()
        tasks = [{"id": 1, "name": "Justin", "tag": "HTB"}, {"id": 2, "name": "Prog", "tag": "FCY"}]
        store = make_store([
            {"id": 1, "taskId": 1, "startedAt": at(5, 9), "duration": 2 * 3600},
            {"id": 2, "taskId": 1, "startedAt": at(6, 9), "duration": 3600},
            {"id": 3, "taskId": 2, "startedAt": at(7, 9), "duration": 90 * 60},
        ], tasks=tasks)
        conf = {"clients": {"HTB": {"billing": "day", "rounding": "none", "block_minutes": 15, "rate": 40},
                            "FCY": {"billing": "hourly", "rate": 30}}}
        saved = {}
        with mock.patch.object(web.service, "open_store", return_value=store), \
                mock.patch.object(web.cfg, "load_config", side_effect=lambda: dict(conf)), \
                mock.patch.object(web.cfg, "save_config", side_effect=lambda c: (conf.clear(), conf.update(c), saved.update(c))), \
                mock.patch.object(allowance, "bank_holidays", return_value=(set(), None)):
            today = date(2026, 11, 5)
            with self.assertRaisesRegex(ValueError, "your business name"):
                web._create_bill("HTB", "2026-10", today)
            self.assertEqual(web.bills_overview(today)["missing"],
                             ["your business name", "your business address", "how clients pay you"])
            # two businesses, each with its own numbering; FCY is invoiced by the second
            conf["businesses"] = billing.clean_businesses([
                {"name": "Me Ltd", "address": "1 Road\nTown", "payment": "Sort 00-00-00", "vat_registered": True,
                 "vat_number": "GB1", "next_number": 7},
                {"name": "Other Co", "address": "2 Lane", "payment": "Sort 11-11-11", "next_number": 7}])
            self.assertEqual([b["id"] for b in conf["businesses"]], ["b1", "b2"])
            conf["client_details"] = {"htb": billing.clean_client_details({"name": "HTB Ltd", "terms_days": "14"}),
                                      "FCY": billing.clean_client_details({"name": "FCY Ltd", "business": "b2"})}
            # extras go on the invoice after the time: mileage and an expense
            store.add_extra("HTB", "2026-10", "mileage", "Mileage to site", 40, 0.45)
            store.add_extra("HTB", "2026-10", "expense", "Parking", 1, 6.0)
            self.assertEqual(web._month_due(store, conf, "HTB", "2026-10")[1], 144.0)
            bill = web._create_bill("HTB", "2026-10", today)
            self.assertEqual((bill["number"], bill["net"], bill["vat"], bill["total"]), ("INV-0007", 144.0, 28.8, 172.8))
            self.assertEqual([l["amount"] for l in bill["data"]["lines"]], [80.0, 40.0, 18.0, 6.0])
            self.assertEqual(store.extras("HTB", "2026-10"), [])                                   # used up
            self.assertEqual((bill["issued_on"], bill["due_on"]), ("2026-11-05", "2026-11-19"))   # the client's own 14 days
            self.assertEqual([b["next_number"] for b in saved["businesses"]], [8, 7])
            self.assertEqual(store.invoiced("HTB")["2026-10"]["amount"], 144.0)                    # month marked invoiced
            with self.assertRaisesRegex(ValueError, "already has invoice INV-0007"):
                web._create_bill("HTB", "2026-10", today)
            # an hourly client: one line per task, usual terms. Its business has the same number, with no VAT
            other = web._create_bill("FCY", "2026-10", today)
            self.assertEqual((other["number"], other["net"], other["total"], other["due_on"]),
                             ("INV-0007", 45.0, 45.0, "2026-12-05"))
            self.assertEqual(other["data"]["business"]["name"], "Other Co")
            # the invoice is a fixed copy: later changes to Settings don't touch it
            conf["businesses"][0]["name"] = "Renamed"
            self.assertEqual(store.bill(bill["id"])["data"]["business"]["name"], "Me Ltd")
            raw = billing.bill_pdf(store.bill(bill["id"]))
            self.assertTrue(raw.startswith(b"%PDF-1.4") and raw.rstrip().endswith(b"%%EOF"))
            self.assertIn("INV-0007", billing.bill_page(store.bill(bill["id"]), None, today))
            # Money to collect chases it by its own due date and total
            late = web.money_to_collect(date(2026, 11, 25))
            chase = next(p for p in late["prompts"] if p["key"] == "unpaid:htb:2026-10")
            self.assertEqual((chase["level"], chase["value"], chase["number"]), ("critical", 172.8, "INV-0007"))
            self.assertIn("6 days overdue", chase["title"])
            o = web.bills_overview(date(2026, 11, 25))
            self.assertEqual((o["overdue"], o["awaiting"], o["ready"], o["several"]), (172.8, 45.0, [], True))
            # voiding frees the month and its extras, but never the number
            store.void_bill(bill["id"])
            self.assertEqual(store.invoiced("HTB"), {})
            self.assertIsNone(store.bill_for("HTB", "2026-10"))
            self.assertEqual(len(store.extras("HTB", "2026-10")), 2)
            again = web._create_bill("HTB", "2026-10", today)
            self.assertEqual((again["number"], again["net"]), ("INV-0008", 144.0))
            self.assertEqual(sorted(b["status"] for b in web.bills_overview(today)["bills"]), ["awaiting", "awaiting", "void"])

    def test_version_30_invoices_and_business_carry_over(self):
        import sqlite3
        from tftrack import billing
        tmp = Path(tempfile.mkdtemp()) / "t.sqlite"
        db = sqlite3.connect(str(tmp))
        db.execute("""CREATE TABLE bill (number TEXT PRIMARY KEY, seq INTEGER NOT NULL, client TEXT NOT NULL, month TEXT NOT NULL,
                      issued_on TEXT NOT NULL, due_on TEXT NOT NULL, seconds INTEGER NOT NULL, net REAL NOT NULL, vat REAL NOT NULL,
                      total REAL NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL, voided_at TEXT)""")
        db.execute("INSERT INTO bill VALUES ('INV-0001', 1, 'HTB', '2026-10', '2026-11-01', '2026-12-01', 3600, 40, 0, 40, '{}', 'x', NULL)")
        db.commit()
        db.close()
        store = Store(tmp)
        store = Store(tmp)   # opening twice must not migrate twice
        [b] = store.bills()
        self.assertEqual((b["business_id"], b["number"], b["id"]), ("b1", "INV-0001", 1))
        self.assertTrue(store.number_used("b1", "INV-0001"))
        old = {"business": {"name": "Me", "next_number": 2}}
        self.assertEqual([(x["id"], x["name"], x["next_number"]) for x in billing.businesses(old)], [("b1", "Me", 2)])

    def test_long_invoice_runs_onto_a_second_page(self):
        from tftrack import billing
        lines = [{"description": f"Line {i}", "hours": 1.0, "rate": 10.0, "amount": 10.0} for i in range(60)]
        bill = {"number": "INV-1", "client": "X", "month": "2026-10", "issued_on": "2026-11-01", "due_on": "2026-12-01",
                "net": 600.0, "vat": 0.0, "total": 600.0,
                "data": {"lines": lines, "vat_rate": None, "terms_days": 30,
                         "business": {"name": "Me", "address": "A", "email": "", "phone": "", "company_number": "",
                                      "vat_number": "", "payment": "Pay me", "footer": "Thanks"},
                         "client": {"name": "X Ltd", "address": "", "email": "", "reference": "PO1"}}}
        self.assertIn(b"/Count 2", billing.bill_pdf(bill))


class InsightsTest(unittest.TestCase):
    def test_months_clients_and_notes(self):
        from datetime import date
        from tftrack import insights
        at = lambda mo, d, h: datetime(2026, mo, d, h).astimezone().astimezone(UTC).isoformat()
        tasks = [{"id": 1, "name": "A", "tag": "Low"}, {"id": 2, "name": "B", "tag": "High"}, {"id": 3, "name": "Admin", "tag": None}]
        iv, n = [], 0
        for mo in (8, 9, 10):
            for task, hours in ((1, 10), (2, 5), (3, 5)):
                n += 1
                iv.append({"id": n, "taskId": task, "startedAt": at(mo, 3 + task, 8), "duration": hours * 3600})
        iv.append({"id": 99, "taskId": 2, "startedAt": at(11, 3, 9), "duration": 2 * 3600})
        store = make_store(iv, tasks=tasks)
        conf = {"clients": {"Low": {"billing": "hourly", "rate": 20}, "High": {"billing": "hourly", "rate": 80}}}
        d = insights.build(store, conf, date(2026, 11, 12))
        self.assertEqual([(m["month"], m["earned"]) for m in d["months"]],
                         [("2026-08", 600.0), ("2026-09", 600.0), ("2026-10", 600.0), ("2026-11", 160.0)])   # empty months trimmed
        self.assertEqual([(c["client"], c["amount"], c["rate"]) for c in d["clients"]],
                         [("High", 1200.0, 80.0), ("Low", 600.0, 20.0)])
        k = d["kpis"]
        self.assertEqual((k["recent_avg"], k["this_month"], k["avg_rate"], k["tax_year"]), (600.0, 160.0, 40.0, 1960.0))
        kinds = {x["kind"]: x for x in d["notes"]}
        self.assertIn("Low pays the least", kinds["rate"]["title"])
        self.assertIn("£200.00 more a month", kinds["rate"]["text"])        # 10h a month at £20 below the £40 average
        self.assertIn("25% of your tracked time", kinds["unpaid"]["title"])  # 5h of 20h a month is admin
        self.assertEqual(d["weekdays"][0]["day"], "Mon")


class MailerTest(unittest.TestCase):
    def test_draft_passes_everything_as_arguments(self):
        import subprocess
        from unittest import mock
        from tftrack import mailer
        home = Path(tempfile.mkdtemp())
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(mailer.cfg, "APP_DIR", home), \
                mock.patch.object(mailer.shutil, "which", return_value="/usr/bin/osascript"), \
                mock.patch.object(mailer.subprocess, "run", return_value=ok) as run:
            mailer.draft('Invoice "1"', "Hi\nthere", "a@example.com", "me@example.com", [("INV 1.pdf", b"%PDF"), ("T/S.pdf", b"x")], 7)
        args = run.call_args[0][0]
        self.assertEqual(args[:2], ["osascript", "-e"])
        self.assertNotIn("a@example.com", args[2])                       # the script text holds no user data
        self.assertEqual(args[3:7], ['Invoice "1"', "Hi\nthere", "a@example.com", "me@example.com"])
        self.assertEqual([Path(p).name for p in args[7:]], ["INV 1.pdf", "T-S.pdf"])
        self.assertEqual(Path(args[7]).read_bytes(), b"%PDF")
        self.assertEqual(Path(args[7]).parent, home / "outbox" / "7")
        denied = subprocess.CompletedProcess([], 1, "", "execution error: Not authorized to send Apple events to Mail. (-1743)")
        with mock.patch.object(mailer.cfg, "APP_DIR", home), \
                mock.patch.object(mailer.shutil, "which", return_value="/usr/bin/osascript"), \
                mock.patch.object(mailer.subprocess, "run", return_value=denied):
            with self.assertRaisesRegex(ValueError, "Automation"):
                mailer.draft("s", "b", "", "", [], 7)
        with mock.patch.object(mailer.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "needs Apple Mail"):
                mailer.draft("s", "b", "", "", [], 7)


class SmtpTest(unittest.TestCase):
    def test_sends_through_a_mail_server_with_attachments_and_a_copy(self):
        import socketserver
        from email import message_from_bytes
        from tftrack import billing, mailer
        got = {}

        class Fake(socketserver.StreamRequestHandler):   # just enough SMTP to accept one message
            def handle(self):
                self.wfile.write(b"220 fake\r\n")
                while True:
                    line = self.rfile.readline()
                    cmd = line[:4].upper()
                    if cmd in (b"EHLO", b"HELO"):
                        self.wfile.write(b"250 fake\r\n")
                    elif cmd == b"RCPT":
                        got.setdefault("rcpt", []).append(line.decode().strip())
                        self.wfile.write(b"250 ok\r\n")
                    elif cmd == b"DATA":
                        self.wfile.write(b"354 go\r\n")
                        data = b""
                        while not data.endswith(b"\r\n.\r\n"):
                            data += self.rfile.readline()
                        got["data"] = data
                        self.wfile.write(b"250 ok\r\n")
                    elif cmd == b"QUIT" or not line:
                        self.wfile.write(b"221 bye\r\n")
                        return
                    else:
                        self.wfile.write(b"250 ok\r\n")

        server = socketserver.TCPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            b = billing.clean_business({"name": "Me Ltd", "email": "me@example.com", "mail_method": "smtp",
                                        "smtp_host": "127.0.0.1", "smtp_port": server.server_address[1], "smtp_security": "none"})
            mailer.send(b, None, "client@example.com", "Invoice INV-1 from Me Ltd", "Hi\n\nAttached.", [("INV-1.pdf", b"%PDF-1.4 x")])
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(len(got["rcpt"]), 2)                                  # the client, and a copy to the sender
        self.assertTrue(any("client@example.com" in r for r in got["rcpt"]) and any("me@example.com" in r for r in got["rcpt"]))
        msg = message_from_bytes(got["data"])
        self.assertEqual((msg["Subject"], msg["To"]), ("Invoice INV-1 from Me Ltd", "client@example.com"))
        self.assertIsNone(msg["Bcc"])                                           # the copy isn't visible to the client
        files = [p for p in msg.walk() if p.get_filename()]
        self.assertEqual([(p.get_filename(), p.get_payload(decode=True)) for p in files], [("INV-1.pdf", b"%PDF-1.4 x")])
        with self.assertRaisesRegex(ValueError, "no email address for this client"):
            mailer.send(b, None, "", "s", "b", [])
        with self.assertRaisesRegex(ValueError, "outgoing mail server"):
            billing.clean_business({"name": "X", "email": "x@example.com", "mail_method": "smtp"})
        with self.assertRaisesRegex(ValueError, "Couldn't send"):
            mailer.send({**b, "smtp_port": 1}, None, "c@example.com", "s", "b", [])


class AreasTest(unittest.TestCase):
    def test_cards_page_and_hours_in_lieu(self):
        from datetime import date
        from tftrack import areas
        at = lambda mo, d, h: datetime(2026, mo, d, h).astimezone().astimezone(UTC).isoformat()
        tasks = [{"id": 1, "name": "Coaching", "tag": "FYPT1"}, {"id": 2, "name": "Admin", "tag": "FYPT1"},
                 {"id": 3, "name": "Justin", "tag": "HTB"}]
        store = make_store([
            {"id": 1, "taskId": 1, "startedAt": at(11, 9, 9), "duration": 2 * 3600},    # Mon 9 Nov: this week
            {"id": 2, "taskId": 2, "startedAt": at(11, 3, 9), "duration": 3600},        # Tue 3 Nov: last week
            {"id": 3, "taskId": 1, "startedAt": at(10, 20, 9), "duration": 4 * 3600},   # October
            {"id": 4, "taskId": 3, "startedAt": at(11, 10, 9), "duration": 3600},
        ], tasks=tasks)
        today = date(2026, 11, 12)                                                      # a Thursday
        conf = {"clients": {"HTB": {"billing": "day", "rate": 40}}}
        cards = {c["client"]: c for c in areas.cards(store, conf, today)}
        self.assertEqual(cards["FYPT1"]["label"], "FYPT Clients")                       # friendly name until renamed
        self.assertEqual((cards["FYPT1"]["week_seconds"], cards["FYPT1"]["month_seconds"]), (2 * 3600, 3 * 3600))
        conf["areas"] = areas.clean([{"client": "htb", "label": "Hit The Books"}, {"client": "nope"},
                                     {"client": "HTB", "label": "twice"}], ["FYPT1", "HTB"])
        self.assertEqual(conf["areas"], [{"client": "HTB", "label": "Hit The Books"}])
        d = areas.detail(store, conf, "fypt1", today)
        self.assertEqual((d["week_seconds"], d["month_seconds"], d["task_order"]), (2 * 3600, 3 * 3600, ["Coaching", "Admin"]))
        self.assertEqual((len(d["series"]["day"]), len(d["series"]["week"]), len(d["series"]["month"])), (28, 12, 12))
        self.assertEqual([b["seconds"] for b in d["series"]["week"][-2:]], [3600, 2 * 3600])
        self.assertEqual([b["seconds"] for b in d["series"]["month"][-2:]], [4 * 3600, 3 * 3600])
        self.assertEqual(d["series"]["week"][-1]["tasks"], {"Coaching": 2 * 3600})
        # hours in lieu: bank by hand, then add to a week as real time
        store.add_lieu("HTB", 3 * 3600, "Extra evening")
        with self.assertRaisesRegex(ValueError, "more than the hours in lieu"):
            areas.use_lieu(store, conf, "HTB", 3, "day", today, 4 * 3600)
        with self.assertRaisesRegex(ValueError, "task from this area"):
            areas.use_lieu(store, conf, "HTB", 1, "day", today, 3600)
        areas.use_lieu(store, conf, "HTB", 3, "week", date(2026, 11, 5), 2 * 3600)      # lands on Mon 2 Nov
        self.assertEqual(store.lieu_balance("htb"), 3600)
        h = areas.detail(store, conf, "HTB", today, carried_s=1800)
        self.assertEqual(h["lieu"]["bank_seconds"], 3600)
        self.assertEqual(h["lieu"]["total_seconds"], 3600 + 1800)                        # the bank plus carried-over time
        self.assertEqual([b["seconds"] for b in h["series"]["week"][-2:]], [2 * 3600, 3600])
        added = [r for r in store.segments(datetime(2026, 11, 2).astimezone(), datetime(2026, 11, 3).astimezone()) if r["manual"]]
        self.assertEqual([(r["task"], r["seconds"]) for r in added], [("Justin", 2 * 3600)])
        store.delete_manual(added[0]["id"])                                              # deleting the entry gives the hours back
        self.assertEqual(store.lieu_balance("HTB"), 3 * 3600)


class AllottedHoursTest(unittest.TestCase):
    def test_behind_on_track_and_done(self):
        from datetime import date
        from tftrack import areas
        ws = date(2026, 11, 9)                                    # a Monday
        h = 3600
        # Thursday: three working days are finished, so 10h a week means 6h by now
        p = areas.week_progress(10, 4 * h, ws, date(2026, 11, 12), set())
        self.assertEqual((p["status"], p["to_go_seconds"], p["behind_seconds"], p["days_left"], p["per_day_seconds"]),
                         ("behind", 6 * h, 2 * h, 2, 3 * h))
        self.assertEqual(areas.week_progress(10, 6 * h, ws, date(2026, 11, 12), set())["status"], "on_track")
        self.assertEqual(areas.week_progress(10, 0, ws, date(2026, 11, 9), set())["status"], "on_track")   # Monday morning
        self.assertEqual(areas.week_progress(10, 10 * h, ws, date(2026, 11, 12), set())["status"], "done")
        self.assertEqual(areas.week_progress(10, 9 * h, ws, date(2026, 11, 14), set())["status"], "behind")  # Saturday, 1h short
        self.assertIsNone(areas.week_progress(None, 5 * h, ws, date(2026, 11, 12), set()))
        # a bank holiday Monday leaves four working days: 10h a week is 2.5h a day
        p = areas.week_progress(10, 2 * h, ws, date(2026, 11, 11), {date(2026, 11, 9)})
        self.assertEqual((p["status"], p["behind_seconds"]), ("behind", round(0.5 * h)))

    def test_where_the_hours_come_from_and_saving_them(self):
        from tftrack import areas
        store = make_store([], tasks=[{"id": 1, "name": "Justin", "tag": "HTB"}, {"id": 2, "name": "Prog", "tag": "FCY"},
                                      {"id": 3, "name": "Dev", "tag": "FYPTD"}])
        conf = {"limits": [{"task": "Justin", "weekly_hours": 6, "allowance": True}, {"client": "FCY", "weekly_hours": 8}],
                "targets": {"hours": {"fcy": {"week": 12, "month": 40}}}}
        self.assertEqual(areas.allotted_hours(conf, store, "HTB"), 6.0)      # its allowance task
        self.assertEqual(areas.allotted_hours(conf, store, "FCY"), 12.0)     # the weekly target wins over the limit
        self.assertIsNone(areas.allotted_hours(conf, store, "FYPTD"))
        known = ["FCY", "FYPTD", "HTB"]
        areas.set_allotted(conf, [{"client": "FYPTD", "week_hours": "5"}, {"client": "FCY", "week_hours": ""},
                                  {"client": "HTB"}], known)
        self.assertEqual(conf["targets"]["hours"]["FYPTD"]["week"], 5.0)
        self.assertEqual(conf["targets"]["hours"]["fcy"], {"day": None, "week": None, "month": 40})   # month target kept
        self.assertEqual(areas.allotted_hours(conf, store, "FCY"), 8.0)      # back to the limit
        with self.assertRaisesRegex(ValueError, "must be a number"):
            areas.set_allotted(conf, [{"client": "HTB", "week_hours": "lots"}], known)


class AreaCarriedTest(unittest.TestCase):
    def test_area_counts_carried_time_from_allowances(self):
        from datetime import date
        from unittest import mock
        from tftrack import web, allowance
        at = lambda d, h: datetime(2026, 10, d, h).astimezone().astimezone(UTC).isoformat()
        store = make_store([{"id": 1, "taskId": 1, "startedAt": at(5, 9), "duration": 2 * 3600},
                            {"id": 2, "taskId": 1, "startedAt": at(6, 9), "duration": 3600}],
                           tasks=[{"id": 1, "name": "Justin", "tag": "HTB"}])
        store.set_rolled(2, "carried", "2026-10-06")
        conf = {"clients": {"HTB": {"billing": "day", "rounding": "up", "block_minutes": 15, "rate": 40}},
                "limits": [{"task": "Justin", "weekly_hours": 6, "allowance": True, "allowance_start": "2026-10"}]}
        with mock.patch.object(web.service, "open_store", return_value=store), \
                mock.patch.object(web.cfg, "load_config", return_value=conf), \
                mock.patch.object(allowance, "bank_holidays", return_value=(set(), None)):
            self.assertEqual(web._carried("HTB", date(2026, 10, 23)), 3600)
            self.assertEqual(web._carried("Nobody", date(2026, 10, 23)), 0)


class AutoBankTest(unittest.TestCase):
    def setUp(self):
        self.at = lambda d, h: datetime(2026, 10, d, h).astimezone().astimezone(UTC).isoformat()
        self.tasks = [{"id": 1, "name": "Prog", "tag": "FCY"}, {"id": 2, "name": "Dev", "tag": "FYPTD"},
                      {"id": 3, "name": "Justin", "tag": "HTB"}, {"id": 4, "name": "Odd jobs", "tag": "Daily"},
                      {"id": 5, "name": "Bits", "tag": "Plain"}]
        self.conf = {
            "clients": {"FCY": {"billing": "week", "rounding": "none", "block_minutes": 15, "rate": 20},
                        "FYPTD": {"billing": "hourly", "rate": 50, "notional": True},
                        "HTB": {"billing": "day", "rounding": "up", "block_minutes": 15, "rate": 40},
                        "Daily": {"billing": "day", "rate": 30}, "Plain": {"billing": "hourly", "rate": 30}},
            "limits": [{"task": "Justin", "weekly_hours": 6, "allowance": True, "allowance_start": "2026-10"}],
            "targets": {"hours": {"FCY": {"week": 10}, "FYPTD": {"week": 5}, "Daily": {"week": 5}, "Plain": {"week": 5}}},
            "areas": [{"client": c, "label": c} for c in ("FCY", "FYPTD", "HTB", "Daily", "Plain")]}

    def patched(self, store):
        from unittest import mock
        from tftrack import web, allowance
        stack = mock.patch.multiple(web.cfg, load_config=mock.DEFAULT, save_config=mock.DEFAULT)
        return (mock.patch.object(web.service, "open_store", return_value=store),
                mock.patch.object(web.cfg, "load_config", side_effect=lambda: self.conf),
                mock.patch.object(web.cfg, "save_config", side_effect=lambda c: None),
                mock.patch.object(allowance, "bank_holidays", return_value=(set(), None)))

    def test_hours_over_the_week_are_banked_held_back_and_charged_once(self):
        from datetime import date
        from tftrack import areas, web
        from tftrack.reports import money_summary
        h = 3600
        store = make_store([
            {"id": 1, "taskId": 1, "startedAt": self.at(5, 8), "duration": 7 * h},    # w/c Mon 5 Oct: 12h, 2 over
            {"id": 2, "taskId": 1, "startedAt": self.at(6, 8), "duration": 5 * h},
            {"id": 3, "taskId": 1, "startedAt": self.at(13, 8), "duration": 8 * h},   # w/c Mon 12 Oct: 8h, 2 short
            {"id": 4, "taskId": 2, "startedAt": self.at(7, 8), "duration": 6 * h},    # own business: 1 over
        ], tasks=self.tasks)
        today = date(2026, 10, 23)
        areas.settle_starts(self.conf, store)
        self.assertEqual({a["client"]: a["bank_start"] for a in self.conf["areas"]}["FCY"], "2026-09-01")
        rules = {c: areas.bank_rules(self.conf, store, c) for c in ("FCY", "FYPTD", "HTB", "Daily", "Plain")}
        self.assertEqual([(c, r["auto"], r["holds_back"]) for c, r in rules.items()],
                         [("FCY", True, True), ("FYPTD", True, False), ("HTB", True, True), ("Daily", True, True),
                          ("Plain", False, False)])
        self.assertIn("by the day or the week", rules["Plain"]["why"])
        # a client without a card isn't banked: there'd be nowhere to see or use the hours
        self.assertIn("card", areas.bank_rules({**self.conf, "areas": []}, store, "FCY")["why"])
        self.assertEqual(areas.bank(store, self.conf, "FCY", today)["seconds"], 2 * h)
        self.assertEqual(areas.bank(store, self.conf, "FYPTD", today)["seconds"], 1 * h)
        p = self.patched(store)
        with p[0], p[1], p[2], p[3]:
            inv = web._invoice({"client": "FCY", "month": "2026-10"})
            self.assertEqual([(g["billed_s"], g["banked_s"]) for g in inv["groups"][:2]], [(10 * h, 2 * h), (8 * h, 0)])
            self.assertEqual(web._month_due(store, self.conf, "FCY", "2026-10")[:2], (18 * h, 360.0))
            # use the banked hours in the short week: they're charged there, and the bank is empty
            areas.use_lieu(store, self.conf, "FCY", 1, "week", date(2026, 10, 14), 2 * h)
            self.assertEqual(areas.bank(store, self.conf, "FCY", today)["seconds"], 0)
            inv = web._invoice({"client": "FCY", "month": "2026-10"})
            self.assertEqual([g["billed_s"] for g in inv["groups"][:2]], [10 * h, 10 * h])
            self.assertEqual(web._month_due(store, self.conf, "FCY", "2026-10")[:2], (20 * h, 400.0))   # 20h worked, 20h charged
            # reports count the work when it was done, once
            self.assertEqual(money_summary(store, date(2026, 10, 1), date(2026, 10, 31), self.conf["clients"])["earned"], 400.0)
            # once the month is invoiced, a change to the weekly hours can't alter it or lose banked time
            secs, amount, _ = web._month_due(store, self.conf, "FCY", "2026-10")
            store.set_invoiced("FCY", "2026-10", secs, amount)
            web._fix_banked(store, "FCY", "2026-10")
            self.conf["targets"]["hours"]["FCY"]["week"] = 11
            self.assertEqual(web._month_due(store, self.conf, "FCY", "2026-10")[:2], (20 * h, 400.0))
            self.assertEqual(areas.bank(store, self.conf, "FCY", today)["seconds"], 0)
            # the area page says which invoiced months the bank is counting weeks from
            self.assertEqual(areas.invoiced_since_start(store, self.conf, "FCY"), ["2026-10"])
            self.assertEqual(areas.invoiced_since_start(store, self.conf, "FYPTD"), [])

    def test_start_date_and_switching_off(self):
        from datetime import date
        from tftrack import areas
        h = 3600
        store = make_store([{"id": 1, "taskId": 2, "startedAt": self.at(5, 8), "duration": 9 * h},     # 4 over
                            {"id": 2, "taskId": 2, "startedAt": self.at(13, 8), "duration": 7 * h}],   # 2 over
                           tasks=self.tasks)
        today = date(2026, 10, 23)
        self.assertEqual(areas.bank(store, self.conf, "FYPTD", today)["seconds"], 6 * h)
        self.conf["areas"][1]["bank_start"] = "2026-10-12"            # only weeks starting on or after this count
        self.assertEqual(areas.bank(store, self.conf, "FYPTD", today)["seconds"], 2 * h)
        store.add_lieu("FYPTD", 3 * h, "From before")                  # changes by hand still add on
        self.assertEqual(areas.bank(store, self.conf, "FYPTD", today), {
            "auto_seconds": 2 * h, "hand_seconds": 3 * h, "seconds": 5 * h,
            "weeks": [{"start": "2026-10-12", "worked_seconds": 7 * h, "over_seconds": 2 * h}]})
        self.conf["areas"][1]["bank_auto"] = False
        self.assertEqual(areas.bank(store, self.conf, "FYPTD", today)["seconds"], 3 * h)
        # the start is the same fixed date even when earlier months are already invoiced: those invoices are taken
        # to have charged only the hours a week, so their extra hours belong in the bank
        store.set_invoiced("FCY", "2026-09", 3600, 20.0)
        conf = {**self.conf, "areas": [{"client": "FCY", "label": "FCY"}]}
        areas.settle_starts(conf, store)
        self.assertEqual(conf["areas"][0]["bank_start"], "2026-09-01")

    def test_weeks_over_in_months_already_invoiced_are_in_the_bank(self):
        from datetime import date
        from tftrack import areas, web
        h = 3600
        sep = lambda d, hh: datetime(2026, 9, d, hh).astimezone().astimezone(UTC).isoformat()
        store = make_store([{"id": 1, "taskId": 1, "startedAt": sep(7, 8), "duration": 13 * h},       # w/c 7 Sep: 3 over
                            {"id": 2, "taskId": 1, "startedAt": sep(14, 8), "duration": 9 * h},       # w/c 14 Sep: under
                            {"id": 3, "taskId": 1, "startedAt": self.at(5, 8), "duration": 11 * h}],  # w/c 5 Oct: 1 over
                           tasks=self.tasks)
        store.set_invoiced("FCY", "2026-09", 19 * h, 380.0)       # invoiced for the allotted hours only: 10 + 9
        areas.settle_starts(self.conf, store)
        b = areas.bank(store, self.conf, "FCY", date(2026, 11, 13))
        self.assertEqual(b["seconds"], 4 * h)
        self.assertEqual([(w["start"], w["over_seconds"]) for w in b["weeks"]], [("2026-09-07", 3 * h), ("2026-10-05", 1 * h)])
        p = self.patched(store)
        with p[0], p[1], p[2], p[3]:
            # the September timesheet now agrees with what was invoiced
            self.assertEqual(web._month_due(store, self.conf, "FCY", "2026-09")[0], 19 * h)


class AllowanceBankTest(unittest.TestCase):
    """A client invoiced by the day, with a monthly allowance: the week from Dave's screenshot (8h 15m against 6h)."""

    def test_week_over_the_allowance_is_banked_off_its_last_days(self):
        from datetime import date
        from unittest import mock
        from tftrack import areas, web, allowance
        m = 60
        at = lambda d, hh, mm=0: datetime(2026, 10, d, hh, mm).astimezone().astimezone(UTC).isoformat()
        store = make_store([
            {"id": 1, "taskId": 1, "startedAt": at(5, 9), "duration": 60 * m},          # Mon 1h
            {"id": 2, "taskId": 1, "startedAt": at(7, 9), "duration": 135 * m},         # Wed 2h15
            {"id": 3, "taskId": 1, "startedAt": at(9, 13, 30), "duration": 136 * m},    # Fri 2h16 -> 2h30
            {"id": 4, "taskId": 1, "startedAt": at(10, 11), "duration": 143 * m},       # Sat 2h23 -> 2h30: 8h15 in all
            {"id": 5, "taskId": 1, "startedAt": at(13, 9), "duration": 4 * 60 * m},     # the next week: 4h, 2h short
        ], tasks=[{"id": 1, "name": "HTB", "tag": "HTB"}])
        conf = {"clients": {"HTB": {"billing": "day", "rounding": "up", "block_minutes": 15, "rate": 40}},
                "limits": [{"task": "HTB", "weekly_hours": 6, "allowance": True, "allowance_start": "2026-10"}],
                "areas": [{"client": "HTB", "label": "HTB", "bank_start": "2026-09-01"}]}
        today = date(2026, 10, 23)
        self.assertEqual(areas.allotted_hours(conf, store, "HTB"), 6.0)
        self.assertEqual(areas.bank(store, conf, "HTB", today)["weeks"],
                         [{"start": "2026-10-05", "worked_seconds": 495 * m, "over_seconds": 135 * m}])   # 2h15 over
        with mock.patch.object(web.service, "open_store", return_value=store), \
                mock.patch.object(web.cfg, "load_config", side_effect=lambda: conf), \
                mock.patch.object(allowance, "bank_holidays", return_value=(set(), None)):
            inv = web._invoice({"client": "HTB", "month": "2026-10"})
            days = {d["date"]: (d["billed_s"] // m, d["banked_s"] // m) for g in inv["groups"] for d in g["days"]}
            self.assertEqual(days, {"2026-10-05": (60, 0), "2026-10-07": (135, 0), "2026-10-09": (150, 0),
                                    "2026-10-10": (15, 135), "2026-10-13": (240, 0)})      # Saturday keeps 15m of the 6h
            self.assertEqual(inv["total_billed_s"], (6 + 4) * 60 * m)
            self.assertEqual(inv["allowances"][0]["charging_s"], 10 * 60 * m)
            self.assertEqual(web._month_due(store, conf, "HTB", "2026-10")[:2], (10 * 60 * m, 400.0))
            self.assertIn("2h 15m over the weekly hours: banked", web.invoice_page(inv))
            self.assertTrue(web.pdf.invoice_pdf(inv).startswith(b"%PDF"))
            # an entry rolled over goes to the carried hours instead, so it isn't banked as well
            store.set_rolled(1, "carried", "2026-10-05")
            self.assertEqual(areas.bank(store, conf, "HTB", today)["seconds"], 75 * m)       # 7h15 left: 1h15 over
            self.assertEqual(web._carried("HTB", today), 60 * m)                            # and 1h carried
            store.set_rolled(1, None)
            # add the banked hours to the short week: charged there in full, on top of that week's own hours
            areas.use_lieu(store, conf, "HTB", 1, "day", date(2026, 10, 14), 120 * m)
            self.assertEqual(areas.bank(store, conf, "HTB", today)["seconds"], 15 * m)
            self.assertEqual(web._month_due(store, conf, "HTB", "2026-10")[0], 12 * 60 * m)
            # banked hours used in a week that's already full aren't banked a second time
            areas.use_lieu(store, conf, "HTB", 1, "day", date(2026, 10, 8), 15 * m)
            self.assertEqual(areas.bank(store, conf, "HTB", today)["seconds"], 0)
            self.assertEqual(web._month_due(store, conf, "HTB", "2026-10")[0], 12 * 60 * m + 15 * m)   # 12h15 worked in all
            # invoicing fixes each day, so a later change to the hours a week leaves the month alone
            secs, amount, _ = web._month_due(store, conf, "HTB", "2026-10")
            store.set_invoiced("HTB", "2026-10", secs, amount)
            web._fix_banked(store, "HTB", "2026-10")
            conf["limits"][0]["weekly_hours"] = 8
            self.assertEqual(web._month_due(store, conf, "HTB", "2026-10")[0], secs)
            self.assertEqual(areas.bank(store, conf, "HTB", today)["seconds"], 0)
