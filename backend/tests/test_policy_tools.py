from __future__ import annotations

import asyncio
import json

import pytest

from huvu.accounts import AuthError
from huvu.mcp_server import caller_from
from huvu.spec import Caller
from huvu.tools import ToolError
from huvu.triggers import TriggerSpec

from .test_triggers import settle, wakes


async def test_silent_actions_run_and_are_audited(kernel, home):
    result = await kernel.tools.call("home_turn_on", {"entity_id": "light.porch", "reason": "you asked"}, Caller(kind="hermes", session="s1"))
    assert result.data == {"done": "turn_on"}
    assert home.acted == [("turn_on", {"entity_id": "light.porch"})]
    audit = [r["type"] for r in kernel.data.rows("huvu", "events", "1 = 1", ())]
    assert "action.requested" in audit and "action.ran" in audit


async def test_notify_actions_run_at_once(kernel, home):
    result = await kernel.policy.request("home", "announce", {}, Caller(kind="hermes"))
    assert result.data == {"done": "announce"}
    assert home.acted == [("announce", {})]


async def test_requires_human_always_asks_and_only_its_users_phone_answers(kernel, home, family):
    held = await kernel.tools.call("home_unlock", {}, Caller(kind="hermes", user="me"))
    assert held.data["status"] == "pending_approval"
    assert home.acted == []
    for other in (family.mom_phone, family.me_web, family.tv):
        with pytest.raises(AuthError):
            await kernel.policy.resolve(held.data["approval"], approve=True, by=other)
    approval = await kernel.policy.resolve(held.data["approval"], approve=True, by=family.me_phone)
    assert approval.status == "approved"
    assert home.acted == [("unlock", {})]
    assert home.actors[-1].user == "me" and home.actors[-1].device == family.me_phone.id


async def test_approvals_go_to_the_users_phones_or_everyones(kernel, family):
    from .test_triggers import push_events

    mine, mine_task = await push_events(kernel, family.me_phone.id)
    moms, moms_task = await push_events(kernel, family.mom_phone.id)
    laptop, laptop_task = await push_events(kernel, family.me_web.id)
    await kernel.policy.request("home", "unlock", {}, Caller(kind="hermes", user="me"))
    await kernel.policy.request("home", "unlock", {}, Caller(kind="hermes", device=family.tv.id))
    await asyncio.sleep(0)
    assert [d["user"] for _, d in mine] == ["me", None]
    assert [d["user"] for _, d in moms] == [None]
    assert laptop == []
    # The household's approval can be answered from either phone.
    household = moms[0][1]["id"]
    assert (await kernel.policy.resolve(household, approve=False, by=family.mom_phone)).status == "denied"
    for task in (mine_task, moms_task, laptop_task):
        task.cancel()


async def test_denied_and_expired_approvals_never_run(kernel, clock, home, family):
    held = await kernel.policy.request("home", "unlock", {}, Caller(kind="hermes", user="me"))
    await kernel.policy.resolve(held.data["approval"], approve=False, by=family.me_phone)
    second = await kernel.policy.request("home", "unlock", {}, Caller(kind="hermes", user="me"))
    clock.advance(minutes=11)
    with pytest.raises(Exception, match="expired"):
        await kernel.policy.resolve(second.data["approval"], approve=True, by=family.me_phone)
    assert home.acted == []


async def test_taps_run_at_once_on_personal_devices_only(kernel, home, family):
    await kernel.policy.request("home", "unlock", {}, Caller(kind="frontend", user="me", device=family.me_web.id))
    assert home.acted == [("unlock", {})]
    held = await kernel.policy.request("home", "unlock", {}, Caller(kind="frontend", device=family.tv.id))
    assert held.data["status"] == "pending_approval"
    assert home.acted == [("unlock", {})]


async def test_notifications_reach_the_users_phones(kernel, family):
    from .test_triggers import push_events

    mine, mine_task = await push_events(kernel, family.me_phone.id)
    moms, moms_task = await push_events(kernel, family.mom_phone.id)
    await kernel.tools.call("huvu_notify", {"text": "for me"}, Caller(kind="hermes", user="me"))
    await kernel.tools.call("huvu_notify", {"text": "for everyone"}, Caller(kind="hermes"))
    await asyncio.sleep(0)
    assert [d["text"] for _, d in mine] == ["for me", "for everyone"]
    assert [d["text"] for _, d in moms] == ["for everyone"]
    mine_task.cancel()
    moms_task.cancel()


