"""The browser's screen: a ticket, then a relay to Hermes's dashboard, played
against a fake dashboard."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from datetime import timedelta
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
        # Who drives the screen, and every lease call, as (method, params).
        self.holder = "agent"
        self.leases: list[tuple[str, dict]] = []
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
            async for raw in ws:
                request = json.loads(raw)
                method, params = request["method"], request["params"]
                result: dict | None = None
                error: dict | None = None
                if method == "display.observe":
                    self.profiles.append(params["profile"])
                    if self.running:
                        self.tickets.add("display-ticket")
                        result = {"ticket": "display-ticket", "path": "/api/display/ws", "viewer_id": "v1"}
                    else:
                        error = {"code": 5300, "message": "this profile's Bot Desktop is not running"}
                elif method in ("display.lease.acquire", "display.lease.release"):
                    self.leases.append((method, params))
                    self.holder = "human" if method == "display.lease.acquire" else "agent"
                    result = {"lease": {"holder": self.holder}}
                elif method == "display.status":
                    result = {"running": self.running, "lease": {"holder": self.holder}}
                # The dashboard pushes events nobody asked for.
                await ws.send(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {"type": "sessions.changed", "payload": {}}}))
                reply = {"jsonrpc": "2.0", "id": request["id"], **({"error": error} if error else {"result": result})}
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


@pytest.fixture
def opened():
    """Clients app_for opened, closed after the test. One client used as a
    context manager runs every request and socket on one event loop, as the
    server does, so a lease call meets the view's own dashboard connection."""
    clients: list[TestClient] = []
    yield clients
    for client in clients:
        client.__exit__(None, None, None)


def app_for(tmp_path, dashboard: FakeDashboard | None, opened: list):
    config = Config(data_dir=tmp_path / "data", public_url="http://test")
    if dashboard is not None:
        config.dashboard_url = f"http://127.0.0.1:{dashboard.port}"
        config.dashboard_token = TOKEN
    kernel = Kernel(config, hermes=FakeHermes())
    kernel.accounts.add_user("me", "Me", "correct horse", "key-me")
    kernel.accounts.join("home", "me", "member")
    _, token = kernel.accounts.login("me", "correct horse", "laptop", "web")
    client = TestClient(create_app(config, kernel))
    client.__enter__()
    opened.append(client)
    client.kernel = kernel  # type: ignore[attr-defined]
    return client, {"Authorization": f"Bearer {token}"}


def someone_else(client) -> dict[str, str]:
    """A device of another member, mom's laptop."""
    kernel = client.kernel
    kernel.accounts.add_user("mom", "Mom", "battery staple", "key-mom")
    kernel.accounts.join("home", "mom", "member")
    _, token = kernel.accounts.login("mom", "battery staple", "laptop", "web")
    return {"Authorization": f"Bearer {token}"}


def test_the_app_watches_its_users_screen_through_the_dashboard(tmp_path, dashboard, opened):
    client, auth = app_for(tmp_path, dashboard, opened)
    ticket = client.post("/api/screen/ticket", headers=auth).json()["ticket"]
    with client.websocket_connect(f"/api/screen?ticket={ticket}") as ws:
        assert ws.receive_bytes() == HELLO
        # What noVNC sends goes through, both ways.
        ws.send_bytes(b"RFB 003.008\n")
        assert ws.receive_bytes() == b"RFB 003.008\n"
    assert dashboard.profiles == ["me"]


def test_a_ticket_is_used_once(tmp_path, dashboard, opened):
    client, auth = app_for(tmp_path, dashboard, opened)
    ticket = client.post("/api/screen/ticket", headers=auth).json()["ticket"]
    with client.websocket_connect(f"/api/screen?ticket={ticket}") as ws:
        ws.receive_bytes()
    with client.websocket_connect(f"/api/screen?ticket={ticket}") as ws, pytest.raises(WebSocketDisconnect) as closed:
        ws.receive_bytes()
    assert closed.value.code == 4401


