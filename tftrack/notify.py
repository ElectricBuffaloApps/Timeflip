"""macOS notifications via osascript."""
from __future__ import annotations

import shutil
import subprocess


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, message: str, sound: bool = True) -> None:
    if not shutil.which("osascript"):
        print(f"[notification] {title}: {message}")
        return
    script = f"display notification {_quote(message)} with title {_quote(title)}"
    if sound:
        script += ' sound name "Glass"'
    subprocess.run(["osascript", "-e", script], check=False)
