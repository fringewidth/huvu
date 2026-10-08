"""The `huvu` source: state no device owns, behind the same contract.

The family mode, the aliases between what the owner says and what devices
are called, pending approvals, and notifications to the owner. `huvu.mode`
works in a trigger condition the same way `ha.entity_state` does, and the
agent changes it through actions that go through the same policy.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from ..db import dumps, loads
from ..names import UnknownName
from ..source import Source, SourceError
from ..triggers import TriggerError
from ..spec import (
    ActionSpec,
    Caller,
    EventSpec,
    Match,
    Reading,
    Result,
    SourceSpec,
    ValueSpec,
)

if TYPE_CHECKING:
    from ..kernel import Kernel

MODES = ["normal", "away", "guests", "sleeping"]
NAMESPACE = "huvu"


class HuvuSource(Source):
    def __init__(self, kernel: Kernel) -> None:
        super().__init__({})
        self._kernel = kernel

    def describe(self) -> SourceSpec:
        return SourceSpec(
            name="huvu",
            description="State no device owns: family mode, aliases, approvals, notifications.",
            guide=(
                "Aliases map what people call a thing to its value key, and state_search puts them first: when someone names a thing "
                "their own way and you find it, set an alias. huvu_notify reaches a person's phone whenever you want, and is how you "
                "tell them about anything you changed on your own. The family mode (normal, away, guests, sleeping) works in trigger "
                "conditions like any value."
            ),
            values=[
                ValueSpec(name="mode", description=f"Family mode: {', '.join(MODES)}", type="string", labels=["owner"]),
                ValueSpec(
                    name="alias",
                    description="The value key an alias points to, such as 'hall light'",
                    type="string",
                    params={"alias": "string"},
                    updates="on_demand",
                    labels=["owner"],
                ),
                ValueSpec(name="aliases", description="Every alias and the value key it points to", type="object", updates="on_demand", labels=["owner"]),
                ValueSpec(
                    name="approvals",
                    description="The actions waiting for the person's tap: theirs, and those of the groups they're a member of",
                    type="list",
                    params={"user": "string"},
                    updates="on_demand",
                    labels=["owner"],
                    personal=True,
                ),
            ],
            events=[
                EventSpec(type="mode_changed", description="The family mode changed.", data={"mode": "string"}),
                EventSpec(type="notified", description="huvu sent the owner a notification."),
            ],
            actions=[
                ActionSpec(
                    name="set_mode",
                    description="Sets the family mode.",
                    input_schema=_object({"mode": {"type": "string", "enum": MODES}}, ["mode"]),
                    autonomy="notify",
                ),
                ActionSpec(
                    name="set_alias",
                    description="Points an alias (what the owner calls a thing) at a value key, so search finds it.",
                    input_schema=_object({"alias": {"type": "string"}, "value": {"type": "string"}}, ["alias", "value"]),
                    autonomy="silent",
                ),
                ActionSpec(
                    name="remove_alias",
                    description="Removes an alias.",
                    input_schema=_object({"alias": {"type": "string"}}, ["alias"]),
                    autonomy="silent",
                ),
                ActionSpec(
                    name="activate_trigger",
                    description="Turns on a trigger that wakes the advanced model, once its person says yes.",
                    input_schema=_object({"trigger": {"type": "string"}}, ["trigger"]),
                    requires_human=True,
                    # Set at night by a dream, it waits for the morning.
                    approval_ttl=timedelta(days=1),
                ),
                ActionSpec(
                    name="notify",
                    description="Sends a notification to the phones of the person you're helping, or to the members' phones when you serve a shared device. `image` is a ref from a source, such as a doorbell snapshot.",
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

    async def run(self) -> None:
        # Pushed once at start, so triggers on the mode have a reading.
        await self.ctx.update("mode", self._get("mode") or "normal", labels=["owner"])

    async def read(self, value: str, params: dict[str, Any]) -> Reading:
        now = self.ctx.now()
        if value == "mode":
            reading = Reading(value=self._get("mode") or "normal", at=now, labels=["owner"])
        elif value == "aliases":
            reading = Reading(value=self._aliases(), at=now, labels=["owner"])
        elif value == "approvals":
            pending = self._kernel.policy.pending_for(self._kernel.accounts.scope(params["user"], None))
            reading = Reading(value=[a.public() for a in pending], at=now, labels=["owner"])
        else:
            target = self._aliases().get(params["alias"].lower())
            reading = Reading(value=target, at=now, labels=["owner"], reason=None if target else "no such alias")
        return reading

    async def search(self, text: str) -> list[Match]:
        needle = text.lower().strip()
        return [Match(value=target, name=f"you call it {alias!r}") for alias, target in self._aliases().items() if needle in alias or alias in needle]

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
        await self.ctx.update("mode", mode, labels=["owner"])
        await self.ctx.emit("mode_changed", f"family mode set to {mode}", data={"mode": mode}, labels=["owner"])
        return Result(data={"mode": mode}, labels=["owner"])

    async def _set_alias(self, args: dict[str, Any], caller: Caller) -> Result:
        try:
            ref, _ = self._kernel.registry.resolve_value(args["value"])
        except UnknownName as err:
            raise SourceError(str(err)) from err
        aliases = self._aliases()
        aliases[args["alias"].lower().strip()] = ref.key
        self._put("aliases", aliases)
        return Result(data={"alias": args["alias"], "value": ref.key}, labels=["owner"])

    async def _remove_alias(self, args: dict[str, Any], caller: Caller) -> Result:
        aliases = self._aliases()
        removed = aliases.pop(args["alias"].lower().strip(), None)
        self._put("aliases", aliases)
        return Result(data={"removed": removed is not None}, labels=["owner"])

    async def _notify(self, args: dict[str, Any], caller: Caller) -> Result:
        image = self._kernel.media.url_for(args["image"]) if args.get("image") else None
        await self._kernel.notify(caller.user, caller.group, args["text"], image=image, urgency=args.get("urgency", "normal"))
        whom = caller.user or f"the members of {caller.group or 'the default group'}"
        await self.ctx.emit("notified", f"told {whom}: {args['text']}", refs={"image": args["image"]} if image else {}, labels=["owner"], user=caller.user, group=None if caller.user else caller.group)
        return Result(data={"sent": True}, labels=["owner"])


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
