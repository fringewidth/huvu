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

import logging
import os
import shutil
import stat
from pathlib import Path

from .prompts import SKILL_PREFIX as PREFIX
from .prompts import skill as render
from .spec import SourceSpec

log = logging.getLogger("huvu.skills")


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
