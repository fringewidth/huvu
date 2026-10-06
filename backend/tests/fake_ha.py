"""A Home Assistant on the other end of the driver's WebSocket, in memory.

It answers the commands the driver sends, the way HA does, and pushes
state changes to its subscribers. A script call runs the script's effects
(state changes in the call's context) before it answers, as HA does.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

from .fakes import FakeClock


class Dropped(ConnectionError):
    pass


class FakeTransport:
    def __init__(self, ha: FakeHA) -> None:
        self._ha = ha
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()

    async def send(self, message: str) -> None:
        self._ha.handle(self, json.loads(message))

    async def recv(self) -> str:
        message = await self.inbox.get()
        if message is None:
            raise Dropped("Home Assistant went away")
        return message

    async def close(self) -> None:
        pass

    def put(self, message: dict[str, Any]) -> None:
        self.inbox.put_nowait(json.dumps(message))


class FakeHA:
    OWN_USER = "ha-huvu"

    def __init__(self, clock: FakeClock, token: str = "secret") -> None:
        self.clock = clock
        self.token = token
        self.admin = True
        self.areas = {"bedroom": "Bedroom", "living_room": "Living room", "hall": "Hall"}
        self.states: dict[str, dict[str, Any]] = {}
        self.entries: dict[str, dict[str, Any]] = {}
        self.exposed: set[str] = set()
        self.scripts: dict[str, dict[str, Any]] = {}
        # Script object id -> [(entity_id, state, attributes)] it sets.
        self.effects: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
        self.responses: dict[str, Any] = {}
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[dict[str, Any]] = []
        self.commands: list[str] = []
        self.transport: FakeTransport | None = None
        self._subs: dict[str, list[int]] = {}

    async def connect(self, url: str) -> FakeTransport:
        self.transport = FakeTransport(self)
        self._subs = {}
        self.transport.put({"type": "auth_required", "ha_version": "2026.10.0"})
        return self.transport

    def drop(self) -> None:
        assert self.transport is not None
        self.transport.inbox.put_nowait(None)

    # Setting up the house

    def entity(self, entity_id: str, state: str, *, area: str | None = None, labels: list[str] | None = None, exposed: bool = True, aliases: list[str] | None = None, **attributes: Any) -> None:
        attributes.setdefault("friendly_name", entity_id.partition(".")[2].replace("_", " ").title())
        self.states[entity_id] = self._state(entity_id, state, attributes, {"id": _ctx(), "parent_id": None, "user_id": None})
        self.entries[entity_id] = {"entity_id": entity_id, "area_id": area, "device_id": None, "labels": labels or [], "aliases": aliases or []}
        if exposed:
            self.exposed.add(entity_id)

    def script(self, object_id: str, *, alias: str, description: str = "", fields: dict[str, Any] | None = None, labels: list[str] | None = None, area: str | None = None, sequence: list[Any] | None = None) -> None:
        entity_id = f"script.{object_id}"
        self.entity(entity_id, "off", area=area, labels=labels, friendly_name=alias, last_triggered=None)
        self.scripts[entity_id] = {"alias": alias, "description": description, "fields": fields or {}, "sequence": sequence or [], "mode": "queued"}

    def _state(self, entity_id: str, state: str, attributes: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        now = self.clock.now.isoformat()
        return {"entity_id": entity_id, "state": state, "attributes": attributes, "last_changed": now, "last_updated": now, "context": context}

    # What happens in the house

    def set(self, entity_id: str, state: str, *, user: str | None = None, parent: str | None = None, context: dict[str, Any] | None = None, **attributes: Any) -> dict[str, Any]:
        """Changes a state and tells subscribers. Returns the context."""
        old = self.states.get(entity_id)
        context = context or {"id": _ctx(), "parent_id": parent, "user_id": user}
        merged = {**((old or {}).get("attributes") or {}), **attributes}
        new = self._state(entity_id, state, merged, context)
        if old is not None and old["state"] == state:
            new["last_changed"] = old["last_changed"]
        self.states[entity_id] = new
        self.fire("state_changed", {"entity_id": entity_id, "old_state": old, "new_state": new}, context)
        return context

    def fire(self, event_type: str, data: dict[str, Any], context: dict[str, Any] | None = None) -> None:
        assert self.transport is not None
        event = {"event_type": event_type, "data": data, "origin": "LOCAL", "time_fired": self.clock.now.isoformat(), "context": context or {"id": _ctx(), "parent_id": None, "user_id": None}}
        for sub in self._subs.get(event_type, []):
            self.transport.put({"id": sub, "type": "event", "event": event})

    # The protocol

    def handle(self, transport: FakeTransport, message: dict[str, Any]) -> None:
        if message["type"] == "auth":
            ok = message["access_token"] == self.token
            transport.put({"type": "auth_ok", "ha_version": "2026.10.0"} if ok else {"type": "auth_invalid", "message": "Invalid access token"})
            return
        self.commands.append(message["type"])
        handler = getattr(self, "_" + message["type"].replace("/", "__"), None)
        if handler is None:
            transport.put({"id": message["id"], "type": "result", "success": False, "error": {"code": "unknown_command", "message": "Unknown command."}})
            return
        try:
            result = handler(message)
        except KeyError as err:
            transport.put({"id": message["id"], "type": "result", "success": False, "error": {"code": "not_found", "message": f"{err} not found"}})
            return
        transport.put({"id": message["id"], "type": "result", "success": True, "result": result})

    def _subscribe_events(self, message: dict[str, Any]) -> None:
        self._subs.setdefault(message["event_type"], []).append(message["id"])

    def _auth__current_user(self, message: dict[str, Any]) -> dict[str, Any]:
        return {"id": self.OWN_USER, "name": "huvu", "is_owner": False, "is_admin": self.admin}

    def _config__area_registry__list(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"area_id": k, "name": v, "aliases": [], "floor_id": None} for k, v in self.areas.items()]

    def _config__device_registry__list(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    def _config__entity_registry__list(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        return [{k: v for k, v in e.items() if k != "aliases"} for e in self.entries.values()]

    def _config__entity_registry__get_entries(self, message: dict[str, Any]) -> dict[str, Any]:
        return {e: self.entries.get(e) for e in message["entity_ids"]}

    def _get_states(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        return list(self.states.values())

    def _homeassistant__expose_entity__list(self, message: dict[str, Any]) -> dict[str, Any]:
        return {"exposed_entities": {e: {"conversation": e in self.exposed, "cloud.alexa": False} for e in self.states}}

    def _script__config(self, message: dict[str, Any]) -> dict[str, Any]:
        return {"config": self.scripts[message["entity_id"]]}

    def _history__history_during_period(self, message: dict[str, Any]) -> dict[str, Any]:
        return {e: self.history.get(e, []) for e in message["entity_ids"]}

    def _call_service(self, message: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(message)
        assert message["domain"] == "script"
        object_id = message["service"]
        context = {"id": _ctx(), "parent_id": None, "user_id": self.OWN_USER}
        self.set(f"script.{object_id}", "on", context=context, last_triggered=self.clock.now.isoformat())
        for entity_id, state, attributes in self.effects.get(object_id, []):
            self.set(entity_id, state, context=context, **attributes)
        self.set(f"script.{object_id}", "off", context=context)
        return {"context": context, "response": self.responses.get(object_id)}


def _ctx() -> str:
    return secrets.token_hex(13)
