"""A small JSON state file, written atomically so a power cut cannot corrupt it."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STATE_VERSION = 1


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "checks": {},
        "last_run": {},
        "alert_times": [],
        "suppressed": 0,
        "heartbeat_last": None,
        "deadman_last": None,
        "last_loop": None,
        # dry_* lists: what a dry run would have done, so it keeps the same cooldown and cap
        # (one message per would-be action) without counting against the real ones.
        "power": {
            "cycles": [],
            "dry_cycles": [],
            "busy_seen": None,
            "busy_reason": None,
            "last_decision": None,
        },
        "recovery": {"runs": [], "dry_runs": [], "last_decision": None},
        "expiry": {"items": {}, "rdap_servers": {}, "rdap_backoff_until": None},
        "phone": {"calls": [], "called_outage": None, "last_attempt": None, "last_decision": None},
    }


def load_state(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return empty_state()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            raise ValueError("unexpected state format")
    except (OSError, ValueError) as exc:
        log.warning("state file %s unreadable (%s); starting fresh", p, type(exc).__name__)
        return empty_state()
    return _sanitize(data)


CHECK_KEYS = {"check": str, "ok": bool, "failures": int, "alerting": bool, "message": str}
TIME_LISTS = {
    ("alert_times",),
    ("power", "cycles"),
    ("power", "dry_cycles"),
    ("recovery", "runs"),
    ("recovery", "dry_runs"),
    ("phone", "calls"),
}
TIMES = {
    ("deadman_last",),
    ("last_loop",),
    ("power", "busy_seen"),
    ("phone", "called_outage"),
    ("phone", "last_attempt"),
    ("expiry", "rdap_backoff_until"),
}


def _sanitize(data: dict[str, Any]) -> dict[str, Any]:
    """Fill missing parts from the defaults and reset parts of the wrong type.

    The file is ours, but it may come from an older or newer version or be edited by
    hand; a bad value must cost that one value, never crash every loop.
    """
    base = empty_state()
    for key, default in base.items():
        if key not in data or key == "version":
            continue
        value = data[key]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                log.warning("state file: %s has the wrong type; reset", key)
                continue
            if key in ("checks", "last_run"):
                base[key] = value
            else:
                base[key] = {**default, **{k: v for k, v in value.items() if _same_type(default.get(k), v)}}
        elif _same_type(default, value):
            base[key] = value
        else:
            log.warning("state file: %s has the wrong type; reset", key)
    base["checks"] = {
        k: v
        for k, v in base["checks"].items()
        if isinstance(v, dict)
        and all(isinstance(v.get(f), t) for f, t in CHECK_KEYS.items())
        and _time_or_none(v.get("first_failure"))
        and _time_or_none(v.get("last_alert"))
        and _time_or_none(v.get("held_since"))
        and _time_or_none(v.get("retry_at"))
    }
    base["last_run"] = {k: v for k, v in base["last_run"].items() if _aware(v)}
    for path in TIME_LISTS:
        holder = base if len(path) == 1 else base[path[0]]
        holder[path[-1]] = [t for t in holder[path[-1]] if _aware(t)]
    for path in TIMES:
        holder = base if len(path) == 1 else base[path[0]]
        if not _time_or_none(holder[path[-1]]):
            holder[path[-1]] = None
    return base


def _same_type(default: Any, value: Any) -> bool:
    if default is None:
        return True  # optional values: a timestamp, a reason, ...
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(default, bool) and isinstance(value, bool)
    return isinstance(value, type(default))


def _aware(value: Any) -> bool:
    """A timezone-aware ISO timestamp, as this module writes them."""
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _time_or_none(value: Any) -> bool:
    return value is None or _aware(value)


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ha-watcher-", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def ts(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
