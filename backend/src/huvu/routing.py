"""The router: how each ask in a conversation is answered.

1. **Direct**, with no model. "turn on <device>", "switch on <device>",
   "turn off <device>", "switch off <device>", "start <device>" and "stop
   <device>", "the" before the device allowed and nothing else around it.
   The device is found by its name, an alias, or its area or device before
   the name or left off it, and may carry a typo ("night ksy"), but only
   when exactly one thing that takes the verb is that close and nothing
   else is near: a wrong guess costs more than the seconds a model takes.
   A thing takes on and off when its action's only argument is `on`, and
   start and stop when its only argument is a `command` with both among its
   options (a vacuum). An action that asks first goes to a model instead,
   which can say what happens next; one that runs silently or tells goes
   direct, and the policy sends the notice.
1. **Basic**, for a one-off ask. Clef-flash, Cloudflare's decision model on
   Workers AI, answers one yes-or-no question (`ESCALATE_QUESTION` in
   prompts.py): does the ask need anything said earlier in the
   conversation? Below `router.escalate_at` (its probability of yes), the
   ask runs on basic at `router.effort` in a Hermes session of its own,
   with the ask alone, used once.
1. **Pro**, for an ask that needs what came before, in the conversation's
   main session. Also when Clef-flash errs, takes longer than
   `router.timeout`, or there are no credentials (CLOUDFLARE_ACCOUNT_ID and
   CLOUDFLARE_AI_TOKEN), and every ask when the router is off.

Clef-flash gives the same answer for the same text, so its answers are kept
by the ask's words, and a repeated ask never waits for it twice. Workers
AI's free 10,000 Neurons a day cover about 2.9 million of its tokens,
thousands of asks.

The main session hears of every turn it didn't run, direct or on basic, on
its next ask (see conversations.py), so its prompt cache holds. Every
turn's route goes into its `chat_turns` row, with Clef-flash's probability
when it was asked, so real asks show where the router falls short.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import httpx

from . import prompts
from .config import ModelChoice
from .spec import Caller

if TYPE_CHECKING:
    from .kernel import Kernel

# The verbs of a direct ask: each "turn" or "switch" verb takes on or off
# after it, and a command verb is the option it sends.
SWITCH_VERBS = frozenset("turn switch".split())
COMMAND_VERBS = frozenset("start stop".split())
# How far apart, in characters, other devices' names must stay beyond the
# typos a name is allowed: any closer and the ask is ambiguous.
MARGIN = 2

WORKERS_AI = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/clef-flash"
# How many of Clef-flash's answers are kept, by the ask's words.
CACHE_SIZE = 2000
# How long the connection to Workers AI stays open between asks, in seconds.
KEEPALIVE = 300.0

log = logging.getLogger("huvu.routing")


@dataclass(frozen=True)
class Switchable:
    """A thing huvu can work by itself, and every way an ask may name it."""

    tool: str
    name: str
    # Lowercased and split by words.
    phrases: tuple[str, ...]
    # "on" and "off", or "start" and "stop".
    verbs: frozenset[str]


@dataclass(frozen=True)
class Command:
    """What huvu does itself for a direct ask."""

    tool: str
    name: str
    # "on", "off", "start" or "stop".
    verb: str

    @property
    def args(self) -> dict[str, Any]:
        return {"on": self.verb == "on"} if self.verb in ("on", "off") else {"command": self.verb}


@dataclass(frozen=True)
class Direct:
    """huvu works the ask itself, with no model."""

    command: Command

    def row(self) -> dict[str, Any]:
        return {"tier": "direct", "model": None, "effort": None, "why": "direct", "p": None}


# "one_off" when Clef-flash said the ask needs nothing said before it,
# "context" when it said it does, "failed" when it erred, took too long or
# has no credentials, and "off" when the router is turned off.
Why = Literal["one_off", "context", "failed", "off"]


@dataclass(frozen=True)
class OnModel:
    """A model answers the ask: basic alone, or pro in the main session."""

    tier: Literal["basic", "pro"]
    choice: ModelChoice
    # The reasoning effort to ask Hermes for; None keeps the profile's.
    effort: str | None
    why: Why
    # Clef-flash's probability that the ask needs earlier context, when it
    # was asked.
    p: float | None = None

    @property
    def key(self) -> str:
        """What a session's prompt cache depends on: the main session runs
        on one route for good."""
        return f"{self.choice.model}|{self.choice.provider or ''}|{self.effort or ''}"

    def row(self) -> dict[str, Any]:
        return {"tier": self.tier, "model": self.choice.model, "effort": self.effort, "why": self.why, "p": self.p}


Route = Direct | OnModel


class Router:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        config = kernel.config
        self._account = config.cloudflare_account_id
        self._token = config.cloudflare_ai_token
        # Kept open between asks: a new TLS handshake to Cloudflare costs
        # about as much as Clef-flash's answer. `route` holds each call to
        # router.timeout as a whole.
        self._http = httpx.AsyncClient(limits=httpx.Limits(keepalive_expiry=KEEPALIVE))
        # Clef-flash's answers, by the ask's words.
        self._answers: OrderedDict[str, float] = OrderedDict()

    async def close(self) -> None:
        await self._http.aclose()

    async def warm(self) -> None:
        """Opens the connection to Workers AI, so the first ask doesn't pay
        for the handshake. A GET is refused, and that's fine: only the
        connection matters."""
        if self._account and self._token:
            try:
                await self._http.get(WORKERS_AI.format(account=self._account), headers={"Authorization": f"Bearer {self._token}"}, timeout=5.0)
            except httpx.HTTPError:
                pass

    async def route(self, ask: str, caller: Caller) -> Route:
        """`caller` is who the ask is from, for the autonomy of what a
        direct ask would work."""
        config = self._kernel.config
        if not config.router_enabled:
            return self._on("pro", "off")
        command = direct(ask, self.switchables(caller))
        if command is not None:
            return Direct(command)
        p = await self._needs_context(ask)
        if p is None:
            return self._on("pro", "failed")
        if p < config.router_escalate_at:
            return self._on("basic", "one_off", p)
        return self._on("pro", "context", p)

    def _on(self, tier: Literal["basic", "pro"], why: Why, p: float | None = None) -> OnModel:
        config = self._kernel.config
        effort = config.router_effort if tier == "basic" else None
        return OnModel(tier, config.model(tier), effort, why, p)

    async def _needs_context(self, ask: str) -> float | None:
        """Clef-flash's probability that the ask needs anything said before
        it, or None when it can't say in time."""
        key = " ".join(words(ask))
        if key in self._answers:
            self._answers.move_to_end(key)
            return self._answers[key]
        if not (self._account and self._token):
            return None
        body = {
            "model": "clef-flash",
            "state": {"ask": ask},
            "questions": {"escalate": {"type": "noul", "instructions": prompts.ESCALATE_QUESTION, "criteria": prompts.ESCALATE_CRITERIA}},
        }
        try:
            async with asyncio.timeout(self._kernel.config.router_timeout):
                res = await self._http.post(WORKERS_AI.format(account=self._account), headers={"Authorization": f"Bearer {self._token}"}, json=body)
            res.raise_for_status()
            p = float(res.json()["result"]["answers"]["escalate"]["noul"])
        except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError) as err:
            log.warning("Clef-flash couldn't say, so the ask runs on pro: %r", err)
            return None
        self._answers[key] = p
        if len(self._answers) > CACHE_SIZE:
            self._answers.popitem(last=False)
        return p

    def switchables(self, caller: Caller) -> list[Switchable]:
        """What huvu can work by itself now for `caller`: every live row
        with an action that takes on and off or start and stop, and doesn't
        ask first."""
        found: list[Switchable] = []
        for spec in self._kernel.registry.specs():
            for table in spec.tables:
                if not table.live or table.column("action") is None:
                    continue
                for row in self._kernel.data.live_rows(spec.name, table.name):
                    tool = row.get("action")
                    action = spec.action(tool.removeprefix(f"{spec.name}_")) if isinstance(tool, str) else None
                    verbs = _verbs(action.input_schema) if action is not None else None
                    if action is None or verbs is None or self._kernel.policy.level(spec.name, action, caller) == "ask":
                        continue
                    found.append(Switchable(tool, str(row.get("name") or tool), _phrases(row), verbs))
        return found


