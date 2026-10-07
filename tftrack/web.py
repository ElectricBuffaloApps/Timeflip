"""Local web page for limits, charts and timesheets. Listens on 127.0.0.1 only."""
from __future__ import annotations

import html
import json
import subprocess
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config as cfg
from . import service, updater
from .store import MANUAL_ID_OFFSET
from .limits import WEEKDAYS, parse_limits
from .reports import (BILLING_STYLES, ROUNDING_MODES, build_report, invoice, invoice_lines, money_summary, month_summary,
                      timesheet_csv, timesheet_rows)

STATIC = Path(__file__).parent / "static"
# One writer at a time: syncs and config saves must not interleave.
LOCK = threading.Lock()


def _date(value: str | None, default: date) -> date:
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


def _app_path() -> str | None:
    from .macapp import find_app
    app = find_app()
    return str(app) if app else None


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
        "version": updater.current_version(),
        "app_path": _app_path(),
        "auto_update": conf.get("auto_update", True),
        "whats_new": updater.changes_since(conf.get("last_seen_version")),
        "last_sync": store.last_sync(),
        "last_error": error.split("|", 1)[1] if error else None,
        "unmatched_intervals": int(store.get_meta("unmatched_intervals") or 0),
        "week_starts": conf.get("week_starts", "monday"),
        "alert_levels_percent": conf.get("alert_levels_percent", [80, 100]),
        "limits": conf.get("limits", []),
        "tasks": sorted({t["name"] for t in tasks}),
        "task_list": [{"id": t["id"], "name": t["name"], "client": t["tag"]}
                      for t in sorted(tasks, key=lambda t: t["name"].lower())],
        "ignored_tasks": conf.get("ignored_tasks", []),
        "clients": sorted({t["tag"] for t in tasks if t["tag"]}),
        "client_billing": conf.get("clients", {}),
        "status": status,
    }


PAGE_STYLE = """
body{font:14px/1.6 Inter,-apple-system,BlinkMacSystemFont,sans-serif;color:#111827;background:#fff;margin:32px}
h1,h2,button{font-family:Sora,Inter,sans-serif;letter-spacing:-0.02em}
h1{font-size:24px;font-weight:700;margin:0 0 4px}
h1::before{content:"";display:block;width:40px;height:3px;border-radius:2px;background:#F97316;margin-bottom:14px}
h2{font-size:17px;margin:28px 0 6px} p{color:#6B7280;margin:0 0 20px}
table{border-collapse:collapse;width:100%} th,td{padding:6px 8px;border-bottom:1px solid #E5E7EB;text-align:left}
th{font-weight:600;color:#6B7280} .n{text-align:right;font-variant-numeric:tabular-nums}
tr.sub td{font-weight:600;background:#FFF7ED}
tr.x td{text-decoration:line-through;color:#9CA3AF} tr.x td.keep{text-decoration:none}
.tag{font-size:12px;color:#6B7280;text-decoration:none;display:inline-block;margin-left:6px}
.grand td{font-family:Sora,Inter,sans-serif;font-weight:700;border-top:2px solid #1E2530;border-bottom:none;font-size:15px}
.bar{margin-bottom:24px;display:flex;gap:12px;align-items:center;color:#6B7280}
button{font-size:14px;font-weight:600;padding:8px 16px;border-radius:8px;border:1px solid #D1D5DB;background:#fff;color:#111827;cursor:pointer}
button:hover{border-color:#F97316}
.bar button{background:#F97316;border-color:#F97316;color:#fff} .bar button:hover{background:#c2410c;border-color:#c2410c}
label.ex{cursor:pointer;font-size:12px;color:#6B7280;white-space:nowrap} input[type=checkbox]{accent-color:#F97316}
.lines{border-radius:12px;padding:18px 22px;margin:0 0 28px;background:#FFF7ED;border-left:3px solid #F97316;
  box-shadow:0 2px 12px rgba(0,0,0,0.08)}
.lines td,.lines th{border-bottom-color:#FED7AA}
button.copy{font-size:12px;padding:3px 10px;margin-left:6px;border-radius:6px}
.copied{color:#00c853;font-size:13px}
button.edit{font-size:12px;padding:2px 9px;margin-left:8px;border-radius:6px}
.bar button.secondary{background:#fff;color:#111827;border-color:#D1D5DB}
.bar button.secondary:hover{background:#fff;border-color:#F97316}
.tag.adj{color:#c2410c;font-style:italic}
dialog{border:none;border-radius:12px;padding:24px;box-shadow:0 10px 40px rgba(0,0,0,.25);min-width:340px}
dialog::backdrop{background:rgba(15,18,25,.45)}
dialog input,dialog select{font:inherit;padding:7px 10px;border:1px solid #D1D5DB;border-radius:8px;margin-top:4px}
dialog select{width:100%} .dlg-buttons{display:flex;gap:8px;margin-top:18px;align-items:center}
dialog button.primary{background:#F97316;border-color:#F97316;color:#fff} button.danger{color:#d03b3b}
.err{color:#d03b3b;min-height:1.2em;margin:10px 0 0}
@media print{.bar,label.ex,.lines,button.edit{display:none} body{margin:0}}
"""

