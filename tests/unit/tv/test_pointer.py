"""The pointer: the events of the touchpad become the TV's cursor and a virtual mouse on the room Pi (spec 2026-10-13 TV remote and
touchpad, sections 3 and 5). The mouse runs on a fake device (the tests use pointer.CODES, and need neither evdev nor /dev/uinput), the TV
on the fake client, and nothing here leaves the machine."""
import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from croom.tv.errors import TvError
from croom.tv.pointer import CODES, PiMouse, PointerRelay
from tests.unit.tv.fake_client import FakeTvClient
from tests.unit.tv.helpers import build_service, forgetful_tv, running, silent_tv, until, within

EV_KEY, EV_REL = CODES["EV_KEY"], CODES["EV_REL"]
REL_X, REL_Y, REL_WHEEL = CODES["REL_X"], CODES["REL_Y"], CODES["REL_WHEEL"]
BTN_LEFT, BTN_RIGHT = CODES["BTN_LEFT"], CODES["BTN_RIGHT"]

TV_MOVE = {"target": "tv", "type": "move", "dx": 1, "dy": 0}


class FakeUInput:
    """What evdev.UInput is to the mouse: it notes the events written, and the syn() that reports them, in `writes`."""

    def __init__(self):
        self.writes = []
        self.closed = 0
        self.fail = None                  # an error that write() raises: the device has gone

    def write(self, kind, code, value):
        if self.fail is not None:
            raise self.fail
        self.writes.append((kind, code, value))

    def syn(self):
        self.writes.append("syn")

    def close(self):
        self.closed += 1


def a_mouse():
    """A virtual mouse on a fake device, and the device."""
    device = FakeUInput()
    return PiMouse(uinput_factory=lambda: device), device


class PointerTv(FakeTvClient):
    """A TV whose cursor can be held up. A move is noted as soon as it is sent, and then waits for `hold` (an asyncio.Event, when there
    is one) to open, as a TV that has been written to and has not taken it yet; `fail` is an error that the move then raises."""
    hold = None
    fail = None

    async def move(self, *args, **kwargs):
        self._record("move", *args, **kwargs)
        if self.hold is not None:
            await self.hold.wait()
        if self.fail is not None:
            raise self.fail

    async def click(self, *args, **kwargs):
        self._record("click", *args, **kwargs)
        if self.hold is not None:
            await self.hold.wait()


def sent(client):
    """What the TV's cursor was told, in order."""
    return [call for call in client.calls if call[0] in ("move", "click", "scroll")]


# The virtual mouse on the room Pi

async def test_pi_moves_clicks_and_scrolls_reach_the_virtual_mouse():
    mouse, device = a_mouse()
    relay = PointerRelay(tv_service=None, pi_mouse=mouse)
    await relay.handle({"target": "pi", "type": "move", "dx": 3, "dy": -2})
    await relay.handle({"target": "pi", "type": "tap"})
    await relay.handle({"target": "pi", "type": "scroll", "dy": -1})
    assert device.writes[:3] == [(EV_REL, REL_X, 3), (EV_REL, REL_Y, -2), "syn"]
    assert (EV_KEY, BTN_LEFT, 1) in device.writes and (EV_KEY, BTN_LEFT, 0) in device.writes
    assert (EV_REL, REL_WHEEL, -1) in device.writes


async def test_each_pi_event_is_what_a_mouse_would_report():
    mouse, device = a_mouse()
    relay = PointerRelay(None, mouse)
    for message in ({"type": "tap"}, {"type": "right"}, {"type": "scroll", "dy": 2}, {"type": "down"}, {"type": "move", "dx": 4, "dy": 0},
                    {"type": "up"}):
        await relay.handle(dict(message, target="pi"))
    assert device.writes == [
        (EV_KEY, BTN_LEFT, 1), "syn", (EV_KEY, BTN_LEFT, 0), "syn",                # a tap: the press and the release, each reported
        (EV_KEY, BTN_RIGHT, 1), "syn", (EV_KEY, BTN_RIGHT, 0), "syn",              # a two-finger tap
        (EV_REL, REL_WHEEL, 2), "syn",
        (EV_KEY, BTN_LEFT, 1), "syn", (EV_REL, REL_X, 4), "syn", (EV_KEY, BTN_LEFT, 0), "syn",     # a drag
    ]


