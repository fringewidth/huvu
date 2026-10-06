from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from huvu.native_push import FcmClient, Unregistered
from huvu.spec import Caller


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
