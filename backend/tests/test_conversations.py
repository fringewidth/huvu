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
        ("tool.started", {"tool_name": "mcp__huvu__act", "args": {"name": "home_turn_on", "args": {"entity_id": "light.porch"}}}),
        ("tool.completed", {"tool_name": "mcp__huvu__act", "preview": "{}"}),
        ("tool.started", {"tool_name": "mcp__huvu__data_sql", "args": {"sql": "SELECT count(*) FROM events"}}),
        ("tool.completed", {"tool_name": "mcp__huvu__data_sql", "preview": "{}"}),
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


async def test_words_beside_tool_calls_open_the_answer(kernel, hermes: FakeHermes):
    hermes.script = [
        ("run.started", {"run_id": "run_c"}),
        # Hermes sent the preamble as deltas already, then marks it.
        ("assistant.delta", {"delta": "On it, setting that up."}),
        ("assistant.commentary", {"text": "On it, setting that up.", "already_streamed": True}),
        ("tool.started", {"tool_name": "mcp__huvu__triggers_list", "args": {}}),
        ("tool.completed", {"tool_name": "mcp__huvu__triggers_list", "preview": "[]"}),
        # A preamble sent only as commentary.
        ("assistant.commentary", {"text": "Checking the clock too."}),
        ("assistant.delta", {"delta": "Done: every day at 9."}),
        ("run.completed", {}),
    ]
    live = await kernel.conversations.ask("c1", "remind me to water the plants at 9", user="me")
    events = await collect(kernel, live)
    shown = "".join(d["text"] for n, d in events if n == "answer.delta")
    [turn] = kernel.conversations.turns("c1")
    assert turn.answer == shown == "On it, setting that up.\n\nChecking the clock too.\n\nDone: every day at 9."


async def test_thinking_reaches_the_turn_and_stays_out_of_the_answer(kernel, family, hermes: FakeHermes):
    shown: list[bool] = []

    async def think(session_id: str) -> None:
        shown.append(kernel.conversations.think(session_id, "Weighing a script ", True, "me", None))
        shown.append(kernel.conversations.think(session_id, "against a wake.", False, "me", None))
        # Another profile can't put words in this turn.
        shown.append(kernel.conversations.think(session_id, "nope", False, "mom", None))

    hermes.script = [("run.started", {"run_id": "run_t"}), think, ("assistant.delta", {"delta": "Done."}), ("run.completed", {})]
    live = await kernel.conversations.ask("c1", "remind me at 9", user="me")
    events = await collect(kernel, live)
    assert shown == [True, True, False]
    thoughts = [d for n, d in events if n == "thinking.delta"]
    assert [(d["text"], d["start"]) for d in thoughts] == [("Weighing a script ", True), ("against a wake.", False)]
    [turn] = kernel.conversations.turns("c1")
    assert turn.answer == "Done."


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
    assert "Them: one" in message and "two, but better" in message and "Them: two\n" not in message
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
    assert seeded.index("opened the live feed balcony") < seeded.index("Them: one")


async def test_a_clients_turn_id_is_kept_and_used_once(kernel):
    import pytest

    live = await kernel.conversations.ask("c1", "hi", user="me", turn_id="t_mine")
    await collect(kernel, live)
    assert [t.id for t in kernel.conversations.turns("c1")] == ["t_mine"]
    with pytest.raises(ValueError):
        await kernel.conversations.ask("c1", "again", user="me", turn_id="t_mine")


async def stopped_when(hermes, *, then: list) -> list:
    """A script that streams a little, waits for a stop, then does `then`."""

    async def wait(session_id: str) -> None:
        await hermes.stop_requested.wait()

    return [("run.started", {"run_id": "run_5"}), ("assistant.delta", {"delta": "thinking"}), wait, *then]


async def test_a_stop_keeps_what_hermes_sends_until_it_ends(kernel, hermes):
    hermes.script = await stopped_when(hermes, then=[("assistant.delta", {"delta": " and more"}), ("run.cancelled", {})])
    live = await kernel.conversations.ask("c1", "slow one", user="me")
    await asyncio.sleep(0.01)
    await kernel.conversations.stop(live.turn.id, wait=True)
    [turn] = kernel.conversations.turns("c1")
    assert turn.ending == "stopped" and turn.answer == "thinking and more" and hermes.stopped == ["run_5"]


async def test_a_turn_hermes_finishes_anyway_is_complete(kernel, hermes):
    hermes.script = await stopped_when(hermes, then=[("assistant.delta", {"delta": ", done"}), ("run.completed", {})])
    live = await kernel.conversations.ask("c1", "slow one", user="me")
    await asyncio.sleep(0.01)
    await kernel.conversations.stop(live.turn.id, wait=True)
    [turn] = kernel.conversations.turns("c1")
    assert turn.ending is None and turn.answer == "thinking, done"


