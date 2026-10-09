"""The individual health checks.

Every check takes the HA client, its options and a context, and returns a list of
``Result`` objects. Most checks return one result whose key is the check name;
checks that watch several things (entities, add-ons) return one result per thing,
so each gets its own debounce and its own recovery message.
"""

from __future__ import annotations

import platform
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ha_watcher.ha import HAAuthError, HAClient, HAError

UNAVAILABLE = {"unavailable", "unknown"}
BACKUP_BUSY_STATES = {"create_backup", "receive_backup", "restore_backup"}


@dataclass
class Result:
    key: str
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Context:
    now: datetime
    # Set by the engine; the host check uses these hooks so tests can stub the network.
    tcp_connect: Callable[[str, int, float], None] | None = None
    ping: Callable[[str, float], bool] | None = None


def parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp from HA; naive values are treated as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def fmt_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 90 * 60:
        return f"{seconds // 60}m"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


# --------------------------------------------------------------------------- checks


def check_api(client: HAClient, opts: dict[str, Any], ctx: Context) -> list[Result]:
    try:
        r = client.get("/api/")
    except HAAuthError as exc:
        return [Result("api", False, f"Token rejected: {exc}")]
    except HAError as exc:
        return [Result("api", False, f"API unreachable: {exc}")]
    if r.status_code != 200:
        return [Result("api", False, f"API returned HTTP {r.status_code}")]
    ms = round(r.elapsed_ms)
    limit = opts["max_response_ms"]
    if ms > limit:
        return [Result("api", False, f"API slow: {ms} ms (limit {limit} ms)", {"ms": ms})]
    return [Result("api", True, f"API OK in {ms} ms", {"ms": ms})]


def check_core(client: HAClient, opts: dict[str, Any], ctx: Context) -> list[Result]:
    try:
        cfg = client.get_json("/api/config")
    except HAError as exc:
        return [Result("core", False, f"Cannot read /api/config: {exc}")]
    state = cfg.get("state")
    version = cfg.get("version", "?")
    out = [
        Result(
            "core",
            state == "RUNNING",
            f"Core state is {state} (version {version})",
            {"state": state, "version": version},
        )
    ]
    if opts["alert_on_safe_mode"]:
        flags = [f for f in ("safe_mode", "recovery_mode") if cfg.get(f)]
        msg = (
            f"Home Assistant is in {' and '.join(f.replace('_', ' ') for f in flags)}"
            if flags
            else "Not in safe or recovery mode"
        )
        out.append(Result("core:safe_mode", not flags, msg))
    return out


def _newest_backup_ws(client: HAClient) -> tuple[datetime | None, str | None]:
    info = client.ws_command("backup/info") or {}
    dates = [parse_ts(b.get("date")) for b in info.get("backups") or []]
    dates = [d for d in dates if d]
    completed = info.get("last_completed_automatic_backup")
    if parse_ts(completed):
        dates.append(parse_ts(completed))  # type: ignore[arg-type]
    return (max(dates) if dates else None), info.get("state")


def check_backup(client: HAClient, opts: dict[str, Any], ctx: Context) -> list[Result]:
    max_hours = opts["max_age_hours"]
    manager_state = None
    try:
        if opts["source"] == "websocket":
            newest, manager_state = _newest_backup_ws(client)
        else:
            st = client.get_state(opts["sensor_entity"])
            if st is None:
                return [Result("backup", False, f"Backup sensor {opts['sensor_entity']} not found")]
            newest = parse_ts(st.get("state"))
            if newest is None:
                return [Result("backup", False, f"Backup sensor reports {st.get('state')!r}")]
    except HAError as exc:
        return [Result("backup", False, f"Cannot read backup status: {exc}")]
    if newest is None:
        return [Result("backup", False, "No backups found")]
    age = (ctx.now - newest).total_seconds()
    data = {"newest": newest.isoformat(), "age_hours": round(age / 3600, 2)}
    if manager_state:
        data["manager_state"] = manager_state
    if age > max_hours * 3600:
        return [Result("backup", False, f"Newest backup is {fmt_age(age)} old (limit {max_hours}h)", data)]
    return [Result("backup", True, f"Newest backup is {fmt_age(age)} old", data)]


