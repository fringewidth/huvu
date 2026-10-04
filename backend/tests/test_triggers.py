from __future__ import annotations

import asyncio

import pytest

from huvu.policy import Caller
from huvu.triggers import TriggerError, TriggerSpec, compare

OWNER = Caller(kind="hermes", session="s1")

PORCH = {
    "note": "Turn on the porch light.",
    "when": [
        {"value": "clock.time", "op": "between", "arg": ["00:00", "05:00"]},
        {"value": "home.eta_min", "op": "<=", "arg": 10},
        {"value": "home.light", "params": {"entity_id": "light.porch"}, "op": "==", "arg": "off"},
    ],
    "repeat": "daily",
    "include": ["huvu.mode"],
}


async def settle(kernel) -> None:
    for _ in range(3):
        await asyncio.sleep(0)
        await asyncio.gather(*kernel.triggers._tasks, return_exceptions=True)


async def tick_clock(kernel) -> None:
    await kernel.registry.get("clock")._push(kernel.now())


def wakes(hermes) -> list[str]:
    return [m for s, m in hermes.messages if s.startswith("wake-")]


def test_compare_wraps_time_windows_past_midnight():
    assert compare("between", "23:30", ["22:00", "05:00"], "time")
    assert compare("between", "04:59", ["22:00", "05:00"], "time")
    assert not compare("between", "05:00", ["22:00", "05:00"], "time")
    assert compare("<=", "9", 10, "number")
    assert compare("in", "away", ["away", "sleeping"], "string")


async def test_porch_light_fires_on_the_rising_edge_once_a_night(kernel, clock, hermes):
    home = kernel.registry.get("home").ctx
    await home.update("light", "off", params={"entity_id": "light.porch"})
    await home.update("eta_min", 9)
    await tick_clock(kernel)
    trigger = await kernel.triggers.register(TriggerSpec.model_validate(PORCH), OWNER)
    await settle(kernel)
    # 23:50: outside the window, so nothing yet.
    assert wakes(hermes) == []

    clock.advance(minutes=10)
    await home.update("eta_min", 8)
    await tick_clock(kernel)
    await settle(kernel)
    assert len(wakes(hermes)) == 1
    assert "Turn on the porch light." in wakes(hermes)[0]
    assert "huvu.mode" in wakes(hermes)[0]

    # Still true a minute later: no second fire tonight.
    clock.advance(minutes=1)
    await home.update("eta_min", 2)
    await tick_clock(kernel)
    await settle(kernel)
    assert len(wakes(hermes)) == 1
    assert trigger.fire_count == 1

    # The next night it fires again.
    clock.advance(days=1)
    await home.update("eta_min", 7)
    await tick_clock(kernel)
    await settle(kernel)
    assert len(wakes(hermes)) == 2


async def test_whichever_part_comes_true_last_fires_it(kernel, clock, hermes):
    """At 23:58 the owner is already 8 minutes away. The window opens at
    00:00, and the trigger fires then."""
    home = kernel.registry.get("home").ctx
    clock.advance(minutes=8)
    await home.update("light", "off", params={"entity_id": "light.porch"})
    await home.update("eta_min", 8)
    await tick_clock(kernel)
    await kernel.triggers.register(TriggerSpec.model_validate(PORCH), OWNER)
    await settle(kernel)
    assert wakes(hermes) == []
    clock.advance(minutes=2)
    await tick_clock(kernel)
    await settle(kernel)
    assert len(wakes(hermes)) == 1


async def test_stale_readings_count_as_false(kernel, clock, hermes):
    home = kernel.registry.get("home").ctx
    await home.update("light", "off", params={"entity_id": "light.porch"})
    await home.update("eta_min", 5)
    clock.advance(minutes=40)
    await tick_clock(kernel)
    await kernel.triggers.register(TriggerSpec.model_validate(PORCH), OWNER)
    await settle(kernel)
    # eta_min is 40 minutes old, past its 30 minute max age.
    assert wakes(hermes) == []


async def test_repeat_yes_rearms_when_the_condition_turns_false(kernel, hermes):
    home = kernel.registry.get("home").ctx
    spec = {"note": "Light went off.", "when": [{"value": "home.light[light.porch]", "op": "==", "arg": "off"}], "repeat": "yes"}
    await home.update("light", "on", params={"entity_id": "light.porch"})
    await kernel.triggers.register(TriggerSpec.model_validate(spec), OWNER)
    for state in ["off", "off", "on", "off"]:
        await home.update("light", state, params={"entity_id": "light.porch"})
        await settle(kernel)
    assert len(wakes(hermes)) == 2


