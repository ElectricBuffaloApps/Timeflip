"""Monthly allowances: a task, or a whole client, capped at so many hours a week (a ticked "Bill as allowance"
limit). A client can have several task allowances, or one for the whole client, but not both.

Each month's maximum is the weekly hours shared across its working days (weekdays that aren't England and Wales
bank holidays), so a week split across two months is shared by its weekdays, rounded down to the client's
rounding block. You choose what to charge on the timesheet itself: tick "Roll over" on an entry to charge it in the
next month instead. Rolled entries appear in that month's "Brought forward" section, where they can be rolled on
again. This module works out the month's maximum and what's being charged, for the timesheet and the dashboard,
and the month-by-month ledger of carried hours.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import date, timedelta

from . import config as cfg
from .reports import days_between, round_seconds

HOLIDAYS_URL = "https://www.gov.uk/bank-holidays.json"
REGION = "england-and-wales"
REFRESH_SECONDS = 7 * 24 * 3600
RETRY_SECONDS = 3600
_lock = threading.Lock()
_last_try = 0.0


def _holidays_path():
    return cfg.APP_DIR / "bank-holidays.json"


def parse_holidays(data: dict) -> set[date]:
    return {date.fromisoformat(e["date"]) for e in (data.get(REGION) or {}).get("events", [])}


def bank_holidays(fetch: bool = True) -> tuple[set[date] | None, float | None]:
    """England and Wales bank holidays from gov.uk, kept in a local copy. (None, None) if never downloaded."""
    global _last_try
    path = _holidays_path()
    with _lock:
        age = time.time() - path.stat().st_mtime if path.exists() else None
        if fetch and (age is None or age > REFRESH_SECONDS) and time.time() - _last_try > RETRY_SECONDS:
            _last_try = time.time()
            try:
                with urllib.request.urlopen(HOLIDAYS_URL, timeout=8) as r:
                    data = json.loads(r.read())
                if parse_holidays(data):
                    cfg.APP_DIR.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".tmp")
                    tmp.write_text(json.dumps({REGION: data[REGION]}))
                    tmp.replace(path)
            except Exception as e:
                print(f"Couldn't download bank holidays: {e}")
        if not path.exists():
            return None, None
        try:
            return parse_holidays(json.loads(path.read_text())), path.stat().st_mtime
        except (ValueError, KeyError):
            return None, None


def working_days(start: date, end: date, holidays: set[date]) -> int:
    return sum(1 for d in days_between(start, end) if d.weekday() < 5 and d not in holidays)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _task_client(store, task: str) -> str | None:
    for t in store.tasks():
        if t["name"].lower() == task.lower():
            return t["tag"]
    return None


def _client_conf(conf: dict, client: str) -> dict:
    for name, c in (conf.get("clients") or {}).items():
        if name.lower() == client.lower():
            return c
    return {"billing": "hourly", "rounding": "none", "block_minutes": 15}


def configured(conf: dict) -> list[dict]:
    """Allowances are limits (on a task or a whole client) with "Bill as a monthly allowance" ticked."""
    out = []
    for l in cfg.migrate(dict(conf)).get("limits") or []:
        name = l.get("task") or l.get("client")
        if l.get("allowance") and name and l.get("weekly_hours") and l.get("allowance_start"):
            out.append({"scope": "task" if l.get("task") else "client", "name": name,
                        "hours_per_week": l["weekly_hours"], "start": l["allowance_start"],
                        "opening_hours": l.get("opening_hours") or 0})
    return out


def key(a: dict) -> str:
    """How an allowance's added hours are filed: the task's name, or "" for a whole-client allowance."""
    return a["name"] if a["scope"] == "task" else ""


def validate(limits: list[dict], tasks: list[dict]) -> None:
    """A client can't have an allowance for all its work and one for a task as well: hours would count twice."""
    client_of = {t["name"].lower(): t["tag"] for t in tasks}
    whole, by_task, seen = set(), {}, set()
    for a in configured({"limits": limits}):
        if (a["scope"], a["name"].lower()) in seen:
            raise ValueError(f"{a['name']} has more than one monthly allowance. Keep one.")
        seen.add((a["scope"], a["name"].lower()))
        if a["scope"] == "client":
            whole.add(a["name"].lower())
        elif client_of.get(a["name"].lower()):
            by_task.setdefault(client_of[a["name"].lower()].lower(), []).append(a["name"])
    for client in sorted(whole & set(by_task)):
        raise ValueError(f"{_proper(client, tasks)} has an allowance for all its work and one for "
                         f"{' and '.join(by_task[client])}, so the same hours would count twice. Keep one or the other.")