def check_supervisor(client: HAClient, opts: dict[str, Any], ctx: Context) -> list[Result]:
    out: list[Result] = []
    try:
        if opts["alert_on_unhealthy"]:
            res = client.ws_command("supervisor/api", endpoint="/resolution/info", method="get") or {}
            unhealthy = res.get("unhealthy") or []
            msg = (
                f"Supervisor reports unhealthy: {', '.join(unhealthy)}"
                if unhealthy
                else "Supervisor reports healthy"
            )
            out.append(Result("supervisor:health", not unhealthy, msg))
        if opts["addons"]:
            res = client.ws_command("supervisor/api", endpoint="/addons", method="get") or {}
            by_slug = {a.get("slug"): a for a in res.get("addons") or []}
            for slug in opts["addons"]:
                addon = by_slug.get(slug)
                key = f"supervisor:addon:{slug}"
                if addon is None:
                    out.append(Result(key, False, f"Add-on {slug} is not installed"))
                else:
                    state = addon.get("state")
                    name = addon.get("name") or slug
                    out.append(Result(key, state == "started", f"Add-on {name} is {state}"))
    except HAError as exc:
        hint = ""
        if "unauthorized" in str(exc).lower():
            hint = " (the token must belong to an administrator for Supervisor checks)"
        elif "unknown_command" in str(exc).lower():
            hint = " (no Supervisor: this install type has no add-ons)"
        return [Result("supervisor", False, f"Cannot query Supervisor: {exc}{hint}")]
    return out


def check_entities(client: HAClient, opts: dict[str, Any], ctx: Context) -> list[Result]:
    limit = opts["max_unavailable_minutes"] * 60
    out: list[Result] = []
    for entity_id in opts["entity_ids"]:
        key = f"entity:{entity_id}"
        try:
            st = client.get_state(entity_id)
        except HAError as exc:
            out.append(Result(key, False, f"Cannot read {entity_id}: {exc}"))
            continue
        if st is None:
            out.append(Result(key, False, f"{entity_id} does not exist"))
            continue
        state = st.get("state")
        if state not in UNAVAILABLE:
            out.append(Result(key, True, f"{entity_id} is {state}"))
            continue
        since = parse_ts(st.get("last_changed"))
        age = (ctx.now - since).total_seconds() if since else limit + 1
        if age > limit:
            out.append(Result(key, False, f"{entity_id} has been {state} for {fmt_age(age)}"))
        else:  # unavailable, but not for long enough yet
            out.append(Result(key, True, f"{entity_id} is {state} for {fmt_age(age)} (within limit)"))
    return out


def check_memory(client: HAClient, opts: dict[str, Any], ctx: Context) -> list[Result]:
    entity_id, limit = opts["entity_id"], opts["max_percent"]
    try:
        st = client.get_state(entity_id)
    except HAError as exc:
        return [Result("memory", False, f"Cannot read {entity_id}: {exc}")]
    if st is None:
        return [Result("memory", False, f"{entity_id} does not exist")]
    try:
        value = float(st.get("state"))
    except (TypeError, ValueError):
        return [Result("memory", False, f"{entity_id} reports {st.get('state')!r}")]
    unit = (st.get("attributes") or {}).get("unit_of_measurement", "%")
    msg = f"Memory use {value:g}{unit} (limit {limit:g}{unit})"
    return [Result("memory", value <= limit, msg, {"value": value})]


def _tcp_connect(host: str, port: int, timeout: float) -> None:
    with socket.create_connection((host, port), timeout=timeout):
        pass


def _ping(host: str, timeout: float) -> bool:
    if platform.system() == "Windows":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), host]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout + 2).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def check_host(client: HAClient | None, opts: dict[str, Any], ctx: Context) -> list[Result]:
    host, timeout = opts["host"], float(opts["timeout_seconds"])
    if opts["method"] == "ping":
        ok = (ctx.ping or _ping)(host, timeout)
        return [Result("host", ok, f"Ping {host}: {'reply' if ok else 'no reply'}")]
    port = int(opts["port"])
    try:
        (ctx.tcp_connect or _tcp_connect)(host, port, timeout)
    except OSError as exc:
        return [Result("host", False, f"TCP {host}:{port} failed: {type(exc).__name__}")]
    return [Result("host", True, f"TCP {host}:{port} open")]


# Order matters: "api" runs first so the others can be skipped while HA is down.
CHECKS: dict[str, Callable[..., list[Result]]] = {
    "api": check_api,
    "host": check_host,
    "core": check_core,
    "backup": check_backup,
    "supervisor": check_supervisor,
    "entities": check_entities,
    "memory": check_memory,
}
# Checks that need HA's API; skipped (state kept) while the api check is failing.
NEEDS_API = {"core", "backup", "supervisor", "entities", "memory"}


def detect_busy(client: HAClient) -> list[str]:
    """Return what HA is busy with (backup or update in progress), from /api/states.

    Used to stop the power-cycle action from cutting power mid-backup or mid-update.
    """
    reasons: list[str] = []
    for st in client.get_states():
        eid = st.get("entity_id", "")
        if eid.startswith("update.") and (st.get("attributes") or {}).get("in_progress"):
            reasons.append(f"{eid} is installing")
        elif (
            eid.startswith("sensor.backup_")
            and eid.endswith("manager_state")
            and st.get("state") in BACKUP_BUSY_STATES
        ):
            reasons.append(f"backup manager is {st.get('state')}")
    return reasons
