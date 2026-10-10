from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from huvu.config import Config
from huvu.hermes import HermesClient, Profile
from huvu.kernel import Kernel
from huvu.routing import Vocabulary, plain_control, words
from huvu.spec import Caller
from huvu_drivers.ha import source as ha_source
from huvu_drivers.ha.source import HomeAssistant

from .fake_ha import FakeHA
from .fakes import FakeHermes

THEATRE = Vocabulary(
    names=frozenset(
        words("Home Theatre AV AC Temperature Strip Lights Night Sky Focus Lights Projector on Projector off Amplifier mute")
        + words("Home theatre volume Home theatre play Xiaomi Robot Vacuum-Mop Pro ground floor projector")
        + words("Netflix Prime Video YouTube JioHotstar")
    ),
    options=("jiohotstar", "netflix", "prime video", "youtube"),
)

PLAIN = [
    "turn on the projector",
    "Turn on the, uh, the projector.",
    "can you turn on the projector for me please",
    "projector on",
    "night sky off",
    "set the AC to 24",
    "AC to 22",
    "make the AC 26 degrees",
    "turn the AC up 2 degrees",
    "volume down 5",
    "turn the volume up by 10",
    "lower the volume a bit",
    "increase the volume",
    "decrease the volume by 5",
    "start the vacuum",
    "send the vacuum back to its dock",
    "turn off the AC and the lights",
    "turn on the strip lights, then turn the volume up by 10",
    "play Stranger Things on Netflix",
    "play the latest episode of the boys on prime video",
    "is the projector on",
    "are the theatre lights on?",
]
NOT_PLAIN = [
    # for later, or on a change
    "turn off the projector at midnight",
    "turn on the AC in 10",
    "turn on the AC in 10 minutes",
    "turn on the AC when I get home",
    "if the vacuum hasn't run by noon, start it",
    "turn on the AC if it's above 28 inside",
    # questions, finding, advice
    "what's the best projector under 1 lakh",
    "the lights keep turning off by themselves, what's going on",
    "why is the AC not cooling",
    # hints, vague, and too little to act on
    "it's too hot in here",
    "it's stuffy in here",
    "movie time",
    "set the mood for a movie",
    "let's chill with some youtube",
    "lights",
    "play",
    "turn it on",
    # a number that isn't a setting
    "turn on the AC 6",
    # a typo
    "projectr on pls",
    # a read of something that isn't the house's
    "is it going to rain",
    "is mom home",
    "is the projector broken",
    # nothing of the house's
    "hi",
    "remind me to call mom at 6",
]


@pytest.mark.parametrize("ask", PLAIN)
def test_plain_house_control_goes_to_basic(ask):
    assert plain_control(ask, THEATRE)


@pytest.mark.parametrize("ask", NOT_PLAIN)
def test_anything_else_goes_to_pro(ask):
    assert not plain_control(ask, THEATRE)


async def test_the_vocabulary_is_the_names_of_what_huvu_can_work(tmp_path, clock, hermes, monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "secret")
    monkeypatch.setattr(ha_source, "REFRESH_DEBOUNCE", 0.0)
    ha = FakeHA(clock)
    ha.entity("light.theatre_night_sky", "off", area="living_room", friendly_name="Night Sky", aliases=["stars"])
    ha.entity("sensor.theatre_humidity", "60", area="living_room", friendly_name="Humidity", device_class="humidity")
    ha.entity("vacuum.robot", "docked", area="hall", friendly_name="Robot Vacuum-Mop 2 Pro")
    ha.script(
        "theatre_play",
        alias="Theatre play",
        labels=["huvu_silent"],
        fields={"app": {"selector": {"select": {"options": ["Netflix", {"value": "prime_video", "label": "Prime Video"}]}}}},
    )
    source = HomeAssistant({}, connect=ha.connect)
    kernel = Kernel(Config(data_dir=tmp_path), hermes=hermes, sources={"ha": source}, clock=clock)
    task = asyncio.create_task(source.run())
    for _ in range(200):
        if source._connected and kernel.router.vocabulary().names:
            break
        await asyncio.sleep(0.01)
    vocabulary = kernel.router.vocabulary()
    # A sensor isn't something huvu works, and numbers in names don't count.
    assert {"night", "sky", "stars", "living", "room", "robot", "vacuum", "mop", "pro", "theatre", "netflix", "prime", "video"} <= vocabulary.names
    assert "humidity" not in vocabulary.names and "2" not in vocabulary.names
    assert "prime video" in vocabulary.options and "netflix" in vocabulary.options
    assert (await kernel.router.route("turn on the stars")).tier == "basic"
    assert (await kernel.router.route("play Severance on Prime Video")).tier == "basic"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await kernel.conversations.close()
    kernel.data.close()
    kernel.db.close()


