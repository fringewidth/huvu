"""The action policy: one check for every write, whoever asks.

Hermes, a trigger's script and a frontend button all run actions through
here. Each action has an autonomy level (silent, notify, ask). Ask holds the
action as a pending approval with a deadline, which the frontend shows and
only the owner can answer. Every request, allowed or refused, goes into the
event log with who asked and why.

An action marked `requires_human` always asks, and config can't lower it.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from .db import dumps, from_iso, iso, loads
from .source import SourceError
from .spec import ActionSpec, Autonomy, Result

if TYPE_CHECKING:
    from .kernel import Kernel

log = logging.getLogger("huvu.policy")

CallerKind = Literal["hermes", "script", "frontend", "kernel"]
ApprovalStatus = Literal["pending", "approved", "denied", "expired", "failed"]


@dataclass(frozen=True)
class Caller:
    kind: CallerKind
    session: str | None = None
    trigger: str | None = None

    def describe(self) -> str:
        parts = [self.kind]
        if self.session:
            parts.append(f"session {self.session}")
        if self.trigger:
            parts.append(f"trigger {self.trigger}")
        return ", ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "session": self.session, "trigger": self.trigger}


@dataclass
class Approval:
    id: str
    source: str
    action: str
    args: dict[str, Any]
    reason: str | None
    caller: dict[str, Any]
    status: ApprovalStatus
    created_at: datetime
    deadline: datetime
    resolved_at: datetime | None = None
    result: Any = field(default=None)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": f"{self.source}.{self.action}",
            "args": self.args,
            "reason": self.reason,
            "asked_by": self.caller,
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
        """Runs an action, or holds it for approval. The owner's own taps
        from the frontend run at once: the tap is the approval."""
        spec = self._kernel.registry.resolve_action(source, action)
        level: Autonomy = "silent" if caller.kind == "frontend" else self.level(source, spec, caller)
        await self._kernel.kernel_event(
            "action.requested",
            f"{caller.describe()} asked for {source}.{action} ({level})" + (f": {reason}" if reason else ""),
            data={"action": f"{source}.{action}", "level": level, "caller": caller.as_dict()},
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
                await self._kernel.push.notify(f"huvu ran {source}.{action}" + (f": {reason}" if reason else ""), urgency="low")
        return result

    async def run(self, source: str, action: str, args: dict[str, Any], caller: Caller) -> Result:
        try:
            result = await self._kernel.registry.get(source).act(action, args)
        except SourceError as err:
            await self._kernel.kernel_event("action.failed", f"{source}.{action} failed: {err}", data={"action": f"{source}.{action}"})
            raise
        await self._kernel.kernel_event(
            "action.ran",
            f"ran {source}.{action} for {caller.describe()}",
            data={"action": f"{source}.{action}", "caller": caller.as_dict()},
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
            status="pending",
            created_at=now,
            deadline=now + spec.approval_ttl,
        )
        self._kernel.db.execute(
            "INSERT INTO approvals (id, source, action, args, reason, caller, status, created_at, deadline) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (approval.id, source, action, dumps(args), reason, dumps(approval.caller), "pending", iso(now), iso(approval.deadline)),
        )
        await self._kernel.kernel_event(
            "approval.created",
            f"waiting for the owner on {source}.{action}" + (f": {reason}" if reason else ""),
            data={"approval": approval.id, "action": f"{source}.{action}"},
        )
        await self._kernel.push.approval(approval)
        return approval

    def get(self, approval_id: str) -> Approval | None:
        row = self._kernel.db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return None if row is None else _approval(row)

    def pending(self) -> list[Approval]:
        rows = self._kernel.db.execute("SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at").fetchall()
        return [_approval(r) for r in rows]

    async def resolve(self, approval_id: str, approve: bool) -> Approval:
        """The owner's answer, from the frontend. Nothing else calls this,
        so no model can approve its own request."""
        approval = self.get(approval_id)
        if approval is None or approval.status != "pending":
            raise SourceError(f"no pending approval {approval_id!r}")
        now = self._kernel.now()
        if now > approval.deadline:
            await self._finish(approval, "expired", now)
            raise SourceError(f"approval {approval_id} expired at {approval.deadline:%H:%M}")
        status: ApprovalStatus = "denied"
        if approve:
            status = await self._carry_out(approval)
        await self._finish(approval, status, now)
        return approval

    async def _carry_out(self, approval: Approval) -> ApprovalStatus:
        caller = Caller(kind="frontend")
        status: ApprovalStatus = "approved"
        try:
            approval.result = (await self.run(approval.source, approval.action, approval.args, caller)).data
        except SourceError as err:
            approval.result = {"error": str(err)}
            status = "failed"
        return status

    async def _finish(self, approval: Approval, status: ApprovalStatus, now: datetime) -> None:
        approval.status = status
        approval.resolved_at = now
        self._kernel.db.execute(
            "UPDATE approvals SET status = ?, resolved_at = ?, result = ? WHERE id = ?",
            (status, iso(now), dumps(approval.result), approval.id),
        )
        await self._kernel.kernel_event(
            "approval.resolved",
            f"{approval.source}.{approval.action}: {status}",
            data={"approval": approval.id, "status": status},
            labels=["owner"],
        )
        await self._kernel.push.approval(approval)

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
        status=row["status"],
        created_at=from_iso(row["created_at"]),
        deadline=from_iso(row["deadline"]),
        resolved_at=from_iso(row["resolved_at"]),
        result=loads(row["result"]),
    )