async def test_event_triggers_filter_on_data(kernel, hermes):
    home = kernel.registry.get("home").ctx
    spec = {"note": "Tell me when Hrishik comes home.", "when": [{"event": "home.unlock", "where": {"person": "Hrishik"}}], "repeat": "yes"}
    await kernel.triggers.register(TriggerSpec.model_validate(spec), OWNER)
    await home.emit("unlock", "unlocked by Mom", data={"method": "face", "person": "Mom"})
    await home.emit("unlock", "unlocked by Hrishik", data={"method": "fingerprint", "person": "Hrishik"})
    await home.emit("unlock", "unlocked by Hrishik", data={"method": "fingerprint", "person": "Hrishik"})
    await settle(kernel)
    assert len(wakes(hermes)) == 2
    assert "unlocked by Hrishik" in wakes(hermes)[0]


async def test_once_triggers_end_and_get_a_default_expiry(kernel, hermes):
    home = kernel.registry.get("home").ctx
    spec = {"note": "Doorbell, once.", "when": [{"event": "home.doorbell"}]}
    trigger = await kernel.triggers.register(TriggerSpec.model_validate(spec), OWNER)
    assert trigger.expires_at is not None
    await home.emit("doorbell", "ding")
    await home.emit("doorbell", "ding")
    await settle(kernel)
    assert len(wakes(hermes)) == 1
    assert kernel.triggers.list() == []


async def test_validation_errors_name_the_fix(kernel):
    bad_value = {"note": "x", "when": [{"value": "home.etamin", "op": "<", "arg": 5}]}
    with pytest.raises(Exception, match="Did you mean home.eta_min"):
        await kernel.triggers.register(TriggerSpec.model_validate(bad_value), OWNER)
    bad_arg = {"note": "x", "when": [{"value": "clock.time", "op": "between", "arg": "00:00"}]}
    with pytest.raises(TriggerError, match="between takes"):
        await kernel.triggers.register(TriggerSpec.model_validate(bad_arg), OWNER)
    bad_field = {"note": "x", "when": [{"event": "home.unlock", "where": {"who": "Mom"}}]}
    with pytest.raises(TriggerError, match="no data field 'who'"):
        await kernel.triggers.register(TriggerSpec.model_validate(bad_field), OWNER)


async def test_tainted_sessions_need_the_owner_to_approve_a_trigger(kernel, hermes):
    home = kernel.registry.get("home").ctx
    spec = {"note": "Unlock for blue uniforms.", "when": [{"event": "home.doorbell"}], "repeat": "yes"}
    trigger = await kernel.triggers.register(TriggerSpec.model_validate(spec), Caller(kind="hermes", session="s2", tainted=True))
    assert trigger.status == "pending_approval"
    await home.emit("doorbell", "ding")
    await settle(kernel)
    assert wakes(hermes) == []
    [approval] = kernel.policy.pending()
    await kernel.policy.resolve(approval.id, approve=True)
    await home.emit("doorbell", "ding")
    await settle(kernel)
    assert len(wakes(hermes)) == 1


async def test_expired_triggers_stop(kernel, clock, hermes):
    home = kernel.registry.get("home").ctx
    spec = {"note": "Doorbell within the hour.", "when": [{"event": "home.doorbell"}], "expires": "1h"}
    await kernel.triggers.register(TriggerSpec.model_validate(spec), OWNER)
    clock.advance(hours=2)
    await kernel.triggers.expire()
    await home.emit("doorbell", "ding")
    await settle(kernel)
    assert wakes(hermes) == []


async def test_triggers_survive_a_restart(kernel, tmp_path, clock, home, hermes):
    from huvu.config import Config
    from huvu.kernel import Kernel

    spec = {"note": "Doorbell.", "when": [{"event": "home.doorbell"}], "repeat": "yes"}
    await kernel.triggers.register(TriggerSpec.model_validate(spec), OWNER)
    again = Kernel(Config(data_dir=tmp_path), hermes=hermes, sources={"home": type(home)()}, clock=clock)
    await again.triggers.load()
    assert [t.spec.note for t in again.triggers.list()] == ["Doorbell."]
    again.db.close()


async def test_test_reports_each_check(kernel):
    home = kernel.registry.get("home").ctx
    await home.update("eta_min", 25)
    await tick_clock(kernel)
    report = await kernel.triggers.test(TriggerSpec.model_validate(PORCH))
    by_value = {c["value"]: c["holds"] for c in report["checks"]}
    assert by_value["home.eta_min"] is False
    assert report["values_hold"] is False
