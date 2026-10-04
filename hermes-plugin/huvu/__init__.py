"""huvu's Hermes plugin: the Hermes side of the boundary.

Hermes connects to an MCP server once, with fixed headers, so the backend
can't tell which session a tool call came from. This plugin fills that in
and keeps session taint, which is Hermes's to track:

1. Every huvu tool call gets `_session` (the Hermes session) and `_tainted`
   (whether the session has read outside content). The backend strips both.
2. A session is tainted from the moment a tool result carries outside
   content: a huvu result marked `"taint": "external"`, or one of Hermes's
   own tools that reads the outside world (web search, the browser).
3. In a tainted session, memory and skill writes go to the human-approval
   gate, so text a stranger wrote can't become a lasting memory.
4. At the start of a session, the situation block (time, chosen values,
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
import re
import threading
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

SERVER = os.environ.get("HUVU_MCP_SERVER", "huvu")
PREFIX = f"mcp__{SERVER}__"
URL = os.environ.get("HUVU_URL", "http://localhost:3001").rstrip("/")
TOKEN = os.environ.get("HUVU_MCP_TOKEN", "")
SITUATION = [v.strip() for v in os.environ.get("HUVU_SITUATION", "huvu.mode").split(",") if v.strip()]

# Hermes's own tools whose results come from outside the house.
OUTSIDE_TOOLS = {"web_search", "web_extract", "vision_analyze"}
OUTSIDE_PREFIXES = ("browser_",)
# Writes that last past the session.
LASTING_WRITES = {"memory", "skill_manage"}

# Hermes may wrap the backend's JSON in its own, escaping the quotes.
TAINT_RE = re.compile(r'\\?"taint\\?"\s*:\s*\\?"external')

_tainted: set[str] = set()
_lock = threading.Lock()


def _is_tainted(session_id: str) -> bool:
    with _lock:
        return session_id in _tainted


def _taint(session_id: str) -> None:
    if session_id:
        with _lock:
            _tainted.add(session_id)


def _on_pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", **_: Any) -> dict[str, Any] | None:
    directive: dict[str, Any] | None = None
    if tool_name.startswith(PREFIX):
        directive = {"action": "modify", "args": {"_session": session_id, "_tainted": _is_tainted(session_id)}}
    elif tool_name in LASTING_WRITES and _is_tainted(session_id):
        directive = {
            "action": "approve",
            "message": f"This session read content from outside the house. Allow this {tool_name} write?",
            "rule_key": f"huvu-tainted-{tool_name}",
        }
    return directive


def _on_transform_tool_result(tool_name: str = "", result: Any = None, session_id: str = "", **_: Any) -> None:
    """Only watches; returning None leaves the result as it is."""
    outside = tool_name in OUTSIDE_TOOLS or tool_name.startswith(OUTSIDE_PREFIXES)
    if tool_name.startswith(PREFIX) and isinstance(result, str):
        outside = bool(TAINT_RE.search(result))
    if outside:
        _taint(session_id)
    return None


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
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
