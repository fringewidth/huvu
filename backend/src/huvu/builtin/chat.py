"""The `chat` source: every conversation turn, as history.

The kernel records one row in `chat_turns` when a turn ends (see
conversations.py), as its user's, or its group's on a shared device nobody
was recognized at. Rows are never removed: an edit rewinds the
conversation in the app, and the new turn's row names the turn it replaced,
so what an edit dropped stays readable. A fresh Hermes session reads only
the latest turns, and the model reads the rest here.
"""

from __future__ import annotations

from ..prompts import TEXT
from ..source import Source
from ..spec import ColumnSpec, SourceSpec, TableSpec

COLUMNS = [
    ("turn_id", "text"),
    ("device_id", "text"),
    ("device", "text"),
    ("n", "integer"),
    ("them", "text"),
    ("you", "text"),
    ("ending", "text"),
    ("actions", "json"),
    ("replaces", "text"),
    ("usage", "json"),
]


class ChatSource(Source):
    def describe(self) -> SourceSpec:
        return SourceSpec(
            name="chat",
            description=TEXT["chat.description"],
            guide=TEXT["chat.guide"],
            tables=[
                TableSpec(
                    name="turns",
                    description=TEXT["chat.table.turns"],
                    columns=[ColumnSpec(name=n, type=t, description=TEXT[f"chat.table.turns.column.{n}"]) for n, t in COLUMNS],
                    labels=["owner"],
                    indexes=[["device_id", "n"], ["turn_id"]],
                )
            ],
        )
