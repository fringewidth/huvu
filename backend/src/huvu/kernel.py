"""The kernel: every part of the backend, wired together.

Sources (the drivers) plug in through the registry. Everything else here is
the orchestration around them: the tables, triggers and their scripts, the
action policy, the tool box Hermes and scripts call, the Hermes client
with its sessions and wakes, and the link to the frontend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import prompts, skills, wiki
from .accounts import Accounts
from .attendance import Attendance
from .builtin.chat import ChatSource
from .builtin.huvu import HuvuSource
from .config import Config
from .conversations import ConversationHub
from .data import DataStore
from .db import connect, from_iso, iso
from .dream import Dreams
from .hearing import Hearing
from .hermes import HermesClient
from .routing import Router
from .media import MediaStore
from .native_push import FcmClient, NativePush
from .web_push import WebPushClient, load_key
from .policy import Policy
from .push import PushHub
from .registry import Registry
from .scripts import ScriptRunner
from .sessions import SessionBook
from .source import Source
from .spec import Feed, Label
from .speech import Voice
from .tools import ToolBox
from .triggers import TriggerEngine
from .wake import Waker

log = logging.getLogger("huvu.kernel")

TICK = 60.0
# Hermes sessions huvu started are deleted after this long unused. The
# history and the turns huvu keeps for the stage stay.
SESSION_RETENTION = timedelta(days=30)
# How often the sweep runs.
SWEEP_EVERY = timedelta(days=1)
# How long a notification stays listed for a phone that missed it.
NOTIFY_KEEP = timedelta(days=3)


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
        # The house's timezone: every local time huvu computes is in it,
        # never the server's (UTC in the container).
        self.tz = ZoneInfo(config.timezone)
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = connect(config.data_dir / "huvu.db")
        self.accounts = Accounts(self.db, self.now, household_key=config.hermes_api_key, household_mcp_token=config.mcp_token)
        self.data = DataStore(self)
        self.registry = Registry(self)
        self.policy = Policy(self, config.policy)
        self.tools = ToolBox(self)
        self.triggers = TriggerEngine(self)
        self.scripts = ScriptRunner(self)
        self.hermes = hermes or HermesClient(config.hermes_url)
        self.sessions = SessionBook(self.db, self.now)
        self.conversations = ConversationHub(self)
        self.waker = Waker(self)
        self.attendance = Attendance(self)
        fcm = FcmClient(config.fcm_credentials) if config.fcm_credentials else None
        web_push = WebPushClient(load_key(config.data_dir / "vapid.pem"), config.public_url)
        self.native_push = NativePush(self, fcm, web_push)
        self.push = PushHub(self.native_push.forward)
        self.media = MediaStore(self)
        self.voice = Voice(config)
        self.hearing = Hearing(self)
        self.router = Router(self)
        self.dreams = Dreams(self)
        self.huvu = HuvuSource(self)
        self._ticker: asyncio.Task[None] | None = None
        self._warming: asyncio.Task[None] | None = None

        self.registry.add("huvu", self.huvu)
        self.registry.add("chat", ChatSource())
        for source in config.sources:
            self.registry.load(source.name, source.driver, source.options)
        for name, source in (sources or {}).items():
            self.registry.add(name, source)
        for spec in self.registry.specs():
            self.data.ensure(spec)
        self.tools.build()
        self.data.on_record = self.triggers.nudge
        self.huvu.publish()

    def now(self) -> datetime:
        return self._clock().astimezone(self.tz) if self._clock else datetime.now(self.tz)

    def situation(self, user: str | None, group: str | None) -> str:
        """The situation block for a session acting for a user, or for a
        group's shared devices: the time, who huvu helps, the family mode,
        what waits on them, and where their profile's wiki is."""
        helping = shared = None
        if user:
            named = self.accounts.user(user)
            helping = named.name if named else user
        else:
            group_named = self.accounts.group(group)
            people = ", ".join(f"{u.name} ({u.id})" for u in self.accounts.people(group or self.accounts.default_group()))
            shared = (group_named.name if group_named else group, people)
        pending = self.policy.pending_for(self.accounts.scope(user, group))
        folder = wiki.folder(self.config.hermes_home, self.accounts.profile(user, group))
        return prompts.situation(f"{self.now():%a %Y-%m-%d %H:%M}", helping, shared, self.huvu.mode(), len(pending), str(folder))

    async def start(self) -> None:
        # Turns that were running when the kernel last stopped never ended.
        self.db.execute("UPDATE turns SET running = 0, ending = 'failed' WHERE running = 1")
        await self.registry.start()
        self.write_skills()
        await self.triggers.start()
        await self.dreams.ensure()
        if self.native_push.sender is None:
            log.info("native push is off: HUVU_FCM_CREDENTIALS is not set")
        self._ticker = asyncio.create_task(self._tick(), name="kernel:tick")
        self._warming = asyncio.create_task(self.router.warm(), name="router:warm")
        await self.kernel_event("started", f"kernel started with sources {', '.join(self.registry.names)}")

    def write_skills(self) -> None:
        skills.write_all(self.registry.specs(), self.config.skills_folder)
        if self.config.hermes_skills_dir is not None:
            skills.link_all(self.registry.specs(), self.config.skills_folder, self.config.hermes_skills_dir)

    async def respec(self, name: str) -> None:
        """A source's spec changed while it runs (see SourceContext.respec):
        describe it again, and rebuild what is built from specs. The MCP
        tool list doesn't change, since source actions run through `act`,
        so no Hermes session loses its prompt cache; only the source's
        skill does. A trigger script whose grant names a tool that is gone
        fails at its next run, as a trigger.error row the dream reads."""
        old, new = self.registry.respec(name)
        self.data.ensure(new)
        self.tools.build()
        self.write_skills()
        before = {a.name for a in old.actions}
        after = {a.name for a in new.actions}
        added = sorted(after - before)
        removed = sorted(before - after)
        parts = []
        if added:
            parts.append("added " + ", ".join(f"{name}_{n}" for n in added))
        if removed:
            parts.append("removed " + ", ".join(f"{name}_{n}" for n in removed))
        summary = "; ".join(parts) or "changed descriptions or arguments"
        await self.kernel_event("source.respec", f"{name} changed what it offers: {summary}", entity=name, data={"added": added, "removed": removed})

    async def stop(self) -> None:
        if self._ticker:
            self._ticker.cancel()
        if self._warming:
            self._warming.cancel()
        await self.conversations.close()
        await self.triggers.stop()
        await self.waker.stop()
        await self.registry.stop()
        await self.hermes.close()
        await self.native_push.close()
        await self.hearing.close()
        await self.router.close()
        self.data.close()
        self.db.close()

    async def _tick(self) -> None:
        """Expires triggers, approvals and attendance once a minute."""
        while True:
            await asyncio.sleep(TICK)
            try:
                await self.triggers.expire()
                await self.policy.expire()
                await self.attendance.expire()
                await self._sweep_if_due()
                # A user added while the backend runs gets their dream.
                await self.dreams.ensure()
            except Exception:
                log.exception("tick failed")

    async def _sweep_if_due(self) -> None:
        row = self.db.execute("SELECT value FROM kv WHERE namespace = 'kernel' AND key = 'last_sweep'").fetchone()
        last = datetime.fromisoformat(json.loads(row["value"])) if row else None
        if last is None or self.now() - last >= SWEEP_EVERY:
            await self.sweep_sessions()

    async def sweep_sessions(self) -> int:
        """Deletes the Hermes sessions huvu started that nobody has used for
        SESSION_RETENTION: chats left behind by a rewind or a cold resume,
        old wakes. Hermes's own sessions (cron jobs, the dream) are its
        business."""
        deleted = 0
        for session, owner in self.sessions.unused_since(self.now() - SESSION_RETENTION):
            try:
                await self.hermes.delete_session(self.accounts.profile(owner.user, owner.group), session)
            except Exception:
                # Hermes is down, or the user is gone. Kept, and tried again
                # at the next sweep, unless the user is gone for good.
                log.warning("couldn't delete Hermes session %s", session, exc_info=True)
                if owner.user is not None and self.accounts.user(owner.user) is None:
                    self.sessions.forget(session)
                continue
            self.sessions.forget(session)
            deleted += 1
        self.db.execute(
            "INSERT INTO kv (namespace, key, value) VALUES ('kernel', 'last_sweep', ?) ON CONFLICT (namespace, key) DO UPDATE SET value = excluded.value",
            (json.dumps(self.now().isoformat()),),
        )
        if deleted:
            await self.kernel_event("sessions.swept", f"deleted {deleted} Hermes sessions unused for {SESSION_RETENTION.days} days")
        return deleted

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
    ) -> int:
        """Logs one of the kernel's own events in `huvu_events`: trigger runs
        and errors, actions, approvals, wakes, attendance. `user` makes it
        that user's alone, and `group` its members' (see scope.py). Returns
        the row's id."""
        row = {
            "type": type,
            "entity": entity,
            "summary": summary,
            "refs": refs or {},
            "data": data or {},
            "labels": labels or ["house"],
            "device_id": device,
        }
        return self.data.record("huvu", "events", row, at=self.now(), user=user, group=group)

    def events_since(self, type: str, since: datetime, *, entity: str | None = None, user: str | None = None) -> int:
        """How many of the kernel's events of a type since a time, for one
        entity or user."""
        where, args = ["type = ?", "ts >= ?"], [type, since.timestamp()]
        if entity is not None:
            where.append("entity = ?")
            args.append(entity)
        if user is not None:
            where.append("user_id = ?")
            args.append(user)
        return self.data.count("huvu", "events", " AND ".join(where), args)

    async def notify(
        self,
        user: str | None,
        group: str | None,
        text: str,
        *,
        image: str | None = None,
        urgency: str = "normal",
        opens: str | None = None,
        sender: str | None = None,
    ) -> None:
        """A notification on a user's phones, or on a group's members'
        phones when there is no user. It is kept for NOTIFY_KEEP, so a phone
        that wasn't connected finds it later. `opens` names the app screen
        that deals with it, such as "triggers". `sender` is the user who
        sent it through huvu_message: each recipient sees them by their own
        alias for them, and a group's message skips its sender."""
        now = self.now()
        # With no user, a group's members get it: the default group's when
        # none is named.
        group = None if user else (group or self.accounts.default_group())
        payload = {"id": f"n_{secrets.token_hex(5)}", "at": now, "text": text, "image": image, "urgency": urgency, "opens": opens, "user": user, "group": group}
        self.db.execute("DELETE FROM notifications WHERE ts < ?", ((now - NOTIFY_KEEP).timestamp(),))
        self.db.execute(
            "INSERT INTO notifications (id, at, ts, text, image, urgency, opens, user_id, group_id, sender_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (payload["id"], iso(now), now.timestamp(), text, image, urgency, opens, user, group, sender),
        )
        # One send per person, since each sees the sender by their own alias.
        recipients = [user] if user else [m for m in self.accounts.members(group) if m != sender]
        for person in recipients:
            said_by = self._sender(person, sender)
            self.push.send("notify", {**payload, "from": said_by}, self.accounts.phones(person))
            # The next ask on any of their own devices carries it, so a reply
            # to what huvu asked comes with the question.
            for device in self.accounts.devices_for(person):
                if device.user == person:
                    self.conversations.note(device.id, prompts.notified(text, said_by))

    def _sender(self, recipient: str | None, sender: str | None) -> str | None:
        """Who a notification is from, as its recipient calls them. None
        when huvu sent it."""
        return self.accounts.alias(recipient, sender) if sender else None

    def notifications(self, user: str | None) -> list[dict[str, Any]]:
        """What was sent to a user's phones: their own notifications and
        their member groups', oldest first. A message the user sent to a
        group is left out."""
        groups = self.accounts.member_groups(user)
        marks = ", ".join("?" * len(groups))
        rows = self.db.execute(
            f"SELECT * FROM notifications WHERE user_id = ? OR (user_id IS NULL AND group_id IN ({marks}) AND sender_id IS NOT ?) ORDER BY ts",
            (user, *groups, user),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "at": from_iso(r["at"]),
                "text": r["text"],
                "image": r["image"],
                "urgency": r["urgency"],
                "opens": r["opens"],
                "user": r["user_id"],
                "group": r["group_id"],
                "from": self._sender(user, r["sender_id"]),
            }
            for r in rows
        ]

    async def feeds(self) -> list[Feed]:
        feeds: list[Feed] = []
        for name in self.registry.names:
            try:
                feeds.extend(await self.registry.get(name).feeds())
            except Exception:
                log.exception("feeds from %s failed", name)
        return feeds
