# Security policy

## Supported versions

Only the latest release gets fixes. Upgrade before reporting.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private
vulnerability reporting instead: the **Security** tab of this repository, **Report a
vulnerability**. You should get an answer within a week. Once a fix is out, the report is credited in the changelog
unless you ask otherwise.

Useful to include: the version (`ha-watcher --version`), how it is installed (pip, Docker,
systemd), and the smallest config that shows the problem, with every token, URL, address
and password replaced by a placeholder.

## What counts

- A secret (Home Assistant token, webhook URL, ntfy token, SMTP or plug password, Twilio
  credentials) ending up in a log line, a notification, `--check-config` output or the
  state file.
- The recovery command or the power cycle acting when its guards say it must not
  (outage too short, cooldown, daily cap, busy hold-off, `only_if_passing`), or acting
  repeatedly.
- Anything that lets someone else on the network make the watcher run a command or
  switch the plug.

Out of scope: problems that need an attacker who can already edit the config file or the
environment file, and the security of Home Assistant, the plug or the notification
services themselves.
