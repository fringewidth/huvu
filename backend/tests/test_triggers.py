from __future__ import annotations

import asyncio

import pytest

from huvu.spec import Caller
from huvu.tools import ToolError
from huvu.triggers import FIRE_LIMIT, TriggerError, TriggerSpec

from .fakes import WAKE

OWNER = Caller(kind="hermes", session="s1", user="me")

# Wakes run in their user's Hermes profile, so the users must exist.
pytestmark = pytest.mark.usefixtures("family")

# Between midnight and 05:00, with someone close and the porch light off.
PORCH = {
    "note": "Turn on the porch light.",
    "script": WAKE,
    "sql": "SELECT s.eta_min, l.state FROM home_status s, home_lights l "
    "WHERE l.entity_id = 'light.porch' AND l.state = 'off' AND s.eta_min <= 10 AND now_minute() < 300",
    "fire": "edge",
    "once": False,
}
DOORBELL = {"note": "Doorbell.", "script": WAKE, "sql": "SELECT id, note FROM home_doorbell", "fire": "rows", "once": False}


def spec(**fields) -> TriggerSpec:
    return TriggerSpec.model_validate(fields)


async def settle(kernel) -> None:
    await kernel.triggers.settle()


def wakes(hermes) -> list[str]:
    return [m for s, m in hermes.messages if s.startswith("wake-")]


async def test_an_edge_fires_when_the_answer_turns_up_and_rearms_when_it_goes(kernel, clock, home, hermes):
    home.set_status(eta_min=9)
    trigger = await kernel.triggers.register(spec(**PORCH), OWNER)
    await settle(kernel)
    # 23:50: outside the window, so nothing yet.
    assert wakes(hermes) == []

    clock.advance(minutes=10)
    await kernel.triggers.tick()
    await settle(kernel)
    assert len(wakes(hermes)) == 1
    assert "Turn on the porch light." in wakes(hermes)[0] and '"eta_min": 9' in wakes(hermes)[0]

    # Still true: no second fire.
    clock.advance(minutes=1)
    home.set_status(eta_min=2)
    await kernel.triggers.tick()
    await settle(kernel)
    assert len(wakes(hermes)) == 1 and trigger.fire_count == 1

    # The light goes on, then off again: it fires again, with no tick.
    home.set_light("light.porch", "on")
    await settle(kernel)
    home.set_light("light.porch", "off")
    await settle(kernel)
    assert len(wakes(hermes)) == 2


REMINDER = {
    "note": "Remind them to water the plants.",
    "sql": "SELECT 1 WHERE now_minute() = 480",
    "fire": "edge",
    "once": False,
    "script": 'def run(ctx):\n    ctx.call("huvu_notify", {"text": "Water the plants."})\n',
    "grants": ["huvu_notify"],
}


async def test_a_daily_reminder_is_an_edge_on_the_minute(kernel, clock, hermes):
    await kernel.triggers.register(spec(**REMINDER), OWNER)
    await settle(kernel)
    # 23:50: not yet.
    assert kernel.notifications("me") == []

    clock.advance(minutes=8 * 60 + 10)
    await kernel.triggers.tick()
    await settle(kernel)
    assert [n["text"] for n in kernel.notifications("me")] == ["Water the plants."]
    # The script did it all: the model was never woken.
    assert wakes(hermes) == []

    # Later that morning, nothing more, and it rearms.
    clock.advance(minutes=1)
    await kernel.triggers.tick()
    await settle(kernel)
    assert len(kernel.notifications("me")) == 1

    # The next morning at 8, again.
    clock.advance(days=1, minutes=-1)
    await kernel.triggers.tick()
    await settle(kernel)
    assert len(kernel.notifications("me")) == 2


async def test_rows_fire_at_once_for_each_new_row_and_never_for_old_ones(kernel, home, hermes):
    await home.press("before it existed")
    trigger = await kernel.triggers.register(spec(**DOORBELL), OWNER)
    await settle(kernel)
    assert wakes(hermes) == []
    # A recorded row runs the trigger at once: no poll.
    await home.press("a courier")
    await settle(kernel)
    assert len(wakes(hermes)) == 1 and "a courier" in wakes(hermes)[0]
    await home.press("a neighbour")
    await settle(kernel)
    assert len(wakes(hermes)) == 2 and "a neighbour" in wakes(hermes)[1] and "a courier" not in wakes(hermes)[1]
    assert trigger.fire_count == 2


