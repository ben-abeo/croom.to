# The room's camera on a laptop: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A **Laptop** button on the touch Pi moves the room's Logitech MeetUp to a laptop at the table through an ATEN US234 USB switch (its remote button pressed by a relay on GPIO17), switches the TV to the laptop's HDMI input, holds the time with a booking, refuses Join while the laptop has the MeetUp, and gives everything back at the booking's end or on **Back to room**.

**Architecture:** A new `croom.laptop` package: `switch.py` (press the relay through libgpiod, read whether the MeetUp is on this Pi's USB from sysfs, move with press / wait / look / press again) and `service.py` (`LaptopService`, the `laptop` service: Laptop mode's state in `laptop.json`, a 2 s watch, the booking, the TV, the devices). Small additions elsewhere: `RoomBookings.in_progress()` and `find()`, `DevicesService.release()` and `reclaim()`, `TvService.show_input()` and `tv.laptop_input`, the control API's `/api/laptop`, the status's `laptop` block and the Join refusal, the room page's Laptop button and screen, `croom --check-laptop`.

**Tech Stack:** Python 3.11+ asyncio, aiohttp, `gpiod` (libgpiod 2 Python bindings, PyPI), pytest + pytest-asyncio (`asyncio_mode = "auto"`), Playwright for the page tests, plain JS on the room page.

**Spec:** `docs/superpowers/specs/2026-10-27-laptop-switch-design.md`

## Global Constraints

