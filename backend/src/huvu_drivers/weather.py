"""The weather at every place huvu keeps it for, from Open-Meteo.

Open-Meteo is free for non-commercial use, personal home automation
included, with no account and no key: up to 10,000 calls a day, 5,000 an
hour and 600 a minute, by IP address. A call that asks for more than 10
variables or more than 14 days counts as more than one, by both
(variables / 10 times days / 14). Every call this driver makes is weighed
that way, and once a day's calls reach `daily_calls` it makes no more until
the next day. Its data is CC BY 4.0.

A place is a named point. The config's places belong to the household's
group; the model adds more for the person it helps (`weather_place_add`),
which are that person's alone until shared with a group. Each place has
its own tables' rows, scoped the way the place is, so nobody reads the
weather at a place someone else keeps to themselves.

For each place, conditions now are fetched every 15 minutes (`weather_now`),
and the hourly and daily forecast (`weather_hourly`, `weather_daily`, with
air quality merged into the hours) every hour, from the start of yesterday
to 16 days ahead. Every hour that has passed is recorded for good in
`weather_history`, weather and air together; a new place starts with the
92 days before it, which is as far back as the forecast API reaches, and
after a gap huvu fills it from the same.

With `places_max` places, the default 12, the calls come to about 4,400 a
day, under half the free tier.

    [[sources]]
    name = "weather"
    driver = "huvu_drivers.weather:WeatherSource"
    options = { places = { home = { lat = 17.385, lon = 78.4867 } }, group = "home" }
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import apsw
import httpx

from huvu.db import Row, open_db
from huvu.prompts import TEXT, WEATHER_GUIDE
from huvu.source import Source, SourceError
from huvu.spec import ActionSpec, Caller, ColumnSpec, ColumnType, Result, SourceSpec, TableSpec

log = logging.getLogger("huvu.source.weather")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

NOW_EVERY = timedelta(minutes=15)
FORECAST_EVERY = timedelta(hours=1)
# How long after a failed call huvu tries that place again.
RETRY_AFTER = timedelta(minutes=5)
# Conditions older than this are no longer now: the place's row leaves
# weather_now until a fetch works again.
STALE_AFTER = timedelta(hours=1)
FORECAST_DAYS = 16
# The air quality model's forecast reaches about 5 days; 7 is the API's most.
AIR_FORECAST_DAYS = 7
# How far back the forecast and air quality APIs reach.
MAX_PAST_DAYS = 92
PLACES_MAX = 12
# Under the free tier's 10,000 a day, with room for a second huvu (a
# development machine) behind the same address.
DAILY_CALLS = 8000
GEOCODE_CANDIDATES = 10

# Every column of an hour-shaped table (weather_now, weather_hourly,
# weather_history) with Open-Meteo's name for it.
WEATHER: list[tuple[str, str, ColumnType]] = [
    ("temperature_c", "temperature_2m", "real"),
    ("feels_like_c", "apparent_temperature", "real"),
    ("humidity_pct", "relative_humidity_2m", "integer"),
    ("dew_point_c", "dew_point_2m", "real"),
    ("precipitation_probability_pct", "precipitation_probability", "integer"),
    ("precipitation_mm", "precipitation", "real"),
    ("rain_mm", "rain", "real"),
    ("showers_mm", "showers", "real"),
    ("snowfall_cm", "snowfall", "real"),
    ("weather_code", "weather_code", "integer"),
    ("cloud_cover_pct", "cloud_cover", "integer"),
    ("visibility_m", "visibility", "real"),
    ("wind_speed_kmh", "wind_speed_10m", "real"),
    ("wind_direction_deg", "wind_direction_10m", "integer"),
    ("wind_gusts_kmh", "wind_gusts_10m", "real"),
    ("uv_index", "uv_index", "real"),
    ("is_day", "is_day", "boolean"),
    ("sunshine_s", "sunshine_duration", "real"),
    ("pressure_hpa", "pressure_msl", "real"),
    ("cape_jkg", "cape", "real"),
    ("et0_mm", "et0_fao_evapotranspiration", "real"),
    ("radiation_wm2", "shortwave_radiation", "real"),
    ("soil_moisture", "soil_moisture_0_to_1cm", "real"),
]
AIR: list[tuple[str, str, ColumnType]] = [
    ("us_aqi", "us_aqi", "integer"),
    ("european_aqi", "european_aqi", "integer"),
    ("pm2_5", "pm2_5", "real"),
    ("pm10", "pm10", "real"),
    ("ozone", "ozone", "real"),
    ("nitrogen_dioxide", "nitrogen_dioxide", "real"),
    ("sulphur_dioxide", "sulphur_dioxide", "real"),
    ("carbon_monoxide", "carbon_monoxide", "real"),
    ("dust", "dust", "real"),
    ("aerosol_optical_depth", "aerosol_optical_depth", "real"),
]
DAILY: list[tuple[str, str, ColumnType]] = [
    ("weather_code", "weather_code", "integer"),
    ("temperature_max_c", "temperature_2m_max", "real"),
    ("temperature_min_c", "temperature_2m_min", "real"),
    ("feels_like_max_c", "apparent_temperature_max", "real"),
    ("feels_like_min_c", "apparent_temperature_min", "real"),
    ("sunrise", "sunrise", "text"),
    ("sunset", "sunset", "text"),
    ("daylight_s", "daylight_duration", "real"),
    ("sunshine_s", "sunshine_duration", "real"),
    ("uv_index_max", "uv_index_max", "real"),
    ("precipitation_mm", "precipitation_sum", "real"),
    ("rain_mm", "rain_sum", "real"),
    ("showers_mm", "showers_sum", "real"),
    ("snowfall_cm", "snowfall_sum", "real"),
    ("precipitation_hours", "precipitation_hours", "real"),
    ("precipitation_probability_max_pct", "precipitation_probability_max", "integer"),
    ("wind_speed_max_kmh", "wind_speed_10m_max", "real"),
    ("wind_gusts_max_kmh", "wind_gusts_10m_max", "real"),
    ("wind_direction_deg", "wind_direction_10m_dominant", "integer"),
    ("radiation_mj", "shortwave_radiation_sum", "real"),
    ("et0_mm", "et0_fao_evapotranspiration", "real"),
]
# Daily columns Open-Meteo gives as times.
DAILY_TIMES = {"sunrise", "sunset"}


def _column(table: str, name: str, type: ColumnType) -> ColumnSpec:
    """A column, described by its table's own text when it has one, else by
    the text every hour-shaped table shares."""
    key = f"weather.table.{table}.column.{name}"
    return ColumnSpec(name=name, type=type, description=TEXT.get(key) or TEXT[f"weather.column.{name}"])


def _hour_columns(table: str) -> list[ColumnSpec]:
    """The weather and air columns. weather_hourly describes them, and the
    other tables point there, since every skill is in every session's
    prompt."""
    if table == "hourly":
        return [_column(table, name, type) for name, _, type in WEATHER + AIR]
    return [ColumnSpec(name=name, type=type, description=TEXT["weather.column.as_hourly"]) for name, _, type in WEATHER + AIR]


TABLES = [
    TableSpec(
        name="places",
        description=TEXT["weather.table.places"],
        columns=[
            _column("places", name, type)
            for name, type in (
                ("name", "text"),
                ("lat", "real"),
                ("lon", "real"),
                ("elevation_m", "real"),
                ("timezone", "text"),
                ("found", "text"),
                ("added_by", "text"),
                ("from_config", "boolean"),
                ("updated_at", "text"),
            )
        ],
        live=True,
    ),
    TableSpec(
        name="now",
        description=TEXT["weather.table.now"],
        columns=[_column("now", "place", "text"), _column("now", "observed_at", "text"), *_hour_columns("now")],
        live=True,
    ),
    TableSpec(
        name="hourly",
        description=TEXT["weather.table.hourly"],
        columns=[
            _column("hourly", "place", "text"),
            _column("hourly", "time", "text"),
            _column("hourly", "time_ts", "integer"),
            _column("hourly", "date", "text"),
            *_hour_columns("hourly"),
        ],
        live=True,
    ),
    TableSpec(
        name="daily",
        description=TEXT["weather.table.daily"],
        columns=[_column("daily", "place", "text"), _column("daily", "date", "text"), *[_column("daily", name, type) for name, _, type in DAILY]],
        live=True,
    ),
    TableSpec(
        name="history",
        description=TEXT["weather.table.history"],
        columns=[_column("history", "place", "text"), _column("history", "place_time", "text"), *_hour_columns("history")],
        indexes=[["place", "ts"]],
    ),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS places (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    -- What the geocoder matched for a place added by name; NULL otherwise.
    found TEXT,
    -- The person who added it. NULL for the config's and a group's own.
    user_id TEXT,
    -- The group it is shared with, or the group that owns it.
    group_id TEXT,
    -- 'config' or 'added'.
    origin TEXT NOT NULL,
    created_at TEXT NOT NULL,
    removed_at TEXT
);
"""


