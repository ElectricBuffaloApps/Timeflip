"""Reaching the tracker from Dave's other devices over Tailscale, and "viewer" Macs that show another Mac's tracker.

The tracker itself only ever listens on 127.0.0.1. Tailscale Serve (on the main Mac) gives it a private HTTPS address,
https://<this-mac>.<tailnet>.ts.net:8765/, that only devices signed in to the same Tailscale account can reach.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

from . import config as cfg

CLI_PATHS = ("/Applications/Tailscale.app/Contents/MacOS/Tailscale", "/usr/local/bin/tailscale",
             "/opt/homebrew/bin/tailscale")


def cli() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    return next((p for p in CLI_PATHS if os.path.exists(p)), None)


def _run(*args: str, timeout: float = 15) -> tuple[int, str]:
    path = cli()
    if not path:
        return 127, "Tailscale isn't installed on this Mac."
    # The App Store build is one binary for both the app and the command line; this makes it act as the CLI.
    env = dict(os.environ, TAILSCALE_BE_CLI="1")
    try:
        r = subprocess.run([path, *args], capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, out
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def dns_name() -> str | None:
    """This Mac's Tailscale name, e.g. 'daves-mac-studio.tail1234.ts.net', or None if Tailscale isn't running."""
    code, out = _run("status", "--json")
    if code != 0:
        return None
    try:
        name = (json.loads(out).get("Self") or {}).get("DNSName") or ""
    except ValueError:
        return None
    return name.rstrip(".") or None


def address(name: str) -> str:
    return f"https://{name}:{cfg.PORT}/"


def status() -> dict:
    conf = cfg.load_config()
    if not cli():
        return {"installed": False, "running": False, "serving": False, "url": None}
    name = dns_name()
    serving = False
    if name:
        code, out = _run("serve", "status", "--json")
        serving = code == 0 and f":{cfg.PORT}" in out and re.search(rf"(127\.0\.0\.1|localhost):{cfg.PORT}", out) is not None
        if name != (conf.get("remote") or {}).get("dns_name"):
            conf["remote"] = {"dns_name": name}
            cfg.save_config(conf)
    return {"installed": True, "running": bool(name), "serving": serving,
            "url": address(name) if name and serving else None, "name": name}


def enable() -> dict:
    """Ask Tailscale to serve the tracker privately over HTTPS. May need a one-off approval in the browser."""
    name = dns_name()
    if not name:
        raise ValueError("Tailscale isn't running or isn't signed in on this Mac. Open Tailscale and sign in first.")
    code, out = _run("serve", "--bg", f"--https={cfg.PORT}", f"http://127.0.0.1:{cfg.PORT}", timeout=20)
    link = re.search(r"https://login\.tailscale\.com/\S+", out)
    if link:   # HTTPS (or Serve) isn't switched on for the tailnet yet: Tailscale gives a link to approve it
        return {"needs_approval": link.group(0).rstrip(".,)"), "message": out.strip()}
    if code != 0:
        raise ValueError(f"Tailscale couldn't share the tracker: {out.strip() or code}")
    conf = cfg.load_config()
    conf["remote"] = {"dns_name": name}
    cfg.save_config(conf)
    return {"url": address(name)}


def disable() -> None:
    _run("serve", f"--https={cfg.PORT}", "off")


def allowed_hosts() -> set[str]:
    """Host headers the tracker accepts: this Mac itself, plus its own Tailscale name (never any other name)."""
    hosts = {f"127.0.0.1:{cfg.PORT}", f"localhost:{cfg.PORT}"}
    name = (cfg.load_config().get("remote") or {}).get("dns_name")
    if name:
        hosts |= {name, f"{name}:{cfg.PORT}"}
    return hosts


# ---------- viewer Macs ----------

def check_main(url: str) -> dict:
    """Confirm a URL is a TimeFlip tracker this Mac can reach. Returns its version."""
    url = url.strip()
    if not re.match(r"^https://[a-z0-9.-]+\.ts\.net(:\d+)?/?$", url, re.I):
        raise ValueError("Enter the main Mac's Tailscale address, like https://daves-mac.tail1234.ts.net:8765/")
    url = url.rstrip("/") + "/"
    try:
        with urllib.request.urlopen(url + "api/state", timeout=10) as r:
            data = json.loads(r.read())
    except Exception as e:
        raise ValueError(f"Couldn't reach the tracker at {url} ({e}). Is Tailscale on here and on the main Mac?")
    if "version" not in data or "client_billing" not in data:
        raise ValueError(f"{url} answered, but it isn't a TimeFlip tracker.")
    return {"url": url, "version": data["version"]}


def make_viewer(url: str) -> dict:
    """Turn this Mac into a viewer of the main Mac's tracker. Its own data is copied to a backup folder first."""
    main = check_main(url)
    backup = Path.home() / f".tftrack-backup-{datetime.now():%Y-%m-%d-%H%M}"
    shutil.copytree(cfg.APP_DIR, backup, ignore=shutil.ignore_patterns("*.log"))
    conf = cfg.load_config()
    conf["viewer_of"] = main["url"]
    cfg.save_config(conf)
    return {"url": main["url"], "backup": str(backup)}


def stop_viewer() -> None:
    conf = cfg.load_config()
    conf.pop("viewer_of", None)
    cfg.save_config(conf)
