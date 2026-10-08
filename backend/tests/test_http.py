"""End to end over real HTTP: the server in a thread, the frontend's chat,
and Hermes's side through the official MCP client."""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass

import httpx
import pytest
import uvicorn

from huvu.config import Config
from huvu.http import create_app
from huvu.kernel import Kernel

from .fakes import FakeHermes, HomeSource
from .fakes import WAKE

HOUSEHOLD_MCP_TOKEN = "household-token"


@dataclass
class Server:
    base: str
    kernel: Kernel
    hermes: FakeHermes
    me_mcp_token: str

    def login(self, kind: str = "web", user: str = "me", password: str = "correct horse") -> dict[str, str]:
        res = httpx.post(f"{self.base}/api/login", json={"username": user, "password": password, "device": {"name": f"{user}'s {kind}", "kind": kind}})
        res.raise_for_status()
        return {"Authorization": f"Bearer {res.json()['token']}"}


@pytest.fixture
def server(tmp_path):
    hermes = FakeHermes()
    config = Config(data_dir=tmp_path, public_url="http://test", mcp_token=HOUSEHOLD_MCP_TOKEN)
    kernel = Kernel(config, hermes=hermes, sources={"home": HomeSource()})
    me_token = kernel.accounts.add_user("me", "Me", "correct horse", "key-me")
    kernel.accounts.add_user("mom", "Mom", "battery staple", "key-mom")
    kernel.accounts.join("home", "me", "member")
    kernel.accounts.join("home", "mom", "member")
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
    yield Server(f"http://127.0.0.1:{port}", kernel, hermes, me_token)
    srv.should_exit = True
    thread.join(5)


def test_requests_need_a_device(server):
    assert httpx.get(f"{server.base}/api/conversation").status_code == 401
    bad = httpx.post(f"{server.base}/api/login", json={"username": "me", "password": "nope", "device": {"name": "x", "kind": "web"}})
    assert bad.status_code == 401
    tv = httpx.post(f"{server.base}/api/login", json={"username": "me", "password": "correct horse", "device": {"name": "x", "kind": "tv"}})
    assert tv.status_code == 401 and "pairs" in tv.json()["detail"]
    me = httpx.get(f"{server.base}/api/me", headers=server.login()).json()
    assert me["user"]["id"] == "me" and me["device"]["kind"] == "web"


def test_chat_streams_events(server):
    with httpx.stream("POST", f"{server.base}/api/chat", json={"ask": "hi"}, headers=server.login()) as res:
        body = "".join(res.iter_text())
    names = [line[7:] for line in body.splitlines() if line.startswith("event: ")]
    assert names == ["turn.started", "answer.delta", "turn.ended"]
    ended = json.loads([line for line in body.splitlines() if line.startswith("data: ")][-1][6:])
    assert ended["ending"] is None and ended["answer"] == "ok"
    assert list(server.hermes.profiles.values()) == ["me"]


def test_each_device_has_its_own_conversation(server):
    laptop, phone = server.login("web"), server.login("phone")
    body = {"id": "t_abc", "ask": "who is out there?", "actions": [{"text": "opened the live feed balcony", "at": "2026-10-03T13:42:00+00:00"}]}
    with httpx.stream("POST", f"{server.base}/api/chat", json=body, headers=laptop) as res:
        "".join(res.iter_text())
    [turn] = httpx.get(f"{server.base}/api/conversation", headers=laptop).json()
    assert turn["id"] == "t_abc" and turn["actions"][0]["text"] == "opened the live feed balcony"
    message = server.hermes.messages[-1][1]
    assert "opened the live feed balcony" in message and message.endswith("who is out there?")
    assert httpx.get(f"{server.base}/api/conversation", headers=phone).json() == []
    assert httpx.post(f"{server.base}/api/turns/t_abc/stop", headers=phone).status_code == 404
    # A turn id is used once, and an unknown turn can't be replaced.
    assert httpx.post(f"{server.base}/api/chat", json={"id": "t_abc", "ask": "again"}, headers=laptop).status_code == 409
    assert httpx.post(f"{server.base}/api/chat", json={"ask": "again", "replaces": "t_nope"}, headers=laptop).status_code == 404


