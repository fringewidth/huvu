"""Test doubles: a home source with a door, a porch light and a phone, a
scripted Hermes, and a clock the test moves by hand."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

from huvu.hermes import HermesEvent, Profile
from huvu.source import Source, SourceError
from huvu.spec import ActionSpec, Blob, Caller, ColumnSpec, Feed, Result, SourceSpec, TableSpec


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


class HomeSource(Source):
    """Lights and a status board as live tables, from dicts a test changes
    by hand; doorbell presses and unlocks as history, recorded with
    `press` and `unlock`."""

    def __init__(self) -> None:
        super().__init__({})
        self.acted: list[tuple[str, dict[str, Any]]] = []
        self.actors: list[Caller] = []
        self.lights: dict[str, dict[str, Any]] = {
            "light.porch": {"name": "Porch Light", "state": "off"},
            "light.light_3": {"name": "Light 3", "state": "off"},
        }
        self.status: dict[str, Any] = {"eta_min": None, "battery": 80}
        self.down = False

    def describe(self) -> SourceSpec:
        return SourceSpec(
            name="home",
            description="A fake house.",
            guide="Search for a light before turning it on.",
            tables=[
                TableSpec(
                    name="doorbell",
                    description="Every press of the doorbell, with its snapshot.",
                    columns=[ColumnSpec(name="snapshot", type="text", description="The snapshot's ref"), ColumnSpec(name="note", type="text", description="What the snapshot shows")],
                    labels=["external"],
                ),
                TableSpec(
                    name="unlocks",
                    description="Every unlock of the door.",
                    columns=[ColumnSpec(name="method", type="text", description="How"), ColumnSpec(name="person", type="text", description="Who")],
                ),
                TableSpec(
                    name="lights",
                    description="Every light, now.",
                    columns=[
                        ColumnSpec(name="entity_id", type="text", description="The light"),
                        ColumnSpec(name="name", type="text", description="Its name"),
                        ColumnSpec(name="state", type="text", description="on or off"),
                    ],
                    live=True,
                ),
                TableSpec(
                    name="status",
                    description="The house now: one row.",
                    columns=[ColumnSpec(name="eta_min", type="real", description="Minutes to home by car"), ColumnSpec(name="battery", type="integer", description="Door lock battery")],
                    live=True,
                ),
            ],
            actions=[
                ActionSpec(name="turn_on", description="Turns a light on.", autonomy="silent", input_schema={"type": "object", "properties": {"entity_id": {"type": "string"}}, "required": ["entity_id"]}),
                ActionSpec(name="announce", description="Says something on the speaker.", autonomy="notify"),
                ActionSpec(name="unlock", description="Unlocks the front door.", autonomy="silent", requires_human=True),
            ],
        )

    def live(self, table: str) -> list[dict[str, Any]]:
        if self.down:
            raise SourceError("the house is offline")
        if table == "lights":
            return [{"entity_id": e, **light} for e, light in dict(self.lights).items()]
        return [dict(self.status)]

    async def press(self, note: str = "a note held up to the camera") -> int:
        return await self.ctx.record("doorbell", {"snapshot": "home:snap/1", "note": note})

    async def unlock(self, method: str, person: str | None = None, *, user: str | None = None) -> int:
        return await self.ctx.record("unlocks", {"method": method, "person": person}, user=user)

    async def act(self, name: str, args: dict[str, Any], caller: Caller) -> Result:
        self.acted.append((name, args))
        self.actors.append(caller)
        return Result(data={"done": name})

    async def fetch(self, ref: str) -> Blob:
        if ref != "home:snap/1":
            raise SourceError("no such ref")
        return Blob(content=b"\xff\xd8jpeg", content_type="image/jpeg")

    async def feeds(self) -> list[Feed]:
        return [Feed(id="balcony", label="balcony", kind="camera", src="http://cam/whep/balcony")]


class FakeHermes:
    """Records sessions and messages, and plays back a scripted stream. Each
    script entry is a list of (event name, data) pairs, or a coroutine
    function called with the kernel's tool box between events."""

    def __init__(self) -> None:
        self.sessions: list[str] = []
        # Session id -> the profile it was created in. None is the default.
        self.profiles: dict[str, str | None] = {}
        # The model each session's last run asked for.
        self.models: dict[str, str | None] = {}
        self.providers: dict[str, str | None] = {}
        self.messages: list[tuple[str, str]] = []
        self.stopped: list[str] = []
        self.deleted: list[str] = []
        # Set when a run is stopped, for scripts that wait on it.
        self.stop_requested = asyncio.Event()
        self.script: list[Any] = []
        self.hooks: list[Any] = []

    async def create_session(self, profile: Profile, session_id: str, *, title: str | None = None) -> str:
        self.sessions.append(session_id)
        self.profiles[session_id] = profile.name
        return session_id

    async def chat(
        self, profile: Profile, session_id: str, message: str, *, model: str | None = None, provider: str | None = None
    ) -> AsyncIterator[HermesEvent]:
        self.messages.append((session_id, message))
        self.models[session_id] = model
        self.providers[session_id] = provider
        steps = self.script or [("run.started", {"run_id": "run_1"}), ("assistant.delta", {"delta": "ok"}), ("run.completed", {})]
        for step in steps:
            if callable(step):
                await step(session_id)
                continue
            name, data = step
            yield HermesEvent(name, {"session_id": session_id, **data})

    async def stop(self, profile: Profile, run_id: str) -> None:
        self.stopped.append(run_id)
        self.stop_requested.set()

    async def delete_session(self, profile: Profile, session_id: str) -> None:
        self.deleted.append(session_id)

    async def close(self) -> None:
        pass


# The script for a test trigger that only wakes the model.
WAKE = 'def run(ctx):\n    ctx.wake("It fired.")\n'
