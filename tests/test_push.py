"""The optional status push to the HA Watcher companion integration (payload contract v1)."""

import hashlib
import hmac
import importlib.util
import json
import logging
import os
import re
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from ha_watcher import __version__
from ha_watcher.config import ConfigError
from ha_watcher.engine import Watcher
from ha_watcher.push import MAX_BODY, MAX_CHECKS, build_payload, check_names
from tests.conftest import NOW, FakeHA, make_cfg, make_client
from tests.test_engine import API_DOWN, Inbox

M = timedelta(minutes=1)
URL = "http://192.0.2.10:8123/api/webhook/" + "ab" * 32
NAME_RE = re.compile(r"[a-z0-9_]{1,40}")


# ------------------------------------------------- the contract, from PAYLOAD.md
def _ts(value, optional=False):
    if value is None and optional:
        return
    assert isinstance(value, str)
    assert datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None


def _int(value, low, high=10**9):
    assert isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def assert_contract(payload: dict, body: bytes | None = None) -> None:
    """Every rule of docs/PAYLOAD.md (v1) that the sender is responsible for."""
    if body is not None:
        assert len(body) <= 65536
        assert json.loads(body) == payload
    required = {
        "v",
        "watcher_id",
        "version",
        "sent_at",
        "interval_s",
        "checks",
        "outage",
        "alerts_sent_24h",
        "last_power_cycle",
        "last_recovery_command",
    }
    assert required <= set(payload)
    assert payload["v"] == 1
    assert isinstance(payload["watcher_id"], str) and 1 <= len(payload["watcher_id"]) <= 128
    assert isinstance(payload["version"], str) and len(payload["version"]) <= 128
    _ts(payload["sent_at"])
    _int(payload["interval_s"], 1, 86400)
    _int(payload["alerts_sent_24h"], 0)
    _ts(payload["last_power_cycle"], optional=True)
    _ts(payload["last_recovery_command"], optional=True)
    checks = payload["checks"]
    assert isinstance(checks, dict) and len(checks) <= 32
    for name, c in checks.items():
        assert NAME_RE.fullmatch(name), name
        assert c["ok"] is None or isinstance(c["ok"], bool)
        _ts(c["since"], optional=True)
        assert isinstance(c["detail"], str) and len(c["detail"]) <= 1024
        _int(c["failures"], 0, 10**6)
    outage = payload["outage"]
    assert isinstance(outage["active"], bool)
    _ts(outage["since"], optional=True)
    assert (outage["since"] is None) == (not outage["active"])


# ------------------------------------------------------------- name mapping
def test_check_names_follow_the_contract_examples():
    names = check_names(
        ["api", "core:safe_mode", "supervisor:health", "addon:mosquitto", "entity:sensor.example_temperature"]
    )
    assert names == {
        "addon:mosquitto": "addon_mosquitto",
        "api": "api",
        "core:safe_mode": "core_safe_mode",
        "entity:sensor.example_temperature": "entity_sensor_example_temperature",
        "supervisor:health": "supervisor_health",
    }


def test_long_names_are_cut_and_hashed():
    key = "entity:sensor.living_room_lamp_power_consumption_today"
    name = check_names([key])[key]
    sha = hashlib.sha256(key.encode()).hexdigest()[:8]
    expected = re.sub(r"[^a-z0-9_]", "_", key.lower())[:31] + "_" + sha
    assert name == expected and len(name) == 40


def test_collisions_go_to_the_later_key_in_sorted_order():
    # "a:b" and "a.b" both map to "a_b"; "a.b" sorts first and keeps it.
    names = check_names(["a:b", "a.b"])
    assert names["a.b"] == "a_b"
    assert names["a:b"] == "a_b"[:31] + "_" + hashlib.sha256(b"a:b").hexdigest()[:8]


