from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from huvu.spec import Caller
from huvu.tools import ToolError
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


REMINDER = {
    "note": "Remind them to water the plants.",
    "when": [{"value": "clock.time", "op": "==", "arg": "08:00"}],
    "repeat": "daily",
    "script": 'def run(ctx):\n    ctx.call("huvu_notify", {"text": "Water the plants."})\n',
    "grants": ["huvu_notify"],
}


async def test_a_daily_reminder_is_a_clock_trigger_with_a_script(kernel, clock, hermes):
    await tick_clock(kernel)
    await kernel.triggers.register(TriggerSpec.model_validate(REMINDER), OWNER)
    await settle(kernel)
    # 23:50: not yet.
    assert kernel.notifications("me") == []

    clock.advance(minutes=8 * 60 + 10)
    await tick_clock(kernel)
    await settle(kernel)
    assert [n["text"] for n in kernel.notifications("me")] == ["Water the plants."]
    # The script did it all: the model was never woken.
    assert wakes(hermes) == []

    # Later that morning, nothing more.
    clock.advance(minutes=1)
    await tick_clock(kernel)
    await settle(kernel)
    assert len(kernel.notifications("me")) == 1

    # The next morning at 8, again.
    clock.advance(days=1, minutes=-1)
    await tick_clock(kernel)
    await settle(kernel)
    assert len(kernel.notifications("me")) == 2


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
    await settle(kernel)
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


def restarted(kernel, tmp_path, clock, hermes):
    """A second kernel on the same database: the backend after a restart."""
    from huvu.config import Config
    from huvu.kernel import Kernel

    from .fakes import HomeSource

    return Kernel(Config(data_dir=tmp_path), hermes=hermes, sources={"home": HomeSource()}, clock=clock)


async def test_readings_survive_a_restart(kernel, tmp_path, clock, hermes):
    await kernel.registry.get("home").ctx.update("battery", 70)
    await kernel.registry.get("home").ctx.update("light", "off", params={"entity_id": "light.porch"})
    clock.advance(hours=3)
    again = restarted(kernel, tmp_path, clock, hermes)
    readings = await again.state.get(["home.battery", "home.light[light.porch]"])
    assert readings["home.battery"].value == 70 and readings["home.light[light.porch]"].value == "off"
    assert clock.now - readings["home.battery"].at >= timedelta(hours=3)
    again.db.close()


async def test_a_restart_doesnt_fire_a_trigger_twice(kernel, tmp_path, clock, hermes):
    home = kernel.registry.get("home").ctx
    spec = {"note": "Light went off.", "when": [{"value": "home.light[light.porch]", "op": "==", "arg": "off"}], "repeat": "yes"}
    await home.update("light", "off", params={"entity_id": "light.porch"})
    await kernel.triggers.register(TriggerSpec.model_validate(spec), OWNER)
    await settle(kernel)
    assert len(wakes(hermes)) == 1
    again = restarted(kernel, tmp_path, clock, hermes)
    await again.triggers.load()
    await again.registry.get("home").ctx.update("light", "off", params={"entity_id": "light.porch"})
    await settle(again)
    assert len(wakes(hermes)) == 1
    await again.triggers.stop()
    again.db.close()


async def test_an_input_going_unknown_doesnt_rearm(kernel, clock, hermes):
    home = kernel.registry.get("home").ctx
    spec = {
        "note": "Close by with the light off.",
        "when": [{"value": "home.eta_min", "op": "<=", "arg": 10}, {"value": "home.light[light.porch]", "op": "==", "arg": "off"}],
        "repeat": "yes",
    }
    await home.update("eta_min", 8)
    await home.update("light", "off", params={"entity_id": "light.porch"})
    await kernel.triggers.register(TriggerSpec.model_validate(spec), OWNER)
    await settle(kernel)
    assert len(wakes(hermes)) == 1
    # 40 minutes on, eta is past its 30 minute max age: unknown. The light
    # reporting again makes the trigger look while eta is unknown.
    clock.advance(minutes=40)
    await home.update("light", "off", params={"entity_id": "light.porch"})
    await home.update("eta_min", 7)
    await settle(kernel)
    assert len(wakes(hermes)) == 1
    # Going false for real rearms it.
    await home.update("eta_min", 25)
    await home.update("eta_min", 6)
    await settle(kernel)
    assert len(wakes(hermes)) == 2


