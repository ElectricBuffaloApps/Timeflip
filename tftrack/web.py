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
from .reports import (BILLING_STYLES, ROUNDING_MODES, build_report, invoice, timesheet_csv,
                      timesheet_rows)

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
        "client_billing": conf.get("clients", {}),
        "status": status,
    }


PAGE_STYLE = """
body{font:14px system-ui,-apple-system,sans-serif;color:#0b0b0b;background:#fff;margin:32px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 6px} p{color:#52514e;margin:0 0 20px}
table{border-collapse:collapse;width:100%} th,td{padding:6px 8px;border-bottom:1px solid #e1e0d9;text-align:left}
th{font-weight:600;color:#52514e} .n{text-align:right;font-variant-numeric:tabular-nums}
tr.sub td{font-weight:600;background:#f4f4f1}
tr.x td{text-decoration:line-through;color:#898781} tr.x td.keep{text-decoration:none}
.tag{font-size:12px;color:#898781;text-decoration:none;display:inline-block;margin-left:6px}
.grand td{font-weight:700;border-top:2px solid #0b0b0b;border-bottom:none;font-size:15px}
.bar{margin-bottom:20px;display:flex;gap:12px;align-items:center;color:#52514e}
button{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid #c3c2b7;background:#fff;cursor:pointer}
label.ex{cursor:pointer;font-size:12px;color:#52514e;white-space:nowrap}
@media print{.bar,label.ex{display:none} body{margin:0}}
"""

PAGE_SCRIPT = """<script>
for (const box of document.querySelectorAll("input[data-id]")) {
  box.addEventListener("change", async () => {
    box.disabled = true;
    const r = await fetch("/api/exclude", {method: "POST",
      headers: {"Content-Type": "application/json", "X-TFTrack": "1"},
      body: JSON.stringify({id: Number(box.dataset.id), excluded: box.checked})});
    if (r.ok) location.reload(); else { alert("Couldn't save that change."); box.disabled = false; }
  });
}
</script>"""


def _hm(seconds: int) -> str:
    m = round(seconds / 60)
    return f"{m // 60}h {m % 60:02d}m"


def _exclude_cell(r: dict) -> str:
    checked = " checked" if r["excluded"] else ""
    note = '<span class="tag">not charged</span>' if r["excluded"] else ""
    return (f'<td class="keep">{note}<label class="ex"><input type="checkbox" data-id="{int(r["id"])}"{checked}> '
            f'Exclude</label></td>')


def _page(title: str, heading: str, subtitle: str, table: str) -> str:
    esc = html.escape
    return f"""<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<title>{esc(title)}</title><style>{PAGE_STYLE}</style></head><body>
<div class="bar"><button onclick="window.print()">Print or save as PDF</button>
<span>Tick <b>Exclude</b> on any entry you won't charge for. It stays on the sheet, crossed out.</span></div>
<h1>{esc(heading)}</h1><p>{esc(subtitle)}</p>{table}{PAGE_SCRIPT}</body></html>"""


def timesheet_page(rows: list[dict], start: date, end: date, client: str | None) -> str:
    esc = html.escape
    body = "".join(
        f"<tr class=\"{'x' if r['excluded'] else ''}\"><td>{date.fromisoformat(r['date']):%a %d %b}</td>"
        f"<td>{esc(r['start'])}–{esc(r['end'])}</td>"
        f"<td>{esc(r['client'])}</td><td>{esc(r['task'])}</td><td class=n>{r['hours']:.2f}</td>"
        f"<td class=n>{esc(r['currency']) + format(r['amount'], '.2f') if r['amount'] else '–'}</td>"
        f"{_exclude_cell(r)}</tr>"
        for r in rows
    )
    charged = [r for r in rows if not r["excluded"]]
    hours = sum(r["hours"] for r in charged)
    currencies = {r["currency"] for r in charged if r["amount"]}
    total = (f"{next(iter(currencies), '')}{sum(r['amount'] for r in charged):.2f}"
             if len(currencies) <= 1 else "mixed currencies")
    who = client or "All clients"
    table = f"""<table><thead><tr><th>Date</th><th>Time</th><th>Client</th><th>Task</th><th class=n>Hours</th>
<th class=n>Amount</th><th></th></tr></thead>
<tbody>{body or '<tr><td colspan=7>No time recorded in this period.</td></tr>'}</tbody>
<tfoot><tr class="grand"><td colspan=4>Total</td><td class=n>{hours:.2f}</td><td class=n>{esc(total)}</td><td></td></tr>
</tfoot></table>"""
    return _page(f"Timesheet – {who}", f"Timesheet: {who}", f"{start:%d %b %Y} to {end:%d %b %Y}", table)


