"""The tool box: the one tool surface Hermes and trigger scripts share.

Kernel tools come first (data, triggers, ui, people). Then every source
action becomes a tool named `<source>_<name>`, built from the source's
spec, which goes through the policy. There is one way to read: data_sql,
over every source's tables, what happened and what is true now alike.

Hermes sees the kernel tools, plus `act`, which runs any source action by
name. The per-source tools stay off MCP, so a new driver
adds nothing to a session's tool list: what a source can do is in its
Hermes skill (see skills.py), loaded when needed. Scripts still call the
per-source tools by name, so their grants stay per action.

Hermes prefixes each one as `mcp__huvu__<name>`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .accounts import AuthError, Device
from .data import DataError
from .prompts import ACT_REASON, ACTION_REASON, DRY_RUN, NEEDS_TAP, TOOLS, signature
from .registry import UnknownName, suggest
from .scripts import ScriptError
from .source import SourceError
from .spec import Caller, Label, Result
from .triggers import TriggerError, TriggerSpec
from .wake import WakeEnded

if TYPE_CHECKING:
    from .kernel import Kernel

log = logging.getLogger("huvu.tools")

Handler = Callable[[dict[str, Any], Caller], Awaitable[Result]]

# Source names that would collide with kernel tool names.
RESERVED = {"triggers", "ui", "kernel", "people", "act", "data", "wake"}


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
    # Listed to Hermes over MCP. Per-source actions aren't; `act` reaches
    # them.
    exposed: bool = True


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


TRIGGER_SCHEMA = TriggerSpec.model_json_schema()
REGISTER_SCHEMA = {**TRIGGER_SCHEMA, "properties": {**TRIGGER_SCHEMA["properties"], "dry_run": {"type": "boolean", "description": DRY_RUN}}}


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
            for action in spec.actions:
                schema = _with_reason(action.input_schema)
                desc = action.description + (NEEDS_TAP if action.requires_human else "")
                handler = self._action_handler(spec.name, action.name)
                self._add(Tool(f"{spec.name}_{action.name}", desc, schema, handler, writes=True, exposed=False))

    def _add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"two tools are named {tool.name!r}")
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def list(self) -> list[Tool]:
        """What Hermes sees."""
        return [t for t in self._tools.values() if t.exposed]

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
        except (UnknownName, SourceError, TriggerError, ScriptError, ValidationError, AuthError, DataError, WakeEnded) as err:
            raise ToolError(str(err)) from err
        return result

    def _action_handler(self, source: str, name: str) -> Handler:
        async def handle(args: dict[str, Any], caller: Caller) -> Result:
            clean = dict(args)
            reason = clean.pop("reason", None)
            return await self._kernel.policy.request(source, name, clean, caller, reason)

        return handle

    def _kernel_tools(self) -> list[Tool]:
        return [
            Tool(
                "act",
                TOOLS["act"],
                _schema({"name": {"type": "string"}, "args": {"type": "object"}, "reason": {"type": "string", "description": ACT_REASON}}, ["name"]),
                self._act,
                writes=True,
            ),
            Tool(
                "data_sql",
                TOOLS["data_sql"],
                _schema({"sql": {"type": "string"}, "limit": {"type": "integer"}}, ["sql"]),
                self._data_sql,
            ),
            Tool(
                "triggers_register",
                TOOLS["triggers_register"],
                REGISTER_SCHEMA,
                self._triggers_register,
                writes=True,
            ),
            Tool("triggers_remove", TOOLS["triggers_remove"], _schema({"id": {"type": "string"}}, ["id"]), self._triggers_remove, writes=True),
            Tool(
                "triggers_pause",
                TOOLS["triggers_pause"],
                _schema({"id": {"type": "string"}, "reason": {"type": "string"}}, ["id"]),
                self._triggers_pause,
                writes=True,
            ),
            Tool(
                "triggers_resume",
                TOOLS["triggers_resume"],
                _schema({"id": {"type": "string"}}, ["id"]),
                self._triggers_resume,
                writes=True,
            ),
            Tool(
                "people_group",
                TOOLS["people_group"],
                _schema({"group": {"type": "string"}}),
                self._people_group,
            ),
            Tool(
                "people_identify",
                TOOLS["people_identify"],
                _schema({"user": {"type": "string"}}, ["user"]),
                self._people_identify,
                writes=True,
            ),
            Tool(
                "people_release",
                TOOLS["people_release"],
                _schema({}),
                self._people_release,
                writes=True,
            ),
            Tool(
                "ui_choices",
                TOOLS["ui_choices"],
                _schema({"options": {"type": "array", "items": {"type": "string"}}}, ["options"]),
                self._ui_choices,
            ),
            Tool(
                "ui_show_feed",
                TOOLS["ui_show_feed"],
                _schema({"feed": {"type": "string"}, "caption": {"type": "string"}}, ["feed"]),
                self._ui_show_feed,
            ),
            Tool(
                "wake_wait",
                TOOLS["wake_wait"],
                _schema({"waits_for": {"type": "string"}}, ["waits_for"]),
                self._wake_wait,
                writes=True,
            ),
            Tool(
                "ui_show_image",
                TOOLS["ui_show_image"],
                _schema({"src": {"type": "string"}, "caption": {"type": "string"}}, ["src"]),
                self._ui_show_image,
            ),
        ]

    async def _act(self, args: dict[str, Any], caller: Caller) -> Result:
        tool = self._named(str(args.get("name", "")))
        given = dict(args.get("args") or {})
        if args.get("reason"):
            given["reason"] = args["reason"]
        self._check_args(tool, given)
        return await self.call(tool.name, given, caller)

    def _named(self, name: str) -> Tool:
        """A source action by name, or an error naming the closest."""
        candidates = [t.name for t in self._tools.values() if not t.exposed]
        tool = self._tools.get(name)
        if tool is None or tool.exposed:
            raise ToolError(f"no action named {name!r}{suggest(name, candidates)}; each source's skill (huvu-<source>) lists them")
        return tool

    @staticmethod
    def _check_args(tool: Tool, given: dict[str, Any]) -> None:
        properties = tool.input_schema.get("properties") or {}
        missing = [k for k in tool.input_schema.get("required", []) if k not in given]
        unknown = [k for k in given if properties and k not in properties]
        if missing or unknown:
            problem = f"needs {', '.join(missing)}" if missing else f"takes no {', '.join(unknown)}"
            raise ToolError(f"{tool.name} {problem}; it is {signature(tool.name, tool.input_schema)}")

    async def _data_sql(self, args: dict[str, Any], caller: Caller) -> Result:
        viewer = self._kernel.accounts.scope(caller.user, caller.group)
        answer = await self._kernel.data.sql(str(args["sql"]), viewer, int(args.get("limit") or 200))
        return Result(data=answer.public(), labels=answer.labels or ["house"])

    async def _triggers_register(self, args: dict[str, Any], caller: Caller) -> Result:
        given = dict(args)
        if given.pop("dry_run", False):
            report = await self._kernel.triggers.dry_run(TriggerSpec.model_validate(given), caller)
            return Result(data=report, labels=["house"])
        trigger = await self._kernel.triggers.register(TriggerSpec.model_validate(given), caller)
        data = trigger.public()
        if trigger.status == "unconfirmed":
            data["note_for_you"] = (
                "Not live yet. Everyone who uses this device was asked on their phone whether it's them; it goes live, as theirs, "
                "only once one of them taps yes, and is dropped if nobody does within 5 minutes. Tell the person to check their phone and tap yes."
            )
        return Result(data=data, labels=["house"])

    async def _triggers_pause(self, args: dict[str, Any], caller: Caller) -> Result:
        trigger = await self._kernel.triggers.pause(args["id"], caller, args.get("reason"))
        return Result(data=trigger.public(), labels=["house"])

    async def _triggers_resume(self, args: dict[str, Any], caller: Caller) -> Result:
        trigger = await self._kernel.triggers.resume(args["id"], caller)
        return Result(data=trigger.public(), labels=["house"])

    async def _triggers_remove(self, args: dict[str, Any], caller: Caller) -> Result:
        trigger = await self._kernel.triggers.remove(args["id"], caller)
        return Result(data=trigger.public(), labels=["house"])

    async def _wake_wait(self, args: dict[str, Any], caller: Caller) -> Result:
        until = self._kernel.waker.wait(caller.session, str(args["waits_for"]))
        return Result(data={"waits_until": until}, labels=["house"])

    async def _people_group(self, args: dict[str, Any], caller: Caller) -> Result:
        """The caller's groups, or one of them, and who is in each, by what
        the caller calls them."""
        accounts = self._kernel.accounts
        mine = list(accounts.memberships(caller.user)) if caller.user else [caller.group or accounts.default_group()]
        wanted = args.get("group")
        if wanted is not None and wanted not in mine:
            raise ToolError(f"you aren't in a group {wanted!r}; your groups: {', '.join(mine) or 'none'}")
        groups = []
        for group_id in [wanted] if wanted else mine:
            group = accounts.group(group_id)
            people = [
                {
                    "id": u.id,
                    "alias": accounts.alias(caller.user, u.id),
                    "role": accounts.role(group_id, u.id),
                    "you": u.id == caller.user,
                }
                for u in accounts.people(group_id)
            ]
            groups.append(
                {
                    "id": group_id,
                    "name": group.name if group else group_id,
                    "people": people,
                }
            )
        return Result(data={"groups": groups}, labels=["owner"])

    async def _people_identify(self, args: dict[str, Any], caller: Caller) -> Result:
        device = self._shared_device(caller)
        wanted = str(args["user"]).strip().lower()
        users = self._kernel.accounts.people(device.group or self._kernel.accounts.default_group())
        user = next((u for u in users if wanted in (u.id, u.name.lower())), None)
        if user is None:
            raise ToolError(f"nobody called {args['user']!r} uses this device; the people who do: {', '.join(f'{u.name} ({u.id})' for u in users)}")
        check = await self._kernel.attendance.ask(device.id, user.id, "model")
        status = check.status if check else "already_asked"
        return Result(data={"asked": user.name, "status": status, "say": "Tell them to check their phone and tap yes."}, labels=["owner"])

    async def _people_release(self, args: dict[str, Any], caller: Caller) -> Result:
        ended = await self._kernel.attendance.release(self._shared_device(caller).id, None, "the model was told they left")
        return Result(data={"released": ended.user if ended else None}, labels=["owner"])

    def _shared_device(self, caller: Caller) -> Device:
        device = self._kernel.accounts.device(caller.device)
        if device is None or device.user is not None:
            raise ToolError("this only works in a conversation on a shared device, such as the TV")
        return device

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
        url = self._kernel.media.url_for(args["src"])
        return await self._show(caller, "image", {"src": url, "caption": args.get("caption", "")}, ["house"])

    async def _show(self, caller: Caller, kind: str, args: dict[str, Any], labels: list[Label]) -> Result:
        shown = await self._kernel.conversations.show(caller, kind, args)
        if not shown:
            raise ToolError("there is no conversation on screen to show this in; send it with huvu_notify instead")
        return Result(data={"shown": True}, labels=labels)


def _with_reason(schema: dict[str, Any]) -> dict[str, Any]:
    """Every action takes an optional `reason`, one line for the audit log
    and the approval the owner sees."""
    properties = dict(schema.get("properties") or {})
    properties["reason"] = {"type": "string", "description": ACTION_REASON}
    return {**schema, "properties": properties}
