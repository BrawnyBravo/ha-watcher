"""Plug pre-flight, and the stale, battery and stuck checks."""

import json
from datetime import UTC, timedelta

import httpx
import pytest

from ha_watcher import checks, cli
from ha_watcher.checks import Context
from ha_watcher.config import ConfigError, PowerCycleConfig, parse_config
from ha_watcher.engine import Watcher
from ha_watcher.power import KasaDriver, PowerError, ShellyGen2Driver, describe_status
from tests.conftest import NOW, TOKEN, FakeHA, base_raw, make_cfg, make_client

CTX = Context(now=NOW)
H = timedelta(hours=1)
M = timedelta(minutes=1)


def opts(name, **kw):
    return {**make_cfg().checks[name].options, **kw}


def ent(eid, state="on", age=None, changed=None, **attrs):
    """A state object; `age` is how long ago it last reported, `changed` since the state changed."""
    st = {"entity_id": eid, "state": state, "attributes": attrs}
    if age is not None:
        st["last_reported"] = (NOW - age).isoformat()
        st["last_updated"] = (NOW - age - H).isoformat()
    if changed is not None:
        st["last_changed"] = (NOW - changed).isoformat()
    return st


def ha_with(*states):
    routes = {"/api/states": (200, list(states))}
    for st in states:
        routes[f"/api/states/{st['entity_id']}"] = (200, st)
    return make_client(FakeHA(routes))


def pc(**kw):
    return PowerCycleConfig(**{"enabled": True, "host": "192.0.2.20", **kw})


# ----------------------------------------------------------------- plug: drivers
def test_shelly_status_is_get_status_only():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"id": 1, "result": {"id": 0, "output": True}})

    st = ShellyGen2Driver(pc(switch_id=2), transport=httpx.MockTransport(handler)).status()
    assert st == {"driver": "shelly", "on": True}
    assert seen == [{"id": 1, "method": "Switch.GetStatus", "params": {"id": 2}}]


def test_shelly_status_errors():
    for handler, text in (
        (lambda r: httpx.Response(401), "401"),
        (lambda r: httpx.Response(200, json={"error": {"message": "bad id"}}), "bad id"),
        (lambda r: httpx.Response(200, json={"result": {}}), "no switch state"),
    ):
        with pytest.raises(PowerError, match=text):
            ShellyGen2Driver(pc(), transport=httpx.MockTransport(handler)).status()

    def boom(r):
        raise httpx.ConnectError("no route")

    with pytest.raises(PowerError, match="unreachable"):
        ShellyGen2Driver(pc(), transport=httpx.MockTransport(boom)).status()


class QueryPlug:
    """A Kasa device that records every call, so a test can prove nothing was switched."""

    is_on = True
    model = "EP10"
    hw_info = {"sw_ver": "1.0.3"}

    class config:  # noqa: N801
        class connection_type:  # noqa: N801
            class encryption_type:  # noqa: N801
                value = "KLAP"

            login_version = 2

    def __init__(self):
        self.calls = []

    async def update(self):
        self.calls.append("update")

    async def turn_off(self):
        self.calls.append("off")

    async def turn_on(self):
        self.calls.append("on")

    async def disconnect(self):
        self.calls.append("disconnect")


def test_kasa_status_reads_details_and_never_switches():
    plug = QueryPlug()

    async def discover(host, username=None, password=None):
        return plug

    st = KasaDriver(pc(driver="kasa"), discover=discover).status()
    assert st["on"] is True and st["model"] == "EP10" and st["firmware"] == "1.0.3"
    assert st["encryption"] == "KLAP" and st["login_version"] == 2
    assert plug.calls == ["update", "disconnect"]  # no "off" / "on"
    assert "model EP10" in describe_status(st)


def test_kasa_status_failures_become_power_errors():
    async def missing(host, **kw):
        return None

    async def broken(host, **kw):
        raise OSError("x")

    with pytest.raises(PowerError, match="not found"):
        KasaDriver(pc(driver="kasa"), discover=missing).status()
    with pytest.raises(PowerError, match="OSError"):
        KasaDriver(pc(driver="kasa"), discover=broken).status()


def test_kasa_status_is_bounded():
    import asyncio

    async def hang(host, **kw):
        await asyncio.sleep(30)

    with pytest.raises(PowerError):
        KasaDriver(pc(driver="kasa"), discover=hang, step_timeout=0.05).status()


# ------------------------------------------------------------------- plug: cli
CLI_CFG = """
home_assistant: {url: "http://192.0.2.10:8123", token: "${HA_TOKEN}"}
checks: {backup: {enabled: false}}
power_cycle: {enabled: true, driver: kasa, host: 192.0.2.20}
"""


