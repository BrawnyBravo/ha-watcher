# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- `every_seconds` is now accepted on every check, as documented. It was rejected as an
  unknown key on checks that have no default for it (`api`, `core`, `entities`, `memory`,
  `host`). Found on the first real install.

### Added

- README "Tested on" section: Raspberry Pi Zero W, Raspberry Pi OS Lite 32-bit (Trixie).

## [0.1.0] - 2026-10-09

First release.

### Added

- Checks: API reachability, token and response time; core state and safe/recovery mode;
  backup age (WebSocket `backup/info` or the backup sensor); Supervisor health and add-on
  state; entities unavailable for too long; a numeric memory sensor; TCP or ping to the host.
- Per-check consecutive-failure debounce, run interval, recovery messages with outage length,
  re-alerts, and a global hourly rate limit.
- Notifiers: ntfy, webhook (Discord, Slack or generic JSON), email over SMTP.
- Daily heartbeat and a healthchecks.io-style dead-man ping.
- Optional power cycle through a Shelly Gen2+ plug (local RPC) or a Kasa plug (python-kasa
  extra), off by default, with cooldown, daily cap, dry run, and a hold-off when a backup or
  update was last seen in progress.
- YAML config with `${VAR}` environment substitution, `--check-config`, `--once` and
  `--test-notify` modes, and an atomically written JSON state file.
- Dockerfile, systemd unit, GitHub Actions CI (ruff and pytest on Python 3.11 to 3.13).
