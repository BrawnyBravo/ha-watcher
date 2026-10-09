import json
from datetime import UTC, timedelta

import httpx

from ha_watcher.engine import Watcher
from ha_watcher.notifiers import Notifier
from ha_watcher.power import PowerError
from tests.conftest import NOW, FakeHA, FakeWS, make_cfg, make_client

M = timedelta(minutes=1)
API_DOWN = {"/api/": (0, httpx.ConnectError("refused"))}


class Inbox(Notifier):
    name = "inbox"

    def __init__(self):
        self.msgs = []

    def send(self, msg):
        self.msgs.append(msg)


class Pings:
    def __init__(self):
        self.urls = []

    def __call__(self, req):
        self.urls.append(str(req.url))
        return httpx.Response(200)


class Plug:
    def __init__(self, fail=False):
        self.cycles = 0
        self.fail = fail

    def power_cycle(self, off):
        self.cycles += 1
        if self.fail:
            raise PowerError("Shelly unreachable: ConnectError")


def watcher(tmp_path, fake=None, ws=None, plug=None, **cfg_over):
    cfg = make_cfg(**cfg_over)
    inbox, pings = Inbox(), Pings()
    w = Watcher(
        cfg,
        make_client(fake or FakeHA(), ws, cfg),
        [inbox],
        httpx.Client(transport=httpx.MockTransport(pings)),
        driver=plug,
        local_tz=UTC,
        state_path=str(tmp_path / "state.json"),
    )
    return w, inbox, pings


def run(w, fake, minutes):
    """Run the loop once per minute for `minutes` minutes; returns the time after."""
    t = NOW
    for i in range(minutes):
        t = NOW + i * M
        w.run_once(t)
    return t


# ------------------------------------------------------------------ debounce
def test_no_alert_until_threshold_then_one_alert(tmp_path):
    fake = FakeHA(API_DOWN)
    w, inbox, _ = watcher(
        tmp_path, fake, checks={"backup": {"enabled": False}}, alerts={"realert_minutes": 0}
    )
    w.run_once(NOW)
    w.run_once(NOW + M)
    assert inbox.msgs == []  # 2 failures < 3
    w.run_once(NOW + 2 * M)
    assert len(inbox.msgs) == 1 and "API unreachable" in inbox.msgs[0].body
    run(w, fake, 10)
    assert len(inbox.msgs) == 1  # realert off: one alert per outage


def test_single_success_resets_the_counter(tmp_path):
    fake = FakeHA(API_DOWN)
    w, inbox, _ = watcher(tmp_path, fake)
    w.run_once(NOW)
    w.run_once(NOW + M)
    fake.routes["/api/"] = (200, {"message": "API running."})
    w.run_once(NOW + 2 * M)
    fake.routes.update(API_DOWN)
    w.run_once(NOW + 3 * M)
    w.run_once(NOW + 4 * M)
    assert inbox.msgs == []


def test_recovery_message_with_duration(tmp_path):
    fake = FakeHA(API_DOWN)
    w, inbox, _ = watcher(tmp_path, fake)
    for i in range(5):
        w.run_once(NOW + i * M)
    fake.routes["/api/"] = (200, {"message": "API running."})
    w.run_once(NOW + 5 * M)
    assert [m.severity for m in inbox.msgs] == ["alert", "recovery"]
    assert "was failing for 5m" in inbox.msgs[1].body


def test_no_recovery_message_if_never_alerted(tmp_path):
    fake = FakeHA(API_DOWN)
    w, inbox, _ = watcher(tmp_path, fake)
    w.run_once(NOW)
    fake.routes["/api/"] = (200, {"message": "API running."})
    w.run_once(NOW + M)
    assert inbox.msgs == []


def test_realert_while_still_failing(tmp_path):
    fake = FakeHA(API_DOWN)
    w, inbox, _ = watcher(tmp_path, fake, alerts={"realert_minutes": 30})
    run(w, fake, 64)  # first alert at minute 2, reminders at 32 and 62
    assert len(inbox.msgs) == 3
    assert inbox.msgs[1].body.startswith("- Still failing for 32m")


def test_dependent_checks_skipped_while_api_down(tmp_path):
    fake = FakeHA(API_DOWN)
    w, inbox, _ = watcher(tmp_path, fake)
    run(w, fake, 5)
    assert [r.url.path for r in fake.requests].count("/api/config") == 0
    assert len(inbox.msgs) == 1 and "core" not in w.state["checks"]