class StubDriver:
    def __init__(self, result):
        self.result = result
        self.switched = False

    def status(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def power_cycle(self, off):  # must never be reached
        self.switched = True


def _cfg(tmp_path, monkeypatch, text=CLI_CFG):
    monkeypatch.setenv("HA_TOKEN", TOKEN)
    p = tmp_path / "c.yaml"
    p.write_text(text, encoding="utf-8")
    return cli.load_config(str(p))


def test_check_plug_ok(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    drv = StubDriver({"driver": "kasa", "on": True, "model": "EP10"})
    assert cli._check_plug(cfg, drv) == 0
    assert "Plug check OK" in capsys.readouterr().out and not drv.switched


def test_check_plug_failure_exits_1_with_hint(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    assert cli._check_plug(cfg, StubDriver(PowerError("Kasa failed: AuthenticationError"))) == 1
    err = capsys.readouterr().err
    assert "FAILED" in err and "Shelly plug is the supported choice" in err


def test_check_plug_needs_power_cycle(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch, CLI_CFG.replace("enabled: true", "enabled: false"))
    assert cli._check_plug(cfg, StubDriver({})) == 2


def test_check_plug_flag_runs_through_main(tmp_path, monkeypatch, capsys):
    p = tmp_path / "c.yaml"
    monkeypatch.setenv("HA_TOKEN", TOKEN)
    p.write_text(CLI_CFG.replace("kasa", "shelly"), encoding="utf-8")
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"result": {"output": False}}))
    monkeypatch.setattr(cli, "build_driver", lambda c: ShellyGen2Driver(c, transport=t))
    assert cli.main(["--check-plug", "-c", str(p)]) == 0
    assert "currently off" in capsys.readouterr().out


# ----------------------------------------------------------------- plug: check
def test_plug_check_ok_and_failure():
    ok = Context(now=NOW, plug_status=lambda: {"driver": "shelly", "on": True})
    [r] = checks.check_plug(None, {"driver": "shelly"}, ok)
    assert r.ok and r.key == "plug"

    def dead():
        raise PowerError("Kasa failed: TimeoutError")

    [r] = checks.check_plug(None, {"driver": "kasa"}, Context(now=NOW, plug_status=dead))
    assert not r.ok and "not answering" in r.message and "Shelly plug" in r.message
    [r] = checks.check_plug(None, {"driver": "shelly"}, Context(now=NOW, plug_status=dead))
    assert "Shelly plug" not in r.message
    [r] = checks.check_plug(None, {}, CTX)
    assert not r.ok


def test_plug_check_in_engine_is_query_only(tmp_path):
    class Spy(StubDriver):
        def __init__(self):
            super().__init__({"driver": "shelly", "on": True})
            self.status_calls = 0

        def status(self):
            self.status_calls += 1
            return self.result

    spy = Spy()
    cfg = make_cfg(
        power_cycle={"enabled": True, "host": "192.0.2.20"},
        checks={"plug": {"enabled": True}, "backup": {"enabled": False}},
    )
    w = Watcher(
        cfg,
        make_client(FakeHA(), cfg=cfg),
        [],
        httpx.Client(),
        driver=spy,
        local_tz=UTC,
        state_path=str(tmp_path / "s.json"),
    )
    results = w.run_once(NOW)
    assert [r.ok for r in results if r.key == "plug"] == [True]
    w.run_once(NOW + 10 * M)  # every 24 h: not asked again
    assert spy.status_calls == 1
    w.run_once(NOW + 25 * H)
    assert spy.status_calls == 2 and not spy.switched


def test_plug_check_config_rules():
    with pytest.raises(ConfigError, match="needs power_cycle"):
        parse_config(base_raw(checks={"plug": {"enabled": True}}), env={"HA_TOKEN": TOKEN})
    with pytest.raises(ConfigError, match="at least 300"):
        parse_config(
            base_raw(
                power_cycle={"enabled": True, "host": "192.0.2.20"},
                checks={"plug": {"enabled": True, "every_seconds": 10}},
            ),
            env={"HA_TOKEN": TOKEN},
        )
    cfg = make_cfg()
    assert not cfg.checks["plug"].enabled and cfg.checks["plug"].every_seconds == 86400