- Branch `laptop-switch`; every task ends with its own commit; no attribution lines in commit messages.
- Run tests with `.venv/bin/python -m pytest <path> -q` from the repo root (`/home/cpm_ssh/croom.to`).
- Config defaults, verbatim: `laptop.enabled: false`, `gpio_pin: 17`, `active_low: false`, `press_ms: 200`, `usb_id: "046d:0866"`, `switch_wait_s: 10`; `tv.laptop_input: ""`. A room with `laptop.enabled: false` behaves exactly as today (no `laptop` service is registered).
- The header's GPIO chip is found by label: `pinctrl-rp1` (Pi 5) or `pinctrl-bcm2711` (Pi 4). Never `RPi.GPIO`.
- Words the person sees, verbatim: "The switch did not answer: press the button on the switch box"; "The camera is on the laptop: press Back to room first"; "Choose HDMI 2 on the TV with its Input button" (the input's id with `_` as a space); "Choose Logitech MeetUp as the camera, microphone and speaker in your meeting app"; "The camera, microphones and speaker are on the laptop until 10:30" (or without " until …"); the booking's title "Laptop"; the duration list's title "Use a laptop for".
- Watch every 2 s; a press holds the pin `press_ms`; a move waits `switch_wait_s`, looks once more, and only then presses a second time; two presses at most.
- Code style: match the surrounding modules (docstrings that say why, `logger = logging.getLogger(__name__)`, words for the person in the errors, `# noqa: BLE001` on deliberate broad catches).

## Review Focus

- **The MeetUp takes longer than `switch_wait_s` to re-enumerate** (a USB 3 device after a host change): one press must still be one move; the switch never presses again before its last look. Pinned in Task 2 (`test_a_move_seen_only_at_the_last_look_is_not_pressed_again`).
- **A room with no Google calendar** (no `RoomBookings`): Laptop must still work, with no end and no booking, and Back to room must not touch a booking. Pinned in Task 6 (`test_a_room_with_no_calendar_lends_the_meetup_with_no_end`).
- **The agent restarts while the laptop has the MeetUp, after its end has passed**: the first look at start gives it back, with no tap. Pinned in Task 6 (`test_a_restart_after_the_end_gives_the_meetup_back_at_once`).
- **The MeetUp comes back at the switch itself** (someone presses the US234's own button, or the US234 hands it back when the laptop is unplugged): Laptop mode ends, the TV goes back to the Pi, the booking is left alone. Pinned in Task 6 (`test_the_meetup_given_back_at_the_switch_ends_laptop_mode_and_keeps_the_booking`).
- **A failed switch after Laptop booked the room**: the booking it made must not be left behind holding the room. Pinned in Task 6 (`test_a_switch_that_does_not_answer_ends_the_booking_it_made`).

## Decisions this plan adds to the spec

- `FakeUsbSwitch` lives with the tests (`tests/unit/laptop/fakes.py`), not in the package: nothing in a room runs it.
- A room with no Google calendar (the calendar service has no `bookings`) gets Laptop mode with no end and no booking; it lasts until Back to room or the switch's own button.
- The status's `holder` is `"room"` or `"laptop"` (Laptop mode's own view, kept true to the switch by the watch); there is no `"unknown"`.
- The Laptop screen is the idle screen's status card taken over (kicker, headline, detail, a **Back to room** button), with the join-by-link section and Options hidden; the TV Remote button in the top bar stays. This keeps the page's mode and view machinery as it is.

---

## File structure

| File | What it holds |
|---|---|
| `src/croom/core/config.py` (modify) | `LaptopConfig`, `Config.laptop`, `TvConfig.laptop_input`, parsing and `to_dict` |
| `src/croom/laptop/__init__.py` (create) | package marker with a one-line docstring |
| `src/croom/laptop/switch.py` (create) | `usb_present`, `GpiodPin`, `UsbSwitch`, `NO_ANSWER`, `ROOM`, `LAPTOP` |
| `src/croom/laptop/service.py` (create) | `LaptopService`, `LaptopError`, the words |
| `src/croom/laptop/check.py` (create) | `check_laptop` for `croom --check-laptop` |
| `src/croom/calendar/bookings.py` (modify) | `RoomBookings.in_progress()`, `RoomBookings.find()` |
| `src/croom/devices/service.py` (modify) | `DevicesService.release()`, `reclaim()`, the background check skipping while away |
| `src/croom/tv/selection.py` (modify) | `laptop_input` accepted and checked |
| `src/croom/tv/service.py` (modify) | `show_input()`, `_show()` (from `_show_the_pi`), `laptop_input` in `status()` |
| `src/croom/control/service.py` (modify) | `laptop=` argument, `/api/laptop`, status `laptop`, Join refusal, `laptop_input` in TV select |
| `src/croom/core/agent.py` (modify) | register `laptop` when enabled, hand it to the control page; `--check-laptop [--switch]` |
| `pyproject.toml` (modify) | `gpiod>=2.1; sys_platform == 'linux'` |
| `src/croom/control/static/{index.html,app.js,booking.js,remote.js,style.css,icons.svg}` (modify) | the Laptop button, the duration list in `laptop` mode, the Laptop screen, the Laptop input picker |
| `tests/unit/laptop/{__init__.py,fakes.py,test_switch.py,test_service.py,test_check.py}` (create) | the package's tests |
| `tests/unit/control/test_laptop_api.py`, `tests/unit/control/test_laptop_page.py` (create) | the route and the page |

---

### Task 1: The config

**Files:**
- Modify: `src/croom/core/config.py` (the `TvConfig` dataclass near line 226, a new dataclass after it, `Config` fields near line 274, `from_dict` near line 311, `to_dict` near line 383)
- Test: `tests/unit/core/test_config.py`

**Interfaces:**
- Produces: `LaptopConfig(enabled: bool, gpio_pin: int, active_low: bool, press_ms: int, usb_id: str, switch_wait_s: float)`, `Config.laptop`, `TvConfig.laptop_input: str`.

- [ ] **Step 1: Write the failing test** (append to `tests/unit/core/test_config.py`)

```python
def test_the_laptop_section_and_the_laptops_tv_input_have_defaults_and_round_trip():
    from croom.core.config import Config, LaptopConfig
    config = Config()
    assert config.laptop == LaptopConfig()
    assert (config.laptop.enabled, config.laptop.gpio_pin, config.laptop.active_low, config.laptop.press_ms,
            config.laptop.usb_id, config.laptop.switch_wait_s) == (False, 17, False, 200, "046d:0866", 10)
    assert config.tv.laptop_input == ""
    loaded = Config.from_dict({"laptop": {"enabled": True, "gpio_pin": 27, "active_low": True}, "tv": {"laptop_input": "HDMI_2"}})
    assert loaded.laptop.enabled and loaded.laptop.gpio_pin == 27 and loaded.laptop.active_low and loaded.laptop.usb_id == "046d:0866"
    again = Config.from_dict(loaded.to_dict())
    assert again.laptop == loaded.laptop and again.tv.laptop_input == "HDMI_2"


def test_an_empty_laptop_section_is_its_defaults():
    from croom.core.config import Config, LaptopConfig
    assert Config.from_dict({"laptop": None}).laptop == LaptopConfig()
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/python -m pytest tests/unit/core/test_config.py -q -k laptop`
Expected: FAIL with `ImportError: cannot import name 'LaptopConfig'`

- [ ] **Step 3: Implement**

In `TvConfig`, after `pi_input`:

```python
    laptop_input: str = ""     # the input a laptop at the table is on (spec 2026-10-27); empty: Laptop mode leaves the TV alone
```

After `TvConfig`:

```python
@dataclass
class LaptopConfig:
    """The room's MeetUp lent to a laptop at the table through an ATEN US234, whose remote button a relay on a GPIO pin presses
    (spec 2026-10-27). Off unless a room has the switch."""
    enabled: bool = False
    gpio_pin: int = 17              # BCM numbering: header pin 11
    active_low: bool = False        # true for a relay or optocoupler board that switches on when the pin goes low
    press_ms: int = 200             # how long the button is held
    usb_id: str = "046d:0866"       # the MeetUp, as lsusb shows it
    switch_wait_s: float = 10.0     # how long a press is given to move the MeetUp
```

In `Config`, after `tv`:

```python
    laptop: LaptopConfig = field(default_factory=LaptopConfig)
```

In `from_dict`'s tuple, after `("tv", TvConfig),` add `("laptop", LaptopConfig),`.

In `to_dict`, add `"laptop_input": self.tv.laptop_input,` after `"pi_input"` in the `"tv"` block, and after the `"tv"` block:

```python
            "laptop": {
                "enabled": self.laptop.enabled,
                "gpio_pin": self.laptop.gpio_pin,
                "active_low": self.laptop.active_low,
                "press_ms": self.laptop.press_ms,
                "usb_id": self.laptop.usb_id,
                "switch_wait_s": self.laptop.switch_wait_s,
            },
```

- [ ] **Step 4: Run the config tests**

Run: `.venv/bin/python -m pytest tests/unit/core/test_config.py -q`
Expected: all PASS (a test that lists `to_dict`'s keys exactly gets `laptop` added to its expected list in this step)

- [ ] **Step 5: Commit**

```bash
git add src/croom/core/config.py tests/unit/core/test_config.py
git commit -m "feat(laptop): the laptop config section and the TV's laptop input"
```

---

### Task 2: The USB switch driver

**Files:**
- Create: `src/croom/laptop/__init__.py`, `src/croom/laptop/switch.py`
- Create: `tests/unit/laptop/__init__.py` (empty), `tests/unit/laptop/test_switch.py`

**Interfaces:**
- Consumes: `Config.laptop` (Task 1).
- Produces: `ROOM = "room"`, `LAPTOP = "laptop"`, `NO_ANSWER`; `usb_present(usb_id: str, root: Path = USB_DEVICES) -> bool`; `GpiodPin(pin: int, active_low: bool)` with `.chip: str`, `.set(on: bool)`, `.close()`; `UsbSwitch(pin_factory, usb_id, press_ms=200, wait_s=10.0, poll_s=0.25, present=usb_present, sleep=asyncio.sleep, clock=time.monotonic)` with `open() -> bool`, `available: bool`, `problem: Optional[str]`, `chip: Optional[str]`, `holder() -> str`, `async press()`, `async move_to(holder: str) -> bool`, `close()`, `UsbSwitch.from_config(config)`.

- [ ] **Step 1: Write the failing tests** (`tests/unit/laptop/test_switch.py`)

```python
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
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest tests/unit/laptop/test_switch.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'croom.laptop'`

- [ ] **Step 3: Implement**

`src/croom/laptop/__init__.py`:

```python
"""Laptop mode: the room's MeetUp lent to a laptop at the table through a USB switch (spec 2026-10-27)."""
```

`src/croom/laptop/switch.py`:

```python
"""
The ATEN US234's remote button, pressed from a GPIO pin through a relay, and where the room's MeetUp is: on this Pi's USB or
not (spec docs/superpowers/specs/2026-10-27-laptop-switch-design.md, section 2). The button moves the switch's devices to the
other computer each time it is pressed, so a press is never repeated before the first is known to have failed.
"""

import asyncio
import glob
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

USB_DEVICES = Path("/sys/bus/usb/devices")
HEADER_CHIPS = ("pinctrl-rp1", "pinctrl-bcm2711")   # the 40-pin header's GPIO chip on a Pi 5 and on a Pi 4
NO_ANSWER = "The switch did not answer: press the button on the switch box"
ROOM, LAPTOP = "room", "laptop"


def usb_present(usb_id: str, root: Path = USB_DEVICES) -> bool:
    """Whether a device with this vendor:product id (as lsusb prints it) is on this computer's USB."""
    wanted = tuple(part.strip().lower() for part in usb_id.split(":"))
    try:
        devices = list(root.iterdir())
    except OSError:
        return False
    for device in devices:
        try:
            found = ((device / "idVendor").read_text().strip().lower(), (device / "idProduct").read_text().strip().lower())
        except OSError:
            continue        # an interface, or a device that went while it was read
        if found == wanted:
            return True
    return False


class GpiodPin:
    """One pin of the header as an output, through the kernel's GPIO character device (libgpiod 2): the Pi 5 has no RPi.GPIO. Held
    off from the moment it is requested. The chip is found by its label, so the kernel's numbering of the chips does not matter."""

    def __init__(self, pin: int, active_low: bool = False):
        import gpiod
        from gpiod.line import Direction, Value

        self.chip = self._header_chip(gpiod)
        self._pin, self._value = pin, Value
        settings = gpiod.LineSettings(direction=Direction.OUTPUT, active_low=active_low, output_value=Value.INACTIVE)
        self._request = gpiod.request_lines(self.chip, consumer="croom-laptop", config={pin: settings})

    @staticmethod
    def _header_chip(gpiod: Any) -> str:
        for path in sorted(glob.glob("/dev/gpiochip*")):
            try:
                with gpiod.Chip(path) as chip:
                    if chip.get_info().label in HEADER_CHIPS:
                        return path
            except OSError:
                continue
        raise OSError(f"no GPIO chip labelled {' or '.join(HEADER_CHIPS)}: is this a Raspberry Pi 4 or 5?")

    def set(self, on: bool) -> None:
        self._request.set_value(self._pin, self._value.ACTIVE if on else self._value.INACTIVE)

    def close(self) -> None:
        self._request.release()


class UsbSwitch:
    """The switch as Laptop mode uses it. `pin_factory` makes the pin (a GpiodPin on a Pi) when the service opens the switch;
    `present(usb_id)` says whether the MeetUp is on this Pi. One move at a time."""

    def __init__(self, pin_factory: Callable[[], Any], usb_id: str, press_ms: int = 200, wait_s: float = 10.0, poll_s: float = 0.25,
                 present: Callable[[str], bool] = usb_present, sleep=asyncio.sleep, clock=time.monotonic):
        self._pin_factory = pin_factory
        self._usb_id = usb_id
        self._press_s = press_ms / 1000
        self._wait_s = wait_s
        self._poll_s = poll_s
        self._present = present
        self._sleep = sleep
        self._clock = clock
        self._pin: Any = None
        self._problem: Optional[str] = "not opened yet"
        self._lock = asyncio.Lock()

    @classmethod
    def from_config(cls, config) -> "UsbSwitch":
        laptop = config.laptop
        return cls(lambda: GpiodPin(laptop.gpio_pin, laptop.active_low), laptop.usb_id, laptop.press_ms, laptop.switch_wait_s)

    def open(self) -> bool:
        """Request the pin. One that cannot be had (no gpiod, not a Pi, the pin taken by another program) leaves the room without
        Laptop mode, and says why, never without its page."""
        try:
            self._pin = self._pin_factory()
        except ImportError as e:
            self._problem = f"the gpiod library is not installed ({e})"
        except Exception as e:  # noqa: BLE001 - whatever the pin's library raises, the room goes on without Laptop mode
            self._problem = f"the switch's GPIO pin cannot be used: {e}"
        else:
            self._problem = None
        if self._problem:
            logger.warning(f"Laptop switch unavailable: {self._problem}")
        return self.available

    @property
    def available(self) -> bool:
        return self._pin is not None

    @property
    def problem(self) -> Optional[str]:
        return self._problem

    @property
    def chip(self) -> Optional[str]:
        return getattr(self._pin, "chip", None)

    def holder(self) -> str:
        """Who has the MeetUp: this Pi when it is on its USB, the laptop's side of the switch otherwise."""
        return ROOM if self._present(self._usb_id) else LAPTOP

    async def press(self) -> None:
        if self._pin is None:
            raise RuntimeError(self._problem or "the switch is not open")
        self._pin.set(True)
        try:
            await self._sleep(self._press_s)
        finally:
            self._pin.set(False)

    async def move_to(self, holder: str) -> bool:
        """Send the MeetUp to `holder` (ROOM or LAPTOP): a press, a wait for it to arrive, one look more, and only then a second
        press. Whether it got there."""
        async with self._lock:
            if self.holder() == holder:
                return True
            for _ in range(2):
                await self.press()
                if await self._arrives(holder):
                    return True
            logger.warning(f"Two presses did not move the MeetUp to the {holder}")
            return False

    async def _arrives(self, holder: str) -> bool:
        deadline = self._clock() + self._wait_s
        while self._clock() < deadline:
            if self.holder() == holder:
                return True
            await self._sleep(self._poll_s)
        return self.holder() == holder       # the look after the wait: a MeetUp that arrived as it ran out is not pressed back

    def close(self) -> None:
        pin, self._pin = self._pin, None
        if pin is not None:
            try:
                pin.close()
            except OSError:
                pass
        self._problem = "closed"
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/laptop/test_switch.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/croom/laptop tests/unit/laptop
git commit -m "feat(laptop): press the US234's button through a GPIO pin and find the MeetUp on the USB"
```

---

### Task 3: The booking in progress, and a booking by key

**Files:**
- Modify: `src/croom/calendar/bookings.py` (`RoomBookings`, after `busy()` near line 322)
- Test: `tests/unit/calendar/test_bookings.py`

**Interfaces:**
- Produces: `RoomBookings.in_progress() -> Optional[CalendarEvent]`, `RoomBookings.find(key: str) -> Optional[CalendarEvent]` (key: the room's tag `booking_id` or Google's `id`).

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/calendar/test_bookings.py`; `make`, `at`, `Clock`, the `clock` and `google` fixtures are already in the file)

```python
async def test_the_booking_in_progress_is_googles_or_the_rooms_own(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google)
    assert rooms.in_progress() is None
    gid = google.add("Design review", at(9), at(10))
    rooms.remember(list(google.events.values()))
    assert rooms.in_progress().id == gid
    clock.now = at(10, 5)
    assert rooms.in_progress() is None                  # it ended at 10:00
    booking = await rooms.start_now(30, "Laptop", "room page")
    assert rooms.in_progress().booking_id == booking.id


async def test_a_declined_or_cancelled_booking_is_not_in_progress(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google)
    gid = google.add("Declined", at(9), at(10))
    google.events[gid].response_status = "declined"
    rooms.remember(list(google.events.values()))
    assert rooms.in_progress() is None


async def test_a_booking_is_found_by_the_rooms_tag_or_googles_id(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google)
    booking = await rooms.start_now(30, "Laptop", "room page")
    assert rooms.find(booking.id).end_time == booking.end
    assert rooms.find(booking.google_id).booking_id == booking.id
    assert rooms.find("nothing-like-it") is None
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest tests/unit/calendar/test_bookings.py -q -k "in_progress or found_by"`
Expected: FAIL with `AttributeError: 'RoomBookings' object has no attribute 'in_progress'`

- [ ] **Step 3: Implement** (in `RoomBookings`, after `busy()`)

```python
    def in_progress(self) -> Optional[CalendarEvent]:
        """The booking the room is in at this moment, Google's or its own, not cancelled or declined: the one Laptop mode lends
        the MeetUp for (spec 2026-10-27). None when the room is free."""
        now = self.clock()
        return next((e for e in self.merge() if e.start_time <= now < e.end_time
                     and e.status != "cancelled" and e.response_status != "declined"), None)

    def find(self, key: str) -> Optional[CalendarEvent]:
        """A booking the room shows, by the room's tag or by Google's id: Laptop mode follows its end as it moves in Google."""
        return next((e for e in self.merge() if key in (e.booking_id, e.id)), None)
```

- [ ] **Step 4: Run the bookings tests**

Run: `.venv/bin/python -m pytest tests/unit/calendar/test_bookings.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/croom/calendar/bookings.py tests/unit/calendar/test_bookings.py
git commit -m "feat(booking): the booking in progress, and a booking by its key"
```

---

### Task 4: The devices let go of the MeetUp and take it back

**Files:**
- Modify: `src/croom/devices/service.py`
- Test: `tests/unit/devices/test_service.py`

**Interfaces:**
- Produces: `DevicesService.release() -> None` (async), `DevicesService.reclaim() -> None` (async), class attributes `RECLAIM_S = 15.0`, `RECLAIM_POLL_S = 0.5`.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/devices/test_service.py`; `service_for`, `HOME`, `until`, `MEETUP` are in the file)

```python
# ----------------------------------------------------------------------
# Laptop mode (spec 2026-10-27): the MeetUp goes to a laptop and comes back
# ----------------------------------------------------------------------

async def test_release_closes_the_camera_and_the_background_check_leaves_it_alone(tmp_path):
    service, v4l2, _ = service_for(tmp_path)
    service.REDISCOVER_S = 0.02
    await service.start()
    try:
        service.camera.set_preview(True)
        await service.release()
        assert not service.camera.available and not service.camera.preview
        opens = [c for c in v4l2.calls if c[0] == "open"]
        await asyncio.sleep(0.1)                        # several rounds of the background check
        assert [c for c in v4l2.calls if c[0] == "open"] == opens
    finally:
        await service.stop()


async def test_reclaim_finds_the_camera_now_and_homes_it(tmp_path):
    service, v4l2, runner = service_for(tmp_path, home=HOME)
    await service.start()
    try:
        await service.homing
        await service.release()
        dumps = runner.calls.count(["pw-dump"])
        await service.reclaim()
        assert service.camera.available and runner.calls.count(["pw-dump"]) > dumps
        await service.homing
        assert service.camera.position_known
    finally:
        await service.stop()


async def test_reclaim_waits_for_a_camera_that_takes_a_moment_to_come_back(tmp_path):
    service, v4l2, _ = service_for(tmp_path)
    service.RECLAIM_POLL_S = 0.01
    await service.start()
    try:
        await service.release()
        meetup = v4l2._nodes.pop("/dev/video0")           # the switch has moved it, and it is not back yet
        reclaiming = asyncio.ensure_future(service.reclaim())
        await asyncio.sleep(0.05)
        assert not reclaiming.done()
        v4l2._nodes["/dev/video0"] = meetup
        await asyncio.wait_for(reclaiming, 2)
        assert service.camera.available
    finally:
        await service.stop()


async def test_reclaim_gives_up_on_a_camera_that_does_not_come_back(tmp_path):
    service, v4l2, _ = service_for(tmp_path)
    service.RECLAIM_S, service.RECLAIM_POLL_S = 0.1, 0.01
    await service.start()
    try:
        await service.release()
        v4l2._nodes.pop("/dev/video0")
        await asyncio.wait_for(service.reclaim(), 2)
        assert not service.camera.available
    finally:
        await service.stop()
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest tests/unit/devices/test_service.py -q -k "release or reclaim"`
Expected: FAIL with `AttributeError: 'DevicesService' object has no attribute 'release'`

- [ ] **Step 3: Implement**

In `DevicesService`, beside `REDISCOVER_S`:

```python
    RECLAIM_S = 15.0        # the MeetUp takes a few seconds to come back as a camera and a speaker after the switch moves it
    RECLAIM_POLL_S = 0.5
```

In `__init__`, after `self._last_failure`:

```python
        self._away = False   # the MeetUp is lent to a laptop (spec 2026-10-27): the background check does not look for it
```

In `_rediscover`, first line inside the loop after the sleep:

```python
            if self._away:
                continue
```

After `_rediscover`:

```python
    async def release(self) -> None:
        """The MeetUp is going to a laptop (spec 2026-10-27 Laptop mode): the preview ends, the camera is closed (a homing under way
        is cut short), and the background check leaves the camera and the speaker alone until reclaim()."""
        self._away = True
        self.camera.set_preview(False)
        await self.camera.close()

    async def reclaim(self) -> None:
        """The MeetUp is back: look for it now, not at the next background check, giving it RECLAIM_S to reappear as a camera, then
        home the camera, which the laptop may have moved."""
        self._away = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.RECLAIM_S
        while True:
            await self._check(self.camera.discover)
            await self._check(self.volume.refresh, force=True)
            if self.camera.available or loop.time() >= deadline:
                break
            await asyncio.sleep(self.RECLAIM_POLL_S)
        if self.camera.available and self.camera.home_saved and (self.homing is None or self.homing.done()):
            self.homing = asyncio.create_task(self._home_at_start())
```

- [ ] **Step 4: Run the devices tests**

Run: `.venv/bin/python -m pytest tests/unit/devices -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/croom/devices/service.py tests/unit/devices/test_service.py
git commit -m "feat(devices): let go of the MeetUp for a laptop and take it back"
```

---

### Task 5: The TV's laptop input

**Files:**
- Modify: `src/croom/tv/selection.py`, `src/croom/tv/service.py` (`status()` near line 379, `_show_the_pi` near line 835)
- Test: `tests/unit/tv/test_selection.py`, create `tests/unit/tv/test_laptop_input.py`

**Interfaces:**
- Consumes: `TvConfig.laptop_input` (Task 1).
- Produces: `TvService.show_input(which: str) -> Optional[str]` (async; `which` is `"pi"` or `"laptop"`; None when the TV shows it, else the words); `status()["laptop_input"]`; `connection_settings` accepting `laptop_input`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/tv/test_selection.py`:

```python
def test_the_laptops_input_is_an_hdmi_input_of_its_own_or_not_set():
    from croom.core.config import TvConfig
    from croom.tv.errors import TvError
    from croom.tv.selection import connection_settings
    current = TvConfig(enabled=True, host="10.0.0.41", pi_input="HDMI_1")
    assert connection_settings(current, {"laptop_input": "HDMI_2"}).laptop_input == "HDMI_2"
    assert connection_settings(current, {"laptop_input": ""}).laptop_input == ""
    with pytest.raises(TvError, match="of its own"):
        connection_settings(current, {"laptop_input": "HDMI_1"})
    with pytest.raises(TvError, match="laptop"):
        connection_settings(current, {"laptop_input": "USB"})
```

(If the file does not import `pytest` yet, add `import pytest` at its top.)

`tests/unit/tv/test_laptop_input.py`:

```python
"""Laptop mode and the TV (spec 2026-10-27): the TV switched to the laptop's input and back, or words that say which to choose."""
from tests.unit.tv.helpers import running, service_for
from tests.unit.tv.test_keep_on import on_input


def switched(client):
    return [call for call in client.calls if call[0] == "set_input"]


async def test_the_tv_goes_to_the_laptops_input_and_back_to_the_pis(tmp_path):
    service = service_for(tmp_path, on_input("HDMI_1"), key="key-9", laptop_input="HDMI_2")
    async with running(service) as client:
        assert service.status()["laptop_input"] == "HDMI_2"
        assert await service.show_input("laptop") is None
        assert service.status()["input"] == "HDMI_2"
        assert await service.show_input("pi") is None
        assert switched(client) == [("set_input", "HDMI_2"), ("set_input", "HDMI_1")]


async def test_a_tv_already_on_the_input_is_left_alone(tmp_path):
    service = service_for(tmp_path, on_input("HDMI_2"), key="key-9", laptop_input="HDMI_2")
    async with running(service) as client:
        assert await service.show_input("laptop") is None
        assert switched(client) == []


async def test_a_room_with_no_laptop_input_is_told_to_choose_one(tmp_path):
    service = service_for(tmp_path, on_input("HDMI_1"), key="key-9")
    async with running(service) as client:
        words = await service.show_input("laptop")
        assert "Options" in words and switched(client) == []


async def test_a_tv_that_is_not_paired_gets_the_input_in_words(tmp_path):
    service = service_for(tmp_path, laptop_input="HDMI_2")          # no key: not paired
    async with running(service):
        assert await service.show_input("laptop") == "Choose HDMI 2 on the TV with its Input button"


async def test_a_tv_that_refuses_the_switch_gets_the_input_in_words(tmp_path):
    def refusing(host, key):
        client = on_input("HDMI_1")(host, key)

        async def set_input(*args):
            raise ConnectionError("no")
        client.set_input = set_input
        return client
    service = service_for(tmp_path, refusing, key="key-9", laptop_input="HDMI_2")
    async with running(service):
        assert await service.show_input("laptop") == "Choose HDMI 2 on the TV with its Input button"
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest tests/unit/tv/test_laptop_input.py tests/unit/tv/test_selection.py -q`
Expected: FAIL (`TvError: Only the MAC address …` for `laptop_input`, `AttributeError: … 'show_input'`)

- [ ] **Step 3: Implement**

`src/croom/tv/selection.py`, `connection_settings` becomes:

```python
def connection_settings(current: TvConfig, changes: Dict[str, Any]) -> TvConfig:
    """Only the settings a person can correct without selecting another TV: its MAC address and the HDMI inputs."""
    if set(changes) - {"mac", "pi_input", "laptop_input"}:
        raise TvError("Only the MAC address and the HDMI inputs can be changed here; use Find TVs to choose another TV")
    mac = changes.get("mac", current.mac)
    if not isinstance(mac, str):
        raise TvError("The MAC address must be text")
    mac = mac.strip().replace("-", ":").upper()
    if re.fullmatch(r"[0-9A-F]{12}", mac):
        mac = ":".join(mac[index:index + 2] for index in range(0, 12, 2))
    if mac:
        if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
            raise TvError("Enter the TV's MAC address as six pairs, like AA:BB:CC:DD:EE:02")
        if mac == "00:00:00:00:00:00" or int(mac[:2], 16) & 1:
            raise TvError("Enter the TV's own MAC address, not a broadcast or multicast address")
    pi_input = changes.get("pi_input", current.pi_input)
    if not isinstance(pi_input, str) or not re.fullmatch(r"HDMI_[1-9][0-9]?", pi_input):
        raise TvError("Choose the HDMI input the room Pi is connected to")
    laptop_input = changes.get("laptop_input", current.laptop_input)
    if not isinstance(laptop_input, str) or (laptop_input and not re.fullmatch(r"HDMI_[1-9][0-9]?", laptop_input)):
        raise TvError("Choose the HDMI input the laptop's cable is on, or Not set")
    if laptop_input and laptop_input == pi_input:
        raise TvError("The laptop needs an HDMI input of its own, not the room Pi's")
    return replace(current, mac=mac, pi_input=pi_input, laptop_input=laptop_input)
```

In `selected_config`, the dict passed to `connection_settings` gains `"laptop_input": device.get("laptop_input", current.laptop_input),`.

`src/croom/tv/service.py`: in `status()`, add `laptop_input=self._config.laptop_input,` after `pi_input=self._config.pi_input,`. Replace `_show_the_pi`'s body and add `_show` and `show_input` after it:

```python
    async def _show_the_pi(self, deadline: Optional[float] = None) -> None:
        """(keep the existing docstring unchanged)"""
        await self._show(self._config.pi_input, "the room Pi's input", deadline)

    async def _show(self, input_id: str, what: str, deadline: Optional[float] = None) -> bool:
        """The TV switched to `input_id` unless it shows it already (or says too little to tell, see shows_the_pi); `what` names the
        input in the log. Whether it shows it now. A TV that refuses is logged; the switch and the reads after it are held to
        `deadline` (a time on the loop's clock)."""
        shown, app, client = self._shown["input"], self._shown["app"], self._client
        if shows_the_pi(input_id, shown, app) is not False:
            return True
        ask = self._within(self.ASK_TIMEOUT_S, deadline)
        if client is None or not ask:
            return False
        try:
            await asyncio.wait_for(client.set_input(input_id), ask)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"The TV at {self._config.host} did not switch to {what} {input_id}: {_why(e)}")
            return False
        logger.info(f"The TV at {self._config.host} showed {shown or app}: switched to {what} {input_id}")
        await self._read_back(client, "input", deadline)
        return True

    async def show_input(self, which: str) -> Optional[str]:
        """Laptop mode (spec 2026-10-27): the TV woken and switched to the room Pi's input ("pi") or the laptop's ("laptop"). None
        when the TV shows it; else the words that tell the person at the table which input to choose with the TV's own remote (a TV
        not set up, not answering, not reporting its inputs, or refusing)."""
        input_id = self._config.pi_input if which == "pi" else self._config.laptop_input
        if not input_id:
            return "Choose the laptop's input under Options › TV, then on the TV with its Input button"
        words = f"Choose {input_id.replace('_', ' ')} on the TV with its Input button"
        if not self._can_wait():
            return words
        self._held_off = False
        if not await self.ensure_on() or not self._is_on():
            return words
        if self.capabilities().get("direct_inputs") is False:
            return words
        what = "the room Pi's input" if which == "pi" else "the laptop's input"
        return None if await self._show(input_id, what) else words
