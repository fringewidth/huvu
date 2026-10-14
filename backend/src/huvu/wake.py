"""Wakes: how a fired trigger reaches Hermes, and the conversation its
person can open while it runs or waits for them.

Every wake starts a fresh Hermes session, in the Hermes profile of the
trigger's user, with the trigger's note and the rows that fired it. A wake
is also a conversation of its own (the `wakes` table, and its turns in
`turns`): its first turn has no ask, and runs the trigger's note. Its
events go to every device of its person, never to a shared device, and the
menu lists it while it is open. A notification the wake sends its own
person opens it (`opens` is `wake:<id>`). A group's wake has no person: it
shows nowhere, and can't wait.

A wake ends when its run ends, unless the model called `wake_wait` in that
run: then it waits for its person, for up to WAIT. Whatever they send in it
is the wake's next turn, in the same Hermes session, on the same tier, and
that run ends or waits the same way. The person can end it from the app.

A waiting wake is compacted COMPACT_AFTER its last run, while the
provider's cache of it (CACHE_TTL) is still warm: huvu asks it, in its own
session, to write down where it stands. Hermes's API server has no
compaction of its own to call. The next turn then starts a fresh session
from those notes, so a reply after the cache went cold doesn't pay to
replay the whole run, its tool output and pages included.

A wake's first turn runs through Hermes's /v1/runs (HermesClient.run), with
the situation block as the history before it. A turn whose caller brings
the history is one Hermes takes as finished when it ends, so subagents the
wake starts run inline and their results come back within it. The situation
goes in the history since the plugin adds it only to a session's first
turn, and with a history this isn't one. A later turn brings no history,
and Hermes loads the session's own.

A trigger may wake the model at most WAKE_LIMIT times in WAKE_WINDOW (since
it was last resumed, if later). From the WAKE_WARN-th wake on, the wake
tells the model and asks it to deal with the repeats; one more past the
limit doesn't happen, and the trigger pauses itself and tells its user. The
count is read from the kernel's log, so a restart doesn't reset it. A
person's turn in a wake isn't a wake, and isn't counted.

A wake runs apart from the script that asked for it, so the script's own
time limit never cuts a long wake short; the trigger's run waits for it
after the script ends. Each run in a wake has a deadline of its own
(`[wakes] deadline`): past it, huvu stops the Hermes run, and for the
trigger's own run logs a trigger.error, which the next dream reads.

A wake on pro or advanced may use the browser (`Kernel.may_browse`): the
huvu Hermes plugin asks before every browser tool call.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from . import prompts
from .conversations import CACHE_TTL, Action, LiveTurn, _with_actions
from .db import from_iso, iso
from .hermes import TITLE_MAX, HermesError
from .times import for_model

if TYPE_CHECKING:
    from .accounts import Device
    from .kernel import Kernel
    from .spec import Caller

log = logging.getLogger("huvu.wake")

# Wakes running at once. More wait their turn. A person's turn in a wake
# takes no slot.
CONCURRENT = 2
WAKE_WINDOW = timedelta(hours=2)
WAKE_WARN = 10
WAKE_LIMIT = 20
SUMMARY_CHARS = 300
# How long a wake waits for its person after its last run.
WAIT = timedelta(days=1)
# When a waiting wake is compacted after its last run: before the
# provider's cache of it expires.
COMPACT_AFTER = CACHE_TTL - timedelta(minutes=5)


class WakeEnded(Exception):
    """An ask in a wake that has ended."""


class Waker:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        self._slots = asyncio.Semaphore(CONCURRENT)
        self._tasks: set[asyncio.Task[None]] = set()
        # Compactions running, by wake. An ask in the wake waits for its one.
        self._compacting: dict[str, asyncio.Task[None]] = {}

    @property
    def _db(self):
        return self._kernel.db

    def start(self, payload: dict[str, Any], user: str | None) -> asyncio.Task[None]:
        """Starts one wake and returns it at once, running."""
        task = asyncio.create_task(self._run(payload, user), name=f"wake:{payload.get('trigger')}")
        self._track(task)
        return task

    def _track(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run(self, payload: dict[str, Any], user: str | None) -> None:
        try:
            await self.wake(payload, user)
        except HermesError:
            # wake() has logged it already.
            pass
        except Exception:
            log.exception("wake for trigger %s failed", payload.get("trigger"))

    async def wake(self, payload: dict[str, Any], user: str | None) -> None:
        """Runs one wake's first turn for `user` to the end or to its
        deadline, or doesn't, past the trigger's wake limit."""
        trigger_id = payload.get("trigger")
        wakes = self._wakes_so_far(trigger_id)
        if wakes >= WAKE_LIMIT:
            await self._kernel.kernel_event(
                "wake.refused", f"trigger {trigger_id} hit its limit of {WAKE_LIMIT} wakes", entity=trigger_id, user=user
            )
            hours = int(WAKE_WINDOW.total_seconds() // 3600)
            await self._kernel.triggers.pause_for_limit(trigger_id, f"it woke the model {wakes} times in {hours} hours")
            return
        await self._kernel.kernel_event("wake.started", f"waking the model for trigger {trigger_id}", entity=trigger_id, user=user)
        async with self._slots:
            await self._wake({**payload, "wakes": wakes + 1}, user)

    def _wakes_so_far(self, trigger_id: str | None) -> int:
        trigger = self._kernel.triggers.live(trigger_id) if trigger_id else None
        if trigger is None:
            return 0
        since = self._kernel.now() - WAKE_WINDOW
        if trigger.resumed_at and trigger.resumed_at > since:
            since = trigger.resumed_at
        return self._kernel.events_since("wake.started", since, entity=trigger_id)

    async def _wake(self, payload: dict[str, Any], user: str | None) -> None:
        now = self._kernel.now()
        trigger = payload.get("trigger", "manual")
        group = payload.get("group")
        tier = payload.get("tier", "basic")
        profile = self._kernel.accounts.profile(user)
        session = await self._new_session(trigger, user, payload.get("note", ""))
        self._db.execute(
            "INSERT INTO wakes (id, trigger_id, user_id, group_id, note, tier, session, started_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running')",
            (session, trigger, user, group, payload.get("note", ""), tier, session, iso(now)),
        )
        self._announce(session)
        situation = self._kernel.situation(user, group)
        history = [{"role": "user", "content": f"{prompts.SITUATION_HEADER}\n{situation}"}]
        choice = self._kernel.config.model(tier)
        events = self._kernel.hermes.run(profile, session, history, render(payload, now), model=choice.model, provider=choice.provider)
        deadline = self._deadline(trigger)
        live = await self._kernel.conversations.wake_turn(
            session, session, profile, events, user=user, group=group, ask="", deadline=deadline, on_end=self._ran
        )
        if live.task is not None:
            await asyncio.shield(live.task)
        reply = live.turn.answer
        if live.error is not None and not live.timed_out:
            await self._kernel.kernel_event("trigger.error", f"wake for {trigger} failed: {live.error}", entity=trigger, user=user)
            raise HermesError(live.error)
        kind, ending = ("trigger.error", f"was stopped at its deadline of {deadline:g} s") if live.timed_out else ("wake.done", "ended")
        await self._kernel.kernel_event(
            kind,
            f"wake for {trigger} {ending}: {reply[:SUMMARY_CHARS] or '(no reply)'}",
            entity=trigger,
            data={"session": session},
            user=user,
        )

    async def _new_session(self, trigger: str, user: str | None, note: str) -> str:
        session = f"wake-{trigger}-{self._kernel.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        # The session id goes first: it names the trigger, and keeps the title
        # unique when the note is cut.
        title = f"huvu wake {session}: {note}"[:TITLE_MAX]
        await self._kernel.hermes.create_session(self._kernel.accounts.profile(user), session, title=title)
        self._kernel.sessions.record(session, "wake", user=user, trigger_id=trigger)
        return session

    def _deadline(self, trigger: str | None) -> float:
        config = self._kernel.config
        return config.dream_deadline if self._kernel.dreams.is_dream(trigger) else config.wake_deadline

    def _ran(self, live: LiveTurn) -> None:
        """A run in the wake ended: it waits if the model asked to, and ends
        otherwise. One its person ended meanwhile stays ended."""
        row = self._row(live.wake)
        if row is None or row["status"] == "ended":
            return
        status = "waiting" if row["waits_for"] is not None and row["user_id"] is not None else "ended"
        self._db.execute("UPDATE wakes SET status = ?, ran_at = ? WHERE id = ?", (status, iso(self._kernel.now()), live.wake))
        self._announce(live.wake)

    async def ask(
        self,
        wake_id: str,
        text: str,
        device: Device,
        *,
        actions: list[Action] | None = None,
        turn_id: str | None = None,
        mode: str = "text",
    ) -> LiveTurn:
        """The person's next message in their wake, as its next turn."""
        row = self._mine(wake_id, device)
        if self._status(row) == "ended":
            raise WakeEnded(f"wake {wake_id} has ended")
        compacting = self._compacting.get(wake_id)
        if compacting is not None:
            await asyncio.shield(compacting)
            row = self._mine(wake_id, device)
        user = row["user_id"]
        profile = self._kernel.accounts.profile(user)
        message = _with_actions(f"{prompts.mode_note(mode)}\n\n{text}", actions or [], self._kernel.now().tzinfo)
        session = row["session"]
        history: list[dict[str, str]] = []
        if row["carried"] is not None:
            # Compacted: a fresh session, from the wake's own notes.
            session = await self._new_session(row["trigger_id"] or "manual", user, row["note"])
            situation = self._kernel.situation(user, row["group_id"])
            history = [{"role": "user", "content": f"{prompts.SITUATION_HEADER}\n{situation}"}]
            message = prompts.wake_carried(row["carried"], message)
        self._db.execute(
            "UPDATE wakes SET status = 'running', waits_for = NULL, session = ?, carried = NULL, compacted_at = NULL WHERE id = ?", (session, wake_id)
        )
        self._announce(wake_id)
        choice = self._kernel.config.model(row["tier"])
        events = self._kernel.hermes.run(profile, session, history, message, model=choice.model, provider=choice.provider)
        return await self._kernel.conversations.wake_turn(
            wake_id,
            session,
            profile,
            events,
            user=user,
            group=row["group_id"],
            ask=text,
            deadline=self._deadline(row["trigger_id"]),
            on_end=self._ran,
            actions=actions,
            turn_id=turn_id,
        )

    async def close(self, wake_id: str, device: Device) -> None:
        """Its person ends the wake, and a run still going in it stops."""
        self._mine(wake_id, device)
        self._db.execute("UPDATE wakes SET status = 'ended' WHERE id = ?", (wake_id,))
        for turn in self._kernel.conversations.turns(wake_id):
            if turn.running:
                await self._kernel.conversations.stop(turn.id, wait=True)
        self._announce(wake_id)

    def wait(self, session: str | None, waits_for: str) -> datetime:
        """`wake_wait`: the wake running in `session` stays open after this
        run, for its person. Returns until when, at the latest."""
        row = self._db.execute("SELECT * FROM wakes WHERE session = ? AND status = 'running'", (session,)).fetchone() if session else None
        if row is None:
            raise WakeEnded("only a wake can wait, and this session isn't one: your person is reading this conversation already")
        if row["user_id"] is None:
            raise WakeEnded("a group's wake can't wait: it has no person to open it; reach the group with huvu_notify")
        self._db.execute("UPDATE wakes SET waits_for = ? WHERE id = ?", (waits_for.strip() or "their reply", row["id"]))
        self._announce(row["id"])
        return self._kernel.now() + WAIT

    async def sweep(self) -> None:
        """Once a minute: ends wakes that waited too long, and compacts the
        ones whose cache is about to go cold."""
        now = self._kernel.now()
        for row in self._db.execute("SELECT * FROM wakes WHERE status = 'waiting'").fetchall():
            ran = from_iso(row["ran_at"])
            if self._status(row) == "ended":
                self._db.execute("UPDATE wakes SET status = 'ended' WHERE id = ?", (row["id"],))
                self._announce(row["id"])
            elif row["compacted_at"] is None and ran is not None and now - ran >= COMPACT_AFTER and row["id"] not in self._compacting:
                task = asyncio.create_task(self._compact(row["id"]), name=f"wake:compact:{row['id']}")
                self._compacting[row["id"]] = task
                task.add_done_callback(lambda _, wake=row["id"]: self._compacting.pop(wake, None))
                self._track(task)

    async def _compact(self, wake_id: str) -> None:
        """Asks the wake, in its warm session, to write down where it
        stands. A failed try isn't repeated: the next turn then continues
        the old session, cold."""
        row = self._row(wake_id)
        if row is None:
            return
        self._db.execute("UPDATE wakes SET compacted_at = ? WHERE id = ?", (iso(self._kernel.now()), wake_id))
        profile = self._kernel.accounts.profile(row["user_id"])
        choice = self._kernel.config.model(row["tier"])
        notes = ""
        run_id: str | None = None
        try:
            async with asyncio.timeout(self._deadline(row["trigger_id"])):
                async for event in self._kernel.hermes.run(profile, row["session"], [], prompts.WAKE_COMPACT, model=choice.model, provider=choice.provider):
                    if event.name == "run.started":
                        run_id = event.data.get("run_id")
                    elif event.name == "assistant.delta":
                        notes += event.data.get("delta", "")
                    elif event.name == "assistant.completed" and not notes:
                        notes = event.data.get("content", "")
                    elif event.name == "run.failed":
                        raise HermesError(str(event.data.get("error") or "the run failed"))
        except TimeoutError:
            if run_id:
                await self._kernel.hermes.stop(profile, run_id)
            notes = ""
        except HermesError as err:
            log.warning("compacting wake %s failed: %s", wake_id, err)
            notes = ""
        self._kernel.sessions.touch(row["session"])
        if notes.strip():
            self._db.execute("UPDATE wakes SET carried = ? WHERE id = ? AND status = 'waiting'", (notes.strip(), wake_id))
        await self._kernel.kernel_event(
            "wake.compacted" if notes.strip() else "wake.error",
            f"wake {wake_id} {'compacted' if notes.strip() else 'could not be compacted'}",
            entity=row["trigger_id"],
            user=row["user_id"],
        )

    def open_for(self, user: str | None) -> list[dict[str, Any]]:
        """A person's open wakes, newest first: running, or waiting for
        them."""
        if user is None:
            return []
        rows = self._db.execute(
            "SELECT * FROM wakes WHERE user_id = ? AND status IN ('running', 'waiting') ORDER BY started_at DESC", (user,)
        ).fetchall()
        return [self._public(r) for r in rows if self._status(r) != "ended"]

    def view(self, wake_id: str, device: Device) -> dict[str, Any]:
        """One of the device user's wakes, open or ended, with its turns."""
        row = self._mine(wake_id, device)
        return {"wake": self._public(row), "turns": [t.public() for t in self._kernel.conversations.turns(wake_id)]}

    def owner(self, wake_id: str) -> str | None:
        """The person a wake is for, or None when it isn't a wake or has none."""
        row = self._row(wake_id)
        return row["user_id"] if row else None

    def opens_for(self, caller: Caller) -> str | None:
        """What a notification from `caller` opens: the wake it runs in,
        when it is for the caller's own person."""
        row = self._db.execute("SELECT id, user_id FROM wakes WHERE session = ?", (caller.session,)).fetchone() if caller.session else None
        return f"wake:{row['id']}" if row and row["user_id"] is not None and row["user_id"] == caller.user else None

    def tier(self, session: str) -> str | None:
        """The tier of the wake running in `session`, or None when it isn't one."""
        row = self._db.execute("SELECT tier FROM wakes WHERE session = ?", (session,)).fetchone()
        return row["tier"] if row else None

    def _row(self, wake_id: str | None):
        return self._db.execute("SELECT * FROM wakes WHERE id = ?", (wake_id,)).fetchone() if wake_id else None

    def _mine(self, wake_id: str, device: Device):
        row = self._row(wake_id)
        if row is None or device.user is None or row["user_id"] != device.user:
            raise KeyError(f"no wake {wake_id!r}")
        return row

    def _status(self, row) -> str:
        """Its status now: a wait past WAIT has ended, swept or not."""
        ran = from_iso(row["ran_at"])
        expired = row["status"] == "waiting" and ran is not None and self._kernel.now() - ran >= WAIT
        return "ended" if expired else row["status"]

    def _public(self, row) -> dict[str, Any]:
        ran = from_iso(row["ran_at"])
        status = self._status(row)
        return {
            "id": row["id"],
            "trigger": row["trigger_id"],
            "note": row["note"],
            "tier": row["tier"],
            "status": status,
            "waits_for": row["waits_for"] if status == "waiting" else None,
            "started_at": from_iso(row["started_at"]),
            "ran_at": ran,
            "until": ran + WAIT if status == "waiting" and ran is not None else None,
        }

    def _announce(self, wake_id: str) -> None:
        """Tells its person's devices that the wake changed."""
        row = self._row(wake_id)
        if row is None or row["user_id"] is None:
            return
        user = row["user_id"]
        devices = [d.id for d in self._kernel.accounts.devices_for(user) if d.user == user]
        self._kernel.push.send("wake", self._public(row), devices)


def render(payload: dict[str, Any], now) -> str:
    """The wake as the model reads it: readable times, labels called out."""
    hours = int(WAKE_WINDOW.total_seconds() // 3600)
    return prompts.wake(for_model(payload, now), "external" in payload.get("labels", []), payload.get("wakes", 0), WAKE_WARN, WAKE_LIMIT, hours)
