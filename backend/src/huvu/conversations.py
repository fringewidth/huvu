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
replaying the old session's thinking and tool output. Each turn's cache
reads land in the event log (`conversation.turn`, `usage`), so the window
can be checked against the provider.

Edit and retry rewind: the turn and everything after it are replaced.
Hermes's API can fork a session but can't cut one short, so a rewind always
starts a fresh session from the transcript of the turns that stay. (A rewind
endpoint in Hermes, wrapping what its CLI's /undo does, would keep a warm
cache; see docs/backend-architecture.md.)

Stopping a turn asks Hermes to stop, and keeps relaying until Hermes's own
stream ends, so the turn shows every token Hermes produced and ends the way
Hermes says it did: complete if it finished anyway, stopped if it was cut
off. Only a Hermes that hasn't ended STOP_GRACE later is cut off here.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from .db import dumps, from_iso, iso, loads
from .hermes import HermesError, HermesEvent, Profile
from .spec import Caller, Event

if TYPE_CHECKING:
    from .kernel import Kernel

log = logging.getLogger("huvu.conversations")

Ending = Literal["failed", "stopped"] | None

HUVU_PREFIX = "mcp__huvu__"
# How long the provider keeps a session's prompt cached after its last use.
CACHE_TTL = timedelta(minutes=5)
# How long a stopped turn may take to end before huvu cuts it off itself.
STOP_GRACE = 15.0
# Opens a fresh session that continues a conversation, the way a compacted
# session continues.
COMPACTED = (
    "This conversation is being continued from an earlier session that was compacted. "
    "What was said so far is below: their messages, and your answers. "
    "Carry on from where you left off as the same assistant. Don't mention or acknowledge this note; "
    "just answer their new message at the end."
)
# How much of a turn the event log keeps in its summary.
TURN_ASK_CHARS = 200
TURN_ANSWER_CHARS = 300
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
    # Whose turn it is in the event log: the device's user, or whoever is
    # recognized at a shared device.
    user: str | None = None
    # The shared device's group, if it is one.
    group: str | None = None
    run_id: str | None = None
    task: asyncio.Task[None] | None = None
    history: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    subscribers: list[asyncio.Queue[tuple[str, dict[str, Any]] | None]] = field(default_factory=list)
    # Part ids of tools that started and haven't completed, by tool name.
    open_tools: dict[str, list[str]] = field(default_factory=dict)
    stop_requested: bool = False
    # Token counts from Hermes's last event, cache reads included.
    usage: dict[str, Any] | None = None
    # Set after huvu's words beside a tool call ("On it, setting that up."),
    # so what it writes next starts a new paragraph.
    paragraph_due: bool = False
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
        notes = self._notes(conversation_id)
        notes.append(Action(text=text, at=self._kernel.now()))
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

    def owns(self, conversation_id: str, turn_id: str) -> bool:
        row = self._db.execute("SELECT conversation_id FROM turns WHERE id = ?", (turn_id,)).fetchone()
        return bool(row and row["conversation_id"] == conversation_id)

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
        notes = self._take_notes(conversation_id)
        message = _with_notes(_with_actions(text, actions, tz), notes, tz)
        if replaces is not None:
            index = next((i for i, t in enumerate(kept) if t.id == replaces), None)
            if index is None:
                raise KeyError(f"no turn {replaces!r} in conversation {conversation_id!r}")
            self._db.execute("DELETE FROM turns WHERE conversation_id = ? AND seq >= ?", (conversation_id, kept[index].seq))
            kept = kept[:index]
            session = None
        elif session is not None and self._cold(session):
            session = None
        if session is None:
            # A rewind, a cold cache, or a session the retention sweep
            # deleted: a fresh session carries the conversation so far.
            message = _seeded(kept, message, tz)
            session = await self._new_session(conversation_id, user, group, profile)
        turn = Turn(
            id=turn_id or f"t_{secrets.token_hex(5)}",
            conversation_id=conversation_id,
            seq=(kept[-1].seq + 1) if kept else 0,
            at=self._kernel.now(),
            ask=text,
            hermes_session=session,
            actions=actions,
        )
        self._save(turn, insert=True)
        live = LiveTurn(turn, profile, user=user or self._kernel.attendance.holder(conversation_id), group=group)
        self._live[turn.id] = live
        self._emit(live, "turn.started", {"ask": text, "at": turn.at})
        live.task = asyncio.create_task(self._relay(live, message), name=f"turn:{turn.id}")
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

    async def _new_session(self, conversation_id: str, user: str | None, group: str | None, profile: Profile) -> str:
        session = f"huvu-{conversation_id}-{secrets.token_hex(3)}"
        title = f"huvu chat on {conversation_id} {self._kernel.now():%Y-%m-%d %H:%M}"
        await self._kernel.hermes.create_session(profile, session, title=title)
        self._kernel.sessions.record(session, "chat", user=user, group=group, device=conversation_id)
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
                part: dict[str, Any] = {"kind": "image", "src": args["src"]}
            else:
                part = {"kind": "tool", "id": f"{target.turn.id}-p{len(target.turn.parts)}", "toolName": kind, "args": args, "result": {}}
            target.turn.parts.append(part)
            self._emit(target, "part", {"part": part})
        return target is not None

    async def _relay(self, live: LiveTurn, message: str) -> None:
        ending: Ending = "failed"
        try:
            async for event in self._kernel.hermes.chat(live.profile, live.turn.hermes_session or "", message):
                ending = self._translate(live, event, ending)
        except asyncio.CancelledError:
            ending = "stopped"
        except HermesError as err:
            log.error("turn %s: %s", live.turn.id, err)
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
            self._say(live, data["delta"])
        elif name == "assistant.commentary" and data.get("text"):
            # What huvu says beside its tool calls is part of its answer: the
            # first thing the person sees while the tools run. Hermes has
            # sometimes sent it as deltas already.
            if not data.get("already_streamed"):
                self._say(live, data["text"])
            live.paragraph_due = True
        elif name == "tool.started":
            self._tool_started(live, data.get("tool_name") or "", data.get("args") or {})
        elif name in ("tool.completed", "tool.failed"):
            self._tool_completed(live, data.get("tool_name") or "", data.get("preview"), failed=name == "tool.failed")
        elif name == "assistant.completed" and data.get("content") and not live.turn.answer:
            # A turn that streamed no deltas still has its final text here.
            live.turn.answer = data["content"]
            self._emit(live, "answer.delta", {"text": data["content"]})
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

    def _say(self, live: LiveTurn, text: str) -> None:
        if live.paragraph_due and live.turn.answer.strip():
            text = "\n\n" + text.lstrip()
        live.paragraph_due = False
        live.turn.answer += text
        self._emit(live, "answer.delta", {"text": text})

    def _emit(self, live: LiveTurn, name: str, data: dict[str, Any]) -> None:
        payload = {"turn": live.turn.id, "conversation": live.turn.conversation_id, **data}
        live.history.append((name, payload))
        for queue in live.subscribers:
            queue.put_nowait((name, payload))
        self._kernel.push.send(name, payload, [live.turn.conversation_id])

    def _end(self, live: LiveTurn, ending: Ending) -> None:
        if live.ended:
            return
        live.turn.ending = ending
        live.turn.running = False
        self._save(live.turn, insert=False)
        if live.turn.hermes_session:
            self._kernel.sessions.touch(live.turn.hermes_session)
        self._emit(live, "turn.ended", {"ending": ending, "answer": live.turn.answer})
        self._log(live)
        live.ended = True
        for queue in live.subscribers:
            queue.put_nowait(None)
        self._live.pop(live.turn.id, None)

    def _log(self, live: LiveTurn) -> None:
        """The turn in the event log, as its user's, so their dream can read
        what was said, even at a household device whose Hermes session
        lives in the household's profile."""
        turn = live.turn
        answer = turn.answer.strip().replace("\n", " ")
        self._kernel.events.append(
            Event(
                at=self._kernel.now(),
                source="kernel",
                type="conversation.turn",
                entity=turn.id,
                summary=f"asked {turn.ask[:TURN_ASK_CHARS]!r}; huvu said {answer[:TURN_ANSWER_CHARS]!r}" + (f" ({turn.ending})" if turn.ending else ""),
                refs={"turn": f"kernel:turn/{turn.id}"},
                data={"device": turn.conversation_id, "session": turn.hermes_session, "usage": live.usage},
                labels=["owner"],
                user=live.user,
                group=None if live.user else live.group,
                device=turn.conversation_id,
            )
        )

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


