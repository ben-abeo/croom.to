"""The controls: what each one asks of the TV, what is refused in words before the TV is asked, what the TV's refusals
become, and what the table sees afterwards."""
import asyncio

import pytest
from croom.tv.errors import TvError
from croom.tv.service import KEYS
from tests.unit.tv.fake_client import FakeTvClient
from tests.unit.tv.helpers import build_service, forgetful_tv, running, silent_tv

READS = ("connect", "get_volume")                         # what the service may ask of the TV besides the control itself


def asked(client):
    """The calls the TV was given that were not reads."""
    return [call for call in client.calls if call[0] not in READS]


class WebOsTvCommandError(Exception):
    """Named as the library's exception for a command the TV did not carry out."""


@pytest.mark.parametrize("control, call", [
    (("volume_up", None), ("volume_up",)),
    (("volume_down", None), ("volume_down",)),
    (("volume", 0), ("set_volume", 0)),
    (("volume", 100), ("set_volume", 100)),
    (("volume", "55"), ("set_volume", 55)),               # the digits of a number, as a form sends them
    (("volume", 55.0), ("set_volume", 55)),
    (("mute", None), ("set_mute", True)),                 # the TV is not muted: switching it mutes
    (("mute", True), ("set_mute", True)),
    (("mute", False), ("set_mute", False)),
    (("channel_up", None), ("channel_up",)),
    (("channel_down", None), ("channel_down",)),
    (("channel", "12"), ("set_channel", "12")),
    (("channel", 12), ("set_channel", "12")),
    (("channel", "7-1"), ("set_channel", "7-1")),
    (("input", "HDMI_2"), ("set_input", "HDMI_2")),
    (("input", "pi"), ("set_input", "HDMI_1")),           # the room Pi's input is the config's pi_input
    (("input", " pi "), ("set_input", "HDMI_1")),
    (("input", " HDMI_2"), ("set_input", "HDMI_2")),
    (("app", "netflix"), ("launch_app", "netflix")),
    (("key", "HOME"), ("button", "HOME")),
    (("key", " home "), ("button", "HOME")),              # the remote's keys are capitals, however they were sent
    (("digit", "7"), ("button", "7")),
    (("digit", 7), ("button", "7")),
    (("toast", "Hello"), ("send_message", "Hello")),
    (("toast", "  Lunch is here "), ("send_message", "Lunch is here")),
])
async def test_each_control_asks_the_tv_for_exactly_that(tmp_path, control, call):
    service = build_service(tmp_path)
    async with running(service) as client:
        await service.control(*control)
        assert asked(client) == [call]


async def test_every_remote_key_of_the_spec_is_a_key_the_table_can_press(tmp_path):
    service = build_service(tmp_path)
    async with running(service) as client:
        for name in sorted(KEYS - {"POWER"}):             # the power key is a power control: see test_keep_on.py
            await service.control("key", name)
        assert asked(client) == [("button", name) for name in sorted(KEYS - {"POWER"})]


def test_the_remote_keys_are_those_of_the_spec():
    assert len(KEYS) == 57                                # the names aiowebostv 0.10 has in its buttons module, as the spec lists them
    assert {"LEFT", "RIGHT", "UP", "DOWN", "ENTER", "BACK", "EXIT", "HOME", "MENU", "INFO", "GUIDE", "POWER", "3D_MODE",
            "ASTERISK", "NETFLIX", "INPUT_HUB"} <= KEYS
    assert set("0123456789") <= KEYS
    assert "SELF_DESTRUCT" not in KEYS and "home" not in KEYS


@pytest.mark.parametrize("control", [
    ("volume", -1), ("volume", 101), ("volume", "loud"), ("volume", True), ("volume", 55.5), ("volume", None), ("volume", [50]),
    ("mute", "yes"), ("mute", "false"), ("mute", "off"), ("mute", ""), ("mute", 0), ("mute", 1), ("mute", []),
    ("channel", ""), ("channel", "abc"), ("channel", None), ("channel", "12; reboot"), ("channel", True), ("channel", 1.5),
    ("input", ""), ("input", None), ("input", "HDMI_9"), ("input", 3),
    ("app", ""), ("app", None), ("app", 12),
    ("key", ""), ("key", None), ("key", "SELF_DESTRUCT"), ("key", 7),
    ("digit", "10"), ("digit", "x"), ("digit", None), ("digit", -1), ("digit", True),
    ("toast", ""), ("toast", "   "), ("toast", None), ("toast", 5),
    ("power", "dim"), ("power", None), ("power", True),
    ("sing", None), ("", None), (None, None), (["volume_up"], None),
])
async def test_a_control_that_makes_no_sense_is_refused_in_words_before_the_tv_is_asked(tmp_path, control):
    service = build_service(tmp_path)
    async with running(service) as client:
        with pytest.raises(TvError) as failure:
            await service.control(*control)
        assert str(failure.value) and "10.0.0.41" not in str(failure.value)     # words about the control, not about the TV
        assert asked(client) == []