PAGE_SCRIPT = """<script>
for (const b of document.querySelectorAll("button.copy")) {
  b.addEventListener("click", async () => {
    const text = b.dataset.copy;
    try { await navigator.clipboard.writeText(text); }
    catch (_) {
      const t = document.createElement("textarea"); t.value = text; document.body.append(t);
      t.select(); document.execCommand("copy"); t.remove();
    }
    const old = b.textContent; b.textContent = "Copied ✓"; setTimeout(() => (b.textContent = old), 1500);
  });
}
async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-TFTrack": "1"},
                               body: JSON.stringify(body)});
  if (!r.ok) throw new Error((await r.json()).error || "Couldn't save that change.");
}
const dlg = document.getElementById("entryDialog");
const $f = (id) => document.getElementById(id);
let editing = null;
function openEntry(entry) {
  editing = entry;
  const defaults = JSON.parse(document.body.dataset.defaults);
  $f("dlgTitle").textContent = entry ? "Edit entry" : "Add entry";
  $f("fDate").value = entry ? entry.date : defaults.date;
  $f("fStart").value = entry ? entry.start : "09:00";
  $f("fEnd").value = entry ? entry.end : "10:00";
  if (entry) $f("fTask").value = String(entry.task_id);
  else if (defaults.client) {
    const match = [...$f("fTask").options].find((o) => o.textContent.endsWith(`(${defaults.client})`));
    if (match) $f("fTask").value = match.value;
  }
  $f("fReset").style.display = entry && entry.adjusted ? "" : "none";
  $f("fDelete").style.display = entry && entry.manual ? "" : "none";
  $f("fErr").textContent = "";
  dlg.showModal();
}
for (const b of document.querySelectorAll("button.edit")) b.addEventListener("click", () => openEntry(JSON.parse(b.dataset.entry)));
$f("addEntry").addEventListener("click", () => openEntry(null));
$f("fCancel").addEventListener("click", () => dlg.close());
async function act(fn) {
  try { await fn(); location.reload(); } catch (e) { $f("fErr").textContent = e.message; }
}
$f("entryForm").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const body = {date: $f("fDate").value, start: $f("fStart").value, end: $f("fEnd").value, task_id: Number($f("fTask").value)};
  act(() => editing ? post("/api/entry/adjust", {...body, id: editing.id}) : post("/api/entry/add", body));
});
$f("fReset").addEventListener("click", () => act(() => post("/api/entry/reset", {id: editing.id})));
$f("fDelete").addEventListener("click", () => {
  if (confirm("Delete this added entry?")) act(() => post("/api/entry/delete", {id: editing.id}));
});
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
    entry = html.escape(json.dumps({
        "id": r["id"], "date": r["entry_date"], "start": r["entry_start"], "end": r["entry_end"],
        "task_id": r["task_id"], "adjusted": r["adjusted"], "manual": r["manual"],
    }))
    return (f'<td class="keep">{note}<label class="ex"><input type="checkbox" data-id="{int(r["id"])}"{checked}> '
            f'Exclude</label><button class="edit" data-entry="{entry}">Edit</button></td>')


def _task_cell(r: dict) -> str:
    """Task name, plus a visible (and printed) note when the entry was adjusted or added by hand."""
    note = ""
    if r["manual"]:
        note = ' <span class="tag adj">added</span>'
    elif r["adjusted"]:
        note = f' <span class="tag adj">adjusted · {html.escape(r["note"])}</span>'
    return f"<td>{html.escape(r['task'])}{note}</td>"


def _task_options() -> str:
    tasks = service.open_store().tasks()
    return "".join(
        f'<option value="{t["id"]}">{html.escape(t["name"])}{" (" + html.escape(t["tag"]) + ")" if t["tag"] else ""}</option>'
        for t in sorted(tasks, key=lambda t: ((t["tag"] or "~").lower(), t["name"].lower()))
    )


def _page(title: str, heading: str, subtitle: str, table: str, default_date: date | None = None,
          client: str | None = None) -> str:
    esc = html.escape
    dialog = f"""<dialog id="entryDialog"><form method="dialog" id="entryForm">
