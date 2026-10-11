from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import http_ece
import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from huvu.native_push import FcmClient, Unregistered
from huvu.spec import Caller
from huvu.web_push import WebPushClient, b64url, load_key


class FakeFcm:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.unknown: set[str] = set()

    async def send(self, message: dict[str, Any]) -> None:
        if message["token"] in self.unknown:
            raise Unregistered("UNREGISTERED")
        self.sent.append(message)

    async def close(self) -> None:
        pass


def fcm(kernel) -> FakeFcm:
    fake = FakeFcm()
    kernel.native_push.sender = fake
    return fake


async def settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_a_notification_reaches_the_phones_token(kernel, family):
    fake = fcm(kernel)
    kernel.native_push.register(family.me_phone.id, "tok-me", "android")
    kernel.native_push.register(family.mom_phone.id, "tok-mom", "ios")
    await kernel.notify("me", None, "The porch light is on.", opens="triggers")
    await settle()
    [message] = fake.sent
    assert message["token"] == "tok-me"
    assert message["notification"]["body"] == "The porch light is on."
    assert message["data"]["kind"] == "notify" and message["data"]["opens"] == "triggers"
    # A normal notice waits in the tray.
    assert message["android"]["notification"]["channel_id"] == "huvu"


async def test_a_message_is_titled_with_its_sender(kernel, family):
    fake = fcm(kernel)
    kernel.native_push.register(family.mom_phone.id, "tok-mom", "android")
    kernel.accounts.set_alias("mom", "me", "kanna")
    await kernel.notify("mom", None, "running late", sender="me")
    await settle()
    [message] = fake.sent
    assert message["notification"]["title"] == "kanna"


async def test_an_approval_goes_out_once_while_it_waits(kernel, family):
    fake = fcm(kernel)
    kernel.native_push.register(family.me_phone.id, "tok-me", "android")
    held = await kernel.policy.request("home", "unlock", {}, Caller(kind="hermes", user="me", device=family.me_web.id))
    await settle()
    [message] = fake.sent
    assert message["notification"]["body"] == "Unlocks the front door. Asked by huvu, talking with Me on laptop."
    assert message["data"] == {"kind": "approval", "id": held.data["approval"]}
    assert message["android"]["notification"]["channel_id"] == "huvu.urgent"
    await kernel.policy.resolve(held.data["approval"], False, family.me_phone)
    await settle()
    assert len(fake.sent) == 1


async def test_a_question_goes_to_the_person_asked(kernel, family):
    fake = fcm(kernel)
    kernel.native_push.register(family.mom_phone.id, "tok-mom", "ios")
    await kernel.attendance.ask(family.tv.id, "mom", "device")
    await settle()
    [message] = fake.sent
    assert message["notification"]["title"] == "Is it you?" and "living room" in message["notification"]["body"]


async def test_tokens_fcm_forgot_and_logged_out_devices_are_dropped(kernel, family):
    fake = fcm(kernel)
    kernel.native_push.register(family.me_phone.id, "tok-old", "android")
    kernel.native_push.register(family.me_phone.id, "tok-me", "android")
    kernel.native_push.register(family.mom_phone.id, "tok-mom", "android")
    fake.unknown.add("tok-me")
    await kernel.notify(None, "home", "Everyone: the door is open.")
    await settle()
    assert [m["token"] for m in fake.sent] == ["tok-mom"]
    kernel.accounts.revoke(family.mom_phone.id)
    tokens = [r["token"] for r in kernel.db.execute("SELECT token FROM push_tokens")]
    assert tokens == []


async def test_the_fcm_client_signs_and_reuses_its_access_token(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    credentials = tmp_path / "sa.json"
    credentials.write_text(json.dumps({"client_email": "huvu@p.iam.gserviceaccount.com", "private_key": pem, "private_key_id": "k1", "project_id": "p"}))
    grants: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            assertion = dict(httpx.QueryParams(request.content.decode()))["assertion"]
            grants.append(jwt.decode(assertion, key.public_key(), algorithms=["RS256"], audience="https://oauth2.googleapis.com/token"))
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        assert request.url.path == "/v1/projects/p/messages:send" and request.headers["Authorization"] == "Bearer at-1"
        body = json.loads(request.content)
        sends.append(body)
        status = 404 if body["message"]["token"] == "gone" else 200
        return httpx.Response(status, json={})

    client = FcmClient(credentials, httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    await client.send({"token": "t1"})
    await client.send({"token": "t2"})
    try:
        await client.send({"token": "gone"})
        raise AssertionError("an unknown token should raise")
    except Unregistered:
        pass
    await client.close()
    assert len(grants) == 1 and grants[0]["scope"] == "https://www.googleapis.com/auth/firebase.messaging"
    assert [s["message"]["token"] for s in sends] == ["t1", "t2", "gone"]


class Browser:
    """A browser's push subscription, and the push service it lives at."""

    def __init__(self, endpoint: str = "https://web.push.apple.com/sub-1") -> None:
        self.endpoint = endpoint
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        point = self.key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.keys = {"p256dh": b64url(point), "auth": b64url(self.auth)}
        self.received: list[tuple[httpx.Request, dict[str, Any]]] = []
        self.gone = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.gone:
            return httpx.Response(410)
        body = http_ece.decrypt(request.content, private_key=self.key, auth_secret=self.auth, version="aes128gcm")
        self.received.append((request, json.loads(body)))
        return httpx.Response(201)


def web(kernel, browser: Browser) -> WebPushClient:
    client = WebPushClient(kernel.native_push.web._key, "https://huvu.example", httpx.AsyncClient(transport=httpx.MockTransport(browser.handle)))
    kernel.native_push.web = client
    return client


async def test_a_notification_reaches_a_web_push_subscription_encrypted_and_signed(kernel, family):
    browser = Browser()
    client = web(kernel, browser)
    kernel.native_push.register(family.mom_phone.id, browser.endpoint, "web", browser.keys)
    kernel.accounts.set_alias("mom", "me", "kanna")
    await kernel.notify("mom", None, "running late", sender="me", opens="triggers")
    await settle()
    [(request, payload)] = browser.received
    assert payload["title"] == "kanna" and payload["body"] == "running late"
    assert payload["kind"] == "notify" and payload["opens"] == "triggers"
    assert request.headers["Content-Encoding"] == "aes128gcm" and request.headers["Urgency"] == "normal"
    scheme, params = request.headers["Authorization"].split(" ", 1)
    fields = dict(part.split("=", 1) for part in params.split(", "))
    assert scheme == "vapid" and fields["k"] == client.public_key
    claims = jwt.decode(fields["t"], kernel.native_push.web._key.public_key(), algorithms=["ES256"], audience="https://web.push.apple.com")
    assert claims["sub"] == "https://huvu.example"


async def test_web_push_works_without_fcm_and_drops_a_gone_subscription(kernel, family):
    assert kernel.native_push.sender is None
    browser = Browser()
    web(kernel, browser)
    kernel.native_push.register(family.mom_phone.id, browser.endpoint, "web", browser.keys)
    await kernel.attendance.ask(family.tv.id, "mom", "device")
    await settle()
    [(request, payload)] = browser.received
    assert payload["title"] == "Is it you?" and request.headers["Urgency"] == "high"
    browser.gone = True
    await kernel.notify("mom", None, "hello")
    await settle()
    assert kernel.db.execute("SELECT token FROM push_tokens").fetchall() == []


def test_the_vapid_key_is_made_once_and_kept(tmp_path):
    path = tmp_path / "vapid.pem"
    first = load_key(path)
    again = load_key(path)
    assert first.private_numbers() == again.private_numbers()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
