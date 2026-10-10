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

- Optional `recovery_command` action: runs a configured command (for example `ha core
  restart` through an SSH key restricted by a forced command) once Home Assistant has been
  down for `after_minutes` while the `only_if_passing` checks (typically `host`) still pass.
  Off by default; cooldown, daily cap, optional busy hold-off and dry run; announced before
  and after; run history kept in the state file so a watcher restart does not reset the cap.
  It runs before the power cycle, and the two never act in the same loop. README section
  "Restarting Home Assistant Core automatically".
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
