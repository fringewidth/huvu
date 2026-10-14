"""Conversations: the frontend's turns, multiplexed onto Hermes sessions.

Every device has one conversation, keyed by the device's id, and it belongs
to the device's user, or to its group on a shared device. It maps to one
Hermes session in that user's Hermes profile, or in the group's. Each ask becomes a turn: the kernel sends it to Hermes, turns
Hermes's stream into frontend events (see the relay below), and sends those
to the client that asked and to the same device's other open streams. A
turn never reaches another device.

Prompt caching decides when a session continues. The provider keeps a
session's prompt cached for CACHE_TTL after its last use, so an ask within
that window continues the same Hermes session and hits the cache. An ask
after it starts a fresh session, primed with a transcript of the asks and
answers so far: with the cache cold, a short transcript costs less than
replaying the old session's thinking and tool output. The transcript holds
only the latest turns that fit in TRANSCRIPT_CHARS, and the model reads
older ones with data_sql, from `chat_turns`.

Every turn that ends becomes a `chat_turns` row (builtin/chat.py), with
its cache reads (`usage`), so the window can be checked against the
provider, and its route.

The router (routing.py) picks each ask's model before its turn starts. A
one-off ask runs on basic in a session of its own, with the ask alone, used
once. Everything else runs on pro in the
conversation's main session, so no other turn touches its cache.

The main session must know every turn of the conversation. Its next ask
carries a note for each turn after the last one it ran (`_unseen`): what
was said and what was answered. The notes are appended to the new message,
so the cached prompt before it holds. A fresh main session reads every turn
in its transcript instead.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from . import prompts
from .db import dumps, from_iso, iso, loads
from .hermes import HermesError, HermesEvent, Profile
from .routing import Route
from .source import SourceError
from .spec import Caller

if TYPE_CHECKING:
    from .accounts import Device
    from .kernel import Kernel

log = logging.getLogger("huvu.conversations")

Ending = Literal["failed", "stopped"] | None

HUVU_PREFIX = "mcp__huvu__"
# How long the provider keeps a session's prompt cached after its last use.
CACHE_TTL = timedelta(hours=1)
# How much of the conversation a fresh session reads: the latest turns that
# fit, about 12,000 tokens at four characters a token. Older turns are left
# out, so a long conversation doesn't cost more on every cold start; the
# model reads them from `chat_turns`.
TRANSCRIPT_CHARS = 50_000
# How many notes wait for a device's next ask; older ones go. A phone
# nobody asks from for days gathers every notification.
NOTES_KEPT = 12
# How long a stopped turn may take to end before huvu cuts it off itself.
STOP_GRACE = 15.0
# Hermes's own tools that are the agent's housekeeping, not something to
# show the owner. The tool search bridge's lookups are among them; its
# `tool_call` reaches here under the name of the tool it ran.
HIDDEN_TOOLS = {
    "memory",
    "skill_manage",
    "skill_view",
    "skills_list",
    "session_search",
    "todo",
    "clarify",
    "hermes_tool_search",
    "tool_search",
    "tool_describe",
}
# Hermes's way to attach a file: `MEDIA:<path>` in the answer, such as the
# path of a screenshot its browser took. The path is bare, ending at a
# space, or quoted when it has spaces. An https URL works too. Up to three
# quote or emphasis marks may wrap the tag (`MEDIA:/a.png`).
MEDIA_TAG = re.compile(
    r"""[`"'*_]{0,3}MEDIA:[^\S\n]*(?:`(?P<tick>[^`\n]+)`|"(?P<double>[^"\n]+)"|'(?P<single>[^'\n]+)'|(?P<bare>(?:[/~]|https://)\S*))[`"'*_]{0,3}"""
)
# What follows `MEDIA:` while its path may still be coming in.
MEDIA_PENDING = re.compile(r"""[^\S\n]*(?:[`"'][^\n]*|\S*)\Z""")
# Marks and punctuation after a bare path that are not part of it.
MEDIA_CLOSERS = "`\"'*_.,;:!?)]}"
# A tag held back longer than this is text after all.
MEDIA_HOLD = 4096


