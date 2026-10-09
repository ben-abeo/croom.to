"""
The TV screensaver page in the venv's Chromium: four styles from the status
and bookings, switched in place when the room page picks another one
(spec 2026-10-07 TV, section 4.1).
"""

from types import SimpleNamespace

import pytest

from tests.unit.control.test_page import PageServer, browser, event  # noqa: F401 - fixture and helpers

playwright = pytest.importorskip("playwright.sync_api")


def open_tv(browser, server, width=1280, height=720):
    page = browser.new_page(viewport={"width": width, "height": height})
    page.goto(f"http://127.0.0.1:{server.port}/tv", wait_until="networkidle")
    page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
    return page


def set_style(page, server, style):
    page.request.post(f"http://127.0.0.1:{server.port}/api/screensaver", data={"style": style},
                      headers={"Content-Type": "application/json"})
    page.wait_for_function(f"document.body.dataset.style === '{style}'", timeout=5000)


def test_info_style_shows_name_status_next_booking_and_clock(browser):
    with PageServer(calendar_events=[event("e1", "Design review", 25)], room_name="Room 3") as server:
        page = open_tv(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        assert page.locator("body").get_attribute("data-style") == "info"
        assert page.locator("#room-name").inner_text() == "Room 3"
        assert page.locator("#headline").inner_text().startswith("Free until")
        assert "Design review" in page.locator("#detail").inner_text()
        assert page.locator("#clock").inner_text() != ""
        assert "Press Join on the controller" in page.locator("#hint").inner_text()
        assert page.locator("button").count() == 0  # the TV has nothing to press
        assert errors == []
        page.close()


def test_quiet_style_shows_only_name_and_status(browser):
    with PageServer(room_name="Room 3") as server:
        page = open_tv(browser, server)
        set_style(page, server, "quiet")
        assert page.locator("#room-name").is_visible() and page.locator("#headline").is_visible()
        assert page.locator("#detail").is_hidden() and page.locator("#clock").is_hidden() and page.locator("#hint").is_hidden()
        page.close()


def test_brand_style_shows_the_logo_and_nothing_live(browser):
    with PageServer() as server:
        page = open_tv(browser, server)
        set_style(page, server, "brand")
        assert page.locator("#brand-logo").is_visible()
        assert page.locator("#headline").is_hidden() and page.locator("#room-name").is_hidden()
        page.close()


def test_bounce_moves_the_logo_and_changes_colour_at_an_edge(browser):
    with PageServer() as server:
        page = open_tv(browser, server, width=600, height=400)   # small, so an edge comes quickly
        set_style(page, server, "bounce")
        logo = page.locator("#bounce-logo")
        assert logo.is_visible()
        first = logo.bounding_box()
        colour_before = page.evaluate("getComputedStyle(document.getElementById('bounce-logo')).color")
        page.wait_for_timeout(600)
        second = logo.bounding_box()
        assert (first["x"], first["y"]) != (second["x"], second["y"])
        page.wait_for_function(
            f"getComputedStyle(document.getElementById('bounce-logo')).color !== '{colour_before}'", timeout=8000)
        assert page.locator("#bounce-logo svg path").count() > 0   # the real logo, inlined so it can take the colour
        page.close()


def test_style_change_re_renders_without_a_reload(browser):
    with PageServer() as server:
        page = open_tv(browser, server)
        loaded_at = page.evaluate("window.__tvLoadedAt")
        set_style(page, server, "bounce")
        set_style(page, server, "info")
        assert page.evaluate("window.__tvLoadedAt") == loaded_at
        page.close()


def test_bounce_logo_fits_a_narrow_window_so_it_never_strobes(browser):
    with PageServer() as server:
        page = open_tv(browser, server, width=200, height=400)   # narrower than the logo's TV size
        set_style(page, server, "bounce")
        box = page.locator("#bounce-logo").bounding_box()
        assert box["width"] < 200
        page.close()


# --- the idle camera preview (spec 2026-10-08 sound and camera, section 4.7) ---

from tests.unit.control.test_service import StubCamera, StubDevices  # noqa: E402


@pytest.fixture(scope="module")
def camera_browser(browser):  # noqa: F811 - the fixture imported above
    """Chromium with its built-in fake camera, so getUserMedia yields a real stream.

    Launched through the plain browser's type: Playwright's sync API cannot be started a second time in the same
    thread while the module's `browser` fixture is still running it."""
    instance = browser.browser_type.launch(args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
    yield instance
    instance.close()


def test_preview_shows_the_camera_while_asked_for_and_the_screensaver_returns(camera_browser):
    camera = StubCamera()
    with PageServer(devices=StubDevices(camera=camera)) as server:
        context = camera_browser.new_context(permissions=["camera"], viewport={"width": 1280, "height": 720})
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{server.port}/tv", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.locator("#camera-preview").is_hidden()
        camera.preview_on = True
        page.wait_for_function("document.body.dataset.preview === 'on'", timeout=5000)
        page.wait_for_function("document.getElementById('camera-preview').videoWidth > 0", timeout=8000)
        assert page.locator("#camera-preview").is_visible() and page.locator("#headline").is_hidden()
        assert page.locator("#preview-caption").inner_text() == "Camera preview"
        camera.preview_on = False
        page.wait_for_function("document.body.dataset.preview !== 'on'", timeout=5000)
        assert page.locator("#camera-preview").is_hidden() and page.locator("#headline").is_visible()
        assert page.evaluate("document.getElementById('camera-preview').srcObject === null")
        context.close()


def test_preview_never_shows_during_a_meeting(camera_browser):
    camera = StubCamera()
    camera.preview_on = True
    with PageServer(devices=StubDevices(camera=camera)) as server:
        context = camera_browser.new_context(permissions=["camera"], viewport={"width": 1280, "height": 720})
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{server.port}/tv", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.preview === 'on'", timeout=5000)
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}',
                          headers={"Content-Type": "application/json"})
        page.wait_for_function("document.body.dataset.state === 'occupied'", timeout=8000)
        assert page.evaluate("document.body.dataset.preview") != "on"
        assert page.locator("#camera-preview").is_hidden()
        context.close()


