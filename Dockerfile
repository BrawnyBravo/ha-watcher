# Build stage: wheels for everything. Some dependencies (cffi, PyYAML) have no
# prebuilt wheels for 32-bit ARM (linux/arm/v7), so a compiler is needed there.
FROM python:3.12-slim AS build
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libffi-dev libyaml-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels ".[kasa]"

FROM python:3.12-slim

# ping: only for checks.host.method: ping. tzdata: so TZ=... gives local heartbeat times.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping tzdata libyaml-0-2 \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-index --find-links /wheels "ha-watcher[kasa]"

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
