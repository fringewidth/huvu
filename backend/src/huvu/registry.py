"""The registry: every loaded source, its spec, and action names.

An action name a model writes resolves here. An unknown one fails with the
nearest real one in the message, so a small model can fix its call on the
next try.
"""

from __future__ import annotations

import asyncio
import difflib
import importlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .source import Source, SourceContext, SourceError
from .spec import ActionSpec, SourceSpec

if TYPE_CHECKING:
    from .kernel import Kernel

log = logging.getLogger("huvu.registry")

RESTART_FIRST = 5.0
RESTART_MAX = 300.0
# A run at least this long counts as healthy, and resets the backoff.
HEALTHY_RUN = 60.0


class UnknownName(ValueError):
    """An unknown or malformed name. The message names the nearest real
    one, so a small model can fix its call on the next try."""


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
        data_dir = self._kernel.config.data_dir / "sources" / name
        data_dir.mkdir(parents=True, exist_ok=True)
        # Bound first, so `describe` can read the source's own folder, such
        # as a cache of what it offered before the last restart.
        source.bind(SourceContext(self._kernel, name, data_dir))
        self._sources[name] = Loaded(source, _described(name, source))

    def respec(self, name: str) -> tuple[SourceSpec, SourceSpec]:
        """Describes a loaded source again. Returns the old spec and the
        new one."""
        loaded = self._loaded(name)
        old = loaded.spec
        loaded.spec = _described(name, loaded.source)
        return old, loaded.spec

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
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            try:
                await loaded.source.run()
                break
            except asyncio.CancelledError:
                raise
            except Exception as err:
                log.exception("source %s crashed", name)
                # A source that ran fine for a while and then lost its
                # connection starts again from the first delay, so a
                # Home Assistant restart once a month doesn't cost 5 min.
                if loop.time() - started >= HEALTHY_RUN:
                    delay = RESTART_FIRST
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

    def resolve_action(self, source: str, name: str) -> ActionSpec:
        spec = self.spec(source).action(name)
        if spec is None:
            known = [a.name for a in self.spec(source).actions]
            raise UnknownName(f"{source} has no action {name!r}{suggest(name, known)}")
        return spec

    def index(self) -> str:
        """Every table, one per line, for the model. It changes only when a
        source is added, so it stays in the prompt cache."""
        lines: list[str] = []
        for spec in self.specs():
            for table in spec.tables:
                kind = "now" if table.live else "history"
                lines.append(f"{spec.name}_{table.name}  ({kind}) {table.description}")
        return "\n".join(lines)


def _described(name: str, source: Source) -> SourceSpec:
    spec = source.describe().model_copy(update={"name": name})
    # Rebuilt so the name passes the same check a driver's own name does.
    return SourceSpec.model_validate(spec.model_dump())


def suggest(name: str, known: list[str]) -> str:
    close = difflib.get_close_matches(name, known, n=1, cutoff=0.5)
    return f". Did you mean {close[0]}?" if close else ""