def direct(ask: str, switchables: list[Switchable]) -> Command | None:
    """The command of a direct ask, when exactly one thing that takes its
    verb is close to the name it gives, and nothing else is."""
    parsed = _parse(ask)
    if parsed is None:
        return None
    verb, name = parsed
    takers = [s for s in switchables if verb in s.verbs and s.phrases]
    allowed = _typos(name)
    distances = sorted((min(_distance(name, p) for p in s.phrases), i) for i, s in enumerate(takers))
    if not distances or distances[0][0] > allowed:
        return None
    if len(distances) > 1 and distances[1][0] <= allowed + MARGIN:
        return None
    best = takers[distances[0][1]]
    return Command(best.tool, best.name, verb)


def _parse(ask: str) -> tuple[str, str] | None:
    """The verb and the device's name of "turn on <device>", "switch off
    <device>", "start <device>" or "stop <device>", "the" before the device
    allowed, and None for anything else."""
    tokens = words(ask)
    if len(tokens) >= 3 and tokens[0] in SWITCH_VERBS and tokens[1] in ("on", "off"):
        verb, name = tokens[1], tokens[2:]
    elif len(tokens) >= 2 and tokens[0] in COMMAND_VERBS:
        verb, name = tokens[0], tokens[1:]
    else:
        return None
    if name[0] == "the":
        name = name[1:]
    return (verb, " ".join(name)) if name else None


