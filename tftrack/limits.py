"""Daily and weekly limits per task or per client (the TimeFlip task tag)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@dataclass
class Limit:
    label: str
    period: str          # "daily" or "weekly"
    hours: float
    task: str | None = None
    client: str | None = None

    @property
    def key(self) -> str:
        target = f"task:{self.task}" if self.task else f"client:{self.client}"
        return f"{target}|{self.period}|{self.hours}"


@dataclass
class LimitStatus:
    limit: Limit
    period_start: datetime
    seconds: int

    @property
    def percent(self) -> float:
        return 100.0 * self.seconds / (self.limit.hours * 3600) if self.limit.hours else 0.0


def parse_limits(config: dict) -> list[Limit]:
    out = []
    for n, raw in enumerate(config.get("limits") or [], 1):
        task, client = raw.get("task"), raw.get("client")
        if bool(task) == bool(client):
            raise ValueError(f"Limit #{n}: set exactly one of 'task' or 'client'.")
        for period in ("daily", "weekly"):
            hours = raw.get(f"{period}_hours")
            if hours is not None:
                out.append(Limit(label=raw.get("label") or task or client, period=period,
                                 hours=float(hours), task=task, client=client))
        if raw.get("daily_hours") is None and raw.get("weekly_hours") is None:
            raise ValueError(f"Limit #{n}: set 'daily_hours' and/or 'weekly_hours'.")
    return out


def period_window(period: str, now: datetime, week_starts: str = "monday") -> tuple[datetime, datetime]:
    """Window in the Mac's local time containing `now`, as aware datetimes.

    Arithmetic is done on naive wall-clock times, then localised, so a DST change inside the
    window still gives local midnight at both ends.
    """
    wall = now.astimezone().replace(tzinfo=None)
    day_start = wall.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "daily":
        start = day_start
        end = start + timedelta(days=1)
    else:
        offset = (wall.weekday() - WEEKDAYS.index(week_starts.lower())) % 7
        start = day_start - timedelta(days=offset)
        end = start + timedelta(days=7)
    return start.astimezone(), end.astimezone()


def evaluate(limits: list[Limit], tasks, seconds_for_window, now: datetime,
             week_starts: str = "monday") -> list[LimitStatus]:
    """`tasks` are rows with id/name/tag; `seconds_for_window(start, end)` returns {task_id: seconds}."""
    by_name: dict[str, list[int]] = {}
    by_client: dict[str, list[int]] = {}
    for t in tasks:
        by_name.setdefault(t["name"].strip().lower(), []).append(t["id"])
        if t["tag"]:
            by_client.setdefault(t["tag"].strip().lower(), []).append(t["id"])

    cache: dict[tuple, dict[int, int]] = {}
    results = []
    for lim in limits:
        start, end = period_window(lim.period, now, week_starts)
        if (start, end) not in cache:
            cache[(start, end)] = seconds_for_window(start, end)
        totals = cache[(start, end)]
        ids = by_name.get(lim.task.strip().lower(), []) if lim.task else by_client.get(lim.client.strip().lower(), [])
        results.append(LimitStatus(lim, start, sum(totals.get(i, 0) for i in ids)))
    return results


def alerts_due(statuses: list[LimitStatus], levels: list[int], already_sent) -> list[tuple[LimitStatus, int]]:
    """Highest newly crossed level per limit; `already_sent(key, period_start_iso, level)` -> bool."""
    due = []
    for st in statuses:
        crossed = [lv for lv in sorted(levels) if st.percent >= lv]
        if crossed and not already_sent(st.limit.key, st.period_start.isoformat(), crossed[-1]):
            due.append((st, crossed[-1]))
    return due


def format_hm(seconds: int) -> str:
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h {m:02d}m"
