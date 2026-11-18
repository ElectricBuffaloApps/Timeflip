"""Invoices made in the app: your business details, numbering, VAT, and the invoice itself as a page and a PDF.

An invoice is a snapshot. Once created, its lines, totals and the business and client details on it never change,
whatever happens to the timesheet or Settings afterwards. A wrong invoice is voided (its number is never reused) and a
new one made.
"""
from __future__ import annotations

import html
import json
import re
from datetime import date, timedelta
from urllib.parse import quote

from . import logo as logos
from . import pdf
from .reports import pence
from .pdf import ACCENT, FAINT, GREY_BG, INK, MARGIN, MUTED, PAGE_H, PAGE_W, Document, fit, text_width

BUSINESS_TEXT = ("name", "address", "email", "phone", "company_number", "vat_number", "payment", "footer", "prefix",
                 "website")
DEFAULT_BUSINESS = {"name": "", "address": "", "email": "", "phone": "", "company_number": "", "vat_registered": False,
                    "vat_number": "", "vat_rate": 20.0, "payment": "", "footer": "", "prefix": "INV-", "digits": 4,
                    "next_number": 1,
                    # how invoices are emailed: "mail" (a draft in Apple Mail) or "smtp" (your email provider, sent
                    # straight away; the password lives in the Keychain, never in this file)
                    "mail_method": "mail", "smtp_host": "", "smtp_port": 587, "smtp_security": "starttls",
                    "smtp_user": "", "smtp_password_saved": False,
                    # how its invoices and timesheets look: a logo (kept by logo.py, named here), a colour, a website
                    "logo": "", "colour": "", "website": "", "layout": "",
                    # the address and bank details as fields; `address` and `payment` are put together from them
                    "addr1": "", "addr2": "", "town": "", "county": "", "postcode": "",
                    "bank_account_name": "", "bank_name": "", "bank_sort_code": "", "bank_account_number": "",
                    "bank_note": ""}
CLIENT_TEXT = ("name", "address", "email", "reference")


# The address and bank details are kept as separate fields. What's printed on an invoice (`address`, `payment`) is
# put together from them, so everything that prints or checks those two carries on working.
ADDRESS_FIELDS = ("addr1", "addr2", "town", "county", "postcode")
BANK_FIELDS = ("bank_account_name", "bank_name", "bank_sort_code", "bank_account_number", "bank_note")
_POSTCODE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}$", re.I)
_BANK_LABELS = (("bank_account_name", r"(?:account\s+name|name(?:\s+on\s+(?:the\s+)?account)?|payee)"),
                ("bank_sort_code", r"sort\s*code"), ("bank_account_number", r"(?:account|acc(?:ount)?\.?)\s*(?:number|no\.?|num)?"),
                ("bank_name", r"bank"))


def sort_code(value: str) -> str:
    """Six digits become 12-34-56. Anything else is left as typed (other countries write theirs differently)."""
    digits = re.sub(r"\D", "", value or "")
    return f"{digits[:2]}-{digits[2:4]}-{digits[4:]}" if len(digits) == 6 else (value or "").strip()


def compose_address(b: dict) -> str:
    return "\n".join(str(b.get(k) or "").strip() for k in ADDRESS_FIELDS if str(b.get(k) or "").strip())


def compose_payment(b: dict) -> str:
    """The bank details as they're printed in the invoice's How to pay box."""
    lines = [f"{label}: {str(b.get(key) or '').strip()}" for key, label in
             (("bank_account_name", "Account name"), ("bank_name", "Bank"), ("bank_sort_code", "Sort code"),
              ("bank_account_number", "Account number")) if str(b.get(key) or "").strip()]
    note = str(b.get("bank_note") or "").strip()
    return "\n".join(lines + ([note] if note else []))


def split_address(text: str) -> dict:
    """An address typed as one block, into its fields: first line, anything between, town, postcode."""
    lines = [l.strip().rstrip(",") for l in (text or "").replace(",", "\n").splitlines() if l.strip().rstrip(",")]
    out = dict.fromkeys(ADDRESS_FIELDS, "")
    if lines and _POSTCODE.match(lines[-1]):
        out["postcode"] = lines.pop().upper()
    if lines:
        out["addr1"] = lines.pop(0)
    if len(lines) >= 3:   # enough lines for a county after the town; with fewer, the last line is taken as the town
        out["county"] = lines.pop()
    if lines:
        out["town"] = lines.pop()
    out["addr2"] = ", ".join(lines)
    return out   # whichever fields the lines land in, they print in the order they were typed


