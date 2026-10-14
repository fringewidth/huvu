"""The Hermes client's run route, against a stand-in for Hermes's API server."""

from __future__ import annotations

import json

import httpx
import pytest

from huvu.hermes import HermesClient, HermesError, Profile

PROFILE = Profile(name="me", key="key-me")


def client(handler) -> HermesClient:
    hermes = HermesClient("http://hermes")
    hermes._http = httpx.AsyncClient(base_url="http://hermes", transport=httpx.MockTransport(handler))
    return hermes


def frames(*events: dict) -> bytes:
    return b"".join(f"id: {i}\ndata: {json.dumps(e)}\n\n".encode() for i, e in enumerate(events)) + b": keepalive\n\n"


async def test_a_run_sends_its_history_and_streams_under_chats_names():
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer key-me"
        if request.method == "POST":
            assert request.url.path == "/p/me/v1/runs"
            sent.append(json.loads(request.content))
            return httpx.Response(202, json={"run_id": "run_9", "status": "queued"})
        assert request.url.path == "/p/me/v1/runs/run_9/events"
        return httpx.Response(200, content=frames(
            {"event": "tool.started", "tool": "delegate_task"},
            {"event": "message.delta", "delta": "Learned "},
            {"event": "message.delta", "delta": "a lot."},
            {"event": "run.completed", "output": "Learned a lot."},
            {"event": "message.delta", "delta": "never read"},
        ))

    hermes = client(handler)
    history = [{"role": "user", "content": "[huvu situation]\nNow: Tue"}]
    events = [(e.name, e.data) async for e in hermes.run(PROFILE, "wake-1", history, "Your dream.", model="gpt-6-sol")]
    assert sent == [{"session_id": "wake-1", "input": [*history, {"role": "user", "content": "Your dream."}], "model": "gpt-6-sol"}]
    assert [n for n, _ in events] == ["run.started", "tool.started", "assistant.delta", "assistant.delta", "assistant.completed", "run.completed"]
    assert events[0][1] == {"run_id": "run_9"}
    assert events[4][1] == {"content": "Learned a lot."}
    await hermes.close()



async def test_a_runs_tool_events_come_as_the_session_streams_do():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "run_3"})
        return httpx.Response(200, content=frames(
            {"event": "tool.started", "tool": "browser_navigate", "preview": "keka.com"},
            {"event": "tool.completed", "tool": "browser_navigate", "duration": 1.2, "error": False},
            {"event": "tool.started", "tool": "browser_click"},
            {"event": "tool.completed", "tool": "browser_click", "duration": 0.1, "error": True},
            {"event": "run.completed", "output": "done"},
        ))

    hermes = client(handler)
    events = [(e.name, e.data) async for e in hermes.run(PROFILE, "wake-3", [], "go")]
    assert events[1:5] == [
        ("tool.started", {"tool_name": "browser_navigate", "preview": "keka.com", "args": {}}),
        ("tool.completed", {"tool_name": "browser_navigate", "preview": None}),
        ("tool.started", {"tool_name": "browser_click", "preview": None, "args": {}}),
        ("tool.failed", {"tool_name": "browser_click", "preview": None}),
    ]
    await hermes.close()

async def test_a_run_ends_on_failure_and_a_refusal_raises():
    def failing(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "run_1"})
        return httpx.Response(200, content=frames({"event": "run.failed", "error": "rate limited"}))

    hermes = client(failing)
    events = [e async for e in hermes.run(PROFILE, "wake-2", [{"role": "user", "content": "x"}], "go")]
    assert [e.name for e in events] == ["run.started", "run.failed"] and events[1].data["error"] == "rate limited"

    hermes = client(lambda request: httpx.Response(429, text="Too many concurrent runs"))
    with pytest.raises(HermesError, match="429"):
        [e async for e in hermes.run(PROFILE, "wake-3", [{"role": "user", "content": "x"}], "go")]
