"""Users, groups and devices.

A user is a person with a username and a password. A group is a set of
people who share devices and trust each other: the household is one, and a
second house would be another. A device is one install of the app. A
personal device (a phone, a browser) belongs to the user who logged in on
it. A shared device (the TV, a speaker) belongs to a group: it pairs with a
code that a member confirms on their phone.

A user is in a group as a member or a guest. Members trust each other
implicitly: they see the group's events, answer its approvals, and pair
its devices. A guest (a babysitter, a visitor) can use the
group's shared devices and be recognized at them, but trusts nothing
implicitly: they don't see the group's events, and they never approve
anything. Every approval a guest causes goes to the group's members.

Each user can have an alias for each other user, such as "mom". The model
names people by the caller's aliases (people_group, huvu_message), and a
notification one user sends shows the sender by the recipient's alias for
them. Without an alias, the person's name stands in.

A tracker is an app that posts a user's own data, added from the command
line: OwnTracks on their phone, or huvu's extension in their Chrome. It has
a token of its own, revoked apart from the phone's login, and it can only
post to a source's ingest route.

Every device has its own token, stored hashed, so a lost phone can be cut
off without logging anyone else out. Phones are taken to be on their user's
person, so approvals, "is it you?" questions and notifications go to
phones.

Every user, and every group but the default one, has a Hermes profile of the
same name, with its own API key and its own token for huvu's MCP server,
which is how the backend knows whose Hermes is calling. The default group
(`home`) uses Hermes's default profile, whose key (HERMES_API_KEY) and MCP
token (HUVU_MCP_TOKEN) come from the environment.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import apsw
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from .db import from_iso, iso
from .hermes import Profile
from .spec import Scope

DeviceKind = Literal["phone", "web", "tv", "speaker", "tracker"]
Role = Literal["member", "guest"]
PERSONAL: set[str] = {"phone", "web"}
HOUSEHOLD: set[str] = {"tv", "speaker"}

# The group that always exists, on Hermes's default profile.
DEFAULT_GROUP = "home"
# The device every request is from when auth is off, for development.
DEV_DEVICE = "dev"
# How long a shared device's pairing code stays good.
PAIRING_TTL = timedelta(minutes=10)
# No 0/O or 1/I, so a code read off a TV can't be mistyped.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# The same rule as a Hermes profile name, since users and groups get one.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# scrypt cost: about 50 ms and 16 MB a check.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


class AuthError(Exception):
    """A login, pairing or permission that failed. The message is safe to
    show the client, and `status` is the HTTP status it answers with: 403
    unless the request named something that isn't there (404), was already
    done (409), ran out of time (410) or left out what it needs (400)."""

    def __init__(self, message: str, status: int = 403) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class User:
    id: str
    name: str
    created_at: datetime

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
        }


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    is_default: bool
    created_at: datetime

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
        }


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    kind: DeviceKind
    # None on a shared device.
    user: str | None
    # The group a shared device belongs to. None on a personal device.
    group: str | None
    created_at: datetime

    @property
    def is_phone(self) -> bool:
        return self.kind == "phone"

    @property
    def is_tracker(self) -> bool:
        return self.kind == "tracker"

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "user": self.user,
            "group": self.group,
            "created_at": self.created_at,
        }


@dataclass
class Pairing:
    id: str
    code: str
    name: str
    kind: DeviceKind
    expires_at: datetime
    # Set once a member confirms the code. The token is handed over once.
    device: Device | None = None
    token: str | None = None


class Accounts:
    def __init__(self, conn: apsw.Connection, now: Callable[[], datetime], *, household_key: str | None, household_mcp_token: str | None) -> None:
        self._conn = conn
        self._now = now
        self._household_key = household_key
        self._household_mcp_token = household_mcp_token
        self._pairings: dict[str, Pairing] = {}
        if self._conn.execute("SELECT 1 FROM groups WHERE is_default = 1").fetchone() is None:
            self._conn.execute(
                "INSERT INTO groups (id, name, is_default, created_at) VALUES (?, ?, 1, ?)",
                (DEFAULT_GROUP, "Home", iso(self._now())),
            )

    # Users

    def add_user(self, user_id: str, name: str, password: str, hermes_key: str) -> str:
        """Adds a user and returns their profile's MCP token. Only its hash
        is kept, so this is the one chance to copy it."""
        self._check_new_name(user_id)
        mcp_token = secrets.token_urlsafe(32)
        self._conn.execute(
            "INSERT INTO users (id, name, password, hermes_key, mcp_token, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, name, hash_password(password), hermes_key, _digest(mcp_token), iso(self._now())),
        )
        return mcp_token

    def set_password(self, user_id: str, password: str) -> None:
        self._require_user(user_id)
        self._conn.execute("UPDATE users SET password = ? WHERE id = ?", (hash_password(password), user_id))

    def set_hermes_key(self, user_id: str, hermes_key: str) -> None:
        self._require_user(user_id)
        self._conn.execute("UPDATE users SET hermes_key = ? WHERE id = ?", (hermes_key, user_id))

    def remove_user(self, user_id: str) -> None:
        """Removes a user, their memberships and every device they're
        logged in on."""
        self._require_user(user_id)
        self._conn.execute("DELETE FROM devices WHERE user_id = ?", (user_id,))
        self._conn.execute("DELETE FROM memberships WHERE user_id = ?", (user_id,))
        self._conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def user(self, user_id: str) -> User | None:
        row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return None if row is None else _user(row)

    def users(self) -> list[User]:
        return [_user(r) for r in self._conn.execute("SELECT * FROM users ORDER BY id").fetchall()]

    def _require_user(self, user_id: str) -> None:
        if self.user(user_id) is None:
            raise AuthError(f"no user {user_id!r}")

    def _check_new_name(self, name: str) -> None:
        """Users and groups share one namespace, since both name Hermes
        profiles. A name someone already calls a person by is taken too,
        so a name always means one person or one group."""
        if not NAME_RE.match(name) or name == "default":
            raise AuthError("a name is lowercase letters, digits, _ and -, starts with a letter or digit, and can't be 'default'")
        if self.user(name) is not None or self.group(name) is not None:
            raise AuthError(f"{name!r} is already a user or a group")
        alias = self._conn.execute("SELECT owner_id, user_id FROM people_aliases WHERE lower(alias) = ?", (name,)).fetchone()
        if alias is not None:
            raise AuthError(f"{alias['owner_id']} calls {alias['user_id']} {name!r}; pick another name or change that alias first", 409)

    # Groups

    def add_group(self, group_id: str, name: str, hermes_key: str) -> str:
        """Adds a group with its own Hermes profile, and returns the
        profile's MCP token, shown this once."""
        self._check_new_name(group_id)
        mcp_token = secrets.token_urlsafe(32)
        self._conn.execute(
            "INSERT INTO groups (id, name, hermes_key, mcp_token, is_default, created_at) VALUES (?, ?, ?, ?, 0, ?)",
            (group_id, name, hermes_key, _digest(mcp_token), iso(self._now())),
        )
        return mcp_token

    def group(self, group_id: str | None) -> Group | None:
        row = self._conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone() if group_id else None
        return None if row is None else _group(row)

    def groups(self) -> list[Group]:
        return [_group(r) for r in self._conn.execute("SELECT * FROM groups ORDER BY is_default DESC, id").fetchall()]

    def default_group(self) -> str:
        return self._conn.execute("SELECT id FROM groups WHERE is_default = 1").fetchone()["id"]

    def join(self, group_id: str, user_id: str, role: Role) -> None:
        """Puts a user in a group, or changes their role there."""
        self._require_user(user_id)
        if self.group(group_id) is None:
            raise AuthError(f"no group {group_id!r}")
        self._conn.execute(
            "INSERT INTO memberships (group_id, user_id, role) VALUES (?, ?, ?) ON CONFLICT (group_id, user_id) DO UPDATE SET role = excluded.role",
            (group_id, user_id, role),
        )

    def leave(self, group_id: str, user_id: str) -> None:
        self._conn.execute("DELETE FROM memberships WHERE group_id = ? AND user_id = ?", (group_id, user_id))

    def role(self, group_id: str | None, user_id: str | None) -> Role | None:
        row = self._conn.execute("SELECT role FROM memberships WHERE group_id = ? AND user_id = ?", (group_id, user_id)).fetchone()
        return row["role"] if row else None

    def memberships(self, user_id: str) -> dict[str, Role]:
        rows = self._conn.execute("SELECT group_id, role FROM memberships WHERE user_id = ? ORDER BY group_id", (user_id,)).fetchall()
        return {r["group_id"]: r["role"] for r in rows}

    def member_groups(self, user_id: str | None) -> list[str]:
        return [g for g, role in self.memberships(user_id).items() if role == "member"] if user_id else []

    def people(self, group_id: str) -> list[User]:
        """Everyone in a group, members and guests."""
        rows = self._conn.execute(
            "SELECT u.* FROM users u JOIN memberships m ON m.user_id = u.id WHERE m.group_id = ? ORDER BY u.id", (group_id,)
        ).fetchall()
        return [_user(r) for r in rows]

    def is_member(self, group_id: str | None, user_id: str | None) -> bool:
        return self.role(group_id, user_id) == "member"

    def members(self, group_id: str) -> list[str]:
        """A group's members, without its guests."""
        rows = self._conn.execute("SELECT user_id FROM memberships WHERE group_id = ? AND role = 'member' ORDER BY user_id", (group_id,)).fetchall()
        return [r["user_id"] for r in rows]

    # What people call each other

    def set_alias(self, owner_id: str, user_id: str, alias: str) -> None:
        """Sets what one user calls another. An empty alias goes back to the
        person's name. The alias can't be what the owner calls anyone else
        (their alias or their name), anyone else's username, or a group's
        id, so it names one person only."""
        self._require_user(owner_id)
        self._require_user(user_id)
        alias = alias.strip()
        taken: dict[str, str] = {g.id: f"the group {g.id}" for g in self.groups()}
        for other in self.users():
            if other.id != user_id:
                taken[other.id] = f"{other.id}'s username"
                taken[self.alias(owner_id, other.id).lower()] = f"what {owner_id} calls {other.id}"
        if alias and alias.lower() in taken:
            raise AuthError(f"{alias!r} is already {taken[alias.lower()]}", 409)
        self._conn.execute("DELETE FROM people_aliases WHERE owner_id = ? AND user_id = ?", (owner_id, user_id))
        if alias:
            self._conn.execute("INSERT INTO people_aliases (owner_id, user_id, alias) VALUES (?, ?, ?)", (owner_id, user_id, alias))

    def alias(self, owner_id: str | None, user_id: str) -> str:
        """What `owner_id` calls a user: their alias, or the user's name.
        With no owner (a shared device nobody is recognized at), the name."""
        row = self._conn.execute("SELECT alias FROM people_aliases WHERE owner_id = ? AND user_id = ?", (owner_id, user_id)).fetchone()
        user = self.user(user_id)
        return row["alias"] if row else (user.name if user else user_id)

    # Trust

    def scope(self, user_id: str | None, group_id: str | None) -> Scope:
        """What a caller may read. A user reads their own things and their
        member groups'. A group profile or an unrecognized shared device
        reads its group's."""
        if user_id is not None:
            groups = tuple(self.member_groups(user_id))
            scope = Scope(user=user_id, groups=groups, house=bool(groups))
        else:
            scope = Scope(user=None, groups=(group_id or self.default_group(),), house=True)
        return scope

    def approvers(self, user_id: str | None, group_id: str | None) -> tuple[str | None, str | None]:
        """Who answers an approval a caller causes: (user, group). A user
        answers their own, unless they're a guest where it happened; then,
        like a request with no user, the group's members answer."""
        guest_group = self._guest_group(user_id, group_id)
        approvers: tuple[str | None, str | None] = (user_id, group_id)
        if user_id is None:
            approvers = (None, group_id or self.default_group())
        elif guest_group is not None:
            approvers = (None, guest_group)
        return approvers

    def _guest_group(self, user_id: str | None, group_id: str | None) -> str | None:
        """The group where a user is only a guest, for this request. On a
        group's shared device, that group. Elsewhere (their own phone), the
        group they're a guest in, if they're a member nowhere."""
        memberships = self.memberships(user_id) if user_id else {}
        found = None
        if group_id is not None and memberships.get(group_id) == "guest":
            found = group_id
        elif group_id is None and memberships and "member" not in memberships.values():
            found = next(iter(memberships))
        return found

    def answers(self, device: Device, user_id: str | None, group_id: str | None) -> bool:
        """Whether a device may answer what is waiting on (user, group): the
        user's own phone, or a member's phone for the group's."""
        if not device.is_phone or device.user is None:
            return False
        return device.user == user_id if user_id is not None else self.is_member(group_id, device.user)

    def reachable(self, user_id: str) -> list[str]:
        """Who a user can send a message to: everyone in a group the user
        is a member of, guests too, but not the user."""
        groups = self.member_groups(user_id)
        marks = ", ".join("?" * len(groups))
        rows = self._conn.execute(
            f"SELECT DISTINCT user_id FROM memberships WHERE group_id IN ({marks}) AND user_id != ? ORDER BY user_id", (*groups, user_id)
        ).fetchall()
        return [r["user_id"] for r in rows]

    def phones(self, user_id: str | None, group_id: str | None = None) -> list[str]:
        """The phones that reach a user, or a group's members when there is
        no user (a shared device or a group trigger)."""
        if user_id is not None:
            rows = self._conn.execute("SELECT id FROM devices WHERE kind = 'phone' AND user_id = ?", (user_id,)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT d.id FROM devices d JOIN memberships m ON m.user_id = d.user_id "
                "WHERE d.kind = 'phone' AND m.group_id = ? AND m.role = 'member'",
                (group_id or self.default_group(),),
            ).fetchall()
        return [r["id"] for r in rows]

    # Hermes profiles

    def profile(self, user_id: str | None, group_id: str | None = None) -> Profile:
        """The Hermes profile that answers for a user, or for a group's
        shared devices. The default group uses the default profile."""
        if user_id is not None:
            row = self._conn.execute("SELECT hermes_key FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise AuthError(f"no user {user_id!r}")
            profile = Profile(name=user_id, key=row["hermes_key"])
        else:
            group = self.group(group_id or self.default_group())
            if group is None:
                raise AuthError(f"no group {group_id!r}")
            profile = Profile(name=None, key=self._household_key) if group.is_default else Profile(name=group.id, key=self._group_key(group.id))
        return profile

    def _group_key(self, group_id: str) -> str | None:
        return self._conn.execute("SELECT hermes_key FROM groups WHERE id = ?", (group_id,)).fetchone()["hermes_key"]

    def mcp_caller(self, token: str) -> tuple[bool, str | None, str | None]:
        """Whose Hermes profile sent this MCP token: (known, user, group).
        The default profile's token gives the default group."""
        digest = _digest(token)
        user = self._conn.execute("SELECT id FROM users WHERE mcp_token = ?", (digest,)).fetchone()
        group = self._conn.execute("SELECT id FROM groups WHERE mcp_token = ?", (digest,)).fetchone()
        caller: tuple[bool, str | None, str | None] = (False, None, None)
        if self._household_mcp_token and hmac.compare_digest(token, self._household_mcp_token):
            caller = (True, None, self.default_group())
        elif user is not None:
            caller = (True, user["id"], None)
        elif group is not None:
            caller = (True, None, group["id"])
        return caller

    # Devices

    def login(self, username: str, password: str, name: str, kind: str) -> tuple[Device, str]:
        """A personal device logs in. Returns the new device and its token."""
        if kind not in PERSONAL:
            raise AuthError(f"a {kind} is a shared device; it pairs with a code instead")
        row = self._conn.execute("SELECT password FROM users WHERE id = ?", (username,)).fetchone()
        # A missing user costs as much time as a wrong password.
        ok = verify_password(password, row["password"] if row else _DUMMY_HASH)
        if row is None or not ok:
            raise AuthError("wrong username or password")
        return self._add_device(name, cast(DeviceKind, kind), username, None)

    def add_tracker(self, user_id: str, name: str) -> tuple[Device, str]:
        """Adds a tracker for a user (OwnTracks, or the Chrome extension).
        Returns the device and its token, shown this once."""
        self._require_user(user_id)
        return self._add_device(name, "tracker", user_id, None)

    def start_pairing(self, name: str, kind: str) -> Pairing:
        """A shared device asks to pair. It shows the code, and polls
        `pairing` until a member confirms it."""
        if kind not in HOUSEHOLD:
            raise AuthError(f"a {kind} is a personal device; it logs in instead")
        self._drop_expired_pairings()
        pairing = Pairing(
            id=secrets.token_urlsafe(16),
            code="".join(secrets.choice(CODE_ALPHABET) for _ in range(8)),
            name=name,
            kind=cast(DeviceKind, kind),
            expires_at=self._now() + PAIRING_TTL,
        )
        self._pairings[pairing.id] = pairing
        return pairing

    def confirm_pairing(self, code: str, by: Device, group_id: str | None = None) -> Device:
        """A member confirms, on their phone, the code a shared device
        shows. The device joins `group_id`, or the member's only group."""
        if not by.is_phone or by.user is None:
            raise AuthError("pair a device from your phone")
        groups = self.member_groups(by.user)
        if group_id is None and len(groups) == 1:
            group_id = groups[0]
        if group_id is None or group_id not in groups:
            raise AuthError(f"say which of your groups the device joins: {', '.join(groups) or 'you are a member of none'}", 400)
        self._drop_expired_pairings()
        wanted = code.strip().upper().replace("-", "").replace(" ", "")
        pairing = next((p for p in self._pairings.values() if p.device is None and hmac.compare_digest(p.code, wanted)), None)
        if pairing is None:
            raise AuthError("no device is showing that code", 404)
        pairing.device, pairing.token = self._add_device(pairing.name, pairing.kind, None, group_id)
        return pairing.device

    def collect_pairing(self, pairing_id: str) -> tuple[Device, str] | None:
        """What the shared device gets when it polls: its device and token
        once confirmed, None while it waits. Raises once the pairing is gone."""
        self._drop_expired_pairings()
        pairing = self._pairings.get(pairing_id)
        if pairing is None:
            raise AuthError("this pairing expired; start again")
        collected: tuple[Device, str] | None = None
        if pairing.device is not None and pairing.token is not None:
            del self._pairings[pairing_id]
            collected = (pairing.device, pairing.token)
        return collected

    def _drop_expired_pairings(self) -> None:
        now = self._now()
        for pairing_id in [p.id for p in self._pairings.values() if p.expires_at < now]:
            del self._pairings[pairing_id]

    def authenticate(self, token: str) -> Device | None:
        row = self._conn.execute("SELECT * FROM devices WHERE token = ?", (_digest(token),)).fetchone() if token else None
        return None if row is None else _device(row)

    def device(self, device_id: str | None) -> Device | None:
        row = self._conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone() if device_id else None
        return None if row is None else _device(row)

    def devices(self) -> list[Device]:
        return [_device(r) for r in self._conn.execute("SELECT * FROM devices ORDER BY created_at").fetchall()]

    def devices_for(self, user_id: str) -> list[Device]:
        """A user's own devices, and the shared devices of their member
        groups."""
        groups = set(self.member_groups(user_id))
        return [d for d in self.devices() if d.user == user_id or (d.group is not None and d.group in groups)]

    def manages(self, by: Device, target: Device) -> bool:
        """Whether `by` may log `target` out: itself, its user's own device,
        or a shared device of a group its user is a member of."""
        own = target.id == by.id or (by.user is not None and target.user == by.user)
        return own or (target.group is not None and self.is_member(target.group, by.user))

    def revoke(self, device_id: str) -> None:
        self._conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))

    def dev_device(self, user_id: str | None) -> Device:
        """The device behind every request when auth is off: a phone, so it
        can answer approvals, owned by `auth.dev_user` when that is set."""
        if user_id is not None:
            self._require_user(user_id)
        self._conn.execute(
            "INSERT INTO devices (id, name, kind, user_id, token, created_at) VALUES (?, ?, 'phone', ?, NULL, ?) "
            "ON CONFLICT (id) DO UPDATE SET user_id = excluded.user_id",
            (DEV_DEVICE, "development", user_id, iso(self._now())),
        )
        device = self.device(DEV_DEVICE)
        assert device is not None
        return device

    def _add_device(self, name: str, kind: DeviceKind, user_id: str | None, group_id: str | None) -> tuple[Device, str]:
        token = secrets.token_urlsafe(32)
        device = Device(
            id=f"d_{secrets.token_hex(5)}",
            name=name.strip()[:60] or kind,
            kind=kind,
            user=user_id,
            group=group_id,
            created_at=self._now(),
        )
        self._conn.execute(
            "INSERT INTO devices (id, name, kind, user_id, group_id, token, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (device.id, device.name, device.kind, device.user, device.group, _digest(token), iso(device.created_at)),
        )
        return device, token


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    _, n, r, p, salt, key = stored.split("$")
    given = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    return hmac.compare_digest(given.hex(), key)


_DUMMY_HASH = hash_password(secrets.token_hex(8))


def _digest(token: str) -> str:
    """Tokens are long and random, so a plain hash is enough to store them."""
    return hashlib.sha256(token.encode()).hexdigest()


def _user(row) -> User:
    return User(
        id=row["id"],
        name=row["name"],
        created_at=from_iso(row["created_at"]),
    )


def _group(row) -> Group:
    return Group(
        id=row["id"],
        name=row["name"],
        is_default=bool(row["is_default"]),
        created_at=from_iso(row["created_at"]),
    )


def _device(row) -> Device:
    return Device(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        user=row["user_id"],
        group=row["group_id"],
        created_at=from_iso(row["created_at"]),
    )
