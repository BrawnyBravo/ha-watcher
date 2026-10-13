"""Warnings before an HTTPS certificate or a domain registration runs out.

Certificates are read straight from the server (a normal TLS handshake). Domain expiry
dates come from RDAP, the registries' public JSON replacement for WHOIS (RFC 9083): the
IANA bootstrap file (RFC 9224) says which registry server answers for each top-level
domain, and that server is asked for the domain. Neither needs an account or a key.

Each item is looked up once per ``every_hours``; a good RDAP answer is kept for at least
a day and a failed one is retried after 6 hours. At most one RDAP request goes out per
watch loop, the bootstrap file is fetched at most weekly, and a 403 or 429 from a
registry stops RDAP lookups for a day (longer if it sends Retry-After). An alert goes out
once per warning level crossed, not on every run, and a renewal closes it.
"""

from __future__ import annotations

import logging
import socket
import ssl
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

import httpx

from ha_watcher import __version__
from ha_watcher.checks import parse_ts
from ha_watcher.config import ExpiryConfig, split_host
from ha_watcher.notifiers import Message
from ha_watcher.state import parse, ts

log = logging.getLogger(__name__)

IANA_BOOTSTRAP = "https://data.iana.org/rdap/dns.json"
USER_AGENT = f"ha-watcher/{__version__} (+https://github.com/BrawnyBravo/ha-watcher)"
RDAP_MIN_INTERVAL = timedelta(hours=24)
BOOTSTRAP_MAX_AGE = timedelta(days=7)
TLS_RETRY_AFTER_FAILURE = timedelta(hours=1)
RDAP_RETRY_AFTER_FAILURE = timedelta(hours=6)
RATE_LIMIT_BACKOFF = timedelta(hours=24)
X509_V_ERR_CERT_HAS_EXPIRED = 10


class LookupFailed(Exception):  # noqa: N818 - reads better at the call site
    """A lookup did not produce a date. The message is safe to show and log."""


class RateLimited(LookupFailed):
    def __init__(self, message: str, retry_after: timedelta) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# ----------------------------------------------------------------- certificates


def fetch_cert_expiry(host: str, port: int, timeout: float) -> datetime | None:
    """Return the certificate's notAfter, or None if it has already expired.

    The handshake verifies the chain and host name as a browser would, so a certificate
    that is self-signed or for the wrong name is reported as a failed lookup, not a date.
    """
    ctx = ssl.create_default_context()
    try:
        with (
            socket.create_connection((host, port), timeout=timeout) as sock,
            ctx.wrap_socket(sock, server_hostname=host) as tls,
        ):
            cert = tls.getpeercert()
    except ssl.SSLCertVerificationError as exc:
        if exc.verify_code == X509_V_ERR_CERT_HAS_EXPIRED:
            return None
        raise LookupFailed(f"certificate not trusted ({exc.verify_message})") from None
    except (OSError, ssl.SSLError) as exc:
        raise LookupFailed(f"cannot connect ({type(exc).__name__})") from None
    not_after = (cert or {}).get("notAfter")
    if not not_after:
        raise LookupFailed("server sent no certificate expiry date")
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), UTC)


# ------------------------------------------------------------------------- RDAP


def parse_bootstrap(data: Any) -> dict[str, str]:
    """IANA dns.json -> {tld: base URL}, preferring https URLs."""
    servers: dict[str, str] = {}
    for entry in (data or {}).get("services") or []:
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        tlds, urls = entry[0], entry[1]
        https = [u for u in urls if isinstance(u, str) and u.startswith("https://")]
        if not https:
            continue
        base = https[0] if https[0].endswith("/") else https[0] + "/"
        for tld in tlds:
            if isinstance(tld, str):
                servers[tld.lower()] = base
    return servers


def parse_rdap_expiry(data: Any) -> datetime:
    """Pull the registration expiry date out of an RDAP domain answer."""
    for event in (data or {}).get("events") or []:
        if isinstance(event, dict) and event.get("eventAction") == "expiration":
            when = parse_ts(event.get("eventDate"))
            if when:
                return when
    raise LookupFailed("the registry does not publish an expiry date for this domain")


def _retry_after(response: httpx.Response) -> timedelta:
    try:
        seconds = int(response.headers.get("Retry-After", "0"))
    except ValueError:
        seconds = 0
    return max(RATE_LIMIT_BACKOFF, timedelta(seconds=seconds))


