"""Local web page for limits, charts and timesheets. Listens on 127.0.0.1 only."""
from __future__ import annotations

import html
import io
import socket
import json
import zipfile
import subprocess
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import config as cfg
from . import allowance, areas, billing, collect, insights, mailer, merge, pdf, remote, service, updater
from .store import MANUAL_ID_OFFSET
from .limits import WEEKDAYS, parse_limits
from .reports import (BILLING_STYLES, ROUNDING_MODES, build_report, invoice, invoice_lines, money_summary, month_summary, targets_summary,
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
        "payment_terms_days": collect.terms_days(conf),
        "businesses": billing.businesses(conf),
        "areas": [{**a, "week_hours": next((t.get("week") for n, t in ((conf.get("targets") or {}).get("hours") or {}).items()
                                            if n.lower() == a["client"].lower()), None),
                   "allotted_hours": areas.allotted_hours(conf, store, a["client"]),
                   "bank": {k: (v.isoformat() if k == "start" else v)
                            for k, v in areas.bank_rules(conf, store, a["client"]).items()}}
                  for a in areas.configured(conf, store)],
        "client_details": conf.get("client_details", {}),
        "alert_levels_percent": conf.get("alert_levels_percent", [80, 100]),
        "limits": conf.get("limits", []),
        "tasks": sorted({t["name"] for t in tasks}),
        "task_list": [{"id": t["id"], "name": t["name"], "client": t["tag"]}
                      for t in sorted(tasks, key=lambda t: t["name"].lower())],
        "ignored_tasks": conf.get("ignored_tasks", []),
        "targets": conf.get("targets", {}),
        "clients": sorted({t["tag"] for t in tasks if t["tag"]}),
        "client_billing": conf.get("clients", {}),
        "status": status,
        "viewer_of": conf.get("viewer_of"),
    }


# On every page that isn't the main one: a way back. A phone's home-screen app has no browser Back button.
BACK_LINK = ('''<a class="btn back" href="/" onclick="if(history.length>1&&document.referrer.indexOf(location.origin)===0)'''
             '''{history.back();return false}">← Tracker</a>''')
# Copies each column heading onto its cells, so a table can stack on a phone and still say what each figure is.
LABEL_SCRIPT = ('''<script>for(const t of document.querySelectorAll("table.labelled")){const h=[...t.querySelectorAll("thead th")]'''
                '''.map(x=>x.textContent.trim());for(const r of t.querySelectorAll("tbody tr, tfoot tr"))[...r.children]'''
                '''.forEach((c,i)=>{if(i&&h[i]&&c.textContent.trim())c.dataset.label=h[i]})}</script>''')

PAGE_STYLE = """
body{font:14px/1.6 Inter,-apple-system,BlinkMacSystemFont,sans-serif;color:#111827;background:#fff;margin:32px}
h1,h2,button{font-family:Sora,Inter,sans-serif;letter-spacing:-0.02em}
h1{font-size:24px;font-weight:700;margin:0 0 4px}
h1::before{content:"";display:block;width:40px;height:3px;border-radius:2px;background:#F97316;margin-bottom:14px}
h2{font-size:17px;margin:28px 0 6px} p{color:#6B7280;margin:0 0 20px}
table{border-collapse:collapse;width:100%} th,td{padding:6px 8px;border-bottom:1px solid #E5E7EB;text-align:left}
th{font-weight:600;color:#6B7280} .n{text-align:right;font-variant-numeric:tabular-nums}
tr.sub td{font-weight:600;background:#FFF7ED}
tr.week td{font-family:Sora,Inter,sans-serif;font-weight:700;background:#F3F4F6;border-bottom:2px solid #D1D5DB}
.allow{border:1px solid #FED7AA;background:#FFF7ED;border-radius:10px;padding:10px 14px;margin:0 0 20px;font-size:13px}
.allow .warn,.allow a{color:#c2410c}
tr.rolled td{color:#9CA3AF;font-style:italic} tr.rolled td.keep{font-style:normal}
.carrybox{border:1px solid #FED7AA;border-radius:10px;padding:12px 14px;margin:-8px 0 20px;font-size:13px}
.carrybtns{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px} .carrybtns button{font-size:13px;padding:5px 12px}
.carrybtns button.primary{background:#F97316;border-color:#F97316;color:#fff} .carrybtns button:disabled{opacity:.4;cursor:default}
.carrybox .hint{margin:8px 0 0;font-size:12px}
.bankbox{border-color:#F97316;background:#FFF7ED;margin:0 0 20px}
.bankform{display:flex;flex-wrap:wrap;gap:10px 12px;align-items:flex-end;margin-top:12px}
.bankform label{font-size:12px;color:#6B7280;font-weight:600}
.bankform input,.bankform select{font:inherit;font-size:14px;padding:7px 10px;border:1px solid #D1D5DB;border-radius:8px;margin-top:3px;max-width:100%}
.bankform input[type=number]{width:110px} .bankbox button{font-size:13px;padding:7px 14px}
.bankform label{min-width:0} .bankform select{min-width:0;max-width:100%}
.bankbox button.primary{background:#F97316;border-color:#F97316;color:#fff}
label.roll{margin-left:8px} th.section{padding-top:18px;color:#0b0b0b;font-size:15px}
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
.lines td,.lines th{border-bottom-color:#FED7AA} label.smooth{font-size:14px;color:#111827}
button.copy{font-size:12px;padding:3px 10px;margin-left:6px;border-radius:6px}
.copied{color:#00c853;font-size:13px}
button.edit{font-size:12px;padding:2px 9px;margin-left:8px;border-radius:6px}
.bar button.secondary{background:#fff;color:#111827;border-color:#D1D5DB}
.bar a.btn{font:600 14px Sora,Inter,sans-serif;padding:8px 16px;border-radius:8px;background:#F97316;color:#fff;text-decoration:none}
.bar a.btn:hover{background:#c2410c}
.bar .done{color:#00a344;font-weight:600} .bar .warn{color:#c2410c}
.bar{flex-wrap:wrap} .bar button,.bar a.btn,.bar .done{white-space:nowrap} .bar>span:last-child{flex-basis:100%}
.bar button.secondary:hover{background:#fff;border-color:#F97316}
.tag.adj{color:#c2410c;font-style:italic}
dialog{border:none;border-radius:12px;padding:24px;box-shadow:0 10px 40px rgba(0,0,0,.25);min-width:340px}
dialog::backdrop{background:rgba(15,18,25,.45)}
dialog input,dialog select{font:inherit;padding:7px 10px;border:1px solid #D1D5DB;border-radius:8px;margin-top:4px}
dialog select{width:100%} .dlg-buttons{display:flex;gap:8px;margin-top:18px;align-items:center}
dialog button.primary{background:#F97316;border-color:#F97316;color:#fff} button.danger{color:#d03b3b}
.err{color:#d03b3b;min-height:1.2em;margin:10px 0 0}
.bar a.back{background:#fff;color:#111827;border:1px solid #D1D5DB} .bar a.back:hover{background:#fff;border-color:#F97316}
/* On a phone nothing scrolls sideways: each table row becomes a short block that wraps. */
@media screen and (max-width:700px){body{margin:14px;overflow-wrap:anywhere} h1{font-size:21px} .lines{padding:12px}
 table,tbody,tfoot{display:block;width:100%} thead{display:none}
 tr{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 10px;padding:9px 2px;border-bottom:1px solid #E5E7EB}
 th,td{display:block;border:none!important;padding:0;font-size:13px} td.n{margin-left:auto;white-space:nowrap}
 td.n+td.n{margin-left:0} td[colspan]{flex:1 1 45%;min-width:0} td:empty{display:none}
 td.keep{flex:1 1 100%;display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;margin-top:4px}
 td.keep button.edit,label.roll{margin-left:0} label.ex{font-size:13px}
 tr.sub,tr.week{padding:9px 8px;border-radius:8px;margin:2px 0} tr.week{background:#F3F4F6} tr.sub{background:#FFF7ED}
 tr.sub td,tr.week td{background:none!important} tr.grand{border-top:2px solid #1E2530;border-bottom:none}
 .tag{margin-left:0;display:block} button.copy{margin-left:4px}
 table.labelled tr{display:grid;grid-template-columns:1fr 1fr;gap:3px 12px}
 table.labelled td:first-child{grid-column:1/-1;font-weight:600} table.labelled td.n{margin:0;text-align:left!important;white-space:normal}
 table.labelled td[data-label]::before{content:attr(data-label) ": ";color:#6B7280;font-size:11px;font-weight:400}
 .bar{gap:8px} .bar button,.bar a.btn{padding:8px 12px;font-size:13px} .carrybtns button{flex:1 1 auto}
 .bankform label{flex:1 1 100%} .bankform select,.bankform input{width:100%!important;box-sizing:border-box}
 .bankform button{flex:1 1 100%}
 dialog{min-width:0;width:calc(100% - 32px)} dialog input,dialog select{max-width:100%}}
@media print{.bar,label.ex,.lines,button.edit,.carrybox{display:none} body{margin:0}}
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
for (const box of document.querySelectorAll("input[data-smooth]")) {
  box.addEventListener("change", async () => {
    try { await post("/api/smooth", {client: box.dataset.smooth, on: box.checked}); location.reload(); }
    catch (e) { alert(e.message); }
  });
}
for (const b of document.querySelectorAll("button.invoiced")) {
  b.addEventListener("click", async () => {
    b.disabled = true;
    try { await post("/api/invoiced", {client: b.dataset.client, month: b.dataset.month, invoiced: !!b.dataset.invoiced}); location.reload(); }
    catch (e) { alert(e.message); b.disabled = false; }
  });
}
for (const b of document.querySelectorAll("button.mkbill")) {
  b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      const r = await fetch("/api/bill/create", {method: "POST", headers: {"Content-Type": "application/json", "X-TFTrack": "1"},
                                                 body: JSON.stringify({client: b.dataset.client, month: b.dataset.month})});
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || "Couldn't create the invoice.");
      location.href = "/bill?id=" + data.ids[0];
    } catch (e) { alert(e.message); b.disabled = false; }
  });
}
for (const b of document.querySelectorAll("button.paid")) {
  b.addEventListener("click", async () => {
    b.disabled = true;
    try { await post("/api/paid", {client: b.dataset.client, month: b.dataset.month, paid: !!b.dataset.paid}); location.reload(); }
    catch (e) { alert(e.message); b.disabled = false; }
  });
}
const bankBox = document.querySelector(".bankbox[data-client]");
if (bankBox) {
  const useBank = (date, hours, mode) => post("/api/lieu/use", {client: bankBox.dataset.client, task_id: Number($f("bankTask").value),
                                                                 mode, date, hours});
  const fail = (e) => { $f("bankErr").textContent = e.message; };
  $f("bankWhere").addEventListener("change", () => { $f("bankDayWrap").style.display = $f("bankWhere").value === "day" ? "" : "none"; });
  $f("bankAdd").addEventListener("click", async () => {
    const hours = Number($f("bankHours").value), day = $f("bankWhere").value === "day";
    if (!(hours > 0)) return fail(new Error("Enter how many hours to add."));
    $f("bankAdd").disabled = true;
    try { await useBank(day ? $f("bankDay").value : $f("bankWhere").value, hours, "day"); location.reload(); }
    catch (e) { fail(e); $f("bankAdd").disabled = false; }
  });
  const fill = $f("bankFill");
  if (fill) fill.addEventListener("click", async () => {
    fill.disabled = true;
    try { for (const p of JSON.parse(fill.dataset.plan)) await useBank(p.date, p.seconds / 3600, "day"); location.reload(); }
    catch (e) { fail(e); setTimeout(() => location.reload(), 2500); }
  });
}
for (const b of document.querySelectorAll("button.carry")) {
  b.addEventListener("click", async () => {
    b.disabled = true;
    try { await post("/api/carry", {client: b.dataset.client, task: b.dataset.task, month: b.dataset.month, seconds: Number(b.dataset.seconds)}); location.reload(); }
    catch (e) { alert(e.message); b.disabled = false; }
  });
}
for (const box of document.querySelectorAll("input[data-roll]")) {
  box.addEventListener("change", async () => {
    box.disabled = true;
    try {
      await post("/api/rollover", {id: Number(box.dataset.roll), date: box.dataset.date, rolled: box.checked});
      location.reload();
    } catch (e) { alert(e.message); box.disabled = false; }
  });
}
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
          client: str | None = None, pdf_url: str = "", status: str = "") -> str:
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
<meta name="viewport" content="width=device-width, initial-scale=1"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<title>{esc(title)}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800&family=Inter:wght@400;500;600;700&display=swap">
<style>{PAGE_STYLE}</style></head><body data-defaults="{defaults}">
<div class="bar">{BACK_LINK}<a class="btn" href="{esc(pdf_url)}">Save PDF</a>
<button class="secondary" onclick="window.print()">Print</button>
<button id="addEntry" class="secondary">+ Add entry</button>
{status}<span>Use <b>Edit</b> to change an entry's times or task, or <b>Exclude</b> to leave it uncharged.</span></div>
<h1>{esc(heading)}</h1><p>{esc(subtitle)}</p>{table}{dialog}{PAGE_SCRIPT}{LABEL_SCRIPT}</body></html>"""


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
    q = f"start={start}&end={end}" + (f"&client={quote(client)}" if client else "")
    return _page(f"Timesheet – {who}", f"Timesheet: {who}", f"{start:%d %b %Y} to {end:%d %b %Y}", table,
                 start, client, pdf_url=f"/timesheet.pdf?{q}")


