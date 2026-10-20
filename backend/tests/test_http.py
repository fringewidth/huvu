"""End to end over real HTTP: the server in a thread, the frontend's chat,
and Hermes's side through the official MCP client."""

from __future__ import annotations

import io
import json
import socket
import threading
import time
import wave
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
    config = Config(data_dir=tmp_path, hermes_home=tmp_path / "hermes", public_url="http://test", mcp_token=HOUSEHOLD_MCP_TOKEN)
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


def test_wrong_passwords_block_their_address(server):
    def login(password: str, address: str) -> int:
        body = {"username": "me", "password": password, "device": {"name": "x", "kind": "web"}}
        return httpx.post(f"{server.base}/api/login", json=body, headers={"CF-Connecting-IP": address}).status_code

    assert [login("nope", "203.0.113.7") for _ in range(10)] == [401] * 10
    assert login("correct horse", "203.0.113.7") == 429
    assert login("correct horse", "198.51.100.2") == 200


def test_the_login_limit_lets_an_address_in_again():
    from huvu.auth import LoginLimit

    now = [0.0]
    limit = LoginLimit(clock=lambda: now[0])
    for _ in range(LoginLimit.FAILURES):
        limit.failed("a")
    assert limit.blocked("a") and not limit.blocked("b")
    now[0] += LoginLimit.WINDOW + 1
    assert not limit.blocked("a")
    limit.failed("a")
    limit.succeeded("a")
    assert not limit.blocked("a")


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
            assert "home_lights  (now)" in (init.instructions or "") and "home_doorbell  (history)" in (init.instructions or "")
            tools = {t.name for t in (await session.list_tools()).tools}
            assert {"act", "data_sql", "triggers_register", "ui_choices"} <= tools
            assert not {"state_get", "state_search", "events_search", "triggers_test"} & tools
            # Source actions are reached through act.
            assert not any(t.startswith("home_") for t in tools)
            done = await session.call_tool("act", {"name": "home_turn_on", "args": {"entity_id": "light.porch"}})
            assert json.loads(done.content[0].text) == {"done": "turn_on"}
            result = await session.call_tool("data_sql", {"sql": "SELECT state FROM home_lights WHERE entity_id = 'light.porch'", "_session": "hs1"})
            assert json.loads(result.content[0].text)["rows"] == [["off"]]
            await server.kernel.registry.get("home").press()
            outside = await session.call_tool("data_sql", {"sql": "SELECT note FROM home_doorbell", "_session": "hs1"})
            assert json.loads(outside.content[0].text)["provenance"] == "external"
            # The profile's token says whose trigger this is.
            bell = {"note": "Doorbell.", "script": WAKE, "sql": "SELECT id FROM home_doorbell", "fire": "rows", "once": False}
            registered = await session.call_tool("triggers_register", bell)
            assert json.loads(registered.content[0].text)["user"] == "me"
            bad = await session.call_tool("data_sq", {})
            assert bad.is_error and "Did you mean data_sql" in bad.content[0].text


def test_the_situation_names_the_profiles_user(server):
    mine = httpx.post(f"{server.base}/api/situation", json={}, headers={"Authorization": f"Bearer {server.me_mcp_token}"})
    assert "You are helping: Me" in mine.json()["text"] and "Family mode: normal" in mine.json()["text"]
    household = httpx.post(f"{server.base}/api/situation", json={}, headers={"Authorization": f"Bearer {HOUSEHOLD_MCP_TOKEN}"})
    assert "Home from a shared device" in household.json()["text"]
    assert httpx.post(f"{server.base}/api/situation", json={}).status_code == 401
    # Each profile's wiki is in its own folder; the household's in Hermes's.
    hermes_home = server.kernel.config.hermes_home
    assert f"Your wiki: {hermes_home / 'profiles' / 'me' / 'wiki'}" in mine.json()["text"]
    assert f"Your wiki: {hermes_home / 'wiki'}" in household.json()["text"]