async def test_external_results_keep_their_label(kernel, home):
    await home.press()
    result = await kernel.tools.call("data_sql", {"sql": "SELECT note FROM home_doorbell"}, Caller(kind="hermes", session="s9"))
    assert "external" in result.labels
    plain = await kernel.tools.call("data_sql", {"sql": "SELECT state FROM home_lights"}, Caller(kind="hermes", session="s9"))
    assert plain.labels == ["house"]


async def test_the_profile_names_the_user_and_the_session_the_device(kernel, family):
    kernel.sessions.record("s9", "chat", user="me", device=family.me_web.id)
    args = {"_session": "s9", "sql": "SELECT 1"}
    caller = caller_from(kernel, args, "me", None)
    assert (caller.user, caller.session, caller.device) == ("me", "s9", family.me_web.id)
    assert args == {"sql": "SELECT 1"}
    # Mom's profile can't borrow my session.
    borrowed = caller_from(kernel, {"_session": "s9"}, "mom", None)
    assert (borrowed.user, borrowed.session, borrowed.device) == ("mom", None, None)


async def test_unknown_tools_and_missing_args_explain_themselves(kernel):
    with pytest.raises(ToolError, match="Did you mean data_sql"):
        await kernel.tools.call("data_sq", {}, Caller(kind="hermes"))
    with pytest.raises(ToolError, match="needs sql"):
        await kernel.tools.call("data_sql", {}, Caller(kind="hermes"))
    # The read tools of before are gone.
    with pytest.raises(ToolError, match="no tool named 'state_get'"):
        await kernel.tools.call("state_get", {"values": ["home.light"]}, Caller(kind="hermes"))


SCRIPT = """
def run(ctx):
    for press in ctx.rows:
        ctx.log("snapshot says", press["note"])
        porch = ctx.call("data_sql", {"sql": "SELECT state FROM home_lights WHERE entity_id = 'light.porch'"})
        if porch["rows"] == [["off"]]:
            ctx.call("home_turn_on", {"entity_id": "light.porch"})
        if "note" in press["note"]:
            ctx.wake("someone held up a note", {"ref": press["snapshot"]})
"""
TRIAGE = {"note": "Triage the doorbell.", "sql": "SELECT id, note, snapshot FROM home_doorbell", "fire": "rows", "once": False, "script": SCRIPT, "grants": ["data_sql", "home_turn_on"]}


async def test_scripts_call_granted_tools_and_wake(kernel, home, hermes, family):
    await kernel.triggers.register(TriggerSpec.model_validate(TRIAGE), Caller(kind="hermes", session="s1", user="me"))
    await home.press()
    await settle(kernel)
    assert home.acted == [("turn_on", {"entity_id": "light.porch"})]
    assert len(wakes(hermes)) == 1
    assert "someone held up a note" in wakes(hermes)[0]
    assert kernel.data.rows("huvu", "events", "type = 'trigger.script'", ())


async def test_dry_runs_skip_actions_and_wakes(kernel, home, hermes):
    await home.press()
    report = await kernel.tools.call("triggers_register", {**TRIAGE, "dry_run": True}, Caller(kind="hermes"))
    run = report.data["script"]
    assert run["error"] is None, run["log"]
    assert [c.get("skipped") for c in run["calls"]] == [None, "dry run"]
    assert run["wakes"] and home.acted == [] and wakes(hermes) == []
    assert "snapshot says" in run["log"]


async def test_scripts_outside_their_grants_are_refused(kernel, home):
    await home.press()
    report = await kernel.tools.call("triggers_register", {**TRIAGE, "grants": ["data_sql"], "dry_run": True}, Caller(kind="hermes"))
    assert "not in this trigger's grants" in report.data["script"]["error"]


async def test_bad_scripts_fail_at_registration(kernel):
    for script, message in [("def run(ctx)\n  pass", "script line"), ("x = 1", "must define run"), (SCRIPT, "names no tool")]:
        spec = {**TRIAGE, "script": script, "grants": ["home_snapshots"] if message == "names no tool" else []}
        with pytest.raises(ToolError, match=message):
            await kernel.tools.call("triggers_register", spec, Caller(kind="hermes"))


