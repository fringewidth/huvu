"""Value keys: the one way every part of huvu names a value.

`clock.time` names a value with no params. `ha.entity_state[light.porch]`
names one with a single param, given by position. `presence.room[person=x]`
names params by key, which a value with several params needs. Triggers can
also give params apart from the name, as `{"value": "ha.entity_state",
"params": {"entity_id": "light.porch"}}`. Both forms resolve to the same
ValueRef.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

KEY_RE = re.compile(r"^([a-z][a-z0-9]*)\.([a-z][a-z0-9_]*)(?:\[(.*)\])?$")


class UnknownName(ValueError):
    """An unknown or malformed name. The message names the nearest real
    one, so a small model can fix its call on the next try."""


@dataclass(frozen=True)
class ValueRef:
    source: str
    name: str
    params: tuple[tuple[str, str], ...] = field(default=())

    @property
    def qualified(self) -> str:
        return f"{self.source}.{self.name}"

    @property
    def params_dict(self) -> dict[str, str]:
        return dict(self.params)

    @property
    def key(self) -> str:
        """The canonical key. A single param goes by position."""
        if len(self.params) == 1:
            inner = self.params[0][1]
        else:
            inner = ",".join(f"{k}={v}" for k, v in self.params)
        return f"{self.qualified}[{inner}]" if inner else self.qualified

    def __str__(self) -> str:
        return self.key


@dataclass(frozen=True)
class RawKey:
    """A key split into its parts, before the source's spec gives the
    positional param its name."""

    source: str
    name: str
    positional: str | None
    named: dict[str, str]


def split_key(key: str) -> RawKey:
    m = KEY_RE.match(key.strip())
    if not m:
        raise UnknownName(f"{key!r} is not a value key; keys look like source.value or source.value[param]")
    source, name, inner = m.group(1), m.group(2), m.group(3) or ""
    positional: str | None = None
    named: dict[str, str] = {}
    if inner and "=" not in inner:
        positional = inner
    elif inner:
        for pair in inner.split(","):
            k, sep, v = pair.partition("=")
            if not sep or not k.strip():
                raise UnknownName(f"{key!r}: params look like [name=value,name=value]")
            named[k.strip()] = v.strip()
    return RawKey(source, name, positional, named)


def make_ref(source: str, name: str, params: dict[str, Any] | None) -> ValueRef:
    items = tuple(sorted((str(k), str(v)) for k, v in (params or {}).items()))
    return ValueRef(source, name, items)