# The conversation side.


async def collect(kernel, live) -> list[tuple[str, dict]]:
    return [item async for item in kernel.conversations.subscribe(live)]


@pytest.fixture
def theatre(kernel, family):
    """The kernel, its users, and the theatre's names. Chats run in their
    user's Hermes profile, so the users must exist."""
    kernel.router.vocabulary = lambda: THEATRE
    return kernel


async def routes(kernel, device: str = "c1") -> list[dict]:
    me = Caller(kind="hermes", user="me", device=device)
    sql = f"SELECT route FROM chat_turns WHERE device_id = '{device}' ORDER BY n"
    return [json.loads(r[0]) for r in (await kernel.tools.call("data_sql", {"sql": sql}, me)).data["rows"]]


async def test_plain_house_control_runs_on_basic_without_thinking(theatre, hermes: FakeHermes):
    live = await theatre.conversations.ask("c1", "turn on the projector", user="me")
    await collect(theatre, live)
    session = live.turn.hermes_session
    assert (hermes.models[session], hermes.efforts[session]) == ("gpt-6-luna", "none")
    assert await routes(theatre) == [{"tier": "basic", "model": "gpt-6-luna", "effort": "none", "why": "control", "p": None}]


async def test_without_clef_anything_else_runs_on_pro(theatre, hermes: FakeHermes):
    live = await theatre.conversations.ask("c1", "it's stuffy in here", user="me")
    await collect(theatre, live)
    session = live.turn.hermes_session
    assert (hermes.models[session], hermes.efforts[session]) == ("gpt-6-sol", None)
    assert await routes(theatre) == [{"tier": "pro", "model": "gpt-6-sol", "effort": None, "why": "failed", "p": None}]


async def test_with_the_router_off_every_ask_runs_on_pro(theatre, hermes: FakeHermes):
    theatre.config.router_enabled = False
    live = await theatre.conversations.ask("c1", "turn on the projector", user="me")
    await collect(theatre, live)
    assert hermes.models[live.turn.hermes_session] == "gpt-6-sol"
    assert (await routes(theatre))[0]["why"] == "off"


async def test_a_quick_turn_runs_alone_and_leaves_the_main_session(theatre, hermes: FakeHermes, clock):
    await collect(theatre, await theatre.conversations.ask("c1", "plan a movie night for saturday", user="me"))
    main = hermes.sessions[0]
    clock.advance(minutes=1)
    quick = await theatre.conversations.ask("c1", "turn on the projector", user="me")
    await collect(theatre, quick)
    # A session of its own, with the ask alone: no transcript, no notes.
    assert quick.turn.hermes_session != main and hermes.models[quick.turn.hermes_session] == "gpt-6-luna"
    assert hermes.messages[-1][1].endswith("\n\nturn on the projector") and "<earlier>" not in hermes.messages[-1][1]
    row = theatre.db.execute("SELECT hermes_session FROM conversations WHERE id = 'c1'").fetchone()
    assert row["hermes_session"] == main


async def test_the_main_session_hears_of_quick_turns_on_its_next_ask(theatre, hermes: FakeHermes, clock):
    await collect(theatre, await theatre.conversations.ask("c1", "plan a movie night for saturday", user="me"))
    main = hermes.sessions[0]
    for ask in ["turn on the projector", "night sky off"]:
        clock.advance(minutes=1)
        await collect(theatre, await theatre.conversations.ask("c1", ask, user="me"))
    clock.advance(minutes=1)
    await collect(theatre, await theatre.conversations.ask("c1", "what snacks go with that?", user="me"))
    session, message = hermes.messages[-1]
    assert session == main
    assert 'they said "turn on the projector", and you answered "ok"' in message
    assert message.index("turn on the projector") < message.index("night sky off") < message.index("what snacks go with that?")
    # Told once: the ask after that carries no notes.
    clock.advance(minutes=1)
    await collect(theatre, await theatre.conversations.ask("c1", "and drinks?", user="me"))
    assert "between your turns" not in hermes.messages[-1][1]
    assert [r["why"] for r in await routes(theatre)] == ["failed", "control", "control", "failed", "failed"]


