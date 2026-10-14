from __future__ import annotations

import pytest

from huvu.spec import Caller
from huvu.triggers import TriggerSpec
from huvu.wipe import wipe

from .fakes import WAKE

pytestmark = pytest.mark.usefixtures("family")


def count(kernel, table: str, user: str) -> int:
    return kernel.db.execute(f"SELECT count(*) FROM {table} WHERE user_id = ?", (user,)).fetchone()[0]


def history(kernel, table: str, user: str) -> int:
    return kernel.data.count(*table.split("_", 1), "user_id = ?", (user,))


async def test_a_wipe_takes_one_persons_rows_and_leaves_the_rest(kernel, home, family):
    spec = TriggerSpec.model_validate({"note": "Doorbell.", "sql": "SELECT id FROM home_doorbell", "fire": "rows", "once": False, "script": WAKE})
    mine = await kernel.triggers.register(spec, Caller(kind="hermes", user="me"))
    await kernel.triggers.register(spec, Caller(kind="hermes", user="mom"))
    live = await kernel.conversations.ask(family.me_web.id, "hi", user="me")
    async for _ in kernel.conversations.subscribe(live):
        pass
    await kernel.conversations.ask(family.mom_phone.id, "hi", user="mom")
    await kernel.notify("me", None, "for me")
    await home.unlock("fingerprint", "me", user="me")
    await home.unlock("fingerprint", "mom", user="mom")
    await home.press()
    scripts = kernel.config.data_dir / "scripts"
    (scripts / f"{mine.id}.py").write_text(WAKE)
    sources = kernel.config.data_dir / "sources"
    for person in ("me", "mom"):
        (sources / "mail" / person / "1").mkdir(parents=True)
        (sources / "mail" / person / "1" / "bill.pdf").write_bytes(b"%PDF")
    counts = wipe(kernel.db, kernel.data.path, scripts, sources=sources, user="me")

    assert counts["triggers"] == 1 and counts["turns"] == 1 and counts["notifications"] == 1
    assert counts["chat_turns"] == 1 and counts["home_unlocks"] == 1 and counts["huvu_events"] > 0
    for table in ("triggers", "conversations", "notifications", "hermes_sessions"):
        assert count(kernel, table, "me") == 0, table
    for table in ("chat_turns", "home_unlocks", "huvu_events"):
        assert history(kernel, table, "me") == 0, table
    # Mom's rows, and the house's, stay.
    assert history(kernel, "home_unlocks", "mom") == 1 and kernel.data.count("home", "doorbell", "1 = 1", ()) == 1
    assert count(kernel, "triggers", "mom") == 1 and count(kernel, "conversations", "mom") == 1
    assert not (scripts / f"{mine.id}.py").exists()
    # The files a source keeps for them go too, and only theirs.
    assert not (sources / "mail" / "me").exists() and (sources / "mail" / "mom" / "1" / "bill.pdf").exists()
    # The account stays.
    assert kernel.accounts.user("me") is not None and kernel.accounts.devices()


async def test_wiping_the_default_group_takes_the_houses_rows(kernel, home, family):
    await home.press()
    await home.unlock("fingerprint", "me", user="me")
    counts = wipe(kernel.db, kernel.data.path, kernel.config.data_dir / "scripts", group="home", default_group=True)
    assert counts["home_doorbell"] == 1
    assert kernel.data.count("home", "doorbell", "1 = 1", ()) == 0 and history(kernel, "home_unlocks", "me") == 1
