from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from huvu.hermes import HermesClient, Profile
from huvu.spec import Caller

from .fakes import FakeHermes


async def collect(kernel, live) -> list[tuple[str, dict]]:
    return [item async for item in kernel.conversations.subscribe(live)]


async def routes(kernel, device: str = "c1") -> list[dict]:
    me = Caller(kind="hermes", user="me", device=device)
    sql = f"SELECT route FROM chat_turns WHERE device_id = '{device}' ORDER BY n"
    return [json.loads(r[0]) for r in (await kernel.tools.call("data_sql", {"sql": sql}, me)).data["rows"]]


def clef(kernel, answers: dict[str, float] | None = None, *, status: int = 200, slow: bool = False) -> list[str]:
    """Points the router at a fake Workers AI that answers each ask with its
    probability of needing context from `answers`, and returns the asks it
    was sent."""
    asked: list[str] = []

    async def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        asked.append(body["state"]["ask"])
        assert request.headers["authorization"] == "Bearer cf-test"
        assert body["questions"]["escalate"]["type"] == "noul"
        if slow:
            await asyncio.sleep(5)
        if status != 200:
            return httpx.Response(status, json={"success": False})
        p = (answers or {})[body["state"]["ask"]]
        return httpx.Response(200, json={"result": {"answers": {"escalate": {"type": "noul", "noul": p}}, "usage": {}}, "success": True})

    router = kernel.router
    router._account, router._token = "acct", "cf-test"
    router._http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    return asked


# Clef-flash, and the sessions each route runs in.


@pytest.fixture
def talk(kernel, family):
    """The kernel and its users: chats run in their user's Hermes profile,
    so the users must exist."""
    return kernel


async def test_a_one_off_runs_on_basic_alone_without_thinking(talk, hermes: FakeHermes):
    clef(talk, {"what's the temperature today": 0.1})
    live = await talk.conversations.ask("c1", "what's the temperature today", user="me")
    await collect(talk, live)
    session = live.turn.hermes_session
    assert (hermes.models[session], hermes.efforts[session]) == ("gpt-6-luna", "none")
    assert await routes(talk) == [{"tier": "basic", "model": "gpt-6-luna", "effort": "none", "why": "one_off", "p": 0.1}]


async def test_an_ask_that_needs_context_runs_on_pro(talk, hermes: FakeHermes):
    clef(talk, {"make it shorter": 0.9})
    live = await talk.conversations.ask("c1", "make it shorter", user="me")
    await collect(talk, live)
    assert (hermes.models[live.turn.hermes_session], hermes.efforts[live.turn.hermes_session]) == ("gpt-6-sol", None)
    assert (await routes(talk))[0]["why"] == "context"


async def test_an_ask_at_the_line_runs_on_pro(talk):
    clef(talk, {"what's the weather": 0.30})
    assert (await talk.router.route("what's the weather")).tier == "pro"


async def test_without_clef_every_ask_runs_on_pro(talk, hermes: FakeHermes):
    live = await talk.conversations.ask("c1", "it's stuffy in here", user="me")
    await collect(talk, live)
    assert hermes.models[live.turn.hermes_session] == "gpt-6-sol"
    assert await routes(talk) == [{"tier": "pro", "model": "gpt-6-sol", "effort": None, "why": "failed", "p": None}]


async def test_a_failed_clef_runs_on_pro(talk):
    clef(talk, status=503)
    assert (await talk.router.route("what's the weather")).why == "failed"


async def test_a_slow_clef_is_cut_off_at_the_timeout(talk):
    clef(talk, slow=True)
    talk.config.router_timeout = 0.05
    started = time.monotonic()
    assert (await talk.router.route("what's the weather")).why == "failed"
    assert time.monotonic() - started < 0.5


async def test_clef_answers_a_repeated_ask_once(talk):
    asked = clef(talk, {"what's the weather": 0.2})
    for ask in ["what's the weather", "What's the weather?", "what's  the weather"]:
        assert (await talk.router.route(ask)).tier == "basic"
    assert asked == ["what's the weather"]


async def test_with_the_router_off_every_ask_runs_on_pro(talk, hermes: FakeHermes):
    talk.config.router_enabled = False
    live = await talk.conversations.ask("c1", "turn on the projector", user="me")
    await collect(talk, live)
    assert hermes.models[live.turn.hermes_session] == "gpt-6-sol"
    assert (await routes(talk))[0]["why"] == "off"


ONE_OFFS = {"turn on the projector": 0.05, "night sky off": 0.05, "what's the time": 0.05}
CONTEXT = {"plan a movie night for saturday": 0.8, "plan a movie night": 0.8, "what snacks go with that?": 0.9, "what snacks go with it?": 0.9, "and drinks?": 0.9}


