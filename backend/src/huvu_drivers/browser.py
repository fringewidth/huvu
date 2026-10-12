"""What each person browses, from huvu's extension in their desktop Chrome.

The extension (browser-extension/ at the repo root) reads Chrome's own
history about once a minute and posts the visits it hasn't sent yet to
POST /api/ingest/browser, with a tracker's token (`huvu tracker add <user>
--browser`). Incognito windows never reach Chrome's history, so they never
reach huvu.

Only a page's title leaves the browser, never its address. An address can
carry secrets anywhere in it: a password reset token in its path, an OAuth
code in its query, a "anyone with the link" share id. A title says what the
page is (a YouTube video's name, a search's words) and carries none of
that. So a visit is its title, its time and how Chrome got there, and the
backend keeps nothing else, whatever a post holds.

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
from datetime import UTC, datetime
from typing import Any

from huvu.prompts import BROWSER_GUIDE, TEXT
from huvu.source import Source, SourceError
from huvu.spec import Caller, ColumnSpec, SourceSpec, TableSpec

log = logging.getLogger("huvu.source.browser")

# The visits one post may carry. The extension sends 500 at a time.
MAX_VISITS = 1000
TITLE_MAX = 500


def _column(name: str, type: str) -> ColumnSpec:
    return ColumnSpec(name=name, type=type, description=TEXT[f"browser.table.visits.column.{name}"])


TABLES = [
    TableSpec(
        name="visits",
        description=TEXT["browser.table.visits"],
        columns=[
            ColumnSpec(name="device", type="text", description=TEXT["browser.column.device"]),
            _column("visit_id", "text"),
            _column("title", "text"),
            _column("transition", "text"),
        ],
        labels=["external"],
        indexes=[["device", "visit_id"]],
    ),
]


class BrowserSource(Source):
    def describe(self) -> SourceSpec:
        return SourceSpec(name="browser", description=TEXT["browser.description"], guide=BROWSER_GUIDE, tables=TABLES)

    async def ingest(self, body: Any, caller: Caller) -> Any:
        """Takes a batch of visits, `{"visits": [{"id", "title", "at" (ms
        since the epoch), "transition"}]}`, and answers how many it kept.
        Anything else in a visit, an address included, is never stored. The
        extension sends a batch again when it didn't hear back, so a visit
        huvu has is skipped, and so is one with no title. A visit huvu can't
        read is logged and dropped, not refused, so it can't hold up the
        extension's queue."""
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
                title = str(visit.get("title") or "").strip()[:TITLE_MAX]
                at = datetime.fromtimestamp(float(visit["at"]) / 1000, UTC)
            except (KeyError, TypeError, ValueError, OverflowError, OSError) as err:
                # The visit as it came may hold an address, so the log names
                # only what was wrong.
                log.warning("dropped a visit from %s that huvu can't read: %s", device, err)
                continue
            if not title or visit_id in known:
                continue
            known.add(visit_id)
            row = {"device": device, "visit_id": visit_id, "title": title, "transition": visit.get("transition")}
            await self.ctx.record("visits", row, at=at, user=user)
            taken += 1
        return {"taken": taken, "skipped": len(visits) - taken}
