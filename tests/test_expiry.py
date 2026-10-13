from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ha_watcher.config import ConfigError, parse_config
from ha_watcher.engine import Watcher
from ha_watcher.expiry import (
    IANA_BOOTSTRAP,
    USER_AGENT,
    LookupFailed,
    parse_bootstrap,
    parse_rdap_expiry,
)
from tests.conftest import NOW, TOKEN, FakeHA, base_raw, make_cfg, make_client
from tests.test_engine import Inbox

H = timedelta(hours=1)
D = timedelta(days=1)


class Certs:
    """Stands in for the TLS handshake: host -> expiry date (None = already expired)."""

    def __init__(self, dates):
        self.dates = dates
        self.calls = []

    def __call__(self, host, port, timeout):
        self.calls.append((host, port))
        value = self.dates[host]
        if isinstance(value, Exception):
            raise value
        return value


class RDAP:
    """MockTransport handler for the IANA bootstrap file and a registry's RDAP server."""

    def __init__(self, expires="2026-02-20T04:00:00Z", status=200):
        self.expires = expires
        self.status = status
        self.requests = []

    def __call__(self, req):
        self.requests.append(req)
        if str(req.url) == IANA_BOOTSTRAP:
            return httpx.Response(
                200,
                json={
                    "services": [
                        [["com", "net"], ["https://rdap.example-registry.test/com/v1/"]],
                        [["org"], ["http://insecure.test/", "https://rdap.org-registry.test"]],
                        [["xyz"], ["http://only-http.test/"]],
                    ]
                },
            )
        if self.status != 200:
            return httpx.Response(self.status, headers={"Retry-After": "120"})
        return httpx.Response(200, json=rdap_answer(self.expires))


def rdap_answer(expires):
    return {
        "objectClassName": "domain",
        "ldhName": "EXAMPLE.COM",
        "events": [
            {"eventAction": "registration", "eventDate": "1995-08-14T04:00:00Z"},
            {"eventAction": "expiration", "eventDate": expires},
            {"eventAction": "last update of RDAP database", "eventDate": "2026-01-15T10:00:00Z"},
        ],
    }


def build(tmp_path, expiry, certs=None, rdap=None):
    cfg = make_cfg(expiry=expiry)
    inbox = Inbox()
    w = Watcher(
        cfg,
        make_client(FakeHA(), cfg=cfg),
        [inbox],
        httpx.Client(transport=httpx.MockTransport(rdap or RDAP())),
        local_tz=UTC,
        state_path=str(tmp_path / "state.json"),
        cert_lookup=certs or Certs({}),
    )
    return w, inbox


def expiring(inbox):
    return [m for m in inbox.msgs if "expir" in m.title or "renewed" in m.title or "cannot" in m.title]


# --------------------------------------------------------------------- config
def test_expiry_is_off_and_empty_by_default(tmp_path):
    cfg = make_cfg()
    assert cfg.expiry.tls_hosts == [] and cfg.expiry.domains == []
    assert cfg.expiry.warn_days == [21, 7]
    certs = Certs({})
    w, inbox = build(tmp_path, {}, certs)
    w.run_once(NOW)
    assert certs.calls == [] and expiring(inbox) == []


def test_expiry_config_normalises_and_rejects_bad_entries():
    cfg = make_cfg(
        expiry={"tls_hosts": ["Example.com:8443"], "domains": ["Example.COM."], "warn_days": [7, 30]}
    )
    assert cfg.expiry.tls_hosts == ["example.com:8443"]
    assert cfg.expiry.domains == ["example.com"]
    assert cfg.expiry.warn_days == [30, 7]
    for bad in (
        {"tls_hosts": ["https://example.com"]},
        {"tls_hosts": ["example.com:99999"]},
        {"tls_hosts": ["example.com:abc"]},
        {"domains": ["localhost"]},
        {"domains": ["https://example.com/"]},
        {"warn_days": []},
        {"warn_days": [0]},
        {"warn_days": [7.5]},
        {"every_hours": 0.5},
        {"failed_lookups_before_alert": 0},
        {"nonsense": 1},
    ):
        with pytest.raises(ConfigError):
            make_cfg(expiry=bad)


# ----------------------------------------------------------------- thresholds
def test_certificate_alerts_once_per_threshold(tmp_path):
    certs = Certs({"example.com": NOW + 30 * D})
    w, inbox = build(tmp_path, {"tls_hosts": ["example.com"]}, certs)
    t = NOW
    while t < NOW + 31 * D:  # one loop every 30 minutes for a month
        w.run_once(t)
        t += H / 2
    msgs = expiring(inbox)
    assert [m.severity for m in msgs] == ["alert", "alert", "alert"]
    assert "expires in 21 days (on Sat 14 Feb 2026)" in msgs[0].body
    assert "expires in 7 days" in msgs[1].body
    assert "has EXPIRED" in msgs[2].body
    # Looked up once a day, not every loop.
    assert len(certs.calls) == 31
    assert certs.calls[0] == ("example.com", 443)