# The camera is let go on every way out, and only one stream is ever open. A probe in front of getUserMedia counts
# the calls, records every stream it hands out, and can keep a call pending (hold) or make it fail. The page's
# interval timers run ten times faster in these tests, so a poll comes every 200 ms and the waits stay short.

CAMERA_PROBE = """
(() => {
  const real = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  const probe = (window.__camera = { calls: 0, streams: [], hold: false, fail: false, held: [] });
  navigator.mediaDevices.getUserMedia = (constraints) => {
    probe.calls += 1;
    if (probe.fail) return Promise.reject(new DOMException("No camera", "NotFoundError"));
    const open = () => real(constraints).then((stream) => { probe.streams.push(stream); return stream; });
    if (!probe.hold) return open();
    return new Promise((resolve, reject) => probe.held.push(() => open().then(resolve, reject)));
  };
  // Let the oldest pending call go. The answer is read in the same task, once the page has had its turn on the
  // promise and before any poll can run, so it shows what the page itself did with the stream.
  probe.release = async () => {
    await probe.held.shift()();
    for (let i = 0; i < 10; i++) await Promise.resolve();
    return { cleared: document.getElementById("camera-preview").srcObject === null,
             ended: probe.streams.every((s) => s.getTracks().every((t) => t.readyState === "ended")) };
  };
  const every = window.setInterval;
  window.setInterval = (fn, ms, ...args) => every(fn, ms / 10, ...args);
})();
"""
FRAMES = "document.getElementById('camera-preview').videoWidth > 0"
NO_STREAM = "document.getElementById('camera-preview').srcObject === null"
PREVIEW_OFF = "document.body.dataset.preview !== 'on'"


