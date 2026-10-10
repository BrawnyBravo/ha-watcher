"""Load and validate the YAML config file.

Secrets never live in the file: any string value may contain ``${VAR}`` or
``${VAR:-default}``, which is replaced with the environment variable of that name.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """The config file is missing, unreadable or invalid."""


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any, env: dict[str, str] | None = None, path: str = "") -> Any:
    """Recursively replace ``${VAR}`` / ``${VAR:-default}`` in strings."""
    env = os.environ if env is None else env
    if isinstance(value, dict):
        return {k: expand_env(v, env, f"{path}.{k}" if path else str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v, env, f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            raise ConfigError(f"{path}: environment variable {name} is not set")

        return _ENV_RE.sub(repl, value)
    return value


# --------------------------------------------------------------------------- dataclasses


@dataclass
class HomeAssistantConfig:
    url: str
    token: str
    verify_ssl: bool = True
    ca_file: str | None = None  # CA bundle for a self-signed certificate
    timeout_seconds: float = 10.0


@dataclass
class CheckConfig:
    """Settings shared by every check."""

    enabled: bool = False
    failures_before_alert: int = 3
    every_seconds: int = 0  # 0 = every loop
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class AlertConfig:
    realert_minutes: float = 60.0  # 0 = alert once per outage
    max_per_hour: int = 20


@dataclass
class HeartbeatConfig:
    enabled: bool = False
    time: str = "09:00"  # local time HH:MM


@dataclass
class DeadmanConfig:
    url: str | None = None
    every_seconds: int = 60
    signal_failures: bool = False


@dataclass
class PowerCycleConfig:
    enabled: bool = False
    driver: str = "shelly"  # shelly | kasa
    host: str = ""
    switch_id: int = 0
    username: str | None = None
    password: str | None = None
    after_minutes: float = 10.0
    off_seconds: int = 15
    cooldown_minutes: float = 60.0
    max_per_day: int = 2
    trigger_checks: list[str] = field(default_factory=lambda: ["api"])
    busy_grace_minutes: float = 120.0
    dry_run: bool = False


@dataclass
class RecoveryCommandConfig:
    """Run a command (e.g. a remote "restart HA Core") when HA is down but the box answers."""

    enabled: bool = False
    command: list[str] = field(default_factory=list)  # argv, no shell
    timeout_seconds: float = 120.0
    trigger_checks: list[str] = field(default_factory=lambda: ["api"])
    only_if_passing: list[str] = field(default_factory=list)  # e.g. [host]
    after_minutes: float = 10.0
    cooldown_minutes: float = 60.0
    max_per_day: int = 2
    busy_grace_minutes: float = 0.0  # 0 = do not hold off for backups/updates
    dry_run: bool = False


@dataclass
class Config:
    home_assistant: HomeAssistantConfig
    checks: dict[str, CheckConfig]
    notifiers: list[dict[str, Any]]
    interval_seconds: int = 60
    state_file: str = "ha-watcher-state.json"
    name: str = "Home Assistant"
    alerts: AlertConfig = field(default_factory=AlertConfig)
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    deadman: DeadmanConfig = field(default_factory=DeadmanConfig)
    power_cycle: PowerCycleConfig = field(default_factory=PowerCycleConfig)
    recovery_command: RecoveryCommandConfig = field(default_factory=RecoveryCommandConfig)


# Defaults for each check. Anything not listed in COMMON_KEYS is check-specific.
CHECK_DEFAULTS: dict[str, dict[str, Any]] = {
    "api": {"enabled": True, "failures_before_alert": 3, "max_response_ms": 5000},
    "core": {
        "enabled": True,
        "failures_before_alert": 2,
        "alert_on_safe_mode": True,
    },
    "backup": {
        "enabled": True,
        "failures_before_alert": 1,
        "every_seconds": 1800,
        "source": "websocket",
        "max_age_hours": 26,
        "sensor_entity": "sensor.backup_last_successful_automatic_backup",
    },
    "supervisor": {
        "enabled": False,
        "failures_before_alert": 2,
        "every_seconds": 600,
        "addons": [],
        "alert_on_unhealthy": True,
    },
    "entities": {
        "enabled": False,
        "failures_before_alert": 1,
        "entity_ids": [],
        "max_unavailable_minutes": 15,
    },
    "memory": {
        "enabled": False,
        "failures_before_alert": 3,
        "entity_id": "",
        "max_percent": 90,
    },
    "host": {
        "enabled": False,
        "failures_before_alert": 3,
        "host": "",
        "port": 8123,
        "method": "tcp",
        "timeout_seconds": 3,
    },
}
COMMON_KEYS = {"enabled", "failures_before_alert", "every_seconds"}

NOTIFIER_TYPES = {"ntfy", "webhook", "email"}
TOP_LEVEL_KEYS = {
    "home_assistant",
    "checks",
    "notifiers",
    "interval_seconds",
    "state_file",
    "name",
    "alerts",
    "heartbeat",
    "deadman",
    "power_cycle",
    "recovery_command",
}


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"{key}: must be a mapping")
    return value


def _build(cls: type, data: dict[str, Any], where: str) -> Any:
    allowed = set(cls.__dataclass_fields__)
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    try:
        return cls(**data)
    except TypeError as exc:  # missing required field
        raise ConfigError(f"{where}: {exc}") from exc


def _positive(value: Any, where: str, allow_zero: bool = False) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{where}: must be a number")
    if value < 0 or (value == 0 and not allow_zero):
        raise ConfigError(f"{where}: must be {'>= 0' if allow_zero else '> 0'}")


def parse_config(raw: Any, env: dict[str, str] | None = None) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config file must be a YAML mapping")
    raw = expand_env(raw, env)
    unknown = set(raw) - TOP_LEVEL_KEYS
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")

    ha_raw = _section(raw, "home_assistant")
    if not ha_raw.get("url"):
        raise ConfigError("home_assistant.url is required")
    if not ha_raw.get("token"):
        raise ConfigError("home_assistant.token is required (use ${HA_TOKEN})")
    ha = _build(HomeAssistantConfig, ha_raw, "home_assistant")
    ha.url = ha.url.rstrip("/")
    if not ha.url.startswith(("http://", "https://")):
        raise ConfigError("home_assistant.url must start with http:// or https://")

    checks: dict[str, CheckConfig] = {}
    checks_raw = _section(raw, "checks")
    unknown = set(checks_raw) - set(CHECK_DEFAULTS)
    if unknown:
        raise ConfigError(f"checks: unknown check(s) {', '.join(sorted(unknown))}")
    for name, defaults in CHECK_DEFAULTS.items():
        user = checks_raw.get(name) or {}
        if not isinstance(user, dict):
            raise ConfigError(f"checks.{name}: must be a mapping")
        merged = {**defaults, **user}
        extra = set(merged) - set(defaults) - COMMON_KEYS
        if extra:
            raise ConfigError(f"checks.{name}: unknown key(s) {', '.join(sorted(extra))}")
        fba = merged["failures_before_alert"]
        if not isinstance(fba, int) or fba < 1:
            raise ConfigError(f"checks.{name}.failures_before_alert: must be an integer >= 1")
        _positive(merged.get("every_seconds", 0), f"checks.{name}.every_seconds", allow_zero=True)
        checks[name] = CheckConfig(
            enabled=bool(merged["enabled"]),
            failures_before_alert=fba,
            every_seconds=int(merged.get("every_seconds", 0)),
            options={k: v for k, v in merged.items() if k not in COMMON_KEYS},
        )
    _validate_checks(checks)

    notifiers = raw.get("notifiers") or []
    if not isinstance(notifiers, list):
        raise ConfigError("notifiers: must be a list")
    for i, n in enumerate(notifiers):
        if not isinstance(n, dict) or n.get("type") not in NOTIFIER_TYPES:
            raise ConfigError(f"notifiers[{i}].type must be one of {sorted(NOTIFIER_TYPES)}")

    cfg = Config(
        home_assistant=ha,
        checks=checks,
        notifiers=notifiers,
        interval_seconds=raw.get("interval_seconds", 60),
        state_file=raw.get("state_file", "ha-watcher-state.json"),
        name=raw.get("name", "Home Assistant"),
        alerts=_build(AlertConfig, _section(raw, "alerts"), "alerts"),
        heartbeat=_build(HeartbeatConfig, _section(raw, "heartbeat"), "heartbeat"),
        deadman=_build(DeadmanConfig, _section(raw, "deadman"), "deadman"),
        power_cycle=_build(PowerCycleConfig, _section(raw, "power_cycle"), "power_cycle"),
        recovery_command=_build(RecoveryCommandConfig, _section(raw, "recovery_command"), "recovery_command"),
    )
    _positive(cfg.interval_seconds, "interval_seconds")
    _positive(cfg.alerts.realert_minutes, "alerts.realert_minutes", allow_zero=True)
    _positive(cfg.alerts.max_per_hour, "alerts.max_per_hour")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(cfg.heartbeat.time)):
        raise ConfigError("heartbeat.time must be HH:MM (24-hour, local time)")
    _validate_power(cfg.power_cycle, checks)
    _validate_recovery(cfg.recovery_command, checks)
    return cfg


def _validate_checks(checks: dict[str, CheckConfig]) -> None:
    b = checks["backup"]
    if b.options["source"] not in ("websocket", "sensor"):
        raise ConfigError("checks.backup.source must be 'websocket' or 'sensor'")
    _positive(b.options["max_age_hours"], "checks.backup.max_age_hours")
    e = checks["entities"]
    if e.enabled and not e.options["entity_ids"]:
        raise ConfigError("checks.entities.entity_ids: list at least one entity")
    _positive(e.options["max_unavailable_minutes"], "checks.entities.max_unavailable_minutes", True)
    m = checks["memory"]
    if m.enabled and not m.options["entity_id"]:
        raise ConfigError("checks.memory.entity_id is required when the check is enabled")
    h = checks["host"]
    if h.enabled and not h.options["host"]:
        raise ConfigError("checks.host.host is required when the check is enabled")
    if h.options["method"] not in ("tcp", "ping"):
        raise ConfigError("checks.host.method must be 'tcp' or 'ping'")
    _positive(checks["api"].options["max_response_ms"], "checks.api.max_response_ms")


def _validate_power(p: PowerCycleConfig, checks: dict[str, CheckConfig]) -> None:
    if not p.enabled:
        return
    if p.driver not in ("shelly", "kasa"):
        raise ConfigError("power_cycle.driver must be 'shelly' or 'kasa'")
    if not p.host:
        raise ConfigError("power_cycle.host is required when power_cycle is enabled")
    for name in p.trigger_checks:
        if name not in checks or not checks[name].enabled:
            raise ConfigError(f"power_cycle.trigger_checks: '{name}' is not an enabled check")
    _positive(p.after_minutes, "power_cycle.after_minutes")
    _positive(p.off_seconds, "power_cycle.off_seconds")
    _positive(p.cooldown_minutes, "power_cycle.cooldown_minutes", allow_zero=True)
    _positive(p.max_per_day, "power_cycle.max_per_day")


def _validate_recovery(r: RecoveryCommandConfig, checks: dict[str, CheckConfig]) -> None:
    if not r.enabled:
        return
    if (
        not isinstance(r.command, list)
        or not r.command
        or not all(isinstance(a, str) and a for a in r.command)
    ):
        raise ConfigError("recovery_command.command must be a non-empty list of strings (argv, no shell)")
    if not r.trigger_checks:
        raise ConfigError("recovery_command.trigger_checks: list at least one check")
    for key in ("trigger_checks", "only_if_passing"):
        for name in getattr(r, key):
            if name not in checks or not checks[name].enabled:
                raise ConfigError(f"recovery_command.{key}: '{name}' is not an enabled check")
    overlap = set(r.trigger_checks) & set(r.only_if_passing)
    if overlap:
        raise ConfigError(
            f"recovery_command: {', '.join(sorted(overlap))} cannot be both a trigger and in only_if_passing"
        )
    _positive(r.timeout_seconds, "recovery_command.timeout_seconds")
    _positive(r.after_minutes, "recovery_command.after_minutes")
    _positive(r.cooldown_minutes, "recovery_command.cooldown_minutes", allow_zero=True)
    _positive(r.max_per_day, "recovery_command.max_per_day")
    _positive(r.busy_grace_minutes, "recovery_command.busy_grace_minutes", allow_zero=True)


def load_config(path: str | Path, env: dict[str, str] | None = None) -> Config:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {p}: {exc.strerror}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: invalid YAML: {exc}") from exc
    return parse_config(raw, env)
