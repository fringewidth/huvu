"""The `huvu` source: what no device owns, as tables like any source's.

The kernel's own log (trigger runs, actions, approvals, wakes, attendance),
the family mode, the aliases between what people call a thing and what it
is, pending approvals, the triggers, and the notifications huvu sent,
messages between people included. A
trigger reads `huvu_mode` the same way it reads `ha_entities`, and the
agent changes the mode through actions that go through the same policy.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from ..accounts import AuthError
from ..db import dumps, loads
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
        columns=_columns("notifications", ("text", "text"), ("image", "text"), ("urgency", "text"), ("sender", "text"), ("recipient", "text")),
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
            ("skip", "integer"),
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
                ActionSpec(
                    name="set_person_alias",
                    description=TEXT["huvu.action.set_person_alias"],
                    input_schema=_object({"person": {"type": "string"}, "alias": {"type": "string"}}, ["person", "alias"]),
                    # It changes only what the caller's own agent and phone
                    # call someone.
                    autonomy="silent",
                ),
                ActionSpec(
                    name="message",
                    description=TEXT["huvu.action.message"],
                    input_schema=_object(
                        {
                            "person": {"type": "string"},
                            "group": {"type": "string"},
                            "text": {"type": "string"},
                            "image": {"type": "string"},
                            "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
                        },
                        ["text"],
                    ),
                    # Reaching someone else is a bigger act than reaching
                    # your own person, so the sender hears that it went.
                    autonomy="notify",
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

    # Live tables, handed to the kernel whenever they change

    def publish(self) -> None:
        """Every live table, at start."""
        self.publish_mode()
        self.publish_aliases()
        self.publish_approvals()
        self.publish_triggers()

    def publish_mode(self) -> None:
        self.ctx.replace("mode", [{"mode": self.mode()}])

    def publish_aliases(self) -> None:
        self.ctx.replace("aliases", [{"alias": alias, "target": target} for alias, target in sorted(self._aliases().items())])

    def publish_approvals(self) -> None:
        """Called by the policy whenever an approval opens or closes."""
        self.ctx.replace(
            "approvals",
            [
                {
                    "approval_id": a.id,
                    "action": f"{a.source}_{a.action}",
                    "args": a.args,
                    "reason": a.reason,
                    "created_at": a.created_at.isoformat(),
                    "deadline": a.deadline.isoformat(),
                    "user_id": a.user,
                    "group_id": None if a.user else a.group,
                }
                for a in self._kernel.policy.pending()
            ],
        )

    def publish_triggers(self) -> None:
        """Called by the trigger engine whenever a trigger changes."""
        self.ctx.replace("triggers", self._trigger_rows())

    def _trigger_rows(self) -> list[dict[str, Any]]:
        rows = []
        for t in self._kernel.triggers.alive():
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
                    "skip": t.skip,
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
            "message": self._message,
            "set_person_alias": self._set_person_alias,
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
        self.publish_mode()
        await self.ctx.record("modes", {"mode": mode}, user=caller.user, group=None if caller.user else caller.group)
        return Result(data={"mode": mode}, labels=["owner"])

    async def _set_alias(self, args: dict[str, Any], caller: Caller) -> Result:
        alias, target = args["alias"].lower().strip(), args["target"].strip()
        if not alias or not target:
            raise SourceError("an alias needs both what people call the thing and what it is")
        aliases = self._aliases()
        aliases[alias] = target
        self._put("aliases", aliases)
        self.publish_aliases()
        return Result(data={"alias": alias, "target": target}, labels=["owner"])

    async def _remove_alias(self, args: dict[str, Any], caller: Caller) -> Result:
        aliases = self._aliases()
        removed = aliases.pop(args["alias"].lower().strip(), None)
        self._put("aliases", aliases)
        self.publish_aliases()
        return Result(data={"removed": removed is not None}, labels=["owner"])

    async def _notify(self, args: dict[str, Any], caller: Caller) -> Result:
        image = self._kernel.media.url_for(args["image"]) if args.get("image") else None
        # From a wake, to its own person: the notice opens the wake.
        opens = self._kernel.waker.opens_for(caller)
        await self._kernel.notify(caller.user, caller.group, args["text"], image=image, urgency=args.get("urgency", "normal"), opens=opens)
        row = {"text": args["text"], "image": args.get("image"), "urgency": args.get("urgency", "normal")}
        await self.ctx.record("notifications", row, user=caller.user, group=None if caller.user else caller.group)
        return Result(data={"sent": True}, labels=["owner"])

    async def _message(self, args: dict[str, Any], caller: Caller) -> Result:
        """A notification from one person to another, or to a group's
        members, sent in the sender's name. The sender names the person by
        their own alias for them."""
        accounts = self._kernel.accounts
        sender = caller.user
        if sender is None:
            raise SourceError("only a person can send a message, and nobody is recognized here; huvu_notify reaches this group's members")
        if bool(args.get("person")) == bool(args.get("group")):
            raise SourceError("name a person or a group, one of the two")
        group = args.get("group")
        if group is not None and not accounts.is_member(group, sender):
            raise SourceError(f"you can message only your own groups: {', '.join(accounts.member_groups(sender)) or 'you are a member of none'}")
        person = self._person(sender, args["person"]) if args.get("person") else None
        image = self._kernel.media.url_for(args["image"]) if args.get("image") else None
        urgency = args.get("urgency", "normal")
        await self._kernel.notify(person, group, args["text"], image=image, urgency=urgency, sender=sender)
        row = {"text": args["text"], "image": args.get("image"), "urgency": urgency, "sender": sender, "recipient": person or group}
        # A group's members, the sender among them, all see the group's row.
        # A message to one person is a row for each of the two.
        owners = [(person, None), (sender, None)] if person else [(None, group)]
        for user, owner_group in owners:
            await self.ctx.record("notifications", row, user=user, group=owner_group)
        return Result(data={"sent": True, "to": args.get("person") or group}, labels=["owner"])

    async def _set_person_alias(self, args: dict[str, Any], caller: Caller) -> Result:
        """Sets what the caller calls someone they share a group with."""
        accounts = self._kernel.accounts
        if caller.user is None:
            raise SourceError("only a person has names for the others, and nobody is recognized here")
        person = self._person(caller.user, args["person"])
        try:
            accounts.set_alias(caller.user, person, args["alias"])
        except AuthError as err:
            raise SourceError(str(err)) from err
        return Result(data={"person": person, "alias": accounts.alias(caller.user, person)}, labels=["owner"])

    def _person(self, sender: str, alias: str) -> str:
        """The user the sender calls `alias` (or names by username), among
        the people they can message."""
        accounts = self._kernel.accounts
        wanted = alias.strip().lower()
        known = {u: accounts.alias(sender, u) for u in accounts.reachable(sender)}
        match = next((u for u, called in known.items() if wanted in (u, called.lower())), None)
        if match is None:
            raise SourceError(f"nobody you can message is called {alias!r}; you can message {', '.join(known.values()) or 'nobody'}")
        return match


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
