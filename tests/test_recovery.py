import subprocess
import sys
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ha_watcher import cli, recovery
from ha_watcher.config import ConfigError, RecoveryCommandConfig
from ha_watcher.engine import Watcher
from ha_watcher.state import load_state
from tests.conftest import NOW, FakeHA, make_cfg, make_client
from tests.test_cli import write_cfg
from tests.test_engine import API_DOWN, Inbox, Plug

M = timedelta(minutes=1)
HOST = {"host": {"enabled": True, "host": "192.0.2.10", "method": "ping"}}
RC = {
    "enabled": True,
    "command": ["ssh", "restart@192.0.2.10"],
    "trigger_checks": ["api"],
    "only_if_passing": ["host"],
    "after_minutes": 10,
    "cooldown_minutes": 60,
    "max_per_day": 2,
}


class Runner:
    """Stands in for subprocess.run."""

    def __init__(self, returncode=0, stdout="Command completed successfully.\n", stderr="", exc=None):
        self.calls = []
        self.returncode, self.stdout, self.stderr, self.exc = returncode, stdout, stderr, exc

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def make(tmp_path, runner=None, plug=None, host_up=True, fake=None, **over):
    cfg = make_cfg(checks=HOST, recovery_command={**RC, **over.pop("rc", {})}, **over)
    inbox = Inbox()
    w = Watcher(
        cfg,
        make_client(fake or FakeHA(API_DOWN), cfg=cfg),
        [inbox],
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        driver=plug,
        runner=runner or Runner(),
        ctx_hooks={"ping": lambda h, t: host_up},
        local_tz=UTC,
        state_path=str(tmp_path / "state.json"),
    )
    return w, inbox


def loop(w, start, end):
    for i in range(start, end):
        w.run_once(NOW + i * M)


# ------------------------------------------------------------------ engine
def test_runs_after_n_minutes_with_messages_before_and_after(tmp_path):
    runner = Runner()
    w, inbox = make(tmp_path, runner, alerts={"realert_minutes": 0})
    loop(w, 0, 10)  # minutes 0..9: under 10 minutes down
    assert runner.calls == []
    w.run_once(NOW + 10 * M)
    assert len(runner.calls) == 1
    argv, kw = runner.calls[0]
    assert argv == ["ssh", "restart@192.0.2.10"]
    assert kw["timeout"] == 120 and kw["stdin"] is subprocess.DEVNULL
    titles = [m.title for m in inbox.msgs]
    assert titles.index("Home Assistant: restarting") < titles.index("Home Assistant: restart sent")
    before = next(m for m in inbox.msgs if m.title == "Home Assistant: restarting")
    assert "down for 10 min" in before.body and "attempt 1 of 2" in before.body
    after = next(m for m in inbox.msgs if m.title == "Home Assistant: restart sent")
    assert "Command completed successfully." in after.body


def test_cooldown_and_daily_cap(tmp_path):
    runner = Runner()
    w, _ = make(tmp_path, runner, alerts={"realert_minutes": 0})
    loop(w, 0, 70)
    assert len(runner.calls) == 1  # cooldown: nothing until minute 70
    w.run_once(NOW + 70 * M)
    assert len(runner.calls) == 2
    loop(w, 71, 400)
    assert len(runner.calls) == 2  # daily cap of 2
    assert "daily cap" in w.state["recovery"]["last_decision"]


def test_not_run_when_host_is_also_down(tmp_path):
    runner = Runner()
    w, _ = make(tmp_path, runner, host_up=False)
    loop(w, 0, 30)
    assert runner.calls == []
    assert w.state["recovery"]["last_decision"].startswith("skipped: host also failing")


def test_not_run_when_ha_is_up(tmp_path):
    runner = Runner()
    w, _ = make(tmp_path, runner, fake=FakeHA())
    loop(w, 0, 30)
    assert runner.calls == []


def test_counts_survive_a_watcher_restart(tmp_path):
    runner = Runner()
    w, _ = make(tmp_path, runner, rc={"max_per_day": 1})
    loop(w, 0, 11)
    assert len(runner.calls) == 1
    assert len(load_state(tmp_path / "state.json")["recovery"]["runs"]) == 1
    runner2 = Runner()
    w2, _ = make(tmp_path, runner2, rc={"max_per_day": 1})  # fresh process, same state file
    loop(w2, 11, 300)
    assert runner2.calls == []


def test_failure_is_reported_and_counted(tmp_path):
    runner = Runner(returncode=255, stdout="", stderr="ssh: connect to host 192.0.2.10 port 22: refused\n")
    w, inbox = make(tmp_path, runner)
    loop(w, 0, 12)
    assert len(runner.calls) == 1
    failed = next(m for m in inbox.msgs if m.title == "Home Assistant: restart FAILED")
    assert "code 255" in failed.body and "refused" in failed.body
    assert len(w.state["recovery"]["runs"]) == 1


def test_timeout_is_reported(tmp_path):
    runner = Runner(exc=subprocess.TimeoutExpired(["ssh"], 120))
    w, inbox = make(tmp_path, runner)
    loop(w, 0, 12)
    assert any("timed out after 120s" in m.body for m in inbox.msgs)


def test_dry_run(tmp_path):
    runner = Runner()
    w, inbox = make(tmp_path, runner, rc={"dry_run": True})
    loop(w, 0, 12)
    assert runner.calls == []
    assert any("dry run" in m.body for m in inbox.msgs)
    assert w.state["recovery"]["runs"] == []