async def test_a_cold_main_session_reads_quick_turns_in_its_transcript(theatre, hermes: FakeHermes, clock, monkeypatch):
    from huvu import conversations

    for ask in ["plan a movie night", "turn on the projector", "turn on the strip lights", "night sky off"]:
        await collect(theatre, await theatre.conversations.ask("c1", ask, user="me"))
    # Each turn is its ask and "ok": room for the last two.
    monkeypatch.setattr(conversations, "TRANSCRIPT_CHARS", 45)
    clock.advance(minutes=6)
    await collect(theatre, await theatre.conversations.ask("c1", "what snacks go with it?", user="me"))
    message = hermes.messages[-1][1]
    assert "starts at turn 3, and the turns before it were left out" in message
    assert "Them: turn on the strip lights\nYou: ok\nThem: night sky off\nYou: ok\n</earlier>" in message
    assert "between your turns" not in message


async def test_an_edit_by_a_quick_turn_restarts_the_main_session(theatre, hermes: FakeHermes, clock):
    first = await theatre.conversations.ask("c1", "plan a movie night for saturday", user="me")
    await collect(theatre, first)
    await collect(theatre, await theatre.conversations.ask("c1", "turn on the projector", replaces=first.turn.id, user="me"))
    await collect(theatre, await theatre.conversations.ask("c1", "what snacks go with it?", user="me"))
    session, message = hermes.messages[-1]
    assert session not in (hermes.sessions[0], hermes.sessions[1])
    assert "Them: turn on the projector\nYou: ok\n</earlier>" in message and "plan a movie night" not in message


def clef(kernel, answers: dict[str, float] | None = None, *, status: int = 200, slow: bool = False) -> list[str]:
    """Points the router at a fake Workers AI that answers each ask with its
    probability of pro from `answers`, and returns the asks it was sent."""
    asked: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        asked.append(body["state"]["ask"])
        assert request.headers["authorization"] == "Bearer cf-test"
        assert body["questions"]["escalate"]["type"] == "noul"
        if slow:
            raise httpx.ReadTimeout("too slow", request=request)
        if status != 200:
            return httpx.Response(status, json={"success": False})
        p = (answers or {})[body["state"]["ask"]]
        return httpx.Response(200, json={"result": {"answers": {"escalate": {"type": "noul", "noul": p}}, "usage": {}}, "success": True})

    router = kernel.router
    router._account, router._token = "acct", "cf-test"
    router._http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    return asked


async def test_clef_sends_quick_asks_to_basic_and_the_rest_to_pro(theatre, hermes: FakeHermes):
    asked = clef(theatre, {"what's the temperature today": 0.18, "plan a movie night for saturday": 0.8})
    quick = await theatre.conversations.ask("c1", "what's the temperature today", user="me")
    await collect(theatre, quick)
    assert hermes.models[quick.turn.hermes_session] == "gpt-6-luna" and hermes.efforts[quick.turn.hermes_session] == "none"
    slow = await theatre.conversations.ask("c1", "plan a movie night for saturday", user="me")
    await collect(theatre, slow)
    assert hermes.models[slow.turn.hermes_session] == "gpt-6-sol"
    # A house command never waits for Clef.
    await collect(theatre, await theatre.conversations.ask("c1", "turn on the projector", user="me"))
    assert asked == ["what's the temperature today", "plan a movie night for saturday"]
    assert [(r["why"], r["p"]) for r in await routes(theatre)] == [("clef", 0.18), ("other", 0.8), ("control", None)]


async def test_an_ask_at_the_line_runs_on_pro(theatre):
    clef(theatre, {"what's the weather": 0.30})
    assert (await theatre.router.route("what's the weather")).tier == "pro"


async def test_a_failed_or_slow_clef_runs_on_pro(theatre):
    clef(theatre, status=503)
    assert (await theatre.router.route("what's the weather")).why == "failed"
    clef(theatre, slow=True)
    assert (await theatre.router.route("what's the weather")).why == "failed"


async def test_clef_answers_a_repeated_ask_once(theatre):
    asked = clef(theatre, {"what's the weather": 0.2})
    for ask in ["what's the weather", "What's the weather?", "what's  the weather"]:
        assert (await theatre.router.route(ask)).tier == "basic"
    assert asked == ["what's the weather"]


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
