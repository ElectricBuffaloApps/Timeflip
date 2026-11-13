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
_TABLES = ("excluded", "adjustment", "manual_entry", "rollover", "carry_added", "invoiced", "paid", "dismissed", "bill", "extra", "lieu", "banked_week")
# Settings worth carrying over; the TimeFlip account, update and viewer settings stay with each Mac.
_SETTINGS = ("clients", "limits", "targets", "monthly_income", "ignored_tasks", "smooth_weeks", "week_starts",
             "payment_terms_days", "businesses", "client_details", "areas")


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
            task = c.get("task") or ""
            mine = db.execute("SELECT seconds FROM carry_added WHERE lower(client)=lower(?) AND month=? "
                              "AND lower(task)=lower(?)", (c["client"], c["month"], task)).fetchone()
            if not mine:
                db.execute("INSERT INTO carry_added VALUES (?, ?, ?, ?)", (c["client"], c["month"], c["seconds"], task))
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

        n = 0
        for p in t.get("paid", []):
            n += db.execute("INSERT OR IGNORE INTO paid VALUES (?, ?, ?)",
                            (p["client"], p["month"], p["paid_at"])).rowcount
        if n:
            report.append(f"{n} month{'s' * (n != 1)} marked as paid")
        n = 0
        for b in t.get("bill", []):
            b = {"business_id": "b1", **b}   # files from version 30 had one business
            mine = db.execute("SELECT client, month, total FROM bill WHERE business_id = ? AND number = ?",
                              (b["business_id"], b["number"])).fetchone()
            if mine:
                if (mine["client"], mine["month"], mine["total"]) != (b["client"], b["month"], b["total"]):
                    kept.append(f"invoice {b['number']} is a different invoice on each Mac")
                continue
            keys = ("business_id", "number", "seq", "client", "month", "issued_on", "due_on", "seconds", "net", "vat",
                    "total", "data", "created_at", "voided_at")
            db.execute(f"INSERT INTO bill ({', '.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                       tuple(b[k] for k in keys))
            n += 1
        if n:
            report.append(f"{n} invoice{'s' * (n != 1)}")
        n = 0
        for x in t.get("extra", []):
            if x.get("bill_id") is not None:
                continue   # already on one of that Mac's invoices, which came across as an invoice
            same = db.execute("SELECT 1 FROM extra WHERE client = ? AND month = ? AND description = ? AND created_at = ?",
                              (x["client"], x["month"], x["description"], x["created_at"])).fetchone()
            if not same:
                db.execute("INSERT INTO extra (client, month, kind, description, quantity, unit_price, created_at) "
                           "VALUES (?,?,?,?,?,?,?)", (x["client"], x["month"], x["kind"], x["description"],
                                                      x["quantity"], x["unit_price"], x["created_at"]))
                n += 1
        if n:
            report.append(f"{n} invoice extra{'s' * (n != 1)}")
        n = 0
        for l in t.get("lieu", []):
            if not db.execute("SELECT 1 FROM lieu WHERE client = ? AND seconds = ? AND created_at = ?",
                              (l["client"], l["seconds"], l["created_at"])).fetchone():
                db.execute("INSERT INTO lieu (client, seconds, note, created_at, entry_id) VALUES (?,?,?,?,?)",
                           (l["client"], l["seconds"], l["note"], l["created_at"],
                            entry(l["entry_id"]) if l["entry_id"] is not None else None))
                n += 1
        if n:
            report.append(f"{n} change{'s' * (n != 1)} to hours in lieu")
        for w in t.get("banked_week", []):
            db.execute("INSERT OR IGNORE INTO banked_week VALUES (?, ?, ?, ?)",
                       (w["client"], w["month"], w["week_start"], w["seconds"]))
        for d in t.get("dismissed", []):
            db.execute("INSERT OR IGNORE INTO dismissed VALUES (?, ?)", (d["key"], d["dismissed_at"]))

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
    from . import billing
    theirs = billing.businesses(s) if (s.get("businesses") or s.get("business")) else []
    if theirs:
        mine = billing.businesses(conf)
        if not any(b["name"] for b in mine):
            mine = theirs
            n += 1
        else:
            for t in theirs:
                same = next((b for b in mine if b["id"] == t["id"]), None)
                if same is None:
                    mine.append(t)
                    n += 1
                    continue
                if {k: v for k, v in same.items() if k != "next_number"} != {k: v for k, v in t.items() if k != "next_number"}:
                    kept.append(f"business details for {same['name'] or t['name']} differ")
                # invoice numbers must never go backwards
                same["next_number"] = max(int(same.get("next_number") or 1), int(t.get("next_number") or 1))
        conf["businesses"] = mine
        conf.pop("business", None)
    details = dict(conf.get("client_details") or {})
    for name, d in (s.get("client_details") or {}).items():
        mine = next((k for k in details if k.lower() == name.lower()), None)
        if mine is None:
            details[name] = d
            n += 1
        elif details[mine] != d:
            kept.append(f"billing details for {name} differ")
    conf["client_details"] = details
    if conf.get("areas") is None and s.get("areas") is not None:
        conf["areas"] = s["areas"]
        n += 1
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
