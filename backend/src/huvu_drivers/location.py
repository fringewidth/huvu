"""Where each person is, from OwnTracks on their phone.

OwnTracks runs on each family member's phone in HTTP mode and posts to
POST /api/ingest/location, with a tracker's token (`huvu tracker add`). In
significant mode it sends a fix every 15 minutes or so, and a transition
the moment the phone enters or leaves a waypoint.

A waypoint is a named circle: a centre and a radius, such as "home, 150 m".
The phone watches its waypoints itself, through the OS's region monitoring,
so an arrival comes at once and costs little battery. huvu holds the master
list and sends each phone the waypoints its person sees. OwnTracks fetches
nothing on its own, so they go in the reply to the phone's next post, as a
`setWaypoints` command. huvu can't tell what a phone holds from what it
sent, since a phone can lose its waypoints. So it asks each phone for its
list now and then, and sends the waypoints again when the list differs.

A waypoint belongs to the person who named it, and they can share it with
one of their groups, whose members' phones then get it too. `home` and
`near_home` come from the config and belong to the household's group.
`near_home` is the early warning: entering it means someone is on their way
back, which is what triggers use in place of an ETA.

Everything here is one person's. Every row is its person's alone, so
nobody else reads it or uses it in a trigger; the members of their groups
see none of it. A waypoint is the exception: one shared with a group shows
to its members.

Every fix and transition is a row in the history tables (`location_fixes`,
`location_transitions`), kept for good for the dream, and read by the model
with data_sql. A transition that changed where huvu thinks the person is
(`changed`) is an arrival or a departure. What is true now is in the live
tables: where each person is (`location_people`) and the waypoints
(`location_waypoints`). The source's own database (`location.db` in its
folder) holds what it works from: the waypoints, where each person is, and
what each phone said it holds.

    [[sources]]
    name = "location"
    driver = "huvu_drivers.location:LocationSource"
    options = { home = { lat = 17.385, lon = 78.4867, radius_m = 150 }, near_home_km = 3, group = "home" }
"""

from __future__ import annotations

import base64
import json
import logging
import math
import secrets
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import apsw

from huvu.db import Row, dumps, loads, open_db
from huvu.prompts import LOCATION_GUIDE, TEXT
from huvu.source import Source, SourceError
from huvu.spec import ActionSpec, Caller, ColumnSpec, Result, SourceSpec, TableSpec

log = logging.getLogger("huvu.source.location")

DEFAULT_RADIUS_M = 150
MIN_RADIUS_M = 50
MAX_RADIUS_M = 50_000
NEAR_HOME_KM = 3.0
# The place of someone inside no waypoint. No waypoint can take the name.
ELSEWHERE = "elsewhere"
# The waypoints the config makes, by id.
HOME = "wp_home"
NEAR_HOME = "wp_near_home"
# How often huvu asks a phone for its waypoints, in the reply to a post.
ASK_EVERY_S = 30 * 60
# How long huvu waits before it sends a phone the same waypoints again,
# for a phone that says it lacks them after they were sent.
RESEND_AFTER_S = 60 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS waypoints (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    radius_m REAL NOT NULL,
    -- The person who named it. NULL for a group's own (home, or one named
    -- at a shared device nobody was recognized at).
    user_id TEXT,
    -- The group it is shared with, or the group that owns it.
    group_id TEXT,
    -- OwnTracks's key for a waypoint: the same tst overwrites it on the phone.
    tst INTEGER NOT NULL UNIQUE,
    -- 'config' (home, near_home) or 'named'.
    origin TEXT NOT NULL,
    created_at TEXT NOT NULL,
    removed_at TEXT
);

-- Each person's latest crossing of each waypoint.
CREATE TABLE IF NOT EXISTS presence (
    user_id TEXT NOT NULL,
    waypoint_id TEXT NOT NULL,
    inside INTEGER NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (user_id, waypoint_id)
);