async def test_a_move_along_one_axis_writes_that_axis_and_nothing_at_all_writes_nothing():
    mouse, device = a_mouse()
    relay = PointerRelay(None, mouse)
    await relay.handle({"target": "pi", "type": "move", "dy": 5})                    # no dx: it is 0
    await relay.handle({"target": "pi", "type": "move", "dx": 0, "dy": 0})
    await relay.handle({"target": "pi", "type": "scroll"})
    assert device.writes == [(EV_REL, REL_Y, 5), "syn"]


async def test_an_up_with_no_down_releases_nothing():
    mouse, device = a_mouse()
    relay = PointerRelay(None, mouse)
    await relay.handle({"target": "pi", "type": "up"})
    assert device.writes == []


async def test_a_dropped_socket_releases_a_held_button():
    mouse, device = a_mouse()
    relay = PointerRelay(None, mouse)
    await relay.handle({"target": "pi", "type": "down"})
    assert (EV_KEY, BTN_LEFT, 0) not in device.writes
    await relay.release_all()
    assert (EV_KEY, BTN_LEFT, 0) in device.writes
    n = len(device.writes)
    await relay.release_all()                                    # and once is enough
    assert len(device.writes) == n


async def test_a_dropped_socket_whose_mouse_has_gone_says_nothing():
    mouse, device = a_mouse()
    relay = PointerRelay(None, mouse)
    await relay.handle({"target": "pi", "type": "down"})
    device.fail = OSError(19, "No such device")
    await relay.release_all()                                    # there is nobody to tell: it raises nothing


def test_the_device_is_made_when_it_is_first_needed_and_closed_at_the_end():
    made = []

    def factory():
        made.append(FakeUInput())
        return made[-1]
    mouse = PiMouse(uinput_factory=factory)
    assert made == [] and mouse.unavailable is None              # a room that never uses the touchpad has no mouse on its desktop
    mouse.move(1, 1)
    mouse.click()
    assert len(made) == 1 and mouse.unavailable is None
    mouse.close()
    mouse.close()                                                # (once is enough)
    assert made[0].closed == 1
    mouse.move(1, 1)                                             # used again after it was closed: a new device
    assert len(made) == 2


@pytest.mark.parametrize("error, words", [
    (PermissionError(13, "Permission denied", "/dev/uinput"), ["not allowed to use /dev/uinput", "input group", "systemctl restart croom"]),
    (FileNotFoundError(2, "No such file or directory", "/dev/uinput"), ["/dev/uinput", "not there", "uinput kernel module", "modprobe uinput"]),
    (Exception('"/dev/uinput" cannot be opened for writing'), ["could not be made", "cannot be opened for writing", "uinput kernel module", "input group"]),
    (OSError(5, "Input/output error"), ["could not be made", "Input/output error"]),
])
async def test_a_mouse_that_cannot_be_made_says_what_to_do_to_every_event(caplog, error, words):
    attempts = []

    def failing():
        attempts.append(1)
        raise error
    mouse = PiMouse(uinput_factory=failing)
    relay = PointerRelay(None, mouse)
    assert mouse.unavailable is None                             # not known until it is tried
    for _ in range(3):
        with pytest.raises(TvError) as failure:
            await relay.handle({"target": "pi", "type": "move", "dx": 1, "dy": 1})
        for word in words:
            assert word in str(failure.value)
    assert mouse.unavailable == str(failure.value) and len(attempts) == 3
    said = [r for r in caplog.records if r.levelname == "WARNING" and r.name == "croom.tv.pointer"]
    assert len(said) == 1 and said[0].getMessage() == str(failure.value)                                                    # said once


async def test_a_mouse_that_could_not_be_made_is_tried_again_when_it_has_been_put_right():
    devices = []

    def factory():
        if not devices:
            devices.append(None)
            raise PermissionError(13, "Permission denied")
        devices.append(FakeUInput())
        return devices[-1]
    mouse = PiMouse(uinput_factory=factory)
    relay = PointerRelay(None, mouse)
    with pytest.raises(TvError):
        await relay.handle({"target": "pi", "type": "tap"})
    assert mouse.unavailable is not None
    await relay.handle({"target": "pi", "type": "tap"})          # the installer's group is there now, and the agent has been restarted
    assert mouse.unavailable is None and devices[1].writes[:2] == [(EV_KEY, BTN_LEFT, 1), "syn"]


