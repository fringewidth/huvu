"""The Home Assistant source.

One WebSocket connection to HA carries everything (client.py). On connect
the driver subscribes to state changes and registry changes, then loads
the registries, the Assist exposure list, every state and the config of
every exposed script. It keeps:

1. **A mirror of every exposed entity's state**, in memory. The live
   tables answer from it (`ha_entities`), so nothing is written per state
   change but the history row. HA sends every state again on reconnect,
   so the mirror needs no persistence.
2. **The exposed scripts, as actions** (scripts.py). They are cached in
   the source's folder, so the actions exist before HA connects. When the
   scripts change in HA (a registry event, or the 5-minute refresh), the
   driver calls `ctx.respec()`.
3. **History for the dream**, as rows in its tables (TABLES): every change
   of an exposed entity with who caused it (history.py), every script run,
   and numeric sensors as 5-minute buckets.

What HA exposes to Assist is what huvu sees. Nothing else can be read,
searched or worked.

Options (huvu.toml):

    [[sources]]
    name = "ha"
    driver = "huvu_drivers.ha:HomeAssistant"
    options = { url = "http://homeassistant.local:8123", users = { "<HA user id>" = "mom" } }

`token_env` names the environment variable with the long-lived access
token (default HA_TOKEN; backend/.env works too). `exposure = "all"` reads
every entity instead of the Assist list, for development. `assistant`
picks another assistant's exposure list.
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime
from typing import Any

from huvu.config import secret
from huvu.prompts import HA_GUIDE, TEXT
from huvu.source import Source, SourceError
from huvu.spec import Caller, ColumnSpec, Result, SourceSpec, TableSpec

from .client import Connect, HAClient, HAError, connect_websocket
from .history import Actor, Attribution, SensorBuckets
from .scripts import Script, action, from_config, problems_of, usable

TOKEN_ENV = "HA_TOKEN"
CACHE = "scripts.json"
# How often the registries, the exposure list and the scripts are read
# again, for changes HA sends no event for (a script edited in place).
REFRESH_EVERY = 300.0
# Registry events come in bursts; one reload after the burst.
REFRESH_DEBOUNCE = 2.0
FLUSH_EVERY = 30.0
# A script call waits this long for HA to finish the script.
ACT_TIMEOUT = 120.0

REGISTRY_EVENTS = ("entity_registry_updated", "area_registry_updated", "device_registry_updated", "label_registry_updated")
# The attributes that say what a person chose, per domain. A change in one
# of them is a change row even when the state stays the same.
TRACKED = {
    "light": ("brightness", "color_temp_kelvin", "rgb_color", "effect"),
    "climate": ("temperature", "target_temp_high", "target_temp_low", "fan_mode", "swing_mode", "preset_mode"),
    "fan": ("percentage", "preset_mode", "oscillating", "direction"),
    "media_player": ("source", "media_title", "sound_mode", "is_volume_muted"),
    "cover": ("current_position", "current_tilt_position"),
    "vacuum": ("fan_speed",),
    "water_heater": ("temperature", "operation_mode"),
    "humidifier": ("humidity", "mode"),
    "select": (),
}
# Left out of entity_attributes: pictures, icons and bit masks.
NOISE = {"entity_picture", "icon", "supported_features", "supported_color_modes", "attribution", "friendly_name"}

_ACTOR = ColumnSpec(name="actor", type="text", description=TEXT["ha.column.actor"])
_HA_USER = ColumnSpec(name="ha_user", type="text", description=TEXT["ha.column.ha_user"])
_CONTEXT = ColumnSpec(name="context_id", type="text", description=TEXT["ha.column.context_id"])
_AREA = ColumnSpec(name="area", type="text", description=TEXT["ha.column.area"])

TABLES = [
    TableSpec(
        name="changes",
        description=TEXT["ha.table.changes"],
        columns=[
            ColumnSpec(name="entity_id", type="text", description=TEXT["ha.table.changes.column.entity_id"]),
            ColumnSpec(name="name", type="text", description=TEXT["ha.table.changes.column.name"]),
            ColumnSpec(name="domain", type="text", description=TEXT["ha.table.changes.column.domain"]),
            ColumnSpec(name="device_class", type="text", description=TEXT["ha.table.changes.column.device_class"]),
            _AREA,
            ColumnSpec(name="from_state", type="text", description=TEXT["ha.table.changes.column.from_state"]),
            ColumnSpec(name="to_state", type="text", description=TEXT["ha.table.changes.column.to_state"]),
            ColumnSpec(name="attributes", type="json", description=TEXT["ha.table.changes.column.attributes"]),
            ColumnSpec(name="assumed", type="boolean", description=TEXT["ha.table.changes.column.assumed"]),
            _ACTOR,
            _HA_USER,
            _CONTEXT,
        ],
        indexes=[["entity_id", "ts"], ["area", "ts"], ["actor", "ts"]],
    ),
    TableSpec(
        name="script_runs",
        description=TEXT["ha.table.script_runs"],
        columns=[
            ColumnSpec(name="script", type="text", description=TEXT["ha.table.script_runs.column.script"]),
            ColumnSpec(name="name", type="text", description=TEXT["ha.table.script_runs.column.name"]),
            _AREA,
            ColumnSpec(name="fields", type="json", description=TEXT["ha.table.script_runs.column.fields"]),
            _ACTOR,
            _HA_USER,
            _CONTEXT,
        ],
        indexes=[["script", "ts"]],
    ),
    TableSpec(
        name="sensor_5min",
        description=TEXT["ha.table.sensor_5min"],
        columns=[
            ColumnSpec(name="entity_id", type="text", description=TEXT["ha.table.sensor_5min.column.entity_id"]),
            _AREA,
            ColumnSpec(name="device_class", type="text", description=TEXT["ha.table.sensor_5min.column.device_class"]),
            ColumnSpec(name="unit", type="text", description=TEXT["ha.table.sensor_5min.column.unit"]),
            ColumnSpec(name="mean", type="real", description=TEXT["ha.table.sensor_5min.column.mean"]),
            ColumnSpec(name="low", type="real", description=TEXT["ha.table.sensor_5min.column.low"]),
            ColumnSpec(name="high", type="real", description=TEXT["ha.table.sensor_5min.column.high"]),
            ColumnSpec(name="last", type="real", description=TEXT["ha.table.sensor_5min.column.last"]),
            ColumnSpec(name="changes", type="integer", description=TEXT["ha.table.sensor_5min.column.changes"]),
        ],
        indexes=[["entity_id", "ts"]],
    ),
    TableSpec(
        name="entities",
        description=TEXT["ha.table.entities"],
        columns=[
            ColumnSpec(name=n, type=t, description=TEXT[f"ha.table.entities.column.{n}"])
            for n, t in (
                ("entity_id", "text"),
                ("name", "text"),
                ("domain", "text"),
                ("device_class", "text"),
                ("area", "text"),
                ("device", "text"),
                ("aliases", "json"),
                ("state", "text"),
                ("number", "real"),
                ("attributes", "json"),
                ("assumed", "boolean"),
                ("last_changed", "text"),
            )
        ],
        live=True,
    ),
    TableSpec(
        name="scripts",
        description=TEXT["ha.table.scripts"],
        columns=[
            ColumnSpec(name=n, type=t, description=TEXT[f"ha.table.scripts.column.{n}"])
            for n, t in (
                ("action", "text"),
                ("name", "text"),
                ("description", "text"),
                ("area", "text"),
                ("fields", "json"),
                ("targets", "json"),
                ("usable", "boolean"),
            )
        ],
        live=True,
    ),
    TableSpec(
        name="setup",
        description=TEXT["ha.table.setup"],
        columns=[
            ColumnSpec(name=n, type=t, description=TEXT[f"ha.table.setup.column.{n}"])
            for n, t in (("connected", "boolean"), ("ha_version", "text"), ("problems", "json"))
        ],
        live=True,
    ),
]


class HomeAssistant(Source):
    def __init__(self, options: dict[str, Any] | None = None, *, connect: Connect | None = None) -> None:
        super().__init__(options)
        opts = self.options
        self._url = str(opts.get("url", "http://homeassistant.local:8123"))
        self._token_env = str(opts.get("token_env", TOKEN_ENV))
        self._assistant = str(opts.get("assistant", "conversation"))
        self._expose_all = opts.get("exposure", "assist") == "all"
        self._users: dict[str, str] = dict(opts.get("users", {}))
        self._connect = connect or connect_websocket
        self._client: HAClient | None = None
        self._connected = False
        self._states: dict[str, dict[str, Any]] = {}
        self._exposed: set[str] = set()
        self._area: dict[str, str | None] = {}
        self._device: dict[str, str | None] = {}
        self._aliases: dict[str, list[str]] = {}
        # Object id -> script. None until the cache or HA has been read.
        self._scripts: dict[str, Script] | None = None
        self._problems: list[str] = []
        self._logged: set[str] = set()
        # Script entity -> (huvu user, args) of each call huvu has waiting
        # in HA, oldest first.
        self._inflight: dict[str, list[tuple[str | None, dict[str, Any]]]] = {}
        self._attribution = Attribution(None, self._users)
        self._buckets = SensorBuckets()
        self._refresh = asyncio.Event()

    # The spec

    def describe(self) -> SourceSpec:
        name = self.ctx.name
        return SourceSpec(
            name=name,
            description=TEXT["ha.description"],
            guide=HA_GUIDE.format(name=name),
            actions=[action(s) for s in self._known_scripts().values() if usable(s)],
            tables=TABLES,
        )

    def _known_scripts(self) -> dict[str, Script]:
        if self._scripts is None:
            cache = self.ctx.data_dir / CACHE
            cached = json.loads(cache.read_text()) if cache.exists() else []
            self._scripts = {s["object_id"]: Script(**s) for s in cached}
        return self._scripts

    def _spec_key(self) -> str:
        return json.dumps([action(s).model_dump(mode="json") for s in self._known_scripts().values() if usable(s)], sort_keys=True)

    # The connection

    async def run(self) -> None:
        token = secret(self._token_env)
        if not token:
            raise SourceError(f"{self._token_env} is not set: make a long-lived access token for huvu's own HA user and put it there")
        client = await HAClient.open(self._url, token, self._connect)
        self._client = client
        self._buckets = SensorBuckets()
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(client.listen(), name="ha:listen")
                await client.subscribe("state_changed", self._on_state_changed)
                for event_type in REGISTRY_EVENTS:
                    await client.subscribe(event_type, self._on_registry_changed)
                await self._load(fresh=True)
                self._connected = True
                self.ctx.log.info("connected to Home Assistant %s: %d exposed entities, %d scripts", client.version, len(self._exposed), len(self._known_scripts()))
                tasks.create_task(self._refresher(), name="ha:refresh")
                tasks.create_task(self._flusher(), name="ha:flush")
        except ExceptionGroup as group:
            # The kernel logs the first cause, which says more than the group.
            raise group.exceptions[0] from None
        finally:
            self._connected = False
            self._client = None
            await client.close()

    async def _load(self, *, fresh: bool) -> None:
        """Reads the registries, the exposure list and the scripts. `fresh`
        (on connect) also takes every state; a refresh adds only the states
        of newly exposed entities, since the subscription keeps the rest."""
        client = self._require()
        me = await client.call("auth/current_user")
        if not me.get("is_admin"):
            raise SourceError("huvu's Home Assistant user must be an administrator, to read the registries and the exposure list")
        self._attribution.own_user = me["id"]
        areas = {a["area_id"]: a["name"] for a in await client.call("config/area_registry/list")}
        devices = {d["id"]: d for d in await client.call("config/device_registry/list")}
        entries = {e["entity_id"]: e for e in await client.call("config/entity_registry/list")}
        states = {s["entity_id"]: s for s in await client.call("get_states")}
        exposed = set(states) if self._expose_all else await self._exposure(client, set(states))
        for entity_id in exposed:
            entry = entries.get(entity_id) or {}
            device = devices.get(entry.get("device_id") or "") or {}
            self._area[entity_id] = areas.get(entry.get("area_id") or device.get("area_id") or "")
            self._device[entity_id] = device.get("name_by_user") or device.get("name")
        self._aliases = await self._aliases_of(client, sorted(exposed))
        if fresh:
            self._states = {e: states[e] for e in exposed}
            now = self.ctx.now()
            for entity_id, state in self._states.items():
                number = _number(state["state"])
                if entity_id.startswith("sensor.") and number is not None:
                    self._buckets.add(entity_id, number, now)
        else:
            for entity_id in exposed - self._exposed:
                self._states[entity_id] = states[entity_id]
            for entity_id in self._exposed - exposed:
                self._states.pop(entity_id, None)
        self._exposed = exposed
        scripts: dict[str, Script] = {}
        problems: list[str] = []
        if self._attribution.own_user in self._users:
            problems.append("the token belongs to a family member's HA user; make a separate HA user for huvu, or huvu's actions look like theirs")
        for entity_id in sorted(e for e in exposed if e.startswith("script.")):
            try:
                config = (await client.call("script/config", entity_id=entity_id)).get("config") or {}
            except HAError as err:
                problems.append(f"couldn't read the config of {entity_id}: {err}")
                config = {}
            labels = list((entries.get(entity_id) or {}).get("labels") or [])
            script = from_config(entity_id, _name(states[entity_id]), config, labels, self._area.get(entity_id), exposed)
            scripts[script.object_id] = script
            problems.extend(problems_of(script))
        self._set_problems(problems)
        await self._set_scripts(scripts)

    async def _exposure(self, client: HAClient, known: set[str]) -> set[str]:
        listed = (await client.call("homeassistant/expose_entity/list")).get("exposed_entities") or {}
        return {e for e, assistants in listed.items() if assistants.get(self._assistant)} & known

    async def _aliases_of(self, client: HAClient, entity_ids: list[str]) -> dict[str, list[str]]:
        aliases: dict[str, list[str]] = {}
        try:
            entries = await client.call("config/entity_registry/get_entries", entity_ids=entity_ids)
        except HAError:
            entries = {}
        for entity_id, entry in (entries or {}).items():
            names = [a for a in (entry or {}).get("aliases") or [] if a]
            if names:
                aliases[entity_id] = names
        return aliases

    async def _set_scripts(self, scripts: dict[str, Script]) -> None:
        before = self._spec_key()
        self._scripts = scripts
        (self.ctx.data_dir / CACHE).write_text(json.dumps([s.as_dict() for s in scripts.values()], indent=1))
        if self._spec_key() != before:
            await self.ctx.respec()

    def _set_problems(self, problems: list[str]) -> None:
        self._problems = problems
        for problem in problems:
            if problem not in self._logged:
                self.ctx.log.warning("Home Assistant setup: %s", problem)
                self._logged.add(problem)

    async def _refresher(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._refresh.wait(), REFRESH_EVERY)
            except TimeoutError:
                pass
            await asyncio.sleep(REFRESH_DEBOUNCE)
            self._refresh.clear()
            await self._load(fresh=False)

    async def _flusher(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_EVERY)
            await self._write_buckets(self._buckets.flush(self.ctx.now()))

    def _require(self) -> HAClient:
        if self._client is None:
            raise SourceError("Home Assistant is not connected right now; try again in a minute")
        return self._client

    # What HA sends

    async def _on_registry_changed(self, event: dict[str, Any]) -> None:
        self._refresh.set()

    async def _on_state_changed(self, event: dict[str, Any]) -> None:
        data = event.get("data") or {}
        entity_id = data.get("entity_id") or ""
        old, new = data.get("old_state"), data.get("new_state")
        context = (new or old or {}).get("context") or event.get("context") or {}
        # last_updated moves on an attribute change too; last_changed only
        # when the state does.
        at = _time((new or {}).get("last_updated") or event.get("time_fired")) or self.ctx.now()
        script = self._script_of(entity_id)
        if script is not None and _ran(old, new):
            await self._script_ran(script, context, at)
        if entity_id in self._exposed:
            if new is None:
                self._states.pop(entity_id, None)
            else:
                self._states[entity_id] = new
                await self._changed(entity_id, old, new, context, at)
            # ha_entities changed even when no row was recorded (a sensor
            # reading goes only into its bucket).
            self.ctx.nudge()

    async def _script_ran(self, script: Script, context: dict[str, Any], at: datetime) -> None:
        pending = self._inflight.get(script.entity_id) or []
        own = bool(context.get("user_id")) and context.get("user_id") == self._attribution.own_user
        user, args = pending.pop(0) if own and pending else (None, None)
        actor = self._attribution.actor(context, huvu_user=user)
        row = {
            "script": script.object_id,
            "name": script.name,
            "area": script.area,
            "fields": args,
            "actor": actor.kind,
            "ha_user": actor.ha_user,
            "context_id": context.get("id"),
        }
        await self.ctx.record("script_runs", row, at=at, user=actor.user)

    async def _changed(self, entity_id: str, old: dict[str, Any] | None, new: dict[str, Any], context: dict[str, Any], at: datetime) -> None:
        domain = entity_id.partition(".")[0]
        number = _number(new["state"])
        # A script's own on and off are script_runs rows, not changes.
        if domain == "sensor" and number is not None:
            await self._write_buckets(self._buckets.add(entity_id, number, at))
        elif domain != "script":
            if domain == "sensor":
                await self._write_buckets(self._buckets.drop(entity_id, at))
            await self._record_change(entity_id, domain, old, new, context, at)

    async def _record_change(self, entity_id: str, domain: str, old: dict[str, Any] | None, new: dict[str, Any], context: dict[str, Any], at: datetime) -> None:
        before = old["state"] if old else None
        state_moved = before != new["state"]
        chosen = _tracked(domain, new)
        if not state_moved and chosen == _tracked(domain, old):
            return
        actor = self._attribution.actor(context, huvu_user=self._only_inflight_user())
        attributes = new.get("attributes") or {}
        device_class = attributes.get("device_class")
        row = {
            "entity_id": entity_id,
            "name": _name(new),
            "domain": domain,
            "device_class": device_class,
            "area": self._area.get(entity_id),
            "from_state": before,
            "to_state": new["state"],
            "attributes": chosen or None,
            "assumed": bool(attributes.get("assumed_state")),
            "actor": actor.kind,
            "ha_user": actor.ha_user,
            "context_id": context.get("id"),
        }
        await self.ctx.record("changes", row, at=at, user=actor.user)

    def _only_inflight_user(self) -> str | None:
        """The huvu user of the one call huvu has waiting in HA, if there
        is exactly one. A change in huvu's own context that no script run
        claimed is most likely that call's."""
        calls = [user for pending in self._inflight.values() for user, _ in pending]
        return calls[0] if len(calls) == 1 else None

    async def _write_buckets(self, rows: list[tuple[str, datetime, dict[str, Any]]]) -> None:
        for entity_id, start, values in rows:
            attributes = (self._states.get(entity_id) or {}).get("attributes") or {}
            row = {"entity_id": entity_id, "area": self._area.get(entity_id), "device_class": attributes.get("device_class"), "unit": attributes.get("unit_of_measurement"), **values}
            await self.ctx.record("sensor_5min", row, at=start)

    def _script_of(self, entity_id: str) -> Script | None:
        script = self._known_scripts().get(entity_id.partition(".")[2]) if entity_id.startswith("script.") else None
        return script if script is not None and script.entity_id == entity_id else None

    # The contract

    def live(self, table: str) -> list[dict[str, Any]]:
        readers = {"entities": self._entity_rows, "scripts": self._script_rows, "setup": self._setup_rows}
        return readers[table]()

    def _entity_rows(self) -> list[dict[str, Any]]:
        if not self._connected:
            raise SourceError("Home Assistant is not connected right now; ha_setup says why")
        states = dict(self._states)
        rows = []
        for entity_id in sorted(states):
            state = states[entity_id]
            domain = entity_id.partition(".")[0]
            if domain == "script":
                continue
            attributes = state.get("attributes") or {}
            rows.append(
                {
                    "entity_id": entity_id,
                    "name": _name(state),
                    "domain": domain,
                    "device_class": attributes.get("device_class"),
                    "area": self._area.get(entity_id),
                    "device": self._device.get(entity_id),
                    "aliases": self._aliases.get(entity_id) or None,
                    "state": state["state"],
                    "number": _number(state["state"]),
                    "attributes": {k: v for k, v in attributes.items() if k not in NOISE} or None,
                    "assumed": bool(attributes.get("assumed_state")),
                    "last_changed": state.get("last_changed"),
                }
            )
        return rows

    def _script_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "action": f"{self.ctx.name}_{script.object_id}",
                "name": script.name,
                "description": script.description,
                "area": script.area,
                "fields": script.fields or None,
                "targets": script.targets or None,
                "usable": usable(script),
            }
            for script in list(self._known_scripts().values())
        ]

    def _setup_rows(self) -> list[dict[str, Any]]:
        client = self._client
        return [{"connected": self._connected, "ha_version": client.version if client else None, "problems": list(self._problems)}]

    async def act(self, name: str, args: dict[str, Any], caller: Caller) -> Result:
        script = self._known_scripts().get(name)
        if script is None or not usable(script):
            raise SourceError(f"{self.ctx.name} has no script {name!r}")
        client = self._require()
        pending = self._inflight.setdefault(script.entity_id, [])
        call = (caller.user, dict(args))
        pending.append(call)
        try:
            result = await client.call(
                "call_service", domain="script", service=script.object_id, service_data=args, return_response=True, timeout=ACT_TIMEOUT
            )
        except HAError as err:
            raise SourceError(f"Home Assistant refused {script.entity_id}: {err}") from err
        except TimeoutError:
            result = None
        finally:
            if call in pending:
                pending.remove(call)
        data: dict[str, Any] = {"status": "still_running", "script": script.entity_id, "note": f"HA hasn't finished it after {ACT_TIMEOUT:.0f} s"}
        if result is not None:
            context_id = (result.get("context") or {}).get("id")
            self._attribution.remember(context_id, Actor("huvu", caller.user, self._attribution.own_user))
            data = {"status": "done", "script": script.entity_id}
            if result.get("response"):
                data["response"] = result["response"]
        return Result(data=data, labels=["house"])

def _name(state: dict[str, Any]) -> str:
    return str((state.get("attributes") or {}).get("friendly_name") or state.get("entity_id") or "")


def _number(raw: Any) -> float | None:
    try:
        number = float(raw)
    except (TypeError, ValueError):
        number = None
    return number if number is not None and math.isfinite(number) else None


def _time(raw: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(raw)).astimezone() if raw else None
    except ValueError:
        at = None
    return at


def _tracked(domain: str, state: dict[str, Any] | None) -> dict[str, Any]:
    attributes = (state or {}).get("attributes") or {}
    return {k: attributes[k] for k in TRACKED.get(domain, ()) if attributes.get(k) is not None}


def _ran(old: dict[str, Any] | None, new: dict[str, Any] | None) -> bool:
    """A script ran when its last_triggered moved. Its state alone isn't
    enough: a queued script stays on across runs."""
    before = ((old or {}).get("attributes") or {}).get("last_triggered")
    after = ((new or {}).get("attributes") or {}).get("last_triggered")
    return after is not None and after != before
