"""Time for the model: readable timestamps out, relative times in.

The kernel, sources, triggers and the frontend keep timestamps as datetimes
and ISO strings, because code compares them. The model gets each one as a
local time with its age, since subtracting ISO timestamps in its head is
where a small model slips. The absolute time stays next to the age: a tool
result stays in the context after the age is out of date.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Any

from pydantic import BaseModel


def format_time(at: datetime, now: datetime) -> str:
    local = at.astimezone(now.tzinfo)
    delta = now - local
    future = delta < timedelta(0)
    span = -delta if future else delta
    day_gap = (now.date() - local.date()).days
    clock = f"{local:%H:%M}"
    if day_gap == 0:
        where = clock
    elif day_gap == 1:
        where = f"yesterday {clock}"
    elif day_gap == -1:
        where = f"tomorrow {clock}"
    elif 1 < day_gap < 7:
        where = f"{local:%a} {clock}"
    else:
        where = f"{local:%Y-%m-%d}"
    return f"{where}, {_age(span, future)}"


def _age(span: timedelta, future: bool) -> str:
    minutes = int(span.total_seconds() // 60)
    if minutes < 1:
        amount = ""
    elif minutes < 60:
        amount = f"{minutes} min"
    elif minutes < 48 * 60:
        amount = f"{minutes // 60} h"
    else:
        amount = f"{minutes // (24 * 60)} days"
    if not amount:
        phrase = "just now"
    elif future:
        phrase = f"in {amount}"
    else:
        phrase = f"{amount} ago"
    return phrase


def format_span(span: timedelta) -> str:
    minutes = int(span.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    parts = [f"{hours} h" if hours else "", f"{minutes} min" if minutes else ""]
    return " ".join(p for p in parts if p) or f"{int(span.total_seconds())} s"


def for_model(value: Any, now: datetime) -> Any:
    """Turns every datetime and timedelta inside `value` into the readable
    form, and every model into a plain dict without empty fields."""
    if isinstance(value, BaseModel):
        value = value.model_dump(exclude_none=True)
    if isinstance(value, datetime):
        out: Any = format_time(value, now)
    elif isinstance(value, timedelta):
        out = format_span(value)
    elif isinstance(value, dict):
        out = {k: for_model(v, now) for k, v in value.items()}
    elif isinstance(value, (list, tuple)):
        out = [for_model(v, now) for v in value]
    else:
        out = value
    return out


RELATIVE_RE = re.compile(r"^(\d+)\s*(s|sec|m|min|h|hr|d|day|days|w)$")
DAY_TIME_RE = re.compile(r"^(today|tomorrow)?\s*(\d{1,2}):(\d{2})$")
UNITS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600, "d": 86400, "day": 86400, "days": 86400, "w": 604800}


def parse_when(text: str, now: datetime) -> datetime:
    """Reads a time a model wrote: `2h`, `30 min`, `tomorrow 09:00`,
    `18:00` (the next one), or ISO 8601. Raises ValueError otherwise."""
    raw = text.strip().lower()
    rel = RELATIVE_RE.match(raw)
    day_time = DAY_TIME_RE.match(raw)
    if rel:
        when = now + timedelta(seconds=int(rel.group(1)) * UNITS[rel.group(2)])
    elif day_time:
        when = _day_time(day_time, now)
    else:
        when = _iso(text.strip(), now)
    return when


def _day_time(m: re.Match[str], now: datetime) -> datetime:
    at = time(int(m.group(2)), int(m.group(3)))
    candidate = datetime.combine(now.date(), at, tzinfo=now.tzinfo)
    if m.group(1) == "tomorrow" or (m.group(1) is None and candidate <= now):
        candidate += timedelta(days=1)
    return candidate


def _iso(text: str, now: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as err:
        raise ValueError(f"can't read the time {text!r}; use 2h, 30 min, tomorrow 09:00, 18:00 or ISO 8601") from err
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=now.tzinfo)


def parse_clock(text: str) -> time:
    """Reads `HH:MM` for clock predicates."""
    m = re.match(r"^(\d{1,2}):(\d{2})$", text.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"{text!r} is not a time of day; write HH:MM")
    return time(int(m.group(1)), int(m.group(2)))
