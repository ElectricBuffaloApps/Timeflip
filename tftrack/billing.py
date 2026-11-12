"""Invoices made in the app: your business details, numbering, VAT, and the invoice itself as a page and a PDF.

An invoice is a snapshot. Once created, its lines, totals and the business and client details on it never change,
whatever happens to the timesheet or Settings afterwards. A wrong invoice is voided (its number is never reused) and a
new one made.
"""
from __future__ import annotations

import html
import json
from datetime import date, timedelta
from urllib.parse import quote

from . import pdf
from .pdf import ACCENT, FAINT, GREY_BG, INK, MARGIN, MUTED, PAGE_H, PAGE_W, Document, fit, text_width

BUSINESS_TEXT = ("name", "address", "email", "phone", "company_number", "vat_number", "payment", "footer", "prefix")
DEFAULT_BUSINESS = {"name": "", "address": "", "email": "", "phone": "", "company_number": "", "vat_registered": False,
                    "vat_number": "", "vat_rate": 20.0, "payment": "", "footer": "", "prefix": "INV-", "digits": 4,
                    "next_number": 1}
CLIENT_TEXT = ("name", "address", "email", "reference")


def businesses(conf: dict) -> list[dict]:
    """The user's businesses (at least one, even if blank). Each has an id, its own details and its own numbering."""
    saved = conf.get("businesses")
    if not saved and conf.get("business"):   # version 30 had a single business
        saved = [{**conf["business"], "id": "b1"}]
    return [{**DEFAULT_BUSINESS, **b} for b in saved] if saved else [{**DEFAULT_BUSINESS, "id": "b1"}]


def business_for(conf: dict, client: str | None) -> dict:
    """The business that invoices this client: the one chosen for it in Settings, else the first."""
    every = businesses(conf)
    chosen = client_details(conf, client)["business"] if client else None
    return next((b for b in every if b["id"] == chosen), every[0])


