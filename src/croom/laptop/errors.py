"""
What Laptop mode says when it cannot do what was asked: its error and its words.

These live apart from the service because the control service imports them, and importing anything under `croom.control` runs
`croom/control/__init__.py`, which imports the control service. The Laptop service imports `croom.control.settings`, so were
these in `croom.laptop.service` the two would import each other. This module imports nothing from croom, and so never reaches
the control package.
"""

TITLE = "Laptop"
SOURCE = "room page"
CAMERA_AWAY = "The camera is on the laptop: press Back to room first"
IN_A_MEETING = "The room is in a meeting: leave it first"
SWITCHING = "Switching…"
HOW_LONG = "Choose how long the laptop needs the room"
NOT_READY = "The switch is not ready"
TV_FAILED = "The TV did not switch: choose its input with the TV's Input button"



def wrong_camera(found: str) -> str:
    """Laptop mode's words in a room whose camera (`found`, as lsusb shows it) is not the one laptop.usb_id names."""
    return f"This room's camera is {found}, not the one the Laptop switch watches: choose it in Settings › Laptop switch"


class LaptopError(Exception):
    """What the person at the table is told when Laptop mode cannot do what they asked; `status` is the route's answer."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status
