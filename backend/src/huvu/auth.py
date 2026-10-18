"""Auth: device tokens for the frontend, and profile tokens for /mcp.

Every frontend request carries its device's bearer token (or `?token=` on
streams, for clients that can't set headers). The token names the device,
and the device names its user, or its group for a shared device. A tracker
(OwnTracks) can't set a bearer header, so the ingest route also takes HTTP
Basic auth, with the username as the user and the token as the password.
A tracker's token works on the ingest route only.

Hermes calls /mcp from one of several profiles: one per user, and one per
group for its shared devices (the default group's is Hermes's default
profile). Each profile sends its own token, so the token says whose Hermes
is calling. That holds even for Hermes's own cron jobs, whose sessions the
backend never sees.

With `auth.mode = "off"`, a request without a valid token comes from the
development device (see accounts.py), and an MCP call without one comes
from the default group. That is only for development on localhost.
"""

from __future__ import annotations

import base64
import binascii
import time
from collections.abc import Callable
from contextvars import ContextVar

from fastapi import HTTPException, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from .accounts import Accounts, Device
from .config import Config

# Whose Hermes profile sent the MCP request being handled: (user, group),
# one of them set. Set by McpAuth for the MCP server's handlers.
mcp_profile: ContextVar[tuple[str | None, str | None]] = ContextVar("mcp_profile", default=(None, None))


def bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else request.query_params.get("token", "")


def client_address(request: Request) -> str:
    """Who sent the request. Behind the tunnel every request comes from
    cloudflared, so this is Cloudflare's CF-Connecting-IP, which a client
    can't set: Cloudflare overwrites it, and the backend's port is open on
    the host only."""
    direct = request.client.host if request.client else ""
    return request.headers.get("cf-connecting-ip") or direct


class LoginLimit:
    """Wrong passwords per address. After FAILURES of them within WINDOW
    seconds, the address can't log in until the oldest one leaves the
    window, so nobody can guess a password fast. Only failures count, and a
    login that works clears its address's."""

    FAILURES = 10
    WINDOW = 15 * 60.0

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._failed: dict[str, list[float]] = {}

    def blocked(self, address: str) -> bool:
        return len(self._recent(address)) >= self.FAILURES

    def failed(self, address: str) -> None:
        self._recent(address).append(self._clock())
        # Addresses that stopped trying would otherwise stay for good.
        if len(self._failed) > 10_000:
            since = self._clock() - self.WINDOW
            self._failed = {a: times for a, times in self._failed.items() if times and times[-1] > since}

    def succeeded(self, address: str) -> None:
        self._failed.pop(address, None)

    def _recent(self, address: str) -> list[float]:
        since = self._clock() - self.WINDOW
        recent = [t for t in self._failed.get(address, []) if t > since]
        self._failed[address] = recent
        return recent


def device_auth(config: Config, accounts: Accounts):
    """A FastAPI dependency that returns the calling device. It is async so
    it runs on the event loop, like every other use of the kernel's one
    SQLite connection: FastAPI runs a plain function in a thread pool, and
    two requests at once would then share the connection across threads."""

    async def check(request: Request) -> Device:
        device = accounts.authenticate(bearer(request))
        if device is None and config.auth_mode == "off":
            device = accounts.dev_device(config.dev_user)
        if device is None:
            raise HTTPException(401, "log in or pair this device")
        if device.is_tracker:
            raise HTTPException(403, "a tracker can only post to /api/ingest")
        return device

    return check


def ingest_auth(config: Config, accounts: Accounts):
    """A FastAPI dependency for a device posting its own data: a tracker or
    a personal device, by bearer token or by Basic auth. With Basic auth the
    username must be the device's user."""

    async def check(request: Request) -> Device:
        username, token = _basic(request)
        device = accounts.authenticate(token or bearer(request))
        if device is None and config.auth_mode == "off":
            device = accounts.dev_device(config.dev_user)
        if device is None or (username is not None and username != device.user):
            raise HTTPException(401, "unknown device; add a tracker with `huvu tracker add`", headers={"WWW-Authenticate": 'Basic realm="huvu"'})
        if device.user is None:
            raise HTTPException(403, "a shared device has nobody's data to post")
        return device

    return check


def _basic(request: Request) -> tuple[str | None, str | None]:
    """(username, password) from a Basic auth header, or (None, None)."""
    header = request.headers.get("authorization", "")
    found: tuple[str | None, str | None] = (None, None)
    if header.startswith("Basic "):
        try:
            username, sep, password = base64.b64decode(header.removeprefix("Basic ").strip()).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            username, sep, password = "", "", ""
        if sep:
            found = (username, password)
    return found


def phone_auth(config: Config, accounts: Accounts):
    """Like device_auth, for what only a phone may do: answer approvals,
    answer "is it you?", pair shared devices."""
    device_check = device_auth(config, accounts)

    async def check(request: Request) -> Device:
        device = await device_check(request)
        if not device.is_phone:
            raise HTTPException(403, "do this from your phone")
        return device

    return check


def personal_auth(config: Config, accounts: Accounts):
    """Like device_auth, for a person's own phone or browser: what shows
    their notifications."""
    device_check = device_auth(config, accounts)

    async def check(request: Request) -> Device:
        device = await device_check(request)
        if not device.is_personal:
            raise HTTPException(403, "do this from your phone or browser")
        return device

    return check


def mcp_caller(config: Config, accounts: Accounts, token: str) -> tuple[bool, str | None, str | None]:
    """Whose Hermes profile sent this token: (allowed, user, group)."""
    known, user, group = accounts.mcp_caller(token) if token else (False, None, None)
    if not known and config.auth_mode == "off":
        known, user, group = True, None, accounts.default_group()
    return known, user, group


class McpAuth:
    """Wraps the MCP app: checks the profile's token and sets `mcp_profile`."""

    def __init__(self, app: ASGIApp, config: Config, accounts: Accounts) -> None:
        self._app = app
        self._config = config
        self._accounts = accounts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        given = headers.get(b"authorization", b"").decode().removeprefix("Bearer ").strip()
        known, user, group = mcp_caller(self._config, self._accounts, given)
        if not known:
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"unauthorized"})
            return
        reset = mcp_profile.set((user, group))
        try:
            await self._app(scope, receive, send)
        finally:
            mcp_profile.reset(reset)
