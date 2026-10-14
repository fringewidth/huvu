"""Wakes as conversations: a person sees their wakes, a wake waits for them
when the model asks it to, and their reply runs in it."""

from __future__ import annotations

from datetime import timedelta

import pytest

from huvu import prompts
from huvu.spec import Caller
from huvu.triggers import TriggerSpec
from huvu.wake import WakeEnded

from .fakes import WAKE

pytestmark = pytest.mark.usefixtures("family")

ME = Caller(kind="hermes", session="s1", user="me")
BELL = {"note": "Doorbell.", "sql": "SELECT id FROM home_doorbell", "fire": "rows", "once": False, "script": WAKE}
DONE = [("run.started", {"run_id": "run_1"}), ("assistant.delta", {"delta": "done"}), ("run.completed", {})]


async def ring(kernel, **fields) -> str:
    """Rings the doorbell under a trigger of mine, and returns the wake."""
    await kernel.triggers.register(TriggerSpec.model_validate({**BELL, **fields}), ME)
    await kernel.registry.get("home").press()
    await kernel.triggers.settle()
    [wake] = kernel.db.execute("SELECT id FROM wakes").fetchall()
    return wake["id"]


def waiting(kernel, text: str = "the OTP"):
    """A Hermes script whose run calls wake_wait."""

    async def wait(session: str) -> None:
        await kernel.tools.call("wake_wait", {"waits_for": text}, Caller(kind="hermes", session=session, user="me"))

    return [("run.started", {"run_id": "run_1"}), wait, ("assistant.delta", {"delta": "waiting for you"}), ("run.completed", {})]


async def collect(kernel, live) -> None:
    async for _ in kernel.conversations.subscribe(live):
        pass


def pushed(kernel, sent: list, name: str) -> list:
    return [(data, devices) for n, data, devices in sent if n == name]


@pytest.fixture
def sent(kernel, monkeypatch) -> list:
    """What the push hub sends, with the devices it goes to."""
    out: list = []
    original = kernel.push.send
    monkeypatch.setattr(kernel.push, "send", lambda name, data, devices: out.append((name, data, list(devices))) or original(name, data, devices))
    return out


async def test_a_wake_is_a_conversation_of_its_person_that_ends_with_its_run(kernel, family, sent):
    wake = await ring(kernel)
    view = kernel.waker.view(wake, family.me_phone)
    assert view["wake"]["status"] == "ended" and view["wake"]["note"] == "Doorbell."
    [turn] = view["turns"]
    assert turn["ask"] == "" and turn["answer"] == "ok" and not turn["running"]
    # Its events go to my own devices, never to a shared one or to mom's.
    started = pushed(kernel, sent, "turn.started")
    assert started and all(data["wake"] == wake for data, _ in started)
    assert {d for _, devices in started for d in devices} == {family.me_phone.id, family.me_web.id}
    # Ended, so it isn't listed, and nothing can be said in it.
    assert kernel.waker.open_for("me") == []
    with pytest.raises(WakeEnded):
        await kernel.waker.ask(wake, "hello?", family.me_phone)


async def test_a_wake_waits_when_the_model_asks_and_the_reply_continues_it(kernel, family, hermes):
    hermes.script = waiting(kernel)
    wake = await ring(kernel, tier="pro")
    [listed] = kernel.waker.open_for("me")
    assert listed["id"] == wake and listed["status"] == "waiting" and listed["waits_for"] == "the OTP"
    assert listed["until"] - listed["ran_at"] == timedelta(days=1)

    hermes.script = DONE
    live = await kernel.waker.ask(wake, "it's 482913", family.me_web)
    await collect(kernel, live)
    # The same session, on the trigger's tier, with no history: Hermes
    # loads the session's own.
    session, message = hermes.messages[-1]
    assert session == wake and message.endswith("it's 482913")
    assert hermes.histories[wake] == [] and hermes.models[wake] == "gpt-6-sol"
    turns = kernel.waker.view(wake, family.me_phone)["turns"]
    assert [t["ask"] for t in turns] == ["", "it's 482913"] and turns[1]["answer"] == "done"
    # That run didn't ask to wait, so the wake has ended.
    assert kernel.waker.open_for("me") == []


async def test_a_wake_is_its_persons_alone(kernel, family):
    wake = await ring(kernel)
    [turn] = kernel.conversations.turns(wake)
    assert kernel.conversations.visible(family.me_web, turn.id)
    assert not kernel.conversations.visible(family.mom_phone, turn.id)
    assert not kernel.conversations.visible(family.tv, turn.id)
    with pytest.raises(KeyError):
        kernel.waker.view(wake, family.mom_phone)
    assert kernel.waker.open_for("mom") == [] and kernel.waker.open_for(None) == []


