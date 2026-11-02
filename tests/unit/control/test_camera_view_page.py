"""The touch Pi's camera screen with the live picture (camera view spec 2026-11-01, section 3), driven in the venv's Playwright Chromium.
Only the risky paths: a drag turns the camera against the finger and stops with it however the release comes (a lost one included: the
camera's watchdog is the last resort, not the plan), the 30 px dead zone sends nothing, the socket (which keeps the TV's tap running) is closed
when the screen is left, and a page that is not the allowed touch Pi opens none. The room is the page tests' own (a fake agent with a stub
camera), with a CameraView that counts watch() and unwatch() and an allowed touch Pi at the browser's own address."""

import json
import urllib.request

from croom.settings.checkin import Registry
from tests.unit.control.test_camera_view_api import WatchedView
from tests.unit.control.test_devices_api import HEADER, TOUCH, checkin_body
from tests.unit.control.test_page import PageServer, browser, open_camera_screen, open_page  # noqa: F401  (browser is a fixture)

# Remembers the sockets the page opens to the camera's picture (WebSocket is wrapped, nothing else changes).
RECORD_SOCKETS = """
(() => {
  window.__views = [];
  window.WebSocket = new Proxy(window.WebSocket, { construct(target, args) {
    const socket = new target(...args);
    if (String(args[0]).includes('/api/camera/view')) window.__views.push(socket);
    return socket;
  } });
})();
"""


def let_in(server, registry, allowed=True):
    """The browser's address checks in as the touch Pi, and (allowed) the room lets it in."""
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/devices/checkin", data=json.dumps(checkin_body()).encode(),
                                     headers=dict(HEADER, **{"Content-Type": "application/json"}))
    urllib.request.urlopen(request, timeout=5).read()
    if allowed:
        registry.allow(TOUCH)


class Room:
    """A page server with a CameraView and a touch Pi that is (or is not) allowed to see it."""

    def __init__(self, allowed=True):
        self.registry = Registry(None)
        self.server = PageServer(registry=self.registry)
        self.view = WatchedView()
        self.allowed = allowed

    def __enter__(self):
        self.server.__enter__()
        self.server.meeting.camera_view = self.view
        let_in(self.server, self.registry, self.allowed)
        return self

    def __exit__(self, *exc):
        self.server.__exit__(*exc)

    @property
    def moves(self):
        return self.server.devices.camera.moves

    def page(self, browser):  # noqa: F811
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.add_init_script(RECORD_SOCKETS)
        page.goto(f"http://127.0.0.1:{self.server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        return page


def wait_until(condition, seconds=5):
    import time
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


def drag(page, dx, dy, hold_s=0.0):
    """One finger down in the picture, moved by (dx, dy) and (hold_s later) lifted. Returns nothing; the caller looks at the moves."""
    box = page.locator("#picture").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx, y + dy, steps=4)
    if hold_s:
        page.wait_for_timeout(int(hold_s * 1000))
    return x, y


def test_a_drag_right_turns_the_camera_left_and_its_release_stops_it(browser):  # noqa: F811
    with Room() as room:
        page = room.page(browser)
        try:
            open_camera_screen(page)
            drag(page, 40, 0)
            assert wait_until(lambda: room.moves == [(-1, 0)]), room.moves
            page.mouse.up()
            assert wait_until(lambda: room.moves == [(-1, 0), (0, 0)]), room.moves
            page.wait_for_timeout(300)
            assert room.moves == [(-1, 0), (0, 0)]       # the stop is sent once (lostpointercapture comes after the lift)
        finally:
            page.close()


def test_a_drag_within_30_px_sends_nothing(browser):  # noqa: F811
    with Room() as room:
        page = room.page(browser)
        try:
            open_camera_screen(page)
            drag(page, 20, 15, hold_s=0.3)
            page.mouse.up()
            page.wait_for_timeout(300)
            assert room.moves == []
        finally:
            page.close()


def test_a_drag_whose_release_never_comes_stops_the_camera_on_pointercancel(browser):  # noqa: F811
    with Room() as room:
        page = room.page(browser)
        try:
            open_camera_screen(page)
            drag(page, 0, 40)
            assert wait_until(lambda: room.moves == [(0, 1)]), room.moves     # (a drag down: the camera tilts up)
            page.evaluate("document.getElementById('picture').dispatchEvent(new PointerEvent('pointercancel', {pointerId: 1, bubbles: true}))")
            assert wait_until(lambda: room.moves == [(0, 1), (0, 0)]), room.moves
            page.wait_for_timeout(1000)                                      # (nothing repeats after it: the arrow pad's repeat is 750 ms)
            assert room.moves == [(0, 1), (0, 0)]
            page.mouse.up()
        finally:
            page.close()


def test_leaving_the_camera_screen_closes_the_socket_and_lets_go_of_the_tap(browser):  # noqa: F811
    with Room() as room:
        page = room.page(browser)
        try:
            open_camera_screen(page)
            assert wait_until(lambda: room.view.watches == 1)
            assert page.evaluate("window.__views.length") == 1
            page.click("#back-from-camera")
            assert wait_until(lambda: room.view.unwatches == 1)
            page.wait_for_function("window.__views.length === 1 && window.__views[0].readyState === 3", timeout=3000)
            page.wait_for_timeout(500)
            assert room.view.watches == 1
        finally:
            page.close()


def test_a_page_that_is_not_the_allowed_touch_pi_opens_no_socket(browser):  # noqa: F811
    with Room(allowed=False) as room:
        page = room.page(browser)
        try:
            open_camera_screen(page)
            page.wait_for_timeout(800)
            assert page.evaluate("window.__views.length") == 0 and room.view.watches == 0
            assert page.is_visible("#arrow-pad") and not page.is_visible("#picture")      # today's screen
        finally:
            page.close()
