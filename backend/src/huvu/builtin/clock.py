"""The clock, as a source, so time can be one condition inside a data
trigger: the porch light's 00:00 to 05:00 window. Time-only schedules are
Hermes cron jobs, not triggers, so the clock never wakes anyone alone.

It pushes on every minute, which is all the trigger engine needs to open
and close windows.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from ..source import Source
from ..spec import Reading, SourceSpec, ValueSpec

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class ClockSource(Source):
    def describe(self) -> SourceSpec:
        return SourceSpec(
            name="clock",
            description="Local time, for conditions inside triggers.",
            values=[
                ValueSpec(name="time", description="Local time of day as HH:MM", type="time"),
                ValueSpec(name="weekday", description="Day of the week: mon, tue, wed, thu, fri, sat, sun", type="string"),
                ValueSpec(name="date", description="Local date as YYYY-MM-DD", type="string"),
            ],
        )

    async def run(self) -> None:
        while True:
            now = self.ctx.now()
            await self._push(now)
            following = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
            await asyncio.sleep(max(0.0, (following - self.ctx.now()).total_seconds()))

    async def _push(self, now: datetime) -> None:
        for name, value in _values(now).items():
            await self.ctx.update(name, value, at=now)

    async def read(self, value: str, params: dict[str, Any]) -> Reading:
        now = self.ctx.now()
        return Reading(value=_values(now)[value], at=now)


def _values(now: datetime) -> dict[str, str]:
    return {"time": f"{now:%H:%M}", "weekday": WEEKDAYS[now.weekday()], "date": f"{now:%Y-%m-%d}"}