def _roll_cell(r: dict, inv: dict) -> str:
    """Roll over tick for an entry under an allowance: not charged now; its time joins the carried-over balance."""
    if r["excluded"] or not any(allowance.covers(al, r) for al in inv.get("allowances") or []):
        return ""
    checked = " checked" if r["rolled"] else ""
    return (f'<label class="ex roll"><input type="checkbox" data-roll="{int(r["id"])}" '
            f'data-date="{esc_attr(r["entry_date"])}"{checked}> Roll over</label>')


def esc_attr(v: str) -> str:
    return html.escape(str(v), quote=True)


def _entry_row(r: dict, day: date, inv: dict) -> str:
    esc = html.escape
    cls = "x" if r["excluded"] else "rolled" if r["rolled"] else ""
    task_cell = _task_cell(r)
    if r["rolled"] and not r["excluded"]:
        task_cell = task_cell[:-5] + ' <span class="tag adj">rolled over: carried to a later invoice</span></td>'
    return (f'<tr class="{cls}"><td>{day:%a %d %b}</td><td>{esc(r["start"])}–{esc(r["end"])}</td>{task_cell}'
            f'<td class=n>{_hm(r["seconds"])}</td>{_exclude_cell(r).replace("</td>", _roll_cell(r, inv) + "</td>", 1)}</tr>')


def _day_total(d: dict, day: date) -> str:
    banked = d.get("banked_s", 0)
    actual = ("" if d["actual_s"] == d["billed_s"] + banked else f' <span class="tag">(actual {_hm(d["actual_s"])})</span>')
    if banked:
        actual += f' <span class="tag adj">{_hm(banked)} over the weekly hours: banked, not charged here</span>'
    return (f'<tr class="sub"><td colspan=3>{day:%A %d %B} total{actual}</td>'
            f'<td class=n>{_hm(d["billed_s"])}</td><td class=n>{d["billed_s"] / 3600:.2f} h</td></tr>')