def split_payment(text: str) -> dict:
    """Bank details typed as one block ("Sort code: 12-34-56 …"), into their fields. Lines it can't place are kept
    as the extra note, so nothing typed is lost."""
    out, note = dict.fromkeys(BANK_FIELDS, ""), []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for key, label in _BANK_LABELS:
            m = re.match(rf"^{label}\s*[:\-–]\s*(.+)$", line, re.I)
            if m and not out[key]:
                out[key] = sort_code(m.group(1)) if key == "bank_sort_code" else m.group(1).strip()
                break
        else:
            note.append(line)
    out["bank_note"] = "\n".join(note)
    return out


def with_fields(b: dict) -> dict:
    """A business with its address and bank details as fields. One saved before they were fields has them worked out
    from the text it was typed as."""
    b = dict(b)
    if not any(b.get(k) for k in ADDRESS_FIELDS) and b.get("address"):
        b.update(split_address(b["address"]))
    if not any(b.get(k) for k in BANK_FIELDS) and b.get("payment"):
        b.update(split_payment(b["payment"]))
    return b


def businesses(conf: dict) -> list[dict]:
    """The user's businesses (at least one, even if blank). Each has an id, its own details and its own numbering."""
    saved = conf.get("businesses")
    if not saved and conf.get("business"):   # version 30 had a single business
        saved = [{**conf["business"], "id": "b1"}]
    return [with_fields({**DEFAULT_BUSINESS, **b}) for b in saved] if saved else [{**DEFAULT_BUSINESS, "id": "b1"}]


def brand(b: dict | None) -> pdf.Brand:
    """How a business's PDFs look, from its settings (or from the copy of them kept on an invoice)."""
    b = b or {}
    return pdf.Brand(logos.for_pdf(b.get("logo")), logos.colour(b.get("colour"), None) if b.get("colour") else None,
                     b.get("name") or "", b.get("website") or "", _layout(b))


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
    # Address and bank details arrive as fields from Settings; what's printed is put together from them. Sent the
    # old way, as one block of text each, they're split into fields instead.
    if any(k in raw for k in ADDRESS_FIELDS):
        for k in ADDRESS_FIELDS:
            out[k] = str(raw.get(k) or "").strip()[:200]
        out["postcode"] = out["postcode"].upper()
        out["address"] = compose_address(out)
    else:
        out.update(split_address(out["address"]))
    if any(k in raw for k in BANK_FIELDS):
        for k in BANK_FIELDS:
            out[k] = str(raw.get(k) or "").strip()[:300]
        out["bank_sort_code"] = sort_code(out["bank_sort_code"])
        out["bank_account_number"] = re.sub(r"\s+", " ", out["bank_account_number"])
        out["payment"] = compose_payment(out)
    else:
        out.update(split_payment(out["payment"]))
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
    out["colour"] = logos.clean_colour(raw.get("colour"))
    out["layout"] = raw.get("layout") if raw.get("layout") in LAYOUTS else ""
    out["mail_method"] = "smtp" if raw.get("mail_method") == "smtp" else "mail"
    out["smtp_host"] = str(raw.get("smtp_host") or "").strip()[:200]
    out["smtp_user"] = str(raw.get("smtp_user") or "").strip()[:200]
    out["smtp_security"] = raw.get("smtp_security") if raw.get("smtp_security") in ("ssl", "starttls", "none") else "starttls"
    try:
        out["smtp_port"] = int(raw.get("smtp_port") or 587)
    except (TypeError, ValueError):
        raise ValueError("The mail server port must be a number (usually 587 or 465).")
    if not 1 <= out["smtp_port"] <= 65535:
        raise ValueError("The mail server port must be between 1 and 65535.")
    if out["mail_method"] == "smtp":
        if not out["smtp_host"]:
            raise ValueError(f"Enter the outgoing mail server for {out['name'] or 'this business'}, or choose Apple Mail.")
        if not out["email"]:
            raise ValueError(f"Enter the email address {out['name'] or 'this business'} sends from.")
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
             "amount": pence(e["quantity"] * e["unit_price"]), "unit": EXTRA_KINDS.get(e["kind"], ""),
             "extra": e["kind"]} for e in extras]


