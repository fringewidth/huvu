"""Triggers: conditions over source data that wake Hermes when they come true.

Hermes registers a trigger through MCP, and the engine here evaluates it.
Triggers are huvu's only scheduler, and Hermes's cron toolset is off in
every profile. The clock is a source like any other, so "every day at 8"
is a trigger on `clock.time` with `repeat: daily`; a script does what needs
no thought, such as a reminder, and a trigger without one wakes the model.

A trigger's `when` is a flat list, and all of it must hold. A value check
holds while its value matches. An event check holds at the moment the
event arrives, so a trigger with one fires on that event, if every value
check holds then. A trigger without one fires when the whole condition goes
from false to true, whichever part became true last.

`repeat` decides what happens after a fire:
  no:    the trigger is done
  yes:   it rearms once `when` turns false, or once `reset` holds, if given
  daily: it rearms on the next day

Every trigger belongs to a user: its wakes run in that user's Hermes
profile, and its approvals and notifications go to that user's phones. A
trigger set up from a shared device nobody is recognized at has no user
yet, so it waits, unconfirmed, and everyone in the device's group gets "is
this you on the living room TV?" on their phone. The first yes makes that
person the device's attendee (see attendance.py), and the device's waiting
triggers become theirs and go live. If everyone says no, or nobody answers
in time, they are dropped.

A trigger runs once at a time: from deciding to fire to the end of its wake
or script. A fire that comes while it runs is dropped. A value that changed
meanwhile is looked at once more when the run ends, so a condition that went
false in between still rearms it. A trigger whose own action causes the
event it watches finds itself running, so it can't feed itself.

A runaway trigger is held back by counting its model wakes (see wake.py):
from the 10th in two hours the wake tells the model so, and asks it to
handle the repeats in a script or pause the trigger; a 21st doesn't happen,
and the trigger pauses itself and tells its user. A user with more than 10
fires an hour across their triggers is told once an hour. Any session acting
for a trigger's user can pause and resume it; one the limit paused resumes
only by the user's own tap.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, field_validator

from .db import dumps, from_iso, iso, loads
from .events import EventLog, Viewer, sees
from .names import UnknownName, ValueRef
from .spec import Caller, Event, Reading, Strict, ValueSpec, merge_labels
from .state import check_personal, is_fresh
from .times import parse_clock, parse_when
from .wake import WAKE_WINDOW

if TYPE_CHECKING:
    from .kernel import Kernel

log = logging.getLogger("huvu.triggers")

Op = Literal["==", "!=", "<", "<=", ">", ">=", "between", "in", "contains"]
Repeat = Literal["no", "yes", "daily"]
Status = Literal["active", "unconfirmed", "paused", "done", "expired", "removed"]
# Who paused a trigger. One the wake limit paused resumes only by its user.
PausedBy = Literal["model", "user", "limit"]

# A trigger that fires once and names no expiry gets this one, so a
# condition that never comes true doesn't stay forever.
DEFAULT_EXPIRY = timedelta(days=7)
# A user with more fires than this across their triggers in an hour is told.
BUSY_FIRES = 10
BUSY_WINDOW = timedelta(hours=1)
# How long a trigger from a shared device waits for its person, should the
# questions on their phones somehow outlive it.
UNCONFIRMED_TTL = timedelta(minutes=15)
# How many past events a test replays.
REPLAY = 5


class ValueCheck(Strict):
    value: str = Field(description="A value key, such as clock.time, clock.weekday or ha.entity_state.")
    params: dict[str, Any] | None = Field(default=None, description="The value's params, such as {\"entity_id\": \"light.porch\"}.")
    op: Op = Field(description="between takes [from, to] and wraps past midnight; in takes a list.")
    arg: Any = Field(description="What to compare with: \"09:00\" for a time, [\"mon\", \"fri\"] for in.")


class EventCheck(Strict):
    event: str = Field(description="A qualified event type, such as location.arrived.")
    # Fields of the event's `data` that must equal these values.
    where: dict[str, Any] | None = Field(default=None, description="Fields of the event's data that must equal these values.")


class TriggerSpec(Strict):
    """What a model registers."""

    note: str = Field(description="Why the trigger exists and what to do when it fires. It comes back in the wake.")
    when: list[ValueCheck | EventCheck] = Field(
        min_length=1,
        description="Checks that must all hold. A time is a check on clock.time, so every day at 9 is "
        "[{\"value\": \"clock.time\", \"op\": \"==\", \"arg\": \"09:00\"}] with repeat daily.",
    )
    repeat: Repeat = Field(default="no", description="no: fire once. yes: fire again once `when` turns false. daily: at most once a day.")
    reset: list[ValueCheck] | None = None
    include: list[str] = Field(default_factory=list, description="Value keys to bring along in the wake.")
    expires: str | None = Field(default=None, description="2h, tomorrow 09:00 or ISO. A repeat=no trigger without one expires in 7 days.")
    # A Python function `run(ctx)`, run when `when` matches, in place of a
    # plain wake. See scripts.py.
    script: str | None = Field(
        default=None,
        description="Python defining run(ctx), run in place of waking you when `when` matches. Use one whenever the "
        "action needs no thought, such as a reminder. ctx.call(tool, args) calls a tool named in `grants`; "
        "ctx.wake(text) wakes you after all; ctx.log(...) logs. A reminder: "
        "def run(ctx):\n    ctx.call(\"huvu_notify\", {\"text\": \"Water the plants.\"})",
    )
    # The tools the script may call.
    grants: list[str] = Field(default_factory=list, description="The tools the script may call, such as [\"huvu_notify\"].")

    @field_validator("note")
    @classmethod
    def _note(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("note must say why the trigger exists and what to do")
        return v


@dataclass
class Check:
    """A value check after resolution."""

    raw: ValueCheck
    ref: ValueRef
    spec: ValueSpec


@dataclass
class Trigger:
    id: str
    spec: TriggerSpec
    status: Status
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
    # Filled in by the engine when the trigger is active.
    checks: list[Check] = field(default_factory=list)
    resets: list[Check] = field(default_factory=list)
    event: EventCheck | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user": self.user,
            "group": self.group,
            "device": self.device,
            "paused_by": self.paused_by,
            "note": self.spec.note,
            "when": [c.model_dump(exclude_none=True) for c in self.spec.when],
            "repeat": self.spec.repeat,
            "reset": [c.model_dump(exclude_none=True) for c in self.spec.reset] if self.spec.reset else None,
            "include": self.spec.include or None,
            "script": bool(self.spec.script),
            "grants": self.spec.grants or None,
            "status": self.status,
            "armed": self.armed,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "last_fired_at": self.last_fired_at,
            "fire_count": self.fire_count,
        }


class TriggerError(ValueError):
    pass


def compare(op: Op, value: Any, arg: Any, kind: str) -> bool:
    """One value check. Times of day compare as minutes; `between` on times
    wraps past midnight, so 22:00 to 05:00 works."""
    coerce = {"time": _minutes, "number": float}.get(kind, lambda x: x)
    if op != "contains":
        value = coerce(value)
        arg = [coerce(a) for a in arg] if isinstance(arg, list) else coerce(arg)
    checks = {
        "==": lambda: value == arg,
        "!=": lambda: value != arg,
        "<": lambda: value < arg,
        "<=": lambda: value <= arg,
        ">": lambda: value > arg,
        ">=": lambda: value >= arg,
        "between": lambda: _between(value, arg, wraps=kind == "time"),
        "in": lambda: value in arg,
        "contains": lambda: arg in value,
    }
    return bool(checks[op]())


def _between(value: Any, arg: list[Any], wraps: bool) -> bool:
    start, end = arg
    inside = start <= value < end
    if wraps and start > end:
        inside = value >= start or value < end
    return inside


def _minutes(text: Any) -> int:
    t = parse_clock(str(text))
    return t.hour * 60 + t.minute


class TriggerEngine:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        self._triggers: dict[str, Trigger] = {}
        self._by_ref: dict[ValueRef, set[str]] = {}
        self._by_event: dict[str, set[str]] = {}
        # Triggers in the middle of a run, and value triggers whose inputs
        # changed during one.
        self._running: set[str] = set()
        self._dirty: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def _db(self):
        return self._kernel.db

    async def load(self) -> None:
        """Reloads stored triggers and activates the live ones."""
        rows = self._db.execute("SELECT * FROM triggers WHERE status IN ('active', 'unconfirmed', 'paused')").fetchall()
        for row in rows:
            trigger = Trigger(
                id=row["id"],
                spec=TriggerSpec.model_validate(loads(row["spec"])),
                status=row["status"],
                armed=bool(row["armed"]),
                created_at=from_iso(row["created_at"]),
                created_by=row["created_by"],
                user=row["user_id"],
                group=row["group_id"],
                expires_at=from_iso(loads(row["spec"]).get("expires")),
                device=row["device_id"],
                paused_by=row["paused_by"],
                resumed_at=from_iso(row["resumed_at"]),
                last_fired_at=from_iso(row["last_fired_at"]),
                fire_count=row["fire_count"],
            )
            try:
                self._resolve(trigger)
            except (TriggerError, UnknownName) as err:
                # A source was removed or renamed since. The dream sees this.
                await self._kernel.kernel_event("trigger.error", f"trigger {trigger.id} no longer resolves: {err}", entity=trigger.id, **_owner(trigger))
                continue
            self._triggers[trigger.id] = trigger
            if trigger.status == "active":
                await self._activate(trigger)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def validate(self, spec: TriggerSpec, user: str | None = None) -> TriggerSpec:
        """Checks every name and argument, for a trigger of `user`, and
        returns the spec with `expires` resolved to ISO. Raises TriggerError
        with a fix in the message."""
        trigger = Trigger(
            id="check",
            spec=spec,
            status="active",
            armed=True,
            created_at=self._kernel.now(),
            created_by=None,
            user=user,
            expires_at=None,
        )
        self._resolve(trigger)
        expires = spec.expires
        if expires:
            expires = iso(parse_when(expires, self._kernel.now()))
        elif spec.repeat == "no":
            expires = iso(self._kernel.now() + DEFAULT_EXPIRY)
        if spec.script is not None:
            self._kernel.scripts.check(spec.script, spec.grants)
        return spec.model_copy(update={"expires": expires})

    def _resolve(self, trigger: Trigger) -> None:
        registry = self._kernel.registry
        events = [c for c in trigger.spec.when if isinstance(c, EventCheck)]
        if len(events) > 1:
            raise TriggerError("a trigger can check one event at most; two events never arrive at the same moment")
        viewer = self._viewer(trigger)
        trigger.checks = [self._check(c, viewer) for c in trigger.spec.when if isinstance(c, ValueCheck)]
        trigger.resets = [self._check(c, viewer) for c in trigger.spec.reset or []]
        trigger.event = events[0] if events else None
        if trigger.event:
            event_spec = registry.resolve_event(trigger.event.event)
            unknown = [k for k in (trigger.event.where or {}) if event_spec.data and k not in event_spec.data]
            if unknown:
                raise TriggerError(f"{trigger.event.event} has no data field {unknown[0]!r}; it has {', '.join(event_spec.data)}")
        for key in trigger.spec.include:
            check_personal(*registry.resolve_value(key), viewer)

    def _viewer(self, trigger: Trigger) -> Viewer:
        """What a trigger may read and see: what its user may. One still
        waiting for its person reads only what anyone at the device may."""
        return self._kernel.accounts.scope(trigger.user, trigger.group)

    def _check(self, raw: ValueCheck, viewer: Viewer) -> Check:
        ref, spec = self._kernel.registry.resolve_value(raw.value, raw.params)
        check_personal(ref, spec, viewer)
        if raw.op == "between" and not (isinstance(raw.arg, list) and len(raw.arg) == 2):
            raise TriggerError(f"{ref.key}: between takes [start, end]")
        if raw.op == "in" and not isinstance(raw.arg, list):
            raise TriggerError(f"{ref.key}: in takes a list")
        try:
            sample = raw.arg[0] if isinstance(raw.arg, list) else raw.arg
            if spec.type == "time":
                for a in raw.arg if isinstance(raw.arg, list) else [raw.arg]:
                    parse_clock(str(a))
            elif spec.type == "number" and raw.op != "in":
                float(sample)
        except (ValueError, TypeError) as err:
            raise TriggerError(f"{ref.key} is a {spec.type}: {err}") from err
        return Check(raw, ref, spec)

    async def register(self, spec: TriggerSpec, caller: Caller) -> Trigger:
        """Registers a trigger for the caller's user. On a shared device
        nobody is recognized at, it waits, and everyone in the device's
        group is asked on their phone whether it's them."""
        spec = self.validate(spec, caller.user)
        unconfirmed = caller.user is None
        if unconfirmed and caller.device is None:
            raise TriggerError("a trigger belongs to a person, and nobody is known here to ask; set it up from someone's own conversation")
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
        )
        self._resolve(trigger)
        self._db.execute(
            "INSERT INTO triggers (id, spec, status, armed, created_at, created_by, user_id, group_id, device_id, fire_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (trigger.id, dumps(spec.model_dump()), trigger.status, 1, iso(now), caller.session, trigger.user, trigger.group, trigger.device),
        )
        self._triggers[trigger.id] = trigger
        await self._kernel.kernel_event(
            "trigger.registered",
            f"trigger {trigger.id} registered ({trigger.user or 'waiting for its person'}): {spec.note}",
            entity=trigger.id,
            **_owner(trigger),
        )
        if unconfirmed:
            await self._kernel.attendance.ask_everyone(trigger.device, "trigger", f"setting up: {spec.note}")
        else:
            await self._activate(trigger)
        return trigger

    async def confirm(self, device: str, user: str) -> list[Trigger]:
        """Someone was confirmed at a shared device: its waiting triggers
        are theirs now, and go live."""
        confirmed = [t for t in self._triggers.values() if t.status == "unconfirmed" and t.device == device]
        for trigger in confirmed:
            trigger.user = user
            trigger.status = "active"
            self._db.execute("UPDATE triggers SET status = 'active', user_id = ? WHERE id = ?", (user, trigger.id))
            await self._kernel.kernel_event("trigger.confirmed", f"trigger {trigger.id} is {user}'s: {trigger.spec.note}", entity=trigger.id, **_owner(trigger))
            await self._activate(trigger)
        return confirmed

    async def drop_unconfirmed(self, device: str, reason: str) -> None:
        """Nobody said it was them at the device: its waiting triggers go."""
        for trigger in [t for t in self._triggers.values() if t.status == "unconfirmed" and t.device == device]:
            await self._end(trigger, "removed", f"trigger {trigger.id} was dropped, {reason}: {trigger.spec.note}")

    async def remove(self, trigger_id: str, caller: Caller) -> Trigger:
        trigger = self._triggers.get(trigger_id)
        if trigger is None or trigger not in self.list(caller):
            raise TriggerError(f"no live trigger {trigger_id!r}")
        await self._end(trigger, "removed", f"trigger {trigger.id} removed")
        return trigger

    def get(self, trigger_id: str) -> Trigger | None:
        return self._triggers.get(trigger_id)

    def list(self, caller: Caller) -> list[Trigger]:
        """A user's live triggers. On a shared device nobody is recognized
        at, the device's triggers still waiting for their person."""

        def mine(t: Trigger) -> bool:
            return t.user == caller.user if caller.user is not None else (t.user is None and t.device == caller.device)

        return [t for t in self._triggers.values() if t.status in ("active", "unconfirmed", "paused") and mine(t)]

    async def pause(self, trigger_id: str, caller: Caller, reason: str | None = None) -> Trigger:
        """Pauses one of the caller's live triggers: it stops watching, and
        keeps everything else. Any session acting for its user may."""
        trigger = self._triggers.get(trigger_id)
        if trigger is None or trigger not in self.list(caller) or trigger.status != "active":
            raise TriggerError(f"no live trigger {trigger_id!r} to pause")
        await self._pause(trigger, "user" if caller.kind == "frontend" else "model", reason or "asked to")
        return trigger

    async def pause_for_limit(self, trigger_id: str, wakes: int) -> None:
        """The wake limit: the trigger pauses itself and tells its user."""
        trigger = self._triggers.get(trigger_id)
        if trigger is None or trigger.status != "active":
            return
        reason = f"it woke the model {wakes} times in {int(WAKE_WINDOW.total_seconds() // 3600)} hours"
        await self._pause(trigger, "limit", reason)
        await self._kernel.notify(trigger.user, None, f"Paused a trigger, since {reason}: {trigger.spec.note}", urgency="high", opens="triggers")

    async def _pause(self, trigger: Trigger, by: PausedBy, reason: str) -> None:
        trigger.status = "paused"
        trigger.paused_by = by
        self._deactivate(trigger)
        self._save(trigger)
        await self._kernel.kernel_event("trigger.paused", f"trigger {trigger.id} paused by {by}, {reason}: {trigger.spec.note}", entity=trigger.id, **_owner(trigger))

    async def resume(self, trigger_id: str, caller: Caller) -> Trigger:
        trigger = self._triggers.get(trigger_id)
        if trigger is None or trigger not in self.list(caller) or trigger.status != "paused":
            raise TriggerError(f"no paused trigger {trigger_id!r}")
        if trigger.paused_by == "limit" and caller.kind != "frontend":
            raise TriggerError("the wake limit paused this trigger, so only its person can resume it, from the app; tell them, or change the trigger first")
        trigger.status = "active"
        trigger.paused_by = None
        trigger.resumed_at = self._kernel.now()
        self._save(trigger)
        await self._kernel.kernel_event("trigger.resumed", f"trigger {trigger.id} resumed: {trigger.spec.note}", entity=trigger.id, **_owner(trigger))
        await self._activate(trigger)
        return trigger

    def live(self, trigger_id: str) -> Trigger | None:
        return self._triggers.get(trigger_id)

    def _get(self, trigger_id: str) -> Trigger:
        trigger = self._triggers.get(trigger_id)
        if trigger is None:
            raise TriggerError(f"no live trigger {trigger_id!r}")
        return trigger

    async def _activate(self, trigger: Trigger) -> None:
        for check in [*trigger.checks, *trigger.resets]:
            ids = self._by_ref.setdefault(check.ref, set())
            if not ids:
                await self._kernel.state.watch(check.ref, self._on_value)
            ids.add(trigger.id)
        if trigger.event:
            self._by_event.setdefault(trigger.event.event, set()).add(trigger.id)
        else:
            # A condition that already holds fires now: the state before
            # registration counts as false.
            await self._look(trigger, None)

    def _deactivate(self, trigger: Trigger) -> None:
        for check in [*trigger.checks, *trigger.resets]:
            ids = self._by_ref.get(check.ref, set())
            ids.discard(trigger.id)
            if not ids:
                self._by_ref.pop(check.ref, None)
                self._kernel.state.unwatch(check.ref, self._on_value)
        if trigger.event:
            self._by_event.get(trigger.event.event, set()).discard(trigger.id)

    async def _end(self, trigger: Trigger, status: Status, summary: str) -> None:
        trigger.status = status
        self._deactivate(trigger)
        self._triggers.pop(trigger.id, None)
        self._save(trigger)
        await self._kernel.kernel_event(f"trigger.{status}", summary, entity=trigger.id, **_owner(trigger))

    def _save(self, trigger: Trigger) -> None:
        self._db.execute(
            "UPDATE triggers SET status = ?, armed = ?, last_fired_at = ?, fire_count = ?, paused_by = ?, resumed_at = ? WHERE id = ?",
            (trigger.status, int(trigger.armed), iso(trigger.last_fired_at), trigger.fire_count, trigger.paused_by, iso(trigger.resumed_at), trigger.id),
        )

    async def _on_value(self, ref: ValueRef, reading: Reading) -> None:
        for trigger_id in list(self._by_ref.get(ref, ())):
            trigger = self._triggers.get(trigger_id)
            if trigger and trigger.status == "active" and trigger.event is None:
                await self._look(trigger, None)
            elif trigger and trigger.status == "active":
                self._rearm(trigger, holds=None)

    async def on_event(self, event: Event) -> None:
        for trigger_id in list(self._by_event.get(event.type, ())):
            trigger = self._triggers.get(trigger_id)
            if trigger is None or trigger.status != "active" or trigger.event is None:
                continue
            where = trigger.event.where or {}
            # Someone else's event (where they arrived) never reaches a
            # trigger of a user who couldn't see it in the log.
            if all(event.data.get(k) == v for k, v in where.items()) and sees(self._viewer(trigger), event):
                await self._look(trigger, event)

    async def _look(self, trigger: Trigger, event: Event | None) -> None:
        """Looks at a trigger now, in order with what changed, and fires it
        if it should. From the look to the end of its wake or script, the
        trigger is running: an event for it meanwhile is dropped, and a value
        change is looked at again when the run ends."""
        if trigger.id in self._running:
            if event is None:
                self._dirty.add(trigger.id)
            else:
                log.info("trigger %s is still running; dropped %s", trigger.id, event.type)
            return
        self._running.add(trigger.id)
        payload = None
        try:
            payload = await (self._decide_event(trigger, event) if event else self._decide_value(trigger))
        finally:
            if payload is None:
                await self._release(trigger)
        if payload is not None:
            self._spawn(self._deliver_and_release(trigger, payload))

    async def _decide_event(self, trigger: Trigger, event: Event) -> dict[str, Any] | None:
        self._rearm(trigger, holds=None)
        holds, _, _ = await self._holds(trigger.checks)
        return await self._fire(trigger, event) if trigger.armed and holds else None

    async def _decide_value(self, trigger: Trigger) -> dict[str, Any] | None:
        holds, readings, _ = await self._holds(trigger.checks)
        # Unknown counts as false for firing, but not for rearming: an input
        # nobody has reported (after a driver restarts, say) hasn't turned
        # the condition false.
        now = self._kernel.now()
        known = all(is_fresh(readings[c.ref.key], c.spec, now) for c in trigger.checks)
        self._rearm(trigger, holds if (holds or known) else None)
        return await self._fire(trigger, None) if holds and trigger.armed else None

    async def _deliver_and_release(self, trigger: Trigger, payload: dict[str, Any]) -> None:
        try:
            await self._deliver(trigger, payload)
        finally:
            await self._release(trigger)

    async def _release(self, trigger: Trigger) -> None:
        """Ends a run. A value that changed during it gets its look now."""
        self._running.discard(trigger.id)
        if trigger.id in self._dirty:
            self._dirty.discard(trigger.id)
            if trigger.status == "active":
                await self._look(trigger, None)

    def _rearm(self, trigger: Trigger, holds: bool | None) -> None:
        """Sets `armed` from the repeat mode. `holds` is the condition now,
        or None when it doesn't apply (event triggers) or isn't known."""
        if trigger.armed or trigger.spec.repeat == "no":
            return
        now = self._kernel.now()
        rearm = False
        if trigger.spec.repeat == "daily":
            rearm = trigger.last_fired_at is None or trigger.last_fired_at.date() < now.date()
        elif trigger.resets:
            rearm = all(self._holds_now(c) for c in trigger.resets)
        elif trigger.event is not None:
            rearm = True
        elif holds is False:
            rearm = True
        if rearm:
            trigger.armed = True
            self._save(trigger)

    def _holds_now(self, check: Check) -> bool:
        reading = self._kernel.state.cached(check.ref)
        ok = is_fresh(reading, check.spec, self._kernel.now())
        return ok and _safe_compare(check, reading.value)

    async def _holds(self, checks: list[Check]) -> tuple[bool, dict[str, Reading], dict[str, bool]]:
        """Whether every check holds, the readings it used, and each check's
        own result. A missing or stale reading counts as unknown, and unknown
        counts as false."""
        readings: dict[str, Reading] = {}
        results: dict[str, bool] = {}
        now = self._kernel.now()
        for check in checks:
            reading = self._kernel.state.cached(check.ref)
            if reading is None or check.spec.updates == "on_demand":
                reading = await self._kernel.state.read(check.ref, check.spec)
            readings[check.ref.key] = reading
            results[check.ref.key] = is_fresh(reading, check.spec, now) and _safe_compare(check, reading.value)
        return all(results.values()), readings, results

    async def _fire(self, trigger: Trigger, event: Event | None) -> dict[str, Any]:
        """Marks the trigger fired and logs it, and returns the payload to
        deliver."""
        now = self._kernel.now()
        _, matched, _ = await self._holds(trigger.checks)
        included = await self._kernel.state.get(trigger.spec.include, self._viewer(trigger))
        labels = merge_labels(*(r.labels for r in matched.values()), *(r.labels for r in included.values()), event.labels if event else [])
        trigger.armed = False
        trigger.last_fired_at = now
        trigger.fire_count += 1
        self._save(trigger)
        payload = {
            "trigger": trigger.id,
            "note": trigger.spec.note,
            "fired_at": now,
            "event": event.model_dump() if event else None,
            "matched": {k: r.model_dump() for k, r in matched.items()},
            "include": {k: r.model_dump() for k, r in included.items()},
            "labels": labels,
        }
        await self._kernel.kernel_event(
            "trigger.run",
            f"trigger {trigger.id} fired: {trigger.spec.note}",
            entity=trigger.id, **_owner(trigger),
            refs={"event": event.id} if event else {},
            labels=labels,
        )
        await self._tell_if_busy(trigger)
        if trigger.spec.repeat == "no":
            await self._end(trigger, "done", f"trigger {trigger.id} fired once and is done")
        return payload

    async def _tell_if_busy(self, trigger: Trigger) -> None:
        """Tells a user, once an hour at most, when their triggers fired more
        than BUSY_FIRES times in the last hour."""
        if trigger.user is None:
            return
        since = self._kernel.now() - BUSY_WINDOW
        fires = self._kernel.events.count(type="trigger.run", user=trigger.user, since=since)
        told = self._kernel.events.count(type="triggers.busy", user=trigger.user, since=since)
        if fires > BUSY_FIRES and not told:
            text = f"Your triggers fired {fires} times in the last hour. Check them, or ask huvu to pause one."
            await self._kernel.kernel_event("triggers.busy", text, user=trigger.user)
            await self._kernel.notify(trigger.user, None, text, opens="triggers")

    async def _deliver(self, trigger: Trigger, payload: dict[str, Any]) -> None:
        try:
            if trigger.spec.script:
                await self._kernel.scripts.run(trigger, payload, dry=False)
            else:
                await self._kernel.waker.wake(payload, trigger.user)
        except Exception as err:
            log.exception("trigger %s failed to deliver", trigger.id)
            await self._kernel.kernel_event("trigger.error", f"trigger {trigger.id} failed: {err}", entity=trigger.id, **_owner(trigger))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def expire(self) -> None:
        now = self._kernel.now()
        for trigger in list(self._triggers.values()):
            if trigger.status == "unconfirmed" and now - trigger.created_at > UNCONFIRMED_TTL:
                await self._end(trigger, "expired", f"trigger {trigger.id} was never confirmed: {trigger.spec.note}")
            elif trigger.expires_at and now > trigger.expires_at:
                await self._end(trigger, "expired", f"trigger {trigger.id} expired: {trigger.spec.note}")

    async def test(self, spec: TriggerSpec, user: str | None) -> dict[str, Any]:
        """What a trigger would do now, without registering it: each check's
        current reading and result, the last few events an event check
        would have matched, and what its script would have done for `user`."""
        spec = self.validate(spec, user)
        trigger = Trigger(
            id="test",
            spec=spec,
            status="active",
            armed=True,
            created_at=self._kernel.now(),
            created_by=None,
            user=user,
            expires_at=from_iso(spec.expires),
        )
        self._resolve(trigger)
        holds, readings, results = await self._holds(trigger.checks)
        checks = [
            {"value": c.ref.key, "op": c.raw.op, "arg": c.raw.arg, "reading": readings[c.ref.key], "holds": results[c.ref.key]}
            for c in trigger.checks
        ]
        report: dict[str, Any] = {"checks": checks, "values_hold": holds, "expires": spec.expires}
        replay = self._replay(trigger) if trigger.event else []
        if trigger.event:
            report["recent_matching_events"] = [e.model_dump() for e in replay]
        if spec.script:
            report["script_runs"] = await self._dry_runs(trigger, replay, readings)
        return report

    def _replay(self, trigger: Trigger) -> list[Event]:
        assert trigger.event is not None
        log_: EventLog = self._kernel.events
        recent = log_.search(type=trigger.event.event, limit=50, viewer=self._viewer(trigger))
        where = trigger.event.where or {}
        return [e for e in recent if all(e.data.get(k) == v for k, v in where.items())][:REPLAY]

    async def _dry_runs(self, trigger: Trigger, replay: list[Event], readings: dict[str, Reading]) -> list[dict[str, Any]]:
        included = await self._kernel.state.get(trigger.spec.include, self._viewer(trigger))
        base = {
            "trigger": trigger.id,
            "note": trigger.spec.note,
            "fired_at": self._kernel.now(),
            "matched": {k: r.model_dump() for k, r in readings.items()},
            "include": {k: r.model_dump() for k, r in included.items()},
            "labels": [],
        }
        payloads = [{**base, "event": e.model_dump()} for e in replay] or [{**base, "event": None}]
        return [await self._kernel.scripts.run(trigger, p, dry=True) for p in payloads]


def _safe_compare(check: Check, value: Any) -> bool:
    try:
        ok = value is not None and compare(check.raw.op, value, check.raw.arg, check.spec.type)
    except (ValueError, TypeError):
        ok = False
    return ok


def _owner(trigger: Trigger) -> dict[str, str | None]:
    """Whose a trigger's events are: its user's, or, while it waits for its
    person, its group's, from the device it was set up on."""
    return {
        "user": trigger.user,
        "group": None if trigger.user else trigger.group,
        "device": trigger.device,
    }
