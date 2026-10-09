"""Who is at a household device right now.

A shared device (the TV, a speaker) serves its group, but a person is often
standing in front of it. Anyone in the group, member or guest, can be
recognized there; a guest still approves nothing. When someone asks (the device's
"who's this?" picker, the model, later voice recognition), huvu asks that
person on their phone: "is it you on the living room TV?". When a trigger is
set up there with nobody recognized, huvu asks everyone in the group, and
the first yes wins. A yes makes them the device's attendee until someone
else is confirmed, the attendance is released, or the device sits idle for
IDLE, and the device's waiting triggers become theirs. If every question
for the device ends without a yes, its waiting triggers are dropped.

The Hermes session doesn't change profile, so its prompt cache holds. What
changes is everything huvu controls:

1. The history. Rows from the device (its chat turns, the kernel's events
   about it) are the attendee's from the moment they were asked, so their
   own profile's dream finds them.
2. Tool calls from the device's session act as the attendee: their
   triggers, their approvals, their notifications.
3. The model hears who it's talking to in a note on the next ask.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from .accounts import AuthError, Device
from .db import changed, from_iso, iso

if TYPE_CHECKING:
    from .kernel import Kernel

AskedBy = Literal["device", "model", "voice", "trigger"]
# withdrawn: someone else said yes first.
Status = Literal["pending", "answering", "confirmed", "denied", "expired", "withdrawn", "ended"]

# How long a phone has to answer "is it you?".
ASK_TTL = timedelta(minutes=5)
# A device with no turn for this long has nobody at it anymore.
IDLE = timedelta(minutes=10)


@dataclass
class Check:
    id: str
    device: str
    user: str
    asked_by: AskedBy
    status: Status
    asked_at: datetime
    reason: str | None = None
    confirmed_at: datetime | None = None
    ended_at: datetime | None = None
    last_active_at: datetime | None = None

    def public(self, device_name: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "device": {"id": self.device, "name": device_name},
            "user": self.user,
            "asked_by": self.asked_by,
            "reason": self.reason,
            "status": self.status,
            "asked_at": self.asked_at,
            "expires_at": self.asked_at + ASK_TTL,
        }


class Attendance:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel

    @property
    def _db(self):
        return self._kernel.db

    async def ask(self, device_id: str, user_id: str, asked_by: AskedBy, reason: str | None = None) -> Check | None:
        """Asks a user, on their phones, whether they're at this device.
        Asking again while they haven't answered returns None: the first
        question stands, and nothing new reaches their phone."""
        accounts = self._kernel.accounts
        device = self._shared(device_id)
        if accounts.role(device.group, user_id) is None:
            raise AuthError(f"{user_id!r} isn't in the group {device.name} belongs to")
        now = self._kernel.now()
        check = Check(
            id=f"at_{secrets.token_hex(4)}",
            device=device_id,
            user=user_id,
            asked_by=asked_by,
            status="pending",
            asked_at=now,
            reason=reason,
        )
        inserted = changed(
            self._db,
            "INSERT INTO attendance (id, device_id, user_id, asked_by, reason, status, asked_at) VALUES (?, ?, ?, ?, ?, 'pending', ?) "
            "ON CONFLICT DO NOTHING",
            (check.id, device_id, user_id, asked_by, reason, iso(now)),
        )
        if not inserted:
            return None
        await self._kernel.kernel_event(
            "attendance.asked", f"asked {user_id} whether they're at {device.name} ({asked_by})", user=user_id, device=device_id
        )
        self._kernel.push.send("identify", check.public(device.name), accounts.phones(user_id))
        return check

    async def ask_everyone(self, device_id: str, asked_by: AskedBy, reason: str) -> list[Check]:
        """Asks everyone in the device's group, members and guests, whether
        it's them. Someone already asked about this device isn't asked
        twice."""
        device = self._shared(device_id)
        asked = [await self.ask(device_id, u.id, asked_by, reason) for u in self._kernel.accounts.people(device.group or "")]
        return [c for c in asked if c is not None]

    def _shared(self, device_id: str) -> Device:
        device = self._kernel.accounts.device(device_id)
        if device is None or device.user is not None or device.group is None:
            raise AuthError("only a shared device can be attended")
        return device

    async def answer(self, check_id: str, yes: bool, by: Device) -> Check:
        """A user's answer from their phone."""
        check = self._get(check_id)
        if check is None or check.status != "pending":
            raise AuthError(f"no open question {check_id!r}", 404)
        if not by.is_phone or by.user != check.user:
            raise AuthError("answer this from your own phone")
        now = self._kernel.now()
        if now - check.asked_at > ASK_TTL:
            await self._close(check, "expired", now)
            raise AuthError("this question expired", 410)
        # A double tap: the first answer counts, the second gets nothing.
        won = changed(self._db, "UPDATE attendance SET status = 'answering' WHERE id = ? AND status = 'pending'", (check_id,))
        if not won:
            raise AuthError("this question was already answered", 409)
        if yes:
            await self._confirm(check, now)
            self._done(check)
        else:
            await self._kernel.kernel_event("attendance.denied", f"{check.user} said they're not at {self._name(check.device)}", user=check.user, device=check.device)
            await self._close(check, "denied", now)
        return check

    async def _close(self, check: Check, status: Status, now: datetime) -> None:
        """Ends a question without a yes. Once no question about the device
        is open and nobody is there, its waiting triggers go."""
        self._set(check, status, now)
        self._done(check)
        still_asking = self._db.execute("SELECT 1 FROM attendance WHERE device_id = ? AND status = 'pending'", (check.device,)).fetchone()
        if still_asking is None and self.current(check.device) is None:
            await self._kernel.triggers.drop_unconfirmed(check.device, "since nobody said it was them")

    def _done(self, check: Check) -> None:
        self._kernel.push.send("identify.done", {"id": check.id, "status": check.status}, self._kernel.accounts.phones(check.user))

    async def _confirm(self, check: Check, now: datetime) -> None:
        current = self.current(check.device)
        if current is not None:
            await self._end(current, now, "someone else was recognized")
        check.status = "confirmed"
        check.confirmed_at = now
        check.last_active_at = now
        self._db.execute(
            "UPDATE attendance SET status = 'confirmed', confirmed_at = ?, last_active_at = ? WHERE id = ?",
            (iso(now), iso(now), check.id),
        )
        # Everyone else asked about this device is too late.
        for row in self._db.execute("SELECT * FROM attendance WHERE device_id = ? AND status = 'pending' AND id != ?", (check.device, check.id)).fetchall():
            other = _check(row)
            self._set(other, "withdrawn", now)
            self._done(other)
        # What the device did since the question was asked is theirs too.
        self._kernel.data.claim(check.device, check.user, check.asked_at)
        await self._kernel.triggers.confirm(check.device, check.user)
        name = self._user_name(check.user)
        await self._kernel.kernel_event("attendance.confirmed", f"{name} is at {self._name(check.device)}", user=check.user, device=check.device)
        self._kernel.conversations.note(check.device, f"{name} was recognized at this device; you're talking with them now")
        self._push(check, {"id": check.user, "name": name})

    async def release(self, device_id: str, by: Device | None, reason: str) -> Check | None:
        """Ends the device's attendance. From the device itself, the
        attendee's phone, or the model (`by` None)."""
        current = self.current(device_id)
        if current is None:
            return None
        if by is not None and by.id != device_id and by.user != current.user:
            raise AuthError("only the device or the person at it can end this")
        await self._end(current, self._kernel.now(), reason)
        return current

    async def _end(self, check: Check, now: datetime, reason: str) -> None:
        self._set(check, "ended", now)
        name = self._user_name(check.user)
        await self._kernel.kernel_event("attendance.ended", f"{name} left {self._name(check.device)}: {reason}", user=check.user, device=check.device)
        self._kernel.conversations.note(check.device, f"{name} is no longer recognized at this device ({reason}); assume nobody in particular")
        self._push(check, None)

    def _push(self, check: Check, user: dict[str, str] | None) -> None:
        """Tells the device who is recognized at it, and the person's phones
        where they are recognized, so a phone can end it from afar."""
        payload = {"device": {"id": check.device, "name": self._name(check.device)}, "user": user}
        self._kernel.push.send("attendance", payload, [check.device, *self._kernel.accounts.phones(check.user)])

    def current(self, device_id: str | None) -> Check | None:
        """The device's confirmed attendance, if one is live."""
        row = (
            self._db.execute("SELECT * FROM attendance WHERE device_id = ? AND status = 'confirmed'", (device_id,)).fetchone()
            if device_id
            else None
        )
        return None if row is None else _check(row)

    def places(self, user_id: str | None) -> list[dict[str, Any]]:
        """The shared devices a user is recognized at now."""
        rows = self._db.execute("SELECT * FROM attendance WHERE status = 'confirmed' AND user_id = ? ORDER BY confirmed_at", (user_id,)).fetchall()
        return [{"device": {"id": r["device_id"], "name": self._name(r["device_id"])}, "since": from_iso(r["confirmed_at"])} for r in rows]

    def holder(self, device_id: str | None) -> str | None:
        current = self.current(device_id)
        return current.user if current else None

    def touch(self, device_id: str) -> None:
        """A turn on the device keeps its attendance alive."""
        self._db.execute(
            "UPDATE attendance SET last_active_at = ? WHERE device_id = ? AND status = 'confirmed'",
            (iso(self._kernel.now()), device_id),
        )

    def pending_for(self, user_id: str | None) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT * FROM attendance WHERE status = 'pending' AND user_id = ? ORDER BY asked_at", (user_id,)).fetchall()
        return [_check(r).public(self._name(r["device_id"])) for r in rows]

    async def expire(self) -> None:
        now = self._kernel.now()
        for row in self._db.execute("SELECT * FROM attendance WHERE status IN ('pending', 'confirmed')").fetchall():
            check = _check(row)
            if check.status == "pending" and now - check.asked_at > ASK_TTL:
                await self._close(check, "expired", now)
            elif check.status == "confirmed" and check.last_active_at and now - check.last_active_at > IDLE:
                await self._end(check, now, f"idle for {int(IDLE.total_seconds() // 60)} minutes")

    def _get(self, check_id: str) -> Check | None:
        row = self._db.execute("SELECT * FROM attendance WHERE id = ?", (check_id,)).fetchone()
        return None if row is None else _check(row)

    def _set(self, check: Check, status: Status, now: datetime) -> None:
        check.status = status
        check.ended_at = now
        self._db.execute("UPDATE attendance SET status = ?, ended_at = ? WHERE id = ?", (status, iso(now), check.id))

    def _name(self, device_id: str) -> str:
        device = self._kernel.accounts.device(device_id)
        return device.name if device else device_id

    def _user_name(self, user_id: str) -> str:
        user = self._kernel.accounts.user(user_id)
        return user.name if user else user_id


def _check(row) -> Check:
    return Check(
        id=row["id"],
        device=row["device_id"],
        user=row["user_id"],
        asked_by=row["asked_by"],
        status=row["status"],
        asked_at=from_iso(row["asked_at"]),
        reason=row["reason"],
        confirmed_at=from_iso(row["confirmed_at"]),
        ended_at=from_iso(row["ended_at"]),
        last_active_at=from_iso(row["last_active_at"]),
    )