# ----------------------------------------------------------------------- stale
def test_stale_lists_old_entities_in_one_result():
    client = ha_with(
        ent("sensor.example_a", age=30 * H),
        ent("sensor.example_b", age=2 * H),
        ent("sensor.example_c", age=50 * H),
        ent("sensor.example_off", "unavailable", age=90 * H),
    )
    o = opts("stale", entity_ids=["sensor.example_a", "sensor.example_b", "sensor.example_c"])
    o["patterns"] = ["sensor.example_off"]
    [r] = checks.check_stale(client, o, CTX)
    assert not r.ok and r.key == "stale" and r.data["count"] == 2
    assert r.message.index("example_c") < r.message.index("example_a")  # oldest first
    assert "example_off" not in r.message and "example_b" not in r.message


def test_stale_overrides_patterns_exclude_and_fallback():
    client = ha_with(
        ent("sensor.example_t1", age=30 * H),
        ent("sensor.example_t2", age=30 * H),
        ent("sensor.example_t3", age=30 * H),
        {"entity_id": "sensor.example_old", "state": "1", "attributes": {}, "last_updated": iso_ago(40 * H)},
    )
    o = opts(
        "stale",
        patterns=["sensor.example_t*", "sensor.example_old"],
        exclude=["*_t3"],
        overrides={"sensor.example_t1": 48},
    )
    [r] = checks.check_stale(client, o, CTX)
    assert r.data["entities"][0].startswith("sensor.example_old")  # last_updated fallback
    assert len(r.data["entities"]) == 2  # t2 and old; t1 overridden, t3 excluded
    assert "t2" in r.message and "t1" not in r.message


def iso_ago(delta):
    return (NOW - delta).isoformat()


def test_stale_all_fresh_and_missing_and_clip():
    client = ha_with(ent("sensor.example_a", age=H))
    [r] = checks.check_stale(client, opts("stale", entity_ids=["sensor.example_a"]), CTX)
    assert r.ok
    [r] = checks.check_stale(client, opts("stale", entity_ids=["sensor.example_missing"]), CTX)
    assert not r.ok and "not found" in r.message
    many = [ent(f"sensor.example_{i:02d}", age=(30 + i) * H) for i in range(15)]
    [r] = checks.check_stale(ha_with(*many), opts("stale", patterns=["sensor.example_*"], max_listed=3), CTX)
    assert r.data["count"] == 15 and "and 12 more" in r.message


def test_stale_startup_grace():
    client = ha_with(ent("sensor.example_a", age=30 * H))
    o = opts("stale", entity_ids=["sensor.example_a"], startup_grace_minutes=10)
    [r] = checks.check_stale(client, o, Context(now=NOW, ha_up_since=NOW - 3 * M))
    assert r.ok and "just started" in r.message
    [r] = checks.check_stale(client, o, Context(now=NOW, ha_up_since=NOW - 11 * M))
    assert not r.ok


def test_stale_config_needs_something_to_watch():
    with pytest.raises(ConfigError, match="entity_ids or patterns"):
        parse_config(base_raw(checks={"stale": {"enabled": True}}), env={"HA_TOKEN": TOKEN})
    with pytest.raises(ConfigError, match="overrides"):
        parse_config(
            base_raw(checks={"stale": {"enabled": True, "patterns": ["a.*"], "overrides": {"a.b": "x"}}}),
            env={"HA_TOKEN": TOKEN},
        )
    with pytest.raises(ConfigError, match="max_age_hours"):
        parse_config(base_raw(checks={"stale": {"max_age_hours": 0}}), env={"HA_TOKEN": TOKEN})


# --------------------------------------------------------------------- battery
def test_battery_low_and_silent_in_one_summary():
    client = ha_with(
        ent("sensor.example_ok", "80", age=H, device_class="battery"),
        ent("sensor.example_low", "12", age=H, device_class="battery"),
        ent("sensor.example_edge", "20", age=H, device_class="battery"),
        ent("sensor.example_silent", "60", age=60 * H, device_class="battery"),
        ent("sensor.example_gone", "unavailable", age=H, device_class="battery"),
        ent("sensor.example_temp", "5", age=H, device_class="temperature"),
        ent("binary_sensor.example_flag", "on", age=H, device_class="battery"),
    )
    [r] = checks.check_battery(client, opts("battery"), CTX)
    assert not r.ok and r.key == "battery"
    assert "1 battery low: sensor.example_low 12%" in r.message
    assert "1 battery not reporting: sensor.example_silent" in r.message
    assert r.data["low"] == ["sensor.example_low 12%"]
    assert "example_edge" not in r.message and "example_temp" not in r.message


