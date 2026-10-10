"""One Blue Star account: its ACs, their state, and the link to the cloud.

The hub keeps one MQTT connection to AWS IoT, subscribed to every AC's
state topic, and holds each AC's latest state. It also asks the REST API
for the device list every minute, since only the API says whether an AC is
online: an AC with its mains off sends nothing, and MQTT can't tell that
from an AC with nothing to say.

A command publishes the desired state and waits until the AC reports it,
so a command that returns has happened, and one the AC didn't take fails.
An AC that is offline when a command is sent would take it later from its
shadow, perhaps hours later, so a command to an offline AC is refused.

The hub logs in only at the start and when the API refuses its session.
The AWS IoT keys from a login don't expire, so a reconnect reuses them.

This module imports nothing from Home Assistant, so it runs on its own too.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from . import api
from .api import BlueStarError, Login
from .const import COMMAND_SOURCE, CONFIRM_SECONDS, POLL_SECONDS, STATE_REQUEST
from .iot import MqttConnection, MqttError

_LOGGER = logging.getLogger(__name__)


@dataclass
class Thing:
    """One AC, as the API lists it, with its latest state."""

    thing_id: str
    name: str
    model: dict[str, Any]
    firmware: str
    state: dict[str, Any] = field(default_factory=dict)
    state_ms: int = 0
    connected: bool = False
    # `src` and `lcmd` of the last report that came from a change, since a
    # report the integration asked for carries neither.
    changed_by: str | None = None
    changed: str | None = None


@dataclass
class _Waiter:
    thing_id: str
    desired: dict[str, Any]
    future: asyncio.Future[None]


def _same(reported: Any, wanted: Any) -> bool:
    """Whether a reported value is the wanted one. "25.0" is 25."""
    try:
        return float(reported) == float(wanted)
    except (TypeError, ValueError):
        return reported == wanted


class Hub:
    def __init__(self, http: aiohttp.ClientSession, auth_id: str, password: str) -> None:
        self._http = http
        self._auth_id = auth_id
        self._password = password
        self._login: Login | None = None
        self._connection: MqttConnection | None = None
        self._listeners: list[Callable[[], None]] = []
        self._waiters: list[_Waiter] = []
        self.things: dict[str, Thing] = {}
        self.online = False

    async def start(self) -> Login:
        """Log in and list the ACs. Raises InvalidAuth or BlueStarError."""
        self._login = await api.login(self._http, self._auth_id, self._password)
        await self.refresh()
        return self._login

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call `listener` on every change. Returns a function that removes it."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    async def refresh(self) -> None:
        """Read the device list: the ACs, whether each is online, and its state."""
        listing = await self._listing()
        states = listing.get("states") or {}
        for item in listing.get("things") or []:
            thing_id = str(item["thing_id"])
            thing = self.things.get(thing_id)
            if thing is None:
                thing = Thing(
                    thing_id=thing_id,
                    name=str((item.get("user_config") or {}).get("name") or thing_id),
                    model=item.get("model_config") or {},
                    firmware=str(item.get("f_ver", "")),
                )
                self.things[thing_id] = thing
            entry = states.get(thing_id) or {}
            self._apply(thing, entry.get("state") or {}, int(entry.get("state_ts") or 0))
            # The API's flag can lag a report that came over MQTT. An AC that
            # reported after its last connect or disconnect is online.
            changed_ms = int(entry.get("conn_ts") or 0)
            thing.connected = bool(entry.get("connected")) or thing.state_ms > changed_ms
        self._notify()

    async def poll(self) -> None:
        """Refresh the device list every minute, for good."""
        while True:
            await asyncio.sleep(POLL_SECONDS)
            try:
                await self.refresh()
            except BlueStarError as error:
                _LOGGER.warning("Blue Star device list failed: %s", error)

    async def run(self) -> None:
        """Keep the MQTT connection up, for good."""
        delay = 5
        while True:
            started = time.monotonic()
            try:
                await self._connect_and_read()
            except (BlueStarError, MqttError) as error:
                _LOGGER.warning("Blue Star MQTT connection lost: %s", error)
            if time.monotonic() - started > 300:
                delay = 5
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

    async def command(self, thing_id: str, desired: dict[str, Any]) -> None:
        """Send `desired` to the AC and wait until it reports it."""
        thing = self.things[thing_id]
        connection = self._connection
        if connection is None:
            raise BlueStarError("not connected to Blue Star's cloud")
        if not thing.connected:
            raise BlueStarError(f"{thing.name} is offline; its mains may be off")
        if all(_same(thing.state.get(key), value) for key, value in desired.items()):
            return
        payload = {
            "state": {
                "desired": {
                    **desired,
                    "ts": int(time.time() * 1000),
                    "src": COMMAND_SOURCE,
                },
            },
        }
        waiter = _Waiter(
            thing_id=thing_id,
            desired=desired,
            future=asyncio.get_running_loop().create_future(),
        )
        self._waiters.append(waiter)
        try:
            await connection.publish(f"$aws/things/{thing_id}/shadow/update", json.dumps(payload).encode())
            async with asyncio.timeout(CONFIRM_SECONDS):
                await waiter.future
        except MqttError as error:
            raise BlueStarError(f"sending the command failed: {error}") from error
        except TimeoutError as error:
            raise BlueStarError(f"{thing.name} didn't confirm the change within {CONFIRM_SECONDS} s") from error
        finally:
            self._waiters.remove(waiter)

    async def _listing(self) -> dict[str, Any]:
        """The device list, logging in again once if the session was refused."""
        if self._login is None:
            self._login = await api.login(self._http, self._auth_id, self._password)
        try:
            listing = await api.things(self._http, self._login.session)
        except BlueStarError:
            self._login = await api.login(self._http, self._auth_id, self._password)
            listing = await api.things(self._http, self._login.session)
        return listing

    async def _connect_and_read(self) -> None:
        if self._login is None:
            self._login = await api.login(self._http, self._auth_id, self._password)
        credentials = self._login
        connection = await MqttConnection.open(
            self._http,
            api.mqtt_url(credentials),
            f"u-{credentials.session}",
        )
        try:
            for thing_id in self.things:
                await connection.subscribe(f"things/{thing_id}/state/reported")
            self._connection = connection
            self.online = True
            self._notify()
            for thing_id in self.things:
                await connection.publish(f"things/{thing_id}/control", b'{"fpsh": 1}')
            async for topic, payload in connection.messages():
                self._on_message(topic, payload)
        finally:
            self._connection = None
            self.online = False
            self._notify()
            await connection.close()

    def _on_message(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        thing = self.things.get(parts[1]) if len(parts) == 4 else None
        if thing is None or parts[2:] != ["state", "reported"]:
            return
        try:
            state = json.loads(payload)
        except ValueError:
            _LOGGER.warning("Blue Star sent a report that isn't JSON on %s", topic)
            return
        thing.connected = True
        self._apply(thing, state, int(state.get("ts") or time.time() * 1000))
        self._notify()

    def _apply(self, thing: Thing, state: dict[str, Any], state_ms: int) -> None:
        """Take a state that is newer than the one held, and settle its waiters."""
        if not state or state_ms < thing.state_ms:
            return
        thing.state = state
        thing.state_ms = state_ms
        if state.get("src") != STATE_REQUEST:
            thing.changed_by = state.get("src")
            thing.changed = state.get("lcmd") or None
        for waiter in self._waiters:
            matches = all(_same(state.get(key), value) for key, value in waiter.desired.items())
            if waiter.thing_id == thing.thing_id and matches and not waiter.future.done():
                waiter.future.set_result(None)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()
