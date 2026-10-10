"""Blue Star's cloud API, as the Blue Star Smart AC app uses it.

Nothing here is documented by Blue Star. It was read from
github.com/djsmanchanda/Blue_Star_Smart_AC_control and checked against a
live account on 2026-10-10.

Login takes the app's phone number (or email) and password, and returns a
session for the REST API and AWS IoT keys for MQTT. `GET /things` lists the
account's ACs, each with its model config and its last reported state.
Commands and state go over MQTT on AWS IoT (see iot.py).

This module imports nothing from Home Assistant, so it runs on its own too.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import urllib.parse
from dataclasses import dataclass
from typing import Any

import aiohttp

from .const import API_BASE, APP_HEADERS, REGION

TIMEOUT = aiohttp.ClientTimeout(total=15)


class BlueStarError(Exception):
    """Blue Star's cloud didn't answer, or answered with an error."""


class InvalidAuth(BlueStarError):
    """Blue Star refused the phone number and password."""


@dataclass(frozen=True)
class Login:
    user_id: str
    name: str
    session: str
    endpoint: str
    access_key_id: str
    secret_access_key: str


async def login(http: aiohttp.ClientSession, auth_id: str, password: str) -> Login:
    """Log in as the app does. A 10-digit number is a phone number."""
    auth_type = 1
    if len(auth_id) != 10 or not auth_id.isdigit():
        auth_type = 0
    body = {
        "auth_id": auth_id,
        "auth_type": auth_type,
        "password": password,
    }
    try:
        async with http.post(f"{API_BASE}/auth/login", json=body, headers=APP_HEADERS, timeout=TIMEOUT) as response:
            payload = await response.json(content_type=None)
            status = response.status
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        raise BlueStarError(f"login failed: {error}") from error
    if 400 <= status < 500:
        raise InvalidAuth(f"login refused with HTTP {status}")
    if status != 200 or not isinstance(payload, dict) or "mi" not in payload:
        raise BlueStarError(f"login failed with HTTP {status}")
    parts = base64.b64decode(payload["mi"]).decode().split("::")
    if len(parts) < 3:
        raise BlueStarError("login returned MQTT keys in an unknown format")
    user = payload.get("user") or {}
    return Login(
        user_id=str(user.get("id", "")),
        name=str(user.get("name", "")),
        session=str(payload.get("session", "")),
        endpoint=parts[0],
        access_key_id=parts[1],
        secret_access_key=parts[2],
    )


async def things(http: aiohttp.ClientSession, session: str) -> dict[str, Any]:
    """The account's ACs: `{"things": [...], "states": {thing_id: {...}}}`."""
    headers = {
        **APP_HEADERS,
        "X-APP-SESSION": session,
    }
    try:
        async with http.get(f"{API_BASE}/things", headers=headers, timeout=TIMEOUT) as response:
            payload = await response.json(content_type=None)
            status = response.status
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        raise BlueStarError(f"listing devices failed: {error}") from error
    if status != 200 or not isinstance(payload, dict):
        raise BlueStarError(f"listing devices failed with HTTP {status}")
    return payload


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def mqtt_url(credentials: Login) -> str:
    """A SigV4-presigned WebSocket URL for AWS IoT's MQTT endpoint."""
    service = "iotdevicegateway"
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    scope = f"{date_stamp}/{REGION}/{service}/aws4_request"
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{credentials.access_key_id}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": "86400",
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}" for k, v in sorted(query.items())
    )
    canonical_request = "\n".join([
        "GET",
        "/mqtt",
        canonical_query,
        f"host:{credentials.endpoint}\n",
        "host",
        hashlib.sha256(b"").hexdigest(),
    ])
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    key = _sign(("AWS4" + credentials.secret_access_key).encode(), date_stamp)
    key = _sign(key, REGION)
    key = _sign(key, service)
    key = _sign(key, "aws4_request")
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return f"wss://{credentials.endpoint}/mqtt?{canonical_query}&X-Amz-Signature={signature}"