def _proper(client: str, tasks: list[dict]) -> str:
    return next((t["tag"] for t in tasks if (t["tag"] or "").lower() == client.lower()), client)


def for_client(store, conf: dict, client: str) -> list[dict]:
    """The client's allowances, in Settings order. One for the whole client replaces any on its tasks."""
    mine, seen = [], set()
    for a in configured(conf):
        owner = a["name"] if a["scope"] == "client" else (_task_client(store, a["name"]) or "")
        if owner.lower() == client.lower() and (a["scope"], a["name"].lower()) not in seen:
            seen.add((a["scope"], a["name"].lower()))
            mine.append(a)
    whole = [a for a in mine if a["scope"] == "client"]
    return whole[:1] or mine


def covers(a: dict, r: dict) -> bool:
    """Whether a timesheet entry counts towards this allowance."""
    return a["scope"] == "client" or r["task"].lower() == a["name"].lower()


def _owner(allowances: list[dict], r: dict) -> int:
    """Which allowance a rolled-over entry belongs to. One that fits none (its task's allowance was removed,
    say) stays with the client's first, so its hours are never lost."""
    return next((i for i, a in enumerate(allowances) if covers(a, r)), 0)


def _rolled_split(inv: dict, allowances: list[dict]) -> list[int]:
    """What rolling over took off the invoice, for each allowance. Day totals are rounded, so each allowance's
    share is what the day's charge drops by as its rolled entries come off, in turn."""
    out = [0] * len(allowances)
    block, mode = inv.get("block_minutes", 15), inv.get("rounding", "none")
    for g in inv["groups"]:
        for d in g["days"]:
            charged = [r for r in d["entries"] if not r["excluded"]]
            left = sum(r["seconds"] for r in charged)
            before = round_seconds(left, block, mode)
            for i in range(len(allowances)):
                left -= sum(r["seconds"] for r in charged if r["rolled"] and _owner(allowances, r) == i)
                now = round_seconds(left, block, mode)
                out[i] += before - now
                before = now
    return out


def _added(store, client: str, allowances: list[dict]) -> list[dict[str, int]]:
    """Carried hours added to each month's invoice (month -> seconds), for each allowance. Hours filed under a
    name that's no longer an allowance (or from before allowances had names) stay with the client's first."""
    keys = [key(a).lower() for a in allowances]
    out: list[dict[str, int]] = [{} for _ in allowances]
    for row in store.carry_rows(client):
        i = keys.index(row["task"].lower()) if row["task"].lower() in keys else 0
        out[i][row["month"]] = out[i].get(row["month"], 0) + row["seconds"]
    return out


def set_added(store, conf: dict, client: str, name: str, month: str, seconds: int) -> None:
    """Set the carried hours added to a month's invoice for one of the client's allowances ("" = its first)."""
    allowances = for_client(store, conf, client)
    keys = [key(a).lower() for a in allowances]
    if not allowances:
        raise ValueError(f"{client} has no monthly allowance.")
    i = keys.index(name.lower()) if name.lower() in keys else 0
    mine = {r["task"] for r in store.carry_rows(client)
            if r["month"] == month and (keys.index(r["task"].lower()) if r["task"].lower() in keys else 0) == i}
    for old in mine:
        store.set_carry_added(client, month, 0, old)
    store.set_carry_added(client, month, seconds, key(allowances[i]))


def _style(c: dict) -> str:
    return c.get("billing") if c.get("billing") in ("day", "week") else "day"


