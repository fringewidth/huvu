"""Test doubles: a home source with a door, a porch light and a phone, a
scripted Hermes, and a clock the test moves by hand."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

from huvu.hermes import HermesEvent
from huvu.source import Source, SourceError
from huvu.spec import ActionSpec, Blob, EventSpec, Feed, Match, QuerySpec, Result, SourceSpec, ValueSpec


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


class HomeSource(Source):
    def __init__(self) -> None:
        super().__init__({})
        self.acted: list[tuple[str, dict[str, Any]]] = []
        self.watched: list[str] = []

    def describe(self) -> SourceSpec:
        return SourceSpec(
            name="home",
            description="A fake house.",
            values=[
                ValueSpec(name="eta_min", description="Minutes to home by car", type="number", max_age=timedelta(minutes=30)),
                ValueSpec(name="light", description="State of a light", type="string", params={"entity_id": "string"}),
                ValueSpec(name="battery", description="Door lock battery", type="number", updates="on_wake"),
            ],
            events=[
                EventSpec(type="doorbell", description="Someone rang the doorbell."),
                EventSpec(type="unlock", description="The door was unlocked.", data={"method": "string", "person": "string"}),
            ],
            queries=[QuerySpec(name="snapshot", description="The last doorbell snapshot.", input_schema={"type": "object", "properties": {}})],
            actions=[
                ActionSpec(name="turn_on", description="Turns a light on.", autonomy="silent", input_schema={"type": "object", "properties": {"entity_id": {"type": "string"}}, "required": ["entity_id"]}),
                ActionSpec(name="announce", description="Says something on the speaker.", autonomy="notify"),
                ActionSpec(name="unlock", description="Unlocks the front door.", autonomy="silent", requires_human=True),
            ],
        )

    async def search(self, text: str) -> list[Match]:
        lights = {"light.porch": "Porch Light", "light.light_3": "Light 3"}
        return [Match(value=f"home.light[{k}]", name=v) for k, v in lights.items() if text.lower() in v.lower()]

    async def query(self, name: str, args: dict[str, Any]) -> Result:
        if name != "snapshot":
            raise SourceError(f"no query {name}")
        return Result(data={"ref": "home:snap/1", "text": "a note held up to the camera"}, labels=["external"])

    async def act(self, name: str, args: dict[str, Any]) -> Result:
        self.acted.append((name, args))
        return Result(data={"done": name})

    async def watch(self, value: str, params: dict[str, Any]) -> None:
        self.watched.append(value)

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
        self.messages: list[tuple[str, str]] = []
        self.stopped: list[str] = []
        self.script: list[Any] = []
        self.hooks: list[Any] = []

    async def create_session(self, session_id: str, *, title: str | None = None, source: str = "huvu") -> str:
        self.sessions.append(session_id)
        return session_id

    async def chat(self, session_id: str, message: str) -> AsyncIterator[HermesEvent]:
        self.messages.append((session_id, message))
        steps = self.script or [("run.started", {"run_id": "run_1"}), ("assistant.delta", {"delta": "ok"}), ("run.completed", {})]
        for step in steps:
            if callable(step):
                await step(session_id)
                continue
            name, data = step
            yield HermesEvent(name, {"session_id": session_id, **data})

    async def stop(self, run_id: str) -> None:
        self.stopped.append(run_id)

    async def close(self) -> None:
        pass
