"""Alert delivery. None of these paths go through Home Assistant."""

from __future__ import annotations

import logging
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

import httpx

log = logging.getLogger(__name__)

SEVERITIES = ("alert", "recovery", "info")


@dataclass
class Message:
    title: str
    body: str
    severity: str = "alert"  # alert | recovery | info


class Notifier:
    name = "notifier"

    def send(self, msg: Message) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class NtfyNotifier(Notifier):
    name = "ntfy"
    PRIORITY = {"alert": "high", "recovery": "default", "info": "low"}
    TAGS = {"alert": "rotating_light", "recovery": "white_check_mark", "info": "information_source"}

    def __init__(self, opts: dict[str, Any], http: httpx.Client) -> None:
        server = opts.get("server", "https://ntfy.sh").rstrip("/")
        if not opts.get("topic"):
            raise ValueError("ntfy notifier needs a topic")
        self.url = f"{server}/{opts['topic']}"
        self.token = opts.get("token")
        self.http = http

    def send(self, msg: Message) -> None:
        headers = {
            "Title": msg.title.encode("utf-8").decode("latin-1", "replace"),
            "Priority": self.PRIORITY.get(msg.severity, "default"),
            "Tags": self.TAGS.get(msg.severity, ""),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.http.post(self.url, content=msg.body.encode("utf-8"), headers=headers).raise_for_status()


class WebhookNotifier(Notifier):
    """POST JSON. ``format``: discord, slack or generic."""

    name = "webhook"

    def __init__(self, opts: dict[str, Any], http: httpx.Client) -> None:
        if not opts.get("url"):
            raise ValueError("webhook notifier needs a url")
        self.url = opts["url"]
        self.format = opts.get("format", "generic")
        if self.format not in ("discord", "slack", "generic"):
            raise ValueError("webhook format must be discord, slack or generic")
        self.headers = dict(opts.get("headers") or {})
        self.http = http

    def payload(self, msg: Message) -> dict[str, Any]:
        text = f"**{msg.title}**\n{msg.body}" if self.format == "discord" else f"*{msg.title}*\n{msg.body}"
        if self.format == "discord":
            return {"content": text[:2000]}
        if self.format == "slack":
            return {"text": text}
        return {"title": msg.title, "message": msg.body, "severity": msg.severity}

    def send(self, msg: Message) -> None:
        self.http.post(self.url, json=self.payload(msg), headers=self.headers).raise_for_status()


class EmailNotifier(Notifier):
    name = "email"

    def __init__(self, opts: dict[str, Any], smtp_factory: Callable[..., Any] | None = None) -> None:
        for key in ("host", "from", "to"):
            if not opts.get(key):
                raise ValueError(f"email notifier needs '{key}'")
        self.opts = opts
        self.to = opts["to"] if isinstance(opts["to"], list) else [opts["to"]]
        self.security = opts.get("security", "starttls")  # starttls | ssl | none
        if self.security not in ("starttls", "ssl", "none"):
            raise ValueError("email security must be starttls, ssl or none")
        default_port = {"starttls": 587, "ssl": 465, "none": 25}[self.security]
        self.port = int(opts.get("port", default_port))
        self.smtp_factory = smtp_factory

    def send(self, msg: Message) -> None:
        em = EmailMessage()
        em["Subject"] = msg.title
        em["From"] = self.opts["from"]
        em["To"] = ", ".join(self.to)
        em.set_content(msg.body)
        ctx = ssl.create_default_context()
        if self.smtp_factory:
            smtp = self.smtp_factory(self.opts["host"], self.port)
        elif self.security == "ssl":
            smtp = smtplib.SMTP_SSL(self.opts["host"], self.port, timeout=20, context=ctx)
        else:
            smtp = smtplib.SMTP(self.opts["host"], self.port, timeout=20)
        with smtp:
            if self.security == "starttls":
                smtp.starttls(context=ctx)
            if self.opts.get("username"):
                smtp.login(self.opts["username"], self.opts.get("password") or "")
            smtp.send_message(em)


def build_notifiers(
    configs: list[dict[str, Any]], http: httpx.Client, smtp_factory: Callable[..., Any] | None = None
) -> list[Notifier]:
    out: list[Notifier] = []
    for c in configs:
        opts = {k: v for k, v in c.items() if k != "type"}
        if c["type"] == "ntfy":
            out.append(NtfyNotifier(opts, http))
        elif c["type"] == "webhook":
            out.append(WebhookNotifier(opts, http))
        elif c["type"] == "email":
            out.append(EmailNotifier(opts, smtp_factory))
    return out


def send_all(notifiers: list[Notifier], msg: Message) -> int:
    """Send to every notifier; one failing never stops the others. Returns successes."""
    sent = 0
    for n in notifiers:
        try:
            n.send(msg)
            sent += 1
        except Exception as exc:  # noqa: BLE001 - delivery must not crash the watcher
            # Never log str(exc): webhook URLs and SMTP errors can carry secrets.
            detail = type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError):
                detail = f"HTTP {exc.response.status_code}"
            log.error("notifier %s failed: %s", n.name, detail)
    return sent