async def test_mcp_results_are_readable_and_marked(kernel, family):
    from huvu.times import for_model

    result = await kernel.tools.call("triggers_register", TRIAGE, Caller(kind="hermes", user="me"))
    text = json.dumps(for_model(result.data, kernel.now()))
    assert "just now" in text


async def test_a_double_press_opens_one_approval(kernel, home, family):
    me = Caller(kind="hermes", user="me")
    first, second = await asyncio.gather(
        kernel.policy.request("home", "unlock", {}, me),
        kernel.policy.request("home", "unlock", {}, me),
    )
    assert first.data["status"] == "pending_approval"
    assert second.data == {"status": "already_pending"}
    assert len(kernel.policy.pending()) == 1
    # Once it's answered, the same request opens a new one.
    await kernel.policy.resolve(first.data["approval"], approve=False, by=family.me_phone)
    again = await kernel.policy.request("home", "unlock", {}, me)
    assert again.data["status"] == "pending_approval"


async def test_two_answers_at_once_run_it_once(kernel, home, family):
    held = await kernel.policy.request("home", "unlock", {}, Caller(kind="hermes", device=family.tv.id, group="home"))
    results = await asyncio.gather(
        kernel.policy.resolve(held.data["approval"], approve=True, by=family.me_phone),
        kernel.policy.resolve(held.data["approval"], approve=True, by=family.mom_phone),
        return_exceptions=True,
    )
    assert home.acted == [("unlock", {})]
    assert sum(isinstance(r, Exception) for r in results) == 1


async def test_missed_notifications_are_listed_for_the_phone(kernel, family):
    await kernel.tools.call("huvu_notify", {"text": "for me"}, Caller(kind="hermes", user="me"))
    await kernel.tools.call("huvu_notify", {"text": "for everyone"}, Caller(kind="hermes"))
    assert [n["text"] for n in kernel.notifications("me")] == ["for me", "for everyone"]
    assert [n["text"] for n in kernel.notifications("mom")] == ["for everyone"]


async def test_approvals_say_what_and_who_in_words(kernel, family):
    held = await kernel.policy.request("home", "unlock", {}, Caller(kind="hermes", user="me", device=family.me_web.id))
    asked_at_tv = await kernel.policy.request("home", "unlock", {"door": "back"}, Caller(kind="frontend", device=family.tv.id, group="home"))
    mine = kernel.policy.public(kernel.policy.get(held.data["approval"]))
    assert mine["label"] == "Unlocks the front door." and mine["requester"] == "huvu, talking with Me on laptop"
    theirs = kernel.policy.public(kernel.policy.get(asked_at_tv.data["approval"]))
    assert theirs["requester"] == "someone on living room"


async def test_people_group_lists_the_family_by_the_callers_aliases(kernel, family):
    kernel.accounts.set_alias("me", "mom", "Amma")
    seen = await kernel.tools.call("people_group", {}, Caller(kind="hermes", user="me"))
    [home] = seen.data["groups"]
    assert home["id"] == "home"
    assert {p["id"]: (p["alias"], p["you"]) for p in home["people"]} == {"me": ("Me", True), "mom": ("Amma", False)}
    # Mom set nothing, so she sees my name.
    moms = await kernel.tools.call("people_group", {"group": "home"}, Caller(kind="hermes", user="mom"))
    assert {p["id"]: p["alias"] for p in moms.data["groups"][0]["people"]}["me"] == "Me"
    with pytest.raises(ToolError, match="your groups: home"):
        await kernel.tools.call("people_group", {"group": "beach"}, Caller(kind="hermes", user="me"))


async def test_an_alias_names_one_person_only(kernel, family):
    accounts = kernel.accounts
    accounts.set_alias("me", "mom", "Amma")
    # What I call someone else, their name when I set nothing, someone
    # else's username, and a group's id are all taken.
    accounts.add_user("dad", "Dad", "a long password", "key-dad")
    accounts.add_user("sis", "Sister", "a long password", "key-sis")
    for taken, why in (("amma", "what me calls mom"), ("sis", "sis's username"), ("Sister ", "what me calls sis"), ("home", "the group home")):
        with pytest.raises(AuthError, match=why):
            accounts.set_alias("me", "dad", taken)
    # Mom has her own names: she can call dad "Amma". Dad's own name is
    # fine for him.
    accounts.set_alias("mom", "dad", "Amma")
    accounts.set_alias("me", "dad", "dad")
    accounts.set_alias("me", "mom", "")
    assert accounts.alias("me", "mom") == "Mom"