def _typos(name: str) -> int:
    """How many typos a name may carry: none in a short one."""
    if len(name) < 5:
        return 0
    return 1 if len(name) < 12 else 2


def _distance(a: str, b: str) -> int:
    """Edits between two strings, a swap of two neighbouring letters
    counting as one (optimal string alignment)."""
    rows = [list(range(len(b) + 1))]
    for i in range(1, len(a) + 1):
        row = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            row[j] = min(rows[-1][j] + 1, row[j - 1] + 1, rows[-1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                row[j] = min(row[j], rows[-2][j - 2] + 1)
        rows.append(row)
    return rows[-1][-1]


def _verbs(schema: dict[str, Any]) -> frozenset[str] | None:
    """The verbs an action takes: on and off when its only required
    argument is `on`, start and stop when it is a `command` with both among
    its options, and None otherwise."""
    required = list(schema.get("required") or [])
    argument = (schema.get("properties") or {}).get(required[0]) if len(required) == 1 else None
    verbs = None
    if required == ["on"] and (argument or {}).get("type") == "boolean":
        verbs = frozenset({"on", "off"})
    elif required == ["command"] and COMMAND_VERBS <= set((argument or {}).get("enum") or []):
        verbs = COMMAND_VERBS
    return verbs


def _phrases(row: dict[str, Any]) -> tuple[str, ...]:
    """Every way an ask may name a row: its name and aliases, with its area
    or device before the name, or left off it."""
    name = " ".join(words(str(row.get("name") or "")))
    found = {name, *(" ".join(words(a)) for a in _texts(row.get("aliases")))}
    for place in (row.get("area"), row.get("device")):
        place = " ".join(words(place)) if isinstance(place, str) else ""
        if not place or not name:
            continue
        if name.startswith(place + " "):
            found.add(name.removeprefix(place + " "))
        else:
            found.add(f"{place} {name}")
    return tuple(sorted(p for p in found if p))


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()


def _texts(value: Any) -> list[str]:
    """A name column's text: a string, or a list of them (aliases), or
    either as JSON."""
    if isinstance(value, str) and value.startswith("["):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []
