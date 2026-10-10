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
- One YAML file, secrets from environment variables, a JSON state file written atomically,
  `--check-config`, `--once` and `--test-notify` modes.

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

You need Python 3.11+ (Raspberry Pi OS Bookworm ships 3.11, Trixie ships 3.13).

1. In Home Assistant: your profile, **Security** tab, **Long-lived access tokens**,
   create one called `ha-watcher`. See [Security](#security) for which user to create it on.
2. On the Pi:

   ```sh
   sudo useradd --system --home /var/lib/ha-watcher ha-watcher
   sudo install -d -o ha-watcher /var/lib/ha-watcher
   sudo python3 -m venv /opt/ha-watcher/venv
   sudo /opt/ha-watcher/venv/bin/pip install ha-watcher        # add [kasa] for Kasa plugs
   sudo mkdir -p /etc/ha-watcher
   sudo curl -o /etc/ha-watcher/config.yaml \
     https://raw.githubusercontent.com/BrawnyBravo/ha-watcher/main/config.example.yaml
   sudo nano /etc/ha-watcher/config.yaml                       # url, checks, notifiers
   sudo install -m 640 -g ha-watcher /dev/null /etc/ha-watcher/env
   sudo nano /etc/ha-watcher/env                               # HA_TOKEN=..., NTFY_TOPIC=...
   ```

   Until it is on PyPI, install from a checkout: `pip install /path/to/ha-watcher`.

3. Test it as the service user:

   ```sh
   sudo -u ha-watcher bash -c 'set -a; . /etc/ha-watcher/env;
     /opt/ha-watcher/venv/bin/ha-watcher -c /etc/ha-watcher/config.yaml --check-config'
   # then the same with --test-notify, then --once
   ```

4. Run it as a service with [`contrib/ha-watcher.service`](contrib/ha-watcher.service):

   ```sh
   sudo cp contrib/ha-watcher.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now ha-watcher
   journalctl -u ha-watcher -f
   ```

   Set `state_file: /var/lib/ha-watcher/state.json` in the config.

## Quick start: Docker

```sh
docker build -t ha-watcher .
docker run -d --name ha-watcher --restart unless-stopped \
  -v /path/to/config.yaml:/config/config.yaml:ro \
  -v ha-watcher-data:/data \
  --env-file /path/to/ha-watcher.env \
  ha-watcher
```

Or with Compose:

```yaml
services:
  ha-watcher:
    build: .
    restart: unless-stopped
    env_file: ./ha-watcher.env
    volumes:
      - ./config.yaml:/config/config.yaml:ro
      - ha-watcher-data:/data
volumes:
  ha-watcher-data:
```

Set `state_file: /data/state.json`. Do not run the container on the Home Assistant host:
it would go down with the thing it watches.

## Command line

```
ha-watcher -c config.yaml                 run forever
ha-watcher -c config.yaml --check-config  validate config (and notifier/plug settings), exit 2 on error
ha-watcher -c config.yaml --once          one full cycle, print each result, exit 1 if anything failed
ha-watcher -c config.yaml --test-notify   send a test message to every notifier
```

`--once` is a real cycle: it updates the state file and can send alerts, so it also works
from cron if you prefer that to a long-running service:

```
*/2 * * * * set -a; . /etc/ha-watcher/env; /opt/ha-watcher/venv/bin/ha-watcher -c /etc/ha-watcher/config.yaml --once >/dev/null 2>&1
```

(Per-check `every_seconds` still applies, because the last run time is kept in the state
file; the debounce counts runs of that check.)

## Configuration reference

See [`config.example.yaml`](config.example.yaml) for a complete, commented file. Any string
may use `${VAR}` or `${VAR:-default}`; a missing variable without a default is a config
error that names the variable. Unknown keys are errors, so typos are caught.

### Top level

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | `Home Assistant` | Used in alert titles. |
| `interval_seconds` | `60` | Loop interval. |
| `state_file` | `ha-watcher-state.json` | Debounce counters, alert times, power-cycle and recovery-command history. |

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
| `max_per_hour` | `20` | Cap on messages. Recoveries, heartbeats and power-cycle reports bypass it. |

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
| `deadman.url` | none | Pinged with `GET` every `every_seconds`. |
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
| `dry_run` | `false` | Report what would happen without switching. |

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
| `dry_run` | `false` | Report what would happen without running anything. |

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
Giving the power cycle `trigger_checks: [host]` makes it act only when the machine itself
stops answering. The two never act in the same loop.

## How the power cycle decides

1. Every trigger check has been failing continuously for `after_minutes`.
2. No cycle in the last `cooldown_minutes`, and fewer than `max_per_day` in the last 24 hours.
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
- **The plug.** A Shelly with no password lets anyone on your LAN switch it. Set one.

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
- Alerts that fail to deliver are logged, not queued for later.
- Tested with mocked HTTP and WebSocket traffic, plus one real local WebSocket server. It has
  not yet been run against every HA install type: reports welcome.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,kasa]"
pytest
ruff check . && ruff format --check .
```

## License

MIT. See [LICENSE](LICENSE).