-- What each tracker holds and was sent.
DROP TABLE IF EXISTS synced;
CREATE TABLE IF NOT EXISTS phones (
    device_id TEXT PRIMARY KEY,
    -- The waypoints the phone last said it holds, as JSON in OwnTracks's
    -- shape. NULL until it first says.
    holds TEXT,
    held_at REAL,
    asked_at REAL,
    -- The waypoints last sent, as JSON in OwnTracks's shape.
    sent TEXT,
    sent_at REAL
);
"""


@dataclass(frozen=True)
class Waypoint:
    id: str
    name: str
    lat: float
    lon: float
    radius_m: float
    user: str | None
    group: str | None
    tst: int
    origin: str

    def owntracks(self) -> dict[str, Any]:
        return {
            "_type": "waypoint",
            "desc": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "rad": int(self.radius_m),
            "tst": self.tst,
        }

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "radius_m": int(self.radius_m),
            "owner": self.user or f"group {self.group}",
            "shared_with": self.group if self.user else None,
            "from_config": self.origin == "config",
        }

    def row(self) -> dict[str, Any]:
        """Its row of location_waypoints. One named by a person is theirs
        alone until they share it; a shared one, and a group's own, show to
        the group's members."""
        return {
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "radius_m": self.radius_m,
            "named_by": self.user,
            "from_config": self.origin == "config",
            "user_id": self.user if self.group is None else None,
            "group_id": self.group,
        }


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_DEVICE = ColumnSpec(name="device", type="text", description=TEXT["location.column.device"])
_RAW = ColumnSpec(name="raw", type="json", description=TEXT["location.column.raw"])

TABLES = [
    TableSpec(
        name="fixes",
        description=TEXT["location.table.fixes"],
        columns=[
            _DEVICE,
            ColumnSpec(name="lat", type="real", description=TEXT["location.table.fixes.column.lat"]),
            ColumnSpec(name="lon", type="real", description=TEXT["location.table.fixes.column.lon"]),
            ColumnSpec(name="accuracy_m", type="real", description=TEXT["location.table.fixes.column.accuracy_m"]),
            ColumnSpec(name="altitude_m", type="real", description=TEXT["location.table.fixes.column.altitude_m"]),
            ColumnSpec(name="speed_kmh", type="real", description=TEXT["location.table.fixes.column.speed_kmh"]),
            ColumnSpec(name="battery", type="integer", description=TEXT["location.table.fixes.column.battery"]),
            ColumnSpec(name="battery_status", type="integer", description=TEXT["location.table.fixes.column.battery_status"]),
            ColumnSpec(name="wifi", type="text", description=TEXT["location.table.fixes.column.wifi"]),
            ColumnSpec(name="connection", type="text", description=TEXT["location.table.fixes.column.connection"]),
            ColumnSpec(name="reason", type="text", description=TEXT["location.table.fixes.column.reason"]),
            ColumnSpec(name="regions", type="json", description=TEXT["location.table.fixes.column.regions"]),
            _RAW,
        ],
        indexes=[["device", "ts"]],
    ),
    TableSpec(
        name="transitions",
        description=TEXT["location.table.transitions"],
        columns=[
            _DEVICE,
            ColumnSpec(name="waypoint", type="text", description=TEXT["location.table.transitions.column.waypoint"]),
            ColumnSpec(name="name_on_phone", type="text", description=TEXT["location.table.transitions.column.name_on_phone"]),
            ColumnSpec(name="event", type="text", description=TEXT["location.table.transitions.column.event"]),
            ColumnSpec(name="changed", type="boolean", description=TEXT["location.table.transitions.column.changed"]),
            ColumnSpec(name="lat", type="real", description=TEXT["location.table.transitions.column.lat"]),
            ColumnSpec(name="lon", type="real", description=TEXT["location.table.transitions.column.lon"]),
            ColumnSpec(name="accuracy_m", type="real", description=TEXT["location.table.transitions.column.accuracy_m"]),
            ColumnSpec(name="stayed_min", type="integer", description=TEXT["location.table.transitions.column.stayed_min"]),
            ColumnSpec(name="delay_min", type="integer", description=TEXT["location.table.transitions.column.delay_min"]),
            _RAW,
        ],
        indexes=[["waypoint", "ts"], ["device", "ts"]],
    ),
    TableSpec(
        name="people",
        description=TEXT["location.table.people"].format(elsewhere=ELSEWHERE),
        columns=[
            ColumnSpec(name=n, type=t, description=TEXT[f"location.table.people.column.{n}"].format(elsewhere=ELSEWHERE))
            for n, t in (
                ("place", "text"),
                ("inside", "json"),
                ("lat", "real"),
                ("lon", "real"),
                ("accuracy_m", "real"),
                ("fix_at", "text"),
                ("distance_home_km", "real"),
                ("battery", "integer"),
                ("wifi", "text"),
            )
        ],
        live=True,
        key=["user_id"],
    ),
    TableSpec(
        name="waypoints",
        description=TEXT["location.table.waypoints"],
        columns=[
            ColumnSpec(name=n, type=t, description=TEXT[f"location.table.waypoints.column.{n}"])
            for n, t in (
                ("name", "text"),
                ("lat", "real"),
                ("lon", "real"),
                ("radius_m", "real"),
                ("named_by", "text"),
                ("from_config", "boolean"),
            )
        ],
        live=True,
    ),
]

