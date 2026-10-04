"""The action policy: one check for every write, whoever asks.

Hermes, a trigger's script and a frontend button all run actions through
here. Each action has an autonomy level (silent, notify, ask). Ask holds the
action as a pending approval with a deadline. Every request, allowed or
refused, goes into the event log with who asked and why.

An approval belongs to the user it was asked for: the user of the chat, or
the trigger's user. It goes to that user's phones, and only one of them can
answer it. One asked for no user (from a household device) goes to every
user's phones, and the first answer wins.

An action marked `requires_human` always asks, and config can't lower it.
A tap on a personal device runs at once, since the tap is the approval. A
tap on a household device goes through the policy like any other request,
even while someone is recognized there, so the TV can't open the door.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from .accounts import AuthError, Device
from .db import dumps, from_iso, iso, loads
from .source import SourceError
from .spec import ActionSpec, Autonomy, Caller, Result

if TYPE_CHECKING:
    from .kernel import Kernel

log = logging.getLogger("huvu.policy")

ApprovalStatus = Literal["pending", "approved", "denied", "expired", "failed"]


@dataclass
class Approval:
    id: str
    source: str
    action: str
    args: dict[str, Any]
    reason: str | None
    caller: dict[str, Any]
    # Whose phones answer it. None sends it to every user's phones.
    user: str | None
    status: ApprovalStatus
    created_at: datetime
    deadline: datetime
    resolved_at: datetime | None = None
    result: Any = field(default=None)

    def answerable_by(self, device: Device) -> bool:
        return device.is_phone and (self.user is None or self.user == device.user)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": f"{self.source}.{self.action}",
            "args": self.args,
            "reason": self.reason,
            "asked_by": self.caller,
            "user": self.user,
            "status": self.status,
            "created_at": self.created_at,
            "deadline": self.deadline,
            "result": self.result,
        }


class Policy:
    def __init__(self, kernel: Kernel, overrides: dict[str, Autonomy]) -> None:
        self._kernel = kernel
        # `source.action` -> level, from config.
        self._overrides = overrides

    def level(self, source: str, spec: ActionSpec, caller: Caller) -> Autonomy:
        level: Autonomy = self._overrides.get(f"{source}.{spec.name}", spec.autonomy)
        if spec.requires_human:
            level = "ask"
        return level

    async def request(self, source: str, action: str, args: dict[str, Any], caller: Caller, reason: str | None = None) -> Result:
        """Runs an action, or holds it for approval. A user's own tap on a
        personal device runs at once: the tap is the approval."""
        spec = self._kernel.registry.resolve_action(source, action)
        level: Autonomy = "silent" if self._own_tap(caller) else self.level(source, spec, caller)
        await self._kernel.kernel_event(
            "action.requested",
            f"{caller.describe()} asked for {source}.{action} ({level})" + (f": {reason}" if reason else ""),
            data={"action": f"{source}.{action}", "level": level, "caller": caller.as_dict()},
            user=caller.user,
            device=caller.device,
        )
        if level == "ask":
            approval = await self._hold(source, action, args, caller, reason, spec)
            result = Result(
                data={"status": "pending_approval", "approval": approval.id, "deadline": approval.deadline},
                labels=["house"],
            )
        else:
            result = await self.run(source, action, args, caller)
            if level == "notify":
                await self._kernel.notify(caller.user, f"huvu ran {source}.{action}" + (f": {reason}" if reason else ""), urgency="low")
        return result

    def _own_tap(self, caller: Caller) -> bool:
        """A tap on a personal device: the person is holding the device they
        logged in on, so the tap is their approval. Being recognized at a
        household device is not enough."""
        device = self._kernel.accounts.device(caller.device) if caller.kind == "frontend" and caller.device else None
        return device is not None and device.user is not None

    async def run(self, source: str, action: str, args: dict[str, Any], caller: Caller) -> Result:
        try:
            result = await self._kernel.registry.get(source).act(action, args, caller)
        except SourceError as err:
            await self._kernel.kernel_event(
                "action.failed", f"{source}.{action} failed: {err}", data={"action": f"{source}.{action}"}, user=caller.user, device=caller.device
            )
            raise
        await self._kernel.kernel_event(
            "action.ran",
            f"ran {source}.{action} for {caller.describe()}",
            data={"action": f"{source}.{action}", "caller": caller.as_dict()},
            user=caller.user,
            device=caller.device,
        )
        return result

    async def _hold(self, source: str, action: str, args: dict[str, Any], caller: Caller, reason: str | None, spec: ActionSpec) -> Approval:
        now = self._kernel.now()
        approval = Approval(
            id=f"ap_{secrets.token_hex(4)}",
            source=source,
            action=action,
            args=args,
            reason=reason,
            caller=caller.as_dict(),
            user=caller.user,
            status="pending",
            created_at=now,
            deadline=now + spec.approval_ttl,
        )
        self._kernel.db.execute(
            "INSERT INTO approvals (id, source, action, args, reason, caller, user_id, status, created_at, deadline) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (approval.id, source, action, dumps(args), reason, dumps(approval.caller), approval.user, "pending", iso(now), iso(approval.deadline)),
        )
        await self._kernel.kernel_event(
            "approval.created",
            f"waiting for {approval.user or 'anyone'} on {source}.{action}" + (f": {reason}" if reason else ""),
            data={"approval": approval.id, "action": f"{source}.{action}", "user": approval.user},
            user=approval.user,
            device=caller.device,
        )
        self._push(approval)
        return approval

    def _push(self, approval: Approval) -> None:
        self._kernel.push.send("approval", approval.public(), self._kernel.accounts.phones(approval.user))

    def get(self, approval_id: str) -> Approval | None:
        row = self._kernel.db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return None if row is None else _approval(row)

    def pending(self) -> list[Approval]:
        rows = self._kernel.db.execute("SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at").fetchall()
        return [_approval(r) for r in rows]

    def pending_for(self, user: str | None) -> list[Approval]:
        """What waits on a user: their own approvals and the household's.
        For the household, only the household's."""
        return [a for a in self.pending() if a.user is None or a.user == user]

    async def resolve(self, approval_id: str, approve: bool, by: Device) -> Approval:
        """A user's answer, from their phone. Nothing else calls this, so no
        model can approve its own request."""
        approval = self.get(approval_id)
        if approval is None or approval.status != "pending":
            raise SourceError(f"no pending approval {approval_id!r}")
        if not approval.answerable_by(by):
            raise AuthError("this approval is answered from its user's phone")
        now = self._kernel.now()
        if now > approval.deadline:
            await self._finish(approval, "expired", now)
            raise SourceError(f"approval {approval_id} expired at {approval.deadline:%H:%M}")
        status: ApprovalStatus = "denied"
        if approve:
            status = await self._carry_out(approval, by)
        await self._finish(approval, status, now, by)
        return approval

    async def _carry_out(self, approval: Approval, by: Device) -> ApprovalStatus:
        caller = Caller(kind="frontend", user=by.user, device=by.id)
        status: ApprovalStatus = "approved"
        try:
            approval.result = (await self.run(approval.source, approval.action, approval.args, caller)).data
        except SourceError as err:
            approval.result = {"error": str(err)}
            status = "failed"
        return status

    async def _finish(self, approval: Approval, status: ApprovalStatus, now: datetime, by: Device | None = None) -> None:
        approval.status = status
        approval.resolved_at = now
        self._kernel.db.execute(
            "UPDATE approvals SET status = ?, resolved_at = ?, result = ? WHERE id = ?",
            (status, iso(now), dumps(approval.result), approval.id),
        )
        await self._kernel.kernel_event(
            "approval.resolved",
            f"{approval.source}.{approval.action}: {status}" + (f" by {by.user or by.id}" if by else ""),
            data={"approval": approval.id, "status": status, "device": by.id if by else None},
            labels=["owner"],
            user=approval.user,
        )
        self._push(approval)

    async def expire(self) -> None:
        now = self._kernel.now()
        for approval in self.pending():
            if now > approval.deadline:
                await self._finish(approval, "expired", now)


def _approval(row) -> Approval:
    return Approval(
        id=row["id"],
        source=row["source"],
        action=row["action"],
        args=loads(row["args"]),
        reason=row["reason"],
        caller=loads(row["caller"]),
        user=row["user_id"],
        status=row["status"],
        created_at=from_iso(row["created_at"]),
        deadline=from_iso(row["deadline"]),
        resolved_at=from_iso(row["resolved_at"]),
        result=loads(row["result"]),
    )
