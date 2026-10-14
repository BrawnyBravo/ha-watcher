FROM python:3.12-slim

# ping: only for checks.host.method: ping. tzdata: so TZ=... gives local heartbeat times.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[kasa]" \
    && rm -rf /app/src

RUN useradd --system --uid 10001 --home /data watcher \
    && mkdir -p /data /config && chown watcher /data
USER watcher
WORKDIR /data
VOLUME ["/data"]

# Mount your config at /config/config.yaml and set state_file: /data/state.json
# Healthy = the watcher finished a loop within the last max(5 min, 3 x interval_seconds).
HEALTHCHECK --interval=60s --timeout=15s --start-period=120s --retries=3 \
    CMD ["ha-watcher", "-c", "/config/config.yaml", "--health"]

ENTRYPOINT ["ha-watcher", "-c", "/config/config.yaml"]
