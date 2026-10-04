"""huvu's Hermes plugin: the Hermes side of the boundary.

Hermes connects to an MCP server once, with fixed headers, so the backend
can't tell which session a tool call came from. This plugin fills that in:

1. Every huvu tool call gets `_session` (the Hermes session). The backend
   strips it.
2. At the start of a session, the situation block (time, chosen values,
   pending approvals) is added to the context, fetched from the backend.

Settings come from the environment Hermes runs in:
    HUVU_URL              the backend, default http://localhost:3001
    HUVU_MCP_TOKEN        the same token the backend checks on /mcp
    HUVU_MCP_SERVER       the server's name in Hermes's mcp_servers, default huvu
    HUVU_SITUATION        comma-separated value keys for the situation block
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

SERVER = os.environ.get("HUVU_MCP_SERVER", "huvu")
PREFIX = f"mcp__{SERVER}__"
URL = os.environ.get("HUVU_URL", "http://localhost:3001").rstrip("/")
TOKEN = os.environ.get("HUVU_MCP_TOKEN", "")
SITUATION = [v.strip() for v in os.environ.get("HUVU_SITUATION", "huvu.mode").split(",") if v.strip()]


def _on_pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", **_: Any) -> dict[str, Any] | None:
    if not tool_name.startswith(PREFIX):
        return None
    return {"action": "modify", "args": {"_session": session_id}}


def _on_pre_llm_call(session_id: str = "", is_first_turn: bool = False, **_: Any) -> dict[str, str] | None:
    """The situation block, once, on a session's first turn, so the prompt
    cache stays intact for the rest of it."""
    if not is_first_turn:
        return None
    try:
        text = _situation()
    except Exception as err:
        logger.warning("huvu: no situation block: %s", err)
        text = ""
    return {"context": f"[huvu situation]\n{text}"} if text else None


def _situation() -> str:
    request = urllib.request.Request(
        f"{URL}/api/situation",
        data=json.dumps({"values": SITUATION}).encode(),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=3) as res:
        return json.loads(res.read())["text"]


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
