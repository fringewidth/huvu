"""The tool box: the one tool surface Hermes and trigger scripts share.

The MCP server lists and calls these, and scripts call them over their
pipe. Kernel tools come first (state, events, triggers, ui); then every
source query and action becomes a tool named `<source>_<name>`, built from
the source's spec. Actions go through the policy; queries don't.

Hermes prefixes each one as `mcp__huvu__<name>`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .accounts import AuthError
from .events import day_bounds
from .names import UnknownName
from .registry import suggest
from .scripts import ScriptError
from .source import SourceError
from .spec import Caller, Label, Result, merge_labels
from .times import RELATIVE_RE, UNITS, parse_when
from .triggers import TriggerError, TriggerSpec

if TYPE_CHECKING:
    from .kernel import Kernel

log = logging.getLogger("huvu.tools")

Handler = Callable[[dict[str, Any], Caller], Awaitable[Result]]

# Source names that would collide with kernel tool names.
RESERVED = {"state", "events", "sources", "triggers", "ui", "kernel", "people"}


class ToolError(Exception):
    """A call that failed in a way the model can fix. The message goes back
    to it as the tool's error."""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    # Changes something. Skipped in a script's dry run.
    writes: bool = False


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


TRIGGER_SCHEMA = TriggerSpec.model_json_schema()