class LocationSource(Source):
    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self._conn: apsw.Connection | None = None

    async def run(self) -> None:
        """Hands the kernel the waypoints, and where everyone is, from
        before the last restart."""
        self._publish_waypoints()
        latest = self.ctx.rows("fixes", "id IN (SELECT max(id) FROM location_fixes GROUP BY user_id)", ())
        users = {r["user_id"] for r in latest} | {r["user_id"] for r in self.db.execute("SELECT DISTINCT user_id FROM presence").fetchall()}
        self.ctx.replace("people", [self._person(user) for user in sorted(u for u in users if u is not None)])

    def describe(self) -> SourceSpec:
        return SourceSpec(
            name="location",
            description=TEXT["location.description"],
            guide=LOCATION_GUIDE,
            actions=[
                ActionSpec(
                    name="waypoint_add",
                    description=(
                        TEXT["location.action.waypoint_add"]
                    ),
                    input_schema=_object(
                        {
                            "name": {"type": "string"},
                            "lat": {"type": "number"},
                            "lon": {"type": "number"},
                            "from": {"type": "string"},
                            "until": {"type": "string"},
                            "radius_m": {"type": "number", "description": TEXT["location.action.waypoint_add.arg.radius_m"].format(default=DEFAULT_RADIUS_M)},
                            "share": {"type": "boolean"},
                            "group": {"type": "string"},
                        },
                        ["name"],
                    ),
                    autonomy="silent",
                ),
                ActionSpec(
                    name="waypoint_remove",
                    description=TEXT["location.action.waypoint_remove"],
                    input_schema=_object({"name": {"type": "string"}}, ["name"]),
                    autonomy="silent",
                ),
                ActionSpec(
                    name="waypoint_share",
                    description=TEXT["location.action.waypoint_share"],
                    input_schema=_object({"name": {"type": "string"}, "share": {"type": "boolean"}, "group": {"type": "string"}}, ["name", "share"]),
                    autonomy="silent",
                ),
            ],
            tables=TABLES,
        )

    # Storage

    @property
    def db(self) -> apsw.Connection:
        if self._conn is None:
            conn = open_db(self.ctx.data_dir / "location.db")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(SCHEMA)
            self._conn = conn
            self._seed()
        return self._conn

    def _seed(self) -> None:
        """Makes `home` and `near_home` from the config, or takes them away
        when the config has no home. The config wins over any change."""
        home = self.options.get("home")
        if home is None:
            self.db.execute("UPDATE waypoints SET removed_at = ? WHERE origin = 'config' AND removed_at IS NULL", (self.ctx.now().isoformat(),))
        else:
            group = self.options.get("group", "home")
            near_m = float(self.options.get("near_home_km", NEAR_HOME_KM)) * 1000
            self._upsert_config(HOME, "home", float(home["lat"]), float(home["lon"]), float(home.get("radius_m", DEFAULT_RADIUS_M)), group)
            self._upsert_config(NEAR_HOME, "near_home", float(home["lat"]), float(home["lon"]), near_m, group)

    def _upsert_config(self, wp_id: str, name: str, lat: float, lon: float, radius_m: float, group: str) -> None:
        self.db.execute(
            "INSERT INTO waypoints (id, name, lat, lon, radius_m, user_id, group_id, tst, origin, created_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 'config', ?) "
            "ON CONFLICT (id) DO UPDATE SET lat = excluded.lat, lon = excluded.lon, radius_m = excluded.radius_m, "
            "group_id = excluded.group_id, removed_at = NULL",
            (wp_id, name, lat, lon, radius_m, group, self._new_tst(), self.ctx.now().isoformat()),
        )

    def _new_tst(self) -> int:
        tst = int(self.ctx.now().timestamp())
        while self.db.execute("SELECT 1 FROM waypoints WHERE tst = ?", (tst,)).fetchone():
            tst += 1
        return tst

    def _visible(self, user: str | None, group: str | None = None) -> list[Waypoint]:
        """The waypoints a person sees: their own, and those of the groups
        they're a member of. With no person (a shared device nobody is
        recognized at), the group's. Smallest first."""
        groups = self.ctx.member_groups(user) if user else [group] if group else []
        marks = ", ".join("?" * len(groups))
        rows = self.db.execute(
            f"SELECT * FROM waypoints WHERE removed_at IS NULL AND (user_id = ? OR group_id IN ({marks})) ORDER BY radius_m, name",
            (user, *groups),
        ).fetchall()
        return [_waypoint(r) for r in rows]

    # Data from phones

    async def ingest(self, body: Any, caller: Caller) -> Any:
        """Takes what OwnTracks posts, and answers with the commands for the
        phone: its waypoints, when it lacks them, and now and then a
        request for the list it holds. A payload huvu can't read is logged
        and dropped, not refused: OwnTracks retries a refused post, and the
        bad one would hold up the phone's queue for good."""
        user, device = caller.user, caller.device
        if user is None or device is None:
            raise SourceError("location data needs a person's device")
        for payload in body if isinstance(body, list) else [body]:
            try:
                await self._take(payload, user, device)
            except (KeyError, TypeError, ValueError) as err:
                log.warning("dropped a payload from %s that huvu can't read (%s): %s", device, err, str(payload)[:300])
        await self._publish(user)
        return self._sync(user, device)

    async def _take(self, payload: Any, user: str, device: str) -> None:
        kind = payload.get("_type") if isinstance(payload, dict) else None
        if kind == "location":
            await self._fix(payload, user, device)
        elif kind == "transition":
            await self._transition(payload, user, device)
        elif kind == "waypoints":
            self._held(payload, device)
        elif kind == "encrypted":
            log.warning("%s sends encrypted payloads; turn encryption off in OwnTracks, since TLS covers it", device)

    async def _fix(self, p: dict[str, Any], user: str, device: str) -> None:
        at = _when(p["tst"])
        row = {
            "device": device,
            "lat": float(p["lat"]),
            "lon": float(p["lon"]),
            "accuracy_m": _number(p.get("acc")),
            "altitude_m": _number(p.get("alt")),
            "speed_kmh": _number(p.get("vel")),
            "battery": _number(p.get("batt")),
            "battery_status": _number(p.get("bs")),
            "wifi": p.get("SSID"),
            "connection": p.get("conn"),
            "reason": p.get("t"),
            "regions": p.get("inregions"),
            "raw": p,
        }
        # OwnTracks sends a post again when it didn't hear back.
        if not self.ctx.rows("fixes", "device = ? AND ts = ?", (device, at.timestamp()), limit=1):
            await self.ctx.record("fixes", row, at=at, user=user)

    async def _transition(self, p: dict[str, Any], user: str, device: str) -> None:
        at = _when(p["tst"])
        event = p["event"]
        if event not in ("enter", "leave"):
            raise ValueError(f"a transition's event is enter or leave, not {event!r}")
        name = p.get("desc")
        repeat = self.ctx.rows("transitions", "device = ? AND ts = ? AND name_on_phone IS ? AND event = ?", (device, at.timestamp(), name, event), limit=1)
        # A region made on the phone itself is kept, and changes nothing.
        waypoint = self._match(user, p.get("wtst"), name)
        crossing = await self._cross(user, waypoint, event == "enter", at) if not repeat and waypoint is not None else None
        if not repeat:
            row = {
                "device": device,
                "waypoint": waypoint.name if waypoint else None,
                "name_on_phone": name,
                "event": event,
                "changed": crossing is not None,
                "lat": _number(p.get("lat")),
                "lon": _number(p.get("lon")),
                "accuracy_m": _number(p.get("acc")),
                **(crossing or {}),
                "raw": p,
            }
            await self.ctx.record("transitions", row, at=at, user=user)

    def _match(self, user: str, wtst: Any, desc: Any) -> Waypoint | None:
        """The waypoint a transition is about: by its tst, which huvu set,
        or else by name among the person's. A removed waypoint, still on a
        phone that hasn't been cleared yet, matches nothing."""
        visible = self._visible(user)
        by_tst = next((w for w in visible if wtst is not None and w.tst == int(wtst)), None)
        return by_tst or next((w for w in visible if w.name == desc), None)

    async def _cross(self, user: str, waypoint: Waypoint, entering: bool, at: datetime) -> dict[str, Any] | None:
        """Takes a crossing, and when it changes where the person is,
        returns how late it came and how long they stayed, for its row. One
        older than the latest crossing huvu knows (a queue the phone sent
        late) changes nothing."""
        row = self.db.execute("SELECT * FROM presence WHERE user_id = ? AND waypoint_id = ?", (user, waypoint.id)).fetchone()
        newer = row is None or at.timestamp() > row["ts"]
        if not (newer and (row is None or bool(row["inside"]) != entering)):
            return None
        self.db.execute(
            "INSERT INTO presence (user_id, waypoint_id, inside, ts) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (user_id, waypoint_id) DO UPDATE SET inside = excluded.inside, ts = excluded.ts",
            (user, waypoint.id, int(entering), at.timestamp()),
        )
        crossing: dict[str, Any] = {"delay_min": max(0, int((self.ctx.now() - at).total_seconds() // 60))}
        if row is not None and row["inside"] and not entering:
            crossing["stayed_min"] = int((at - datetime.fromtimestamp(row["ts"], UTC)).total_seconds() // 60)
        return crossing

    def _held(self, p: dict[str, Any], device: str) -> None:
        """Keeps the list of waypoints a phone says it holds, which it
        sends when huvu asks (the `waypoints` command)."""
        held = [
            {"desc": w["desc"], "lat": float(w["lat"]), "lon": float(w["lon"]), "rad": int(w.get("rad") or 0), "tst": int(w["tst"])}
            for w in p.get("waypoints") or []
        ]
        self.db.execute(
            "INSERT INTO phones (device_id, holds, held_at) VALUES (?, ?, ?) "
            "ON CONFLICT (device_id) DO UPDATE SET holds = excluded.holds, held_at = excluded.held_at",
            (device, dumps(held), self.ctx.now().timestamp()),
        )

    def _sync(self, user: str, device: str) -> list[dict[str, Any]]:
        """The commands for a tracker: its waypoints, when the phone lacks
        one or holds one huvu took away, and the `waypoints` command, which
        asks the phone for its list. A phone that never said what it holds
        gets them too. setWaypoints only adds and overwrites, so for a
        waypoint taken away, clearWaypoints goes first; it also clears any
        region made on the phone itself. The same waypoints go out again
        only after RESEND_AFTER_S, so a phone whose list never matches
        can't keep posting."""
        want = [w.owntracks() for w in self._visible(user)]
        now = self.ctx.now().timestamp()
        row = self.db.execute("SELECT * FROM phones WHERE device_id = ?", (device,)).fetchone()
        holds = loads(row["holds"]) if row is not None and row["holds"] is not None else None
        ours = {r["tst"] for r in self.db.execute("SELECT tst FROM waypoints").fetchall()}
        stale = {w["tst"] for w in holds or []} & (ours - {w["tst"] for w in want})
        missing = holds is None or any(not any(_same(w, h) for h in holds) for w in want)
        resent = row is not None and row["sent"] is not None and loads(row["sent"]) == want and now - row["sent_at"] < RESEND_AFTER_S
        commands: list[dict[str, Any]] = []
        if (missing or stale) and not resent:
            if stale:
                commands.append({"_type": "cmd", "action": "clearWaypoints"})
            commands.append({"_type": "cmd", "action": "setWaypoints", "waypoints": {"_type": "waypoints", "waypoints": want}})
            self.db.execute(
                "INSERT INTO phones (device_id, sent, sent_at) VALUES (?, ?, ?) "
                "ON CONFLICT (device_id) DO UPDATE SET sent = excluded.sent, sent_at = excluded.sent_at",
                (device, dumps(want), now),
            )
        # After a setWaypoints the phone says what it took at once.
        if commands or row is None or row["asked_at"] is None or now - row["asked_at"] >= ASK_EVERY_S:
            commands.append({"_type": "cmd", "action": "waypoints"})
            self.db.execute(
                "INSERT INTO phones (device_id, asked_at) VALUES (?, ?) ON CONFLICT (device_id) DO UPDATE SET asked_at = excluded.asked_at",
                (device, now),
            )
        return commands

    # Live tables

    async def _publish(self, user: str) -> None:
        """Hands the kernel the person's row of location_people."""
        self.ctx.put("people", self._person(user))

    def _publish_waypoints(self) -> None:
        """Hands the kernel every waypoint, for location_waypoints."""
        rows = self.db.execute("SELECT * FROM waypoints WHERE removed_at IS NULL ORDER BY radius_m, name").fetchall()
        self.ctx.replace("waypoints", [_waypoint(r).row() for r in rows])

    def _person(self, user: str) -> dict[str, Any]:
        visible = self._visible(user)
        fix = self._latest_fix(user)
        entered = {r["waypoint_id"] for r in self.db.execute("SELECT waypoint_id FROM presence WHERE user_id = ? AND inside = 1", (user,)).fetchall()}
        # Smallest first, as `_visible` sorts them.
        inside = [w for w in visible if w.id in entered]
        row: dict[str, Any] = {"place": inside[0].name if inside else ELSEWHERE, "inside": [w.name for w in inside], "user_id": user}
        if fix is not None:
            home = next((w for w in visible if w.name == "home"), None)
            row |= {
                "lat": fix["lat"],
                "lon": fix["lon"],
                "accuracy_m": fix["accuracy_m"],
                "fix_at": fix["at"],
                "battery": fix["battery"],
                "wifi": fix["wifi"],
                "distance_home_km": round(_metres(fix["lat"], fix["lon"], home.lat, home.lon) / 1000, 2) if home is not None else None,
            }
        return row

    def _latest_fix(self, user: str | None) -> dict[str, Any] | None:
        latest = self.ctx.rows("fixes", "user_id = ?", (user,), order="ts DESC", limit=1)
        return latest[0] if latest else None

    # Actions

    async def act(self, name: str, args: dict[str, Any], caller: Caller) -> Result:
        handlers = {
            "waypoint_add": self._add,
            "waypoint_remove": self._remove,
            "waypoint_share": self._share,
        }
        handler = handlers.get(name)
        if handler is None:
            raise SourceError(f"location has no action {name!r}")
        return await handler(args, caller)

    async def _add(self, args: dict[str, Any], caller: Caller) -> Result:
        name = _clean_name(args["name"])
        if caller.user is None and caller.group is None:
            raise SourceError("a waypoint belongs to someone; name it from a person's conversation")
        if caller.user is None:
            # Named at a shared device nobody is recognized at: the group's.
            group = caller.group
        elif args.get("share"):
            group = self._share_group(caller.user, args.get("group"))
        else:
            group = None
        taken = {w.name for w in self._visible(caller.user, caller.group)} | {w.name for w in self._visible(None, group)}
        if name in taken:
            raise SourceError(f"a waypoint named {name!r} exists already; remove it first, or pick another name")
        radius = float(args.get("radius_m") or DEFAULT_RADIUS_M)
        if not MIN_RADIUS_M <= radius <= MAX_RADIUS_M:
            raise SourceError(f"radius_m must be between {MIN_RADIUS_M} and {MAX_RADIUS_M}")
        lat, lon, about = self._centre(args, caller.user)
        waypoint = Waypoint(
            id=f"wp_{secrets.token_hex(4)}",
            name=name,
            lat=lat,
            lon=lon,
            radius_m=radius,
            user=caller.user,
            group=group,
            tst=self._new_tst(),
            origin="named",
        )
        self.db.execute(
            "INSERT INTO waypoints (id, name, lat, lon, radius_m, user_id, group_id, tst, origin, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (waypoint.id, name, lat, lon, radius, waypoint.user, group, waypoint.tst, waypoint.origin, self.ctx.now().isoformat()),
        )
        self._publish_waypoints()
        return Result(data={**waypoint.public(), "centre_from": about, "reaches_phones": "with each phone's next post"})

    def _centre(self, args: dict[str, Any], user: str | None) -> tuple[float, float, dict[str, Any]]:
        """Where a new waypoint goes: the coordinates given, the centre of
        the person's fixes in a window, or their latest fix."""
        given = [k for k in ("lat", "lon") if args.get(k) is not None]
        window = [k for k in ("from", "until") if args.get(k)]
        if len(given) == 1 or len(window) == 1:
            raise SourceError("give lat and lon together, or from and until together")
        if not given and user is None:
            raise SourceError("nobody is recognized here, so give lat and lon")
        if given:
            lat, lon, about = float(args["lat"]), float(args["lon"]), {"coordinates": "as given"}
        elif window:
            lat, lon, about = self._window_centre(user, _local(args["from"], self.ctx.now()), _local(args["until"], self.ctx.now()))
        else:
            fix = self._latest_fix(user)
            if fix is None:
                raise SourceError(f"{user}'s phone has sent no location yet")
            lat, lon, about = fix["lat"], fix["lon"], {"latest_fix_at": fix["at"], "accuracy_m": fix["accuracy_m"]}
        return lat, lon, about

    def _window_centre(self, user: str | None, start: datetime, end: datetime) -> tuple[float, float, dict[str, Any]]:
        """The median of the person's fixes in a window, which a stray fix
        can't drag, and how far the fixes spread around it."""
        rows = self.ctx.rows("fixes", "user_id = ? AND ts BETWEEN ? AND ?", (user, start.timestamp(), end.timestamp()))
        if not rows:
            raise SourceError(f"{user}'s phone sent no location between {start:%Y-%m-%d %H:%M} and {end:%Y-%m-%d %H:%M}")
        lat = statistics.median(r["lat"] for r in rows)
        lon = statistics.median(r["lon"] for r in rows)
        distances = sorted(_metres(lat, lon, r["lat"], r["lon"]) for r in rows)
        spread = distances[min(len(distances) - 1, int(len(distances) * 0.9))]
        return lat, lon, {"fixes": len(rows), "spread_m": int(spread)}

    def _share_group(self, user: str | None, given: str | None) -> str:
        """The group to share with: the one named, or the person's only one."""
        groups = self.ctx.member_groups(user) if user else []
        if given is not None and given not in groups:
            raise SourceError(f"{user} is not a member of {given!r}; their groups: {', '.join(groups) or 'none'}")
        if given is None and len(groups) != 1:
            raise SourceError(f"say which group to share with: {', '.join(groups) or 'they are a member of none'}")
        return given or groups[0]

    def _named(self, name: str, caller: Caller) -> Waypoint:
        found = next((w for w in self._visible(caller.user, caller.group) if w.name == name.strip().lower()), None)
        if found is None:
            names = ", ".join(w.name for w in self._visible(caller.user, caller.group)) or "none"
            raise SourceError(f"no waypoint named {name!r}; the ones here: {names}")
        return found

    async def _remove(self, args: dict[str, Any], caller: Caller) -> Result:
        waypoint = self._named(args["name"], caller)
        if waypoint.origin == "config":
            raise SourceError(f"{waypoint.name} comes from huvu's config; change it there")
        if waypoint.user is not None and waypoint.user != caller.user:
            raise SourceError(f"{waypoint.name} is {waypoint.user}'s; only they can remove it")
        inside = [r["user_id"] for r in self.db.execute("SELECT user_id FROM presence WHERE waypoint_id = ? AND inside = 1", (waypoint.id,)).fetchall()]
        self.db.execute("UPDATE waypoints SET removed_at = ? WHERE id = ?", (self.ctx.now().isoformat(), waypoint.id))
        self.db.execute("DELETE FROM presence WHERE waypoint_id = ?", (waypoint.id,))
        # Whoever was inside it is somewhere else now.
        for user in inside:
            await self._publish(user)
        self._publish_waypoints()
        return Result(data={"removed": waypoint.name})

    async def _share(self, args: dict[str, Any], caller: Caller) -> Result:
        waypoint = self._named(args["name"], caller)
        if waypoint.user is None or waypoint.user != caller.user:
            raise SourceError(f"only the person who named {waypoint.name} can share it")
        group = self._share_group(caller.user, args.get("group")) if args["share"] else None
        self.db.execute("UPDATE waypoints SET group_id = ? WHERE id = ?", (group, waypoint.id))
        self._publish_waypoints()
        return Result(data={"name": waypoint.name, "shared_with": group})


def setup_link(url: str, user: str, device: str, token: str) -> str:
    """An `owntracks:///config?inline=` link that sets OwnTracks up in one
    tap: HTTP mode to `url`, the tracker's token as the password,
    significant-change monitoring, and remote commands on, so huvu's
    replies can set the phone's waypoints."""
    config = {
        "_type": "configuration",
        "mode": 3,
        "url": url,
        "auth": True,
        "username": user,
        "password": token,
        "deviceId": device,
        "tid": user[:2].upper(),
        "monitoring": 1,
        "cmd": True,
        "extendedData": True,
    }
    return "owntracks:///config?inline=" + base64.b64encode(json.dumps(config).encode()).decode()


def _waypoint(row: Row) -> Waypoint:
    return Waypoint(
        id=row["id"],
        name=row["name"],
        lat=row["lat"],
        lon=row["lon"],
        radius_m=row["radius_m"],
        user=row["user_id"],
        group=row["group_id"],
        tst=row["tst"],
        origin=row["origin"],
    )


def _same(want: dict[str, Any], held: dict[str, Any]) -> bool:
    """Whether a phone holds a waypoint as huvu sent it."""
    return (
        held["tst"] == want["tst"]
        and held["desc"] == want["desc"]
        and held["rad"] == want["rad"]
        and abs(held["lat"] - want["lat"]) < 1e-6
        and abs(held["lon"] - want["lon"]) < 1e-6
    )


def _clean_name(raw: str) -> str:
    name = " ".join(str(raw).split()).lower()
    if not name or len(name) > 60:
        raise SourceError("a waypoint's name is 1 to 60 characters")
    if name == ELSEWHERE:
        raise SourceError(f"{ELSEWHERE!r} means inside no waypoint; pick another name")
    return name


def _when(tst: Any) -> datetime:
    """An OwnTracks timestamp (seconds since the epoch), as an aware time;
    the kernel turns it into the house's time."""
    return datetime.fromtimestamp(float(tst), UTC)


def _local(text: str, now: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError as err:
        raise SourceError(f"can't read the time {text!r}; use ISO 8601 local time, such as 2026-10-05T15:00") from err
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=now.tzinfo)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))
