"""Optional status push to the HA Watcher companion integration (payload contract v1).

Every ``home_assistant_push.every_seconds`` the watcher POSTs a small JSON report to the
webhook URL the integration shows, so Home Assistant can show the watcher's checks as
entities. It is informational only: a failed push is normal (Home Assistant being down is
what the watcher is for), is logged once per failure streak, and never touches alerting.
The URL is a secret and is never logged.

The contract is docs/PAYLOAD.md in github.com/BrawnyBravo/ha-watcher-companion.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from ha_watcher import __version__
from ha_watcher.config import Config
from ha_watcher.deadline import Bounded
from ha_watcher.state import parse, ts

if TYPE_CHECKING:  # pragma: no cover
    from ha_watcher.engine import Watcher

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
MAX_CHECKS = 32
MAX_DETAIL = 1024
MAX_TEXT = 128
MAX_COUNT = 10**9  # alerts_sent_24h
MAX_FAILURES = 10**6
MAX_INTERVAL = 86400
NAME_LEN = 40
OUTAGE_KEYS = ("api", "core")  # Home Assistant itself down, not just any alert
_NOT_NAME = re.compile(r"[^a-z0-9_]")
# Keys only reported when a check cannot run at all (its own error or a crash).
ERROR_ONLY = {"supervisor", "entities"}


def _sha8(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def check_names(keys: list[str], taken: set[str] | None = None) -> dict[str, str]:
    """Map result keys to contract names: lowercase, [a-z0-9_] only, at most 40 characters.

    Keys are taken in sorted order; a long name, or one already taken by an earlier key,
    becomes its first 31 characters + "_" + the first 8 hex digits of sha256(key).
    """
    out: dict[str, str] = {}
    taken = set() if taken is None else taken
    for key in sorted(keys):
        name = _NOT_NAME.sub("_", key.lower())
        if len(name) > NAME_LEN or name in taken:
            name = f"{name[:31]}_{_sha8(key)}"
        if name in taken:
            continue  # two hash collisions in a row: practically impossible, but never send a duplicate
        taken.add(name)
        out[key] = name
    return out


def configured_keys(cfg: Config) -> list[str]:
    """Every result key the configured checks can ever report, sorted.

    Names are computed over this whole set, so a check keeps its name whichever other
    checks happen to be in a given report.
    """
    keys: set[str] = set()
    for name, c in cfg.checks.items():
        if not c.enabled:
            continue
        keys.add(name)  # also the key of a crashed check
        o = c.options
        if name == "core" and o["alert_on_safe_mode"]:
            keys.add("core:safe_mode")
        if name == "supervisor":
            if o["alert_on_unhealthy"]:
                keys.add("supervisor:health")
            keys.update(f"supervisor:addon:{slug}" for slug in o["addons"])
        if name == "entities":
            keys.update(f"entity:{e}" for e in o["entity_ids"])
    return sorted(keys)


def _count(n: int, high: int = MAX_COUNT) -> int:
    return max(0, min(int(n), high))


def _latest(stamps: list[str]) -> str | None:
    times = [parse(s) for s in stamps]
    return ts(max(t for t in times if t)) if times else None


def build_payload(w: Watcher, now: datetime, sent_at: datetime | None = None) -> tuple[dict[str, Any], bytes]:
    """The report and its exact JSON bytes, kept under the 64 KiB limit."""
    cfg = w.cfg.home_assistant_push
    states = w.state["checks"]
    configured = configured_keys(w.cfg)
    names = check_names(configured)
    names.update(check_names([k for k in states if k not in names], set(names.values())))
    entries: dict[str, dict[str, Any]] = {}
    for key, st in states.items():
        entries[key] = {
            "ok": bool(st["ok"]),
            "since": st.get("since") or (st["first_failure"] if not st["ok"] else None),
            "detail": st["message"],
            "failures": _count(st["failures"], MAX_FAILURES),
        }
    for key in configured:
        if key not in entries and key not in ERROR_ONLY:
            entries[key] = {"ok": None, "since": None, "detail": "not run yet", "failures": 0}
    # Sorted and cut to 32: the same checks every time, because the integration creates
    # at most 32 entities per install, ever.
    keys = sorted(k for k in entries if k in names)
    checks: dict[str, dict[str, Any]] = {}
    for key in keys[:MAX_CHECKS]:
        entry = dict(entries[key])
        if names[key] != _NOT_NAME.sub("_", key.lower()):
            entry["detail"] = f"{key}: {entry['detail']}"  # the name was shortened; keep the original
        entry["detail"] = entry["detail"][:MAX_DETAIL]
        checks[names[key]] = entry
    down = [st for k, st in states.items() if k in OUTAGE_KEYS and st["alerting"] and st["first_failure"]]
    day_ago = now - timedelta(hours=24)
    sent_24h = [t for t in w.state["alerts_24h"] if parse(t) > day_ago]
    w.state["alerts_24h"] = sent_24h
    payload: dict[str, Any] = {
        "v": 1,
        "watcher_id": cfg.watcher_id[:MAX_TEXT],
        "version": __version__[:MAX_TEXT],
        "sent_at": ts(sent_at or now),
        "interval_s": max(1, _count(cfg.every_seconds, MAX_INTERVAL)),
        "checks": checks,
        "outage": {
            "active": bool(down),
            "since": ts(min(parse(st["first_failure"]) for st in down)) if down else None,
        },
        "alerts_sent_24h": _count(len(sent_24h)),
        "last_power_cycle": _latest(w.state["power"]["cycles"]),
        "last_recovery_command": _latest(w.state["recovery"]["runs"]),
    }
    body = _encode(payload)
    for limit in (256, 64, 0):  # only reachable with very long non-ASCII details
        if len(body) <= MAX_BODY:
            break
        for entry in checks.values():
            entry["detail"] = entry["detail"][:limit]
        body = _encode(payload)
    return payload, body


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def signature(secret: str, stamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{stamp}.".encode() + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


class StatusPush:
    def __init__(self, w: Watcher, http: httpx.Client) -> None:
        self.w = w
        self.http = http
        self.last: datetime | None = None
        self.last_stamp: int | None = None
        self.failing = False
        self.clock: Callable[[], float] = time.time  # wall clock at the moment of sending
        self.limit = Bounded("status push")

    def run(self, now: datetime) -> None:
        cfg = self.w.cfg.home_assistant_push
        if not cfg.enabled or not cfg.url:
            return
        if self.last and timedelta(0) <= now - self.last < timedelta(seconds=cfg.every_seconds):
            return
        # sent_at and the signature use the time of sending, not the loop's start (a loop
        # with a restart command can take minutes), and must strictly increase: the
        # integration refuses a signed timestamp that is not newer than the last one.
        moment = self.clock()
        stamp = int(moment)
        if self.last_stamp is not None and stamp <= self.last_stamp:
            log.debug("status push skipped: one was already sent this second")
            return
        self.last, self.last_stamp = now, stamp
        _, body = build_payload(self.w, now, datetime.fromtimestamp(moment, UTC))
        headers = {"Content-Type": "application/json"}
        if cfg.secret:
            headers["X-HA-Watcher-Timestamp"] = str(stamp)
            headers["X-HA-Watcher-Signature"] = signature(cfg.secret, stamp, body)
        try:
            # timeout_seconds bounds the whole request, not just each read.
            r = self.limit.call(
                cfg.timeout_seconds,
                self.http.post,
                cfg.url,
                content=body,
                headers=headers,
                timeout=cfg.timeout_seconds,
            )
            problem = None if r.status_code < 400 else f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001 - only the type: the URL is a secret
            problem = type(exc).__name__
        if problem is None:
            if self.failing:
                log.info("status push to Home Assistant works again")
            self.failing = False
        elif not self.failing:
            self.failing = True
            log.warning("status push to Home Assistant failed (%s); not logged again until it works", problem)
        else:
            log.debug("status push to Home Assistant failed (%s)", problem)
