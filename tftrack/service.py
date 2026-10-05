"""Shared actions used by the command line, the background check and the web page."""
from __future__ import annotations

from datetime import datetime, timezone

from . import config as cfg
from .api import TimeFlipClient
from .limits import alerts_due, evaluate, format_hm, parse_limits
from .notify import notify
from .store import Store


def open_store() -> Store:
    return Store(cfg.DB_PATH)


def client(conf: dict) -> TimeFlipClient:
    return TimeFlipClient(conf["email"], cfg.get_password(conf["email"]))


def sync(conf: dict, store: Store) -> tuple[int, int]:
    """Download everything; record any failure so the web page can show it."""
    try:
        result = store.replace_all(client(conf).sync_all(), conf.get("duration_unit", "seconds"))
    except Exception as e:
        store.set_meta("last_error", f"{datetime.now(timezone.utc).isoformat()}|{e}")
        raise
    store.set_meta("last_error", None)
    return result


def statuses(conf: dict, store: Store):
    return evaluate(parse_limits(conf), store.tasks(), store.seconds_by_task,
                    datetime.now().astimezone(), conf.get("week_starts", "monday"))


def run_check(conf: dict, store: Store) -> list[str]:
    """Sync, then notify for any limit that has newly crossed an alert level."""
    try:
        sync(conf, store)
    except Exception as e:  # keep checking against the last good copy
        print(f"{datetime.now():%Y-%m-%d %H:%M} sync failed: {e}")
    levels = conf.get("alert_levels_percent", [80, 100])
    sent = []
    for st, level in alerts_due(statuses(conf, store), levels, store.alert_already_sent):
        lim = st.limit
        when = "today" if lim.period == "daily" else "this week"
        title = f"Limit reached: {lim.label}" if level >= 100 else f"{level}% of limit: {lim.label}"
        notify(title, f"{format_hm(st.seconds)} of {lim.hours:g}h {when}.")
        for lv in levels:
            if lv <= level:
                store.mark_alert_sent(lim.key, st.period_start.isoformat(), lv)
        sent.append(title)
    try:
        from . import updater
        version = updater.maybe_auto_update(conf, store)
        if version:
            sent.append(f"updated to version {version}")
    except Exception as e:
        print(f"{datetime.now():%Y-%m-%d %H:%M} auto-update failed: {e}")
    return sent
