"""The kernel: every part of the backend, wired together.

Sources (the drivers) plug in through the registry. Everything else here is
the orchestration around them: state, the event log, triggers and their
scripts, the action policy, the tool box Hermes and scripts call, the
Hermes client with its sessions and wakes, and the link to the frontend.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .accounts import Accounts
from .attendance import Attendance
from .builtin.clock import ClockSource
from .builtin.huvu import HuvuSource
from .config import Config
from .conversations import ConversationHub
from .db import connect
from .events import EventLog
from .hermes import HermesClient
from .media import MediaStore
from .policy import Policy
from .push import PushHub
from .registry import Registry
from .scripts import ScriptRunner
from .sessions import SessionBook
from .source import Source, SourceError
from .spec import Event, Feed, Label
from .state import StateService
from .tools import ToolBox
from .triggers import TriggerEngine
from .wake import Waker

log = logging.getLogger("huvu.kernel")

TICK = 60.0


class Kernel:
    def __init__(
        self,
        config: Config,
        *,
        hermes: HermesClient | None = None,
        sources: dict[str, Source] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`sources` adds drivers by instance, next to the ones the config
        names; `clock` replaces the wall clock. Both are for tests and for
        embedding the kernel."""
        self.config = config
        self._clock = clock
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = connect(config.data_dir / "huvu.db")
        self.accounts = Accounts(self.db, self.now, household_key=config.hermes_api_key, household_mcp_token=config.mcp_token)
        self.events = EventLog(self.db)
        self.registry = Registry(self)
        self.state = StateService(self)
        self.policy = Policy(self, config.policy)
        self.tools = ToolBox(self)
        self.triggers = TriggerEngine(self)
        self.scripts = ScriptRunner(self)
        self.hermes = hermes or HermesClient(config.hermes_url)
        self.sessions = SessionBook(self.db, self.now)
        self.conversations = ConversationHub(self)
        self.waker = Waker(self)
        self.attendance = Attendance(self)
        self.push = PushHub()
        self.media = MediaStore(self)
        self.huvu = HuvuSource(self)
        self._ticker: asyncio.Task[None] | None = None

        self.registry.add("clock", ClockSource())
        self.registry.add("huvu", self.huvu)
        for source in config.sources:
            self.registry.load(source.name, source.driver, source.options)
        for name, source in (sources or {}).items():
            self.registry.add(name, source)
        self.tools.build()

    def now(self) -> datetime:
        return self._clock() if self._clock else datetime.now().astimezone()

    async def start(self) -> None:
        # Turns that were running when the kernel last stopped never ended.
        self.db.execute("UPDATE turns SET running = 0, ending = 'failed' WHERE running = 1")
        await self.registry.start()
        await self.triggers.load()
        self._ticker = asyncio.create_task(self._tick(), name="kernel:tick")
        await self.kernel_event("started", f"kernel started with sources {', '.join(self.registry.names)}")

    async def stop(self) -> None:
        if self._ticker:
            self._ticker.cancel()
        await self.conversations.close()
        await self.triggers.stop()
        await self.registry.stop()
        await self.hermes.close()
        self.db.close()

    async def _tick(self) -> None:
        """Expires triggers, approvals and attendance once a minute."""
        while True:
            await asyncio.sleep(TICK)
            try:
                await self.triggers.expire()
                await self.policy.expire()
                await self.attendance.expire()
            except Exception:
                log.exception("tick failed")

    async def emit(self, event: Event) -> Event:
        """Logs a source's event, then lets triggers see it. The type must be
        one the source declared."""
        source, _, type_ = event.type.partition(".")
        if source != event.source or self.registry.spec(source).event(type_) is None:
            raise SourceError(f"{event.source} emitted {event.type}, which its spec doesn't declare")
        stored = self.events.append(event)
        await self.triggers.on_event(stored)
        return stored

    async def kernel_event(
        self,
        type: str,
        summary: str,
        *,
        entity: str | None = None,
        refs: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
        labels: list[Label] | None = None,
        user: str | None = None,
        group: str | None = None,
        device: str | None = None,
    ) -> Event:
        """Logs one of the kernel's own events: trigger runs and errors,
        actions, approvals, wakes, chat turns. Triggers can't watch these.
        `user` makes it that user's alone, and `group` its members' (see
        events.py)."""
        return self.events.append(
            Event(
                at=self.now(),
                source="kernel",
                type=type,
                entity=entity,
                summary=summary,
                refs=refs or {},
                data=data or {},
                labels=labels or ["house"],
                user=user,
                group=group,
                device=device,
            )
        )

    async def notify(self, user: str | None, group: str | None, text: str, *, image: str | None = None, urgency: str = "normal") -> None:
        """A notification on a user's phones, or on a group's members'
        phones when there is no user."""
        payload = {"text": text, "image": image, "urgency": urgency, "user": user, "group": None if user else group}
        self.push.send("notify", payload, self.accounts.phones(user, group))

    async def feeds(self) -> list[Feed]:
        feeds: list[Feed] = []
        for name in self.registry.names:
            try:
                feeds.extend(await self.registry.get(name).feeds())
            except Exception:
                log.exception("feeds from %s failed", name)
        return feeds
