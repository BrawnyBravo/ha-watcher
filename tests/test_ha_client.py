import json
import threading

import httpx
import pytest
from websockets.sync.server import serve

from ha_watcher.config import HomeAssistantConfig
from ha_watcher.ha import HAAuthError, HAClient, HAError
from tests.conftest import TOKEN, FakeHA, FakeWS, make_client


def test_get_ok_and_timed(fake_ha):
    r = make_client(fake_ha).get("/api/")
    assert r.status_code == 200 and r.data["message"] == "API running."
    assert r.elapsed_ms >= 0


def test_401_raises_auth_error():
    fake = FakeHA({"/api/": (401, {"message": "unauthorized"})})
    with pytest.raises(HAAuthError):
        make_client(fake).get("/api/")


def test_timeout_message_has_no_token():
    fake = FakeHA({"/api/": (0, httpx.ReadTimeout("slow"))})
    with pytest.raises(HAError) as exc:
        make_client(fake).get("/api/")
    assert "timed out" in str(exc.value) and TOKEN not in str(exc.value)


def test_connect_error():
    fake = FakeHA({"/api/": (0, httpx.ConnectError("refused"))})
    with pytest.raises(HAError, match="ConnectError"):
        make_client(fake).get("/api/")


def test_get_state_404_is_none(fake_ha):
    assert make_client(fake_ha).get_state("sensor.missing") is None


def test_ws_url_scheme():
    c = HAClient(HomeAssistantConfig(url="https://ha.example.com", token="t"))
    assert c.ws_url == "wss://ha.example.com/api/websocket"
    c = HAClient(HomeAssistantConfig(url="http://192.0.2.10:8123", token="t"))
    assert c.ws_url == "ws://192.0.2.10:8123/api/websocket"


def test_ws_command_auth_and_result_skipping_events(fake_ha):
    ws = FakeWS({"backup/info": {"backups": []}}, events_first=2)
    assert make_client(fake_ha, ws).ws_command("backup/info") == {"backups": []}
    assert ws.sent[0] == {"type": "auth", "access_token": TOKEN}
    assert ws.sent[1] == {"id": 1, "type": "backup/info"}


def test_ws_auth_invalid(fake_ha):
    with pytest.raises(HAAuthError):
        make_client(fake_ha, FakeWS({}, auth_ok=False)).ws_command("backup/info")


def test_ws_error_result(fake_ha):
    ws = FakeWS({"supervisor/api": ("error", "unauthorized", "Unauthorized")})
    with pytest.raises(HAError, match="unauthorized"):
        make_client(fake_ha, ws).ws_command("supervisor/api", endpoint="/addons", method="get")


def test_ws_against_a_real_websocket_server():
    """Same protocol over a real socket, using the websockets library both ends."""

    def handler(conn):
        conn.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(conn.recv())
        assert auth == {"type": "auth", "access_token": TOKEN}
        conn.send(json.dumps({"type": "auth_ok"}))
        cmd = json.loads(conn.recv())
        conn.send(
            json.dumps({"id": cmd["id"], "type": "result", "success": True, "result": {"echo": cmd["type"]}})
        )

    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        client = HAClient(HomeAssistantConfig(url=f"http://127.0.0.1:{port}", token=TOKEN, timeout_seconds=5))
        assert client.ws_command("backup/info") == {"echo": "backup/info"}
        server.shutdown()


def test_ws_connection_refused_is_haerror():
    client = HAClient(HomeAssistantConfig(url="http://127.0.0.1:9", token="t", timeout_seconds=2))
    with pytest.raises(HAError):
        client.ws_command("backup/info")
