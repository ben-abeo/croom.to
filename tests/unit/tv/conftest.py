import pytest
from tests.unit.tv.fake_client import FakeTvClient


@pytest.fixture(autouse=True)
def a_fresh_fake():
    """The fake keeps its instances and its refuse-next flag on the class: every test starts and ends without them."""
    FakeTvClient.instances.clear()
    FakeTvClient.refuse_next = False
    yield
    FakeTvClient.instances.clear()
    FakeTvClient.refuse_next = False
