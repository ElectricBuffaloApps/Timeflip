"""Local SQLite copy of TimeFlip tasks and intervals, plus a record of alerts already sent."""
from __future__ import annotations

import re
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
-- Time entries the user has chosen not to charge for. Kept across syncs.
CREATE TABLE IF NOT EXISTS excluded (interval_id INTEGER PRIMARY KEY, excluded_at TEXT NOT NULL);
-- The user's own corrections to a TimeFlip entry (new start, length or task). Kept across syncs.
CREATE TABLE IF NOT EXISTS adjustment (
    interval_id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,   -- ISO 8601, UTC
    duration_s INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    adjusted_at TEXT NOT NULL
);
-- Time the user added by hand (not tracked on the cube).
CREATE TABLE IF NOT EXISTS manual_entry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,   -- ISO 8601, UTC
    duration_s INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    added_at TEXT NOT NULL
);
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
    # TimeFlip sends offsets like "+01"; Python 3.9 needs "+01:00".
    m = re.search(r"([+-])(\d{2}):?(\d{2})?$", s) if "T" in s else None
    if m:
        s = f"{s[:m.start()]}{m.group(1)}{m.group(2)}:{m.group(3) or '00'}"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Entries added by hand get ids below this (negative), so they never clash with TimeFlip's ids.
MANUAL_ID_OFFSET = 10 ** 12