class ToolBox:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        self._tools: dict[str, Tool] = {}

    def build(self) -> None:
        """Builds the list from the kernel's tools and every source's spec.
        Called once the sources are loaded."""
        self._tools = {t.name: t for t in self._kernel_tools()}
        for spec in self._kernel.registry.specs():
            if spec.name in RESERVED:
                raise ValueError(f"a source can't be named {spec.name!r}; it collides with kernel tools")
            for query in spec.queries:
                self._add(Tool(f"{spec.name}_{query.name}", query.description, query.input_schema, self._query_handler(spec.name, query.name)))
            for action in spec.actions:
                schema = _with_reason(action.input_schema)
                desc = action.description + (" Always needs the owner's tap." if action.requires_human else "")
                self._add(Tool(f"{spec.name}_{action.name}", desc, schema, self._action_handler(spec.name, action.name), writes=True))

    def _add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"two tools are named {tool.name!r}")
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def writes(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.writes)

    async def call(self, name: str, args: dict[str, Any], caller: Caller) -> Result:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"no tool named {name!r}{suggest(name, self.names())}")
        missing = [k for k in tool.input_schema.get("required", []) if k not in args]
        if missing:
            raise ToolError(f"{name} needs {', '.join(missing)}")
        try:
            result = await tool.handler(args, caller)
        except (UnknownName, SourceError, TriggerError, ScriptError, ValidationError, AuthError) as err:
            raise ToolError(str(err)) from err
        return result

    def _query_handler(self, source: str, name: str) -> Handler:
        async def handle(args: dict[str, Any], caller: Caller) -> Result:
            return await self._kernel.registry.get(source).query(name, args, caller)

        return handle

    def _action_handler(self, source: str, name: str) -> Handler:
        async def handle(args: dict[str, Any], caller: Caller) -> Result:
            clean = dict(args)
            reason = clean.pop("reason", None)
            return await self._kernel.policy.request(source, name, clean, caller, reason)

        return handle

    def _kernel_tools(self) -> list[Tool]:
        return [
            Tool(
                "state_get",
                "Reads values by key, such as clock.time or ha.entity_state[light.porch]. Each reading says how old it is.",
                _schema({"values": {"type": "array", "items": {"type": "string"}}}, ["values"]),
                self._state_get,
            ),
            Tool(
                "state_search",
                "Finds values by what things are called: device names, areas, aliases. Each result has its key and current value.",
                _schema({"query": {"type": "string"}}, ["query"]),
                self._state_search,
            ),
            Tool(
                "events_search",
                "Searches the event log, newest first. `query` words must all match. `since` and `until` take 2h, 3d, today, yesterday or ISO 8601.",
                _schema(
                    {
                        "query": {"type": "string"},
                        "source": {"type": "string"},
                        "type": {"type": "string", "description": "A qualified type, such as tuya.doorbell"},
                        "since": {"type": "string"},
                        "until": {"type": "string"},
                        "limit": {"type": "integer"},
                    }
                ),
                self._events_search,
            ),
            Tool("events_get", "One event by id, with its refs.", _schema({"id": {"type": "string"}}, ["id"]), self._events_get),
            Tool(
                "events_digest",
                "A rollup of one day (today, yesterday or YYYY-MM-DD), or one hour of it: counts per event type with a few summaries, and counts per hour.",
                _schema({"day": {"type": "string"}, "hour": {"type": "integer"}}),
                self._events_digest,
            ),
            Tool("sources_list", "Every source with its values, events, queries and actions.", _schema({}), self._sources_list),
            Tool(
                "triggers_register",
                "Registers a trigger: a condition over source data that wakes you when it comes true. Write `note` first: why it exists and what to do. "
                "`when` is a flat list that must all hold. `repeat` is no, yes or daily. `include` lists values to bring along in the wake. "
                "`expires` takes 2h, tomorrow 09:00 or ISO; a repeat=no trigger without one expires in 7 days. "
                "On a household device the trigger waits until someone claims it: the result has a `claim` number, which you tell them to tap on their phone.",
                TRIGGER_SCHEMA,
                self._triggers_register,
                writes=True,
            ),
            Tool("triggers_test", "Checks a trigger without registering it: each check's current reading and result, recent matching events, and what its script would do.", TRIGGER_SCHEMA, self._triggers_test),
            Tool("triggers_list", "Every live trigger of the person you're helping, or the household's.", _schema({}), self._triggers_list),
            Tool("triggers_remove", "Removes a trigger.", _schema({"id": {"type": "string"}}, ["id"]), self._triggers_remove, writes=True),
            Tool(
                "people_identify",
                "Asks a family member, on their phone, whether they're the one at this shared device. "
                "Use it when someone says who they are, or you think you know. `user` is their username or name. "
                "Once they tap yes, you act for them: their triggers, approvals and notifications.",
                _schema({"user": {"type": "string"}}, ["user"]),
                self._people_identify,
                writes=True,
            ),
            Tool(
                "people_release",
                "Forgets who is at this shared device, such as when they say they're leaving.",
                _schema({}),
                self._people_release,
                writes=True,
            ),
            Tool(
                "ui_choices",
                "Shows the owner buttons under your answer. Picking one sends its text as their next message.",
                _schema({"options": {"type": "array", "items": {"type": "string"}}}, ["options"]),
                self._ui_choices,
            ),
            Tool(
                "ui_show_feed",
                "Shows a live feed in your answer. `feed` is a feed id from GET /api/feeds; `caption` says what it shows right now.",
                _schema({"feed": {"type": "string"}, "caption": {"type": "string"}}, ["feed"]),
                self._ui_show_feed,
            ),
            Tool(
                "ui_show_image",
                "Shows an image in your answer. `ref` is a ref from a source, such as an event's snapshot.",
                _schema({"ref": {"type": "string"}, "caption": {"type": "string"}}, ["ref"]),
                self._ui_show_image,
            ),
        ]

    async def _state_get(self, args: dict[str, Any], caller: Caller) -> Result:
        readings = await self._kernel.state.get(list(args["values"]))
        return Result(data=readings, labels=merge_labels(*(r.labels for r in readings.values())))

    async def _state_search(self, args: dict[str, Any], caller: Caller) -> Result:
        return Result(data=await self._kernel.state.search(args["query"]), labels=["house"])

    async def _events_search(self, args: dict[str, Any], caller: Caller) -> Result:
        now = self._kernel.now()
        events = self._kernel.events.search(
            args.get("query"),
            source=args.get("source"),
            type=args.get("type"),
            since=_ago(args["since"], now) if args.get("since") else None,
            until=_ago(args["until"], now) if args.get("until") else None,
            limit=int(args.get("limit") or 20),
            viewer=caller.user,
        )
        return Result(data=events, labels=merge_labels(["house"], *(e.labels for e in events)))

    async def _events_get(self, args: dict[str, Any], caller: Caller) -> Result:
        event = self._kernel.events.get(args["id"], viewer=caller.user)
        if event is None:
            raise ToolError(f"no event {args['id']!r}")
        return Result(data=event, labels=event.labels)

    async def _events_digest(self, args: dict[str, Any], caller: Caller) -> Result:
        start, end = day_bounds(_day(args.get("day") or "today", self._kernel.now()))
        if args.get("hour") is not None:
            start = start + timedelta(hours=int(args["hour"]))
            end = start + timedelta(hours=1)
        return Result(data=self._kernel.events.digest(start, end, viewer=caller.user), labels=["house"])

    async def _sources_list(self, args: dict[str, Any], caller: Caller) -> Result:
        data = [
            {
                "name": s.name,
                "description": s.description,
                "values": [f"{s.name}.{v.name}" + (f"[{','.join(v.params)}]" if v.params else "") for v in s.values],
                "events": [f"{s.name}.{e.type}" for e in s.events],
                "queries": [f"{s.name}_{q.name}" for q in s.queries],
                "actions": [f"{s.name}_{a.name}" for a in s.actions],
            }
            for s in self._kernel.registry.specs()
        ]
        return Result(data=data, labels=["house"])

    async def _triggers_register(self, args: dict[str, Any], caller: Caller) -> Result:
        trigger = await self._kernel.triggers.register(TriggerSpec.model_validate(args), caller)
        return Result(data=trigger.public(), labels=["house"])

    async def _triggers_test(self, args: dict[str, Any], caller: Caller) -> Result:
        return Result(data=await self._kernel.triggers.test(TriggerSpec.model_validate(args), caller.user), labels=["house"])

    async def _triggers_list(self, args: dict[str, Any], caller: Caller) -> Result:
        return Result(data=[t.public() for t in self._kernel.triggers.list(caller.user)], labels=["house"])

    async def _triggers_remove(self, args: dict[str, Any], caller: Caller) -> Result:
        trigger = await self._kernel.triggers.remove(args["id"], caller.user)
        return Result(data=trigger.public(), labels=["house"])

    async def _people_identify(self, args: dict[str, Any], caller: Caller) -> Result:
        device = self._shared_device(caller)
        wanted = str(args["user"]).strip().lower()
        users = self._kernel.accounts.users()
        user = next((u for u in users if wanted in (u.id, u.name.lower())), None)
        if user is None:
            raise ToolError(f"no family member {args['user']!r}; they are: {', '.join(f'{u.name} ({u.id})' for u in users)}")
        check = await self._kernel.attendance.ask(device, user.id, "model")
        return Result(data={"asked": user.name, "status": check.status, "say": "Tell them to check their phone and tap yes."}, labels=["owner"])

    async def _people_release(self, args: dict[str, Any], caller: Caller) -> Result:
        ended = await self._kernel.attendance.release(self._shared_device(caller), None, "the model was told they left")
        return Result(data={"released": ended.user if ended else None}, labels=["owner"])

    def _shared_device(self, caller: Caller) -> str:
        device = self._kernel.accounts.device(caller.device) if caller.device else None
        if device is None or device.user is not None:
            raise ToolError("this only works in a conversation on a shared device, such as the TV")
        return device.id

    async def _ui_choices(self, args: dict[str, Any], caller: Caller) -> Result:
        options = [str(o) for o in args["options"]]
        return await self._show(caller, "choices", {"options": options}, ["owner"])

    async def _ui_show_feed(self, args: dict[str, Any], caller: Caller) -> Result:
        feeds = {f.id: f for f in await self._kernel.feeds()}
        feed = feeds.get(args["feed"])
        if feed is None:
            raise ToolError(f"no feed {args['feed']!r}{suggest(args['feed'], list(feeds))}")
        return await self._show(caller, "video_feed", {"src": feed.src, "label": feed.label, "caption": args.get("caption", "")}, ["house"])

    async def _ui_show_image(self, args: dict[str, Any], caller: Caller) -> Result:
        url = self._kernel.media.url_for(args["ref"])
        return await self._show(caller, "image", {"src": url, "caption": args.get("caption", "")}, ["house"])

    async def _show(self, caller: Caller, kind: str, args: dict[str, Any], labels: list[Label]) -> Result:
        shown = await self._kernel.conversations.show(caller.session, caller.user, kind, args)
        if not shown:
            raise ToolError("there is no conversation on screen to show this in; send it with huvu_notify instead")
        return Result(data={"shown": True}, labels=labels)


def _with_reason(schema: dict[str, Any]) -> dict[str, Any]:
    """Every action takes an optional `reason`, one line for the audit log
    and the approval the owner sees."""
    properties = dict(schema.get("properties") or {})
    properties["reason"] = {"type": "string", "description": "One line: why. Shown to the owner on approvals."}
    return {**schema, "properties": properties}


def _ago(text: str, now):
    """`2h` here means two hours ago."""
    raw = text.strip().lower()
    rel = RELATIVE_RE.match(raw)
    if rel:
        when = now - timedelta(seconds=int(rel.group(1)) * UNITS[rel.group(2)])
    elif raw in ("today", "yesterday"):
        when = _day(raw, now).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        when = parse_when(text, now)
    return when


def _day(text: str, now):
    raw = text.strip().lower()
    days = {"today": 0, "yesterday": 1}
    if raw in days:
        day = now - timedelta(days=days[raw])
    else:
        day = parse_when(raw if "T" in raw else f"{raw}T00:00", now)
    return day