def quantity_text(line: dict) -> str:
    """How much of a line: hours to two places, miles and counts without needless zeros."""
    unit = line.get("unit", "h")
    if line.get("extra") == "expense":
        return ""
    if unit == "h":
        return f"{line['hours']:.2f} h"
    return f"{line['hours']:g} {unit}".strip()


def quantity_words(line: dict) -> str:
    """The same, as it reads on an invoice: "3 hours", "0.75 hours", "40 miles"."""
    if line.get("unit", "h") == "h" and line.get("extra") != "expense":
        return f"{line['hours']:g} hour{'s' * (line['hours'] != 1)}"
    return quantity_text(line)


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
                                                "payment", "footer", "logo", "colour", "website", "layout")},
                 "client": {k: details[k] for k in CLIENT_TEXT}},
    }
    bill["id"] = store.add_bill(bill)
    store.claim_extras(client, month, bill["id"])
    store.set_invoiced(client, month, seconds, net)
    conf = dict(conf)
    conf["businesses"] = [{**x, "next_number": n + 1} if x["id"] == b["id"] else x for x in businesses(conf)]
    conf.pop("business", None)
    return bill, conf


def sample_bill(b: dict, today: date) -> dict:
    """A made-up invoice from a business, to see how its logo, colour and details look before sending a real one."""
    rate = 40.0
    lines = [{"description": "Example client Hourly Rate", "detail": f"W/C {d} Oct 2026", "hours": h, "rate": rate,
              "amount": round(h * rate, 2)} for d, h in (("5th", 6.0), ("12th", 5.5), ("19th", 6.0), ("26th", 4.25))]
    lines += extra_lines([{"description": "Additional agreed hours, Thu 22 Oct 2026", "quantity": 2, "unit_price": rate,
                           "kind": "time"},
                          {"description": "Mileage, Thu 08 Oct 2026", "quantity": 40, "unit_price": 0.45, "kind": "mileage"}])
    net = round(sum(l["amount"] for l in lines), 2)
    vat_rate = b.get("vat_rate") if b.get("vat_registered") else None
    vat = round(net * vat_rate / 100, 2) if vat_rate else 0.0
    return {"number": format_number(b, int(b.get("next_number") or 1)), "client": "Example client", "month": "2026-10",
            "issued_on": today.isoformat(), "due_on": (today + timedelta(days=30)).isoformat(),
            "net": net, "vat": vat, "total": round(net + vat, 2),
            "data": {"lines": lines, "vat_rate": vat_rate, "terms_days": 30,
                     "business": {k: b.get(k, "") for k in ("name", "address", "email", "phone", "company_number",
                                                             "vat_number", "payment", "footer", "logo", "colour", "website", "layout")},
                     "client": {"name": "Example Client Ltd", "address": "1 Example Street\nBrighton\nBN1 1AA", "email": "",
                                "reference": "PO 1234"}}}


def summary(bill: dict, paid_at: str | None, today: date) -> dict:
    """An invoice for the list on the page, with where it stands."""
    due = date.fromisoformat(bill["due_on"])
    status = ("void" if bill["voided_at"] else "paid" if paid_at else
              "overdue" if today > due else "awaiting")
    return {**{k: bill[k] for k in ("id", "number", "client", "month", "issued_on", "due_on", "net", "vat", "total")},
            "business": bill["data"]["business"]["name"], "status": status, "days_overdue": (today - due).days if status == "overdue" else 0,
            "email": bill["data"]["client"].get("email") or "", "business_id": bill["business_id"]}


def file_name(bill: dict) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_ " else "-" for ch in f"{bill['number']} {bill['client']}")
    return f"{safe} {month_title(bill['month'])}.pdf"


def _d(iso: str) -> str:
    return f"{date.fromisoformat(iso):%d/%m/%Y}"


def _gbp(v: float) -> str:
    return f"£{v:,.2f}"


