"""Timesheets as PDF files, written directly (no print dialog, no extra software).

A small PDF writer using the built-in Helvetica fonts, so every Mac opens the result the same way.
"""
from __future__ import annotations

import zlib
from datetime import date, timedelta

PAGE_W, PAGE_H = 595.28, 841.89   # A4 in points
MARGIN = 40
INK, MUTED, FAINT = (0.07, 0.09, 0.15), (0.42, 0.45, 0.50), (0.61, 0.64, 0.69)
ACCENT, TINT, GREY_BG, RULE = (0.98, 0.45, 0.09), (1.0, 0.97, 0.93), (0.95, 0.96, 0.96), (0.90, 0.91, 0.92)
ORANGE_TEXT = (0.76, 0.25, 0.05)

# Adobe Helvetica / Helvetica-Bold advance widths (1/1000 em) for WinAnsi codes 32-255.
_WIDTHS = {
    "F1": [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556,
    556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778,
    722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278,
    278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584, 350, 556, 350, 222, 556,
    333, 1000, 556, 556, 333, 1000, 667, 333, 1000, 350, 611, 350, 350, 222, 222, 333, 333, 350, 556,
    1000, 333, 1000, 500, 333, 944, 350, 500, 667, 278, 333, 556, 556, 556, 556, 260, 556, 333, 737,
    370, 556, 584, 333, 737, 333, 400, 584, 333, 333, 333, 556, 537, 278, 333, 333, 365, 556, 834, 834,
    834, 611, 667, 667, 667, 667, 667, 667, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278, 722, 722,
    778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611, 556, 556, 556, 556, 556, 556,
    889, 500, 556, 556, 556, 556, 278, 278, 278, 278, 556, 556, 556, 556, 556, 556, 556, 584, 611, 556,
    556, 556, 556, 500, 556, 500
    ],
    "F2": [
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556,
    556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778,
    722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333,
    278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584, 350, 556, 350, 278, 556,
    500, 1000, 556, 556, 333, 1000, 667, 333, 1000, 350, 611, 350, 350, 278, 278, 500, 500, 350, 556,
    1000, 333, 1000, 556, 333, 944, 350, 500, 667, 278, 333, 556, 556, 556, 556, 280, 556, 333, 737,
    370, 556, 584, 333, 737, 333, 400, 584, 333, 333, 333, 611, 556, 278, 333, 333, 365, 556, 834, 834,
    834, 611, 722, 722, 722, 722, 722, 722, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278, 722, 722,
    778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611, 556, 556, 556, 556, 556, 556,
    889, 556, 556, 556, 556, 556, 278, 278, 278, 278, 611, 611, 611, 611, 611, 611, 611, 584, 611, 611,
    611, 611, 611, 556, 611, 556
    ],
}


def _enc(s: str) -> bytes:
    return s.encode("cp1252", "replace")


def text_width(s: str, size: float, bold: bool = False) -> float:
    w = _WIDTHS["F2" if bold else "F1"]
    return sum(w[b - 32] if 32 <= b < 256 else 556 for b in _enc(s)) * size / 1000


def fit(s: str, width: float, size: float, bold: bool = False) -> str:
    """Shorten s with an ellipsis so it fits in width."""
    if text_width(s, size, bold) <= width:
        return s
    while s and text_width(s + "…", size, bold) > width:
        s = s[:-1]
    return s.rstrip() + "…"


