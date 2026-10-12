"""What each person browses, from huvu's extension in their desktop Chrome.

The extension (browser-extension/ at the repo root) reads Chrome's own
history about once a minute and posts the visits it hasn't sent yet to
POST /api/ingest/browser, with a tracker's token (`huvu tracker add <user>
--browser`). On its first run it sends the 90 days Chrome keeps. Incognito
windows never reach Chrome's history, so they never reach huvu.

A URL can carry secrets: a password reset link, an OAuth code, a session
token. So every URL loses its query string and fragment here, whatever the
extension sent, except the few parameters in KEEP, which say what a page is
(a YouTube video, a search). The extension strips them the same way before
they leave the browser; this is the rule that holds when it doesn't.

Every visit is a row of `browser_visits`, its person's alone, so nobody else
reads it or uses it in a trigger. A page's title is written by whoever made
the page, so the table is labelled `external`, and a wake that read it says
so to the model.

    [[sources]]
    name = "browser"
    driver = "huvu_drivers.browser:BrowserSource"
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import SplitResult, parse_qs, urlencode, urlsplit, urlunsplit

from huvu.prompts import BROWSER_GUIDE, TEXT
from huvu.source import Source, SourceError
from huvu.spec import Caller, ColumnSpec, SourceSpec, TableSpec

log = logging.getLogger("huvu.source.browser")

# The query parameters a URL keeps, by site and path: the ones that say what
# the page is. Everything else in a query goes.
KEEP: dict[tuple[str, str], tuple[str, ...]] = {
    ("youtube.com", "/watch"): ("v",),
    ("youtube.com", "/results"): ("search_query",),
    ("google.com", "/search"): ("q",),
    ("bing.com", "/search"): ("q",),
    ("duckduckgo.com", "/"): ("q",),
}
# The parameter that holds a search page's words.
SEARCH = {"q", "search_query"}
# The visits one post may carry. The extension sends 500 at a time.
MAX_VISITS = 1000
TITLE_MAX = 500
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")


@dataclass(frozen=True)
class Page:
    url: str
    domain: str
    video_id: str | None
    search: str | None


def page(raw: str) -> Page | None:
    """The URL with only the kept parameters, and what it says, or None for
    anything but a web page (chrome://, file://, data:)."""
    parts = urlsplit(raw)
    found = None
    if parts.scheme in ("http", "https") and parts.hostname:
        found = _web_page(parts)
    return found


def _web_page(parts: SplitResult) -> Page:
    domain = (parts.hostname or "").removeprefix("www.")
    site = domain.removeprefix("m.")
    path = parts.path or "/"
    kept = KEEP.get((site, path), ())
    query = {k: v[0] for k, v in parse_qs(parts.query).items() if k in kept}
    video = query.get("v") if site == "youtube.com" else None
    if site == "youtube.com" and path.startswith("/shorts/"):
        video = path.removeprefix("/shorts/").strip("/")
    elif site == "youtu.be":
        video = path.strip("/")
    # A user and password in the address go too.
    url = urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], path, urlencode(query), ""))
    return Page(
        url=url,
        domain=domain,
        video_id=video if video and VIDEO_ID.match(video) else None,
        search=next((query[k] for k in SEARCH if k in query), None),
    )


def _column(name: str, type: str) -> ColumnSpec:
    return ColumnSpec(name=name, type=type, description=TEXT[f"browser.table.visits.column.{name}"])


TABLES = [
    TableSpec(
        name="visits",
        description=TEXT["browser.table.visits"],
        columns=[
            ColumnSpec(name="device", type="text", description=TEXT["browser.column.device"]),
            _column("visit_id", "text"),
            _column("url", "text"),
            _column("domain", "text"),
            _column("title", "text"),
            _column("transition", "text"),
            _column("video_id", "text"),
            _column("search", "text"),
        ],
        labels=["external"],
        indexes=[["device", "visit_id"], ["domain", "ts"], ["video_id"]],
    ),
]


class BrowserSource(Source):
    def describe(self) -> SourceSpec:
        return SourceSpec(name="browser", description=TEXT["browser.description"], guide=BROWSER_GUIDE, tables=TABLES)

    async def ingest(self, body: Any, caller: Caller) -> Any:
        """Takes a batch of visits, `{"visits": [{"id", "url", "title",
        "at" (ms since the epoch), "transition"}]}`, and answers how many it
        kept. The extension sends a batch again when it didn't hear back, so
        a visit huvu has is skipped. A visit huvu can't read is logged and
        dropped, not refused, so it can't hold up the extension's queue."""
        user, device = caller.user, caller.device
        if user is None or device is None:
            raise SourceError("browser history needs a person's device")
        visits = body.get("visits") if isinstance(body, dict) else None
        if not isinstance(visits, list):
            raise SourceError('post {"visits": [...]}')
        if len(visits) > MAX_VISITS:
            raise SourceError(f"at most {MAX_VISITS} visits in one post, not {len(visits)}")
        ids = [str(v.get("id")) for v in visits if isinstance(v, dict)]
        marks = ", ".join("?" * len(ids))
        known = {r["visit_id"] for r in self.ctx.rows("visits", f"device = ? AND visit_id IN ({marks})", (device, *ids))} if ids else set()
        taken = 0
        for visit in visits:
            try:
                visit_id = str(visit["id"])
                found = page(str(visit["url"]))
                at = datetime.fromtimestamp(float(visit["at"]) / 1000, UTC)
            except (KeyError, TypeError, ValueError, OverflowError, OSError) as err:
                log.warning("dropped a visit from %s that huvu can't read (%s): %s", device, err, str(visit)[:300])
                continue
            if found is None or visit_id in known:
                continue
            known.add(visit_id)
            await self.ctx.record("visits", _row(found, visit, visit_id, device), at=at, user=user)
            taken += 1
        return {"taken": taken, "skipped": len(visits) - taken}


def _row(found: Page, visit: dict[str, Any], visit_id: str, device: str) -> dict[str, Any]:
    return {
        "device": device,
        "visit_id": visit_id,
        "url": found.url,
        "domain": found.domain,
        "title": str(visit.get("title") or "")[:TITLE_MAX] or None,
        "transition": visit.get("transition"),
        "video_id": found.video_id,
        "search": found.search,
    }
