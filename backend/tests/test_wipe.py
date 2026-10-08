from __future__ import annotations

import pytest

from huvu.spec import Caller
from huvu.triggers import TriggerSpec
from huvu.wipe import wipe

from .fakes import WAKE

pytestmark = pytest.mark.usefixtures("family")


def count(kernel, table: str, user: str) -> int:
    return kernel.db.execute(f"SELECT count(*) FROM {table} WHERE user_id = ?", (user,)).fetchone()[0]


async def test_a_wipe_takes_one_persons_rows_and_leaves_the_rest(kernel, family):
    spec = TriggerSpec.model_validate({"note": "Doorbell.", "when": [{"event": "home.doorbell"}], "repeat": "yes", "script": WAKE})
    mine = await kernel.triggers.register(spec, Caller(kind="hermes", user="me"))
    await kernel.triggers.register(spec, Caller(kind="hermes", user="mom"))
    live = await kernel.conversations.ask(family.me_web.id, "hi", user="me")
    async for _ in kernel.conversations.subscribe(live):
        pass
    await kernel.conversations.ask(family.mom_phone.id, "hi", user="mom")
    await kernel.notify("me", None, "for me")
    scripts = kernel.config.data_dir / "scripts"
    (scripts / f"{mine.id}.py").write_text(WAKE)

    counts = wipe(kernel.db, scripts, user="me")

    assert counts["triggers"] == 1 and counts["turns"] == 1 and counts["notifications"] == 1
    for table in ("triggers", "conversations", "notifications", "events", "hermes_sessions"):
        assert count(kernel, table, "me") == 0, table
    assert count(kernel, "triggers", "mom") == 1 and count(kernel, "conversations", "mom") == 1
    assert not (scripts / f"{mine.id}.py").exists()
    # The account stays.
    assert kernel.accounts.user("me") is not None and kernel.accounts.devices()