def test_soft_restart_first_power_cycle_later(tmp_path):
    runner, plug = Runner(), Plug()
    w, _ = make(
        tmp_path,
        runner,
        plug=plug,
        host_up=True,
        power_cycle={"enabled": True, "host": "192.0.2.20", "after_minutes": 20, "trigger_checks": ["api"]},
    )
    loop(w, 0, 15)
    assert len(runner.calls) == 1 and plug.cycles == 0
    loop(w, 15, 21)
    assert plug.cycles == 1  # the soft restart did not help, so the power cycle follows


def test_same_tick_never_does_both(tmp_path):
    runner, plug = Runner(), Plug()
    w, _ = make(
        tmp_path,
        runner,
        plug=plug,
        power_cycle={"enabled": True, "host": "192.0.2.20", "after_minutes": 10, "trigger_checks": ["api"]},
    )
    loop(w, 0, 11)
    assert len(runner.calls) == 1 and plug.cycles == 0
    w.run_once(NOW + 11 * M)
    assert plug.cycles == 1


def test_busy_hold_off_when_enabled(tmp_path):
    busy = FakeHA(
        {
            "/api/states": (
                200,
                [{"entity_id": "sensor.backup_backup_manager_state", "state": "create_backup"}],
            )
        }
    )
    runner = Runner()
    w, _ = make(tmp_path, runner, fake=busy, rc={"busy_grace_minutes": 30})
    w.run_once(NOW)  # HA up and busy
    busy.routes.update(API_DOWN)
    loop(w, 1, 30)
    assert runner.calls == []
    assert "held off" in w.state["recovery"]["last_decision"]
    loop(w, 30, 32)
    assert len(runner.calls) == 1


# ------------------------------------------------------------------ pure parts
def test_decide_matrix():
    cfg = RecoveryCommandConfig(enabled=True, command=["true"])
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert not recovery.decide(RecoveryCommandConfig(), now, now - 30 * M, [], []).allowed
    assert recovery.decide(cfg, now, None, [], []).reason == "Home Assistant is not down"
    assert "waiting" in recovery.decide(cfg, now, now - 5 * M, [], []).reason
    assert recovery.decide(cfg, now, now - 10 * M, [], []).allowed
    assert "skipped" in recovery.decide(cfg, now, now - 30 * M, [], ["host"]).reason
    assert "cooldown" in recovery.decide(cfg, now, now - 30 * M, [now - 5 * M], []).reason
    two = [now - 3 * timedelta(hours=1), now - 2 * timedelta(hours=1)]
    assert "daily cap" in recovery.decide(cfg, now, now - 30 * M, two, []).reason
    # busy guard is off by default
    assert recovery.decide(cfg, now, now - 30 * M, [], [], now - M, "backup").allowed


def test_run_command_real_subprocess():
    ok = RecoveryCommandConfig(enabled=True, command=[sys.executable, "-c", "print('done')"])
    assert recovery.run_command(ok).endswith("(done)")
    bad = RecoveryCommandConfig(enabled=True, command=[sys.executable, "-c", "import sys; sys.exit(3)"])
    with pytest.raises(recovery.RecoveryError, match="code 3"):
        recovery.run_command(bad)
    missing = RecoveryCommandConfig(enabled=True, command=["definitely-not-a-real-binary-xyz"])
    with pytest.raises(recovery.RecoveryError, match="could not start"):
        recovery.run_command(missing)


def test_long_output_is_trimmed():
    cfg = RecoveryCommandConfig(enabled=True, command=["x"])
    out = recovery.run_command(cfg, Runner(stdout="a" * 1000))
    assert len(out) < 400 and out.endswith("...)")


# ------------------------------------------------------------------ config / cli
@pytest.mark.parametrize(
    "rc, msg",
    [
        ({"command": []}, "non-empty list"),
        ({"command": "ssh host"}, "non-empty list"),
        ({"trigger_checks": ["memory"]}, "'memory' is not an enabled check"),
        ({"only_if_passing": ["supervisor"]}, "'supervisor' is not an enabled check"),
        ({"only_if_passing": ["api"]}, "cannot be both"),
        ({"after_minutes": 0}, "after_minutes"),
        ({"max_per_day": 0}, "max_per_day"),
        ({"timeout_seconds": -1}, "timeout_seconds"),
        ({"bogus": 1}, "unknown key"),
    ],
)
def test_config_validation(rc, msg):
    with pytest.raises(ConfigError, match=msg):
        make_cfg(checks=HOST, recovery_command={**RC, **rc})


def test_disabled_by_default():
    assert make_cfg().recovery_command.enabled is False


def test_check_config_summary_and_missing_binary(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HA_TOKEN", "x")
    base = """
home_assistant: {url: "http://192.0.2.10:8123", token: "${HA_TOKEN}"}
checks:
  backup: {enabled: false}
  host: {enabled: true, host: 192.0.2.10, method: ping}
recovery_command:
  enabled: true
  only_if_passing: [host]
"""
    ok = write_cfg(tmp_path, base + f"  command: ['{sys.executable}', '-c', 'pass']\n")
    assert cli.main(["--check-config", "-c", str(ok)]) == 0
    assert "Recovery command:" in capsys.readouterr().out
    bad = write_cfg(tmp_path, base + "  command: [definitely-not-a-real-binary-xyz]\n")
    assert cli.main(["--check-config", "-c", str(bad)]) == 2
    assert "not found" in capsys.readouterr().err
