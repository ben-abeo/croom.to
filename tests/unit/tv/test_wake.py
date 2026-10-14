"""Wake-on-LAN: the magic packet for the TV's MAC address, and words for what stops it. The library that sends it is
never touched here: a stand-in takes its place in sys.modules."""
import sys
from types import SimpleNamespace

import pytest
from croom.tv.errors import TvError
from croom.tv.wake import wake

MAC = "AA:BB:CC:DD:EE:FF"


@pytest.fixture
def sent(monkeypatch):
    """What reached wakeonlan.send_magic_packet: the library's name for it, replaced by one that records."""
    calls = []
    monkeypatch.setitem(sys.modules, "wakeonlan", SimpleNamespace(send_magic_packet=lambda *args, **kwargs: calls.append((args, kwargs))))
    return calls


async def test_the_magic_packet_is_sent_once_for_that_mac(sent):
    await wake(MAC)
    assert sent == [((MAC,), {})]                         # the library's own broadcast, nothing else asked of it


@pytest.mark.parametrize("mac", ["aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabbccddeeff", "  AA:BB:CC:DD:EE:FF\n"])
async def test_the_other_ways_a_mac_is_written_are_sent_as_they_were_written(sent, mac):
    await wake(mac)
    assert sent == [((mac.strip(),), {})]


@pytest.mark.parametrize("mac", [
    "", "   ", None, "not a mac", "AA:BB:CC:DD:EE", "AA:BB:CC:DD:EE:FF:00", "GG:BB:CC:DD:EE:FF", "AA:BB-CC:DD-EE:FF",
    "AABBCCDDEEF", "AA:BB:CC:DD:EE:F", "AA BB CC DD EE FF",
])
async def test_a_mac_that_is_not_one_is_refused_in_words_and_nothing_is_sent(sent, mac):
    with pytest.raises(TvError) as failure:
        await wake(mac)
    assert "MAC" in str(failure.value)
    assert sent == []


async def test_a_mac_that_yaml_made_a_number_of_is_explained(sent):
    """`mac: 10:20:30:40:50:59` in the config, unquoted, is the number 8041827059 to YAML (a time in base 60)."""
    with pytest.raises(TvError) as failure:
        await wake(8041827059)
    assert "8041827059" in str(failure.value) and "in quotes" in str(failure.value)
    assert sent == []


async def test_the_librarys_wake_is_used_when_it_has_one_and_the_deprecated_call_is_left_alone(monkeypatch):
    """wakeonlan 4 has wake() and deprecates send_magic_packet(); the pin is wakeonlan>=3.0, which has only the latter."""
    calls = []

    def deprecated(*args, **kwargs):
        raise AssertionError("send_magic_packet is deprecated since wakeonlan 4.0.0: wake() is there")
    monkeypatch.setitem(sys.modules, "wakeonlan", SimpleNamespace(wake=lambda *args, **kwargs: calls.append((args, kwargs)),
                                                                  send_magic_packet=deprecated))
    await wake(MAC)
    assert calls == [((MAC,), {})]


@pytest.mark.parametrize("error", [OSError("Network is unreachable"), ValueError("Incorrect MAC address format")])
async def test_a_wake_the_library_cannot_send_is_explained_the_same_way(monkeypatch, error):
    def failing(*args, **kwargs):
        raise error
    monkeypatch.setitem(sys.modules, "wakeonlan", SimpleNamespace(wake=failing))
    with pytest.raises(TvError) as failure:
        await wake(MAC)
    assert "could not be sent" in str(failure.value) and str(error) in str(failure.value)


async def test_a_library_with_no_way_to_send_is_explained(monkeypatch):
    monkeypatch.setitem(sys.modules, "wakeonlan", SimpleNamespace())
    with pytest.raises(TvError) as failure:
        await wake(MAC)
    assert "cannot send" in str(failure.value) and "wakeonlan" in str(failure.value)


async def test_a_missing_mac_says_where_it_is_set(sent):
    with pytest.raises(TvError) as failure:
        await wake("")
    assert "tv.mac" in str(failure.value) and "not set" in str(failure.value)


async def test_a_room_without_the_library_gets_words_not_an_import_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "wakeonlan", None)   # an import of it fails from here on
    with pytest.raises(TvError) as failure:
        await wake(MAC)
    assert "wakeonlan" in str(failure.value)


@pytest.mark.parametrize("error", [OSError("Network is unreachable"), ValueError("Incorrect MAC address format")])
async def test_a_packet_that_cannot_be_sent_is_explained(monkeypatch, error):
    def failing(*args, **kwargs):
        raise error
    monkeypatch.setitem(sys.modules, "wakeonlan", SimpleNamespace(send_magic_packet=failing))
    with pytest.raises(TvError) as failure:
        await wake(MAC)
    assert "could not be sent" in str(failure.value) and str(error) in str(failure.value)
