from pathlib import Path

import pytest

from ha_watcher.config import ConfigError, expand_env, load_config, parse_config
from tests.conftest import TOKEN, base_raw, make_cfg

ROOT = Path(__file__).resolve().parent.parent


def test_env_expansion_and_default():
    out = expand_env({"a": "${X}", "b": ["${Y:-fallback}"], "c": 5}, env={"X": "1"})
    assert out == {"a": "1", "b": ["fallback"], "c": 5}


def test_missing_env_var_names_the_variable_not_a_value():
    with pytest.raises(ConfigError, match="HA_TOKEN is not set"):
        parse_config(base_raw(), env={})


def test_defaults_applied():
    cfg = make_cfg()
    assert cfg.home_assistant.token == TOKEN
    assert cfg.checks["api"].enabled and cfg.checks["api"].failures_before_alert == 3
    assert cfg.checks["core"].enabled
    assert not cfg.checks["backup"].enabled  # disabled in base_raw
    assert cfg.checks["backup"].options["max_age_hours"] == 26
    assert not cfg.power_cycle.enabled
    assert cfg.alerts.max_per_hour == 20


def test_trailing_slash_stripped():
    raw = base_raw()
    raw["home_assistant"]["url"] = "http://192.0.2.10:8123/"
    assert parse_config(raw, env={"HA_TOKEN": "x"}).home_assistant.url == "http://192.0.2.10:8123"


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda r: r.update(bogus=1), "unknown top-level"),
        (lambda r: r["checks"].update(nope={}), "unknown check"),
        (lambda r: r["checks"].update(api={"typo_key": 1}), "unknown key"),
        (lambda r: r["checks"].update(api={"failures_before_alert": 0}), "failures_before_alert"),
        (lambda r: r["checks"].update(entities={"enabled": True}), "entity_ids"),
        (lambda r: r["checks"].update(memory={"enabled": True}), "entity_id"),
        (lambda r: r["checks"].update(host={"enabled": True}), "checks.host.host"),
        (lambda r: r["checks"].update(host={"method": "carrier-pigeon"}), "method"),
        (lambda r: r["checks"].update(backup={"source": "ftp"}), "source"),
        (lambda r: r.update(notifiers=[{"type": "pager"}]), "notifiers"),
        (lambda r: r.update(heartbeat={"enabled": True, "time": "25:00"}), "HH:MM"),
        (lambda r: r.update(interval_seconds=0), "interval_seconds"),
        (lambda r: r.update(power_cycle={"enabled": True}), "power_cycle.host"),
        (lambda r: r.update(power_cycle={"enabled": True, "host": "192.0.2.20", "driver": "x"}), "driver"),
        (
            lambda r: r.update(
                power_cycle={"enabled": True, "host": "192.0.2.20", "trigger_checks": ["host"]}
            ),
            "not an enabled check",
        ),
        (lambda r: r["home_assistant"].update(url="192.0.2.10:8123"), "http://"),
    ],
)
def test_validation_errors(mutate, msg):
    raw = base_raw()
    mutate(raw)
    with pytest.raises(ConfigError, match=msg):
        parse_config(raw, env={"HA_TOKEN": "x"})


@pytest.mark.parametrize("name", ["api", "core", "entities", "memory", "host"])
def test_every_seconds_accepted_on_checks_without_a_default(name):
    raw = base_raw()
    raw["checks"][name] = {"every_seconds": 300}
    cfg = parse_config(raw, env={"HA_TOKEN": "x"})
    assert cfg.checks[name].every_seconds == 300
    assert "every_seconds" not in cfg.checks[name].options