def test_first_lookup_inside_both_levels_sends_one_alert(tmp_path):
    certs = Certs({"example.com": NOW + 5 * D + H})
    w, inbox = build(tmp_path, {"tls_hosts": ["example.com"]}, certs)
    for i in range(48):
        w.run_once(NOW + i * H)
    msgs = expiring(inbox)
    assert len(msgs) == 1 and "expires in 5 days (on Tue 20 Jan 2026)" in msgs[0].body
    assert w.state["expiry"]["items"]["tls:example.com"]["warned"] == [21, 7]


def test_custom_levels_and_port(tmp_path):
    certs = Certs({"example.com": NOW + 40 * D})
    w, inbox = build(tmp_path, {"tls_hosts": ["example.com:8443"], "warn_days": [45, 30, 3]}, certs)
    w.run_once(NOW)
    assert certs.calls == [("example.com", 8443)]
    assert len(expiring(inbox)) == 1 and "in 40 days" in inbox.msgs[0].body


def test_renewal_closes_the_warning_and_rearms_the_levels(tmp_path):
    certs = Certs({"example.com": NOW + 6 * D})
    w, inbox = build(tmp_path, {"tls_hosts": ["example.com"]}, certs)
    w.run_once(NOW)
    certs.dates["example.com"] = NOW + 96 * D  # auto-renewal ran
    w.run_once(NOW + D)
    w.run_once(NOW + 2 * D)
    msgs = expiring(inbox)
    assert [m.severity for m in msgs] == ["alert", "recovery"]
    assert "renewed" in msgs[1].body and "in 95 days" in msgs[1].body
    assert w.state["expiry"]["items"]["tls:example.com"]["warned"] == []
    w.run_once(NOW + 80 * D)  # 16 days left on the new certificate
    assert len(expiring(inbox)) == 3 and "in 16 days" in expiring(inbox)[2].body


def test_already_expired_certificate_alerts_once(tmp_path):
    certs = Certs({"example.com": None})
    w, inbox = build(tmp_path, {"tls_hosts": ["example.com"]}, certs)
    for i in range(5):
        w.run_once(NOW + i * D)
    msgs = expiring(inbox)
    assert len(msgs) == 1 and "has EXPIRED" in msgs[0].body and "refuse to connect" in msgs[0].body
    certs.dates["example.com"] = NOW + 95 * D
    w.run_once(NOW + 5 * D)
    assert expiring(inbox)[-1].severity == "recovery"


def test_failed_lookups_tell_once_then_clear(tmp_path):
    certs = Certs({"example.com": LookupFailed("cannot connect (ConnectionRefusedError)")})
    w, inbox = build(tmp_path, {"tls_hosts": ["example.com"]}, certs)
    for i in range(10):  # retries hourly after a failure
        w.run_once(NOW + i * H)
    msgs = expiring(inbox)
    assert len(certs.calls) == 10
    assert len(msgs) == 1 and msgs[0].severity == "info"
    assert "3 times in a row" in msgs[0].body and "ConnectionRefusedError" in msgs[0].body
    certs.dates["example.com"] = NOW + 60 * D
    w.run_once(NOW + 10 * H)
    item = w.state["expiry"]["items"]["tls:example.com"]
    assert item["fails"] == 0 and not item["fail_alerted"]
    w.run_once(NOW + 11 * H)  # healthy again: back to once a day
    assert len(certs.calls) == 11


def test_state_survives_a_restart(tmp_path):
    certs = Certs({"example.com": NOW + 6 * D})
    w, inbox = build(tmp_path, {"tls_hosts": ["example.com"]}, certs)
    w.run_once(NOW)
    w2, inbox2 = build(tmp_path, {"tls_hosts": ["example.com"]}, certs)
    w2.run_once(NOW + 2 * D)
    assert len(expiring(inbox)) == 1 and expiring(inbox2) == []


def test_items_removed_from_config_are_forgotten(tmp_path):
    certs = Certs({"example.com": NOW + 6 * D, "example.net": NOW + 60 * D})
    w, _ = build(tmp_path, {"tls_hosts": ["example.com", "example.net"]}, certs)
    w.run_once(NOW)
    w.cfg.expiry.tls_hosts = ["example.net"]
    w.run_once(NOW + H)
    assert list(w.state["expiry"]["items"]) == ["tls:example.net"]


# ----------------------------------------------------------------------- RDAP
def test_parse_rdap_expiry():
    assert parse_rdap_expiry(rdap_answer("2027-08-13T04:00:00Z")) == datetime(2027, 8, 13, 4, tzinfo=UTC)
    with pytest.raises(LookupFailed, match="does not publish"):
        parse_rdap_expiry({"events": [{"eventAction": "registration", "eventDate": "1995-08-14T04:00:00Z"}]})
    with pytest.raises(LookupFailed):
        parse_rdap_expiry({"events": [{"eventAction": "expiration", "eventDate": "soon"}]})
    with pytest.raises(LookupFailed):
        parse_rdap_expiry(None)


def test_parse_bootstrap_prefers_https_and_skips_http_only():
    servers = parse_bootstrap(RDAP()(httpx.Request("GET", IANA_BOOTSTRAP)).json())
    assert servers == {
        "com": "https://rdap.example-registry.test/com/v1/",
        "net": "https://rdap.example-registry.test/com/v1/",
        "org": "https://rdap.org-registry.test/",
    }


