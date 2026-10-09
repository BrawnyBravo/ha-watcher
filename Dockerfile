FROM python:3.12-slim

# ping is only needed for checks.host.method: ping
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[kasa]"

RUN useradd --system --uid 10001 --home /data watcher \
    && mkdir -p /data /config && chown watcher /data
USER watcher
VOLUME ["/data"]

# Mount your config at /config/config.yaml and set state_file: /data/state.json
ENTRYPOINT ["ha-watcher", "-c", "/config/config.yaml"]
