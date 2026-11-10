"""Bringing one Mac's own changes into another's, so a Mac can become a viewer without losing anything.

TimeFlip's entries come from the cloud and are the same everywhere. What's only on one Mac is what Dave did there:
edits, added entries, exclusions, roll-overs, carried hours added, "invoiced" marks and settings. Export takes all of
that; import adds whatever the receiving Mac doesn't have. Where both Macs changed the same thing differently, the
receiving Mac keeps its own and the report says so.
"""
from __future__ import annotations

import socket
from datetime import datetime, timezone

from .store import MANUAL_ID_OFFSET

FORMAT = "timeflip-tracker-changes/1"
_TABLES = ("excluded", "adjustment", "manual_entry", "rollover", "carry_added", "invoiced")
# Settings worth carrying over; the TimeFlip account, update and viewer settings stay with each Mac.
_SETTINGS = ("clients", "limits", "targets", "monthly_income", "ignored_tasks", "smooth_weeks", "week_starts")


def export(store, conf: dict) -> dict:
    rows = {t: [dict(r) for r in store.db.execute(f"SELECT * FROM {t}")] for t in _TABLES}
    return {"format": FORMAT, "exported_at": datetime.now(timezone.utc).isoformat(),
            "from": socket.gethostname(), "settings": {k: conf[k] for k in _SETTINGS if k in conf}, "tables": rows}


def _manual_id(rowid: int) -> int:
    return -(MANUAL_ID_OFFSET + rowid)


def import_changes(store, conf: dict, data: dict) -> tuple[dict, list[str]]:
    """Merge another Mac's export into this one. Returns (updated settings, report lines)."""
    if data.get("format") != FORMAT:
        raise ValueError("That isn't a TimeFlip Tracker changes file.")
    t = data.get("tables") or {}
    report: list[str] = []
    kept: list[str] = []
    db = store.db
    with db:
        # Added entries first: match by start, length and task; new ones get this Mac's own ids.
        ids: dict[int, int] = {}
        added = 0
        for m in t.get("manual_entry", []):
            row = db.execute("SELECT id FROM manual_entry WHERE started_at=? AND duration_s=? AND task_id=?",
                             (m["started_at"], m["duration_s"], m["task_id"])).fetchone()
            if row:
                ids[_manual_id(m["id"])] = _manual_id(row["id"])
            else:
                cur = db.execute("INSERT INTO manual_entry (started_at, duration_s, task_id, added_at) VALUES (?,?,?,?)",
                                 (m["started_at"], m["duration_s"], m["task_id"], m["added_at"]))
                ids[_manual_id(m["id"])] = _manual_id(cur.lastrowid)
                added += 1
        entry = lambda i: ids.get(i, i)
        if added:
            report.append(f"{added} added entr{'y' if added == 1 else 'ies'}")

        n = 0
        for x in t.get("excluded", []):
            n += db.execute("INSERT OR IGNORE INTO excluded VALUES (?, ?)",
                            (entry(x["interval_id"]), x["excluded_at"])).rowcount
        if n:
            report.append(f"{n} exclusion{'s' * (n != 1)}")

        n = 0
        for a in t.get("adjustment", []):
            mine = db.execute("SELECT * FROM adjustment WHERE interval_id=?", (a["interval_id"],)).fetchone()
            if not mine:
                db.execute("INSERT INTO adjustment VALUES (?,?,?,?,?)",
                           (a["interval_id"], a["started_at"], a["duration_s"], a["task_id"], a["adjusted_at"]))
                n += 1
            elif (mine["started_at"], mine["duration_s"], mine["task_id"]) != (a["started_at"], a["duration_s"], a["task_id"]):
                kept.append(f"entry {a['interval_id']} was edited differently on both Macs")
        if n:
            report.append(f"{n} edited entr{'y' if n == 1 else 'ies'}")

        n = 0
        for r in t.get("rollover", []):
            n += db.execute("INSERT OR IGNORE INTO rollover VALUES (?, ?, ?)",
                            (entry(r["interval_id"]), r["month"], r["entry_date"])).rowcount
        if n:
            report.append(f"{n} rolled-over entr{'y' if n == 1 else 'ies'}")

        n = 0
        for c in t.get("carry_added", []):
            mine = db.execute("SELECT seconds FROM carry_added WHERE lower(client)=lower(?) AND month=?",
                              (c["client"], c["month"])).fetchone()
            if not mine:
                db.execute("INSERT INTO carry_added VALUES (?, ?, ?)", (c["client"], c["month"], c["seconds"]))
                n += 1
            elif mine["seconds"] != c["seconds"]:
                kept.append(f"carried hours added to {c['client']} {c['month']} differ")
        if n:
            report.append(f"{n} lot{'s' * (n != 1)} of carried hours added to invoices")

        n = 0
        for i in t.get("invoiced", []):
            n += db.execute("INSERT OR IGNORE INTO invoiced VALUES (?, ?, ?, ?, ?)",
                            (i["client"], i["month"], i["marked_at"], i["seconds"], i["amount"])).rowcount
        if n:
            report.append(f"{n} month{'s' * (n != 1)} marked as invoiced")

    conf = dict(conf)
    s = data.get("settings") or {}
    n = 0
    clients = dict(conf.get("clients") or {})
    for name, c in (s.get("clients") or {}).items():
        mine = next((k for k in clients if k.lower() == name.lower()), None)
        if mine is None:
            clients[name] = c
            n += 1
        elif clients[mine] != c:
            kept.append(f"invoicing settings for {name} differ")
    conf["clients"] = clients
    incomes = {k: dict(v) for k, v in (conf.get("monthly_income") or {}).items()}
    for name, months in (s.get("monthly_income") or {}).items():
        for month, amount in months.items():
            have = incomes.setdefault(name, {})
            if month not in have:
                have[month] = amount
                n += 1
            elif have[month] != amount:
                kept.append(f"{name}'s income for {month} differs")
    conf["monthly_income"] = incomes
    limits = list(conf.get("limits") or [])
    names = {(l.get("task") or "").lower() for l in limits} | {(l.get("client") or "").lower() for l in limits}
    for l in s.get("limits") or []:
        key = (l.get("task") or l.get("client") or "").lower()
        if key and key not in names:
            limits.append(l)
            n += 1
        elif key and l not in limits:
            kept.append(f"limit for {l.get('task') or l.get('client')} differs")
    conf["limits"] = limits
    for key in ("ignored_tasks", "smooth_weeks"):
        merged = list(conf.get(key) or [])
        for v in s.get(key) or []:
            if v not in merged:
                merged.append(v)
                n += 1
        conf[key] = merged
    if not conf.get("targets") and s.get("targets"):
        conf["targets"] = s["targets"]
        n += 1
    elif s.get("targets") and s["targets"] != conf.get("targets"):
        kept.append("income and time targets differ")
    if n:
        report.append(f"{n} setting{'s' * (n != 1)}")
    if kept:
        report.append("Kept this Mac's version where both differed: " + "; ".join(kept) + ".")
    return conf, report or ["Nothing new: this Mac already had everything in that file."]
