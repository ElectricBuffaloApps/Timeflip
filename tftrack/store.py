"""Local SQLite copy of TimeFlip tasks and intervals, plus a record of alerts already sent."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS task (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    tag TEXT,
    billable INTEGER,
    hourly_rate REAL,
    currency TEXT
);
CREATE TABLE IF NOT EXISTS interval (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,   -- ISO 8601, UTC
    duration_s INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS interval_started ON interval(started_at);
CREATE TABLE IF NOT EXISTS alert_sent (
    limit_key TEXT NOT NULL,
    period_start TEXT NOT NULL,
    level INTEGER NOT NULL,     -- percentage threshold that fired
    sent_at TEXT NOT NULL,
    PRIMARY KEY (limit_key, period_start, level)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def parse_started_at(value) -> datetime:
    """Parse the API's startedAt into an aware UTC datetime.

    Accepts ISO 8601 (with or without offset/'Z') or epoch seconds/milliseconds.
    Naive ISO values are treated as UTC.
    """
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().lstrip("-").isdigit()):
        n = float(value)
        if n > 1e11:  # milliseconds
            n /= 1000
        return datetime.fromtimestamp(n, tz=timezone.utc)
    s = str(value).strip().replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Python 3.9's fromisoformat only accepts 3 or 6 fractional digits.
    if "." in s:
        head, _, rest = s.partition(".")
        frac = ""
        while rest and rest[0].isdigit():
            frac, rest = frac + rest[0], rest[1:]
        s = f"{head}.{(frac + '000000')[:6]}{rest}"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def replace_all(self, data: dict, duration_unit: str = "seconds") -> tuple[int, int]:
        """Replace the local copy with the server's full data set. Returns (tasks, intervals)."""
        divisor = 1000 if duration_unit == "milliseconds" else 1
        tasks = [t for t in data.get("tasks") or [] if t.get("id") is not None]
        intervals = [
            i for i in data.get("timeIntervals") or []
            if not i.get("delDate") and i.get("taskId") is not None and i.get("startedAt")
        ]
        with self.db:
            self.db.execute("DELETE FROM task")
            self.db.execute("DELETE FROM interval")
            self.db.executemany(
                "INSERT INTO task (id, name, tag, billable, hourly_rate, currency) VALUES (?,?,?,?,?,?)",
                [(t["id"], t.get("name") or f"Task {t['id']}", (t.get("tag") or None),
                  int(bool(t.get("isBillable", t.get("billable")))), t.get("hourlyRate"), t.get("currency"))
                 for t in tasks],
            )
            rows = []
            for n, i in enumerate(intervals):
                rows.append((
                    i.get("id") if i.get("id") is not None else -(n + 1),
                    i["taskId"],
                    parse_started_at(i["startedAt"]).isoformat(),
                    int(i.get("duration") or 0) // divisor,
                ))
            self.db.executemany(
                "INSERT OR REPLACE INTO interval (id, task_id, started_at, duration_s) VALUES (?,?,?,?)", rows
            )
            self.db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_sync', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
        return len(tasks), len(rows)

    def tasks(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM task ORDER BY name"))

    def segments(self, start: datetime, end: datetime) -> list[dict]:
        """Intervals overlapping [start, end), clipped to it, with their task details."""
        # An interval can start before `start` and still overlap; look back a day to catch those.
        lookback = (start - timedelta(days=1)).astimezone(timezone.utc).isoformat()
        upper = end.astimezone(timezone.utc).isoformat()
        out = []
        for row in self.db.execute(
            """SELECT i.id, i.task_id, i.started_at, i.duration_s, t.name, t.tag, t.billable,
                      t.hourly_rate, t.currency
               FROM interval i LEFT JOIN task t ON t.id = i.task_id
               WHERE i.started_at >= ? AND i.started_at < ? ORDER BY i.started_at""",
            (lookback, upper),
        ):
            s = datetime.fromisoformat(row["started_at"])
            e = s + timedelta(seconds=row["duration_s"])
            seg_start, seg_end = max(s, start), min(e, end)
            seconds = int((seg_end - seg_start).total_seconds())
            if seconds > 0:
                out.append({
                    "task_id": row["task_id"], "task": row["name"] or f"Task {row['task_id']}",
                    "client": row["tag"], "billable": bool(row["billable"]),
                    "hourly_rate": row["hourly_rate"], "currency": row["currency"],
                    "start": seg_start, "end": seg_end, "seconds": seconds,
                })
        return out

    def seconds_by_task(self, start: datetime, end: datetime) -> dict[int, int]:
        """Seconds tracked per task id within [start, end), clipping intervals at the edges."""
        totals: dict[int, int] = {}
        for seg in self.segments(start, end):
            totals[seg["task_id"]] = totals.get(seg["task_id"], 0) + seg["seconds"]
        return totals

    def alert_already_sent(self, limit_key: str, period_start: str, level: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM alert_sent WHERE limit_key=? AND period_start=? AND level=?",
            (limit_key, period_start, level),
        ).fetchone() is not None

    def mark_alert_sent(self, limit_key: str, period_start: str, level: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO alert_sent VALUES (?,?,?,?)",
                (limit_key, period_start, level, datetime.now(timezone.utc).isoformat()),
            )

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def last_sync(self) -> str | None:
        return self.get_meta("last_sync")
