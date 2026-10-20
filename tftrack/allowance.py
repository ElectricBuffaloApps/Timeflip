"""Monthly allowances: a task capped at so many hours a week (a ticked "Bill as allowance" limit).

Each month's maximum is the weekly hours shared across its working days (weekdays that aren't England and Wales
bank holidays), so a week split across two months is shared by its weekdays, rounded down to the client's
rounding block. You choose what to charge on the timesheet itself: tick "Roll over" on an entry to charge it in the
next month instead. Rolled entries appear in that month's "Brought forward" section, where they can be rolled on
again. This module works out the month's maximum and what's being charged, for the timesheet and the dashboard.
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
    """Allowances are task limits with "Bill as a monthly allowance" ticked."""
    return [{"task": l["task"], "hours_per_week": l["weekly_hours"], "start": l["allowance_start"],
             "opening_hours": l.get("opening_hours") or 0}
            for l in cfg.migrate(dict(conf)).get("limits") or []
            if l.get("allowance") and l.get("task") and l.get("weekly_hours") and l.get("allowance_start")]


def _rounded_by_day(rows: list[dict], task: str, c: dict) -> dict[str, int]:
    """The task's time per day (excluded entries left out), rounded like the client's invoice."""
    by_day: dict[str, int] = {}
    for r in rows:
        if not r["excluded"] and r["task"].lower() == task.lower():
            by_day[r["date"]] = by_day.get(r["date"], 0) + r["seconds"]
    return {d: round_seconds(v, c.get("block_minutes", 15), c.get("rounding", "none")) for d, v in by_day.items()}


def _allowance_for(store, conf: dict, client: str) -> dict | None:
    for a in configured(conf):
        if (_task_client(store, a["task"]) or "").lower() == client.lower():
            return a
    return None


def for_invoice(store, conf: dict, inv: dict, holidays: set[date]) -> dict | None:
    """The allowance figures for a client's monthly timesheet, if one of its tasks has an allowance."""
    a = _allowance_for(store, conf, inv["client"])
    if not a:
        return None
    c = _client_conf(conf, inv["client"])
    block = int(c.get("block_minutes") or 0) * 60 if c.get("rounding", "none") != "none" else 0
    whole = (lambda v: v - v % block) if block else (lambda v: v)   # keep charged days in whole blocks
    per_day = float(a["hours_per_week"]) * 3600 / 5
    start, end = date.fromisoformat(inv["start"]), date.fromisoformat(inv["end"])
    rows = [r for g in inv["groups"] for d in g["days"] for r in d["entries"]]
    worked = _rounded_by_day([r for r in rows if not r["rolled"]], a["task"], c)
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
    days_n = working_days(start, end, holidays)
    info = {"task": a["task"], "month": inv["month"], "working_days": days_n,
            "max_s": whole(round(per_day * days_n)), "worked_s": sum(worked.values()),
            "rolling_s": inv.get("rolled_s", 0), "weeks": [weeks[k] for k in sorted(weeks)]}
    info["carry"] = carry(store, conf, inv, a, c, info)
    info["charging_s"] = info["worked_s"] + info["carry"]["added_s"]
    return info


def _month_before(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def carry(store, conf: dict, inv: dict, a: dict, c: dict, info: dict) -> dict:
    """The carried-over balance going into this month, and what's been added to this month's invoice.

    Rolled-over entries and the hours brought in at the start build the balance; the buttons on each timesheet
    add some of it to that month's invoice. Added hours fill the weeks with spare room under their max first.
    """
    from .reports import invoice
    style = c.get("billing") if c.get("billing") in ("day", "week") else "day"
    rolled = store.rolled()
    earliest = min([a["start"]] + [_month_before(r["entry_date"][:7]) for r in rolled.values()])
    added = store.carry_added(inv["client"])
    opening = round(float(a.get("opening_hours") or 0) * 3600)
    balance, m = 0, earliest
    while m < inv["month"]:
        balance += opening if m == a["start"] else 0
        balance -= min(added.get(m, 0), balance)
        y, mo = (int(x) for x in m.split("-"))
        balance += invoice(store, inv["client"], y, mo, style, c.get("block_minutes", 15),
                           c.get("rounding", "none"))["rolled_s"]
        m = next_month(m)
    available = balance + (opening if inv["month"] == a["start"] else 0)
    added_now = min(added.get(inv["month"], 0), available)
    by_week, left = [], added_now
    for w in info["weeks"]:   # spread what's added over the weeks with spare room, earliest first
        take = min(left, w["spare_s"])
        if take:
            by_week.append({"monday": w["monday"], "seconds": take})
            left -= take
    return {"available_s": available, "added_s": added_now, "remaining_s": available - added_now,
            "by_week": by_week, "unplaced_s": left, "spare_s": sum(w["spare_s"] for w in info["weeks"]),
            "rolling_s": info["rolling_s"]}


def apply(inv: dict, info: dict) -> dict:
    """Attach the allowance and add the carried-over hours chosen for this month to the invoice."""
    inv["allowance"] = info
    inv["carry"] = info["carry"]
    inv["total_billed_s"] += info["carry"]["added_s"]
    return inv


def summary(store, conf: dict, today: date) -> dict:
    """This month's position for every allowance, for the dashboard."""
    from .reports import invoice
    holidays, updated = bank_holidays()
    out = []
    for a in configured(conf):
        client = _task_client(store, a["task"])
        if not client:
            continue
        c = _client_conf(conf, client)
        style = c.get("billing") if c.get("billing") in ("day", "week") else "day"
        inv = invoice(store, client, today.year, today.month, style, c.get("block_minutes", 15), c.get("rounding", "none"))
        info = for_invoice(store, conf, inv, holidays or set())
        if info and info["task"] == a["task"]:
            info.pop("weeks")
            out.append(info)
    return {"allowances": out, "holidays_loaded": holidays is not None,
            "holidays_updated": date.fromtimestamp(updated).isoformat() if updated else None}


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