async def test_a_hermes_that_never_ends_is_cut_off(kernel, hermes, monkeypatch):
    import huvu.conversations

    monkeypatch.setattr(huvu.conversations, "STOP_GRACE", 0.05)
    never = asyncio.Event()

    async def hang(session_id: str) -> None:
        await never.wait()

    hermes.script = [("run.started", {"run_id": "run_5"}), ("assistant.delta", {"delta": "thinking"}), hang]
    live = await kernel.conversations.ask("c1", "slow one", user="me")
    await asyncio.sleep(0.01)
    await kernel.conversations.stop(live.turn.id, wait=True)
    [turn] = kernel.conversations.turns("c1")
    assert turn.ending == "stopped" and turn.answer == "thinking"


async def test_a_stop_before_the_run_starts_reaches_it(kernel, hermes):
    gate = asyncio.Event()

    async def slow_start(session_id: str) -> None:
        await gate.wait()

    async def wait(session_id: str) -> None:
        await hermes.stop_requested.wait()

    hermes.script = [slow_start, ("run.started", {"run_id": "run_7"}), wait, ("run.cancelled", {})]
    live = await kernel.conversations.ask("c1", "hi", user="me")
    await asyncio.sleep(0.01)
    await kernel.conversations.stop(live.turn.id)
    gate.set()
    await collect(kernel, live)
    assert hermes.stopped == ["run_7"] and kernel.conversations.turns("c1")[0].ending == "stopped"


async def test_a_warm_session_continues_and_a_cold_one_starts_over(kernel, hermes, clock):
    await collect(kernel, await kernel.conversations.ask("c1", "one", user="me"))
    clock.advance(minutes=4)
    await collect(kernel, await kernel.conversations.ask("c1", "two", user="me"))
    assert len(hermes.sessions) == 1 and hermes.messages[-1][1] == "two"
    clock.advance(minutes=6)
    await collect(kernel, await kernel.conversations.ask("c1", "three", user="me"))
    assert len(hermes.sessions) == 2
    session, message = hermes.messages[-1]
    assert session == hermes.sessions[-1]
    assert message.startswith("This conversation is being continued from an earlier session that was compacted.")
    assert "Them: one\nYou: ok\nThem: two\nYou: ok\n</earlier>" in message and message.endswith("three")


async def test_turns_log_their_usage(kernel, hermes):
    hermes.script = [("run.started", {"run_id": "r"}), ("assistant.delta", {"delta": "ok"}), ("run.completed", {"usage": {"input_tokens": 900, "cache_read_tokens": 800}})]
    await collect(kernel, await kernel.conversations.ask("c1", "hi", user="me"))
    [logged] = kernel.events.search(type="conversation.turn")
    assert logged.data["usage"] == {"input_tokens": 900, "cache_read_tokens": 800}


async def test_the_sweep_deletes_old_sessions_and_the_next_ask_starts_over(kernel, hermes, clock):
    await collect(kernel, await kernel.conversations.ask("c1", "one", user="me"))
    first = hermes.sessions[0]
    clock.advance(days=31)
    assert await kernel.sweep_sessions() == 1
    assert hermes.deleted == [first] and kernel.sessions.last_used(first) is None
    await collect(kernel, await kernel.conversations.ask("c1", "two", user="me"))
    assert "Them: one" in hermes.messages[-1][1]
    assert kernel.events.search(type="conversation.turn")


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


async def test_a_running_turn_replays_what_came_before(kernel, hermes):
    go_on = asyncio.Event()

    async def wait(session_id: str) -> None:
        await go_on.wait()

    hermes.script = [("run.started", {"run_id": "run_1"}), ("assistant.delta", {"delta": "The porch light is"}), wait, ("assistant.delta", {"delta": " on."}), ("run.completed", {})]
    live = await kernel.conversations.ask("c1", "porch?", user="me")
    await asyncio.sleep(0.01)
    replay = asyncio.create_task(_drain(kernel.conversations.replay(live.turn.id)))
    await asyncio.sleep(0.01)
    go_on.set()
    events = await replay
    deltas = "".join(d["text"] for n, d in events if n == "answer.delta")
    assert events[0][0] == "turn.started" and deltas == "The porch light is on."
    assert events[-1][1]["answer"] == "The porch light is on."


async def _drain(events) -> list[tuple[str, dict]]:
    return [item async for item in events]