async def test_a_new_user_or_group_cant_take_an_alias(kernel, family):
    kernel.accounts.set_alias("me", "mom", "amma")
    with pytest.raises(AuthError, match="me calls mom 'amma'"):
        kernel.accounts.add_user("amma", "Amma", "a long password", "key-a")
    with pytest.raises(AuthError, match="me calls mom 'amma'"):
        kernel.accounts.add_group("amma", "Amma's", "key-g")


async def test_the_model_sets_what_its_person_calls_someone(kernel, family):
    me = Caller(kind="hermes", user="me")
    result = await kernel.tools.call("huvu_set_person_alias", {"person": "Mom", "alias": "Amma"}, me)
    assert result.data == {"person": "mom", "alias": "Amma"}
    # Now the old alias and the new one both reach her: the username, and "Amma".
    await kernel.tools.call("huvu_set_person_alias", {"person": "amma", "alias": "Ma"}, me)
    assert kernel.accounts.alias("me", "mom") == "Ma"
    assert kernel.accounts.alias("mom", "me") == "Me"
    with pytest.raises(Exception, match="the group home"):
        await kernel.tools.call("huvu_set_person_alias", {"person": "mom", "alias": "home"}, me)
    with pytest.raises(Exception, match="nobody is recognized"):
        await kernel.tools.call("huvu_set_person_alias", {"person": "mom", "alias": "x"}, Caller(kind="hermes", device=family.tv.id))


async def test_messages_reach_the_person_by_their_alias_for_the_sender(kernel, family):
    from .test_triggers import push_events

    kernel.accounts.set_alias("me", "mom", "Amma")
    kernel.accounts.set_alias("mom", "me", "kanna")
    mine, mine_task = await push_events(kernel, family.me_phone.id)
    moms, moms_task = await push_events(kernel, family.mom_phone.id)
    result = await kernel.tools.call("huvu_message", {"person": "amma", "text": "running late"}, Caller(kind="hermes", user="me"))
    await asyncio.sleep(0)
    assert result.data == {"sent": True, "to": "amma"}
    assert [(d["text"], d["from"]) for _, d in moms] == [("running late", "kanna")]
    # The sender hears that it went (notify autonomy), from huvu.
    assert [d["from"] for _, d in mine] == [None]
    assert [(n["text"], n["from"]) for n in kernel.notifications("mom")] == [("running late", "kanna")]
    rows = kernel.data.rows("huvu", "notifications", "1 = 1", ())
    assert sorted((r["user_id"], r["sender"], r["recipient"]) for r in rows) == [("me", "me", "mom"), ("mom", "me", "mom")]
    mine_task.cancel()
    moms_task.cancel()


async def test_group_messages_skip_their_sender(kernel, family):
    from .test_triggers import push_events

    kernel.accounts.set_alias("mom", "me", "kanna")
    moms, moms_task = await push_events(kernel, family.mom_phone.id)
    await kernel.tools.call("huvu_message", {"group": "home", "text": "dinner", "reason": "asked"}, Caller(kind="hermes", user="me"))
    await asyncio.sleep(0)
    assert [(d["text"], d["from"]) for _, d in moms] == [("dinner", "kanna")]
    assert [n["text"] for n in kernel.notifications("me") if n["from"]] == []
    assert [n["from"] for n in kernel.notifications("mom")] == ["kanna"]
    moms_task.cancel()


async def test_messages_go_only_to_people_the_sender_shares_a_group_with(kernel, family):
    kernel.accounts.add_user("stranger", "Stranger", "a long password", "key-s")
    with pytest.raises(Exception, match="you can message Mom"):
        await kernel.tools.call("huvu_message", {"person": "stranger", "text": "hi"}, Caller(kind="hermes", user="me"))
    with pytest.raises(Exception, match="nobody is recognized"):
        await kernel.tools.call("huvu_message", {"person": "mom", "text": "hi"}, Caller(kind="hermes", device=family.tv.id))
    with pytest.raises(Exception, match="one of the two"):
        await kernel.tools.call("huvu_message", {"person": "mom", "group": "home", "text": "hi"}, Caller(kind="hermes", user="me"))