class Document:
    def __init__(self, title: str):
        self.title = title
        self.pages: list[list[str]] = []
        self.new_page()

    def new_page(self) -> None:
        self.ops: list[str] = []
        self.pages.append(self.ops)

    @staticmethod
    def _rgb(c, stroke=False) -> str:
        return f"{c[0]:.3f} {c[1]:.3f} {c[2]:.3f} {'RG' if stroke else 'rg'}"

    def text(self, x: float, y: float, s: str, size: float = 9, bold: bool = False, color=INK,
             align: str = "left") -> None:
        if align == "right":
            x -= text_width(s, size, bold)
        raw = _enc(s).replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
        self.ops.append(f"BT {self._rgb(color)} /{'F2' if bold else 'F1'} {size} Tf {x:.2f} {y:.2f} Td ("
                        + raw.decode("latin-1") + ") Tj ET")

    def line(self, x1: float, y1: float, x2: float, y2: float, width: float = 0.5, color=RULE) -> None:
        self.ops.append(f"{self._rgb(color, True)} {width} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    def rect(self, x: float, y: float, w: float, h: float, color) -> None:
        self.ops.append(f"{self._rgb(color)} {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")

    def to_bytes(self) -> bytes:
        objs: list[bytes] = []
        font = lambda name: (f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} "
                             f"/Encoding /WinAnsiEncoding >>").encode()
        objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(len(self.pages)))
        objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(self.pages)} >>".encode())
        objs.append(font("Helvetica"))
        objs.append(font("Helvetica-Bold"))
        for n, ops in enumerate(self.pages, 1):
            footer = Document.__new__(Document)
            footer.ops = []
            footer.text(MARGIN, 22, fit(self.title, 380, 8), 8, color=FAINT)
            footer.text(PAGE_W - MARGIN, 22, f"Page {n} of {len(self.pages)}", 8, color=FAINT, align="right")
            stream = zlib.compress("\n".join(ops + footer.ops).encode("latin-1"))
            idx = len(objs) + 1
            objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                        f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {idx + 1} 0 R >>".encode())
            objs.append(f"<< /Length {len(stream)} /Filter /FlateDecode >>\nstream\n".encode() + stream
                        + b"\nendstream")
        title = ("\ufeff" + self.title).encode("utf-16-be").hex()   # PDF text strings: UTF-16 with a byte-order mark
        objs.append(f"<< /Title <{title}> /Producer (TimeFlip Tracker) >>".encode())
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
        out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
        out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info {len(objs)} 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n").encode()
        return bytes(out)


def _hm(seconds: int) -> str:
    m = round(seconds / 60)
    return f"{m // 60}h {m % 60:02d}m"


class _Sheet:
    """A table that flows over pages, repeating its column headings."""
    ROW = 15

    def __init__(self, doc: Document, title: str, subtitle: str, columns: list[tuple[str, float, str]]):
        self.doc, self.columns = doc, columns   # (heading, x, align)
        self.y = PAGE_H - MARGIN
        doc.rect(MARGIN, self.y - 3, 28, 2.5, ACCENT)
        self.y -= 26
        doc.text(MARGIN, self.y, title, 18, bold=True)
        self.y -= 16
        doc.text(MARGIN, self.y, fit(subtitle, PAGE_W - 2 * MARGIN, 9), 9, color=MUTED)
        self.y -= 14

    def note(self, s: str, bold_part: str = "") -> None:
        """A tinted box above the table (e.g. the allowance line), wrapped over as many lines as it needs."""
        width = PAGE_W - 2 * MARGIN - 16
        first = width - (text_width(bold_part + " ", 8.5, True) if bold_part else 0)
        lines, line = [], ""
        for word in s.split(" "):
            trial = f"{line} {word}".strip()
            if text_width(trial, 8.5) > (first if not lines else width) and line:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
        h = 12 * len(lines) + 10
        self.doc.rect(MARGIN, self.y - h, PAGE_W - 2 * MARGIN, h, TINT)
        y = self.y - 15
        for n, text in enumerate(lines):
            x = MARGIN + 8
            if n == 0 and bold_part:
                self.doc.text(x, y, bold_part, 8.5, bold=True)
                x += text_width(bold_part + " ", 8.5, True)
            self.doc.text(x, y, text, 8.5)
            y -= 12
        self.y -= h + 10

    def head(self) -> None:
        for heading, x, align in self.columns:
            self.doc.text(x, self.y - 10, heading, 8, bold=True, color=MUTED, align=align)
        self.y -= 15
        self.doc.line(MARGIN, self.y, PAGE_W - MARGIN, self.y, 0.8, (0.82, 0.84, 0.86))

    def room(self, rows: int = 1) -> None:
        if self.y - rows * self.ROW < MARGIN + 20:
            self.doc.new_page()
            self.y = PAGE_H - MARGIN
            self.head()

    def row(self, cells: list[str], *, bold: bool = False, color=INK, fill=None, strike: bool = False,
            notes: dict[int, tuple[str, tuple]] | None = None, size: float = 8.5, rule: bool = True) -> None:
        self.room()
        top = self.y
        if fill:
            self.doc.rect(MARGIN, top - self.ROW, PAGE_W - 2 * MARGIN, self.ROW, fill)
        base = top - 10.5
        cells = list(cells) + [""] * (len(self.columns) - len(cells))
        for i, ((_, x, align), cell) in enumerate(zip(self.columns, cells)):
            if not cell:
                continue
            limit = 200
            if align == "left":   # a left cell can run on over empty cells to the next one with text
                j = next((j for j in range(i + 1, len(cells)) if cells[j]), None)
                if j is None:
                    limit = PAGE_W - MARGIN - x
                else:
                    _, nx, nalign = self.columns[j]
                    limit = (nx - text_width(cells[j], size, bold) if nalign == "right" else nx) - x - 10
            s = fit(cell, limit, size, bold)
            self.doc.text(x, base, s, size, bold, color, align)
            w = text_width(s, size, bold)
            if strike:
                x0 = x - w if align == "right" else x
                self.doc.line(x0, base + 3, x0 + w, base + 3, 0.6, color)
            if notes and i in notes and align == "left":
                note, ncol = notes[i]
                self.doc.text(x + w + 5, base, fit(note, max(0, limit - w - 5), 7.5), 7.5, color=ncol)
        self.y -= self.ROW
        if rule:
            self.doc.line(MARGIN, self.y, PAGE_W - MARGIN, self.y)

    def gap(self, h: float = 6) -> None:
        self.y -= h