def test_feeds_media_taps_and_approvals(server):
    laptop, phone = server.login("web"), server.login("phone")
    assert httpx.get(f"{server.base}/api/feeds", headers=laptop).json()[0]["id"] == "balcony"
    url = server.kernel.media.url_for("home:snap/1").replace("http://test", server.base)
    assert httpx.get(url).headers["content-type"] == "image/jpeg"
    tapped = httpx.post(f"{server.base}/api/actions/home/announce", json={}, headers=laptop)
    assert tapped.json() == {"done": "announce"}
    assert httpx.get(f"{server.base}/api/approvals", headers=laptop).status_code == 403
    assert httpx.get(f"{server.base}/api/approvals", headers=phone).json() == []


def test_a_household_device_pairs_from_a_phone(server):
    started = httpx.post(f"{server.base}/api/pair", json={"name": "living room", "kind": "tv"}).json()
    poll = f"{server.base}/api/pair/{started['pairing']}"
    assert httpx.get(poll).status_code == 202
    laptop = server.login("web")
    assert httpx.post(f"{server.base}/api/pair/confirm", json={"code": started["code"]}, headers=laptop).status_code == 403
    confirmed = httpx.post(f"{server.base}/api/pair/confirm", json={"code": started["code"].lower()}, headers=server.login("phone"))
    assert confirmed.json()["user"] is None
    paired = httpx.get(poll).json()
    assert paired["status"] == "paired"
    # The token is handed over once.
    assert httpx.get(poll).status_code == 410
    tv = {"Authorization": f"Bearer {paired['token']}"}
    me = httpx.get(f"{server.base}/api/me", headers=tv).json()
    assert me["user"] is None and me["device"]["kind"] == "tv"
    # The TV can't approve anything, and its taps go through the policy.
    assert httpx.get(f"{server.base}/api/approvals", headers=tv).status_code == 403
    held = httpx.post(f"{server.base}/api/actions/home/unlock", json={}, headers=tv).json()
    assert held["status"] == "pending_approval"
    assert [a["user"] for a in httpx.get(f"{server.base}/api/approvals", headers=server.login("phone", "mom", "battery staple")).json()] == [None]


async def test_hermes_side_over_mcp(server):
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient() as anonymous:
        assert (await anonymous.post(f"{server.base}/mcp", json={})).status_code == 401
    headers = {"Authorization": f"Bearer {server.me_mcp_token}"}
    server.kernel.accounts.login("me", "correct horse", "laptop", "web")
    async with httpx2.AsyncClient(headers=headers) as client, streamable_http_client(f"{server.base}/mcp", http_client=client) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert "state_search" in (init.instructions or "")
            tools = {t.name for t in (await session.list_tools()).tools}
            assert {"act", "state_get", "data_sql", "triggers_register", "ui_choices"} <= tools
            assert "query" not in tools
            # Source actions are reached through act.
            assert not any(t.startswith("home_") for t in tools)
            done = await session.call_tool("act", {"name": "home_turn_on", "args": {"entity_id": "light.porch"}})
            assert json.loads(done.content[0].text) == {"done": "turn_on"}
            result = await session.call_tool("state_get", {"values": ["clock.time"], "_session": "hs1"})
            body = json.loads(result.content[0].text)
            assert "just now" in body["clock.time"]["at"]
            outside = await session.call_tool("state_get", {"values": ["home.snapshot"], "_session": "hs1"})
            assert json.loads(outside.content[0].text)["provenance"] == "external"
            # The profile's token says whose trigger this is.
            registered = await session.call_tool("triggers_register", {"note": "Doorbell.", "script": WAKE, "when": [{"event": "home.doorbell"}]})
            assert json.loads(registered.content[0].text)["user"] == "me"
            bad = await session.call_tool("state_gte", {})
            assert bad.is_error and "Did you mean state_get" in bad.content[0].text


def test_the_situation_names_the_profiles_user(server):
    mine = httpx.post(f"{server.base}/api/situation", json={"values": []}, headers={"Authorization": f"Bearer {server.me_mcp_token}"})
    assert "You are helping: Me" in mine.json()["text"]
    household = httpx.post(f"{server.base}/api/situation", json={"values": []}, headers={"Authorization": f"Bearer {HOUSEHOLD_MCP_TOKEN}"})
    assert "Home from a shared device" in household.json()["text"]
    assert httpx.post(f"{server.base}/api/situation", json={"values": []}).status_code == 401


