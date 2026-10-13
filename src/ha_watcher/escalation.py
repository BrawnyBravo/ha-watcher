"""Phone-call escalation through Twilio's REST API, for the top severity only.

A call goes out when every ``phone_call.trigger_checks`` check (default: the API) has been
failing for ``after_minutes``, once per outage: the outage is identified by when it began,
so a call is never repeated for the same outage. ``max_per_day`` caps calls over a rolling
24 hours (failed attempts count, so a broken account is not hammered); when the cap is hit
the watcher just stays quiet and the normal alerts carry on.

It is off unless TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER and
TWILIO_TO_NUMBER are all set in the environment. Nothing about the account or the numbers
is ever logged or put in a notification.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from xml.sax.saxutils import escape

import httpx

from ha_watcher.config import PhoneCallConfig, TwilioCredentials
from ha_watcher.notifiers import Message
from ha_watcher.state import parse, ts

log = logging.getLogger(__name__)

TWILIO_CALLS_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"


class CallError(Exception):
    """The call could not be placed. The message is safe to show and log."""


def twiml(text: str) -> str:
    """Say the message twice, with a pause, so it is not missed while answering."""
    said = f"<Say>{escape(text)}</Say>"
    return f'<Response>{said}<Pause length="1"/>{said}</Response>'


def place_call(http: httpx.Client, creds: TwilioCredentials, text: str) -> str:
    """Ask Twilio to place the call. Returns Twilio's call id (CA...)."""
    try:
        r = http.post(
            TWILIO_CALLS_URL.format(sid=creds.account_sid),
            auth=(creds.account_sid, creds.auth_token),
            data={"To": creds.to_number, "From": creds.from_number, "Twiml": twiml(text)},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        raise CallError(f"could not reach Twilio ({type(exc).__name__})") from None
    if r.status_code >= 400:
        code = ""
        try:
            body = r.json()
            if isinstance(body, dict) and isinstance(body.get("code"), int):
                code = f", Twilio error {body['code']}"  # a number; look it up in Twilio's docs
        except ValueError:
            pass
        raise CallError(f"Twilio returned HTTP {r.status_code}{code}")
    try:
        sid = r.json().get("sid")
    except (ValueError, AttributeError):
        sid = None
    return sid if isinstance(sid, str) else "unknown"


@dataclass
class Decision:
    allowed: bool
    reason: str
    down_minutes: int = 0


def decide(
    pc: PhoneCallConfig,
    now: datetime,
    down_since: datetime | None,
    calls: list[datetime],
    called_outage: datetime | None,
    last_attempt: datetime | None,
) -> Decision:
    if down_since is None:
        return Decision(False, "up")
    down = int((now - down_since).total_seconds() // 60)
    if down < pc.after_minutes:
        return Decision(False, f"waiting ({down} of {pc.after_minutes:g} min)", down)
    if called_outage is not None and called_outage == down_since:
        return Decision(False, "already called for this outage", down)
    if (
        last_attempt
        and last_attempt >= down_since
        and now - last_attempt < timedelta(minutes=pc.retry_minutes)
    ):
        return Decision(False, "waiting to retry a failed call", down)
    recent = [c for c in calls if now - c < timedelta(hours=24)]
    if len(recent) >= pc.max_per_day:
        return Decision(False, f"daily cap reached ({len(recent)} of {pc.max_per_day} calls in 24 h)", down)
    return Decision(True, f"down for {down} min", down)


class PhoneEscalation:
    def __init__(
        self,
        cfg: PhoneCallConfig,
        state: dict[str, Any],
        http: httpx.Client,
        send: Callable[[Message, datetime], Any],
        name: str,
        caller: Callable[[httpx.Client, TwilioCredentials, str], str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.state = state  # the "phone" part of the watcher's state file
        self.http = http
        self.send = send
        self.name = name
        self.caller = caller or place_call

    def run(self, now: datetime, down_since: datetime | None) -> bool:
        """Place the call if it is due. Returns True if a call was attempted."""
        try:
            return self._run(now, down_since)
        except Exception:  # noqa: BLE001 - must never stop the watch loop
            log.exception("phone escalation crashed")
            return False

    def _run(self, now: datetime, down_since: datetime | None) -> bool:
        if not self.cfg.active or self.cfg.twilio is None:
            return False
        st = self.state
        calls = [parse(c) for c in st["calls"] if now - parse(c) < timedelta(days=2)]
        st["calls"] = [ts(c) for c in calls]
        if down_since is None:
            st["called_outage"] = None  # outage over; the next one may call again
        decision = decide(
            self.cfg, now, down_since, calls, parse(st["called_outage"]), parse(st["last_attempt"])
        )
        changed = decision.reason != st["last_decision"]
        st["last_decision"] = decision.reason
        if not decision.allowed:
            if changed and decision.reason.startswith("daily cap"):
                log.warning("phone call not placed: %s", decision.reason)
            return False
        text = self.cfg.message.format(name=self.name, minutes=decision.down_minutes)
        calls.append(now)  # count failed attempts too
        st["calls"] = [ts(c) for c in calls]
        st["last_attempt"] = ts(now)
        try:
            self.caller(self.http, self.cfg.twilio, text)
        except CallError as exc:
            log.error("phone call failed: %s", exc)
            self.send(
                Message(
                    f"{self.name}: phone call FAILED",
                    f"{self.name} has been down for {decision.down_minutes} min and the phone call "
                    f"did not go through: {exc}.",
                    "alert",
                ),
                now,
            )
            return True
        st["called_outage"] = ts(down_since)
        log.info("phone call placed: %s", decision.reason)
        self.send(
            Message(
                f"{self.name}: phone call placed",
                f"{self.name} has been down for {decision.down_minutes} min, so the watcher phoned "
                "the configured number. No more calls for this outage.",
                "info",
            ),
            now,
        )
        return True
