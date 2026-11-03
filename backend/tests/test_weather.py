"""The weather source: places and who sees them, the tables Open-Meteo fills,
every past hour recorded once, stale conditions, and the free tier's budget."""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest

from huvu.config import Config
from huvu.kernel import Kernel
from huvu.source import SourceError
from huvu.spec import Caller
from huvu.tools import ToolError
from huvu_drivers.weather import AIR_URL, GEOCODE_URL, WeatherSource

ME = Caller(kind="hermes", session="s-me", user="me")
MOM = Caller(kind="hermes", session="s-mom", user="mom")
IST = ZoneInfo("Asia/Kolkata")
HOME = {"home": {"lat": 12.97, "lon": 77.59}}

pytestmark = pytest.mark.usefixtures("family")


class FakeOpenMeteo:
    """Answers the forecast, air quality and geocoding APIs the way
    Open-Meteo does, from the test's clock, and counts the calls."""

    def __init__(self, clock) -> None:
        self.clock = clock
        self.calls: list[dict[str, str]] = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        query = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        self.calls.append({"url": url, **query})
        if self.down:
            return httpx.Response(502, text="bad gateway")
        if url == GEOCODE_URL:
            return httpx.Response(200, json={"results": [
                {"name": "Panaji", "latitude": 15.49, "longitude": 73.82, "admin1": "Goa", "country": "India", "population": 114405},
                {"name": "Goa", "latitude": 21.7, "longitude": 69.6, "admin1": "Gujarat", "country": "India"},
            ]})
        base = {"timezone": "Asia/Kolkata", "utc_offset_seconds": 19800, "elevation": 914.0}
        now = self.clock.now.astimezone(IST)
        if "current" in query:
            quarter = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
            current = {var: 1 for var in query["current"].split(",")} | {"time": int(quarter.timestamp()), "temperature_2m": 24.5}
            return httpx.Response(200, json=base | {"current": current})
        start = datetime.combine(now.date() - timedelta(days=int(query["past_days"])), datetime.min.time(), IST)
        days = int(query["past_days"]) + int(query["forecast_days"])
        hours = [int((start + timedelta(hours=h)).timestamp()) for h in range(days * 24)]
        hourly: dict[str, list] = {"time": hours}
        for var in query["hourly"].split(","):
            hourly[var] = [float(i % 24) for i in range(len(hours))]
        if url == AIR_URL:
            # The air quality model's forecast stops short of its last days.
            hourly["us_aqi"] = [80] * (len(hours) - 48) + [None] * 48
            return httpx.Response(200, json=base | {"hourly": hourly})
        midnights = [int((start + timedelta(days=d)).timestamp()) for d in range(days)]
        daily: dict[str, list] = {"time": midnights}
        for var in query["daily"].split(","):
            daily[var] = [m + 6 * 3600 for m in midnights] if var in ("sunrise", "sunset") else [3.0] * days
        return httpx.Response(200, json=base | {"hourly": hourly, "daily": daily})

    def weight(self) -> int:
        return len([c for c in self.calls if c["url"] != GEOCODE_URL])


@pytest.fixture
def meteo(clock) -> FakeOpenMeteo:
    return FakeOpenMeteo(clock)


@pytest.fixture
def options() -> dict:
    return {"places": HOME}


@pytest.fixture
async def kernel(tmp_path, clock, home, hermes, meteo, options):
    source = WeatherSource(options, transport=httpx.MockTransport(meteo))
    k = Kernel(Config(data_dir=tmp_path, hermes_home=tmp_path / "hermes"), hermes=hermes, sources={"home": home, "weather": source}, clock=clock)
    await home.run()
    await k.triggers.load()
    yield k
    await k.triggers.stop()
    await k.waker.stop()
    await k.conversations.close()
    k.data.close()
    k.db.close()


@pytest.fixture
def weather(kernel) -> WeatherSource:
    return kernel.registry.get("weather")


async def sql(kernel, caller: Caller, query: str) -> list:
    return (await kernel.tools.call("data_sql", {"sql": query}, caller)).data["rows"]


async def test_the_configs_place_has_its_weather_for_the_household(weather, kernel):
    await weather.refresh()
    for caller in (ME, MOM):
        assert await sql(kernel, caller, "SELECT name, timezone, from_config FROM weather_places") == [["home", "Asia/Kolkata", 1]]
        assert await sql(kernel, caller, "SELECT place, observed_at, temperature_c FROM weather_now") == [["home", "2026-10-03T23:45+05:30", 24.5]]
    # From the start of yesterday to 16 days ahead, in the place's own days.
    hours = await sql(kernel, ME, "SELECT count(*), min(time), max(date) FROM weather_hourly")
    assert hours == [[17 * 24, "2026-10-02T00:00+05:30", "2026-10-18"]]
    days = await sql(kernel, ME, "SELECT count(*), min(date), max(sunrise) FROM weather_daily")
    assert days == [[17, "2026-10-02", "2026-10-18T06:00+05:30"]]
    # Air quality beyond its forecast is unknown, not zero: the API's 7 days
    # end 9 days before the weather's, and the model's own forecast short of that.
    assert await sql(kernel, ME, "SELECT count(*) FROM weather_hourly WHERE us_aqi IS NULL") == [[9 * 24 + 48]]


