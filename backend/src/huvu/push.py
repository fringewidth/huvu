"""The push channel: one long-lived stream per open client.

Some things reach a client that didn't ask for them: a turn another device
started (the phone asks, the TV shows), a notification from a wake, a
pending approval. Every open client keeps `GET /api/push` open and gets
all of them. One owner, so every client sees everything.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from .policy import Approval

log = logging.getLogger("huvu.push")

# A client that stops reading loses events past this many.
QUEUE_SIZE = 256


@dataclass
class Client:
    id: str
    kind: str
    queue: asyncio.Queue[tuple[str, Any]] = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))


class PushHub:
    def __init__(self) -> None:
        self._clients: dict[str, Client] = {}

    @property
    def clients(self) -> list[dict[str, str]]:
        return [{"id": c.id, "kind": c.kind} for c in self._clients.values()]

    async def subscribe(self, client_id: str, kind: str) -> AsyncIterator[tuple[str, Any]]:
        client = Client(client_id, kind)
        self._clients[client_id] = client
        try:
            while True:
                yield await client.queue.get()
        finally:
            if self._clients.get(client_id) is client:
                del self._clients[client_id]

    def broadcast(self, name: str, data: Any) -> None:
        for client in self._clients.values():
            try:
                client.queue.put_nowait((name, data))
            except asyncio.QueueFull:
                log.warning("push client %s is not reading; dropped %s", client.id, name)

    async def notify(self, text: str, *, image: str | None = None, urgency: str = "normal") -> None:
        self.broadcast("notify", {"text": text, "image": image, "urgency": urgency})

    async def approval(self, approval: Approval) -> None:
        self.broadcast("approval", approval.public())


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