async def test_a_device_that_stops_taking_events_is_let_go_of_and_made_again():
    devices = []

    def factory():
        devices.append(FakeUInput())
        return devices[-1]
    mouse = PiMouse(uinput_factory=factory)
    relay = PointerRelay(None, mouse)
    await relay.handle({"target": "pi", "type": "move", "dx": 1, "dy": 0})
    devices[0].fail = OSError(19, "No such device")
    with pytest.raises(TvError) as failure:
        await relay.handle({"target": "pi", "type": "move", "dx": 1, "dy": 0})
    assert "stopped taking events" in str(failure.value) and "No such device" in str(failure.value)
    assert devices[0].closed == 1
    await relay.handle({"target": "pi", "type": "move", "dx": 2, "dy": 0})
    assert len(devices) == 2 and devices[1].writes == [(EV_REL, REL_X, 2), "syn"]


def test_the_real_device_is_evdevs_uinput_with_the_capabilities_of_a_mouse(monkeypatch):
    made = []

    class UInput(FakeUInput):
        def __init__(self, capabilities, name):
            super().__init__()
            made.append((capabilities, name, self))
    monkeypatch.setitem(sys.modules, "evdev", SimpleNamespace(UInput=UInput))
    mouse = PiMouse()
    assert made == []
    mouse.move(2, 3)
    capabilities, name, device = made[0]
    assert capabilities == {EV_REL: [REL_X, REL_Y, REL_WHEEL], EV_KEY: [BTN_LEFT, BTN_RIGHT]} and name == "Crystal Meet touchpad"
    assert device.writes == [(EV_REL, REL_X, 2), (EV_REL, REL_Y, 3), "syn"]


async def test_without_the_evdev_library_the_mouse_says_so_and_the_module_still_imports(monkeypatch):
    monkeypatch.setitem(sys.modules, "evdev", None)              # an import of it fails from here on
    relay = PointerRelay(None, PiMouse())
    with pytest.raises(TvError) as failure:
        await relay.handle({"target": "pi", "type": "tap"})
    assert "evdev" in str(failure.value) and "pip install evdev" in str(failure.value)


KERNEL_HEADER = Path("/usr/include/linux/input-event-codes.h")


@pytest.mark.skipif(not KERNEL_HEADER.is_file(), reason="the kernel's input header is not on this machine")
def test_the_codes_are_the_kernels():
    """evdev is not here to check them: a wrong number would make the Pi's mouse press and move the wrong things, and nothing else
    here could tell."""
    defined = dict(re.findall(r"^#define\s+(\w+)\s+(0x[0-9a-fA-F]+|\d+)\b", KERNEL_HEADER.read_text(), re.M))
    assert set(CODES) == {"EV_KEY", "EV_REL", "REL_X", "REL_Y", "REL_WHEEL", "BTN_LEFT", "BTN_RIGHT"}
    for name, value in CODES.items():
        assert int(defined[name], 0) == value, name


# The TV's cursor

