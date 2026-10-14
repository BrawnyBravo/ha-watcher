# ha-watcher

An external watchdog for [Home Assistant](https://www.home-assistant.io/). It runs on a
**different machine** (a spare Raspberry Pi, a NAS, any small Linux box), checks that your
Home Assistant instance is up and healthy, and alerts you through channels that **do not
depend on Home Assistant**: ntfy, a Discord/Slack-style webhook, or email.

Home Assistant cannot tell you it is down. Its own notifications, automations and
watchdog add-ons all stop when it does. ha-watcher sits outside it.

## What it checks

| Check | What it looks at | Default |
| --- | --- | --- |
| `api` | `GET /api/` with a long-lived token: reachable, token accepted, response time | on |
| `core` | `GET /api/config`: `state` is `RUNNING`, not in safe mode or recovery mode | on |
| `backup` | Age of the newest backup (WebSocket `backup/info`, or the backup sensor) | on, 26 h |
| `supervisor` | Supervisor health and selected add-ons running (HA OS / Supervised only) | off |
| `entities` | Selected entities `unavailable`/`unknown` for longer than N minutes | off |
| `memory` | Any numeric sensor (e.g. System Monitor memory %) above a threshold | off |
| `host` | TCP connect or ping to the host itself, independent of HA | off |

Every check has a **consecutive-failure debounce** (`failures_before_alert`), its own run
interval (`every_seconds`), and sends a **recovery message** with the outage length when
it clears. Checks that need the API are skipped while the API is down, so one outage
gives one alert, not seven.

Also:

- **Re-alerts** while something stays broken (`alerts.realert_minutes`), and a global
  **rate limit** (`alerts.max_per_hour`). Recoveries always go out and report how many
  messages the rate limit swallowed.
- **Daily heartbeat** message, so silence means something.
- **Dead-man ping** to a [healthchecks.io](https://healthchecks.io)-style URL, so the
  watcher itself is watched.
- Optional **recovery command**: run a command of your choice, typically a remote
  "restart Home Assistant Core" over a locked-down SSH key, after N minutes down while the
  machine itself still answers. **Off by default**, with a cooldown and a daily cap, and a
  message before and after.
- Optional **power cycle** of the HA host through a smart plug (Shelly Gen2+ local RPC, or
  TP-Link Kasa via python-kasa) after N minutes down. **Off by default**, with a cooldown,
  a daily cap, and a hold-off if a backup or update was running just before the outage.
- **Certificate and domain expiry warnings** for the hosts and domains you list: one alert
  at 21 days and one at 7 (configurable), one more if it actually expires, and a message
  when it is renewed. Domain dates come from RDAP, the registries' free public lookup.
  Off until you list something.
- Optional **phone call** through Twilio when Home Assistant has been down for 30 minutes
  (configurable) and has not come back: once per outage, at most twice a day. **Off unless
  the Twilio environment variables are set**; see "Phone call escalation" below.
- Optional **status push** to the [HA Watcher integration](https://github.com/BrawnyBravo/ha-watcher-companion),
  so Home Assistant shows the watcher's checks as entities (`home_assistant_push`).
- One YAML file, secrets from environment variables, a JSON state file written atomically,
  `--check-config`, `--once`, `--test-notify`, `--test-call` and `--health` modes.

Three runtime dependencies: `httpx`, `PyYAML`, `websockets`.

## Why not X

Checked before building (October 2026). If one of these already fits, use it.

| Option | What it does well | Why it was not enough here |
| --- | --- | --- |
| [Uptime Kuma](https://github.com/louislam/uptime-kuma) | Polished web UI, HTTP/JSON-query monitors, retries, dozens of notifiers. Can check `/api/config` state with a bearer header. | No Home Assistant WebSocket checks (so no backup age from `backup/info` or Supervisor health without extra glue), no per-entity "unavailable for N minutes", no guarded power-cycle action. A full Node app with a database: heavier than needed on a small Pi. **If you only need "is HA up, tell me", use Uptime Kuma or Gatus.** |
| [Gatus](https://github.com/TwiN/gatus) | Single binary, YAML config, conditions on JSON bodies (`[BODY].state == RUNNING`), many alert providers. | Conditions do not compute "timestamp older than N hours", so backup freshness and unavailable-for-too-long are out of reach; no WebSocket API calls; no remediation action. |
| [healthchecks.io](https://healthchecks.io) | Dead-man switch: alerts when pings stop. | Only a heartbeat: it cannot look inside HA. ha-watcher uses it to watch itself (`deadman.url`). |
| A Shelly script watchdog (e.g. [Vigilo23/Shelly_Home_Assistant_watchdog](https://github.com/Vigilo23/Shelly_Home_Assistant_watchdog)) | Runs on the plug itself, no extra box. | Power cycle only: no alerts, no backup or entity checks, no daily cap or backup-in-progress guard. |
| [yanavery/ha-monitor](https://github.com/yanavery/ha-monitor) | Polls one URL, power-cycles a Shelly with retries and a cooldown. | One check, no notifications or recovery messages. |
| [sagilo/home-assistant-watchdog](https://github.com/sagilo/home-assistant-watchdog) | Google Apps Script, no hardware. | Needs HA reachable from the internet; only "is it up". |
| [SirMartin/HomeAssistant_Health_Check](https://github.com/SirMartin/HomeAssistant_Health_Check) | Restarts an HA VM on Proxmox, Telegram alerts. | Proxmox-specific, up/down only. |
| HA's own watchdog / add-on watchdog | Restarts add-ons inside HA. | Runs inside the thing that fails. |

No small, maintained tool was found that combines HA-specific checks (backup age,
Supervisor, entity availability), alerting outside HA, and a guarded power cycle. That
combination is the reason this exists; the generic parts are deliberately thin.

## Quick start: Raspberry Pi (or any Linux box)

About ten minutes. You need Python 3.11+ (Raspberry Pi OS Bookworm ships 3.11, Trixie
ships 3.13) and a way to get alerts; the quickest is the free [ntfy](https://ntfy.sh) app
with a topic name nobody can guess.

1. In Home Assistant: your profile, **Security** tab, **Long-lived access tokens**,
   create one called `ha-watcher`. See [Security](#security) for which user to create it on.
2. On the Pi, install it into its own virtual environment and fetch the example files:

   ```sh
   sudo apt install -y python3-venv git
   sudo useradd --system --user-group --home /var/lib/ha-watcher ha-watcher
   sudo install -d -o ha-watcher -g ha-watcher -m 750 /var/lib/ha-watcher
   sudo python3 -m venv /opt/ha-watcher/venv
   sudo /opt/ha-watcher/venv/bin/pip install "ha-watcher @ git+https://github.com/BrawnyBravo/ha-watcher@v1.0.0"
   # for a Kasa/Tapo plug: "ha-watcher[kasa] @ git+https://github.com/BrawnyBravo/ha-watcher@v1.0.0"
   # once 1.0.0 is on PyPI, plain "pip install ha-watcher" does the same
   sudo mkdir -p /etc/ha-watcher
   base=https://raw.githubusercontent.com/BrawnyBravo/ha-watcher/v1.0.0
   sudo curl -fsSL -o /etc/ha-watcher/config.yaml $base/config.example.yaml
   sudo curl -fsSL -o /etc/systemd/system/ha-watcher.service $base/contrib/ha-watcher.service
   ```

3. Edit the config and put the secrets in a separate file only the service can read:

   ```sh
   sudo nano /etc/ha-watcher/config.yaml   # home_assistant.url, notifiers; remove the ones you don't use
   sudo install -m 640 -g ha-watcher /dev/null /etc/ha-watcher/env
   sudo nano /etc/ha-watcher/env           # one per line: HA_TOKEN=...  NTFY_TOPIC=...
   ```

   The example's `state_file: /var/lib/ha-watcher/state.json` matches the service.

4. Test it as the service user. Fix anything `--check-config` reports before going on:

   ```sh
   run() { sudo -u ha-watcher bash -c "set -a; . /etc/ha-watcher/env; set +a;
     /opt/ha-watcher/venv/bin/ha-watcher -c /etc/ha-watcher/config.yaml $*"; }
   run --check-config     # "Config OK" and a summary
   run --test-notify      # your phone should buzz
   run --once             # one real cycle: every check printed with OK or FAIL
   ```

5. Start the service and watch its log:

   ```sh
   sudo systemctl daemon-reload && sudo systemctl enable --now ha-watcher
   journalctl -u ha-watcher -f
   ```

   To see it work end to end, stop Home Assistant (or unplug its network cable) for about
   five minutes: one alert arrives after `failures_before_alert` loops, and one recovery
   message when it is back.

To upgrade later, run the same `pip install` line with the new version tag, then
`sudo systemctl restart ha-watcher`. The state file carries over between versions.

## Quick start: Docker

Release images are published for `linux/amd64`, `linux/arm64` and `linux/arm/v7` (32-bit
Raspberry Pi OS) as `ghcr.io/brawnybravo/ha-watcher:1` (or a full version such as `1.0.0`).
To build your own instead, in a checkout: `docker build -t ha-watcher .` with BuildKit, the
default builder since Docker 23. On an older Docker (for example Debian's `docker.io` 20.10)
use `DOCKER_BUILDKIT=1 docker build -t ha-watcher .` or `docker buildx build --load -t ha-watcher .`.

```sh
docker run -d --name ha-watcher --restart unless-stopped \
  -e TZ=Europe/London \
  -v /path/to/config.yaml:/config/config.yaml:ro \
  -v ha-watcher-data:/data \
  --env-file /path/to/ha-watcher.env \
  ghcr.io/brawnybravo/ha-watcher:1
```

Or with Compose:

```yaml
services:
  ha-watcher:
    image: ghcr.io/brawnybravo/ha-watcher:1
    restart: unless-stopped
    stop_grace_period: 90s       # lets a power cycle finish; see below
    environment:
      TZ: Europe/London          # local time for heartbeat.time
    env_file: ./ha-watcher.env   # HA_TOKEN=..., NTFY_TOPIC=...
    volumes:
      - ./config.yaml:/config/config.yaml:ro
      - ha-watcher-data:/data
volumes:
  ha-watcher-data:
```

Set `state_file: /data/state.json`. The container runs as an unprivileged user (uid 10001),
so the config file must be world-readable (`chmod 644`); keep the secrets in the env file,
not in the config. Check the config before starting:
`docker run --rm --env-file ha-watcher.env -v $PWD/config.yaml:/config/config.yaml:ro ghcr.io/brawnybravo/ha-watcher:1 --check-config`.
`docker ps` shows `healthy` once the watcher is finishing loops (see `--health`). Do not
run the container on the Home Assistant host: it would go down with the thing it watches.

**With `power_cycle` enabled, give the container time to finish a cycle when it is
stopped.** A stop request during a power cycle waits for the plug to be switched back on,
but Docker kills the container after 10 seconds by default, and a Kasa cycle takes
`off_seconds` plus up to three turn-on tries. Use `--stop-timeout 90` with `docker run`, or
`stop_grace_period: 90s` in Compose (this also covers updates by tools such as Watchtower).
Shelly plugs switch themselves back on (`toggle_after`), so they are safe either way.

## Command line

```
ha-watcher -c config.yaml                 run forever
ha-watcher -c config.yaml --check-config  validate config (and notifier/plug settings), exit 2 on error
ha-watcher -c config.yaml --once          one full cycle, print each result, exit 1 if anything failed
ha-watcher -c config.yaml --test-notify   send a test message to every notifier
ha-watcher -c config.yaml --test-call     place one test phone call (needs the TWILIO_* variables)
ha-watcher -c config.yaml --health        exit 0 if the running watcher saved a loop in the last
                                          max(5 min, 3 x interval_seconds); used by the Docker HEALTHCHECK
```

`--once` is a real cycle: it updates the state file and can send alerts, so it also works
from cron if you prefer that to a long-running service:

```
*/2 * * * * set -a; . /etc/ha-watcher/env; /opt/ha-watcher/venv/bin/ha-watcher -c /etc/ha-watcher/config.yaml --once >/dev/null 2>&1
```

(Per-check `every_seconds` still applies, because the last run time is kept in the state
file; the debounce counts runs of that check.)

Timing from cron is only as fine as the cron spacing: `failures_before_alert: 3` means three
cron runs, and `after_minutes` for the recovery command, power cycle and phone call is
reached at the first run after that much time. Keep the spacing well below those delays
(every 1 to 2 minutes). In `--once` mode the watcher cannot tell a long gap between runs
from a stopped service, so the "outage clock restarts after a gap" protection described
under the power cycle applies to the long-running service only.

## Configuration reference

See [`config.example.yaml`](config.example.yaml) for a complete, commented file. Any string
may use `${VAR}` or `${VAR:-default}`; a missing variable without a default is a config
error that names the variable. Unknown keys are errors, so typos are caught. A value that
comes from a variable is always text: `true`/`false` (also `yes`/`no`, `on`/`off`, `1`/`0`)
work for on/off settings, and number settings accept text that is a number (`"8123"`).

### Top level

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | `Home Assistant` | Used in alert titles. |
| `interval_seconds` | `60` | Loop interval. |
| `state_file` | `ha-watcher-state.json` | Debounce counters, alert times, power-cycle, recovery-command and phone-call history, expiry dates and warnings sent. |

### `home_assistant`

| Key | Default | Meaning |
| --- | --- | --- |
| `url` | required | e.g. `http://192.0.2.10:8123` or `https://ha.example.com`. |
| `token` | required | Long-lived access token. Use `${HA_TOKEN}`. |
| `verify_ssl` | `true` | Leave on. Prefer `ca_file` for a self-signed certificate. |
| `ca_file` | none | CA bundle to trust for HTTPS / WSS. |
| `timeout_seconds` | `10` | Per request. |

### `checks`

Every check accepts `enabled`, `failures_before_alert` (consecutive failures before the
first alert, >= 1) and `every_seconds` (minimum gap between runs; 0 = every loop).

| Check | Options |
| --- | --- |
| `api` | `max_response_ms` (5000). Slower than this counts as a failure. |
| `core` | `alert_on_safe_mode` (true): also alert on safe mode or recovery mode. |
| `backup` | `source`: `websocket` (default; `backup/info`, newest of all listed backups and the last completed automatic backup) or `sensor` (reads `sensor_entity`, default `sensor.backup_last_successful_automatic_backup`, which tracks *automatic* backups only). `max_age_hours` (26). Runs every 30 min by default. |
| `supervisor` | `alert_on_unhealthy` (true): Supervisor `/resolution/info` unhealthy reasons. `addons`: add-on slugs that must be `started`. Each add-on alerts separately. Every 10 min by default. |
| `entities` | `entity_ids` (list), `max_unavailable_minutes` (15). Each entity alerts separately, timed from its `last_changed`. A missing entity is a failure. |
| `memory` | `entity_id`, `max_percent` (90). Any numeric sensor works; above the limit is a failure. |
| `host` | `host`, `method` (`tcp` or `ping`), `port` (8123, tcp only), `timeout_seconds` (3). Does not use HA at all. |

Defaults were picked for a daily automatic backup (26 h leaves a 2 h margin) and a
60-second loop (3 failures = about 3 minutes before the first alert). Measure your own
healthy response time with `--once` before tightening `max_response_ms`.

### `alerts`

| Key | Default | Meaning |
| --- | --- | --- |
| `realert_minutes` | `60` | Reminder interval while still failing. `0` = once per outage. |
| `max_per_hour` | `20` | Cap on messages. Recoveries, heartbeats and power-cycle reports bypass it. An alert held back by the cap is not dropped: it goes out on the first loop with room, unless the problem has cleared by then. |

### `notifiers`

A list; every message goes to all of them. One failing never blocks the others.

```yaml
notifiers:
  - type: ntfy
    server: https://ntfy.sh        # or your own server
    topic: ${NTFY_TOPIC}
    token: ${NTFY_TOKEN:-}         # optional access token
  - type: webhook
    format: discord                # discord | slack | generic
    url: ${DISCORD_WEBHOOK_URL}
    headers: {}                    # optional extra headers
  - type: email
    host: smtp.example.com
    port: 587                      # default follows security
    security: starttls             # starttls | ssl | none
    username: watcher@example.com
    password: ${SMTP_PASSWORD}
    from: watcher@example.com
    to: [you@example.com]
```

`generic` webhooks receive `{"title", "message", "severity"}` with severity `alert`,
`recovery` or `info`.

### `heartbeat` and `deadman`

| Key | Default | Meaning |
| --- | --- | --- |
| `heartbeat.enabled` | `false` | Send one message a day with the current status. |
| `heartbeat.time` | `09:00` | Local time of the watcher box (24-hour). |
| `deadman.url` | none | Pinged with `GET` every `every_seconds`. Must start with `http://` or `https://`; empty = off. |
| `deadman.every_seconds` | `60` | |
| `deadman.signal_failures` | `false` | Ping `<url>/fail` while any alert is open (healthchecks.io convention). |

### `power_cycle`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Nothing is ever switched unless this is true. |
| `driver` | `shelly` | `shelly` (Gen2/Gen3/Gen4 local RPC) or `kasa` (needs `pip install ha-watcher[kasa]`). |
| `host` | required | Plug address, e.g. `192.0.2.20`. |
| `switch_id` | `0` | Shelly switch channel. |
| `username` / `password` | none | Shelly digest auth (user `admin`), or Kasa/Tapo credentials if the device needs them. |
| `trigger_checks` | `[api]` | Checks that must **all** be failing. Add `host` to require the box to be unreachable too. |
| `after_minutes` | `10` | Continuous failure before acting, counted from the first failed check. |
| `off_seconds` | `15` | How long power stays off. |
| `cooldown_minutes` | `60` | Minimum gap between cycles. |
| `max_per_day` | `2` | Cap in any rolling 24 hours. Failed attempts count too. |
| `busy_grace_minutes` | `120` | Hold off if a backup or update was seen running within this many minutes. |
| `dry_run` | `false` | Report what would happen without switching. A dry run follows the same cooldown and daily cap, so it reports once per would-be cycle, not every loop. |

### `recovery_command`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Nothing is ever run unless this is true. |
| `command` | required | The program and its arguments as a list (no shell), e.g. `[ssh, -i, /etc/ha-watcher/restart_key, ...]`. `--check-config` checks the program exists. |
| `timeout_seconds` | `120` | The command is killed and reported as failed after this long. |
| `trigger_checks` | `[api]` | Checks that must **all** be failing. Leave `core` out when `api` is listed: `core` is skipped while the API is down, so `[api, core]` would never fire on a full outage. With `[api]` alone, a Core that answers but is still starting (for example a long database migration after an update) is left alone. |
| `only_if_passing` | `[]` | Checks that must **not** be failing, typically `[host]`: if the machine itself does not answer, a software restart cannot help, so it is left to the power cycle. |
| `after_minutes` | `10` | Continuous failure before acting, counted from the first failed trigger check. |
| `cooldown_minutes` | `60` | Minimum gap between runs. |
| `max_per_day` | `2` | Cap in any rolling 24 hours. Failed attempts count too. The run history lives in the state file, so restarting the watcher does not reset it. |
| `busy_grace_minutes` | `0` | If above 0, hold off when a backup or update was seen running within this many minutes (same probe as the power cycle). Off by default: a restart is not dangerous the way cutting power is. |
| `dry_run` | `false` | Report what would happen without running anything. Follows the same cooldown and daily cap as a real run. |

### `expiry`

Off until `tls_hosts` or `domains` lists something. Runs whether or not Home Assistant is up.

| Key | Default | Meaning |
| --- | --- | --- |
| `tls_hosts` | `[]` | Host names whose HTTPS certificate to watch, `host` or `host:port` (443). The handshake checks the chain and name like a browser, so a self-signed or wrong-name certificate is reported as "cannot check", not as a date. An expired certificate is reported as expired. |
| `domains` | `[]` | Registered domains (`example.com`, not `www.example.com`) whose registration to watch over RDAP. |
| `warn_days` | `[21, 7]` | One alert when fewer than this many days are left, per level, plus one when it expires. A first check that is already inside both levels sends one alert, not two. A later expiry date (a renewal) sends a "renewed" message and re-arms the levels. |
| `every_hours` | `24` | How often each item is looked up. A good RDAP answer is kept for at least a day regardless. |
| `timeout_seconds` | `10` | Per lookup. |
| `failed_lookups_before_alert` | `3` | Consecutive failed lookups before one "cannot check expiry" message, sent once and cleared when a lookup works. Failed certificate lookups retry after 1 hour, failed RDAP lookups after 6 hours. |

How the domain lookup treats the public service: the IANA RDAP bootstrap file
(`data.iana.org/rdap/dns.json`, RFC 9224) names the registry server for each top-level
domain; it is fetched at most once a week and only the entries for your domains are kept.
Each domain then costs one request a day to its registry, at most one RDAP request goes
out per watch loop, requests carry a `ha-watcher/<version>` user agent, and a `403` or
`429` stops all RDAP lookups for a day (or for the server's `Retry-After`, if longer). No
account or key is needed. Some top-level domains have no RDAP server in the IANA file
(for example `.io` and `.de` when this was written) and some registries publish no expiry
date over RDAP; those report "cannot check" and should be removed from the list.

### `phone_call`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | A kill switch. Calls also need all four `TWILIO_*` environment variables; without them this is off. |
| `trigger_checks` | `[api]` | Checks that must **all** be failing. Nothing else (backups, certificates, entities) ever calls. |
| `after_minutes` | `30` | Continuous failure before calling. Keep it above the recovery command and power cycle delays so those get their chance first. |
| `max_per_day` | `2` | Cap in any rolling 24 hours. Failed attempts count. When the cap is reached the watcher logs it once and stays quiet; normal alerts carry on. |
| `retry_minutes` | `10` | Wait after a failed call attempt before trying again (still within the cap). |
| `message` | see example | What the call says, twice. `{name}` and `{minutes}` are filled in. |

The account and numbers come from the environment only (`TWILIO_ACCOUNT_SID`,
`TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_TO_NUMBER`); a `twilio` key in the file
is an error. `--check-config` says whether calls are on, or which variables are missing,
without printing their values.

### `home_assistant_push`

Off unless `url` is set. See "Home Assistant companion" below.

| Key | Default | Meaning |
| --- | --- | --- |
| `url` | none | The webhook URL the HA Watcher integration shows when it is added. It is a secret: anyone who has it can post reports. Use `${HA_PUSH_URL}`. Empty = off. |
| `every_seconds` | `60` | Seconds between reports, 1 to 86400. Keep it well under half the integration's offline timeout (default 5 minutes). |
| `timeout_seconds` | `10` | Per report, at most 30. |
| `secret` | none | Optional shared secret, at least 16 characters. When set, each report carries `X-HA-Watcher-Timestamp` and `X-HA-Watcher-Signature: sha256=<HMAC-SHA256 of "timestamp.body">`; set the same secret in the integration. Use `${HA_PUSH_SECRET}`. |
| `watcher_id` | host name | Stable id for this watcher install. |

## Home Assistant companion

The optional [HA Watcher integration](https://github.com/BrawnyBravo/ha-watcher-companion)
shows the watcher inside Home Assistant: one problem sensor per check, the watcher's
version and an "online" sensor that turns off when reports stop. Add the integration in
Home Assistant, copy the webhook URL it shows into `home_assistant_push.url` (through the
env file), and restart the watcher.

This is a convenience, not part of the alerting: reports go to Home Assistant, which is
exactly what may be down. A failed report is dropped and logged once until reports work
again; it never delays or changes an alert. Reports follow the integration's payload
contract (version 1): check names are lowercased with anything outside `a-z0-9_` turned
into `_` (`core:safe_mode` becomes `core_safe_mode`), names longer than 40 characters or
already taken are shortened with a hash. Names are worked out once over every check the
config can report, in sorted order, so a check keeps its name whatever else is in a
report. At most 32 checks are sent, the first 32 in sorted order of their keys, so the
same ones every time (the integration creates at most 32 entities per install). Many
`entities` or add-ons can therefore push some checks past the 32nd; alerts still cover
them.

- A signed report must carry a newer timestamp than the last one Home Assistant accepted.
  A watcher restarted within the same second as its last signed report, or one whose clock
  steps back, gets `401` until its time passes that report; it logs one warning and
  recovers on its own.
- Give a Kasa plug's `power_cycle.host` as an IP address, not a host name: a stalled name
  lookup can hold up a power cycle.

The report's "outage" flag means Home Assistant itself is down (the `api` or `core`
check is alerting), not that any check is failing, and `alerts_sent_24h` counts problem
alerts that reached at least one notifier (not power-cycle or restart reports).

## Phone call escalation

For the one problem worth waking up for: Home Assistant down and not coming back. One
outage gives one call. The outage is identified by when it started, so a long outage is
never called twice, while a new outage after a recovery may call again (within
`max_per_day`). Each call is followed by a normal notification saying it was placed, or
that it failed and why (HTTP status and Twilio error number only).

Setting it up (a few minutes; Twilio charges per call, so check their current prices):

1. Create a Twilio account. A trial account can only call numbers you have verified in
   the console, which is enough for calling yourself.
2. Get a phone number with voice capability in the console. That is `TWILIO_FROM_NUMBER`.
3. Copy the **Account SID** (starts with `AC`) and **Auth Token** from the console's
   account dashboard.
4. Add them to the watcher's environment file (the same root-owned file as `HA_TOKEN`),
   numbers in `+E.164` form:

   ```
   TWILIO_ACCOUNT_SID=AC...
   TWILIO_AUTH_TOKEN=...
   TWILIO_FROM_NUMBER=+15555550100
   TWILIO_TO_NUMBER=+15555550199
   ```

5. Run `--check-config` (it should say `Phone call: on`), then `--test-call` once to hear
   it, then restart the service.

To switch calls off again, set `phone_call.enabled: false` or remove the variables. The
call uses Twilio's REST API directly (`POST .../Accounts/<sid>/Calls.json` with inline
TwiML); no Twilio library is installed and nothing is hosted on your side.

## Restarting Home Assistant Core automatically

A hung Core with a healthy machine underneath is the most common outage that does not fix
itself. The `recovery_command` action covers it without giving the watcher any power over
the rest of the box: on Home Assistant OS, install the **Advanced SSH & Web Terminal**
add-on and give the watcher a key that can do exactly one thing.

1. On the watcher box, make a key owned by the service user and readable by it only:

   ```sh
   sudo -u ha-watcher ssh-keygen -t ed25519 -N "" -C ha-watcher-restart -f /var/lib/ha-watcher/restart_key
   ```

2. In the add-on configuration, enable the SSH port (Network section) and add the public
   key to `authorized_keys` with a forced command, so a login with that key runs
   `ha core restart` and nothing else, whatever the client asks for. The add-on keeps the
   Supervisor token in a profile script that a forced command does not load, so source it
   first:

   ```yaml
   authorized_keys:
     - >-
       command=". /etc/profile.d/homeassistant.sh && ha core restart",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding
       ssh-ed25519 AAAA... ha-watcher-restart
   ```

   To try the setup without restarting anything, use `ha core info` as the forced command
   first, then switch to `ha core restart`.

   Leave the add-on password empty if nothing else needs password logins. Check that the
   key cannot open a shell: `ssh -i ... -tt user@host` must not give you a prompt, and
   `ssh -i ... user@host id` must run the forced command instead of `id`.

3. Pin the add-on's host key once (`ssh-keyscan -p 22 192.0.2.10 > /etc/ha-watcher/known_hosts`,
   compare the fingerprint with the add-on log if you can), then configure:

   ```yaml
   checks:
     host: {enabled: true, host: 192.0.2.10, method: ping}   # the machine, not HA's port
   recovery_command:
     enabled: true
     command: [ssh, -T, -F, /dev/null, -i, /var/lib/ha-watcher/restart_key, -p, "22",
               -o, BatchMode=yes, -o, ConnectTimeout=10, -o, IdentitiesOnly=yes,
               -o, StrictHostKeyChecking=yes, -o, UserKnownHostsFile=/etc/ha-watcher/known_hosts,
               user@192.0.2.10]
     timeout_seconds: 600
     trigger_checks: [api]
     only_if_passing: [host]
     after_minutes: 10
     cooldown_minutes: 60
     max_per_day: 2
   ```

   Point the `host` check at something that keeps answering while Core is down (ping, or a
   TCP port other than 8123), otherwise `only_if_passing: [host]` blocks every restart.

4. Test the command once by hand as the service user (`sudo -u ha-watcher <command>`). It
   really restarts Home Assistant, so pick a quiet moment. `ha core restart` only returns
   once Core is back up (about four minutes on a Home Assistant Green), so keep
   `timeout_seconds` generous.

**With a power cycle as well**, set `power_cycle.after_minutes` well above
`recovery_command.after_minutes` plus a restart (for example 10 and 20, or 30): the soft
restart goes first, and the plug only acts if Home Assistant is still down afterwards.
`--check-config` refuses a power cycle delay that is not greater than the restart delay.
Giving the power cycle `trigger_checks: [host]` makes it act only when the machine itself
stops answering. The two never act in the same loop.

## How the power cycle decides

1. Every trigger check has been failing continuously for `after_minutes`, as seen by a
   running watcher. If the watcher was not running for a while, or the system clock jumps
   (a Raspberry Pi has no real-time clock and sets its time from the network after boot),
   the outage is counted again from that moment, so nothing fires on time nobody watched.
2. No cycle in the last `cooldown_minutes`, and fewer than `max_per_day` in the last 24
   hours. The attempt is recorded in the state file *before* the plug is switched, so a
   crash or a broken plug cannot lead to a second cycle on the next loop.
3. **Busy hold-off.** While HA is up, each loop reads `/api/states` and records when it last
   saw an `update.*` entity with `in_progress` set, or the backup manager state sensor in
   `create_backup`, `receive_backup` or `restore_backup`. If that was within
   `busy_grace_minutes`, it waits. Once HA is down nothing can be asked, so this is
   *last known* state, which is the best an outside observer can do.

The Shelly driver uses `Switch.Set` with `toggle_after`, so the plug turns itself back on
even if the watcher crashes mid-cycle. Kasa plugs cannot do that, so the driver retries
`turn_on` three times and alerts if it still fails.

Never put the watcher on the same plug, or the same power strip, as the HA host. Cutting
power can corrupt a database mid-write: it is a last resort, which is why it is off by
default, capped, and announced every time.

## Security

- **Token scope.** Home Assistant tokens carry the full rights of the user that created
  them; there are no read-only scopes. Create a dedicated user for the watcher.
  - `api`, `core`, `entities`, `memory`, and `backup` with `source: sensor` work with a
    **non-admin** user.
  - The Supervisor checks need an **administrator** (HA enforces this; a non-admin token
    gets `unauthorized`, which ha-watcher reports with that hint). The WebSocket backup
    commands are admin-only too. To stay non-admin, use `backup.source: sensor` and leave
    `supervisor` off.
- **Where secrets live.** Keep the token, webhook URLs, ntfy tokens and SMTP passwords in
  environment variables: a root-owned `EnvironmentFile` readable only by the service group
  for systemd, or `--env-file` for Docker. Do not write them into `config.yaml` if that file
  is backed up or committed anywhere.
- **Logs.** The token is only sent in the `Authorization` header and never logged. Notifier
  failures log the notifier type and HTTP status only, never the URL (Discord webhook URLs
  are secrets). `httpx`'s own request logging is turned down for the same reason.
- **TLS.** Leave `verify_ssl` on. For a self-signed certificate set `ca_file`.
- **Network.** The watcher needs outbound access to HA, the plug and your notifiers only.
  It opens no ports.
- **The recovery command** runs with the watcher's own rights, with no shell, and its output
  is trimmed to one line in messages. For SSH, use a dedicated key with a forced command
  (above) so that a stolen key can only restart Home Assistant.
- **Twilio.** The auth token and both phone numbers stay in the environment; they are never
  logged, printed by `--check-config`, or put in a notification.
- **The plug.** A Shelly with no password lets anyone on your LAN switch it. Set one.
- **The SSH key for `recovery_command`, in short:** a key made only for this, readable only
  by the service user (`chmod 600`, owned by `ha-watcher`); on the Home Assistant side a
  forced `command=` plus `no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding`
  in `authorized_keys`; the host key pinned with `StrictHostKeyChecking=yes` and a
  `UserKnownHostsFile`; `BatchMode=yes` so it never waits for a password prompt. Check by
  hand that the key cannot open a shell (see "Restarting Home Assistant Core automatically").
- **The state file** holds no secrets (times, counts and check messages), but keep its
  folder owned by the service user.
- **Reporting a vulnerability:** see [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `config error: ... environment variable X is not set` | The variable is used in the config but missing from the environment. With systemd it must be in `/etc/ha-watcher/env`; when testing by hand, load that file first (`set -a; . /etc/ha-watcher/env`). Use `${X:-}` for optional values. |
| `config error: ... unknown key(s)` | A typo, or a key from another section. The message names the section; compare with `config.example.yaml`. |
| `Token rejected` / `401` | The long-lived token was deleted, or belongs to another HA instance. Create a new one and update the env file. |
| `API unreachable: GET /api/: ConnectError` | Wrong `home_assistant.url`, HA down, or a firewall. Try `curl -sS <url>/api/ -H "Authorization: Bearer $HA_TOKEN"` from the watcher box. |
| `CERTIFICATE_VERIFY_FAILED` / `SSLError` | HTTPS with a self-signed certificate: point `ca_file` at its CA (or the certificate itself). Leave `verify_ssl` on. |
| `Cannot query Supervisor ... administrator` | The Supervisor checks and the WebSocket backup source need an admin user's token; use `backup.source: sensor` and turn `supervisor` off to stay non-admin. |
| `--test-notify` says `Sent to 0 of 1` | The log line above it names the notifier and the HTTP status. 401/403: wrong ntfy token or access rules; 404: wrong webhook URL. |
| Heartbeat arrives at the wrong hour | It uses the watcher box's local time. Set the system timezone (`sudo timedatectl set-timezone ...`), or `TZ=` for Docker. |
| `ping` check always fails under systemd | The service runs with `NoNewPrivileges`, so `ping` must work without extra privileges: check `sysctl net.ipv4.ping_group_range` covers the service's group (the systemd default `0 2147483647` does), or use `method: tcp` on a port that answers. |
| Container `unhealthy` | `docker logs ha-watcher`. The health check fails when no loop has been saved recently: a config error, or `/data` not writable by uid 10001. |
| No alert during a test outage | Each check alerts after `failures_before_alert` consecutive failures, one per loop (3 minutes for the API by default). Run with `-v` to see every result. |
| "rate limit reached" in the log | More than `alerts.max_per_hour` messages in an hour, usually a flapping check. Raise `failures_before_alert` for it; held-back alerts still go out later. |

## Tested on

Real installs, watching a real Home Assistant OS instance. Reports from other hardware welcome.

| Hardware | OS | Python | Install | Running |
| --- | --- | --- | --- | --- |
| Raspberry Pi Zero W v1.1 (ARMv6, 512 MB, wifi) | Raspberry Pi OS Lite 32-bit, Trixie (2026-10-06 image) | 3.13.5 | `pip install` from GitHub in about 100 s; every dependency came as a prebuilt armv6l wheel from piwheels, nothing compiled | About 25 MB resident memory. One full cycle of five checks takes about 7 s of mostly start-up time with `--once`; the service averages a few percent CPU at a 60 s interval. |

On a Zero, put the journal in RAM (`Storage=volatile` in a `journald.conf.d` drop-in) to
spare the SD card; ha-watcher itself only writes its small state file.

## Limits and honest caveats

- Supervisor checks exist only on Home Assistant OS and Supervised installs, and only with
  an admin token. Container and Core installs report "no Supervisor".
- The `sensor` backup source tracks automatic backups only; manual ones are seen only
  through the WebSocket source.
- The busy hold-off only knows what HA reported before it went down (see above).
- The `ping` host method shells out to the system `ping`; the Docker image includes it.
- An alert that no notifier accepted is tried again on the next loop until one does, but it
  lives in memory and the state file only: there is no separate outbox, and recovery,
  heartbeat and action reports that fail to deliver are logged, not retried.
- A phone call needs the watcher's own internet connection, like every other alert.
- Expiry checks run inside the watch loop: an unreachable host can delay one loop by up to
  `expiry.timeout_seconds`, at most once an hour.
- Tested with mocked HTTP and WebSocket traffic, plus one real local WebSocket server. It has
  not yet been run against every HA install type: reports welcome.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,kasa]"
pytest --cov
ruff check . && ruff format --check .
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Releases are cut by pushing a `vX.Y.Z` tag that
matches `__version__`: the release workflow tests, publishes to PyPI (trusted publishing)
and pushes the multi-arch image to GHCR.

## License

MIT. See [LICENSE](LICENSE).