def test_battery_overrides_exclude_and_ok():
    client = ha_with(
        ent("sensor.example_a", "15", age=H, device_class="battery"),
        ent("sensor.example_b", "15", age=H, device_class="battery"),
        ent("sensor.example_c", "5", age=H, device_class="battery"),
    )
    o = opts("battery", overrides={"sensor.example_a": 10}, exclude=["sensor.example_c"])
    [r] = checks.check_battery(client, o, CTX)
    assert "example_b 15%" in r.message and "example_a" not in r.message
    o = opts("battery", overrides={"sensor.example_a": 10, "sensor.example_b": 10}, exclude=["*_c"])
    [r] = checks.check_battery(client, o, CTX)
    assert r.ok


def test_battery_silent_option_and_grace():
    client = ha_with(ent("sensor.example_s", "60", age=60 * H, device_class="battery"))
    assert checks.check_battery(client, opts("battery", stale_hours=0), CTX)[0].ok
    assert checks.check_battery(client, opts("battery", stale_hours=72), CTX)[0].ok
    assert not checks.check_battery(client, opts("battery"), CTX)[0].ok
    in_grace = Context(now=NOW, ha_up_since=NOW - 2 * M)
    assert checks.check_battery(client, opts("battery"), in_grace)[0].ok


def test_battery_still_flags_low_during_grace():
    client = ha_with(ent("sensor.example_low", "3", age=H, device_class="battery"))
    [r] = checks.check_battery(client, opts("battery"), Context(now=NOW, ha_up_since=NOW - 2 * M))
    assert not r.ok


# ----------------------------------------------------------------------- stuck
def test_stuck_error_state_after_n_minutes_case_insensitive():
    client = ha_with(
        ent("vacuum.example_robot", "Error", changed=30 * M),
        ent("sensor.example_litter", "error", changed=3 * M),
        ent("sensor.example_fine", "ok", changed=400 * H),
    )
    o = opts(
        "stuck",
        entity_ids=["vacuum.example_robot", "sensor.example_litter", "sensor.example_fine"],
        max_error_minutes=10,
    )
    rs = {r.key: r for r in checks.check_stuck(client, o, CTX)}
    assert not rs["stuck:vacuum.example_robot"].ok
    assert (
        rs["stuck:sensor.example_litter"].ok and "within limit" in rs["stuck:sensor.example_litter"].message
    )
    assert rs["stuck:sensor.example_fine"].ok  # no max_unchanged_hours: age is fine


def test_stuck_unchanged_and_unavailable_and_missing():
    client = ha_with(
        ent("sensor.example_frozen", "42", changed=30 * H),
        ent("sensor.example_moving", "7", changed=H),
        ent("sensor.example_dead", "unavailable", changed=90 * H),
    )
    o = opts(
        "stuck",
        entity_ids=[
            "sensor.example_frozen",
            "sensor.example_moving",
            "sensor.example_dead",
            "sensor.example_nope",
        ],
        max_unchanged_hours=24,
    )
    rs = {r.key: r for r in checks.check_stuck(client, o, CTX)}
    assert (
        not rs["stuck:sensor.example_frozen"].ok
        and "not changed" in rs["stuck:sensor.example_frozen"].message
    )
    assert rs["stuck:sensor.example_moving"].ok
    assert rs["stuck:sensor.example_dead"].ok
    assert not rs["stuck:sensor.example_nope"].ok


def test_stuck_config_rules():
    with pytest.raises(ConfigError, match="entity_ids"):
        parse_config(base_raw(checks={"stuck": {"enabled": True}}), env={"HA_TOKEN": TOKEN})
    with pytest.raises(ConfigError, match="error_states or max_unchanged_hours"):
        parse_config(base_raw(checks={"stuck": {"error_states": []}}), env={"HA_TOKEN": TOKEN})
    cfg = make_cfg()
    assert not any(cfg.checks[n].enabled for n in ("stale", "battery", "stuck", "plug"))


# ---------------------------------------------------------- engine integration
def _engine(tmp_path, fake, **checks_cfg):
    cfg = make_cfg(checks={"backup": {"enabled": False}, **checks_cfg})
    return Watcher(
        cfg,
        make_client(fake, cfg=cfg),
        [],
        httpx.Client(),
        local_tz=UTC,
        state_path=str(tmp_path / "s.json"),
    )


def test_registry_and_needs_api():
    for name in ("stale", "battery", "stuck"):
        assert name in checks.CHECKS and name in checks.NEEDS_API
    assert "plug" in checks.CHECKS and "plug" not in checks.NEEDS_API


