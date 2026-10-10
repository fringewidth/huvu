"""The router: which tier answers each ask in a conversation.

Most asks are plain house control (turn on the projector, AC to 24), and pro
thinks before every one of them. Basic with no thinking does those just as
well (benchmarked on the home theatre on 2026-10-10: 18 of 20 direct and 18
of 20 multi-step commands), so an ask that is plain house control runs on
basic at `router.effort`, and every other ask on pro.

Plain house control is a whitelist, so the rule is deterministic and a gap
in it only costs time: every word of the ask must be one it knows. An
unknown word sends the ask to pro, so anything for later ("at midnight"), a
question ("why"), a hint ("it's stuffy") or a typo goes there without being
listed. The words it knows:

1. The house's names: every word of the name, aliases, area and device of
   each live row huvu can work (one with an `action`, such as `ha_entities`
   and `ha_scripts`), and the options of their fields, such as the apps the
   play script takes. Read on every ask, so a new device needs no change
   here.
1. The verbs, particles, read words, units and fillers below, the same for
   every house.
1. A number, but only right after to, by, up or down, or right before a
   unit, so "turn on the AC in 10" isn't one.
1. Any title in "play <title> on <app>", when the app is one of the house's
   options.

It also needs a word of the house's names that isn't one of its own, and a
verb, particle or read word.

Every turn's route goes into its `chat_turns` row, so real asks show where
the whitelist falls short.

A model, a provider and an effort each have their own prompt cache, so the
conversation starts a fresh session whenever its route changes, primed with
the transcript the way a cold one is (see conversations.py). Routes only
escalate: a session moves from basic to pro, never back, and the router
isn't asked while it is on pro. A fresh session, after the cache went cold
or an edit, is routed from the start again.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .config import ModelChoice

if TYPE_CHECKING:
    from .kernel import Kernel

VERBS = frozenset("turn switch set put start stop pause resume dock send play mute unmute dim lower raise kill flip toggle make open close".split())
PARTICLES = frozenset("on off up down to by back".split())
READS = frozenset("is are what's whats".split())
UNITS = frozenset("degrees degree percent steps step notches notch bit little more less".split())
FILLER = frozenset(
    "please pls plz can could would will you hey huvu the a an my me for now uh um like yeah just and then also too both all "
    "it them its in here of ok okay so".split()
)
GRAMMAR = VERBS | PARTICLES | READS | UNITS | FILLER
# The words a number may follow.
NUMBER_AFTER = frozenset("to by up down".split())
# The columns of a row huvu can work that name it.
NAME_COLUMNS = ("name", "aliases", "area", "device")

Why = Literal["control", "other", "kept", "off"]


@dataclass(frozen=True)
class Route:
    tier: Literal["basic", "pro"]
    choice: ModelChoice
    # The reasoning effort to ask Hermes for; None keeps the profile's.
    effort: str | None
    # "control" when the ask was plain house control, "other" when it
    # wasn't, "kept" when its session was on pro already and the router
    # wasn't asked, and "off" when the router is turned off.
    why: Why

    @property
    def key(self) -> str:
        """What a session's prompt cache depends on: a session runs on one
        route for good."""
        return f"{self.choice.model}|{self.choice.provider or ''}|{self.effort or ''}"

    def row(self) -> dict[str, Any]:
        return {"tier": self.tier, "model": self.choice.model, "effort": self.effort, "why": self.why}


@dataclass(frozen=True)
class Vocabulary:
    # Every word of the house's names, numbers left out.
    names: frozenset[str]
    # The options its actions' fields take, such as "prime video".
    options: tuple[str, ...]


class Router:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel

    def route(self, ask: str) -> Route:
        config = self._kernel.config
        if not config.router_enabled:
            return self._pro("off")
        if plain_control(ask, self.vocabulary()):
            return Route("basic", config.model("basic"), config.router_effort, "control")
        return self._pro("other")

    def stay_on_pro(self) -> Route:
        """The route of an ask in a session already on pro: routes only
        escalate, so it stays there."""
        return self._pro("kept")

    def _pro(self, why: Why) -> Route:
        return Route("pro", self._kernel.config.model("pro"), None, why)

    def vocabulary(self) -> Vocabulary:
        """The house's names now, from every live row huvu can work."""
        names: set[str] = set()
        options: set[str] = set()
        for spec in self._kernel.registry.specs():
            for table in spec.tables:
                if not table.live or table.column("action") is None:
                    continue
                for row in self._kernel.data.live_rows(spec.name, table.name):
                    if not row.get("action"):
                        continue
                    for column in NAME_COLUMNS:
                        names.update(w for text in _texts(row.get(column)) for w in words(text) if not w.isdigit())
                    for option in _options(row.get("fields")):
                        options.add(" ".join(words(option)))
        names.update(w for option in options for w in option.split() if not w.isdigit())
        return Vocabulary(frozenset(names), tuple(sorted(o for o in options if o)))


def plain_control(ask: str, vocabulary: Vocabulary) -> bool:
    """Whether every word of `ask` is one the whitelist knows, with a name
    of the house's and a verb, particle or read word among them."""
    text = " ".join(words(ask))
    for app in sorted(vocabulary.options, key=len, reverse=True):
        text = re.sub(rf"\bplay\b .+? \b(?:on|in)\b (?:the )?{re.escape(app)}\b", f"play {app}", text)
    tokens = text.split()
    for i, token in enumerate(tokens):
        if token.isdigit():
            after = i > 0 and tokens[i - 1] in NUMBER_AFTER
            before = i + 1 < len(tokens) and tokens[i + 1] in UNITS
            if not (after or before):
                return False
        elif token not in GRAMMAR and token not in vocabulary.names:
            return False
    named = any(t in vocabulary.names and t not in GRAMMAR for t in tokens)
    acts = any(t in VERBS or t in PARTICLES or t in READS for t in tokens)
    return named and acts


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


def _options(value: Any) -> list[str]:
    """Every option anywhere in an action's fields, such as a select's."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    found: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            if key == "options" and isinstance(inner, list):
                for option in inner:
                    if isinstance(option, str):
                        found.append(option)
                    elif isinstance(option, dict) and isinstance(option.get("value"), str):
                        found.append(option["value"])
                        if isinstance(option.get("label"), str):
                            found.append(option["label"])
            else:
                found += _options(inner)
    elif isinstance(value, list):
        for inner in value:
            found += _options(inner)
    return found
