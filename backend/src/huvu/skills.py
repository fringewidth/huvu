"""Every source as a Hermes skill.

A Hermes session sees one line per skill (its name and description) and
loads a skill's body only when it needs it. That is how the model learns
what huvu can do without every action's schema in every session: each
source becomes a skill named `huvu-<source>`, whose body is the driver's
hand-written guide (when to use it, the gotchas) followed by a reference
huvu generates from the driver's spec (actions, values, events, tables),
so it can't drift from the code.

The skills are written into the backend's data folder at every start, and
again when a source's spec changes while it runs (Kernel.respec), and
linked, one symlink per source, into a fixed folder that every Hermes
profile reads through `skills.external_dirs` (the repo's `hermes-skills/`
by default). The links are made or repaired at every start too, so nothing
needs setting up again when the data folder is wiped or a driver comes or
goes. Anything else in that folder is left alone. The files are read-only:
Hermes lets the model edit a skill wherever it lives, and huvu would
overwrite the edit at the next start, so what the model learns belongs in its
own skills and memory.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from .spec import STANDARD_COLUMNS, SourceSpec

log = logging.getLogger("huvu.skills")

PREFIX = "huvu-"
OWNED = (
    "huvu wrote this skill from its driver and rewrites it at every start and whenever the source changes, so don't edit it; "
    "put what you learn in your own skills or memory."
)


def signature(name: str, schema: dict[str, Any]) -> str:
    """`home_turn_on(entity_id, brightness?: number)`: an action as the
    model calls it, with optional arguments marked."""
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    parts = []
    for arg, prop in properties.items():
        kind = "|".join(str(v) for v in prop["enum"]) if "enum" in prop else prop.get("type", "")
        parts.append(f"{arg}{'' if arg in required else '?'}{f': {kind}' if kind and kind != 'string' else ''}")
    return f"{name}({', '.join(parts)})"


def render(spec: SourceSpec) -> str:
    lines = [
        "---",
        f"name: {PREFIX}{spec.name}",
        f"description: {json.dumps(_one_line(spec.description) + f' Load before using huvu actions, values or tables of {spec.name}.')}",
        "metadata:",
        "  hermes:",
        "    tags: [huvu]",
        "---",
        "",
        f"# huvu: {spec.name}",
        "",
        OWNED,
        "",
    ]
    if spec.guide.strip():
        lines += [spec.guide.strip(), ""]
    if spec.actions:
        lines += ["## Actions", "", "Run with `act`, as `act({\"name\": ..., \"args\": {...}})`. Every action takes an optional `reason`.", ""]
        for action in spec.actions:
            notes = ["asks first" if action.autonomy == "ask" else "", "always needs a person's tap" if action.requires_human else ""]
            note = "; ".join(n for n in notes if n)
            lines.append(f"- `{signature(f'{spec.name}_{action.name}', action.input_schema)}`: {action.description}" + (f" ({note})" if note else ""))
        lines.append("")
    if spec.values:
        lines += ["## Values", "", "Read with `state_get`; triggers can watch them.", ""]
        for value in spec.values:
            params = f"[{','.join(value.params)}]" if value.params else ""
            unit = f", {value.unit}" if value.unit else ""
            personal = ", only the person `user` names can read it" if value.personal else ""
            lines.append(f"- `{spec.name}.{value.name}{params}`: {value.description} ({value.type}, {value.updates}{unit}{personal})")
        lines.append("")
    if spec.events:
        lines += ["## Events", "", "Search them with `events_search`; triggers can watch them.", ""]
        for event in spec.events:
            fields = f" Fields: {', '.join(f'{k} ({v})' for k, v in event.data.items())}." if event.data else ""
            lines.append(f"- `{spec.name}.{event.type}`: {event.description}{fields}")
        lines.append("")
    if spec.tables:
        standard = ", ".join(STANDARD_COLUMNS)
        lines += [
            "## Tables",
            "",
            f"History for analysis, kept for good. Query with `data_sql` (SQLite, one read-only statement; JSON functions work). "
            f"Every table also has the standard columns {standard}: `at` is ISO local time, `ts` seconds since the epoch, "
            "`local_minute` minutes since local midnight, `weekday` 1 for Monday, and `user_id` whose row it is. "
            "Join tables of different sources on time (`ts` windows, or the latest row before a time) and on `user_id`.",
            "",
        ]
        for table in spec.tables:
            lines += [f"### `{spec.name}_{table.name}`", "", _one_line(table.description), ""]
            lines += [f"- `{c.name}` ({c.type}): {c.description}" for c in table.columns]
            indexes = [["ts"], ["user_id", "ts"], *table.indexes]
            lines += ["", "Indexed on " + ", ".join(f"({', '.join(i)})" for i in indexes) + ".", ""]
    return "\n".join(lines)


def write_all(specs: list[SourceSpec], folder: Path) -> None:
    """Writes every source's skill, read-only, and removes the skills of
    sources that are gone."""
    folder.mkdir(parents=True, exist_ok=True)
    wanted = {f"{PREFIX}{s.name}" for s in specs}
    for stale in folder.glob(f"{PREFIX}*"):
        if stale.is_dir() and stale.name not in wanted:
            _writable(stale)
            shutil.rmtree(stale)
    for spec in specs:
        target = folder / f"{PREFIX}{spec.name}" / "SKILL.md"
        target.parent.mkdir(exist_ok=True)
        text = render(spec)
        if target.exists() and target.read_text() == text:
            continue
        if target.exists():
            target.chmod(stat.S_IRUSR | stat.S_IWUSR)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, target)
        target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def link_all(specs: list[SourceSpec], folder: Path, links: Path) -> None:
    """Points `links/huvu-<source>` at each written skill, and removes the
    huvu-* links of sources that are gone. Hermes follows the links."""
    links.mkdir(parents=True, exist_ok=True)
    wanted = {f"{PREFIX}{s.name}": (folder / f"{PREFIX}{s.name}").resolve() for s in specs}
    for entry in links.glob(f"{PREFIX}*"):
        if entry.is_symlink() and entry.name not in wanted:
            entry.unlink()
    for name, target in wanted.items():
        link = links / name
        if link.is_symlink() and link.resolve() == target:
            continue
        if link.is_symlink() or link.is_file():
            link.unlink()
        elif link.is_dir():
            log.warning("%s is a real folder, not huvu's link; left alone", link)
            continue
        link.symlink_to(target, target_is_directory=True)


def _writable(path: Path) -> None:
    for item in [path, *path.rglob("*")]:
        item.chmod(item.stat().st_mode | stat.S_IWUSR)


def _one_line(text: str) -> str:
    line = " ".join(text.split())
    return line if line.endswith(".") else f"{line}."
