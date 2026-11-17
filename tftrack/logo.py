"""Logos for invoices and timesheets: read a PNG or JPEG, and keep it in a form the PDF writer can place.

Nothing beyond Python's own library is used, so it works on a plain Mac. A PNG is decoded once, when it's uploaded
(shrunk if it's huge), and the result is kept beside the original; a JPEG goes into the PDF as it is.

Logos are filed by a fingerprint of their contents and never overwritten. An invoice remembers the fingerprint it
was made with, so changing a business's logo later doesn't change invoices already sent.
"""
from __future__ import annotations

import hashlib
import json
import struct
import zlib
from pathlib import Path

from . import config as cfg

MAX_BYTES = 4 * 1024 * 1024      # the file you upload
MAX_SIDE = 900                   # pixels kept on the longer side: sharp in print at logo size, small in the PDF
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def folder() -> Path:
    path = cfg.APP_DIR / "logos"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------- reading images ----------

def _jpeg_size(raw: bytes) -> tuple[int, int, int]:
    """(width, height, colour components) from a JPEG's frame header."""
    i = 2
    while i + 9 < len(raw):
        if raw[i] != 0xFF:
            i += 1
            continue
        marker = raw[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):   # baseline, extended or progressive frame
            h, w, n = struct.unpack(">HHB", raw[i + 5:i + 10])
            return w, h, n
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + struct.unpack(">H", raw[i + 2:i + 4])[0]
    raise ValueError("That JPEG couldn't be read. Try saving it again, or use a PNG.")


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def _png_pixels(raw: bytes) -> tuple[int, int, bytes, bytes | None]:
    """Decode a PNG to (width, height, RGB bytes, alpha bytes or None)."""
    pos, chunks, idat = 8, {}, []
    while pos + 8 <= len(raw):
        length, kind = struct.unpack(">I4s", raw[pos:pos + 8])
        body = raw[pos + 8:pos + 8 + length]
        if kind == b"IDAT":
            idat.append(body)
        else:
            chunks.setdefault(kind, body)
        pos += 12 + length
    if b"IHDR" not in chunks or not idat:
        raise ValueError("That PNG couldn't be read. Try saving it again.")
    w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", chunks[b"IHDR"])
    if interlace:
        raise ValueError("That PNG is saved as 'interlaced', which isn't supported. Save it again without interlacing.")
    if depth not in (8, 16) and not (ctype in (0, 3) and depth in (1, 2, 4)):
        raise ValueError("That PNG uses an unusual colour depth. Save it as a normal 8-bit PNG.")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None:
        raise ValueError("That PNG's colour type isn't supported. Save it as a normal RGB PNG.")
    bits = channels * depth
    bpp = max(1, bits // 8)                 # bytes per whole pixel, for the filters
    stride = (w * bits + 7) // 8
    data = zlib.decompress(b"".join(idat))
    if len(data) < (stride + 1) * h:
        raise ValueError("That PNG is damaged. Try saving it again.")
    rows, prev = [], bytearray(stride)
    for y in range(h):
        start = y * (stride + 1)
        f, line = data[start], bytearray(data[start + 1:start + 1 + stride])
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                line[i] = (line[i] + ((line[i - bpp] if i >= bpp else 0) + prev[i]) // 2) & 255
        elif f == 4:
            for i in range(stride):
                line[i] = (line[i] + _paeth(line[i - bpp] if i >= bpp else 0, prev[i], prev[i - bpp] if i >= bpp else 0)) & 255
        rows.append(line)
        prev = line

    def samples(line: bytearray) -> list[int]:
        """One value per channel sample, scaled to 0-255 (palette indexes left as they are)."""
        if depth == 8:
            return list(line)
        if depth == 16:
            return list(line[0::2])
        out, mask, scale = [], (1 << depth) - 1, 1 if ctype == 3 else 255 // ((1 << depth) - 1)
        for byte in line:
            for shift in range(8 - depth, -1, -depth):
                out.append(((byte >> shift) & mask) * scale)
        return out[:w * channels]

    palette = chunks.get(b"PLTE", b"")
    trns = chunks.get(b"tRNS")
    rgb, alpha = bytearray(), bytearray()
    has_alpha = ctype in (4, 6) or (ctype == 3 and trns is not None)
    for line in rows:
        s = samples(line)
        if ctype == 2:
            rgb += bytes(s)
        elif ctype == 6:
            rgb += bytes(v for i, v in enumerate(s) if i % 4 != 3)
            alpha += bytes(s[3::4])
        elif ctype == 0:
            for v in s:
                rgb += bytes((v, v, v))
        elif ctype == 4:
            for v in s[0::2]:
                rgb += bytes((v, v, v))
            alpha += bytes(s[1::2])
        else:   # palette
            for v in s:
                rgb += palette[v * 3:v * 3 + 3] or b"\x00\x00\x00"
                if has_alpha:
                    alpha.append(trns[v] if v < len(trns) else 255)
    return w, h, bytes(rgb), bytes(alpha) if has_alpha else None


def _shrink(w: int, h: int, rgb: bytes, alpha: bytes | None) -> tuple[int, int, bytes, bytes | None]:
    """Shrink by a whole number so the longer side is at most MAX_SIDE, averaging each block of pixels."""
    n = -(-max(w, h) // MAX_SIDE)
    if n <= 1:
        return w, h, rgb, alpha
    nw, nh = w // n, h // n
    out, out_a, area = bytearray(nw * nh * 3), bytearray(nw * nh) if alpha else None, n * n
    for y in range(nh):
        for x in range(nw):
            r = g = b = a = 0
            for dy in range(n):
                base = ((y * n + dy) * w + x * n) * 3
                block = rgb[base:base + n * 3]
                r += sum(block[0::3])
                g += sum(block[1::3])
                b += sum(block[2::3])
                if alpha:
                    ab = (y * n + dy) * w + x * n
                    a += sum(alpha[ab:ab + n])
            i = (y * nw + x) * 3
            out[i], out[i + 1], out[i + 2] = r // area, g // area, b // area
            if alpha:
                out_a[y * nw + x] = a // area
    return nw, nh, bytes(out), bytes(out_a) if alpha else None


# ---------- keeping and fetching ----------

def save(raw: bytes) -> str:
    """Keep an uploaded logo. Returns its key (a fingerprint and the file type), to store with the business."""
    if len(raw) > MAX_BYTES:
        raise ValueError("That file is too big for a logo (over 4 MB). Use a smaller version.")
    if raw.startswith(PNG_MAGIC):
        ext = "png"
    elif raw[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    else:
        raise ValueError("A logo needs to be a PNG or JPG file.")
    key = f"{hashlib.sha256(raw).hexdigest()[:20]}.{ext}"
    meta_path = folder() / f"{key}.json"
    if meta_path.exists():
        return key
    if ext == "jpg":
        w, h, n = _jpeg_size(raw)
        if n not in (1, 3):
            raise ValueError("That JPEG is in CMYK (a print format). Save it as RGB, or use a PNG.")
        meta = {"w": w, "h": h, "filter": "DCTDecode", "space": "DeviceGray" if n == 1 else "DeviceRGB", "alpha": False}
        (folder() / f"{key}.img").write_bytes(raw)
    else:
        w, h, rgb, alpha = _shrink(*_png_pixels(raw))
        meta = {"w": w, "h": h, "filter": "FlateDecode", "space": "DeviceRGB", "alpha": alpha is not None}
        (folder() / f"{key}.img").write_bytes(zlib.compress(rgb, 9))
        if alpha is not None:
            (folder() / f"{key}.alpha").write_bytes(zlib.compress(alpha, 9))
    (folder() / key).write_bytes(raw)                       # the original, for showing on screen
    meta_path.write_text(json.dumps(meta))                  # written last: its presence means the logo is complete
    return key


def _safe(key: str) -> bool:
    return bool(key) and "/" not in key and "\\" not in key and not key.startswith(".") and key[-4:] in (".png", ".jpg")


def original(key: str) -> tuple[bytes, str] | None:
    """The file as uploaded, with its content type, for showing in the browser."""
    path = folder() / key if _safe(key) else None
    if not path or not path.exists():
        return None
    return path.read_bytes(), "image/png" if key.endswith(".png") else "image/jpeg"


def for_pdf(key: str | None) -> dict | None:
    """The logo ready for the PDF writer: {w, h, filter, space, data, alpha}. None if there isn't one."""
    if not key or not _safe(key):
        return None
    meta_path = folder() / f"{key}.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text())
        meta["data"] = (folder() / f"{key}.img").read_bytes()
        meta["alpha"] = (folder() / f"{key}.alpha").read_bytes() if meta.get("alpha") else None
    except (OSError, ValueError):
        return None
    return meta


def colour(value: str | None, default: tuple[float, float, float]) -> tuple[float, float, float]:
    """"#F97316" -> (0.98, 0.45, 0.09) for the PDF. Anything else gives the default."""
    v = (value or "").strip().lstrip("#")
    if len(v) == 3:
        v = "".join(ch * 2 for ch in v)
    try:
        if len(v) != 6:
            raise ValueError
        return tuple(int(v[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return default


def tint(c: tuple[float, float, float], amount: float = 0.9) -> tuple[float, float, float]:
    """A pale version of a colour, for backgrounds."""
    return tuple(x + (1 - x) * amount for x in c)


def clean_colour(value) -> str:
    """A colour from Settings as "#rrggbb", or "" for none."""
    v = str(value or "").strip()
    if not v:
        return ""
    r, g, b = colour(v, (-1, -1, -1))
    if r < 0:
        raise ValueError("Choose the brand colour with the colour picker.")
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))
