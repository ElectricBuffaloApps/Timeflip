"""Areas: the clients shown as cards at the top of the Dashboard, each with its own page.

An area is a client (a TimeFlip tag) with a display name. Its page charts the hours by day, week or month and keeps
the area's hours in lieu: a bank of hours which can be added to a day or a week as real time.

The bank fills by itself. Every week you work more than the area's hours a week, the extra goes in. For a client
invoiced by the day or by the week, those extra hours are also held back from the invoice (from the last days of
that week), so they're charged once: when you add them to a later day or week. A client with a monthly allowance
keeps Roll over and its carried hours as well: an entry rolled over goes there, anything else over the week goes
in the bank. Clients on the plain hourly timesheet can only be banked by hand, as that sheet can't hold hours back.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .limits import WEEKDAYS
from .reports import NO_CLIENT, round_seconds, timesheet_rows

DAYS, WEEKS, MONTHS = 28, 12, 12
MAX_AREAS = 8
# Friendlier names for some tags, used until the user names their own cards.
DEFAULT_LABELS = {"fypt1": "FYPT Clients", "fyptd": "FYPT Deep"}
DEFAULT_BANK_START = "2026-09-01"


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
        area = {"client": match, "label": str(a.get("label") or "").strip()[:40] or match}
        if "bank_auto" in a:
            area["bank_auto"] = bool(a["bank_auto"])
        if a.get("bank_start"):
            try:
                area["bank_start"] = date.fromisoformat(str(a["bank_start"])).isoformat()
            except ValueError:
                raise ValueError("Choose a real date for banking to start from.")
        out.append(area)
    return out


# ---------- the bank: hours over the weekly hours ----------

def _client_conf(conf: dict, client: str) -> dict | None:
    return next((c for n, c in (conf.get("clients") or {}).items() if n.lower() == client.lower()), None)


def _has_allowance(conf: dict, store, client: str) -> bool:
    mine = {t["name"].lower() for t in store.tasks() if (t["tag"] or "").lower() == client.lower()}
    return any(l.get("allowance") and ((l.get("client") or "").lower() == client.lower() or (l.get("task") or "").lower() in mine)
               for l in conf.get("limits") or [])


def bank_rules(conf: dict, store, client: str) -> dict:
    """Whether this client's bank fills by itself, from when, and whether the hours are held back from invoices."""
    area = next((a for a in conf.get("areas") or [] if a["client"].lower() == client.lower()), None)
    c = _client_conf(conf, client)
    limit = allotted_hours(conf, store, client)
    unbilled = not c or c.get("notional") or c.get("billing") == "monthly" or not c.get("rate")
    holds = not unbilled and c.get("billing") in ("day", "week")
    why = None
    if area is None:
        why = "Give this client a card first"
    elif not limit:
        why = "Set hours a week first"
    elif not (holds or unbilled):
        why = "Only for clients invoiced by the day or the week (bank by hand instead)"
    try:
        start = date.fromisoformat((area or {}).get("bank_start") or DEFAULT_BANK_START)
    except ValueError:
        start = date.fromisoformat(DEFAULT_BANK_START)
    return {"eligible": why is None, "why": why, "auto": why is None and area.get("bank_auto", True) is not False,
            "start": start, "limit_seconds": round((limit or 0) * 3600), "holds_back": holds, "monday_weeks": holds}


def invoiced_since_start(store, conf: dict, client: str) -> list[str]:
    """For a client invoiced by the week whose bank holds hours back: the months already marked as invoiced that the
    bank counts weeks from. Banking those weeks is only right if their invoices charged no more than the hours a
    week, so the page says which months it is assuming that for."""
    rules = bank_rules(conf, store, client)
    if not (rules["auto"] and rules["holds_back"]):
        return []
    first = f"{rules['start']:%Y-%m}"
    return sorted(m for m in store.invoiced(client) if m >= first)


def settle_starts(conf: dict, store) -> bool:
    """Give every area a fixed date its bank starts from, the first time it's needed. Fixed, because once hours have
    been held back from an invoice the start must not drift. Returns True if the settings changed."""
    areas_now = configured(conf, store)
    changed = conf.get("areas") is None
    for a in areas_now:
        if not a.get("bank_start"):
            a["bank_start"] = DEFAULT_BANK_START
            changed = True
    if changed:
        conf["areas"] = areas_now
    return changed


