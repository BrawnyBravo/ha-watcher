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
    return w, sent


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
    w, sent = watcher(tmp_path, push={"secret": "s3cret"})
    w.run_once(NOW)
    req = sent[0]
    stamp = req.headers["X-HA-Watcher-Timestamp"]
    assert stamp.isdigit()
    mac = hmac.new(b"s3cret", stamp.encode() + b"." + req.content, hashlib.sha256).hexdigest()
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
    cfg = make_cfg(home_assistant_push={"url": URL, "secret": "s3cret", "every_seconds": "30"})
    assert cfg.home_assistant_push.enabled and cfg.home_assistant_push.every_seconds == 30
    assert "ab" * 32 not in repr(cfg) and "s3cret" not in repr(cfg)


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
