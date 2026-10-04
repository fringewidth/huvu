"""The registry: every loaded source, its spec, and name resolution.

Every name a model or a trigger writes (a value key, an event type, a query,
an action) resolves here. An unknown name fails with the nearest real one in
the message, so a small model can fix its call on the next try.
"""

from __future__ import annotations

import asyncio
import difflib
import importlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .names import UnknownName, ValueRef, make_ref, split_key
from .source import Source, SourceContext, SourceError
from .spec import ActionSpec, EventSpec, QuerySpec, SourceSpec, ValueSpec

if TYPE_CHECKING:
    from .kernel import Kernel

log = logging.getLogger("huvu.registry")

RESTART_FIRST = 5.0
RESTART_MAX = 300.0


@dataclass
class Loaded:
    source: Source
    spec: SourceSpec
    task: asyncio.Task[None] | None = None


class Registry:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        self._sources: dict[str, Loaded] = {}

    def add(self, name: str, source: Source) -> None:
        if name in self._sources:
            raise ValueError(f"two sources are named {name!r}")
        spec = source.describe().model_copy(update={"name": name})
        # Rebuilt so the name passes the same check a driver's own name does.
        spec = SourceSpec.model_validate(spec.model_dump())
        data_dir = self._kernel.config.data_dir / "sources" / name
        data_dir.mkdir(parents=True, exist_ok=True)
        source.bind(SourceContext(self._kernel, name, data_dir))
        self._sources[name] = Loaded(source, spec)

    def load(self, name: str, target: str, options: dict[str, Any]) -> None:
        """Loads a driver from `module:Class`, as the config names it."""
        module_name, _, class_name = target.partition(":")
        if not class_name:
            raise ValueError(f"source {name!r}: {target!r} must look like package.module:Class")
        cls = getattr(importlib.import_module(module_name), class_name)
        self.add(name, cls(options))

    async def start(self) -> None:
        for name, loaded in self._sources.items():
            loaded.task = asyncio.create_task(self._supervise(name, loaded), name=f"source:{name}")

    async def stop(self) -> None:
        tasks = [l.task for l in self._sources.values() if l.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _supervise(self, name: str, loaded: Loaded) -> None:
        """Runs a source's `run`, and starts it again after a backoff when it
        raises. A `run` that returns is done for good."""
        delay = RESTART_FIRST
        while True:
            try:
                await loaded.source.run()
                break
            except asyncio.CancelledError:
                raise
            except Exception as err:
                log.exception("source %s crashed", name)
                await self._kernel.kernel_event(
                    "source_error",
                    f"{name} stopped with an error and restarts in {int(delay)} s: {err}",
                    entity=name,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, RESTART_MAX)

    @property
    def names(self) -> list[str]:
        return list(self._sources)

    def get(self, name: str) -> Source:
        return self._loaded(name).source

    def spec(self, name: str) -> SourceSpec:
        return self._loaded(name).spec

    def specs(self) -> list[SourceSpec]:
        return [l.spec for l in self._sources.values()]

    def _loaded(self, name: str) -> Loaded:
        loaded = self._sources.get(name)
        if loaded is None:
            raise SourceError(f"no source named {name!r}{suggest(name, self.names)}")
        return loaded

    def resolve_value(self, key: str, params: dict[str, Any] | None = None) -> tuple[ValueRef, ValueSpec]:
        """Resolves a key (`ha.entity_state[light.porch]`), or a qualified
        name plus params, to a ref and the value's spec."""
        raw = split_key(key)
        spec = self._value_spec(raw.source, raw.name)
        given: dict[str, Any] = {**raw.named, **(params or {})}
        if raw.positional is not None:
            if len(spec.params) != 1:
                raise UnknownName(f"{key!r}: {raw.source}.{raw.name} takes params {_param_list(spec)}; name them as [param=value]")
            given[next(iter(spec.params))] = raw.positional
        missing = [p for p in spec.params if p not in given]
        extra = [p for p in given if p not in spec.params]
        if missing or extra:
            raise UnknownName(f"{key!r}: {raw.source}.{raw.name} takes params {_param_list(spec)}")
        return make_ref(raw.source, raw.name, given), spec

    def _value_spec(self, source: str, name: str) -> ValueSpec:
        loaded = self._sources.get(source)
        spec = loaded.spec.value(name) if loaded else None
        if spec is None:
            known = [f"{s.name}.{v.name}" for s in self.specs() for v in s.values]
            raise UnknownName(f"unknown value {source}.{name}{suggest(f'{source}.{name}', known)}")
        return spec

    def resolve_event(self, qualified: str) -> EventSpec:
        source, _, type_ = qualified.partition(".")
        loaded = self._sources.get(source)
        spec = loaded.spec.event(type_) if loaded else None
        if spec is None:
            known = [f"{s.name}.{e.type}" for s in self.specs() for e in s.events]
            raise UnknownName(f"unknown event {qualified}{suggest(qualified, known)}")
        return spec

    def resolve_query(self, source: str, name: str) -> QuerySpec:
        spec = self.spec(source).query(name)
        if spec is None:
            known = [q.name for q in self.spec(source).queries]
            raise UnknownName(f"{source} has no query {name!r}{suggest(name, known)}")
        return spec

    def resolve_action(self, source: str, name: str) -> ActionSpec:
        spec = self.spec(source).action(name)
        if spec is None:
            known = [a.name for a in self.spec(source).actions]
            raise UnknownName(f"{source} has no action {name!r}{suggest(name, known)}")
        return spec

    def index(self) -> str:
        """Every kind of value, one per line, for the model. It changes only
        when a source is added, so it stays in the prompt cache."""
        lines: list[str] = []
        for spec in self.specs():
            for value in spec.values:
                params = f"[{','.join(value.params)}]" if value.params else ""
                unit = f", {value.unit}" if value.unit else ""
                lines.append(f"{spec.name}.{value.name}{params}  {value.description} ({value.updates}{unit})")
        return "\n".join(lines)


def suggest(name: str, known: list[str]) -> str:
    close = difflib.get_close_matches(name, known, n=1, cutoff=0.5)
    return f". Did you mean {close[0]}?" if close else ""


def _param_list(spec: ValueSpec) -> str:
    return ", ".join(spec.params) or "none"