def test_a_shared_device_asks_who_is_there(server):
    started = httpx.post(f"{server.base}/api/pair", json={"name": "living room", "kind": "tv"}).json()
    httpx.post(f"{server.base}/api/pair/confirm", json={"code": started["code"]}, headers=server.login("phone"))
    tv = {"Authorization": f"Bearer {httpx.get(f'{server.base}/api/pair/{started['pairing']}').json()['token']}"}
    assert [p["id"] for p in httpx.get(f"{server.base}/api/people", headers=tv).json()] == ["me", "mom"]
    asked = httpx.post(f"{server.base}/api/attendance", json={"user": "mom"}, headers=tv).json()
    moms_phone = server.login("phone", "mom", "battery staple")
    [question] = httpx.get(f"{server.base}/api/identify", headers=moms_phone).json()
    assert question["id"] == asked["id"] and question["device"]["name"] == "living room"
    answered = httpx.post(f"{server.base}/api/identify/{asked['id']}", json={"yes": True}, headers=moms_phone)
    assert answered.json()["status"] == "confirmed"
    assert httpx.get(f"{server.base}/api/attendance", headers=tv).json()["user"]["name"] == "Mom"
    tv_id = httpx.get(f"{server.base}/api/me", headers=tv).json()["device"]["id"]
    [place] = httpx.get(f"{server.base}/api/attendance/mine", headers=moms_phone).json()
    assert place["device"] == {"id": tv_id, "name": "living room"}
    assert httpx.delete(f"{server.base}/api/attendance/{tv_id}", headers=server.login("phone")).status_code == 403
    assert httpx.delete(f"{server.base}/api/attendance/{tv_id}", headers=tv).json() == {"ok": True}
    assert httpx.get(f"{server.base}/api/attendance", headers=tv).json()["user"] is None


def test_a_turn_replays_from_its_start(server):
    laptop = server.login("web")
    with httpx.stream("POST", f"{server.base}/api/chat", json={"id": "t_replay", "ask": "hi"}, headers=laptop) as res:
        "".join(res.iter_text())
    with httpx.stream("GET", f"{server.base}/api/turns/t_replay/events", headers=laptop) as res:
        body = "".join(res.iter_text())
    names = [line[7:] for line in body.splitlines() if line.startswith("event: ")]
    assert names == ["turn.started", "turn.ended"]
    ended = json.loads([line for line in body.splitlines() if line.startswith("data: ")][-1][6:])
    assert ended["answer"] == "ok" and ended["turn"] == "t_replay"
    assert httpx.get(f"{server.base}/api/turns/t_replay/events", headers=server.login("phone")).status_code == 404


def test_a_turn_is_spoken_as_audio(server, monkeypatch):
    from huvu.speech import Voice

    laptop = server.login("web")
    with httpx.stream("POST", f"{server.base}/api/chat", json={"id": "t_said", "ask": "hi"}, headers=laptop) as res:
        "".join(res.iter_text())
    async def pcm(self, text: str) -> bytes:
        return b"\x01\x00" * len(text)

    monkeypatch.setattr(Voice, "pcm", pcm)
    res = httpx.get(f"{server.base}/api/turns/t_said/speech", headers=laptop)
    assert res.status_code == 200 and res.headers["content-type"].startswith("audio/L16")
    assert res.content == b"\x01\x00" * len("ok")
    assert httpx.get(f"{server.base}/api/turns/t_said/speech", headers=server.login("phone")).status_code == 404


def test_wrong_answers_say_what_went_wrong(server):
    phone = server.login("phone")
    assert httpx.post(f"{server.base}/api/pair/confirm", json={"code": "ZZZZ-ZZZZ"}, headers=phone).status_code == 404
    assert httpx.post(f"{server.base}/api/identify/nope", json={"yes": True}, headers=phone).status_code == 404
    assert httpx.get(f"{server.base}/api/notifications", headers=phone).json() == []


def test_requests_at_once_all_know_their_device(server):
    from concurrent.futures import ThreadPoolExecutor

    phone = server.login("phone")
    paths = ["/api/me", "/api/approvals", "/api/identify", "/api/notifications", "/api/triggers", "/api/conversation"] * 8
    with ThreadPoolExecutor(len(paths)) as pool:
        codes = list(pool.map(lambda path: httpx.get(f"{server.base}{path}", headers=phone).status_code, paths))
    assert codes == [200] * len(paths)


def test_only_a_phone_registers_a_push_token(server):
    body = {"token": "fcm-1", "platform": "android"}
    assert httpx.post(f"{server.base}/api/push/token", json=body, headers=server.login("web")).status_code == 403
    assert httpx.post(f"{server.base}/api/push/token", json=body, headers=server.login("phone")).json() == {"ok": True}
    [row] = server.kernel.db.execute("SELECT token, platform FROM push_tokens").fetchall()
    assert tuple(row) == ("fcm-1", "android")
