from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from ha_watcher.config import parse_config
from ha_watcher.ha import HAClient

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
TOKEN = "test-token-not-real"


def base_raw(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "home_assistant": {"url": "http://192.0.2.10:8123", "token": "${HA_TOKEN}"},
        "checks": {"backup": {"enabled": False}},
        "notifiers": [],
    }
    for k, v in overrides.items():
        if k == "checks":
            raw["checks"] = {**raw["checks"], **v}
        else:
            raw[k] = v
    return raw


def make_cfg(**overrides: Any):
    return parse_config(base_raw(**overrides), env={"HA_TOKEN": TOKEN})


class FakeHA:
    """Routes for an httpx.MockTransport. Values: (status, json) or a callable(request)."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes: dict[str, Any] = {
            "/api/": (200, {"message": "API running."}),
            "/api/config": (
                200,
                {"state": "RUNNING", "version": "2026.1.0", "safe_mode": False, "recovery_mode": False},
            ),
            "/api/states": (200, []),
        }
        self.routes.update(routes or {})
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        route = self.routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={"message": "Entity not found."})
        if callable(route):
            return route(request)
        status, body = route
        if isinstance(body, Exception):
            raise body
        return httpx.Response(status, json=body)


class FakeWS:
    """Stands in for websockets.sync.client.connect: replays HA's WS protocol."""

    def __init__(self, results: dict[str, Any], auth_ok: bool = True, events_first: int = 0) -> None:
        self.results = results  # msg type -> result dict, or ("error", code, message)
        self.auth_ok = auth_ok
        self.events_first = events_first
        self.sent: list[dict[str, Any]] = []
        self.urls: list[str] = []
        self._queue: list[dict[str, Any]] = []

    # factory
    def __call__(self, url: str, **kwargs: Any) -> FakeWS:
        self.urls.append(url)
        self._queue = [{"type": "auth_required", "ha_version": "2026.1.0"}]
        return self

    def __enter__(self) -> FakeWS:
        return self

    def __exit__(self, *exc: Any) -> None:
        pass

    def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["type"] == "auth":
            ok = self.auth_ok and msg["access_token"] == TOKEN
            self._queue.append({"type": "auth_ok" if ok else "auth_invalid"})
            return
        for _ in range(self.events_first):
            self._queue.append({"id": 99, "type": "event", "event": {}})
        res = self.results.get(msg["type"])
        if isinstance(res, tuple):
            _, code, text = res
            self._queue.append(
                {
                    "id": msg["id"],
                    "type": "result",
                    "success": False,
                    "error": {"code": code, "message": text},
                }
            )
        elif res is None:
            self._queue.append(
                {
                    "id": msg["id"],
                    "type": "result",
                    "success": False,
                    "error": {"code": "unknown_command", "message": "Unknown command."},
                }
            )
        else:
            payload = res(msg) if callable(res) else res
            self._queue.append({"id": msg["id"], "type": "result", "success": True, "result": payload})

    def recv(self, timeout: float | None = None) -> str:
        return json.dumps(self._queue.pop(0))


@pytest.fixture
def fake_ha() -> FakeHA:
    return FakeHA()


def make_client(fake: FakeHA, ws: FakeWS | None = None, cfg=None) -> HAClient:
    cfg = cfg or make_cfg()
    return HAClient(cfg.home_assistant, transport=httpx.MockTransport(fake), ws_factory=ws or FakeWS({}))
