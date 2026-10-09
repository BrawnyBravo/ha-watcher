"""The watch loop: run due checks, debounce, alert, recover, heartbeat, dead-man, power."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

import httpx

from ha_watcher import power
from ha_watcher.checks import CHECKS, NEEDS_API, Context, Result, detect_busy, fmt_age
from ha_watcher.config import Config
from ha_watcher.ha import HAClient, HAError
from ha_watcher.notifiers import Message, Notifier, send_all
from ha_watcher.state import load_state, parse, save_state, ts

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Watcher:
    def __init__(
        self,
        cfg: Config,
        client: HAClient,
        notifiers: list[Notifier],
        http: httpx.Client,
        driver: Any = None,
        ctx_hooks: dict[str, Any] | None = None,
        local_tz: tzinfo | None = None,
        state_path: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.notifiers = notifiers
        self.http = http  # used for the dead-man ping
        self.driver = driver
        self.ctx_hooks = ctx_hooks or {}
        self.local_tz = local_tz
        self.state_path = state_path or cfg.state_file
        self.state = load_state(self.state_path)

    # ------------------------------------------------------------------ public
    def run_once(self, now: datetime | None = None) -> list[Result]:
        now = now or utcnow()
        results, ran = self._run_checks(now)
        alerts, recoveries = self._update(results, ran, now)
        self._deliver(alerts, recoveries, now)
        self._busy_probe(results, now)
        self._maybe_power_cycle(now)
        self._heartbeat(now)
        self._deadman(now)
        save_state(self.state_path, self.state)
        return results

    def run_forever(self, sleep: Callable[[float], None] = time.sleep) -> None:
        log.info("watching %s every %ss", self.cfg.home_assistant.url, self.cfg.interval_seconds)
        while True:
            start = time.monotonic()
            try:
                for r in self.run_once():
                    log.debug("%s ok=%s %s", r.key, r.ok, r.message)
            except Exception:  # noqa: BLE001 - the watcher must keep watching
                log.exception("watch cycle failed")
            sleep(max(1.0, self.cfg.interval_seconds - (time.monotonic() - start)))

    # ------------------------------------------------------------------ checks
    def _run_checks(self, now: datetime) -> tuple[list[Result], set[str]]:
        ctx = Context(now=now, **self.ctx_hooks)
        results: list[Result] = []
        ran: set[str] = set()
        api_down = False
        for name, fn in CHECKS.items():
            ccfg = self.cfg.checks[name]
            if not ccfg.enabled:
                continue
            if name in NEEDS_API and api_down:
                continue  # keep previous state; the api alert already says HA is down
            last = parse(self.state["last_run"].get(name))
            if ccfg.every_seconds and last and (now - last).total_seconds() < ccfg.every_seconds:
                if name == "api":
                    api_down = self._failing("api")
                continue
            try:
                out = fn(self.client, ccfg.options, ctx)
            except Exception as exc:  # noqa: BLE001 - one broken check must not stop the rest
                log.exception("check %s crashed", name)
                out = [Result(name, False, f"check crashed: {type(exc).__name__}")]
            for r in out:
                r.data.setdefault("check", name)
            results.extend(out)
            ran.add(name)
            self.state["last_run"][name] = ts(now)
            if name == "api":
                api_down = not all(r.ok for r in out)
        return results, ran

    def _failing(self, key: str) -> bool:
        st = self.state["checks"].get(key)
        return bool(st and not st["ok"])

    # ---------------------------------------------------------- debounce logic
    def _update(self, results: list[Result], ran: set[str], now: datetime) -> tuple[list[str], list[str]]:
        checks = self.state["checks"]
        alerts: list[str] = []
        recoveries: list[str] = []
        realert = timedelta(minutes=self.cfg.alerts.realert_minutes)
        seen: set[str] = set()

        for r in results:
            seen.add(r.key)
            check = r.data["check"]
            need = self.cfg.checks[check].failures_before_alert
            st = checks.setdefault(
                r.key,
                {
                    "check": check,
                    "ok": True,
                    "failures": 0,
                    "alerting": False,
                    "first_failure": None,
                    "last_alert": None,
                    "message": "",
                },
            )
            st["message"] = r.message
            if r.ok:
                if st["alerting"]:
                    first = parse(st["first_failure"])
                    took = f" (was failing for {fmt_age((now - first).total_seconds())})" if first else ""
                    recoveries.append(f"{r.message}{took}")
                st.update(ok=True, failures=0, alerting=False, first_failure=None, last_alert=None)
                continue
            st["ok"] = False
            st["failures"] += 1
            st["first_failure"] = st["first_failure"] or ts(now)
            if not st["alerting"] and st["failures"] >= need:
                st["alerting"] = True
                st["last_alert"] = ts(now)
                alerts.append(r.message)
            elif st["alerting"] and realert and now - parse(st["last_alert"]) >= realert:
                first = parse(st["first_failure"])
                st["last_alert"] = ts(now)
                alerts.append(f"Still failing for {fmt_age((now - first).total_seconds())}: {r.message}")

        # A key that a check stopped reporting (e.g. a "cannot query" error that cleared,
        # or an entity removed from the config) counts as resolved.
        for key in [k for k, v in checks.items() if v["check"] in ran and k not in seen]:
            st = checks.pop(key)
            if st["alerting"]:
                recoveries.append(f"Resolved: {st['message']}")
        # Forget state for checks that are no longer enabled.
        for key in [k for k, v in checks.items() if not self.cfg.checks[v["check"]].enabled]:
            checks.pop(key)
        return alerts, recoveries

    # ---------------------------------------------------------------- delivery
    def _rate_ok(self, now: datetime) -> bool:
        hour_ago = now - timedelta(hours=1)
        times = [t for t in self.state["alert_times"] if parse(t) > hour_ago]
        self.state["alert_times"] = times
        return len(times) < self.cfg.alerts.max_per_hour

    def _send(self, msg: Message, now: datetime, bypass_limit: bool = False) -> bool:
        if not bypass_limit and not self._rate_ok(now):
            self.state["suppressed"] += 1
            log.warning("rate limit reached; suppressed: %s", msg.title)
            return False
        if self.state["suppressed"]:
            msg.body += f"\n\n({self.state['suppressed']} earlier message(s) suppressed by the rate limit)"
            self.state["suppressed"] = 0
        self.state["alert_times"].append(ts(now))
        log.info("notify [%s] %s: %s", msg.severity, msg.title, msg.body.replace("\n", " | "))
        send_all(self.notifiers, msg)
        return True

    def _deliver(self, alerts: list[str], recoveries: list[str], now: datetime) -> None:
        name = self.cfg.name
        if alerts:
            title = f"{name}: problem" if len(alerts) == 1 else f"{name}: {len(alerts)} problems"
            self._send(Message(title, "\n".join(f"- {a}" for a in alerts), "alert"), now)
        if recoveries:
            # Recoveries always go out: they close an alert someone is acting on.
            self._send(
                Message(f"{name}: recovered", "\n".join(f"- {r}" for r in recoveries), "recovery"),
                now,
                bypass_limit=True,
            )

    # ------------------------------------------------------------------- power
    def _busy_probe(self, results: list[Result], now: datetime) -> None:
        if not self.cfg.power_cycle.enabled:
            return
        if not any(r.key == "api" and r.ok for r in results):
            return
        try:
            reasons = detect_busy(self.client)
        except HAError as exc:
            log.warning("busy probe failed: %s", exc)
            return
        if reasons:
            self.state["power"]["busy_seen"] = ts(now)
            self.state["power"]["busy_reason"] = "; ".join(reasons)

    def _down_since(self, now: datetime) -> datetime | None:
        """When all trigger checks have been failing since, or None if any is passing."""
        starts = []
        for check in self.cfg.power_cycle.trigger_checks:
            failing = [
                parse(v["first_failure"])
                for v in self.state["checks"].values()
                if v["check"] == check and not v["ok"] and v["first_failure"]
            ]
            if not failing:
                return None
            starts.append(min(failing))
        return max(starts) if starts else None

    def _maybe_power_cycle(self, now: datetime) -> None:
        pc = self.cfg.power_cycle
        if not pc.enabled:
            return
        pstate = self.state["power"]
        cycles = [parse(c) for c in pstate["cycles"] if now - parse(c) < timedelta(days=2)]
        decision = power.decide(
            pc, now, self._down_since(now), cycles, parse(pstate["busy_seen"]), pstate["busy_reason"]
        )
        changed = decision.reason != pstate["last_decision"]
        pstate["last_decision"] = decision.reason
        if not decision.allowed:
            if changed and decision.reason.startswith(("held off", "daily cap", "cooldown")):
                log.warning("power cycle not done: %s", decision.reason)
            return
        try:
            outcome = power.run_cycle(self.driver, pc)
            if not pc.dry_run:
                cycles.append(now)
            pstate["cycles"] = [ts(c) for c in cycles]
            body = f"{self.cfg.name} {decision.reason}: {outcome}."
            self._send(Message(f"{self.cfg.name}: power cycle", body, "alert"), now, bypass_limit=True)
        except power.PowerError as exc:
            cycles.append(now)  # count failed attempts too, so a broken plug is not hammered
            pstate["cycles"] = [ts(c) for c in cycles]
            self._send(
                Message(f"{self.cfg.name}: power cycle FAILED", str(exc), "alert"), now, bypass_limit=True
            )

    # -------------------------------------------------------- heartbeat / dead-man
    def _heartbeat(self, now: datetime) -> None:
        hb = self.cfg.heartbeat
        if not hb.enabled:
            return
        local = now.astimezone(self.local_tz)
        hh, mm = (int(x) for x in hb.time.split(":"))
        today = local.date().isoformat()
        if self.state["heartbeat_last"] == today or (local.hour, local.minute) < (hh, mm):
            return
        self.state["heartbeat_last"] = today
        failing = [v["message"] for v in self.state["checks"].values() if v["alerting"]]
        body = (
            "All checks passing."
            if not failing
            else "Still failing:\n" + "\n".join(f"- {m}" for m in failing)
        )
        self._send(Message(f"{self.cfg.name}: daily heartbeat", body, "info"), now, bypass_limit=True)

    def _deadman(self, now: datetime) -> None:
        dm = self.cfg.deadman
        if not dm.url:
            return
        last = parse(self.state["deadman_last"])
        if last and (now - last).total_seconds() < dm.every_seconds:
            return
        url = dm.url.rstrip("/")
        if dm.signal_failures and any(v["alerting"] for v in self.state["checks"].values()):
            url += "/fail"
        try:
            self.http.get(url, timeout=10).raise_for_status()
            self.state["deadman_last"] = ts(now)
        except httpx.HTTPError as exc:
            log.warning("dead-man ping failed: %s", type(exc).__name__)