async def test_rows_filter_in_sql_and_carry_their_labels(kernel, home, hermes):
    sql = "SELECT id, method FROM home_unlocks WHERE method = 'fingerprint'"
    await kernel.triggers.register(spec(note="Unlocked by finger.", script=WAKE, sql=sql, fire="rows", once=False), OWNER)
    await home.unlock("keypad")
    await settle(kernel)
    assert wakes(hermes) == []
    await home.unlock("fingerprint")
    await settle(kernel)
    assert len(wakes(hermes)) == 1 and "outside the family" not in wakes(hermes)[0]
    # The doorbell's rows are what a stranger shows the camera.
    await kernel.triggers.register(spec(**DOORBELL), OWNER)
    await home.press()
    await settle(kernel)
    assert "outside the family" in wakes(hermes)[-1]


async def test_a_change_reaches_only_the_triggers_that_read_that_table(kernel, home, hermes, monkeypatch):
    clock_only = await kernel.triggers.register(spec(note="Midnight.", script=WAKE, sql="SELECT 1 WHERE now_minute() = 0", fire="edge", once=False), OWNER)
    bell = await kernel.triggers.register(spec(**DOORBELL), OWNER)
    porch = await kernel.triggers.register(spec(note="Porch on.", script=WAKE, sql="SELECT 1 FROM home_lights WHERE entity_id = 'light.porch' AND state = 'on'", fire="edge", once=False), OWNER)
    assert bell.compiled.tables == {"home_doorbell"} and porch.compiled.tables == {"home_lights"}
    assert clock_only.compiled.tables == set() and clock_only.compiled.clock
    looked: list[str] = []
    look = kernel.triggers._look

    async def counted(trigger):
        looked.append(trigger.id)
        await look(trigger)

    monkeypatch.setattr(kernel.triggers, "_look", counted)
    # A doorbell row runs the doorbell trigger and no other.
    await home.press()
    await settle(kernel)
    assert looked == [bell.id] and bell.fire_count == 1
    # The light changing runs the porch trigger and no other.
    home.set_light("light.porch", "on")
    await settle(kernel)
    assert looked == [bell.id, porch.id] and porch.fire_count == 1 and clock_only.fire_count == 0


async def test_a_source_hands_over_only_what_its_spec_says(kernel, home):
    from huvu.source import SourceError

    with pytest.raises(SourceError, match="live table"):
        home.ctx.put("doorbell", {"note": "x"})
    with pytest.raises(SourceError, match="no key"):
        home.ctx.put("status", {"battery": 5})
    with pytest.raises(SourceError, match="no columns colour"):
        home.ctx.put("lights", {"entity_id": "light.porch", "colour": "red"})
    with pytest.raises(SourceError, match="two rows"):
        home.ctx.replace("lights", [{"entity_id": "light.a"}, {"entity_id": "light.a"}])
    with pytest.raises(SourceError, match="username"):
        home.ctx.put("lights", {"entity_id": "light.porch", "user_id": 7})


async def test_a_source_reads_only_its_own_history(kernel, home):
    from huvu.source import SourceError

    await home.press("mine")
    assert [r["note"] for r in home.ctx.rows("doorbell", "1 = 1")] == ["mine"]
    with pytest.raises(SourceError):
        home.ctx.rows("doorbell", "id IN (SELECT id FROM huvu_events)")
    with pytest.raises(SourceError):
        home.ctx.rows("doorbell", "1 = 1; DELETE FROM home_doorbell")
    assert kernel.data.count("home", "doorbell", "1 = 1", ()) == 1


async def test_once_triggers_end_and_get_a_default_expiry(kernel, home, hermes):
    trigger = await kernel.triggers.register(spec(**{**DOORBELL, "once": True}), OWNER)
    assert trigger.expires_at is not None
    await home.press()
    await settle(kernel)
    await home.press()
    await settle(kernel)
    assert len(wakes(hermes)) == 1 and trigger.status == "done"
    assert kernel.triggers.list(OWNER) == []


