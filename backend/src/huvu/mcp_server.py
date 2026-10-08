"""The MCP server Hermes connects to. Every tool comes from the tool box.

Whose Hermes is calling comes from the profile's token (see auth.py): one
profile per user, and one per group for its shared devices (the default
group's is Hermes's default profile). The huvu Hermes plugin
adds `_session` (the Hermes session making the call) to every huvu tool
call, which names the device whose conversation it is. Both become the
call's Caller. A shared device's session acts for whoever is recognized
there (see attendance.py). Without the plugin, calls still work; they just
carry no session and no device.

Results go back with every timestamp made readable (see times.py). A result
with outside content is wrapped and marked `"provenance": "external"`, so
the model knows a stranger wrote it.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import mcp_types as types
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from .auth import mcp_profile
from .prompts import MCP_INSTRUCTIONS as INSTRUCTIONS
from .spec import Caller
from .times import for_model
from .tools import ToolError

if TYPE_CHECKING:
    from .kernel import Kernel

log = logging.getLogger("huvu.mcp")



def build(kernel: Kernel) -> StreamableHTTPSessionManager:
    async def list_tools(ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        tools = [types.Tool(name=t.name, description=t.description, input_schema=t.input_schema) for t in kernel.tools.list()]
        return types.ListToolsResult(tools=tools)

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        args = dict(params.arguments or {})
        user, group = mcp_profile.get()
        caller = caller_from(kernel, args, user, group)
        try:
            result = await kernel.tools.call(params.name, args, caller)
            body: Any = for_model(result.data, kernel.now())
            if "external" in result.labels:
                body = {"data": body, "provenance": "external"}
            out = types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(body, ensure_ascii=False))])
        except ToolError as err:
            out = types.CallToolResult(content=[types.TextContent(type="text", text=str(err))], is_error=True)
        except Exception as err:
            log.exception("tool %s failed", params.name)
            out = types.CallToolResult(content=[types.TextContent(type="text", text=f"{params.name} failed inside huvu: {err}")], is_error=True)
        return out

    server = Server(
        "huvu",
        version="0.0.0",
        instructions=INSTRUCTIONS.format(index=kernel.registry.index()),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    return StreamableHTTPSessionManager(app=server, stateless=True)


def caller_from(kernel: Kernel, args: dict[str, Any], user: str | None, group: str | None) -> Caller:
    """Takes the plugin's arguments out of `args`, in place. `user` and
    `group` are the calling profile's. A session that belongs to another
    profile is ignored, so one profile can't reach into another's
    conversation."""
    session = args.pop("_session", None)
    owner = kernel.sessions.owner(session)
    if owner is not None and (owner.user, owner.group) != (user, group):
        log.warning("profile of %s sent session %s, which isn't its own; ignored", user or group, session)
        session, owner = None, None
    device = owner.device if owner else None
    if user is None:
        user = kernel.attendance.holder(device)
    return Caller(kind="hermes", session=session, user=user, device=device, group=group)