def _entry_notes(r: dict) -> tuple[str, tuple] | None:
    if r["excluded"]:
        return "not charged", MUTED
    if r.get("rolled"):
        return "rolled over: carried to a later invoice", ORANGE_TEXT
    if r["manual"]:
        return "added", ORANGE_TEXT
    if r["adjusted"]:
        return f"adjusted · {r['note']}", ORANGE_TEXT
    return None


def invoice_pdf(inv: dict) -> bytes:
    """The monthly sheet for a client billed by the day or by the week, as on the timesheet page."""
    month = date.fromisoformat(inv["month"] + "-01")
    start, end = date.fromisoformat(inv["start"]), date.fromisoformat(inv["end"])
    style = "by the day" if inv["style"] == "day" else "by the week (weeks starting on a Monday in the month)"
    rounding = ("" if inv["rounding"] == "none" else
                f" · day totals rounded {'to the nearest' if inv['rounding'] == 'nearest' else inv['rounding'] + ' to'}"
                f" {inv['block_minutes']} minutes")
    title = f"{inv['client']}: {month:%B %Y}"
    doc = Document(f"{inv['client']} – {month:%B %Y}")
    cols = [("Date", MARGIN + 4, "left"), ("Time", MARGIN + 74, "left"), ("Task", MARGIN + 150, "left"),
            ("Duration", PAGE_W - MARGIN - 70, "right"), ("Hours", PAGE_W - MARGIN - 4, "right")]
    t = _Sheet(doc, title, f"{start:%d %b %Y} to {end:%d %b %Y} · billed {style}{rounding}", cols)
    al = inv.get("allowance")
    if al:
        over = al["charging_s"] - al["max_s"]
        state = f"{_hm(over)} over the max" if over > 0 else f"{_hm(-over)} under the max"
        added = al["carry"]["added_s"]
        extra = (f" (incl. {_hm(added)} carried over)" if added else "")
        t.note(f"max {_hm(al['max_s'])} ({al['working_days']} working days) · charging {_hm(al['charging_s'])}"
               f"{extra} · {state}", f"{al['task']}, {month:%B %Y}:")
    t.head()
    weeks = {w["monday"]: w for w in al["weeks"]} if al else {}
    carried = {w["monday"]: w["seconds"] for w in al["carry"]["by_week"]} if al else {}

    def day_rows(d: dict) -> None:
        day = date.fromisoformat(d["date"])
        for r in d["entries"]:
            faded = r["excluded"] or bool(r.get("rolled"))
            note = _entry_notes(r)
            t.row([f"{day:%a %d %b}", f"{r['start']}–{r['end']}", r["task"], _hm(r["seconds"]), ""],
                  color=FAINT if faded else INK, strike=r["excluded"], notes={2: note} if note else None)
        actual = "" if d["actual_s"] == d["billed_s"] else f"actual {_hm(d['actual_s'])}"
        t.room(2)
        t.row([f"{day:%A %d %B} total", "", "", _hm(d["billed_s"]), f"{d['billed_s'] / 3600:.2f} h"],
              bold=True, fill=TINT, notes={0: (actual, MUTED)} if actual else None)

    for g in inv["groups"]:
        if g["label"]:
            t.room(3)
            t.gap(4)
            t.row([g["label"]], bold=True, size=10, rule=False)
        if not g["days"]:
            t.row(["No time recorded."], color=MUTED)
        week_s = 0
        for i, d in enumerate(g["days"]):
            day_rows(d)
            if inv["style"] == "day":
                day = date.fromisoformat(d["date"])
                week_s += d["billed_s"]
                monday = day - timedelta(days=day.weekday())
                nxt = date.fromisoformat(g["days"][i + 1]["date"]) if i + 1 < len(g["days"]) else None
                if nxt is None or nxt - timedelta(days=nxt.weekday()) != monday:
                    w = weeks.get(monday.isoformat())
                    note = None
                    if w:
                        over = w["worked_s"] - w["max_s"]
                        extra = carried.get(monday.isoformat())
                        note = (f"{al['task']} {_hm(w['worked_s'])} of {_hm(w['max_s'])} max"
                                + (f" · {_hm(over)} over" if over > 0 else "")
                                + (f" · +{_hm(extra)} carried over" if extra else ""), ORANGE_TEXT if over > 0 else MUTED)
                    t.row([f"Week of {monday:%a %d %b} total", "", "", _hm(week_s), f"{week_s / 3600:.2f} h"],
                          bold=True, fill=GREY_BG, notes={0: note} if note else None)
                    week_s = 0
        if g["label"]:
            t.row(["Week total", "", "", _hm(g["billed_s"]), f"{g['billed_s'] / 3600:.2f} h"], bold=True, fill=TINT)
    t.gap(8)
    t.room(4)
    if inv["style"] == "day":
        t.row(["Days worked", "", "", str(inv["days_worked"]), ""], bold=True, size=10)
    if al:
        for w in al["carry"]["by_week"]:
            t.row([f"{al['task']}: carried over, week of {date.fromisoformat(w['monday']):%a %d %b}", "", "",
                   _hm(w["seconds"]), f"{w['seconds'] / 3600:.2f} h"], bold=True, size=10)
        left = al["carry"]["unplaced_s"]
        if left:
            t.row([f"{al['task']}: carried over from earlier months", "", "", _hm(left), f"{left / 3600:.2f} h"],
                  bold=True, size=10)
    t.doc.line(MARGIN, t.y, PAGE_W - MARGIN, t.y, 1.2, INK)
    t.row([f"Total for {month:%B %Y}", "", "", _hm(inv["total_billed_s"]), f"{inv['total_billed_s'] / 3600:.2f} h"],
          bold=True, size=10.5, rule=False)
    return doc.to_bytes()


