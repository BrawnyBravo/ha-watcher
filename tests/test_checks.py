from datetime import timedelta

import httpx

from ha_watcher import checks
from ha_watcher.checks import Context, detect_busy
from tests.conftest import NOW, FakeHA, FakeWS, make_cfg, make_client

CTX = Context(now=NOW)


def opts(name, **kw):
    return {**make_cfg().checks[name].options, **kw}


def iso(delta):
    return (NOW - delta).isoformat()


# ----------------------------------------------------------------------- api
def test_api_ok(fake_ha):
    [r] = checks.check_api(make_client(fake_ha), opts("api"), CTX)
    assert r.ok and r.key == "api"


def test_api_bad_token():
    [r] = checks.check_api(make_client(FakeHA({"/api/": (401, {})})), opts("api"), CTX)
    assert not r.ok and "Token rejected" in r.message


def test_api_down():
    fake = FakeHA({"/api/": (0, httpx.ConnectError("x"))})
    [r] = checks.check_api(make_client(fake), opts("api"), CTX)
    assert not r.ok and "unreachable" in r.message


def test_api_http_error():
    [r] = checks.check_api(make_client(FakeHA({"/api/": (502, {})})), opts("api"), CTX)
    assert not r.ok and "502" in r.message


def test_api_slow(fake_ha, monkeypatch):
    client = make_client(fake_ha)
    real = client.get

    def slow(path):
        r = real(path)
        r.elapsed_ms = 9000
        return r

    monkeypatch.setattr(client, "get", slow)
    [r] = checks.check_api(client, opts("api", max_response_ms=5000), CTX)
    assert not r.ok and "slow" in r.message


# ---------------------------------------------------------------------- core
def test_core_running(fake_ha):
    rs = checks.check_core(make_client(fake_ha), opts("core"), CTX)
    assert [r.ok for r in rs] == [True, True]


def test_core_not_running_and_safe_mode():
    fake = FakeHA({"/api/config": (200, {"state": "STARTING", "safe_mode": True, "recovery_mode": False})})
    core, safe = checks.check_core(make_client(fake), opts("core"), CTX)
    assert not core.ok and "STARTING" in core.message
    assert not safe.ok and "safe mode" in safe.message


def test_core_recovery_mode():
    fake = FakeHA({"/api/config": (200, {"state": "RUNNING", "recovery_mode": True})})
    _, safe = checks.check_core(make_client(fake), opts("core"), CTX)
    assert not safe.ok and "recovery mode" in safe.message


def test_core_safe_mode_alert_can_be_off():
    fake = FakeHA({"/api/config": (200, {"state": "RUNNING", "safe_mode": True})})
    rs = checks.check_core(make_client(fake), opts("core", alert_on_safe_mode=False), CTX)
    assert len(rs) == 1 and rs[0].ok


# -------------------------------------------------------------------- backup
def backup_ws(backups, completed=None, state="idle"):
    return FakeWS(
        {"backup/info": {"backups": backups, "last_completed_automatic_backup": completed, "state": state}}
    )


def test_backup_fresh_via_websocket(fake_ha):
    ws = backup_ws(
        [
            {"backup_id": "a", "date": iso(timedelta(hours=30))},
            {"backup_id": "b", "date": iso(timedelta(hours=5))},
        ]
    )
    [r] = checks.check_backup(make_client(fake_ha, ws), opts("backup"), CTX)
    assert r.ok and r.data["age_hours"] == 5.0 and r.data["manager_state"] == "idle"


def test_backup_stale_via_websocket(fake_ha):
    ws = backup_ws([{"date": iso(timedelta(hours=50))}])
    [r] = checks.check_backup(make_client(fake_ha, ws), opts("backup"), CTX)
    assert not r.ok and "limit 26h" in r.message


