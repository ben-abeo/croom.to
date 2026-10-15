"""Fixtures of the control tests, all of them automatic: evdev is kept out of every test (no real mouse is made on the machine they run on),
and the browser tests leave no page open for the tests after them."""

import sys

import pytest


@pytest.fixture(autouse=True)
def no_real_mouse_is_made(monkeypatch):
    """A pointer socket makes the room Pi's virtual mouse when it opens, and a service that is not given a mouse has the real one, which
    makes its device with evdev: on a machine where evdev is installed and /dev/uinput can be written, a test would put a mouse on the
    desktop. With evdev made unimportable, such a test is told in words that the library is missing (as is any room without it). The
    tests that want the library give themselves a stand-in."""
    monkeypatch.setitem(sys.modules, "evdev", None)


@pytest.fixture(autouse=True)
def no_page_outlives_its_test(request):
    """After every test that used the shared browser, close every page still open in it.

    The browser is one for the whole module, and each `browser.new_page()` makes a context of its own. A test that fails before its
    `page.close()` would leave a page polling a server that has gone, and any `page.route` handler it set, for every test after it.
    The tests are not changed for this: the ones that close their pages find nothing left to close.
    """
    yield
    if "browser" in request.fixturenames:
        for context in list(request.getfixturevalue("browser").contexts):
            context.close()
