"""A small Home Assistant WebSocket client.

One connection carries everything: commands, each with an id and one
result, and the events of subscriptions, tagged with the id of the command
that subscribed. `listen` reads the connection and routes both; it must be
running for `call` to get its results.

Event handlers run inside the reader, in order, so the driver sees state
changes in the order HA sent them. A handler must therefore never wait on
`call`, or it waits for a result only the reader it blocks could deliver.

`Transport` is what the client needs from a connection. A `websockets`
connection is one; the tests give a fake Home Assistant.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

log = logging.getLogger("huvu.source.ha.client")

CALL_TIMEOUT = 30.0

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class Transport(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


Connect = Callable[[str], Awaitable[Transport]]


class HAError(Exception):
    """Home Assistant answered a command with an error, or refused the
    token."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{message} ({code})")
        self.code = code


async def connect_websocket(url: str) -> Transport:
    from websockets.asyncio.client import connect

    # get_states on a big install is several megabytes.
    return await connect(url, max_size=64 * 1024 * 1024)


def websocket_url(base: str) -> str:
    """`http://ha:8123` to `ws://ha:8123/api/websocket`."""
    scheme, sep, rest = base.rstrip("/").partition("://")
    ws = {"http": "ws", "https": "wss"}.get(scheme, scheme)
    return f"{ws}{sep}{rest}/api/websocket"


class HAClient:
    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._handlers: dict[int, Handler] = {}
        self.version: str | None = None

    @classmethod
    async def open(cls, base_url: str, token: str, connect: Connect = connect_websocket) -> HAClient:
        """Connects and logs in. Raises HAError on a refused token."""
        transport = await connect(websocket_url(base_url))
        client = cls(transport)
        try:
            await client._login(token)
        except BaseException:
            await transport.close()
            raise
        return client

    async def _login(self, token: str) -> None:
        hello = json.loads(await self._transport.recv())
        if hello.get("type") != "auth_required":
            raise HAError("protocol", f"expected auth_required, got {hello.get('type')!r}")
        await self._transport.send(json.dumps({"type": "auth", "access_token": token}))
        reply = json.loads(await self._transport.recv())
        if reply.get("type") != "auth_ok":
            raise HAError("auth_invalid", reply.get("message") or "Home Assistant refused the token")
        self.version = reply.get("ha_version")

    async def close(self) -> None:
        await self._transport.close()

    async def listen(self) -> None:
        """Reads until the connection fails, and then raises. Every command
        still waiting fails with ConnectionError."""
        try:
            while True:
                message = json.loads(await self._transport.recv())
                # HA sends a list when it coalesces messages, which it only
                # does for clients that ask; take both.
                for item in message if isinstance(message, list) else [message]:
                    await self._route(item)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("the connection to Home Assistant closed"))
            self._pending.clear()

    async def _route(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "event":
            handler = self._handlers.get(message.get("id", -1))
            if handler is not None:
                try:
                    await handler(message["event"])
                except Exception:
                    log.exception("a Home Assistant event handler failed")
        elif kind == "result":
            future = self._pending.pop(message.get("id", -1), None)
            if future is None or future.done():
                return
            if message.get("success"):
                future.set_result(message.get("result"))
            else:
                error = message.get("error") or {}
                future.set_exception(HAError(error.get("code", "unknown"), error.get("message", "Home Assistant refused the command")))

    async def call(self, type: str, *, timeout: float = CALL_TIMEOUT, **payload: Any) -> Any:
        """Sends one command and returns its result."""
        return await self._command({"type": type, **payload}, None, timeout)

    async def subscribe(self, event_type: str, handler: Handler) -> None:
        """Calls `handler(event)` for each event of a type from now on."""
        await self._command({"type": "subscribe_events", "event_type": event_type}, handler, CALL_TIMEOUT)

    async def _command(self, message: dict[str, Any], handler: Handler | None, timeout: float) -> Any:
        message_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        if handler is not None:
            self._handlers[message_id] = handler
        await self._transport.send(json.dumps({"id": message_id, **message}))
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(message_id, None)
