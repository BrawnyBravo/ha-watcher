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
