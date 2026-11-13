"""Areas: the clients shown as cards at the top of the Dashboard, each with its own page.

An area is a client (a TimeFlip tag) with a display name. Its page charts the hours by day, week or month and keeps
the area's hours in lieu: a bank of hours typed in by hand, which can be added to a day or a week as real time.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .limits import WEEKDAYS
from .reports import NO_CLIENT, timesheet_rows

DAYS, WEEKS, MONTHS = 28, 12, 12
MAX_AREAS = 8
# Friendlier names for some tags, used until the user names their own cards.
DEFAULT_LABELS = {"fypt1": "FYPT Clients", "fyptd": "FYPT Deep"}


def week_start(day: date, conf: dict) -> date:
    return day - timedelta(days=(day.weekday() - WEEKDAYS.index(conf.get("week_starts", "monday"))) % 7)


def _month_back(day: date, n: int) -> date:
    y, m = day.year, day.month - n
    while m < 1:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def configured(conf: dict, store) -> list[dict]:
    """The areas to show: the user's own list, else up to four clients (the busiest lately first)."""
    saved = conf.get("areas")
    if saved is not None:
        return [dict(a) for a in saved]
    today = date.today()
    busy: dict[str, int] = {}
    for r in timesheet_rows(store, today - timedelta(days=60), today):
        if r["client"] != NO_CLIENT and not r["excluded"]:
            busy[r["client"]] = busy.get(r["client"], 0) + r["seconds"]
    names = sorted({t["tag"] for t in store.tasks() if t["tag"]}, key=lambda c: (-busy.get(c, 0), c.lower()))
    return [{"client": c, "label": DEFAULT_LABELS.get(c.lower(), c)} for c in names[:4]]


def clean(raw: list, known: list[str]) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("Choose which clients to show as cards.")
    out, seen = [], set()
    for a in raw[:MAX_AREAS]:
        client = str((a or {}).get("client") or "").strip()
        match = next((k for k in known if k.lower() == client.lower()), None)
        if not match or match.lower() in seen:
            continue
        seen.add(match.lower())
        out.append({"client": match, "label": str(a.get("label") or "").strip()[:40] or match})
    return out


def set_allotted(conf: dict, raw: list, known: list[str]) -> None:
    """Save the hours a week typed beside each card. They're the clients' weekly time targets, so Targets shows them too."""
    targets = conf.setdefault("targets", {})
    hours = targets.setdefault("hours", {})
    for a in raw if isinstance(raw, list) else []:
        if "week_hours" not in (a or {}):
            continue
        match = next((k for k in known if k.lower() == str(a.get("client") or "").lower()), None)
        if not match:
            continue
        value = a["week_hours"]
        try:
            value = None if value in (None, "") else float(value)
        except (TypeError, ValueError):
            raise ValueError("Hours a week must be a number.")
        if value is not None and value < 0:
            raise ValueError("Hours a week can't be negative.")
        key = next((k for k in hours if k.lower() == match.lower()), match)
        entry = dict(hours.get(key) or {})
        entry["week"] = value or None
        if any(entry.get(p) for p in ("day", "week", "month")):
            hours[key] = {p: entry.get(p) for p in ("day", "week", "month")}
        else:
            hours.pop(key, None)


def allotted_hours(conf: dict, store, client: str) -> float | None:
    """The hours a week meant for this client: its weekly time target, else its weekly limit, else the weekly hours
    of its allowance tasks."""
    for name, t in ((conf.get("targets") or {}).get("hours") or {}).items():
        if name.lower() == client.lower() and (t or {}).get("week"):
            return float(t["week"])
    limits = conf.get("limits") or []
    for l in limits:
        if (l.get("client") or "").lower() == client.lower() and l.get("weekly_hours"):
            return float(l["weekly_hours"])
    mine = {t["name"].lower() for t in store.tasks() if (t["tag"] or "").lower() == client.lower()}
    total = sum(float(l["weekly_hours"]) for l in limits
                if l.get("allowance") and l.get("weekly_hours") and (l.get("task") or "").lower() in mine)
    return total or None


def week_progress(target_hours: float | None, seconds: int, ws: date, today: date, holidays: set) -> dict | None:
    """Where this week stands against the allotted hours. "Behind" means under the pace of the working days already
    finished (today isn't counted until it's over), so it's a prompt to prioritise, not a telling-off at 9am."""
    if not target_hours:
        return None
    target = round(target_hours * 3600)
    days = [ws + timedelta(days=n) for n in range(7)]
    working = [d for d in days if d.weekday() < 5 and d not in holidays] or days
    done = len([d for d in working if d < today])
    left = len([d for d in working if d >= today])
    to_go = max(0, target - seconds)
    expected = target * done / len(working)
    status = "done" if seconds >= target else "behind" if seconds < expected else "on_track"
    return {"target_seconds": target, "to_go_seconds": to_go, "status": status,
            "behind_seconds": max(0, round(expected - seconds)) if status == "behind" else 0,
            "days_left": left, "per_day_seconds": round(to_go / left) if left and to_go else 0}


def _seconds(store, client: str, start: date, end: date) -> int:
    return sum(r["seconds"] for r in timesheet_rows(store, start, end, client) if not r["excluded"])