def invoice_page(inv: dict) -> str:
    """Monthly sheet for a client billed by the day or by the week."""
    esc = html.escape
    rounding = ("" if inv["rounding"] == "none" else
                f" · day totals rounded {'to the nearest' if inv['rounding'] == 'nearest' else inv['rounding'] + ' to'}"
                f" {inv['block_minutes']} minutes")
    als = inv.get("allowances") or []
    parts = []
    for g in inv["groups"]:
        rows = []
        week_rows = inv["style"] == "day"
        week_s = 0
        if g["label"]:
            rows.append(f'<tr><th colspan=4 style="padding-top:22px;color:#0b0b0b;font-size:15px">{esc(g["label"])}</th></tr>')
        if not g["days"]:
            rows.append('<tr><td colspan=4>No time recorded.</td></tr>')
        for i, d in enumerate(g["days"]):
            day = date.fromisoformat(d["date"])
            rows += [_entry_row(r, day, inv) for r in d["entries"]]
            rows.append(_day_total(d, day))
            if week_rows:
                week_s += d["billed_s"]
                monday = day - timedelta(days=day.weekday())
                nxt = date.fromisoformat(g["days"][i + 1]["date"]) if i + 1 < len(g["days"]) else None
                if nxt is None or nxt - timedelta(days=nxt.weekday()) != monday:
                    note = ""
                    for al in als:
                        w = next((w for w in al["weeks"] if w["monday"] == monday.isoformat()), None)
                        if not w:
                            continue
                        over = w["worked_s"] - w["max_s"]
                        extra = next((x["seconds"] for x in al["carry"]["by_week"] if x["monday"] == w["monday"]), 0)
                        note += (f' <span class="tag{" adj" if over > 0 else ""}">({esc(al["name"])} {_hm(w["worked_s"])} of '
                                 f'{_hm(w["max_s"])} max{f" · {_hm(over)} over" if over > 0 else ""}'
                                 f'{f" · +{_hm(extra)} carried over" if extra else ""})</span>')
                    rows.append(f'<tr class="week"><td colspan=3>Week of {monday:%a %d %b} total{note}</td>'
                                f'<td class=n>{_hm(week_s)}</td><td class=n>{week_s / 3600:.2f} h</td></tr>')
                    week_s = 0
        if g["label"]:
            banked = (f' <span class="tag">{_hm(g["banked_s"])} over the weekly hours: banked, not charged here</span>'
                      if g.get("banked_s") else "")
            rows.append(f'<tr class="sub"><td colspan=3>Week total{banked}</td><td class=n>{_hm(g["billed_s"])}</td>'
                        f'<td class=n>{g["billed_s"] / 3600:.2f} h</td></tr>')
        parts.append("".join(rows))
    month = date.fromisoformat(inv["month"] + "-01")
    days_line = (f'<tr class="grand"><td colspan=3>Days worked</td><td class=n>{inv["days_worked"]}</td><td></td></tr>'
                 if inv["style"] == "day" else "")
    extra_line = ""
    summary = ""
    for al in als:
        c, task = al["carry"], al["name"]
        for w in c["by_week"]:
            extra_line += (f'<tr class="grand"><td colspan=3>{esc(task)}: carried over, week of '
                           f'{date.fromisoformat(w["monday"]):%a %d %b}</td><td class=n>{_hm(w["seconds"])}</td>'
                           f'<td class=n>{w["seconds"] / 3600:.2f} h</td></tr>')
        if c["unplaced_s"]:
            extra_line += (f'<tr class="grand"><td colspan=3>{esc(task)}: carried over from earlier months</td>'
                           f'<td class=n>{_hm(c["unplaced_s"])}</td><td class=n>{c["unplaced_s"] / 3600:.2f} h</td></tr>')
        over = al["charging_s"] - al["max_s"]
        state = (f'<b class="warn">{_hm(over)} over the max</b> · tick <b>Roll over</b> on entries to carry them'
                 if over > 0 else f'{_hm(-over)} under the max')
        added = f' (incl. {_hm(c["added_s"])} carried over)' if c["added_s"] else ""
        rolling = f' · rolling over {_hm(al["rolling_s"])}' if al["rolling_s"] else ""
        if al.get("banked_s"):
            rolling += f' · {_hm(al["banked_s"])} over the weekly hours banked automatically'
        summary += (f'<div class="allow"><b>{esc(task)}, {month:%B %Y}:</b> max {_hm(al["max_s"])} '
                    f'({al["working_days"]} working days) · charging {_hm(al["charging_s"])}{added}{rolling} · {state}'
                    f' · <a href="/carried">Carried hours ledger</a></div>'
                    + _carry_box(inv, al))
    table = summary + f"""<table><thead><tr><th>Date</th><th>Time</th><th>Task</th><th class=n>Duration</th><th></th></tr></thead>
<tbody>{"".join(parts)}</tbody><tfoot>{days_line}{extra_line}
<tr class="grand"><td colspan=3>Total for {month:%B %Y}</td><td class=n>{_hm(inv["total_billed_s"])}</td>
<td class=n>{inv["total_billed_s"] / 3600:.2f} h</td></tr></tfoot></table>"""
    table = _bank_box(inv) + _lines_panel(inv) + table
    start, end = date.fromisoformat(inv["start"]), date.fromisoformat(inv["end"])
    style = "by the day" if inv["style"] == "day" else "by the week (weeks starting on a Monday in the month)"
    return _page(f"{inv['client']} – {month:%B %Y}", f"{inv['client']}: {month:%B %Y}",
                 f"{start:%d %b %Y} to {end:%d %b %Y} · billed {style}{rounding}", table, start, inv["client"],
                 pdf_url=f"/invoice.pdf?client={quote(inv['client'])}&month={inv['month']}", status=_invoiced_status(inv))


def _invoiced_status(inv: dict) -> str:
    """Mark as invoiced button, or when it was marked (and a warning if the timesheet has changed since)."""
    esc = html.escape
    data = f'data-client="{esc(inv["client"])}" data-month="{inv["month"]}"'
    done = inv.get("invoiced")
    if not done:
        return (f'<button class="mkbill" {data}>Create invoice</button>'
                f'<button class="secondary invoiced" {data} data-invoiced="1">Mark as invoiced</button>')
    when = datetime.fromisoformat(done["marked_at"]).astimezone()
    amount = f", £{done['amount']:,.2f}" if done["amount"] is not None else ""
    changed = ""
    if done["seconds"] != inv["total_billed_s"]:
        changed = (f' <b class="warn">Changed since: now {inv["total_billed_s"] / 3600:.2f} h</b>')
    if inv.get("paid"):
        paid_on = datetime.fromisoformat(inv["paid"]).astimezone()
        paid = (f'<span class="done">✓ Paid {paid_on:%-d %b %Y}</span>'
                f'<button class="secondary paid" {data} data-paid="">Not paid</button>')
    else:
        paid = f'<button class="secondary paid" {data} data-paid="1">Mark as paid</button>'
    bill = inv.get("bill")
    undo = (f'<a class="btn" href="/bill?id={bill["id"]}">Invoice {esc(bill["number"])}</a>' if bill else
            f'<button class="secondary invoiced" {data} data-invoiced="">Undo</button>')
    return (f'<span class="done">✓ Invoiced {when:%-d %b %Y} ({done["seconds"] / 3600:.2f} h{amount}){changed}</span>'
            f'{undo}{paid}')


def _carry_box(inv: dict, al: dict) -> str:
    """Buttons to add an allowance's carried-over hours to this month's invoice. Not printed."""
    c = al["carry"]
    if not (c["available_s"] or c["added_s"]):
        return ""
    esc = html.escape
    q, have, avail = 15 * 60, c["added_s"], c["available_s"]
    fill = min(avail, c["spare_s"])

    def btn(label: str, target: int, primary: bool = False) -> str:
        target = max(0, min(avail, target))
        off = " disabled" if target == have else ""
        return (f'<button class="carry{" primary" if primary else ""}" data-client="{esc(inv["client"])}" '
                f'data-task="{esc(al["key"])}" data-month="{inv["month"]}" data-seconds="{target}"{off}>{label}</button>')

    return f"""<div class="carrybox"><div><b>{esc(al["name"])}: carried over to charge: {_hm(avail)}</b> · adding to this invoice:
<b>{_hm(have)}</b> · left for later: {_hm(avail - have)}</div>
<div class="carrybtns">{btn(f"Fill spare weeks ({_hm(fill)})", fill, True)} {btn("+15m", have + q)} {btn("+30m", have + 2 * q)}
{btn("+1h", have + 4 * q)} {btn("Add all", avail)} {btn("−15m", have - q)} {btn("Clear", 0)}</div>
<p class="hint">Weeks under {esc(al["name"])}'s max have {_hm(c["spare_s"])} spare this month; added hours go into
those weeks first, one invoice line per week.</p></div>"""