def test_stale_grace_follows_a_restart_seen_by_the_engine(tmp_path):
    stale_state = ent("sensor.example_a", age=30 * H)
    fake = FakeHA({"/api/states": (200, [stale_state]), "/api/states/sensor.example_a": (200, stale_state)})
    w = _engine(
        tmp_path, fake, stale={"enabled": True, "every_seconds": 0, "entity_ids": ["sensor.example_a"]}
    )
    assert not [r for r in w.run_once(NOW) if r.key == "stale"][0].ok  # watcher started on a running HA
    fake.routes["/api/"] = (0, httpx.ConnectError("down"))
    w.run_once(NOW + M)
    fake.routes["/api/"] = (200, {"message": "API running."})
    back = NOW + 2 * M
    [r] = [r for r in w.run_once(back) if r.key == "stale"]
    assert r.ok and "just started" in r.message
    [r] = [r for r in w.run_once(back + 11 * M) if r.key == "stale"]
    assert not r.ok


# ------------------------------------------------- plug lockout care, battery include
def test_kasa_plug_check_needs_six_hour_interval():
    kasa = {"power_cycle": {"enabled": True, "driver": "kasa", "host": "192.0.2.20"}}
    with pytest.raises(ConfigError, match="21600"):
        parse_config(
            base_raw(**kasa, checks={"plug": {"enabled": True, "every_seconds": 3600}}),
            env={"HA_TOKEN": TOKEN},
        )
    cfg = parse_config(base_raw(**kasa, checks={"plug": {"enabled": True}}), env={"HA_TOKEN": TOKEN})
    assert cfg.checks["plug"].every_seconds == 86400
    shelly = {"power_cycle": {"enabled": True, "host": "192.0.2.20"}}
    parse_config(
        base_raw(**shelly, checks={"plug": {"enabled": True, "every_seconds": 600}}),
        env={"HA_TOKEN": TOKEN},
    )


def test_plug_check_backs_off_after_failure_and_resets(tmp_path):
    class Flaky:
        def __init__(self):
            self.calls = 0
            self.fail = True

        def status(self):
            self.calls += 1
            if self.fail:
                raise PowerError("Kasa failed: AuthenticationError")
            return {"driver": "kasa", "on": True}

    drv = Flaky()
    cfg = make_cfg(
        power_cycle={"enabled": True, "driver": "kasa", "host": "192.0.2.20"},
        checks={"plug": {"enabled": True}, "backup": {"enabled": False}},
    )
    w = Watcher(
        cfg,
        make_client(FakeHA(), cfg=cfg),
        [],
        httpx.Client(),
        driver=drv,
        local_tz=UTC,
        state_path=str(tmp_path / "s.json"),
    )
    w.run_once(NOW)  # fail 1: next try after 2 days
    w.run_once(NOW + 25 * H)
    w.run_once(NOW + 47 * H)
    assert drv.calls == 1
    w.run_once(NOW + 49 * H)  # fail 2: next try after 4 days
    assert drv.calls == 2 and w.state["plug_fails"] == 2
    w.run_once(NOW + 49 * H + 3 * 24 * H)
    assert drv.calls == 2
    drv.fail = False
    w.run_once(NOW + 49 * H + 5 * 24 * H)
    assert drv.calls == 3 and w.state["plug_fails"] == 0
    w.run_once(NOW + 49 * H + 5 * 24 * H + 25 * H)  # back to the normal day
    assert drv.calls == 4


def test_plug_backoff_is_capped_at_seven_days():
    from ha_watcher.engine import PLUG_MAX_WAIT

    assert PLUG_MAX_WAIT == 7 * 86400


def test_check_plug_makes_exactly_one_attempt():
    calls = []

    async def discover(host, username=None, password=None):
        calls.append(host)
        raise OSError("login refused")

    with pytest.raises(PowerError):
        KasaDriver(pc(driver="kasa"), discover=discover).status()
    assert len(calls) == 1


def test_battery_include_only_and_exclude_globs():
    client = ha_with(
        ent("sensor.example_door_battery", "5", age=H, device_class="battery"),
        ent("sensor.example_phone_battery_level", "4", age=H, device_class="battery"),
        ent("sensor.example_tablet_battery_level", "3", age=H, device_class="battery"),
        ent("sensor.example_watch_battery", "2", age=H, device_class="battery"),
    )
    [r] = checks.check_battery(client, opts("battery", exclude=["*phone*", "*tablet*", "*watch*"]), CTX)
    assert r.data["low"] == ["sensor.example_door_battery 5%"]
    [r] = checks.check_battery(client, opts("battery", include=["*watch*", "*door*"]), CTX)
    assert len(r.data["low"]) == 2 and "phone" not in r.message
    [r] = checks.check_battery(client, opts("battery", include=["*door*"], exclude=["*door*"]), CTX)
    assert r.ok  # exclude wins over include
    with pytest.raises(ConfigError, match="include"):
        parse_config(base_raw(checks={"battery": {"include": "sensor.x"}}), env={"HA_TOKEN": TOKEN})
