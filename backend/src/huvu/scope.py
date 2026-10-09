"""Who sees which row.

Every row in huvu, history or live, belongs to one user, to one group, or to
the house:

1. A user's row (their actions, approvals, triggers, chat turns, where they
   are, and what they did at a shared device while recognized there, see
   attendance.py) shows only to them.
2. A group's row (a shared device's turn with nobody recognized) shows to
   the group's members.
3. The house's rows (a doorbell press, the kernel starting) show to every
   member of any group.

`user_id` and `group_id` on each row say which. A caller reads as a Scope
(accounts.py): a Hermes profile reads its own user's rows and their groups',
so a dream sees its person's rows and never another user's. A guest sees
only their own.
"""

from __future__ import annotations

from typing import Any

from .spec import Scope


class Everyone:
    """Reads with no user filter, for the kernel itself."""


EVERYONE = Everyone()
Viewer = Scope | Everyone


def visible(viewer: Viewer, prefix: str = "") -> tuple[str, list[Any]]:
    """The SQL that keeps what `viewer` may see, with its arguments. Empty
    for everyone."""
    clause, args = "", []
    if isinstance(viewer, Scope):
        parts, args = [], []
        if viewer.user is not None:
            parts.append(f"{prefix}user_id = ?")
            args.append(viewer.user)
        if viewer.groups:
            parts.append(f"({prefix}user_id IS NULL AND {prefix}group_id IN ({', '.join('?' * len(viewer.groups))}))")
            args.extend(viewer.groups)
        if viewer.house:
            parts.append(f"({prefix}user_id IS NULL AND {prefix}group_id IS NULL)")
        clause = f"({' OR '.join(parts)})" if parts else "0"
    return clause, args
