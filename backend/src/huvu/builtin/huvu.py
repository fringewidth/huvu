"""The `huvu` source: what no device owns, as tables like any source's.

The kernel's own log (trigger runs, actions, approvals, wakes, attendance),
the family mode, the aliases between what people call a thing and what it
is, pending approvals, the triggers, and the notifications huvu sent. A
trigger reads `huvu_mode` the same way it reads `ha_entities`, and the
agent changes the mode through actions that go through the same policy.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from ..db import Row, dumps, loads, open_db
from ..prompts import TEXT
from ..source import Source, SourceError
from ..spec import ActionSpec, Caller, ColumnSpec, Result, SourceSpec, TableSpec
from ..triggers import TriggerError

if TYPE_CHECKING:
    from ..kernel import Kernel

MODES = ["normal", "away", "guests", "sleeping"]
NAMESPACE = "huvu"


def _columns(table: str, *names: tuple[str, str]) -> list[ColumnSpec]:
    return [ColumnSpec(name=n, type=t, description=TEXT[f"huvu.table.{table}.column.{n}"]) for n, t in names]


TABLES = [
    TableSpec(
        name="events",
        description=TEXT["huvu.table.events"],
        columns=_columns(
            "events",
            ("type", "text"),
            ("entity", "text"),
            ("summary", "text"),
            ("refs", "json"),
            ("data", "json"),
            ("labels", "json"),
            ("device_id", "text"),
        ),
        indexes=[["type", "ts"], ["entity", "ts"]],
    ),
    TableSpec(name="modes", description=TEXT["huvu.table.modes"], columns=_columns("modes", ("mode", "text")), labels=["owner"]),
    TableSpec(
        name="notifications",
        description=TEXT["huvu.table.notifications"],
        columns=_columns("notifications", ("text", "text"), ("image", "text"), ("urgency", "text")),
        labels=["owner"],
    ),
    TableSpec(name="mode", description=TEXT["huvu.table.mode"], columns=_columns("mode", ("mode", "text")), live=True, labels=["owner"]),
    TableSpec(
        name="aliases",
        description=TEXT["huvu.table.aliases"],
        columns=_columns("aliases", ("alias", "text"), ("target", "text")),
        live=True,
        labels=["owner"],
    ),
    TableSpec(
        name="approvals",
        description=TEXT["huvu.table.approvals"],
        columns=_columns(
            "approvals",
            ("approval_id", "text"),
            ("action", "text"),
            ("args", "json"),
            ("reason", "text"),
            ("created_at", "text"),
            ("deadline", "text"),
        ),
        live=True,
        labels=["owner"],
    ),
    TableSpec(
        name="triggers",
        description=TEXT["huvu.table.triggers"],
        columns=_columns(
            "triggers",
            ("trigger_id", "text"),
            ("note", "text"),
            ("sql", "text"),
            ("fire", "text"),
            ("once", "boolean"),
            ("tier", "text"),
            ("status", "text"),
            ("paused_by", "text"),
            ("created_at", "text"),
            ("expires_at", "text"),
            ("last_fired_at", "text"),
            ("fire_count", "integer"),
            ("grants", "json"),
            ("script", "text"),
        ),
        live=True,
        labels=["owner"],
    ),
]


class HuvuSource(Source):
    def __init__(self, kernel: Kernel) -> None:
        super().__init__({})
        self._kernel = kernel

    def describe(self) -> SourceSpec:
        return SourceSpec(
            name="huvu",
            description=TEXT["huvu.description"],
            guide=TEXT["huvu.guide"],
            tables=TABLES,
            actions=[
                ActionSpec(
                    name="set_mode",
                    description=TEXT["huvu.action.set_mode"],
                    input_schema=_object({"mode": {"type": "string", "enum": MODES}}, ["mode"]),
                    autonomy="notify",
                ),
                ActionSpec(
                    name="set_alias",
                    description=TEXT["huvu.action.set_alias"],
                    input_schema=_object({"alias": {"type": "string"}, "target": {"type": "string"}}, ["alias", "target"]),
                    autonomy="silent",
                ),
                ActionSpec(
                    name="remove_alias",
                    description=TEXT["huvu.action.remove_alias"],
                    input_schema=_object({"alias": {"type": "string"}}, ["alias"]),
                    autonomy="silent",
                ),
                ActionSpec(
                    name="activate_trigger",
                    description=TEXT["huvu.action.activate_trigger"],
                    input_schema=_object({"trigger": {"type": "string"}}, ["trigger"]),
                    requires_human=True,
                    # Set at night by a dream, it waits for the morning.
                    approval_ttl=timedelta(days=1),
                ),
                ActionSpec(
                    name="notify",
                    description=TEXT["huvu.action.notify"],
                    input_schema=_object(
                        {
                            "text": {"type": "string"},
                            "image": {"type": "string"},
                            "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
                        },
                        ["text"],
                    ),
                    autonomy="silent",
                ),
            ],
        )

    def mode(self) -> str:
        return self._get("mode") or "normal"

    def _get(self, key: str) -> Any:
        row = self._kernel.db.execute("SELECT value FROM kv WHERE namespace = ? AND key = ?", (NAMESPACE, key)).fetchone()
        return None if row is None else loads(row["value"])

    def _put(self, key: str, value: Any) -> None:
        self._kernel.db.execute(
            "INSERT INTO kv (namespace, key, value) VALUES (?, ?, ?) ON CONFLICT (namespace, key) DO UPDATE SET value = excluded.value",
            (NAMESPACE, key, dumps(value)),
        )

    def _aliases(self) -> dict[str, str]:
        return self._get("aliases") or {}

    # Live tables. Each read opens its own connection to huvu.db, since it
    # runs on a query's worker thread.

    def live(self, table: str) -> list[dict[str, Any]]:
        readers = {
            "mode": self._mode_rows,
            "aliases": self._alias_rows,
            "approvals": self._approval_rows,
            "triggers": self._trigger_rows,
        }
        return readers[table]()

    def _read(self, sql: str, args: tuple[Any, ...] = ()) -> list[Row]:
        conn = open_db(self._kernel.config.data_dir / "huvu.db", readonly=True)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def _kv(self, key: str) -> Any:
        rows = self._read("SELECT value FROM kv WHERE namespace = ? AND key = ?", (NAMESPACE, key))
        return loads(rows[0]["value"]) if rows else None

    def _mode_rows(self) -> list[dict[str, Any]]:
        return [{"mode": self._kv("mode") or "normal"}]

    def _alias_rows(self) -> list[dict[str, Any]]:
        return [{"alias": alias, "target": target} for alias, target in sorted((self._kv("aliases") or {}).items())]

    def _approval_rows(self) -> list[dict[str, Any]]:
        rows = self._read("SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at")
        return [
            {
                "approval_id": r["id"],
                "action": f"{r['source']}_{r['action']}",
                "args": loads(r["args"]),
                "reason": r["reason"],
                "created_at": r["created_at"],
                "deadline": r["deadline"],
                "user_id": r["user_id"],
                "group_id": None if r["user_id"] else r["group_id"],
            }
            for r in rows
        ]

    def _trigger_rows(self) -> list[dict[str, Any]]:
        rows = []
        for t in self._kernel.triggers.snapshot():
            rows.append(
                {
                    "trigger_id": t.id,
                    "note": t.spec.note,
                    "sql": t.spec.sql,
                    "fire": t.spec.fire,
                    "once": t.spec.once,
                    "tier": t.spec.tier,
                    "status": t.status,
                    "paused_by": t.paused_by,
                    "created_at": t.created_at.isoformat(),
                    "expires_at": t.expires_at.isoformat() if t.expires_at else None,
                    "last_fired_at": t.last_fired_at.isoformat() if t.last_fired_at else None,
                    "fire_count": t.fire_count,
                    "grants": t.spec.grants,
                    "script": t.spec.script,
                    "user_id": t.user,
                    "group_id": None if t.user else t.group,
                }
            )
        return rows

    # Actions

    async def act(self, name: str, args: dict[str, Any], caller: Caller) -> Result:
        handlers = {
            "set_mode": self._set_mode,
            "set_alias": self._set_alias,
            "remove_alias": self._remove_alias,
            "notify": self._notify,
            "activate_trigger": self._activate_trigger,
        }
        handler = handlers.get(name)
        if handler is None:
            raise SourceError(f"huvu has no action {name!r}")
        return await handler(args, caller)

    async def _activate_trigger(self, args: dict[str, Any], caller: Caller) -> Result:
        try:
            trigger = await self._kernel.triggers.approve(args["trigger"])
        except TriggerError as err:
            raise SourceError(str(err)) from err
        return Result(data=trigger.public(), labels=["owner"])

    async def _set_mode(self, args: dict[str, Any], caller: Caller) -> Result:
        mode = args["mode"]
        if mode not in MODES:
            raise SourceError(f"mode must be one of {', '.join(MODES)}")
        self._put("mode", mode)
        await self.ctx.record("modes", {"mode": mode}, user=caller.user, group=None if caller.user else caller.group)
        return Result(data={"mode": mode}, labels=["owner"])

    async def _set_alias(self, args: dict[str, Any], caller: Caller) -> Result:
        alias, target = args["alias"].lower().strip(), args["target"].strip()
        if not alias or not target:
            raise SourceError("an alias needs both what people call the thing and what it is")
        aliases = self._aliases()
        aliases[alias] = target
        self._put("aliases", aliases)
        return Result(data={"alias": alias, "target": target}, labels=["owner"])

    async def _remove_alias(self, args: dict[str, Any], caller: Caller) -> Result:
        aliases = self._aliases()
        removed = aliases.pop(args["alias"].lower().strip(), None)
        self._put("aliases", aliases)
        return Result(data={"removed": removed is not None}, labels=["owner"])

    async def _notify(self, args: dict[str, Any], caller: Caller) -> Result:
        image = self._kernel.media.url_for(args["image"]) if args.get("image") else None
        await self._kernel.notify(caller.user, caller.group, args["text"], image=image, urgency=args.get("urgency", "normal"))
        row = {"text": args["text"], "image": args.get("image"), "urgency": args.get("urgency", "normal")}
        await self.ctx.record("notifications", row, user=caller.user, group=None if caller.user else caller.group)
        return Result(data={"sent": True}, labels=["owner"])


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
