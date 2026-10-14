"""What the service makes of the TV's state: the names the real library uses, what counts as on, and a status that holds."""
import asyncio
from types import SimpleNamespace

import pytest
from croom.tv.errors import TvError
from tests.unit.tv.fake_client import FakeTvClient
from tests.unit.tv.helpers import DoingTv, build_service


class LibraryShapedClient(FakeTvClient):
    """The state as aiowebostv's WebOsTvInfo and WebOsTvState have it, which names things differently from the fake."""

    def __init__(self, host, key):
        super().__init__(host, key)
        self.tv_info = SimpleNamespace(hello={"deviceOS": "webOS"}, system={"modelName": "OLED65C6"}, software={}, connection={})
        self.tv_state = SimpleNamespace(
            power_state={"state": "Active"}, current_app_id="com.webos.app.hdmi2", muted=None, volume=12, apps={},
            inputs={"com.webos.app.hdmi1": {"id": "HDMI_1", "label": "HDMI 1", "appId": "com.webos.app.hdmi1", "connected": True},
                    "com.webos.app.hdmi2": {"id": "HDMI_2", "label": "HDMI 2", "appId": "com.webos.app.hdmi2", "connected": False}},
            current_channel=None, is_on=True)


async def test_the_state_is_read_in_the_names_the_real_library_uses(tmp_path):
    service = build_service(tmp_path, LibraryShapedClient)
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        s = service.status()
        assert s["name"] == "OLED65C6"                                      # the hello names no device: the model stands in
        assert s["input"] == "HDMI_2" and s["app"] == "com.webos.app.hdmi2"    # the input is the one whose app is on screen
        assert s["inputs"] == [{"id": "HDMI_1", "label": "HDMI 1"}, {"id": "HDMI_2", "label": "HDMI 2"}]
        assert s["volume"] == 12 and s["muted"] is False and s["channel"] is None and s["on"] is True
        await client.update(current_app_id="com.webos.app.livetv", current_channel={"channelNumber": "11", "channelName": "News"})
        assert service.status()["input"] is None and service.status()["channel"] == "11"    # live TV is not an input
        await client.update(power_state={}, is_on=False, volume=None, inputs={}, current_app_id=None, current_channel=None)   # the library's cleared state
        assert service.status()["on"] is False and service.status()["volume"] is None
    finally:
        await service.stop()


@pytest.mark.parametrize("hello, system, name", [
    ({"deviceName": "Boardroom TV"}, {"modelName": "OLED65C6"}, "Boardroom TV"),
    ({"deviceName": "Salle de réunion"}, {}, "Salle de réunion"),
    ({"deviceName": "Board\x1b[2Jroom\n  TV"}, {}, "Board[2Jroom TV"),         # the TV chooses its name: one printable line
    ({}, {"modelName": "OLED65C6\x07"}, "OLED65C6"),                          # and its model, which stands in for it
    ({"deviceName": "\x1b\x07"}, {"modelName": "OLED65C6"}, "OLED65C6"),       # a name with nothing printable in it is no name
    ({"deviceName": "   "}, {"modelName": "OLED65C6"}, "OLED65C6"),
    ({"deviceName": "\x1b\x07"}, {"modelName": "\x07"}, ""),
    ({"deviceName": "x" * 300}, {}, "x" * 100),
    ({}, {}, ""),
])
async def test_the_name_the_tv_gives_is_one_printable_line(tmp_path, hello, system, name):
    def named(host, key):
        client = FakeTvClient(host, key)
        client.tv_info = SimpleNamespace(hello_info=hello, system_info=system)
        return client
    service = build_service(tmp_path, named)
    await service.start()
    try:
        assert service.status()["name"] == name
    finally:
        await service.stop()


