"""The Hermes plugin's hooks, called the way Hermes calls them."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "hermes-plugin" / "huvu" / "__init__.py"


def load():
    spec = importlib.util.spec_from_file_location("huvu_hermes_plugin", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_huvu_calls_carry_the_session_and_taint():
    plugin = load()
    out = plugin._on_pre_tool_call(tool_name="mcp__huvu__state_get", args={"values": []}, session_id="s1")
    assert out == {"action": "modify", "args": {"_session": "s1", "_tainted": False}}
    assert plugin._on_pre_tool_call(tool_name="terminal", args={}, session_id="s1") is None


def test_outside_content_taints_and_gates_memory_writes():
    plugin = load()
    assert plugin._on_pre_tool_call(tool_name="memory", args={}, session_id="s2") is None
    wrapped = json.dumps({"result": json.dumps({"data": {}, "taint": "external"})})
    plugin._on_transform_tool_result(tool_name="mcp__huvu__home_snapshot", result=wrapped, session_id="s2")
    gate = plugin._on_pre_tool_call(tool_name="memory", args={}, session_id="s2")
    assert gate["action"] == "approve"
    flagged = plugin._on_pre_tool_call(tool_name="mcp__huvu__state_get", args={}, session_id="s2")
    assert flagged["args"]["_tainted"] is True


def test_web_tools_taint_too():
    plugin = load()
    plugin._on_transform_tool_result(tool_name="web_search", result="...", session_id="s3")
    plugin._on_transform_tool_result(tool_name="browser_navigate", result="...", session_id="s4")
    assert plugin._is_tainted("s3") and plugin._is_tainted("s4")
    plugin._on_transform_tool_result(tool_name="mcp__huvu__state_get", result='{"clock.time": {}}', session_id="s5")
    assert not plugin._is_tainted("s5")


def test_the_situation_block_comes_once(monkeypatch):
    plugin = load()
    monkeypatch.setattr(plugin, "_situation", lambda: "Now: Sat 00:10")
    assert plugin._on_pre_llm_call(session_id="s1", is_first_turn=True) == {"context": "[huvu situation]\nNow: Sat 00:10"}
    assert plugin._on_pre_llm_call(session_id="s1", is_first_turn=False) is None
