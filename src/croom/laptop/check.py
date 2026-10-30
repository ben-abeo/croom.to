"""
`croom --check-laptop`: the laptop switch's pin, the GPIO chip it is on, and where the MeetUp is now; with --switch, one move the
other way and how long it took (spec 2026-10-27, section 7). The agent holds the pin while it runs, so the command is for a room
whose croom is stopped. Exit 0 when the pin could be had (and, with --switch, the MeetUp moved), 1 otherwise.

Settings' Test the switch (spec 2026-10-29) runs the same check with the running Laptop service's switch (`held`): the agent's own pin,
which the check neither opens nor closes.
"""

import sys
import time
from typing import Optional, TextIO

from croom.core.config import Config
from croom.laptop.switch import LAPTOP, ROOM, UsbSwitch

# The 40-pin header's pin for a BCM GPIO number, for the words: the wiring is done by the header's numbers.
HEADER_PIN = {2: 3, 3: 5, 4: 7, 5: 29, 6: 31, 7: 26, 8: 24, 9: 21, 10: 19, 11: 23, 12: 32, 13: 33, 14: 8, 15: 10, 16: 36, 17: 11,
              18: 12, 19: 35, 20: 38, 21: 40, 22: 15, 23: 16, 24: 18, 25: 22, 26: 37, 27: 13}


async def check_laptop(config: Config, out: TextIO = sys.stdout, switch: Optional[UsbSwitch] = None, move: bool = False,
                       held: bool = False) -> int:
    """The check's lines go to `out`. `held`: `switch` is the running agent's, open already, and stays open."""
    laptop = config.laptop
    header = HEADER_PIN.get(laptop.gpio_pin)
    print(f"Pin: GPIO{laptop.gpio_pin}" + (f" (header pin {header})" if header else "") +
          f", {'active low' if laptop.active_low else 'active high'}, held {laptop.press_ms} ms", file=out)
    if not laptop.enabled:
        print("Laptop mode is off in the config (laptop.enabled is false): the room page has no Laptop button.", file=out)
    switch = switch or UsbSwitch.from_config(config)
    if not (switch.available if held else switch.open()):
        print(f"The switch cannot be pressed: {switch.problem}", file=out)
        if not held:
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
        if not held:
            switch.close()
