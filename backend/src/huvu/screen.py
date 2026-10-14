"""The browser's screen, watched from the app.

Hermes's browser draws on its profile's own screen (its Bot Desktop, an
Xvnc per profile), which its first browser call starts. Hermes's dashboard
serves that screen as raw RFB over a WebSocket, behind a ticket that its
JSON-RPC method `display.observe` mints. The dashboard listens inside the
container only, and the backend knows its session token (50-huvu makes it),
so the backend is the only client it has.

The app asks for a ticket of huvu's own (`POST /api/screen/ticket`, its
user's personal device only), opens `/api/screen?ticket=...` with it, and
the backend relays bytes both ways between that socket and the dashboard's
display socket, for the user's own profile. noVNC in the app does the RFB
handshake, so the relay never reads what passes. The app only watches:
Hermes drops input from a viewer that hasn't taken the screen's lease, and
the backend never takes it. Watching never starts a screen; one that isn't
running is closed with SCREEN_OFF.

Each viewer gets its own display socket, since RFB keeps state per
connection. A ticket is single use and lasts TICKET_TTL, so a link in a
log is spent by the time anyone reads it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote

import websockets
from fastapi import WebSocket, WebSocketDisconnect

if TYPE_CHECKING:
    from .kernel import Kernel

log = logging.getLogger("huvu.screen")

TICKET_TTL = timedelta(seconds=30)
# Close codes the app reads.
SCREEN_OFF = 4001
BAD_TICKET = 4401
NO_SCREEN = 4503
# The dashboard's JSON-RPC error when the profile's screen isn't running.
NOT_RUNNING = 5300


class ScreenError(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Ticket:
    user: str
    expires: datetime


class Screen:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        self._tickets: dict[str, Ticket] = {}

    @property
    def available(self) -> bool:
        return self._kernel.config.dashboard_token is not None

    def ticket(self, user: str) -> str:
        now = self._kernel.now()
        self._tickets = {k: t for k, t in self._tickets.items() if t.expires > now}
        ticket = secrets.token_urlsafe(24)
        self._tickets[ticket] = Ticket(user, now + TICKET_TTL)
        return ticket

    def _redeem(self, ticket: str) -> str | None:
        found = self._tickets.pop(ticket, None)
        return found.user if found and found.expires > self._kernel.now() else None

    async def watch(self, client: WebSocket, ticket: str) -> None:
        """Relays the screen of the ticket's user to `client` until either
        side closes. Refusals close `client` with a code the app reads."""
        await client.accept()
        user = self._redeem(ticket)
        try:
            if user is None:
                raise ScreenError(BAD_TICKET, "ticket missing, expired or used")
            if not self.available:
                raise ScreenError(NO_SCREEN, "huvu has no screen to show: Hermes's dashboard isn't set up")
            path = await self._observe(self._kernel.accounts.profile(user).name or "default")
            async with websockets.connect(self._ws_url(path), max_size=None, open_timeout=10) as display:
                await _relay(client, display)
        except ScreenError as err:
            await _close(client, err.code, err.reason)
        except (OSError, websockets.WebSocketException) as err:
            log.warning("screen for %s: %s", user, err)
            await _close(client, NO_SCREEN, "huvu's screen can't be reached")

    async def _observe(self, profile: str) -> str:
        """A display ticket for `profile`'s screen, from the dashboard's
        JSON-RPC, as the path to open."""
        url = self._ws_url(f"/api/ws?token={quote(self._kernel.config.dashboard_token or '')}")
        request = {"jsonrpc": "2.0", "id": 1, "method": "display.observe", "params": {"profile": profile}}
        async with websockets.connect(url, open_timeout=10) as rpc:
            await rpc.send(json.dumps(request))
            async with asyncio.timeout(15):
                reply = await _reply(rpc, 1)
        error = reply.get("error")
        if error is not None:
            raise ScreenError(SCREEN_OFF if error.get("code") == NOT_RUNNING else NO_SCREEN, str(error.get("message") or "no screen"))
        result = reply.get("result") or {}
        return f"{result.get('path') or '/api/display/ws'}?display_ticket={quote(str(result['ticket']))}"

    def _ws_url(self, path: str) -> str:
        base = self._kernel.config.dashboard_url.rstrip("/")
        return f"ws{base.removeprefix('http')}{path}"


async def _reply(rpc, request_id: int) -> dict:
    """The answer to one JSON-RPC request, past the events pushed before it."""
    message: dict = {}
    while message.get("id") != request_id:
        message = json.loads(await rpc.recv())
    return message


async def _relay(client: WebSocket, display) -> None:
    async def up() -> None:
        while True:
            await display.send(await client.receive_bytes())

    async def down() -> None:
        async for frame in display:
            if isinstance(frame, bytes):
                await client.send_bytes(frame)

    tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        err = task.exception()
        if err is not None and not isinstance(err, (WebSocketDisconnect, websockets.ConnectionClosed)):
            log.warning("screen relay ended: %s", err)
    await _close(client, 1000, "")


async def _close(client: WebSocket, code: int, reason: str) -> None:
    with contextlib.suppress(Exception):
        await client.close(code=code, reason=reason)