def _bank_for_sheet(inv: dict) -> dict | None:
    """The client's hours in lieu, for the box at the top of its timesheet: what's banked, and this month's weeks
    with how much room each has under the hours a week."""
    store, conf = service.open_store(), cfg.load_config()
    rules = areas.bank_rules(conf, store, inv["client"])
    banked = areas.bank(store, conf, inv["client"], date.today())["seconds"]
    if banked <= 0 and not rules["auto"]:
        return None
    charged: dict[str, int] = {}
    worked_by_task: dict[int, int] = {}
    for g in inv["groups"]:
        for d in g["days"]:
            day = date.fromisoformat(d["date"])
            monday = (day - timedelta(days=day.weekday())).isoformat()
            charged[monday] = charged.get(monday, 0) + d["billed_s"]
            for r in d["entries"]:
                worked_by_task[r["task_id"]] = worked_by_task.get(r["task_id"], 0) + r["seconds"]
    start, end = date.fromisoformat(inv["start"]), date.fromisoformat(inv["end"])
    weeks, monday = [], start - timedelta(days=start.weekday())
    while monday <= end:
        got = charged.get(monday.isoformat(), 0)
        # a week that runs into another month is only partly on this sheet, so its spare room can't be told from here
        whole = monday >= start and monday + timedelta(days=6) <= end or inv["style"] == "week"
        weeks.append({"monday": monday.isoformat(), "first": max(monday, start).isoformat(), "charged_s": got, "whole": whole,
                      "spare_s": max(0, rules["limit_seconds"] - got) if rules["limit_seconds"] and whole else 0})
        monday += timedelta(days=7)
    tasks = [t for t in store.tasks() if (t["tag"] or "").lower() == inv["client"].lower()]
    tasks.sort(key=lambda t: (-worked_by_task.get(t["id"], 0), t["name"].lower()))
    return {"seconds": banked, "limit_seconds": rules["limit_seconds"], "weeks": weeks,
            "tasks": [{"id": t["id"], "name": t["name"]} for t in tasks]}


def _bank_box(inv: dict) -> str:
    """Hours in lieu at the top of the timesheet, with a way to add some to this month. Not printed."""
    b = inv.get("bank")
    if not b:
        return ""
    esc = html.escape
    client = esc(inv["client"])
    head = f'<div><b>Hours in lieu for {client}: {_hm(max(0, b["seconds"]))} banked</b></div>'
    if b["seconds"] <= 0:
        return (f'<div class="carrybox bankbox">{head}<p class="hint">Nothing to add. Hours over the weekly hours go in '
                f'here by themselves.</p></div>')
    if inv.get("invoiced"):
        return (f'<div class="carrybox bankbox">{head}<p class="hint">This month is already invoiced, so add them to a '
                f'later month. (To add them here, undo or void the invoice first.)</p></div>')
    short = [w for w in b["weeks"] if w["spare_s"] > 0]
    fill, plan = b["seconds"], []
    for w in short:   # what one press of "Fill short weeks" would do: earliest week first, until the bank runs out
        take = min(fill, w["spare_s"])
        take -= take % 900 or 0   # whole quarter hours
        if take > 0:
            plan.append({"date": w["first"], "seconds": take})
            fill -= take
    total = sum(p["seconds"] for p in plan)
    fill_btn = (f'<button class="primary" id="bankFill" data-plan="{esc(json.dumps(plan))}">Fill short weeks ({_hm(total)})</button>'
                if total else "")
    week_opts = "".join(
        f'<option value="{w["first"]}">Week of {date.fromisoformat(w["monday"]):%a %d %b} · {_hm(w["charged_s"])} charged'
        f'{" · " + _hm(w["spare_s"]) + " spare" if w["spare_s"] else "" if w["whole"] else " · partly in another month"}</option>'
        for w in b["weeks"])
    task_opts = "".join(f'<option value="{t["id"]}">{esc(t["name"])}</option>' for t in b["tasks"])
    limit = (f' Weeks under {_hm(b["limit_seconds"])} have room: <b>Fill short weeks</b> tops them up, earliest first.'
             if b["limit_seconds"] and total else "")
    return f"""<div class="carrybox bankbox" data-client="{client}" data-max="{b["seconds"]}">{head}
<div class="carrybtns">{fill_btn}</div>
<div class="bankform">
<label>Hours<br><input type="number" id="bankHours" min="0.25" step="0.25" max="{b["seconds"] / 3600:.2f}" placeholder="up to {b["seconds"] / 3600:g}"></label>
<label>Add to<br><select id="bankWhere">{week_opts}<option value="day">A day I choose…</option></select></label>
<label id="bankDayWrap" style="display:none">Day<br><input type="date" id="bankDay" min="{inv["start"]}" max="{inv["end"]}" value="{inv["start"]}"></label>
<label>Task<br><select id="bankTask">{task_opts}</select></label>
<button id="bankAdd">Add to this timesheet</button></div>
<p class="hint">Added hours come out of the bank straight away and show below as an “added” entry, charged in full.
Delete that entry to put them back.{limit}</p><p class="err" id="bankErr"></p></div>"""


def ledger_page(books: list[dict], today: date) -> str:
    """Carried hours month by month for every allowance: rolled over, added to invoices, and the balance left."""
    esc = html.escape
    money = lambda secs, rate: f"£{secs / 3600 * rate:,.2f}" if rate else ""
    hm = lambda secs: _hm(secs) if secs else "–"
    parts = []
    for b in books:
        who = f'{b["client"]} – {b["name"]}' if b["scope"] == "task" else f'{b["client"]} (all work)'
        rows = []
        for r in b["rows"]:
            month = date.fromisoformat(r["month"] + "-01")
            link = f'/invoice?client={quote(b["client"])}&month={r["month"]}'
            over = r["worked_s"] - r["max_s"]
            note = f' <span class="tag adj">{_hm(over)} over</span>' if over > 0 else ""
            brought = f' <span class="tag">+{_hm(r["opening_s"])} brought in</span>' if r["opening_s"] else ""
            status = ("so far" if r["current"] else "✓" if r["invoiced"] else '<span class="warn">not marked</span>')
            rows.append(f'<tr><td><a href="{link}">{month:%B %Y}</a>{brought}</td><td class=n>{_hm(r["max_s"])}</td>'
                        f'<td class=n>{_hm(r["worked_s"])}{note}</td><td class=n>{hm(r["rolled_s"])}</td>'
                        f'<td class=n>{hm(r["added_s"])}</td><td class=n><b>{_hm(r["balance_s"])}</b></td>'
                        f'<td class=n>{money(r["balance_s"], b["rate"])}</td><td>{status}</td></tr>')
        total = lambda k: sum(r[k] for r in b["rows"])
        parts.append(f"""<h2>{esc(who)}</h2>
<p>{b["hours_per_week"]:g}h a week, from {date.fromisoformat(b["start"] + "-01"):%B %Y}. Still to charge:
<b>{_hm(b["balance_s"])}</b>{" · " + money(b["balance_s"], b["rate"]) if b["rate"] else ""}</p>
<table class="labelled"><thead><tr><th>Month</th><th class=n>Max</th><th class=n>Worked</th><th class=n>Rolled over</th>
<th class=n>Added to invoice</th><th class=n>Balance left</th><th class=n>Value</th><th>Invoiced</th></tr></thead>
<tbody>{"".join(rows) or '<tr><td colspan=8>Nothing yet.</td></tr>'}</tbody>
<tfoot><tr class="grand"><td>Total</td><td></td><td class=n>{_hm(total("worked_s"))}</td>
<td class=n>{_hm(total("rolled_s"))}</td><td class=n>{_hm(total("added_s"))}</td><td class=n>{_hm(b["balance_s"])}</td>
<td class=n>{money(b["balance_s"], b["rate"])}</td><td></td></tr></tfoot></table>""")
    body = "".join(parts) or ("<p>No monthly allowances yet. In Settings → Limits, tick <b>Bill as allowance</b> on a "
                              "task's or a client's weekly limit.</p>")
    return f"""<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Carried hours</title>
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800&family=Inter:wght@400;500;600;700&display=swap">
<style>{PAGE_STYLE}
h2{{margin-top:36px}} td a{{color:#c2410c}} .warn{{color:#c2410c}} @media print{{.bar{{display:none}}}}</style></head><body>
<div class="bar">{BACK_LINK}<button onclick="window.print()">Print</button><span>As at {today:%d/%m/%Y}</span></div>
<h1>Carried hours</h1>
<p>Month by month for each allowance. Worked is everything tracked that month, including what was rolled over.
Balance left is what has been rolled over or brought in and not yet added to an invoice.</p>
{body}{LABEL_SCRIPT}</body></html>"""