class Action(BaseModel):
    """Something the owner did in the app between two asks, such as opening
    a live feed. It is context for the model, never a request: it rides with
    the next ask and gets no reply of its own."""

    text: str
    at: datetime


@dataclass
class Turn:
    id: str
    conversation_id: str
    seq: int
    at: datetime
    ask: str
    answer: str = ""
    parts: list[dict[str, Any]] = field(default_factory=list)
    ending: Ending = None
    running: bool = True
    hermes_session: str | None = None
    actions: list[Action] = field(default_factory=list)
    # The turn an edit replaced, for its history row. Not kept here.
    replaces: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "conversation": self.conversation_id,
            "at": self.at,
            "ask": self.ask,
            "answer": self.answer,
            "parts": self.parts,
            "ending": self.ending,
            "running": self.running,
            "actions": [a.model_dump() for a in self.actions],
        }


@dataclass
class LiveTurn:
    turn: Turn
    profile: Profile
    # The model it runs on, and why (routing.py). None in a wake, which
    # runs on its trigger's tier.
    route: Route | None
    # Whose turn it is in chat_turns: the device's user, or whoever is
    # recognized at a shared device.
    user: str | None = None
    # The shared device's group, if it is one.
    group: str | None = None
    # The devices its events go to: the asking device, or every device of
    # a wake's person.
    audience: list[str] = field(default_factory=list)
    # The wake it runs in (wake.py), if it does.
    wake: str | None = None
    # Seconds the Hermes run may take before huvu stops it.
    deadline: float | None = None
    timed_out: bool = False
    # Why Hermes failed it, when it did.
    error: str | None = None
    # Called once it has ended and been saved.
    on_end: Callable[[LiveTurn], None] | None = None
    run_id: str | None = None
    task: asyncio.Task[None] | None = None
    history: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    subscribers: list[asyncio.Queue[tuple[str, dict[str, Any]] | None]] = field(default_factory=list)
    # Part ids of tools that started and haven't completed, by tool name.
    open_tools: dict[str, list[str]] = field(default_factory=dict)
    stop_requested: bool = False
    # Token counts from Hermes's last event, cache reads included.
    usage: dict[str, Any] | None = None
    # Answer text held back while a MEDIA tag in it may not be complete.
    heard: str = ""
    ended: bool = False