@dataclass(frozen=True)
class Place:
    id: str
    name: str
    lat: float
    lon: float
    found: str | None
    user: str | None
    group: str | None
    origin: str

    def scope(self) -> dict[str, Any]:
        """Whose its rows are: its person's alone, or, shared or a group's
        own, the group's."""
        return {"user_id": self.user if self.group is None else None, "group_id": self.group}

    def owner(self) -> tuple[str | None, str | None]:
        scope = self.scope()
        return scope["user_id"], scope["group_id"]


@dataclass
class Weather:
    """What huvu last fetched for a place, and when to fetch again."""

    timezone: tzinfo | None = None
    timezone_name: str | None = None
    elevation_m: float | None = None
    now: dict[str, Any] | None = None
    observed: datetime | None = None
    hourly: list[dict[str, Any]] = field(default_factory=list)
    daily: list[dict[str, Any]] = field(default_factory=list)
    updated: datetime | None = None
    now_due: datetime | None = None
    forecast_due: datetime | None = None


class OverBudget(Exception):
    """A call that would take the day's calls past `daily_calls`."""


class WeatherSource(Source):
    def __init__(self, options: dict[str, Any] | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(options)
        self._conn: apsw.Connection | None = None
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._weather: dict[str, Weather] = {}
        self._spent: dict[date, float] = {}
        self._published_stale: set[str] = set()

    @property
    def places_max(self) -> int:
        return int(self.options.get("places_max", PLACES_MAX))

    @property
    def daily_calls(self) -> float:
        return float(self.options.get("daily_calls", DAILY_CALLS))

    def describe(self) -> SourceSpec:
        return SourceSpec(
            name="weather",
            description=TEXT["weather.description"],
            guide=WEATHER_GUIDE.format(places_max=self.places_max, past_days=MAX_PAST_DAYS),
            actions=[
                ActionSpec(
                    name="place_add",
                    description=TEXT["weather.action.place_add"],
                    input_schema=_object(
                        {
                            "name": {"type": "string"},
                            "lat": {"type": "number"},
                            "lon": {"type": "number"},
                            "search": {"type": "string", "description": TEXT["weather.action.place_add.arg.search"]},
                            "country": {"type": "string", "description": TEXT["weather.action.place_add.arg.country"]},
                            "share": {"type": "boolean"},
                            "group": {"type": "string"},
                        },
                        ["name"],
                    ),
                    autonomy="silent",
                ),
                ActionSpec(
                    name="place_remove",
                    description=TEXT["weather.action.place_remove"],
                    input_schema=_object({"name": {"type": "string"}}, ["name"]),
                    autonomy="silent",
                ),
            ],
            tables=TABLES,
        )

    async def run(self) -> None:
        """Hands the kernel the places, then keeps every place's weather
        fresh: a pass every minute fetches what is due."""
        self._publish_places()
        while True:
            await self.refresh()
            await asyncio.sleep(60)

    # Storage

    @property
    def db(self) -> apsw.Connection:
        if self._conn is None:
            conn = open_db(self.ctx.data_dir / "weather.db")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(SCHEMA)
            self._conn = conn
            self._seed()
        return self._conn

    def _seed(self) -> None:
        """Makes the config's places, owned by its group, and removes those
        the config no longer has. The config wins over any change."""
        group = self.options.get("group", "home")
        configured = self.options.get("places") or {}
        now = self.ctx.now().isoformat()
        for row in self.db.execute("SELECT * FROM places WHERE origin = 'config' AND removed_at IS NULL").fetchall():
            if row["name"] not in configured:
                self.db.execute("UPDATE places SET removed_at = ? WHERE id = ?", (now, row["id"]))
        for name, point in configured.items():
            clean = _clean_name(name)
            existing = self.db.execute("SELECT id FROM places WHERE origin = 'config' AND name = ? AND removed_at IS NULL", (clean,)).fetchone()
            if existing is None:
                self.db.execute(
                    "INSERT INTO places (id, name, lat, lon, group_id, origin, created_at) VALUES (?, ?, ?, ?, ?, 'config', ?)",
                    (f"pl_{secrets.token_hex(4)}", clean, float(point["lat"]), float(point["lon"]), group, now),
                )
            else:
                self.db.execute("UPDATE places SET lat = ?, lon = ?, group_id = ? WHERE id = ?", (float(point["lat"]), float(point["lon"]), group, existing["id"]))

    def _places(self) -> list[Place]:
        return [_place(r) for r in self.db.execute("SELECT * FROM places WHERE removed_at IS NULL ORDER BY origin DESC, name").fetchall()]

    def _visible(self, user: str | None, group: str | None) -> list[Place]:
        """The places a caller sees: their own, and their groups'."""
        groups = set(self.ctx.member_groups(user)) if user else set()
        if group is not None:
            groups.add(group)
        return [p for p in self._places() if (p.user is not None and p.user == user and p.group is None) or (p.group in groups)]

    # Fetching

    async def refresh(self) -> None:
        """One pass: fetches every place's conditions and forecast that are
        due, records the hours that passed, and hands over what changed. A
        call that fails is tried again after RETRY_AFTER; the conditions it
        leaves behind leave weather_now once they are STALE_AFTER old."""
        changed = False
        for place in self._places():
            weather = self._weather.setdefault(place.id, Weather())
            now = self.ctx.now()
            if weather.forecast_due is None or weather.forecast_due <= now:
                changed |= await self._attempt(place, weather, self._fetch_forecast)
            if weather.now_due is None or weather.now_due <= now:
                changed |= await self._attempt(place, weather, self._fetch_now)
        stale = {pid for pid, w in self._weather.items() if w.observed is not None and self.ctx.now() - w.observed > STALE_AFTER}
        if changed or stale != self._published_stale:
            self._publish()

    async def _attempt(self, place: Place, weather: Weather, fetch: Any) -> bool:
        """Runs one fetch, and says whether it brought anything."""
        try:
            await fetch(place, weather)
        except OverBudget:
            log.warning("the day's %.0f Open-Meteo calls are nearly spent; %s's %s waits for tomorrow", self.daily_calls, place.name, fetch.__name__)
            self._defer(weather, fetch, datetime.combine(self.ctx.now().date() + timedelta(days=1), datetime.min.time(), self.ctx.now().tzinfo))
            return False
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as err:
            log.warning("weather for %s: %s", place.name, err)
            self._defer(weather, fetch, self.ctx.now() + RETRY_AFTER)
            return False
        return True

    def _defer(self, weather: Weather, fetch: Any, until: datetime) -> None:
        """Puts off the one fetch that didn't happen; the other keeps its time."""
        if fetch == self._fetch_forecast:
            weather.forecast_due = until
        else:
            weather.now_due = until

    async def _fetch_forecast(self, place: Place, weather: Weather) -> None:
        """The hourly and daily forecast with air quality, from the start of
        the day before the latest recorded hour (92 days back for a new
        place) to 16 days ahead; records every passed hour huvu hasn't."""
        now = self.ctx.now()
        last = self._last_recorded(place)
        past_days = MAX_PAST_DAYS if last is None else min(MAX_PAST_DAYS, max(2, math.ceil((now.timestamp() - last) / 86400) + 1))
        hourly_vars = [api for _, api, _ in WEATHER]
        daily_vars = [api for _, api, _ in DAILY]
        air_vars = [api for _, api, _ in AIR]
        self._reserve(_weight(len(hourly_vars) + len(daily_vars), past_days + FORECAST_DAYS) + _weight(len(air_vars), past_days + AIR_FORECAST_DAYS))
        forecast = await self._get(
            FORECAST_URL,
            place,
            {"hourly": ",".join(hourly_vars), "daily": ",".join(daily_vars), "past_days": past_days, "forecast_days": FORECAST_DAYS},
        )
        air = await self._get(AIR_URL, place, {"hourly": ",".join(air_vars), "past_days": past_days, "forecast_days": AIR_FORECAST_DAYS})
        tz = _zone(forecast)
        weather.timezone, weather.timezone_name, weather.elevation_m = tz, forecast.get("timezone"), forecast.get("elevation")
        air_by_time = {t: row for t, row in _series(air["hourly"], AIR)}
        hours = []
        for t, row in _series(forecast["hourly"], WEATHER):
            moment = datetime.fromtimestamp(t, tz)
            row |= air_by_time.get(t) or {name: None for name, _, _ in AIR}
            hours.append((t, moment, row))
        for t, moment, row in hours:
            if t <= now.timestamp() and (last is None or t > last):
                await self.ctx.record("history", {"place": place.name, "place_time": _iso(moment), **row}, at=moment, user=place.scope()["user_id"], group=place.group)
        yesterday = (now.astimezone(tz) - timedelta(days=1)).date()
        weather.hourly = [
            {"place": place.name, "time": _iso(moment), "time_ts": t, "date": moment.date().isoformat(), **row, **place.scope()}
            for t, moment, row in hours
            if moment.date() >= yesterday
        ]
        daily = []
        for t, row in _series(forecast["daily"], DAILY):
            day = datetime.fromtimestamp(t, tz).date()
            if day < yesterday:
                continue
            for name in DAILY_TIMES:
                row[name] = _iso(datetime.fromtimestamp(row[name], tz)) if row[name] is not None else None
            daily.append({"place": place.name, "date": day.isoformat(), **row, **place.scope()})
        weather.daily = daily
        weather.updated = now
        weather.forecast_due = now + FORECAST_EVERY

    async def _fetch_now(self, place: Place, weather: Weather) -> None:
        """Conditions now, Open-Meteo's latest 15 minutes, with the air of
        the hour from the forecast."""
        current_vars = [api for _, api, _ in WEATHER]
        self._reserve(_weight(len(current_vars), 1))
        answer = await self._get(FORECAST_URL, place, {"current": ",".join(current_vars), "forecast_days": 1})
        tz = _zone(answer)
        current = answer["current"]
        observed = datetime.fromtimestamp(current["time"], tz)
        row = {name: _value(current.get(api), type) for name, api, type in WEATHER}
        hour = observed.replace(minute=0, second=0, microsecond=0).timestamp()
        air = next((h for h in weather.hourly if h["time_ts"] == hour), None)
        row |= {name: air[name] if air is not None else None for name, _, _ in AIR}
        weather.timezone = weather.timezone or tz
        weather.now = {"place": place.name, "observed_at": _iso(observed), **row, **place.scope()}
        weather.observed = observed
        weather.now_due = self.ctx.now() + NOW_EVERY

    def _reserve(self, weight: float) -> None:
        """Counts the calls of one fetch against the day's, before any is
        made, so a fetch never spends half its calls and is refused the
        rest."""
        today = self.ctx.now().date()
        spent = self._spent.get(today, 0.0)
        if spent + weight > self.daily_calls:
            raise OverBudget()
        self._spent = {today: spent + weight}

    async def _get(self, url: str, place: Place, params: dict[str, Any]) -> dict[str, Any]:
        """One call to Open-Meteo for a place."""
        query = {"latitude": place.lat, "longitude": place.lon, "timezone": "auto", "timeformat": "unixtime", **params}
        response = await self.client.get(url, params=query)
        if response.status_code != 200:
            reason = response.json().get("reason") if response.headers.get("content-type", "").startswith("application/json") else response.text[:200]
            raise httpx.HTTPStatusError(f"Open-Meteo answered {response.status_code}: {reason}", request=response.request, response=response)
        return response.json()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self._transport, timeout=30, headers={"User-Agent": "huvu"})
        return self._client

    def _last_recorded(self, place: Place) -> float | None:
        """The time of the latest hour recorded for a place under its owner,
        so a place added again, or huvu coming back, records no hour twice."""
        user, group = place.owner()
        rows = self.ctx.rows("history", "place = ? AND user_id IS ? AND group_id IS ?", (place.name, user, group), order="ts DESC", limit=1)
        return rows[0]["ts"] if rows else None

    # Publishing

    def _publish_places(self) -> None:
        rows = []
        for place in self._places():
            weather = self._weather.get(place.id) or Weather()
            rows.append(
                {
                    "name": place.name,
                    "lat": place.lat,
                    "lon": place.lon,
                    "elevation_m": weather.elevation_m,
                    "timezone": weather.timezone_name,
                    "found": place.found,
                    "added_by": place.user,
                    "from_config": place.origin == "config",
                    "updated_at": weather.updated.isoformat() if weather.updated else None,
                    **place.scope(),
                }
            )
        self.ctx.replace("places", rows)

    def _publish(self) -> None:
        """Hands over every table, every place's rows together. A table no
        place has data for yet stays unknown rather than empty, unless
        there are no places at all."""
        live = {p.id: self._weather.get(p.id) or Weather() for p in self._places()}
        now = self.ctx.now()
        stale = {pid for pid, w in live.items() if w.observed is not None and now - w.observed > STALE_AFTER}
        self._publish_places()
        if not live or any(w.observed is not None for w in live.values()):
            self.ctx.replace("now", [w.now for pid, w in live.items() if w.now is not None and pid not in stale])
        if not live or any(w.updated is not None for w in live.values()):
            self.ctx.replace("hourly", [row for w in live.values() for row in w.hourly])
            self.ctx.replace("daily", [row for w in live.values() for row in w.daily])
        self._published_stale = stale

    # Actions

    async def act(self, name: str, args: dict[str, Any], caller: Caller) -> Result:
        handlers = {
            "place_add": self._add,
            "place_remove": self._remove,
        }
        handler = handlers.get(name)
        if handler is None:
            raise SourceError(f"weather has no action {name!r}")
        return await handler(args, caller)

    async def _add(self, args: dict[str, Any], caller: Caller) -> Result:
        name = _clean_name(args["name"])
        if caller.user is None and caller.group is None:
            raise SourceError("a place belongs to someone; add it from a person's conversation")
        if caller.user is None:
            group = caller.group
        elif args.get("share"):
            group = self._share_group(caller.user, args.get("group"))
        else:
            group = None
        if name in {p.name for p in self._visible(caller.user, caller.group)} | {p.name for p in self._visible(None, group)}:
            raise SourceError(f"a place named {name!r} exists already; remove it first, or pick another name")
        if len(self._places()) >= self.places_max:
            raise SourceError(f"huvu keeps the weather for at most {self.places_max} places, to stay in Open-Meteo's free tier; remove one first")
        lat, lon, found, others = await self._point(args)
        place = Place(id=f"pl_{secrets.token_hex(4)}", name=name, lat=lat, lon=lon, found=found, user=caller.user, group=group, origin="added")
        self.db.execute(
            "INSERT INTO places (id, name, lat, lon, found, user_id, group_id, origin, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'added', ?)",
            (place.id, name, lat, lon, found, place.user, group, self.ctx.now().isoformat()),
        )
        # Fetched at once, so the tables have it when the model looks.
        await self.refresh()
        weather = self._weather.get(place.id)
        data: dict[str, Any] = {
            "name": name,
            "lat": lat,
            "lon": lon,
            "owner": place.user or f"group {group}",
            "shared_with": group if place.user else None,
            "fetched": weather is not None and weather.updated is not None,
        }
        if found is not None:
            data |= {"found": found, "other_matches": others}
        return Result(data=data)

    async def _point(self, args: dict[str, Any]) -> tuple[float, float, str | None, list[str]]:
        """The coordinates given, or the most populous match the geocoder
        finds for `search` (the name by default), with the other matches."""
        given = [k for k in ("lat", "lon") if args.get(k) is not None]
        if len(given) == 1:
            raise SourceError("give lat and lon together, or neither and huvu looks the name up")
        if given:
            lat, lon = float(args["lat"]), float(args["lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise SourceError("lat must be within -90 and 90, lon within -180 and 180")
            return lat, lon, None, []
        query: dict[str, Any] = {"name": args.get("search") or args["name"], "count": GEOCODE_CANDIDATES, "language": "en", "format": "json"}
        if args.get("country"):
            query["countryCode"] = str(args["country"]).upper()
        response = await self.client.get(GEOCODE_URL, params=query)
        response.raise_for_status()
        results = response.json().get("results") or []
        if not results:
            raise SourceError(f"Open-Meteo's geocoder knows no place called {query['name']!r}; give lat and lon")
        ranked = sorted(results, key=lambda r: r.get("population") or 0, reverse=True)
        best = ranked[0]
        return float(best["latitude"]), float(best["longitude"]), _match(best), [_match(r) for r in ranked[1:5]]

    def _share_group(self, user: str, given: str | None) -> str:
        """The group to share with: the one named, or the person's only one."""
        groups = self.ctx.member_groups(user)
        if given is not None and given not in groups:
            raise SourceError(f"{user} is not a member of {given!r}; their groups: {', '.join(groups) or 'none'}")
        if given is None and len(groups) != 1:
            raise SourceError(f"say which group to share with: {', '.join(groups) or 'they are a member of none'}")
        return given or groups[0]

    async def _remove(self, args: dict[str, Any], caller: Caller) -> Result:
        wanted = _clean_name(args["name"])
        place = next((p for p in self._visible(caller.user, caller.group) if p.name == wanted), None)
        if place is None:
            names = ", ".join(p.name for p in self._visible(caller.user, caller.group)) or "none"
            raise SourceError(f"no place named {wanted!r}; the ones here: {names}")
        if place.origin == "config":
            raise SourceError(f"{place.name} comes from huvu's config; change it there")
        if place.user is not None and place.user != caller.user:
            raise SourceError(f"{place.name} is {place.user}'s; only they can remove it")
        self.db.execute("UPDATE places SET removed_at = ? WHERE id = ?", (self.ctx.now().isoformat(), place.id))
        self._weather.pop(place.id, None)
        self._publish()
        return Result(data={"removed": place.name, "history": "kept"})


def _weight(variables: int, days: int) -> float:
    """How many calls Open-Meteo counts one request as."""
    return max(1.0, variables / 10) * max(1.0, days / 14)


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _place(row: Row) -> Place:
    return Place(
        id=row["id"],
        name=row["name"],
        lat=row["lat"],
        lon=row["lon"],
        found=row["found"],
        user=row["user_id"],
        group=row["group_id"],
        origin=row["origin"],
    )


def _series(block: dict[str, Any], columns: list[tuple[str, str, ColumnType]]) -> list[tuple[int, dict[str, Any]]]:
    """Open-Meteo's arrays as one row per time, by huvu's column names."""
    times = block["time"]
    return [(int(t), {name: _value((block.get(api) or [None] * len(times))[i], type) for name, api, type in columns}) for i, t in enumerate(times)]


def _value(value: Any, type: ColumnType) -> Any:
    if value is None:
        return None
    if type == "integer":
        return round(value)
    if type == "boolean":
        return bool(value)
    return value


def _zone(answer: dict[str, Any]) -> tzinfo:
    """The place's own timezone, as Open-Meteo found it."""
    try:
        return ZoneInfo(answer["timezone"])
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        from datetime import timezone

        return timezone(timedelta(seconds=answer.get("utc_offset_seconds", 0)))


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="minutes")


def _match(result: dict[str, Any]) -> str:
    """A geocoder match as a line: the name, its region and country, and
    how many live there."""
    parts = [result.get("name"), result.get("admin1"), result.get("country")]
    line = ", ".join(str(p) for p in parts if p)
    if result.get("population"):
        line += f" (population {result['population']})"
    return line


def _clean_name(raw: str) -> str:
    name = " ".join(str(raw).split()).lower()
    if not name:
        raise SourceError("a place needs a name")
    return name
