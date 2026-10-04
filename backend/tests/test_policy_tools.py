from __future__ import annotations

import json

import pytest

from huvu.mcp_server import caller_from
from huvu.policy import Caller
from huvu.tools import ToolError
from huvu.triggers import TriggerSpec

from .test_triggers import settle, wakes


async def test_silent_actions_run_and_are_audited(kernel, home):
    result = await kernel.tools.call("home_turn_on", {"entity_id": "light.porch", "reason": "you asked"}, Caller(kind="hermes", session="s1"))
    assert result.data == {"done": "turn_on"}
    assert home.acted == [("turn_on", {"entity_id": "light.porch"})]
    audit = [e.type for e in kernel.events.search(source="kernel")]
    assert "action.requested" in audit and "action.ran" in audit


async def test_tainted_callers_get_ask_in_place_of_notify(kernel, home):
    clean = await kernel.policy.request("home", "announce", {}, Caller(kind="hermes"))
    assert clean.data == {"done": "announce"}
    held = await kernel.policy.request("home", "announce", {}, Caller(kind="hermes", tainted=True))
    assert held.data["status"] == "pending_approval"
    assert home.acted == [("announce", {})]


async def test_requires_human_always_asks_and_only_the_owner_answers(kernel, home):
    held = await kernel.tools.call("home_unlock", {}, Caller(kind="hermes"))
    assert held.data["status"] == "pending_approval"
    assert home.acted == []
    approval = await kernel.policy.resolve(held.data["approval"], approve=True)
    assert approval.status == "approved"
    assert home.acted == [("unlock", {})]


async def test_denied_and_expired_approvals_never_run(kernel, clock, home):
    held = await kernel.policy.request("home", "unlock", {}, Caller(kind="hermes"))
    await kernel.policy.resolve(held.data["approval"], approve=False)
    second = await kernel.policy.request("home", "unlock", {}, Caller(kind="hermes"))
    clock.advance(minutes=11)
    with pytest.raises(Exception, match="expired"):
        await kernel.policy.resolve(second.data["approval"], approve=True)
    assert home.acted == []


async def test_frontend_taps_run_at_once(kernel, home):
    await kernel.policy.request("home", "unlock", {}, Caller(kind="frontend"))
    assert home.acted == [("unlock", {})]


async def test_external_results_taint_the_session(kernel):
    caller = Caller(kind="hermes", session="s9")
    result = await kernel.tools.call("home_snapshot", {}, caller)
    assert "external" in result.labels
    assert kernel.sessions.is_tainted("s9")
    args = {"_session": "s9", "values": ["clock.time"]}
    later = caller_from(kernel, args)
    assert later.tainted and args == {"values": ["clock.time"]}


async def test_the_plugin_flag_taints_too(kernel):
    caller = caller_from(kernel, {"_session": "s3", "_tainted": True})
    assert caller.tainted and kernel.sessions.is_tainted("s3")


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
    snap = ctx.call("home_snapshot")
    ctx.log("snapshot says", snap["text"])
    ctx.call("home_turn_on", {"entity_id": "light.porch"})
    if "note" in snap["text"]:
        ctx.wake("someone held up a note", {"ref": snap["ref"]})
"""


async def test_scripts_call_granted_tools_and_wake(kernel, home, hermes):
    spec = {"note": "Triage the doorbell.", "when": [{"event": "home.doorbell"}], "repeat": "yes", "script": SCRIPT, "grants": ["home_snapshot", "home_turn_on"]}
    await kernel.triggers.register(TriggerSpec.model_validate(spec), Caller(kind="hermes", session="s1"))
    await kernel.registry.get("home").ctx.emit("doorbell", "ding")
    await settle(kernel)
    assert home.acted == [("turn_on", {"entity_id": "light.porch"})]
    assert len(wakes(hermes)) == 1
    assert "someone held up a note" in wakes(hermes)[0]
    script_events = kernel.events.search(type="trigger.script")
    assert script_events, [e.summary for e in kernel.events.search(source="kernel")]


async def test_dry_runs_skip_actions_and_wakes(kernel, home, hermes):
    await kernel.registry.get("home").ctx.emit("doorbell", "ding")
    spec = {"note": "Triage.", "when": [{"event": "home.doorbell"}], "script": SCRIPT, "grants": ["home_snapshot", "home_turn_on"]}
    report = await kernel.tools.call("triggers_test", spec, Caller(kind="hermes"))
    [run] = report.data["script_runs"]
    assert run["error"] is None, run["log"]
    assert [c.get("skipped") for c in run["calls"]] == [None, "dry run"]
    assert run["wakes"] and home.acted == [] and wakes(hermes) == []
    assert "snapshot says" in run["log"]


async def test_scripts_outside_their_grants_are_refused(kernel, home):
    spec = {"note": "x", "when": [{"event": "home.doorbell"}], "script": SCRIPT, "grants": ["home_snapshot"]}
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