```

- [ ] **Step 4: Run the TV tests**

Run: `.venv/bin/python -m pytest tests/unit/tv -q`
Expected: all PASS (the join tests still find "did not switch to the room Pi's input HDMI_1")

- [ ] **Step 5: Commit**

```bash
git add src/croom/tv tests/unit/tv
git commit -m "feat(tv): the laptop's HDMI input, and switching the TV to it and back"
```

---

### Task 6: Laptop mode

**Files:**
- Create: `src/croom/laptop/service.py`, `tests/unit/laptop/fakes.py`, `tests/unit/laptop/test_service.py`

**Interfaces:**
- Consumes: `UsbSwitch` API and `ROOM`, `LAPTOP`, `NO_ANSWER` (Task 2); `RoomBookings.in_progress()`, `find()`, `start_now()`, `end()` (Task 3 and existing); `DevicesService.release()`, `reclaim()` (Task 4); `TvService.show_input()` (Task 5); `MeetingService.state` (a `MeetingState`).
- Produces: `LaptopService(switch, store, calendar=None, meeting=None, devices=None, tv=None, clock=None)` named `"laptop"`, `LaptopService.from_config(config, calendar=None, meeting=None, devices=None, tv=None, switch=None)`, `async to_laptop(minutes: Optional[int] = None) -> dict`, `async to_room(by_hand: bool = True) -> dict`, `state() -> dict` with keys `enabled, available, holder, until, made_booking, switching, problem`, `camera_away: bool`, `WATCH_S = 2.0`; `LaptopError(message, status=409)`; constants `CAMERA_AWAY`, `IN_A_MEETING`, `SWITCHING`, `HOW_LONG`, `TITLE = "Laptop"`.

- [ ] **Step 1: Write the fakes** (`tests/unit/laptop/fakes.py`)

```python
"""Stand-ins for Laptop mode's tests: the switch, the devices and the TV, each recording what it was asked."""
from croom.laptop.switch import LAPTOP, ROOM


