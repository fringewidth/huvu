"""The router: which model answers each ask in a conversation.

Clef-flash, Cloudflare's decision model on Workers AI, answers one
yes-or-no question about each ask (`ESCALATE_QUESTION` in prompts.py):
does it need anything said earlier in the conversation?

1. **Basic**, for a one-off: below `router.escalate_at` (Clef-flash's
   probability of yes), the ask runs on basic at `router.effort` in a
   Hermes session of its own, with the ask alone, used once.
1. **Pro**, for an ask that needs what came before, in the conversation's
   main session. Also when Clef-flash errs, takes longer than
   `router.timeout`, or there are no credentials (CLOUDFLARE_ACCOUNT_ID and
   CLOUDFLARE_AI_TOKEN), and every ask when the router is off.

Clef-flash gives the same answer for the same text, so its answers are kept
by the ask's words, and a repeated ask never waits for it twice. Workers
AI's free 10,000 Neurons a day cover about 2.9 million of its tokens,
thousands of asks.

The main session hears of every turn it didn't run on its next ask (see
conversations.py), so its prompt cache holds. Every turn's route goes into
its `chat_turns` row, with Clef-flash's probability when it was asked, so
real asks show where the router falls short.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import httpx

from . import prompts
from .config import ModelChoice

if TYPE_CHECKING:
    from .kernel import Kernel

WORKERS_AI = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/clef-flash"
# How many of Clef-flash's answers are kept, by the ask's words.
CACHE_SIZE = 2000
# How long the connection to Workers AI stays open between asks, in seconds.
KEEPALIVE = 300.0

log = logging.getLogger("huvu.routing")


# "one_off" when Clef-flash said the ask needs nothing said before it,
# "context" when it said it does, "failed" when it erred, took too long or
# has no credentials, "off" when the router is turned off, and "notes" when
# notes wait for the conversation, such as a notification the ask may answer.
Why = Literal["one_off", "context", "failed", "off", "notes"]


@dataclass(frozen=True)
class Route:
    """The model that answers an ask: basic alone, or pro in the main
    session."""

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

    async def route(self, ask: str, *, notes: bool = False) -> Route:
        """`notes`: notes wait for the conversation's next ask, such as a
        notification on the person's phone. The ask may answer one, and
        only the main session reads them, so it goes there without asking
        Clef-flash, which sees the ask alone."""
        config = self._kernel.config
        if not config.router_enabled:
            return self._on("pro", "off")
        if notes:
            return self._on("pro", "notes")
        p = await self._needs_context(ask)
        if p is None:
            return self._on("pro", "failed")
        if p < config.router_escalate_at:
            return self._on("basic", "one_off", p)
        return self._on("pro", "context", p)

    def _on(self, tier: Literal["basic", "pro"], why: Why, p: float | None = None) -> Route:
        config = self._kernel.config
        effort = config.router_effort if tier == "basic" else None
        return Route(tier, config.model(tier), effort, why, p)

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


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()
