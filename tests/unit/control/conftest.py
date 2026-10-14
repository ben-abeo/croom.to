import sys

import pytest


@pytest.fixture(autouse=True)
def no_real_mouse_is_made(monkeypatch):
    """A pointer socket makes the room Pi's virtual mouse when it opens, and a service that is not given a mouse has the real one, which
    makes its device with evdev: on a machine where evdev is installed and /dev/uinput can be written, a test would put a mouse on the
    desktop. With evdev made unimportable, such a test is told in words that the library is missing (as is any room without it). The
    tests that want the library give themselves a stand-in."""
    monkeypatch.setitem(sys.modules, "evdev", None)
