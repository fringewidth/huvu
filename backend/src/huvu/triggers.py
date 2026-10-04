"""Triggers: conditions over source data that wake Hermes when they come true.

Hermes registers a trigger through MCP, and the engine here evaluates it.
Time-only schedules are Hermes cron jobs, not triggers; the clock exists
here only as one condition inside a data trigger.

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
trigger set up from a household device has no user yet, so it waits,
unclaimed. The model tells the room a two-digit number, every user's phone
offers three numbers, and whoever taps the right one owns the trigger. A
wrong tap drops it, and an unclaimed trigger expires after CLAIM_TTL.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, field_validator

from .accounts import AuthError, Device
from .db import dumps, from_iso, iso, loads
from .events import EventLog
from .names import UnknownName, ValueRef
from .spec import Caller, Event, Reading, Strict, ValueSpec, merge_labels
from .state import is_fresh
from .times import parse_clock, parse_when

if TYPE_CHECKING:
    from .kernel import Kernel

log = logging.getLogger("huvu.triggers")

Op = Literal["==", "!=", "<", "<=", ">", ">=", "between", "in", "contains"]
Repeat = Literal["no", "yes", "daily"]
Status = Literal["active", "unclaimed", "done", "expired", "removed"]

# A trigger that fires once and names no expiry gets this one, so a
# condition that never comes true doesn't stay forever.
DEFAULT_EXPIRY = timedelta(days=7)
# How long a trigger from a household device waits for someone to claim it.
CLAIM_TTL = timedelta(minutes=10)
CLAIM_CHOICES = 3
# How many past events a test replays.
REPLAY = 5


class ValueCheck(Strict):
    value: str
    params: dict[str, Any] | None = None
    op: Op
    arg: Any


class EventCheck(Strict):
    event: str
    # Fields of the event's `data` that must equal these values.
    where: dict[str, Any] | None = None


class TriggerSpec(Strict):
    """What a model registers."""

    note: str = Field(description="Why the trigger exists and what to do when it fires. It comes back in the wake.")
    when: list[ValueCheck | EventCheck] = Field(min_length=1)
    repeat: Repeat = "no"
    reset: list[ValueCheck] | None = None
    include: list[str] = Field(default_factory=list)
    expires: str | None = None
    # A Python function `run(ctx)`, run when `when` matches, in place of a
    # plain wake. See scripts.py.
    script: str | None = None
    # The tools the script may call.
    grants: list[str] = Field(default_factory=list)

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
class Claim:
    """The number a household device shows, and the numbers every phone
    offers, one of them right."""

    number: int
    choices: list[int]


@dataclass
class Trigger:
    id: str
    spec: TriggerSpec
    status: Status
    armed: bool
    created_at: datetime
    created_by: str | None
    # None only while unclaimed.
    user: str | None
    expires_at: datetime | None
    claim: Claim | None = None
    last_fired_at: datetime | None = None
    fire_count: int = 0
    # Filled in by the engine when the trigger is active.
    checks: list[Check] = field(default_factory=list)
    resets: list[Check] = field(default_factory=list)
    event: EventCheck | None = None

    def claim_public(self) -> dict[str, Any]:
        """What a phone shows: the note and the choices, never the answer."""
        return {
            "trigger": self.id,
            "note": self.spec.note,
            "choices": self.claim.choices if self.claim else [],
            "expires_at": self.created_at + CLAIM_TTL,
        }

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user": self.user,
            "claim": self.claim.number if self.claim else None,
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
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def _db(self):
        return self._kernel.db

    async def load(self) -> None:
        """Reloads stored triggers and activates the live ones."""
        rows = self._db.execute("SELECT * FROM triggers WHERE status IN ('active', 'unclaimed')").fetchall()
        for row in rows:
            claim = loads(row["claim"])
            trigger = Trigger(
                id=row["id"],
                spec=TriggerSpec.model_validate(loads(row["spec"])),
                status=row["status"],
                armed=bool(row["armed"]),
                created_at=from_iso(row["created_at"]),
                created_by=row["created_by"],
                user=row["user_id"],
                expires_at=from_iso(loads(row["spec"]).get("expires")),
                claim=Claim(**claim) if claim else None,
                last_fired_at=from_iso(row["last_fired_at"]),
                fire_count=row["fire_count"],
            )
            try:
                self._resolve(trigger)
            except (TriggerError, UnknownName) as err:
                # A source was removed or renamed since. The dream sees this.
                await self._kernel.kernel_event("trigger.error", f"trigger {trigger.id} no longer resolves: {err}", entity=trigger.id, user=trigger.user, device=_device(self._kernel, trigger))
                continue
            self._triggers[trigger.id] = trigger
            if trigger.status == "active":
                await self._activate(trigger)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def validate(self, spec: TriggerSpec) -> TriggerSpec:
        """Checks every name and argument, and returns the spec with
        `expires` resolved to ISO. Raises TriggerError with a fix in the
        message."""
        trigger = Trigger(
            id="check",
            spec=spec,
            status="active",
            armed=True,
            created_at=self._kernel.now(),
            created_by=None,
            user=None,
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
        trigger.checks = [self._check(c) for c in trigger.spec.when if isinstance(c, ValueCheck)]
        trigger.resets = [self._check(c) for c in trigger.spec.reset or []]
        trigger.event = events[0] if events else None
        if trigger.event:
            event_spec = registry.resolve_event(trigger.event.event)
            unknown = [k for k in (trigger.event.where or {}) if event_spec.data and k not in event_spec.data]
            if unknown:
                raise TriggerError(f"{trigger.event.event} has no data field {unknown[0]!r}; it has {', '.join(event_spec.data)}")
        for key in trigger.spec.include:
            registry.resolve_value(key)

    def _check(self, raw: ValueCheck) -> Check:
        ref, spec = self._kernel.registry.resolve_value(raw.value, raw.params)
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
        """Registers a trigger for the caller's user. Without one (a
        household device), it waits to be claimed."""
        spec = self.validate(spec)
        now = self._kernel.now()
        unclaimed = caller.user is None
        trigger = Trigger(
            id=f"tr_{secrets.token_hex(4)}",
            spec=spec,
            status="unclaimed" if unclaimed else "active",
            armed=True,
            created_at=now,
            created_by=caller.session,
            user=caller.user,
            expires_at=from_iso(spec.expires),
            claim=_new_claim() if unclaimed else None,
        )
        self._resolve(trigger)
        self._db.execute(
            "INSERT INTO triggers (id, spec, status, armed, created_at, created_by, user_id, claim, fire_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                trigger.id,
                dumps(spec.model_dump()),
                trigger.status,
                1,
                iso(now),
                caller.session,
                trigger.user,
                dumps(vars(trigger.claim)) if trigger.claim else None,
            ),
        )
        self._triggers[trigger.id] = trigger
        await self._kernel.kernel_event(
            "trigger.registered", f"trigger {trigger.id} registered ({trigger.user or 'unclaimed'}): {spec.note}", entity=trigger.id, user=trigger.user, device=_device(self._kernel, trigger)
        )
        if unclaimed:
            self._kernel.push.send("claim", trigger.claim_public(), self._kernel.accounts.phones(None))
        else:
            await self._activate(trigger)
        return trigger

    async def claim(self, trigger_id: str, number: int, by: Device) -> Trigger:
        """A user claims a household device's trigger from their phone, by
        tapping the number the device showed. A wrong number drops it."""
        trigger = self._triggers.get(trigger_id)
        if trigger is None or trigger.status != "unclaimed" or trigger.claim is None:
            raise TriggerError(f"no trigger {trigger_id!r} is waiting to be claimed")
        if not by.is_phone or by.user is None:
            raise AuthError("claim a trigger from your phone")
        if number != trigger.claim.number:
            await self._end(trigger, "removed", f"trigger {trigger.id} was dropped: the wrong number was tapped")
            self._claim_done(trigger)
            raise TriggerError("that wasn't the number on the device, so the trigger was dropped")
        trigger.user = by.user
        trigger.claim = None
        trigger.status = "active"
        self._db.execute("UPDATE triggers SET status = 'active', user_id = ?, claim = NULL WHERE id = ?", (trigger.user, trigger.id))
        await self._kernel.kernel_event("trigger.claimed", f"trigger {trigger.id} claimed by {trigger.user}: {trigger.spec.note}", entity=trigger.id, user=trigger.user, device=_device(self._kernel, trigger))
        self._claim_done(trigger)
        await self._activate(trigger)
        return trigger

    def claims(self) -> list[dict[str, Any]]:
        """Every trigger waiting to be claimed, as phones show it."""
        return [t.claim_public() for t in self._triggers.values() if t.status == "unclaimed"]

    def _claim_done(self, trigger: Trigger) -> None:
        """Tells every phone the claim is over, so they drop it."""
        self._kernel.push.send("claim.done", {"trigger": trigger.id, "user": trigger.user}, self._kernel.accounts.phones(None))

    async def remove(self, trigger_id: str, user: str | None) -> Trigger:
        trigger = self._triggers.get(trigger_id)
        if trigger is None or trigger.user != user:
            raise TriggerError(f"no live trigger {trigger_id!r}")
        await self._end(trigger, "removed", f"trigger {trigger.id} removed")
        return trigger

    def list(self, user: str | None) -> list[Trigger]:
        """A user's live triggers. For the household, the ones still waiting
        to be claimed."""
        return [t for t in self._triggers.values() if t.user == user and t.status in ("active", "unclaimed")]

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
            await self._evaluate(trigger)

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
        await self._kernel.kernel_event(f"trigger.{status}", summary, entity=trigger.id, user=trigger.user, device=_device(self._kernel, trigger))

    def _save(self, trigger: Trigger) -> None:
        self._db.execute(
            "UPDATE triggers SET status = ?, armed = ?, last_fired_at = ?, fire_count = ? WHERE id = ?",
            (trigger.status, int(trigger.armed), iso(trigger.last_fired_at), trigger.fire_count, trigger.id),
        )

    async def _on_value(self, ref: ValueRef, reading: Reading) -> None:
        for trigger_id in list(self._by_ref.get(ref, ())):
            trigger = self._triggers.get(trigger_id)
            if trigger and trigger.status == "active" and trigger.event is None:
                await self._evaluate(trigger)
            elif trigger and trigger.status == "active":
                self._rearm(trigger, holds=None)

    async def on_event(self, event: Event) -> None:
        for trigger_id in list(self._by_event.get(event.type, ())):
            trigger = self._triggers.get(trigger_id)
            if trigger is None or trigger.status != "active" or trigger.event is None:
                continue
            async with self._lock:
                where = trigger.event.where or {}
                matches = all(event.data.get(k) == v for k, v in where.items())
                self._rearm(trigger, holds=None)
                holds, _, _ = await self._holds(trigger.checks)
                if matches and trigger.armed and holds:
                    await self._fire(trigger, event)

    async def _evaluate(self, trigger: Trigger) -> None:
        async with self._lock:
            holds, _, _ = await self._holds(trigger.checks)
            self._rearm(trigger, holds)
            if holds and trigger.armed:
                await self._fire(trigger, None)

    def _rearm(self, trigger: Trigger, holds: bool | None) -> None:
        """Sets `armed` from the repeat mode. `holds` is the condition now,
        or None when it doesn't apply (event triggers)."""
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

    async def _fire(self, trigger: Trigger, event: Event | None) -> None:
        now = self._kernel.now()
        _, matched, _ = await self._holds(trigger.checks)
        included = await self._kernel.state.get(trigger.spec.include)
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
            entity=trigger.id, user=trigger.user, device=_device(self._kernel, trigger),
            refs={"event": event.id} if event else {},
            labels=labels,
        )
        if trigger.spec.repeat == "no":
            await self._end(trigger, "done", f"trigger {trigger.id} fired once and is done")
        self._spawn(self._deliver(trigger, payload))

    async def _deliver(self, trigger: Trigger, payload: dict[str, Any]) -> None:
        try:
            if trigger.spec.script:
                await self._kernel.scripts.run(trigger, payload, dry=False)
            else:
                await self._kernel.waker.wake(payload, trigger.user)
        except Exception as err:
            log.exception("trigger %s failed to deliver", trigger.id)
            await self._kernel.kernel_event("trigger.error", f"trigger {trigger.id} failed: {err}", entity=trigger.id, user=trigger.user, device=_device(self._kernel, trigger))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def expire(self) -> None:
        now = self._kernel.now()
        for trigger in list(self._triggers.values()):
            if trigger.status == "unclaimed" and now - trigger.created_at > CLAIM_TTL:
                await self._end(trigger, "expired", f"trigger {trigger.id} was never claimed: {trigger.spec.note}")
                self._claim_done(trigger)
            elif trigger.expires_at and now > trigger.expires_at:
                await self._end(trigger, "expired", f"trigger {trigger.id} expired: {trigger.spec.note}")

    async def test(self, spec: TriggerSpec, user: str | None) -> dict[str, Any]:
        """What a trigger would do now, without registering it: each check's
        current reading and result, the last few events an event check
        would have matched, and what its script would have done for `user`."""
        spec = self.validate(spec)
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
        recent = log_.search(type=trigger.event.event, limit=50)
        where = trigger.event.where or {}
        return [e for e in recent if all(e.data.get(k) == v for k, v in where.items())][:REPLAY]

    async def _dry_runs(self, trigger: Trigger, replay: list[Event], readings: dict[str, Reading]) -> list[dict[str, Any]]:
        included = await self._kernel.state.get(trigger.spec.include)
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


def _new_claim() -> Claim:
    """A two-digit number and decoys, in random order."""
    choices = secrets.SystemRandom().sample(range(10, 100), CLAIM_CHOICES)
    return Claim(number=secrets.choice(choices), choices=choices)


def _device(kernel: Kernel, trigger: Trigger) -> str | None:
    """The device a trigger was set up from, through its session."""
    owner = kernel.sessions.owner(trigger.created_by)
    return owner.device if owner else None