def test_a_screen_that_isnt_running_is_never_started(tmp_path, dashboard, opened):
    dashboard.running = False
    client, auth = app_for(tmp_path, dashboard, opened)
    ticket = client.post("/api/screen/ticket", headers=auth).json()["ticket"]
    with client.websocket_connect(f"/api/screen?ticket={ticket}") as ws, pytest.raises(WebSocketDisconnect) as closed:
        ws.receive_bytes()
    assert closed.value.code == 4001


def test_no_dashboard_no_screen(tmp_path, opened):
    client, auth = app_for(tmp_path, None, opened)
    assert client.post("/api/screen/ticket", headers=auth).status_code == 503


def test_the_person_takes_the_screen_over_and_hands_it_back(tmp_path, dashboard, opened):
    client, auth = app_for(tmp_path, dashboard, opened)
    ticket = client.post("/api/screen/ticket", headers=auth).json()["ticket"]
    with client.websocket_connect(f"/api/screen?ticket={ticket}") as ws:
        ws.receive_bytes()
        assert client.post("/api/screen/lease", headers=auth, json={"ticket": ticket, "take": True}).json() == {"holding": True}
        assert dashboard.holder == "human"
        client.post("/api/screen/lease", headers=auth, json={"ticket": ticket, "take": False})
        assert dashboard.holder == "agent"
    # The view's own viewer id, on the connection that minted it.
    assert [(m, p["viewer_id"]) for m, p in dashboard.leases] == [("display.lease.acquire", "v1"), ("display.lease.release", "v1")]


def test_closing_the_view_hands_the_screen_back(tmp_path, dashboard, opened):
    client, auth = app_for(tmp_path, dashboard, opened)
    ticket = client.post("/api/screen/ticket", headers=auth).json()["ticket"]
    with client.websocket_connect(f"/api/screen?ticket={ticket}") as ws:
        ws.receive_bytes()
        client.post("/api/screen/lease", headers=auth, json={"ticket": ticket, "take": True})
    for _ in range(50):
        if dashboard.holder == "agent":
            break
        threading.Event().wait(0.05)
    assert dashboard.holder == "agent"


def test_only_the_views_own_person_takes_it_over(tmp_path, dashboard, opened):
    client, auth = app_for(tmp_path, dashboard, opened)
    ticket = client.post("/api/screen/ticket", headers=auth).json()["ticket"]
    with client.websocket_connect(f"/api/screen?ticket={ticket}") as ws:
        ws.receive_bytes()
        assert client.post("/api/screen/lease", headers=someone_else(client), json={"ticket": ticket, "take": True}).status_code == 404
    assert client.post("/api/screen/lease", headers=auth, json={"ticket": ticket, "take": True}).status_code == 404
    assert dashboard.leases == []


def test_the_browser_is_heard_only_while_the_person_holds_it(tmp_path, dashboard, monkeypatch, opened):
    client, auth = app_for(tmp_path, dashboard, opened)
    sound = b"ID3" + b"\xff\xfb" * 100
    monkeypatch.setattr(client.kernel.screen, "audio_command", lambda profile: [sys.executable, "-c", f"import sys; sys.stdout.buffer.write({sound!r})"])
    monkeypatch.setattr("huvu.screen.HOLD_WAIT", timedelta(seconds=0.3))
    ticket = client.post("/api/screen/ticket", headers=auth).json()["ticket"]
    with client.websocket_connect(f"/api/screen?ticket={ticket}") as ws:
        ws.receive_bytes()
        assert client.get(f"/api/screen/audio?ticket={ticket}").status_code == 404
        client.post("/api/screen/lease", headers=auth, json={"ticket": ticket, "take": True})
        heard = client.get(f"/api/screen/audio?ticket={ticket}")
        assert heard.headers["content-type"] == "audio/mpeg" and heard.content == sound


def test_a_lease_left_with_a_person_is_handed_back_at_start(tmp_path, dashboard, opened):
    client, _ = app_for(tmp_path, dashboard, opened)
    dashboard.holder = "human"
    client.portal.call(client.kernel.screen.sweep)
    assert dashboard.holder == "agent"
    assert ("display.lease.release", {"profile": "default", "force": True}) in dashboard.leases