class FakeUsbSwitch:
    """The switch with the MeetUp on this Pi to begin with. `answers = False` makes a move that does not happen."""

    def __init__(self, available=True):
        self._available, self.on_pi, self.answers, self.moves, self.opened, self.closed = available, True, True, [], False, False

    def open(self):
        self.opened = True
        return self._available

    @property
    def available(self):
        return self._available

    @property
    def problem(self):
        return None if self._available else "the switch's GPIO pin cannot be used: busy"

    def holder(self):
        return ROOM if self.on_pi else LAPTOP

    async def move_to(self, holder):
        self.moves.append(holder)
        if self.answers:
            self.on_pi = holder == ROOM
        return self.holder() == holder

    def close(self):
        self.closed = True


class FakeDevices:
    def __init__(self):
        self.calls = []

    async def release(self):
        self.calls.append("release")

    async def reclaim(self):
        self.calls.append("reclaim")


class FakeTv:
    def __init__(self, words=None):
        self.calls, self.words = [], words

    async def show_input(self, which):
        self.calls.append(which)
        return self.words
```

- [ ] **Step 2: Write the failing tests** (`tests/unit/laptop/test_service.py`)

```python
"""Laptop mode (spec 2026-10-27): the MeetUp lent and given back, the booking, the TV, the devices, a restart, the switch's own button."""
import asyncio
from types import SimpleNamespace

import pytest

from croom.calendar.bookings import RoomBookings, SlotTaken
from croom.control.settings import SettingsStore
from croom.laptop.service import CAMERA_AWAY, HOW_LONG, IN_A_MEETING, LaptopError, LaptopService
from croom.laptop.switch import NO_ANSWER
from croom.meeting.providers.base import MeetingState
from tests.unit.calendar.test_bookings import ROOM_CAL, Clock, FakeGoogle, at
from tests.unit.laptop.fakes import FakeDevices, FakeTv, FakeUsbSwitch


class Room:
    """Laptop mode on fakes, with the real bookings on a fake Google and a clock the test moves."""

    def __init__(self, tmp_path, calendar=True, switch=None, tv_words=None):
        self.tmp_path = tmp_path
        self.clock = Clock(at(9, 30))
        self.google = FakeGoogle()
        self.rooms = None
        if calendar:
            self.rooms = RoomBookings(tmp_path / "bookings.json", tmp_path / "calendar-cache.json", "Room 1",
                                      booking_start="08:00", booking_end="18:00", clock=self.clock)
            self.rooms.attach(self.google, ROOM_CAL)
        self.calendar = SimpleNamespace(bookings=self.rooms)
        self.meeting = SimpleNamespace(state=MeetingState.IDLE)
        self.switch = switch or FakeUsbSwitch()
        self.devices, self.tv = FakeDevices(), FakeTv(tv_words)
        self.service = self.make()

    def make(self):
        return LaptopService(self.switch, SettingsStore(self.tmp_path / "laptop.json"), calendar=self.calendar, meeting=self.meeting,
                             devices=self.devices, tv=self.tv, clock=self.clock)

    async def look(self):
        await self.service._tick()


@pytest.fixture
def room(tmp_path):
    return Room(tmp_path)


async def test_laptop_with_nothing_booked_books_the_room_and_switches(room):
    state = await room.service.to_laptop(45)
    assert state["holder"] == "laptop" and state["made_booking"] and room.service.camera_away
    [booked] = room.google.events.values()
    assert booked.title == "Laptop" and booked.end_time == at(10, 15)
    assert state["until"] == at(10, 15).isoformat()
    assert room.devices.calls == ["release"] and room.switch.moves == ["laptop"] and room.tv.calls == ["laptop"]


async def test_laptop_during_a_booking_uses_it_and_books_nothing(room):
    room.google.add("Design review", at(9), at(11))
    room.rooms.remember(list(room.google.events.values()))
    state = await room.service.to_laptop()
    assert state["until"] == at(11).isoformat() and not state["made_booking"]
    assert len(room.google.events) == 1


async def test_laptop_with_nothing_booked_asks_how_long(room):
    with pytest.raises(LaptopError) as refused:
        await room.service.to_laptop()
    assert str(refused.value) == HOW_LONG and refused.value.status == 400 and room.switch.moves == []


async def test_a_time_already_taken_is_refused_before_any_press(room):
    room.google.add("Board", at(9, 35), at(10))
    room.rooms.remember(list(room.google.events.values()))
    with pytest.raises(SlotTaken):
        await room.service.to_laptop(30)
    assert room.switch.moves == [] and room.devices.calls == []


@pytest.mark.parametrize("state", [MeetingState.JOINING, MeetingState.CONNECTED])
async def test_laptop_is_refused_while_the_room_is_in_a_meeting(room, state):
    room.meeting.state = state
    with pytest.raises(LaptopError, match=IN_A_MEETING):
        await room.service.to_laptop(30)
    assert room.switch.moves == []


async def test_a_switch_that_does_not_answer_ends_the_booking_it_made(room):
    room.switch.answers = False
    with pytest.raises(LaptopError, match=NO_ANSWER):
        await room.service.to_laptop(30)
    assert room.google.events == {} and not room.service.camera_away
    assert room.devices.calls == ["release", "reclaim"] and room.service.state()["problem"] == NO_ANSWER


async def test_back_to_room_by_hand_ends_the_booking_laptop_made(room):
    await room.service.to_laptop(60)
    room.clock.now = at(9, 50)
    state = await room.service.to_room()
    assert state["holder"] == "room" and not room.service.camera_away
    [booked] = room.google.events.values()
    assert booked.end_time == at(9, 50)                          # ended now, not left holding the room
    assert room.devices.calls == ["release", "reclaim"] and room.tv.calls == ["laptop", "pi"]


async def test_back_to_room_leaves_a_booking_someone_else_made(room):
    room.google.add("Design review", at(9), at(11))
    room.rooms.remember(list(room.google.events.values()))
    await room.service.to_laptop()
    await room.service.to_room()
    [booked] = room.google.events.values()
    assert booked.end_time == at(11)


async def test_the_meetup_goes_back_when_the_booking_ends(room):
    await room.service.to_laptop(30)
    room.clock.now = at(10, 0)
    await room.look()
    assert room.switch.moves == ["laptop", "room"] and not room.service.camera_away
    [booked] = room.google.events.values()
    assert booked.end_time == at(10, 0)                          # it ran its time; nothing was cut short


async def test_the_end_follows_the_booking_as_it_moves_in_google(room):
    gid = room.google.add("Design review", at(9), at(10))
    room.rooms.remember(list(room.google.events.values()))
    await room.service.to_laptop()
    room.google.events[gid].end_time = at(10, 30)
    room.rooms.remember(list(room.google.events.values()))
    await room.look()
    assert room.service.state()["until"] == at(10, 30).isoformat()
    room.clock.now = at(10, 5)
    await room.look()
    assert room.service.camera_away                              # not at the old end
    del room.google.events[gid]
    room.rooms.remember(list(room.google.events.values()))
    await room.look()
    assert room.service.state()["until"] == at(10, 30).isoformat()   # deleted: the last end known is kept


async def test_the_meetup_taken_at_the_switch_shows_laptop_mode_with_no_end(room):
    room.switch.on_pi = False
    await room.look()
    state = room.service.state()
    assert state["holder"] == "laptop" and state["until"] is None and room.devices.calls == ["release"]


async def test_the_meetup_given_back_at_the_switch_ends_laptop_mode_and_keeps_the_booking(room):
    await room.service.to_laptop(60)
    room.switch.on_pi = True
    await room.look()
    assert not room.service.camera_away and room.tv.calls == ["laptop", "pi"]
    [booked] = room.google.events.values()
    assert booked.end_time == at(10, 30)