<h2 id="dlgTitle" style="margin-top:0">Edit entry</h2>
<label>Date<br><input type="date" id="fDate" required></label>
<div style="display:flex;gap:12px;margin-top:10px">
<label>Start<br><input type="time" id="fStart" required></label>
<label>End<br><input type="time" id="fEnd" required></label></div>
<label style="display:block;margin-top:10px">Task<br><select id="fTask">{_task_options()}</select></label>
<p class="err" id="fErr"></p>
<div class="dlg-buttons">
<button type="button" id="fDelete" class="danger">Delete</button>
<button type="button" id="fReset">Back to original</button>
<span style="flex:1"></span>
<button type="button" id="fCancel">Cancel</button>
<button type="submit" id="fSave" class="primary">Save</button></div>
</form></dialog>"""
    defaults = esc(json.dumps({"date": (default_date or date.today()).isoformat(), "client": client or ""}))
    return f"""<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<title>{esc(title)}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800&family=Inter:wght@400;500;600;700&display=swap">
<style>{PAGE_STYLE}</style></head><body data-defaults="{defaults}">
<div class="bar"><button onclick="window.print()">Print or save as PDF</button>
<button id="addEntry" class="secondary">+ Add entry</button>
<span>Use <b>Edit</b> to change an entry's times or task, or <b>Exclude</b> to leave it uncharged.</span></div>
<h1>{esc(heading)}</h1><p>{esc(subtitle)}</p>{table}{dialog}{PAGE_SCRIPT}</body></html>"""


def timesheet_page(rows: list[dict], start: date, end: date, client: str | None) -> str:
    esc = html.escape
    body = "".join(
        f"<tr class=\"{'x' if r['excluded'] else ''}\"><td>{date.fromisoformat(r['date']):%a %d %b}</td>"
        f"<td>{esc(r['start'])}–{esc(r['end'])}</td>"
        f"<td>{esc(r['client'])}</td>{_task_cell(r)}<td class=n>{r['hours']:.2f}</td>"
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
    return _page(f"Timesheet – {who}", f"Timesheet: {who}", f"{start:%d %b %Y} to {end:%d %b %Y}", table,
                 start, client)


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
                            f'<td>{esc(r["start"])}–{esc(r["end"])}</td>{_task_cell(r)}'
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
    table = _lines_panel(inv) + table
    start, end = date.fromisoformat(inv["start"]), date.fromisoformat(inv["end"])
    style = "by the day" if inv["style"] == "day" else "by the week (weeks starting on a Monday in the month)"
    return _page(f"{inv['client']} – {month:%B %Y}", f"{inv['client']}: {month:%B %Y}",
                 f"{start:%d %b %Y} to {end:%d %b %Y} · billed {style}{rounding}", table, start, inv["client"])


def _lines_panel(inv: dict) -> str:
    """Copy-ready invoice lines for pasting into Revolut Business (or any invoicing app). Not printed."""
    esc = html.escape
    rate = inv.get("rate")
    lines = invoice_lines(inv, rate)
    if not lines:
        return ""

    def btn(text: str, value: str) -> str:
        return f'<button class="copy" data-copy="{esc(value)}">{esc(text)}</button>'

    rows = "".join(
        f"<tr><td>{esc(l['description'])} {btn('Copy', l['description'])}</td>"
        f"<td class=n>{l['hours']:.2f} {btn('Copy', format(l['hours'], '.2f'))}</td>"
        f"<td class=n>{'£' + format(rate, '.2f') if rate else '–'}</td>"
        f"<td class=n>{'£' + format(l['amount'], '.2f') if l['amount'] is not None else '–'}</td></tr>"
        for l in lines
    )
    total_hours = sum(l["hours"] for l in lines)
    total_amount = sum(l["amount"] or 0 for l in lines)
    all_text = "\n".join(f"{l['description']}\t{l['hours']:.2f}" + (f"\t{rate:.2f}" if rate else "") for l in lines)
    rate_note = ("" if rate else
                 ' Add an hourly rate for this client in <b>Settings → How each client is invoiced</b> to see amounts.')
    return f"""<div class="lines bar-block">
