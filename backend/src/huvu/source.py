"""The source contract: what a driver implements, and what the kernel gives it.

A source owns its own working storage and processing. The kernel never
tells it where to keep things, except its history: what happened goes
through `ctx.record` into the kernel's history tables. What is true now it
answers itself, each time a query reads one of its live tables (`live`).
The model reads both with data_sql (data.py), and so do triggers.

Every method has a default, so a driver implements only what it has. A
source with no long-lived connection leaves `run` alone. A source with no
live tables leaves `live` alone.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .spec import Blob, Caller, Feed, Result, SourceSpec

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
        """The source's tables and actions. Called at load, after `bind`, and
        again each time the source calls `ctx.respec()`. The kernel builds
        the history tables, the live tables, the actions and the source's
        skill from it."""
        raise NotImplementedError

    async def run(self) -> None:
        """The long-lived part: a Pulsar consumer, a websocket, a polling
        loop. It reports through `self.ctx`. If it raises, the kernel logs a
        `source_error` row and starts it again after a backoff."""

    def live(self, table: str) -> list[dict[str, Any]]:
        """The rows of one of the source's live tables, now, as dicts by
        column name, with `user_id` and `group_id` for a row that is one
        person's or one group's. It runs on a query's worker thread, so it
        answers from what the source already holds, without awaiting and
        without touching what the event loop may be changing: copy a dict
        before walking it."""
        return []

    async def act(self, name: str, args: dict[str, Any], caller: Caller) -> Result:
        """Runs an action. The kernel calls this only after the action
        policy allowed it, so the source does no permission checks.
        `caller.user` is whom it's for, so a source with per-person data
        acts on that person's; it is None for the household."""
        raise SourceError(f"{self.ctx.name} has no action {name!r}")

    async def fetch(self, ref: str) -> Blob:
        """The binary content behind one of the source's refs."""
        raise SourceError(f"{self.ctx.name} can't fetch {ref!r}")

    async def feeds(self) -> list[Feed]:
        """Live feeds this source can stream, for the frontend's list."""
        return []

    async def ingest(self, body: Any, caller: Caller) -> Any:
        """Takes data a device posts to the source (POST /api/ingest/<name>),
        such as a phone's location. `caller.user` is whose device it is, and
        `caller.device` which one. The return value is the HTTP reply's JSON
        body. A source with no data arriving from outside leaves this alone,
        and the route answers 404."""
        raise NotIngesting(f"{self.ctx.name} takes no data from devices")


class NotIngesting(SourceError):
    """A source that doesn't implement `ingest`."""


class SourceContext:
    """What the kernel gives a source: a way to record history rows, a way
    to say its spec changed, a folder of its own, and its logger."""

    def __init__(self, kernel: Kernel, name: str, data_dir: Path) -> None:
        self._kernel = kernel
        self.name = name
        self.data_dir = data_dir
        self.log = logging.getLogger(f"huvu.source.{name}")

    def now(self) -> datetime:
        return self._kernel.now()

    def member_groups(self, user: str) -> list[str]:
        """The groups a user is a member of (not a guest), for a source
        whose things can be shared with a group."""
        return self._kernel.accounts.member_groups(user)

    def user_name(self, user: str) -> str:
        """What to call a user in a summary: their name, or their username
        if they're gone."""
        found = self._kernel.accounts.user(user)
        return found.name if found else user

    async def record(
        self,
        table: str,
        row: dict[str, Any],
        *,
        at: datetime | None = None,
        user: str | None = None,
        group: str | None = None,
    ) -> int:
        """Adds a row to one of the source's history tables (`table` is
        unqualified, as in the spec), and returns its id. The kernel fills
        in the standard columns: the id, the time keys from `at`, and whose
        row it is from `user` and `group` (see scope.py). The triggers that
        read this source run at once. See data.py."""
        return self._kernel.data.record(self.name, table, row, at=at or self._kernel.now(), user=user, group=group)

    def nudge(self) -> None:
        """Says something the source's live tables show may have changed,
        with no row to record, such as a sensor reading that only goes
        into a 5-minute bucket. The triggers that read the source run at
        once. Only a matter of speed: the 5-second poll catches the change
        anyway, so a nudge that is missed costs at most that."""
        self._kernel.triggers.nudge(self.name)

    def rows(self, table: str, where: str, args: Sequence[Any] = (), *, order: str = "ts", limit: int | None = None) -> list[dict[str, Any]]:
        """Reads the source's own history rows back, every person's, for a
        source that works from its past. `where` and `order` are SQL the
        driver writes; values go in `args`."""
        return self._kernel.data.rows(self.name, table, where, args, order=order, limit=limit)

    async def respec(self) -> None:
        """Tells the kernel the source's spec changed, such as when the
        scripts in Home Assistant change. The kernel calls `describe` again,
        and rebuilds the tools, the history tables and the source's skill."""
        await self._kernel.respec(self.name)
