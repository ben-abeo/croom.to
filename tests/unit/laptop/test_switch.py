"""The US234's remote button pressed through a pin, and where the MeetUp is, read from the USB devices (spec 2026-10-27, section 2)."""
import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

from croom.laptop import switch as switch_module
from croom.laptop.switch import LAPTOP, ROOM, GpiodPin, UsbSwitch, usb_present

MEETUP = "046d:0866"


class Bench:
    """The switch on a fake pin, a fake USB and a fake clock. A press moves the MeetUp to the other side `delay` seconds later; the
    next `lost` presses do nothing (a press the switch did not take)."""

    def __init__(self, delay=1.0, lost=0):
        self.now, self.delay, self.lost = 0.0, delay, lost
        self.on_pi, self.arrival, self.presses, self.levels = True, None, 0, []
        self.pin = SimpleNamespace(set=self.set, close=lambda: None, chip="/dev/gpiochip4")

    def set(self, on):
        self.levels.append((self.now, on))
        if not on:
            return
        self.presses += 1
        if self.lost:
            self.lost -= 1
        else:
            self.arrival = self.now + self.delay

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        if self.arrival is not None and self.now >= self.arrival:
            self.on_pi, self.arrival = not self.on_pi, None
        await asyncio.sleep(0)

    def present(self, usb_id):
        assert usb_id == MEETUP
        return self.on_pi

    def switch(self, **kwargs):
        made = UsbSwitch(lambda: self.pin, MEETUP, press_ms=200, wait_s=10.0, poll_s=0.25, present=self.present, sleep=self.sleep,
                         clock=self.clock, **kwargs)
        assert made.open()
        return made


async def test_a_press_holds_the_pin_for_press_ms_and_lets_go():
    bench = Bench()
    await bench.switch().press()
    assert bench.levels == [(0.0, True), (0.2, False)]


async def test_a_move_presses_once_and_waits_for_the_meetup_to_leave():
    bench = Bench(delay=3.0)
    switch = bench.switch()
    assert switch.holder() == ROOM
    assert await switch.move_to(LAPTOP) is True
    assert switch.holder() == LAPTOP and bench.presses == 1


async def test_a_move_to_where_the_meetup_is_presses_nothing():
    bench = Bench()
    assert await bench.switch().move_to(ROOM) is True
    assert bench.presses == 0


async def test_a_move_seen_only_at_the_last_look_is_not_pressed_again():
    bench = Bench(delay=10.0)          # it arrives as the wait runs out: the look after the wait sees it
    assert await bench.switch().move_to(LAPTOP) is True
    assert bench.presses == 1


async def test_a_press_the_switch_did_not_take_is_pressed_again():
    bench = Bench(lost=1)
    assert await bench.switch().move_to(LAPTOP) is True
    assert bench.presses == 2 and bench.on_pi is False


async def test_two_presses_that_do_nothing_give_up():
    bench = Bench(lost=2)
    assert await bench.switch().move_to(LAPTOP) is False
    assert bench.presses == 2 and bench.on_pi is True


async def test_moves_are_one_at_a_time():
    bench = Bench(delay=2.0)
    switch = bench.switch()
    assert await asyncio.gather(switch.move_to(LAPTOP), switch.move_to(LAPTOP)) == [True, True]
    assert bench.presses == 1


@pytest.mark.parametrize("failure, words", [(OSError("[Errno 16] Device or resource busy"), "busy"),
                                            (ImportError("No module named 'gpiod'"), "gpiod")])
def test_a_pin_that_cannot_be_had_leaves_the_switch_unavailable_and_says_why(failure, words):
    def refuse():
        raise failure
    switch = UsbSwitch(refuse, MEETUP)
    assert switch.open() is False and not switch.available and words in switch.problem