async def test_a_restart_keeps_the_time_to_give_it_back(room):
    await room.service.to_laptop(30)
    again = room.make()
    assert again.camera_away and again.state()["until"] == at(10, 0).isoformat()


async def test_a_restart_after_the_end_gives_the_meetup_back_at_once(room):
    await room.service.to_laptop(30)
    room.clock.now = at(10, 20)
    again = room.make()
    room.service = again
    await again.start()
    try:
        assert not again.camera_away and room.switch.moves == ["laptop", "room"]
    finally:
        await again.stop()


async def test_a_room_with_no_calendar_lends_the_meetup_with_no_end(tmp_path):
    room = Room(tmp_path, calendar=False)
    state = await room.service.to_laptop()
    assert state["holder"] == "laptop" and state["until"] is None and not state["made_booking"]
    await room.service.to_room()
    assert not room.service.camera_away


async def test_the_tvs_words_are_the_problem_and_the_switch_goes_on(tmp_path):
    room = Room(tmp_path, tv_words="Choose HDMI 2 on the TV with its Input button")
    state = await room.service.to_laptop(30)
    assert state["holder"] == "laptop" and state["problem"] == "Choose HDMI 2 on the TV with its Input button"


async def test_an_unavailable_switch_refuses_in_its_own_words(tmp_path):
    room = Room(tmp_path, switch=FakeUsbSwitch(available=False))
    with pytest.raises(LaptopError, match="busy"):
        await room.service.to_laptop(30)
    assert room.service.state()["available"] is False and "busy" in room.service.state()["problem"]


async def test_a_second_tap_while_switching_is_told_so(room):
    gate = asyncio.Event()
    moving = room.switch.move_to

    async def slow(holder):
        await gate.wait()
        return await moving(holder)
    room.switch.move_to = slow
    first = asyncio.ensure_future(room.service.to_laptop(30))
    for _ in range(100):
        if room.service.state()["switching"]:
            break
        await asyncio.sleep(0.01)
    assert room.service.state()["switching"]
    with pytest.raises(LaptopError, match="Switching"):
        await room.service.to_room()
    gate.set()
    await first


def test_the_words_for_join_are_the_specs():
    assert CAMERA_AWAY == "The camera is on the laptop: press Back to room first"
```

- [ ] **Step 3: Run them to see them fail**

Run: `.venv/bin/python -m pytest tests/unit/laptop/test_service.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'croom.laptop.service'`

- [ ] **Step 4: Implement** (`src/croom/laptop/service.py`)

```python
"""
Laptop mode (spec docs/superpowers/specs/2026-10-27-laptop-switch-design.md): the room's MeetUp lent to a laptop at the table
through the USB switch and given back, the TV following it, a booking holding the time, and Join refused while it is away.

Where the MeetUp is comes from the switch (is it on this Pi's USB?), looked at every WATCH_S. What Laptop mode adds to that (until
when, the booking it follows, whether it made that booking) is kept in laptop.json, so a restart keeps the time to give it back.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from croom.calendar.bookings import BookingError
from croom.control.settings import SettingsStore
from croom.core.service import Service
from croom.laptop.switch import LAPTOP, NO_ANSWER, ROOM, UsbSwitch
from croom.meeting.providers.base import MeetingState

logger = logging.getLogger(__name__)

TITLE = "Laptop"
SOURCE = "room page"
CAMERA_AWAY = "The camera is on the laptop: press Back to room first"
IN_A_MEETING = "The room is in a meeting: leave it first"
SWITCHING = "Switching…"
HOW_LONG = "Choose how long the laptop needs the room"
NOT_READY = "The switch is not ready"
TV_FAILED = "The TV did not switch: choose its input with the TV's Input button"
BUSY_STATES = (MeetingState.JOINING, MeetingState.IN_LOBBY, MeetingState.CONNECTED, MeetingState.LEAVING)


class LaptopError(Exception):
    """What the person at the table is told when Laptop mode cannot do what they asked; `status` is the route's answer."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class LaptopService(Service):
    WATCH_S = 2.0

    def __init__(self, switch, store: SettingsStore, calendar=None, meeting=None, devices=None, tv=None,
                 clock: Optional[Callable[[], datetime]] = None):
        super().__init__("laptop")
        self._switch, self._store = switch, store
        self._calendar, self._meeting, self._devices, self._tv = calendar, meeting, devices, tv
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._lock = asyncio.Lock()
        self._watch_task: Optional[asyncio.Task] = None
        self._problem: Optional[str] = None
        self._active, self._until, self._key, self._made = False, None, None, False
        self._load()

    @classmethod
    def from_config(cls, config, calendar=None, meeting=None, devices=None, tv=None, switch=None) -> "LaptopService":
        return cls(switch or UsbSwitch.from_config(config), SettingsStore(config.resolve_data_dir() / "laptop.json"),
                   calendar=calendar, meeting=meeting, devices=devices, tv=tv)

    # -- what is kept

    def _load(self) -> None:
        saved = self._store.get("laptop")
        if not isinstance(saved, dict) or not saved.get("active"):
            return
        try:
            until = datetime.fromisoformat(saved["until"]) if saved.get("until") else None
        except (TypeError, ValueError):
            until = None
        key = saved.get("booking")
        self._active, self._until = True, until
        self._key, self._made = (key if isinstance(key, str) else None), bool(saved.get("made_booking"))

    def _set(self, active: bool, until: Optional[datetime] = None, key: Optional[str] = None, made: bool = False) -> None:
        self._active, self._until, self._key, self._made = active, until, key, made
        data = {"active": active, "until": until.isoformat() if until else None, "booking": key, "made_booking": made}
        try:
            self._store.save("laptop", data)
        except (OSError, ValueError) as e:
            logger.warning(f"Could not keep Laptop mode in {self._store.path}: {e}")

    # -- what the page reads

    @property
    def bookings(self):
        return getattr(self._calendar, "bookings", None)

    @property
    def camera_away(self) -> bool:
        return self._active

    def state(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "available": self._switch.available,
            "holder": LAPTOP if self._active else ROOM,
            "until": self._until.isoformat() if self._until else None,
            "made_booking": self._made,
            "switching": self._lock.locked(),
            "problem": self._problem or (None if self._switch.available else self._switch.problem),
        }

    # -- the lifecycle

    async def start(self) -> None:
        self._switch.open()
        await self._tick()          # a restart in Laptop mode: an end that has passed gives the MeetUp back now
        self._watch_task = asyncio.create_task(self._watch())
        logger.info("Laptop mode ready" if self._switch.available else f"Laptop mode unavailable: {self._switch.problem}")

    async def stop(self) -> None:
        task, self._watch_task = self._watch_task, None
        if task is not None:
            task.cancel()
            await asyncio.wait({task})
        self._switch.close()

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self.WATCH_S)
            await self._tick()

    async def _tick(self) -> None:
        """One look: the MeetUp moved at the switch itself, the booking's end moved in Google, or the end has come."""
        if self._lock.locked():
            return
        try:
            holder = self._switch.holder()
            if holder == LAPTOP and not self._active:
                logger.info("The MeetUp left this Pi without Laptop mode asking: the laptop has it")
                self._set(True)
                await self._release()
            elif holder == ROOM and self._active:
                logger.info("The MeetUp came back to this Pi at the switch: Laptop mode ends")
                self._set(False)
                await self._reclaim()
            elif self._active:
                self._follow_booking()
                if self._until is not None and self._clock() >= self._until:
                    logger.info("The laptop's time is up: the MeetUp goes back to the room")
                    await self.to_room(by_hand=False)
        except LaptopError as e:
            self._problem = str(e)
        except Exception:  # noqa: BLE001 - the watch must go on
            logger.exception("Laptop mode's look at the switch failed")

    def _follow_booking(self) -> None:
        bookings = self.bookings
        if bookings is None or self._key is None:
            return
        event = bookings.find(self._key)
        if event is not None and event.end_time != self._until:
            logger.info(f"The laptop's booking now ends at {event.end_time:%H:%M}")
            self._set(True, event.end_time, self._key, self._made)

    # -- what the page asks

    async def to_laptop(self, minutes: Optional[int] = None) -> Dict[str, Any]:
        """Lend the MeetUp to the laptop: for the booking in progress, or for `minutes` booked now as "Laptop" (BookingError when
        that time is taken, before anything is pressed), or with no end in a room with no calendar to book in."""
        if not self._switch.available:
            raise LaptopError(self._switch.problem or NOT_READY)
        if self._meeting is not None and getattr(self._meeting, "state", None) in BUSY_STATES:
            raise LaptopError(IN_A_MEETING)
        if self._lock.locked():
            raise LaptopError(SWITCHING)
        async with self._lock:
            if self._active:
                return self.state()
            bookings = self.bookings
            ongoing = bookings.in_progress() if bookings is not None else None
            if ongoing is not None:
                until, key, made = ongoing.end_time, ongoing.booking_id or ongoing.id, False
            elif bookings is not None:
                if not isinstance(minutes, int) or isinstance(minutes, bool):
                    raise LaptopError(HOW_LONG, 400)
                booking = await bookings.start_now(minutes, TITLE, SOURCE)
                until, key, made = booking.end, booking.id, True
            else:
                until, key, made = None, None, False
            self._problem = None
            await self._release()
            if not await self._switch.move_to(LAPTOP):
                if made:
                    await self._end_booking(key)
                await self._reclaim()
                self._problem = NO_ANSWER
                raise LaptopError(NO_ANSWER)
            self._set(True, until, key, made)
            self._problem = await self._show("laptop")
            logger.info(f"The MeetUp is on the laptop{' until ' + format(until, '%H:%M') if until else ''}")
            return self.state()

    async def to_room(self, by_hand: bool = True) -> Dict[str, Any]:
        """Give the MeetUp back to the room. By hand, a booking Laptop mode made is ended now; at its end it has run its time."""
        if self._lock.locked():
            raise LaptopError(SWITCHING)
        async with self._lock:
            if not self._switch.available:
                raise LaptopError(self._switch.problem or NOT_READY)
            if not await self._switch.move_to(ROOM):
                self._problem = NO_ANSWER
                raise LaptopError(NO_ANSWER)
            key, made = self._key, self._made
            self._set(False)
            await self._reclaim()
            if by_hand and made and key:
                await self._end_booking(key)
            logger.info("The MeetUp is back on the room")
            return self.state()

    # -- the others

    async def _release(self) -> None:
        if self._devices is not None:
            await self._devices.release()

    async def _reclaim(self) -> None:
        if self._devices is not None:
            await self._devices.reclaim()
        self._problem = await self._show("pi")

    async def _show(self, which: str) -> Optional[str]:
        if self._tv is None:
            return None
        try:
            return await self._tv.show_input(which)
        except Exception as e:  # noqa: BLE001 - the TV never stops the switch
            logger.warning(f"The TV did not switch to the {which} input: {type(e).__name__}: {e}")
            return TV_FAILED

    async def _end_booking(self, key: Optional[str]) -> None:
        bookings = self.bookings
        if bookings is None or key is None:
            return
        try:
            await bookings.end(key)
        except BookingError as e:
            logger.info(f"The laptop's booking was left as it is: {e}")
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/laptop -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/croom/laptop/service.py tests/unit/laptop
git commit -m "feat(laptop): Laptop mode lends the MeetUp, books the time and gives it back"
```

---

### Task 7: The control API

**Files:**
- Modify: `src/croom/control/service.py`
- Create: `tests/unit/control/test_laptop_api.py`
- Modify: `tests/unit/control/test_service.py` (`make_service` gains `laptop=None`)

**Interfaces:**
- Consumes: `LaptopService.to_laptop`, `to_room`, `state`, `camera_away`, `LaptopError`, `CAMERA_AWAY` (Task 6); `BookingError` (existing).
- Produces: `ControlService(…, laptop=None)`, `ControlService.from_config(…, laptop=None)`; `POST /api/laptop` answering `{"laptop": state}` (200) or `{"error": words}` (400/409/503); `/api/status` key `laptop`; Join answering 409 `CAMERA_AWAY`; `/api/tv/select` passing `laptop_input`.

- [ ] **Step 1: Give `make_service` the laptop** (`tests/unit/control/test_service.py`, near line 597)

```python
def make_service(meeting=None, calendar=None, devices=None, tv=None, discover=None, mouse=None, scores=None, laptop=None, **config):
    settings = {"host": "127.0.0.1", "port": 0, "room_name": "Lab", "room_location": "2nd floor"}
    settings.update(config)
    return ControlService(config=settings, meeting=meeting, calendar=calendar, devices=devices, tv=tv, discover=discover, mouse=mouse,
                          scores=scores, laptop=laptop)