async def test_every_past_hour_is_recorded_once(weather, kernel, clock, meteo):
    await weather.refresh()
    first = await sql(kernel, ME, "SELECT count(*), min(at), max(at) FROM weather_history WHERE place = 'home'")
    # A new place starts 92 days back, up to the hour that has begun.
    assert first == [[92 * 24 + 24, "2026-07-03T00:00:00+05:30", "2026-10-03T23:00:00+05:30"]]
    clock.advance(hours=2)
    await weather.refresh()
    assert meteo.calls[-2]["past_days"] == "2"
    after = await sql(kernel, ME, "SELECT count(*), count(DISTINCT ts), max(place_time) FROM weather_history")
    assert after == [[92 * 24 + 26, 92 * 24 + 26, "2026-10-04T01:00+05:30"]]


async def test_a_place_added_for_a_person_is_theirs_until_shared(weather, kernel):
    await weather.refresh()
    result = await weather.act("place_add", {"name": "Goa"}, ME)
    assert result.data["found"] == "Panaji, Goa, India (population 114405)"
    assert result.data["other_matches"] == ["Goa, Gujarat, India"]
    assert result.data["fetched"] is True
    assert await sql(kernel, ME, "SELECT count(*) FROM weather_now WHERE place = 'goa'") == [[1]]
    for table in ("places WHERE name", "now WHERE place", "hourly WHERE place", "history WHERE place"):
        assert await sql(kernel, MOM, f"SELECT count(*) FROM weather_{table} = 'goa'") == [[0]]
    await weather.act("place_add", {"name": "trip", "lat": 48.85, "lon": 2.35, "share": True}, ME)
    assert await sql(kernel, MOM, "SELECT added_by FROM weather_places WHERE name = 'trip'") == [["me"]]
    with pytest.raises(SourceError, match="only they can remove it"):
        await weather.act("place_remove", {"name": "trip"}, MOM)
    with pytest.raises(SourceError, match="config"):
        await weather.act("place_remove", {"name": "home"}, ME)
    assert (await weather.act("place_remove", {"name": "goa"}, ME)).data == {"removed": "goa", "history": "kept"}
    assert await sql(kernel, ME, "SELECT count(*) FROM weather_now WHERE place = 'goa'") == [[0]]
    assert (await sql(kernel, ME, "SELECT count(*) > 0 FROM weather_history WHERE place = 'goa'")) == [[1]]


@pytest.mark.parametrize("options", [{"places": HOME, "places_max": 2}])
async def test_places_stop_at_the_most_huvu_keeps(weather):
    await weather.act("place_add", {"name": "office", "lat": 12.93, "lon": 77.62}, ME)
    with pytest.raises(SourceError, match="at most 2 places"):
        await weather.act("place_add", {"name": "goa"}, ME)


async def test_conditions_older_than_an_hour_are_unknown_not_kept(weather, kernel, clock, meteo):
    await weather.refresh()
    meteo.down = True
    clock.advance(minutes=50)
    await weather.refresh()
    assert await sql(kernel, ME, "SELECT count(*) FROM weather_now") == [[1]]
    clock.advance(minutes=30)
    await weather.refresh()
    assert await sql(kernel, ME, "SELECT count(*) FROM weather_now") == [[0]]
    # The forecast still stands, with when it was fetched.
    assert await sql(kernel, ME, "SELECT updated_at FROM weather_places") == [["2026-10-03T23:50:00+05:30"]]
    meteo.down = False
    clock.advance(minutes=5)
    await weather.refresh()
    assert await sql(kernel, ME, "SELECT count(*) FROM weather_now") == [[1]]


@pytest.mark.parametrize("options", [{"places": HOME, "daily_calls": 40}])
async def test_a_fetch_past_the_days_budget_waits_for_the_next_day(weather, kernel, clock, meteo):
    # A new place's forecast and air, 92 days back, weigh about 41, so huvu
    # makes neither call; conditions now, about 2.3, still fit.
    await weather.refresh()
    assert [c.get("current") is not None for c in meteo.calls] == [True]
    with pytest.raises(ToolError, match="hasn't handed this table over"):
        await sql(kernel, ME, "SELECT count(*) FROM weather_hourly")
    assert await sql(kernel, ME, "SELECT count(*) FROM weather_now") == [[1]]
    clock.advance(minutes=20)
    await weather.refresh()
    assert [c.get("current") is not None for c in meteo.calls] == [True, True]
