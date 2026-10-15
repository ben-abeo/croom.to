import inspect
import sys
from types import SimpleNamespace

import pytest
from croom.tv.client import TvClient, real_client_factory
from croom.tv.errors import TvError
from tests.unit.tv.fake_client import FakeTvClient


def test_the_real_client_is_built_from_the_library_only_when_asked_for(monkeypatch):
    class Stub:
        def __init__(self, host, key):
            self.host, self.key = host, key
    monkeypatch.setitem(sys.modules, "aiowebostv", SimpleNamespace(WebOsClient=Stub))
    client = real_client_factory("10.0.0.41", "key-9")
    assert isinstance(client, Stub) and (client.host, client.key) == ("10.0.0.41", "key-9")


def test_a_room_without_the_library_gets_words_not_an_import_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "aiowebostv", None)  # an import of it fails from here on
    with pytest.raises(TvError) as failure:
        real_client_factory("10.0.0.41", None)
    assert "aiowebostv" in str(failure.value)


def test_the_fake_client_has_every_call_of_the_protocol_and_no_other_call():
    """Tests run on the fake, so it must not drift from what the service is allowed to call: a name the protocol lacks
    would pass here and fail on a real TV, and one the fake lacks would only fail in the test that needs it."""
    protocol = {name for name, member in vars(TvClient).items() if callable(member) and not name.startswith("_")}
    fake = {name for name, member in vars(FakeTvClient).items() if callable(member) and not name.startswith("_")}
    helpers = {"update", "drop"}                          # how a test moves the fake: the TV changes, the connection ends
    assert fake - helpers == protocol


@pytest.mark.parametrize("name, kind", [
    ("get_volume", int), ("get_muted", bool), ("get_input", str), ("get_current_app", str), ("get_apps", list),
    ("get_inputs", list), ("get_power_state", dict), ("get_current_channel", dict), ("power_on", dict), ("set_volume", dict),
])
async def test_the_fake_answers_in_the_kinds_the_library_does(name, kind):
    answer = await getattr(FakeTvClient("10.0.0.41", "key-9"), name)(*([50] if name == "set_volume" else []))
    assert isinstance(answer, kind) and not (kind is int and isinstance(answer, bool))
    assert inspect.iscoroutinefunction(getattr(FakeTvClient, name))