class ConversationHub:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        self._live: dict[str, LiveTurn] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    def note(self, conversation_id: str, text: str) -> None:
        """Something huvu tells the model on the device's next ask, such as
        who was recognized there. Appended to that ask's message, so the
        session's prompt cache holds. Kept in the database, so a restart
        doesn't lose it."""
        notes = [*self._notes(conversation_id), Action(text=text, at=self._kernel.now())][-NOTES_KEPT:]
        self._db.execute(
            "INSERT INTO kv (namespace, key, value) VALUES ('notes', ?, ?) ON CONFLICT (namespace, key) DO UPDATE SET value = excluded.value",
            (conversation_id, dumps([n.model_dump() for n in notes])),
        )

    def _notes(self, conversation_id: str) -> list[Action]:
        row = self._db.execute("SELECT value FROM kv WHERE namespace = 'notes' AND key = ?", (conversation_id,)).fetchone()
        return [Action.model_validate(n) for n in loads(row["value"])] if row else []

    def _take_notes(self, conversation_id: str) -> list[Action]:
        notes = self._notes(conversation_id)
        self._db.execute("DELETE FROM kv WHERE namespace = 'notes' AND key = ?", (conversation_id,))
        return notes

    @property
    def _db(self):
        return self._kernel.db

    def turns(self, conversation_id: str) -> list[Turn]:
        rows = self._db.execute("SELECT * FROM turns WHERE conversation_id = ? ORDER BY seq", (conversation_id,)).fetchall()
        return [self._live[r["id"]].turn if r["id"] in self._live else _turn(r) for r in rows]

    def visible(self, device: Device, turn_id: str) -> bool:
        """Whether a device may see a turn: one of its own conversation, or
        of a wake of its user's."""
        row = self._db.execute("SELECT conversation_id FROM turns WHERE id = ?", (turn_id,)).fetchone()
        if row is None:
            return False
        conversation = row["conversation_id"]
        return conversation == device.id or (device.user is not None and self._kernel.waker.owner(conversation) == device.user)

    async def ask(
        self,
        conversation_id: str,
        text: str,
        replaces: str | None = None,
        *,
        user: str | None,
        group: str | None = None,
        actions: list[Action] | None = None,
        turn_id: str | None = None,
        mode: str = "text",
    ) -> LiveTurn:
        """`conversation_id` is the asking device's id, and `user` its user,
        or `group` its group on a shared device. `turn_id` lets the client
        name its own turn, so it can stop or rewind it by an id it already
        has."""
        if turn_id is not None and self._db.execute("SELECT 1 FROM turns WHERE id = ?", (turn_id,)).fetchone():
            raise ValueError(f"turn {turn_id!r} already exists")
        conversation = self._conversation(conversation_id, user)
        profile = self._kernel.accounts.profile(user, group)
        for live in [l for l in self._live.values() if l.turn.conversation_id == conversation_id]:
            await self.stop(live.turn.id, wait=True)
        kept = self.turns(conversation_id)
        session = conversation["hermes_session"]
        actions = actions or []
        tz = self._kernel.now().tzinfo
        self._kernel.attendance.touch(conversation_id)
        asked = f"{prompts.mode_note(mode)}\n\n{text}"
        if replaces is not None:
            index = next((i for i, t in enumerate(kept) if t.id == replaces), None)
            if index is None:
                raise KeyError(f"no turn {replaces!r} in conversation {conversation_id!r}")
            self._db.execute("DELETE FROM turns WHERE conversation_id = ? AND seq >= ?", (conversation_id, kept[index].seq))
            kept = kept[:index]
            # The main session holds turns the rewind dropped.
            self._db.execute("UPDATE conversations SET hermes_session = NULL WHERE id = ?", (conversation_id,))
            session = None
        route = await self._kernel.router.route(text, notes=bool(self._notes(conversation_id)))
        if route.tier == "basic":
            # A one-off: a session of its own with the ask alone, so the
            # main session and its cache are never touched.
            message = asked
            session = await self._new_session(conversation_id, user, group, profile, route.key, main=False)
        else:
            if session is not None and (self._cold(session) or self._kernel.sessions.route(session) != route.key):
                session = None
            notes = self._take_notes(conversation_id)
            message = _with_notes(_with_actions(asked, actions, tz), notes, tz)
            if session is None:
                # A rewind, a cold cache, another model, or a session the
                # retention sweep deleted: a fresh session carries the
                # conversation so far, the quick turns among it.
                message = _seeded(kept, message, tz, conversation_id)
                session = await self._new_session(conversation_id, user, group, profile, route.key, main=True)
            else:
                message = _with_quick_turns(message, _unseen(kept, session), tz)
        turn = Turn(
            id=turn_id or f"t_{secrets.token_hex(5)}",
            conversation_id=conversation_id,
            seq=(kept[-1].seq + 1) if kept else 0,
            at=self._kernel.now(),
            ask=text,
            hermes_session=session,
            actions=actions,
            replaces=replaces,
        )
        live = LiveTurn(turn, profile, route, user=user or self._kernel.attendance.holder(conversation_id), group=group, audience=[conversation_id])
        events = self._kernel.hermes.chat(profile, session, message, model=route.choice.model, provider=route.choice.provider, effort=route.effort)
        return self._begin(live, events)

    async def wake_turn(
        self,
        wake: str,
        session: str,
        profile: Profile,
        events: AsyncIterator[HermesEvent],
        *,
        user: str | None,
        group: str | None,
        ask: str,
        deadline: float,
        on_end: Callable[[LiveTurn], None],
        actions: list[Action] | None = None,
        turn_id: str | None = None,
    ) -> LiveTurn:
        """A turn in a wake (wake.py): the run a trigger started, with no
        ask, or its person's ask in it. `events` is the Hermes run. Its
        events go to every device of the wake's person."""
        if turn_id is not None and self._db.execute("SELECT 1 FROM turns WHERE id = ?", (turn_id,)).fetchone():
            raise ValueError(f"turn {turn_id!r} already exists")
        for live in [l for l in self._live.values() if l.turn.conversation_id == wake]:
            await self.stop(live.turn.id, wait=True)
        kept = self.turns(wake)
        turn = Turn(
            id=turn_id or f"t_{secrets.token_hex(5)}",
            conversation_id=wake,
            seq=(kept[-1].seq + 1) if kept else 0,
            at=self._kernel.now(),
            ask=ask,
            hermes_session=session,
            actions=actions or [],
        )
        audience = [d.id for d in self._kernel.accounts.devices_for(user) if d.user == user] if user else []
        live = LiveTurn(turn, profile, None, user=user, group=group, audience=audience, wake=wake, deadline=deadline, on_end=on_end)
        return self._begin(live, events)

    def _begin(self, live: LiveTurn, events: AsyncIterator[HermesEvent]) -> LiveTurn:
        self._save(live.turn, insert=True)
        self._live[live.turn.id] = live
        self._emit(live, "turn.started", {"ask": live.turn.ask, "at": live.turn.at})
        live.task = asyncio.create_task(self._relay(live, events), name=f"turn:{live.turn.id}")
        return live

    def _conversation(self, conversation_id: str, user: str | None):
        row = self._db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO conversations (id, user_id, created_at) VALUES (?, ?, ?)",
                (conversation_id, user, iso(self._kernel.now())),
            )
            row = self._db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        elif row["user_id"] != user:
            raise PermissionError(f"conversation {conversation_id} belongs to someone else")
        return row

    async def _new_session(
        self, conversation_id: str, user: str | None, group: str | None, profile: Profile, route: str, *, main: bool
    ) -> str:
        """A Hermes session for the conversation. `main` makes it the one
        its next asks on pro continue; a quick turn's session is used once."""
        session = f"huvu-{conversation_id}-{secrets.token_hex(3)}"
        # Hermes refuses a title another session of the profile has, and
        # two quick turns can start in the same minute: the id keeps it unique.
        title = f"huvu {'chat' if main else 'quick turn'} on {conversation_id} {self._kernel.now():%Y-%m-%d %H:%M} ({session})"
        await self._kernel.hermes.create_session(profile, session, title=title)
        self._kernel.sessions.record(session, "chat", user=user, group=group, device=conversation_id, route=route)
        if main:
            self._db.execute("UPDATE conversations SET hermes_session = ? WHERE id = ?", (session, conversation_id))
        return session

    async def subscribe(self, live: LiveTurn) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """The turn's events from its start, then live until it ends. Takes
        the turn itself, since a fast turn can end before anyone reads it."""
        queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
        for item in live.history:
            queue.put_nowait(item)
        if live.ended:
            queue.put_nowait(None)
        else:
            live.subscribers.append(queue)
        while (item := await queue.get()) is not None:
            yield item

    def replay(self, turn_id: str) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """A turn's events from its start, for a client that opens while the
        turn runs. A delta doesn't say where its text goes, so a client
        joining mid-turn can't add later deltas to a snapshot; it rebuilds
        the turn from here instead. A live turn replays what happened and
        then follows it. An ended one is rebuilt from what was kept."""
        live = self._live.get(turn_id)
        return self.subscribe(live) if live is not None else self._kept_events(turn_id)

    async def _kept_events(self, turn_id: str) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        row = self._db.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
        if row is None:
            return
        turn = _turn(row)
        base = {"turn": turn.id, "conversation": turn.conversation_id}
        yield "turn.started", {**base, "ask": turn.ask, "at": turn.at}
        for part in turn.parts:
            yield "part", {**base, "part": part}
        yield "turn.ended", {**base, "ending": turn.ending, "answer": turn.answer}

    def _cold(self, session: str) -> bool:
        """Whether the provider's cache of a session has expired."""
        last = self._kernel.sessions.last_used(session)
        return last is None or self._kernel.now() - last > CACHE_TTL

    async def stop(self, turn_id: str, *, wait: bool = False) -> None:
        """Asks Hermes to stop the turn. The relay goes on until Hermes's
        stream ends; `wait` waits for that, up to STOP_GRACE."""
        live = self._live.get(turn_id)
        if live is None or live.ended:
            return
        if not live.stop_requested:
            live.stop_requested = True
            # Before run.started there is no run to stop yet; the relay
            # stops it when it starts.
            if live.run_id:
                await self._kernel.hermes.stop(live.profile, live.run_id)
        grace = self._grace(live)
        if wait:
            await grace
        else:
            self._spawn(grace)

    async def _grace(self, live: LiveTurn) -> None:
        if live.task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(live.task), STOP_GRACE)
        except TimeoutError:
            log.warning("turn %s: Hermes didn't end within %.0f s of a stop; cutting it off", live.turn.id, STOP_GRACE)
            live.task.cancel()
            await asyncio.gather(live.task, return_exceptions=True)

    async def close(self) -> None:
        """At shutdown: cut every turn off now."""
        for live in list(self._live.values()):
            if live.run_id:
                await self._kernel.hermes.stop(live.profile, live.run_id)
            if live.task and not live.task.done():
                live.task.cancel()
                await asyncio.gather(live.task, return_exceptions=True)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def show(self, caller: Caller, kind: str, args: dict[str, Any]) -> bool:
        """Adds a part from a `ui_*` tool to the live turn of the Hermes
        session that called it. Without a session (the huvu Hermes plugin
        isn't installed), it uses the caller's only live turn, if there is
        one."""
        live = [
            l
            for l in self._live.values()
            if not l.ended
            and (l.turn.hermes_session == caller.session if caller.session else (l.user, l.group) == (caller.user, caller.group))
        ]
        target = live[0] if len(live) == 1 else None
        if target is not None:
            if kind == "image":
                self._flush(target)
                part: dict[str, Any] = {"kind": "image", "src": args["src"], "caption": args.get("caption", ""), "after": _said(target.turn)}
            else:
                part = {
                    "kind": "tool",
                    "id": f"{target.turn.id}-p{len(target.turn.parts)}",
                    "toolName": kind,
                    "args": args,
                    "result": {},
                    "after": _said(target.turn),
                }
            target.turn.parts.append(part)
            self._emit(target, "part", {"part": part})
        return target is not None

    async def _relay(self, live: LiveTurn, events: AsyncIterator[HermesEvent]) -> None:
        ending: Ending = "failed"
        try:
            async with asyncio.timeout(live.deadline):
                async for event in events:
                    ending = self._translate(live, event, ending)
        except TimeoutError:
            live.timed_out = True
            ending = "stopped"
            # Closing the stream may not end the run, so it is stopped by name.
            if live.run_id:
                await self._kernel.hermes.stop(live.profile, live.run_id)
        except asyncio.CancelledError:
            ending = "stopped"
        except HermesError as err:
            log.error("turn %s: %s", live.turn.id, err)
            live.error = str(err)
            ending = "failed"
        except Exception:
            log.exception("turn %s failed", live.turn.id)
            ending = "failed"
        finally:
            self._end(live, ending)

    def _translate(self, live: LiveTurn, event: HermesEvent, ending: Ending) -> Ending:
        """Turns one Hermes event into frontend events, and returns how the
        turn would end if the stream stopped here."""
        data = event.data
        name = event.name
        if name == "run.started":
            live.run_id = data.get("run_id")
            if live.stop_requested and live.run_id:
                self._spawn(self._kernel.hermes.stop(live.profile, live.run_id))
        elif name == "assistant.delta" and data.get("delta"):
            self._hear(live, data["delta"])
        elif name == "assistant.commentary" and data.get("text"):
            # What huvu says beside its tool calls is part of its answer: the
            # first thing the person sees while the tools run. Hermes has
            # sometimes sent it as deltas already.
            if not data.get("already_streamed"):
                self._hear(live, data["text"])
            self._flush(live)
            # Those words are finished: the paragraph breaks now, not when
            # huvu writes again, so its voice says their last sentence while
            # the tools run instead of going quiet in the middle.
            if live.turn.answer.strip() and not live.turn.answer.endswith("\n\n"):
                self._say(live, "\n\n")
        elif name == "tool.started":
            self._flush(live)
            self._tool_started(live, data.get("tool_name") or "", data.get("args") or {})
        elif name in ("tool.completed", "tool.failed"):
            self._tool_completed(live, data.get("tool_name") or "", data.get("preview"), failed=name == "tool.failed")
        elif name == "assistant.completed":
            self._flush(live)
        if name == "assistant.completed" and data.get("content") and not live.turn.answer and not data.get("interrupted"):
            # A turn that streamed no deltas still has its final text here.
            # An interrupted one has Hermes's placeholder ("Operation
            # interrupted: waiting for model response"), which is no answer:
            # the stage says "stopped" itself.
            live.turn.answer = data["content"]
            self._emit(live, "answer.delta", {"text": data["content"]})
        if name == "run.failed":
            live.error = str(data.get("error") or "the run failed")
        if name in ("run.completed", "run.cancelled", "run.failed") and isinstance(data.get("usage"), dict):
            live.usage = data["usage"]
        # Hermes says how the turn ended. A turn stopped after Hermes had
        # finished is complete.
        endings: dict[str, Ending] = {"run.completed": None, "run.cancelled": "stopped", "run.failed": "failed", "error": "failed"}
        return endings.get(name, ending)

    def _tool_started(self, live: LiveTurn, tool: str, args: dict[str, Any]) -> None:
        bare = tool.removeprefix(HUVU_PREFIX)
        huvu = tool.startswith(HUVU_PREFIX)
        # The huvu `ui_*` tools add their own parts (see show), and huvu's
        # reads are lookups. Only huvu's actions show, as notes.
        hidden = tool in HIDDEN_TOOLS or (huvu and (bare.startswith("ui_") or not self._kernel.tools.writes(bare)))
        if hidden:
            return
        adapter = ADAPTERS.get(bare)
        # `act` runs a source action by name: the stage shows the action.
        shown = str(args.get("name") or bare) if huvu and bare == "act" else bare
        part = {
            "kind": "tool",
            "id": f"{live.turn.id}-p{len(live.turn.parts)}",
            "toolName": shown,
            "args": adapter.args(args) if adapter else {},
            "result": None,
            "after": _said(live.turn),
        }
        live.turn.parts.append(part)
        live.open_tools.setdefault(bare, []).append(part["id"])
        self._emit(live, "part", {"part": part})

    def _tool_completed(self, live: LiveTurn, tool: str, preview: str | None, failed: bool) -> None:
        bare = tool.removeprefix(HUVU_PREFIX)
        open_ids = live.open_tools.get(bare) or []
        if not open_ids:
            return
        part_id = open_ids.pop(0)
        adapter = ADAPTERS.get(bare)
        result = {"error": True} if failed else (adapter.result(preview) if adapter else {})
        for part in live.turn.parts:
            if part.get("id") == part_id:
                part["result"] = result
        self._emit(live, "part.update", {"id": part_id, "result": result})

    def think(self, session: str, text: str, start: bool, user: str | None, group: str | None) -> bool:
        """A piece of the model's reasoning summary for the turn running in
        `session`, from the huvu Hermes plugin. The stage shows it while the
        turn runs; it isn't kept with the turn. `start` begins a new burst of
        thinking. A session that belongs to another profile is ignored."""
        owner = self._kernel.sessions.owner(session)
        if owner is None or (owner.user, owner.group) != (user, group):
            return False
        live = next((l for l in self._live.values() if l.turn.hermes_session == session and not l.ended), None)
        if live is None or not text:
            return False
        self._emit(live, "thinking.delta", {"text": text, "start": start})
        return True

    def _hear(self, live: LiveTurn, text: str) -> None:
        """Answer text from Hermes. A MEDIA tag in it becomes an image part
        where the tag was, and text that may be the start of one waits for
        the next delta."""
        live.heard += text
        self._release(live, final=False)

    def _flush(self, live: LiveTurn) -> None:
        """Ends the text Hermes is writing, so a held-back tag is finished."""
        self._release(live, final=True)

    def _release(self, live: LiveTurn, final: bool) -> None:
        at = 0
        text = live.heard
        for tag in MEDIA_TAG.finditer(text):
            if tag["bare"] is not None and tag.end() == len(text) and not final:
                # The path may go on in the next delta.
                break
            path = tag["tick"] or tag["double"] or tag["single"] or tag["bare"].rstrip(MEDIA_CLOSERS)
            self._say(live, text[at : tag.start()])
            self._show_media(live, path, tag[0])
            at = tag.end()
        rest = text[at:]
        held = len(rest) if final else _unfinished_tag(rest)
        self._say(live, rest[:held])
        live.heard = rest[held:]

    def _show_media(self, live: LiveTurn, path: str, tag: str) -> None:
        try:
            src = self._kernel.media.url_for(path)
        except SourceError as err:
            # The tag stays as text, so the person sees what huvu tried.
            log.warning("turn %s: no image for %s: %s", live.turn.id, path, err)
            self._say(live, tag)
            return
        part = {"kind": "image", "src": src, "caption": "", "after": _said(live.turn)}
        live.turn.parts.append(part)
        self._emit(live, "part", {"part": part})

    def _say(self, live: LiveTurn, text: str) -> None:
        if not text:
            return
        # huvu's own break after a paragraph that already has one.
        if live.turn.answer.endswith("\n\n"):
            text = text.lstrip("\n")
            if not text:
                return
        live.turn.answer += text
        self._emit(live, "answer.delta", {"text": text})

    def _emit(self, live: LiveTurn, name: str, data: dict[str, Any]) -> None:
        payload = {"turn": live.turn.id, "conversation": live.turn.conversation_id, **({"wake": live.wake} if live.wake else {}), **data}
        live.history.append((name, payload))
        for queue in live.subscribers:
            queue.put_nowait((name, payload))
        self._kernel.push.send(name, payload, live.audience)

    def _end(self, live: LiveTurn, ending: Ending) -> None:
        if live.ended:
            return
        self._flush(live)
        live.turn.ending = ending
        live.turn.running = False
        # A break after words beside a last tool call, with nothing after.
        live.turn.answer = live.turn.answer.rstrip()
        self._save(live.turn, insert=False)
        if live.turn.hermes_session:
            self._kernel.sessions.touch(live.turn.hermes_session)
        self._emit(live, "turn.ended", {"ending": ending, "answer": live.turn.answer})
        # A wake's runs are in the kernel's log (wake.py); chat_turns is
        # what was said in the conversations.
        if live.route is not None:
            self._log(live, live.route)
        live.ended = True
        for queue in live.subscribers:
            queue.put_nowait(None)
        self._live.pop(live.turn.id, None)
        if live.on_end is not None:
            live.on_end(live)

    def _log(self, live: LiveTurn, route: Route) -> None:
        """The turn as a `chat_turns` row, its user's, so their dream can
        read what was said, even at a household device whose Hermes session
        lives in the household's profile."""
        turn = live.turn
        device = self._kernel.accounts.device(turn.conversation_id)
        row = {
            "turn_id": turn.id,
            "device_id": turn.conversation_id,
            "device": device.name if device else None,
            "n": turn.seq + 1,
            "them": turn.ask,
            "you": turn.answer,
            "ending": turn.ending,
            "actions": [{"text": a.text, "at": a.at.isoformat()} for a in turn.actions] or None,
            "replaces": turn.replaces,
            "usage": live.usage,
            "route": route.row(),
        }
        self._kernel.data.record("chat", "turns", row, at=turn.at, user=live.user, group=None if live.user else live.group)

    def _save(self, turn: Turn, insert: bool) -> None:
        if insert:
            self._db.execute(
                "INSERT INTO turns (id, conversation_id, seq, at, ask, answer, parts, ending, running, hermes_session, actions) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    turn.id,
                    turn.conversation_id,
                    turn.seq,
                    iso(turn.at),
                    turn.ask,
                    turn.answer,
                    dumps(turn.parts),
                    turn.ending,
                    int(turn.running),
                    turn.hermes_session,
                    dumps([a.model_dump() for a in turn.actions]),
                ),
            )
        else:
            self._db.execute(
                "UPDATE turns SET answer = ?, parts = ?, ending = ?, running = ? WHERE id = ?",
                (turn.answer, dumps(turn.parts), turn.ending, int(turn.running), turn.id),
            )


