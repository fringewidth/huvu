"""The browser's screen: a ticket, then a relay to Hermes's dashboard, played
against a fake dashboard."""

from __future__ import annotations

import asyncio
import json
import threading
from urllib.parse import parse_qs, urlparse

import pytest
import websockets
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from huvu.config import Config
from huvu.http import create_app
from huvu.kernel import Kernel

from .fakes import FakeHermes

TOKEN = "dash-token"
HELLO = b"RFB 003.008\n"


class FakeDashboard:
    """Hermes's dashboard, as far as the screen uses it: display.observe on
    /api/ws behind the session token, and a display socket that sends the
    RFB greeting, then echoes. `running` False answers as Hermes does for a
    profile whose screen is off."""

    def __init__(self) -> None:
        self.running = True
        self.profiles: list[str] = []
        self.tickets: set[str] = set()
        self.port = 0
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Future | None = None

    async def _handle(self, ws) -> None:
        url = urlparse(ws.request.path)
        query = parse_qs(url.query)
        if url.path == "/api/ws":
            if query.get("token") != [TOKEN]:
                await ws.close(4401)
                return
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {"type": "gateway.ready", "payload": {}}}))
            request = json.loads(await ws.recv())
            self.profiles.append(request["params"]["profile"])
            if self.running:
                self.tickets.add("display-ticket")
                reply = {"jsonrpc": "2.0", "id": request["id"], "result": {"ticket": "display-ticket", "path": "/api/display/ws", "viewer_id": "v1"}}
            else:
                reply = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": 5300, "message": "this profile's Bot Desktop is not running"}}
            await ws.send(json.dumps(reply))
        elif url.path == "/api/display/ws":
            ticket = query.get("display_ticket", [""])[0]
            if ticket not in self.tickets:
                await ws.close(4401)
                return
            self.tickets.discard(ticket)
            await ws.send(HELLO)
            async for frame in ws:
                await ws.send(frame)

    def __enter__(self) -> FakeDashboard:
        threading.Thread(target=lambda: asyncio.run(self._serve()), daemon=True).start()
        self._ready.wait(5)
        return self

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = self._loop.create_future()
        async with websockets.serve(self._handle, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop

    def __exit__(self, *_) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set_result, None)


@pytest.fixture
def dashboard():
    with FakeDashboard() as fake:
        yield fake


def app_for(tmp_path, dashboard: FakeDashboard | None):
    config = Config(data_dir=tmp_path / "data", public_url="http://test")
    if dashboard is not None:
        config.dashboard_url = f"http://127.0.0.1:{dashboard.port}"
        config.dashboard_token = TOKEN
    kernel = Kernel(config, hermes=FakeHermes())
    kernel.accounts.add_user("me", "Me", "correct horse", "key-me")
    kernel.accounts.join("home", "me", "member")
    _, token = kernel.accounts.login("me", "correct horse", "laptop", "web")
    return TestClient(create_app(config, kernel)), {"Authorization": f"Bearer {token}"}


def test_the_app_watches_its_users_screen_through_the_dashboard(tmp_path, dashboard):
    client, auth = app_for(tmp_path, dashboard)
    ticket = client.post("/api/screen/ticket", headers=auth).json()["ticket"]
    with client.websocket_connect(f"/api/screen?ticket={ticket}") as ws:
        assert ws.receive_bytes() == HELLO
        # What noVNC sends goes through, both ways.
        ws.send_bytes(b"RFB 003.008\n")
        assert ws.receive_bytes() == b"RFB 003.008\n"
    assert dashboard.profiles == ["me"]


def test_a_ticket_is_used_once(tmp_path, dashboard):
    client, auth = app_for(tmp_path, dashboard)
    ticket = client.post("/api/screen/ticket", headers=auth).json()["ticket"]
    with client.websocket_connect(f"/api/screen?ticket={ticket}") as ws:
        ws.receive_bytes()
    with client.websocket_connect(f"/api/screen?ticket={ticket}") as ws, pytest.raises(WebSocketDisconnect) as closed:
        ws.receive_bytes()
    assert closed.value.code == 4401


def test_a_screen_that_isnt_running_is_never_started(tmp_path, dashboard):
    dashboard.running = False
    client, auth = app_for(tmp_path, dashboard)
    ticket = client.post("/api/screen/ticket", headers=auth).json()["ticket"]
    with client.websocket_connect(f"/api/screen?ticket={ticket}") as ws, pytest.raises(WebSocketDisconnect) as closed:
        ws.receive_bytes()
    assert closed.value.code == 4001


def test_no_dashboard_no_screen(tmp_path):
    client, auth = app_for(tmp_path, None)
    assert client.post("/api/screen/ticket", headers=auth).status_code == 503
