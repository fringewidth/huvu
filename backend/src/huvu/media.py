"""Media URLs: how the frontend gets an image a source holds, such as a
decrypted doorbell snapshot, without ever seeing the source's own URL.

A ref looks like `tuya:album/8f2c`: the source's name, a colon, and
whatever the source uses to find the content. The kernel hands out an
unguessable token per ref. `<img>` tags can't send a bearer header, so the
token itself is the permission.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from .db import dumps, loads
from .source import SourceError
from .spec import Blob

if TYPE_CHECKING:
    from .kernel import Kernel

NAMESPACE = "media"


class MediaStore:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel

    def url_for(self, ref: str) -> str:
        source, sep, _ = ref.partition(":")
        if not sep:
            raise SourceError(f"{ref!r} is not a ref; refs look like source:path")
        self._kernel.registry.get(source)
        row = self._kernel.db.execute(
            "SELECT key FROM kv WHERE namespace = ? AND value = ?", (NAMESPACE, dumps(ref))
        ).fetchone()
        token = row["key"] if row else secrets.token_urlsafe(18)
        if row is None:
            self._kernel.db.execute("INSERT INTO kv (namespace, key, value) VALUES (?, ?, ?)", (NAMESPACE, token, dumps(ref)))
        return f"{self._kernel.config.public_url}/api/media/{token}"

    async def fetch(self, token: str) -> Blob:
        row = self._kernel.db.execute("SELECT value FROM kv WHERE namespace = ? AND key = ?", (NAMESPACE, token)).fetchone()
        if row is None:
            raise SourceError("no such media")
        ref: str = loads(row["value"])
        source = ref.partition(":")[0]
        return await self._kernel.registry.get(source).fetch(ref)