def test_alerts_batched_into_one_message(tmp_path):
    fake = FakeHA({"/api/config": (200, {"state": "NOT_RUNNING", "safe_mode": True})})
    w, inbox, _ = watcher(tmp_path, fake)
    w.run_once(NOW)
    w.run_once(NOW + M)
    assert len(inbox.msgs) == 1 and inbox.msgs[0].title == "Home Assistant: 2 problems"


def test_rate_limit_suppresses_and_reports(tmp_path):
    fake = FakeHA(API_DOWN)
    w, inbox, _ = watcher(
        tmp_path,
        fake,
        checks={"api": {"failures_before_alert": 1}},
        alerts={"realert_minutes": 1, "max_per_hour": 3},
    )
    run(w, fake, 10)
    assert len(inbox.msgs) == 3  # minutes 0,1,2; then capped
    assert w.state["suppressed"] == 7
    fake.routes["/api/"] = (200, {"message": "API running."})
    w.run_once(NOW + 10 * M)  # recovery bypasses the cap and carries the count
    assert inbox.msgs[-1].severity == "recovery"
    assert "7 earlier message(s) suppressed" in inbox.msgs[-1].body


def test_check_that_stops_reporting_a_key_sends_resolved(tmp_path):
    fake = FakeHA()
    ws = FakeWS({"supervisor/api": ("error", "unauthorized", "Unauthorized")})
    w, inbox, _ = watcher(
        tmp_path,
        fake,
        ws,
        checks={
            "backup": {"enabled": False},
            "supervisor": {"enabled": True, "failures_before_alert": 1, "every_seconds": 0},
        },
    )
    w.run_once(NOW)
    assert "administrator" in inbox.msgs[0].body
    ws.results["supervisor/api"] = {"unhealthy": []}
    w.run_once(NOW + M)
    assert inbox.msgs[-1].severity == "recovery" and "Resolved" in inbox.msgs[-1].body


def test_every_seconds_spacing(tmp_path):
    ws = FakeWS({"backup/info": {"backups": [{"date": NOW.isoformat()}]}})
    w, _, _ = watcher(tmp_path, FakeHA(), ws, checks={"backup": {"enabled": True, "every_seconds": 1800}})
    run(w, None, 31)
    assert sum(1 for m in ws.sent if m["type"] == "backup/info") == 2  # minute 0 and minute 30


def test_crashing_check_is_reported_not_fatal(tmp_path, monkeypatch):
    from ha_watcher import checks

    def boom(*a):
        raise RuntimeError("bug")

    monkeypatch.setitem(checks.CHECKS, "core", boom)
    w, _, _ = watcher(tmp_path)
    rs = {r.key: r for r in w.run_once(NOW)}
    assert rs["api"].ok and not rs["core"].ok and "crashed" in rs["core"].message


def test_state_persists_across_restarts(tmp_path):
    fake = FakeHA(API_DOWN)
    w, inbox, _ = watcher(tmp_path, fake)
    w.run_once(NOW)
    w.run_once(NOW + M)
    w2, inbox2, _ = watcher(tmp_path, fake)  # restart: counter must survive
    w2.run_once(NOW + 2 * M)
    assert len(inbox2.msgs) == 1
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["checks"]["api"]["alerting"] is True


def test_corrupt_state_file_starts_fresh(tmp_path):
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    w, _, _ = watcher(tmp_path)
    assert w.state["checks"] == {}
    w.run_once(NOW)


# --------------------------------------------------------- heartbeat / deadman
def test_heartbeat_once_per_day_after_time(tmp_path):
    w, inbox, _ = watcher(tmp_path, heartbeat={"enabled": True, "time": "12:30"})
    w.run_once(NOW)  # 12:00, too early
    assert inbox.msgs == []
    w.run_once(NOW + 31 * M)
    w.run_once(NOW + 40 * M)
    assert [m.title for m in inbox.msgs] == ["Home Assistant: daily heartbeat"]
    assert inbox.msgs[0].body == "All checks passing."
    w.run_once(NOW + timedelta(days=1, minutes=31))
    assert len(inbox.msgs) == 2