def _billed_weeks(store, conf: dict, client: str, today: date, rules: dict) -> tuple[dict[str, int], list[dict]]:
    """For a client whose invoices hold the hours back: (day -> seconds held back, the weeks that went over).

    Works on what the invoice would charge: each day's time rounded the client's way, Monday to Sunday. The week's
    hours a week are used up in date order, so what's held back comes off the last days worked. Hours that came out
    of the bank are charged in full and never count towards the week. A month already invoiced keeps what it held
    back then.
    """
    c = _client_conf(conf, client) or {}
    block, mode = c.get("block_minutes", 15), c.get("rounding", "none")
    from_bank, rolled = store.lieu_entry_ids(), store.rolled()
    first = rules["start"] + timedelta(days=(7 - rules["start"].weekday()) % 7)   # the first Monday on or after the start
    days: dict[str, list[int]] = {}
    for r in timesheet_rows(store, first, today, client):
        if r["excluded"] or r["id"] in rolled:
            continue
        d = days.setdefault(r["date"], [0, 0])
        d[0] += r["seconds"]
        d[1] += r["seconds"] if r["id"] in from_bank else 0
    weeks: dict[date, list[tuple[str, int]]] = {}
    for iso in sorted(days):
        total = round_seconds(days[iso][0], block, mode)
        d = date.fromisoformat(iso)
        weeks.setdefault(d - timedelta(days=d.weekday()), []).append((iso, total - min(total, days[iso][1])))
    fixed_days, fixed_weeks = store.banked_days(client), store.banked_weeks(client)
    held, out = {}, []
    for ws, items in sorted(weeks.items()):
        worked = sum(secs for _, secs in items)
        budget = rules["limit_seconds"]
        if ws.isoformat() in fixed_weeks and not any(iso in fixed_days for iso, _ in items):
            budget = max(0, worked - fixed_weeks[ws.isoformat()])   # a week fixed as a whole (how version 36 kept it)
        used = over = 0
        for iso, secs in items:
            h = min(fixed_days[iso], secs) if iso in fixed_days else max(0, secs - max(0, budget - used))
            used += secs - h
            if h:
                held[iso] = h
                over += h
        if over:
            out.append({"start": ws.isoformat(), "worked_seconds": worked, "over_seconds": over})
    return held, out


def over_weeks(store, conf: dict, client: str, today: date) -> list[dict]:
    """Each week since the bank's start with more worked than the hours a week: [{start, worked_seconds, over_seconds}].

    Worked means tracked or typed in, and charged: not excluded, not rolled over, and not hours that came out of the
    bank. Weeks run Monday to Sunday for a client whose invoices hold the hours back (to match its timesheet), else
    from the day the week starts in Settings.
    """
    rules = bank_rules(conf, store, client)
    if not rules["auto"]:
        return []
    if rules["holds_back"]:
        return _billed_weeks(store, conf, client, today, rules)[1]
    skip, rolled = store.lieu_entry_ids(), store.rolled()
    weeks: dict[date, int] = {}
    for r in timesheet_rows(store, rules["start"], today, client):
        if r["excluded"] or r["id"] in skip or r["id"] in rolled:
            continue
        ws = week_start(date.fromisoformat(r["date"]), conf)
        if ws >= rules["start"]:
            weeks[ws] = weeks.get(ws, 0) + r["seconds"]
    return [{"start": ws.isoformat(), "worked_seconds": secs, "over_seconds": secs - rules["limit_seconds"]}
            for ws, secs in sorted(weeks.items()) if secs > rules["limit_seconds"]]


def bank(store, conf: dict, client: str, today: date) -> dict:
    """The area's hours in lieu: what the weeks over the limit put in, plus changes made by hand, less what's been used."""
    weeks = over_weeks(store, conf, client, today)
    auto = sum(w["over_seconds"] for w in weeks)
    by_hand = store.lieu_balance(client)
    return {"auto_seconds": auto, "hand_seconds": by_hand, "seconds": auto + by_hand, "weeks": weeks}


def held_back(store, conf: dict, client: str, today: date) -> dict[str, int]:
    """Day -> seconds to leave off that day's charge, because they're in the bank. Empty unless this client's
    invoices hold banked hours back."""
    rules = bank_rules(conf, store, client)
    if not (rules["auto"] and rules["holds_back"]):
        return {}
    return _billed_weeks(store, conf, client, today, rules)[0]


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
                    "lieu_seconds": bank(store, conf, a["client"], today)["seconds"],
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
    banked = bank(store, conf, match, today)
    rules = bank_rules(conf, store, match)
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
        "lieu": {"bank_seconds": banked["seconds"], "carried_seconds": carried_s,
                 "total_seconds": banked["seconds"] + carried_s, "auto_seconds": banked["auto_seconds"],
                 "hand_seconds": banked["hand_seconds"], "weeks": banked["weeks"][::-1][:12], "auto": rules["auto"],
                 "why": rules["why"], "start": rules["start"].isoformat(), "holds_back": rules["auto"] and rules["holds_back"],
                 "invoiced_months": invoiced_since_start(store, conf, match),
                 "log": store.lieu_log(match)},
    }


def use_lieu(store, conf: dict, client: str, task_id: int, mode: str, when: date, seconds: int) -> None:
    """Take hours out of the bank and add them as time on a day (or on the first day of a week)."""
    if seconds <= 0:
        raise ValueError("Enter how many hours to add.")
    if seconds > bank(store, conf, client, date.today())["seconds"]:
        raise ValueError("That's more than the hours in lieu you have banked.")
    task = next((t for t in store.tasks() if t["id"] == task_id and (t["tag"] or "").lower() == client.lower()), None)
    if not task:
        raise ValueError("Choose a task from this area.")
    day = week_start(when, conf) if mode == "week" else when
    start = datetime(day.year, day.month, day.day, 9).astimezone()
    entry_id = store.add_manual(start, seconds, task_id)
    store.add_lieu(client, -seconds, f"Added to {'week of ' if mode == 'week' else ''}{day:%d/%m/%Y} ({task['name']})", entry_id)