@pytest.mark.parametrize(
    "fields, error",
    [
        ({"sql": "SELECT * FROM home_bells"}, "no such table"),
        ({"sql": "SELECT note FROM home_doorbell", "fire": "rows"}, "id"),
        ({"sql": "SELECT 1"}, "reads nothing"),
        ({"sql": "SELECT 1 FROM home_doorbell; SELECT 2"}, "one statement"),
        ({"script": "print('no run')"}, "run"),
        ({"script": WAKE, "grants": ["huvu_notfy"]}, "huvu_notify"),
    ],
)
async def test_mistakes_fail_at_registration_and_name_the_fix(kernel, fields, error):
    # TriggerError, or ScriptError for the script.
    with pytest.raises(ValueError, match=error):
        await kernel.triggers.register(spec(**{**DOORBELL, "fire": "edge", **fields}), OWNER)


async def test_expired_triggers_stop(kernel, clock, home, hermes):
    await kernel.triggers.register(spec(**{**DOORBELL, "expires": "1h"}), OWNER)
    clock.advance(hours=2)
    await kernel.triggers.expire()
    await home.press()
    await settle(kernel)
    assert wakes(hermes) == []


async def test_a_dry_run_shows_the_answer_and_what_the_script_would_do(kernel, home, hermes):
    home.set_status(eta_min=25)
    await home.press("a courier")
    report = (await kernel.tools.call("triggers_register", {**DOORBELL, "dry_run": True}, OWNER)).data
    assert report["reads"] == ["home_doorbell"] and report["rows_now"] == 1 and report["would_fire_now"]
    assert report["first_rows"][0]["note"] == "a courier"
    assert report["script"]["dry_run"] and report["script"]["wakes"] == [{"text": "It fired.", "data": None}]
    assert "at once when home_doorbell changes" in report["runs"]
    # Nothing was registered and nothing woke.
    assert kernel.triggers.list(OWNER) == [] and wakes(hermes) == []


async def test_wakes_run_in_the_triggers_users_profile(kernel, home, hermes):
    await kernel.triggers.register(spec(**DOORBELL), OWNER)
    await home.press()
    await settle(kernel)
    [session] = [s for s in hermes.sessions if s.startswith("wake-")]
    assert hermes.profiles[session] == "me"
    assert kernel.sessions.owner(session).user == "me"
    assert kernel.sessions.owner(session).device is None


async def test_a_trigger_reads_only_what_its_user_may(kernel, home, hermes):
    sql = "SELECT id, person FROM home_unlocks"
    await kernel.triggers.register(spec(note="Unlocks.", script=WAKE, sql=sql, fire="rows", once=False), OWNER)
    # Mom's own row is hers: it never reaches my trigger.
    await home.unlock("fingerprint", "mom", user="mom")
    await settle(kernel)
    assert wakes(hermes) == []
    await home.unlock("keypad", "the house")
    await settle(kernel)
    assert len(wakes(hermes)) == 1


async def test_triggers_are_scoped_to_their_user(kernel):
    trigger = await kernel.triggers.register(spec(**DOORBELL), OWNER)
    assert kernel.triggers.list(Caller(kind="hermes", user="mom")) == []
    with pytest.raises(TriggerError):
        await kernel.triggers.remove(trigger.id, Caller(kind="hermes", user="mom"))
    await kernel.triggers.remove(trigger.id, Caller(kind="hermes", user="me"))


async def test_the_model_reads_its_triggers_as_a_table(kernel):
    await kernel.triggers.register(spec(**DOORBELL), OWNER)
    await kernel.triggers.register(spec(**DOORBELL), Caller(kind="hermes", user="mom"))
    result = await kernel.tools.call("data_sql", {"sql": "SELECT note, fire, status FROM huvu_triggers WHERE note = 'Doorbell.'"}, OWNER)
    assert result.data["rows"] == [["Doorbell.", "rows", "active"]]


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