def tracks_are(state):
    """A page expression: the probe has handed out a stream, and every track of every stream is 'live' or 'ended'."""
    return ("window.__camera.streams.length > 0 && "
            f"window.__camera.streams.every((s) => s.getTracks().every((t) => t.readyState === '{state}'))")


class PolledCamera(StubCamera):
    """A StubCamera that counts how often the status asks for its state: once per poll of the TV page."""

    def __init__(self):
        super().__init__()
        self.polls = 0

    def state(self):
        self.polls += 1
        return super().state()


def wait_for_polls(page, camera, more):
    """Let the page poll the status `more` more times."""
    target = camera.polls + more
    for _ in range(200):
        if camera.polls >= target:
            return
        page.wait_for_timeout(50)
    pytest.fail("the page stopped polling the status")


@pytest.fixture
def probed_tv(camera_browser):
    """The idle TV page with the getUserMedia probe in front of Chromium's fake camera, and a polled stub camera."""
    camera = PolledCamera()
    with PageServer(devices=StubDevices(camera=camera)) as server:
        context = camera_browser.new_context(permissions=["camera"], viewport={"width": 1280, "height": 720})
        context.add_init_script(CAMERA_PROBE)
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{server.port}/tv")   # not networkidle: the page polls every 200 ms here
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        yield SimpleNamespace(page=page, camera=camera, server=server)
        context.close()


def test_switching_the_preview_off_releases_the_camera_and_a_new_preview_opens_afresh(probed_tv):
    page, camera = probed_tv.page, probed_tv.camera
    camera.preview_on = True
    page.wait_for_function(FRAMES, timeout=8000)
    assert page.evaluate(tracks_are("live"))
    camera.preview_on = False
    page.wait_for_function(PREVIEW_OFF, timeout=5000)
    page.wait_for_function(tracks_are("ended"), timeout=5000)
    assert page.evaluate(NO_STREAM)
    camera.preview_on = True
    page.wait_for_function("window.__camera.streams.length === 2", timeout=8000)
    page.wait_for_function(FRAMES, timeout=8000)
    assert page.evaluate("window.__camera.streams[0].getTracks().every((t) => t.readyState === 'ended')")
    assert page.evaluate("window.__camera.streams[1].getTracks().every((t) => t.readyState === 'live')")
    assert page.evaluate("window.__camera.calls") == 2


def test_a_meeting_starting_releases_the_camera(probed_tv):
    page, camera, server = probed_tv.page, probed_tv.camera, probed_tv.server
    camera.preview_on = True
    page.wait_for_function(FRAMES, timeout=8000)
    page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}',
                      headers={"Content-Type": "application/json"})
    page.wait_for_function("document.body.dataset.state === 'occupied'", timeout=5000)
    page.wait_for_function(tracks_are("ended"), timeout=5000)
    assert page.evaluate(NO_STREAM)
    wait_for_polls(page, camera, 3)   # the stub still says preview; the meeting keeps the camera shut all the same
    assert page.evaluate("window.__camera.calls") == 1


def test_the_page_hiding_releases_the_camera(probed_tv):
    page, camera = probed_tv.page, probed_tv.camera
    camera.preview_on = True
    page.wait_for_function(FRAMES, timeout=8000)
    # Read in the same task as the event: the preview is still asked for, so the next poll would open the camera again.
    released = page.evaluate("""() => {
        window.dispatchEvent(new Event('pagehide'));
        return { cleared: document.getElementById('camera-preview').srcObject === null,
                 ended: window.__camera.streams.every((s) => s.getTracks().every((t) => t.readyState === 'ended')) };
    }""")
    assert released == {"cleared": True, "ended": True}


