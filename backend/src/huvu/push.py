"""The push channel: one long-lived stream per open client.

Some things reach a device that didn't ask for them right then: a turn in
its conversation that it didn't start, a notification, a pending approval,
a question asking whether it's them at a shared device. Every open client
keeps `GET /api/push` open, and gets only what is addressed to its device.
Notifications, approvals and those questions go to phones, which are taken
to be on their user's person. A device that isn't connected misses them
here; the hub hands everything it sends to `forward` as well, which is how
native push (native_push.py) reaches a phone whose app is closed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel

log = logging.getLogger("huvu.push")

# A client that stops reading loses events past this many.
QUEUE_SIZE = 256


@dataclass
class Client:
    id: str
    device: str
    queue: asyncio.Queue[tuple[str, Any]] = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))


class PushHub:
    def __init__(self, forward: Callable[[str, Any, list[str]], None] | None = None) -> None:
        self._clients: dict[str, Client] = {}
        self._forward = forward

    @property
    def clients(self) -> list[dict[str, str]]:
        return [{"id": c.id, "device": c.device} for c in self._clients.values()]

    async def subscribe(self, device: str) -> AsyncIterator[tuple[str, Any]]:
        """One open stream for a device. A device can hold several."""
        client = Client(f"{device}-{secrets.token_hex(3)}", device)
        self._clients[client.id] = client
        try:
            while True:
                yield await client.queue.get()
        finally:
            self._clients.pop(client.id, None)

    def send(self, name: str, data: Any, devices: Iterable[str]) -> None:
        wanted = set(devices)
        for client in self._clients.values():
            if client.device not in wanted:
                continue
            try:
                client.queue.put_nowait((name, data))
            except asyncio.QueueFull:
                log.warning("push client %s is not reading; dropped %s", client.id, name)
        if self._forward is None:
            return
        # The streams have it already, so a failure here only logs.
        try:
            self._forward(name, data, sorted(wanted))
        except Exception:
            log.exception("forwarding %s failed", name)


def sse(name: str, data: Any) -> str:
    """One Server-Sent Event, with datetimes as ISO."""
    return f"event: {name}\ndata: {json.dumps(api_json(data), ensure_ascii=False)}\n\n"


def api_json(value: Any) -> Any:
    """JSON for the frontend and other code: ISO datetimes, plain dicts."""
    if isinstance(value, BaseModel):
        value = value.model_dump(exclude_none=True)
    if isinstance(value, datetime):
        out: Any = value.isoformat()
    elif isinstance(value, timedelta):
        out = value.total_seconds()
    elif isinstance(value, dict):
        out = {k: api_json(v) for k, v in value.items()}
    elif isinstance(value, (list, tuple)):
        out = [api_json(v) for v in value]
    else:
        out = value
    return out