async def test_a_trigger_on_a_shared_device_asks_everyone_and_the_first_yes_owns_it(kernel, family, home, hermes):
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
    await home.press()
    await settle(kernel)
    assert wakes(hermes) == []
    # Mom says yes first: she is at the TV now, and the trigger is hers.
    await kernel.attendance.answer(moms_asked["id"], True, family.mom_phone)
    await settle(kernel)
    [trigger] = kernel.triggers.list(Caller(kind="hermes", user="mom"))
    assert trigger.status == "active" and kernel.attendance.holder(family.tv.id) == "mom"
    await asyncio.sleep(0)
    assert mine[-1] == ("identify.done", {"id": mine_asked["id"], "status": "withdrawn"})
    # The press while it waited for its person doesn't fire it.
    assert wakes(hermes) == []
    await home.press()
    await settle(kernel)
    assert len(wakes(hermes)) == 1
    assert {hermes.profiles[s] for s in hermes.sessions if s.startswith("wake-")} == {"mom"}
    mine_task.cancel()
    moms_task.cancel()


async def test_nobody_saying_yes_drops_it(kernel, family):
    await kernel.triggers.register(spec(**DOORBELL), at_the_tv(family))
    questions = {u: kernel.attendance.pending_for(u) for u in ("me", "mom")}
    await kernel.attendance.answer(questions["me"][0]["id"], False, family.me_phone)
    assert kernel.triggers.list(at_the_tv(family)) != []
    await kernel.attendance.answer(questions["mom"][0]["id"], False, family.mom_phone)
    assert kernel.triggers.list(at_the_tv(family)) == []


async def test_unanswered_questions_drop_it(kernel, family, clock):
    await kernel.triggers.register(spec(**DOORBELL), at_the_tv(family))
    clock.advance(minutes=6)
    await kernel.attendance.expire()
    assert kernel.triggers.list(at_the_tv(family)) == []


async def test_a_trigger_needs_someone_to_ask(kernel, family):
    with pytest.raises(TriggerError, match="belongs to a person"):
        await kernel.triggers.register(spec(**DOORBELL), Caller(kind="hermes", group="home"))


def restarted(kernel, tmp_path, clock, hermes, home=None):
    """A second kernel on the same database: the backend after a restart."""
    from huvu.config import Config
    from huvu.kernel import Kernel

    from .fakes import HomeSource

    return Kernel(Config(data_dir=tmp_path), hermes=hermes, sources={"home": home or HomeSource()}, clock=clock)


async def test_a_restart_fires_neither_twice_nor_for_old_rows(kernel, tmp_path, clock, home, hermes):
    light_off = {"note": "Light went off.", "script": WAKE, "sql": "SELECT 1 FROM home_lights WHERE entity_id = 'light.porch' AND state = 'off'", "fire": "edge", "once": False}
    await kernel.triggers.register(spec(**light_off), OWNER)
    await kernel.triggers.register(spec(**DOORBELL), OWNER)
    await home.press()
    await settle(kernel)
    assert len(wakes(hermes)) == 2
    again = restarted(kernel, tmp_path, clock, hermes)
    await again.registry.get("home").run()
    await again.triggers.load()
    await again.triggers.tick()
    await settle(again)
    # The light is still off and the press was seen: nothing new.
    assert len(wakes(hermes)) == 2
    await again.registry.get("home").press()
    await settle(again)
    assert len(wakes(hermes)) == 3
    await again.triggers.stop()
    again.data.close()
    again.db.close()


async def test_a_table_its_source_cant_vouch_for_changes_nothing(kernel, home, hermes):
    trigger = await kernel.triggers.register(spec(**{**PORCH, "sql": "SELECT 1 FROM home_lights WHERE state = 'off'"}), OWNER)
    await settle(kernel)
    assert trigger.fire_count == 1 and not trigger.armed
    home.go_down()
    with pytest.raises(Exception, match="home_lights: the house is offline"):
        await kernel.data.sql("SELECT * FROM home_lights", kernel.accounts.scope("me", None))
    # Looked at while down: the query fails, it is logged once, and it doesn't rearm.
    for _ in range(3):
        await kernel.triggers._look(trigger)
    assert kernel.events_since("trigger.error", kernel.now().replace(year=2000)) == 1 and not trigger.armed
    # The house hands its lights over again, still off: no second fire.
    await home.run()
    await settle(kernel)
    assert trigger.fire_count == 1


