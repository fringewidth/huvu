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
    out = plugin._on_pre_tool_call(tool_name="mcp__huvu__data_sql", args={"sql": "SELECT 1"}, session_id="s1")
    assert out == {"action": "modify", "args": {"_session": "s1"}}
    assert plugin._on_pre_tool_call(tool_name="terminal", args={}, session_id="s1") is None
    assert plugin._on_pre_tool_call(tool_name="memory", args={}, session_id="s1") is None


def test_the_situation_block_comes_once(monkeypatch):
    plugin = load()
    monkeypatch.setattr(plugin, "_situation", lambda: "Now: Sat 00:10")
    assert plugin._on_pre_llm_call(session_id="s1", is_first_turn=True) == {"context": "[huvu situation]\nNow: Sat 00:10"}
    assert plugin._on_pre_llm_call(session_id="s1", is_first_turn=False) is None


def test_the_guide_goes_into_every_session_prompt():
    plugin = load()

    class Ctx:
        def __init__(self) -> None:
            self.sections: dict[str, str] = {}
            self.hooks: list[str] = []

        def register_hook(self, name, fn) -> None:
            self.hooks.append(name)

        def register_system_prompt_section(self, id, content) -> None:
            self.sections[id] = content

    ctx = Ctx()
    plugin.register(ctx)
    assert ctx.hooks == ["pre_tool_call", "pre_llm_call", "on_stream_delta"]
    assert "huvu-<source>" in ctx.sections["huvu"] and "huvu_notify" in ctx.sections["huvu"]
    assert len(ctx.sections["huvu"]) <= 4000


def test_reasoning_deltas_go_to_the_backend_in_bursts(monkeypatch):
    plugin = load()
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(plugin, "_post", lambda path, body: sent.append((path, body)) or {})
    plugin._on_stream_delta(delta="Weighing ", kind="reasoning", session_id="s1", turn_id="t1", iteration=1)
    plugin._on_stream_delta(delta="a script.", kind="reasoning", session_id="s1", turn_id="t1", iteration=1)
    # The answer's own text isn't thinking.
    plugin._on_stream_delta(delta="Done.", kind="text", session_id="s1", turn_id="t1", iteration=1)
    # The next model response in the same turn starts a new burst.
    plugin._on_stream_delta(delta="Checking.", kind="reasoning", session_id="s1", turn_id="t1", iteration=2)
    assert sent == [
        ("/api/thinking", {"session": "s1", "text": "Weighing ", "start": True}),
        ("/api/thinking", {"session": "s1", "text": "a script.", "start": False}),
        ("/api/thinking", {"session": "s1", "text": "Checking.", "start": True}),
    ]
