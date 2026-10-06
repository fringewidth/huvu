"""Native push: FCM, so a phone hears what needs its person while the app
is closed.

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
tokens are still kept.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx
import jwt

from .db import iso

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


class NativePush:
    def __init__(self, kernel: Kernel, sender: Sender | None) -> None:
        self._kernel = kernel
        self.sender = sender
        self._tasks: set[asyncio.Task[None]] = set()

    def register(self, device_id: str, token: str, platform: str) -> None:
        """A phone's current FCM token. A device has one; a token moves to
        the device that registered it last."""
        db = self._kernel.db
        db.execute("DELETE FROM push_tokens WHERE device_id = ? OR token = ?", (device_id, token))
        db.execute(
            "INSERT INTO push_tokens (token, device_id, platform, updated_at) VALUES (?, ?, ?, ?)",
            (token, device_id, platform, iso(self._kernel.now())),
        )

    def forward(self, name: str, data: Any, devices: Iterable[str]) -> None:
        """Called by the push hub for everything it sends. Only what needs a
        person goes on."""
        message = _message(name, data)
        wanted = list(devices)
        if message is None or self.sender is None or not wanted:
            return
        marks = ", ".join("?" * len(wanted))
        rows = self._kernel.db.execute(f"SELECT token FROM push_tokens WHERE device_id IN ({marks})", wanted).fetchall()
        for row in rows:
            task = asyncio.get_running_loop().create_task(self._send(row["token"], message), name=f"native_push:{name}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self.sender is not None:
            await self.sender.close()

    async def _send(self, token: str, message: dict[str, Any]) -> None:
        try:
            await self.sender.send({**message, "token": token})  # type: ignore[union-attr]
        except Unregistered:
            log.info("dropping a push token FCM no longer knows")
            self._kernel.db.execute("DELETE FROM push_tokens WHERE token = ?", (token,))
        except Exception:
            log.exception("native push failed")


def _message(name: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """The FCM message for a push event, without its token. None for an
    event that needs no person: turn events, answers, attendance."""
    shown: tuple[str, str] | None = None
    urgent = True
    if name == "notify":
        shown = ("huvu", data["text"])
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
    notification = {"title": title, "body": body, **({"image": image} if image and image.startswith("http") else {})}
    # Strings only: FCM's data is a map of strings.
    extra = {"kind": name, "id": str(data["id"]), **({"opens": data["opens"]} if data.get("opens") else {})}
    return {
        "notification": notification,
        "data": extra,
        "android": {
            "priority": "high" if urgent else "normal",
            # One notification per approval or question, replaced if sent again.
            "notification": {"channel_id": URGENT_CHANNEL if urgent else CHANNEL, "tag": extra["id"]},
        },
        "apns": {"headers": {"apns-priority": "10" if urgent else "5", "apns-collapse-id": extra["id"]}},
    }