def clean_businesses(raw: list) -> list[dict]:
    """Check and tidy the whole list from Settings. New businesses (no id yet) get the next free id."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("Keep at least one business.")
    out, used = [], {str(b.get("id")) for b in raw if b.get("id")}
    n = 1
    for b in raw:
        clean = clean_business(b)
        bid = str(b.get("id") or "")
        if not bid or bid in {x["id"] for x in out}:
            while f"b{n}" in used:
                n += 1
            bid = f"b{n}"
            used.add(bid)
        out.append({**clean, "id": bid})
    names = [b["name"].lower() for b in out if b["name"]]
    if len(names) != len(set(names)):
        raise ValueError("Two businesses have the same name. Give each its own name.")
    if len(out) > 1 and any(not b["name"] for b in out):
        raise ValueError("Give every business a name.")
    return out


def clean_business(raw: dict) -> dict:
    out = dict(DEFAULT_BUSINESS)
    for key in BUSINESS_TEXT:
        if isinstance(raw.get(key), str):
            out[key] = raw[key].strip()[:2000]
    out["vat_registered"] = bool(raw.get("vat_registered"))
    try:
        out["vat_rate"] = float(raw.get("vat_rate") if raw.get("vat_rate") not in (None, "") else 20)
        out["digits"] = int(raw.get("digits") or 4)
        out["next_number"] = int(raw.get("next_number") or 1)
    except (TypeError, ValueError):
        raise ValueError("VAT rate, number length and next invoice number must be numbers.")
    if not 0 <= out["vat_rate"] <= 100:
        raise ValueError("VAT rate must be between 0 and 100.")
    if not 1 <= out["digits"] <= 10:
        raise ValueError("Invoice number length must be between 1 and 10 digits.")
    if out["next_number"] < 1:
        raise ValueError("The next invoice number must be 1 or more.")
    if out["vat_registered"] and not out["vat_number"]:
        raise ValueError("Enter your VAT number, or untick VAT registered.")
    return out


def client_details(conf: dict, client: str) -> dict:
    for name, d in (conf.get("client_details") or {}).items():
        if name.lower() == client.lower():
            return {"name": "", "address": "", "email": "", "reference": "", "terms_days": None, "business": None, **d}
    return {"name": "", "address": "", "email": "", "reference": "", "terms_days": None, "business": None}


def clean_client_details(raw: dict) -> dict:
    out = {key: str(raw.get(key) or "").strip()[:2000] for key in CLIENT_TEXT}
    out["business"] = str(raw["business"]) if raw.get("business") else None
    days = raw.get("terms_days")
    if days in (None, ""):
        out["terms_days"] = None
    else:
        out["terms_days"] = int(days)
        if not 0 <= out["terms_days"] <= 365:
            raise ValueError("Payment terms must be between 0 and 365 days.")
    return out


def missing(conf: dict, client: str | None = None) -> list[str]:
    """What still has to be filled in before an invoice can go out (plain words, for the page)."""
    b = business_for(conf, client)
    out = []
    if not b["name"]:
        out.append("your business name")
    if not b["address"]:
        out.append("your business address")
    if not b["payment"]:
        out.append("how clients pay you")
    if client and not client_details(conf, client)["name"]:
        out.append(f"{client}'s billing name")
    return out


EXTRA_KINDS = {"time": "h", "mileage": "miles", "expense": ""}


def clean_extra(raw: dict) -> dict:
    kind = raw.get("kind") if raw.get("kind") in EXTRA_KINDS else None
    if not kind:
        raise ValueError("Choose what kind of extra it is.")
    description = str(raw.get("description") or "").strip()[:300]
    if not description:
        raise ValueError("Describe the extra, as it should read on the invoice.")
    try:
        quantity = 1.0 if kind == "expense" else float(raw.get("quantity"))
        unit_price = float(raw.get("unit_price"))
    except (TypeError, ValueError):
        raise ValueError("Enter the amount" + ("." if kind == "expense" else " and how many."))
    if quantity <= 0 or unit_price < 0:
        raise ValueError("Amounts must be more than zero.")
    return {"kind": kind, "description": description, "quantity": quantity, "unit_price": unit_price}


def extra_lines(extras: list[dict]) -> list[dict]:
    """Invoice lines for a month's extras, in the same shape as the time lines."""
    return [{"description": e["description"], "hours": e["quantity"], "rate": e["unit_price"],
             "amount": round(e["quantity"] * e["unit_price"], 2), "unit": EXTRA_KINDS.get(e["kind"], ""),
             "extra": e["kind"]} for e in extras]


def quantity_text(line: dict) -> str:
    """How much of a line: hours to two places, miles and counts without needless zeros."""
    unit = line.get("unit", "h")
    if line.get("extra") == "expense":
        return ""
    if unit == "h":
        return f"{line['hours']:.2f} h"
    return f"{line['hours']:g} {unit}".strip()


def format_number(b: dict, n: int) -> str:
    return f"{b['prefix']}{n:0{b['digits']}d}"