async def test_what_the_tv_calls_its_inputs_apps_and_channels_is_one_printable_line(tmp_path):
    """The status goes to the table's page and, by `croom --check-tv`, to a terminal: what the TV calls its things is text, on
    one line, and never an escape. What the page is sent is data, not markup: `<` and `&` are the TV's words and are kept."""
    def odd(host, key):
        client = FakeTvClient(host, key)
        client.tv_state.inputs = {
            "com.webos.app.hdmi1": {"id": "HDMI\x07_1", "label": "Board\x1b[2Jroom\n   PC"},
            "com.webos.app.hdmi2": {"id": "HDMI_2", "label": "\x1b\x07"},                  # no name worth showing: the id stands in
            "com.webos.app.hdmi3": {"id": "HDMI_3", "label": "Tom & Jerry <TV>"},
        }
        client.tv_state.current_input = "HDMI\x07_1"
        client.tv_state.current_app_id = "com.webos.app.hdmi1\x1b[2J"
        client.tv_state.channel_number = "11\x1b[0m"
        return client
    service = build_service(tmp_path, odd)
    await service.start()
    try:
        s = service.status()
        assert s["inputs"] == [{"id": "HDMI_1", "label": "Board[2Jroom PC"}, {"id": "HDMI_2", "label": "HDMI_2"},
                               {"id": "HDMI_3", "label": "Tom & Jerry <TV>"}]
        assert s["input"] == "HDMI_1" and s["app"] == "com.webos.app.hdmi1[2J" and s["channel"] == "11[0m"
        client = FakeTvClient.instances[-1]
        await client.update(current_app_id="com.webos.app.livetv\x00", channel_number="12\u200b")      # the same when the TV tells of a change
        assert service.status()["app"] == "com.webos.app.livetv" and service.status()["channel"] == "12"
    finally:
        await service.stop()


async def test_what_the_tv_says_when_it_is_asked_after_a_control_is_one_printable_line_too(tmp_path):
    class OddReads(DoingTv):
        """A TV whose answers to the read-backs carry what a terminal would act on."""
        async def set_input(self, input_id):
            await super().set_input(input_id)
            self.tv_state.current_input = "HDMI\x07_1"             # (the fake names its current input itself, as the library does not)

        async def get_input(self):
            return "com.webos.app.hdmi1\x1b[2J"

        async def get_current_app(self):
            return "com.x\x1b[2J.app"

        async def get_current_channel(self):
            return {"channelNumber": "11\x1b[0m"}
    service = build_service(tmp_path, OddReads)
    await service.start()
    try:
        s = await service.control("input", "HDMI_1")
        assert s["app"] == "com.webos.app.hdmi1[2J" and s["input"] == "HDMI_1"
        assert (await service.control("app", "com.x.app"))["app"] == "com.x[2J.app"
        assert (await service.control("channel_up"))["channel"] == "11[0m"
    finally:
        await service.stop()


async def test_the_input_of_a_tv_that_does_not_name_it_is_found_by_the_app_id_as_the_tv_gave_it(tmp_path):
    """The library has no current input: it is the one of the list whose app is on the screen, found by the id of the app as the TV
    wrote it. The app shown is tidied (names.one_line), the match is not made on what has been tidied."""
    raw = "com.webos.app.hdmi1\x07"

    def unnamed(host, key):
        client = FakeTvClient(host, key)
        del client.tv_state.current_input                  # (the fake names its input itself, as the library does not)
        client.tv_state.inputs = {raw: {"id": "HDMI_1", "label": "HDMI 1", "appId": raw},
                                  "com.webos.app.hdmi2": {"id": "HDMI_2", "label": "HDMI 2", "appId": "com.webos.app.hdmi2"}}
        client.tv_state.current_app_id = raw
        return client
    service = build_service(tmp_path, unnamed)
    await service.start()
    try:
        s = service.status()                               # from the state the TV tells
        assert s["app"] == "com.webos.app.hdmi1" and s["input"] == "HDMI_1"
        await FakeTvClient.instances[-1].update(current_app_id="com.webos.app.hdmi2")
        assert service.status()["input"] == "HDMI_2"
        FakeTvClient.instances[-1].tv_state.current_app_id = raw
        s = await service.control("input", "HDMI_1")        # and from the read-back after a control
        assert s["app"] == "com.webos.app.hdmi1" and s["input"] == "HDMI_1"
    finally:
        await service.stop()