def _note(action: Action, tz: tzinfo | None) -> str:
    when = f"{action.at.astimezone(tz):%a %Y-%m-%d %H:%M}"
    return f"(Owner action in the app, {when}: {action.text}. Not a request, no reply needed.)"


def _with_notes(text: str, notes: list[Action], tz: tzinfo | None) -> str:
    lines = [f"(huvu, {n.at.astimezone(tz):%H:%M}: {n.text}. Not a request, no reply needed.)" for n in notes]
    return "\n".join([*lines, "", text]) if lines else text


def _with_actions(text: str, actions: list[Action], tz: tzinfo | None) -> str:
    if not actions:
        return text
    return "\n".join([*(_note(a, tz) for a in actions), "", text])


def _seeded(kept: list[Turn], text: str, tz: tzinfo | None) -> str:
    """The first message of a fresh session that continues a conversation,
    the way a compacted session continues: what was asked and answered so
    far, then the new message. The model is told to carry on as if nothing
    happened, not to acknowledge it."""
    if not kept:
        return text
    lines = [COMPACTED, "", "<earlier>"]
    for turn in kept:
        lines += [_note(a, tz) for a in turn.actions]
        lines.append(f"Them: {turn.ask}")
        if turn.answer:
            lines.append(f"You: {turn.answer}")
    lines += ["</earlier>", "", text]
    return "\n".join(lines)


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
