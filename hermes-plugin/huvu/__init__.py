"""huvu's Hermes plugin: the Hermes side of the boundary.

Hermes connects to an MCP server once, with fixed headers, so the backend
can't tell which session a tool call came from. This plugin fills that in:

1. Every huvu tool call gets `_session` (the Hermes session). The backend
   strips it.
2. At the start of a session, the situation block (time, who huvu helps,
   the family mode, pending approvals) is added to the context, fetched
   from the backend.
3. Every session's system prompt gets GUIDE (from backend/src/huvu/prompts.py): how huvu works, and what to do
   with it. Hermes never shows the model an MCP server's own instructions,
   so this is the one place huvu can say it.
4. The model's reasoning summary, as it streams, goes to the backend
   (`POST /api/thinking`), so the stage can show it while a turn runs. It
   needs `plugins.stream_reasoning_deltas` on, which setup.sh sets.
5. Hermes's browser tools run only in a session on pro or advanced: a
   conversation's main session, or a wake on either tier. Before each
   browser call, the backend says whether the session may
   (`POST /api/browsing`); a no, or no answer, blocks the call, and the
   model reads why.

Settings come from the environment Hermes runs in:
    HUVU_URL              the backend, default http://localhost:3001
    HUVU_MCP_TOKEN        the same token the backend checks on /mcp
    HUVU_MCP_SERVER       the server's name in Hermes's mcp_servers, default huvu

One gateway process serves every profile, so the process environment holds
only the default profile's values. HUVU_URL and HUVU_MCP_TOKEN are read at
call time from the routed profile's secrets, the way Hermes reads the
`${HUVU_MCP_TOKEN}` in the MCP headers. Read once at import, every profile
would send the household's token and be told it serves a shared device.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _load_prompts():
    """huvu's prompts.py, every text a model reads from huvu, loaded by path:
    the plugin runs in Hermes's Python, where the backend isn't installed.
    The plugin is linked into each profile, so its real path is in the repo."""
    path = Path(__file__).resolve().parents[2] / "backend" / "src" / "huvu" / "prompts.py"
    spec = importlib.util.spec_from_file_location("huvu_prompts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prompts = _load_prompts()
GUIDE = prompts.GUIDE

SERVER = os.environ.get("HUVU_MCP_SERVER", "huvu")
PREFIX = f"mcp__{SERVER}__"


# Hermes's own browser tools all start with this.
BROWSER = "browser_"


def _on_pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", **_: Any) -> dict[str, Any] | None:
    if tool_name.startswith(BROWSER):
        return _browsing(session_id)
    if not tool_name.startswith(PREFIX):
        return None
    return {"action": "modify", "args": {"_session": session_id}}


def _browsing(session_id: str) -> dict[str, Any] | None:
    """None lets a browser call through; a block carries why. Without an
    answer from the backend, the call is blocked."""
    try:
        answer = _post("/api/browsing", {"session": session_id})
    except Exception as err:
        logger.warning("huvu: browser call blocked, the backend didn't answer: %s", err)
        answer = {"allowed": False, "why": prompts.BROWSER_BLOCKED}
    return None if answer.get("allowed") else {"action": "block", "message": answer.get("why") or prompts.BROWSER_BLOCKED}


# The model response each session's reasoning last came from, so the first
# delta of a new one starts a new burst on the stage. Hermes runs each hook
# on its own worker thread, so the delta hook tracks this itself rather
# than trusting on_stream_start to arrive first.
_bursts: dict[str, tuple[Any, Any]] = {}


def _on_stream_delta(delta: str = "", kind: str = "", session_id: str = "", turn_id: Any = None, iteration: Any = None, **_: Any) -> None:
    if kind != "reasoning" or not delta or not session_id:
        return
    burst = (turn_id, iteration)
    start = _bursts.get(session_id) != burst
    _bursts[session_id] = burst
    try:
        _post("/api/thinking", {"session": session_id, "text": delta, "start": start})
    except Exception as err:
        logger.debug("huvu: thinking not sent: %s", err)


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
    return {"context": f"{prompts.SITUATION_HEADER}\n{text}"} if text else None


def _secret(name: str, default: str = "") -> str:
    """A setting of the profile this turn runs in."""
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        # Older Hermes builds have no profile scopes.
        return os.environ.get(name, default)
    return get_secret(name, default) or default


def _situation() -> str:
    return _post("/api/situation", {})["text"]


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """POSTs to the backend as this turn's profile."""
    url = _secret("HUVU_URL", "http://localhost:3001").rstrip("/")
    token = _secret("HUVU_MCP_TOKEN")
    request = urllib.request.Request(
        f"{url}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=3) as res:
        return json.loads(res.read())




def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("on_stream_delta", _on_stream_delta)
    # Older Hermes builds have no system prompt sections.
    if hasattr(ctx, "register_system_prompt_section"):
        ctx.register_system_prompt_section("huvu", GUIDE)
