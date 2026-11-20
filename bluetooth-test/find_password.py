"""Second test: find the cube's password in your TimeFlip cloud data, then
use it to read the cube's history.

The official app changes the cube's password from the default. This script
signs in with FlipTracker's saved login (it never asks you for anything),
downloads your cloud data, and looks for a value that works as the cube's
password. It shows field names and lengths only, never the values. It then
tries up to 3 candidates on the cube and reads the history.

It changes nothing on the cube and nothing in your TimeFlip account.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Reuse the first script's cube helpers (same folder).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cube_test as ct  # noqa: E402

REPORT = Path.home() / "Desktop" / "FlipTracker password test.txt"
MAX_TRIES = 3
_lines: list[str] = []


def log(text: str = "") -> None:
    print(text, flush=True)
    _lines.append(text)


# Point Python at the installed FlipTracker so we can reuse its login.
def load_tftrack():
    candidates = [
        Path.home() / "Library" / "Application Support" / "TimeFlip Tracker",
        Path(__file__).resolve().parent.parent,  # the repo, if run from a clone
    ]
    for base in candidates:
        if (base / "tftrack").is_dir():
            sys.path.insert(0, str(base))
            try:
                from tftrack import config as cfg  # type: ignore
                from tftrack import service  # type: ignore
                return cfg, service
            except Exception as exc:  # keep going to the next candidate
                log(f"(found FlipTracker at {base} but could not load it: {exc})")
    return None, None


def walk(obj, path=""):
    """Yield (path, key, value) for every leaf in the cloud data."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from walk(value, f"{path}.{key}" if path else str(key))
    elif isinstance(obj, list):
        for n, value in enumerate(obj[:5]):  # a sample is enough to see the shape
            yield from walk(value, f"{path}[{n}]")
    else:
        yield path, path.split(".")[-1].split("[")[0], obj


def candidates_from(data: dict) -> list[str]:
    """Collect things that might be the cube's 6-character password."""
    named: list[str] = []  # fields whose name hints at a password
    sixish: list[str] = []  # any 6-character string
    hint = ("pass", "pin", "code", "secret", "key", "device")
    log("Fields in your cloud data that might hold a cube password:")
    shown = 0
    for fullpath, key, value in walk(data):
        text = str(value) if value is not None else ""
        name_match = any(h in key.lower() for h in hint)
        if name_match and shown < 40:
            log(f"  {fullpath}  (text, {len(text)} characters)")
            shown += 1
        if name_match and 4 <= len(text) <= 12:
            named.append(text)
        if len(text) == 6 and text.isascii():
            sixish.append(text)
    if shown == 0:
        log("  (none found by name)")
    # Order: named hits first, then 6-char values, default last, de-duplicated.
    ordered, seen = [], set()
    for value in named + sixish + ["000000"]:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


async def try_on_cube(passwords: list[str]) -> None:
    from bleak import BleakClient

    device = await ct.find_cube()
    if device is None:
        return
    log("Connecting...")
    async with BleakClient(device, timeout=30) as client:
        log("Connected.")
        for n, pw in enumerate(passwords[:MAX_TRIES], 1):
            log(f"\nTry {n} of {min(len(passwords), MAX_TRIES)}: a {len(pw)}-character password")
            await client.write_gatt_char(ct.PASSWORD, pw.encode("ascii")[:6].ljust(6, b"\x00"), response=True)
            events = bytes(await client.read_gatt_char(ct.EVENTS))
            ok = b"password ok" in events.lower()
            log("Cube says: " + events.decode("ascii", "replace").strip("\x00 "))
            if ok:
                log(">>> This password works. <<<")
                log("Reading stored history (can take a minute)...")
                results: dict = {}
                entries = await ct.read_history(client, results)
                if entries:
                    log("Oldest: " + ct.describe(entries[0]))
                    log("Newest: " + ct.describe(entries[-1]))
                _summary(True, len(entries))
                return
    _summary(False, 0)


def _summary(found: bool, history_count: int) -> None:
    log()
    log("===== SUMMARY =====")
    log(f"Password found:   {'YES' if found else 'NO'}")
    if found:
        log(f"History records:  {history_count}")


async def main() -> int:
    log(f"FlipTracker password test, {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    cfg, service = load_tftrack()
    if cfg is None:
        log("STOPPED: could not find FlipTracker. Run the FlipTracker installer first.")
        _save()
        return 1
    try:
        conf = cfg.load_config()
        log(f"Signing in to TimeFlip as {conf.get('email', '(unknown)')}...")
        data = service.client(conf).sync_all()
        log(f"Downloaded cloud data. Top-level sections: {sorted(data) if isinstance(data, dict) else type(data).__name__}")
    except Exception as exc:
        log(f"STOPPED: could not download your cloud data ({type(exc).__name__}: {exc})")
        _save()
        return 1

    passwords = candidates_from(data if isinstance(data, dict) else {})
    log(f"\n{len(passwords)} password candidate(s) to try (values hidden), capped at {MAX_TRIES}.")
    if not passwords:
        log("Nothing to try. The password isn't in the cloud data, so option 1 is out.")
        _summary(False, 0)
        _save()
        return 0
    try:
        await try_on_cube(passwords)
    except Exception as exc:
        log(f"STOPPED during the cube test: {type(exc).__name__}: {exc}")
        _summary(False, 0)
    _save()
    return 0


def _save() -> None:
    try:
        REPORT.write_text("\n".join(_lines) + "\n")
        print(f"\nSaved to: {REPORT}")
    except OSError as exc:
        print(f"\nCould not save the report ({exc}). Copy the text above instead.")


if __name__ == "__main__":
    # Make cube_test's helpers log into our report too.
    ct.log = log
    sys.exit(asyncio.run(main()))