```

- [ ] **Step 2: Write the failing tests** (`tests/unit/control/test_laptop_api.py`)

```python
"""The room page's Laptop route (spec 2026-10-27): both ways, what it refuses, the status's laptop block, and Join refused while the
laptop has the MeetUp."""
from croom.calendar.bookings import SlotTaken
from croom.laptop.service import CAMERA_AWAY, LaptopError
from tests.unit.control.test_service import StubMeeting, client_factory, make_service  # noqa: F401  (client_factory is a fixture)


class StubLaptop:
    def __init__(self):
        self.calls, self.away, self.fail = [], False, None

    @property
    def camera_away(self):
        return self.away

    def state(self):
        return {"enabled": True, "available": True, "holder": "laptop" if self.away else "room", "until": None,
                "made_booking": False, "switching": False, "problem": None}

    async def to_laptop(self, minutes=None):
        self.calls.append(("laptop", minutes))
        if self.fail is not None:
            raise self.fail
        self.away = True
        return self.state()

    async def to_room(self, by_hand=True):
        self.calls.append(("room", by_hand))
        self.away = False
        return self.state()


async def test_laptop_and_back(client_factory):  # noqa: F811
    laptop = StubLaptop()
    client = await client_factory(make_service(meeting=StubMeeting(), laptop=laptop))
    response = await client.post("/api/laptop", json={"to": "laptop", "minutes": 45})
    assert response.status == 200 and (await response.json())["laptop"]["holder"] == "laptop"
    response = await client.post("/api/laptop", json={"to": "room"})
    assert response.status == 200 and (await response.json())["laptop"]["holder"] == "room"
    assert laptop.calls == [("laptop", 45), ("room", True)]


async def test_laptop_with_no_minutes_is_asked_as_such(client_factory):  # noqa: F811
    laptop = StubLaptop()
    client = await client_factory(make_service(laptop=laptop))
    assert (await client.post("/api/laptop", json={"to": "laptop"})).status == 200
    assert laptop.calls == [("laptop", None)]


async def test_what_the_route_refuses(client_factory):  # noqa: F811
    laptop = StubLaptop()
    client = await client_factory(make_service(laptop=laptop))
    assert (await client.post("/api/laptop", json={"to": "somewhere"})).status == 400
    assert (await client.post("/api/laptop", json={"to": "laptop", "minutes": "45"})).status == 400
    assert (await client.post("/api/laptop", json={"to": "laptop", "minutes": True})).status == 400
    assert (await client.post("/api/laptop", data="to=laptop")).status == 415
    laptop.fail = LaptopError("The switch did not answer: press the button on the switch box")
    response = await client.post("/api/laptop", json={"to": "laptop", "minutes": 30})
    assert response.status == 409 and "switch box" in (await response.json())["error"]
    laptop.fail = SlotTaken("That time is already booked")
    assert (await client.post("/api/laptop", json={"to": "laptop", "minutes": 30})).status == 409
    assert laptop.calls == [("laptop", 30), ("laptop", 30)]


async def test_a_room_without_the_switch_has_no_laptop_route_and_says_so_in_the_status(client_factory):  # noqa: F811
    client = await client_factory(make_service())
    assert (await client.post("/api/laptop", json={"to": "laptop"})).status == 503
    assert (await (await client.get("/api/status")).json())["laptop"] == {"enabled": False}


async def test_the_status_carries_laptop_mode(client_factory):  # noqa: F811
    laptop = StubLaptop()
    laptop.away = True
    client = await client_factory(make_service(laptop=laptop))
    assert (await (await client.get("/api/status")).json())["laptop"]["holder"] == "laptop"


async def test_join_is_refused_while_the_laptop_has_the_meetup(client_factory):  # noqa: F811
    laptop, meeting = StubLaptop(), StubMeeting()
    laptop.away = True
    client = await client_factory(make_service(meeting=meeting, laptop=laptop))
    response = await client.post("/api/meeting/join", json={"url": "https://zoom.us/j/98765432100"})
    assert response.status == 409 and (await response.json())["error"] == CAMERA_AWAY
    assert meeting.joins == []
```

In `tests/unit/control/test_tv_selection.py`, `test_selected_tv_settings_can_be_updated_without_changing_host` (line 52) gains, right after its `assert (await response.json())["tv"]["pi_input"] == "HDMI_2"`:

```python
        response = await client.post("/api/tv/settings", json={"laptop_input": "HDMI_3"})
        assert response.status == 200 and (await response.json())["tv"]["laptop_input"] == "HDMI_3"
        response = await client.post("/api/tv/settings", json={"laptop_input": "HDMI_2"})       # the room Pi's input
        assert response.status == 409
```

- [ ] **Step 3: Run them to see them fail**

Run: `.venv/bin/python -m pytest tests/unit/control/test_laptop_api.py -q`
Expected: FAIL with `TypeError: ControlService.__init__() got an unexpected keyword argument 'laptop'`

- [ ] **Step 4: Implement** (`src/croom/control/service.py`)

Imports, after `from croom.devices.errors import …`:

```python
from croom.laptop.service import CAMERA_AWAY, LaptopError
```

`__init__` gains `laptop=None` after `scores=None`, and stores `self._laptop = laptop` after `self._scores_failure`. `from_config` gains `laptop=None` and passes `laptop=laptop`.

In `create_app`, after the `/api/tv/discover` route:

```python
        app.router.add_post("/api/laptop", self._handle_laptop)
```

In `_status`, after `"tv": self._tv_status(),`:

```python
            "laptop": self._laptop_status(),
```

A new section after the TV handlers (before `_handle_tv_discover` is fine, or after `_handle_tv_settings`):

```python
    # ------------------------------------------------------------------
    # Laptop mode (spec 2026-10-27): the room's MeetUp lent to a laptop at the table through the USB switch
    # ------------------------------------------------------------------

    NO_LAPTOP = "This room has no laptop switch"

    def _laptop_status(self) -> Dict[str, Any]:
        """Laptop mode's block of the status; {"enabled": False} in a room without the switch. The status must always answer."""
        if self._laptop is None:
            return {"enabled": False}
        try:
            return self._laptop.state()
        except Exception as e:  # noqa: BLE001 - the status must always answer
            message = f"Could not read Laptop mode: {type(e).__name__}: {e}"
            if message != self._read_failures.get("laptop"):
                self._read_failures["laptop"] = message
                logger.warning(message, exc_info=True)
            return {"enabled": False}

    async def _handle_laptop(self, request: web.Request) -> web.Response:
        """{"to": "laptop", "minutes"?: n} or {"to": "room"}, answered with Laptop mode's state, or 409 in words."""
        rejection = self._require_json(request)
        if rejection is not None:
            return rejection
        if self._laptop is None:
            return self._error_response(self.NO_LAPTOP, 503)
        data = await self._read_object(request)
        if data is None:
            return self._error_response("Send a JSON object", 400)
        to = data.get("to")
        try:
            if to == "laptop":
                minutes = data.get("minutes")
                if minutes is not None and (not isinstance(minutes, int) or isinstance(minutes, bool)):
                    return self._error_response("minutes is a whole number", 400)
                state = await self._laptop.to_laptop(minutes)
            elif to == "room":
                state = await self._laptop.to_room()
            else:
                return self._error_response('Send {"to": "laptop"} or {"to": "room"}', 400)
        except (LaptopError, BookingError) as e:
            return self._error_response(str(e), e.status)
        return web.json_response({"laptop": state})
```

In `_handle_join`, just before `if self._meeting_state() in IN_PROGRESS_STATES:`:

```python
        if self._laptop is not None and self._laptop.camera_away:
            return self._error_response(CAMERA_AWAY, 409)
```

In `_handle_tv_select`, `for name in ("mac", "pi_input"):` becomes `for name in ("mac", "pi_input", "laptop_input"):`.

- [ ] **Step 5: Run the control tests**

Run: `.venv/bin/python -m pytest tests/unit/control/test_laptop_api.py tests/unit/control/test_service.py tests/unit/control/test_tv_selection.py tests/unit/control/test_bookings_api.py -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/croom/control/service.py tests/unit/control
git commit -m "feat(control): the Laptop route, its status, and Join refused while the laptop has the MeetUp"
```

---

### Task 8: The agent, the dependency and `croom --check-laptop`

**Files:**
- Modify: `src/croom/core/agent.py`, `pyproject.toml`
- Create: `src/croom/laptop/check.py`, `tests/unit/laptop/test_check.py`
- Test: `tests/unit/core/test_agent.py`

**Interfaces:**
- Consumes: `LaptopService.from_config` (Task 6), `UsbSwitch` (Task 2), `ControlService(…, laptop=)` (Task 7).
- Produces: the `laptop` service registered when `config.laptop.enabled`, with dependencies `["meeting", "calendar", "devices"]` plus `"tv"` when there is one, handed to the control page (which then depends on it); `check_laptop(config, out=sys.stdout, switch=None, move=False) -> int`; CLI `--check-laptop` and `--switch`.

- [ ] **Step 1: Write the failing tests**

Append to `TestServiceRegistration` in `tests/unit/core/test_agent.py`:

```python
    def test_laptop_mode_is_registered_when_enabled_and_handed_to_the_control_page(self, tmp_path, no_hardware):
        config = make_config(tmp_path, control=True)
        config.laptop.enabled = True
        agent = make_agent(config)
        agent._initialize_services()
        laptop = agent.service_manager.get_service("laptop")
        control = agent.service_manager.get_service("control")
        assert laptop is not None and control._laptop is laptop
        assert laptop._devices is agent.service_manager.get_service("devices")
        order = agent.service_manager._start_order
        assert order.index("devices") < order.index("laptop") < order.index("control")
        assert order.index("tv") < order.index("laptop")

    def test_laptop_mode_is_absent_when_not_enabled(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path, control=True))
        agent._initialize_services()
        assert agent.service_manager.get_service("laptop") is None
        assert agent.service_manager.get_service("control")._laptop is None
