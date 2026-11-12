"""Insights for the Reports tab: what was earned month by month, how clients compare, and what the numbers suggest.

Money is "earned" the same way as everywhere else: paying clients' time at their rate (rounded like their invoices)
plus monthly-income clients' income. Own-business value is never counted.
"""
from __future__ import annotations

from datetime import date, timedelta

from .reports import NO_CLIENT, month_range, month_summary, timesheet_rows

MONTHS = 12
RECENT = 3          # full months used for "lately" figures
WEEKDAY_WEEKS = 12


def _back(year: int, month: int, n: int) -> tuple[int, int]:
    month -= n
    while month < 1:
        year, month = year - 1, month + 12
    return year, month


def _gbp(v: float) -> str:
    return f"£{v:,.2f}" if abs(v) < 1000 else f"£{v:,.0f}"


def build(store, conf: dict, today: date) -> dict:
    clients_conf = conf.get("clients", {}) or {}
    incomes = conf.get("monthly_income", {}) or {}
    months = []
    for n in range(MONTHS - 1, -1, -1):
        y, m = _back(today.year, today.month, n)
        s = month_summary(store, y, m, clients_conf, incomes)
        # a monthly-income figure carried forward is only an estimate for the month in progress
        paying = [{"client": c["client"], "amount": round(c["amount"] or 0, 2), "seconds": c["seconds"]}
                  for c in s["clients"] if c["kind"] in ("hourly", "monthly")]
        first, last = month_range(y, m, "day")
        tracked = sum(r["seconds"] for r in timesheet_rows(store, first, last) if not r["excluded"])
        months.append({"month": s["month"], "label": f"{first:%b}", "name": f"{first:%B %Y}",
                       "earned": s["earned"], "seconds": tracked, "current": n == 0, "clients": paying,
                       "paid_seconds": sum(c["seconds"] for c in paying)})

    # trim months before any tracking began, so a new user doesn't see a year of zeros
    while len(months) > 1 and not months[0]["seconds"] and not months[0]["earned"]:
        months.pop(0)

    full = [m for m in months if not m["current"]]
    recent, before = full[-RECENT:], full[-2 * RECENT:-RECENT]
    avg = lambda rows: round(sum(m["earned"] for m in rows) / len(rows), 2) if rows else None

    # how clients compare, over the recent full months (or the current month if that's all there is)
    span = recent or months
    per: dict[str, dict] = {}
    for m in span:
        for c in m["clients"]:
            p = per.setdefault(c["client"], {"client": c["client"], "amount": 0.0, "seconds": 0})
            p["amount"] += c["amount"]
            p["seconds"] += c["seconds"]
    total_amount = sum(p["amount"] for p in per.values())
    clients = sorted(per.values(), key=lambda p: -p["amount"])
    for p in clients:
        p["amount"] = round(p["amount"], 2)
        p["rate"] = round(p["amount"] / (p["seconds"] / 3600), 2) if p["seconds"] and p["amount"] else None
        p["share"] = round(100 * p["amount"] / total_amount, 1) if total_amount else 0.0
    paid_s = sum(p["seconds"] for p in clients if p["amount"])
    tracked_s = sum(m["seconds"] for m in span)
    avg_rate = round(total_amount / (paid_s / 3600), 2) if paid_s else None

    # tax year to date (from 6 April)
    start = date(today.year, 4, 6) if today >= date(today.year, 4, 6) else date(today.year - 1, 4, 6)
    tax = _earned_between(store, clients_conf, incomes, start, today)

    # which days of the week the work happens
    monday = today - timedelta(days=today.weekday())
    w_start = monday - timedelta(weeks=WEEKDAY_WEEKS)
    by_day = [0] * 7
    for r in timesheet_rows(store, w_start, monday - timedelta(days=1)):
        if not r["excluded"]:
            by_day[date.fromisoformat(r["date"]).weekday()] += r["seconds"]
    weekdays = [{"day": d, "seconds": round(s / WEEKDAY_WEEKS)} for d, s in zip("Mon Tue Wed Thu Fri Sat Sun".split(), by_day)]

    return {
        "months": months, "clients": clients, "weekdays": weekdays,
        "span_label": (f"last {len(recent)} full month{'s' * (len(recent) != 1)}" if recent else "this month so far"),
        "kpis": {"tax_year": tax, "tax_year_from": start.isoformat(), "recent_avg": avg(recent),
                 "before_avg": avg(before), "this_month": months[-1]["earned"], "avg_rate": avg_rate,
                 "best": max(full, key=lambda m: m["earned"], default=None)},
        "notes": _notes(clients, avg_rate, avg(recent), avg(before), tracked_s, paid_s, len(span), bool(recent)),
    }


