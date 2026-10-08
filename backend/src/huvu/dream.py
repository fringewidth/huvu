"""The dream: each person's agent, woken every night to look back on the day.

It is an ordinary trigger, one per user, that the kernel sets up itself:
`clock.time` at config.dream_at, daily, on config.dream_tier, with a
script that only wakes the model. Its note is the dream prompt, so the
woken session reads it first. The dream ends with a brief to its person,
a few short notes through huvu_notify.

A user's dream is set up once. Their trigger list shows it like any other,
so they can pause it or remove it, and a removed dream stays removed: the
kv entry remembers it was made.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .prompts import DREAM_NOTE, DREAM_SCRIPT
from .spec import Caller
from .triggers import TriggerSpec

if TYPE_CHECKING:
    from .kernel import Kernel

NAMESPACE = "dream"


class Dreams:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel

    def spec(self) -> TriggerSpec:
        config = self._kernel.config
        return TriggerSpec.model_validate(
            {
                "note": DREAM_NOTE,
                "when": [{"value": "clock.time", "op": "==", "arg": config.dream_at}],
                "repeat": "daily",
                "script": DREAM_SCRIPT,
                "tier": config.dream_tier,
            }
        )

    async def ensure(self) -> None:
        """Sets up the dream of every user who never had one."""
        db = self._kernel.db
        for user in self._kernel.accounts.users():
            if db.execute("SELECT 1 FROM kv WHERE namespace = ? AND key = ?", (NAMESPACE, user.id)).fetchone():
                continue
            trigger = await self._kernel.triggers.register(self.spec(), Caller(kind="kernel", user=user.id))
            db.execute("INSERT INTO kv (namespace, key, value) VALUES (?, ?, ?)", (NAMESPACE, user.id, f'"{trigger.id}"'))
