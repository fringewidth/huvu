"""The source contract: what a driver implements, and what the kernel gives it.

A source owns its own storage, processing, querying and retrieval. The
kernel never tells it where to keep things. It only asks the source
questions through the methods below, and hears from it through the context.

Every method has a default, so a driver implements only what it has. A
source with no long-lived connection leaves `run` alone. A source whose
values are all pushed leaves `read` alone, since the kernel answers from
what it pushed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .spec import Blob, Caller, Event, Feed, Label, Match, Reading, Result, SourceSpec

if TYPE_CHECKING:
    from .kernel import Kernel


class SourceError(Exception):
    """A failure the caller should see, such as an unknown ref or a device
    that is offline. The message goes back to the model as is."""


class Source:
    """Base class for every driver. `options` comes from the source's entry
    in the config file."""

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = options or {}
        self._ctx: SourceContext | None = None

    @property
    def ctx(self) -> SourceContext:
        if self._ctx is None:
            raise RuntimeError(f"{type(self).__name__}: used before the kernel bound it")
        return self._ctx

    def bind(self, ctx: SourceContext) -> None:
        self._ctx = ctx

    def describe(self) -> SourceSpec:
        """The source's values, events, queries and actions. Called once at
        load. The kernel builds the model's tools and the trigger catalog
        from it."""
        raise NotImplementedError

    async def run(self) -> None:
        """The long-lived part: a Pulsar consumer, a websocket, a polling
        loop. It reports through `self.ctx`. If it raises, the kernel logs a
        `<source>.error` event and starts it again after a backoff."""

    async def read(self, value: str, params: dict[str, Any]) -> Reading:
        """The current reading of one value. Only `on_demand` values need it;
        the kernel answers pushed values from what the source pushed."""
        return Reading(reason="this source has not reported this value yet")

    async def search(self, text: str) -> list[Match]:
        """The source's things whose name, area or alias matches `text`,
        such as HA entities. The kernel merges every source's answer."""
        return []

    async def query(self, name: str, args: dict[str, Any], caller: Caller) -> Result:
        """Runs a query. `caller.user` is whom it's for, so a source with
        per-person data (a calendar) answers with that person's; it is None
        for the household."""
        raise SourceError(f"{self.ctx.name} has no query {name!r}")

    async def act(self, name: str, args: dict[str, Any], caller: Caller) -> Result:
        """Runs an action. The kernel calls this only after the action
        policy allowed it, so the source does no permission checks.
        `caller.user` is whom it's for, as in `query`."""
        raise SourceError(f"{self.ctx.name} has no action {name!r}")

    async def watch(self, value: str, params: dict[str, Any]) -> None:
        """Asks the source to push changes for one value from now on. A
        source that pushes everything anyway can leave this alone."""

    async def fetch(self, ref: str) -> Blob:
        """The binary content behind one of the source's refs."""
        raise SourceError(f"{self.ctx.name} can't fetch {ref!r}")

    async def feeds(self) -> list[Feed]:
        """Live feeds this source can stream, for the frontend's list."""
        return []


class SourceContext:
    """What the kernel gives a source: a way to report events and values, a
    way to read other sources (for derived sources like presence), a
    folder of its own, and its logger."""

    def __init__(self, kernel: Kernel, name: str, data_dir: Path) -> None:
        self._kernel = kernel
        self.name = name
        self.data_dir = data_dir
        self.log = logging.getLogger(f"huvu.source.{name}")

    def now(self) -> datetime:
        return self._kernel.now()

    async def emit(
        self,
        type: str,
        summary: str,
        *,
        entity: str | None = None,
        refs: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
        labels: list[Label] | None = None,
        at: datetime | None = None,
        user: str | None = None,
    ) -> Event:
        """Logs an event. `type` is unqualified (`doorbell`); the kernel adds
        the source's name. It must be one of the types in the spec. `user`
        makes it one person's (their calendar), not the household's."""
        event = Event(
            at=at or self._kernel.now(),
            source=self.name,
            type=f"{self.name}.{type}",
            entity=entity,
            summary=summary,
            refs=refs or {},
            data=data or {},
            labels=labels or ["house"],
            user=user,
        )
        return await self._kernel.emit(event)

    async def update(
        self,
        value: str,
        reading: Any,
        *,
        params: dict[str, Any] | None = None,
        at: datetime | None = None,
        labels: list[Label] | None = None,
    ) -> None:
        """Reports the current reading of one of the source's values. Only
        watchers hear each change; the event log doesn't."""
        await self._kernel.state.update(
            self.name,
            value,
            params or {},
            Reading(value=reading, at=at or self._kernel.now(), labels=labels or ["house"]),
        )

    async def read(self, keys: list[str]) -> dict[str, Reading]:
        """Reads values from any source, by their keys (`ha.entity_state[light.porch]`)."""
        return await self._kernel.state.get(keys)

    async def query(self, source: str, name: str, args: dict[str, Any], caller: Caller | None = None) -> Result:
        """Runs another source's query, for the caller this source is
        answering, or for the kernel."""
        return await self._kernel.registry.get(source).query(name, args, caller or Caller(kind="kernel"))

    async def watch(self, key: str, callback) -> None:
        """Calls `callback(key, reading)` on each change of another source's
        value. For derived sources."""
        await self._kernel.state.watch(key, callback)
