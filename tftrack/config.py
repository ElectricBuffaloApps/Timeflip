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
    "email": "you@example.com",
    "week_starts": "monday",
    "alert_levels_percent": [80, 100],
    "duration_unit": "seconds",
    "limits": [
        {"task": "Admin", "daily_hours": 2},
        {"client": "Acme Ltd", "weekly_hours": 10},
    ],
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"No config at {CONFIG_PATH}. Run: python3 -m tftrack setup")
    with open(CONFIG_PATH) as f:
        return json.load(f)


def write_default_config() -> bool:
    if CONFIG_PATH.exists():
        return False
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    return True


def save_password(email: str) -> None:
    """Prompt for the password and store it in the login Keychain.

    `-w` given last with no value makes `security` prompt, so the password never appears in argv.
    """
    if not shutil.which("security"):
        raise RuntimeError("macOS 'security' tool not found; set TIMEFLIP_PASSWORD instead.")
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", email, "-w"],
        check=True,
    )


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
    raise RuntimeError("No password stored. Run: python3 -m tftrack setup")