async def test_a_channel_that_is_a_number_and_an_input_that_is_nothing_stay_what_they_are(tmp_path):
    """Only words are tidied: a channel the TV gives as a number is a number, and no input is None and not an empty string."""
    def numbered(host, key):
        client = FakeTvClient(host, key)
        client.tv_state.channel_number, client.tv_state.current_input, client.tv_state.current_app_id = 11, None, "com.webos.app.livetv"
        return client
    service = build_service(tmp_path, numbered)
    await service.start()
    try:
        s = service.status()
        assert s["channel"] == 11 and s["input"] is None and s["app"] == "com.webos.app.livetv"
    finally:
        await service.stop()


def librarys_is_on(power_state, app):
    """What aiowebostv 0.10 works out for tv_state.is_on (WebOsClient._is_tv_on), before it hands the state over."""
    if not power_state:
        return app not in (None, "")                      # webOS versions that do not report a power state
    return power_state.get("state") not in (None, "Power Off", "Suspend", "Active Standby")


HDMI = "com.webos.app.hdmi1"


@pytest.mark.parametrize("power_state, app, from_library, on", [
    ({"state": "Active"}, HDMI, True, True),
    ({"state": "Screen Off"}, HDMI, True, True),          # the TV is on and only its screen is dark: it is not woken
    ({"state": "Screen Saver"}, HDMI, True, True),
    ({"state": "Active Standby"}, None, True, False),     # answers on the network, but is off
    ({"state": "Suspend"}, None, True, False),
    ({"state": "Power Off"}, None, True, False),
    ({}, None, True, False),                              # the cleared state
    ({}, HDMI, True, True),                               # a webOS that reports no power state: the library goes by the running app
    ({"state": "Active"}, HDMI, False, True),             # no word from the library (the test fake): only Active is on
    ({"state": "Off"}, HDMI, False, False),
    ({"state": "Active Standby"}, HDMI, False, False),
    ({"state": "Suspend"}, HDMI, False, False),
    ({"state": "Power Off"}, HDMI, False, False),
    ({}, HDMI, False, False),
])
async def test_what_counts_as_on(tmp_path, power_state, app, from_library, on):
    def tv(host, key):
        client = FakeTvClient(host, key)
        client.tv_state.power_state, client.tv_state.current_app_id = power_state, app
        if from_library:
            client.tv_state.is_on = librarys_is_on(power_state, app)
        return client
    service = build_service(tmp_path, tv)
    await service.start()
    try:
        s = service.status()
        assert s["reachable"] is True and s["on"] is on   # a TV in standby answers: it is reachable, not on
    finally:
        await service.stop()


@pytest.mark.parametrize("power_state, on", [
    ({"state": "Active"}, True), ({"state": "Screen Off"}, True), ({"state": "Screen Saver"}, True),   # on, if dark
    ({"state": "Active Standby"}, False), ({"state": "Suspend"}, False), ({"state": "Power Off"}, False), ({"state": "Off"}, False),
    ({}, None), (None, None), ("Active", None),                                                          # nothing that can be used
])
def test_what_a_power_state_that_was_read_back_says(power_state, on):
    from croom.tv.service import _power_says_on
    assert _power_says_on(power_state) is on


async def test_a_state_the_service_cannot_read_never_reaches_the_library(tmp_path, caplog):
    service = build_service(tmp_path)
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        await client.update(power_state="Active")         # not the dict the library sends: reading it fails, and must not raise here
        assert "could not be read" in caplog.text
        assert service.status()["volume"] == 24 and service.status()["on"] is True      # the state stays as it was
    finally:
        await service.stop()