def test_backup_uses_last_completed_when_list_empty(fake_ha):
    ws = backup_ws([], completed=iso(timedelta(hours=2)))
    [r] = checks.check_backup(make_client(fake_ha, ws), opts("backup"), CTX)
    assert r.ok


def test_backup_none(fake_ha):
    [r] = checks.check_backup(make_client(fake_ha, backup_ws([])), opts("backup"), CTX)
    assert not r.ok and r.message == "No backups found"


def test_backup_ws_error(fake_ha):
    ws = FakeWS({"backup/info": ("error", "unauthorized", "Unauthorized")})
    [r] = checks.check_backup(make_client(fake_ha, ws), opts("backup"), CTX)
    assert not r.ok and "Cannot read backup status" in r.message


def test_backup_via_sensor():
    eid = "sensor.backup_last_successful_automatic_backup"
    fake = FakeHA({f"/api/states/{eid}": (200, {"entity_id": eid, "state": iso(timedelta(hours=3))})})
    [r] = checks.check_backup(make_client(fake), opts("backup", source="sensor"), CTX)
    assert r.ok


def test_backup_sensor_unknown_and_missing(fake_ha):
    eid = "sensor.backup_last_successful_automatic_backup"
    fake = FakeHA({f"/api/states/{eid}": (200, {"entity_id": eid, "state": "unknown"})})
    [r] = checks.check_backup(make_client(fake), opts("backup", source="sensor"), CTX)
    assert not r.ok and "unknown" in r.message
    [r] = checks.check_backup(make_client(fake_ha), opts("backup", source="sensor"), CTX)
    assert not r.ok and "not found" in r.message


# ---------------------------------------------------------------- supervisor
def sup_ws(unhealthy=(), addons=None):
    def handler(msg):
        if msg["endpoint"] == "/resolution/info":
            return {"unhealthy": list(unhealthy), "unsupported": []}
        return {"addons": addons or []}

    return FakeWS({"supervisor/api": handler})


def test_supervisor_healthy_and_addons(fake_ha):
    ws = sup_ws(
        addons=[
            {"slug": "core_mosquitto", "name": "Mosquitto broker", "state": "started"},
            {"slug": "core_zigbee2mqtt", "name": "Zigbee2MQTT", "state": "stopped"},
        ]
    )
    o = opts("supervisor", addons=["core_mosquitto", "core_zigbee2mqtt", "core_missing"])
    rs = {r.key: r for r in checks.check_supervisor(make_client(fake_ha, ws), o, CTX)}
    assert rs["supervisor:health"].ok
    assert rs["supervisor:addon:core_mosquitto"].ok
    assert not rs["supervisor:addon:core_zigbee2mqtt"].ok
    assert "not installed" in rs["supervisor:addon:core_missing"].message
    assert ws.sent[1]["endpoint"] == "/resolution/info" and ws.sent[1]["method"] == "get"


def test_supervisor_unhealthy(fake_ha):
    [r] = checks.check_supervisor(make_client(fake_ha, sup_ws(unhealthy=["docker"])), opts("supervisor"), CTX)
    assert not r.ok and "docker" in r.message


def test_supervisor_non_admin_hint(fake_ha):
    ws = FakeWS({"supervisor/api": ("error", "unauthorized", "Unauthorized")})
    [r] = checks.check_supervisor(make_client(fake_ha, ws), opts("supervisor"), CTX)
    assert r.key == "supervisor" and "administrator" in r.message


def test_supervisor_absent_hint(fake_ha):
    [r] = checks.check_supervisor(make_client(fake_ha, FakeWS({})), opts("supervisor"), CTX)
    assert "no Supervisor" in r.message


