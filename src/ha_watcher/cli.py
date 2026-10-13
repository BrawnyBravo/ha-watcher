"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys

import httpx

from ha_watcher import __version__
from ha_watcher.config import TWILIO_ENV, Config, ConfigError, load_config
from ha_watcher.engine import Watcher
from ha_watcher.escalation import CallError, place_call
from ha_watcher.ha import HAClient
from ha_watcher.notifiers import Message, build_notifiers, send_all
from ha_watcher.power import PowerError, build_driver

log = logging.getLogger("ha_watcher")


def build_watcher(cfg: Config) -> Watcher:
    missing = cfg.phone_call.missing_env
    if cfg.phone_call.enabled and 0 < len(missing) < len(TWILIO_ENV):
        log.warning("phone calls are OFF: %s not set", ", ".join(missing))
    http = httpx.Client(timeout=15)
    notifiers = build_notifiers(cfg.notifiers, http)
    driver = build_driver(cfg.power_cycle) if cfg.power_cycle.enabled else None
    return Watcher(cfg, HAClient(cfg.home_assistant), notifiers, http, driver=driver)


def summarize(cfg: Config) -> str:
    enabled = [n for n, c in cfg.checks.items() if c.enabled]
    lines = [
        f"Home Assistant: {cfg.home_assistant.url}",
        f"Checks enabled: {', '.join(enabled) or 'none'}",
        f"Notifiers: {', '.join(n['type'] for n in cfg.notifiers) or 'none (alerts only logged)'}",
        f"Heartbeat: {'daily at ' + cfg.heartbeat.time if cfg.heartbeat.enabled else 'off'}",
        f"Dead-man ping: {'on' if cfg.deadman.url else 'off'}",
    ]
    pc = cfg.power_cycle
    if pc.enabled:
        lines.append(
            f"Power cycle: {pc.driver} after {pc.after_minutes:g}m down, cooldown "
            f"{pc.cooldown_minutes:g}m, max {pc.max_per_day}/day{' (dry run)' if pc.dry_run else ''}"
        )
    else:
        lines.append("Power cycle: off")
    rc = cfg.recovery_command
    if rc.enabled:
        guard = f", only while {', '.join(rc.only_if_passing)} passes" if rc.only_if_passing else ""
        lines.append(
            f"Recovery command: {rc.command[0]} after {rc.after_minutes:g}m down{guard}, cooldown "
            f"{rc.cooldown_minutes:g}m, max {rc.max_per_day}/day{' (dry run)' if rc.dry_run else ''}"
        )
    else:
        lines.append("Recovery command: off")
    lines.append(_phone_summary(cfg))
    ex = cfg.expiry
    if ex.tls_hosts or ex.domains:
        lines.append(
            f"Expiry warnings: {len(ex.tls_hosts)} certificate(s), {len(ex.domains)} domain(s), "
            f"at {'/'.join(str(d) for d in ex.warn_days)} days"
        )
    else:
        lines.append("Expiry warnings: off (no tls_hosts or domains listed)")
    return "\n".join(lines)


def _phone_summary(cfg: Config) -> str:
    pc = cfg.phone_call
    if pc.active:
        return (
            f"Phone call: on, after {pc.after_minutes:g}m of {', '.join(pc.trigger_checks)} failing, "
            f"once per outage, max {pc.max_per_day}/day"
        )
    if not pc.enabled:
        return "Phone call: off (phone_call.enabled is false)"
    if len(pc.missing_env) == len(TWILIO_ENV):
        return "Phone call: off (no TWILIO_* variables set)"
    return f"Phone call: off (missing {', '.join(pc.missing_env)})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ha-watcher", description="External watchdog for Home Assistant.")
    ap.add_argument("-c", "--config", default="config.yaml", help="path to the YAML config file")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check-config", action="store_true", help="validate the config and exit")
    mode.add_argument("--once", action="store_true", help="run every check once, print, exit")
    mode.add_argument("--test-notify", action="store_true", help="send a test message to every notifier")
    mode.add_argument("--test-call", action="store_true", help="place one test phone call through Twilio")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include URLs

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.check_config:
        try:
            build_notifiers(cfg.notifiers, httpx.Client())
            if cfg.power_cycle.enabled:
                build_driver(cfg.power_cycle)
            rc = cfg.recovery_command
            if rc.enabled and not shutil.which(rc.command[0]):
                raise ValueError(f"recovery_command: '{rc.command[0]}' not found or not executable")
        except (ValueError, PowerError) as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
        print("Config OK\n" + summarize(cfg))
        return 0

    try:
        watcher = build_watcher(cfg)
    except (ValueError, PowerError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.test_call:
        twilio = cfg.phone_call.twilio
        if not cfg.phone_call.active or twilio is None:
            print(_phone_summary(cfg), file=sys.stderr)
            return 2
        try:
            place_call(httpx.Client(timeout=15), twilio, f"This is a test call from the {cfg.name} watcher.")
        except CallError as exc:
            print(f"Test call failed: {exc}", file=sys.stderr)
            return 1
        print("Test call placed. The phone should ring within a minute.")
        return 0

    if args.test_notify:
        sent = send_all(watcher.notifiers, Message(f"{cfg.name}: test", "ha-watcher test message.", "info"))
        print(f"Sent to {sent} of {len(watcher.notifiers)} notifier(s).")
        return 0 if sent == len(watcher.notifiers) else 1

    if args.once:
        results = watcher.run_once()
        for r in results:
            print(f"{'OK  ' if r.ok else 'FAIL'} {r.key:<32} {r.message}")
        return 0 if all(r.ok for r in results) else 1

    try:
        watcher.run_forever()
    except KeyboardInterrupt:
        return 0
    return 0
