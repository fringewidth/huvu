"""The Hermes plugin's hooks, called the way Hermes calls them."""

from __future__ import annotations

import importlib.util
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "hermes-plugin" / "huvu" / "__init__.py"


def load():
    spec = importlib.util.spec_from_file_location("huvu_hermes_plugin", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_huvu_calls_carry_the_session():
    plugin = load()
    out = plugin._on_pre_tool_call(tool_name="mcp__huvu__state_get", args={"values": []}, session_id="s1")
    assert out == {"action": "modify", "args": {"_session": "s1"}}
    assert plugin._on_pre_tool_call(tool_name="terminal", args={}, session_id="s1") is None
    assert plugin._on_pre_tool_call(tool_name="memory", args={}, session_id="s1") is None


def test_the_situation_block_comes_once(monkeypatch):
    plugin = load()
    monkeypatch.setattr(plugin, "_situation", lambda: "Now: Sat 00:10")
    assert plugin._on_pre_llm_call(session_id="s1", is_first_turn=True) == {"context": "[huvu situation]\nNow: Sat 00:10"}
    assert plugin._on_pre_llm_call(session_id="s1", is_first_turn=False) is None
