"""Command line: python3 -m tftrack <command>"""
from __future__ import annotations

import argparse
import json
import plistlib
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import config as cfg
from .api import TimeFlipClient
from .limits import alerts_due, evaluate, format_hm, parse_limits
from .notify import notify
from .store import Store

AGENT_LABEL = "com.tftrack.check"
AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def _client(conf: dict) -> TimeFlipClient:
    return TimeFlipClient(conf["email"], cfg.get_password(conf["email"]))


def cmd_setup(_args) -> None:
    if cfg.write_default_config():
        print(f"Created {cfg.CONFIG_PATH} — edit your email and limits there.")
    conf = cfg.load_config()
    if conf["email"] == cfg.DEFAULT_CONFIG["email"]:
        print("Set your TimeFlip email in the config file, then run setup again.")
        return
    print(f"Storing the TimeFlip password for {conf['email']} in your Keychain.")
    cfg.save_password(conf["email"])
    _client(conf).login()
    print("Signed in to TimeFlip successfully.")


def cmd_probe(_args) -> None:
    """Show the shape of the data the API returns, so units and formats can be checked."""
    conf = cfg.load_config()
    data = _client(conf).sync_all()
    tasks, intervals = data.get("tasks") or [], data.get("timeIntervals") or []
    print(f"Top-level keys: {sorted(data)}")
    print(f"{len(tasks)} tasks, {len(intervals)} intervals")
    print("Sample task:", json.dumps(tasks[:1], indent=2, default=str))
    recent = sorted(intervals, key=lambda i: str(i.get("startedAt")))[-3:]
    print("Latest 3 intervals:", json.dumps(recent, indent=2, default=str))


def _sync(conf: dict, store: Store) -> tuple[int, int]:
    data = _client(conf).sync_all()
    return store.replace_all(data, conf.get("duration_unit", "seconds"))


def cmd_sync(_args) -> None:
    conf = cfg.load_config()
    t, i = _sync(conf, Store(cfg.DB_PATH))
    print(f"Synced {t} tasks and {i} intervals.")


def _statuses(conf: dict, store: Store):
    return evaluate(parse_limits(conf), store.tasks(), store.seconds_by_task,
                    datetime.now().astimezone(), conf.get("week_starts", "monday"))


def cmd_status(args) -> None:
    conf = cfg.load_config()
    store = Store(cfg.DB_PATH)
    if not args.offline:
        _sync(conf, store)
    print(f"Last sync: {store.last_sync() or 'never'}")
    for st in _statuses(conf, store):
        lim = st.limit
        print(f"{lim.label:<24} {lim.period:<7} {format_hm(st.seconds):>9} of {lim.hours:g}h  ({st.percent:.0f}%)")
    unknown = _unmatched(conf, store)
    if unknown:
        print("\nWarning: no TimeFlip task/client matches:", ", ".join(unknown))


def _unmatched(conf: dict, store: Store) -> list[str]:
    tasks = store.tasks()
    names = {t["name"].strip().lower() for t in tasks}
    tags = {(t["tag"] or "").strip().lower() for t in tasks}
    out = []
    for lim in parse_limits(conf):
        if lim.task and lim.task.strip().lower() not in names:
            out.append(f"task '{lim.task}'")
        if lim.client and lim.client.strip().lower() not in tags:
            out.append(f"client '{lim.client}'")
    return sorted(set(out))


def cmd_check(_args) -> None:
    """Sync, then notify for any limit that has newly crossed an alert level."""
    conf = cfg.load_config()
    store = Store(cfg.DB_PATH)
    try:
        _sync(conf, store)
    except Exception as e:  # keep checking against the last good copy
        print(f"{datetime.now():%Y-%m-%d %H:%M} sync failed: {e}", file=sys.stderr)
    levels = conf.get("alert_levels_percent", [80, 100])
    for st, level in alerts_due(_statuses(conf, store), levels, store.alert_already_sent):
        lim = st.limit
        when = "today" if lim.period == "daily" else "this week"
        if level >= 100:
            title = f"Limit reached: {lim.label}"
        else:
            title = f"{level}% of limit: {lim.label}"
        notify(title, f"{format_hm(st.seconds)} of {lim.hours:g}h {when}.")
        for lv in levels:
            if lv <= level:
                store.mark_alert_sent(lim.key, st.period_start.isoformat(), lv)
        print(f"{datetime.now():%Y-%m-%d %H:%M} alerted: {title}")


def cmd_install_agent(args) -> None:
    project_dir = Path(__file__).resolve().parent.parent
    cfg.APP_DIR.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [sys.executable, "-m", "tftrack", "check"],
        "WorkingDirectory": str(project_dir),
        "StartInterval": args.minutes * 60,
        "RunAtLoad": True,
        "StandardOutPath": str(cfg.APP_DIR / "check.log"),
        "StandardErrorPath": str(cfg.APP_DIR / "check.log"),
    }
    AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "unload", str(AGENT_PATH)], capture_output=True)
    with open(AGENT_PATH, "wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "load", str(AGENT_PATH)], check=True)
    print(f"Installed: checking every {args.minutes} minutes. Log: {cfg.APP_DIR / 'check.log'}")


def cmd_uninstall_agent(_args) -> None:
    subprocess.run(["launchctl", "unload", str(AGENT_PATH)], capture_output=True)
    AGENT_PATH.unlink(missing_ok=True)
    print("Background check removed.")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="tftrack", description="TimeFlip limits and alerts")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="create config and store your password").set_defaults(fn=cmd_setup)
    sub.add_parser("probe", help="print a sample of the raw API data").set_defaults(fn=cmd_probe)
    sub.add_parser("sync", help="download tasks and intervals").set_defaults(fn=cmd_sync)
    s = sub.add_parser("status", help="show progress against each limit")
    s.add_argument("--offline", action="store_true", help="use the last synced data")
    s.set_defaults(fn=cmd_status)
    sub.add_parser("check", help="sync and send any due notifications").set_defaults(fn=cmd_check)
    a = sub.add_parser("install-agent", help="run 'check' in the background on a timer")
    a.add_argument("--minutes", type=int, default=5)
    a.set_defaults(fn=cmd_install_agent)
    sub.add_parser("uninstall-agent", help="stop the background check").set_defaults(fn=cmd_uninstall_agent)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
