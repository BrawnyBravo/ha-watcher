"""Optional soft fix: run a command (for example a remote "restart Home Assistant Core")
when Home Assistant has been down for a while but the machine it runs on still answers.

Off by default. Guarded like the power cycle: a minimum outage length, a cooldown and a
daily cap, with every attempt (successful or not) remembered in the state file so a
restart of the watcher does not reset the counts. It is meant to run before, and much
sooner than, the power cycle: if the box answers, a restart of the software is the gentle
fix; if the box itself is gone, only a power cycle can help.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ha_watcher.config import RecoveryCommandConfig

MAX_OUTPUT = 300


class RecoveryError(Exception):
    pass


@dataclass
class Decision:
    allowed: bool
    reason: str
    down_minutes: int = 0


def decide(
    cfg: RecoveryCommandConfig,
    now: datetime,
    down_since: datetime | None,
    runs: list[datetime],
    blocking: list[str],
    busy_seen: datetime | None = None,
    busy_reason: str | None = None,
) -> Decision:
    """Pure guard logic: may we run the recovery command right now?

    ``blocking`` lists the ``only_if_passing`` checks that are currently failing.
    """
    if not cfg.enabled:
        return Decision(False, "recovery command is disabled")
    if down_since is None:
        return Decision(False, "Home Assistant is not down")
    down = now - down_since
    mins = int(down.total_seconds() // 60)
    if down < timedelta(minutes=cfg.after_minutes):
        return Decision(False, f"down {mins}m, waiting for {cfg.after_minutes:g}m", mins)
    if blocking:
        return Decision(False, f"skipped: {', '.join(blocking)} also failing (machine not answering)", mins)
    if runs and now - max(runs) < timedelta(minutes=cfg.cooldown_minutes):
        return Decision(
            False, f"cooldown: last recovery command was under {cfg.cooldown_minutes:g}m ago", mins
        )
    recent = [r for r in runs if now - r < timedelta(hours=24)]
    if len(recent) >= cfg.max_per_day:
        return Decision(False, f"daily cap reached ({cfg.max_per_day} in 24h)", mins)
    if cfg.busy_grace_minutes and busy_seen and now - busy_seen < timedelta(minutes=cfg.busy_grace_minutes):
        why = busy_reason or "a backup or update"
        return Decision(False, f"held off: {why} was in progress shortly before the outage", mins)
    return Decision(True, f"down for {mins}m", mins)


def _tail(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    out = lines[-1] if lines else ""
    return out if len(out) <= MAX_OUTPUT else out[: MAX_OUTPUT - 3] + "..."


Runner = Callable[..., Any]


def run_command(cfg: RecoveryCommandConfig, runner: Runner | None = None) -> str:
    """Run the configured command. Returns a one-line outcome; raises RecoveryError on failure."""
    if cfg.dry_run:
        return "dry run: would have run the recovery command"
    runner = runner or subprocess.run
    start = time.monotonic()
    try:
        proc = runner(
            list(cfg.command),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=cfg.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RecoveryError(f"command timed out after {cfg.timeout_seconds:g}s") from exc
    except OSError as exc:
        raise RecoveryError(f"command could not start: {type(exc).__name__}") from exc
    took = time.monotonic() - start
    if proc.returncode != 0:
        msg = f"command exited with code {proc.returncode} after {took:.0f}s"
        err = _tail(proc.stderr) or _tail(proc.stdout)
        raise RecoveryError(f"{msg}: {err}" if err else msg)
    detail = _tail(proc.stdout) or _tail(proc.stderr)
    return f"command finished OK in {took:.0f}s" + (f" ({detail})" if detail else "")
