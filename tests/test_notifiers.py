import json
import logging

import httpx
import pytest

from ha_watcher.notifiers import (
    EmailNotifier,
    Message,
    NtfyNotifier,
    WebhookNotifier,
    build_notifiers,
    send_all,
)


class Recorder:
    def __init__(self, status=200):
        self.requests = []
        self.status = status

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(self.status)


def http(rec):
    return httpx.Client(transport=httpx.MockTransport(rec))


def test_ntfy_alert():
    rec = Recorder()
    NtfyNotifier({"topic": "my-ha", "token": "tk"}, http(rec)).send(Message("HA: problem", "- API down"))
    [req] = rec.requests
    assert str(req.url) == "https://ntfy.sh/my-ha"
    assert req.headers["Title"] == "HA: problem" and req.headers["Priority"] == "high"
    assert req.headers["Authorization"] == "Bearer tk"
    assert req.content == b"- API down"


def test_ntfy_recovery_custom_server():
    rec = Recorder()
    NtfyNotifier({"server": "https://ntfy.example.com/", "topic": "t"}, http(rec)).send(
        Message("HA: recovered", "ok", "recovery")
    )
    assert str(rec.requests[0].url) == "https://ntfy.example.com/t"
    assert rec.requests[0].headers["Priority"] == "default"
    assert "Authorization" not in rec.requests[0].headers


def test_ntfy_requires_topic():
    with pytest.raises(ValueError):
        NtfyNotifier({}, http(Recorder()))


@pytest.mark.parametrize(
    "fmt, check",
    [
        ("discord", lambda b: b == {"content": "**T**\nbody"}),
        ("slack", lambda b: b == {"text": "*T*\nbody"}),
        ("generic", lambda b: b == {"title": "T", "message": "body", "severity": "alert"}),
    ],
)
def test_webhook_formats(fmt, check):
    rec = Recorder()
    WebhookNotifier({"url": "https://example.invalid/hook", "format": fmt}, http(rec)).send(
        Message("T", "body")
    )
    assert check(json.loads(rec.requests[0].content))


def test_webhook_discord_truncates():
    n = WebhookNotifier({"url": "https://example.invalid/hook", "format": "discord"}, http(Recorder()))
    assert len(n.payload(Message("T", "x" * 5000))["content"]) == 2000


def test_webhook_bad_format():
    with pytest.raises(ValueError):
        WebhookNotifier({"url": "u", "format": "teams"}, http(Recorder()))


class FakeSMTP:
    instances = []

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.calls = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.calls.append("quit")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, msg):
        self.calls.append(("send", msg["Subject"], msg["To"], msg.get_content().strip()))


def test_email_starttls_login_send():
    FakeSMTP.instances.clear()
    n = EmailNotifier(
        {
            "host": "smtp.example.com",
            "from": "w@example.com",
            "to": ["a@example.com", "b@example.com"],
            "username": "w@example.com",
            "password": "p",
        },
        smtp_factory=FakeSMTP,
    )
    n.send(Message("HA: problem", "API down"))
    [s] = FakeSMTP.instances
    assert (s.host, s.port) == ("smtp.example.com", 587)
    assert s.calls == [
        "starttls",
        ("login", "w@example.com"),
        ("send", "HA: problem", "a@example.com, b@example.com", "API down"),
        "quit",
    ]


def test_email_plain_no_login():
    FakeSMTP.instances.clear()
    EmailNotifier(
        {"host": "relay", "from": "f@example.com", "to": "t@example.com", "security": "none"},
        smtp_factory=FakeSMTP,
    ).send(Message("s", "b"))
    [s] = FakeSMTP.instances
    assert s.port == 25 and s.calls[0][0] == "send"


def test_email_requires_fields():
    with pytest.raises(ValueError, match="host"):
        EmailNotifier({"from": "a", "to": "b"})


def test_build_notifiers():
    ns = build_notifiers(
        [
            {"type": "ntfy", "topic": "t"},
            {"type": "webhook", "url": "u"},
            {"type": "email", "host": "h", "from": "f", "to": "t"},
        ],
        http(Recorder()),
    )
    assert [n.name for n in ns] == ["ntfy", "webhook", "email"]


def test_send_all_continues_after_failure_and_hides_secret_url(caplog):
    good, bad = Recorder(), Recorder(status=500)
    secret_url = "https://example.invalid/api/webhooks/123/SECRET-PART"
    ns = [WebhookNotifier({"url": secret_url}, http(bad)), NtfyNotifier({"topic": "t"}, http(good))]
    with caplog.at_level(logging.ERROR):
        assert send_all(ns, Message("t", "b")) == 1
    assert len(good.requests) == 1
    assert "HTTP 500" in caplog.text and "SECRET-PART" not in caplog.text