async def test_only_a_wake_can_wait(kernel, family):
    live = await kernel.conversations.ask(family.me_web.id, "hi", user="me")
    await collect(kernel, live)
    caller = Caller(kind="hermes", session=live.turn.hermes_session, user="me", device=family.me_web.id)
    with pytest.raises(Exception, match="only a wake can wait"):
        await kernel.tools.call("wake_wait", {"waits_for": "a reply"}, caller)


async def test_a_wakes_notice_to_its_person_opens_it(kernel, family, hermes):
    async def tell(session: str) -> None:
        caller = Caller(kind="hermes", session=session, user="me")
        await kernel.tools.call("act", {"name": "huvu_notify", "args": {"text": "Need the OTP."}}, caller)
        await kernel.tools.call("act", {"name": "huvu_message", "args": {"person": "mom", "text": "At the door?"}}, caller)

    hermes.script = [("run.started", {"run_id": "run_1"}), tell, ("run.completed", {})]
    wake = await ring(kernel)
    rows = {r["text"]: (r["user_id"], r["opens"]) for r in kernel.db.execute("SELECT text, user_id, opens FROM notifications").fetchall()}
    # Mine opens the wake; the message to mom opens nothing.
    assert rows["Need the OTP."] == ("me", f"wake:{wake}") and rows["At the door?"] == ("mom", None)


async def test_the_person_ends_a_wake(kernel, family, hermes):
    hermes.script = waiting(kernel)
    wake = await ring(kernel)
    await kernel.waker.close(wake, family.me_phone)
    assert kernel.waker.open_for("me") == []
    with pytest.raises(WakeEnded):
        await kernel.waker.ask(wake, "never mind", family.me_phone)


async def test_a_wait_ends_after_a_day(kernel, family, hermes, clock):
    hermes.script = waiting(kernel)
    wake = await ring(kernel)
    clock.advance(hours=23)
    assert len(kernel.waker.open_for("me")) == 1
    clock.advance(hours=1)
    assert kernel.waker.open_for("me") == []
    await kernel.waker.sweep()
    assert kernel.db.execute("SELECT status FROM wakes WHERE id = ?", (wake,)).fetchone()["status"] == "ended"


async def test_a_waiting_wake_is_compacted_before_its_cache_goes_cold(kernel, family, hermes, clock):
    hermes.script = waiting(kernel)
    wake = await ring(kernel)
    clock.advance(minutes=54)
    await kernel.waker.sweep()
    assert all(m != prompts.WAKE_COMPACT for _, m in hermes.messages)

    hermes.script = [("run.started", {"run_id": "run_2"}), ("assistant.delta", {"delta": "Claim form open; needs the OTP."}), ("run.completed", {})]
    clock.advance(minutes=1)
    await kernel.waker.sweep()
    await kernel.waker._compacting[wake]
    # Asked in the warm session itself, with no history.
    assert hermes.messages[-1] == (wake, prompts.WAKE_COMPACT) and hermes.histories[wake] == []
    # Done once.
    await kernel.waker.sweep()
    assert sum(m == prompts.WAKE_COMPACT for _, m in hermes.messages) == 1

    hermes.script = DONE
    live = await kernel.waker.ask(wake, "it's 482913", family.me_phone)
    await collect(kernel, live)
    # A fresh session, from the wake's own notes, with the situation.
    session, message = hermes.messages[-1]
    assert session != wake and session.startswith("wake-")
    assert message.startswith("You are carrying on a task") and "Claim form open; needs the OTP." in message and message.endswith("it's 482913")
    assert hermes.histories[session][0]["content"].startswith(prompts.SITUATION_HEADER)
    # The wake keeps its id, and its turns stay together.
    assert [t["ask"] for t in kernel.waker.view(wake, family.me_phone)["turns"]] == ["", "it's 482913"]


async def test_only_an_advanced_wake_may_browse(kernel, family, hermes):
    pro = await ring(kernel, tier="pro")
    assert not kernel.waker.may_browse(pro, "me", None)
    kernel.db.execute("UPDATE wakes SET tier = 'advanced' WHERE id = ?", (pro,))
    assert kernel.waker.may_browse(pro, "me", None)
    # Not from another profile, and never in a conversation.
    assert not kernel.waker.may_browse(pro, "mom", None)
    live = await kernel.conversations.ask(family.me_web.id, "hi", user="me")
    await collect(kernel, live)
    assert not kernel.waker.may_browse(live.turn.hermes_session, "me", None)