```

`tests/unit/laptop/test_check.py`:

```python
"""`croom --check-laptop`: the pin, where the MeetUp is, and with --switch one move the other way."""
import io

from croom.core.config import Config
from croom.laptop.check import check_laptop
from tests.unit.laptop.fakes import FakeUsbSwitch


class CheckedSwitch(FakeUsbSwitch):
    chip = "/dev/gpiochip4"


async def test_the_check_says_where_the_meetup_is_and_presses_nothing():
    out, switch = io.StringIO(), CheckedSwitch()
    assert await check_laptop(Config(), out, switch=switch) == 0
    text = out.getvalue()
    assert "GPIO17 (header pin 11)" in text and "/dev/gpiochip4" in text and "on this Pi" in text
    assert "laptop.enabled is false" in text and switch.moves == [] and switch.closed


async def test_the_check_with_switch_moves_it_the_other_way():
    out, switch = io.StringIO(), CheckedSwitch()
    assert await check_laptop(Config(), out, switch=switch, move=True) == 0
    assert switch.moves == ["laptop"] and "now on the laptop side" in out.getvalue()


async def test_a_switch_that_does_not_move_fails_the_check():
    out, switch = io.StringIO(), CheckedSwitch()
    switch.answers = False
    assert await check_laptop(Config(), out, switch=switch, move=True) == 1
    assert "did not answer" in out.getvalue()


async def test_a_pin_that_cannot_be_had_fails_the_check_and_says_why():
    out = io.StringIO()
    assert await check_laptop(Config(), out, switch=CheckedSwitch(available=False)) == 1
    assert "busy" in out.getvalue() and "systemctl stop croom" in out.getvalue()
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/python -m pytest tests/unit/laptop/test_check.py tests/unit/core/test_agent.py -q -k "laptop or check"`
Expected: FAIL (`No module named 'croom.laptop.check'`; `laptop` is None)

- [ ] **Step 3: Implement**

`pyproject.toml` `dependencies`, after the `evdev` line:

```toml
    "gpiod>=2.1; sys_platform == 'linux'",
```

`src/croom/laptop/check.py`:

```python
"""
`croom --check-laptop`: the laptop switch's pin, the GPIO chip it is on, and where the MeetUp is now; with --switch, one move the
other way and how long it took (spec 2026-10-27, section 7). The agent holds the pin while it runs, so the check is for a room
whose croom is stopped. Exit 0 when the pin could be had (and, with --switch, the MeetUp moved), 1 otherwise.
"""

import sys
import time
from typing import Optional, TextIO

from croom.core.config import Config
from croom.laptop.switch import LAPTOP, ROOM, UsbSwitch

# The 40-pin header's pin for a BCM GPIO number, for the words: the wiring is done by the header's numbers.
HEADER_PIN = {2: 3, 3: 5, 4: 7, 5: 29, 6: 31, 7: 26, 8: 24, 9: 21, 10: 19, 11: 23, 12: 32, 13: 33, 14: 8, 15: 10, 16: 36, 17: 11,
              18: 12, 19: 35, 20: 38, 21: 40, 22: 15, 23: 16, 24: 18, 25: 22, 26: 37, 27: 13}


async def check_laptop(config: Config, out: TextIO = sys.stdout, switch: Optional[UsbSwitch] = None, move: bool = False) -> int:
    laptop = config.laptop
    header = HEADER_PIN.get(laptop.gpio_pin)
    print(f"Pin: GPIO{laptop.gpio_pin}" + (f" (header pin {header})" if header else "") +
          f", {'active low' if laptop.active_low else 'active high'}, held {laptop.press_ms} ms", file=out)
    if not laptop.enabled:
        print("Laptop mode is off in the config (laptop.enabled is false): the room page has no Laptop button.", file=out)
    switch = switch or UsbSwitch.from_config(config)
    if not switch.open():
        print(f"The switch cannot be pressed: {switch.problem}", file=out)
        print("If croom is running it holds the pin: sudo systemctl stop croom, then check again.", file=out)
        return 1
    try:
        print(f"GPIO chip: {switch.chip}", file=out)
        holder = switch.holder()
        print(f"The MeetUp ({laptop.usb_id}) is " + ("on this Pi." if holder == ROOM else "not on this Pi: the laptop side has it."), file=out)
        if not move:
            return 0
        target = LAPTOP if holder == ROOM else ROOM
        started = time.monotonic()
        if not await switch.move_to(target):
            print("The switch did not answer: check the relay's wiring to the remote button, and the relay's power.", file=out)
            return 1
        print(f"Pressed: the MeetUp is now on the {'laptop side' if target == LAPTOP else 'Pi'} "
              f"({time.monotonic() - started:.1f} s).", file=out)
        return 0
    finally:
        switch.close()
```

`src/croom/core/agent.py`, between the devices block and the control block:

```python
        # Laptop mode (spec 2026-10-27): the MeetUp lent to a laptop at the table through the USB switch
        laptop_service = None
        if self.config.laptop.enabled:
            try:
                from croom.laptop.service import LaptopService
                laptop_service = LaptopService.from_config(
                    self.config,
                    calendar=self.service_manager.get_service("calendar"),
                    meeting=self.service_manager.get_service("meeting"),
                    devices=self.service_manager.get_service("devices"),
                    tv=tv_service,
                )
                self.service_manager.register(
                    laptop_service, dependencies=["meeting", "calendar", "devices"] + (["tv"] if tv_service is not None else [])
                )
                logger.info("Laptop mode registered")
            except ImportError as e:
                logger.warning(f"Laptop mode not available: {e}")
```

In the control block, `ControlService.from_config(…)` gains `laptop=laptop_service,` and `dependencies` gains `+ (["laptop"] if laptop_service is not None else [])`.

In `main()`, after the `--check-tv` argument:

```python
    parser.add_argument(
        "--check-laptop",
        help="Check the laptop switch's pin and where the MeetUp is, and exit (stop croom first: it holds the pin)",
        action="store_true"
    )
    parser.add_argument(
        "--switch",
        help="With --check-laptop: press the switch once and report where the MeetUp went",
        action="store_true"
    )
```

and after the `if args.check_tv:` block:

```python
    if args.check_laptop:
        from croom.laptop.check import check_laptop
        raise SystemExit(asyncio.run(check_laptop(load_config(args.config), move=args.switch)))
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/laptop tests/unit/core -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/croom/core/agent.py src/croom/laptop/check.py tests/unit/laptop/test_check.py tests/unit/core/test_agent.py
git commit -m "feat(laptop): register Laptop mode, depend on gpiod, and croom --check-laptop"
```

---

### Task 9: The room page

**Files:**
- Modify: `src/croom/control/static/icons.svg`, `index.html`, `app.js`, `booking.js`, `remote.js`, `style.css`
- Modify: `tests/unit/control/test_page.py` (`PageServer` gains `laptop=None`), `tests/unit/control/test_tv_selection_page.py` (the saved settings now carry `laptop_input`)
- Create: `tests/unit/control/test_laptop_page.py`

**Interfaces:**
- Consumes: `/api/status` `laptop` block and `POST /api/laptop` (Task 7); `/api/tv/settings` accepting `laptop_input` (Task 5); the `StubLaptop` of `tests/unit/control/test_laptop_api.py`.
- Produces: `#actions` button "Laptop" (id `laptop`), the duration list in mode `"laptop"` (`.bk[data-mode="laptop"]`, title "Use a laptop for", submit "Switch to laptop"), the Laptop screen on the idle status card (`body[data-laptop="on"]`) with a "Back to room" button, the `#tv-settings-laptop` select labelled "Laptop input".

- [ ] **Step 1: Let PageServer take a laptop** (`tests/unit/control/test_page.py`)

`PageServer.__init__` gains `laptop=None` (after `bookings=False`) and stores `self.laptop = laptop`; the `ControlService(...)` call in `_main` gains `laptop=self.laptop`.

- [ ] **Step 2: Write the failing page tests** (`tests/unit/control/test_laptop_page.py`)

```python
"""The room page's Laptop button and screen (spec 2026-10-27), in the browser, against a stub Laptop mode."""
import pytest

from tests.unit.control.test_laptop_api import StubLaptop
from tests.unit.control.test_page import PageServer, browser, fits, tv_page  # noqa: F401  (browser is a fixture)
from tests.unit.control.test_page import RemoteTv

playwright = pytest.importorskip("playwright.sync_api")


class TimedLaptop(StubLaptop):
    def __init__(self, until=None):
        super().__init__()
        self.until = until

    def state(self):
        state = super().state()
        state["until"] = self.until if self.away else None
        return state


def room_page(browser, server):  # noqa: F811
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
    page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
    return page


def test_laptop_in_a_room_with_no_calendar_switches_in_one_tap_and_back(browser):  # noqa: F811
    laptop = TimedLaptop()
    with PageServer(calendar_connected=False, laptop=laptop) as server:
        page = room_page(browser, server)
        page.click("#actions button:has-text('Laptop')")
        page.wait_for_function("document.body.dataset.laptop === 'on'", timeout=5000)
        assert laptop.calls == [("laptop", None)]
        assert page.locator("#headline").inner_text() == "The camera, microphones and speaker are on the laptop"
        assert "Logitech MeetUp" in page.locator("#detail").inner_text()
        assert page.locator(".link").is_hidden() and page.locator("#open-options").is_hidden()
        assert fits(page)
        page.click("#actions button:has-text('Back to room')")
        page.wait_for_function("document.body.dataset.laptop !== 'on'", timeout=5000)
        assert laptop.calls[-1] == ("room", True)
        assert page.locator(".link").is_visible()


def test_laptop_in_a_free_room_asks_how_long(browser):  # noqa: F811
    laptop = TimedLaptop()
    with PageServer(bookings=True, laptop=laptop) as server:
        page = room_page(browser, server)
        page.click("#actions button:has-text('Laptop')")
        page.wait_for_selector(".bk[data-mode='laptop']")
        assert page.locator(".bk-title").inner_text() == "Use a laptop for"
        assert page.locator(".bk-name").is_hidden()
        page.select_option(".bk-length", "45")
        page.click(".bk-submit")
        page.wait_for_function("document.body.dataset.laptop === 'on'", timeout=5000)
        assert laptop.calls == [("laptop", 45)]


def test_the_laptop_screen_says_until_when(browser):  # noqa: F811
    laptop = TimedLaptop(until="2026-10-27T10:30:00-05:00")
    laptop.away = True
    with PageServer(calendar_connected=False, laptop=laptop) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.laptop === 'on'", timeout=5000)
        assert page.locator("#headline").inner_text().startswith("The camera, microphones and speaker are on the laptop until ")


def test_a_refusal_is_said_on_the_card(browser):  # noqa: F811
    from croom.laptop.service import LaptopError
    laptop = TimedLaptop()
    laptop.fail = LaptopError("The switch did not answer: press the button on the switch box")
    with PageServer(calendar_connected=False, laptop=laptop) as server:
        page = room_page(browser, server)
        page.click("#actions button:has-text('Laptop')")
        page.wait_for_function("document.getElementById('message').textContent.includes('switch box')", timeout=5000)


def test_a_room_without_the_switch_has_no_laptop_button(browser):  # noqa: F811
    with PageServer(calendar_connected=False) as server:
        page = room_page(browser, server)
        assert page.locator("#actions button:has-text('Laptop')").count() == 0


def test_the_laptop_input_is_chosen_beside_the_pis(browser):  # noqa: F811
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        saved = []

        def save(route):
            saved.append(route.request.post_data_json)
            tv.shown.update(saved[-1])
            route.fulfill(json={"tv": tv.status()})

        page.route("**/api/tv/settings", save)
        page.get_by_role("button", name="Change TV", exact=True).click()
        page.locator("#tv-connection-settings summary").click()
        assert page.get_by_label("Laptop input").input_value() == ""
        page.get_by_label("Laptop input").select_option("HDMI_2")
        page.get_by_role("button", name="Save connection", exact=True).click()
        page.wait_for_function("document.getElementById('tv-settings-result').textContent.includes('Saved')")
        assert saved[-1]["laptop_input"] == "HDMI_2" and saved[-1]["pi_input"] == "HDMI_1"
```