def cards(store, conf: dict, today: date, holidays: set = frozenset()) -> list[dict]:
    ws = week_start(today, conf)
    out = []
    for a in configured(conf, store):
        week = _seconds(store, a["client"], ws, today)
        out.append({**a, "week_seconds": week,
                    "month_seconds": _seconds(store, a["client"], today.replace(day=1), today),
                    "lieu_seconds": store.lieu_balance(a["client"]),
                    "allotted_hours": allotted_hours(conf, store, a["client"]),
                    "progress": week_progress(allotted_hours(conf, store, a["client"]), week, ws, today, holidays)})
    return out


def detail(store, conf: dict, client: str, today: date, carried_s: int = 0, holidays: set = frozenset()) -> dict:
    """Everything for one area's page."""
    known = sorted({t["tag"] for t in store.tasks() if t["tag"]}, key=str.lower)
    match = next((k for k in known if k.lower() == client.lower()), None)
    if not match:
        raise ValueError("There's no client with that name.")
    label = next((a["label"] for a in configured(conf, store) if a["client"].lower() == match.lower()), match)
    ws = week_start(today, conf)
    first = min(_month_back(today, MONTHS - 1), ws - timedelta(weeks=WEEKS - 1), today - timedelta(days=DAYS - 1))
    rows = [r for r in timesheet_rows(store, first, today, match) if not r["excluded"]]

    def bucket(key_of, keys, label_of, name_of):
        index = {k: {"key": k.isoformat(), "label": label_of(k), "name": name_of(k), "seconds": 0, "tasks": {}} for k in keys}
        for r in rows:
            b = index.get(key_of(date.fromisoformat(r["date"])))
            if b:
                b["seconds"] += r["seconds"]
                b["tasks"][r["task"]] = b["tasks"].get(r["task"], 0) + r["seconds"]
        return list(index.values())

    days = [today - timedelta(days=n) for n in range(DAYS - 1, -1, -1)]
    weeks = [ws - timedelta(weeks=n) for n in range(WEEKS - 1, -1, -1)]
    months = [_month_back(today, n) for n in range(MONTHS - 1, -1, -1)]
    totals: dict[str, int] = {}
    for r in rows:
        totals[r["task"]] = totals.get(r["task"], 0) + r["seconds"]
    tasks = [{"id": t["id"], "name": t["name"]} for t in store.tasks() if (t["tag"] or "").lower() == match.lower()]
    tasks.sort(key=lambda t: (-totals.get(t["name"], 0), t["name"].lower()))
    rate = next((c.get("rate") for n, c in (conf.get("clients") or {}).items() if n.lower() == match.lower()), None)
    bank = store.lieu_balance(match)
    return {
        "client": match, "label": label, "rate": rate,
        "week_seconds": sum(r["seconds"] for r in rows if date.fromisoformat(r["date"]) >= ws),
        "month_seconds": sum(r["seconds"] for r in rows if r["date"][:7] == f"{today:%Y-%m}"),
        "week_starts": ws.isoformat(), "today": today.isoformat(),
        "allotted_hours": allotted_hours(conf, store, match),
        "progress": week_progress(allotted_hours(conf, store, match),
                                  sum(r["seconds"] for r in rows if date.fromisoformat(r["date"]) >= ws), ws, today, holidays),
        "weeks": [{"start": (ws + timedelta(weeks=n)).isoformat(),
                   "label": ("This week" if n == 0 else "Last week" if n == -1 else "Next week" if n == 1 else "Week")
                            + f" ({ws + timedelta(weeks=n):%d/%m} to {ws + timedelta(weeks=n, days=6):%d/%m})"}
                  for n in range(1, -9, -1)],
        "series": {
            "day": bucket(lambda d: d, days, lambda d: str(d.day), lambda d: f"{d:%A %d/%m/%Y}"),
            "week": bucket(lambda d: week_start(d, conf), weeks, lambda d: f"{d.day} {d:%b}",
                           lambda d: f"Week of {d:%d/%m/%Y}"),
            "month": bucket(lambda d: d.replace(day=1), months, lambda d: f"{d:%b}", lambda d: f"{d:%B %Y}"),
        },
        "task_order": [t["name"] for t in tasks], "tasks": tasks,
        "lieu": {"bank_seconds": bank, "carried_seconds": carried_s, "total_seconds": bank + carried_s,
                 "log": store.lieu_log(match)},
    }


def use_lieu(store, conf: dict, client: str, task_id: int, mode: str, when: date, seconds: int) -> None:
    """Take hours out of the bank and add them as time on a day (or on the first day of a week)."""
    if seconds <= 0:
        raise ValueError("Enter how many hours to add.")
    if seconds > store.lieu_balance(client):
        raise ValueError("That's more than the hours in lieu you have banked.")
    task = next((t for t in store.tasks() if t["id"] == task_id and (t["tag"] or "").lower() == client.lower()), None)
    if not task:
        raise ValueError("Choose a task from this area.")
    day = week_start(when, conf) if mode == "week" else when
    start = datetime(day.year, day.month, day.day, 9).astimezone()
    entry_id = store.add_manual(start, seconds, task_id)
    store.add_lieu(client, -seconds, f"Added to {'week of ' if mode == 'week' else ''}{day:%d/%m/%Y} ({task['name']})", entry_id)
