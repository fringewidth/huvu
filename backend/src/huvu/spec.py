"""The types every part of the kernel shares, and the contract's data shapes.

A source describes itself with a SourceSpec. Everything it emits or returns
is built from the types here, so the kernel, the MCP server, the trigger
engine and the frontend all read the same shapes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Where a piece of data came from. The labels are facts about the data, so
# the backend owns them. A tool result with `external` in it reaches the
# model marked as such (see mcp_server.py).
#   owner:    something the owner said or did
#   house:    device and sensor data, like event times and device states
#   external: content a stranger controls, like what a doorbell snapshot
#             shows, a web page, or a message from someone else
Label = Literal["owner", "house", "external"]

# How a value reaches the kernel.
#   pushed:    the source sends every change through its context
#   on_wake:   pushed, but the device sleeps, so readings can be hours old
#   on_demand: the kernel asks the source each time someone reads it
Updates = Literal["pushed", "on_wake", "on_demand"]

# What an action may do without the owner, before hard rules.
#   silent: run it
#   notify: run it, then tell the owner
#   ask:    hold it as a pending approval until the owner answers
Autonomy = Literal["silent", "notify", "ask"]

NAME_RE = re.compile(r"^[a-z][a-z0-9]*$")
MEMBER_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# `device` is a device posting data of its own to a source (a phone's
# location tracker), through POST /api/ingest/{source}.
CallerKind = Literal["hermes", "script", "frontend", "kernel", "device"]


@dataclass(frozen=True)
class Scope:
    """What a caller may read: their own things (`user`), the group-wide
    things of the groups they're a member of (`groups`), and, with `house`,
    things no group owns, such as a doorbell press. A guest-only user gets
    neither of the last two."""

    user: str | None
    groups: tuple[str, ...]
    house: bool


@dataclass(frozen=True)
class Caller:
    """Who asked for something, and for whom. `user` is None on a shared
    device nobody is recognized at, and for the kernel itself. `group` is
    the group of the shared device or group profile the request came
    through, if any. `device` is the device whose conversation the request
    came from; wakes, scripts and Hermes's own cron jobs have none."""

    kind: CallerKind
    session: str | None = None
    trigger: str | None = None
    user: str | None = None
    device: str | None = None
    group: str | None = None

    def describe(self) -> str:
        parts = [self.kind]
        if self.user:
            parts.append(f"user {self.user}")
        if self.group:
            parts.append(f"group {self.group}")
        if self.device:
            parts.append(f"device {self.device}")
        if self.session:
            parts.append(f"session {self.session}")
        if self.trigger:
            parts.append(f"trigger {self.trigger}")
        return ", ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "session": self.session, "trigger": self.trigger, "user": self.user, "device": self.device, "group": self.group}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValueSpec(Strict):
    """A kind of value a source has. `params` names what the value applies
    to, such as `{"entity_id": "string"}` for an HA entity's state."""

    name: str
    description: str
    type: Literal["string", "number", "boolean", "time", "object", "list"]
    params: dict[str, str] = Field(default_factory=dict)
    unit: str | None = None
    updates: Updates = "pushed"
    # Older than this counts as unknown to triggers. None means never stale.
    max_age: timedelta | None = None
    labels: list[Label] = Field(default_factory=lambda: ["house"])
    # One person's value, such as where they are. Its `user` param names
    # whose, and nobody else reads it or uses it in a trigger, members of
    # their groups included.
    personal: bool = False

    @field_validator("name")
    @classmethod
    def _member_name(cls, v: str) -> str:
        if not MEMBER_RE.match(v):
            raise ValueError(f"value name {v!r} must match {MEMBER_RE.pattern}")
        return v

    @model_validator(mode="after")
    def _personal_has_user(self) -> ValueSpec:
        if self.personal and "user" not in self.params:
            raise ValueError(f"personal value {self.name!r} needs a `user` param")
        return self


class EventSpec(Strict):
    """A kind of event a source emits. `data` lists the fields a trigger can
    filter on, with their types."""

    type: str
    description: str
    data: dict[str, str] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _member_name(cls, v: str) -> str:
        if not MEMBER_RE.match(v):
            raise ValueError(f"event type {v!r} must match {MEMBER_RE.pattern}")
        return v


class ActionSpec(Strict):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    autonomy: Autonomy = "ask"
    # Always asks, whoever requests it, and config can't lower it. Only the
    # owner's tap in the frontend can approve it. The front door's unlock is
    # one: a model or a face match alone must never open it.
    requires_human: bool = False
    # How long a pending approval for this action stays answerable.
    approval_ttl: timedelta = timedelta(minutes=10)

    @field_validator("name")
    @classmethod
    def _member_name(cls, v: str) -> str:
        if not MEMBER_RE.match(v):
            raise ValueError(f"action name {v!r} must match {MEMBER_RE.pattern}")
        return v


ColumnType = Literal["text", "integer", "real", "boolean", "json"]