@dataclass
class Adapter:
    """Turns one Hermes tool's events into the `args` and `result` a
    frontend part view reads (see frontend/src/stage/partViews.tsx)."""

    args: Any
    result: Any


# web_search's result stays empty: Hermes sends only the first 500
# characters of a result, and a source count from a cut-off preview would
# be wrong, so the stage shows none.
ADAPTERS: dict[str, Adapter] = {
    "web_search": Adapter(args=lambda a: {"query": str(a.get("query", ""))}, result=lambda preview: {}),
}


def _unfinished_tag(text: str) -> int:
    """Where a MEDIA tag that may not be finished starts in `text`, or its
    length if there is none. A tag cut anywhere counts, even inside the
    word MEDIA itself."""
    start = text.rfind("MEDIA:")
    if start < 0 or MEDIA_PENDING.match(text, start + len("MEDIA:")) is None or len(text) - start >= MEDIA_HOLD:
        start = next((len(text) - n for n in range(len("MEDIA"), 0, -1) if text.endswith("MEDIA"[:n])), len(text))
    if start == len(text):
        return start
    # The marks that may wrap the tag wait with it.
    opened = start
    while opened > max(start - 3, 0) and text[opened - 1] in "`\"'*_":
        opened -= 1
    return opened


def _said(turn: Turn) -> int:
    """Where a part goes in the answer: after everything huvu has said so
    far, so the stage shows each tool between the words around it. Counted
    without leading whitespace, since the stage trims the answer."""
    return len(turn.answer.lstrip())


