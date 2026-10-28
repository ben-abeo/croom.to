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
