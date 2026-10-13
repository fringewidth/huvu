from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from huvu.accounts import Device
from huvu.config import Config
from huvu.kernel import Kernel

from .fakes import FakeClock, FakeHermes, HomeSource


@pytest.fixture
def clock() -> FakeClock:
    # In the house's timezone, which defaults to IST, whatever the machine's.
    return FakeClock(datetime(2026, 10, 3, 23, 50, tzinfo=ZoneInfo("Asia/Kolkata")))


@pytest.fixture
def home() -> HomeSource:
    return HomeSource()


@pytest.fixture
def hermes() -> FakeHermes:
    return FakeHermes()


@pytest.fixture
async def kernel(tmp_path, clock, home, hermes):
    k = Kernel(Config(data_dir=tmp_path, hermes_home=tmp_path / "hermes"), hermes=hermes, sources={"home": home}, clock=clock)
    # Not started: the tests drive the sources and the clock themselves.
    await home.run()
    await k.triggers.load()
    yield k
    await k.triggers.stop()
    await k.waker.stop()
    await k.conversations.close()
    k.data.close()
    k.db.close()


@dataclass
class Family:
    """Two members of the home group with their devices, and the group's TV."""

    me_phone: Device
    me_web: Device
    mom_phone: Device
    tv: Device
    # The MCP token each user's Hermes profile sends.
    me_token: str
    mom_token: str


@pytest.fixture
def family(kernel) -> Family:
    accounts = kernel.accounts
    me_token = accounts.add_user("me", "Me", "correct horse", "key-me")
    mom_token = accounts.add_user("mom", "Mom", "battery staple", "key-mom")
    accounts.join("home", "me", "member")
    accounts.join("home", "mom", "member")
    me_phone, _ = accounts.login("me", "correct horse", "my phone", "phone")
    me_web, _ = accounts.login("me", "correct horse", "laptop", "web")
    mom_phone, _ = accounts.login("mom", "battery staple", "mom's phone", "phone")
    pairing = accounts.start_pairing("living room", "tv")
    tv = accounts.confirm_pairing(pairing.code, me_phone)
    return Family(me_phone, me_web, mom_phone, tv, me_token, mom_token)