def _lines_panel(inv: dict) -> str:
    """Copy-ready invoice lines for pasting into Revolut Business (or any invoicing app). Not printed."""
    esc = html.escape
    rate = inv.get("rate")
    smooth = bool(inv.get("smooth")) and inv["style"] == "week"
    lines = invoice_lines(inv, rate, smooth)
    real = {l["description"]: l["hours"] for l in invoice_lines(inv, rate)} if smooth else {}
    if not lines:
        return ""

    def btn(text: str, value: str) -> str:
        return f'<button class="copy" data-copy="{esc(value)}">{esc(text)}</button>'

    rows = "".join(
        f"<tr><td>{esc(l['description'])} {btn('Copy', l['description'])}</td>"
        f"<td class=n>{l['hours']:.2f} {btn('Copy', format(l['hours'], '.2f'))}"
        + (f'<br><span class="tag">real {real.get(l["description"], 0):.2f}</span>' if smooth else "") + "</td>"
        f"<td class=n>{'£' + format(rate, '.2f') if rate else '–'}</td>"
        f"<td class=n>{'£' + format(l['amount'], '.2f') if l['amount'] is not None else '–'}</td></tr>"
        for l in lines
    )
    total_hours = sum(l["hours"] for l in lines)
    total_amount = sum(l["amount"] or 0 for l in lines)
    all_text = "\n".join(f"{l['description']}\t{l['hours']:.2f}" + (f"\t{rate:.2f}" if rate else "") for l in lines)
    rate_note = ("" if rate else
                 ' Add an hourly rate for this client in <b>Settings → How each client is invoiced</b> to see amounts.')
    smooth_box = ""
    if inv["style"] == "week":
        smooth_box = (f'<p><label class="ex smooth"><input type="checkbox" data-smooth="{esc(inv["client"])}"'
                      f'{" checked" if smooth else ""}> Even out the weeks</label> <span class="tag">'
                      + ("the month's total spread evenly over its weeks, in whole blocks; same total, but the lines "
                         "won't match the week totals on the timesheet" if smooth else
                         "spread the month's total evenly over its weeks (same total)") + '</span></p>')
    return f"""<div class="lines bar-block">
<h2 style="margin-top:0">Invoice lines</h2>
<p>One line per {"day" if inv["style"] == "day" else "week"}, ready to copy into your invoice: description, then quantity
(hours), then rate.{rate_note}</p>{smooth_box}
<table class="labelled"><thead><tr><th>Description</th><th class=n>Hours</th><th class=n>Rate</th><th class=n>Amount</th></tr></thead>
<tbody>{rows}</tbody>
<tfoot><tr class="grand"><td>Total</td><td class=n>{total_hours:.2f}</td><td></td>
<td class=n>{'£' + format(total_amount, '.2f') if rate else '–'}</td></tr></tfoot></table>
<p style="margin-top:10px">{btn('Copy all lines', all_text)} <span class="copied" id="copied"></span></p>
</div>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _allowed_host(self) -> bool:
        # Refuse requests addressed to any other name (DNS rebinding protection). This Mac's own Tailscale name is
        # allowed too: Tailscale Serve passes those requests on from Dave's other devices.
        return self.headers.get("Host", "") in remote.allowed_hosts()

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
            viewer_of = cfg.load_config().get("viewer_of")
            if url.path == "/" and viewer_of and "local" not in q:
                return self._send(302, b"", "text/plain", {"Location": viewer_of})
            if url.path == "/viewer":
                return self._send(200, viewer_page(viewer_of), "text/html; charset=utf-8")
            if url.path == "/":
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if url.path in ("/apple-touch-icon.png", "/favicon.png"):
                return self._send(200, (STATIC / "apple-touch-icon.png").read_bytes(), "image/png")
            if url.path == "/api/remote":
                return self._send(200, {**remote.status(), "viewer_of": viewer_of})
            if url.path == "/api/export":
                name = f"timeflip-changes-{_file_name(socket.gethostname())}-{date.today()}.json"
                return self._send(200, json.dumps(merge.export(service.open_store(include_ignored=True),
                                                               cfg.load_config()), indent=1),
                                  "application/json", {"Content-Disposition": f'attachment; filename="{name}"'})
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
            if url.path == "/api/outstanding":
                return self._send(200, outstanding(date.today()))
            if url.path == "/api/areas":
                with LOCK:   # the first time, each area's bank gets a fixed start date
                    conf = cfg.load_config()
                    if areas.settle_starts(conf, service.open_store()):
                        cfg.save_config(conf)
                return self._send(200, {"areas": areas.cards(service.open_store(), conf, date.today(),
                                                             allowance.bank_holidays()[0] or set())})
            if url.path == "/api/area":
                try:
                    client = q.get("client") or ""
                    return self._send(200, areas.detail(service.open_store(), cfg.load_config(), client, date.today(),
                                                        _carried(client, date.today()),
                                                        allowance.bank_holidays()[0] or set()))
                except ValueError as e:
                    return self._send(404, {"error": str(e)})
            if url.path == "/api/insights":
                return self._send(200, insights.build(service.open_store(), cfg.load_config(), date.today()))
            if url.path == "/api/bills":
                return self._send(200, bills_overview(date.today()))
            if url.path in ("/bill", "/bill.pdf"):
                store = service.open_store()
                bill = store.bill(q.get("id"))
                if not bill:
                    return self._send(404, "No such invoice.", "text/plain; charset=utf-8")
                if url.path == "/bill.pdf":
                    return self._send(200, billing.bill_pdf(bill), "application/pdf",
                                      {"Content-Disposition": f'attachment; filename="{billing.file_name(bill)}"'})
                paid_at = store.paid(bill["client"]).get(bill["month"])
                conf = cfg.load_config()
                sends = next((b for b in billing.businesses(conf) if b["id"] == bill["business_id"]), {})
                to = billing.client_details(conf, bill["client"])["email"] or bill["data"]["client"].get("email") or ""
                return self._send(200, billing.bill_page(bill, paid_at, date.today(), store.get_meta(f"bill_emailed:{bill['id']}"),
                                                         sends.get("mail_method") == "smtp", to), "text/html; charset=utf-8")
            if url.path == "/sheet.pdf":
                raw, name = _sheet_pdf(q.get("client") or "", q.get("month") or "")
                return self._send(200, raw, "application/pdf", {"Content-Disposition": f'attachment; filename="{name}"'})
            if url.path == "/bills.zip":
                store = service.open_store()
                out = io.BytesIO()
                with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                    names = set()
                    for bill_id in (q.get("ids") or "").split(","):
                        bill = store.bill(bill_id)
                        if bill and not bill["voided_at"]:
                            name = billing.file_name(bill)
                            if name in names:   # two businesses can share a number
                                name = f"{bill['data']['business']['name']} {name}"
                            names.add(name)
                            z.writestr(name, billing.bill_pdf(bill))
                            raw, name = _sheet_pdf(bill["client"], bill["month"])
                            z.writestr(name, raw)
                return self._send(200, out.getvalue(), "application/zip",
                                  {"Content-Disposition": f'attachment; filename="Invoices-{date.today()}.zip"'})
            if url.path == "/api/collect":
                return self._send(200, money_to_collect(date.today()))
            if url.path == "/api/allowances":
                return self._send(200, allowance.summary(service.open_store(), cfg.load_config(), date.today()))
            if url.path == "/api/targets":
                return self._send(200, targets_summary(service.open_store(), date.today(), cfg.load_config()))
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
            if url.path == "/carried":
                holidays, _ = allowance.bank_holidays()
                books = allowance.ledger(service.open_store(), cfg.load_config(), date.today(), holidays or set())
                return self._send(200, ledger_page(books, date.today()), "text/html; charset=utf-8")
            if url.path == "/invoice":
                return self._send(200, invoice_page(_invoice(q)), "text/html; charset=utf-8")
            if url.path == "/invoice.pdf":
                inv = _invoice(q)
                month = date.fromisoformat(inv["month"] + "-01")
                return self._send(200, pdf.invoice_pdf(inv), "application/pdf",
                                  {"Content-Disposition": f'attachment; filename="{_file_name(inv["client"])}'
                                                          f'-{month:%B-%Y}.pdf"'})
            if url.path in ("/timesheet", "/timesheet.csv", "/timesheet.pdf"):
                client = q.get("client") or None
                if q.get("month") and client:  # use the client's month range
                    inv = _invoice(q)
                    start, end = date.fromisoformat(inv["start"]), date.fromisoformat(inv["end"])
                rows = timesheet_rows(service.open_store(), start, end, client)
                if url.path == "/timesheet":
                    return self._send(200, timesheet_page(rows, start, end, client), "text/html; charset=utf-8")
                if url.path == "/timesheet.pdf":
                    return self._send(200, pdf.timesheet_pdf(rows, start, end, client), "application/pdf",
                                      {"Content-Disposition": f'attachment; filename="Timesheet-'
                                                              f'{_file_name(client or "all")}-{start}-to-{end}.pdf"'})
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
                    allowance.validate(limits, service.open_store().tasks())
                    conf["limits"] = limits
                    cfg.save_config(conf)
                elif self.path == "/api/settings":
                    conf = cfg.load_config()
                    if body.get("week_starts") in WEEKDAYS:
                        conf["week_starts"] = body["week_starts"]
                    if isinstance(body.get("auto_update"), bool):
                        conf["auto_update"] = body["auto_update"]
                    if body.get("payment_terms_days") not in (None, ""):
                        days = int(body["payment_terms_days"])
                        if not 0 <= days <= 365:
                            raise ValueError("Payment terms must be between 0 and 365 days.")
                        conf["payment_terms_days"] = days
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
                elif self.path == "/api/targets":
                    def amount(v, what):
                        if v in (None, ""):
                            return None
                        v = float(v)
                        if v < 0:
                            raise ValueError(f"{what} can't be negative.")
                        return v
                    conf = cfg.load_config()
                    inc = body.get("income") or {}
                    hours = {}
                    for client, t in (body.get("hours") or {}).items():
                        t = {p: amount((t or {}).get(p), "Target hours") for p in ("day", "week", "month")}
                        if any(v for v in t.values()):
                            hours[str(client)] = t
                    conf["targets"] = {
                        "income": {p: amount(inc.get(p), "Target income") for p in ("day", "week", "month")},
                        "weekends": bool(body.get("weekends")),
                        "hours": hours,
                    }
                    cfg.save_config(conf)
                elif self.path == "/api/smooth":
                    client = str(body.get("client") or "").strip()
                    if not client:
                        raise ValueError("Choose a client.")
                    conf = cfg.load_config()
                    smooth = [c for c in conf.get("smooth_weeks", []) if c.lower() != client.lower()]
                    conf["smooth_weeks"] = smooth + ([client] if body.get("on") else [])
                    cfg.save_config(conf)
                    return self._send(200, {"ok": True})
                elif self.path == "/api/invoiced":
                    client = str(body.get("client") or "").strip()
                    y, m = (int(x) for x in str(body.get("month") or "").split("-"))
                    date(y, m, 1)
                    month = f"{y}-{m:02d}"
                    if not client:
                        raise ValueError("Choose a client.")
                    store = service.open_store()
                    if body.get("invoiced"):
                        secs, amount, _ = _month_due(store, cfg.load_config(), client, month)
                        store.set_invoiced(client, month, secs, amount)
                        _fix_banked(store, client, month)
                    else:
                        bill = store.bill_for(client, month)
                        if bill:
                            raise ValueError(f"This month has invoice {bill['number']}. Void that invoice instead.")
                        store.set_invoiced(client, month, None)
                    return self._send(200, {"ok": True})
                elif self.path == "/api/business":
                    conf = cfg.load_config()
                    raw = body.get("businesses")
                    before = {b["id"]: b for b in billing.businesses(conf)}
                    cleaned = billing.clean_businesses(raw)
                    for b, given in zip(cleaned, raw):
                        b["smtp_password_saved"] = bool(before.get(b["id"], {}).get("smtp_password_saved"))
                        if given.get("smtp_password"):   # typed just now: into the Keychain, never the settings file
                            mailer.save_password(b["id"], str(given["smtp_password"]))
                            b["smtp_password_saved"] = True
                    for gone in set(before) - {b["id"] for b in cleaned}:
                        mailer.delete_password(gone)
                    conf["businesses"] = cleaned
                    conf.pop("business", None)
                    cfg.save_config(conf)
                elif self.path == "/api/business/test-email":
                    conf = cfg.load_config()
                    b = next((x for x in billing.businesses(conf) if x["id"] == body.get("id")), None)
                    if not b:
                        raise ValueError("Save the business first.")
                    if not b["email"]:
                        raise ValueError("Enter and save this business's email address first.")
                    subject = f"Test email from TimeFlip Tracker ({b['name']})"
                    text = "If you're reading this, invoices from this business can be emailed."
                    if b["mail_method"] == "smtp":
                        mailer.send(b, mailer.get_password(b["id"]), b["email"], subject, text, [])
                    else:
                        mailer.draft(subject, text, b["email"], b["email"], [], 0)
                    return self._send(200, {"ok": True, "mode": "sent" if b["mail_method"] == "smtp" else "draft"})
                elif self.path == "/api/client-details":
                    conf = cfg.load_config()
                    conf["client_details"] = {
                        str(name).strip(): billing.clean_client_details(d or {})
                        for name, d in (body.get("details") or {}).items() if str(name).strip()
                    }
                    cfg.save_config(conf)
                elif self.path == "/api/bill/create":
                    today = date.today()
                    if body.get("all"):
                        wanted = [(r["client"], r["month"]) for r in bills_overview(today)["ready"]]
                    else:
                        client = str(body.get("client") or "").strip()
                        y, m = (int(x) for x in str(body.get("month") or "").split("-"))
                        date(y, m, 1)
                        wanted = [(client, f"{y}-{m:02d}")]
                    made, errors = [], []
                    for client, month in wanted:
                        try:
                            bill = _create_bill(client, month, today)
                            made.append({"id": bill["id"], "number": bill["number"]})
                        except ValueError as e:
                            if not body.get("all"):
                                raise
                            errors.append(str(e))
                    return self._send(200, {"ok": True, "ids": [b["id"] for b in made],
                                            "numbers": [b["number"] for b in made], "errors": errors})
                elif self.path == "/api/areas":
                    conf = cfg.load_config()
                    known = sorted({t["tag"] for t in service.open_store().tasks() if t["tag"]})
                    before = {a["client"].lower(): a for a in conf.get("areas") or []}
                    conf["areas"] = areas.clean(body.get("areas"), known)
                    for a in conf["areas"]:   # keep a start date the page didn't send
                        old = before.get(a["client"].lower(), {})
                        a.setdefault("bank_start", old.get("bank_start") or "")
                        if not a["bank_start"]:
                            del a["bank_start"]
                    areas.set_allotted(conf, body.get("areas"), known)
                    areas.settle_starts(conf, service.open_store())
                    cfg.save_config(conf)
                elif self.path == "/api/lieu/bank":
                    client = str(body.get("client") or "").strip()
                    try:
                        seconds = round(float(body.get("hours")) * 3600)
                    except (TypeError, ValueError):
                        raise ValueError("Enter a number of hours.")
                    store = service.open_store()
                    if not client or not seconds:
                        raise ValueError("Enter a number of hours.")
                    if areas.bank(store, cfg.load_config(), client, date.today())["seconds"] + seconds < 0:
                        raise ValueError("You can't take out more than is in the bank.")
                    note = str(body.get("note") or "").strip()[:200] or ("Added by hand" if seconds > 0 else "Taken out by hand")
                    store.add_lieu(client, seconds, note)
                    return self._send(200, {"ok": True})
                elif self.path == "/api/lieu/use":
                    client = str(body.get("client") or "").strip()
                    try:
                        seconds = round(float(body.get("hours")) * 3600)
                        when = date.fromisoformat(str(body.get("date") or ""))
                    except (TypeError, ValueError):
                        raise ValueError("Enter the hours and choose a date.")
                    areas.use_lieu(service.open_store(), cfg.load_config(), client, int(body.get("task_id") or 0),
                                   "week" if body.get("mode") == "week" else "day", when, seconds)
                    return self._send(200, {"ok": True})
                elif self.path == "/api/extra/add":
                    client = str(body.get("client") or "").strip()
                    y, m = (int(x) for x in str(body.get("month") or "").split("-"))
                    date(y, m, 1)
                    if not client:
                        raise ValueError("Choose a client.")
                    e = billing.clean_extra(body)
                    store = service.open_store()
                    if store.bill_for(client, f"{y}-{m:02d}"):
                        raise ValueError(f"{client} has already been invoiced for that month. Pick the next month, "
                                         "or void that invoice first.")
                    store.add_extra(client, f"{y}-{m:02d}", e["kind"], e["description"], e["quantity"], e["unit_price"])
                    return self._send(200, {"ok": True})
                elif self.path == "/api/extra/delete":
                    service.open_store().delete_extra(int(body.get("id")))
                    return self._send(200, {"ok": True})
                elif self.path == "/api/bill/email":
                    store = service.open_store()
                    bill = store.bill(body.get("id"))
                    if not bill or bill["voided_at"]:
                        raise ValueError("No such invoice.")
                    conf = cfg.load_config()
                    # addresses as they are in Settings now, so one added after the invoice was made still works
                    now = next((b for b in billing.businesses(conf) if b["id"] == bill["business_id"]), {})
                    sender = now.get("email") or bill["data"]["business"].get("email") or ""
                    to = billing.client_details(conf, bill["client"])["email"] or bill["data"]["client"].get("email") or ""
                    subject, text = billing.email_text(bill)
                    sheet, sheet_name = _sheet_pdf(bill["client"], bill["month"])
                    files = [(billing.file_name(bill), billing.bill_pdf(bill)), (sheet_name, sheet)]
                    direct = now.get("mail_method") == "smtp"
                    if direct:
                        mailer.send(now, mailer.get_password(now["id"]), to, subject, text, files)
                    else:
                        mailer.draft(subject, text, to, sender, files, bill["id"])
                    store.set_meta(f"bill_emailed:{bill['id']}",
                                   f"{'sent' if direct else 'draft'}:{datetime.now().astimezone().isoformat()}")
                    return self._send(200, {"ok": True, "to": to, "sender": sender, "mode": "sent" if direct else "draft"})
                elif self.path == "/api/bill/void":
                    if not service.open_store().void_bill(body.get("id")):
                        raise ValueError("No such invoice.")
                    return self._send(200, {"ok": True})
                elif self.path == "/api/paid":
                    store = service.open_store()
                    if body.get("all"):
                        store.mark_all_paid()
                        return self._send(200, {"ok": True})
                    client = str(body.get("client") or "").strip()
                    y, m = (int(x) for x in str(body.get("month") or "").split("-"))
                    date(y, m, 1)
                    month = f"{y}-{m:02d}"
                    if not client:
                        raise ValueError("Choose a client.")
                    if body.get("paid") and month not in store.invoiced(client):
                        raise ValueError("Mark the month as invoiced first.")
                    store.set_paid(client, month, bool(body.get("paid")))
                    return self._send(200, {"ok": True})
                elif self.path == "/api/dismiss":
                    store = service.open_store()
                    if body.get("reset"):
                        store.set_dismissed(None)
                    else:
                        key = str(body.get("key") or "")[:300]
                        if not key:
                            raise ValueError("Nothing to hide.")
                        store.set_dismissed(key, body.get("dismissed", True) is not False)
                    return self._send(200, {"ok": True})
                elif self.path == "/api/remote/enable":
                    return self._send(200, remote.enable())
                elif self.path == "/api/remote/disable":
                    remote.disable()
                    return self._send(200, remote.status())
                elif self.path == "/api/viewer/check":
                    return self._send(200, remote.check_main(str(body.get("url") or "")))
                elif self.path == "/api/viewer/start":
                    return self._send(200, remote.make_viewer(str(body.get("url") or "")))
                elif self.path == "/api/viewer/stop":
                    remote.stop_viewer()
                    return self._send(200, {"ok": True})
                elif self.path == "/api/import":
                    conf, report = merge.import_changes(service.open_store(include_ignored=True), cfg.load_config(), body)
                    cfg.save_config(conf)
                    return self._send(200, {"report": report})
                elif self.path == "/api/rollover":
                    service.open_store().set_rolled(int(body["id"]), "carried" if body.get("rolled") else None,
                                                    str(date.fromisoformat(body["date"])))
                    return self._send(200, {"ok": True})
                elif self.path == "/api/carry":
                    client = str(body.get("client") or "").strip()
                    y, m = (int(x) for x in str(body.get("month") or "").split("-"))
                    date(y, m, 1)
                    seconds = int(body.get("seconds") or 0)
                    if not client or seconds < 0:
                        raise ValueError("Choose a client and an amount.")
                    allowance.set_added(service.open_store(), cfg.load_config(), client,
                                        str(body.get("task") or ""), f"{y}-{m:02d}", seconds)
                    return self._send(200, {"ok": True})
                elif self.path == "/api/sync":
                    service.sync(cfg.load_config(), service.open_store())
                else:
                    return self._send(404, {"error": "not found"})
            return self._send(200, state())
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})


def viewer_page(viewer_of: str | None) -> str:
    """This Mac's own page when it's a viewer: where it points, and how to stop."""
    esc = html.escape
    body = (f'<p>This Mac shows the TimeFlip tracker on your main Mac:</p><p><a class="btn" href="{esc(viewer_of)}">'
            f'Open it</a></p><p class="small">{esc(viewer_of)}</p>'
            '<p><button id="stop">Stop being a viewer</button></p><p class="small">Stopping goes back to this Mac\'s own '
            'tracker and data, as they were when it became a viewer. TimeFlip syncing starts again within 5 minutes.</p>'
            if viewer_of else '<p>This Mac uses its own tracker. <a href="/">Open it</a>.</p>')
    return f"""<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>TimeFlip Tracker – viewer</title>