In `tests/unit/control/test_tv_selection_page.py`, `test_connection_settings_save_mac_and_pi_input_without_pairing`'s expected body becomes `[{"mac": "AA:BB:CC:DD:EE:88", "pi_input": "HDMI_2", "laptop_input": ""}]`.

- [ ] **Step 3: Run them to see them fail**

Run: `.venv/bin/python -m pytest tests/unit/control/test_laptop_page.py -q`
Expected: FAIL (no Laptop button; no "Laptop input" label)

- [ ] **Step 4: Implement the page**

`icons.svg`, a new symbol beside `icon-monitor` (Lucide `laptop`, same licence as the file's others):

```html
<symbol id="icon-laptop" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 16V7a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v9m16 0H4m16 0 1.28 2.55a1 1 0 0 1-.9 1.45H3.62a1 1 0 0 1-.9-1.45L4 16"/></symbol>
```

`index.html`, in the TV connection form after the Room Pi input label:

```html
          <label>Laptop input<select id="tv-settings-laptop"><option value="">Not set</option><option value="HDMI_1">HDMI 1</option><option value="HDMI_2">HDMI 2</option><option value="HDMI_3">HDMI 3</option><option value="HDMI_4">HDMI 4</option></select></label>
```

`remote.js`, in `tvChoose()` after `select.value = input;`:

```js
    const laptop = el("tv-settings-laptop");
    const laptopInput = t.laptop_input || "";
    if (laptopInput && !Array.from(laptop.options).some((o) => o.value === laptopInput)) laptop.add(new Option(laptopInput, laptopInput));
    laptop.value = laptopInput;
```

and in `tvSaveSettings` the posted body becomes:

```js
        mac: el("tv-settings-mac").value.trim(), pi_input: el("tv-settings-input").value, laptop_input: el("tv-settings-laptop").value,
```

`booking.js`, in `open()`:
- after `const state = …`: `const laptop = state.mode === "laptop";   // Laptop mode (spec 2026-10-27): a length only, sent to /api/laptop`
- the title becomes `make("h2", "bk-title", laptop ? "Use a laptop for" : state.mode === "now" ? "Use the room now" : "Book the room")`
- the submit's words become `laptop ? "Switch to laptop" : state.mode === "now" ? "Start" : "Book"`
- after `nameLabel.append(name);`: `nameLabel.hidden = laptop;`
- `change.hidden = state.mode === "now";` becomes `change.hidden = state.mode !== "book";`
- in the submit handler, replace the lines from `const words = name.value.trim();` through the `try { finish(await call(...)); }` with:

```js
      const words = name.value.trim();
      if (!laptop && !words) { error.textContent = "Type your name or the meeting's name."; name.focus({ preventScroll: true }); return; }
      const body = laptop ? { to: "laptop", minutes: Number(length.value) } : { title: words, minutes: Number(length.value), source: options.source };
      if (state.mode === "book") body.start = state.chosen.start;
      state.busy = true;
      submit.disabled = true;
      error.textContent = "";
      try {
        const path = laptop ? "/api/laptop" : state.mode === "now" ? "/api/bookings/now" : "/api/bookings";
        const data = await call(path, { method: "POST", body: JSON.stringify(body) });
        if (laptop) close(true);            // the room page's Laptop screen says the rest
        else finish(data);
      } catch (err) {
```

(the `catch` and `finally` that follow stay as they are)
- the final `if (state.mode === "now") {` becomes `if (state.mode === "now" || laptop) {`, and inside it `name.focus(...)` becomes `if (!laptop) name.focus({ preventScroll: true });`.

`app.js`, after `bookingSpecs` and `changeBooking`, before `let bookingSteps`:

```js
  // Laptop mode (spec 2026-10-27): the room's MeetUp lent to a laptop at the table through the USB switch. The status's laptop block
  // says whether the room has the switch and who has the MeetUp; while the laptop has it, the idle card is the Laptop screen.
  const laptopOf = (s) => (s && s.laptop && s.laptop.enabled ? s.laptop : null);
  const laptopHasIt = (s) => Boolean(laptopOf(s) && laptopOf(s).holder === "laptop");
  let laptopBusy = false;

  async function switchLaptop(to) {
    if (laptopBusy) return;
    laptopBusy = true;
    render();
    try {
      await api("/api/laptop", { method: "POST", body: JSON.stringify({ to: to }) });
      model.error = "";
    } catch (e) {
      model.error = e.message;
    } finally {
      laptopBusy = false;
      refreshStatus();
      refreshEvents();
    }
  }

  function laptopSpecs(cal, ongoing) {
    const l = laptopOf(model.status);
    if (!l || !l.available) return [];
    const b = bookingOf(cal);
    // A booking in progress is the laptop's time, and a room with no calendar has none to book: one tap. A free room asks how long.
    const oneTap = Boolean(ongoing) || !b;
    if (!oneTap && !b.now.minutes.length) return [];
    const busy = laptopBusy || l.switching;
    return [{ id: "laptop", label: busy ? "Switching…" : "Laptop", className: "quiet", icon: "laptop", disabled: busy,
              onClick: () => (oneTap ? switchLaptop("laptop") : openBooking("laptop")) }];
  }

  function renderLaptop(l) {
    document.body.dataset.state = "soon";
    el("kicker").textContent = "Laptop";
    el("headline").textContent = "The camera, microphones and speaker are on the laptop" + (l.until ? " until " + fmtTime(new Date(l.until)) : "");
    el("detail").textContent = l.problem || "Choose Logitech MeetUp as the camera, microphone and speaker in your meeting app";
    const busy = laptopBusy || l.switching;
    return [{ id: "laptop-back", label: busy ? "Switching…" : "Back to room", className: "primary", icon: "home", disabled: busy,
              onClick: () => switchLaptop("room") }];
  }
```

`openBooking(mode)` already passes `mode` through; for `"laptop"` its `onClose` refreshes as for the others.

In `renderIdle`, each `return bookingSpecs(cal, …)` and each `.concat(bookingSpecs(cal, …))` gains the Laptop button: change the five returns so that `bookingSpecs(cal, X)` reads `bookingSpecs(cal, X).concat(laptopSpecs(cal, X))` (X is `ongoing`, `current` or `null` as at each place).

In `render()`, the idle branch `specs = renderIdle(cal);` becomes:

```js
      specs = laptopHasIt(s) ? renderLaptop(s.laptop) : renderIdle(cal);
```

and just before `let specs = [];` add:

```js
    document.body.dataset.laptop = laptopHasIt(s) ? "on" : "off";
    if (laptopHasIt(s) && mode() === "idle" && !["idle", "tv"].includes(view())) applyView("idle", true);   // lent at the switch itself
    el("open-options").hidden = mode() !== "idle" || laptopHasIt(s);
```

(and remove the existing `el("open-options").hidden = mode() !== "idle";` line further down, which this replaces).

`style.css`, at the end:

```css
/* Laptop mode (spec 2026-10-27): the idle card is the Laptop screen; joining by link waits for the MeetUp to come back. */
body[data-laptop="on"] .link { display: none; }
```

- [ ] **Step 5: Run the page tests**

Run: `.venv/bin/python -m pytest tests/unit/control/test_laptop_page.py tests/unit/control/test_tv_selection_page.py tests/unit/control/test_booking_pages.py -q`
Expected: all PASS (skipped only when Playwright's Chromium is missing on the machine)

- [ ] **Step 6: Commit**

```bash
git add src/croom/control/static tests/unit/control
git commit -m "feat(room-page): the Laptop button, the Laptop screen and the laptop's TV input"
```

---

### Task 10: The whole suite, and the spec's record

**Files:**
- Modify: `docs/superpowers/specs/2026-10-27-laptop-switch-design.md` (a short "As built" note listing this plan's four added decisions)

- [ ] **Step 1: Run every test**

Run: `.venv/bin/python -m pytest -q`
Expected: no failure that `main` does not also have. To know `main`'s, run its suite in a worktree in the session's scratchpad (`$SCRATCH` below):

```bash
git worktree add "$SCRATCH/main-check" main
(cd "$SCRATCH/main-check" && PYTHONPATH=src /home/cpm_ssh/croom.to/.venv/bin/python -m pytest -q -p no:cacheprovider | tail -3)   # PYTHONPATH: main's code, not this branch's
git worktree remove "$SCRATCH/main-check"
```

- [ ] **Step 2: Add the as-built note to the spec**, under section 8:

```markdown
## 9. As built

The plan (`docs/superpowers/plans/2026-10-27-laptop-switch.md`) settled four things the design left open: the fake switch lives with
the tests; a room with no Google calendar lends the MeetUp with no end and no booking; the status's `holder` is `room` or
`laptop` only; and the Laptop screen is the idle card taken over (the join-by-link section and Options hidden, the TV Remote kept).
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-10-27-laptop-switch-design.md
git commit -m "docs(laptop): what the build settled"
```