def _earned_between(store, clients_conf: dict, incomes: dict, start: date, end: date) -> float:
    """Earned from start to end: whole months from month_summary, the part-month at the start priced by the day."""
    from .reports import money_summary
    total = 0.0
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        first, last = month_range(y, m, "day")
        if first < start:   # 6 April to the end of April: hourly money only, plus that month's monthly income
            total += money_summary(store, start, min(last, end), clients_conf)["earned"]
            s = month_summary(store, y, m, clients_conf, incomes)
            total += sum(c["amount"] or 0 for c in s["clients"] if c["kind"] == "monthly")
        else:
            total += month_summary(store, y, m, clients_conf, incomes)["earned"]
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return round(total, 2)


def _notes(clients: list[dict], avg_rate, recent_avg, before_avg, tracked_s: int, paid_s: int, n_months: int,
           full_months: bool) -> list[dict]:
    """Plain-English observations, each one arithmetic on the figures shown (never a forecast)."""
    notes = []
    per = "a month" if full_months else "so far this month"
    rated = [c for c in clients if c["rate"]]
    if avg_rate and len(rated) >= 2:
        low = min(rated, key=lambda c: c["rate"])
        high = max(rated, key=lambda c: c["rate"])
        gap = (avg_rate - low["rate"]) * low["seconds"] / 3600 / n_months
        if low["rate"] < 0.9 * avg_rate and gap >= 1:
            notes.append({"kind": "rate", "title": f"{low['client']} pays the least per hour",
                          "text": f"{_gbp(low['rate'])} an hour against your average of {_gbp(avg_rate)} "
                                  f"(best: {high['client']} at {_gbp(high['rate'])}). At your average rate, the same "
                                  f"hours would be worth {_gbp(gap)} more {per}."})
    if clients and clients[0]["share"] >= 60 and len([c for c in clients if c["amount"]]) >= 1:
        notes.append({"kind": "risk", "title": f"{clients[0]['share']:.0f}% of your income comes from {clients[0]['client']}",
                      "text": "If that client paused, most of your income would pause with it."})
    if tracked_s and paid_s < tracked_s:
        unpaid = tracked_s - paid_s
        share = 100 * unpaid / tracked_s
        if share >= 15:
            worth = f" At your average rate that time would be {_gbp(unpaid / 3600 * avg_rate / n_months)} {per}." if avg_rate else ""
            notes.append({"kind": "unpaid", "title": f"{share:.0f}% of your tracked time earns nothing",
                          "text": f"{round(unpaid / 3600 / n_months)} hours {per} goes on time with no client, no "
                                  f"rate, or your own business.{worth}"})
    if recent_avg is not None and before_avg:
        change = 100 * (recent_avg - before_avg) / before_avg
        if abs(change) >= 10:
            notes.append({"kind": "trend", "title": f"Income is {'up' if change > 0 else 'down'} {abs(change):.0f}%",
                          "text": f"The last 3 full months averaged {_gbp(recent_avg)}, against {_gbp(before_avg)} for the "
                                  "3 before."})
    return notes
