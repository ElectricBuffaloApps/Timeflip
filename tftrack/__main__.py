"""Command line: python3 -m tftrack <command>. Most people only use the installer and the web page."""
from __future__ import annotations

import argparse
import json
import plistlib
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import config as cfg
from . import service
from .api import AuthError
from .limits import format_hm

AGENTS = {
    "com.tftrack.check": {"args": ["check"], "StartInterval": 300, "RunAtLoad": True},
    "com.tftrack.web": {"args": ["serve"], "KeepAlive": True, "RunAtLoad": True},
}
AGENT_DIR = Path.home() / "Library" / "LaunchAgents"
SHORTCUT = Path.home() / "Desktop" / "TimeFlip Tracker.webloc"


def cmd_install(args) -> None:
    """Save the account, test it, start both background jobs and put a shortcut on the Desktop."""
    conf = cfg.load_config()
    if args.reuse:  # updating: keep the saved account
        if not conf.get("email"):
            sys.exit("No saved account.")
        password = cfg.get_password(conf["email"])
    else:
        password = sys.stdin.readline().rstrip("\n")
        conf["email"] = args.email.strip()
    try:
        service.TimeFlipClient(conf["email"], password).login()
    except AuthError as e:
        sys.exit(f"{e}")
    except Exception as e:
        sys.exit(f"Couldn't reach TimeFlip: {e}")
    cfg.save_config(conf)
    cfg.save_password(conf["email"], password)
    try:
        service.sync(conf, service.open_store())
    except Exception as e:
        print(f"First sync failed (it will retry): {e}")

    project_dir = Path(__file__).resolve().parent.parent
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    for label, spec in AGENTS.items():
        path = AGENT_DIR / f"{label}.plist"
        plist = {
            "Label": label,
            "ProgramArguments": [sys.executable, "-m", "tftrack", *spec["args"]],
            "WorkingDirectory": str(project_dir),
            "StandardOutPath": str(cfg.APP_DIR / f"{spec['args'][0]}.log"),
            "StandardErrorPath": str(cfg.APP_DIR / f"{spec['args'][0]}.log"),
            **{k: v for k, v in spec.items() if k != "args"},
        }
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        with open(path, "wb") as f:
            plistlib.dump(plist, f)
        subprocess.run(["launchctl", "load", str(path)], check=True)

    with open(SHORTCUT, "wb") as f:
        plistlib.dump({"URL": f"http://127.0.0.1:{cfg.PORT}/"}, f)
    print("Installed.")


def cmd_uninstall(_args) -> None:
    for label in AGENTS:
        path = AGENT_DIR / f"{label}.plist"
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        path.unlink(missing_ok=True)
    conf = cfg.load_config()
    if conf.get("email"):
        cfg.delete_password(conf["email"])
    SHORTCUT.unlink(missing_ok=True)
    print("Removed.")


def cmd_serve(_args) -> None:
    from .web import serve
    serve()


def cmd_check(_args) -> None:
    for title in service.run_check(cfg.load_config(), service.open_store()):
        print(f"{datetime.now():%Y-%m-%d %H:%M} alerted: {title}")


def cmd_sync(_args) -> None:
    store = service.open_store()
    t, i = service.sync(cfg.load_config(), store)
    print(f"Synced {t} tasks and {i} intervals ({store.get_meta('unmatched_intervals')} not linked to a task).")


def cmd_status(_args) -> None:
    conf, store = cfg.load_config(), service.open_store()
    print(f"Last sync: {store.last_sync() or 'never'}")
    for st in service.statuses(conf, store):
        lim = st.limit
        print(f"{lim.label:<24} {lim.period:<7} {format_hm(st.seconds):>9} of {lim.hours:g}h  ({st.percent:.0f}%)")


def cmd_probe(_args) -> None:
    """Show the shape of the data the API returns, so units and formats can be checked."""
    data = service.client(cfg.load_config()).sync_all()
    tasks, intervals = data.get("tasks") or [], data.get("timeIntervals") or []
    print(f"Top-level keys: {sorted(data)}")
    print(f"{len(tasks)} tasks, {len(intervals)} intervals")
    print("Sample task:", json.dumps(tasks[:1], indent=2, default=str))
    recent = sorted(intervals, key=lambda i: str(i.get("startedAt")))[-3:]
    print("Latest 3 intervals:", json.dumps(recent, indent=2, default=str))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="tftrack", description="TimeFlip limits, alerts and reports")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install", help="sign in (password on stdin) and start the background jobs")
    i.add_argument("--email", default="")
    i.add_argument("--reuse", action="store_true", help="keep the saved email and password (for updates)")
    i.set_defaults(fn=cmd_install)
    for name, fn, help_ in [
        ("uninstall", cmd_uninstall, "stop the background jobs and forget the password"),
        ("serve", cmd_serve, "run the web page on http://127.0.0.1:8765"),
        ("check", cmd_check, "sync and send any due notifications"),
        ("sync", cmd_sync, "download tasks and intervals"),
        ("status", cmd_status, "show progress against each limit"),
        ("probe", cmd_probe, "print a sample of the raw API data"),
    ]:
        sub.add_parser(name, help=help_).set_defaults(fn=fn)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
