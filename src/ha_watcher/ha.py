"""Minimal Home Assistant client: REST over httpx, WebSocket over websockets (sync)."""

from __future__ import annotations

import json
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from websockets.sync.client import connect as ws_connect

from ha_watcher.config import HomeAssistantConfig


class HAError(Exception):
    """A request to Home Assistant failed. The message never contains the token."""


class HAAuthError(HAError):
    """Home Assistant rejected the token."""


@dataclass
class TimedResponse:
    status_code: int
    elapsed_ms: float
    data: Any


class HAClient:
    def __init__(
        self,
        cfg: HomeAssistantConfig,
        transport: httpx.BaseTransport | None = None,
        ws_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.cfg = cfg
        verify: bool | ssl.SSLContext = cfg.verify_ssl
        if cfg.ca_file and cfg.verify_ssl:
            verify = ssl.create_default_context(cafile=cfg.ca_file)
        self._http = httpx.Client(
            base_url=cfg.url,
            headers={"Authorization": f"Bearer {cfg.token}"},
            timeout=cfg.timeout_seconds,
            verify=verify,
            transport=transport,
        )
        self._ws_factory = ws_factory or ws_connect

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ REST
    def get(self, path: str) -> TimedResponse:
        start = time.perf_counter()
        try:
            resp = self._http.get(path)
        except httpx.TimeoutException as exc:
            raise HAError(f"GET {path}: timed out after {self.cfg.timeout_seconds:g}s") from exc
        except httpx.HTTPError as exc:
            raise HAError(f"GET {path}: {type(exc).__name__}") from exc
        elapsed = (time.perf_counter() - start) * 1000
        if resp.status_code == 401:
            raise HAAuthError(f"GET {path}: 401 unauthorized (token rejected)")
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        return TimedResponse(resp.status_code, elapsed, data)

    def get_json(self, path: str) -> Any:
        r = self.get(path)
        if r.status_code != 200:
            raise HAError(f"GET {path}: HTTP {r.status_code}")
        return r.data

    def get_state(self, entity_id: str) -> dict[str, Any] | None:
        """Return the state object, or None if the entity does not exist."""
        r = self.get(f"/api/states/{entity_id}")
        if r.status_code == 404:
            return None
        if r.status_code != 200 or not isinstance(r.data, dict):
            raise HAError(f"GET /api/states/{entity_id}: HTTP {r.status_code}")
        return r.data

    def get_states(self) -> list[dict[str, Any]]:
        data = self.get_json("/api/states")
        if not isinstance(data, list):
            raise HAError("GET /api/states: unexpected response")
        return data

    # ------------------------------------------------------------- WebSocket
    @property
    def ws_url(self) -> str:
        base = self.cfg.url
        if base.startswith("https://"):
            return "wss://" + base[len("https://") :] + "/api/websocket"
        return "ws://" + base[len("http://") :] + "/api/websocket"

    def ws_command(self, msg_type: str, **payload: Any) -> Any:
        """Open a connection, authenticate, send one command, return its result."""
        kwargs: dict[str, Any] = {"open_timeout": self.cfg.timeout_seconds}
        if self.ws_url.startswith("wss://") and (self.cfg.ca_file or not self.cfg.verify_ssl):
            ctx = ssl.create_default_context(cafile=self.cfg.ca_file)
            if not self.cfg.verify_ssl:  # explicit opt-out; prefer ca_file
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            kwargs["ssl"] = ctx
        try:
            with self._ws_factory(self.ws_url, **kwargs) as ws:
                hello = self._recv(ws)
                if hello.get("type") != "auth_required":
                    raise HAError(f"websocket: unexpected greeting {hello.get('type')!r}")
                ws.send(json.dumps({"type": "auth", "access_token": self.cfg.token}))
                auth = self._recv(ws)
                if auth.get("type") == "auth_invalid":
                    raise HAAuthError("websocket: token rejected")
                if auth.get("type") != "auth_ok":
                    raise HAError(f"websocket: unexpected auth reply {auth.get('type')!r}")
                ws.send(json.dumps({"id": 1, "type": msg_type, **payload}))
                while True:
                    msg = self._recv(ws)
                    if msg.get("id") == 1 and msg.get("type") == "result":
                        break
        except HAError:
            raise
        except Exception as exc:  # network, timeout, protocol errors
            raise HAError(f"websocket {msg_type}: {type(exc).__name__}") from exc
        if not msg.get("success"):
            err = msg.get("error") or {}
            raise HAError(f"websocket {msg_type}: {err.get('code', 'error')}: {err.get('message', '')}")
        return msg.get("result")

    def _recv(self, ws: Any) -> dict[str, Any]:
        raw = ws.recv(timeout=self.cfg.timeout_seconds)
        msg = json.loads(raw)
        if not isinstance(msg, dict):
            raise HAError("websocket: unexpected message")
        return msg
