"""Optional last-resort action: power-cycle the Home Assistant host with a smart plug.

Off by default. Guarded by a minimum outage length, a cooldown, a daily cap, and a
"busy" hold-off so it never cuts power while a backup or update was last seen running.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from ha_watcher.config import PowerCycleConfig


class PowerError(Exception):
    pass


class ShellyGen2Driver:
    """Shelly Gen2/Gen3/Gen4 local RPC. Uses ``toggle_after`` so the plug turns itself
    back on even if this watcher dies mid-cycle."""

    def __init__(self, cfg: PowerCycleConfig, transport: httpx.BaseTransport | None = None) -> None:
        self.cfg = cfg
        auth = None
        if cfg.password:
            auth = httpx.DigestAuth(cfg.username or "admin", cfg.password)
        host = cfg.host if cfg.host.startswith("http") else f"http://{cfg.host}"
        self.http = httpx.Client(base_url=host, auth=auth, timeout=10, transport=transport)

    def power_cycle(self, off_seconds: int) -> None:
        body = {
            "id": 1,
            "method": "Switch.Set",
            "params": {"id": self.cfg.switch_id, "on": False, "toggle_after": off_seconds},
        }
        try:
            resp = self.http.post("/rpc", json=body)
        except httpx.HTTPError as exc:
            raise PowerError(f"Shelly unreachable: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise PowerError(f"Shelly returned HTTP {resp.status_code}")
        data = resp.json()
        if "error" in data:
            raise PowerError(f"Shelly error: {data['error'].get('message', data['error'])}")


class KasaDriver:
    """TP-Link Kasa/Tapo via python-kasa (``pip install ha-watcher[kasa]``)."""

    def __init__(
        self,
        cfg: PowerCycleConfig,
        discover: Callable[..., Any] | None = None,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self.cfg = cfg
        if discover is None:
            try:
                from kasa import Discover
            except ImportError as exc:  # pragma: no cover - depends on install
                raise PowerError("python-kasa is not installed: pip install 'ha-watcher[kasa]'") from exc
            discover = Discover.discover_single
        self.discover = discover
        self.sleep = sleep or asyncio.sleep

    async def _cycle(self, off_seconds: int) -> None:
        dev = await self.discover(self.cfg.host, username=self.cfg.username, password=self.cfg.password)
        if dev is None:
            raise PowerError("Kasa device not found")
        try:
            await dev.update()
            await dev.turn_off()
            await self.sleep(off_seconds)
            last: Exception | None = None
            for _ in range(3):  # Kasa cannot switch itself back on, so try hard
                try:
                    await dev.turn_on()
                    return
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    await self.sleep(2)
            raise PowerError(f"Kasa plug did not turn back on: {type(last).__name__}")
        finally:
            await dev.disconnect()

    def power_cycle(self, off_seconds: int) -> None:
        try:
            asyncio.run(self._cycle(off_seconds))
        except PowerError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PowerError(f"Kasa failed: {type(exc).__name__}") from exc


def build_driver(cfg: PowerCycleConfig) -> Any:
    return KasaDriver(cfg) if cfg.driver == "kasa" else ShellyGen2Driver(cfg)


@dataclass
class Decision:
    allowed: bool
    reason: str


def decide(
    cfg: PowerCycleConfig,
    now: datetime,
    down_since: datetime | None,
    cycles: list[datetime],
    busy_seen: datetime | None,
    busy_reason: str | None = None,
) -> Decision:
    """Pure guard logic: may we power-cycle right now?"""
    if not cfg.enabled:
        return Decision(False, "power cycling is disabled")
    if down_since is None:
        return Decision(False, "Home Assistant is not down")
    down = now - down_since
    if down < timedelta(minutes=cfg.after_minutes):
        return Decision(False, f"down {int(down.total_seconds() // 60)}m, waiting for {cfg.after_minutes:g}m")
    if cycles and now - max(cycles) < timedelta(minutes=cfg.cooldown_minutes):
        return Decision(False, f"cooldown: last power cycle was under {cfg.cooldown_minutes:g}m ago")
    recent = [c for c in cycles if now - c < timedelta(hours=24)]
    if len(recent) >= cfg.max_per_day:
        return Decision(False, f"daily cap reached ({cfg.max_per_day} in 24h)")
    if busy_seen and now - busy_seen < timedelta(minutes=cfg.busy_grace_minutes):
        why = busy_reason or "a backup or update"
        return Decision(False, f"held off: {why} was in progress shortly before the outage")
    return Decision(True, f"down for {int(down.total_seconds() // 60)}m")


def run_cycle(driver: Any, cfg: PowerCycleConfig) -> str:
    if cfg.dry_run:
        return "dry run: would have power-cycled the host"
    start = time.monotonic()
    driver.power_cycle(cfg.off_seconds)
    return f"power-cycled the host (off {cfg.off_seconds}s, took {time.monotonic() - start:.0f}s)"