def email_text(bill: dict) -> tuple[str, str]:
    """(subject, body) of the email that goes with an invoice."""
    b = bill["data"]["business"]
    subject = f"Invoice {bill['number']} from {b['name']}"
    body = (f"Hi,\n\nPlease find attached invoice {bill['number']} for {month_title(bill['month'])}, with the "
            f"timesheet.\n\nAmount due: {_gbp(bill['total'])}\nDue by: {_d(bill['due_on'])}\n\n"
            + (f"How to pay:\n{b['payment']}\n\n" if b["payment"] else "") + f"Thank you,\n{b['name']}")
    return subject, body


def mailto(bill: dict) -> str:
    subject, body = email_text(bill)
    return f"mailto:{quote(bill['data']['client'].get('email') or '')}?subject={quote(subject)}&body={quote(body)}"


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


LAYOUTS = ("", "band", "classic")     # modern (the first look), bold band, classic rules
PAID_GREEN = (0.30, 0.69, 0.38)
WHITE = (1, 1, 1)


def _layout(b: dict) -> str:
    return b.get("layout") if b.get("layout") in LAYOUTS else ""


def _contact(b: dict, data: dict) -> list[str]:
    out = [x for x in (b["phone"], b["email"], b.get("website")) if x]
    if b["company_number"]:
        out.append(f"Company number {b['company_number']}")
    if data["vat_rate"] is not None and b["vat_number"]:
        out.append(f"VAT number {b['vat_number']}")
    return out


def _bill_to(c: dict, width: float) -> list[str]:
    out = [c["name"]] + (_wrap(c["address"], width, 9) if c["address"] else [])
    if c.get("reference"):
        out.append(f"Reference: {c['reference']}")
    return out


def _meta(bill: dict) -> tuple:
    return (("Invoice number", bill["number"]), ("Date", _d(bill["issued_on"])), ("Due date", _d(bill["due_on"])),
            ("For", month_title(bill["month"])))


def _top_modern(doc, bill, look, left, right) -> float:
    data = bill["data"]
    b, c = data["business"], data["client"]
    y = PAGE_H - MARGIN
    doc.rect(left, y - 3, right - left if look.logo else 28, 2.5, look.accent)
    doc.text(right, y - 30, "INVOICE", 22, bold=True, color=look.accent if look.logo or b.get("colour") else INK, align="right")
    ry = y - 50
    for label, value in (("Invoice number", bill["number"]), ("Invoice date", _d(bill["issued_on"])),
                         ("Payment due", _d(bill["due_on"])), ("For", month_title(bill["month"]))):
        doc.text(right - 90, ry, label, 8.5, color=MUTED, align="right")
        doc.text(right, ry, value, 9, bold=True, align="right")
        ry -= 14
    ly = y - 26
    if look.logo:   # the logo leads; the name sits under it, smaller
        _, lh = doc.image(look.logo, left, y - 14, 190, 62)
        ly = y - 14 - lh - 16
        for line in _wrap(b["name"], 280, 11, True):
            doc.text(left, ly, line, 11, bold=True)
            ly -= 14
    else:
        for line in _wrap(b["name"], 280, 15, True):
            doc.text(left, ly, line, 15, bold=True)
            ly -= 18
        ly += 4
    extra = _contact(b, data)
    for line in _wrap(b["address"], 280, 9) + extra:
        doc.text(left, ly, fit(line, 280, 9), 9, color=MUTED)
        ly -= 12
    y = min(ly, ry) - 16
    doc.text(left, y, "BILL TO", 7.5, bold=True, color=look.accent)
    y -= 15
    for i, line in enumerate(_bill_to(c, 300)):
        doc.text(left, y, fit(line, 300, 11 if not i else 9, not i), 11 if not i else 9, bold=not i, color=INK if not i else MUTED)
        y -= 14 if not i else 12
    return y - 14


