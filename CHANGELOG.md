# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- `home_assistant_push.secret`: spaces and line breaks at either end are now removed
  before use, the same way the HA Watcher integration trims the secret it saves. Before,
  a secret pasted with a stray space signed every report with a different key and Home
  Assistant refused them all (401). The 16-character minimum now applies after trimming.

## [1.0.0] - 2026-10-19

The first release meant for anyone to install and rely on. Everything from the unreleased
0.x work below is included.

### Upgrading from 0.1.0

- The state file carries over. Older files are read as before, and parts with a wrong type
  are reset one by one instead of discarding the file or crashing.
- **Run `ha-watcher -c <config> --check-config` before restarting the service.** Config
  checks are stricter, so a config that 0.1.0 accepted may now be refused with a message
  naming the key. The systemd unit now runs this check before every start: a refused
  config keeps the service from starting (it retries every 30 seconds and logs the reason),
  and nothing watches Home Assistant until it is fixed. What is now refused:
  - `power_cycle.after_minutes` must be greater than `recovery_command.after_minutes` when
    both are enabled.
  - Unknown keys in a notifier are errors.
  - On/off settings must be `true`/`false` (strings from `${VAR}` such as `"false"` or
    `"no"` are understood); other values are errors.
  - Lists (`entity_ids`, `addons`, `trigger_checks`, `only_if_passing`) must be lists, ports
    and timeouts must be valid numbers, a non-empty `ca_file` must exist and hold a
    certificate, and `deadman.url` must start with `http://` or `https://`.
  - Still accepted: numbers given as text (`port: "8123"`, for example from `${PORT}`),
    whole numbers written as decimals (`max_per_hour: 20.0`), and an empty `ca_file`
    (from `${HA_CA_FILE:-}`), which means none.

### Fixed

- `"false"` from an environment variable (for example `enabled: ${POWER_CYCLE:-false}`)
  counted as on, because any non-empty string was treated as true. On/off settings are
  now parsed properly.
- An alert held back by the rate limit was marked as sent and, with `realert_minutes: 0`,
  never delivered. It now goes out on the first loop with room.
- An alert that no notifier accepted (network down at that moment) was marked as sent. It
  is now retried every loop until one notifier takes it.
- A dry-run power cycle or recovery command sent a message on every loop once the outage
  passed `after_minutes`. Dry runs now follow the same cooldown and daily cap.
- A power-cycle driver error other than the expected kind (for example a Shelly answering
  with something that is not JSON) skipped the attempt counter, so the guard allowed
  another cycle on the next loop. Every attempt is now counted and saved before the plug
  is touched.
- After the watcher had not been running for a while, or after the system clock jumped (a
  Raspberry Pi sets its clock from the network after boot), an old outage start time could
  make the recovery command or power cycle fire at once, possibly while Home Assistant
  was still booting. Outage timers now restart in both cases.
- A state file naming a check that this version does not know made every loop fail.
- A crash in one step of a loop (delivery, recovery actions, heartbeat) skipped the rest of
  that loop, including the dead-man ping and the state save. Steps are now isolated.
- A Kasa plug that stopped answering could hang the watcher; every Kasa network step is
  now limited to 20 seconds.
- A bad `ca_file` crashed at start-up with a traceback; it is now a config error, also
  reported by `--check-config`.
- `--once` from cron: each run would have looked like a restart after a long gap and
  restarted the outage clock, so at a 5-minute or longer cron spacing the recovery command,
  power cycle and phone call never fired. The gap rule is skipped in `--once` mode.
- A delivered alert whose notifier reply timed out was resent every loop (58 times in an
  hour in a test). Undelivered alerts now wait 1, 2, 4 ... minutes between tries (at most
  30, or `realert_minutes`), a failure streak is logged as an error once, and an alert held
  back by the rate limit is counted once in the "suppressed" note. (Before release the
  retry wait was cut to at most 5 minutes, held alerts go out as soon as any message is
  delivered, and an alert held only by the rate limit goes out as soon as there is room.)
- A clock correction now also resets the dead-man, busy, last-run, expiry and phone
  timestamps that were in the future, so the dead-man ping and checks with `every_seconds`
  do not stop until the clock catches up.
- Kasa: a turn-off whose reply was lost left the host off; turn-on is now always attempted,
  and a failed turn-on says the host may be OFF.
