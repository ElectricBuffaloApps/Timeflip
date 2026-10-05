"""Self-update from the GitHub repository (public, so no sign-in is needed).

The installed copy lives in ~/Library/Application Support/TimeFlip Tracker/tftrack. An update downloads the
branch as a ZIP, swaps the tftrack folder for the new one and restarts the web server in place. The background
check starts a fresh Python each run, so it picks up the new code by itself.
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

REPO = "ElectricBuffaloApps/Timeflip"
BRANCH = "claude/modest-wozniak-j8obe2"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/tftrack/VERSION"
ZIP_URL = f"https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}"

CODE_DIR = Path(__file__).resolve().parent
INSTALL_DIR = CODE_DIR.parent
EXTRA_FILES = ["Uninstall TimeFlip Tracker.command"]


def current_version() -> int:
    return int((CODE_DIR / "VERSION").read_text().strip())


def latest_version() -> int:
    with urllib.request.urlopen(f"{VERSION_URL}?t={int(time.time())}", timeout=15) as r:
        return int(r.read().decode().strip())


def is_installed_copy() -> bool:
    """Only replace the copy the installer made, never a developer's checkout."""
    return "Application Support" in str(INSTALL_DIR)


def check() -> dict:
    current = current_version()
    try:
        latest = latest_version()
    except Exception as e:
        return {"current": current, "latest": None, "available": False, "error": f"Couldn't check: {e}"}
    return {"current": current, "latest": latest, "available": latest > current, "error": None}


def _find_root(zf: zipfile.ZipFile) -> str:
    for name in zf.namelist():
        if name.endswith("tftrack/__main__.py"):
            return name[: -len("tftrack/__main__.py")]
    raise RuntimeError("The download doesn't look like TimeFlip Tracker.")


def install_zip(data: bytes, install_dir: Path = INSTALL_DIR) -> int:
    """Unpack the downloaded ZIP's tftrack folder over the installed one. Returns the new version."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        root = _find_root(zf)
        with tempfile.TemporaryDirectory(dir=install_dir) as tmp:
            staged = Path(tmp)
            for name in zf.namelist():
                rel = name[len(root):]
                if name.endswith("/") or not (rel.startswith("tftrack/") or rel in EXTRA_FILES):
                    continue
                target = staged / rel
                if staged.resolve() not in target.resolve().parents:
                    continue  # ignore anything trying to escape the folder
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(name))
            new_code = staged / "tftrack"
            new_version = int((new_code / "VERSION").read_text().strip())
            compile((new_code / "__main__.py").read_text(), "__main__.py", "exec")  # sanity check

            old = install_dir / "tftrack.old"
            shutil.rmtree(old, ignore_errors=True)
            live = install_dir / "tftrack"
            if live.exists():
                live.rename(old)
            new_code.rename(live)
            shutil.rmtree(old, ignore_errors=True)
            for extra in EXTRA_FILES:
                if (staged / extra).exists():
                    shutil.copyfile(staged / extra, install_dir / extra)
                    os.chmod(install_dir / extra, 0o755)
    return new_version


def update() -> int:
    if not is_installed_copy():
        raise RuntimeError("Updating only works on the installed copy.")
    with urllib.request.urlopen(ZIP_URL, timeout=60) as r:
        data = r.read()
    return install_zip(data)


def restart_soon(delay: float = 1.0) -> None:
    """Replace this process with a fresh server running the new code (after the reply has been sent)."""
    def go():
        time.sleep(delay)
        os.chdir(INSTALL_DIR)
        os.execv(sys.executable, [sys.executable, "-m", "tftrack", "serve"])
    threading.Thread(target=go, daemon=True).start()