<style>body{{font:15px/1.6 -apple-system,Inter,sans-serif;color:#111827;max-width:560px;margin:40px auto;padding:0 16px}}
a.btn,button{{font:600 14px -apple-system,sans-serif;padding:9px 16px;border-radius:8px;border:1px solid #D1D5DB;background:#fff;cursor:pointer;text-decoration:none;color:#111827}}
a.btn{{background:#F97316;border-color:#F97316;color:#fff}} .small{{color:#6B7280;font-size:13px}}</style></head>
<body><h1>TimeFlip Tracker</h1>{body}
<script>const b=document.getElementById("stop");if(b)b.onclick=async()=>{{b.disabled=true;
await fetch("/api/viewer/stop",{{method:"POST",headers:{{"Content-Type":"application/json","X-TFTrack":"1"}},body:"{{}}"}});
location.href="/";}};</script></body></html>"""


def _file_name(s: str) -> str:
    """Safe ASCII for a download's file name."""
    return "".join(c if c.isascii() and (c.isalnum() or c in "-_") else "-" for c in s).strip("-") or "timesheet"


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
    holidays, _ = allowance.bank_holidays()
    al = allowance.for_invoice(service.open_store(), cfg.load_config(), inv, holidays or set())
    if al:
        allowance.apply(inv, al)
    # Hours over the weekly hours are in the bank: leave them off the days they were worked on.
    held = areas.held_back(service.open_store(), cfg.load_config(), client, date.today())
    inv["banked_s"] = 0
    for g in inv["groups"]:
        g["banked_s"] = 0
        for d in g["days"]:
            d["banked_s"] = min(held.get(d["date"], 0), d["billed_s"])
            d["billed_s"] -= d["banked_s"]
            g["banked_s"] += d["banked_s"]
        g["billed_s"] -= g["banked_s"]
        inv["banked_s"] += g["banked_s"]
    inv["total_billed_s"] -= inv["banked_s"]
    if inv["banked_s"]:
        inv["days_worked"] = sum(1 for g in inv["groups"] for d in g["days"] if d["billed_s"] > 0)
        if al:   # the allowance line says what's being charged: that's now less the banked hours
            al[0]["banked_s"] = inv["banked_s"]
            al[0]["charging_s"] -= min(inv["banked_s"], al[0]["charging_s"])
    inv["rate"] = c.get("rate")
    inv["smooth"] = client.lower() in [s.lower() for s in cfg.load_config().get("smooth_weeks", [])]
    inv["invoiced"] = service.open_store().invoiced(client).get(inv["month"])
    inv["paid"] = service.open_store().paid(client).get(inv["month"])
    inv["bill"] = service.open_store().bill_for(client, inv["month"])
    inv["bank"] = _bank_for_sheet(inv)
    return inv


def _month_due(store, conf: dict, client: str, month: str) -> tuple[int, float | None, dict | None]:
    """What a client's invoice for a month comes to: (seconds, amount, invoice data for by-day/by-week clients)."""
    c = _client_settings(conf, client)
    rate = c.get("rate")
    if c.get("billing") in ("day", "week"):
        inv = _invoice({"client": client, "month": month})
        lines = invoice_lines(inv, rate, bool(inv.get("smooth")))   # what you'd actually copy into the invoice
        return inv["total_billed_s"], (round(sum(l["amount"] or 0 for l in lines) + _extras_total(store, client, month), 2)
                                       if rate else None), inv
    y, m = (int(x) for x in month.split("-"))
    first = date(y, m, 1)
    last = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    secs = sum(r["seconds"] for r in timesheet_rows(store, first, last, client) if not r["excluded"])
    return secs, (round(secs / 3600 * rate + _extras_total(store, client, month), 2) if rate else None), None


def _extras_total(store, client: str, month: str) -> float:
    """Extras waiting to go on this client's invoice for the month."""
    return sum(l["amount"] for l in billing.extra_lines(store.extras(client, month)))


def _months(start: str, end: str) -> list[str]:
    out, m = [], start
    while m <= end:
        out.append(m)
        m = allowance.next_month(m)
    return out


def outstanding(today: date) -> dict:
    """Per paying client: months not yet marked as invoiced (this one so far, and any earlier ones), and
    carried-over time not yet charged, with values.

    Earlier months count from the first month you ever marked as invoiced for that client; before you've marked
    any, just last month and this one.
    """
    conf = cfg.load_config()
    store = service.open_store()
    month = f"{today:%Y-%m}"
    out = []
    for client in sorted({t["tag"] for t in store.tasks() if t["tag"]}, key=str.lower):
        c = _client_settings(conf, client)
        if c.get("notional") or c.get("billing") == "monthly":
            continue   # own-business and monthly-income clients aren't invoiced by the hour
        rate = c.get("rate")
        marked = store.invoiced(client)
        start = min(marked) if marked else _month_before(month)
        months, carried = [], 0
        for m in _months(start, month):
            secs, amount, inv = _month_due(store, conf, client, m)
            if m == month and inv:
                carried = inv["rolled_s"] + sum(al["carry"]["remaining_s"] for al in inv.get("allowances") or [])
            if m not in marked and secs:
                months.append({"month": m, "seconds": secs, "value": amount, "current": m == month})
        if not months and not carried:
            continue
        due = sum(x["seconds"] for x in months)
        out.append({"client": client, "rate": rate, "months": months, "due_s": due,
                    "due_value": round(sum(x["value"] or 0 for x in months), 2) if rate else None,
                    "carried_s": carried, "carried_value": round(carried / 3600 * rate, 2) if rate else None})
    total = lambda key: round(sum(c[key] or 0 for c in out), 2)
    return {"month": month, "clients": out, "due_value": total("due_value"), "carried_value": total("carried_value"),
            "missing_rate": [c["client"] for c in out if not c["rate"]]}


def _carried(client: str, today: date) -> int:
    """Carried-over time not yet charged for a client with a monthly allowance (rolled-over entries and what's left
    of the balance). Nothing for other clients."""
    try:
        c = _client_settings(cfg.load_config(), client)
        if c.get("billing") not in ("day", "week") or c.get("notional"):
            return 0
        inv = _invoice({"client": client, "month": f"{today:%Y-%m}"})
        return inv["rolled_s"] + sum(al["carry"]["remaining_s"] for al in inv.get("allowances") or [])
    except (ValueError, KeyError):
        return 0


def _fix_banked(store, client: str, month: str) -> None:
    """A month invoiced by the week has just been invoiced: fix what each of its weeks held back for the bank."""
    c = _client_settings(cfg.load_config(), client)
    if c.get("billing") in ("day", "week") and not c.get("notional"):
        inv = _invoice({"client": client, "month": month})
        store.fix_banked_days(client, month, {d["date"]: d["banked_s"] for g in inv["groups"] for d in g["days"]})


def _month_dates(month: str) -> tuple[date, date]:
    y, m = (int(x) for x in month.split("-"))
    return date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)


def _bill_lines(store, conf: dict, client: str, month: str) -> tuple[list[dict], int]:
    """The lines an invoice for this client's month would carry, and the time they cover."""
    c = _client_settings(conf, client)
    if c.get("notional") or c.get("billing") == "monthly":
        raise ValueError(f"{client} isn't invoiced by the hour.")
    rate = c.get("rate")
    if c.get("billing") in ("day", "week"):
        inv = _invoice({"client": client, "month": month})
        return invoice_lines(inv, rate, bool(inv.get("smooth"))), inv["total_billed_s"]
    first, last = _month_dates(month)
    by_task: dict[str, int] = {}
    for r in timesheet_rows(store, first, last, client):
        if not r["excluded"]:
            by_task[r["task"]] = by_task.get(r["task"], 0) + r["seconds"]
    lines = []
    for task, secs in sorted(by_task.items(), key=lambda kv: kv[0].lower()):
        hours = round(secs / 3600, 2)
        lines.append({"description": f"{client} – {task}, {billing.month_title(month)}", "hours": hours, "rate": rate,
                      "amount": round(hours * rate, 2) if rate else None})
    return lines, sum(by_task.values())


def _create_bill(client: str, month: str, today: date) -> dict:
    conf = cfg.load_config()
    store = service.open_store()
    if not client:
        raise ValueError("Choose a client.")
    lines, seconds = _bill_lines(store, conf, client, month)
    lines = lines + billing.extra_lines(store.extras(client, month))
    bill, conf = billing.create(store, conf, client, month, lines, seconds, today, collect.terms_days(conf))
    cfg.save_config(conf)
    _fix_banked(store, client, month)
    return bill


def _sheet_pdf(client: str, month: str) -> tuple[bytes, str]:
    """The timesheet that backs up an invoice, as a PDF."""
    c = _client_settings(cfg.load_config(), client)
    name = f"Timesheet {_file_name(client)} {billing.month_title(month)}.pdf"
    if c.get("billing") in ("day", "week"):
        return pdf.invoice_pdf(_invoice({"client": client, "month": month})), name
    first, last = _month_dates(month)
    return pdf.timesheet_pdf(timesheet_rows(service.open_store(), first, last, client), first, last, client), name


def bills_overview(today: date) -> dict:
    """For the Invoices tab: finished months ready to invoice, every invoice made, and what's still to fill in."""
    conf = cfg.load_config()
    store = service.open_store()
    ready = []
    for c in outstanding(today)["clients"]:
        for mo in c["months"]:
            if not mo["current"]:
                need = billing.missing(conf, c["client"]) + ([] if c["rate"] else [f"an hourly rate for {c['client']}"])
                ready.append({"client": c["client"], "month": mo["month"], "label": billing.month_title(mo["month"]),
                              "business": billing.business_for(conf, c["client"])["name"],
                              "seconds": mo["seconds"], "value": mo["value"], "missing": need})
    paid = {}
    bills = []
    direct = {b["id"] for b in billing.businesses(conf) if b["mail_method"] == "smtp"}
    for b in store.bills():
        if b["client"].lower() not in paid:
            paid[b["client"].lower()] = store.paid(b["client"])
        paid_at = None if b["voided_at"] else paid[b["client"].lower()].get(b["month"])
        bills.append({**billing.summary(b, paid_at, today), "label": billing.month_title(b["month"]), "paid_at": paid_at,
                      "direct": b["business_id"] in direct,
                      "to": billing.client_details(conf, b["client"])["email"] or b["data"]["client"].get("email") or ""})
    live = [b for b in bills if b["status"] != "void"]
    extras = [{**e, "amount": round(e["quantity"] * e["unit_price"], 2), "label": billing.month_title(e["month"])}
              for e in store.extras()]
    return {"ready": ready, "bills": bills, "missing": billing.missing(conf), "extras": extras,
            "several": len(billing.businesses(conf)) > 1,
            "awaiting": round(sum(b["total"] for b in live if b["status"] == "awaiting"), 2),
            "overdue": round(sum(b["total"] for b in live if b["status"] == "overdue"), 2),
            "paid": round(sum(b["total"] for b in live if b["status"] == "paid"), 2)}


def money_to_collect(today: date) -> dict:
    """Prompts for the Dashboard's Money to collect panel."""
    conf = cfg.load_config()
    store = service.open_store()
    holidays, _ = allowance.bank_holidays()
    due = lambda client, month: _month_due(store, conf, client, month)[:2]
    return collect.build(store, conf, today, outstanding(today), due, holidays or set())


def _month_before(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


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
    if raw.get("allowance"):
        if not (out.get("task") or out.get("client")) or not out.get("weekly_hours"):
            raise ValueError("A monthly allowance needs hours per week.")
        try:
            y, m = (int(x) for x in str(raw.get("allowance_start") or "").split("-"))
            date(y, m, 1)
        except ValueError:
            raise ValueError("Choose the month the allowance starts.")
        opening = raw.get("opening_hours")
        opening = 0.0 if opening in (None, "") else float(opening)
        if opening < 0:
            raise ValueError("Hours brought in can't be negative.")
        out.update(allowance=True, allowance_start=f"{y}-{m:02d}", opening_hours=opening)
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
