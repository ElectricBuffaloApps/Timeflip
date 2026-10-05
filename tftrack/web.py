"""Local web page for limits, charts and timesheets. Listens on 127.0.0.1 only."""
from __future__ import annotations

import html
import json
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config as cfg
from . import service
from .limits import WEEKDAYS, parse_limits
from .reports import build_report, timesheet_csv, timesheet_rows

STATIC = Path(__file__).parent / "static"
# One writer at a time: syncs and config saves must not interleave.
LOCK = threading.Lock()


def _date(value: str | None, default: date) -> date:
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


def state() -> dict:
    conf = cfg.load_config()
    store = service.open_store()
    tasks = store.tasks()
    error = store.get_meta("last_error")
    try:
        status = [{
            "label": s.limit.label, "period": s.limit.period, "hours": s.limit.hours,
            "task": s.limit.task, "client": s.limit.client,
            "seconds": s.seconds, "percent": round(s.percent, 1),
        } for s in service.statuses(conf, store)]
    except ValueError:
        status = []
    return {
        "email": conf.get("email"),
        "last_sync": store.last_sync(),
        "last_error": error.split("|", 1)[1] if error else None,
        "unmatched_intervals": int(store.get_meta("unmatched_intervals") or 0),
        "week_starts": conf.get("week_starts", "monday"),
        "alert_levels_percent": conf.get("alert_levels_percent", [80, 100]),
        "limits": conf.get("limits", []),
        "tasks": sorted({t["name"] for t in tasks}),
        "clients": sorted({t["tag"] for t in tasks if t["tag"]}),
        "status": status,
    }


def timesheet_page(rows: list[dict], start: date, end: date, client: str | None) -> str:
    esc = html.escape
    body = "".join(
        f"<tr><td>{date.fromisoformat(r['date']):%a %d %b}</td><td>{esc(r['start'])}–{esc(r['end'])}</td>"
        f"<td>{esc(r['client'])}</td><td>{esc(r['task'])}</td><td class=n>{r['hours']:.2f}</td>"
        f"<td class=n>{esc(r['currency']) + format(r['amount'], '.2f') if r['amount'] else '–'}</td></tr>"
        for r in rows
    )
    hours = sum(r["hours"] for r in rows)
    currencies = {r["currency"] for r in rows if r["amount"]}
    total = (f"{next(iter(currencies), '')}{sum(r['amount'] for r in rows):.2f}"
             if len(currencies) <= 1 else "mixed currencies")
    who = esc(client) if client else "All clients"
    return f"""<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<title>Timesheet – {who}</title><style>
body{{font:14px system-ui,-apple-system,sans-serif;color:#0b0b0b;background:#fff;margin:32px}}
h1{{font-size:22px;margin:0 0 4px}} p{{color:#52514e;margin:0 0 20px}}
table{{border-collapse:collapse;width:100%}} th,td{{padding:6px 8px;border-bottom:1px solid #e1e0d9;text-align:left}}
th{{font-weight:600;color:#52514e}} .n{{text-align:right;font-variant-numeric:tabular-nums}}
tfoot td{{font-weight:600;border-top:2px solid #0b0b0b;border-bottom:none}}
.bar{{margin-bottom:20px}} button{{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid #c3c2b7;background:#fff;cursor:pointer}}
@media print{{.bar{{display:none}} body{{margin:0}}}}
</style></head><body>
<div class="bar"><button onclick="window.print()">Print or save as PDF</button></div>
<h1>Timesheet: {who}</h1><p>{start:%d %b %Y} to {end:%d %b %Y}</p>
<table><thead><tr><th>Date</th><th>Time</th><th>Client</th><th>Task</th><th class=n>Hours</th><th class=n>Amount</th></tr></thead>
<tbody>{body or '<tr><td colspan=6>No time recorded in this period.</td></tr>'}</tbody>
<tfoot><tr><td colspan=4>Total</td><td class=n>{hours:.2f}</td><td class=n>{esc(total)}</td></tr></tfoot></table>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _allowed_host(self) -> bool:
        # Refuse requests addressed to any other name (DNS rebinding protection).
        return self.headers.get("Host", "") in (f"127.0.0.1:{cfg.PORT}", f"localhost:{cfg.PORT}")

    def _send(self, code: int, body, ctype: str = "application/json", extra: dict | None = None):
        if not isinstance(body, (bytes, str)):
            body = json.dumps(body)
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self._allowed_host():
            return self._send(403, {"error": "forbidden"})
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        today = date.today()
        start = _date(q.get("start"), today - timedelta(days=6))
        end = max(_date(q.get("end"), today), start)
        if (end - start).days > 366:
            start = end - timedelta(days=366)
        try:
            if url.path == "/":
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/state":
                return self._send(200, state())
            if url.path == "/api/report":
                group = "client" if q.get("group") == "client" else "task"
                return self._send(200, build_report(service.open_store(), start, end, group))
            if url.path in ("/timesheet", "/timesheet.csv"):
                client = q.get("client") or None
                rows = timesheet_rows(service.open_store(), start, end, client)
                if url.path == "/timesheet":
                    return self._send(200, timesheet_page(rows, start, end, client), "text/html; charset=utf-8")
                name = f"timesheet-{(client or 'all').replace(' ', '-')}-{start}-to-{end}.csv"
                return self._send(200, timesheet_csv(rows), "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        # The custom header forces a CORS preflight, which we never answer, so other websites can't post here.
        if not self._allowed_host() or self.headers.get("X-TFTrack") != "1":
            return self._send(403, {"error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            with LOCK:
                if self.path == "/api/limits":
                    conf = cfg.load_config()
                    limits = [_clean_limit(l) for l in body.get("limits", [])]
                    parse_limits({"limits": limits})  # raises ValueError with a readable message
                    conf["limits"] = limits
                    cfg.save_config(conf)
                elif self.path == "/api/settings":
                    conf = cfg.load_config()
                    if body.get("week_starts") in WEEKDAYS:
                        conf["week_starts"] = body["week_starts"]
                    cfg.save_config(conf)
                elif self.path == "/api/sync":
                    service.sync(cfg.load_config(), service.open_store())
                else:
                    return self._send(404, {"error": "not found"})
            return self._send(200, state())
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})


def _clean_limit(raw: dict) -> dict:
    out = {}
    for key in ("task", "client", "label"):
        if isinstance(raw.get(key), str) and raw[key].strip():
            out[key] = raw[key].strip()
    for key in ("daily_hours", "weekly_hours"):
        if raw.get(key) not in (None, ""):
            hours = float(raw[key])
            if hours <= 0:
                raise ValueError("Hours must be more than zero.")
            out[key] = hours
    return out


def serve() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", cfg.PORT), Handler)
    print(f"TimeFlip Tracker running at http://127.0.0.1:{cfg.PORT}")
    server.serve_forever()