async def test_rows_during_a_run_fire_once_the_run_ends(kernel, home, hermes):
    gate = asyncio.Event()

    async def slow(session_id: str) -> None:
        await gate.wait()

    hermes.script = [("run.started", {"run_id": "r"}), slow, ("run.completed", {})]
    trigger = await kernel.triggers.register(spec(**DOORBELL), OWNER)
    try:
        await home.press("one")
        async with asyncio.timeout(10):
            while not wakes(hermes):
                await asyncio.sleep(0.01)
        # The first wake is still running: these wait for it.
        await home.press("two")
        await home.press("three")
        await asyncio.sleep(0.05)
        assert len(wakes(hermes)) == 1
    finally:
        gate.set()
    await settle(kernel)
    assert len(wakes(hermes)) == 2 and "two" in wakes(hermes)[1] and "three" in wakes(hermes)[1]
    assert trigger.fire_count == 2


async def test_wakes_past_ten_are_told_and_past_twenty_pause_it(kernel, family, home, hermes, clock):
    mine, mine_task = await push_events(kernel, family.me_phone.id)
    trigger = await kernel.triggers.register(spec(**DOORBELL), OWNER)
    for _ in range(21):
        await home.press()
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
    await home.press()
    await settle(kernel)
    assert len(wakes(hermes)) == 21
    mine_task.cancel()


async def test_a_trigger_that_feeds_itself_pauses(kernel, family, home):
    # Every run logs a row this trigger reads, which would run it again.
    sql = "SELECT id FROM huvu_events WHERE type = 'trigger.run'"
    trigger = await kernel.triggers.register(spec(note="Echo.", sql=sql, fire="rows", once=False, script="def run(ctx):\n    pass\n"), OWNER)
    await home.press()
    await kernel.triggers.register(spec(**DOORBELL), OWNER)
    await home.press()
    await settle(kernel)
    assert trigger.status == "paused" and trigger.paused_by == "limit"
    assert trigger.fire_count == FIRE_LIMIT


async def test_the_model_can_pause_and_resume_its_users_triggers(kernel, family, home, hermes):
    created = await kernel.tools.call("triggers_register", DOORBELL, OWNER)
    paused = await kernel.tools.call("triggers_pause", {"id": created.data["id"], "reason": "too chatty"}, OWNER)
    assert paused.data["status"] == "paused" and paused.data["paused_by"] == "model"
    await home.press()
    await settle(kernel)
    assert wakes(hermes) == []
    with pytest.raises(ToolError, match="no live trigger"):
        await kernel.tools.call("triggers_pause", {"id": created.data["id"]}, Caller(kind="hermes", user="mom"))
    await kernel.tools.call("triggers_resume", {"id": created.data["id"]}, OWNER)
    # A press while it was paused doesn't fire it once it is back.
    await settle(kernel)
    assert wakes(hermes) == []
    await home.press()
    await settle(kernel)
    assert len(wakes(hermes)) == 1


async def test_busy_triggers_tell_their_user_once_an_hour(kernel, family, home):
    mine, mine_task = await push_events(kernel, family.me_phone.id)
    await kernel.triggers.register(spec(**{**DOORBELL, "script": "def run(ctx):\n    pass\n"}), OWNER)
    for _ in range(14):
        await home.press()
        await settle(kernel)
    told = [d for n, d in mine if n == "notify" and "fired" in d["text"]]
    assert len(told) == 1 and "11 times" in told[0]["text"]
    mine_task.cancel()


async def test_a_trigger_without_a_script_or_once_is_refused(kernel):
    with pytest.raises(ToolError, match="script"):
        await kernel.tools.call("triggers_register", {k: v for k, v in DOORBELL.items() if k != "script"}, OWNER)
    with pytest.raises(ToolError, match="once"):
        await kernel.tools.call("triggers_register", {k: v for k, v in DOORBELL.items() if k != "once"}, OWNER)
