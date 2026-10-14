from datetime import UTC
from pathlib import Path

import httpx

from ha_watcher import cli
from ha_watcher.engine import Watcher
from tests.conftest import FakeHA, make_client

ROOT = Path(__file__).resolve().parent.parent
ENV = {
    "HA_TOKEN": "test-token-not-real",
    "NTFY_TOPIC": "t",
    "DISCORD_WEBHOOK_URL": "https://example.invalid/h",
    "SMTP_PASSWORD": "p",
}


def write_cfg(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


MINIMAL = """
home_assistant:
  url: http://192.0.2.10:8123
  token: ${HA_TOKEN}
checks:
  backup: {enabled: false}
state_file: STATE
"""


def test_check_config_example_ok(monkeypatch, capsys):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["--check-config", "-c", str(ROOT / "config.example.yaml")]) == 0
    out = capsys.readouterr().out
    assert "Config OK" in out and "Power cycle: off" in out
    assert "test-token-not-real" not in out


def test_check_config_error_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HA_TOKEN", raising=False)
    p = write_cfg(tmp_path, MINIMAL)
    assert cli.main(["--check-config", "-c", str(p)]) == 2
    assert "HA_TOKEN is not set" in capsys.readouterr().err


def test_check_config_bad_notifier(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HA_TOKEN", "x")
    p = write_cfg(tmp_path, MINIMAL + "notifiers:\n  - type: ntfy\n")
    assert cli.main(["--check-config", "-c", str(p)]) == 2
    assert "topic" in capsys.readouterr().err


def _patch_watcher(monkeypatch, tmp_path, fake):
    def build(cfg):
        return Watcher(
            cfg,
            make_client(fake, cfg=cfg),
            [],
            httpx.Client(),
            local_tz=UTC,
            state_path=str(tmp_path / "state.json"),
        )

    monkeypatch.setattr(cli, "build_watcher", build)


def test_once_prints_results_and_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HA_TOKEN", "test-token-not-real")
    p = write_cfg(tmp_path, MINIMAL)
    _patch_watcher(monkeypatch, tmp_path, FakeHA())
    assert cli.main(["--once", "-c", str(p)]) == 0
    out = capsys.readouterr().out
    assert "OK   api" in out and "OK   core" in out

    _patch_watcher(monkeypatch, tmp_path, FakeHA({"/api/config": (200, {"state": "STARTING"})}))
    assert cli.main(["--once", "-c", str(p)]) == 1
    assert "FAIL core" in capsys.readouterr().out


def test_test_notify(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HA_TOKEN", "x")
    p = write_cfg(tmp_path, MINIMAL)
    _patch_watcher(monkeypatch, tmp_path, FakeHA())
    assert cli.main(["--test-notify", "-c", str(p)]) == 0
    assert "Sent to 0 of 0" in capsys.readouterr().out


TWILIO_VARS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "TWILIO_TO_NUMBER")


def test_check_config_reports_phone_and_expiry_off(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HA_TOKEN", "x")
    for k in TWILIO_VARS:
        monkeypatch.delenv(k, raising=False)
    p = write_cfg(tmp_path, MINIMAL)
    assert cli.main(["--check-config", "-c", str(p)]) == 0
    out = capsys.readouterr().out
    assert "Phone call: off (no TWILIO_* variables set)" in out
    assert "Expiry warnings: off" in out
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret-not-real")
    assert cli.main(["--check-config", "-c", str(p)]) == 0
    out = capsys.readouterr().out
    assert "missing TWILIO_ACCOUNT_SID, TWILIO_FROM_NUMBER, TWILIO_TO_NUMBER" in out
    assert "secret-not-real" not in out


def test_check_config_reports_phone_and_expiry_on(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HA_TOKEN", "x")
    values = ["AC" + "0" * 32, "secret-not-real", "+15555550100", "+15555550199"]
    for k, v in zip(TWILIO_VARS, values, strict=True):
        monkeypatch.setenv(k, v)
    p = write_cfg(tmp_path, MINIMAL + "expiry:\n  tls_hosts: [example.com]\n  domains: [example.com]\n")
    assert cli.main(["--check-config", "-c", str(p)]) == 0
    out = capsys.readouterr().out
    assert "Phone call: on, after 30m of api failing, once per outage, max 2/day" in out
    assert "Expiry warnings: 1 certificate(s), 1 domain(s), at 21/7 days" in out
    for v in values:
        assert v not in out


def test_test_call_refuses_without_twilio(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HA_TOKEN", "x")
    for k in TWILIO_VARS:
        monkeypatch.delenv(k, raising=False)
    p = write_cfg(tmp_path, MINIMAL)
    assert cli.main(["--test-call", "-c", str(p)]) == 2
    assert "Phone call: off" in capsys.readouterr().err


def test_check_config_rejects_a_ca_file_that_is_not_a_certificate(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HA_TOKEN", "x")
    ca = tmp_path / "ca.pem"
    ca.write_text("not a certificate", encoding="utf-8")
    text = MINIMAL.replace("token: ${HA_TOKEN}", f"token: ${{HA_TOKEN}}\n  ca_file: {ca.as_posix()}")
    p = write_cfg(tmp_path, text)
    assert cli.main(["--check-config", "-c", str(p)]) == 2
    assert "ca_file" in capsys.readouterr().err


def test_check_config_rejects_unknown_notifier_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HA_TOKEN", "x")
    p = write_cfg(tmp_path, MINIMAL + "notifiers:\n  - type: ntfy\n    topic: t\n    priority: max\n")
    assert cli.main(["--check-config", "-c", str(p)]) == 2
    assert "notifiers[0]: unknown key(s) priority" in capsys.readouterr().err


def test_sigterm_ends_the_process_cleanly(monkeypatch):
    import signal

    seen = {}
    monkeypatch.setattr(signal, "signal", lambda sig, fn: seen.setdefault(sig, fn))
    cli._exit_on_sigterm()
    try:
        seen[signal.SIGTERM](signal.SIGTERM, None)
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("SIGTERM handler did not exit")


def test_health_reports_a_stale_or_missing_loop(tmp_path, monkeypatch, capsys):
    import json
    from datetime import datetime, timedelta

    monkeypatch.setenv("HA_TOKEN", "x")
    state = tmp_path / "state.json"
    p = write_cfg(tmp_path, MINIMAL.replace("STATE", state.as_posix()))
    assert cli.main(["--health", "-c", str(p)]) == 1  # no loop has run yet
    now = datetime.now(UTC)
    state.write_text(json.dumps({"version": 1, "last_loop": now.isoformat()}), encoding="utf-8")
    assert cli.main(["--health", "-c", str(p)]) == 0
    old = now - timedelta(minutes=10)
    state.write_text(json.dumps({"version": 1, "last_loop": old.isoformat()}), encoding="utf-8")
    assert cli.main(["--health", "-c", str(p)]) == 1
    assert "last loop" in capsys.readouterr().err