def test_a_person_reads_and_edits_their_wiki(server):
    me = server.login("web")
    home = server.kernel.config.hermes_home / "profiles" / "me"
    assert httpx.get(f"{server.base}/api/wiki", headers=me).json() == {"pages": [], "index": []}
    (home / "wiki" / "people").mkdir(parents=True)
    (home / "wiki" / "people" / "mom.md").write_text("# Mom\n\nLikes filter coffee.\n")
    (home / "memories").mkdir()
    (home / "memories" / "MEMORY.md").write_text("people/mom.md: what Mom likes\n§\nhabits.md: daily rhythms")
    listing = httpx.get(f"{server.base}/api/wiki", headers=me).json()
    assert [(p["path"], p["title"]) for p in listing["pages"]] == [("people/mom.md", "Mom")]
    assert listing["index"] == ["people/mom.md: what Mom likes", "habits.md: daily rhythms"]

    page = httpx.get(f"{server.base}/api/wiki/page", params={"path": "people/mom.md"}, headers=me).json()
    assert page["text"].startswith("# Mom")
    saved = httpx.put(f"{server.base}/api/wiki/page", json={"path": "people/mom.md", "text": "# Mom\n\nLikes tea now.\n", "version": page["version"]}, headers=me)
    assert saved.status_code == 200 and saved.json()["version"] != page["version"]
    # The agent changed it meanwhile: the old version can't write over it.
    stale = httpx.put(f"{server.base}/api/wiki/page", json={"path": "people/mom.md", "text": "mine", "version": page["version"]}, headers=me)
    assert stale.status_code == 409
    assert (home / "wiki" / "people" / "mom.md").read_text() == "# Mom\n\nLikes tea now.\n"

    made = httpx.put(f"{server.base}/api/wiki/page", json={"path": "trips.md", "text": "# Trips\n"}, headers=me)
    assert made.status_code == 200
    assert httpx.put(f"{server.base}/api/wiki/page", json={"path": "trips.md", "text": "again"}, headers=me).status_code == 409
    for path in ("../memories/MEMORY.md", "/etc/passwd", "notes.txt", ".hidden.md"):
        assert httpx.put(f"{server.base}/api/wiki/page", json={"path": path, "text": "x"}, headers=me).status_code == 400
    assert httpx.get(f"{server.base}/api/wiki/page", params={"path": "../memories/MEMORY.md"}, headers=me).status_code == 404
    gone = httpx.delete(f"{server.base}/api/wiki/page", params={"path": "trips.md", "version": made.json()["version"]}, headers=me)
    assert gone.json() == {"deleted": True} and not (home / "wiki" / "trips.md").exists()

    # Mom's wiki is hers.
    assert httpx.get(f"{server.base}/api/wiki", headers=server.login("web", "mom", "battery staple")).json()["pages"] == []


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
    async def clip(self, text: str) -> bytes:
        return b"mp3:" + text.encode()

    monkeypatch.setattr(Voice, "clip", clip)
    res = httpx.get(f"{server.base}/api/turns/t_said/speech", headers=laptop)
    assert res.status_code == 200 and res.headers["content-type"] == "application/vnd.huvu.speech"
    assert res.content == (6).to_bytes(4, "big") + b"mp3:ok"
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


def test_a_home_screen_web_app_subscribes_to_web_push(server):
    phone = server.login("phone")
    key = httpx.get(f"{server.base}/api/push/web", headers=phone).json()["key"]
    assert key == server.kernel.native_push.web.public_key
    body = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "pk", "auth": "au"}}
    assert httpx.post(f"{server.base}/api/push/web", json=body, headers=phone).json() == {"ok": True}
    [row] = server.kernel.db.execute("SELECT token, platform, keys FROM push_tokens").fetchall()
    assert tuple(row) == ("https://web.push.apple.com/abc", "web", '{"p256dh": "pk", "auth": "au"}')


def test_a_browser_subscribes_to_web_push_and_a_tv_does_not(server):
    browser = server.login("web")
    assert httpx.get(f"{server.base}/api/push/web", headers=browser).status_code == 200
    body = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "pk", "auth": "au"}}
    assert httpx.post(f"{server.base}/api/push/web", json=body, headers=browser).json() == {"ok": True}
    assert httpx.get(f"{server.base}/api/notifications", headers=browser).json() == []
    started = httpx.post(f"{server.base}/api/pair", json={"name": "living room", "kind": "tv"}).json()
    httpx.post(f"{server.base}/api/pair/confirm", json={"code": started["code"]}, headers=server.login("phone"))
    tv = {"Authorization": f"Bearer {httpx.get(f'{server.base}/api/pair/{started['pairing']}').json()['token']}"}
    assert httpx.post(f"{server.base}/api/push/web", json=body, headers=tv).status_code == 403


