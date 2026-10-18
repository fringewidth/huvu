"""Native push: FCM, so a phone hears what needs its person while the app
is closed, and Web Push (web_push.py) for huvu added to a phone's home
screen, which logs in as a phone too, and for huvu in a browser, which is
sent notifications and nothing that needs an answer.

The push stream (push.py) reaches a phone only while its app holds it open.
A phone also registers its FCM token (`POST /api/push/token`), and the
events that need a person go to that token too: a notification, an
approval waiting for a yes, and an "is it you?" question. They go out
whether or not the app is open, since a phone can't be trusted to say it
is in front: Android keeps a backgrounded WebView's fetch open for a
while, and the stream would look live. The app in front hears them on the
stream, and the OS shows it no notification (Android by default, iOS
through `presentationOptions` in capacitor.config.ts), so nothing shows
twice.

FCM's HTTP v1 API reaches Android, and iOS through APNs once the Firebase
project has the APNs key. It is signed with a service account's JSON key,
whose path is in HUVU_FCM_CREDENTIALS. Without it, native push is off and
tokens are still kept. Web Push needs no setup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx
import jwt

from .db import iso
from .web_push import Gone, WebPushClient

if TYPE_CHECKING:
    from .kernel import Kernel

log = logging.getLogger("huvu.native_push")

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TOKEN_URI = "https://oauth2.googleapis.com/token"
# An access token is good for an hour; a new one is fetched this early.
REFRESH_EARLY = 300.0
# The Android channels the app creates (account/nativePush.ts). What needs
# an answer pops up; everything else waits in the tray.
CHANNEL = "huvu"
URGENT_CHANNEL = "huvu.urgent"


class Unregistered(Exception):
    """FCM no longer knows the token: the app was removed, or its data
    cleared."""


class Sender(Protocol):
    async def send(self, message: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...


class FcmClient:
    """FCM's HTTP v1 API, signed with a service account key."""

    def __init__(self, credentials: Path, http: httpx.AsyncClient | None = None) -> None:
        """`http` replaces the HTTP client, for tests."""
        key = json.loads(credentials.read_text())
        self._email: str = key["client_email"]
        self._private_key: str = key["private_key"]
        self._key_id: str = key["private_key_id"]
        self._token_uri: str = key.get("token_uri", TOKEN_URI)
        self._url = f"https://fcm.googleapis.com/v1/projects/{key['project_id']}/messages:send"
        self._http = http or httpx.AsyncClient(timeout=10)
        self._access: tuple[str, float] | None = None

    async def send(self, message: dict[str, Any]) -> None:
        headers = {"Authorization": f"Bearer {await self._access_token()}"}
        res = await self._http.post(self._url, json={"message": message}, headers=headers)
        if res.status_code == 404 or (res.status_code == 400 and "registration token" in res.text):
            raise Unregistered(res.text)
        res.raise_for_status()

    async def close(self) -> None:
        await self._http.aclose()

    async def _access_token(self) -> str:
        if self._access is None or self._access[1] - REFRESH_EARLY < time.time():
            now = int(time.time())
            claims = {"iss": self._email, "scope": SCOPE, "aud": self._token_uri, "iat": now, "exp": now + 3600}
            assertion = jwt.encode(claims, self._private_key, algorithm="RS256", headers={"kid": self._key_id})
            res = await self._http.post(self._token_uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
            res.raise_for_status()
            body = res.json()
            self._access = (body["access_token"], now + float(body.get("expires_in", 3600)))
        return self._access[0]


@dataclass(frozen=True)
class Notice:
    """What a push event shows on a phone, before FCM or Web Push shapes it."""

    title: str
    body: str
    kind: str
    id: str
    urgent: bool
    image: str | None
    opens: str | None


class NativePush:
    def __init__(self, kernel: Kernel, sender: Sender | None, web: WebPushClient) -> None:
        self._kernel = kernel
        self.sender = sender
        self.web = web
        self._tasks: set[asyncio.Task[None]] = set()

    def register(self, device_id: str, token: str, platform: str, keys: dict[str, str] | None = None) -> None:
        """A phone's current FCM token, or its Web Push subscription: the
        endpoint as the token, and its keys. A device has one; a token
        moves to the device that registered it last."""
        db = self._kernel.db
        db.execute("DELETE FROM push_tokens WHERE device_id = ? OR token = ?", (device_id, token))
        db.execute(
            "INSERT INTO push_tokens (token, device_id, platform, keys, updated_at) VALUES (?, ?, ?, ?, ?)",
            (token, device_id, platform, json.dumps(keys) if keys else None, iso(self._kernel.now())),
        )

    def forward(self, name: str, data: Any, devices: Iterable[str]) -> None:
        """Called by the push hub for everything it sends. Only what needs a
        person goes on."""
        notice = _notice(name, data)
        wanted = list(devices)
        if notice is None or not wanted:
            return
        marks = ", ".join("?" * len(wanted))
        rows = self._kernel.db.execute(f"SELECT token, keys FROM push_tokens WHERE device_id IN ({marks})", wanted).fetchall()
        for row in rows:
            keys = json.loads(row["keys"]) if row["keys"] else None
            if keys is None and self.sender is None:
                continue
            task = asyncio.get_running_loop().create_task(self._send(row["token"], keys, notice), name=f"native_push:{name}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self.sender is not None:
            await self.sender.close()
        await self.web.close()

    async def _send(self, token: str, keys: dict[str, str] | None, notice: Notice) -> None:
        try:
            if keys is None:
                await self.sender.send({**_fcm_message(notice), "token": token})  # type: ignore[union-attr]
            else:
                await self.web.send(token, keys, _web_message(notice), notice.urgent)
        except (Unregistered, Gone):
            log.info("dropping a push token its service no longer knows")
            self._kernel.db.execute("DELETE FROM push_tokens WHERE token = ?", (token,))
        except Exception:
            log.exception("native push failed")


def _notice(name: str, data: dict[str, Any]) -> Notice | None:
    """What a push event shows. None for an event that needs no person:
    turn events, answers, attendance."""
    shown: tuple[str, str] | None = None
    urgent = True
    if name == "notify":
        # A message from someone in the family is titled with their name.
        shown = (data.get("from") or "huvu", data["text"])
        urgent = data.get("urgency") == "high"
    elif name == "approval" and data.get("status") == "pending":
        shown = ("huvu needs a yes", f"{data['label'].rstrip('.')}. Asked by {data['requester']}.")
    elif name == "identify":
        reason = f" {data['reason']}" if data.get("reason") else ""
        shown = ("Is it you?", f"Someone at {data['device']['name']} might be you.{reason}")
    if shown is None:
        return None
    title, body = shown
    image = data.get("image") if name == "notify" else None
    return Notice(
        title=title,
        body=body,
        kind=name,
        id=str(data["id"]),
        urgent=urgent,
        image=image if image and image.startswith("http") else None,
        opens=data.get("opens") or None,
    )


def _fcm_message(notice: Notice) -> dict[str, Any]:
    """The FCM message for a notice, without its token."""
    notification = {"title": notice.title, "body": notice.body, **({"image": notice.image} if notice.image else {})}
    # Strings only: FCM's data is a map of strings.
    extra = {"kind": notice.kind, "id": notice.id, **({"opens": notice.opens} if notice.opens else {})}
    return {
        "notification": notification,
        "data": extra,
        "android": {
            "priority": "high" if notice.urgent else "normal",
            # One notification per approval or question, replaced if sent again.
            "notification": {"channel_id": URGENT_CHANNEL if notice.urgent else CHANNEL, "tag": notice.id},
        },
        "apns": {"headers": {"apns-priority": "10" if notice.urgent else "5", "apns-collapse-id": notice.id}},
    }


def _web_message(notice: Notice) -> dict[str, Any]:
    """What the service worker (frontend/public/sw.js) reads from a push."""
    return {
        "title": notice.title,
        "body": notice.body,
        "kind": notice.kind,
        "id": notice.id,
        **({"image": notice.image} if notice.image else {}),
        **({"opens": notice.opens} if notice.opens else {}),
    }