class ChangingTv(FakeTvClient):
    """A TV that does what it is told, and does not say so unless asked: nothing is pushed to the service."""

    async def set_volume(self, volume):
        await super().set_volume(volume)
        self.tv_state.volume = volume

    async def volume_up(self):
        await super().volume_up()
        self.tv_state.volume += 1


async def test_what_a_control_answers_is_what_the_tv_has_now_and_not_what_it_had(tmp_path):
    service = build_service(tmp_path, ChangingTv)
    async with running(service):
        assert service.status()["volume"] == 24
        shown = await service.control("volume", 55)
        assert shown["volume"] == 55 and service.status()["volume"] == 55         # the TV pushed nothing: the service asked
        shown = await service.control("volume_up")
        assert shown["volume"] == 56 and shown == service.status()


async def test_a_read_the_tv_does_not_answer_leaves_what_is_known_and_the_control_stands(tmp_path):
    class Deaf(FakeTvClient):
        async def get_volume(self):
            raise TimeoutError()
    service = build_service(tmp_path, Deaf)
    async with running(service) as client:
        shown = await service.control("volume_up")
        assert ("volume_up",) in client.calls and shown["volume"] == 24


async def test_a_volume_the_tv_cannot_give_leaves_what_is_known(tmp_path):
    class Unsure(FakeTvClient):
        async def get_volume(self):
            return None
    service = build_service(tmp_path, Unsure)
    async with running(service):
        assert (await service.control("volume_up"))["volume"] == 24


async def test_mute_switches_from_what_the_table_shows_even_before_the_tv_says_so(tmp_path):
    service = build_service(tmp_path)                     # the fake pushes nothing when told to mute
    async with running(service) as client:
        await client.update(muted=True)                   # muted with the TV's own remote
        assert (await service.control("mute"))["muted"] is False
        assert (await service.control("mute"))["muted"] is True             # quick taps are switches, one after the other
        assert (await service.control("mute"))["muted"] is False
        assert [call for call in client.calls if call[0] == "set_mute"] == [("set_mute", False), ("set_mute", True), ("set_mute", False)]


@pytest.mark.parametrize("error, words", [
    (WebOsTvCommandError("Request failed with response {'payload': {'returnValue': False}}"), "Request failed"),
    (WebOsTvCommandError("404 no such service or method"), "404 no such service or method"),
    (TimeoutError(), "no answer in time"),
    (ConnectionResetError("Connection reset by peer"), "Connection reset"),
    (RuntimeError("x" * 500), "x" * 50),
])
async def test_a_tv_that_cannot_do_it_says_so_in_words_and_the_state_stands(tmp_path, error, words):
    class Failing(FakeTvClient):
        async def volume_up(self):
            raise error
    service = build_service(tmp_path, Failing)
    async with running(service):
        with pytest.raises(TvError) as failure:
            await service.control("volume_up")
        assert "The TV did not accept that" in str(failure.value) and words in str(failure.value)
        assert len(str(failure.value)) < 260              # a line for the table, not what the library dumped
        assert service.status()["volume"] == 24 and service.status()["reachable"] is True


@pytest.mark.parametrize("message, reason", [
    ("Invalid request response {'type': 'response', 'id': 33, 'payload': {'returnValue': False, 'errorText': 'invalid channel'}}",
     "invalid channel"),
    ("Request failed with response {'payload': {'returnValue': False, 'errorCode': -1, 'errorText': \"Can't open that\"}}",
     "Can't open that"),
    ("Request failed with response {'payload': {'returnValue': False}}", "Request failed with response {'payload': {'returnValue': False}}"),
])
async def test_the_reason_the_tv_gives_is_what_the_table_is_told(tmp_path, message, reason):
    """The library's message quotes the TV's whole answer: what the table needs is the TV's own words in it."""
    class Refusing(FakeTvClient):
        async def set_channel(self, *args):
            raise WebOsTvCommandError(message)
    service = build_service(tmp_path, Refusing)
    async with running(service):
        with pytest.raises(TvError) as failure:
            await service.control("channel", "12")
        assert str(failure.value) == f"The TV did not accept that: {reason}"


