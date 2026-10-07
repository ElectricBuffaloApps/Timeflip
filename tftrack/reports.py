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
    excluded = store.excluded_ids()
    rows = []
    for d, seg in segments_by_day(store, start, end):
        seg_client = seg["client"] or NO_CLIENT
        if client and seg_client.lower() != client.lower():
            continue
        local_start, local_end = seg["start"].astimezone(), seg["end"].astimezone()
        is_excluded = seg["id"] in excluded
        note = ""
        if seg["manual"]:
            note = "added by hand"
        elif seg["original"]:
            o = seg["original"]
            os_, oe = o["start"].astimezone(), o["end"].astimezone()
            when = f"{os_:%H:%M}–{oe:%H:%M}"
            if os_.date() != seg["entry_start"].astimezone().date():
                when = f"{os_:%a %d %b} {when}"
            note = f"was {when}" + (f", {o['task']}" if o["task_changed"] else "")
        es, ee = seg["entry_start"].astimezone(), seg["entry_end"].astimezone()
        rows.append({
            "id": seg["id"],
            "task_id": seg["task_id"],
            "adjusted": seg["adjusted"],
            "manual": seg["manual"],
            "note": note,
            "entry_date": es.date().isoformat(),
            "entry_start": es.strftime("%H:%M"),
            "entry_end": ee.strftime("%H:%M"),
            "excluded": is_excluded,
            "seconds": seg["seconds"],
            "date": d.isoformat(),
            "start": local_start.strftime("%H:%M"),
            "end": local_end.strftime("%H:%M"),
            "client": seg_client,
            "task": seg["task"],
            "hours": round(seg["seconds"] / 3600, 2),
            "billable": seg["billable"],
            "rate": seg["hourly_rate"] if seg["billable"] else None,
            "currency": currency_symbol(seg["currency"]).strip(),
            "amount": 0.0 if is_excluded else round(amount(seg), 2),
        })
    return rows


def timesheet_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Start", "End", "Client", "Task", "Hours", "Billable", "Rate", "Currency", "Amount",
                "Excluded", "Note"])
    for r in rows:
        w.writerow([r["date"], r["start"], r["end"], r["client"], r["task"], f"{r['hours']:.2f}",
                    "Yes" if r["billable"] else "No", "" if r["rate"] is None else r["rate"],
                    r["currency"], f"{r['amount']:.2f}", "Yes" if r["excluded"] else "", r["note"]])
    rows = [r for r in rows if not r["excluded"]]
    total_hours = sum(r["hours"] for r in rows)
    currencies = {r["currency"] for r in rows if r["amount"]}
    if len(currencies) <= 1:
        total_amount = f"{sum(r['amount'] for r in rows):.2f}"
        currency = currencies.pop() if currencies else ""
    else:  # never add up different currencies
        total_amount, currency = "", "mixed"
    w.writerow(["Total", "", "", "", "", f"{total_hours:.2f}", "", "", currency, total_amount])
    return buf.getvalue()


# ---------- monthly invoices: by day or by week ----------

BILLING_STYLES = ("hourly", "day", "week")
ROUNDING_MODES = ("none", "nearest", "up", "down")


def round_seconds(seconds: int, block_minutes: int, mode: str) -> int:
    if mode == "none" or not block_minutes or seconds <= 0:
        return seconds
    block = block_minutes * 60
    whole, rest = divmod(seconds, block)
    if mode == "up":
        whole += 1 if rest else 0
    elif mode == "nearest":
        whole += 1 if rest * 2 >= block else 0
    return whole * block


def month_range(year: int, month: int, style: str) -> tuple[date, date]:
    """Dates an invoice for this month covers.

    By week: every week whose Monday falls in the month, so the last week can run into the next month
    (and the days before the month's first Monday belong to the previous month's invoice).
    """
    first = date(year, month, 1)
    last = (date(year + (month == 12), month % 12 + 1, 1)) - timedelta(days=1)
    if style != "week":
        return first, last
    first_monday = first + timedelta(days=(7 - first.weekday()) % 7)
    last_monday = last - timedelta(days=last.weekday())
    return first_monday, last_monday + timedelta(days=6)


