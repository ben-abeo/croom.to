"""
The camera's live picture: the tap in the TV's Chromium and CameraView (spec 2026-11-01 camera view, sections 4, 7 and 8). Only the risky
paths: nothing streams unattended (the last viewer, Laptop mode, the call's camera turned off), one tap on the TV's page across a page
change, and a slow viewer gets the newest frame, never a queue.
"""

import asyncio

import pytest
from aiohttp import web

playwright = pytest.importorskip("playwright.async_api")

from croom.meeting.camera_view import CameraView  # noqa: E402
from croom.meeting.display import TvDisplay  # noqa: E402
from croom.meeting.providers.base import MeetingInfo, MeetingState  # noqa: E402
from tests.unit.devices.test_service import service_for  # noqa: E402

PAGE = "<title>{0}</title><h1>{0}</h1>"
TRACK_ENDED = "window.__croomTap.lastTrack && window.__croomTap.lastTrack.readyState === 'ended'"
TRACK_LIVE = "window.__croomTap.lastTrack && window.__croomTap.lastTrack.readyState === 'live'"
# What tv.js's preview asks for (PREVIEW_SIZE), and a call's page opening and closing its own camera
PREVIEW = "navigator.mediaDevices.getUserMedia({video: {width: {ideal: 3840}, height: {ideal: 2160}, frameRate: {ideal: 30}}})" \
          ".then((s) => { window.own = s; return s.getVideoTracks()[0].getSettings().width; })"
OWN_CAMERA_ON = "navigator.mediaDevices.getUserMedia({video: true}).then((s) => { window.own = s; })"
OWN_CAMERA_OFF = "window.own.getTracks().forEach((t) => t.stop())"


class Site:
    """The control service's /tv and a call's page, on a loopback port (a secure context: getUserMedia works there)."""

    async def start(self):
        app = web.Application()
        async def page(request):
            return web.Response(text=PAGE.format(request.path.strip("/")), content_type="text/html")

        app.router.add_get("/tv", page)
        app.router.add_get("/call", page)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.base = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        return self

    async def stop(self):
        await self.runner.cleanup()


class CameraDisplay(TvDisplay):
    """The TV's browser with Chromium's built-in fake camera."""

    def browser_args(self):
        return super().browser_args() + ["--use-fake-device-for-media-stream"]


class FakeMeeting:
    def __init__(self):
        self.state = MeetingState.IDLE
        self.current_meeting = None
        self.callbacks = []

    def add_state_callback(self, callback):
        self.callbacks.append(callback)

    def call(self, camera_on=True):
        self.current_meeting = MeetingInfo(platform="google_meet", meeting_id="abc", meeting_url="https://meet.google.com/abc",
                                           is_camera_on=camera_on)
        self.state = MeetingState.CONNECTED
        for callback in self.callbacks:
            callback(self.state)