def _month_invoice(store, client: str, c: dict, month: str) -> dict:
    from .reports import invoice
    y, m = (int(x) for x in month.split("-"))
    return invoice(store, client, y, m, _style(c), c.get("block_minutes", 15), c.get("rounding", "none"))


def _earliest(store, allowances: list[dict]) -> str:
    return min([a["start"] for a in allowances] + [r["entry_date"][:7] for r in store.rolled().values()])


def _month_infos(allowances: list[dict], added: list[dict], c: dict, inv: dict, holidays: set[date],
                 balances: list[int]) -> list[dict]:
    """Each allowance's figures for the invoice's month. `balances` is the carried-over balance going into the
    month for each allowance, and is moved on to the balance going out of it."""
    block = int(c.get("block_minutes") or 0) * 60 if c.get("rounding", "none") != "none" else 0
    whole = (lambda v: v - v % block) if block else (lambda v: v)   # keep charged days in whole blocks
    start, end = date.fromisoformat(inv["start"]), date.fromisoformat(inv["end"])
    rows = [r for g in inv["groups"] for d in g["days"] for r in d["entries"]]
    rolled = _rolled_split(inv, allowances)
    days_n = working_days(start, end, holidays)
    infos = []
    for i, a in enumerate(allowances):
        per_day = float(a["hours_per_week"]) * 3600 / 5
        worked = _rounded_by_day([r for r in rows if not r["rolled"] and covers(a, r)], c)
        weeks: dict[date, dict] = {}
        for d in days_between(start, end):
            monday = d - timedelta(days=d.weekday())
            w = weeks.setdefault(monday, {"monday": monday.isoformat(), "from": d.isoformat(), "working_days": 0,
                                          "worked_s": 0})
            w["to"] = d.isoformat()
            w["working_days"] += d.weekday() < 5 and d not in holidays
            w["worked_s"] += worked.get(d.isoformat(), 0)
        for w in weeks.values():
            w["max_s"] = whole(round(per_day * w["working_days"]))
            w["spare_s"] = max(0, w["max_s"] - w["worked_s"])
        info = {"name": a["name"], "scope": a["scope"], "key": key(a), "client": inv["client"], "month": inv["month"],
                "start": a["start"], "working_days": days_n, "max_s": whole(round(per_day * days_n)),
                "worked_s": sum(worked.values()), "rolling_s": rolled[i], "weeks": [weeks[k] for k in sorted(weeks)]}
        # The balance: rolled-over entries and the hours brought in at the start build it; the buttons on each
        # timesheet add some of it to that month's invoice. Added hours fill the weeks with spare room first.
        opening = round(float(a.get("opening_hours") or 0) * 3600) if inv["month"] == a["start"] else 0
        available = balances[i] + opening
        added_now = min(added[i].get(inv["month"], 0), available)
        by_week, left = [], added_now
        for w in info["weeks"]:   # spread what's added over the weeks with spare room, earliest first
            take = min(left, w["spare_s"])
            if take:
                by_week.append({"monday": w["monday"], "seconds": take})
                left -= take
        info["carry"] = {"available_s": available, "added_s": added_now, "remaining_s": available - added_now,
                         "by_week": by_week, "unplaced_s": left, "opening_s": opening,
                         "spare_s": sum(w["spare_s"] for w in info["weeks"]), "rolling_s": rolled[i]}
        info["charging_s"] = info["worked_s"] + added_now
        balances[i] = available - added_now + rolled[i]
        infos.append(info)
    return infos


def _rounded_by_day(rows: list[dict], c: dict) -> dict[str, int]:
    """Time per day (excluded entries left out), rounded like the client's invoice."""
    by_day: dict[str, int] = {}
    for r in rows:
        if not r["excluded"]:
            by_day[r["date"]] = by_day.get(r["date"], 0) + r["seconds"]
    return {d: round_seconds(v, c.get("block_minutes", 15), c.get("rounding", "none")) for d, v in by_day.items()}