- A stop request (SIGTERM) during a power cycle now waits for the cycle to finish.
- Every outbound call now has a wall-clock limit on the whole request (Home Assistant
  REST and WebSocket, notifiers, dead-man ping, Shelly, Twilio, RDAP, certificate and TCP
  host lookups, status push). Library timeouts only limit each read, so a server sending
  one byte at a time could hold the watch loop indefinitely.
- Number settings refuse `nan` and `inf` (an `alerts.realert_minutes: nan` stopped every
  alert).
- The container ignored `docker stop` (Python as PID 1 ignores SIGTERM) and was killed
  after the timeout. SIGTERM now ends it at once.

### Added

- `home_assistant_push`: an optional status report every `every_seconds` (default 60) to
  the HA Watcher companion integration, following its payload contract v1: check names
  mapped to `[a-z0-9_]{1,40}` once over every configured check key (sorted, shortened with a
  hash when too long or taken, so names never depend on what one report contains), at
  most 32 checks (the first 32 in sorted order), body under 64 KiB, counts kept within the
  contract's limits (failures 10^6, alerts 10^9, interval 1 day),
  optional HMAC-SHA256 signing with `secret`. Off unless `url` is set; the URL and secret
  are never logged; a failed report is logged once per failure streak and never affects
  alerts. `outage` is true only while the `api` or `core` check is alerting;
  `alerts_sent_24h` counts problem alerts that reached a notifier; `sent_at` and the
  signature timestamp are taken when the report is sent and always increase.
- `--health`: exits 0 when the running watcher saved a loop within max(5 minutes,
  3 x `interval_seconds`). The Docker image uses it as its `HEALTHCHECK`.
- Release workflow: on a `vX.Y.Z` tag, tests, publishes to PyPI through trusted publishing,
  and pushes a multi-arch image (amd64, arm64, arm/v7) to GHCR. CI also builds the image.
- README: a ten-minute Raspberry Pi quick start, Docker image from GHCR, troubleshooting
  table, SSH key lock-down summary. `SECURITY.md`, `CONTRIBUTING.md`, issue templates.
- Coverage report in CI (`pytest --cov`); `pytest-cov` in the `dev` extra.

### Changed

- Docker image: multi-stage build (so 32-bit ARM builds can compile what has no wheel),
  `tzdata` included so `TZ=` sets the heartbeat's local time, runs from `/data` as uid 10001.
- systemd unit: runs `--check-config` before starting, is ordered after `time-sync.target`
  (this only waits for a real sync if `systemd-time-wait-sync.service` is enabled; the
  watcher's own clock-jump handling covers the rest), more sandboxing.
- Tokens and passwords are left out of the config object's printed form.

## 0.x work after 0.1.0 (never tagged, included in 1.0.0)

### Fixed

- A Home Assistant restart now sends one alert and one recovery instead of four. After the
  API came back, Core reported `NOT_RUNNING` for a minute or two while it started, which
  sent "recovered", then a new Core "problem", then a second "recovered". The API recovery
  is now held until Core is `RUNNING` (the message says so), and Core stays quiet during
  that start-up. A Core that is still not running after 15 minutes alerts on its own. A
  held API counts as up, so the recovery command and power cycle do not fire on it.

- `every_seconds` is now accepted on every check, as documented. It was rejected as an
  unknown key on checks that have no default for it (`api`, `core`, `entities`, `memory`,
  `host`). Found on the first real install.

### Added

- `expiry`: warnings before an HTTPS certificate (`tls_hosts`) or a domain registration
  (`domains`, looked up over public RDAP through the IANA bootstrap file, no key) runs out.
  One alert per `warn_days` level crossed (default 21 and 7), one more when it expires, a
  "renewed" message when the date moves later, and one "cannot check" message after
  repeated failed lookups. RDAP: at most one request per loop, answers kept a day, a 403 or
  429 pauses lookups for a day. Empty by default.
- `phone_call`: a Twilio voice call when the `trigger_checks` (default `api`) have been
  failing for `after_minutes` (default 30). Once per outage, `max_per_day` cap (default 2,
  failed attempts included), retry after a failed attempt, a notification after each call.
  Off unless all four `TWILIO_*` environment variables are set; `enabled: false` is a kill
  switch. `--check-config` reports it; `--test-call` places one test call.
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