async def until(condition, timeout=8.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "gave up waiting"
        await asyncio.sleep(0.02)


@pytest.fixture
async def tv(tmp_path):
    site = await Site().start()
    meeting = FakeMeeting()
    devices, _, _ = service_for(tmp_path)
    view = CameraView(meeting=meeting, devices=devices)
    display = CameraDisplay(f"{site.base}/tv", headless=True, kiosk=False)
    display.camera_view = view
    try:
        await display.start()
    except Exception as e:  # noqa: BLE001 - browser missing on this machine
        await site.stop()
        pytest.skip(f"Chromium not available: {e}")
    page = await display.page()
    await page.wait_for_function("document.title === 'tv'", timeout=5000)
    try:
        yield site, display, page, view, meeting, devices
    finally:
        await view.close()
        await display.stop()
        await site.stop()


async def fresh_frame(view, timeout=8.0):
    """Wait for a frame newer than the one there is now."""
    before = view.latest()[0] if view.latest() else 0
    await until(lambda: view.latest() is not None and view.latest()[0] > before, timeout)
    return view.latest()[1]


async def test_frames_arrive_while_watched_and_the_last_viewer_leaving_ends_the_track(tv):
    site, display, page, view, meeting, devices = tv
    assert view.state() == "off" and view.latest() is None
    view.watch()
    view.watch()
    jpeg = await fresh_frame(view)
    assert jpeg[:2] == b"\xff\xd8" and view.state() == "on"
    view.unwatch()
    await fresh_frame(view)                                        # one viewer is still there
    view.unwatch()
    await page.wait_for_function(TRACK_ENDED, timeout=3000)
    assert view.state() == "off" and view.latest() is None

    still = await view.still()                                     # Frame people: the tap for as long as it takes ...
    assert still is not None and still[:2] == b"\xff\xd8" and len(still) > len(jpeg)
    await page.wait_for_function(TRACK_ENDED, timeout=3000)        # ... and no longer
    assert view.state() == "off"


async def test_laptop_mode_and_the_calls_camera_turned_off_stop_the_tap_and_it_comes_back(tv):
    site, display, page, view, meeting, devices = tv
    view.watch()
    await fresh_frame(view)

    await devices.release()                                        # Laptop mode: the MeetUp goes to the laptop
    await page.wait_for_function(TRACK_ENDED, timeout=2000)
    assert view.state() == "laptop"
    await devices.reclaim(home=False)
    await fresh_frame(view)
    assert view.state() == "on"

    meeting.call(camera_on=True)
    await page.evaluate(OWN_CAMERA_ON)                             # the call's own camera (a call's page opens it)
    await fresh_frame(view)
    meeting.current_meeting.is_camera_on = False                   # turned off from the touch Pi: no callback says so
    await page.wait_for_function(TRACK_ENDED, timeout=2000)
    assert view.state() == "camera_off"
    meeting.current_meeting.is_camera_on = True                    # turned on again: the picture comes back by itself
    await fresh_frame(view)
    await page.wait_for_function(TRACK_LIVE, timeout=2000)
    assert view.state() == "on"


async def test_a_page_change_runs_exactly_one_tap_on_the_new_page(tv):
    site, display, page, view, meeting, devices = tv
    view.watch()
    await fresh_frame(view)
    claimed = await display.claim()                                # the TV's page lent to a call ...
    await claimed.goto(f"{site.base}/call")
    await fresh_frame(view)                                        # ... and the picture comes from it
    await asyncio.sleep(1.0)                                       # time for a second start, if anything were to make one
    assert await claimed.evaluate("document.title") == "call"
    assert await claimed.evaluate("window.__croomTap.opened") == 1
    await display.show_idle()                                      # and handed back
    await claimed.wait_for_function("document.title === 'tv'", timeout=5000)
    await fresh_frame(view)
    await asyncio.sleep(1.0)
    assert await claimed.evaluate("window.__croomTap.opened") == 1
    view.unwatch()
    await claimed.wait_for_function(TRACK_ENDED, timeout=3000)


async def test_a_slow_viewer_gets_the_newest_frame_never_a_queue():
    view = CameraView()
    frames = view.frames()
    view._publish(b"1")
    assert await asyncio.wait_for(frames.__anext__(), 1) == b"1"
    for jpeg in (b"2", b"3", b"4"):                                # the viewer is busy while three frames come
        view._publish(jpeg)
    assert await asyncio.wait_for(frames.__anext__(), 1) == b"4"
    waiting = asyncio.ensure_future(frames.__anext__())            # nothing newer: it waits for the next one
    await asyncio.sleep(0.05)
    assert not waiting.done()
    view._publish(b"5")
    assert await asyncio.wait_for(waiting, 1) == b"5"
    await frames.aclose()


async def test_the_tap_opening_first_on_the_tv_page_leaves_the_preview_its_own_size(tv):
    """Chromium opens the camera once, at its first caller's size: the tap asks what the TV's preview asks, so the preview keeps it."""
    site, display, page, view, meeting, devices = tv
    view.watch()
    await fresh_frame(view)                                        # the tap opened the camera first
    assert await page.evaluate(PREVIEW) != 1280                    # the preview, opened second, is not held at the tap's 720p


async def test_on_a_calls_page_the_tap_waits_for_the_calls_camera_and_ends_with_it(tv):
    site, display, page, view, meeting, devices = tv
    claimed = await display.claim()
    await claimed.goto(f"{site.base}/call")
    meeting.call(camera_on=True)
    view.watch()
    await asyncio.sleep(1.0)
    assert await claimed.evaluate("window.__croomTap.opened") == 0  # the call has not opened its camera: the tap does not open it first
    assert view.state() == "starting"
    await claimed.evaluate(OWN_CAMERA_ON)
    await fresh_frame(view)                                        # ... and opens once the call has

    await claimed.evaluate(OWN_CAMERA_OFF)                         # the camera turned off at the TV: the call stops its own track
    await claimed.wait_for_function(TRACK_ENDED, timeout=1000)     # the tap's ends with it, while is_camera_on still says on
    assert meeting.current_meeting.is_camera_on and view.state() == "camera_off"
    await asyncio.sleep(0.6)
    assert await claimed.evaluate(TRACK_ENDED)                     # and nothing starts it again meanwhile

    await claimed.evaluate(OWN_CAMERA_ON)                          # turned on again at the TV
    await fresh_frame(view)
    assert view.state() == "on"
