"""Auth: device tokens for the frontend, and profile tokens for /mcp.

Every frontend request carries its device's bearer token (or `?token=` on
streams, for clients that can't set headers). The token names the device,
and the device names its user, or none for a household device.

Hermes calls /mcp from one of several profiles: one per user, plus the
default profile for the household. Each profile sends its own token, so the
token says whose Hermes is calling. That holds even for Hermes's own cron
jobs, whose sessions the backend never sees.

With `auth.mode = "off"`, a request without a valid token comes from the
development device (see accounts.py), and an MCP call without one comes
from the household. That is only for development on localhost.
"""

from __future__ import annotations

from contextvars import ContextVar

from fastapi import HTTPException, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from .accounts import Accounts, Device
from .config import Config

# The user whose Hermes profile sent the MCP request being handled. None is
# the household. Set by McpAuth for the MCP server's handlers.
mcp_user: ContextVar[str | None] = ContextVar("mcp_user", default=None)


def bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else request.query_params.get("token", "")


def device_auth(config: Config, accounts: Accounts):
    """A FastAPI dependency that returns the calling device."""

    def check(request: Request) -> Device:
        device = accounts.authenticate(bearer(request))
        if device is None and config.auth_mode == "off":
            device = accounts.dev_device(config.dev_user)
        if device is None:
            raise HTTPException(401, "log in or pair this device")
        return device

    return check


def phone_auth(config: Config, accounts: Accounts):
    """Like device_auth, for what only a phone may do: answer approvals,
    claim triggers, pair household devices."""
    device_check = device_auth(config, accounts)

    def check(request: Request) -> Device:
        device = device_check(request)
        if not device.is_phone:
            raise HTTPException(403, "do this from your phone")
        return device

    return check


def mcp_caller(config: Config, accounts: Accounts, token: str) -> tuple[bool, str | None]:
    """Whose Hermes profile sent this token: (allowed, user)."""
    known, user = accounts.mcp_caller(token) if token else (False, None)
    if not known and config.auth_mode == "off":
        known, user = True, None
    return known, user


class McpAuth:
    """Wraps the MCP app: checks the profile's token and sets `mcp_user`."""

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
        known, user = mcp_caller(self._config, self._accounts, given)
        if not known:
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"unauthorized"})
            return
        reset = mcp_user.set(user)
        try:
            await self._app(scope, receive, send)
        finally:
            mcp_user.reset(reset)