def _top_band(doc, bill, look, left, right) -> float:
    """Logo, a big INVOICE in the business's colour and its name on the left, its details on the right, then a grey
    band across the page with who it's to and the invoice's number and dates."""
    data = bill["data"]
    b, c = data["business"], data["client"]
    y = PAGE_H - MARGIN + 6
    ly = y
    if look.logo:
        _, lh = doc.image(look.logo, left, y, 150, 46)
        ly -= lh + 6
    ly -= 22
    doc.text(left, ly, "INVOICE", 25, bold=True, color=look.accent)
    ly -= 20
    for line in _wrap(b["name"], 270, 13):
        doc.text(left, ly, line, 13)
        ly -= 16
    ry = y - 12
    for line in _wrap(b["address"], 230, 8.5) + _contact(b, data):
        doc.text(right, ry, fit(line, 230, 8.5), 8.5, color=MUTED, align="right")
        ry -= 11.5
    y = min(ly + 6, ry) - 14
    to, meta = _bill_to(c, 220), _meta(bill)
    h = max(len(to), len(meta)) * 13 + 16
    doc.rect(0, y - h, PAGE_W, h, GREY_BG)
    doc.text(left, y - 18, "Bill To", 9, bold=True)
    ty = y - 18
    for i, line in enumerate(to):
        doc.text(left + 54, ty, fit(line, 220, 9, not i), 9, bold=not i, color=INK if not i else MUTED)
        ty -= 13
    ty = y - 18
    for label, value in meta:
        doc.text(right - 100, ty, label, 9, bold=True, align="right")
        doc.text(right, ty, value, 9, align="right")
        ty -= 13
    return y - h - 14


def _top_classic(doc, bill, look, left, right) -> float:
    """INVOICE in black with the business's name in its colour, the logo centred above, then three columns: the
    business's details, the invoice's number and dates, and who it's to."""
    data = bill["data"]
    b, c = data["business"], data["client"]
    y = PAGE_H - MARGIN + 8
    if look.logo:
        scale = min(120 / look.logo["w"], 44 / look.logo["h"])
        doc.image(look.logo, PAGE_W / 2 - look.logo["w"] * scale / 2, y, 120, 44)
    y -= 46
    doc.text(left, y, "INVOICE", 26, bold=True)
    y -= 22
    for line in _wrap(b["name"], 300, 12.5, True):
        doc.text(left, y, line, 12.5, bold=True, color=look.accent)
        y -= 16
    y -= 8
    c1, c2, c3 = left, left + 165, left + 345
    y1 = y
    for line in _contact(b, data):
        doc.text(c1, y1, fit(line, 150, 8.5), 8.5, color=MUTED)
        y1 -= 11.5
    y1 -= 2
    for line in _wrap(b["address"], 150, 8.5):
        doc.text(c1, y1, fit(line, 150, 8.5), 8.5)
        y1 -= 11.5
    y2 = y
    for label, value in _meta(bill):
        doc.text(c2, y2, label, 9)
        doc.text(c2 + 82, y2, value, 9, color=MUTED)
        y2 -= 14
    doc.text(c3, y, "Bill To", 9, bold=True)
    y3 = y - 14
    for i, line in enumerate(_bill_to(c, right - c3)):
        doc.text(c3, y3, fit(line, right - c3, 9), 9, color=INK if not i else MUTED)
        y3 -= 12
    return min(y1, y2, y3) - 12


def _stamp(doc, right: float, top: float, paid_at: str) -> None:
    """A green PAID stamp with the date, its top right corner at (right, top)."""
    w, h = 104, 46
    x, y = right - w, top - h
    doc.round_rect(x, y, w, h, 7, PAID_GREEN, 2.5)
    when = _d(paid_at[:10])
    doc.text(x + (w - text_width(when, 8.5, True)) / 2, y + h - 13, when, 8.5, bold=True, color=PAID_GREEN)
    doc.text(x + (w - text_width("PAID", 24, True)) / 2, y + 8, "PAID", 24, bold=True, color=PAID_GREEN)


