"""Monthly allowances: a task capped at so many hours a week, with hours over the cap carried forward.

Each month's pot is the weekly hours shared across its working days (weekdays that aren't England and Wales
bank holidays), so a week split across two months is shared by its weekdays. Each day's time on the task is
rounded the same way as the client's invoice, and the pot is rounded down to whole blocks. Hours brought forward are billed first, then the month's own
days in date order; whatever doesn't fit in the pot carries into the next month.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import date, timedelta

from . import config as cfg
from .reports import days_between, month_range, round_seconds, timesheet_rows

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


def run(store, allowance: dict, conf: dict, year: int, month: int, holidays: set[date]) -> list[dict]:
    """Every month from the allowance's start up to and including (year, month), oldest first."""
    task = allowance["task"]
    client = allowance.get("client") or _task_client(store, task)
    if not client:
        return []
    c = _client_conf(conf, client)
    style = c.get("billing") if c.get("billing") in ("day", "week") else "day"
    sy, sm = (int(x) for x in allowance["start"].split("-"))
    carry = round(float(allowance.get("opening_hours") or 0) * 3600)
    per_day = float(allowance["hours_per_week"]) * 3600 / 5
    months = []
    y, m = sy, sm
    while (y, m) <= (year, month):
        start, end = month_range(y, m, style)
        days_n = working_days(start, end, holidays)
        pot = round(per_day * days_n)
        if c.get("rounding", "none") != "none" and c.get("block_minutes"):
            block = int(c["block_minutes"]) * 60
            pot -= pot % block   # whole blocks, so every billed day stays a multiple of the block
        worked: dict[str, int] = {}
        for r in timesheet_rows(store, start, end, client):
            if not r["excluded"] and r["task"].lower() == task.lower():
                worked[r["date"]] = worked.get(r["date"], 0) + r["seconds"]
        worked = {d: round_seconds(s, c.get("block_minutes", 15), c.get("rounding", "none")) for d, s in worked.items()}
        brought = carry
        used_brought = min(brought, pot)
        room = pot - used_brought
        days = {}
        for d in sorted(worked):
            billed = min(room, worked[d])
            room -= billed
            days[d] = {"worked_s": worked[d], "billed_s": billed, "carried_s": worked[d] - billed}
        worked_s = sum(worked.values())
        carry = brought - used_brought + sum(v["carried_s"] for v in days.values())
        months.append({
            "month": f"{y}-{m:02d}", "task": task, "client": client, "working_days": days_n,
            "pot_s": pot, "brought_s": brought, "brought_used_s": used_brought, "worked_s": worked_s,
            "billed_s": used_brought + worked_s - sum(v["carried_s"] for v in days.values()),
            "carried_s": carry, "days": days,
        })
        y, m = _next_month(y, m)
    return months


def for_month(store, conf: dict, client: str, year: int, month: int, holidays: set[date]) -> dict | None:
    """The allowance month for a client's invoice, if one of its tasks has an allowance."""
    for a in conf.get("allowances") or []:
        months = run(store, a, conf, year, month, holidays)
        if months and months[-1]["client"].lower() == client.lower():
            return months[-1]
    return None


def apply(inv: dict, month: dict) -> dict:
    """Take the carried hours off the invoice's days and add the hours brought forward."""
    for g in inv["groups"]:
        for d in g["days"]:
            day = month["days"].get(d["date"])
            d["carried_s"] = day["carried_s"] if day else 0
            d["billed_s"] -= d["carried_s"]
        g["billed_s"] = sum(d["billed_s"] for d in g["days"])
    inv["allowance"] = month
    inv["total_billed_s"] = sum(g["billed_s"] for g in inv["groups"]) + month["brought_used_s"]
    return inv


def summary(store, conf: dict, today: date) -> dict:
    """This month's position for every allowance, for the dashboard and Settings."""
    holidays, updated = bank_holidays()
    out = []
    for a in conf.get("allowances") or []:
        months = run(store, a, conf, today.year, today.month, holidays or set())
        if months:
            m = dict(months[-1])
            m.pop("days")
            m["hours_per_week"] = a["hours_per_week"]
            out.append(m)
    return {"allowances": out, "holidays_loaded": holidays is not None,
            "holidays_updated": date.fromtimestamp(updated).isoformat() if updated else None}


def previous_month_name(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{(date(y, m, 1) - timedelta(days=1)):%B %Y}"


def next_month_name(month: str) -> str:
    y, m = _next_month(*(int(x) for x in month.split("-")))
    return f"{date(y, m, 1):%B %Y}"
