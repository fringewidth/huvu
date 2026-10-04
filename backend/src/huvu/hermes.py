"""The client for Hermes's API server: sessions for chats and wakes.

The backend starts Hermes sessions and streams their turns. It owns nothing
about the model: prompts, memory, skills and schedules stay in Hermes.

Every user has their own Hermes profile, so their memory, skills and cron
jobs stay theirs. One Hermes gateway serves them all: a profile's routes
live under `/p/<profile>/`, each with its own key. The household uses the
default profile at the plain routes.

The stream is `POST /api/sessions/{id}/chat/stream`, Server-Sent Events:
`run.started`, `message.started`, `assistant.delta`, `assistant.commentary`,
`tool.started`, `tool.completed`, `tool.failed`, `assistant.completed`, then
`run.completed`, `run.failed` or `run.cancelled`, an `error` on a crash, and
`done`. Every payload carries `session_id`, `run_id`, `seq` and `ts`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("huvu.hermes")


class HermesError(Exception):
    pass


@dataclass
class HermesEvent:
    name: str
    data: dict[str, Any]


@dataclass(frozen=True)
class Profile:
    """A Hermes profile and the key its API takes. `name` is None for the
    default profile, which the household uses."""

    name: str | None
    key: str | None

    @property
    def prefix(self) -> str:
        return f"/p/{self.name}" if self.name else ""

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"} if self.key else {}


class HermesClient:
    def __init__(self, base_url: str) -> None:
        # Turns can run for minutes, and Hermes sends a keepalive every 10 s.
        self._http = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(30.0, read=120.0))

    async def close(self) -> None:
        await self._http.aclose()

    async def create_session(self, profile: Profile, session_id: str, *, title: str | None = None) -> str:
        body: dict[str, Any] = {"id": session_id}
        if title:
            body["title"] = title
        res = await self._http.post(f"{profile.prefix}/api/sessions", json=body, headers=profile.headers)
        if res.status_code not in (200, 201):
            raise HermesError(f"creating session {session_id}: {res.status_code} {res.text[:300]}")
        return session_id

    async def chat(self, profile: Profile, session_id: str, message: str) -> AsyncIterator[HermesEvent]:
        """Runs one turn and yields its events as they arrive."""
        path = f"{profile.prefix}/api/sessions/{session_id}/chat/stream"
        async with self._http.stream("POST", path, json={"message": message}, headers=profile.headers) as res:
            if res.status_code != 200:
                body = (await res.aread()).decode(errors="replace")
                raise HermesError(f"chat in {session_id}: {res.status_code} {body[:300]}")
            async for event in parse_sse(res.aiter_lines()):
                yield event

    async def stop(self, profile: Profile, run_id: str) -> None:
        res = await self._http.post(f"{profile.prefix}/v1/runs/{run_id}/stop", headers=profile.headers)
        if res.status_code >= 400:
            log.warning("stopping run %s: %s %s", run_id, res.status_code, res.text[:200])


async def parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[HermesEvent]:
    """Server-Sent Events, one event per blank-line-ended block. Comment
    lines (keepalives) start with a colon and are skipped."""
    name = "message"
    data: list[str] = []
    async for line in lines:
        if line == "":
            if data:
                yield HermesEvent(name, _json("\n".join(data)))
            name, data = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield HermesEvent(name, _json("\n".join(data)))


def _json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = {"text": text}
    return value if isinstance(value, dict) else {"value": value}
