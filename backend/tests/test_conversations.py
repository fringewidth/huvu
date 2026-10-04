from __future__ import annotations

import asyncio

import pytest

from huvu.spec import Caller

from .fakes import FakeHermes

# Chats run in their user's Hermes profile, so the users must exist.
pytestmark = pytest.mark.usefixtures("family")


async def collect(kernel, live) -> list[tuple[str, dict]]:
    return [item async for item in kernel.conversations.subscribe(live)]


async def test_a_turn_streams_answer_parts_and_end(kernel, hermes: FakeHermes):
    async def show_choices(session_id: str) -> None:
        await kernel.tools.call("ui_choices", {"options": ["yes", "no"]}, Caller(kind="hermes", session=session_id, user="me"))

    hermes.script = [
        ("run.started", {"run_id": "run_9"}),
        ("assistant.delta", {"delta": "Searching. "}),
        ("tool.started", {"tool_name": "web_search", "args": {"query": "dentist saturday"}}),
        ("tool.completed", {"tool_name": "web_search", "preview": "[...]"}),
        ("tool.started", {"tool_name": "mcp__huvu__state_get", "args": {"values": ["clock.time"]}}),
        ("tool.completed", {"tool_name": "mcp__huvu__state_get", "preview": "{}"}),
        ("tool.started", {"tool_name": "mcp__huvu__ui_choices", "args": {"options": ["yes", "no"]}}),
        show_choices,
        ("tool.completed", {"tool_name": "mcp__huvu__ui_choices", "preview": "{}"}),
        ("tool.started", {"tool_name": "mcp__huvu__home_turn_on", "args": {"entity_id": "light.porch"}}),
        ("tool.completed", {"tool_name": "mcp__huvu__home_turn_on", "preview": "{}"}),
        ("tool.started", {"tool_name": "memory", "args": {}}),
        ("assistant.delta", {"delta": "Book one?"}),
        ("run.completed", {}),
    ]
    live = await kernel.conversations.ask("c1", "find me a dentist", user="me")
    events = await collect(kernel, live)
    names = [n for n, _ in events]
    assert names[0] == "turn.started" and names[-1] == "turn.ended"
    parts = [d["part"] for n, d in events if n == "part"]
    assert [p.get("toolName") for p in parts] == ["web_search", "choices", "home_turn_on"]
    assert parts[0]["args"] == {"query": "dentist saturday"}
    assert parts[1]["args"] == {"options": ["yes", "no"]}
    assert events[-1][1]["ending"] is None
    [turn] = kernel.conversations.turns("c1")
    assert turn.answer == "Searching. Book one?"
    assert not turn.running and len(turn.parts) == 3


async def test_a_turn_reaches_its_own_device_and_no_other(kernel, family, hermes):
    from .test_triggers import push_events

    same, same_task = await push_events(kernel, family.me_web.id)
    other, other_task = await push_events(kernel, family.me_phone.id)
    live = await kernel.conversations.ask(family.me_web.id, "hi", user="me")
    await collect(kernel, live)
    await asyncio.sleep(0)
    names = [n for n, _ in same]
    assert names[0] == "turn.started" and names[-1] == "turn.ended"
    assert other == []
    assert hermes.profiles[live.turn.hermes_session] == "me"
    owner = kernel.sessions.owner(live.turn.hermes_session)
    assert (owner.user, owner.device) == ("me", family.me_web.id)
    same_task.cancel()
    other_task.cancel()


async def test_a_household_device_chats_in_the_default_profile(kernel, family, hermes):
    live = await kernel.conversations.ask(family.tv.id, "hi", user=None, group="home")
    await collect(kernel, live)
    assert hermes.profiles[live.turn.hermes_session] is None
    # Another user can't continue a device's conversation.
    with pytest.raises(PermissionError):
        await kernel.conversations.ask(family.tv.id, "hi", user="me")


async def test_an_edit_starts_a_seeded_session(kernel, hermes):
    first = await kernel.conversations.ask("c1", "one", user="me")
    await collect(kernel, first)
    second = await kernel.conversations.ask("c1", "two", user="me")
    await collect(kernel, second)
    assert len(hermes.sessions) == 1
    edited = await kernel.conversations.ask("c1", "two, but better", user="me", replaces=second.turn.id)
    await collect(kernel, edited)
    assert len(hermes.sessions) == 2
    session, message = hermes.messages[-1]
    assert session == hermes.sessions[-1]
    assert "Owner: one" in message and "two, but better" in message and "Owner: two\n" not in message
    assert [t.ask for t in kernel.conversations.turns("c1")] == ["one", "two, but better"]


async def test_actions_ride_with_the_next_ask_and_survive_a_rewind(kernel, hermes):
    from datetime import datetime, timezone

    from huvu.conversations import Action

    at = datetime(2026, 10, 3, 13, 42, tzinfo=timezone.utc)
    opened = [Action(text="opened the live feed balcony", at=at)]
    first = await kernel.conversations.ask("c1", "one", user="me", actions=opened)
    await collect(kernel, first)
    session, message = hermes.messages[-1]
    assert "opened the live feed balcony" in message and "Not a request" in message and message.endswith("one")
    second = await kernel.conversations.ask("c1", "two", user="me")
    await collect(kernel, second)
    assert "Owner action" not in hermes.messages[-1][1]
    assert kernel.conversations.turns("c1")[0].actions == opened
    # Editing the second turn replays the first turn's action in the transcript.
    edited = await kernel.conversations.ask("c1", "two, but better", user="me", replaces=second.turn.id)
    await collect(kernel, edited)
    seeded = hermes.messages[-1][1]
    assert seeded.index("opened the live feed balcony") < seeded.index("Owner: one")


async def test_a_clients_turn_id_is_kept_and_used_once(kernel):
    import pytest

    live = await kernel.conversations.ask("c1", "hi", user="me", turn_id="t_mine")
    await collect(kernel, live)
    assert [t.id for t in kernel.conversations.turns("c1")] == ["t_mine"]
    with pytest.raises(ValueError):
        await kernel.conversations.ask("c1", "again", user="me", turn_id="t_mine")


async def test_stopping_a_turn_ends_it_as_stopped(kernel, hermes):
    gate = asyncio.Event()

    async def wait(session_id: str) -> None:
        await gate.wait()

    hermes.script = [("run.started", {"run_id": "run_5"}), ("assistant.delta", {"delta": "thinking"}), wait, ("run.completed", {})]
    live = await kernel.conversations.ask("c1", "slow one", user="me")
    await asyncio.sleep(0.01)
    await kernel.conversations.stop(live.turn.id)
    [turn] = kernel.conversations.turns("c1")
    assert turn.ending == "stopped" and hermes.stopped == ["run_5"]


async def test_ui_tools_without_a_live_turn_say_what_to_do(kernel):
    import pytest

    from huvu.tools import ToolError

    with pytest.raises(ToolError, match="huvu_notify"):
        await kernel.tools.call("ui_choices", {"options": ["a"]}, Caller(kind="hermes", session="nobody"))


async def test_images_get_media_urls(kernel):
    url = kernel.media.url_for("home:snap/1")
    assert url == kernel.media.url_for("home:snap/1")
    blob = await kernel.media.fetch(url.rsplit("/", 1)[1])
    assert blob.content_type == "image/jpeg"