# Every history table has these, filled in by the kernel (see data.py), so
# every source's rows join on the same time and person keys.
STANDARD_COLUMNS: dict[str, tuple[str, str]] = {
    "at": ("text", "When it happened: ISO 8601 with the local offset"),
    "ts": ("real", "The same instant in seconds since the epoch, for ranges and joins"),
    "local_date": ("text", "The local date, YYYY-MM-DD"),
    "local_minute": ("integer", "Minutes since local midnight, 0 to 1439"),
    "weekday": ("integer", "1 is Monday, 7 is Sunday"),
    "user_id": ("text", "The huvu user it belongs to; NULL if nobody is known"),
    "group_id": ("text", "The group it belongs to, when it has no user and isn't the whole house's"),
}


class ColumnSpec(Strict):
    name: str
    type: ColumnType
    description: str

    @field_validator("name")
    @classmethod
    def _column_name(cls, v: str) -> str:
        if not MEMBER_RE.match(v):
            raise ValueError(f"column name {v!r} must match {MEMBER_RE.pattern}")
        if v in STANDARD_COLUMNS:
            raise ValueError(f"column {v!r} is a standard column; the kernel fills it in")
        return v


class TableSpec(Strict):
    """A history table a source keeps for analysis: one row per thing that
    happened, with typed columns, so the dream can join and aggregate it
    with SQL. The kernel keeps every source's tables in one SQLite
    database, as `<source>_<name>`, with the standard columns added, and
    keeps the rows for good. Raw content (images, audio) stays in the
    source; a row holds the facts about it and a ref."""

    name: str
    description: str
    columns: list[ColumnSpec]
    # Each entry is the columns of one index, such as ["entity_id", "ts"].
    # Every table also gets (ts) and (user_id, ts).
    indexes: list[list[str]] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _table_name(cls, v: str) -> str:
        if not MEMBER_RE.match(v):
            raise ValueError(f"table name {v!r} must match {MEMBER_RE.pattern}")
        return v

    def column(self, name: str) -> ColumnSpec | None:
        return next((c for c in self.columns if c.name == name), None)


class SourceSpec(Strict):
    name: str
    # One line: what the source covers. It is the source's skill's
    # description, which every Hermes session sees in its skill index.
    description: str
    # The hand-written part of the source's skill (Markdown): when to use it
    # and the gotchas its schemas can't say. huvu adds the reference part
    # (actions, values, events, tables) itself. See skills.py. There are no
    # queries: what a source knows over time goes in its tables, which the
    # model reads with data_sql, and what is true now in its values.
    guide: str = ""
    values: list[ValueSpec] = Field(default_factory=list)
    events: list[EventSpec] = Field(default_factory=list)
    actions: list[ActionSpec] = Field(default_factory=list)
    tables: list[TableSpec] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _source_name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError(f"source name {v!r} must match {NAME_RE.pattern}")
        return v

    def value(self, name: str) -> ValueSpec | None:
        return next((v for v in self.values if v.name == name), None)

    def event(self, type_: str) -> EventSpec | None:
        return next((e for e in self.events if e.type == type_), None)

    def action(self, name: str) -> ActionSpec | None:
        return next((a for a in self.actions if a.name == name), None)

    def table(self, name: str) -> TableSpec | None:
        return next((t for t in self.tables if t.name == name), None)


class Event(Strict):
    """The thin envelope the event log keeps for everything that happened.
    The full detail stays in the source and is reached through `refs`."""

    id: str = ""
    at: datetime
    source: str
    # Qualified with the source: `tuya.doorbell`.
    type: str
    entity: str | None = None
    # One line a model can read.
    summary: str
    # Source-scoped refs to the detail, such as {"snapshot": "tuya:album/8f2c"}.
    refs: dict[str, str] = Field(default_factory=dict)
    # Small, flat fields triggers can filter on. Big payloads stay in the source.
    data: dict[str, Any] = Field(default_factory=dict)
    labels: list[Label] = Field(default_factory=lambda: ["house"])
    # Whose event it is. One with a user shows only to that user (and their
    # dream). Without one, it shows to the members of `group`, or, with no
    # group either, to every member of any group (see Scope).
    user: str | None = None
    group: str | None = None
    # The device whose conversation it came from, if any.
    device: str | None = None


class Reading(Strict):
    """One value as the kernel knows it: what it is, and how old."""

    value: Any = None
    at: datetime | None = None
    labels: list[Label] = Field(default_factory=lambda: ["house"])
    # Why there is no value, when `value` is None.
    reason: str | None = None


class Match(Strict):
    """One result of a state search: a value key and what to call it, and
    the actions that work the thing, so the model finds how to work a
    device where it finds the device."""

    value: str
    name: str
    area: str | None = None
    now: Any = None
    actions: list[str] = Field(default_factory=list)


class Result(Strict):
    """What an action or a tool returns: plain JSON data and its labels."""

    data: Any = None
    labels: list[Label] = Field(default_factory=lambda: ["house"])


class Blob(Strict):
    """Binary content behind a ref, such as a decrypted snapshot."""

    content: bytes
    content_type: str
    labels: list[Label] = Field(default_factory=lambda: ["external"])


class Feed(Strict):
    """A live feed a source can stream, for the frontend's list. `src` is a
    WHEP endpoint. An offline feed stays listed, so huvu can say the camera
    is down."""

    id: str
    label: str
    kind: str
    online: bool = True
    src: str


def merge_labels(*groups: list[Label]) -> list[Label]:
    seen: list[Label] = []
    for group in groups:
        for label in group:
            if label not in seen:
                seen.append(label)
    return seen