def bill_pdf(bill: dict, paid_at: str | None = None) -> bytes:
    """The invoice as a PDF, in its business's layout. Give paid_at (when it was marked paid) for a PAID stamp."""
    data = bill["data"]
    b, c = data["business"], data["client"]
    doc = Document(f"Invoice {bill['number']} – {b['name']}")
    look = brand(b)   # the logo and colour this invoice was made with
    layout = _layout(b)
    left, right = MARGIN, PAGE_W - MARGIN
    vat_on = data["vat_rate"] is not None
    y = {"band": _top_band, "classic": _top_classic}.get(layout, _top_modern)(doc, bill, look, left, right)

    inset = 0 if layout else 6
    cols = [("Description", left + inset, "left"), ("Quantity", right - (200 if vat_on else 150), "right"),
            ("Rate", right - (125 if vat_on else 80), "right")]
    if vat_on:
        cols.append(("VAT", right - 72, "right"))
    cols.append(("Amount", right - inset, "right"))
    desc_w = cols[1][1] - 70 - cols[0][1]

    def head() -> None:
        nonlocal y
        if layout == "band":
            doc.rect(0, y - 20, PAGE_W, 20, look.accent)
            for label, x, align in cols:
                doc.text(x, y - 13.5, label, 8.5, bold=True, color=WHITE, align=align)
            y -= 24
        elif layout == "classic":
            for label, x, align in cols:
                doc.text(x, y - 13, label, 10, bold=True, color=look.accent, align=align)
            y -= 21
            doc.line(left, y, right, y)
        else:
            doc.rect(left, y - 18, right - left, 18, look.tint if b.get("colour") else GREY_BG)
            for label, x, align in cols:
                doc.text(x, y - 12, label, 8, bold=True, color=MUTED, align=align)
            y -= 18
            doc.line(left, y, right, y, 0.8, look.accent)

    def room(height: float) -> None:
        nonlocal y
        if y - height < MARGIN + 24:
            doc.new_page()
            y = PAGE_H - MARGIN
            head()

    head()
    pad = 3 if layout else 0
    for l in data["lines"]:
        height = 18 + (12 if l.get("detail") else 0) + 2 * pad
        room(height)
        base = y - 12 - pad
        doc.text(cols[0][1], base, fit(l["description"], desc_w, 9), 9)
        if l.get("detail"):
            doc.text(cols[0][1], base - 12, fit(l["detail"], desc_w, 8.5), 8.5, color=MUTED)
        doc.text(cols[1][1], base, quantity_words(l), 9, align="right")
        if l.get("extra") != "expense":
            doc.text(cols[2][1], base, _gbp(l["rate"]), 9, align="right")
        if vat_on:
            doc.text(cols[3][1], base, f"{data['vat_rate']:g}%", 9, align="right")
        doc.text(cols[-1][1], base, _gbp(l["amount"]), 9, align="right")
        y -= height
        if layout != "band":
            doc.line(left, y, right, y)

    pay = _wrap(b["payment"], 250 if layout else right - left - 20, 9) if b["payment"] else []
    foot = _wrap(b["footer"], right - left, 8.5) if b["footer"] else []
    if not layout:
        room(90)
        y -= 10
        totals = [("Subtotal", _gbp(bill["net"]))]
        if vat_on:
            totals.append((f"VAT at {data['vat_rate']:g}%", _gbp(bill["vat"])))
        for label, value in totals:
            doc.text(right - 110, y - 10, label, 9, color=MUTED, align="right")
            doc.text(right - 6, y - 10, value, 9, align="right")
            y -= 16
        y -= 4   # the amount to pay, in a panel of the business's colour
        doc.rect(right - 230, y - 28, 230, 28, look.tint)
        doc.rect(right - 230, y - 28, 3, 28, look.accent)
        doc.text(right - 110, y - 18, "Total due", 11, bold=True, align="right")
        doc.text(right - 8, y - 19, _gbp(bill["total"]), 14, bold=True, align="right")
        y -= 50
        if paid_at:
            room(60)
            _stamp(doc, right, y + 10, paid_at)
            y -= 48
        if pay:
            box = 12 * len(pay) + 34
            room(box + 10)
            doc.rect(left, y - box, right - left, box, look.tint)
            doc.text(left + 10, y - 16, f"HOW TO PAY · due by {_d(bill['due_on'])}", 7.5, bold=True, color=MUTED)
            py = y - 32
            for line in pay:
                doc.text(left + 10, py, line, 9)
                py -= 12
            y -= box + 14
    else:
        # payment details on the left, the sums on the right, side by side
        sums = [("Subtotal", bill["net"])]
        if vat_on:
            sums.append((f"VAT {data['vat_rate']:g}%", bill["vat"]))
        sums.append(("Total", bill["total"]))
        if paid_at:
            sums.append(("Paid", bill["total"]))
        right_h = 16 * len(sums) + 40 + (56 if paid_at else 0)
        left_h = (12 * len(pay) + 34) if pay else 0
        room(max(left_h, right_h) + 14)
        y -= 16 if layout == "band" else 10
        top = y
        classic = layout == "classic"
        for label, value in sums:
            doc.text(right - 145, y - 10, (label.upper() if classic else label) + ":", 9 if not classic else 8.5,
                     bold=classic, align="right")
            doc.text(right, y - 10, _gbp(value), 9, bold=classic, align="right")
            y -= 16
        y -= 6
        doc.line(right - 285, y, right, y, 1.6 if classic else 0.8, look.accent if classic else (0.82, 0.84, 0.86))
        due = 0.0 if paid_at else bill["total"]
        doc.text(right - 145, y - 20, "BALANCE DUE" if classic else "Balance due", 12.5, bold=True, align="right")
        doc.text(right, y - 20, _gbp(due), 12.5, bold=True, align="right")
        y -= 32
        if paid_at:
            _stamp(doc, right, y - 4, paid_at)
            y -= 54
        if pay:
            py = top - 10
            doc.text(left, py, "Payment info", 9, bold=True)
            py -= 15
            for line in pay:
                doc.text(left, py, line, 9, color=MUTED)
                py -= 12
            if not paid_at:
                doc.text(left, py - 3, f"Due by {_d(bill['due_on'])}", 9, bold=True)
                py -= 15
            y = min(y, py)
        y -= 14
    for line in foot:
        room(12)
        doc.text(left, y - 9, line, 8.5, color=FAINT)
        y -= 12
    return doc.to_bytes()


