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
    audit = [e.type for e in kernel.events.search(source="kernel")]
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


async def test_external_results_keep_their_label(kernel):
    result = await kernel.tools.call("state_get", {"values": ["home.snapshot", "clock.time"]}, Caller(kind="hermes", session="s9"))
    assert "external" in result.labels


async def test_the_profile_names_the_user_and_the_session_the_device(kernel, family):
    kernel.sessions.record("s9", "chat", user="me", device=family.me_web.id)
    args = {"_session": "s9", "values": ["clock.time"]}
    caller = caller_from(kernel, args, "me", None)
    assert (caller.user, caller.session, caller.device) == ("me", "s9", family.me_web.id)
    assert args == {"values": ["clock.time"]}
    # Mom's profile can't borrow my session.
    borrowed = caller_from(kernel, {"_session": "s9"}, "mom", None)
    assert (borrowed.user, borrowed.session, borrowed.device) == ("mom", None, None)


async def test_unknown_tools_and_missing_args_explain_themselves(kernel):
    with pytest.raises(ToolError, match="Did you mean state_get"):
        await kernel.tools.call("state_gte", {}, Caller(kind="hermes"))
    with pytest.raises(ToolError, match="needs values"):
        await kernel.tools.call("state_get", {}, Caller(kind="hermes"))


async def test_events_tools_take_relative_times(kernel, clock):
    await kernel.registry.get("home").ctx.emit("doorbell", "ding")
    clock.advance(hours=3)
    recent = await kernel.tools.call("events_search", {"type": "home.doorbell", "since": "2h"}, Caller(kind="hermes"))
    older = await kernel.tools.call("events_search", {"type": "home.doorbell", "since": "4h"}, Caller(kind="hermes"))
    assert recent.data == [] and len(older.data) == 1
    # The clock started at 23:50, so the doorbell rang yesterday.
    digest = await kernel.tools.call("events_digest", {"day": "yesterday"}, Caller(kind="hermes"))
    assert [t["type"] for t in digest.data["by_type"]] == ["home.doorbell"]


SCRIPT = """
def run(ctx):
    snap = ctx.call("state_get", {"values": ["home.snapshot"]})["home.snapshot"]["value"]
    ctx.log("snapshot says", snap["text"])
    ctx.call("home_turn_on", {"entity_id": "light.porch"})
    if "note" in snap["text"]:
        ctx.wake("someone held up a note", {"ref": snap["ref"]})
"""


async def test_scripts_call_granted_tools_and_wake(kernel, home, hermes, family):
    spec = {"note": "Triage the doorbell.", "when": [{"event": "home.doorbell"}], "repeat": "yes", "script": SCRIPT, "grants": ["state_get", "home_turn_on"]}
    await kernel.triggers.register(TriggerSpec.model_validate(spec), Caller(kind="hermes", session="s1", user="me"))
    await kernel.registry.get("home").ctx.emit("doorbell", "ding")
    await settle(kernel)
    assert home.acted == [("turn_on", {"entity_id": "light.porch"})]
    assert len(wakes(hermes)) == 1
    assert "someone held up a note" in wakes(hermes)[0]
    script_events = kernel.events.search(type="trigger.script")
    assert script_events, [e.summary for e in kernel.events.search(source="kernel")]


async def test_dry_runs_skip_actions_and_wakes(kernel, home, hermes):
    await kernel.registry.get("home").ctx.emit("doorbell", "ding")
    spec = {"note": "Triage.", "when": [{"event": "home.doorbell"}], "script": SCRIPT, "grants": ["state_get", "home_turn_on"]}
    report = await kernel.tools.call("triggers_test", spec, Caller(kind="hermes"))
    [run] = report.data["script_runs"]
    assert run["error"] is None, run["log"]
    assert [c.get("skipped") for c in run["calls"]] == [None, "dry run"]
    assert run["wakes"] and home.acted == [] and wakes(hermes) == []
    assert "snapshot says" in run["log"]


async def test_scripts_outside_their_grants_are_refused(kernel, home):
    spec = {"note": "x", "when": [{"event": "home.doorbell"}], "script": SCRIPT, "grants": ["state_get"]}
    report = await kernel.tools.call("triggers_test", spec, Caller(kind="hermes"))
    [run] = report.data["script_runs"]
    assert "not in this trigger's grants" in run["error"]


async def test_bad_scripts_fail_at_registration(kernel):
    for script, message in [("def run(ctx)\n  pass", "script line"), ("x = 1", "must define run"), (SCRIPT, "names no tool")]:
        spec = {"note": "x", "when": [{"event": "home.doorbell"}], "script": script, "grants": ["home_snapshots"] if message == "names no tool" else []}
        with pytest.raises(ToolError, match=message):
            await kernel.tools.call("triggers_register", spec, Caller(kind="hermes"))


async def test_mcp_results_are_readable_and_marked(kernel):
    from huvu.times import for_model

    await kernel.registry.get("home").ctx.update("eta_min", 12)
    result = await kernel.tools.call("state_get", {"values": ["home.eta_min"]}, Caller(kind="hermes"))
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
