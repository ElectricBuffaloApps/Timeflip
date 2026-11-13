"""Local SQLite copy of TimeFlip tasks and intervals, plus a record of alerts already sent."""
from __future__ import annotations

import json
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
-- Entries the user has rolled over: charged in a later month (YYYY-MM) instead of their own. Kept across syncs.
CREATE TABLE IF NOT EXISTS rollover (
    interval_id INTEGER PRIMARY KEY,
    month TEXT NOT NULL,        -- the month it's now charged in
    entry_date TEXT NOT NULL    -- the entry's own (local) date, to find it again
);
-- Carried-over hours added to a month's invoice (by the user, with the buttons on its timesheet).
CREATE TABLE IF NOT EXISTS carry_added (
    client TEXT NOT NULL,
    month TEXT NOT NULL,        -- YYYY-MM
    seconds INTEGER NOT NULL,
    task TEXT NOT NULL DEFAULT '',   -- the allowance's task; '' for a whole-client allowance (or an older entry)
    PRIMARY KEY (client, month, task)
);
-- Months the user has marked as invoiced, with what the invoice came to at the time.
CREATE TABLE IF NOT EXISTS invoiced (
    client TEXT NOT NULL,
    month TEXT NOT NULL,        -- YYYY-MM
    marked_at TEXT NOT NULL,
    seconds INTEGER NOT NULL,
    amount REAL,
    PRIMARY KEY (client, month)
);
-- Invoiced months the client has paid.
CREATE TABLE IF NOT EXISTS paid (
    client TEXT NOT NULL,
    month TEXT NOT NULL,        -- YYYY-MM
    paid_at TEXT NOT NULL,
    PRIMARY KEY (client, month)
);
-- Invoices made in the app. A fixed copy: `data` holds the lines and the business and client details as they
-- were on the day (JSON). Voided invoices stay, so numbers are never reused.
CREATE TABLE IF NOT EXISTS bill (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id TEXT NOT NULL,  -- which of the user's businesses sent it; each numbers its invoices separately
    number TEXT NOT NULL,
    seq INTEGER NOT NULL,
    client TEXT NOT NULL,
    month TEXT NOT NULL,        -- YYYY-MM
    issued_on TEXT NOT NULL,    -- YYYY-MM-DD
    due_on TEXT NOT NULL,
    seconds INTEGER NOT NULL,
    net REAL NOT NULL,
    vat REAL NOT NULL,
    total REAL NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    voided_at TEXT,
    UNIQUE (business_id, number)
);
-- Extras to add to a client's invoice for a month: time not on the cube, mileage, expenses. Once invoiced, bill_id
-- says which invoice took them; voiding that invoice frees them again.
CREATE TABLE IF NOT EXISTS extra (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    month TEXT NOT NULL,        -- YYYY-MM
    kind TEXT NOT NULL,         -- time, mileage, expense
    description TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit_price REAL NOT NULL,
    created_at TEXT NOT NULL,
    bill_id INTEGER
);
-- Hours in lieu: a bank of hours per client, typed in by hand. Positive rows put hours in; negative rows take them
-- out. A row made by adding banked hours to a day points at the entry it created (entry_id), and stops counting if
-- that entry is later deleted, so the hours go back in the bank.
CREATE TABLE IF NOT EXISTS lieu (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    seconds INTEGER NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    entry_id INTEGER
);
-- "Money to collect" prompts the user has hidden. The key changes when the figures behind a prompt change.
CREATE TABLE IF NOT EXISTS dismissed (key TEXT PRIMARY KEY, dismissed_at TEXT NOT NULL);
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
        # Version 30 kept one business's invoices, keyed by number. Move them to the first business ("b1").
        cols = [r["name"] for r in self.db.execute("PRAGMA table_info(bill)")]
        old = bool(cols) and "business_id" not in cols
        if old:
            self.db.execute("ALTER TABLE bill RENAME TO bill_v30")
        self.db.executescript(SCHEMA)
        if "task" not in [r[1] for r in self.db.execute("PRAGMA table_info(carry_added)")]:
            # Older versions kept one lot of carried hours per client and month; now it's one per allowance.
            with self.db:
                self.db.execute("ALTER TABLE carry_added RENAME TO carry_added_old")
                self.db.execute("CREATE TABLE carry_added (client TEXT NOT NULL, month TEXT NOT NULL, "
                                "seconds INTEGER NOT NULL, task TEXT NOT NULL DEFAULT '', "
                                "PRIMARY KEY (client, month, task))")
                self.db.execute("INSERT INTO carry_added SELECT client, month, seconds, '' FROM carry_added_old")
                self.db.execute("DROP TABLE carry_added_old")
        if old:
            with self.db:
                self.db.execute(
                    """INSERT INTO bill (business_id, number, seq, client, month, issued_on, due_on, seconds, net, vat,
                                         total, data, created_at, voided_at)
                       SELECT 'b1', number, seq, client, month, issued_on, due_on, seconds, net, vat, total, data,
                              created_at, voided_at FROM bill_v30 ORDER BY created_at""")
                self.db.execute("DROP TABLE bill_v30")

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
            self.db.execute("DELETE FROM rollover WHERE interval_id=?", (entry_id,))

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

    def rolled(self) -> dict[int, dict]:
        """Rolled-over entries: id -> {"month": month now charged in, "entry_date": the entry's own date}."""
        return {r["interval_id"]: {"month": r["month"], "entry_date": r["entry_date"]}
                for r in self.db.execute("SELECT * FROM rollover")}

    def set_rolled(self, interval_id: int, month: str | None, entry_date: str | None = None) -> None:
        with self.db:
            if month:
                self.db.execute("INSERT OR REPLACE INTO rollover VALUES (?, ?, ?)", (interval_id, month, entry_date))
            else:
                self.db.execute("DELETE FROM rollover WHERE interval_id = ?", (interval_id,))

    def carry_rows(self, client: str) -> list[dict]:
        """Carried-over hours added to a client's invoices: [{"month", "task" ("" = whole client), "seconds"}]."""
        return [{"month": r["month"], "task": r["task"], "seconds": r["seconds"]} for r in self.db.execute(
            "SELECT month, task, seconds FROM carry_added WHERE lower(client) = lower(?) ORDER BY month", (client,))]

    def set_carry_added(self, client: str, month: str, seconds: int, task: str = "") -> None:
        with self.db:
            self.db.execute("DELETE FROM carry_added WHERE lower(client) = lower(?) AND month = ? "
                            "AND lower(task) = lower(?)", (client, month, task))
            if seconds > 0:
                self.db.execute("INSERT INTO carry_added VALUES (?, ?, ?, ?)", (client, month, seconds, task))

    def invoiced(self, client: str) -> dict[str, dict]:
        """Months marked as invoiced for a client: month -> {marked_at, seconds, amount}."""
        return {r["month"]: {"marked_at": r["marked_at"], "seconds": r["seconds"], "amount": r["amount"]}
                for r in self.db.execute("SELECT * FROM invoiced WHERE lower(client) = lower(?)", (client,))}

    def set_invoiced(self, client: str, month: str, seconds: int | None = None, amount: float | None = None) -> None:
        """Mark a month as invoiced (with its total then), or pass seconds=None to unmark it."""
        with self.db:
            self.db.execute("DELETE FROM invoiced WHERE lower(client) = lower(?) AND month = ?", (client, month))
            if seconds is not None:
                self.db.execute("INSERT INTO invoiced VALUES (?, ?, ?, ?, ?)",
                                (client, month, datetime.now(timezone.utc).isoformat(), seconds, amount))
            else:   # no invoice, so nothing to have been paid
                self.db.execute("DELETE FROM paid WHERE lower(client) = lower(?) AND month = ?", (client, month))

    def paid(self, client: str) -> dict[str, str]:
        """Invoiced months a client has paid: month -> when it was marked paid."""
        return {r["month"]: r["paid_at"] for r in
                self.db.execute("SELECT month, paid_at FROM paid WHERE lower(client) = lower(?)", (client,))}

    def set_paid(self, client: str, month: str, paid: bool = True) -> None:
        with self.db:
            self.db.execute("DELETE FROM paid WHERE lower(client) = lower(?) AND month = ?", (client, month))
            if paid:
                self.db.execute("INSERT INTO paid VALUES (?, ?, ?)",
                                (client, month, datetime.now(timezone.utc).isoformat()))

    def mark_all_paid(self) -> int:
        """Mark every invoiced month as paid (for catching up). Returns how many were newly marked."""
        with self.db:
            return self.db.execute(
                """INSERT INTO paid SELECT i.client, i.month, ? FROM invoiced i
                   WHERE NOT EXISTS (SELECT 1 FROM paid p WHERE lower(p.client) = lower(i.client) AND p.month = i.month)""",
                (datetime.now(timezone.utc).isoformat(),)).rowcount

    # ----- invoices made in the app -----

    @staticmethod
    def _bill(row) -> dict:
        return {**dict(row), "data": json.loads(row["data"])}

    def add_bill(self, bill: dict) -> int:
        with self.db:
            cur = self.db.execute(
                """INSERT INTO bill (business_id, number, seq, client, month, issued_on, due_on, seconds, net, vat,
                                     total, data, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (bill["business_id"], bill["number"], bill["seq"], bill["client"], bill["month"], bill["issued_on"],
                 bill["due_on"], bill["seconds"], bill["net"], bill["vat"], bill["total"], json.dumps(bill["data"]),
                 datetime.now(timezone.utc).isoformat()))
        return cur.lastrowid

    def bill(self, bill_id) -> dict | None:
        try:
            row = self.db.execute("SELECT * FROM bill WHERE id = ?", (int(bill_id),)).fetchone()
        except (TypeError, ValueError):
            return None
        return self._bill(row) if row else None

    def number_used(self, business_id: str, number: str) -> bool:
        return self.db.execute("SELECT 1 FROM bill WHERE business_id = ? AND number = ?",
                               (business_id, number)).fetchone() is not None

    def bills(self) -> list[dict]:
        """Every invoice, newest first (voided ones included)."""
        return [self._bill(r) for r in self.db.execute("SELECT * FROM bill ORDER BY created_at DESC, id DESC")]

    def bill_for(self, client: str, month: str) -> dict | None:
        """The live (not voided) invoice for a client's month, if there is one."""
        row = self.db.execute("SELECT * FROM bill WHERE lower(client) = lower(?) AND month = ? AND voided_at IS NULL",
                              (client, month)).fetchone()
        return self._bill(row) if row else None

    def void_bill(self, bill_id) -> dict | None:
        """Void an invoice and put its month back to not invoiced. Returns the invoice, or None if there's no such one."""
        bill = self.bill(bill_id)
        if not bill or bill["voided_at"]:
            return bill
        with self.db:
            self.db.execute("UPDATE bill SET voided_at = ? WHERE id = ?",
                            (datetime.now(timezone.utc).isoformat(), bill["id"]))
            self.db.execute("UPDATE extra SET bill_id = NULL WHERE bill_id = ?", (bill["id"],))
        self.set_invoiced(bill["client"], bill["month"], None)
        return bill

    # ----- extras for invoices -----

    def add_extra(self, client: str, month: str, kind: str, description: str, quantity: float, unit_price: float) -> int:
        with self.db:
            return self.db.execute(
                "INSERT INTO extra (client, month, kind, description, quantity, unit_price, created_at) VALUES (?,?,?,?,?,?,?)",
                (client, month, kind, description, quantity, unit_price, datetime.now(timezone.utc).isoformat())).lastrowid

    def extras(self, client: str | None = None, month: str | None = None) -> list[dict]:
        """Extras not yet on an invoice, oldest first (for one client's month, or all of them)."""
        sql, args = "SELECT * FROM extra WHERE bill_id IS NULL", []
        if client is not None:
            sql, args = sql + " AND lower(client) = lower(?)", args + [client]
        if month is not None:
            sql, args = sql + " AND month = ?", args + [month]
        return [dict(r) for r in self.db.execute(sql + " ORDER BY month, id", args)]

    def delete_extra(self, extra_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM extra WHERE id = ? AND bill_id IS NULL", (extra_id,))

    def claim_extras(self, client: str, month: str, bill_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE extra SET bill_id = ? WHERE bill_id IS NULL AND lower(client) = lower(?) AND month = ?",
                            (bill_id, client, month))

    # ----- hours in lieu -----

    _LIEU_LIVE = f"(l.entry_id IS NULL OR EXISTS (SELECT 1 FROM manual_entry m WHERE m.id = -l.entry_id - {MANUAL_ID_OFFSET}))"

    def add_lieu(self, client: str, seconds: int, note: str = "", entry_id: int | None = None) -> None:
        with self.db:
            self.db.execute("INSERT INTO lieu (client, seconds, note, created_at, entry_id) VALUES (?,?,?,?,?)",
                            (client, seconds, note, datetime.now(timezone.utc).isoformat(), entry_id))

    def lieu_balance(self, client: str) -> int:
        row = self.db.execute(f"SELECT COALESCE(SUM(l.seconds), 0) FROM lieu l WHERE lower(l.client) = lower(?) AND {self._LIEU_LIVE}",
                              (client,)).fetchone()
        return int(row[0])

    def lieu_log(self, client: str, limit: int = 12) -> list[dict]:
        """The latest changes to a client's bank, newest first."""
        return [dict(r) for r in self.db.execute(
            f"SELECT l.id, l.seconds, l.note, l.created_at, l.entry_id FROM lieu l WHERE lower(l.client) = lower(?) AND {self._LIEU_LIVE} "
            "ORDER BY l.id DESC LIMIT ?", (client, limit))]

    def dismissed(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT key FROM dismissed")}

    def set_dismissed(self, key: str | None, dismissed: bool = True) -> None:
        """Hide a prompt, show it again, or (key=None) show every hidden prompt again."""
        with self.db:
            if key is None:
                self.db.execute("DELETE FROM dismissed")
            elif dismissed:
                self.db.execute("INSERT OR IGNORE INTO dismissed VALUES (?, ?)",
                                (key, datetime.now(timezone.utc).isoformat()))
            else:
                self.db.execute("DELETE FROM dismissed WHERE key = ?", (key,))

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def last_sync(self) -> str | None:
        return self.get_meta("last_sync")
