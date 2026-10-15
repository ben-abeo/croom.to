import asyncio

import pytest
from tests.unit.tv.fake_client import FakeTvClient
from tests.unit.tv.helpers import LibraryLike


@pytest.fixture(autouse=True)
def a_fresh_fake():
    """The fake keeps its instances and its refuse-next flag on the class: every test starts and ends without them."""
    FakeTvClient.instances.clear()
    FakeTvClient.refuse_next = False
    yield
    FakeTvClient.instances.clear()
    FakeTvClient.refuse_next = False


@pytest.fixture
def gate():
    """The event a LibraryLike TV's connect() waits for: opened when the test is over, so that nothing is left waiting."""
    LibraryLike.gate = asyncio.Event()
    yield LibraryLike.gate
    LibraryLike.gate.set()
