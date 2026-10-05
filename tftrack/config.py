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
    return conf


def save_config(conf: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
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
