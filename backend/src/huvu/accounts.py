"""Users and devices.

A user is a family member with a username and a password. A device is one
install of the app. A personal device (a phone, a browser) belongs to the
user who logged in on it. A household device (the TV, a speaker) belongs to
nobody: it pairs with a code that a user confirms on their phone.

Every device has its own token, stored hashed, so a lost phone can be cut
off without logging anyone else out. Phones are taken to be on their user's
person, so approvals, trigger claims and notifications go to phones.

Every user has a Hermes profile with the same name, with its own API key
and its own token for huvu's MCP server, which is how the backend knows
whose Hermes is calling. Household devices use Hermes's default profile,
whose key (HERMES_API_KEY) and MCP token (HUVU_MCP_TOKEN) come from the
environment.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from .db import from_iso, iso
from .hermes import Profile

DeviceKind = Literal["phone", "web", "tv", "speaker"]
PERSONAL: set[str] = {"phone", "web"}
HOUSEHOLD: set[str] = {"tv", "speaker"}

# The device every request is from when auth is off, for development.
DEV_DEVICE = "dev"
# How long a household device's pairing code stays good.
PAIRING_TTL = timedelta(minutes=10)
# No 0/O or 1/I, so a code read off a TV can't be mistyped.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# The same rule as a Hermes profile name, since each user gets a profile.
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# scrypt cost: about 50 ms and 16 MB a check.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


class AuthError(Exception):
    """A login, pairing or permission that failed. The message is safe to
    show the client."""


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
class Device:
    id: str
    name: str
    kind: DeviceKind
    # None on a household device.
    user: str | None
    created_at: datetime

    @property
    def is_phone(self) -> bool:
        return self.kind == "phone"

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "user": self.user,
            "created_at": self.created_at,
        }


@dataclass
class Pairing:
    id: str
    code: str
    name: str
    kind: DeviceKind
    expires_at: datetime
    # Set once a user confirms the code. The token is handed over once.
    device: Device | None = None
    token: str | None = None


class Accounts:
    def __init__(self, conn: sqlite3.Connection, now: Callable[[], datetime], *, household_key: str | None, household_mcp_token: str | None) -> None:
        self._conn = conn
        self._now = now
        self._household_key = household_key
        self._household_mcp_token = household_mcp_token
        self._pairings: dict[str, Pairing] = {}

    # Users

    def add_user(self, user_id: str, name: str, password: str, hermes_key: str) -> str:
        """Adds a user and returns their profile's MCP token. Only its hash
        is kept, so this is the one chance to copy it."""
        if not USERNAME_RE.match(user_id) or user_id == "default":
            raise AuthError("a username is lowercase letters, digits, _ and -, starts with a letter or digit, and can't be 'default'")
        if self.user(user_id) is not None:
            raise AuthError(f"user {user_id!r} already exists")
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
        """Removes a user and every device they're logged in on."""
        self._require_user(user_id)
        self._conn.execute("DELETE FROM devices WHERE user_id = ?", (user_id,))
        self._conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def user(self, user_id: str) -> User | None:
        row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return None if row is None else _user(row)

    def users(self) -> list[User]:
        return [_user(r) for r in self._conn.execute("SELECT * FROM users ORDER BY id").fetchall()]

    def _require_user(self, user_id: str) -> None:
        if self.user(user_id) is None:
            raise AuthError(f"no user {user_id!r}")

    # Hermes profiles

    def profile(self, user_id: str | None) -> Profile:
        """The Hermes profile that answers for a user, or the default profile
        for the household."""
        key = self._household_key
        if user_id is not None:
            row = self._conn.execute("SELECT hermes_key FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise AuthError(f"no user {user_id!r}")
            key = row["hermes_key"]
        return Profile(name=user_id, key=key)

    def mcp_caller(self, token: str) -> tuple[bool, str | None]:
        """Whose Hermes profile sent this MCP token: (known, user). The
        household's token gives (True, None)."""
        if self._household_mcp_token and hmac.compare_digest(token, self._household_mcp_token):
            return True, None
        row = self._conn.execute("SELECT id FROM users WHERE mcp_token = ?", (_digest(token),)).fetchone()
        return (True, row["id"]) if row else (False, None)

    # Devices

    def login(self, username: str, password: str, name: str, kind: str) -> tuple[Device, str]:
        """A personal device logs in. Returns the new device and its token."""
        if kind not in PERSONAL:
            raise AuthError(f"a {kind} is a household device; it pairs with a code instead")
        row = self._conn.execute("SELECT password FROM users WHERE id = ?", (username,)).fetchone()
        # A missing user costs as much time as a wrong password.
        ok = verify_password(password, row["password"] if row else _DUMMY_HASH)
        if row is None or not ok:
            raise AuthError("wrong username or password")
        return self._add_device(name, cast(DeviceKind, kind), username)

    def start_pairing(self, name: str, kind: str) -> Pairing:
        """A household device asks to pair. It shows the code, and polls
        `pairing` until a user confirms it."""
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

    def confirm_pairing(self, code: str, by: Device) -> Device:
        """A user confirms, on their phone, the code a household device shows."""
        if not by.is_phone:
            raise AuthError("pair a device from your phone")
        self._drop_expired_pairings()
        wanted = code.strip().upper().replace("-", "").replace(" ", "")
        pairing = next((p for p in self._pairings.values() if p.device is None and hmac.compare_digest(p.code, wanted)), None)
        if pairing is None:
            raise AuthError("no device is showing that code")
        pairing.device, pairing.token = self._add_device(pairing.name, pairing.kind, None)
        return pairing.device

    def collect_pairing(self, pairing_id: str) -> tuple[Device, str] | None:
        """What the household device gets when it polls: its device and
        token once confirmed, None while it waits. Raises once the pairing
        is gone."""
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

    def device(self, device_id: str) -> Device | None:
        row = self._conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        return None if row is None else _device(row)

    def devices(self, user_id: str | None = None, *, household: bool = False) -> list[Device]:
        """Every device, or a user's devices and, with `household`, the
        household's too."""
        rows = self._conn.execute("SELECT * FROM devices ORDER BY created_at").fetchall()
        devices = [_device(r) for r in rows]
        if user_id is not None:
            devices = [d for d in devices if d.user == user_id or (household and d.user is None)]
        return devices

    def revoke(self, device_id: str) -> None:
        self._conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))

    def phones(self, user_id: str | None) -> list[str]:
        """The phones that reach a user, or every user's phones when there
        is no user (a household device or trigger)."""
        if user_id is None:
            rows = self._conn.execute("SELECT id FROM devices WHERE kind = 'phone'").fetchall()
        else:
            rows = self._conn.execute("SELECT id FROM devices WHERE kind = 'phone' AND user_id = ?", (user_id,)).fetchall()
        return [r["id"] for r in rows]

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

    def _add_device(self, name: str, kind: DeviceKind, user_id: str | None) -> tuple[Device, str]:
        token = secrets.token_urlsafe(32)
        device = Device(
            id=f"d_{secrets.token_hex(5)}",
            name=name.strip()[:60] or kind,
            kind=kind,
            user=user_id,
            created_at=self._now(),
        )
        self._conn.execute(
            "INSERT INTO devices (id, name, kind, user_id, token, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (device.id, device.name, device.kind, device.user, _digest(token), iso(device.created_at)),
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


def _device(row) -> Device:
    return Device(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        user=row["user_id"],
        created_at=from_iso(row["created_at"]),
    )
