"""YouTube: finds a video for the `ui_youtube` tool, which hands it off to
the person's YouTube app.

huvu plays nothing itself. The model names what to search for, this finds
the top video through the YouTube Data API's search, and the app shows it
with a button that opens it in the YouTube app (on the web, YouTube's
site).

YOUTUBE_API_KEY comes from the environment or backend/.env: a Google Cloud
API key with the YouTube Data API v3 enabled. The free quota is 10,000
units a day, and a search costs 100, so about 100 searches a day. Without
the key, the tool says huvu can't search YouTube.
"""

from __future__ import annotations

import html
from dataclasses import asdict, dataclass
from typing import Any

import httpx

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"


class YouTubeError(Exception):
    pass


@dataclass
class Video:
    id: str
    title: str
    channel: str
    thumbnail: str

    def public(self) -> dict[str, Any]:
        return asdict(self)


class YouTube:
    def __init__(self, key: str | None) -> None:
        self._key = key
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    async def find(self, query: str) -> Video:
        """The top video YouTube finds for `query`."""
        if not self._key:
            raise YouTubeError("huvu can't search YouTube: YOUTUBE_API_KEY isn't set")
        params = {
            "part": "snippet",
            "type": "video",
            "maxResults": "1",
            "q": query,
            "key": self._key,
        }
        try:
            res = await self._http.get(SEARCH_URL, params=params)
        except httpx.HTTPError as err:
            raise YouTubeError(f"YouTube didn't answer: {err}") from err
        if res.status_code != 200:
            raise YouTubeError(f"YouTube's search failed with {res.status_code}: {res.text[:200]}")
        items = res.json().get("items") or []
        if not items:
            raise YouTubeError(f"YouTube found no video for {query!r}")
        snippet = items[0]["snippet"]
        thumbnails = snippet.get("thumbnails") or {}
        thumbnail = next((thumbnails[size]["url"] for size in ("high", "medium", "default") if size in thumbnails), "")
        # The search returns titles with HTML entities, such as &#39;.
        return Video(
            id=items[0]["id"]["videoId"],
            title=html.unescape(snippet.get("title", "")),
            channel=html.unescape(snippet.get("channelTitle", "")),
            thumbnail=thumbnail,
        )

    async def close(self) -> None:
        await self._http.aclose()
