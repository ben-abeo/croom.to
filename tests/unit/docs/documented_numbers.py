"""
The numbers the docs state, read from the code that has them. A guide that says a pairing waits 60 seconds is pinned to
TvService.PAIR_WAIT_S, and one that says the remote returns after ten minutes to the page script's own constant, so that changing the
code fails a docs test and the docs are never left stating what the code no longer does.
"""

import ast
import re
from pathlib import Path

from croom.tv.service import TvService

STATIC = Path(__file__).resolve().parents[3] / "src" / "croom" / "control" / "static"

# the TV service's waits, in seconds
PAIR_WAIT = int(TvService.PAIR_WAIT_S)          # a pairing waits this long for the person at the TV
PAIR_FINISH = int(TvService.PAIR_FINISH_S)      # and gives a TV that has accepted this much longer to finish
WAKE_WAIT = int(TvService.WAKE_TIMEOUT_S)       # a join waits this long for the TV to wake
WAKE_RESEND = int(TvService.WAKE_RESEND_S)      # the wake-up message goes out again this often meanwhile
REWAKE = int(TvService.REWAKE_AFTER_S)          # a TV that went off by itself is woken after this long
# a Turn on from the table: the wait, the command, the grace and the read-back (the sum the service's own comment on it makes)
TURN_ON = int(TvService.WAKE_TIMEOUT_S + TvService.ASK_TIMEOUT_S + TvService.MANUAL_TURN_ON_GRACE_S + TvService.REREAD_TIMEOUT_S)

_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 10: "ten", 15: "fifteen", 20: "twenty", 30: "thirty"}


def words(number):
    """A whole number as the docs write it in words (a KeyError for one they have no word for: the doc is to be looked at)."""
    return _WORDS[number]


def spoken(seconds):
    """A wait as a person reads it: a minute, or so many seconds."""
    return "a minute" if seconds == 60 else f"{seconds} seconds"


def js_constant(filename, name):
    """The value of `const NAME = ...;` in a script of the room page: a number, a product of numbers (10 * 60 * 1000) or a list."""
    source = (STATIC / filename).read_text(encoding="utf-8")
    found = re.search(rf"\bconst {name} = ([^;]+);", source)
    assert found, f"{filename} has no const {name}"
    text = found.group(1).strip()
    if text.startswith("["):
        return ast.literal_eval(text)
    product = 1
    for factor in text.split("*"):
        product *= int(factor)
    return product


# the page's own waits and thresholds, as the docs state them
IDLE_RETURN_MIN = js_constant("app.js", "IDLE_RETURN_MS") // 60000        # the remote returns to the idle screen after this long
TV_RETURN_MIN = js_constant("app.js", "MEETING_RETURN_MS") // 60000       # ... and to the meeting screen after this long (the sub-screens' two minutes, which the TV view shares)
TOUCHPAD_IDLE_MIN = js_constant("touchpad.js", "TOUCHPAD_IDLE_MS") // 60000   # the full-screen touchpad puts itself away after this long
TAP_PX = js_constant("touchpad.js", "TAP_PX")                             # how far a held finger moves before it drags
RECONNECT_S = [milliseconds // 1000 for milliseconds in js_constant("touchpad.js", "RECONNECT_MS")]   # the pointer socket's waits


def reconnect_words():
    """The pointer socket's retries as the docs say them: after 1 second, then 2, then every 5 seconds."""
    first, second, then = RECONNECT_S
    return f"after {first} second, then {second}, then every {then} seconds"
