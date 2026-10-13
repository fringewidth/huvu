"""Each profile's wiki: the agent's memory, which its person can read and edit in the app.

The agent owns its wiki. It is a folder of Markdown pages in its Hermes
profile, `<profile home>/wiki`, which the agent writes with Hermes's own file
tools, and whose index is the profile's MEMORY.md, always in its prompt.
huvu imposes no structure on it: the guide says where it is and that it is
the agent's, and the dream tends it every night.

This module is the app's side: list the pages, read one, write one, delete
one, for the person whose profile it is. A write names the version it was
made from, so an edit never lands over a change the agent made meanwhile.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .hermes import Profile

# Hermes's always-loaded index of the wiki.
INDEX = Path("memories") / "MEMORY.md"
# Hermes keeps MEMORY.md as entries joined by this line.
INDEX_DELIMITER = "\n§\n"
# A page larger than this is refused, both ways.
MAX_BYTES = 1_000_000


class WikiError(Exception):
    """A request the wiki refuses, with the reason for the person."""


class Conflict(WikiError):
    """The page changed since the version the edit was made from."""


@dataclass(frozen=True)
class Page:
    path: str
    text: str
    version: str
    modified: datetime


def home(hermes_home: Path, profile: Profile) -> Path:
    """A profile's own folder: the default profile's is Hermes's."""
    return hermes_home / "profiles" / profile.name if profile.name else hermes_home


def folder(hermes_home: Path, profile: Profile) -> Path:
    return home(hermes_home, profile) / "wiki"


def pages(root: Path, tz: ZoneInfo) -> list[dict[str, object]]:
    """Every page, by path, newest first."""
    if not root.is_dir():
        return []
    found = []
    for path in root.rglob("*.md"):
        if path.is_symlink() or not path.is_file() or any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        stat = path.stat()
        found.append({
            "path": path.relative_to(root).as_posix(),
            "title": _title(path),
            "modified": datetime.fromtimestamp(stat.st_mtime, tz),
            "size": stat.st_size,
        })
    return sorted(found, key=lambda p: p["modified"], reverse=True)


def index(profile_home: Path) -> list[str]:
    """MEMORY.md's entries, the agent's index of its wiki. Empty when it
    has none yet."""
    path = profile_home / INDEX
    if not path.is_file():
        return []
    return [e for e in (x.strip() for x in path.read_text(errors="replace").split(INDEX_DELIMITER)) if e]


def read(root: Path, relative: str, tz: ZoneInfo) -> Page:
    path = _resolve(root, relative)
    if not path.is_file():
        raise WikiError(f"no page {relative!r}")
    return _page(root, path, tz)


def write(root: Path, relative: str, text: str, version: str | None, tz: ZoneInfo) -> Page:
    """Writes a page. `version` is the one the edit was made from; None
    makes a new page, and fails if one is there already."""
    path = _resolve(root, relative)
    data = text.encode()
    if len(data) > MAX_BYTES:
        raise WikiError(f"a page can be at most {MAX_BYTES // 1000} KB")
    _check(path, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return _page(root, path, tz)


def delete(root: Path, relative: str, version: str) -> None:
    path = _resolve(root, relative)
    _check(path, version)
    path.unlink()


def _check(path: Path, version: str | None) -> None:
    if version is None:
        if path.exists():
            raise Conflict(f"{path.name} exists already")
        return
    if not path.is_file():
        raise Conflict(f"{path.name} is gone")
    if _version(path.read_bytes()) != version:
        raise Conflict(f"{path.name} changed since you opened it")


def _resolve(root: Path, relative: str) -> Path:
    """The page's path, only inside the wiki, only Markdown, and never
    through a link out of it."""
    parts = Path(relative).parts
    if not relative or Path(relative).is_absolute() or ".." in parts or any(p.startswith(".") for p in parts):
        raise WikiError(f"{relative!r} isn't a page in the wiki")
    if not relative.endswith(".md"):
        raise WikiError("a page is a Markdown file, ending in .md")
    path = root / relative
    if path.is_symlink() or not path.parent.resolve().is_relative_to(root.resolve()):
        raise WikiError(f"{relative!r} isn't a page in the wiki")
    return path


def _page(root: Path, path: Path, tz: ZoneInfo) -> Page:
    data = path.read_bytes()
    if len(data) > MAX_BYTES:
        raise WikiError(f"{path.name} is larger than {MAX_BYTES // 1000} KB")
    return Page(
        path=path.relative_to(root).as_posix(),
        text=data.decode(errors="replace"),
        version=_version(data),
        modified=datetime.fromtimestamp(path.stat().st_mtime, tz),
    )


def _version(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _title(path: Path) -> str:
    """The page's first heading, or its file name."""
    try:
        with path.open(errors="replace") as f:
            for line in f:
                if line.startswith("# "):
                    return line[2:].strip()
                if line.strip() and not line.startswith("---"):
                    break
    except OSError:
        pass
    return path.stem.replace("-", " ").replace("_", " ")