# ------------------------------------------------------------------ entities
def test_entities_mixed():
    fake = FakeHA(
        {
            "/api/states/sensor.living_room_lamp_power": (
                200,
                {"state": "12.5", "last_changed": iso(timedelta())},
            ),
            "/api/states/binary_sensor.coordinator": (
                200,
                {"state": "unavailable", "last_changed": iso(timedelta(minutes=40))},
            ),
            "/api/states/sensor.blip": (200, {"state": "unknown", "last_changed": iso(timedelta(minutes=2))}),
        }
    )
    o = opts(
        "entities",
        entity_ids=[
            "sensor.living_room_lamp_power",
            "binary_sensor.coordinator",
            "sensor.blip",
            "sensor.gone",
        ],
    )
    rs = {r.key: r for r in checks.check_entities(make_client(fake), o, CTX)}
    assert rs["entity:sensor.living_room_lamp_power"].ok
    assert not rs["entity:binary_sensor.coordinator"].ok
    assert "40m" in rs["entity:binary_sensor.coordinator"].message
    assert rs["entity:sensor.blip"].ok  # unavailable, but under the 15 minute limit
    assert not rs["entity:sensor.gone"].ok


# -------------------------------------------------------------------- memory
def test_memory_threshold():
    eid = "sensor.system_monitor_memory_usage"
    for value, ok in (("71.2", True), ("95", False), ("unavailable", False)):
        fake = FakeHA(
            {f"/api/states/{eid}": (200, {"state": value, "attributes": {"unit_of_measurement": "%"}})}
        )
        [r] = checks.check_memory(make_client(fake), opts("memory", entity_id=eid, max_percent=90), CTX)
        assert r.ok is ok, value


# ---------------------------------------------------------------------- host
def test_host_tcp_ok_and_fail():
    calls = []
    ctx = Context(now=NOW, tcp_connect=lambda h, p, t: calls.append((h, p)))
    [r] = checks.check_host(None, opts("host", host="192.0.2.10"), ctx)
    assert r.ok and calls == [("192.0.2.10", 8123)]

    def refuse(h, p, t):
        raise ConnectionRefusedError

    [r] = checks.check_host(None, opts("host", host="192.0.2.10"), Context(now=NOW, tcp_connect=refuse))
    assert not r.ok and "ConnectionRefusedError" in r.message


def test_host_ping():
    o = opts("host", host="192.0.2.10", method="ping")
    assert checks.check_host(None, o, Context(now=NOW, ping=lambda h, t: True))[0].ok
    assert not checks.check_host(None, o, Context(now=NOW, ping=lambda h, t: False))[0].ok


def test_host_tcp_real_socket_refused():
    # Port 9 on localhost is almost never listening: exercises the real socket path.
    [r] = checks.check_host(None, opts("host", host="127.0.0.1", port=9, timeout_seconds=1), CTX)
    assert not r.ok


# ---------------------------------------------------------------------- busy
def test_detect_busy():
    fake = FakeHA(
        {
            "/api/states": (
                200,
                [
                    {
                        "entity_id": "update.home_assistant_core_update",
                        "state": "on",
                        "attributes": {"in_progress": True},
                    },
                    {
                        "entity_id": "update.some_addon_update",
                        "state": "on",
                        "attributes": {"in_progress": False},
                    },
                    {
                        "entity_id": "sensor.backup_backup_manager_state",
                        "state": "create_backup",
                        "attributes": {},
                    },
                    {"entity_id": "sensor.living_room_lamp_power", "state": "3", "attributes": {}},
                ],
            )
        }
    )
    reasons = detect_busy(make_client(fake))
    assert reasons == ["update.home_assistant_core_update is installing", "backup manager is create_backup"]


def test_detect_busy_idle(fake_ha):
    assert detect_busy(make_client(fake_ha)) == []


def test_parse_ts_and_fmt_age():
    assert checks.parse_ts("2026-01-15T12:00:00Z") == NOW
    assert checks.parse_ts("garbage") is None and checks.parse_ts(None) is None
    assert checks.fmt_age(30) == "30s" and checks.fmt_age(600) == "10m"
    assert checks.fmt_age(3 * 3600) == "3.0h" and checks.fmt_age(3 * 86400) == "3.0d"
