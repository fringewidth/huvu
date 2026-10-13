"""The dream: each person's agent, woken every night to look back on the day.

It is an ordinary trigger, one per user, that the kernel sets up itself: an
edge on the local minute being config.dream_at, so once a day, on
config.dream_tier, with a script that only wakes the model. Its note is the dream prompt, so the
woken session reads it first. The dream ends with a brief to its person,
a few short notes through huvu_notify.

A user's dream is set up once. Their trigger list shows it like any other,
so they can pause it or remove it, and a removed dream stays removed: the
kv entry remembers it was made. A live dream follows the code: when the
dream's note, script, time or tier change, the next check rewrites it in
place, and it keeps its state.

A dream may run longer than other wakes (config.dream_deadline).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .prompts import DREAM_NOTE, DREAM_SCRIPT
from .spec import Caller
from .times import parse_clock
from .triggers import TriggerSpec

if TYPE_CHECKING:
    from .kernel import Kernel

NAMESPACE = "dream"


class Dreams:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel

    def spec(self) -> TriggerSpec:
        config = self._kernel.config
        at = parse_clock(config.dream_at)
        return TriggerSpec.model_validate(
            {
                "note": DREAM_NOTE,
                "sql": f"SELECT 1 WHERE now_minute() = {at.hour * 60 + at.minute}",
                "fire": "edge",
                "once": False,
                "script": DREAM_SCRIPT,
                "tier": config.dream_tier,
            }
        )

    async def ensure(self) -> None:
        """Sets up the dream of every user who never had one, and brings
        every live one up to date with the code."""
        db = self._kernel.db
        spec = self.spec()
        for user in self._kernel.accounts.users():
            row = db.execute("SELECT value FROM kv WHERE namespace = ? AND key = ?", (NAMESPACE, user.id)).fetchone()
            if row is None:
                trigger = await self._kernel.triggers.register(spec, Caller(kind="kernel", user=user.id))
                db.execute("INSERT INTO kv (namespace, key, value) VALUES (?, ?, ?)", (NAMESPACE, user.id, f'"{trigger.id}"'))
                continue
            live = self._kernel.triggers.get(json.loads(row["value"]))
            if live is not None and live.spec.model_dump() != spec.model_dump():
                await self._kernel.triggers.restate(live.id, spec)

    def is_dream(self, trigger_id: str | None) -> bool:
        if not trigger_id:
            return False
        return self._kernel.db.execute("SELECT 1 FROM kv WHERE namespace = ? AND value = ?", (NAMESPACE, json.dumps(trigger_id))).fetchone() is not None
