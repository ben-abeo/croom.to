import sys
from types import SimpleNamespace

import pytest
from croom.tv.client import real_client_factory
from croom.tv.errors import TvError


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