def _rdap_get(http: httpx.Client, url: str, timeout: float) -> Any:
    try:
        r = http.get(
            url,
            headers={"Accept": "application/rdap+json, application/json", "User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise LookupFailed(f"RDAP request failed ({type(exc).__name__})") from None
    if r.status_code in (403, 429):
        raise RateLimited(f"RDAP server refused (HTTP {r.status_code})", _retry_after(r))
    if r.status_code == 404:
        raise LookupFailed("the registry has no record of this domain (HTTP 404)")
    if r.status_code != 200:
        raise LookupFailed(f"RDAP server returned HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise LookupFailed("RDAP server sent something that is not JSON") from None


# ------------------------------------------------------------------- the watch


def _fmt_left(seconds: float) -> str:
    days = int(seconds // 86400)
    if days >= 2:
        return f"in {days} days"
    if days == 1:
        return "in 1 day"
    return "in less than a day"


class ExpiryWatch:
    """Runs once per watch loop; looks up what is due and alerts once per level crossed."""

    def __init__(
        self,
        cfg: ExpiryConfig,
        state: dict[str, Any],
        http: httpx.Client,
        send: Callable[[Message, datetime], Any],
        name: str,
        local_tz: tzinfo | None = None,
        cert_lookup: Callable[[str, int, float], datetime | None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.state = state  # the "expiry" part of the watcher's state file
        self.http = http
        self.send = send
        self.name = name
        self.local_tz = local_tz
        self.cert_lookup = cert_lookup or fetch_cert_expiry

    @property
    def configured(self) -> bool:
        return bool(self.cfg.tls_hosts or self.cfg.domains)

    def run(self, now: datetime) -> None:
        try:
            self._run(now)
        except Exception:  # noqa: BLE001 - expiry trouble must never stop the watch loop
            log.exception("expiry checks crashed")

    def _run(self, now: datetime) -> None:
        items = self.state["items"]
        wanted = {f"tls:{h}" for h in self.cfg.tls_hosts} | {f"domain:{d}" for d in self.cfg.domains}
        for key in [k for k in items if k not in wanted]:
            items.pop(key)  # removed from the config
        if not wanted:
            return
        every = timedelta(hours=self.cfg.every_hours)
        for entry in self.cfg.tls_hosts:
            item = items.setdefault(f"tls:{entry}", _new_item())
            if self._due(item, now, every, TLS_RETRY_AFTER_FAILURE):
                host, port = split_host(entry)
                self._lookup(entry, "tls", item, now, lambda h=host, p=port: self._cert(h, p))
        backoff = parse(self.state.get("rdap_backoff_until"))
        if backoff and now < backoff:
            return
        for domain in self.cfg.domains:
            item = items.setdefault(f"domain:{domain}", _new_item())
            if self._due(item, now, max(every, RDAP_MIN_INTERVAL), RDAP_RETRY_AFTER_FAILURE):
                # One RDAP request per loop at most: the next due domain waits for the next loop.
                self._lookup(domain, "domain", item, now, lambda d=domain: self._rdap(d, now))
                return

    # ------------------------------------------------------------ lookups
    def _due(self, item: dict[str, Any], now: datetime, every: timedelta, retry: timedelta) -> bool:
        last = parse(item["checked"])
        if last is None:
            return True
        wait = retry if item["fails"] else every
        return now - last >= wait

    def _cert(self, host: str, port: int) -> datetime | None:
        return self.cert_lookup(host, port, float(self.cfg.timeout_seconds))

    def _rdap(self, domain: str, now: datetime) -> datetime:
        base = self._rdap_server(domain, now)
        return parse_rdap_expiry(_rdap_get(self.http, f"{base}domain/{domain}", self.cfg.timeout_seconds))

    def _rdap_server(self, domain: str, now: datetime) -> str:
        cache = self.state["rdap_servers"]
        labels = domain.split(".")
        suffixes = [".".join(labels[i:]) for i in range(1, len(labels))]  # co.uk before uk
        fetched = parse(cache.get("fetched"))
        tld = labels[-1]
        if not fetched or now - fetched >= BOOTSTRAP_MAX_AGE or tld not in cache.get("tlds", []):
            servers = parse_bootstrap(_rdap_get(self.http, IANA_BOOTSTRAP, self.cfg.timeout_seconds))
            tlds = sorted({d.rsplit(".", 1)[-1] for d in self.cfg.domains})
            # Keep only what the configured domains need, not the whole 1,000-TLD file.
            keep = {k: v for k, v in servers.items() if k.rsplit(".", 1)[-1] in tlds}
            cache.clear()
            cache.update(fetched=ts(now), tlds=tlds, servers=keep)
        for suffix in suffixes:
            if suffix in cache["servers"]:
                return cache["servers"][suffix]
        raise LookupFailed(f"no RDAP server is listed for .{labels[-1]} domains")

    def _lookup(
        self,
        label: str,
        kind: str,
        item: dict[str, Any],
        now: datetime,
        fetch: Callable[[], datetime | None],
    ) -> None:
        item["checked"] = ts(now)
        try:
            try:
                expires = fetch()
            except LookupFailed:
                raise
            except Exception as exc:  # noqa: BLE001 - e.g. a host name the resolver rejects
                raise LookupFailed(f"lookup crashed ({type(exc).__name__})") from None
        except RateLimited as exc:
            self.state["rdap_backoff_until"] = ts(now + exc.retry_after)
            log.warning("expiry: %s %s: %s; RDAP lookups paused for a day", kind, label, exc)
            return
        except LookupFailed as exc:
            item["fails"] += 1
            item["error"] = str(exc)
            log.warning("expiry: %s %s lookup failed (%d in a row): %s", kind, label, item["fails"], exc)
            if item["fails"] >= self.cfg.failed_lookups_before_alert and not item["fail_alerted"]:
                item["fail_alerted"] = True
                what = "the HTTPS certificate" if kind == "tls" else "the registration date"
                self.send(
                    Message(
                        f"{self.name}: cannot check expiry",
                        f"Could not check {what} for {label} {item['fails']} times in a row: {exc}. "
                        "Expiry warnings for it are paused until a lookup works.",
                        "info",
                    ),
                    now,
                )
            return
        if item["fail_alerted"]:
            log.info("expiry: %s %s lookup works again", kind, label)
        item.update(fails=0, error="", fail_alerted=False)
        self._judge(label, kind, item, expires, now)

    # ----------------------------------------------------------- thresholds
    def _judge(
        self, label: str, kind: str, item: dict[str, Any], expires: datetime | None, now: datetime
    ) -> None:
        previous = parse(item["expires"])
        was_expired = item["expired"]
        item["expired"] = expires is None
        if expires is not None:
            item["expires"] = ts(expires)
        left = (expires - now).total_seconds() if expires else -1.0
        # Level 0 is "has expired": one more alert when the date actually passes.
        crossed = [d for d in [*self.cfg.warn_days, 0] if left <= d * 86400]
        warned = set(item["warned"])

        if warned and expires and expires > now and (was_expired or (previous and expires > previous)):
            # Renewed: close the warning, and start counting the new date from scratch.
            item["warned"] = crossed
            self.send(
                Message(
                    f"{self.name}: {'certificate' if kind == 'tls' else 'domain'} renewed",
                    f"{self._what(kind, label)} has been renewed; it now expires {_fmt_left(left)} "
                    f"(on {self._date(expires)}).",
                    "recovery",
                ),
                now,
            )
            return
        new = [d for d in crossed if d not in warned]
        if not new:
            return
        item["warned"] = sorted(warned | set(crossed), reverse=True)
        if expires is None or left <= 0:
            body = f"{self._what(kind, label)} has EXPIRED."
            if kind == "tls":
                body += " Browsers and apps will refuse to connect until it is renewed."
            else:
                body += " Renew it with the registrar now, before someone else can register it."
        else:
            body = f"{self._what(kind, label)} expires {_fmt_left(left)} (on {self._date(expires)}). " + (
                "Check that automatic renewal is working."
                if kind == "tls"
                else "Renew it with the registrar."
            )
        noun = "certificate" if kind == "tls" else "domain"
        self.send(Message(f"{self.name}: {noun} expiring", body, "alert"), now)

    def _what(self, kind: str, label: str) -> str:
        return f"The HTTPS certificate for {label}" if kind == "tls" else f"The domain {label}"

    def _date(self, when: datetime) -> str:
        return when.astimezone(self.local_tz).strftime("%a %d %b %Y")


def _new_item() -> dict[str, Any]:
    return {
        "checked": None,
        "expires": None,
        "expired": False,
        "warned": [],
        "fails": 0,
        "error": "",
        "fail_alerted": False,
    }