def month_title(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{date(y, m, 1):%B %Y}"


def create(store, conf: dict, client: str, month: str, lines: list[dict], seconds: int, today: date,
           default_terms: int) -> tuple[dict, dict]:
    """Make the next invoice for a client's month. Returns (the invoice, the settings with the number moved on)."""
    need = missing(conf, client)
    if need:
        raise ValueError("Before invoicing, add " + ", ".join(need) + " in Settings.")
    if not lines:
        raise ValueError(f"There's nothing to invoice {client} for in {month_title(month)}.")
    if any(l["amount"] is None for l in lines):
        raise ValueError(f"Add an hourly rate for {client} in Settings first.")
    existing = store.bill_for(client, month)
    if existing:
        raise ValueError(f"{client} already has invoice {existing['number']} for {month_title(month)}. "
                         "Void it first to make a new one.")
    b = business_for(conf, client)
    details = client_details(conf, client)
    n = b["next_number"]
    while store.number_used(b["id"], format_number(b, n)):   # never reuse a number, even a voided one
        n += 1
    net = round(sum(l["amount"] for l in lines), 2)
    vat_rate = b["vat_rate"] if b["vat_registered"] else None
    vat = round(net * vat_rate / 100, 2) if vat_rate else 0.0
    terms = details["terms_days"] if details["terms_days"] is not None else default_terms
    bill = {
        "business_id": b["id"], "number": format_number(b, n), "seq": n, "client": client, "month": month,
        "issued_on": today.isoformat(), "due_on": (today + timedelta(days=terms)).isoformat(),
        "seconds": seconds, "net": net, "vat": vat, "total": round(net + vat, 2),
        "data": {"lines": lines, "vat_rate": vat_rate, "terms_days": terms,
                 "business": {k: b[k] for k in ("name", "address", "email", "phone", "company_number", "vat_number",
                                                "payment", "footer")},
                 "client": {k: details[k] for k in CLIENT_TEXT}},
    }
    bill["id"] = store.add_bill(bill)
    store.claim_extras(client, month, bill["id"])
    store.set_invoiced(client, month, seconds, net)
    conf = dict(conf)
    conf["businesses"] = [{**x, "next_number": n + 1} if x["id"] == b["id"] else x for x in businesses(conf)]
    conf.pop("business", None)
    return bill, conf


def summary(bill: dict, paid_at: str | None, today: date) -> dict:
    """An invoice for the list on the page, with where it stands."""
    due = date.fromisoformat(bill["due_on"])
    status = ("void" if bill["voided_at"] else "paid" if paid_at else
              "overdue" if today > due else "awaiting")
    return {**{k: bill[k] for k in ("id", "number", "client", "month", "issued_on", "due_on", "net", "vat", "total")},
            "business": bill["data"]["business"]["name"], "status": status, "days_overdue": (today - due).days if status == "overdue" else 0,
            "email": bill["data"]["client"].get("email") or ""}


def file_name(bill: dict) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_ " else "-" for ch in f"{bill['number']} {bill['client']}")
    return f"{safe} {month_title(bill['month'])}.pdf"


def _d(iso: str) -> str:
    return f"{date.fromisoformat(iso):%d/%m/%Y}"


def _gbp(v: float) -> str:
    return f"£{v:,.2f}"


def mailto(bill: dict) -> str:
    b, c = bill["data"]["business"], bill["data"]["client"]
    subject = f"Invoice {bill['number']} from {b['name']}"
    body = (f"Hi,\n\nPlease find attached invoice {bill['number']} for {month_title(bill['month'])}.\n\n"
            f"Amount due: {_gbp(bill['total'])}\nDue by: {_d(bill['due_on'])}\n\n"
            + (f"How to pay:\n{b['payment']}\n\n" if b["payment"] else "") + f"Thank you,\n{b['name']}")
    return f"mailto:{quote(c.get('email') or '')}?subject={quote(subject)}&body={quote(body)}"


# ---------- PDF ----------

def _wrap(s: str, width: float, size: float, bold: bool = False) -> list[str]:
    out = []
    for para in s.splitlines() or [""]:
        line = ""
        for word in para.split(" "):
            trial = f"{line} {word}".strip()
            if line and text_width(trial, size, bold) > width:
                out.append(line)
                line = word
            else:
                line = trial
        out.append(line)
    return out


def bill_pdf(bill: dict) -> bytes:
    data = bill["data"]
    b, c = data["business"], data["client"]
    doc = Document(f"Invoice {bill['number']} – {b['name']}")
    left, right = MARGIN, PAGE_W - MARGIN
    y = PAGE_H - MARGIN
    doc.rect(left, y - 3, 28, 2.5, ACCENT)

    # who it's from (left) and the invoice's own details (right)
    doc.text(right, y - 28, "INVOICE", 22, bold=True, align="right")
    ry = y - 48
    for label, value in (("Invoice number", bill["number"]), ("Invoice date", _d(bill["issued_on"])),
                         ("Payment due", _d(bill["due_on"])), ("For", month_title(bill["month"]))):
        doc.text(right - 90, ry, label, 8.5, color=MUTED, align="right")
        doc.text(right, ry, value, 9, bold=True, align="right")
        ry -= 14
    ly = y - 26
    for line in _wrap(b["name"], 280, 15, True):
        doc.text(left, ly, line, 15, bold=True)
        ly -= 18
    ly += 4
    extra = [x for x in (b["email"], b["phone"]) if x]
    if b["company_number"]:
        extra.append(f"Company number {b['company_number']}")
    if data["vat_rate"] is not None and b["vat_number"]:
        extra.append(f"VAT number {b['vat_number']}")
    for line in _wrap(b["address"], 280, 9) + extra:
        doc.text(left, ly, fit(line, 280, 9), 9, color=MUTED)
        ly -= 12
    y = min(ly, ry) - 16

    doc.text(left, y, "BILL TO", 7.5, bold=True, color=MUTED)
    y -= 15
    doc.text(left, y, fit(c["name"], 300, 11, True), 11, bold=True)
    y -= 14
    for line in _wrap(c["address"], 300, 9) if c["address"] else []:
        doc.text(left, y, fit(line, 300, 9), 9, color=MUTED)
        y -= 12
    if c.get("reference"):
        doc.text(left, y, f"Reference: {fit(c['reference'], 250, 9)}", 9, color=MUTED)
        y -= 12
    y -= 14

    cols = (("Description", left + 6, "left"), ("Quantity", right - 150, "right"), ("Rate", right - 80, "right"),
            ("Amount", right - 6, "right"))

    def head() -> None:
        nonlocal y
        doc.rect(left, y - 18, right - left, 18, GREY_BG)
        for label, x, align in cols:
            doc.text(x, y - 12, label, 8, bold=True, color=MUTED, align=align)
        y -= 18

    def room(height: float) -> None:
        nonlocal y
        if y - height < MARGIN + 24:
            doc.new_page()
            y = PAGE_H - MARGIN
            head()

    head()
    for l in data["lines"]:
        room(18)
        doc.text(cols[0][1], y - 12, fit(l["description"], right - 215 - cols[0][1], 9), 9)
        doc.text(cols[1][1], y - 12, quantity_text(l), 9, align="right")
        if l.get("extra") != "expense":
            doc.text(cols[2][1], y - 12, _gbp(l["rate"]), 9, align="right")
        doc.text(cols[3][1], y - 12, _gbp(l["amount"]), 9, align="right")
        y -= 18
        doc.line(left, y, right, y)

    pay = _wrap(b["payment"], right - left - 20, 9) if b["payment"] else []
    room(90)
    y -= 10
    totals = [("Subtotal", _gbp(bill["net"]), False)]
    if data["vat_rate"] is not None:
        totals.append((f"VAT at {data['vat_rate']:g}%", _gbp(bill["vat"]), False))
    for label, value, _ in totals:
        doc.text(right - 110, y - 10, label, 9, color=MUTED, align="right")
        doc.text(right - 6, y - 10, value, 9, align="right")
        y -= 16
    doc.line(right - 220, y, right, y, 1.2, INK)
    doc.text(right - 110, y - 16, "Total due", 11, bold=True, align="right")
    doc.text(right - 6, y - 16, _gbp(bill["total"]), 13, bold=True, align="right")
    y -= 40

    if pay:
        box = 12 * len(pay) + 34
        room(box + 10)
        doc.rect(left, y - box, right - left, box, (1.0, 0.97, 0.93))
        doc.text(left + 10, y - 16, f"HOW TO PAY · due by {_d(bill['due_on'])}", 7.5, bold=True, color=MUTED)
        py = y - 32
        for line in pay:
            doc.text(left + 10, py, line, 9)
            py -= 12
        y -= box + 14
    for line in _wrap(b["footer"], right - left, 8.5) if b["footer"] else []:
        room(12)
        doc.text(left, y - 9, line, 8.5, color=FAINT)
        y -= 12
    return doc.to_bytes()


# ---------- page ----------

STYLE = """
body{font:14px/1.6 Inter,-apple-system,BlinkMacSystemFont,sans-serif;color:#111827;background:#F3F4F6;margin:0;padding:24px}
h1,h2,button,.big{font-family:Sora,Inter,sans-serif;letter-spacing:-0.02em}
.bar{max-width:820px;margin:0 auto 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
button,a.btn{font:600 14px Sora,Inter,sans-serif;padding:8px 16px;border-radius:8px;border:1px solid #D1D5DB;background:#fff;
 color:#111827;cursor:pointer;text-decoration:none;white-space:nowrap}
button:hover,a.btn:hover{border-color:#F97316}
.primary{background:#F97316!important;border-color:#F97316!important;color:#fff!important}
button.danger{color:#c62f2f;margin-left:auto}
.state{font-weight:600;padding:4px 12px;border-radius:999px;font-size:13px}
.state.paid{background:#E3F7EA;color:#00a344}.state.overdue{background:#FDE4E4;color:#c62f2f}
.state.awaiting{background:#FFEDD5;color:#c2410c}.state.void{background:#E5E7EB;color:#6B7280}
.sheet{max-width:820px;margin:0 auto;background:#fff;border-radius:12px;padding:44px 48px;box-shadow:0 2px 14px rgba(0,0,0,.08)}
.sheet.void{opacity:.55}
.top{display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap}
.top::before{content:"";display:block;width:40px;height:3px;border-radius:2px;background:#F97316;position:absolute;margin-top:-16px}
.from b{font:700 20px Sora,Inter,sans-serif;display:block;margin-bottom:4px}.muted,.from{color:#6B7280;white-space:pre-line}
.from b{color:#111827}.meta{text-align:right}.meta .big{font-size:28px;font-weight:700;margin:0 0 6px}
.meta table{margin-left:auto}.meta td{padding:1px 0 1px 16px;text-align:right}.meta td:first-child{color:#6B7280}
.meta td:last-child{font-weight:600}
h2{font-size:11px;letter-spacing:.08em;color:#6B7280;margin:28px 0 4px}
.to b{font-size:16px}
table.lines{border-collapse:collapse;width:100%;margin-top:24px}
.lines th{background:#F3F4F6;color:#6B7280;font-size:12px;text-align:left;padding:7px 8px}
.lines td{padding:7px 8px;border-bottom:1px solid #E5E7EB}.n{text-align:right!important;font-variant-numeric:tabular-nums}
.totals{margin:14px 0 0 auto;border-collapse:collapse}.totals td{padding:3px 8px;text-align:right}
.totals td:first-child{color:#6B7280}.totals tr.due td{font:700 17px Sora,Inter,sans-serif;color:#111827;border-top:2px solid #111827;padding-top:8px}
.pay{background:#FFF7ED;border-radius:10px;padding:14px 18px;margin-top:28px;white-space:pre-line}
.pay h2{margin:0 0 6px}.foot{color:#9CA3AF;font-size:12px;margin-top:20px;white-space:pre-line}
.hint{max-width:820px;margin:14px auto 0;color:#6B7280;font-size:13px}
@media (max-width:700px){body{padding:12px}.sheet{padding:22px 18px}.meta{text-align:left}.meta table{margin-left:0}
 .meta td{text-align:left;padding-left:0;padding-right:16px}.lines{display:block;overflow-x:auto}button.danger{margin-left:0}}
@media print{.bar,.hint{display:none}body{background:#fff;padding:0}.sheet{box-shadow:none;padding:0;max-width:none}}
"""

SCRIPT = """<script>
async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-TFTrack": "1"},
                               body: JSON.stringify(body)});
  if (!r.ok) throw new Error((await r.json()).error || "Couldn't save that change.");
}
const info = JSON.parse(document.body.dataset.bill);
for (const b of document.querySelectorAll("button[data-paid]")) {
  b.onclick = async () => {
    try { await post("/api/paid", {client: info.client, month: info.month, paid: b.dataset.paid === "1"}); location.reload(); }
    catch (e) { alert(e.message); }
  };
}
const v = document.getElementById("void");
if (v) v.onclick = async () => {
  if (!confirm(`Void invoice ${info.number}? Its number won't be used again, and the month goes back to "not invoiced".`)) return;
  try { await post("/api/bill/void", {id: info.id}); location.reload(); } catch (e) { alert(e.message); }
};
</script>"""


def bill_page(bill: dict, paid_at: str | None, today: date) -> str:
    esc = html.escape
    data = bill["data"]
    b, c = data["business"], data["client"]
    s = summary(bill, paid_at, today)
    state = {"paid": f"✓ Paid {_d(paid_at[:10])}" if paid_at else "", "void": "Void",
             "overdue": f"{s['days_overdue']} day{'s' * (s['days_overdue'] != 1)} overdue",
             "awaiting": f"Awaiting payment, due {_d(bill['due_on'])}"}[s["status"]]
    q = f"id={bill['id']}"
    sheet_q = f"client={quote(bill['client'])}&month={bill['month']}"
    buttons = ""
    if s["status"] != "void":
        buttons = (f'<a class="btn primary" href="/bill.pdf?{q}">Save PDF</a>'
                   f'<a class="btn" href="{esc(mailto(bill))}">Email it</a>'
                   f'<a class="btn" href="/sheet.pdf?{sheet_q}">Save timesheet PDF</a>'
                   '<button onclick="window.print()">Print</button>'
                   + ('<button data-paid="0">Not paid</button>' if paid_at else '<button data-paid="1">Mark as paid</button>')
                   + '<button class="danger" id="void">Void</button>')
    extra = [esc(x) for x in (b["email"], b["phone"]) if x]
    if b["company_number"]:
        extra.append(f"Company number {esc(b['company_number'])}")
    if data["vat_rate"] is not None and b["vat_number"]:
        extra.append(f"VAT number {esc(b['vat_number'])}")
    rows = "".join(f"<tr><td>{esc(l['description'])}</td><td class=n>{quantity_text(l)}</td>"
                   f"<td class=n>{'' if l.get('extra') == 'expense' else _gbp(l['rate'])}</td>"
                   f"<td class=n>{_gbp(l['amount'])}</td></tr>" for l in data["lines"])
    vat = (f"<tr><td>VAT at {data['vat_rate']:g}%</td><td class=n>{_gbp(bill['vat'])}</td></tr>"
           if data["vat_rate"] is not None else "")
    pay = (f'<div class="pay"><h2>HOW TO PAY · DUE BY {_d(bill["due_on"])}</h2>{esc(b["payment"])}</div>'
           if b["payment"] else "")
    info = esc(json.dumps({"id": bill["id"], "number": bill["number"], "client": bill["client"], "month": bill["month"]}))
    return f"""<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Invoice {esc(bill['number'])} – {esc(bill['client'])}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700&family=Inter:wght@400;500;600&display=swap">
<style>{STYLE}</style></head><body data-bill="{info}">
<div class="bar"><span class="state {s['status']}">{state}</span>{buttons}</div>
<div class="sheet {s['status']}">
<div class="top"><div class="from"><b>{esc(b['name'])}</b>{esc(b['address'])}{''.join('<br>' + x for x in extra)}</div>
<div class="meta"><p class="big">Invoice</p><table>
<tr><td>Invoice number</td><td>{esc(bill['number'])}</td></tr>
<tr><td>Invoice date</td><td>{_d(bill['issued_on'])}</td></tr>
<tr><td>Payment due</td><td>{_d(bill['due_on'])}</td></tr>
<tr><td>For</td><td>{month_title(bill['month'])}</td></tr></table></div></div>
<h2>BILL TO</h2><div class="to"><b>{esc(c['name'])}</b><div class="muted">{esc(c['address'])}</div>
{f'<div class="muted">Reference: {esc(c["reference"])}</div>' if c.get('reference') else ''}</div>
<table class="lines"><thead><tr><th>Description</th><th class=n>Quantity</th><th class=n>Rate</th><th class=n>Amount</th></tr></thead>
<tbody>{rows}</tbody></table>
<table class="totals"><tr><td>Subtotal</td><td class=n>{_gbp(bill['net'])}</td></tr>{vat}
<tr class="due"><td>Total due</td><td class=n>{_gbp(bill['total'])}</td></tr></table>
{pay}<div class="foot">{esc(b['footer'])}</div></div>
<p class="hint">This invoice is a fixed copy from {_d(bill['issued_on'])}. If something on it is wrong, void it and make a new one
from the <a href="/invoice?{sheet_q}">timesheet</a>. <b>Email it</b> opens a ready-written email: attach the saved PDF.</p>
{SCRIPT}</body></html>"""
