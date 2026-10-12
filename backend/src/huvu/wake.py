"""Wakes: how a fired trigger reaches Hermes.

Every wake starts a fresh Hermes session, in the Hermes profile of the
trigger's user, with the trigger's note and the rows that fired it. The
session's reply text goes to the kernel's log and nowhere else; if the agent wants to reach the user, it calls `huvu_notify`
or another action.

A trigger may wake the model at most WAKE_LIMIT times in WAKE_WINDOW (since
it was last resumed, if later). From the WAKE_WARN-th wake on, the wake
tells the model and asks it to deal with the repeats; one more past the
limit doesn't happen, and the trigger pauses itself and tells its user. The
count is read from the kernel's log, so a restart doesn't reset it.

A wake runs apart from the script that asked for it, so the script's own
time limit never cuts a long wake short; the trigger's run waits for it
after the script ends. Each wake has a
deadline of its own (`[wakes] deadline`): past it, huvu stops the Hermes run
and logs a trigger.error, which the next dream reads.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from . import prompts
from .hermes import TITLE_MAX, HermesError
from .times import for_model

if TYPE_CHECKING:
    from .kernel import Kernel

log = logging.getLogger("huvu.wake")

# Wakes running at once. More wait their turn.
CONCURRENT = 2
WAKE_WINDOW = timedelta(hours=2)
WAKE_WARN = 10
WAKE_LIMIT = 20
SUMMARY_CHARS = 300


class Waker:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        self._slots = asyncio.Semaphore(CONCURRENT)
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, payload: dict[str, Any], user: str | None) -> asyncio.Task[None]:
        """Starts one wake and returns it at once, running."""
        task = asyncio.create_task(self._run(payload, user), name=f"wake:{payload.get('trigger')}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

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
        """Runs one wake session for `user` to the end or to its deadline, or
        doesn't, past the trigger's wake limit."""
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
        profile = self._kernel.accounts.profile(user)
        session = f"wake-{trigger}-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        # The session id goes first: it names the trigger, and keeps the title
        # unique when the note is cut.
        title = f"huvu wake {session}: {payload.get('note', '')}"[:TITLE_MAX]
        await self._kernel.hermes.create_session(profile, session, title=title)
        self._kernel.sessions.record(session, "wake", user=user, trigger_id=trigger)
        reply = ""
        run_id: str | None = None
        deadline = self._kernel.config.wake_deadline
        stopped = False
        try:
            live = self._kernel.triggers.get(trigger)
            choice = self._kernel.config.model(live.spec.tier if live else "basic")
            async with asyncio.timeout(deadline):
                async for event in self._kernel.hermes.chat(profile, session, render(payload, now), model=choice.model, provider=choice.provider):
                    if event.name == "run.started":
                        run_id = event.data.get("run_id")
                    elif event.name == "assistant.delta":
                        reply += event.data.get("delta", "")
                    elif event.name == "assistant.completed" and not reply:
                        reply = event.data.get("content", "")
        except TimeoutError:
            stopped = True
            # Closing the stream may not end the run, so it is stopped by name.
            if run_id:
                await self._kernel.hermes.stop(profile, run_id)
        except HermesError as err:
            await self._kernel.kernel_event("trigger.error", f"wake for {trigger} failed: {err}", entity=trigger, user=user)
            raise
        self._kernel.sessions.touch(session)
        kind, ending = ("trigger.error", f"was stopped at its deadline of {deadline:g} s") if stopped else ("wake.done", "ended")
        await self._kernel.kernel_event(
            kind,
            f"wake for {trigger} {ending}: {reply[:SUMMARY_CHARS] or '(no reply)'}",
            entity=trigger,
            data={"session": session},
            user=user,
        )

def render(payload: dict[str, Any], now) -> str:
    """The wake as the model reads it: readable times, labels called out."""
    hours = int(WAKE_WINDOW.total_seconds() // 3600)
    return prompts.wake(for_model(payload, now), "external" in payload.get("labels", []), payload.get("wakes", 0), WAKE_WARN, WAKE_LIMIT, hours)
