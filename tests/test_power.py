import json
from datetime import timedelta

import httpx
import pytest

from ha_watcher.config import PowerCycleConfig
from ha_watcher.power import KasaDriver, PowerError, ShellyGen2Driver, decide, run_cycle
from tests.conftest import NOW


def pc(**kw):
    return PowerCycleConfig(**{"enabled": True, "host": "192.0.2.20", **kw})


# --------------------------------------------------------------------- shelly
def test_shelly_sends_switch_set_with_toggle_after():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"id": 1, "result": {"was_on": True}})

    ShellyGen2Driver(pc(switch_id=1), transport=httpx.MockTransport(handler)).power_cycle(15)
    [req] = seen
    assert str(req.url) == "http://192.0.2.20/rpc"
    assert json.loads(req.content) == {
        "id": 1,
        "method": "Switch.Set",
        "params": {"id": 1, "on": False, "toggle_after": 15},
    }


def test_shelly_rpc_error():
    t = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"id": 1, "error": {"code": -103, "message": "bad id"}})
    )
    with pytest.raises(PowerError, match="bad id"):
        ShellyGen2Driver(pc(), transport=t).power_cycle(15)


def test_shelly_http_401_and_unreachable():
    t = httpx.MockTransport(lambda r: httpx.Response(401))
    with pytest.raises(PowerError, match="401"):
        ShellyGen2Driver(pc(), transport=t).power_cycle(15)

    def boom(r):
        raise httpx.ConnectError("no route")

    with pytest.raises(PowerError, match="unreachable"):
        ShellyGen2Driver(pc(), transport=httpx.MockTransport(boom)).power_cycle(15)


def test_shelly_digest_auth_when_password_set():
    d = ShellyGen2Driver(pc(password="pw"))
    assert isinstance(d.http.auth, httpx.DigestAuth)


# ----------------------------------------------------------------------- kasa
class FakePlug:
    def __init__(self, fail_on=0):
        self.calls = []
        self.fail_on = fail_on

    async def update(self):
        self.calls.append("update")

    async def turn_off(self):
        self.calls.append("off")

    async def turn_on(self):
        self.calls.append("on")
        if self.fail_on:
            self.fail_on -= 1
            raise OSError("flaky")

    async def disconnect(self):
        self.calls.append("disconnect")


def kasa(plug, sleeps):
    async def discover(host, username=None, password=None):
        assert host == "192.0.2.20"
        return plug

    async def sleep(s):
        sleeps.append(s)

    return KasaDriver(pc(driver="kasa"), discover=discover, sleep=sleep)


def test_kasa_off_wait_on():
    plug, sleeps = FakePlug(), []
    kasa(plug, sleeps).power_cycle(15)
    assert plug.calls == ["update", "off", "on", "disconnect"] and sleeps == [15]


def test_kasa_retries_turn_on():
    plug, sleeps = FakePlug(fail_on=2), []
    kasa(plug, sleeps).power_cycle(15)
    assert plug.calls.count("on") == 3 and plug.calls[-1] == "disconnect"


def test_kasa_gives_up_after_three():
    plug, sleeps = FakePlug(fail_on=5), []
    with pytest.raises(PowerError, match="did not turn back on"):
        kasa(plug, sleeps).power_cycle(15)
    assert plug.calls[-1] == "disconnect"


def test_kasa_not_found():
    async def discover(host, **kw):
        return None

    with pytest.raises(PowerError, match="not found"):
        KasaDriver(pc(driver="kasa"), discover=discover).power_cycle(1)


# --------------------------------------------------------------------- guard
M = timedelta(minutes=1)


def test_decide_matrix():
    cfg = pc(after_minutes=10, cooldown_minutes=60, max_per_day=2, busy_grace_minutes=120)
    down = NOW - 15 * M
    assert not decide(pc(enabled=False), NOW, down, [], None).allowed
    assert not decide(cfg, NOW, None, [], None).allowed
    assert "waiting" in decide(cfg, NOW, NOW - 5 * M, [], None).reason
    assert decide(cfg, NOW, down, [], None).allowed
    assert "cooldown" in decide(cfg, NOW, down, [NOW - 30 * M], None).reason
    assert decide(cfg, NOW, down, [NOW - 90 * M], None).allowed
    assert "daily cap" in decide(cfg, NOW, down, [NOW - 90 * M, NOW - 5 * 60 * M], None).reason
    assert decide(cfg, NOW, down, [NOW - 90 * M, NOW - 25 * 60 * M], None).allowed  # one is >24h old
    held = decide(cfg, NOW, down, [], NOW - 20 * M, "backup manager is create_backup")
    assert not held.allowed and "create_backup" in held.reason
    assert decide(cfg, NOW, down, [], NOW - 121 * M).allowed


def test_run_cycle_dry_run_does_not_touch_driver():
    class Boom:
        def power_cycle(self, s):
            raise AssertionError("must not be called")

    assert "dry run" in run_cycle(Boom(), pc(dry_run=True))