def test_the_meetup_is_found_among_the_usb_devices_by_its_id(tmp_path):
    hub = tmp_path / "usb1"
    hub.mkdir()
    meetup = tmp_path / "1-1"
    meetup.mkdir()
    (meetup / "idVendor").write_text("046d\n")
    (meetup / "idProduct").write_text("0866\n")
    (tmp_path / "1-1:1.0").mkdir()              # an interface: no ids of its own
    assert usb_present(MEETUP, tmp_path) is True
    assert usb_present("046D:0866", tmp_path) is True
    assert usb_present("046d:0893", tmp_path) is False
    assert usb_present(MEETUP, tmp_path / "missing") is False


def fake_gpiod(monkeypatch, labels):
    """A gpiod module with the chips `labels` names (path -> label); the requests it made are returned."""
    requests = []

    class Chip:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_info(self):
            return SimpleNamespace(label=labels[self.path])

    class Request:
        def __init__(self, path, consumer, config):
            self.path, self.consumer, self.config, self.values, self.released = path, consumer, config, [], False

        def set_value(self, pin, value):
            self.values.append((pin, value))

        def release(self):
            self.released = True

    def request_lines(path, consumer, config):
        requests.append(Request(path, consumer, config))
        return requests[-1]

    line = types.ModuleType("gpiod.line")
    line.Direction = SimpleNamespace(OUTPUT="output")
    line.Value = SimpleNamespace(ACTIVE="active", INACTIVE="inactive")
    gpiod = types.ModuleType("gpiod")
    gpiod.Chip, gpiod.request_lines, gpiod.line = Chip, request_lines, line
    gpiod.LineSettings = lambda **settings: settings
    monkeypatch.setitem(sys.modules, "gpiod", gpiod)
    monkeypatch.setitem(sys.modules, "gpiod.line", line)
    monkeypatch.setattr(switch_module.glob, "glob", lambda pattern: sorted(labels))
    return requests


def test_the_pin_is_asked_of_the_headers_chip_whatever_its_number(monkeypatch):
    requests = fake_gpiod(monkeypatch, {"/dev/gpiochip0": "gpio-brcmstb@107d508500", "/dev/gpiochip4": "pinctrl-rp1"})
    pin = GpiodPin(17, active_low=True)
    assert pin.chip == "/dev/gpiochip4"
    [request] = requests
    assert request.consumer == "croom-laptop"
    assert request.config == {17: {"direction": "output", "active_low": True, "output_value": "inactive"}}
    pin.set(True)
    pin.set(False)
    pin.close()
    assert request.values == [(17, "active"), (17, "inactive")] and request.released


def test_a_computer_without_the_headers_chip_says_so(monkeypatch):
    fake_gpiod(monkeypatch, {"/dev/gpiochip0": "something-else"})
    with pytest.raises(OSError, match="pinctrl-rp1"):
        GpiodPin(17)


def test_from_config_reads_the_laptop_section():
    from croom.core.config import Config
    config = Config.from_dict({"laptop": {"usb_id": "046d:0893", "press_ms": 300, "switch_wait_s": 4}})
    switch = UsbSwitch.from_config(config)
    assert (switch._usb_id, switch._press_s, switch._wait_s) == ("046d:0893", 0.3, 4)


async def test_close_during_a_press_lets_go_of_the_pin_without_an_error():
    bench = Bench()
    made = bench.switch()
    real_sleep = made._sleep

    async def closes_then_sleeps(seconds):
        made.close()
        await real_sleep(seconds)
    made._sleep = closes_then_sleeps
    await made.press()
    assert bench.levels == [(0.0, True), (0.2, False)]


@pytest.mark.parametrize("usb_id", ["046d", "046d:", "046d:086", "046d:08666", "xxxx:0866", "046d-0866", " 046d:0866", ""])
def test_a_usb_id_that_is_not_vendor_colon_product_leaves_the_switch_unavailable(usb_id):
    asked = []
    made = UsbSwitch(lambda: asked.append("pin") or object(), usb_id)
    assert made.open() is False and not made.available and asked == []
    assert "laptop.usb_id" in made.problem and repr(usb_id) in made.problem


def test_a_usb_id_in_either_case_is_accepted():
    assert UsbSwitch(lambda: object(), "046D:0B21").open() is True
