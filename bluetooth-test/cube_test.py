"""One-off test: can this Mac talk to a TIMEFLIP2 cube directly over Bluetooth?

It finds the cube, signs in with the cube's password, and reads the battery,
clock and stored history. It changes nothing on the cube: no settings, no
clock, no history.

Protocol: TimeFlip BLE protocol v4 (github.com/DI-GROUP/TimeFlip.Docs).
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = "f1196f5{}-71a4-11e6-bdf4-0800200c9a66"
SERVICE = BASE.format(0)
EVENTS = BASE.format(1)
FACET = BASE.format(2)
RESULT = BASE.format(3)
COMMAND = BASE.format(4)
STATE = BASE.format(6)
PASSWORD = BASE.format(7)
HISTORY = BASE.format(8)
BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
FIRMWARE = "00002a26-0000-1000-8000-00805f9b34fb"

SCAN_SECONDS = 20
HISTORY_IDLE_SECONDS = 8
HISTORY_MAX_PACKETS = 5000
REPORT = Path.home() / "Desktop" / "FlipTracker Bluetooth test.txt"

_lines: list[str] = []


def log(text: str = "") -> None:
    print(text, flush=True)
    _lines.append(text)


def hexs(data: bytes) -> str:
    return " ".join(f"{b:02x}" for b in data) or "(empty)"


def when(seconds: int) -> str:
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return f"(not a valid time: {seconds})"


def parse_entry(data: bytes) -> dict | None:
    """A history record: event number, side, start time, seconds. None if too short."""
    if len(data) < 17:
        return None
    side = data[4]
    return {
        "id": int.from_bytes(data[0:4], "big"),
        "side": side - 128 if side > 127 else side,
        "pause": side > 127,
        "start": int.from_bytes(data[5:13], "big"),
        "seconds": int.from_bytes(data[13:17], "big"),
    }


def is_end(data: bytes) -> bool:
    return len(data) >= 17 and not any(data[:17])


def describe(entry: dict) -> str:
    kind = "pause on side" if entry["pause"] else "side"
    return f"#{entry['id']}: {kind} {entry['side']}, {when(entry['start'])}, {entry['seconds']} seconds"


async def find_cube():
    from bleak import BleakScanner

    log(f"Looking for the cube for {SCAN_SECONDS} seconds. Give it a shake or a flip to wake it.")
    found = await BleakScanner.discover(timeout=SCAN_SECONDS, return_adv=True)
    cubes, others = [], []
    for device, adv in found.values():
        name = adv.local_name or device.name or ""
        services = [s.lower() for s in (adv.service_uuids or [])]
        if "timeflip" in name.lower() or SERVICE in services:
            cubes.append((device, name, adv.rssi))
        elif name:
            others.append(name)
    log(f"Bluetooth devices seen: {len(found)}")
    if not cubes:
        log("RESULT: no TimeFlip cube found.")
        log("Named devices seen: " + (", ".join(sorted(set(others))) or "none"))
        return None
    cubes.sort(key=lambda c: c[2] or -999, reverse=True)
    for device, name, rssi in cubes:
        log(f"Found: {name!r}, signal {rssi}")
    return cubes[0][0]


async def attempt(results: dict, key: str, label: str, action):
    """Run one step. A failure is recorded and the test carries on."""
    try:
        value = await action()
        results[key] = True
        return value
    except Exception as exc:  # each step is independent; record and continue
        results[key] = False
        log(f"{label}: FAILED ({type(exc).__name__}: {exc})")
        return None


async def read_history(client, results: dict) -> list[dict]:
    queue: asyncio.Queue = asyncio.Queue()

    def on_packet(_sender, data):
        queue.put_nowait(bytes(data))

    await client.start_notify(HISTORY, on_packet)
    entries, packets, ended = [], 0, False
    try:
        await client.write_gatt_char(HISTORY, bytes([0x02, 0, 0, 0, 0]), response=True)
        while packets < HISTORY_MAX_PACKETS:
            try:
                data = await asyncio.wait_for(queue.get(), HISTORY_IDLE_SECONDS)
            except asyncio.TimeoutError:
                break
            packets += 1
            if packets <= 3:
                log(f"  raw packet {packets}: {hexs(data)}")
            if is_end(data):
                ended = True
                log(f"  end packet: {hexs(data)}")
                break
            entry = parse_entry(data)
            if entry:
                entries.append(entry)
    finally:
        await client.stop_notify(HISTORY)
    results["history_ended_cleanly"] = ended
    log(f"History packets received: {packets}. Records: {len(entries)}. Clean end marker: {'yes' if ended else 'no'}")
    return entries


async def test_cube(client, password: bytes) -> dict:
    results: dict = {}

    async def firmware():
        log("Firmware: " + bytes(await client.read_gatt_char(FIRMWARE)).decode("ascii", "replace").strip("\x00"))

    async def battery():
        log(f"Battery: {bytes(await client.read_gatt_char(BATTERY))[0]}%")

    async def sign_in():
        await client.write_gatt_char(PASSWORD, password, response=True)
        result = bytes(await client.read_gatt_char(RESULT))
        events = bytes(await client.read_gatt_char(EVENTS))
        log(f"Password result bytes: {hexs(result)}")
        log("Cube says: " + events.decode("ascii", "replace").strip("\x00 "))
        results["password_ok"] = b"password ok" in events.lower()

    async def state():
        log(f"System state bytes: {hexs(bytes(await client.read_gatt_char(STATE)))}")
        log(f"Side facing up: {bytes(await client.read_gatt_char(FACET))[0]}")

    async def clock():
        await client.write_gatt_char(COMMAND, bytes([0x07]), response=True)
        data = bytes(await client.read_gatt_char(RESULT))
        log(f"Clock bytes: {hexs(data)}")
        if len(data) >= 9 and data[0] == 0x07:
            cube = int.from_bytes(data[1:9], "big")
            drift = cube - int(datetime.now(timezone.utc).timestamp())
            log(f"Cube clock: {when(cube)} ({drift:+d} seconds against this Mac)")

    async def status():
        await client.write_gatt_char(COMMAND, bytes([0x10]), response=True)
        log(f"Lock/pause/auto-pause bytes: {hexs(bytes(await client.read_gatt_char(RESULT)))}")

    async def last_event():
        await client.write_gatt_char(HISTORY, bytes([0x01, 0xFF, 0xFF, 0xFF, 0xFF]), response=True)
        data = bytes(await client.read_gatt_char(HISTORY))
        log(f"Last event bytes: {hexs(data)}")
        entry = parse_entry(data)
        if entry:
            log("Last event: " + describe(entry))

    async def history():
        log("Reading stored history (can take a minute)...")
        entries = await read_history(client, results)
        results["history_count"] = len(entries)
        if entries:
            log("Oldest: " + describe(entries[0]))
            log("Newest: " + describe(entries[-1]))
            log("Last 10 records:")
            for entry in entries[-10:]:
                log("  " + describe(entry))

    await attempt(results, "firmware", "Firmware", firmware)
    await attempt(results, "battery", "Battery", battery)
    await attempt(results, "sign_in", "Sign in", sign_in)
    await attempt(results, "state", "System state", state)
    await attempt(results, "clock", "Clock", clock)
    await attempt(results, "status", "Status", status)
    await attempt(results, "last_event", "Last event", last_event)
    await attempt(results, "history", "History", history)
    return results


def summarise(results: dict) -> None:
    yes = lambda key: "YES" if results.get(key) else "NO"  # noqa: E731
    log()
    log("===== SUMMARY =====")
    log(f"Found the cube:      {yes('found')}")
    log(f"Connected:           {yes('connected')}")
    log(f"Password accepted:   {yes('password_ok')}")
    log(f"Read the clock:      {yes('clock')}")
    log(f"History records:     {results.get('history_count', 0)}")
    log(f"History ended clean: {yes('history_ended_cleanly')}")


async def main() -> int:
    from bleak import BleakClient

    log(f"FlipTracker Bluetooth test, {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    log(f"Python {sys.version.split()[0]}, macOS test")
    results: dict = {}
    password = os.environ.get("CUBE_PASSWORD", "000000").encode("ascii")
    try:
        device = await find_cube()
        results["found"] = device is not None
        if device is not None:
            log("Connecting...")
            async with BleakClient(device, timeout=30) as client:
                results["connected"] = True
                log("Connected.")
                results.update(await test_cube(client, password))
            log("Disconnected.")
    except Exception as exc:  # report whatever went wrong rather than a traceback
        log(f"STOPPED: {type(exc).__name__}: {exc}")
        log("If this mentions Bluetooth being off or not authorised: System Settings, "
            "Privacy & Security, Bluetooth, switch Terminal on, then run this again.")
    summarise(results)
    try:
        REPORT.write_text("\n".join(_lines) + "\n")
        print(f"\nSaved to: {REPORT}")
    except OSError as exc:
        print(f"\nCould not save the report ({exc}). Copy the text above instead.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
