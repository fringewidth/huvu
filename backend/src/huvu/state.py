"""State: the values sources declare, read through one place.

The kernel holds no copy of state beyond what sources push and triggers
watch. A pushed value is answered from the last reading; an `on_demand`
value is asked of its source each time. Every reading carries its time, so
the model and the trigger engine can see how old it is.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .names import UnknownName, ValueRef
from .source import SourceError
from .spec import Match, Reading, ValueSpec

if TYPE_CHECKING:
    from .kernel import Kernel

log = logging.getLogger("huvu.state")

Watcher = Callable[[ValueRef, Reading], Awaitable[None]]

SEARCH_LIMIT = 15


class StateService:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        self._cache: dict[ValueRef, Reading] = {}
        self._watchers: dict[ValueRef, list[Watcher]] = {}

    async def update(self, source: str, value: str, params: dict[str, Any], reading: Reading) -> None:
        ref, _ = self._kernel.registry.resolve_value(f"{source}.{value}", params)
        self._cache[ref] = reading
        for watcher in list(self._watchers.get(ref, [])):
            try:
                await watcher(ref, reading)
            except Exception:
                log.exception("watcher of %s failed", ref)

    async def get(self, keys: list[str]) -> dict[str, Reading]:
        """Reads each key. A key that fails gets a reading with the reason,
        so one bad name doesn't sink the rest."""
        out: dict[str, Reading] = {}
        for key in keys:
            try:
                ref, spec = self._kernel.registry.resolve_value(key)
                out[key] = await self.read(ref, spec)
            except (UnknownName, SourceError) as err:
                out[key] = Reading(reason=str(err))
        return out

    async def read(self, ref: ValueRef, spec: ValueSpec) -> Reading:
        cached = self._cache.get(ref)
        if spec.updates == "on_demand" or cached is None:
            reading = await self._kernel.registry.get(ref.source).read(ref.name, ref.params_dict)
        else:
            reading = cached
        return reading

    def cached(self, ref: ValueRef) -> Reading | None:
        return self._cache.get(ref)

    async def watch(self, key: str | ValueRef, watcher: Watcher) -> ValueRef:
        ref = key if isinstance(key, ValueRef) else self._kernel.registry.resolve_value(key)[0]
        first = ref not in self._watchers
        self._watchers.setdefault(ref, []).append(watcher)
        if first:
            await self._kernel.registry.get(ref.source).watch(ref.name, ref.params_dict)
        return ref

    def unwatch(self, ref: ValueRef, watcher: Watcher) -> None:
        watchers = self._watchers.get(ref, [])
        if watcher in watchers:
            watchers.remove(watcher)
        if not watchers:
            self._watchers.pop(ref, None)

    async def search(self, text: str) -> list[Match]:
        """Aliases first, then every source's own things, then kinds of value
        whose name or description matches."""
        found: list[Match] = list(await self._kernel.huvu.search(text))
        for name in self._kernel.registry.names:
            if name == "huvu":
                continue
            try:
                found.extend(await self._kernel.registry.get(name).search(text))
            except Exception:
                log.exception("search in %s failed", name)
        words = text.lower().split()
        for spec in self._kernel.registry.specs():
            for value in spec.values:
                hay = f"{spec.name}.{value.name} {value.description}".lower()
                if value.params or not all(w in hay for w in words):
                    continue
                reading = self._cache.get(ValueRef(spec.name, value.name))
                found.append(Match(value=f"{spec.name}.{value.name}", name=value.description, now=reading.value if reading else None))
        return _dedupe(found)[:SEARCH_LIMIT]


def is_fresh(reading: Reading | None, spec: ValueSpec, now: datetime) -> bool:
    """A reading counts for triggers only if it exists, has a value, and is
    younger than its value's max age."""
    ok = reading is not None and reading.value is not None
    if ok and spec.max_age is not None and reading.at is not None:
        ok = now - reading.at <= spec.max_age
    return ok


def _dedupe(matches: list[Match]) -> list[Match]:
    seen: set[str] = set()
    out: list[Match] = []
    for m in matches:
        if m.value not in seen:
            seen.add(m.value)
            out.append(m)
    return out