def invoice_page(inv: dict) -> str:
    """Monthly sheet for a client billed by the day or by the week."""
    esc = html.escape
    rounding = ("" if inv["rounding"] == "none" else
                f" · day totals rounded {'to the nearest' if inv['rounding'] == 'nearest' else inv['rounding'] + ' to'}"
                f" {inv['block_minutes']} minutes")
    parts = []
    for g in inv["groups"]:
        rows = []
        if g["label"]:
            rows.append(f'<tr><th colspan=4 style="padding-top:22px;color:#0b0b0b;font-size:15px">{esc(g["label"])}</th></tr>')
        if not g["days"]:
            rows.append('<tr><td colspan=4>No time recorded.</td></tr>')
        for d in g["days"]:
            day = date.fromisoformat(d["date"])
            for r in d["entries"]:
                rows.append(f'<tr class="{"x" if r["excluded"] else ""}"><td>{day:%a %d %b}</td>'
                            f'<td>{esc(r["start"])}–{esc(r["end"])}</td><td>{esc(r["task"])}</td>'
                            f'<td class=n>{_hm(r["seconds"])}</td>{_exclude_cell(r)}</tr>')
            actual = "" if d["actual_s"] == d["billed_s"] else f' <span class="tag">(actual {_hm(d["actual_s"])})</span>'
            rows.append(f'<tr class="sub"><td colspan=3>{day:%A %d %B} total{actual}</td>'
                        f'<td class=n>{_hm(d["billed_s"])}</td><td class=n>{d["billed_s"] / 3600:.2f} h</td></tr>')
        if g["label"]:
            rows.append(f'<tr class="sub"><td colspan=3>Week total</td><td class=n>{_hm(g["billed_s"])}</td>'
                        f'<td class=n>{g["billed_s"] / 3600:.2f} h</td></tr>')
        parts.append("".join(rows))
    month = date.fromisoformat(inv["month"] + "-01")
    days_line = (f'<tr class="grand"><td colspan=3>Days worked</td><td class=n>{inv["days_worked"]}</td><td></td></tr>'
                 if inv["style"] == "day" else "")
    table = f"""<table><thead><tr><th>Date</th><th>Time</th><th>Task</th><th class=n>Duration</th><th></th></tr></thead>
<tbody>{"".join(parts)}</tbody><tfoot>{days_line}
<tr class="grand"><td colspan=3>Total for {month:%B %Y}</td><td class=n>{_hm(inv["total_billed_s"])}</td>
<td class=n>{inv["total_billed_s"] / 3600:.2f} h</td></tr></tfoot></table>"""
    start, end = date.fromisoformat(inv["start"]), date.fromisoformat(inv["end"])
    style = "by the day" if inv["style"] == "day" else "by the week (weeks starting on a Monday in the month)"
    return _page(f"{inv['client']} – {month:%B %Y}", f"{inv['client']}: {month:%B %Y}",
                 f"{start:%d %b %Y} to {end:%d %b %Y} · billed {style}{rounding}", table)


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
            if url.path == "/invoice":
                return self._send(200, invoice_page(_invoice(q)), "text/html; charset=utf-8")
            if url.path in ("/timesheet", "/timesheet.csv"):
                client = q.get("client") or None
                if q.get("month") and client:  # use the client's month range
                    inv = _invoice(q)
                    start, end = date.fromisoformat(inv["start"]), date.fromisoformat(inv["end"])
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
                elif self.path == "/api/exclude":
                    service.open_store().set_excluded(int(body["id"]), bool(body.get("excluded")))
                    return self._send(200, {"ok": True})
                elif self.path == "/api/clients":
                    conf = cfg.load_config()
                    conf["clients"] = {
                        str(name).strip(): _clean_client(c) for name, c in (body.get("clients") or {}).items()
                        if str(name).strip()
                    }
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


def _clean_client(raw: dict) -> dict:
    billing = raw.get("billing") if raw.get("billing") in BILLING_STYLES else "hourly"
    rounding = raw.get("rounding") if raw.get("rounding") in ROUNDING_MODES else "none"
    block = int(raw.get("block_minutes") or 15)
    if not 1 <= block <= 240:
        raise ValueError("Rounding block must be between 1 and 240 minutes.")
    return {"billing": billing, "rounding": rounding, "block_minutes": block}


def _client_settings(conf: dict, client: str) -> dict:
    for name, c in (conf.get("clients") or {}).items():
        if name.lower() == client.lower():
            return c
    return {"billing": "hourly", "rounding": "none", "block_minutes": 15}


def _invoice(q: dict) -> dict:
    client = q.get("client") or ""
    if not client:
        raise ValueError("Choose a client.")
    try:
        year, month = (int(x) for x in (q.get("month") or "").split("-"))
        date(year, month, 1)
    except ValueError:
        raise ValueError("Choose a month.")
    c = _client_settings(cfg.load_config(), client)
    style = c["billing"] if c["billing"] in ("day", "week") else "day"
    return invoice(service.open_store(), client, year, month, style,
                   c.get("block_minutes", 15), c.get("rounding", "none"))


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
