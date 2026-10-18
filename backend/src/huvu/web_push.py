"""Web Push: what native_push.py does for the app, for huvu added to a
phone's home screen, which has no FCM, and for huvu in a browser, which
gets notifications only.

The browser subscribes with huvu's VAPID public key and hands the backend
its subscription: an endpoint at the browser's push service (Apple's for
Safari) and two keys. Each message is encrypted to those keys (RFC 8291,
aes128gcm) and posted to the endpoint, signed with a short JWT (RFC 8292).
The service worker (frontend/public/sw.js) shows it.

The VAPID key is made on first start and kept in the data directory. A new
key makes every subscription useless, so the file must stay with the
database.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import http_ece
import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

# How long a push service keeps a message for a phone that is off, in seconds.
TTL = 86400
# A VAPID JWT may last up to a day; each is used for this long.
JWT_LIFETIME = 12 * 3600
# Fallback subject when huvu has no https address, since Apple takes only
# a mailto: or an https: subject.
FALLBACK_SUBJECT = "mailto:push@huvu.invalid"


class Gone(Exception):
    """The push service no longer knows the subscription: the person turned
    notifications off, or removed huvu from the home screen."""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_key(path: Path) -> ec.EllipticCurvePrivateKey:
    """The VAPID key at `path`, made there if it isn't yet."""
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError(f"{path} isn't a P-256 key")
        return key
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return key


class WebPushClient:
    def __init__(self, key: ec.EllipticCurvePrivateKey, subject: str, http: httpx.AsyncClient | None = None) -> None:
        """`http` replaces the HTTP client, for tests."""
        self._key = key
        self._subject = subject if subject.startswith("https://") else FALLBACK_SUBJECT
        point = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        # What the browser subscribes with (applicationServerKey).
        self.public_key = b64url(point)
        self._http = http or httpx.AsyncClient(timeout=10)
        self._jwts: dict[str, tuple[str, float]] = {}

    async def send(self, endpoint: str, keys: dict[str, str], payload: dict[str, Any], urgent: bool) -> None:
        body = http_ece.encrypt(
            json.dumps(payload).encode(),
            private_key=ec.generate_private_key(ec.SECP256R1()),
            dh=unb64url(keys["p256dh"]),
            auth_secret=unb64url(keys["auth"]),
            version="aes128gcm",
        )
        headers = {
            "Authorization": f"vapid t={self._jwt(endpoint)}, k={self.public_key}",
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(TTL),
            "Urgency": "high" if urgent else "normal",
        }
        res = await self._http.post(endpoint, content=body, headers=headers)
        if res.status_code in (404, 410):
            raise Gone(res.text)
        res.raise_for_status()

    async def close(self) -> None:
        await self._http.aclose()

    def _jwt(self, endpoint: str) -> str:
        """A VAPID JWT for the endpoint's push service, reused while it
        lasts."""
        parts = urlsplit(endpoint)
        audience = f"{parts.scheme}://{parts.netloc}"
        cached = self._jwts.get(audience)
        now = time.time()
        if cached is None or cached[1] - 600 < now:
            expires = int(now) + JWT_LIFETIME
            token = jwt.encode({"aud": audience, "exp": expires, "sub": self._subject}, self._key, algorithm="ES256")
            cached = (token, expires)
            self._jwts[audience] = cached
        return cached[0]