async def test_a_one_off_runs_alone_and_leaves_the_main_session(talk, hermes: FakeHermes, clock):
    clef(talk, {**ONE_OFFS, **CONTEXT})
    await collect(talk, await talk.conversations.ask("c1", "plan a movie night for saturday", user="me"))
    main = hermes.sessions[0]
    clock.advance(minutes=1)
    quick = await talk.conversations.ask("c1", "turn on the projector", user="me")
    await collect(talk, quick)
    # A session of its own, with the ask alone: no transcript, no notes.
    assert quick.turn.hermes_session != main and hermes.models[quick.turn.hermes_session] == "gpt-6-luna"
    assert hermes.messages[-1][1].endswith("\n\nturn on the projector") and "<earlier>" not in hermes.messages[-1][1]
    row = talk.db.execute("SELECT hermes_session FROM conversations WHERE id = 'c1'").fetchone()
    assert row["hermes_session"] == main


async def test_two_one_offs_can_start_in_the_same_minute(talk, hermes: FakeHermes):
    # Hermes refuses a title another session has, and titles hold the minute.
    clef(talk, ONE_OFFS)
    for ask in ["turn on the projector", "night sky off"]:
        await collect(talk, await talk.conversations.ask("c1", ask, user="me"))
    assert len(hermes.sessions) == 2


async def test_the_main_session_hears_every_turn_since_its_last(talk, hermes: FakeHermes, clock):
    clef(talk, {**ONE_OFFS, **CONTEXT})
    await collect(talk, await talk.conversations.ask("c1", "plan a movie night for saturday", user="me"))
    main = hermes.sessions[0]
    for ask in ["turn on the projector", "night sky off"]:
        clock.advance(minutes=1)
        await collect(talk, await talk.conversations.ask("c1", ask, user="me"))
    clock.advance(minutes=1)
    await collect(talk, await talk.conversations.ask("c1", "what snacks go with that?", user="me"))
    session, message = hermes.messages[-1]
    assert session == main
    assert 'they said "turn on the projector", and you answered "ok"' in message
    assert message.index("turn on the projector") < message.index("night sky off") < message.index("what snacks go with that?")
    # Told once: the ask after that carries no notes.
    clock.advance(minutes=1)
    await collect(talk, await talk.conversations.ask("c1", "and drinks?", user="me"))
    assert "between your turns" not in hermes.messages[-1][1]
    assert [r["why"] for r in await routes(talk)] == ["context", "one_off", "one_off", "context", "context"]


async def test_a_cold_main_session_reads_every_turn_in_its_transcript(talk, hermes: FakeHermes, clock, monkeypatch):
    from huvu import conversations

    clef(talk, {**ONE_OFFS, **CONTEXT, "turn on the strip lights": 0.05})
    for ask in ["plan a movie night", "turn on the projector", "turn on the strip lights", "night sky off"]:
        await collect(talk, await talk.conversations.ask("c1", ask, user="me"))
    # Each turn is its ask and "ok": room for the last two.
    monkeypatch.setattr(conversations, "TRANSCRIPT_CHARS", 45)
    clock.advance(minutes=6)
    await collect(talk, await talk.conversations.ask("c1", "what snacks go with it?", user="me"))
    message = hermes.messages[-1][1]
    assert "starts at turn 3, and the turns before it were left out" in message
    assert "Them: turn on the strip lights\nYou: ok\nThem: night sky off\nYou: ok\n</earlier>" in message
    assert "between your turns" not in message


async def test_an_edit_by_a_one_off_restarts_the_main_session(talk, hermes: FakeHermes, clock):
    clef(talk, {**ONE_OFFS, **CONTEXT})
    first = await talk.conversations.ask("c1", "plan a movie night for saturday", user="me")
    await collect(talk, first)
    await collect(talk, await talk.conversations.ask("c1", "turn on the projector", replaces=first.turn.id, user="me"))
    await collect(talk, await talk.conversations.ask("c1", "what snacks go with it?", user="me"))
    session, message = hermes.messages[-1]
    assert session not in (hermes.sessions[0], hermes.sessions[1])
    assert "Them: turn on the projector\nYou: ok\n</earlier>" in message and "plan a movie night" not in message


async def test_hermes_gets_the_effort_as_a_model_option():
    sent: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, text="event: done\ndata: {}\n\n")

    client = HermesClient("http://hermes")
    client._http = httpx.AsyncClient(base_url="http://hermes", transport=httpx.MockTransport(answer))
    profile = Profile(None, None)
    [event async for event in client.chat(profile, "s1", "hi", model="gpt-6-luna", effort="none")]
    [event async for event in client.chat(profile, "s1", "hi", model="gpt-6-sol")]
    assert sent[0] == {"message": "hi", "model": "gpt-6-luna", "model_options": {"reasoning_effort": "none"}}
    assert sent[1] == {"message": "hi", "model": "gpt-6-sol"}
    await client.close()