def test_load_config_bad_yaml(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("home_assistant: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(p)


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope.yaml")


def test_example_config_is_valid():
    env = {
        "HA_TOKEN": "x",
        "NTFY_TOPIC": "t",
        "DISCORD_WEBHOOK_URL": "https://example.invalid/hook",
        "SMTP_PASSWORD": "p",
        "HC_PING_URL": "https://hc-ping.com/uuid",
        "SHELLY_PASSWORD": "s",
    }
    cfg = load_config(ROOT / "config.example.yaml", env=env)
    assert cfg.checks["backup"].enabled


# ------------------------------------------------------------------ hardening
def _parse(mutate, env=None):
    raw = base_raw()
    mutate(raw)
    return parse_config(raw, env={"HA_TOKEN": "x", **(env or {})})


def test_string_false_from_an_env_var_means_false():
    # ${VAR} always expands to a string; "false" used to be truthy, which switched the
    # power cycle ON when the variable said off.
    def mutate(r):
        r["power_cycle"] = {"enabled": "${PC_ON:-false}", "host": "192.0.2.20"}
        r["checks"]["host"] = {"enabled": "${HOST_ON}", "host": "192.0.2.10"}
        r["home_assistant"]["verify_ssl"] = "${VERIFY:-no}"

    cfg = _parse(mutate, {"HOST_ON": "False"})
    assert cfg.power_cycle.enabled is False
    assert cfg.checks["host"].enabled is False
    assert cfg.home_assistant.verify_ssl is False
    cfg = _parse(mutate, {"HOST_ON": "true", "PC_ON": "yes"})
    assert cfg.checks["host"].enabled is True and cfg.power_cycle.enabled is True


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda r: r.update(power_cycle={"enabled": "maybe", "host": "h"}), "power_cycle.enabled"),
        (lambda r: r["checks"].update(api={"enabled": 2}), "checks.api.enabled"),
        (lambda r: r.update(heartbeat={"enabled": "sometimes"}), "heartbeat.enabled"),
        (
            lambda r: r["checks"].update(entities={"enabled": True, "entity_ids": "sensor.one"}),
            "checks.entities.entity_ids: must be a list",
        ),
        (
            lambda r: r["checks"].update(supervisor={"addons": "core_mosquitto"}),
            "checks.supervisor.addons: must be a list",
        ),
        (
            lambda r: r.update(power_cycle={"enabled": True, "host": "h", "trigger_checks": "api"}),
            "power_cycle.trigger_checks: must be a list",
        ),
        (lambda r: r["checks"].update(memory={"max_percent": "high"}), "checks.memory.max_percent"),
        (lambda r: r["checks"].update(host={"port": 70000}), "checks.host.port"),
        (lambda r: r["checks"].update(host={"timeout_seconds": 0}), "checks.host.timeout_seconds"),
        (lambda r: r["home_assistant"].update(timeout_seconds=0), "home_assistant.timeout_seconds"),
        (lambda r: r["home_assistant"].update(ca_file="/nonexistent/ca.pem"), "ca_file"),
        (lambda r: r.update(deadman={"url": "hc-ping.com/uuid"}), "deadman.url"),
        (lambda r: r.update(deadman={"url": "https://hc-ping.com/u", "every_seconds": 0}), "every_seconds"),
        (lambda r: r.update(power_cycle={"enabled": True, "host": "h", "switch_id": -1}), "switch_id"),
        (lambda r: r.update(alerts={"max_per_hour": 2.5}), "alerts.max_per_hour"),
        (lambda r: r.update(name=""), "name"),
        (lambda r: r.update(state_file=""), "state_file"),
        (
            lambda r: r.update(
                power_cycle={"enabled": True, "host": "h", "after_minutes": 10},
                recovery_command={"enabled": True, "command": ["true"], "after_minutes": 10},
            ),
            "power_cycle.after_minutes must be greater than recovery_command.after_minutes",
        ),
    ],
)
def test_hardened_validation_errors(mutate, msg):
    with pytest.raises(ConfigError, match=msg):
        _parse(mutate)


def test_ca_file_that_exists_is_accepted(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("x", encoding="utf-8")
    cfg = _parse(lambda r: r["home_assistant"].update(ca_file=str(ca)))
    assert cfg.home_assistant.ca_file == str(ca)


def test_secrets_are_not_in_the_config_repr():
    cfg = _parse(lambda r: r.update(power_cycle={"enabled": True, "host": "h", "password": "plugpw"}))
    text = repr(cfg)
    assert "plugpw" not in text and "token='x'" not in text


def test_example_config_lists_every_option():
    # Keeps config.example.yaml a complete reference for the loader.
    import yaml

    from ha_watcher import config as c

    ex = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    sections = {
        "home_assistant": c.HomeAssistantConfig,
        "alerts": c.AlertConfig,
        "heartbeat": c.HeartbeatConfig,
        "deadman": c.DeadmanConfig,
        "power_cycle": c.PowerCycleConfig,
        "recovery_command": c.RecoveryCommandConfig,
        "expiry": c.ExpiryConfig,
        "phone_call": c.PhoneCallConfig,
        "home_assistant_push": c.HomeAssistantPushConfig,
    }
    missing = [
        f"{name}.{key}"
        for name, cls in sections.items()
        for key in cls.__dataclass_fields__
        if key not in ("twilio", "missing_env") and key not in ex[name] and f"# {key}:" not in text
    ]
    for check, defaults in c.CHECK_DEFAULTS.items():
        missing += [
            f"checks.{check}.{key}"
            for key in set(defaults) - c.COMMON_KEYS
            if key not in ex["checks"][check] and f"# {key}:" not in text
        ]
    assert missing == []


def test_empty_ca_file_from_an_unset_variable_means_none():
    cfg = _parse(lambda r: r["home_assistant"].update(ca_file="${HA_CA_FILE:-}"))
    assert cfg.home_assistant.ca_file is None


def test_numbers_from_env_vars_and_whole_floats_are_accepted():
    def mutate(r):
        r["checks"]["host"] = {"port": "${PORT}", "timeout_seconds": "2.5"}
        r["alerts"] = {"max_per_hour": 20.0, "realert_minutes": "${RE:-30}"}
        r["interval_seconds"] = "${INTERVAL:-45}"

    cfg = _parse(mutate, {"PORT": "8124"})
    assert cfg.checks["host"].options["port"] == 8124
    assert cfg.checks["host"].options["timeout_seconds"] == 2.5
    assert cfg.alerts.max_per_hour == 20 and isinstance(cfg.alerts.max_per_hour, int)
    assert cfg.alerts.realert_minutes == 30 and cfg.interval_seconds == 45
    with pytest.raises(ConfigError, match="checks.host.port"):
        _parse(lambda r: r["checks"].update(host={"port": "http"}))
