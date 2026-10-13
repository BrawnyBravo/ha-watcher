from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from ha_watcher.config import TWILIO_ENV, ConfigError, TwilioCredentials, parse_config
from ha_watcher.engine import Watcher
from ha_watcher.escalation import CallError, place_call, twiml
from tests.conftest import NOW, TOKEN, FakeHA, base_raw, make_client
from tests.test_engine import API_DOWN, Inbox

M = timedelta(minutes=1)
# Made-up values in Twilio's shapes; 555-01xx numbers are reserved for fiction.
SID = "AC" + "0" * 32
TWILIO = {
    "TWILIO_ACCOUNT_SID": SID,
    "TWILIO_AUTH_TOKEN": "test-auth-token-not-real",
    "TWILIO_FROM_NUMBER": "+15555550100",
    "TWILIO_TO_NUMBER": "+15555550199",
}
UP = {"/api/": (200, {"message": "API running."})}


class Phone:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def __call__(self, http, creds, text):
        self.calls.append(text)
        if self.fail:
            raise CallError(self.fail)
        return "CA123"


def cfg_for(env_extra=None, **over):
    env = {"HA_TOKEN": TOKEN, **(env_extra or {})}
    return parse_config(base_raw(**over), env=env)


def build(tmp_path, fake, env_extra=TWILIO, phone=None, **over):
    cfg = cfg_for(env_extra, **over)
    inbox, phone = Inbox(), phone or Phone()
    w = Watcher(
        cfg,
        make_client(fake, cfg=cfg),
        [inbox],
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        state_path=str(tmp_path / "state.json"),
        caller=phone,
    )
    return w, inbox, phone


def minutes(w, start, count):
    for i in range(count):
        w.run_once(start + i * M)
    return start + count * M


# ---------------------------------------------------------------- off by default
def test_call_is_off_without_twilio_env(tmp_path):
    fake = FakeHA(API_DOWN)
    w, inbox, phone = build(tmp_path, fake, env_extra={})
    assert not w.cfg.phone_call.active
    assert w.cfg.phone_call.missing_env == list(TWILIO_ENV)
    minutes(w, NOW, 180)
    assert phone.calls == []
    assert not any("phone" in m.title for m in inbox.msgs)


@pytest.mark.parametrize("missing", TWILIO_ENV)
def test_call_is_off_when_any_one_twilio_var_is_missing(tmp_path, missing):
    env = {k: v for k, v in TWILIO.items() if k != missing}
    w, _, phone = build(tmp_path, FakeHA(API_DOWN), env_extra=env)
    assert w.cfg.phone_call.missing_env == [missing]
    minutes(w, NOW, 60)
    assert phone.calls == []


def test_enabled_false_is_a_kill_switch(tmp_path):
    w, _, phone = build(tmp_path, FakeHA(API_DOWN), phone_call={"enabled": False})
    minutes(w, NOW, 60)
    assert phone.calls == []


def test_credentials_cannot_go_in_the_file():
    with pytest.raises(ConfigError, match="environment variables only"):
        cfg_for(TWILIO, phone_call={"twilio": {"auth_token": "x"}})


@pytest.mark.parametrize(
    "bad",
    [
        {"TWILIO_ACCOUNT_SID": "not-a-sid"},
        {"TWILIO_FROM_NUMBER": "555-0100"},
        {"TWILIO_TO_NUMBER": "+0123"},
    ],
)
def test_bad_twilio_values_are_config_errors(bad):
    with pytest.raises(ConfigError):
        cfg_for({**TWILIO, **bad})


def test_bad_phone_call_settings_are_config_errors():
    for bad in (
        {"max_per_day": 0},
        {"after_minutes": 0},
        {"trigger_checks": ["memory"]},  # not enabled
        {"trigger_checks": []},
        {"message": "{nope}"},
        {"message": ""},
    ):
        with pytest.raises(ConfigError):
            cfg_for(TWILIO, phone_call=bad)


def test_credentials_are_not_in_the_repr():
    text = repr(cfg_for(TWILIO))
    for value in TWILIO.values():
        assert value not in text


# --------------------------------------------------------------- when it calls
def test_calls_once_per_outage_after_the_delay(tmp_path):
    fake = FakeHA(API_DOWN)
    w, inbox, phone = build(tmp_path, fake)
    minutes(w, NOW, 30)  # failing from NOW; 29 minutes at the last loop
    assert phone.calls == []
    minutes(w, NOW + 30 * M, 150)  # hours more of the same outage
    assert len(phone.calls) == 1
    assert "Home Assistant has been down for 30 minutes" in phone.calls[0]
    placed = [m for m in inbox.msgs if m.title.endswith("phone call placed")]
    assert len(placed) == 1 and placed[0].severity == "info"
    for m in inbox.msgs:
        for value in TWILIO.values():
            assert value not in m.body and value not in m.title


