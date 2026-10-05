"""Report and timesheet data built from the local copy."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta

CURRENCY_SYMBOLS = {"POUND": "£", "EURO": "€", "DOLLAR": "$", "YEN": "¥", "YUAN": "¥"}
NO_CLIENT = "No client"


def currency_symbol(code: str | None) -> str:
    if not code:
        return ""
    return CURRENCY_SYMBOLS.get(code, code + " ")


def local_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day).astimezone()


def days_between(start: date, end: date) -> list[date]:
    """Inclusive list of dates."""
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


def segments_by_day(store, start: date, end: date) -> list[tuple[date, dict]]:
    """Every tracked segment in the range, split at local midnight."""
    out = []
    for d in days_between(start, end):
        for seg in store.segments(local_midnight(d), local_midnight(d + timedelta(days=1))):
            out.append((d, seg))
    return out


def amount(seg: dict) -> float:
    if not seg["billable"] or not seg["hourly_rate"]:
        return 0.0
    return seg["seconds"] / 3600 * float(seg["hourly_rate"])


def build_report(store, start: date, end: date, group: str = "task") -> dict:
    """Seconds per day per task (or per client), with totals and billable amounts."""
    days = days_between(start, end)
    index = {d: n for n, d in enumerate(days)}
    series: dict[str, dict] = {}
    totals_by_currency: dict[str, float] = {}
    for d, seg in segments_by_day(store, start, end):
        name = seg["task"] if group == "task" else (seg["client"] or NO_CLIENT)
        s = series.setdefault(name, {"name": name, "values": [0] * len(days), "total": 0, "amounts": {}})
        s["values"][index[d]] += seg["seconds"]
        s["total"] += seg["seconds"]
        a = amount(seg)
        if a:
            sym = currency_symbol(seg["currency"])
            s["amounts"][sym] = s["amounts"].get(sym, 0) + a
            totals_by_currency[sym] = totals_by_currency.get(sym, 0) + a
    ordered = sorted(series.values(), key=lambda s: -s["total"])
    return {
        "days": [d.isoformat() for d in days],
        "series": ordered,
        "total_seconds": sum(s["total"] for s in ordered),
        "amounts": {k: round(v, 2) for k, v in totals_by_currency.items()},
    }


def timesheet_rows(store, start: date, end: date, client: str | None = None) -> list[dict]:
    rows = []
    for d, seg in segments_by_day(store, start, end):
        seg_client = seg["client"] or NO_CLIENT
        if client and seg_client.lower() != client.lower():
            continue
        local_start, local_end = seg["start"].astimezone(), seg["end"].astimezone()
        rows.append({
            "date": d.isoformat(),
            "start": local_start.strftime("%H:%M"),
            "end": local_end.strftime("%H:%M"),
            "client": seg_client,
            "task": seg["task"],
            "hours": round(seg["seconds"] / 3600, 2),
            "billable": seg["billable"],
            "rate": seg["hourly_rate"] if seg["billable"] else None,
            "currency": currency_symbol(seg["currency"]).strip(),
            "amount": round(amount(seg), 2),
        })
    return rows


def timesheet_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Start", "End", "Client", "Task", "Hours", "Billable", "Rate", "Currency", "Amount"])
    for r in rows:
        w.writerow([r["date"], r["start"], r["end"], r["client"], r["task"], f"{r['hours']:.2f}",
                    "Yes" if r["billable"] else "No", "" if r["rate"] is None else r["rate"],
                    r["currency"], f"{r['amount']:.2f}"])
    total_hours = sum(r["hours"] for r in rows)
    currencies = {r["currency"] for r in rows if r["amount"]}
    if len(currencies) <= 1:
        total_amount = f"{sum(r['amount'] for r in rows):.2f}"
        currency = currencies.pop() if currencies else ""
    else:  # never add up different currencies
        total_amount, currency = "", "mixed"
    w.writerow(["Total", "", "", "", "", f"{total_hours:.2f}", "", "", currency, total_amount])
    return buf.getvalue()
