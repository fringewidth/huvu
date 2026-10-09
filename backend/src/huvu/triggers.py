"""Triggers: a query, and a script that runs when its answer changes.

Hermes registers a trigger through MCP, and the engine here runs it.
Triggers are huvu's only scheduler, and Hermes's cron toolset is off in
every profile.

A trigger is one read-only SQL query over the tables (data.py), run in its
owner's scope, and a script. `fire` says when the script runs:

  edge: when the query's answer goes from no rows to some rows. It rearms
        once the answer is empty again. A condition that already holds when
        the trigger goes live fires at once.
  rows: for every row the answer holds that it hasn't seen, by `id`, so
        the query must return the `id` of a history table. Rows that exist
        when the trigger is registered don't fire, nor rows that came while
        it was paused or waiting for its person.

`once` ends it after its first fire. A trigger fires again only after its
run ends: a change while it runs is looked at when the run ends, and no
row is lost, since rows go by id.

When a trigger's query runs:

1. When a table it reads changes: a row recorded, or a live table's rows
   handed over (`nudge`). Every change to a table goes through the kernel
   (data.py), so the kernel nudges itself and none is missed. This is what
   makes "light on when I walk in" immediate.
2. Every minute, the triggers that read the time (data.CLOCK_FUNCTIONS).

What a query reads is found when it is compiled, from SQLite's authorizer,
without running it (data.py).

Every trigger is a script, decided 2026-10-08. The script does what needs
no thought itself, such as a reminder, and wakes a model instance with
`ctx.wake(...)` when it needs one, on every fire or only on some. So the
model learns when to wake itself by writing the script, and no prompt has
to teach it.

Every trigger belongs to a user: its query runs in their scope, its wakes
in their Hermes profile, and its approvals and notifications go to their
phones. A trigger set up from a shared device nobody is recognized at has
no user yet, so it waits, unconfirmed, and everyone in the device's group
gets "is this you on the living room TV?" on their phone. The first yes
makes that person the device's attendee (see attendance.py), and the
device's waiting triggers become theirs and go live. If everyone says no,
or nobody answers in time, they are dropped.

A runaway trigger is held back twice. One that fires more than FIRE_LIMIT
times in FIRE_WINDOW pauses itself, which stops a trigger that feeds on its
own runs. Its model wakes are counted too (see wake.py): from the 10th in
two hours the wake tells the model so, and a 21st doesn't happen; the
trigger pauses itself and tells its user. A user with more than BUSY_FIRES
fires an hour across their triggers is told once an hour. Any session
acting for a trigger's user can pause and resume it; one a limit paused
resumes only by the user's own tap.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, ValidationError, field_validator

from .data import SQL_MAX, Compiled, DataError
from .db import dumps, from_iso, iso, loads
from .prompts import TRIGGER_FIELDS
from .scope import Viewer
from .spec import Caller, Strict
from .times import parse_when

if TYPE_CHECKING:
    from .kernel import Kernel

log = logging.getLogger("huvu.triggers")

Fire = Literal["edge", "rows"]
Status = Literal["active", "unconfirmed", "awaiting", "paused", "done", "expired", "removed"]
# Which model a trigger's wakes run on (config.models). Advanced waits for
# its person's yes before it goes live.
Tier = Literal["basic", "pro", "advanced"]
# Statuses of a trigger still alive, firing or not.
LIVE = ("active", "unconfirmed", "awaiting", "paused")
# Who paused a trigger. One a limit paused resumes only by its user.
PausedBy = Literal["model", "user", "limit"]

# How often the clock is checked for a new minute, in seconds.
TICK = 5.0
# A trigger that fires once and names no expiry gets this one, so a
# condition that never comes true doesn't stay forever.
DEFAULT_EXPIRY = timedelta(days=7)
# More fires than this in the window, and a trigger pauses itself.
FIRE_LIMIT = 30
FIRE_WINDOW = timedelta(minutes=5)
# A user with more fires than this across their triggers in an hour is told.
BUSY_FIRES = 10
BUSY_WINDOW = timedelta(hours=1)
# How long a trigger from a shared device waits for its person, should the
# questions on their phones somehow outlive it.
UNCONFIRMED_TTL = timedelta(minutes=15)
# How many rows of the answer a dry run shows.
DRY_ROWS = 5


class TriggerSpec(Strict):
    """What a model registers."""

    note: str = Field(description=TRIGGER_FIELDS["TriggerSpec.note"])
    sql: str = Field(description=TRIGGER_FIELDS["TriggerSpec.sql"])
    fire: Fire = Field(description=TRIGGER_FIELDS["TriggerSpec.fire"])
    once: bool = Field(description=TRIGGER_FIELDS["TriggerSpec.once"])
    expires: str | None = Field(default=None, description=TRIGGER_FIELDS["TriggerSpec.expires"])
    # A Python function `run(ctx)`, run each time it fires. See scripts.py
    # and script_host.py.
    script: str = Field(description=TRIGGER_FIELDS["TriggerSpec.script"])
    # The tools the script may call.
    grants: list[str] = Field(default_factory=list, description=TRIGGER_FIELDS["TriggerSpec.grants"])
    tier: Tier = Field(default="basic", description=TRIGGER_FIELDS["TriggerSpec.tier"])

    @field_validator("note")
    @classmethod
    def _note(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("note must say why the trigger exists and what to do")
        return v


@dataclass
class Trigger:
    id: str
    spec: TriggerSpec
    status: Status
    # edge: the answer was empty when last looked at, so the next rows fire.
    armed: bool
    created_at: datetime
    created_by: str | None
    # None only while unconfirmed.
    user: str | None
    expires_at: datetime | None
    # The group and shared device it was set up through, if any.
    group: str | None = None
    device: str | None = None
    paused_by: PausedBy | None = None
    # When it was last resumed. The wake limit counts from here.
    resumed_at: datetime | None = None
    last_fired_at: datetime | None = None
    fire_count: int = 0
    # rows: the id of the last row it has seen.
    after: int = 0
    # Filled in by the engine while the trigger is active.
    compiled: Compiled | None = None
    fires: deque[datetime] = field(default_factory=deque)
    # The last failure of its query, so the same one is logged once.
    failing: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user": self.user,
            "group": self.group,
            "device": self.device,
            "paused_by": self.paused_by,
            "note": self.spec.note,
            "sql": self.spec.sql,
            "fire": self.spec.fire,
            "once": self.spec.once,
            "script": bool(self.spec.script),
            "grants": self.spec.grants or None,
            "tier": self.spec.tier,
            "status": self.status,
            "armed": self.armed,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "last_fired_at": self.last_fired_at,
            "fire_count": self.fire_count,
        }


class TriggerError(ValueError):
    pass


class TriggerEngine:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        self._triggers: dict[str, Trigger] = {}
        # Triggers in the middle of a run, those whose inputs changed during
        # one, and those with a look already on its way.
        self._running: set[str] = set()
        self._dirty: set[str] = set()
        self._queued: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._poller: asyncio.Task[None] | None = None
        self._minute: tuple[str, int] | None = None

    @property
    def _db(self):
        return self._kernel.db

    async def start(self) -> None:
        """Loads the triggers and starts the poll."""
        await self.load()
        self._poller = asyncio.create_task(self._poll_forever(), name="triggers:poll")

    async def load(self) -> None:
        """Reloads stored triggers and activates the live ones."""
        rows = self._db.execute(f"SELECT * FROM triggers WHERE status IN ({', '.join('?' * len(LIVE))})", LIVE).fetchall()
        for row in rows:
            try:
                spec = TriggerSpec.model_validate(loads(row["spec"]))
            except ValidationError as err:
                await self._kernel.kernel_event("trigger.error", f"trigger {row['id']} can't be read any more: {err}", entity=row["id"], user=row["user_id"])
                continue
            trigger = Trigger(
                id=row["id"],
                spec=spec,
                status=row["status"],
                armed=bool(row["armed"]),
                created_at=from_iso(row["created_at"]),
                created_by=row["created_by"],
                user=row["user_id"],
                group=row["group_id"],
                expires_at=from_iso(spec.expires),
                device=row["device_id"],
                paused_by=row["paused_by"],
                resumed_at=from_iso(row["resumed_at"]),
                last_fired_at=from_iso(row["last_fired_at"]),
                fire_count=row["fire_count"],
                after=row["after_id"] or 0,
            )
            self._triggers[trigger.id] = trigger
            if trigger.status == "active":
                try:
                    await self._activate(trigger)
                except TriggerError as err:
                    # A source was removed or renamed since. The dream sees this.
                    await self._kernel.kernel_event("trigger.error", f"trigger {trigger.id} no longer compiles: {err}", entity=trigger.id, **_owner(trigger))
        self._kernel.huvu.publish_triggers()

    async def stop(self) -> None:
        if self._poller:
            self._poller.cancel()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, *([self._poller] if self._poller else []), return_exceptions=True)

    def alive(self) -> list[Trigger]:
        """Every live trigger, for the `huvu_triggers` live table."""
        return [t for t in self._triggers.values() if t.status in LIVE]

    # Registering

    def validate(self, spec: TriggerSpec) -> TriggerSpec:
        """Checks the script and its grants, and returns the spec with
        `expires` resolved to ISO. Raises TriggerError with a fix in the
        message."""
        expires = spec.expires
        try:
            if expires:
                expires = iso(parse_when(expires, self._kernel.now()))
            elif spec.once:
                expires = iso(self._kernel.now() + DEFAULT_EXPIRY)
        except ValueError as err:
            raise TriggerError(f"expires: {err}") from err
        self._kernel.scripts.check(spec.script, spec.grants)
        return spec.model_copy(update={"expires": expires})

    async def _compile(self, spec: TriggerSpec, viewer: Viewer) -> Compiled:
        """What the query reads, found without running it, and checked for
        what its `fire` needs."""
        try:
            compiled = await self._kernel.data.compile(spec.sql, viewer)
        except DataError as err:
            raise TriggerError(f"sql: {err}") from err
        if not compiled.columns:
            raise TriggerError("sql must be a query that returns rows, such as SELECT ...")
        if spec.fire == "rows" and "id" not in compiled.columns:
            raise TriggerError("fire rows tells rows apart by `id`, so the query must return the `id` of a history table's row")
        if not (compiled.tables or compiled.clock):
            raise TriggerError("sql reads nothing that changes: no table and not the time, so it would never fire")
        return compiled

    async def register(self, spec: TriggerSpec, caller: Caller) -> Trigger:
        """Registers a trigger for the caller's user. On a shared device
        nobody is recognized at, it waits, and everyone in the device's
        group is asked on their phone whether it's them."""
        spec = self.validate(spec)
        unconfirmed = caller.user is None
        if unconfirmed and caller.device is None:
            raise TriggerError("a trigger belongs to a person, and nobody is known here to ask; set it up from someone's own conversation")
        # Checked now, in the caller's scope, so a bad query fails here.
        await self._compile(spec, self._kernel.accounts.scope(caller.user, caller.group))
        now = self._kernel.now()
        trigger = Trigger(
            id=f"tr_{secrets.token_hex(4)}",
            spec=spec,
            status="unconfirmed" if unconfirmed else "active",
            armed=True,
            created_at=now,
            created_by=caller.session,
            user=caller.user,
            group=caller.group,
            device=caller.device,
            expires_at=from_iso(spec.expires),
            after=self._kernel.data.last_id(),
        )
        self._db.execute(
            "INSERT INTO triggers (id, spec, status, armed, created_at, created_by, user_id, group_id, device_id, fire_count, after_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (trigger.id, dumps(spec.model_dump()), trigger.status, 1, iso(now), caller.session, trigger.user, trigger.group, trigger.device, trigger.after),
        )
        self._triggers[trigger.id] = trigger
        self._kernel.huvu.publish_triggers()
        await self._kernel.kernel_event(
            "trigger.registered",
            f"trigger {trigger.id} registered ({trigger.user or 'waiting for its person'}): {spec.note}",
            entity=trigger.id,
            **_owner(trigger),
        )
        if unconfirmed:
            await self._kernel.attendance.ask_everyone(trigger.device, "trigger", f"setting up: {spec.note}")
        else:
            await self._go_live(trigger, caller)
        return trigger

    async def dry_run(self, spec: TriggerSpec, caller: Caller) -> dict[str, Any]:
        """What a trigger would do now, without registering it: what its
        query reads, its answer now, and what its script does with that
        answer, with reads real and actions and wakes only recorded."""
        spec = self.validate(spec)
        viewer = self._kernel.accounts.scope(caller.user, caller.group)
        compiled = await self._compile(spec, viewer)
        answer = await self._answer(spec, viewer, None)
        trigger = Trigger(
            id="dry_run",
            spec=spec,
            status="active",
            armed=True,
            created_at=self._kernel.now(),
            created_by=None,
            user=caller.user,
            expires_at=from_iso(spec.expires),
            group=caller.group,
        )
        payload = self._payload(trigger, answer.columns, answer.rows[:DRY_ROWS], answer.labels)
        return {
            "reads": sorted(compiled.tables),
            "runs": _when_it_runs(compiled),
            "columns": answer.columns,
            "rows_now": len(answer.rows),
            "first_rows": payload["rows"],
            "would_fire_now": bool(answer.rows),
            "expires": spec.expires,
            "script": await self._kernel.scripts.run(trigger, payload, dry=True),
        }

    async def _go_live(self, trigger: Trigger, caller: Caller) -> None:
        """Activates a trigger that has its person, or, for an advanced one,
        asks them first. The ask is a card on their phone that waits a day,
        so it works the same from a conversation, a wake or a dream: nobody
        has to be there when the trigger is set."""
        if trigger.spec.tier != "advanced":
            await self._activate(trigger, fresh=True)
            return
        trigger.status = "awaiting"
        self._db.execute("UPDATE triggers SET status = 'awaiting' WHERE id = ?", (trigger.id,))
        self._kernel.huvu.publish_triggers()
        asker = Caller(kind=caller.kind, session=caller.session, trigger=caller.trigger, user=trigger.user, device=caller.device, group=caller.group)
        reason = f"{trigger.spec.note} (wakes the {self._kernel.config.model('advanced')} model)"
        await self._kernel.policy.request("huvu", "activate_trigger", {"trigger": trigger.id}, asker, reason)

    async def approve(self, trigger_id: str) -> Trigger:
        """Its person said yes to an advanced trigger: it goes live."""
        trigger = self._triggers.get(trigger_id)
        if trigger is None or trigger.status not in ("awaiting", "active"):
            raise TriggerError(f"no trigger {trigger_id!r} waiting for a yes")
        if trigger.status == "awaiting":
            trigger.status = "active"
            self._save(trigger)
            await self._kernel.kernel_event("trigger.approved", f"trigger {trigger.id} approved: {trigger.spec.note}", entity=trigger.id, **_owner(trigger))
            await self._activate(trigger, fresh=True)
        return trigger

    async def refused(self, trigger_id: str, how: str) -> None:
        """Its person said no to an advanced trigger, or never answered:
        it goes. One already approved some other way stays."""
        trigger = self._triggers.get(trigger_id)
        if trigger is not None and trigger.status == "awaiting":
            await self._end(trigger, "removed", f"trigger {trigger.id} was {how}: {trigger.spec.note}")

    async def confirm(self, device: str, user: str) -> list[Trigger]:
        """Someone was confirmed at a shared device: its waiting triggers
        are theirs now, and go live."""
        confirmed = [t for t in self._triggers.values() if t.status == "unconfirmed" and t.device == device]
        for trigger in confirmed:
            trigger.user = user
            trigger.status = "active"
            self._db.execute("UPDATE triggers SET status = 'active', user_id = ? WHERE id = ?", (user, trigger.id))
            self._kernel.huvu.publish_triggers()
            await self._kernel.kernel_event("trigger.confirmed", f"trigger {trigger.id} is {user}'s: {trigger.spec.note}", entity=trigger.id, **_owner(trigger))
            try:
                await self._go_live(trigger, Caller(kind="kernel", user=user, device=device))
            except TriggerError as err:
                await self._end(trigger, "removed", f"trigger {trigger.id} doesn't compile for {user}: {err}")
        return confirmed

    async def drop_unconfirmed(self, device: str, reason: str) -> None:
        """Nobody said it was them at the device: its waiting triggers go."""
        for trigger in [t for t in self._triggers.values() if t.status == "unconfirmed" and t.device == device]:
            await self._end(trigger, "removed", f"trigger {trigger.id} was dropped, {reason}: {trigger.spec.note}")

    # Managing

    async def remove(self, trigger_id: str, caller: Caller) -> Trigger:
        trigger = self._triggers.get(trigger_id)
        if trigger is None or trigger not in self.list(caller):
            raise TriggerError(f"no live trigger {trigger_id!r}")
        await self._end(trigger, "removed", f"trigger {trigger.id} removed")
        return trigger

    def get(self, trigger_id: str) -> Trigger | None:
        return self._triggers.get(trigger_id)

    def live(self, trigger_id: str) -> Trigger | None:
        return self._triggers.get(trigger_id)

    def list(self, caller: Caller) -> list[Trigger]:
        """A user's live triggers. On a shared device nobody is recognized
        at, the device's triggers still waiting for their person."""

        def mine(t: Trigger) -> bool:
            return t.user == caller.user if caller.user is not None else (t.user is None and t.device == caller.device)

        return [t for t in self._triggers.values() if t.status in LIVE and mine(t)]

    async def pause(self, trigger_id: str, caller: Caller, reason: str | None = None) -> Trigger:
        """Pauses one of the caller's live triggers: it stops running, and
        keeps everything else. Any session acting for its user may."""
        trigger = self._triggers.get(trigger_id)
        if trigger is None or trigger not in self.list(caller) or trigger.status != "active":
            raise TriggerError(f"no live trigger {trigger_id!r} to pause")
        await self._pause(trigger, "user" if caller.kind == "frontend" else "model", reason or "asked to")
        return trigger

    async def pause_for_limit(self, trigger_id: str, why: str) -> None:
        """A limit: the trigger pauses itself and tells its user."""
        trigger = self._triggers.get(trigger_id)
        if trigger is None or trigger.status != "active":
            return
        await self._pause(trigger, "limit", why)
        await self._kernel.notify(trigger.user, None, f"Paused a trigger, since {why}: {trigger.spec.note}", urgency="high", opens="triggers")

    async def _pause(self, trigger: Trigger, by: PausedBy, reason: str) -> None:
        trigger.status = "paused"
        trigger.paused_by = by
        trigger.compiled = None
        self._save(trigger)
        await self._kernel.kernel_event("trigger.paused", f"trigger {trigger.id} paused by {by}, {reason}: {trigger.spec.note}", entity=trigger.id, **_owner(trigger))

    async def resume(self, trigger_id: str, caller: Caller) -> Trigger:
        trigger = self._triggers.get(trigger_id)
        if trigger is None or trigger not in self.list(caller) or trigger.status != "paused":
            raise TriggerError(f"no paused trigger {trigger_id!r}")
        if trigger.paused_by == "limit" and caller.kind != "frontend":
            raise TriggerError("a limit paused this trigger, so only its person can resume it, from the app; tell them, or change the trigger first")
        trigger.status = "active"
        trigger.paused_by = None
        trigger.resumed_at = self._kernel.now()
        trigger.fires.clear()
        self._save(trigger)
        await self._kernel.kernel_event("trigger.resumed", f"trigger {trigger.id} resumed: {trigger.spec.note}", entity=trigger.id, **_owner(trigger))
        await self._activate(trigger, fresh=True)
        return trigger

    async def expire(self) -> None:
        now = self._kernel.now()
        for trigger in list(self._triggers.values()):
            if trigger.status == "unconfirmed" and now - trigger.created_at > UNCONFIRMED_TTL:
                await self._end(trigger, "expired", f"trigger {trigger.id} was never confirmed: {trigger.spec.note}")
            elif trigger.expires_at and now > trigger.expires_at:
                await self._end(trigger, "expired", f"trigger {trigger.id} expired: {trigger.spec.note}")

    async def _activate(self, trigger: Trigger, *, fresh: bool = False) -> None:
        """Compiles the query in the owner's scope and looks at it once, so
        a condition that already holds fires now. `fresh` (going live, or
        back from a pause) skips the rows recorded until now. A restart
        doesn't: the rows the sources recorded while the triggers loaded
        still count."""
        trigger.compiled = await self._compile(trigger.spec, self._viewer(trigger))
        if fresh:
            trigger.after = self._kernel.data.last_id()
            self._save(trigger)
        await self._look(trigger)

    async def _end(self, trigger: Trigger, status: Status, summary: str) -> None:
        trigger.status = status
        trigger.compiled = None
        self._triggers.pop(trigger.id, None)
        self._save(trigger)
        self._kernel.scripts.remove(trigger.id)
        await self._kernel.kernel_event(f"trigger.{status}", summary, entity=trigger.id, **_owner(trigger))

    def _save(self, trigger: Trigger) -> None:
        self._write(trigger)
        self._kernel.huvu.publish_triggers()

    def _write(self, trigger: Trigger) -> None:
        self._db.execute(
            "UPDATE triggers SET status = ?, armed = ?, last_fired_at = ?, fire_count = ?, paused_by = ?, resumed_at = ?, after_id = ? WHERE id = ?",
            (
                trigger.status,
                int(trigger.armed),
                iso(trigger.last_fired_at),
                trigger.fire_count,
                trigger.paused_by,
                iso(trigger.resumed_at),
                trigger.after,
                trigger.id,
            ),
        )

    def _viewer(self, trigger: Trigger) -> Viewer:
        """What a trigger may read: what its user may. One still waiting for
        its person reads only what anyone at the device may."""
        return self._kernel.accounts.scope(trigger.user, trigger.group)

    # Running

    def nudge(self, table: str) -> None:
        """A table changed (`<source>_<table>`): the triggers that read it
        look now. Several changes in the same moment cause one look."""
        for trigger in list(self._triggers.values()):
            if trigger.status == "active" and trigger.compiled and table in trigger.compiled.tables:
                self._queue(trigger)

    async def tick(self) -> None:
        """A new minute: looks at the triggers that read the time."""
        for trigger in list(self._triggers.values()):
            if trigger.status == "active" and trigger.compiled and trigger.compiled.clock:
                await self._look(trigger)

    async def settle(self) -> None:
        """Waits until every queued look and every run has ended. For tests
        and for a clean stop."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _poll_forever(self) -> None:
        while True:
            await asyncio.sleep(TICK)
            now = self._kernel.now()
            minute = (now.date().isoformat(), now.hour * 60 + now.minute)
            if minute != self._minute:
                try:
                    await self.tick()
                except Exception:
                    log.exception("trigger tick failed")
            self._minute = minute

    def _queue(self, trigger: Trigger) -> None:
        if trigger.id in self._queued:
            return
        self._queued.add(trigger.id)

        async def look() -> None:
            # Yields first, so the rest of the writes of this moment land.
            await asyncio.sleep(0)
            self._queued.discard(trigger.id)
            await self._look(trigger)

        self._spawn(look())

    async def _look(self, trigger: Trigger) -> None:
        """Runs a trigger's query now, and fires it if it should. From the
        look to the end of its script, the trigger is running: a change
        meanwhile is looked at again when the run ends."""
        if trigger.status != "active" or trigger.compiled is None:
            return
        if trigger.id in self._running:
            self._dirty.add(trigger.id)
            return
        self._running.add(trigger.id)
        payload = None
        try:
            payload = await self._decide(trigger)
        finally:
            if payload is None:
                await self._release(trigger)
        if payload is not None:
            self._spawn(self._deliver_and_release(trigger, payload))

    async def _answer(self, spec: TriggerSpec, viewer: Viewer, after: int | None):
        return await self._kernel.data.sql(spec.sql, viewer, SQL_MAX, after=after)

    async def _decide(self, trigger: Trigger) -> dict[str, Any] | None:
        rows_mode = trigger.spec.fire == "rows"
        try:
            answer = await self._answer(trigger.spec, self._viewer(trigger), trigger.after if rows_mode else None)
        except DataError as err:
            # A live table whose source is down, say. Nothing changes, and
            # the same failure is logged once.
            if str(err) != trigger.failing:
                trigger.failing = str(err)
                await self._kernel.kernel_event("trigger.error", f"trigger {trigger.id}'s query failed: {err}", entity=trigger.id, **_owner(trigger))
            return None
        trigger.failing = None
        payload = None
        if rows_mode:
            ids = [row[answer.columns.index("id")] for row in answer.rows]
            if ids:
                trigger.after = max(ids)
                rows = sorted(answer.rows, key=lambda r: r[answer.columns.index("id")])
                payload = await self._fire(trigger, answer.columns, rows, answer.labels)
                if answer.truncated:
                    self._dirty.add(trigger.id)
        elif answer.rows and trigger.armed:
            trigger.armed = False
            payload = await self._fire(trigger, answer.columns, answer.rows, answer.labels)
        elif not answer.rows and not trigger.armed:
            trigger.armed = True
            self._save(trigger)
        return payload

    async def _fire(self, trigger: Trigger, columns: list[str], rows: list[list[Any]], labels: list[Any]) -> dict[str, Any] | None:
        """Marks the trigger fired and logs it, and returns the payload to
        deliver, or None when the fire limit paused it instead."""
        now = self._kernel.now()
        while trigger.fires and now - trigger.fires[0] > FIRE_WINDOW:
            trigger.fires.popleft()
        if len(trigger.fires) >= FIRE_LIMIT:
            self._save(trigger)
            await self.pause_for_limit(trigger.id, f"it fired {len(trigger.fires)} times in {int(FIRE_WINDOW.total_seconds() // 60)} minutes")
            return None
        trigger.fires.append(now)
        trigger.last_fired_at = now
        trigger.fire_count += 1
        self._save(trigger)
        payload = self._payload(trigger, columns, rows, labels)
        await self._kernel.kernel_event(
            "trigger.run",
            f"trigger {trigger.id} fired with {len(rows)} rows: {trigger.spec.note}",
            entity=trigger.id,
            data={"rows": len(rows)},
            **_owner(trigger),
        )
        await self._tell_if_busy(trigger)
        if trigger.spec.once:
            await self._end(trigger, "done", f"trigger {trigger.id} fired once and is done")
        return payload

    def _payload(self, trigger: Trigger, columns: list[str], rows: list[list[Any]], labels: list[Any]) -> dict[str, Any]:
        return {
            "trigger": trigger.id,
            "note": trigger.spec.note,
            "fired_at": self._kernel.now(),
            "columns": columns,
            "rows": [dict(zip(columns, row, strict=True)) for row in rows],
            "labels": labels,
        }

    async def _deliver_and_release(self, trigger: Trigger, payload: dict[str, Any]) -> None:
        try:
            await self._deliver(trigger, payload)
        finally:
            await self._release(trigger)

    async def _release(self, trigger: Trigger) -> None:
        """Ends a run. A change during it gets its look now."""
        self._running.discard(trigger.id)
        if trigger.id in self._dirty:
            self._dirty.discard(trigger.id)
            await self._look(trigger)

    async def _tell_if_busy(self, trigger: Trigger) -> None:
        """Tells a user, once an hour at most, when their triggers fired more
        than BUSY_FIRES times in the last hour."""
        if trigger.user is None:
            return
        since = self._kernel.now() - BUSY_WINDOW
        fires = self._kernel.events_since("trigger.run", since, user=trigger.user)
        told = self._kernel.events_since("triggers.busy", since, user=trigger.user)
        if fires > BUSY_FIRES and not told:
            text = f"Your triggers fired {fires} times in the last hour. Check them, or ask huvu to pause one."
            await self._kernel.kernel_event("triggers.busy", text, user=trigger.user)
            await self._kernel.notify(trigger.user, None, text, opens="triggers")

    async def _deliver(self, trigger: Trigger, payload: dict[str, Any]) -> None:
        try:
            await self._kernel.scripts.run(trigger, payload, dry=False)
        except Exception as err:
            log.exception("trigger %s failed to deliver", trigger.id)
            await self._kernel.kernel_event("trigger.error", f"trigger {trigger.id} failed: {err}", entity=trigger.id, **_owner(trigger))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _when_it_runs(compiled: Compiled) -> list[str]:
    """When a trigger with this query is looked at, in words."""
    when = []
    if compiled.tables:
        when.append(f"at once when {', '.join(sorted(compiled.tables))} changes")
    if compiled.clock:
        when.append("every minute")
    return when


def _owner(trigger: Trigger) -> dict[str, str | None]:
    """Whose a trigger's log rows are: its user's, or, while it waits for
    its person, its group's, from the device it was set up on."""
    return {
        "user": trigger.user,
        "group": None if trigger.user else trigger.group,
        "device": trigger.device,
    }