def test_the_web_client_is_served_beside_the_api(tmp_path):
    from fastapi.testclient import TestClient

    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<title>huvu</title>")
    config = Config(data_dir=tmp_path / "data", public_url="http://test", web_dir=web)
    client = TestClient(create_app(config, Kernel(config, hermes=FakeHermes())))
    assert client.get("/").text == "<title>huvu</title>"
    assert client.get("/health").json()["ok"] is True
    assert client.get("/api/me").status_code == 401


def test_native_apps_get_their_latest_build(tmp_path):
    import io
    import zipfile

    from fastapi.testclient import TestClient

    phone = tmp_path / "bundles" / "phone"
    (phone / "assets").mkdir(parents=True)
    (phone / "index.html").write_text("<title>huvu</title>")
    (phone / "assets" / "app.js").write_text("boot()")
    (phone / "bundle.json").write_text('{"version": "abc123", "native": 2}')
    config = Config(data_dir=tmp_path / "data", public_url="http://test", bundles_dir=tmp_path / "bundles")
    client = TestClient(create_app(config, Kernel(config, hermes=FakeHermes())))

    latest = client.get("/api/app/phone")
    assert latest.json() == {"version": "abc123", "native": 2}
    assert latest.headers["cache-control"] == "no-store"
    built = client.get("/api/app/phone/abc123.zip")
    assert built.status_code == 200 and "immutable" in built.headers["cache-control"]
    with zipfile.ZipFile(io.BytesIO(built.content)) as archive:
        assert sorted(archive.namelist()) == ["assets/app.js", "bundle.json", "index.html"]
        assert archive.read("index.html") == b"<title>huvu</title>"
    assert client.get("/api/app/phone/old.zip").status_code == 404
    assert client.get("/api/app/firetv").status_code == 404
    assert client.get("/api/app/..").status_code == 404


def test_no_builds_without_a_bundles_dir(tmp_path):
    from fastapi.testclient import TestClient

    config = Config(data_dir=tmp_path / "data", public_url="http://test")
    client = TestClient(create_app(config, Kernel(config, hermes=FakeHermes())))
    assert client.get("/api/app/phone").status_code == 404


def _wav(seconds: float, rate: int = 16000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        clip.writeframes(b"\0\0" * int(seconds * rate))
    return out.getvalue()


def test_a_person_records_clips_for_the_wake_word(server):
    me = server.login("phone")
    saved = httpx.post(f"{server.base}/api/voice-clips/wake/near", content=_wav(1.2), headers=me)
    assert saved.status_code == 200 and saved.json()["id"].startswith("phone-")
    httpx.post(f"{server.base}/api/voice-clips/wake/near", content=_wav(1.0), headers=me)
    other = httpx.post(f"{server.base}/api/voice-clips/other/talk", content=_wav(15), headers=me).json()["id"]
    assert httpx.get(f"{server.base}/api/voice-clips", headers=me).json() == {"steps": {"near": 2, "talk": 1}}
    root = server.kernel.config.data_dir / "voice_clips" / "me"
    assert (root / "other" / "talk" / f"{other}.wav").read_bytes() == _wav(15)

    # "That wasn't it" drops the last one.
    assert httpx.delete(f"{server.base}/api/voice-clips/{other}", headers=me).json() == {"deleted": True}
    assert httpx.get(f"{server.base}/api/voice-clips", headers=me).json() == {"steps": {"near": 2}}
    assert httpx.delete(f"{server.base}/api/voice-clips/{other}", headers=me).status_code == 404
    assert httpx.delete(f"{server.base}/api/voice-clips/..", headers=me).status_code == 404

    # Only what the phone hears: 16 kHz mono, a short clip, a known kind.
    for path, body in (("wake/near", _wav(1, 44100)), ("wake/near", _wav(31)), ("wake/near", b"RIFF"), ("song/near", _wav(1)), ("wake/Near!", _wav(1))):
        assert httpx.post(f"{server.base}/api/voice-clips/{path}", content=body, headers=me).status_code in (400, 404)

    # Mom's clips are hers, and a shared device records none.
    assert httpx.get(f"{server.base}/api/voice-clips", headers=server.login("web", "mom", "battery staple")).json() == {"steps": {}}
    started = httpx.post(f"{server.base}/api/pair", json={"name": "living room", "kind": "tv"}).json()
    httpx.post(f"{server.base}/api/pair/confirm", json={"code": started["code"]}, headers=me)
    tv = {"Authorization": f"Bearer {httpx.get(f'{server.base}/api/pair/{started['pairing']}').json()['token']}"}
    assert httpx.post(f"{server.base}/api/voice-clips/wake/near", content=_wav(1), headers=tv).status_code == 403
