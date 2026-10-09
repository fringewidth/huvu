"""Groups: members trust each other, guests trust nothing, and a second
group keeps its devices, profile and claims to itself."""

from __future__ import annotations

import asyncio

import pytest

from huvu.accounts import AuthError
from huvu.mcp_server import caller_from
from huvu.spec import Caller
from huvu.triggers import TriggerSpec

from .test_conversations import collect
from .test_triggers import push_events
from .fakes import WAKE

BELL = {"note": "x", "script": WAKE, "sql": "SELECT id FROM home_doorbell", "fire": "rows", "once": False}


@pytest.fixture
def sitter(kernel, family):
    kernel.accounts.add_user("sitter", "Sitter", "the kids are asleep", "key-sitter")
    kernel.accounts.join("home", "sitter", "guest")
    phone, _ = kernel.accounts.login("sitter", "the kids are asleep", "sitter's phone", "phone")
    return phone


async def test_a_guest_never_approves_even_on_their_own_phone(kernel, home, family, sitter):
    moms, moms_task = await push_events(kernel, family.mom_phone.id)
    tapped = await kernel.policy.request("home", "unlock", {}, Caller(kind="frontend", user="sitter", device=sitter.id))
    assert tapped.data["status"] == "pending_approval" and home.acted == []
    approval = kernel.policy.get(tapped.data["approval"])
    assert (approval.user, approval.group) == (None, "home")
    await asyncio.sleep(0)
    assert [n for n, _ in moms] == ["approval"]
    with pytest.raises(AuthError):
        await kernel.policy.resolve(approval.id, approve=True, by=sitter)
    await kernel.policy.resolve(approval.id, approve=True, by=family.mom_phone)
    assert home.acted == [("unlock", {})]
    moms_task.cancel()


async def test_a_guest_can_be_recognized_but_still_approves_nothing(kernel, family, sitter):
    check = await kernel.attendance.ask(family.tv.id, "sitter", "device")
    await kernel.attendance.answer(check.id, True, sitter)
    caller = Caller(kind="hermes", user="sitter", device=family.tv.id, group="home")
    held = await kernel.policy.request("home", "unlock", {}, caller)
    assert kernel.policy.get(held.data["approval"]).group == "home"
    # The trigger is hers, though; it's only trust she lacks.
    trigger = await kernel.triggers.register(TriggerSpec.model_validate(BELL), caller)
    assert trigger.user == "sitter" and trigger.status == "active"


async def test_guests_see_only_their_own_rows(kernel, home, family, sitter):
    await home.press()
    tv_turn = await kernel.conversations.ask(family.tv.id, "hi", user=None, group="home")
    await collect(kernel, tv_turn)
    await kernel.policy.request("home", "announce", {}, Caller(kind="hermes", user="sitter"))

    async def rows(user: str, sql: str) -> list[list]:
        return (await kernel.tools.call("data_sql", {"sql": sql}, Caller(kind="hermes", user=user))).data["rows"]

    assert await rows("me", "SELECT count(*) FROM home_doorbell") == [[1]]
    assert await rows("me", "SELECT them FROM chat_turns") == [["hi"]]
    assert await rows("sitter", "SELECT count(*) FROM home_doorbell") == [[0]]
    assert await rows("sitter", "SELECT count(*) FROM chat_turns") == [[0]]
    theirs = await rows("sitter", "SELECT DISTINCT user_id FROM huvu_events")
    assert theirs == [["sitter"]]
    assert await rows("me", "SELECT count(*) FROM huvu_events WHERE type = 'action.ran' AND user_id = 'sitter'") == [[0]]


def test_guests_cant_pair(kernel, family, sitter):
    pairing = kernel.accounts.start_pairing("speaker", "speaker")
    with pytest.raises(AuthError):
        kernel.accounts.confirm_pairing(pairing.code, sitter)


async def test_a_guest_is_asked_too_and_can_own_the_trigger(kernel, family, sitter):
    tv = Caller(kind="hermes", session="s-tv", device=family.tv.id, group="home")
    await kernel.triggers.register(TriggerSpec.model_validate(BELL), tv)
    [question] = kernel.attendance.pending_for("sitter")
    await kernel.attendance.answer(question["id"], True, sitter)
    [trigger] = kernel.triggers.list(Caller(kind="hermes", user="sitter"))
    assert trigger.status == "active"


async def test_a_second_group_keeps_to_itself(kernel, family, hermes):
    accounts = kernel.accounts
    beach_token = accounts.add_group("beach", "Beach house", "key-beach")
    accounts.join("beach", "me", "member")
    pairing = accounts.start_pairing("beach TV", "tv")
    # Members of two groups say which one the device joins.
    with pytest.raises(AuthError, match="beach, home"):
        accounts.confirm_pairing(pairing.code, family.me_phone)
    beach_tv = accounts.confirm_pairing(pairing.code, family.me_phone, "beach")
    assert beach_tv.group == "beach"

    # Its shared device chats in the group's own Hermes profile.
    live = await kernel.conversations.ask(beach_tv.id, "hi", user=None, group="beach")
    await collect(kernel, live)
    assert hermes.profiles[live.turn.hermes_session] == "beach"
    assert accounts.mcp_caller(beach_token) == (True, None, "beach")
    caller = caller_from(kernel, {"_session": live.turn.hermes_session}, None, "beach")
    assert (caller.group, caller.device) == ("beach", beach_tv.id)
    # The home profile can't borrow it.
    assert caller_from(kernel, {"_session": live.turn.hermes_session}, None, "home").device is None

    # A trigger set up there asks only its people, and mom isn't one.
    trigger = await kernel.triggers.register(TriggerSpec.model_validate(BELL), caller)
    assert kernel.attendance.pending_for("mom") == []
    [question] = kernel.attendance.pending_for("me")
    await kernel.attendance.answer(question["id"], True, family.me_phone)
    assert trigger.user == "me" and trigger.status == "active"
    # Only its members see what happened there.
    moms = await kernel.tools.call("data_sql", {"sql": "SELECT count(*) FROM chat_turns"}, Caller(kind="hermes", user="mom"))
    assert moms.data["rows"] == [[0]]


def test_users_and_groups_share_one_namespace(kernel, family):
    with pytest.raises(AuthError, match="already"):
        kernel.accounts.add_group("me", "Me's group", "k")
    with pytest.raises(AuthError, match="already"):
        kernel.accounts.add_user("home", "Home", "a long password", "k")
