"""A small "TimeFlip Tracker" app for Applications, Spotlight and the Dock.

It is a plain app bundle whose program is a shell script: it makes sure the tracker's web server is running and
opens the page in the default browser. It is built on the Mac itself (so it carries no download quarantine) and
only ever created, never modified, once it exists.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import config as cfg

APP_NAME = "TimeFlip Tracker.app"
ICON = Path(__file__).parent / "static" / "AppIcon.icns"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
              "/Support/lsregister")

LAUNCHER = f"""#!/bin/bash
# Opens TimeFlip Tracker, starting its web server first if it isn't running.
URL="http://127.0.0.1:{cfg.PORT}/"
if ! /usr/bin/curl -s -o /dev/null --max-time 2 "$URL"; then
  /bin/launchctl kickstart "gui/$(/usr/bin/id -u)/com.tftrack.web" >/dev/null 2>&1
  for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    /usr/bin/curl -s -o /dev/null --max-time 1 "$URL" && break
  done
fi
/usr/bin/open "$URL"
"""


def candidate_dirs() -> list[Path]:
    return [Path("/Applications"), Path.home() / "Applications"]


def find_app() -> Path | None:
    for d in candidate_dirs():
        if (d / APP_NAME).exists():
            return d / APP_NAME
    return None


def build_app(target_dir: Path) -> Path:
    app = target_dir / APP_NAME
    staging = target_dir / (APP_NAME + ".tmp")
    shutil.rmtree(staging, ignore_errors=True)
    (staging / "Contents" / "MacOS").mkdir(parents=True)
    (staging / "Contents" / "Resources").mkdir(parents=True)
    with open(staging / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({
            "CFBundleName": "TimeFlip Tracker",
            "CFBundleDisplayName": "TimeFlip Tracker",
            "CFBundleIdentifier": "com.tftrack.app",
            "CFBundleExecutable": "launcher",
            "CFBundleIconFile": "AppIcon",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1",
            "LSUIElement": True,  # opens the browser and quits, so no lingering Dock icon
        }, f)
    launcher = staging / "Contents" / "MacOS" / "launcher"
    launcher.write_text(LAUNCHER)
    os.chmod(launcher, 0o755)
    shutil.copyfile(ICON, staging / "Contents" / "Resources" / "AppIcon.icns")
    staging.rename(app)
    return app


def ensure_app() -> Path | None:
    """Create the app on a Mac if it isn't in /Applications or ~/Applications yet. Returns its path."""
    if sys.platform != "darwin":
        return None
    existing = find_app()
    if existing:
        return existing
    for d in candidate_dirs():
        try:
            d.mkdir(exist_ok=True)
            app = build_app(d)
        except OSError:
            continue
        subprocess.run([LSREGISTER, "-f", str(app)], capture_output=True)
        return app
    return None


def remove_app() -> None:
    for d in candidate_dirs():
        shutil.rmtree(d / APP_NAME, ignore_errors=True)
