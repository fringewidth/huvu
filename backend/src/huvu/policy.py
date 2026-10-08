"""The action policy: one check for every write, whoever asks.

Hermes, a trigger's script and a frontend button all run actions through
here. Each action has an autonomy level (silent, notify, ask). Ask holds the
action as a pending approval with a deadline. Every request, allowed or
refused, goes into the event log with who asked and why.

An approval belongs to the user it was asked for: the user of the chat, or
the trigger's user. It goes to that user's phones, and only one of them can
answer it. One asked for no user (a shared device nobody is recognized at)
goes to the phones of its group's members, and the first answer wins. So
does one a guest causes: a guest never approves anything, not even by
tapping on their own phone.

The same request twice while the first waits (a double press, two clients
racing) opens one approval: whichever reaches the database first gets it,
and the other gets nothing, only a result saying one is already pending.

An action marked `requires_human` always asks, and config can't lower it.
A tap on a personal device runs at once, since the tap is the approval. A
tap on a household device goes through the policy like any other request,
even while someone is recognized there, so the TV can't open the door.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from .accounts import AuthError, Device
from .db import dumps, from_iso, iso, loads
from .names import UnknownName
from .source import SourceError
from .spec import ActionSpec, Autonomy, Caller, Result, Scope

if TYPE_CHECKING:
    from .kernel import Kernel

log = logging.getLogger("huvu.policy")

# answering: someone's answer won the race and is being carried out.
ApprovalStatus = Literal["pending", "answering", "approved", "denied", "expired", "failed"]


@dataclass
class Approval:
    id: str
    source: str
    action: str
    args: dict[str, Any]
    reason: str | None
    caller: dict[str, Any]
    # Whose phones answer it: the user's, or with no user, the group's
    # members'.
    user: str | None
    group: str | None
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
            "user": self.user,
            "group": self.group,
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
            group=caller.group,
            device=caller.device,
        )
        if level == "ask":
            approval = await self._hold(source, action, args, caller, reason, spec)
            data = {"status": "already_pending"}
            if approval is not None:
                data = {"status": "pending_approval", "approval": approval.id, "deadline": approval.deadline}
            result = Result(data=data, labels=["house"])
        else:
            result = await self.run(source, action, args, caller)
            if level == "notify":
                await self._kernel.notify(caller.user, caller.group, f"huvu ran {source}.{action}" + (f": {reason}" if reason else ""), urgency="low")
        return result

    def _own_tap(self, caller: Caller) -> bool:
        """A tap on a personal device by someone who approves for
        themselves: the person is holding the device they logged in on, so
        the tap is their approval. Being recognized at a shared device is
        not enough, and a guest's tap never is."""
        accounts = self._kernel.accounts
        device = accounts.device(caller.device) if caller.kind == "frontend" else None
        personal = device is not None and device.user is not None
        return personal and accounts.approvers(caller.user, caller.group)[0] == caller.user

    def public(self, approval: Approval) -> dict[str, Any]:
        """An approval as a phone shows it: the raw fields, plus `label`,
        what the action does, and `requester`, who asked, both in words."""
        return {**approval.public(), "label": self._label(approval), "requester": self._requester(approval.caller)}

    def _label(self, approval: Approval) -> str:
        label = f"{approval.source}.{approval.action}"
        try:
            label = self._kernel.registry.resolve_action(approval.source, approval.action).description
        except (SourceError, UnknownName):
            # The source or the action is gone since; the name still says
            # what was asked.
            pass
        return label

    def _requester(self, caller: dict[str, Any]) -> str:
        accounts = self._kernel.accounts
        user = accounts.user(caller["user"]) if caller.get("user") else None
        device = accounts.device(caller.get("device"))
        trigger = self._kernel.triggers.get(caller["trigger"]) if caller.get("trigger") else None
        person = user.name if user else "someone"
        place = f" on {device.name}" if device else ""
        requester = "huvu" + (f", talking with {person}{place}" if device else "")
        if trigger is not None:
            requester = f"a trigger: {trigger.spec.note}"
        elif caller.get("trigger"):
            requester = "a trigger that is gone since"
        elif caller["kind"] == "frontend":
            requester = f"{person}{place}"
        elif caller["kind"] == "kernel":
            requester = "huvu"
        return requester

    def answerable(self, approval: Approval, device: Device) -> bool:
        return self._kernel.accounts.answers(device, approval.user, approval.group)

    async def run(self, source: str, action: str, args: dict[str, Any], caller: Caller) -> Result:
        try:
            result = await self._kernel.registry.get(source).act(action, args, caller)
        except SourceError as err:
            await self._kernel.kernel_event(
                    "action.failed",
                f"{source}.{action} failed: {err}",
                data={"action": f"{source}.{action}"},
                user=caller.user,
                group=caller.group,
                device=caller.device,
            )
            raise
        await self._kernel.kernel_event(
            "action.ran",
            f"ran {source}.{action} for {caller.describe()}",
            data={"action": f"{source}.{action}", "caller": caller.as_dict()},
            user=caller.user,
            group=caller.group,
            device=caller.device,
        )
        return result

    async def _hold(self, source: str, action: str, args: dict[str, Any], caller: Caller, reason: str | None, spec: ActionSpec) -> Approval | None:
        """Opens an approval, or returns None when the same one is already
        pending."""
        now = self._kernel.now()
        user, group = self._kernel.accounts.approvers(caller.user, caller.group)
        approval = Approval(
            id=f"ap_{secrets.token_hex(4)}",
            source=source,
            action=action,
            args=args,
            reason=reason,
            caller=caller.as_dict(),
            user=user,
            group=group,
            status="pending",
            created_at=now,
            deadline=now + spec.approval_ttl,
        )
        key = hashlib.sha256(dumps([source, action, args, user, group], sort_keys=True).encode()).hexdigest()
        inserted = self._kernel.db.execute(
            "INSERT INTO approvals (id, source, action, args, reason, caller, user_id, group_id, status, pending_key, created_at, deadline) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?) ON CONFLICT (pending_key) DO NOTHING",
            (approval.id, source, action, dumps(args), reason, dumps(approval.caller), approval.user, approval.group, key, iso(now), iso(approval.deadline)),
        ).rowcount
        if not inserted:
            return None
        await self._kernel.kernel_event(
            "approval.created",
            f"waiting for {approval.user or f'the members of {approval.group}'} on {source}.{action}" + (f": {reason}" if reason else ""),
            data={"approval": approval.id, "action": f"{source}.{action}", "user": approval.user},
            user=approval.user,
            group=approval.group,
            device=caller.device,
        )
        self._push(approval)
        return approval

    def _push(self, approval: Approval) -> None:
        self._kernel.push.send("approval", self.public(approval), self._kernel.accounts.phones(approval.user, approval.group))

    def get(self, approval_id: str) -> Approval | None:
        row = self._kernel.db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return None if row is None else _approval(row)

    def pending(self) -> list[Approval]:
        rows = self._kernel.db.execute("SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at").fetchall()
        return [_approval(r) for r in rows]

    def pending_for(self, scope: Scope) -> list[Approval]:
        """What a caller may see waiting: their own approvals, and those of
        the groups they're a member of."""
        return [a for a in self.pending() if (a.user is not None and a.user == scope.user) or (a.user is None and a.group in scope.groups)]

    async def resolve(self, approval_id: str, approve: bool, by: Device) -> Approval:
        """A user's answer, from their phone. Nothing else calls this, so no
        model can approve its own request."""
        approval = self.get(approval_id)
        if approval is None or approval.status != "pending":
            raise SourceError(f"no pending approval {approval_id!r}")
        if not self.answerable(approval, by):
            raise AuthError("this approval is answered from its user's phone, or a member's for a group")
        now = self._kernel.now()
        if now > approval.deadline:
            await self._finish(approval, "expired", now)
            raise SourceError(f"approval {approval_id} expired at {approval.deadline:%H:%M}")
        # Two answers at once (two phones, a double tap): the first to flip
        # it out of pending carries it out; the other gets nothing.
        won = self._kernel.db.execute("UPDATE approvals SET status = 'answering' WHERE id = ? AND status = 'pending'", (approval_id,)).rowcount
        if not won:
            raise SourceError(f"approval {approval_id} was already answered")
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
            "UPDATE approvals SET status = ?, pending_key = NULL, resolved_at = ?, result = ? WHERE id = ?",
            (status, iso(now), dumps(approval.result), approval.id),
        )
        await self._kernel.kernel_event(
            "approval.resolved",
            f"{approval.source}.{approval.action}: {status}" + (f" by {by.user or by.id}" if by else ""),
            data={"approval": approval.id, "status": status, "device": by.id if by else None},
            labels=["owner"],
            user=approval.user,
            group=approval.group,
        )
        self._push(approval)
        if (approval.source, approval.action) == ("huvu", "activate_trigger") and status in ("denied", "expired"):
            await self._kernel.triggers.refused(approval.args["trigger"], status)

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
        group=row["group_id"],
        status=row["status"],
        created_at=from_iso(row["created_at"]),
        deadline=from_iso(row["deadline"]),
        resolved_at=from_iso(row["resolved_at"]),
        result=loads(row["result"]),
    )