# ------------------------------------------------------------------ payload
def watcher(tmp_path, fake=None, push=None, **over):
    sent = []

    def handler(req):
        sent.append(req)
        return httpx.Response(200, json={"ok": True})

    cfg = make_cfg(home_assistant_push={"url": URL, **(push or {})}, **over)
    w = Watcher(
        cfg,
        make_client(fake or FakeHA(), cfg=cfg),
        [Inbox()],
        httpx.Client(transport=httpx.MockTransport(handler)),
        local_tz=UTC,
        state_path=str(tmp_path / "state.json"),
    )
    w.push.clock = _ticking_clock()
    return w, sent


def _ticking_clock(start=None, step=60.0):
    t = [(NOW.timestamp() if start is None else start) - step]

    def clock():
        t[0] += step
        return t[0]

    return clock


def test_pushes_a_valid_payload_every_interval(tmp_path):
    w, sent = watcher(tmp_path, push={"every_seconds": 120})
    for i in range(5):
        w.run_once(NOW + i * M)
    assert len(sent) == 3  # minutes 0, 2, 4
    req = sent[0]
    assert req.method == "POST" and str(req.url) == URL
    assert req.headers["Content-Type"] == "application/json"
    payload = json.loads(req.content)
    assert_contract(payload, req.content)
    assert payload["version"] == __version__ and payload["interval_s"] == 120
    assert payload["checks"]["api"]["ok"] is True
    assert payload["checks"]["core_safe_mode"]["ok"] is True
    assert payload["outage"] == {"active": False, "since": None}
    assert "X-HA-Watcher-Signature" not in req.headers


def test_payload_during_an_outage(tmp_path):
    fake = FakeHA(API_DOWN)
    w, sent = watcher(tmp_path, fake, checks={"host": {"enabled": True, "host": "192.0.2.10"}})
    w.ctx_hooks = {"tcp_connect": lambda h, p, t: None}
    for i in range(4):
        w.run_once(NOW + i * M)
    payload = json.loads(sent[-1].content)
    assert_contract(payload, sent[-1].content)
    api = payload["checks"]["api"]
    assert api["ok"] is False and api["failures"] == 4 and api["since"] == NOW.isoformat()
    assert payload["checks"]["host"]["ok"] is True
    assert payload["checks"]["core"] == {"ok": None, "since": None, "detail": "not run yet", "failures": 0}
    assert payload["outage"] == {"active": True, "since": NOW.isoformat()}
    assert payload["alerts_sent_24h"] == 1


def test_signed_when_a_secret_is_set(tmp_path):
    w, sent = watcher(tmp_path, push={"secret": "s3cret-0123456789"})
    w.run_once(NOW)
    req = sent[0]
    stamp = req.headers["X-HA-Watcher-Timestamp"]
    assert stamp.isdigit()
    mac = hmac.new(b"s3cret-0123456789", stamp.encode() + b"." + req.content, hashlib.sha256).hexdigest()
    assert req.headers["X-HA-Watcher-Signature"] == f"sha256={mac}"


def test_more_than_32_checks_sends_the_first_32_in_sorted_order(tmp_path):
    ids = [f"sensor.s{i:02d}" for i in range(40)]
    states = {f"/api/states/{e}": (200, {"state": "on"}) for e in ids}
    w, sent = watcher(tmp_path, FakeHA(states), checks={"entities": {"enabled": True, "entity_ids": ids}})
    w.run_once(NOW)
    payload = json.loads(sent[0].content)
    assert_contract(payload, sent[0].content)
    assert len(payload["checks"]) == MAX_CHECKS == 32
    everything = sorted(["api", "core", "core:safe_mode", *(f"entity:{e}" for e in ids)])
    assert sorted(payload["checks"]) == sorted(check_names(everything[:32]).values())


def test_body_stays_under_64_kib_with_huge_details(tmp_path):
    w, _ = watcher(tmp_path)
    for i in range(32):
        w.state["checks"][f"entity:sensor.x{i}"] = {
            "check": "entities",
            "ok": False,
            "failures": 10**12,
            "alerting": True,
            "first_failure": NOW.isoformat(),
            "last_alert": NOW.isoformat(),
            "message": "☃" * 5000,
        }
    payload, body = build_payload(w, NOW)
    assert len(body) <= MAX_BODY
    assert_contract(payload, body)
    assert all(c["failures"] == 10**6 for n, c in payload["checks"].items() if n.startswith("entity_"))