def test_domain_lookup_uses_bootstrap_and_caches_daily(tmp_path):
    rdap = RDAP("2026-02-20T04:00:00Z")  # 36 days after NOW
    w, inbox = build(tmp_path, {"domains": ["example.com"], "every_hours": 1}, rdap=rdap)
    for i in range(30):  # every_hours: 1 must not make RDAP hourly
        w.run_once(NOW + i * H)
    urls = [str(r.url) for r in rdap.requests]
    assert urls == [IANA_BOOTSTRAP, "https://rdap.example-registry.test/com/v1/domain/example.com"] + [
        "https://rdap.example-registry.test/com/v1/domain/example.com"
    ]  # NOW and NOW + 24 h; the bootstrap is cached
    assert rdap.requests[1].headers["User-Agent"] == USER_AGENT
    assert "application/rdap+json" in rdap.requests[1].headers["Accept"]
    assert expiring(inbox) == []
    assert w.state["expiry"]["rdap_servers"]["servers"]["com"].startswith(
        "https://rdap.example-registry.test"
    )
    assert "org" not in w.state["expiry"]["rdap_servers"]["servers"]  # only the TLDs in use are kept


def test_domain_alerts_once_per_threshold(tmp_path):
    rdap = RDAP("2026-02-01T12:00:00Z")  # 17 days after NOW
    w, inbox = build(tmp_path, {"domains": ["example.com"]}, rdap=rdap)
    for i in range(20):
        w.run_once(NOW + i * D)
    msgs = expiring(inbox)
    assert [m.title for m in msgs] == ["Home Assistant: domain expiring"] * 3
    assert "The domain example.com expires in 17 days" in msgs[0].body
    assert "expires in 7 days" in msgs[1].body and "has EXPIRED" in msgs[2].body


def test_one_rdap_request_per_loop(tmp_path):
    rdap = RDAP("2027-01-01T00:00:00Z")
    w, _ = build(tmp_path, {"domains": ["example.com", "example.net"]}, rdap=rdap)
    w.run_once(NOW)
    assert len(rdap.requests) == 2  # bootstrap + example.com; example.net waits
    w.run_once(NOW + timedelta(minutes=1))
    assert str(rdap.requests[-1].url).endswith("/domain/example.net") and len(rdap.requests) == 3
    w.run_once(NOW + timedelta(minutes=2))
    assert len(rdap.requests) == 3


def test_rate_limit_pauses_rdap_for_a_day(tmp_path):
    rdap = RDAP(status=429)
    w, inbox = build(tmp_path, {"domains": ["example.com", "example.net"]}, rdap=rdap)
    for i in range(23):
        w.run_once(NOW + i * H)
    assert len(rdap.requests) == 2  # bootstrap, then the 429; nothing more that day
    assert expiring(inbox) == []
    rdap.status = 200
    w.run_once(NOW + 24 * H)
    assert len(rdap.requests) == 3


def test_unknown_domain_and_unlisted_tld_count_as_failed_lookups(tmp_path):
    rdap = RDAP(status=404)
    w, inbox = build(
        tmp_path, {"domains": ["example.com", "example.xyz"], "failed_lookups_before_alert": 1}, rdap=rdap
    )
    w.run_once(NOW)
    w.run_once(NOW + timedelta(minutes=1))
    msgs = expiring(inbox)
    assert len(msgs) == 2
    assert "no record of this domain" in msgs[0].body
    assert "no RDAP server is listed for .xyz" in msgs[1].body


def test_config_with_env_var_for_hosts():
    raw = base_raw(expiry={"tls_hosts": ["${SITE_HOST}"]})
    cfg = parse_config(raw, env={"HA_TOKEN": TOKEN, "SITE_HOST": "example.org"})
    assert cfg.expiry.tls_hosts == ["example.org"]


def test_rdap_failures_retry_after_six_hours_and_bootstrap_is_not_refetched(tmp_path):
    rdap = RDAP(status=500)
    w, inbox = build(tmp_path, {"domains": ["example.com", "example.xyz"]}, rdap=rdap)
    for i in range(13 * 60):  # 13 hours of one-minute loops
        w.run_once(NOW + i * timedelta(minutes=1))
    urls = [str(r.url) for r in rdap.requests]
    assert urls.count(IANA_BOOTSTRAP) == 1
    assert urls.count("https://rdap.example-registry.test/com/v1/domain/example.com") == 3  # 0, 6 h, 12 h
    msgs = expiring(inbox)
    assert len(msgs) == 2 and all("3 times in a row" in m.body for m in msgs)
    assert "HTTP 500" in msgs[0].body and ".xyz" in msgs[1].body


def test_an_unexpected_lookup_error_is_a_failed_lookup_not_a_crash(tmp_path):
    certs = Certs({"example.com": UnicodeError("label too long")})
    w, inbox = build(tmp_path, {"tls_hosts": ["example.com"], "failed_lookups_before_alert": 1}, certs)
    w.run_once(NOW)
    assert "lookup crashed (UnicodeError)" in expiring(inbox)[0].body
