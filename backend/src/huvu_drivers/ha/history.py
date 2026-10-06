"""Who caused a change in Home Assistant, and 5-minute sensor buckets.

Every HA state change carries a context: its own id, the id of the context
that caused it (`parent_id`), and the HA user behind it, if any. A script's
service calls run in the script's own context, and an automation's run in a
context whose parent is what triggered it. That is enough to sort every
change into one of four actors:

1. `huvu`: huvu called it. huvu's token belongs to its own HA user, so the
   context's user is that user; the driver also knows which huvu user the
   call was for.
2. `ha_user`: someone in the HA app or dashboard. The context's user is
   theirs, and the driver's `users` option maps it to a huvu user.
3. `automation`: no user, but a parent: an HA automation, or a script it
   ran.
4. `device`: no user and no parent. A wall switch, the device's own
   button, a physical remote seen through a power plug, or a cloud app.

A context seen once is remembered (Attribution.CAPACITY of them), so a
change inside a script run takes the actor of the run.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

ACTORS = ("huvu", "ha_user", "automation", "device")


@dataclass(frozen=True)
class Actor:
    kind: str
    # The huvu user it was, when known.
    user: str | None = None
    # The HA user id behind it, when there is one.
    ha_user: str | None = None

    def words(self, names: dict[str, str] | None = None) -> str:
        names = names or {}
        person = names.get(self.user or "", self.user)
        phrases = {
            "huvu": f"huvu, for {person}" if person else "huvu",
            "ha_user": person or "someone in the Home Assistant app",
            "automation": "a Home Assistant automation",
            "device": "the device itself or someone at it",
        }
        return phrases[self.kind]


class Attribution:
    CAPACITY = 4096

    def __init__(self, own_user: str | None, users: dict[str, str]) -> None:
        # The HA user huvu's token belongs to.
        self.own_user = own_user
        # HA user id -> huvu user.
        self._users = users
        self._seen: OrderedDict[str, Actor] = OrderedDict()

    def remember(self, context_id: str | None, actor: Actor) -> None:
        if not context_id:
            return
        self._seen[context_id] = actor
        self._seen.move_to_end(context_id)
        while len(self._seen) > self.CAPACITY:
            self._seen.popitem(last=False)

    def known(self, context: dict[str, Any]) -> Actor | None:
        found = self._seen.get(context.get("id") or "")
        if found is None and context.get("parent_id"):
            found = self._seen.get(context["parent_id"])
        return found

    def actor(self, context: dict[str, Any], *, huvu_user: str | None = None) -> Actor:
        """Who caused a change with this context. `huvu_user` is the huvu
        user of a call huvu has in flight that this change may belong to."""
        found = self.known(context)
        user = context.get("user_id")
        if found is None and user and user == self.own_user:
            found = Actor("huvu", huvu_user, user)
        elif found is None and user:
            found = Actor("ha_user", self._users.get(user), user)
        elif found is None and context.get("parent_id"):
            found = Actor("automation")
        elif found is None:
            found = Actor("device")
        self.remember(context.get("id"), found)
        return found


BUCKET = timedelta(minutes=5)


def bucket_start(at: datetime) -> datetime:
    at = at.astimezone()
    return at.replace(minute=at.minute - at.minute % 5, second=0, microsecond=0)


@dataclass
class _Open:
    start: datetime
    value: float
    since: datetime
    weighted: float = 0.0
    seconds: float = 0.0
    low: float = 0.0
    high: float = 0.0
    changes: int = 0

    def advance(self, until: datetime) -> None:
        span = (until - self.since).total_seconds()
        if span > 0:
            self.weighted += self.value * span
            self.seconds += span
            self.since = until

    def row(self) -> dict[str, Any]:
        mean = self.weighted / self.seconds if self.seconds else self.value
        return {"mean": round(mean, 4), "low": self.low, "high": self.high, "last": self.value, "changes": self.changes}


class SensorBuckets:
    """Time-weighted 5-minute buckets of numeric sensors. A sensor that
    doesn't change still gets a row per bucket, with its last value
    carried forward, so a bucket join finds a value for every 5 minutes.
    The buckets live only while the connection does: a gap stays a gap."""

    def __init__(self) -> None:
        self._open: dict[str, _Open] = {}

    def add(self, entity_id: str, value: float, at: datetime) -> list[tuple[str, datetime, dict[str, Any]]]:
        """A new reading. Returns the rows of buckets it closed."""
        closed: list[tuple[str, datetime, dict[str, Any]]] = []
        current = self._open.get(entity_id)
        if current is None:
            self._open[entity_id] = _Open(start=bucket_start(at), value=value, since=at, low=value, high=value, changes=1)
        else:
            closed = self._roll(entity_id, current, at)
            current = self._open[entity_id]
            current.advance(at)
            current.value = value
            current.low = min(current.low, value)
            current.high = max(current.high, value)
            current.changes += 1
        return closed

    def drop(self, entity_id: str, at: datetime) -> list[tuple[str, datetime, dict[str, Any]]]:
        """The sensor stopped giving a number (unavailable). Its open
        bucket closes as it stands."""
        current = self._open.get(entity_id)
        rows: list[tuple[str, datetime, dict[str, Any]]] = []
        if current is not None:
            rows = self._roll(entity_id, current, at)
            last = self._open.pop(entity_id)
            last.advance(at)
            if last.seconds:
                rows.append((entity_id, last.start, last.row()))
        return rows

    def flush(self, now: datetime) -> list[tuple[str, datetime, dict[str, Any]]]:
        """Closes every bucket that has ended by `now`."""
        rows: list[tuple[str, datetime, dict[str, Any]]] = []
        for entity_id, current in list(self._open.items()):
            rows.extend(self._roll(entity_id, current, now))
        return rows

    def _roll(self, entity_id: str, current: _Open, until: datetime) -> list[tuple[str, datetime, dict[str, Any]]]:
        rows: list[tuple[str, datetime, dict[str, Any]]] = []
        while current.start + BUCKET <= until:
            end = current.start + BUCKET
            current.advance(end)
            rows.append((entity_id, current.start, current.row()))
            current = _Open(start=end, value=current.value, since=end, low=current.value, high=current.value)
        self._open[entity_id] = current
        return rows
