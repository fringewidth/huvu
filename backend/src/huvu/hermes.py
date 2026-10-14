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

A wake runs through `POST /v1/runs` instead (`run`). Its first turn brings
its own history, the situation block. Hermes takes a turn whose caller
brings the history as finished when it ends, so it runs subagents
(`delegate_task`) inline and returns their results in the same turn. On a
session's chat it would keep them for a next turn, which a wake may never
have. A later turn in the wake, its person's reply, brings no history, and
Hermes loads the session's own.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("huvu.hermes")

# Hermes refuses a longer session title with a 500.
TITLE_MAX = 100


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

    async def chat(
        self,
        profile: Profile,
        session_id: str,
        message: str,
        *,
        model: str | None = None,
        provider: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[HermesEvent]:
        """Runs one turn and yields its events as they arrive. `model`, and
        `provider` with it, override the profile's for this run only, and
        so does `effort`, the reasoning effort ("none" turns it off)."""
        path = f"{profile.prefix}/api/sessions/{session_id}/chat/stream"
        body: dict[str, Any] = {"message": message, **({"model": model} if model else {}), **({"provider": provider} if provider else {})}
        if effort:
            body["model_options"] = {"reasoning_effort": effort}
        async with self._http.stream("POST", path, json=body, headers=profile.headers) as res:
            if res.status_code != 200:
                body = (await res.aread()).decode(errors="replace")
                raise HermesError(f"chat in {session_id}: {res.status_code} {body[:300]}")
            async for event in parse_sse(res.aiter_lines()):
                yield event

    async def run(
        self,
        profile: Profile,
        session_id: str,
        history: list[dict[str, str]],
        message: str,
        *,
        model: str | None = None,
        provider: str | None = None,
    ) -> AsyncIterator[HermesEvent]:
        """Runs one turn through /v1/runs, after `history` (role and content
        per message), and yields its events under chat's names: run.started
        with its run_id, assistant.delta, assistant.completed, then
        run.completed, run.failed or run.cancelled. With no history, Hermes
        loads the session's own, tool calls included, so the turn carries
        on from where the session stopped."""
        body: dict[str, Any] = {
            "session_id": session_id,
            "input": [*history, {"role": "user", "content": message}],
            **({"model": model} if model else {}),
            **({"provider": provider} if provider else {}),
        }
        res = await self._http.post(f"{profile.prefix}/v1/runs", json=body, headers=profile.headers)
        if res.status_code not in (200, 201, 202):
            raise HermesError(f"run in {session_id}: {res.status_code} {res.text[:300]}")
        run_id = res.json()["run_id"]
        yield HermesEvent("run.started", {"run_id": run_id})
        async with self._http.stream("GET", f"{profile.prefix}/v1/runs/{run_id}/events", headers=profile.headers) as res:
            if res.status_code != 200:
                text = (await res.aread()).decode(errors="replace")
                raise HermesError(f"events of run {run_id}: {res.status_code} {text[:300]}")
            async for event in parse_sse(res.aiter_lines()):
                name = str(event.data.get("event") or event.name)
                if name == "message.delta":
                    yield HermesEvent("assistant.delta", {"delta": event.data.get("delta", "")})
                elif name == "run.completed":
                    yield HermesEvent("assistant.completed", {"content": event.data.get("output", "")})
                    yield HermesEvent(name, event.data)
                    return
                elif name in ("run.failed", "run.cancelled"):
                    yield HermesEvent(name, event.data)
                    return
                else:
                    yield HermesEvent(name, event.data)

    async def delete_session(self, profile: Profile, session_id: str) -> None:
        """Deletes a session. One that is already gone counts as deleted."""
        res = await self._http.delete(f"{profile.prefix}/api/sessions/{session_id}", headers=profile.headers)
        if res.status_code >= 400 and res.status_code != 404:
            raise HermesError(f"deleting session {session_id}: {res.status_code} {res.text[:300]}")

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
