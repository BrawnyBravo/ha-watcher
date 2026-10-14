"""A wall-clock limit on a whole outbound call.

httpx and smtplib timeouts apply to each socket read, so a server that answers slowly, one
byte at a time, can hold a call for as long as it likes. The watch loop must never wait
on that: ``Bounded.call`` runs the call on a daemon thread and stops waiting at the
deadline. A call that is still stuck when the next one is due is not started twice; the
next call fails at once with the same error until the stuck one ends.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import httpx


class DeadlineExceeded(httpx.TimeoutException):  # noqa: N818 - reads like the other httpx errors
    """The whole call took longer than its deadline (or an earlier one is still stuck)."""


class Bounded:
    def __init__(self, name: str) -> None:
        self.name = name
        self._thread: threading.Thread | None = None

    def call(self, seconds: float, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._thread is not None and self._thread.is_alive():
            raise DeadlineExceeded(f"{self.name}: an earlier call is still waiting for an answer")
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["value"] = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - handed back to the caller
                box["error"] = exc

        t = threading.Thread(target=run, name=f"ha-watcher {self.name}", daemon=True)
        self._thread = t
        t.start()
        t.join(seconds)
        if t.is_alive():
            raise DeadlineExceeded(f"{self.name}: no complete answer within {seconds:g}s")
        if "error" in box:
            raise box["error"]
        return box.get("value")
