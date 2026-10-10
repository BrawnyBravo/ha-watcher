"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys

import httpx

from ha_watcher import __version__
from ha_watcher.config import Config, ConfigError, load_config
from ha_watcher.engine import Watcher
from ha_watcher.ha import HAClient
from ha_watcher.notifiers import Message, build_notifiers, send_all
from ha_watcher.power import PowerError, build_driver


def build_watcher(cfg: Config) -> Watcher:
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
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ha-watcher", description="External watchdog for Home Assistant.")
    ap.add_argument("-c", "--config", default="config.yaml", help="path to the YAML config file")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check-config", action="store_true", help="validate the config and exit")
    mode.add_argument("--once", action="store_true", help="run every check once, print, exit")
    mode.add_argument("--test-notify", action="store_true", help="send a test message to every notifier")
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