class Store:
    def __init__(self, path: Path, ignored_task_ids=()):
        # Tasks the user doesn't want counted (breaks, timers, "off"): left out of every figure.
        self.ignored_task_ids = {int(i) for i in ignored_task_ids}
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def replace_all(self, data: dict, duration_unit: str = "seconds") -> tuple[int, int]:
        """Replace the local copy with the server's full data set. Returns (tasks, intervals)."""
        divisor = 1000 if duration_unit == "milliseconds" else 1
        # Intervals point at their task by server id (taskId) or, from the phone app, by localId.
        task_rows, by_local = [], {}
        for n, t in enumerate(data.get("tasks") or []):
            tid = t.get("id") if t.get("id") is not None else -(n + 1)
            if t.get("localId"):
                by_local[t["localId"]] = tid
            task_rows.append((tid, t.get("name") or f"Task {tid}", (t.get("tag") or "").strip() or None,
                              int(bool(t.get("isBillable", t.get("billable")))), t.get("hourlyRate"),
                              t.get("currency")))
        task_ids = {r[0] for r in task_rows}
        rows, unmatched = [], 0
        for n, i in enumerate(data.get("timeIntervals") or []):
            if i.get("delDate") or not i.get("startedAt"):
                continue
            tid = i.get("taskId") if i.get("taskId") in task_ids else by_local.get(i.get("taskLocalId"))
            if tid is None:
                unmatched += 1
                continue
            rows.append((
                i.get("id") if i.get("id") is not None else -(n + 1),
                tid,
                parse_started_at(i["startedAt"]).isoformat(),
                int(i.get("duration") or 0) // divisor,
            ))
        with self.db:
            self.db.execute("DELETE FROM task")
            self.db.execute("DELETE FROM interval")
            self.db.executemany(
                "INSERT INTO task (id, name, tag, billable, hourly_rate, currency) VALUES (?,?,?,?,?,?)", task_rows
            )
            self.db.executemany(
                "INSERT OR REPLACE INTO interval (id, task_id, started_at, duration_s) VALUES (?,?,?,?)", rows
            )
            self.db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('unmatched_intervals', ?)", (str(unmatched),)
            )
            self.db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_sync', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
        return len(task_rows), len(rows)

    def tasks(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM task ORDER BY name"))

    def segments(self, start: datetime, end: datetime) -> list[dict]:
        """Entries overlapping [start, end), clipped to it, with their task details.

        Uses the user's adjustments in place of the TimeFlip values, and includes entries they added by hand.
        """
        # An entry can start before `start` and still overlap; look back a day to catch those.
        lookback = (start - timedelta(days=1)).astimezone(timezone.utc).isoformat()
        upper = end.astimezone(timezone.utc).isoformat()
        out = []
        for row in self.db.execute(
            f"""SELECT e.*, t.name, t.tag, t.billable, t.hourly_rate, t.currency, ot.name AS orig_task
               FROM (
                 SELECT i.id, COALESCE(a.started_at, i.started_at) AS started_at,
                        COALESCE(a.duration_s, i.duration_s) AS duration_s,
                        COALESCE(a.task_id, i.task_id) AS task_id,
                        i.started_at AS orig_start, i.duration_s AS orig_duration, i.task_id AS orig_task_id,
                        a.interval_id IS NOT NULL AS adjusted, 0 AS manual
                 FROM interval i LEFT JOIN adjustment a ON a.interval_id = i.id
                 UNION ALL
                 SELECT -({MANUAL_ID_OFFSET} + m.id), m.started_at, m.duration_s, m.task_id,
                        NULL, NULL, NULL, 0, 1
                 FROM manual_entry m
               ) e
               LEFT JOIN task t ON t.id = e.task_id
               LEFT JOIN task ot ON ot.id = e.orig_task_id
               WHERE e.started_at >= ? AND e.started_at < ? ORDER BY e.started_at""",
            (lookback, upper),
        ):
            s = datetime.fromisoformat(row["started_at"])
            e = s + timedelta(seconds=row["duration_s"])
            seg_start, seg_end = max(s, start), min(e, end)
            seconds = int((seg_end - seg_start).total_seconds())
            if seconds > 0 and row["task_id"] not in self.ignored_task_ids:
                orig = None
                if row["adjusted"]:
                    os_ = datetime.fromisoformat(row["orig_start"])
                    orig = {"start": os_, "end": os_ + timedelta(seconds=row["orig_duration"]),
                            "task": row["orig_task"] or f"Task {row['orig_task_id']}",
                            "task_changed": row["orig_task_id"] != row["task_id"]}
                out.append({
                    "id": row["id"], "task_id": row["task_id"], "task": row["name"] or f"Task {row['task_id']}",
                    "client": row["tag"], "billable": bool(row["billable"]),
                    "hourly_rate": row["hourly_rate"], "currency": row["currency"],
                    "start": seg_start, "end": seg_end, "seconds": seconds,
                    "entry_start": s, "entry_end": e,
                    "adjusted": bool(row["adjusted"]), "manual": bool(row["manual"]), "original": orig,
                })
        return out

    # ----- the user's corrections -----

    def set_adjustment(self, interval_id: int, start: datetime, duration_s: int, task_id: int) -> None:
        if interval_id <= -MANUAL_ID_OFFSET:  # an added entry: just change it
            with self.db:
                self.db.execute("UPDATE manual_entry SET started_at=?, duration_s=?, task_id=? WHERE id=?",
                                (start.astimezone(timezone.utc).isoformat(), duration_s, task_id,
                                 -interval_id - MANUAL_ID_OFFSET))
            return
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO adjustment VALUES (?,?,?,?,?)",
                (interval_id, start.astimezone(timezone.utc).isoformat(), duration_s, task_id,
                 datetime.now(timezone.utc).isoformat()),
            )

    def clear_adjustment(self, interval_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM adjustment WHERE interval_id=?", (interval_id,))

    def add_manual(self, start: datetime, duration_s: int, task_id: int) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO manual_entry (started_at, duration_s, task_id, added_at) VALUES (?,?,?,?)",
                (start.astimezone(timezone.utc).isoformat(), duration_s, task_id,
                 datetime.now(timezone.utc).isoformat()),
            )
        return -(MANUAL_ID_OFFSET + cur.lastrowid)

    def delete_manual(self, entry_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM manual_entry WHERE id=?", (-entry_id - MANUAL_ID_OFFSET,))
            self.db.execute("DELETE FROM excluded WHERE interval_id=?", (entry_id,))

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

    def excluded_ids(self) -> set[int]:
        return {r[0] for r in self.db.execute("SELECT interval_id FROM excluded")}

    def set_excluded(self, interval_id: int, excluded: bool) -> None:
        with self.db:
            if excluded:
                self.db.execute("INSERT OR IGNORE INTO excluded VALUES (?, ?)",
                                (interval_id, datetime.now(timezone.utc).isoformat()))
            else:
                self.db.execute("DELETE FROM excluded WHERE interval_id = ?", (interval_id,))

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def last_sync(self) -> str | None:
        return self.get_meta("last_sync")
