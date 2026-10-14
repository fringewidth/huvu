"""The browser's screen, watched and taken over from the app.

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
handshake, so the relay never reads what passes. Watching never starts a
screen; one that isn't running is closed with SCREEN_OFF.

The person can take the screen over from a view they have open, and hand it
back (`POST /api/screen/lease`, naming the view by its ticket). Hermes's
lease decides who drives: it drops input from a viewer that doesn't hold
it, and while a person holds it the agent's browser tools refuse to act.
Hermes honors a viewer id only on the JSON-RPC connection that minted it,
so each view keeps its connection open while it lasts. Closing the view
hands the screen back, and the backend hands back at start (`sweep`) any
lease a crash left with a person, so the agent is never locked out.

While its person holds the screen, a view can be heard
(`GET /api/screen/audio`): each profile's Chromium plays into its own null
sink on huvu's PulseAudio (docker/s6/pulse, PULSE_SINK from setup.sh), and
ffmpeg encodes that sink's monitor as an MP3 stream.

Each viewer gets its own display socket, since RFB keeps state per
connection. A ticket opens one view and lasts TICKET_TTL, so a link in a
log is spent by the time anyone reads it. While its view is open, the
ticket names it for its lease and its sound; an audio element can't send
the device's token, so the ticket stands in for it there.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
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
RPC_TIMEOUT = 15
# How long the start-up sweep waits for the dashboard, which can come up
# after the backend.
SWEEP_TRIES = 30
SWEEP_EVERY = 10
AUDIO_CHUNK = 4096
# How long a request for the sound waits for its view to take the screen:
# the app asks for the sound inside the tap that takes over, before the
# lease call returns, since a phone only lets audio start inside a tap.
HOLD_WAIT = timedelta(seconds=10)


class ScreenError(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Ticket:
    user: str
    expires: datetime


@dataclass
class View:
    """An open view: its person, their profile, the dashboard connection that
    minted its viewer id, and whether its person holds the screen."""

    user: str
    profile: str
    rpc: Rpc
    viewer: str
    holding: bool = False


class Rpc:
    """One JSON-RPC connection to the dashboard. A reader drains it the whole
    time, since the dashboard pushes events nobody here asks for, and hands
    each reply to the call waiting for it."""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._next = 0
        self._waiting: dict[int, asyncio.Future[dict]] = {}
        self._reader = asyncio.create_task(self._read(), name="screen:rpc")

    @classmethod
    async def connect(cls, url: str) -> Rpc:
        return cls(await websockets.connect(url, open_timeout=10, max_size=None))

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """The result of `method`, or a ScreenError with the dashboard's own."""
        self._next += 1
        reply = asyncio.get_running_loop().create_future()
        self._waiting[self._next] = reply
        await self._ws.send(json.dumps({"jsonrpc": "2.0", "id": self._next, "method": method, "params": params}))
        async with asyncio.timeout(RPC_TIMEOUT):
            message = await reply
        error = message.get("error")
        if error is not None:
            raise ScreenError(SCREEN_OFF if error.get("code") == NOT_RUNNING else NO_SCREEN, str(error.get("message") or method))
        return message.get("result") or {}

    async def _read(self) -> None:
        with contextlib.suppress(websockets.ConnectionClosed):
            async for raw in self._ws:
                message = json.loads(raw)
                waiting = self._waiting.pop(message.get("id"), None) if isinstance(message.get("id"), int) else None
                if waiting is not None and not waiting.done():
                    waiting.set_result(message)
        for waiting in self._waiting.values():
            if not waiting.done():
                waiting.set_exception(ScreenError(NO_SCREEN, "the dashboard closed the connection"))

    async def close(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(Exception):
            await self._ws.close()


class Screen:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        self._tickets: dict[str, Ticket] = {}
        self._views: dict[str, View] = {}

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
        rpc: Rpc | None = None
        try:
            if user is None:
                raise ScreenError(BAD_TICKET, "ticket missing, expired or used")
            if not self.available:
                raise ScreenError(NO_SCREEN, "huvu has no screen to show: Hermes's dashboard isn't set up")
            profile = self._profile(user)
            rpc = await Rpc.connect(self._rpc_url())
            result = await rpc.call("display.observe", {"profile": profile})
            self._views[ticket] = View(user, profile, rpc, str(result["viewer_id"]))
            path = f"{result.get('path') or '/api/display/ws'}?display_ticket={quote(str(result['ticket']))}"
            async with websockets.connect(self._ws_url(path), max_size=None, open_timeout=10) as display:
                await _relay(client, display)
        except ScreenError as err:
            await _close(client, err.code, err.reason)
        except (OSError, TimeoutError, websockets.WebSocketException) as err:
            log.warning("screen for %s: %s", user, err)
            await _close(client, NO_SCREEN, "huvu's screen can't be reached")
        finally:
            view = self._views.pop(ticket, None)
            if view is not None and view.holding:
                view.holding = False
                with contextlib.suppress(Exception):
                    await view.rpc.call("display.lease.release", {"profile": view.profile, "viewer_id": view.viewer})
            if rpc is not None:
                await rpc.close()

    async def lease(self, user: str, ticket: str, take: bool) -> None:
        """Takes the screen over for the person in the view `ticket` names,
        or hands it back to the agent. KeyError when that view isn't open or
        isn't theirs."""
        view = self._views.get(ticket)
        if view is None or view.user != user:
            raise KeyError("no view of yours is open with that ticket")
        if take:
            await view.rpc.call("display.lease.acquire", {"profile": view.profile, "viewer_id": view.viewer, "reason": "taken over in huvu"})
        else:
            await view.rpc.call("display.lease.release", {"profile": view.profile, "viewer_id": view.viewer})
        view.holding = take

    async def sound(self, ticket: str) -> AsyncIterator[bytes]:
        """The browser's sound as MP3, for the view `ticket` names while its
        person holds the screen, until they hand it back or close the view.
        Waits up to HOLD_WAIT for the view to take the screen; KeyError when
        it doesn't."""
        deadline = asyncio.get_running_loop().time() + HOLD_WAIT.total_seconds()
        view = self._views.get(ticket)
        while view is not None and not view.holding and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
            view = self._views.get(ticket)
        if view is None or not view.holding:
            raise KeyError("no view holds the screen with that ticket")
        env = {**os.environ, "PULSE_SERVER": self._kernel.config.pulse_server}
        proc = await asyncio.create_subprocess_exec(
            *self.audio_command(view.profile), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env
        )
        return self._stream(ticket, view, proc)

    async def _stream(self, ticket: str, view: View, proc: asyncio.subprocess.Process) -> AsyncIterator[bytes]:
        assert proc.stdout
        try:
            while view.holding and self._views.get(ticket) is view:
                chunk = await proc.stdout.read(AUDIO_CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()

    def audio_command(self, profile: str) -> list[str]:
        """ffmpeg reading the profile's sink and writing MP3 to stdout, with
        as little buffering as it allows."""
        return [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-fflags", "nobuffer",
            "-f", "pulse", "-fragment_size", "2048", "-i", f"huvu_{profile}.monitor",
            "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "96k",
            "-flush_packets", "1", "-f", "mp3", "pipe:1",
        ]  # fmt: skip

    async def sweep(self) -> None:
        """Hands every profile's screen back to its agent: a person's lease
        outlives a backend that stopped while they held it, and the agent's
        browser stays locked until it goes. The dashboard can come up after
        the backend, so this tries for a while."""
        if not self.available:
            return
        profiles = sorted({"default", *(self._profile(u.id) for u in self._kernel.accounts.users())})
        for _ in range(SWEEP_TRIES):
            try:
                rpc = await Rpc.connect(self._rpc_url())
            except (OSError, TimeoutError, websockets.WebSocketException):
                await asyncio.sleep(SWEEP_EVERY)
                continue
            try:
                for profile in profiles:
                    status = await rpc.call("display.status", {"profile": profile})
                    if (status.get("lease") or {}).get("holder") == "human":
                        await rpc.call("display.lease.release", {"profile": profile, "force": True})
                        log.info("screen: handed %s's screen back to its agent", profile)
            except (ScreenError, TimeoutError, websockets.WebSocketException) as err:
                log.warning("screen sweep: %s", err)
            finally:
                await rpc.close()
            return

    def _profile(self, user: str) -> str:
        return self._kernel.accounts.profile(user).name or "default"

    def _rpc_url(self) -> str:
        return self._ws_url(f"/api/ws?token={quote(self._kernel.config.dashboard_token or '')}")

    def _ws_url(self, path: str) -> str:
        base = self._kernel.config.dashboard_url.rstrip("/")
        return f"ws{base.removeprefix('http')}{path}"


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
