"""Money to collect: prompts for money that's been earned but not invoiced, chased or tracked.

Each prompt says what to do and, where it can, what it's worth. Prompts that are a judgement call can be hidden;
their key includes the figures behind them, so a hidden prompt comes back if those figures change.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .allowance import working_days
from .reports import days_between, targets_summary, timesheet_rows

DEFAULT_TERMS_DAYS = 30
GAP_WORKING_DAYS = 10      # how far back to look for working days with nothing tracked
CHANGED_MONTHS = 6         # how far back to check invoiced months for changes
LEVELS = {"critical": 0, "warning": 1, "info": 2}


def terms_days(conf: dict) -> int:
    try:
        return max(0, int(conf.get("payment_terms_days", DEFAULT_TERMS_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_TERMS_DAYS


def _hm(seconds: int) -> str:
    m = round(abs(seconds) / 60)
    return f"{m // 60}h {m % 60:02d}m"


def _gbp(v: float) -> str:
    return f"£{v:,.2f}"


def _month_label(month: str, today: date) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{date(y, m, 1):%B}" + (f" {y}" if y != today.year else "")


def _month_dates(month: str) -> tuple[date, date]:
    y, m = (int(x) for x in month.split("-"))
    return date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)


def _months_back(today: date, n: int) -> str:
    y, m = today.year, today.month - n
    while m < 1:
        y, m = y - 1, m + 12
    return f"{y}-{m:02d}"


def _days(n: int) -> str:
    return f"{n} day{'s' * (n != 1)}"


def build(store, conf: dict, today: date, owed: dict, month_due, holidays: set[date]) -> dict:
    """Everything for the Money to collect panel.

    `owed` is the Outstanding data (months not invoiced and carried hours per paying client) and `month_due(client,
    month)` gives (seconds, amount) for what a month's invoice comes to now.
    """
    prompts: list[dict] = []
    terms = terms_days(conf)

    def add(key, kind, level, title, detail, value=None, client=None, month=None, action=None, dismissable=True,
            number=None, bill_id=None):
        prompts.append({"key": key, "kind": kind, "level": level, "title": title, "detail": detail,
                        "value": round(value, 2) if value else None, "client": client, "month": month,
                        "action": action, "dismissable": dismissable, "number": number, "bill_id": bill_id})

    # 1. Finished months not yet invoiced: the quickest money.
    to_invoice = 0.0
    for c in owed["clients"]:
        for mo in c["months"]:
            if mo["current"]:
                continue
            _, month_end = _month_dates(mo["month"])
            waiting = (today - month_end).days
            to_invoice += mo["value"] or 0
            add(f"invoice:{c['client'].lower()}:{mo['month']}", "invoice", "critical",
                f"Invoice {c['client']} for {_month_label(mo['month'], today)}",
                f"{_hm(mo['seconds'])} not invoiced. The month ended {_days(waiting)} ago.",
                mo["value"], c["client"], mo["month"], "invoice", dismissable=False)

    # 2. Invoices sent but not paid, and invoiced months that have changed since.
    awaiting = overdue = 0.0
    earliest = _months_back(today, CHANGED_MONTHS)
    clients = sorted({t["tag"] for t in store.tasks() if t["tag"]}, key=str.lower)
    for client in clients:
        paid = store.paid(client)
        for month, inv in sorted(store.invoiced(client).items()):
            label = _month_label(month, today)
            if month not in paid:
                bill = store.bill_for(client, month)   # an invoice made in the app carries its own dates and total
                if bill:
                    sent, due = date.fromisoformat(bill["issued_on"]), date.fromisoformat(bill["due_on"])
                    owed_amount, number, days = bill["total"], bill["number"], (due - sent).days
                else:
                    sent = datetime.fromisoformat(inv["marked_at"]).astimezone().date()
                    due = sent + timedelta(days=terms)
                    owed_amount, number, days = inv["amount"], None, terms
                bill_id = bill["id"] if bill else None
                which = f"invoice {number} ({label})" if number else label
                late = (today - due).days
                if late > 0:
                    overdue += owed_amount or 0
                    add(f"unpaid:{client.lower()}:{month}", "unpaid", "critical",
                        f"Chase {client}: {which} is {_days(late)} overdue",
                        f"Invoiced {sent:%d/%m/%Y}, due {due:%d/%m/%Y} ({days}-day terms).",
                        owed_amount, client, month, "paid", dismissable=False, number=number, bill_id=bill_id)
                else:
                    awaiting += owed_amount or 0
                    add(f"unpaid:{client.lower()}:{month}", "awaiting", "info",
                        f"Waiting on {client} for {which}",
                        f"Invoiced {sent:%d/%m/%Y}, due {due:%d/%m/%Y}" + (" (today)." if late == 0 else "."),
                        owed_amount, client, month, "paid", dismissable=False, number=number, bill_id=bill_id)
            if month >= earliest:
                secs, amount = month_due(client, month)
                diff = secs - inv["seconds"]
                if diff:
                    worth = None
                    if amount is not None and inv["amount"] is not None:
                        worth = amount - inv["amount"]
                    if diff > 0:
                        title = f"{client}: {_hm(diff)} added to {label} after you invoiced"
                        detail = "It isn't on the invoice you sent. Bill it, roll it over or exclude it."
                    else:
                        title = f"{client}: {label} is now {_hm(diff)} less than you invoiced"
                        detail = "The timesheet has changed since the invoice went out. Check they still match."
                    add(f"changed:{client.lower()}:{month}:{secs}", "changed", "warning", title, detail,
                        worth if worth and worth > 0 else None, client, month, "timesheet")

    # 3. Carried-over hours nobody has been charged for yet.
    for c in owed["clients"]:
        if c["carried_s"]:
            add(f"carried:{c['client'].lower()}:{c['carried_s']}", "carried", "warning",
                f"{c['client']}: {_hm(c['carried_s'])} carried over, not yet charged",
                "Add it to a timesheet with spare room, or it never gets billed.",
                c["carried_value"], c["client"], owed["month"], "timesheet")

    for client in clients:
        banked = store.lieu_balance(client)
        cc = next((c for n, c in (conf.get("clients") or {}).items() if n.lower() == client.lower()), {})
        if banked > 0 and not cc.get("notional") and cc.get("billing") != "monthly":
            add(f"lieu:{client.lower()}:{banked}", "lieu", "info",
                f"{client}: {_hm(banked)} in lieu, not yet used",
                "Add them to a day or week on the area's page and they go on the invoice.",
                banked / 3600 * cc["rate"] if cc.get("rate") else None, client, action="area")

    # 4. Time left off invoices that haven't gone out yet.
    rates = {name.lower(): c.get("rate") for name, c in (conf.get("clients") or {}).items()}
    for c in owed["clients"]:
        for mo in c["months"]:
            first, last = _month_dates(mo["month"])
            secs = sum(r["seconds"] for r in timesheet_rows(store, first, min(last, today), c["client"]) if r["excluded"])
            if secs:
                rate = rates.get(c["client"].lower())
                add(f"excluded:{c['client'].lower()}:{mo['month']}:{secs}", "excluded", "info",
                    f"{c['client']}: {_hm(secs)} excluded in {_month_label(mo['month'], today)}",
                    "Excluded time isn't charged. Worth a second look before the invoice goes.",
                    secs / 3600 * rate if rate else None, c["client"], mo["month"], "timesheet")

    # 5. Working days with nothing tracked at all.
    gaps = _gaps(store, today, holidays)
    if gaps:
        add("gap:" + ",".join(d.isoformat() for d in gaps), "gap", "info",
            f"Nothing tracked on {_days(len(gaps)).replace('day', 'working day')}",
            ", ".join(f"{d:%a %d/%m}" for d in gaps) + ". If you worked, add the time on a timesheet.",
            action="timesheets")

    # 6. Behind the pace needed to hit this month's targets.
    month = f"{today:%Y-%m}"
    first, last = _month_dates(month)
    total_days = working_days(first, last, holidays)
    done_days = working_days(first, today - timedelta(days=1), holidays)
    if total_days and done_days >= 5:
        pace = done_days / total_days
        summary = targets_summary(store, today, conf)
        income = next((r for r in summary["income"] if r["period"] == "month"), None)
        if income and income["target"]:
            short = income["target"] * pace - income["amount"]
            if short > 0.1 * income["target"] * pace:
                add(f"pace:income:{month}", "pace", "warning",
                    f"{_gbp(short)} behind pace for this month's income target",
                    f"{_gbp(income['amount'])} so far against {_gbp(income['target'])}, "
                    f"with {done_days} of {total_days} working days gone.")
        for row in summary["time"]:
            if row["period"] != "month":
                continue
            short_s = row["target_hours"] * 3600 * pace - row["seconds"]
            if short_s > 0.2 * row["target_hours"] * 3600 * pace:
                rate = rates.get(row["client"].lower())
                notional = any(n.lower() == row["client"].lower() and c.get("notional")
                               for n, c in (conf.get("clients") or {}).items())
                add(f"pace:{row['client'].lower()}:{month}", "pace", "info",
                    f"{row['client']}: {_hm(short_s)} behind pace this month",
                    f"{_hm(row['seconds'])} so far against a target of {row['target_hours']:g}h.",
                    short_s / 3600 * rate if rate and not notional else None, row["client"], month)

    # 7. Time that can't be valued.
    for name in owed.get("missing_rate", []):
        add(f"rate:{name.lower()}", "rate", "info", f"{name} has no hourly rate",
            "Add one in Settings so its time shows as money.", client=name, action="settings")

    hidden_keys = store.dismissed()
    shown = [p for p in prompts if not (p["dismissable"] and p["key"] in hidden_keys)]
    shown.sort(key=lambda p: (LEVELS[p["level"]], -(p["value"] or 0)))
    return {"prompts": shown, "hidden": len(prompts) - len(shown), "terms_days": terms,
            "to_invoice": round(to_invoice, 2), "awaiting": round(awaiting, 2), "overdue": round(overdue, 2)}


def _gaps(store, today: date, holidays: set[date]) -> list[date]:
    """Recent working days (not today) with no counted time. Empty until there's some tracking to compare with."""
    days, d = [], today - timedelta(days=1)
    while len(days) < GAP_WORKING_DAYS:
        if d.weekday() < 5 and d not in holidays:
            days.append(d)
        d -= timedelta(days=1)
    start = days[-1]
    tracked = {r["date"] for r in timesheet_rows(store, start, today - timedelta(days=1))}
    if not tracked:
        return []
    first = min(tracked)   # don't count days before tracking began
    return sorted(x for x in days_between(start, today - timedelta(days=1))
                  if x in days and x.isoformat() not in tracked and x.isoformat() > first)
