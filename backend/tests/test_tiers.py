from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from huvu.spec import Caller
from huvu.triggers import TriggerError, TriggerSpec

from .fakes import WAKE

pytestmark = pytest.mark.usefixtures("family")

ME = Caller(kind="hermes", session="s1", user="me")
BELL = {"note": "Doorbell.", "sql": "SELECT id FROM home_doorbell", "fire": "rows", "once": False, "script": WAKE}


async def until(condition, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def ring(kernel) -> None:
    await kernel.registry.get("home").press()
    await kernel.triggers.settle()


def wake_models(hermes) -> list[str | None]:
    return [hermes.models[s] for s, _ in hermes.messages if s.startswith("wake-")]


async def test_conversations_run_on_pro_and_wakes_on_their_tier(kernel, family, hermes):
    live = await kernel.conversations.ask(family.me_web.id, "hi", user="me")
    async for _ in kernel.conversations.subscribe(live):
        pass
    assert hermes.models[live.turn.hermes_session] == "gpt-6-sol"

    basic = await kernel.triggers.register(TriggerSpec.model_validate(BELL), ME)
    await ring(kernel)
    assert wake_models(hermes) == ["gpt-6-luna"]
    await kernel.triggers.remove(basic.id, ME)

    await kernel.triggers.register(TriggerSpec.model_validate({**BELL, "tier": "pro"}), ME)
    await ring(kernel)
    assert wake_models(hermes) == ["gpt-6-luna", "gpt-6-sol"]


async def test_an_advanced_trigger_waits_for_a_yes_on_the_phone(kernel, family, hermes):
    trigger = await kernel.triggers.register(TriggerSpec.model_validate({**BELL, "tier": "advanced"}), ME)
    assert trigger.status == "awaiting"
    [approval] = kernel.policy.pending()
    assert approval.user == "me" and "Doorbell." in approval.reason and "gpt-6-astra" in approval.reason
    # A day to answer: a dream sets it at night.
    assert approval.deadline - kernel.now() == timedelta(days=1)
    await ring(kernel)
    assert wake_models(hermes) == []

    await kernel.policy.resolve(approval.id, True, family.me_phone)
    assert trigger.status == "active"
    await ring(kernel)
    assert wake_models(hermes) == ["gpt-6-astra"]


async def test_a_no_or_no_answer_drops_it(kernel, family, clock):
    refused = await kernel.triggers.register(TriggerSpec.model_validate({**BELL, "tier": "advanced"}), ME)
    [approval] = kernel.policy.pending()
    await kernel.policy.resolve(approval.id, False, family.me_phone)
    assert refused.status == "removed"

    ignored = await kernel.triggers.register(TriggerSpec.model_validate({**BELL, "note": "Bell again.", "tier": "advanced"}), ME)
    clock.advance(days=1, minutes=1)
    await kernel.policy.expire()
    assert ignored.status == "removed"


async def test_a_tap_on_your_own_device_is_the_yes(kernel, family):
    trigger = await kernel.triggers.register(TriggerSpec.model_validate({**BELL, "tier": "advanced"}), ME)
    tap = Caller(kind="frontend", user="me", device=family.me_web.id)
    await kernel.policy.request("huvu", "activate_trigger", {"trigger": trigger.id}, tap)
    assert trigger.status == "active"
    # The card still on the phone runs out and leaves it alone.
    [card] = kernel.policy.pending()
    await kernel.policy.resolve(card.id, False, family.me_phone)
    assert trigger.status == "active"


async def test_the_model_cant_approve_its_own(kernel, family):
    trigger = await kernel.triggers.register(TriggerSpec.model_validate({**BELL, "tier": "advanced"}), ME)
    result = await kernel.tools.call("act", {"name": "huvu_activate_trigger", "args": {"trigger": trigger.id}}, ME)
    assert result.data["status"] in ("pending_approval", "already_pending")
    assert trigger.status == "awaiting"


async def test_everyone_gets_one_dream_and_a_removed_one_stays_gone(kernel):
    await kernel.dreams.ensure()
    await kernel.dreams.ensure()
    dreams = {u: [t for t in kernel.triggers.list(Caller(kind="hermes", user=u)) if t.spec.note.startswith("Your dream.")] for u in ("me", "mom")}
    assert [len(d) for d in dreams.values()] == [1, 1]
    [mine] = dreams["me"]
    assert mine.spec.tier == "pro" and mine.spec.fire == "edge" and not mine.spec.once and "ctx.wake" in mine.spec.script
    assert mine.spec.sql == "SELECT 1 WHERE now_minute() = 240"
    assert "brief" in mine.spec.note and "quick notes" in mine.spec.note
    # The model can't pause or remove a dream, a dream's own run included;
    # its person can, from the app.
    with pytest.raises(TriggerError, match="only they can pause or remove it"):
        await kernel.triggers.remove(mine.id, ME)
    with pytest.raises(TriggerError, match="only they can pause or remove it"):
        await kernel.triggers.pause(mine.id, ME)
    await kernel.triggers.remove(mine.id, Caller(kind="frontend", user="me"))
    await kernel.dreams.ensure()
    assert not [t for t in kernel.triggers.list(ME) if t.spec.note.startswith("Your dream.")]


async def test_a_dream_wakes_its_person_at_four(kernel, hermes, clock):
    await kernel.dreams.ensure()
    clock.advance(minutes=4 * 60 + 10)
    await kernel.triggers.tick()
    await kernel.triggers.settle()
    dreams = [m for s, m in hermes.messages if s.startswith("wake-")]
    assert len(dreams) == 2 and all("Your dream." in m for m in dreams)


async def test_a_dream_runs_with_the_situation_and_a_longer_deadline(kernel, hermes, clock):
    kernel.config.wake_deadline = 0.05

    async def slow(_session: str) -> None:
        await asyncio.sleep(0.2)

    hermes.script = [("run.started", {"run_id": "run_1"}), slow, ("assistant.delta", {"delta": "slept"}), ("run.completed", {})]
    await kernel.dreams.ensure()
    clock.advance(minutes=4 * 60 + 10)
    await kernel.triggers.tick()
    await kernel.triggers.settle()
    await until(lambda: kernel.events_since("wake.done", kernel.now() - timedelta(hours=1)) == 2, timeout=5)
    [session] = [s for s, _ in hermes.messages if s.startswith("wake-") and hermes.profiles[s] == "me"]
    [situation] = hermes.histories[session]
    assert situation["content"].startswith("[huvu situation]\nNow: ")
    assert f"Your wiki: {kernel.config.hermes_home / 'profiles' / 'me' / 'wiki'}" in situation["content"]

    # Any other wake still ends at the wakes' deadline.
    await kernel.triggers.register(TriggerSpec.model_validate(BELL), ME)
    await ring(kernel)
    await until(lambda: kernel.events_since("trigger.error", kernel.now() - timedelta(hours=1)) == 1, timeout=5)


async def test_a_live_dream_follows_the_code(kernel, monkeypatch):
    from huvu import dream

    await kernel.dreams.ensure()
    [mine] = [t for t in kernel.triggers.list(ME) if kernel.dreams.is_dream(t.id)]
    monkeypatch.setattr(dream, "DREAM_NOTE", "Your dream. Read everything.")
    await kernel.dreams.ensure()
    [now] = [t for t in kernel.triggers.list(ME) if kernel.dreams.is_dream(t.id)]
    assert now.id == mine.id and now.spec.note == "Your dream. Read everything."
    assert not kernel.dreams.is_dream("tr_other") and not kernel.dreams.is_dream(None)


def test_a_tier_can_run_on_another_provider(tmp_path):
    from huvu.config import ModelChoice, load

    toml = tmp_path / "huvu.toml"
    toml.write_text('[models]\nbasic = "gpt-6-luna"\npro = { model = "claude-sonnet-5-5", provider = "anthropic" }\n')
    config = load(toml)
    assert config.model("pro") == ModelChoice("claude-sonnet-5-5", "anthropic")
    assert config.model("basic") == ModelChoice("gpt-6-luna") and config.model("advanced").model == "gpt-6-astra"
    toml.write_text("[models]\npro = { provider = \"anthropic\" }\n")
    with pytest.raises(ValueError, match="models.pro"):
        load(toml)


async def test_the_chosen_provider_goes_with_the_run(kernel, family, hermes):
    from huvu.config import ModelChoice

    kernel.config.models["pro"] = ModelChoice("claude-sonnet-5-5", "anthropic")
    live = await kernel.conversations.ask(family.me_web.id, "hi", user="me")
    async for _ in kernel.conversations.subscribe(live):
        pass
    session = live.turn.hermes_session
    assert (hermes.models[session], hermes.providers[session]) == ("claude-sonnet-5-5", "anthropic")
