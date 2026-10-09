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