async def test_a_fire_while_the_trigger_runs_is_dropped(kernel, hermes):
    gate = asyncio.Event()

    async def slow(session_id: str) -> None:
        await gate.wait()

    hermes.script = [("run.started", {"run_id": "r"}), slow, ("run.completed", {})]
    home = kernel.registry.get("home").ctx
    await kernel.triggers.register(TriggerSpec.model_validate({"note": "Doorbell.", "when": [{"event": "home.doorbell"}], "repeat": "yes"}), OWNER)
    await home.emit("doorbell", "ding")
    await asyncio.sleep(0.01)
    # The first wake is still running: these are dropped.
    await home.emit("doorbell", "ding")
    await home.emit("doorbell", "ding")
    gate.set()
    await settle(kernel)
    assert len(wakes(hermes)) == 1
    # Once it's done, the next one fires.
    await home.emit("doorbell", "ding")
    await settle(kernel)
    assert len(wakes(hermes)) == 2


async def test_wakes_past_ten_are_told_and_past_twenty_pause_it(kernel, family, hermes, clock):

    mine, mine_task = await push_events(kernel, family.me_phone.id)
    home = kernel.registry.get("home").ctx
    trigger = await kernel.triggers.register(
        TriggerSpec.model_validate({"note": "Doorbell.", "when": [{"event": "home.doorbell"}], "repeat": "yes"}), OWNER
    )
    for _ in range(21):
        await home.emit("doorbell", "ding")
        await settle(kernel)
    sent = wakes(hermes)
    assert len(sent) == 20
    assert "woken you" not in sent[8] and "has woken you 10 times" in sent[9] and "triggers_pause" in sent[9]
    assert trigger.status == "paused" and trigger.paused_by == "limit"
    assert any(n == "notify" and "Paused a trigger" in d["text"] for n, d in mine)
    # Only its person can resume it, from the app.
    with pytest.raises(TriggerError, match="only its person"):
        await kernel.triggers.resume(trigger.id, OWNER)
    clock.advance(seconds=1)
    await kernel.triggers.resume(trigger.id, Caller(kind="frontend", user="me", device=family.me_phone.id))
    await home.emit("doorbell", "ding")
    await settle(kernel)
    assert len(wakes(hermes)) == 21
    mine_task.cancel()


async def test_the_model_can_pause_and_resume_its_users_triggers(kernel, family, hermes):
    home = kernel.registry.get("home").ctx
    created = await kernel.tools.call("triggers_register", {"note": "Doorbell.", "when": [{"event": "home.doorbell"}], "repeat": "yes"}, OWNER)
    paused = await kernel.tools.call("triggers_pause", {"id": created.data["id"], "reason": "too chatty"}, OWNER)
    assert paused.data["status"] == "paused" and paused.data["paused_by"] == "model"
    await home.emit("doorbell", "ding")
    await settle(kernel)
    assert wakes(hermes) == []
    with pytest.raises(ToolError, match="no live trigger"):
        await kernel.tools.call("triggers_pause", {"id": created.data["id"]}, Caller(kind="hermes", user="mom"))
    await kernel.tools.call("triggers_resume", {"id": created.data["id"]}, OWNER)
    await home.emit("doorbell", "ding")
    await settle(kernel)
    assert len(wakes(hermes)) == 1


async def test_busy_triggers_tell_their_user_once_an_hour(kernel, family, clock):

    mine, mine_task = await push_events(kernel, family.me_phone.id)
    home = kernel.registry.get("home").ctx
    await kernel.triggers.register(TriggerSpec.model_validate({"note": "Bell.", "when": [{"event": "home.doorbell"}], "repeat": "yes", "script": "def run(ctx):\n    pass\n"}), OWNER)
    for _ in range(14):
        await home.emit("doorbell", "ding")
        await settle(kernel)
    told = [d for n, d in mine if n == "notify" and "fired" in d["text"]]
    assert len(told) == 1 and "11 times" in told[0]["text"]
    mine_task.cancel()
