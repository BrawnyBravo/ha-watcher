# Contributing

Bug reports, test reports from other hardware, and small focused pull requests are welcome.

## Before you start

- For anything bigger than a fix, open an issue first so we can agree it fits. The tool is
  deliberately small: three runtime dependencies, one YAML file, no web UI.
- Never paste real tokens, webhook URLs, addresses or names from your own home into an
  issue, a test or a commit. Use placeholders such as `192.0.2.10`, `homeassistant.local`,
  `sensor.living_room_lamp_power`.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,kasa]"
pytest --cov
ruff check . && ruff format --check .
```

All three must pass; CI runs them on Python 3.11, 3.12 and 3.13 and builds the Docker image.

## Pull requests

- One change per pull request, with a test that fails without it. Guard logic (recovery
  command, power cycle, phone call, rate limit) needs tests for the "must not act" cases
  as well as the "acts" case.
- Match the existing style: plain names, short comments that say why, user-facing
  messages in plain English.
- Add a line to the `[Unreleased]` section of `CHANGELOG.md` (create it if needed).
- Network calls must have a timeout, and nothing may log a secret: log the exception type,
  not its text, when the text can carry a URL.

## Releases

Maintainers bump `__version__` in `src/ha_watcher/__init__.py`, date the changelog entry,
and push a matching `vX.Y.Z` tag. The release workflow does the rest.
