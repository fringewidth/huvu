from __future__ import annotations

from datetime import datetime

import pytest

from huvu.config import Config
from huvu.kernel import Kernel

from .fakes import FakeClock, FakeHermes, HomeSource


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 3, 23, 50).astimezone())


@pytest.fixture
def home() -> HomeSource:
    return HomeSource()


@pytest.fixture
def hermes() -> FakeHermes:
    return FakeHermes()


@pytest.fixture
async def kernel(tmp_path, clock, home, hermes):
    k = Kernel(Config(data_dir=tmp_path), hermes=hermes, sources={"home": home}, clock=clock)
    # Not started: the tests drive the sources and the clock themselves.
    await k.triggers.load()
    yield k
    await k.triggers.stop()
    await k.conversations.close()
    k.db.close()
