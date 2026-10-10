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
        "power": {"cycles": [], "busy_seen": None, "busy_reason": None, "last_decision": None},
        "recovery": {"runs": [], "last_decision": None},
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
    base = empty_state()
    base.update(data)
    base["power"] = {**empty_state()["power"], **(data.get("power") or {})}
    base["recovery"] = {**empty_state()["recovery"], **(data.get("recovery") or {})}
    return base


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