# ---------- page ----------

BACK_LINK = ('''<a class="btn back" href="/" onclick="if(history.length>1&&document.referrer.indexOf(location.origin)===0)'''
             '''{history.back();return false}">← Tracker</a>''')

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
.top::before{content:"";display:block;width:40px;height:3px;border-radius:2px;background:var(--accent,#F97316);position:absolute;margin-top:-16px}
img.logo{display:block;max-width:220px;max-height:72px;margin:0 0 12px}h2{color:var(--accent,#6B7280)!important}
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
.totals td:first-child{color:#6B7280}.totals tr.due td{font:700 17px Sora,Inter,sans-serif;color:#111827;background:var(--tint,#FFF7ED);padding:9px 8px}
.totals tr.due td:first-child{border-left:3px solid var(--accent,#F97316);color:#111827}
.pay{background:var(--tint,#FFF7ED);border-radius:10px;padding:14px 18px;margin-top:28px;white-space:pre-line}
.pay h2{color:#6B7280!important}
.pay h2{margin:0 0 6px}.foot{color:#9CA3AF;font-size:12px;margin-top:20px;white-space:pre-line}
.hint{max-width:820px;margin:14px auto 0;color:#6B7280;font-size:13px}
a.back{margin-right:4px}
@media screen and (max-width:700px){body{padding:12px;overflow-wrap:anywhere}.sheet{padding:22px 18px}.meta{text-align:left}.meta table{margin-left:0}
 .meta td{text-align:left;padding-left:0;padding-right:16px}button.danger{margin-left:0}
 button,a.btn{padding:8px 12px;font-size:13px}.from b{font-size:18px}.meta .big{font-size:24px}
 table.lines,table.lines tbody{display:block}table.lines thead{display:none}
 table.lines tr{display:flex;flex-wrap:wrap;gap:2px 12px;padding:9px 0;border-bottom:1px solid #E5E7EB}
 table.lines td{padding:0;border:none}table.lines td:first-child{flex:1 1 100%;font-weight:500}
 table.lines td.n{color:#6B7280}table.lines td.n:last-child{margin-left:auto;color:#111827;font-weight:600}
 table.lines td:empty{display:none}.totals{width:100%}.pay{padding:12px 14px}}
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
const em = document.getElementById("email");
if (em) em.onclick = async () => {
  if (info.direct && !confirm(`Send invoice ${info.number} to ${info.to || "the client"} now? It goes straight away.`)) return;
  em.disabled = true; em.textContent = info.direct ? "Sending…" : "Making the draft…";
  try {
    await post("/api/bill/email", {id: info.id});
    em.textContent = info.direct ? "Sent ✓" : "Draft is in Mail ✓";
    document.getElementById("emailed").textContent = info.direct ? "Sent just now, with a copy to you."
      : "A draft is open in Mail on your Mac: check it and press send.";
  } catch (e) { alert(e.message); em.textContent = "Email it"; }
  em.disabled = false;
};
const v = document.getElementById("void");
if (v) v.onclick = async () => {
  if (!confirm(`Void invoice ${info.number}? Its number won't be used again, and the month goes back to "not invoiced".`)) return;
  try { await post("/api/bill/void", {id: info.id}); location.reload(); } catch (e) { alert(e.message); }
};
</script>"""


def bill_page(bill: dict, paid_at: str | None, today: date, emailed_at: str | None = None, direct: bool = False,
              to: str = "") -> str:
    esc = html.escape
    emailed = ""
    if emailed_at:
        kind, _, when = emailed_at.partition(":") if emailed_at[:1] in "ds" else ("draft", "", emailed_at)
        emailed = f"{'Sent' if kind == 'sent' else 'Draft last made'} {_d(when[:10])}."
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
                   '<button id="email">Email it</button>'
                   f'<a class="btn" href="/sheet.pdf?{sheet_q}">Save timesheet PDF</a>'
                   '<button onclick="window.print()">Print</button>'
                   + ('<button data-paid="0">Not paid</button>' if paid_at else '<button data-paid="1">Mark as paid</button>')
                   + '<button class="danger" id="void">Void</button>')
    extra = [esc(x) for x in (b["email"], b["phone"]) if x]
    if b["company_number"]:
        extra.append(f"Company number {esc(b['company_number'])}")
    if data["vat_rate"] is not None and b["vat_number"]:
        extra.append(f"VAT number {esc(b['vat_number'])}")
    rows = "".join(f"<tr><td>{esc(l['description'])}{'<div class=muted>' + esc(l['detail']) + '</div>' if l.get('detail') else ''}</td><td class=n>{quantity_words(l)}</td>"
                   f"<td class=n>{'' if l.get('extra') == 'expense' else _gbp(l['rate'])}</td>"
                   f"<td class=n>{_gbp(l['amount'])}</td></tr>" for l in data["lines"])
    vat = (f"<tr><td>VAT at {data['vat_rate']:g}%</td><td class=n>{_gbp(bill['vat'])}</td></tr>"
           if data["vat_rate"] is not None else "")
    pay = (f'<div class="pay"><h2>HOW TO PAY · DUE BY {_d(bill["due_on"])}</h2>{esc(b["payment"])}</div>'
           if b["payment"] else "")
    logo_img = (f'<img class="logo" src="/logo?key={quote(b["logo"])}" alt="{esc(b["name"])} logo">'
                if b.get("logo") and logos.original(b["logo"]) else "")
    accent = logos.clean_colour(b.get("colour")) or "#F97316"
    r, g, bl = logos.tint(logos.colour(accent, (0.98, 0.45, 0.09)))
    tint = "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(bl * 255))
    if b.get("website"):
        extra.append(esc(b["website"]))
    info = esc(json.dumps({"id": bill["id"], "number": bill["number"], "client": bill["client"], "month": bill["month"],
                           "direct": direct, "to": to}))
    return f"""<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Invoice {esc(bill['number'])} – {esc(bill['client'])}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700&family=Inter:wght@400;500;600&display=swap">
<style>{STYLE}
:root{{--accent:{accent};--tint:{tint}}}</style></head><body data-bill="{info}">
<div class="bar">{BACK_LINK}<span class="state {s['status']}">{state}</span>{buttons}</div>
<div class="sheet {s['status']}">
<div class="top"><div class="from">{logo_img}<b>{esc(b['name'])}</b>{esc(b['address'])}{''.join('<br>' + x for x in extra)}</div>
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
from the <a href="/invoice?{sheet_q}">timesheet</a>. <b>Email it</b> sends the invoice and timesheet the way this business is set up in Settings: a draft in Mail on your Mac to
check and send, or straight from your email provider. <span id="emailed">{emailed}</span>
<a href="{esc(mailto(bill))}">Use another email app instead</a> (you attach the PDF yourself).</p>
{SCRIPT}</body></html>"""