async def test_tv_moves_taps_and_scrolls_reach_the_tvs_cursor(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        relay = PointerRelay(service, a_mouse()[0])
        await relay.handle({"target": "tv", "type": "move", "dx": 5, "dy": -3})
        await relay.drain()
        await relay.handle({"target": "tv", "type": "tap"})
        await relay.handle({"target": "tv", "type": "scroll", "dx": 2, "dy": -4})
        await relay.handle({"target": "tv", "type": "scroll", "dy": 7})                  # no dx: it is 0
        assert sent(tv) == [("move", 5, -3), ("click",), ("scroll", 2, -4), ("scroll", 0, 7)]


async def test_a_burst_of_moves_is_coalesced_not_queued(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        tv.hold = asyncio.Event()                                # the TV is slow to take a move
        relay = PointerRelay(service, a_mouse()[0])
        for _ in range(50):
            await within(relay.handle(TV_MOVE))                  # none of them waits for the TV
        await until(lambda: sent(tv) == [("move", 1, 0)])        # the first is in flight, and no more than one is at any time
        await asyncio.sleep(0.05)
        assert sent(tv) == [("move", 1, 0)]
        tv.hold.set()
        await within(relay.drain())
        assert sent(tv) == [("move", 1, 0), ("move", 49, 0)]     # the 49 that came behind it went as one


async def test_the_moves_that_are_added_up_are_never_more_than_one_message_can_carry(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        tv.hold = asyncio.Event()
        relay = PointerRelay(service, a_mouse()[0])
        for _ in range(5):
            await relay.handle({"target": "tv", "type": "move", "dx": 1500, "dy": -1500})
        tv.hold.set()
        await within(relay.drain())
        assert sent(tv) == [("move", 1500, -1500), ("move", 2000, -2000)]


async def test_a_tap_waits_for_the_moves_before_it_and_goes_after_them(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        tv.hold = asyncio.Event()
        relay = PointerRelay(service, a_mouse()[0])
        for dx in (1, 2, 3):
            await relay.handle({"target": "tv", "type": "move", "dx": dx, "dy": 0})
        tapping = asyncio.ensure_future(relay.handle({"target": "tv", "type": "tap"}))
        await asyncio.sleep(0.05)
        assert sent(tv) == [("move", 1, 0)] and not tapping.done()                # the tap waits for the moves, the one in flight and the one behind it
        tv.hold.set()
        await within(tapping)
        assert sent(tv) == [("move", 1, 0), ("move", 5, 0), ("click",)]


async def test_a_scroll_waits_for_the_moves_before_it_too(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        tv.hold = asyncio.Event()
        relay = PointerRelay(service, a_mouse()[0])
        await relay.handle(TV_MOVE)
        await relay.handle(TV_MOVE)
        scrolling = asyncio.ensure_future(relay.handle({"target": "tv", "type": "scroll", "dy": 2}))
        await asyncio.sleep(0.05)
        assert not scrolling.done()
        tv.hold.set()
        await within(scrolling)
        assert sent(tv) == [("move", 1, 0), ("move", 1, 0), ("scroll", 0, 2)]


async def test_a_down_waits_for_the_moves_before_it_and_they_are_not_a_drag(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        tv.hold = asyncio.Event()
        relay = PointerRelay(service, a_mouse()[0])
        await relay.handle(TV_MOVE)
        await relay.handle(TV_MOVE)
        pressing = asyncio.ensure_future(relay.handle({"target": "tv", "type": "down"}))
        await asyncio.sleep(0.05)
        assert not pressing.done()
        tv.hold.set()
        await within(pressing)
        await relay.handle({"target": "tv", "type": "move", "dx": 7, "dy": 0})
        await relay.drain()
        assert sent(tv) == [("move", 1, 0), ("move", 1, 0), ("move", 7, 0, 1)]     # (only what comes after the down is with the button)


async def test_a_drag_moves_with_the_button_down_and_ends_where_the_finger_is_lifted(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        relay = PointerRelay(service, a_mouse()[0])
        await relay.handle({"target": "tv", "type": "move", "dx": 1, "dy": 1})        # before the down: an ordinary move
        await relay.handle({"target": "tv", "type": "down"})
        assert sent(tv) == [("move", 1, 1)]                                           # (nothing is sent for the down itself)
        await relay.handle({"target": "tv", "type": "move", "dx": 3, "dy": 4})
        await relay.handle({"target": "tv", "type": "up"})
        await relay.handle({"target": "tv", "type": "move", "dx": 2, "dy": 2})        # after the up: ordinary again
        await relay.drain()
        assert sent(tv) == [("move", 1, 1), ("move", 3, 4, 1), ("move", 0, 0), ("move", 2, 2)]


async def test_a_press_and_a_release_that_went_nowhere_is_a_click(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        relay = PointerRelay(service, a_mouse()[0])
        await relay.handle({"target": "tv", "type": "down"})
        await relay.handle({"target": "tv", "type": "up"})
        await relay.handle({"target": "tv", "type": "up"})                            # (an up with no down is nothing)
        assert sent(tv) == [("click",)]


async def test_a_move_or_a_scroll_of_nothing_is_sent_for_nothing(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        relay = PointerRelay(service, a_mouse()[0])
        await relay.handle({"target": "tv", "type": "move"})
        await relay.handle({"target": "tv", "type": "move", "dx": 0, "dy": 0})
        await relay.handle({"target": "tv", "type": "scroll"})
        await relay.handle({"target": "tv", "type": "scroll", "dx": 0, "dy": 0})
        assert relay.sending is None and sent(tv) == []


async def test_moves_that_cancel_out_behind_the_one_in_flight_are_not_sent(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        tv.hold = asyncio.Event()
        relay = PointerRelay(service, a_mouse()[0])
        await relay.handle(TV_MOVE)
        await relay.handle({"target": "tv", "type": "move", "dx": 2, "dy": 0})        # behind it ...
        await relay.handle({"target": "tv", "type": "move", "dx": -2, "dy": 0})       # ... and taken back
        tv.hold.set()
        await within(relay.drain())
        assert sent(tv) == [("move", 1, 0)]


async def test_a_press_whose_moves_went_nowhere_is_still_a_click(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        relay = PointerRelay(service, a_mouse()[0])
        await relay.handle({"target": "tv", "type": "down"})
        await relay.handle({"target": "tv", "type": "move", "dx": 0, "dy": 0})        # (a finger that did not move, but reported)
        await relay.handle({"target": "tv", "type": "up"})
        assert sent(tv) == [("click",)]


async def test_the_tvs_cursor_has_no_right_button_and_the_pi_has_one(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        relay = PointerRelay(service, a_mouse()[0])
        with pytest.raises(TvError) as failure:
            await relay.handle({"target": "tv", "type": "right"})
        assert str(failure.value) == "The TV's cursor has no right button" and sent(tv) == []


async def test_a_dropped_socket_lets_the_move_in_flight_finish_and_drops_the_moves_that_waited(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        tv.hold = asyncio.Event()
        relay = PointerRelay(service, a_mouse()[0])
        for dx in (1, 2, 3):
            await relay.handle({"target": "tv", "type": "move", "dx": dx, "dy": 0})
        leaving = asyncio.ensure_future(relay.release_all())
        await asyncio.sleep(0.05)
        assert not leaving.done()                                                   # the move in flight is let finish
        tv.hold.set()
        await within(leaving)
        assert sent(tv) == [("move", 1, 0)]                                         # and the 2 and the 3 that waited for it are dropped


async def test_a_dropped_socket_ends_a_drag_it_was_in_and_sends_nothing_for_a_press_that_never_moved(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        relay = PointerRelay(service, a_mouse()[0])
        await relay.handle({"target": "tv", "type": "down"})
        await relay.release_all()
        assert sent(tv) == []                                                       # (not a click: nobody lifted the finger)
        await relay.handle({"target": "tv", "type": "down"})
        await relay.handle({"target": "tv", "type": "move", "dx": 3, "dy": 4})
        await relay.release_all()
        assert sent(tv) == [("move", 3, 4, 1), ("move", 0, 0)]                       # the TV is not left dragging
        await relay.release_all()
        assert sent(tv) == [("move", 3, 4, 1), ("move", 0, 0)]


async def test_a_dropped_socket_raises_nothing_whatever_the_mouse_does():
    class Broken:
        """A mouse that raises what is not even words."""
        def press(self, button):
            pass

        def release(self, button):
            raise RuntimeError("a bug in the mouse")
    relay = PointerRelay(None, Broken())
    await relay.handle({"target": "pi", "type": "down"})
    await relay.release_all()


async def test_a_dropped_socket_says_nothing_when_the_tv_has_gone(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        relay = PointerRelay(service, a_mouse()[0])
        await relay.handle({"target": "tv", "type": "down"})
        await relay.handle({"target": "tv", "type": "move", "dx": 3, "dy": 4})
        await relay.drain()
        await tv.drop()
        await relay.release_all()                                                   # raises nothing: there is nobody to tell


NOT_READY = [
    pytest.param(FakeTvClient, "key-9", {"enabled": False, "host": ""}, id="not set up"),
    pytest.param(FakeTvClient, None, {}, id="not paired"),
    pytest.param(forgetful_tv, "key-9", {}, id="refused"),
    pytest.param(silent_tv, "key-9", {}, id="not answering"),
]


@pytest.mark.parametrize("factory, key, overrides", NOT_READY)
async def test_a_tv_that_cannot_be_told_anything_is_said_in_the_words_the_controls_have(tmp_path, factory, key, overrides):
    """Not set up, not paired, refused, not answering: the same words, whichever door the table uses; each message says it itself."""
    service = build_service(tmp_path, factory, key=key, **overrides)
    async with running(service):
        with pytest.raises(TvError) as control_says:
            await service.control("volume_up")
        relay = PointerRelay(service, a_mouse()[0])
        for message in (TV_MOVE, {"target": "tv", "type": "tap"}, {"target": "tv", "type": "scroll", "dy": 1},
                        {"target": "tv", "type": "down"}):
            with pytest.raises(TvError) as pointer_says:
                await relay.handle(message)
            assert str(pointer_says.value) == str(control_says.value)


async def test_a_room_without_a_tv_service_says_so_for_the_tv_and_still_has_its_pi():
    mouse, device = a_mouse()
    relay = PointerRelay(None, mouse)
    with pytest.raises(TvError) as failure:
        await relay.handle(TV_MOVE)
    assert str(failure.value) == "The TV is not enabled on this device (tv.enabled in its config)"
    await relay.handle({"target": "pi", "type": "tap"})
    assert (EV_KEY, BTN_LEFT, 1) in device.writes


async def test_a_move_that_failed_on_its_way_is_said_by_the_next_message_once(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        tv.fail = ConnectionResetError("Cannot write to closing transport")           # what the library raises on a socket that closed
        relay = PointerRelay(service, a_mouse()[0])
        await relay.handle(TV_MOVE)                                                   # sent in the background: it is not waited for
        await relay.drain()
        with pytest.raises(TvError) as failure:
            await relay.handle(TV_MOVE)                                               # the next message says what happened
        assert str(failure.value) == "The TV did not accept that: Cannot write to closing transport"
        tv.fail = None
        await relay.handle(TV_MOVE)                                                   # and it is said once: the cursor is tried again
        await relay.drain()
        assert sent(tv)[-1] == ("move", 1, 0) and len(sent(tv)) == 2


async def test_a_failure_that_is_a_bug_is_said_by_the_next_message_and_logged(tmp_path, caplog):
    class Buggy:
        """A TV service that answers once with a client, and then fails in a way that is not words."""
        def __init__(self, client):
            self.client, self.asked = client, 0

        def pointer(self):
            self.asked += 1
            if self.asked > 1:
                raise RuntimeError("the service has a bug")
            return self.client
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        relay = PointerRelay(Buggy(tv), a_mouse()[0])
        await relay.handle(TV_MOVE)                                                   # (the first look is sound; the send is not)
        await relay.drain()
        with pytest.raises(TvError) as failure:
            await relay.handle(TV_MOVE)
        assert str(failure.value) == "The TV did not accept that: the service has a bug"
    assert [r for r in caplog.records if r.levelname == "WARNING" and "the service has a bug" in r.getMessage() and r.exc_info]


async def test_a_tap_whose_call_fails_is_said_by_that_tap(tmp_path):
    class Refusing(PointerTv):
        async def click(self, *args, **kwargs):
            raise RuntimeError("Not connected, can't execute input command.")
    service = build_service(tmp_path, Refusing)
    async with running(service):
        relay = PointerRelay(service, a_mouse()[0])
        with pytest.raises(TvError) as failure:
            await relay.handle({"target": "tv", "type": "tap"})
        assert str(failure.value) == "The TV did not accept that: Not connected, can't execute input command."


async def test_the_moves_that_wait_when_the_tv_goes_are_said_in_the_words_the_service_has(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        tv.hold = asyncio.Event()
        relay = PointerRelay(service, a_mouse()[0])
        await relay.handle(TV_MOVE)
        await until(lambda: sent(tv) == [("move", 1, 0)])                             # the first is with the TV, which is slow to take it
        await relay.handle(TV_MOVE)                                                   # the next waits behind it
        await tv.drop()                                                               # and the TV goes away while it waits
        tv.hold.set()
        await within(relay.drain())
        with pytest.raises(TvError) as failure:
            await relay.handle(TV_MOVE)
        assert str(failure.value) == "The TV is not answering at 10.0.0.41"
        assert sent(tv) == [("move", 1, 0)]


async def test_a_move_that_the_tv_does_not_take_is_cut_off_and_said_by_the_next_message(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        tv.hold = asyncio.Event()                                                     # nothing it is sent is ever taken
        relay = PointerRelay(service, a_mouse()[0])
        relay.CALL_TIMEOUT_S = 0.1
        await relay.handle(TV_MOVE)
        await within(relay.drain(), 2.0)                                              # the move was not waited for for ever
        with pytest.raises(TvError) as failure:
            await relay.handle(TV_MOVE)
        assert str(failure.value) == "The TV is not answering"


async def test_a_tap_behind_a_move_that_the_tv_does_not_take_does_not_wait_for_ever(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        tv.hold = asyncio.Event()
        relay = PointerRelay(service, a_mouse()[0])
        relay.CALL_TIMEOUT_S = 0.1
        await relay.handle(TV_MOVE)
        with pytest.raises(TvError) as failure:
            await within(relay.handle({"target": "tv", "type": "tap"}), 2.0)
        assert str(failure.value) == "The TV is not answering"


async def test_a_tap_that_the_tv_does_not_take_is_cut_off_in_words(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        tv.hold = asyncio.Event()
        relay = PointerRelay(service, a_mouse()[0])
        relay.CALL_TIMEOUT_S = 0.1
        with pytest.raises(TvError) as failure:
            await within(relay.handle({"target": "tv", "type": "tap"}), 2.0)
        assert str(failure.value) == "The TV is not answering" and sent(tv) == [("click",)]


async def test_the_cursor_is_not_a_control(tmp_path):
    """It does not go through control() (which reads the TV back and has its turn behind the others): a hundred moves a second
    would be a hundred reads."""
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        async def never(*args):
            raise AssertionError("the cursor went through control()")
        service.control = never
        relay = PointerRelay(service, a_mouse()[0])
        await relay.handle(TV_MOVE)
        await relay.handle({"target": "tv", "type": "tap"})
        await relay.drain()
        assert sent(tv) == [("move", 1, 0), ("click",)]
        assert not [call for call in tv.calls if call[0].startswith("get_")]          # nothing was read back


async def test_the_real_library_turns_the_relays_calls_into_the_tvs_pointer_messages():
    """aiowebostv's own client, not connected to anything (its pointer socket is a stand-in that notes what is written to it): the
    positional calls the relay makes are the library's, and these are the messages that go to the TV."""
    aiowebostv = pytest.importorskip("aiowebostv")
    written = []

    class PointerSocket:
        async def send_str(self, message):
            written.append(message)
    client = aiowebostv.WebOsClient("10.0.0.41", "key-9")
    client.input_connection = PointerSocket()
    relay = PointerRelay(SimpleNamespace(pointer=lambda: client), a_mouse()[0])
    for message in ({"type": "move", "dx": 3, "dy": 4}, {"type": "down"}, {"type": "move", "dx": 5, "dy": 6}, {"type": "up"},
                    {"type": "tap"}, {"type": "scroll", "dx": 1, "dy": -2}):
        await relay.handle(dict(message, target="tv"))
    await relay.drain()
    assert written == ["type:move\ndx:3\ndy:4\ndown:0\n\n",
                       "type:move\ndx:5\ndy:6\ndown:1\n\n",             # (in the drag)
                       "type:move\ndx:0\ndy:0\ndown:0\n\n",             # (the drag ends)
                       "type:click\n\n",
                       "type:scroll\ndx:1\ndy:-2\n\n"]


async def test_the_task_that_sends_moves_is_there_to_be_ended_and_gone_when_it_is_over(tmp_path):
    service = build_service(tmp_path, PointerTv)
    async with running(service) as tv:
        tv.hold = asyncio.Event()
        relay = PointerRelay(service, a_mouse()[0])
        assert relay.sending is None
        await relay.handle(TV_MOVE)
        await until(lambda: sent(tv) == [("move", 1, 0)])                             # the move is with the TV, which does not take it
        sending = relay.sending
        assert sending is not None and not sending.done()
        sending.cancel()                                                              # what the service's stop does
        await asyncio.wait({sending})
        assert relay.sending is None
        await relay.release_all()                                                     # (and the socket's end has nothing left to wait for)


# What is a message and what is not

@pytest.mark.parametrize("message", ["a string", ["move"], [], None, 3, True])
async def test_a_message_that_is_not_an_object_is_refused_in_words(message):
    relay = PointerRelay(None, a_mouse()[0])
    with pytest.raises(TvError) as failure:
        await relay.handle(message)
    assert "JSON object" in str(failure.value)


@pytest.mark.parametrize("message", [
    {}, {"type": "move"}, {"target": None, "type": "move"}, {"target": "tvv", "type": "move"}, {"target": "TV", "type": "move"},
    {"target": ["tv"], "type": "move"}, {"target": {}, "type": "move"}, {"target": 1, "type": "move"},
])
async def test_a_message_with_no_such_target_is_refused_in_words(message):
    mouse, device = a_mouse()
    relay = PointerRelay(None, mouse)
    with pytest.raises(TvError) as failure:
        await relay.handle(message)
    assert '"tv" or "pi"' in str(failure.value) and device.writes == []


@pytest.mark.parametrize("target", ["tv", "pi"])
@pytest.mark.parametrize("kind", [None, "", "wiggle", "MOVE", "click", 5, ["move"], {}])
async def test_a_message_with_no_such_type_is_refused_in_words(target, kind):
    mouse, device = a_mouse()
    relay = PointerRelay(None, mouse)
    message = {"target": target} if kind is None else {"target": target, "type": kind}
    with pytest.raises(TvError) as failure:
        await relay.handle(message)
    assert "one of move, tap, right, scroll, down, up" in str(failure.value) and device.writes == []


@pytest.mark.parametrize("target", ["tv", "pi"])
@pytest.mark.parametrize("kind", ["move", "scroll"])
@pytest.mark.parametrize("axis", ["dx", "dy"])
@pytest.mark.parametrize("value", ["3", "", True, False, None, [1], {}, float("nan"), float("inf"), -float("inf")])
async def test_a_delta_that_is_not_a_number_is_refused_in_words_and_nothing_is_sent(tmp_path, target, kind, axis, value):
    service = build_service(tmp_path, PointerTv)
    mouse, device = a_mouse()
    async with running(service) as tv:
        relay = PointerRelay(service, mouse)
        with pytest.raises(TvError) as failure:
            await relay.handle({"target": target, "type": kind, axis: value})
        assert str(failure.value) == f"{axis} is a number"
        await relay.drain()
        assert sent(tv) == [] and device.writes == []


@pytest.mark.parametrize("value, written", [(3, 3), (-3, -3), (3.9, 3), (-3.9, -3), (0.4, 0), (10 ** 9, 2000), (-10 ** 9, -2000),
                                            (1e300, 2000), (2000, 2000), (2001, 2000), (-2001, -2000)])
async def test_a_delta_is_made_whole_and_kept_to_a_sane_range(value, written):
    mouse, device = a_mouse()
    relay = PointerRelay(None, mouse)
    await relay.handle({"target": "pi", "type": "move", "dx": value, "dy": value})
    assert device.writes == ([(EV_REL, REL_X, written), (EV_REL, REL_Y, written), "syn"] if written else [])


@pytest.mark.parametrize("target", ["tv", "pi"])
async def test_keys_are_refused_in_words(tmp_path, target):
    service = build_service(tmp_path, PointerTv)
    mouse, device = a_mouse()
    async with running(service) as tv:
        relay = PointerRelay(service, mouse)
        with pytest.raises(TvError) as failure:
            await relay.handle({"target": target, "type": "keys", "text": "a"})
        assert str(failure.value) == "Typing into the room Pi comes in a later version"
        assert sent(tv) == [] and device.writes == []


# The seam on the TV service

async def test_the_pointer_seam_is_the_client_the_service_holds(tmp_path):
    service = build_service(tmp_path)
    async with running(service) as tv:
        assert service.pointer() is tv


@pytest.mark.parametrize("factory, key, overrides", NOT_READY)
async def test_a_tv_that_is_not_ready_says_why_through_the_seam_in_the_words_control_uses(tmp_path, factory, key, overrides):
    service = build_service(tmp_path, factory, key=key, **overrides)
    async with running(service):
        with pytest.raises(TvError) as seam:
            service.pointer()
        with pytest.raises(TvError) as control:
            await service.control("volume_up")
        assert str(seam.value) == str(control.value) and str(seam.value)