def test_push_failure_is_logged_once_never_with_the_url_and_alerts_still_go_out(tmp_path, caplog):
    def down(req):
        raise httpx.ConnectError("refused")

    fake = FakeHA(API_DOWN)
    cfg = make_cfg(home_assistant_push={"url": URL})
    inbox = Inbox()
    w = Watcher(
        cfg,
        make_client(fake, cfg=cfg),
        [inbox],
        httpx.Client(transport=httpx.MockTransport(down)),
        local_tz=UTC,
        state_path=str(tmp_path / "state.json"),
    )
    with caplog.at_level(logging.DEBUG):
        for i in range(10):
            w.run_once(NOW + i * M)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and "push" in r.getMessage()]
    assert len(warnings) == 1
    assert "ab" * 32 not in caplog.text and "webhook" not in caplog.text
    assert len(inbox.msgs) == 1 and "API unreachable" in inbox.msgs[0].body


def test_no_push_without_a_url(tmp_path):
    sent = []
    cfg = make_cfg()
    w = Watcher(
        cfg,
        make_client(FakeHA(), cfg=cfg),
        [],
        httpx.Client(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200))),
        local_tz=UTC,
        state_path=str(tmp_path / "state.json"),
    )
    w.run_once(NOW)
    assert sent == [] and not cfg.home_assistant_push.enabled


# ------------------------------------------------------------------- config
@pytest.mark.parametrize(
    "push, msg",
    [
        ({"url": "192.0.2.10:8123/api/webhook/x"}, "home_assistant_push.url"),
        ({"url": URL, "every_seconds": 0}, "home_assistant_push.every_seconds"),
        ({"url": URL, "every_seconds": 86401}, "home_assistant_push.every_seconds"),
        ({"url": URL, "timeout_seconds": 0}, "home_assistant_push.timeout_seconds"),
        ({"url": URL, "watcher_id": 5}, "home_assistant_push.watcher_id"),
        ({"url": URL, "nope": 1}, "unknown key"),
    ],
)
def test_config_errors(push, msg):
    with pytest.raises(ConfigError, match=msg):
        make_cfg(home_assistant_push=push)


def test_url_and_secret_are_not_in_the_config_repr():
    cfg = make_cfg(home_assistant_push={"url": URL, "secret": "s3cret-0123456789", "every_seconds": "30"})
    assert cfg.home_assistant_push.enabled and cfg.home_assistant_push.every_seconds == 30
    assert "ab" * 32 not in repr(cfg) and "s3cret-0123456789" not in repr(cfg)


def test_empty_url_from_an_unset_variable_means_off():
    cfg = make_cfg(home_assistant_push={"url": "", "secret": ""})
    assert not cfg.home_assistant_push.enabled and cfg.home_assistant_push.secret is None