async def test_a_tv_that_cannot_be_reached_is_not_shown_as_on(tmp_path):
    service = build_service(tmp_path)
    await service.start()
    try:
        FakeTvClient.instances[-1].connected = False      # the connection is gone; nobody has noticed yet
        s = service.status()
        assert s["reachable"] is False and s["on"] is False
    finally:
        await service.stop()


async def test_the_status_is_a_copy(tmp_path):
    service = build_service(tmp_path)
    await service.start()
    try:
        s = service.status()
        s["inputs"].append({"id": "HDMI_9", "label": "HDMI 9"})
        s["volume"] = 99
        assert service.status()["inputs"] == [{"id": "HDMI_1", "label": "HDMI 1"}, {"id": "HDMI_2", "label": "HDMI 2"}]
        assert service.status()["volume"] == 24
    finally:
        await service.stop()


@pytest.mark.parametrize("failure", [RuntimeError("closing"), ConnectionError("gone"), asyncio.CancelledError()])
async def test_a_client_that_cannot_say_whether_it_is_connected_is_not_answering(tmp_path, failure):
    """The library's is_connected() re-raises the error (or the cancellation) of a connect that failed, from the future
    it keeps, while that connect is being cleaned up: the status asked in that moment must not blow up."""
    service = build_service(tmp_path)
    assert await service._connect_once() is True

    def broken():
        raise failure
    FakeTvClient.instances[-1].is_connected = broken
    s = service.status()
    assert s["reachable"] is False and s["on"] is False
    with pytest.raises(TvError) as error:
        await service.control("volume_up")
    assert "not answering" in str(error.value)


async def test_the_goodbye_of_a_client_the_service_closes_is_not_news(tmp_path):
    """The library hands every callback a cleared state as a connection ends, ours included when we hang up (stop,
    forget, pairing again). That is not the TV going off, and nothing may act on it (the re-wake after an unexpected
    off, in the controls task, goes below the check in the state callback that this pins)."""
    service = build_service(tmp_path)
    assert await service._connect_once() is True
    assert service._shown["on"] is True
    await service._drop_client()
    assert service._shown["on"] is True                   # the cleared state the fake sent as it closed was not taken
    assert await service._connect_once() is True          # a new client: the TV is on again
    await FakeTvClient.instances[-1].drop()               # and now the TV is the one that hangs up
    assert service._shown["on"] is False


@pytest.mark.parametrize("pi_input, shown, app, shows", [
    ("HDMI_1", "HDMI_1", "com.webos.app.hdmi1", True),
    ("HDMI_1", "HDMI_2", "com.webos.app.hdmi2", False),     # another input, which the TV's list names
    ("HDMI_2", "HDMI_1", "com.webos.app.hdmi1", False),
    ("HDMI1", "HDMI_1", "com.webos.app.hdmi1", False),      # an input the list names is compared by its id alone
    ("HDMI_1", None, "com.webos.app.hdmi1", True),          # the list names no input for the app: the app's name says it ...
    ("HDMI_2", None, "com.webos.app.hdmi2", True),
    ("HDMI1", None, "com.webos.app.hdmi1", True),           # ... lower-cased, without the underscore
    ("HDMI_1", None, "com.webos.app.hdmi2", False),
    ("HDMI_1", None, "com.webos.app.hdmi10", False),        # (a different app, not one that begins as the Pi's does)
    ("HDMI_1", None, "com.webos.app.livetv", False),        # live TV and the apps are not the room Pi
    ("HDMI_1", None, "netflix", False),
    ("HDMI_1", None, None, None),                           # nothing is known of what is on the screen
    ("HDMI_1", None, "", None),
    ("", "HDMI_2", "com.webos.app.hdmi2", None),            # no input is named for the room Pi
    ("", None, "com.webos.app.hdmi1", None),
])
def test_whether_the_tv_shows_the_room_pis_input(pi_input, shown, app, shows):
    from croom.tv.service import shows_the_pi
    assert shows_the_pi(pi_input, shown, app) is shows
