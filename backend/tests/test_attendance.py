"""Who is at a shared device: asked on a phone, confirmed with a tap, and
then the device's session acts and logs as that person."""

from __future__ import annotations

import asyncio

import pytest

from huvu.accounts import AuthError
from huvu.mcp_server import caller_from
from huvu.spec import Caller
from huvu.tools import ToolError
from huvu.triggers import TriggerSpec

from .test_conversations import collect
from .test_triggers import push_events


async def recognize(kernel, family, user_phone, user="mom"):
    check = await kernel.attendance.ask(family.tv.id, user, "device")
    await kernel.attendance.answer(check.id, True, user_phone)
    return check


async def test_the_question_goes_to_that_persons_phones(kernel, family):
    moms, moms_task = await push_events(kernel, family.mom_phone.id)
    mine, mine_task = await push_events(kernel, family.me_phone.id)
    tv, tv_task = await push_events(kernel, family.tv.id)
    check = await kernel.attendance.ask(family.tv.id, "mom", "device")
    await asyncio.sleep(0)
    [(name, question)] = moms
    assert name == "identify" and question["device"]["name"] == "living room"
    assert mine == []
    for wrong in (family.me_phone, family.tv):
        with pytest.raises(AuthError):
            await kernel.attendance.answer(check.id, True, wrong)
    await kernel.attendance.answer(check.id, True, family.mom_phone)
    await asyncio.sleep(0)
    assert kernel.attendance.holder(family.tv.id) == "mom"
    assert tv == [("attendance", {"user": {"id": "mom", "name": "Mom"}})]
    assert moms[-1] == ("identify.done", {"id": check.id, "status": "confirmed"})
    for task in (moms_task, mine_task, tv_task):
        task.cancel()


async def test_only_shared_devices_are_attended(kernel, family):
    with pytest.raises(AuthError):
        await kernel.attendance.ask(family.me_web.id, "mom", "device")
    with pytest.raises(ToolError, match="shared device"):
        await kernel.tools.call("people_identify", {"user": "mom"}, Caller(kind="hermes", user="me", device=family.me_web.id))


async def test_the_model_can_ask_by_name(kernel, family):
    tv_caller = Caller(kind="hermes", device=family.tv.id)
    with pytest.raises(ToolError, match=r"Mom \(mom\)"):
        await kernel.tools.call("people_identify", {"user": "grandpa"}, tv_caller)
    result = await kernel.tools.call("people_identify", {"user": "Mom"}, tv_caller)
    assert result.data["asked"] == "Mom"
    assert len(kernel.attendance.pending_for("mom")) == 1


async def test_the_session_acts_and_logs_as_the_person(kernel, family, home, hermes, clock):
    first = await kernel.conversations.ask(family.tv.id, "who's at the door?", user=None)
    await collect(kernel, first)
    session = first.turn.hermes_session
    clock.advance(minutes=1)
    check = await kernel.attendance.ask(family.tv.id, "mom", "voice")
    # Something the TV did after the question, before the tap, becomes hers.
    await kernel.policy.request("home", "announce", {}, Caller(kind="hermes", session=session, device=family.tv.id))
    await kernel.attendance.answer(check.id, True, family.mom_phone)

    # Over MCP the household's profile now acts for her.
    caller = caller_from(kernel, {"_session": session}, None)
    assert (caller.user, caller.device) == ("mom", family.tv.id)
    trigger = await kernel.triggers.register(TriggerSpec.model_validate({"note": "x", "when": [{"event": "home.doorbell"}]}), caller)
    assert trigger.status == "active" and trigger.user == "mom"
    held = await kernel.policy.request("home", "unlock", {}, caller)
    assert kernel.policy.get(held.data["approval"]).user == "mom"

    # The model hears it on the next ask, in the same Hermes session.
    second = await kernel.conversations.ask(family.tv.id, "thanks", user=None)
    await collect(kernel, second)
    message = hermes.messages[-1][1]
    assert hermes.messages[-1][0] == session
    assert "Mom was recognized" in message and message.endswith("thanks")

    # The log: the turn before the question stays the household's; what
    # came after is hers, and only she sees it.
    turns = kernel.events.search(type="conversation.turn")
    assert [(t.entity, t.user) for t in reversed(turns)] == [(first.turn.id, None), (second.turn.id, "mom")]
    assert "who's at the door?" in turns[-1].summary
    announced = kernel.events.search(type="action.ran")[-1]
    assert announced.user == "mom"
    assert kernel.events.search(type="conversation.turn", viewer="me")[0].user is None
    assert {e.user for e in kernel.events.search(viewer="mom")} <= {None, "mom"}
    assert all(e.user is None for e in kernel.events.search(viewer=None))


async def test_a_tap_on_the_tv_still_needs_a_phone(kernel, family, home):
    await recognize(kernel, family, family.mom_phone)
    held = await kernel.policy.request("home", "unlock", {}, Caller(kind="frontend", user="mom", device=family.tv.id))
    assert held.data["status"] == "pending_approval"
    assert home.acted == []


async def test_release_and_idle_end_it(kernel, family, clock):
    await recognize(kernel, family, family.mom_phone)
    with pytest.raises(AuthError):
        await kernel.attendance.release(family.tv.id, family.me_phone, "x")
    await kernel.attendance.release(family.tv.id, family.mom_phone, "she left")
    assert kernel.attendance.holder(family.tv.id) is None
    await recognize(kernel, family, family.me_phone, "me")
    clock.advance(minutes=11)
    await kernel.attendance.expire()
    assert kernel.attendance.holder(family.tv.id) is None


async def test_someone_else_takes_over_and_questions_expire(kernel, family, clock):
    await recognize(kernel, family, family.mom_phone)
    await recognize(kernel, family, family.me_phone, "me")
    assert kernel.attendance.holder(family.tv.id) == "me"
    await kernel.attendance.ask(family.tv.id, "mom", "device")
    clock.advance(minutes=6)
    await kernel.attendance.expire()
    assert kernel.attendance.pending_for("mom") == []
