"""Try one or more codes as the cube's password, then read its history.

Codes are passed as command-line arguments by the .command wrapper (which
asks you to type them). Nothing is saved. It changes nothing on the cube.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cube_test as ct  # noqa: E402

REPORT = Path.home() / "Desktop" / "FlipTracker code test.txt"
_lines: list[str] = []


def log(text: str = "") -> None:
    print(text, flush=True)
    _lines.append(text)


def clean(code: str) -> str:
    """The cube password is 6 bytes. Pad or trim so any code can be tried."""
    return code.strip()


async def run(codes: list[str]) -> None:
    from bleak import BleakClient

    device = await ct.find_cube()
    if device is None:
        return
    log("Connecting...")
    async with BleakClient(device, timeout=30) as client:
        log("Connected.")
        for n, code in enumerate(codes, 1):
            payload = code.encode("ascii", "replace")[:6].ljust(6, b"\x00")
            log(f"\nCode {n} of {len(codes)}: {len(code)} characters")
            await client.write_gatt_char(ct.PASSWORD, payload, response=True)
            events = bytes(await client.read_gatt_char(ct.EVENTS))
            log("Cube says: " + events.decode("ascii", "replace").strip("\x00 "))
            if b"password ok" in events.lower():
                log(">>> This code works. <<<")
                log("Reading stored history (can take a minute)...")
                results: dict = {}
                entries = await ct.read_history(client, results)
                if entries:
                    log("Oldest: " + ct.describe(entries[0]))
                    log("Newest: " + ct.describe(entries[-1]))
                log()
                log("===== SUMMARY =====")
                log("Code worked:     YES")
                log(f"History records: {len(entries)}")
                return
    log()
    log("===== SUMMARY =====")
    log("Code worked:     NO")


async def main() -> int:
    codes = [clean(c) for c in sys.argv[1:] if clean(c)]
    log(f"FlipTracker code test, {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    if not codes:
        log("No codes given. Nothing to try.")
    else:
        log(f"Trying {len(codes)} code(s).")
        try:
            await run(codes)
        except Exception as exc:
            log(f"STOPPED: {type(exc).__name__}: {exc}")
            log("===== SUMMARY =====")
            log("Code worked:     NO")
    try:
        REPORT.write_text("\n".join(_lines) + "\n")
        print(f"\nSaved to: {REPORT}")
    except OSError as exc:
        print(f"\nCould not save the report ({exc}). Copy the text above instead.")
    return 0


if __name__ == "__main__":
    ct.log = log
    sys.exit(asyncio.run(main()))
