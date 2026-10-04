from __future__ import annotations

import asyncio

import pytest

from huvu.spec import Caller
from huvu.triggers import TriggerError, TriggerSpec, compare

OWNER = Caller(kind="hermes", session="s1", user="me")
HOUSEHOLD = Caller(kind="hermes", session="s-tv")

# Wakes run in their user's Hermes profile, so the users must exist.
pytestmark = pytest.mark.usefixtures("family")

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
    assert kernel.triggers.list(Caller(kind="hermes", user="me")) == []


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
    assert [t.spec.note for t in again.triggers.list(Caller(kind="hermes", user="me"))] == ["Doorbell."]
    again.db.close()


async def test_test_reports_each_check(kernel):
    home = kernel.registry.get("home").ctx
    await home.update("eta_min", 25)
    await tick_clock(kernel)
    report = await kernel.triggers.test(TriggerSpec.model_validate(PORCH), "me")
    by_value = {c["value"]: c["holds"] for c in report["checks"]}
    assert by_value["home.eta_min"] is False
    assert report["values_hold"] is False


async def test_wakes_run_in_the_triggers_users_profile(kernel, hermes):
    home = kernel.registry.get("home").ctx
    spec = {"note": "Doorbell.", "when": [{"event": "home.doorbell"}], "repeat": "yes"}
    await kernel.triggers.register(TriggerSpec.model_validate(spec), OWNER)
    await home.emit("doorbell", "ding")
    await settle(kernel)
    [session] = [s for s in hermes.sessions if s.startswith("wake-")]
    assert hermes.profiles[session] == "me"
    assert kernel.sessions.owner(session).user == "me"
    assert kernel.sessions.owner(session).device is None


async def test_triggers_are_scoped_to_their_user(kernel):
    spec = {"note": "Doorbell.", "when": [{"event": "home.doorbell"}], "repeat": "yes"}
    trigger = await kernel.triggers.register(TriggerSpec.model_validate(spec), OWNER)
    assert kernel.triggers.list(Caller(kind="hermes", user="mom")) == []
    with pytest.raises(TriggerError):
        await kernel.triggers.remove(trigger.id, Caller(kind="hermes", user="mom"))
    await kernel.triggers.remove(trigger.id, Caller(kind="hermes", user="me"))


async def push_events(kernel, device: str) -> tuple[list[tuple[str, dict]], asyncio.Task]:
    """Opens a push stream for a device, and returns what it has received
    so far each time it is awaited again."""
    received: list[tuple[str, dict]] = []

    async def listen() -> None:
        async for item in kernel.push.subscribe(device):
            received.append(item)

    task = asyncio.create_task(listen())
    await asyncio.sleep(0)
    return received, task


def at_the_tv(family) -> Caller:
    return Caller(kind="hermes", session="s-tv", device=family.tv.id, group="home")


DOORBELL = {"note": "Doorbell.", "when": [{"event": "home.doorbell"}], "repeat": "yes"}


async def test_a_trigger_on_a_shared_device_asks_everyone_and_the_first_yes_owns_it(kernel, family, hermes):
    mine, mine_task = await push_events(kernel, family.me_phone.id)
    moms, moms_task = await push_events(kernel, family.mom_phone.id)
    result = await kernel.tools.call("triggers_register", DOORBELL, at_the_tv(family))
    assert result.data["status"] == "unconfirmed" and result.data["user"] is None
    assert "tap yes" in result.data["note_for_you"]
    await asyncio.sleep(0)
    [(_, mine_asked)] = mine
    [(_, moms_asked)] = moms
    assert mine_asked["reason"] == "setting up: Doorbell." and moms_asked["asked_by"] == "trigger"
    # Waiting, it doesn't fire.
    await kernel.registry.get("home").ctx.emit("doorbell", "ding")
    await settle(kernel)
    assert wakes(hermes) == []
    # Mom says yes first: she is at the TV now, and the trigger is hers.
    await kernel.attendance.answer(moms_asked["id"], True, family.mom_phone)
    [trigger] = kernel.triggers.list(Caller(kind="hermes", user="mom"))
    assert trigger.status == "active" and kernel.attendance.holder(family.tv.id) == "mom"
    await asyncio.sleep(0)
    assert mine[-1] == ("identify.done", {"id": mine_asked["id"], "status": "withdrawn"})
    await kernel.registry.get("home").ctx.emit("doorbell", "ding")
    await settle(kernel)
    [session] = [s for s in hermes.sessions if s.startswith("wake-")]
    assert hermes.profiles[session] == "mom"
    mine_task.cancel()
    moms_task.cancel()


async def test_nobody_saying_yes_drops_it(kernel, family):
    await kernel.triggers.register(TriggerSpec.model_validate(DOORBELL), at_the_tv(family))
    questions = {u: kernel.attendance.pending_for(u) for u in ("me", "mom")}
    await kernel.attendance.answer(questions["me"][0]["id"], False, family.me_phone)
    assert kernel.triggers.list(at_the_tv(family)) != []
    await kernel.attendance.answer(questions["mom"][0]["id"], False, family.mom_phone)
    assert kernel.triggers.list(at_the_tv(family)) == []


async def test_unanswered_questions_drop_it(kernel, family, clock):
    await kernel.triggers.register(TriggerSpec.model_validate(DOORBELL), at_the_tv(family))
    clock.advance(minutes=6)
    await kernel.attendance.expire()
    assert kernel.triggers.list(at_the_tv(family)) == []


async def test_a_trigger_needs_someone_to_ask(kernel, family):
    with pytest.raises(TriggerError, match="belongs to a person"):
        await kernel.triggers.register(TriggerSpec.model_validate(DOORBELL), Caller(kind="hermes", group="home"))
