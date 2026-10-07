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