def invoice(store, client: str, year: int, month: int, style: str,
            block_minutes: int = 15, rounding: str = "none") -> dict:
    """Entries grouped by day (and by week for weekly clients), with day totals rounded to the block."""
    start, end = month_range(year, month, style)
    rows = timesheet_rows(store, start, end, client)
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)

    def day_entry(d: date) -> dict:
        entries = by_day.get(d.isoformat(), [])
        actual = sum(r["seconds"] for r in entries if not r["excluded"])
        return {"date": d.isoformat(), "entries": entries, "actual_s": actual,
                "billed_s": round_seconds(actual, block_minutes, rounding)}

    groups = []
    if style == "week":
        monday = start
        while monday <= end:
            days = [day_entry(monday + timedelta(days=n)) for n in range(7)]
            days = [d for d in days if d["entries"]]
            groups.append({"label": f"Week commencing {monday:%a %d %b %Y}", "days": days,
                           "billed_s": sum(d["billed_s"] for d in days)})
            monday += timedelta(days=7)
    else:
        days = [day_entry(d) for d in days_between(start, end)]
        days = [d for d in days if d["entries"]]
        groups.append({"label": None, "days": days, "billed_s": sum(d["billed_s"] for d in days)})
    all_days = [d for g in groups for d in g["days"]]
    return {
        "client": client, "style": style, "start": start.isoformat(), "end": end.isoformat(),
        "month": f"{year}-{month:02d}", "block_minutes": block_minutes, "rounding": rounding,
        "groups": groups,
        "days_worked": sum(1 for d in all_days if d["billed_s"] > 0),
        "total_billed_s": sum(g["billed_s"] for g in groups),
        "total_actual_s": sum(d["actual_s"] for d in all_days),
    }


def invoice_lines(inv: dict, rate: float | None = None) -> list[dict]:
    """One invoice line per billed day (by-the-day clients) or per week (by-the-week clients)."""
    lines = []

    def add(description: str, seconds: int) -> None:
        if seconds <= 0:
            return
        hours = round(seconds / 3600, 2)
        lines.append({"description": description, "hours": hours, "rate": rate,
                      "amount": round(hours * rate, 2) if rate else None})

    for g in inv["groups"]:
        if inv["style"] == "week":
            add(f"{inv['client']} – {g['label'].replace('Week commencing', 'week commencing')}", g["billed_s"])
        else:
            for d in g["days"]:
                add(f"{inv['client']} – {date.fromisoformat(d['date']):%a %d %b %Y}", d["billed_s"])
    return lines


# ---------- money from the per-client rates in Settings ----------

def _client_conf(clients_conf: dict, client: str | None) -> dict | None:
    if not client:
        return None
    for name, c in (clients_conf or {}).items():
        if name.lower() == client.lower() and c.get("rate"):
            return c
    return None


def money_summary(store, start: date, end: date, clients_conf: dict) -> dict:
    """Earnings (paying clients) and imagined value (own-business clients) for a date range.

    Per client, each day's charged time is rounded the same way as its invoices, then priced at its rate.
    Per task the figures are before rounding, so they can differ from the client totals by a few pence.
    """
    per_client_day: dict[str, dict[str, int]] = {}
    by_task: dict[str, dict] = {}
    for r in timesheet_rows(store, start, end):
        conf = _client_conf(clients_conf, r["client"] if r["client"] != NO_CLIENT else None)
        if r["excluded"] or not conf:
            continue
        days = per_client_day.setdefault(r["client"], {})
        days[r["date"]] = days.get(r["date"], 0) + r["seconds"]
        t = by_task.setdefault(r["task"], {"amount": 0.0, "notional": bool(conf.get("notional"))})
        t["amount"] += r["seconds"] / 3600 * float(conf["rate"])
    by_client, earned, value = [], 0.0, 0.0
    for client, days in per_client_day.items():
        conf = _client_conf(clients_conf, client)
        billed = sum(round_seconds(sec, conf.get("block_minutes", 15), conf.get("rounding", "none"))
                     for sec in days.values())
        amount = round(billed / 3600 * float(conf["rate"]), 2)
        notional = bool(conf.get("notional"))
        by_client.append({"client": client, "amount": amount, "notional": notional})
        if notional:
            value += amount
        else:
            earned += amount
    return {
        "earned": round(earned, 2), "value": round(value, 2),
        "by_client": sorted(by_client, key=lambda c: (c["notional"], -c["amount"])),
        "by_task": {k: {"amount": round(v["amount"], 2), "notional": v["notional"]} for k, v in by_task.items()},
    }
