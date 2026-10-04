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

from pydantic import BaseModel, ConfigDict, Field, field_validator

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

CallerKind = Literal["hermes", "script", "frontend", "kernel"]


@dataclass(frozen=True)
class Caller:
    """Who asked for something, and for whom. `user` is None on a household
    device and for the kernel itself. `device` is the device whose
    conversation the request came from; wakes, scripts and Hermes's own cron
    jobs have none."""

    kind: CallerKind
    session: str | None = None
    trigger: str | None = None
    user: str | None = None
    device: str | None = None

    def describe(self) -> str:
        parts = [self.kind]
        if self.user:
            parts.append(f"user {self.user}")
        if self.device:
            parts.append(f"device {self.device}")
        if self.session:
            parts.append(f"session {self.session}")
        if self.trigger:
            parts.append(f"trigger {self.trigger}")
        return ", ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "session": self.session, "trigger": self.trigger, "user": self.user, "device": self.device}


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

    @field_validator("name")
    @classmethod
    def _member_name(cls, v: str) -> str:
        if not MEMBER_RE.match(v):
            raise ValueError(f"value name {v!r} must match {MEMBER_RE.pattern}")
        return v


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


class QuerySpec(Strict):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})

    @field_validator("name")
    @classmethod
    def _member_name(cls, v: str) -> str:
        if not MEMBER_RE.match(v):
            raise ValueError(f"query name {v!r} must match {MEMBER_RE.pattern}")
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


class SourceSpec(Strict):
    name: str
    description: str
    values: list[ValueSpec] = Field(default_factory=list)
    events: list[EventSpec] = Field(default_factory=list)
    queries: list[QuerySpec] = Field(default_factory=list)
    actions: list[ActionSpec] = Field(default_factory=list)

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

    def query(self, name: str) -> QuerySpec | None:
        return next((q for q in self.queries if q.name == name), None)

    def action(self, name: str) -> ActionSpec | None:
        return next((a for a in self.actions if a.name == name), None)


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
    # Whose event it is. None is the household's, which every user sees;
    # one with a user only shows to that user (and their dream).
    user: str | None = None
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
    """One result of a state search: a value key and what to call it."""

    value: str
    name: str
    area: str | None = None
    now: Any = None


class Result(Strict):
    """What a query or action returns: plain JSON data and its labels."""

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
