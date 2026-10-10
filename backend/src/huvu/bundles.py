"""Live updates for the native apps (Capacitor's phone build; the Fire TV
app loads its page from the host, so it needs none).

An APK carries its own copy of the web build, so without this a frontend
change reaches a phone only through a new APK. The image builds each
native target's web bundle next to the web client, one folder per target
under `bundles_dir` (HUVU_BUNDLES_DIR), and each build stamps itself with
bundle.json (vite.config.ts): its version, a hash of every file in it, and
the native version it needs (frontend/native.json).

The app asks `GET /api/app/<target>` on launch and each time it comes to
the front. When the version differs from its own and the native version
is its APK's, it downloads `GET /api/app/<target>/<version>.zip` and
switches to it the next time it goes to the background
(frontend/src/net/liveUpdate.ts).
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

STAMP = "bundle.json"
TARGET = re.compile(r"^[a-z]+$")


class BundleError(Exception):
    pass


class Bundles:
    def __init__(self, folder: Path | None) -> None:
        self.folder = folder
        # One zip per target, of its current version. A build never changes
        # under a running backend, so it is made once, on the first ask.
        self._zips: dict[str, tuple[str, bytes]] = {}
        self._lock = asyncio.Lock()

    def latest(self, target: str) -> dict[str, Any]:
        """The target's build: its version, and the native version it needs."""
        stamp = json.loads((self._root(target) / STAMP).read_text())
        return {"version": stamp["version"], "native": stamp["native"]}

    async def zip(self, target: str, version: str) -> bytes:
        """The target's build as a zip, with index.html at its root. Only
        the current version exists; an older one is gone with its image."""
        if self.latest(target)["version"] != version:
            raise BundleError(f"{target} has no build {version}")
        async with self._lock:
            made = self._zips.get(target)
            if not made or made[0] != version:
                made = (version, await asyncio.to_thread(_pack, self._root(target)))
                self._zips[target] = made
        return made[1]

    def _root(self, target: str) -> Path:
        if not self.folder or not TARGET.match(target):
            raise BundleError(f"no build for {target}")
        root = self.folder / target
        if not (root / STAMP).is_file():
            raise BundleError(f"no build for {target}")
        return root


def _pack(root: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(root).as_posix())
    return buffer.getvalue()