<h2 style="margin-top:0">Invoice lines</h2>
<p>One line per {"day" if inv["style"] == "day" else "week"}, ready to copy into your invoice: description, then quantity
(hours), then rate.{rate_note}</p>
<table><thead><tr><th>Description</th><th class=n>Hours</th><th class=n>Rate</th><th class=n>Amount</th></tr></thead>
<tbody>{rows}</tbody>
<tfoot><tr class="grand"><td>Total</td><td class=n>{total_hours:.2f}</td><td></td>
<td class=n>{'£' + format(total_amount, '.2f') if rate else '–'}</td></tr></tfoot></table>
<p style="margin-top:10px">{btn('Copy all lines', all_text)} <span class="copied" id="copied"></span></p>
</div>"""


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
            if url.path == "/api/update":
                return self._send(200, {**updater.check(), "can_update": updater.is_installed_copy()})
            if url.path == "/api/state":
                return self._send(200, state())
            if url.path == "/api/today":
                store = service.open_store()
                today = date.today()
                rows = [r for r in timesheet_rows(store, today, today) if not r["excluded"]]
                ignored = set(cfg.load_config().get("ignored_tasks", []))
                all_store = service.open_store(include_ignored=True)
                midnight = datetime(today.year, today.month, today.day).astimezone()
                not_counted = sum(seg["seconds"] for seg in all_store.segments(midnight, midnight + timedelta(days=1))
                                  if seg["task_id"] in ignored)
                return self._send(200, {
                    "date": today.isoformat(),
                    "money": money_summary(store, today, today, cfg.load_config().get("clients", {})),
                    "total_seconds": sum(r["seconds"] for r in rows),
                    "not_counted_seconds": not_counted,
                    "by_task": build_report(store, today, today, "task")["series"],
                    "entries": [{k: r[k] for k in ("start", "end", "task", "client", "seconds", "adjusted", "manual")}
                                for r in rows],
                })
            if url.path == "/api/month":
                try:
                    y, mo = (int(x) for x in (q.get("month") or "").split("-"))
                    date(y, mo, 1)
                except ValueError:
                    y, mo = date.today().year, date.today().month
                conf = cfg.load_config()
                return self._send(200, month_summary(service.open_store(), y, mo, conf.get("clients", {}),
                                                     conf.get("monthly_income", {})))
            if url.path == "/api/report":
                group = "client" if q.get("group") == "client" else "task"
                store = service.open_store()
                report = build_report(store, start, end, group)
                m = money_summary(store, start, end, cfg.load_config().get("clients", {}))
                lookup = ({c["client"]: c for c in m["by_client"]} if group == "client" else m["by_task"])
                for series in report["series"]:
                    hit = lookup.get(series["name"])
                    series["value"] = hit["amount"] if hit else None
                    series["notional"] = hit["notional"] if hit else False
                report["money"] = {"earned": m["earned"], "value": m["value"]}
                return self._send(200, report)
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
                    if isinstance(body.get("auto_update"), bool):
                        conf["auto_update"] = body["auto_update"]
                    cfg.save_config(conf)
                elif self.path == "/api/seen":
                    conf = cfg.load_config()
                    conf["last_seen_version"] = updater.current_version()
                    cfg.save_config(conf)
                elif self.path == "/api/update":
                    version = updater.update()
                    updater.restart_soon()
                    return self._send(200, {"ok": True, "version": version})
                elif self.path == "/api/reveal-app":
                    from .macapp import ensure_app
                    app = ensure_app()
                    if not app:
                        raise ValueError("Couldn't create the app on this computer.")
                    subprocess.run(["open", "-R", str(app)], capture_output=True)
                elif self.path in ("/api/entry/adjust", "/api/entry/add"):
                    start, seconds = _entry_times(body)
                    task_id = int(body["task_id"])
                    store = service.open_store()
                    if task_id not in {t["id"] for t in store.tasks()}:
                        raise ValueError("Choose a task.")
                    if self.path.endswith("add"):
                        store.add_manual(start, seconds, task_id)
                    else:
                        store.set_adjustment(int(body["id"]), start, seconds, task_id)
                    return self._send(200, {"ok": True})
                elif self.path == "/api/entry/reset":
                    service.open_store().clear_adjustment(int(body["id"]))
                    return self._send(200, {"ok": True})
                elif self.path == "/api/entry/delete":
                    entry_id = int(body["id"])
                    if entry_id > -MANUAL_ID_OFFSET:
                        raise ValueError("Only entries you added can be deleted. Use Exclude instead.")
                    service.open_store().delete_manual(entry_id)
                    return self._send(200, {"ok": True})
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
                elif self.path == "/api/income":
                    client = str(body.get("client") or "").strip()
                    month = str(body.get("month") or "")
                    try:
                        y, mo = (int(x) for x in month.split("-"))
                        date(y, mo, 1)
                    except ValueError:
                        raise ValueError("Choose a month.")
                    if not client:
                        raise ValueError("Choose a client.")
                    conf = cfg.load_config()
                    incomes = conf.setdefault("monthly_income", {})
                    months = incomes.setdefault(client, {})
                    if body.get("amount") in (None, ""):
                        months.pop(month, None)  # back to carrying the previous month's figure
                    else:
                        amount = float(body["amount"])
                        if amount < 0:
                            raise ValueError("Income can't be negative.")
                        months[month] = amount
                    cfg.save_config(conf)
                    return self._send(200, {"ok": True})
                elif self.path == "/api/ignored":
                    conf = cfg.load_config()
                    known = {t["id"] for t in service.open_store().tasks()}
                    conf["ignored_tasks"] = sorted({int(i) for i in body.get("ids", []) if int(i) in known})
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


def _entry_times(body: dict) -> tuple[datetime, int]:
    """Local date + start/end times from the form -> aware start and length in seconds."""
    try:
        day = date.fromisoformat(body["date"])
        sh, sm = (int(x) for x in body["start"].split(":")[:2])
        eh, em = (int(x) for x in body["end"].split(":")[:2])
    except (KeyError, ValueError):
        raise ValueError("Enter a date, a start time and an end time.")
    start = datetime(day.year, day.month, day.day, sh, sm).astimezone()
    end = datetime(day.year, day.month, day.day, eh, em).astimezone()
    if end <= start:
        raise ValueError("The end time must be after the start time.")
    return start, int((end - start).total_seconds())


def _clean_client(raw: dict) -> dict:
    billing = raw.get("billing") if raw.get("billing") in BILLING_STYLES else "hourly"
    rounding = raw.get("rounding") if raw.get("rounding") in ROUNDING_MODES else "none"
    block = int(raw.get("block_minutes") or 15)
    if not 1 <= block <= 240:
        raise ValueError("Rounding block must be between 1 and 240 minutes.")
    rate = raw.get("rate")
    rate = None if rate in (None, "") else float(rate)
    if rate is not None and rate < 0:
        raise ValueError("Hourly rate can't be negative.")
    return {"billing": billing, "rounding": rounding, "block_minutes": block, "rate": rate,
            "notional": bool(raw.get("notional"))}


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
    inv = invoice(service.open_store(), client, year, month, style,
                  c.get("block_minutes", 15), c.get("rounding", "none"))
    inv["rate"] = c.get("rate")
    return inv


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
    try:
        from .macapp import ensure_app
        ensure_app()  # existing installs get the app on their next restart or update
    except Exception as e:
        print(f"Couldn't create the app: {e}")
    server = ThreadingHTTPServer(("127.0.0.1", cfg.PORT), Handler)
    print(f"TimeFlip Tracker running at http://127.0.0.1:{cfg.PORT}")
    server.serve_forever()