def for_invoice(store, conf: dict, inv: dict, holidays: set[date]) -> list[dict]:
    """The allowance figures for a client's monthly timesheet: one for each of the client's allowances."""
    allowances = for_client(store, conf, inv["client"])
    if not allowances:
        return []
    c = _client_conf(conf, inv["client"])
    added = _added(store, inv["client"], allowances)
    balances, m = [0] * len(allowances), _earliest(store, allowances)
    while m < inv["month"]:   # walk the earlier months to find the balance carried into this one
        _month_infos(allowances, added, c, _month_invoice(store, inv["client"], c, m), holidays, balances)
        m = next_month(m)
    return _month_infos(allowances, added, c, inv, holidays, balances)


def apply(inv: dict, infos: list[dict]) -> dict:
    """Attach the allowances and add the carried-over hours chosen for this month to the invoice."""
    inv["allowances"] = infos
    inv["total_billed_s"] += sum(i["carry"]["added_s"] for i in infos)
    return inv


def label(inv: dict, info: dict) -> str:
    """"HTB – Justin" for a task's allowance, "HTB" for the whole client's."""
    return f"{inv['client']} – {info['name']}" if info["scope"] == "task" else inv["client"]


def clients(store, conf: dict) -> list[str]:
    """Clients with an allowance, as TimeFlip spells them."""
    tags = {(t["tag"] or "").lower(): t["tag"] for t in store.tasks() if t["tag"]}
    out = []
    for a in configured(conf):
        owner = a["name"] if a["scope"] == "client" else (_task_client(store, a["name"]) or "")
        owner = tags.get(owner.lower())
        if owner and owner not in out:
            out.append(owner)
    return out


def summary(store, conf: dict, today: date) -> dict:
    """This month's position for every allowance, for the dashboard."""
    holidays, updated = bank_holidays()
    out = []
    for client in clients(store, conf):
        c = _client_conf(conf, client)
        for info in for_invoice(store, conf, _month_invoice(store, client, c, f"{today:%Y-%m}"), holidays or set()):
            info.pop("weeks")
            out.append(info)
    return {"allowances": out, "holidays_loaded": holidays is not None,
            "holidays_updated": date.fromtimestamp(updated).isoformat() if updated else None}


def ledger(store, conf: dict, today: date, holidays: set[date]) -> list[dict]:
    """Carried hours month by month for every allowance: what was rolled over, what was added to an invoice, and
    the balance left. The same sums as the timesheets, so the two always agree."""
    out = []
    for client in clients(store, conf):
        allowances = for_client(store, conf, client)
        c = _client_conf(conf, client)
        added = _added(store, client, allowances)
        invoiced = store.invoiced(client)
        books = [{"client": client, "name": a["name"], "scope": a["scope"], "start": a["start"],
                  "hours_per_week": a["hours_per_week"], "rate": c.get("rate"), "rows": []} for a in allowances]
        balances, m = [0] * len(allowances), _earliest(store, allowances)
        while m <= f"{today:%Y-%m}":
            for book, info in zip(books, _month_infos(allowances, added, c, _month_invoice(store, client, c, m),
                                                      holidays, balances)):
                k = info["carry"]
                row = {"month": m, "max_s": info["max_s"], "worked_s": info["worked_s"] + info["rolling_s"],
                       "rolled_s": info["rolling_s"], "added_s": k["added_s"], "opening_s": k["opening_s"],
                       "balance_s": k["remaining_s"] + info["rolling_s"], "invoiced": m in invoiced,
                       "current": m == f"{today:%Y-%m}"}
                if book["rows"] or m >= book["start"] or row["rolled_s"] or row["added_s"]:
                    book["rows"].append(row)
            m = next_month(m)
        for book, balance in zip(books, balances):
            book["balance_s"] = balance
            book["balance_value"] = round(balance / 3600 * book["rate"], 2) if book["rate"] else None
        out += books
    return out


def previous_month_name(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{(date(y, m, 1) - timedelta(days=1)):%B %Y}"


def next_month(month: str) -> str:
    y, m = _next_month(*(int(x) for x in month.split("-")))
    return f"{y}-{m:02d}"


def month_name(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{date(y, m, 1):%B %Y}"


def next_month_name(month: str) -> str:
    y, m = _next_month(*(int(x) for x in month.split("-")))
    return f"{date(y, m, 1):%B %Y}"