def test_new_outage_after_recovery_can_call_again(tmp_path):
    fake = FakeHA(API_DOWN)
    w, _, phone = build(tmp_path, fake)
    t = minutes(w, NOW, 40)
    fake.routes.update(UP)
    t = minutes(w, t, 5)
    fake.routes.update(API_DOWN)
    minutes(w, t, 40)
    assert len(phone.calls) == 2


def test_a_brief_up_inside_the_same_outage_does_not_call_again(tmp_path):
    # An outage keeps its start time across a held recovery (API answering while Core is
    # still starting): seeing "up" for a loop must not re-arm the call for that outage.
    w, _, phone = build(tmp_path, FakeHA(API_DOWN))
    start = NOW - 60 * M
    w.phone.run(NOW, start)
    w.phone.run(NOW + 14 * M, None)
    w.phone.run(NOW + 15 * M, start)  # past retry_minutes, so only the outage id blocks it
    assert len(phone.calls) == 1
    w.phone.run(NOW + 60 * M, NOW + 20 * M)  # a genuinely new outage
    assert len(phone.calls) == 2


def test_short_outage_never_calls(tmp_path):
    fake = FakeHA(API_DOWN)
    w, inbox, phone = build(tmp_path, fake)
    t = minutes(w, NOW, 20)
    fake.routes.update(UP)
    minutes(w, t, 60)
    assert phone.calls == []
    assert [m.severity for m in inbox.msgs] == ["alert", "recovery"]


def test_daily_cap_stays_quiet_then_resets(tmp_path, caplog):
    fake = FakeHA(API_DOWN)
    w, inbox, phone = build(tmp_path, fake, phone_call={"max_per_day": 2, "after_minutes": 5})
    t = NOW
    for _ in range(2):  # two short outages, one call each
        t = minutes(w, t, 10)
        fake.routes.update(UP)
        t = minutes(w, t, 5)
        fake.routes.update(API_DOWN)
    assert len(phone.calls) == 2
    minutes(w, t, 30)  # a third outage the same day: capped
    assert len(phone.calls) == 2
    assert w.state["phone"]["last_decision"].startswith("daily cap reached")
    assert sum("daily cap reached" in r.message for r in caplog.records) == 1  # logged once
    assert len([m for m in inbox.msgs if "phone" in m.title]) == 2  # the cap itself sends nothing
    # Still down a day after the first call: the rolling cap lets one more call through.
    w.run_once(NOW + timedelta(hours=24, minutes=10))
    assert len(phone.calls) == 3


def test_failed_call_alerts_retries_later_and_counts_toward_the_cap(tmp_path):
    fake = FakeHA(API_DOWN)
    phone = Phone(fail="Twilio returned HTTP 401, Twilio error 20003")
    w, inbox, phone = build(tmp_path, fake, phone=phone, phone_call={"max_per_day": 2})
    minutes(w, NOW, 31)
    assert len(phone.calls) == 1
    failed = [m for m in inbox.msgs if "FAILED" in m.title]
    assert len(failed) == 1 and "HTTP 401" in failed[0].body and failed[0].severity == "alert"
    minutes(w, NOW + 31 * M, 9)
    assert len(phone.calls) == 1  # waits retry_minutes
    minutes(w, NOW + 40 * M, 120)
    assert len(phone.calls) == 2  # one retry, then the cap of 2 holds


def test_call_state_survives_a_restart(tmp_path):
    fake = FakeHA(API_DOWN)
    w, _, phone = build(tmp_path, fake)
    minutes(w, NOW, 35)
    w2, _, phone2 = build(tmp_path, fake)
    minutes(w2, NOW + 35 * M, 30)
    assert len(phone.calls) == 1 and phone2.calls == []


# --------------------------------------------------------------- the REST call
def test_place_call_posts_to_twilio():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(201, json={"sid": "CA" + "1" * 32, "status": "queued"})

    creds = TwilioCredentials(*TWILIO.values())
    sid = place_call(httpx.Client(transport=httpx.MockTransport(handler)), creds, "HA & co <down>")
    assert sid == "CA" + "1" * 32
    [req] = seen
    assert req.method == "POST"
    assert str(req.url) == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Calls.json"
    assert req.headers["Authorization"].startswith("Basic ")
    form = parse_qs(req.content.decode())
    assert form["To"] == ["+15555550199"] and form["From"] == ["+15555550100"]
    assert form["Twiml"] == [twiml("HA & co <down>")]
    assert "HA &amp; co &lt;down&gt;" in form["Twiml"][0]


def test_place_call_errors_are_safe_to_show():
    def handler(req):
        return httpx.Response(401, json={"code": 20003, "message": "Authenticate", "more_info": SID})

    creds = TwilioCredentials(*TWILIO.values())
    with pytest.raises(CallError) as err:
        place_call(httpx.Client(transport=httpx.MockTransport(handler)), creds, "x")
    assert str(err.value) == "Twilio returned HTTP 401, Twilio error 20003"

    def down(req):
        raise httpx.ConnectError("dns failure for api.twilio.com")

    with pytest.raises(CallError, match="could not reach Twilio"):
        place_call(httpx.Client(transport=httpx.MockTransport(down)), creds, "x")