def timesheet_pdf(rows: list[dict], start: date, end: date, client: str | None) -> bytes:
    """The plain list of entries (clients billed by the hour, or all clients)."""
    who = client or "All clients"
    doc = Document(f"Timesheet – {who}")
    cols = [("Date", MARGIN + 4, "left"), ("Time", MARGIN + 70, "left"), ("Client", MARGIN + 142, "left"),
            ("Task", MARGIN + 222, "left"), ("Hours", PAGE_W - MARGIN - 70, "right"), ("Amount", PAGE_W - MARGIN - 4, "right")]
    t = _Sheet(doc, f"Timesheet: {who}", f"{start:%d %b %Y} to {end:%d %b %Y}", cols)
    t.head()
    if not rows:
        t.row(["No time recorded in this period."], color=MUTED)
    for r in rows:
        note = _entry_notes(r)
        amount = f"{r['currency']}{r['amount']:.2f}" if r["amount"] else "–"
        t.row([f"{date.fromisoformat(r['date']):%a %d %b}", f"{r['start']}–{r['end']}", r["client"], r["task"],
               f"{r['hours']:.2f}", amount], color=FAINT if r["excluded"] else INK, strike=r["excluded"],
              notes={3: note} if note else None)
    charged = [r for r in rows if not r["excluded"]]
    currencies = {r["currency"] for r in charged if r["amount"]}
    total = (f"{next(iter(currencies), '')}{sum(r['amount'] for r in charged):.2f}"
             if len(currencies) <= 1 else "mixed currencies")
    t.gap(6)
    t.doc.line(MARGIN, t.y, PAGE_W - MARGIN, t.y, 1.2, INK)
    t.row(["Total", "", "", "", f"{sum(r['hours'] for r in charged):.2f}", total], bold=True, size=10.5, rule=False)
    return doc.to_bytes()