def test_heartbeat_lists_open_alerts(tmp_path):
    fake = FakeHA(API_DOWN)
    w, inbox, _ = watcher(
        tmp_path,
        fake,
        heartbeat={"enabled": True, "time": "00:00"},
        checks={"api": {"failures_before_alert": 1}},
    )
    w.run_once(NOW)
    hb = [m for m in inbox.msgs if "heartbeat" in m.title][0]
    assert "Still failing" in hb.body


def test_deadman_ping_interval_and_fail_suffix(tmp_path):
    fake = FakeHA(API_DOWN)
    w, _, pings = watcher(
        tmp_path,
        fake,
        checks={"api": {"failures_before_alert": 1}},
        deadman={"url": "https://hc-ping.example/abc", "every_seconds": 120, "signal_failures": True},
    )
    w.run_once(NOW)
    w.run_once(NOW + M)  # within 120 s, no ping
    w.run_once(NOW + 2 * M)
    assert pings.urls == ["https://hc-ping.example/abc/fail"] * 2


def test_deadman_plain_ping_when_healthy(tmp_path):
    w, _, pings = watcher(tmp_path, deadman={"url": "https://hc-ping.example/abc", "signal_failures": True})
    w.run_once(NOW)
    assert pings.urls == ["https://hc-ping.example/abc"]


# --------------------------------------------------------------------- power
PC = {"enabled": True, "host": "192.0.2.20", "after_minutes": 10, "cooldown_minutes": 60, "max_per_day": 2}


def test_power_cycle_after_n_minutes_with_cooldown_and_cap(tmp_path):
    fake, plug = FakeHA(API_DOWN), Plug()
    w, inbox, _ = watcher(tmp_path, fake, plug=plug, power_cycle=PC, alerts={"realert_minutes": 0})
    run(w, fake, 10)  # minutes 0..9: under 10 minutes down
    assert plug.cycles == 0
    w.run_once(NOW + 10 * M)
    assert plug.cycles == 1
    assert any(m.title == "Home Assistant: power cycle" for m in inbox.msgs)
    for i in range(11, 70):  # cooldown: nothing until minute 70
        w.run_once(NOW + i * M)
    assert plug.cycles == 1
    w.run_once(NOW + 70 * M)
    assert plug.cycles == 2
    for i in range(71, 400):  # daily cap of 2
        w.run_once(NOW + i * M)
    assert plug.cycles == 2


def test_power_cycle_held_off_if_backup_was_running(tmp_path):
    fake, plug = (
        FakeHA(
            {
                "/api/states": (
                    200,
                    [{"entity_id": "sensor.backup_backup_manager_state", "state": "create_backup"}],
                )
            }
        ),
        Plug(),
    )
    w, inbox, _ = watcher(tmp_path, fake, plug=plug, power_cycle={**PC, "busy_grace_minutes": 120})
    w.run_once(NOW)  # HA up and busy
    fake.routes.update(API_DOWN)
    for i in range(1, 60):
        w.run_once(NOW + i * M)
    assert plug.cycles == 0
    assert "held off" in w.state["power"]["last_decision"]
    for i in range(60, 125):
        w.run_once(NOW + i * M)
    assert plug.cycles == 1  # allowed once the grace period has passed


def test_power_cycle_failure_is_reported_and_counted(tmp_path):
    fake, plug = FakeHA(API_DOWN), Plug(fail=True)
    w, inbox, _ = watcher(tmp_path, fake, plug=plug, power_cycle=PC)
    run(w, fake, 12)
    assert plug.cycles == 1
    assert any("FAILED" in m.title for m in inbox.msgs)
    assert len(w.state["power"]["cycles"]) == 1


def test_power_dry_run(tmp_path):
    fake, plug = FakeHA(API_DOWN), Plug()
    w, inbox, _ = watcher(tmp_path, fake, plug=plug, power_cycle={**PC, "dry_run": True})
    run(w, fake, 12)
    assert plug.cycles == 0
    assert any("dry run" in m.body for m in inbox.msgs)


def test_power_needs_all_trigger_checks(tmp_path):
    fake, plug = FakeHA(API_DOWN), Plug()
    up = {"tcp_connect": lambda h, p, t: None}
    w, _, _ = watcher(
        tmp_path,
        fake,
        plug=plug,
        checks={"host": {"enabled": True, "host": "192.0.2.10"}},
        power_cycle={**PC, "trigger_checks": ["api", "host"]},
    )
    w.ctx_hooks = up  # host still answers, so HA is not "down" for cycling purposes
    run(w, fake, 20)
    assert plug.cycles == 0