def _note(action: Action, tz: tzinfo | None) -> str:
    return prompts.action_note(f"{action.at.astimezone(tz):%a %Y-%m-%d %H:%M}", action.text)


def _with_notes(text: str, notes: list[Action], tz: tzinfo | None) -> str:
    lines = [prompts.huvu_note(f"{n.at.astimezone(tz):%H:%M}", n.text) for n in notes]
    return "\n".join([*lines, "", text]) if lines else text


def _with_actions(text: str, actions: list[Action], tz: tzinfo | None) -> str:
    if not actions:
        return text
    return "\n".join([*(_note(a, tz) for a in actions), "", text])


def _unseen(kept: list[Turn], session: str) -> list[Turn]:
    """Every turn after the last one the main session ran: what it must
    hear before its next ask, so its last turn is the conversation's."""
    last = max((i for i, t in enumerate(kept) if t.hermes_session == session), default=None)
    return kept[last + 1 :] if last is not None else []


def _with_quick_turns(text: str, turns: list[Turn], tz: tzinfo | None) -> str:
    """The main session's next message, after the turns it didn't run:
    each one's action notes, what was said and what was answered. Appended,
    so the session's prompt cache holds."""
    if not turns:
        return text
    lines: list[str] = []
    for t in turns:
        lines += [_note(a, tz) for a in t.actions]
        lines.append(prompts.quick_turn(f"{t.at.astimezone(tz):%H:%M}", t.ask, t.answer))
    return "\n".join([*lines, "", text])


