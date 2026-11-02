"""
Park the room Pi's pointer in the screen's corner as the desktop starts: labwc's autostart runs `python -m croom.tv.park_pointer`
(installer/install.sh), so that the desktop shown before the TV's browser is up has no pointer in the middle of the screen. The virtual
mouse is the touchpad's (PiMouse): made, given a moment for the desktop to take it up, moved, and removed again (the pointer stays where
it was put). A room Pi that cannot make it (no uinput, no permission) says why and leaves the pointer where it is.
"""

import sys
import time

from croom.tv.errors import TvError
from croom.tv.pointer import PiMouse

SETTLE_S = 1.0    # a virtual mouse just made is taken up by the desktop a moment later: a move before that is lost
LINGER_S = 0.2    # the move is read before the device goes


def main() -> int:
    mouse = PiMouse()
    try:
        mouse.open()
        time.sleep(SETTLE_S)
        mouse.park()
        time.sleep(LINGER_S)
    except TvError as e:
        print(f"The pointer was not parked: {e}", file=sys.stderr)
        return 1
    finally:
        mouse.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