# --------------------------------------------- the companion's own validator
def _companion_validator():
    """Load custom_components/ha_watcher/payload.py from a companion checkout, if one is
    named in HA_WATCHER_COMPANION and voluptuous is installed; Home Assistant itself is
    stubbed (only its datetime parser is used)."""
    root = os.environ.get("HA_WATCHER_COMPANION")
    if not root:
        pytest.skip("set HA_WATCHER_COMPANION to a companion checkout to run this")
    pytest.importorskip("voluptuous")
    pkg = Path(root) / "custom_components" / "ha_watcher"
    dt = types.ModuleType("homeassistant.util.dt")
    dt.parse_datetime = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
    for name in ("homeassistant", "homeassistant.util"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["homeassistant.util.dt"] = dt
    sys.modules["homeassistant.util"].dt = dt
    package = types.ModuleType("companion")
    package.__path__ = [str(pkg)]
    sys.modules["companion"] = package
    for mod in ("const", "payload"):
        spec = importlib.util.spec_from_file_location(f"companion.{mod}", pkg / f"{mod}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"companion.{mod}"] = module
        spec.loader.exec_module(module)
    return sys.modules["companion.payload"].validate_payload


def test_payload_passes_the_companion_validator(tmp_path):
    validate = _companion_validator()
    fake = FakeHA(API_DOWN)
    w, sent = watcher(tmp_path, fake)
    for i in range(4):
        w.run_once(NOW + i * M)
    payload = json.loads(sent[-1].content)
    out = validate(payload)
    assert set(out["checks"]) == set(payload["checks"])


# ----------------------------------------------------------- critic round 2
def test_names_are_computed_over_the_full_configured_set():
    from ha_watcher.push import configured_keys

    cfg = make_cfg(
        checks={
            "entities": {"enabled": True, "entity_ids": ["sensor.b", "sensor.a"]},
            "supervisor": {"enabled": True, "addons": ["core_mosquitto"]},
        }
    )
    keys = configured_keys(cfg)
    assert {"api", "core", "core:safe_mode", "entity:sensor.a", "entity:sensor.b"} <= set(keys)
    assert {"supervisor", "supervisor:health", "supervisor:addon:core_mosquitto", "entities"} <= set(keys)
    assert "backup" not in keys  # disabled in these tests


def test_a_name_does_not_change_when_a_colliding_key_is_absent_from_the_report(tmp_path):
    # entity:sensor.a.b and entity:sensor.a_b both map to entity_sensor_a_b.
    ids = ["sensor.a_b", "sensor.a.b"]
    states = {"/api/states/sensor.a_b": (200, {"state": "on"})}
    w, sent = watcher(tmp_path, FakeHA(states), checks={"entities": {"enabled": True, "entity_ids": ids}})
    w.run_once(NOW)
    first = {c["detail"]: n for n, c in json.loads(sent[0].content)["checks"].items()}
    w.state["checks"].pop("entity:sensor.a.b")  # the colliding key is not in this report
    w.push.last = None
    w.push.run(NOW + M)
    second = {c["detail"]: n for n, c in json.loads(sent[1].content)["checks"].items()}
    [detail] = [d for d in first if "sensor.a_b is on" in d]
    assert second[detail] == first[detail]
    assert first[detail] != "entity_sensor_a_b"  # sensor.a.b sorts first and keeps the plain name


def test_outage_is_only_home_assistant_itself_being_down(tmp_path):
    old = {"state": "unavailable", "last_changed": "2000-01-01T00:00:00+00:00"}
    w, sent = watcher(
        tmp_path,
        FakeHA({"/api/states/sensor.x": (200, old)}),
        checks={"entities": {"enabled": True, "entity_ids": ["sensor.x"]}},
    )
    w.run_once(NOW)
    payload = json.loads(sent[0].content)
    assert payload["checks"]["entity_sensor_x"]["ok"] is False
    assert payload["outage"] == {"active": False, "since": None}


def test_alerts_sent_counts_only_delivered_problem_alerts(tmp_path):
    from ha_watcher.notifiers import Message

    fake = FakeHA(API_DOWN)
    w, sent = watcher(tmp_path, fake)
    w.notifiers = []  # nothing receives anything
    for i in range(4):
        w.run_once(NOW + i * M)
    assert json.loads(sent[-1].content)["alerts_sent_24h"] == 0
    w.notifiers = [Inbox()]
    w._send(Message("x: power cycle", "cycled", "alert"), NOW + 5 * M, bypass_limit=True)
    assert w.state["alerts_24h"] == []


def test_sent_at_and_signature_use_the_send_time_and_strictly_increase(tmp_path):
    w, sent = watcher(tmp_path, push={"secret": "s3cret-0123456789", "every_seconds": 1})
    later = NOW + timedelta(minutes=7)
    w.push.clock = lambda: later.timestamp()  # the loop started 7 minutes earlier
    w.run_once(NOW)
    assert json.loads(sent[0].content)["sent_at"] == later.isoformat()
    assert sent[0].headers["X-HA-Watcher-Timestamp"] == str(int(later.timestamp()))
    w.run_once(NOW + M)  # same wall-clock second: no second report
    assert len(sent) == 1


def _drip_server():
    """A local HTTP server that sends headers, then one body byte every 0.2 s for 10 s."""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.2)
    stop = threading.Event()

    def drip(conn):
        try:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\n")
            for _ in range(50):
                if stop.is_set():
                    break
                conn.sendall(b"x")
                stop.wait(0.2)
        except OSError:
            pass
        finally:
            conn.close()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            threading.Thread(target=drip, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{srv.getsockname()[1]}", stop, srv


@pytest.fixture
def drip():
    url, stop, srv = _drip_server()
    yield url
    stop.set()
    srv.close()


def test_a_dripping_push_endpoint_cannot_hold_the_loop(tmp_path, drip):
    import time

    cfg = make_cfg(home_assistant_push={"url": drip + "/api/webhook/x", "timeout_seconds": 1})
    w = Watcher(
        cfg,
        make_client(FakeHA(), cfg=cfg),
        [],
        httpx.Client(timeout=1),
        local_tz=UTC,
        state_path=str(tmp_path / "s.json"),
    )
    start = time.monotonic()
    w.push.run(NOW)
    assert time.monotonic() - start < 2.5
    assert w.push.failing


def test_other_outbound_calls_are_bounded_too(tmp_path, drip):
    import time

    from ha_watcher.config import HomeAssistantConfig
    from ha_watcher.ha import HAClient, HAError
    from ha_watcher.notifiers import Message, WebhookNotifier, send_all

    start = time.monotonic()
    with pytest.raises(HAError):
        HAClient(HomeAssistantConfig(url=drip, token="t", timeout_seconds=0.5)).get("/api/")
    assert time.monotonic() - start < 2.5

    hook = WebhookNotifier({"url": drip + "/hook"}, httpx.Client(timeout=0.5))
    hook.deadline = 1.0
    start = time.monotonic()
    assert send_all([hook], Message("t", "b")) == 0
    assert time.monotonic() - start < 2.5

    cfg = make_cfg(deadman={"url": drip + "/ping"})
    w = Watcher(
        cfg,
        make_client(FakeHA(), cfg=cfg),
        [],
        httpx.Client(timeout=0.5),
        local_tz=UTC,
        state_path=str(tmp_path / "s2.json"),
    )
    w.deadman_deadline = 1.0
    start = time.monotonic()
    w._deadman(NOW)
    assert time.monotonic() - start < 2.5 and w.state["deadman_last"] is None


def test_a_short_signing_secret_is_refused():
    with pytest.raises(ConfigError, match="at least 16"):
        make_cfg(home_assistant_push={"url": URL, "secret": "short"})
    assert make_cfg(home_assistant_push={"url": URL, "secret": "x" * 16}).home_assistant_push.secret


def test_signing_secret_is_trimmed_like_the_integration_does():
    """The integration trims the secret it saves, so the watcher must sign with the trimmed one."""
    cfg = make_cfg(home_assistant_push={"url": URL, "secret": "  s3cret-0123456789\n"})
    assert cfg.home_assistant_push.secret == "s3cret-0123456789"
    # The length rule applies after trimming, as in the integration.
    with pytest.raises(ConfigError, match="at least 16"):
        make_cfg(home_assistant_push={"url": URL, "secret": "  " + "x" * 15 + "  "})


def test_a_trimmed_secret_signs_reports(tmp_path):
    w, sent = watcher(tmp_path, push={"secret": " s3cret-0123456789 "})
    w.run_once(NOW)
    req = sent[0]
    stamp = req.headers["X-HA-Watcher-Timestamp"]
    mac = hmac.new(b"s3cret-0123456789", stamp.encode() + b"." + req.content, hashlib.sha256).hexdigest()
    assert req.headers["X-HA-Watcher-Signature"] == f"sha256={mac}"
