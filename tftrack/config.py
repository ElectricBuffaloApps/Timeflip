"""Config file, paths and password storage (macOS Keychain)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

APP_DIR = Path(os.environ.get("TFTRACK_HOME", Path.home() / ".tftrack"))
CONFIG_PATH = APP_DIR / "config.json"
DB_PATH = APP_DIR / "tftrack.sqlite"
KEYCHAIN_SERVICE = "tftrack-timeflip"

DEFAULT_CONFIG = {
    "email": "",
    "week_starts": "monday",
    "alert_levels_percent": [80, 100],
    "duration_unit": "seconds",
    "limits": [],
    "auto_update": True,
    # Tracked entries shorter than this aren't counted or charged (the cube knocked, or flipped by mistake).
    "min_entry_minutes": 5,
    "last_seen_version": None,
    # How each client (TimeFlip task tag) is invoiced: "hourly", "day" (monthly, by day) or "week"
    # (weeks whose Monday is in the month). Day totals can be rounded to a block of minutes.
    "clients": {
        "HTB": {"billing": "day", "rounding": "up", "block_minutes": 15},
        "FCY": {"billing": "week", "rounding": "none", "block_minutes": 15},
    },
}
PORT = 8765


def load_config() -> dict:
    conf = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            conf.update(json.load(f))
    return migrate(conf)


def migrate(conf: dict) -> dict:
    """Older versions kept monthly allowances apart from limits; they're now a tick box on a task's limit."""
    for old in ("allowance_choices", "allowance_charged"):   # settings from the old allowance table
        conf.pop(old, None)
    legacy = conf.pop("allowances", None)
    if not legacy:
        return conf
    limits = [dict(l) for l in conf.get("limits") or []]
    for a in legacy:
        fields = {"weekly_hours": float(a["hours_per_week"]), "allowance": True,
                  "allowance_start": a["start"], "opening_hours": float(a.get("opening_hours") or 0)}
        match = next((l for l in limits if (l.get("task") or "").lower() == a["task"].lower()), None)
        if match:
            match.update(fields)
        else:
            limits.append({"task": a["task"], **fields})
    conf["limits"] = limits
    return conf


BACKUPS_KEPT = 30


def backup_config(today=None) -> Path | None:
    """Keep a copy of the settings as they stood at the start of each day they change (the last 30 such days), so
    anything overwritten by mistake can be put back. Returns the copy made, or None if today's already exists."""
    from datetime import date
    if not CONFIG_PATH.exists():
        return None
    target = APP_DIR / "settings-backups" / f"config-{(today or date.today()).isoformat()}.json"
    if target.exists():
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CONFIG_PATH, target)
        for old in sorted(target.parent.glob("config-*.json"))[:-BACKUPS_KEPT]:
            old.unlink()
    except OSError:
        return None   # a backup that can't be written must never stop a save
    return target


def save_config(conf: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    backup_config()
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(conf, indent=2) + "\n")
    tmp.replace(CONFIG_PATH)


def save_password(email: str, password: str) -> None:
    """Store the password in the login Keychain (read back with the same tool, so no access prompt)."""
    if not shutil.which("security"):
        raise RuntimeError("macOS 'security' tool not found; set TIMEFLIP_PASSWORD instead.")
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", email, "-w", password],
        check=True, capture_output=True,
    )


def delete_password(email: str) -> None:
    if shutil.which("security"):
        subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", email],
                       capture_output=True)


def get_password(email: str) -> str:
    if os.environ.get("TIMEFLIP_PASSWORD"):
        return os.environ["TIMEFLIP_PASSWORD"]
    if shutil.which("security"):
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", email, "-w"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return r.stdout.rstrip("\n")
    raise RuntimeError("No TimeFlip password stored. Run the installer again.")
