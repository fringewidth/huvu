"""The types every part of the kernel shares, and the contract's data shapes.

A source describes itself with a SourceSpec: its tables and its actions.
Everything else the kernel, the MCP server, the trigger engine and the
frontend pass around is built from the types here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .prompts import LIVE_COLUMNS, STANDARD_COLUMNS

# Where a piece of data came from. The labels are facts about the data, so
# the backend owns them. A tool result with `external` in it reaches the
# model marked as such (see mcp_server.py).
#   owner:    something the owner said or did
#   house:    device and sensor data, like event times and device states
#   external: content a stranger controls, like what a doorbell snapshot
#             shows, a web page, or a message from someone else
Label = Literal["owner", "house", "external"]

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



class ColumnSpec(Strict):
    name: str
    type: ColumnType
    description: str

    @field_validator("name")
    @classmethod
    def _column_name(cls, v: str) -> str:
        if not MEMBER_RE.match(v):
            raise ValueError(f"column name {v!r} must match {MEMBER_RE.pattern}")
        if v in STANDARD_COLUMNS or v in LIVE_COLUMNS:
            raise ValueError(f"column {v!r} is a standard column; the kernel fills it in")
        return v


class TableSpec(Strict):
    """A table of a source, named `<source>_<name>` wherever SQL reads it.

    A history table holds what happened: one row per thing, with typed
    columns, recorded with `ctx.record` and kept for good in the kernel's
    history database, with the standard columns added (see data.py). Raw
    content (images, audio) stays in the source; a row holds the facts
    about it and a ref.

    A live table (`live`) holds what is true now. Nothing is stored: each
    read asks the source (`Source.live`), which answers from what it
    already holds. Its rows have `user_id` and `group_id`, so the same
    rule decides who sees which row."""

    name: str
    description: str
    columns: list[ColumnSpec]
    live: bool = False
    # What the rows are, for the labels of a query that reads them.
    labels: list[Label] = Field(default_factory=lambda: ["house"])
    # Each entry is the columns of one index, such as ["entity_id", "ts"].
    # Every history table also gets (id), (ts) and (user_id, ts). A live
    # table has none.
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
    # (tables and actions) itself. See skills.py. Everything a source knows
    # is a table the model reads with data_sql: what happened in its
    # history tables, what is true now in its live tables.
    guide: str = ""
    actions: list[ActionSpec] = Field(default_factory=list)
    tables: list[TableSpec] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _source_name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError(f"source name {v!r} must match {NAME_RE.pattern}")
        return v

    def action(self, name: str) -> ActionSpec | None:
        return next((a for a in self.actions if a.name == name), None)

    def table(self, name: str) -> TableSpec | None:
        return next((t for t in self.tables if t.name == name), None)


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
