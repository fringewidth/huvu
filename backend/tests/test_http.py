"""End to end over real HTTP: the server in a thread, the frontend's chat,
and Hermes's side through the official MCP client."""

from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from huvu.config import Config
from huvu.http import create_app
from huvu.kernel import Kernel

from .fakes import FakeHermes, HomeSource


@pytest.fixture
def server(tmp_path):
    hermes = FakeHermes()
    kernel = Kernel(Config(data_dir=tmp_path, public_url="http://test"), hermes=hermes, sources={"home": HomeSource()})
    app = create_app(kernel.config, kernel)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", kernel, hermes
    srv.should_exit = True
    thread.join(5)


def test_chat_streams_events(server):
    base, kernel, hermes = server
    with httpx.stream("POST", f"{base}/api/chat", json={"conversation": "c1", "ask": "hi"}) as res:
        body = "".join(res.iter_text())
    names = [line[7:] for line in body.splitlines() if line.startswith("event: ")]
    assert names == ["turn.started", "answer.delta", "turn.ended"]
    ended = json.loads([line for line in body.splitlines() if line.startswith("data: ")][-1][6:])
    assert ended["ending"] is None and ended["answer"] == "ok"


def test_chat_keeps_the_clients_turn_id_and_tells_the_model_about_actions(server):
    base, kernel, hermes = server
    body = {"id": "t_abc", "ask": "who is out there?", "actions": [{"text": "opened the live feed balcony", "at": "2026-10-03T13:42:00+00:00"}]}
    with httpx.stream("POST", f"{base}/api/chat", json=body) as res:
        "".join(res.iter_text())
    [turn] = httpx.get(f"{base}/api/conversations/default").json()
    assert turn["id"] == "t_abc" and turn["actions"][0]["text"] == "opened the live feed balcony"
    assert "opened the live feed balcony" in hermes.messages[-1][1] and hermes.messages[-1][1].endswith("who is out there?")
    # A turn id is used once, and an unknown turn can't be replaced.
    assert httpx.post(f"{base}/api/chat", json={"id": "t_abc", "ask": "again"}).status_code == 409
    assert httpx.post(f"{base}/api/chat", json={"ask": "again", "replaces": "t_nope"}).status_code == 404


def test_feeds_media_and_approvals(server):
    base, kernel, _ = server
    assert httpx.get(f"{base}/api/feeds").json()[0]["id"] == "balcony"
    url = kernel.media.url_for("home:snap/1").replace("http://test", base)
    image = httpx.get(url)
    assert image.headers["content-type"] == "image/jpeg"
    held = httpx.post(f"{base}/api/actions/home/announce", json={})
    assert held.json() == {"done": "announce"}
    assert httpx.get(f"{base}/api/approvals").json() == []


async def test_hermes_side_over_mcp(server):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    base, _, _ = server
    async with streamable_http_client(f"{base}/mcp") as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert "state_search" in (init.instructions or "")
            tools = {t.name for t in (await session.list_tools()).tools}
            assert {"state_get", "triggers_register", "home_turn_on", "ui_choices"} <= tools
            result = await session.call_tool("state_get", {"values": ["clock.time"], "_session": "hs1"})
            body = json.loads(result.content[0].text)
            assert "just now" in body["clock.time"]["at"]
            outside = await session.call_tool("home_snapshot", {"_session": "hs1"})
            assert json.loads(outside.content[0].text)["provenance"] == "external"
            bad = await session.call_tool("state_gte", {})
            assert bad.is_error and "Did you mean state_get" in bad.content[0].text