def test_losing_the_control_service_releases_the_camera(probed_tv):
    page, camera = probed_tv.page, probed_tv.camera
    camera.preview_on = True
    page.wait_for_function(FRAMES, timeout=8000)
    page.route("**/api/status", lambda route: route.abort())   # the control service stops answering
    page.wait_for_function("document.body.dataset.state === 'offline'", timeout=5000)
    assert page.evaluate("document.body.dataset.preview") != "on"
    page.wait_for_function(tracks_are("ended"), timeout=5000)
    assert page.evaluate(NO_STREAM)


def test_polls_that_still_ask_for_the_preview_open_one_stream_only(probed_tv):
    page, camera = probed_tv.page, probed_tv.camera
    page.evaluate("window.__camera.hold = true")   # the first getUserMedia stays pending until the test lets it go
    camera.preview_on = True
    page.wait_for_function("window.__camera.calls === 1", timeout=5000)
    wait_for_polls(page, camera, 3)
    assert page.evaluate("window.__camera.calls") == 1   # polls while the camera is still opening ask for nothing
    page.evaluate("window.__camera.release()")
    page.wait_for_function(FRAMES, timeout=8000)
    wait_for_polls(page, camera, 3)
    assert page.evaluate("window.__camera.calls") == 1   # nor do polls while the stream is live
    assert page.evaluate("window.__camera.streams.length") == 1


def test_a_stream_that_opens_after_the_preview_ended_is_stopped(probed_tv):
    page, camera = probed_tv.page, probed_tv.camera
    page.evaluate("window.__camera.hold = true")
    camera.preview_on = True
    page.wait_for_function("window.__camera.calls === 1", timeout=5000)
    camera.preview_on = False
    page.wait_for_function(PREVIEW_OFF, timeout=5000)
    # The camera opens only now, too late: the page stops the stream as the call resolves, not at some later poll.
    assert page.evaluate("window.__camera.release()") == {"cleared": True, "ended": True}
    assert page.evaluate("window.__camera.calls") == 1


def test_a_preview_asked_for_again_while_the_camera_opens_keeps_that_one_stream(probed_tv):
    page, camera = probed_tv.page, probed_tv.camera
    page.evaluate("window.__camera.hold = true")
    camera.preview_on = True
    page.wait_for_function("window.__camera.calls === 1", timeout=5000)
    camera.preview_on = False   # the panel closes and opens again before the camera has opened
    page.wait_for_function(PREVIEW_OFF, timeout=5000)
    camera.preview_on = True
    page.wait_for_function("document.body.dataset.preview === 'on'", timeout=5000)
    wait_for_polls(page, camera, 3)
    page.evaluate("window.__camera.release()")
    page.wait_for_function(FRAMES, timeout=8000)
    assert page.evaluate(tracks_are("live"))
    assert page.evaluate("window.__camera.calls") == 1 and page.evaluate("window.__camera.streams.length") == 1


def test_a_failing_camera_is_named_for_as_long_as_the_preview_is_asked_for(probed_tv):
    page, camera = probed_tv.page, probed_tv.camera
    page.evaluate("window.__camera.fail = true")
    camera.preview_on = True
    page.wait_for_function("document.getElementById('preview-caption').textContent === 'Camera preview unavailable'", timeout=5000)
    assert page.locator("#camera-preview").is_visible() and page.locator("#preview-caption").is_visible()
    assert page.locator("#headline").is_hidden() and page.evaluate(NO_STREAM)
    camera.preview_on = False   # the panel closes, the camera is mended, the panel opens again
    page.wait_for_function(PREVIEW_OFF, timeout=5000)
    page.evaluate("window.__camera.fail = false; window.__camera.hold = true")
    camera.preview_on = True
    page.wait_for_function("window.__camera.held.length === 1", timeout=5000)
    assert page.locator("#preview-caption").inner_text() == "Camera preview"   # not the old complaint while it opens
    page.evaluate("window.__camera.release()")
    page.wait_for_function(FRAMES, timeout=8000)
    assert page.locator("#preview-caption").inner_text() == "Camera preview"