async def test_a_mute_the_tv_did_not_accept_does_not_change_what_the_table_shows(tmp_path):
    class Refusing(FakeTvClient):
        async def set_mute(self, *args):
            raise WebOsTvCommandError("Request failed")
    service = build_service(tmp_path, Refusing)
    async with running(service):
        with pytest.raises(TvError):
            await service.control("mute")
        assert service.status()["muted"] is False


async def test_a_control_that_is_cancelled_is_not_taken_for_a_failure(tmp_path):
    class Slow(FakeTvClient):
        async def volume_up(self):
            await asyncio.sleep(30)
    service = build_service(tmp_path, Slow)
    async with running(service):
        control = asyncio.ensure_future(service.control("volume_up"))
        await asyncio.sleep(0.05)
        control.cancel()
        with pytest.raises(asyncio.CancelledError):
            await control


# What stands between the table and the TV is said in words, the pairing's first.

OUT_OF_REACH = [("volume_up", None), ("key", "HOME"), ("power", "off"), ("power", "on")]


async def said(service, name, value):
    with pytest.raises(TvError) as failure:
        await service.control(name, value)
    return str(failure.value)


@pytest.mark.parametrize("name, value", OUT_OF_REACH)
async def test_a_room_with_no_tv_set_up_says_so(tmp_path, name, value):
    service = build_service(tmp_path, enabled=False, host="")
    assert "not set up" in await said(service, name, value) and FakeTvClient.instances == []


@pytest.mark.parametrize("name, value", OUT_OF_REACH)
async def test_a_tv_that_is_not_paired_says_to_pair_it(tmp_path, name, value):
    service = build_service(tmp_path, key=None)
    assert "Pair the TV" in await said(service, name, value) and FakeTvClient.instances == []


@pytest.mark.parametrize("name, value", OUT_OF_REACH)
async def test_a_tv_that_refused_this_table_says_so_ahead_of_anything_else(tmp_path, name, value):
    service = build_service(tmp_path, forgetful_tv)       # the TV declined the stored key: it is out of reach, and this is why
    async with running(service):
        assert "The TV no longer accepts this table" in await said(service, name, value)
        assert "Forget TV, then Pair" in await said(service, name, value)
        with pytest.raises(TvError, match="Forget TV, then Pair"):
            service._require_ready()                      # for whoever else wants the client: the same words, not "not answering"


@pytest.mark.parametrize("name, value", [("volume_up", None), ("key", "HOME"), ("power", "off")])
async def test_a_tv_that_does_not_answer_says_where(tmp_path, name, value):
    service = build_service(tmp_path, silent_tv)          # (power on is not here: it is how a TV that does not answer is woken)
    async with running(service):
        assert "The TV is not answering at 10.0.0.41" in await said(service, name, value)


async def test_a_typed_channel_is_found_in_the_channel_list_the_tv_keeps(tmp_path):
    def with_channels(host, key):
        client = FakeTvClient(host, key)
        client.tv_state.channels = [{"channelNumber": "11", "channelId": "id-11"}, {"channelNumber": "12", "channelId": "1_12_0"},
                                    {"channelNumber": "13"}, {"channelId": "no-number"}, "junk", None,
                                    {"channelNumber": "14", "channelId": "first-14"}, {"channelNumber": "14", "channelId": "second-14"}]
        return client
    service = build_service(tmp_path, with_channels)
    async with running(service) as client:
        await service.control("channel", "12")
        await service.control("channel", 12)
        await service.control("channel", "99")            # not in the list: sent as it was typed
        await service.control("channel", "13")            # in the list, but without an id: as typed
        await service.control("channel", "14")            # twice in the list: the first
        assert asked(client) == [("set_channel", "1_12_0"), ("set_channel", "1_12_0"), ("set_channel", "99"), ("set_channel", "13"),
                                 ("set_channel", "first-14")]


async def test_an_input_the_tv_does_not_have_is_refused_in_words(tmp_path):
    service = build_service(tmp_path, pi_input="HDMI_7")
    async with running(service) as client:
        with pytest.raises(TvError) as failure:
            await service.control("input", "HDMI_9")
        assert "HDMI_9" in str(failure.value)
        with pytest.raises(TvError) as failure:
            await service.control("input", "pi")          # the config names an input the TV does not list
        assert "HDMI_7" in str(failure.value) and "tv.pi_input" in str(failure.value)
        assert asked(client) == []


async def test_an_input_goes_to_the_tv_as_given_when_the_tv_has_not_listed_its_inputs(tmp_path):
    def unlisted(host, key):
        client = FakeTvClient(host, key)
        client.tv_state.inputs = {}
        return client
    service = build_service(tmp_path, unlisted)
    async with running(service) as client:
        await service.control("input", "HDMI_9")
        assert asked(client) == [("set_input", "HDMI_9")]
