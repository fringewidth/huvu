"""Media URLs: how the frontend gets an image, such as a decrypted
doorbell snapshot or a screenshot Hermes saved, without ever seeing where
it lives.

An image is named three ways. A ref looks like `tuya:album/8f2c`: the
source's name, a colon, and whatever the source uses to find the content.
An absolute path names an image file on the host's disk, such as one
Hermes wrote. An https URL is an image on the web. The kernel hands out an
unguessable token per ref or path. `<img>` tags can't send a bearer
header, so the token itself is the permission. A URL needs no token: the
device loads it itself, so huvu never fetches an address the model picks.

Any file can be named, so a file is served only if its first bytes say it
is an image. A secret, a database or a config file is never one. SVG is
left out: opened on its own, it runs scripts on huvu's origin.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from .db import dumps, loads
from .source import SourceError
from .spec import Blob

if TYPE_CHECKING:
    from .kernel import Kernel

NAMESPACE = "media"
FILE = "file:"
MAX_FILE_BYTES = 20 * 1024 * 1024


class MediaStore:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel

    def url_for(self, src: str) -> str:
        """The URL the frontend loads `src` from: a ref, an absolute path
        or an https URL."""
        src = src.strip()
        if src.startswith("https://"):
            return src
        if src.startswith("http://"):
            raise SourceError("the app loads only https images; plain http is refused")
        if src.startswith(("/", "~")):
            path = _image_file(src)
            _read_image(path)
            return self._token_url(FILE + str(path))
        source, sep, _ = src.partition(":")
        if not sep:
            raise SourceError(f"{src!r} is no image: give a ref (source:path), an absolute path or an https URL")
        self._kernel.registry.get(source)
        return self._token_url(src)

    def _token_url(self, ref: str) -> str:
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
        if ref.startswith(FILE):
            content, content_type = _read_image(Path(ref.removeprefix(FILE)))
            return Blob(content=content, content_type=content_type)
        source = ref.partition(":")[0]
        return await self._kernel.registry.get(source).fetch(ref)


def _image_file(src: str) -> Path:
    """The file `src` names, with `~` and symlinks resolved."""
    try:
        path = Path(os.path.expanduser(src)).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as err:
        raise SourceError(f"no file at {src}") from err
    if not path.is_file():
        raise SourceError(f"{src} is not a file")
    return path


def _read_image(path: Path) -> tuple[bytes, str]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise SourceError(f"{path} is over {MAX_FILE_BYTES // (1024 * 1024)} MB")
        content = path.read_bytes()
    except OSError as err:
        raise SourceError(f"can't read {path}") from err
    content_type = image_type(content)
    if content_type is None:
        raise SourceError(f"{path} is not a JPEG, PNG, GIF, WebP, AVIF or BMP image")
    return content, content_type


def image_type(content: bytes) -> str | None:
    """The image type the first bytes of `content` say it is."""
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content[4:8] == b"ftyp" and content[8:12] in (b"avif", b"avis"):
        return "image/avif"
    if content.startswith(b"BM"):
        return "image/bmp"
    return None