def _seeded(kept: list[Turn], text: str, tz: tzinfo | None, device: str) -> str:
    """The first message of a fresh session that continues a conversation,
    the way a compacted session continues: what was asked and answered so
    far, then the new message. The model is told to carry on as if nothing
    happened, not to acknowledge it. Only the latest turns that fit in
    TRANSCRIPT_CHARS go in."""
    if not kept:
        return text
    earlier: list[tuple[list[str], str, str]] = []
    size = 0
    for t in reversed(kept):
        notes = [_note(a, tz) for a in t.actions]
        size += sum(map(len, notes)) + len(t.ask) + len(t.answer)
        if size > TRANSCRIPT_CHARS:
            break
        earlier.append((notes, t.ask, t.answer))
    earlier.reverse()
    return prompts.continued(earlier, text, left_out=len(kept) - len(earlier), device=device)


def _turn(row) -> Turn:
    return Turn(
        id=row["id"],
        conversation_id=row["conversation_id"],
        seq=row["seq"],
        at=from_iso(row["at"]),
        ask=row["ask"],
        answer=row["answer"],
        parts=loads(row["parts"]),
        ending=row["ending"],
        running=bool(row["running"]),
        hermes_session=row["hermes_session"],
        actions=[Action.model_validate(a) for a in loads(row["actions"])],
    )
