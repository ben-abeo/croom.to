"""
Browser tests for the room page, driven with the Playwright Chromium in the venv.

The control service runs with stub services on its own event loop in a
thread; the sync Playwright API drives a headless page against it. Skipped
when Playwright or its browser is not installed.
"""

import asyncio
import base64
import json
import math
import random
import re
import socket
import struct
import tempfile
import threading
import time
import urllib.request
import zlib
from datetime import datetime, time as time_of_day, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from croom.calendar.bookings import RoomBookings
from croom.calendar.providers.base import CalendarEvent, MeetingPlatform
from croom.control import service as control_service
from croom.control.service import SIGN_BACKGROUNDS, STATIC_DIR, ControlService, read_setting
from croom.control.settings import SettingsStore
from croom.devices.errors import Interrupted, NotReady
from croom.meeting.providers.base import MeetingState
from croom.meeting.providers.zoom_sdk import PASSCODE_ASKED as ZOOM_PASSCODE_ASKED
from croom.scores.espn import ScheduleUnavailable
from croom.scores.service import ScoresService
from croom.tv.errors import TvError
from tests.unit.control.test_service import (
    BOARDROOM, STUB_PEOPLE, StubCalendar, StubDevices, StubMeeting, StubMouse, StubSearch, StubTv, event, skip_if_day_ends_within,
)
from tests.unit.calendar.test_bookings import ROOM_CAL, FakeGoogle
from tests.unit.control.test_bookings_api import BookingCalendar
from tests.unit.scores.schedules import Feed, every_school, game, longest_names, schedule, texas_sample

playwright = pytest.importorskip("playwright.sync_api")


class PageServer:
    """ControlService with stubs, served on an ephemeral port from a background thread. The meeting it joins is a Zoom participant's (role
    "co-host" or "host" for the others) with the whole control model; controls=False is a meeting with none, as a platform that has no
    model yet would give."""

    def __init__(self, calendar_events=(), room_name="Lab", room_location="2nd floor", calendar_connected=True, port=0, devices=None,
                 role="participant", controls=True, tv=None, discover=None, mouse=None, room_company=None, schedules=None,
                 bookings=False):
        self.role = role
        self.controls = controls
        self.events = list(calendar_events)
        self.room_name = room_name
        self.room_location = room_location
        self.room_company = room_company   # None: the config says nothing, so the service's own default shows
        self.calendar_connected = calendar_connected
        self.port_arg = port
        self.schedules = schedules      # team id -> the schedule ESPN would answer; None leaves the room without a scoreboard
        self.with_bookings = bookings   # the room books in its calendar (spec 2026-10-25): self.rooms keeps them, self.google is a fake Google
        self.rooms = None
        self.google = None
        self.feed = None                # the scoreboard's fake feed, once the server runs: what it was asked, and `down` to fail it
        self.devices = devices if devices is not None else StubDevices()
        self.tv = tv                                                       # the room's TV service: none is a room with no TV set up
        self.discover = discover if discover is not None else StubSearch()   # Find TVs never puts a search on this network: a test that wants TVs found gives it some
        self.mouse = mouse                                                 # the room Pi's virtual mouse: none is the real one, which the conftest keeps from being made
        self._settings_dir = tempfile.TemporaryDirectory()   # the screensaver choice must not land in the repo
        self.meeting = None
        self.port = None
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    async def _main(self):
        self.meeting = StubMeeting(role=self.role, controls=self.controls)
        config = {"host": "127.0.0.1", "port": self.port_arg, "room_name": self.room_name, "room_location": self.room_location,
                  "settings_file": str(Path(self._settings_dir.name) / "control-settings.json")}
        if self.room_company is not None:
            config["room_company"] = self.room_company
        store = SettingsStore(config["settings_file"])
        scores = None
        if self.schedules is not None:
            # The agent's wiring, over a fake feed: the schools come from the page's own store, and a school is read within a moment of
            # being turned on and again every fifth of a second, so that what a test changes in the feed shows at the sign's next poll.
            self.feed = Feed(self.schedules)
            scores = ScoresService(self.feed, lambda: read_setting(store, "sign_scores"), interval_s=0.2, retry_s=0.2, check_s=0.05)
            await scores.start()
        calendar = StubCalendar(events=self.events, connected=self.calendar_connected)
        if self.with_bookings:
            # Booking hours that cover the whole day, so that a test finds open times whatever the time it runs at.
            self.rooms = RoomBookings(Path(self._settings_dir.name) / "bookings.json", Path(self._settings_dir.name) / "calendar-cache.json",
                                      self.room_name, booking_start="00:00", booking_end="23:45")
            self.google = FakeGoogle()
            self.rooms.attach(self.google, ROOM_CAL)
            calendar = BookingCalendar(self.rooms, self.events)
        service = ControlService(
            config=config,
            meeting=self.meeting, calendar=calendar,
            devices=self.devices, store=store, scores=scores, tv=self.tv, discover=self.discover, mouse=self.mouse,
        )
        await service.start()
        self.port = service.bound_port
        self._stop = asyncio.Event()
        self._ready.set()
        await self._stop.wait()
        await service.stop()
        if scores is not None:
            await scores.stop()

    def release(self, gate):
        """Open an asyncio.Event that a stub is waiting for. The stub waits on this server's loop, so the event is set there."""
        self._loop.call_soon_threadsafe(gate.set)

    def __enter__(self):
        self._thread.start()
        assert self._ready.wait(5), "page server did not start"
        return self

    def stop(self):
        """The agent goes away: its pointer sockets are closed, and nothing listens on its port any more."""
        if self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._stop.set)
            self._thread.join(5)

    def __exit__(self, *exc):
        self.stop()
        self._settings_dir.cleanup()


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        try:
            instance = p.chromium.launch()
        except Exception as e:  # noqa: BLE001 - browser missing on this machine
            pytest.skip(f"Chromium not available: {e}")
        yield instance
        instance.close()


LEFT_OPEN = []     # the contexts the next test's look at the browser must find closed


def test_a_sign_page_a_test_leaves_open_is_closed_when_that_test_ends(browser):
    """No page outlives its test (the whole-branch review): the browser is one for the whole module and every `new_page()` makes a context of its own,
    so a test that fails before its `page.close()` would leave a page polling a server that has gone, and its route handler, to every test after it.
    This one opens a page, hangs a route handler on it and leaves it open on purpose; the test after it looks."""
    page = browser.new_page()
    page.route("**/*", lambda route: route.abort())
    LEFT_OPEN.append(page.context)
    assert page.context in browser.contexts


def test_the_next_sign_test_finds_the_browser_with_no_page_open(browser):
    if not LEFT_OPEN:
        pytest.skip("the test before it leaves a page open, and this one looks for it")
    assert LEFT_OPEN[0] not in browser.contexts and browser.contexts == []


def open_page(browser, server):
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
    page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
    return page


def join_by_id(page):
    page.fill("#link-input", "98765432100")
    page.click("#link-form button[type=submit]")   # the keyboard toggle sits before Join
    page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)


def open_camera_screen(page):
    """Idle mode: Options, then Camera framing. Waits until the camera view is up."""
    page.click("#open-options")
    page.click("#open-camera-setup")
    page.wait_for_function("document.body.dataset.view === 'camera'", timeout=5000)


def open_screensaver_screen(page):
    page.click("#open-options")
    page.click("#open-screensaver")
    page.wait_for_function("document.body.dataset.view === 'screensaver'", timeout=5000)


def fits(page):
    """True when nothing needs to scroll but the bookings box: not the page, and not the screen under the top bar."""
    return page.evaluate("""() => { const d = document.documentElement, m = document.querySelector('main');
        return d.scrollHeight <= d.clientHeight && m.scrollHeight <= m.clientHeight; }""")


def texts(page, scope):
    """What each button inside the scope says, in order."""
    return [b.inner_text().strip() for b in page.locator(f"{scope} .button").all()]


def in_one_row(page, scope):
    """True when no button inside the scope has wrapped to a second line: they all begin at one height."""
    return page.evaluate("""(scope) => new Set([...document.querySelectorAll(scope + ' .button')].map(b => Math.round(b.getBoundingClientRect().top))).size === 1""", scope)


def test_join_by_id_mute_and_leave_flow(browser):
    with PageServer(calendar_events=[event("e1", "Design review", 25)]) as server:
        page = open_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        assert page.locator("#headline").inner_text().startswith("Free until")
        join_by_id(page)
        assert page.locator("#meeting-state").inner_text() == "In a meeting"      # in the top bar: the idle card's #headline is hidden in a meeting
        assert page.locator("#meeting-detail").is_visible() and page.locator("#meeting-detail").inner_text() == "Zoom meeting"   # a link has no title: the platform names the call
        assert page.locator(".link").is_hidden()
        page.click("#meeting-actions button:has-text('Mute')")
        page.wait_for_selector("#meeting-actions button:has-text('Unmute')", timeout=5000)
        page.click("#meeting-actions button:has-text('Leave')")
        page.wait_for_timeout(600)                                            # a tap within half a second of the ask is a bounce, not the second tap
        page.click("#meeting-actions button:has-text('Tap again to leave')")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert server.meeting.leaves == 1
        assert errors == []
        page.close()


def test_in_meeting_controls_are_not_rebuilt_while_the_timer_ticks(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.evaluate("window.__firstButton = document.querySelector('#meeting-actions button')")
        assert page.evaluate("window.__firstButton !== null")
        before = page.locator("#detail").inner_text()
        page.wait_for_timeout(2600)
        assert page.evaluate("document.querySelector('#meeting-actions button') === window.__firstButton"), \
            "the action buttons were replaced while nothing about them changed"
        assert page.locator("#detail").inner_text() != before, "the elapsed time should keep ticking"
        page.close()


def test_join_now_joins_the_booking_it_shows_when_one_follows_another(browser):
    """Back to back: the first booking ends as the second starts, and both say "Join now". Once the page shows the
    second, its button must join the second, not keep the first's."""
    skip_if_day_ends_within(1)
    now = datetime.now().astimezone()
    began = min(2, (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 60)   # today only
    hand_over = 8 / 60                                                       # the first ends eight seconds from now
    first = event("b1", "First booking", -began, duration=began + hand_over, url="https://zoom.us/j/11111111111")
    second = event("b2", "Second booking", hand_over, duration=30, url="https://zoom.us/j/22222222222")
    with PageServer(calendar_events=[first, second]) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.getElementById('headline').innerText.startsWith('First booking')", timeout=5000)
        page.wait_for_function("document.getElementById('headline').innerText.startsWith('Second booking')", timeout=12000)
        page.click("#actions button:has-text('Join now')")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert server.meeting.joins[-1][0].endswith("22222222222")
        page.close()


def test_long_names_do_not_overflow_on_phone(browser):
    long_name = "The Extraordinarily Long Conference Room Name Nobody Abbreviates"
    with PageServer(calendar_events=[event("e1", "Quarterly planning session with the entire leadership team", 25)],
                    room_name=long_name) as server:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        assert page.locator("#room-name").inner_text() == long_name
        assert page.locator("#kicker").inner_text().upper() == "ROOM FREE"
        page.close()


SIGN_GREEN, SIGN_AMBER, SIGN_RED, SIGN_NAVY = "rgb(31, 169, 113)", "rgb(232, 160, 19)", "rgb(229, 72, 77)", "rgb(0, 22, 54)"   # #1FA971 #E8A013 #E5484D #001636


def sign_colors(page):
    rail = page.locator(".rail").evaluate("e => getComputedStyle(e).backgroundColor")
    chip = page.locator(".chip").evaluate("e => getComputedStyle(e).backgroundColor")
    body = page.evaluate("getComputedStyle(document.body).backgroundColor")
    return rail, chip, body


def test_door_sign_follows_the_room_state(browser):
    skip_if_day_ends_within(1)   # the booking is 25 minutes out, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Design review", 25)], room_name="Room 1") as server:
        page = sign_on_a_stopped_clock(browser, server, 1024, 600)          # its polls are the test's to bring: nothing waits for a real five seconds
        assert page.locator("#chip-text").inner_text().startswith("Free until")
        assert sign_colors(page) == (SIGN_GREEN, SIGN_GREEN, SIGN_NAVY)   # a green rail and chip on navy, never a green wall
        assert page.locator(".chip").evaluate("e => getComputedStyle(e).color") == SIGN_NAVY   # navy words on green
        assert [b.get_attribute("id") for b in page.locator("button:visible").all()] == ["this-week"]  # a sign has one thing to press at a time (Back waits, hidden, for the week view)
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join",
                          data='{"url": "https://zoom.us/j/98765432100"}',
                          headers={"Content-Type": "application/json"})
        wait_for("the meeting to be joined", lambda: status_of(server)["meeting"]["state"] == "connected")
        poll(page)
        assert page.evaluate("document.body.dataset.state") == "occupied"
        assert page.locator("#chip-text").inner_text() == "In use"
        assert sign_colors(page) == (SIGN_RED, SIGN_RED, SIGN_NAVY)
        assert page.locator(".chip").evaluate("e => getComputedStyle(e).color") == "rgb(255, 255, 255)"   # white words on red
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/leave",
                          data="{}", headers={"Content-Type": "application/json"})
        wait_for("the meeting to be left", lambda: status_of(server)["meeting"]["state"] == "idle")
        poll(page)
        assert page.evaluate("document.body.dataset.state") == "free"
        page.close()


LIGHT_RECORDER = "window.CrystalPanel = { setLight: (state) => (window.lights = window.lights || []).push(state) };"


def test_door_sign_tells_the_panel_app_its_state_for_the_light_bar(browser):
    """The door sign app on the SMT1019 panel puts window.CrystalPanel on the page and colours the light bar from the state it is given:
    each state the sign shows, offline included, is passed on, and only when it changes."""
    with PageServer(room_name="Room 1") as server:
        page = sign_on_a_stopped_clock(browser, server, 1024, 600, init_script=LIGHT_RECORDER)
        assert page.evaluate("window.lights") == ["free"]
        poll(page)
        assert page.evaluate("window.lights") == ["free"]   # the same state again is not passed on
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join",
                          data='{"url": "https://zoom.us/j/98765432100"}', headers={"Content-Type": "application/json"})
        wait_for("the meeting to be joined", lambda: status_of(server)["meeting"]["state"] == "connected")
        poll(page)
        assert page.evaluate("window.lights") == ["free", "occupied"]
        page.route("**/api/status", lambda route: route.abort())
        page.clock.run_for(5000)
        page.wait_for_function("document.body.dataset.state === 'offline'", timeout=5000)
        assert page.evaluate("window.lights") == ["free", "occupied", "offline"]
        page.close()


def test_door_sign_without_the_panel_app_has_no_light_bar_to_tell(browser):
    with PageServer(room_name="Room 1") as server:
        page = sign_on_a_stopped_clock(browser, server, 1024, 600)
        errors = []
        page.on("pageerror", lambda error: errors.append(error))
        poll(page)
        assert page.evaluate("typeof window.CrystalPanel") == "undefined" and errors == []
        page.close()


def test_door_sign_shows_bookings_without_a_video_link(browser):
    # An in-person booking has no link, so the API's "current" is null; the room is still taken.
    # It began 5 minutes ago, but never before today's local midnight: the service lists only today's events.
    now = datetime.now().astimezone()
    minutes_into_the_day = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 60
    with PageServer(calendar_events=[event("b1", "Board lunch", -min(5, minutes_into_the_day), duration=20, url=None),
                                     event("e2", "Design review", 40)], room_name="Room 1") as server:
        page = browser.new_page(viewport={"width": 1024, "height": 600})
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'occupied'", timeout=5000)
        assert page.locator("#chip-text").inner_text().startswith("Booked until")
        assert page.locator("#detail").inner_text() == "Board lunch"
        assert sign_colors(page) == (SIGN_RED, SIGN_RED, SIGN_NAVY)   # a booking holds the room as much as a call does
        assert page.locator("#events .event.now .title").inner_text() == "Board lunch"   # the booking in progress is ringed, and only it
        assert page.locator("#events .event.now").count() == 1 and page.locator("#events .event").first.get_attribute("class") == "event now"
        assert page.locator("#events .event.now").evaluate("e => getComputedStyle(e).boxShadow") == "rgb(229, 72, 77) 0px 0px 0px 2px inset"   # the ring: 2 px, inside, in the status colour
        assert page.locator("#events .event:not(.now)").first.evaluate("e => getComputedStyle(e).boxShadow") == "none"
        page.close()


def test_door_sign_warns_before_the_next_meeting(browser):
    skip_if_day_ends_within(1)   # the booking is 6 minutes out, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Standup", 6)], room_name="Room 1") as server:
        page = browser.new_page(viewport={"width": 1024, "height": 600})
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        assert page.locator("#chip-text").inner_text().startswith("Standup starts in")
        assert page.locator("#detail").inner_text().count(" to ") == 1   # "1:06 PM to 1:36 PM"
        page.close()


def test_door_sign_hides_the_schedule_without_a_calendar(browser):
    with PageServer(calendar_connected=False) as server:
        page = browser.new_page(viewport={"width": 1024, "height": 600})
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.locator("#chip-text").inner_text() == "Free"
        assert page.locator("#today").count() == 1 and not page.locator("#today").is_visible()
        assert page.locator("#room-name").inner_text() == "Lab" and page.locator("#company").is_visible()   # the brand row and the name stay
        assert page.locator("#clock").inner_text() and page.locator("#date").inner_text()
        page.close()


def test_the_location_line_disappears_when_the_room_has_none(browser):
    with PageServer(room_location="") as server:
        page = open_page(browser, server)
        assert page.locator("#room-location").is_hidden()
        assert page.locator("#room-location").evaluate("el => getComputedStyle(el).display") == "none"   # an empty paragraph is already "hidden" to Playwright
        assert page.locator("#room-name").inner_text() == "Lab"
        page.close()
    with PageServer() as server:
        page = open_page(browser, server)
        assert page.locator("#room-location").is_visible() and page.locator("#room-location").inner_text() == "2nd floor"
        page.close()


def test_the_door_sign_hides_the_location_line_when_the_room_has_none(browser):
    with PageServer(room_location="") as server:
        page = browser.new_page(viewport={"width": 1024, "height": 600})
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.locator("#room-location").is_hidden()
        assert page.locator("#room-location").evaluate("el => getComputedStyle(el).display") == "none"
        page.close()


def test_the_door_sign_shows_the_location_line_when_the_room_has_one(browser):
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 1024, "height": 600})
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.locator("#room-location").is_visible()
        assert page.locator("#room-location").inner_text() == "2nd floor"
        assert page.locator("#room-name").inner_text() == "Lab"
        page.close()


def test_the_sign_shows_the_status_as_a_rail_and_a_chip_over_a_photograph_that_is_not_tinted(browser):
    skip_if_day_ends_within(1)
    with PageServer(calendar_events=[event("e1", "Design review", 8)]) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        rail, chip, body = sign_colors(page)
        assert rail == chip == "rgb(232, 160, 19)" and body == "rgb(0, 22, 54)"      # amber on navy, never an amber wall
        assert page.locator(".chip").evaluate("e => getComputedStyle(e).color") == SIGN_NAVY   # navy words on amber
        assert page.locator("#chip-text").inner_text() == "Design review starts in 8 minutes"
        page.wait_for_function("document.body.dataset.photo === 'skyline'", timeout=5000)
        layers = page.evaluate("""() => ['::before', '::after'].map(q => getComputedStyle(document.querySelector('.sign'), q).backgroundImage).join(' ')""")
        assert "backgrounds/skyline.jpg" in layers and "232, 160, 19" not in layers   # the status colour is the rail's, the chip's and the ring's; the photograph is left as it is
        for layer in ("::before", "::after"):                                          # and the layers do nothing to it: no blend, no filter, no fading, no colour behind it
            style = page.evaluate("""(q) => { const s = getComputedStyle(document.querySelector('.sign'), q);
                return [s.opacity, s.filter, s.mixBlendMode, s.backgroundBlendMode, s.backgroundColor]; }""", layer)
            assert style[:3] == ["1", "none", "normal"] and set(style[3].split(", ")) == {"normal"} and style[4] == "rgba(0, 0, 0, 0)", (layer, style)   # one blend mode per background layer
        picture = page.evaluate("(q) => { const s = getComputedStyle(document.querySelector('.sign'), q); return [s.backgroundSize, s.backgroundPosition, s.backgroundRepeat]; }", "::before")
        assert picture == ["cover", "50% 40%", "repeat"], picture                       # it fills the sign, whatever its shape, and is cropped about 40 % of the way down
        assert page.locator("#scene").count() == 0 and page.locator(".backdrop svg").count() == 0   # the drawn scenery is gone
        assert page.locator("#company").inner_text() == "Crystal Practice Management"
        assert fits(page)
        page.close()


def test_the_sign_without_a_calendar_keeps_its_brand_name_and_clock(browser):
    with PageServer(calendar_connected=False) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.locator("#chip-text").inner_text() == "Free"
        assert page.locator("#today").is_hidden() and page.locator("#this-week").is_hidden()
        assert page.locator("#company").is_visible() and page.locator("#clock").inner_text()
        assert fits(page)
        page.close()


def test_long_names_wrap_inside_the_sign_at_both_sizes(browser):
    skip_if_day_ends_within(1)
    title = "Quarterly planning review with the regional leadership teams and the partners"
    for width, height in ((1280, 800), (800, 1280)):
        with PageServer(calendar_events=[event("e1", title, 30)], room_name="Conference Room Twelve", room_location="2nd floor, by the kitchen") as server:
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
            page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
            clock = page.locator("#clock").bounding_box()
            assert clock["x"] + clock["width"] <= width
            assert fits(page)
            page.close()


def test_the_sign_keeps_its_words_on_the_sign_when_names_are_long_over_a_photograph(browser):
    """With a long room name, location, company and title, free or about to start, at both sizes: every block of words ends
    inside the sign, on the photograph, and nothing needs to scroll."""
    skip_if_day_ends_within(1)
    title = "Quarterly planning review with the regional leadership teams and the partners"
    for width, height in ((1280, 800), (800, 1280)):
        for minutes, state in ((30, "free"), (8, "soon")):   # soon puts the long title in the chip too, at 1.5 rem
            with PageServer(calendar_events=[event("e1", title, minutes)], room_name="Conference Room Twelve", room_location="2nd floor, by the kitchen",
                            room_company="Lone Star Eye Care and Surgery Partners") as server:
                page = open_sign(browser, server, width, height, state)
                page.wait_for_function("document.body.dataset.photo === 'skyline'", timeout=5000)
                for selector in (".top", ".status", "#today"):
                    box = page.locator(selector).bounding_box()
                    assert box["x"] >= 0 and box["x"] + box["width"] <= width, (width, height, state, selector)
                    assert box["y"] >= 0 and box["y"] + box["height"] <= height, (width, height, state, selector)
                assert fits(page)
                page.close()


def test_the_sign_names_the_company_from_the_room_config(browser):
    with PageServer(room_company="Lone Star Eye Care") as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.locator("#company").inner_text() == "Lone Star Eye Care"
        page.close()
    with PageServer(room_company="") as server:   # a practice that wants the logo alone
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.locator("#company").is_hidden() and page.locator(".brand img").is_visible()
        assert page.locator("#room-name").inner_text() == "Lab" and fits(page)
        page.close()


def test_the_sign_lists_the_next_three_bookings_as_rows_with_their_platform(browser):
    skip_if_day_ends_within(3)   # the last booking is 100 minutes out
    bookings = [event("e1", "Customer call", 40),
                event("e2", "Design review", 70, url="https://meet.google.com/abc-defg-hij", platform=MeetingPlatform.GOOGLE_MEET),
                event("e3", "Offsite lunch", 100, url=None, platform=MeetingPlatform.UNKNOWN),
                event("e4", "Fourth booking", 130)]
    with PageServer(calendar_events=bookings) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        rows = page.locator("#events .event")
        assert rows.count() == 3                                                    # the next three, not the fourth
        assert page.locator("#events .title").all_text_contents() == ["Customer call", "Design review", "Offsite lunch"]
        assert page.locator("#events .who").all_text_contents() == ["Zoom", "Google Meet"]   # an in-person booking names no platform
        assert all(re.match(TWELVE_HOUR, t) for t in page.locator("#events time").all_text_contents())
        assert page.locator("#events .event.now").count() == 0                       # nothing is in progress
        assert page.locator("#chip-text").inner_text().startswith("Free until") and page.locator("#detail").inner_text() == "Next: Customer call"
        assert page.locator("#today h2").inner_text() == "Today" and page.locator("#this-week").inner_text() == "This week"
        assert page.locator("#score").is_hidden()                                   # the scoreboard strip is off until it has a score
        assert errors == []
        page.close()


def test_the_sign_without_bookings_says_so_instead_of_showing_an_empty_list(browser):
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.locator("#chip-text").inner_text() == "Free for the rest of the day"
        assert page.locator("#detail").inner_text() == "Nothing else is booked in here today."
        assert page.locator("#events .event").count() == 0 and page.locator("#calendar-note").inner_text() == "Nothing else scheduled today."
        assert page.locator("#today").is_visible() and fits(page)
        page.close()


def test_the_sign_sits_side_by_side_in_landscape_and_stacks_in_portrait(browser):
    skip_if_day_ends_within(1)
    with PageServer(calendar_events=[event("e1", "Design review", 30)]) as server:
        for (width, height), stacked in (((1280, 800), False), ((800, 1280), True)):
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
            page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
            status, today = page.locator(".status").bounding_box(), page.locator("#today").bounding_box()
            if stacked:
                assert today["y"] >= status["y"] + status["height"]                  # the bookings under the status
            else:
                assert today["x"] >= status["x"] + status["width"]                   # the bookings beside it
            assert fits(page)
            page.close()


def test_the_sign_loads_nothing_but_its_own_files(browser):
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        urls = []
        page.on("request", lambda r: urls.append(r.url))
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert urls and all(u.startswith(f"http://127.0.0.1:{server.port}/") for u in urls), urls
        page.close()


# --- the door sign's photographs (spec 2026-10-13, section 2.4) and the room settings that choose them ---

PHOTOGRAPHS = sorted(p.stem for p in (STATIC_DIR / "backgrounds").glob("*.jpg"))   # the backgrounds with a file on disk: the others show plain until one is added
BACKGROUND_LABELS = ["Austin skyline", "Capitol", "UT Tower", "Pennybacker Bridge", "Hamilton Pool", "Texas star", "Hill Country", "DKR stadium", "Plain"]   # the table's buttons, in the spec's order
SCHOOL_LABELS = ["Texas", "Michigan", "Texas A&M", "Missouri"]
NAVY = (0, 22, 54)


def choose(server, **settings):
    """Change the room's settings through the route the table's page uses."""
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/settings", data=json.dumps(settings).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200


def open_sign(browser, server, width=1280, height=800, state="free", **options):
    page = browser.new_page(viewport={"width": width, "height": height}, **options)
    page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
    page.wait_for_function(f"document.body.dataset.state === '{state}'", timeout=5000)
    return page


def sign_on_a_stopped_clock(browser, server, width=1280, height=800, init_script=None, state="free"):
    """The sign, loaded, with its timers stopped: its polls run only when the test calls page.clock.run_for. `init_script` runs before the sign's own."""
    page = browser.new_page(viewport={"width": width, "height": height})
    page.clock.install()
    if init_script:
        page.add_init_script(init_script)
    page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
    page.wait_for_function(f"document.body.dataset.state === '{state}'", timeout=5000)
    page.clock.pause_at(datetime.now() + timedelta(seconds=2))
    return page


def poll(page):
    """One status poll of the sign (every 5 s of page time), answered and drawn."""
    with page.expect_response("**/api/status"):
        page.clock.run_for(5000)
    page.wait_for_timeout(150)


def photo_request_methods(page):
    """What the page asks of the photographs' folder, as (method, status) pairs, in the order the answers arrive."""
    asked = []
    page.on("response", lambda r: asked.append((r.request.method, r.status)) if "/static/backgrounds/" in r.url else None)
    return asked


def screenshot_pixel(page, x, y):
    """The colour of one pixel as the browser draws it, from a screenshot of just that pixel (a PNG of one pixel needs no decoder:
    whichever filter its row byte names, a first pixel is its raw bytes)."""
    png = page.screenshot(clip={"x": x, "y": y, "width": 1, "height": 1})
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    position, data = 8, b""
    while position < len(png):
        length, kind = struct.unpack(">I4s", png[position:position + 8])
        body = png[position + 8:position + 8 + length]
        if kind == b"IHDR":
            assert body[8] == 8 and body[9] in (2, 6)                      # 8 bits a channel, RGB or RGBA
        elif kind == b"IDAT":
            data += body
        position += 12 + length
    return tuple(zlib.decompress(data)[1:4])


def wait_for_the_photograph_to_paint(page):
    """The photograph the sign shows is decoded and two frames have been drawn with it: a screenshot from here has it in."""
    page.evaluate("""async () => {
        const address = getComputedStyle(document.body).getPropertyValue('--photo').match(/url\\('(.*)'\\)/)[1];
        const image = new Image();
        image.src = address;
        await image.decode();
        await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
    }""")


def white_photograph():
    """A solid white picture, one pixel of it: with `cover` it fills the sign with white, so what a screenshot reads back at a point
    is white seen through the overlay, and the overlay can be measured."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)                          # 1 by 1, 8 bits, RGB
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b"")


# The navy overlay (spec 2.4) as alpha at fractions of the way across the sign (94 % at the left edge, 84 % at 45 %, 55 % at the right)
# and of the way down (60 % at the top edge, none from 40 % down, 55 % at the bottom edge): the two gradients, one over the other.
OVERLAY_ACROSS = [(0, 0.94), (0.45, 0.84), (1, 0.55)]
OVERLAY_DOWN = [(0, 0.6), (0.4, 0.0), (1, 0.55)]


def stop_between(fraction, stops):
    for (a, low), (b, high) in zip(stops, stops[1:]):
        if fraction <= b:
            return low + (high - low) * (fraction - a) / (b - a)
    return stops[-1][1]


def white_under_the_overlay(x, y, width, height):
    """What the pixel at (x, y) shows when the photograph is white: white and navy mixed by the two gradients together."""
    across, down = stop_between((x + 0.5) / width, OVERLAY_ACROSS), stop_between((y + 0.5) / height, OVERLAY_DOWN)
    alpha = 1 - (1 - across) * (1 - down)
    return tuple(255 * (1 - alpha) + navy * alpha for navy in NAVY)


def open_door_sign_screen(page):
    page.click("#open-options")
    page.click("#open-door-sign")
    page.wait_for_function("document.body.dataset.view === 'door-sign'", timeout=5000)


def test_the_background_choice_made_on_the_table_shows_on_the_sign(browser):
    shows_bridge = ("document.body.dataset.bg === 'bridge' && document.body.dataset.photo === 'bridge'"
                    " && getComputedStyle(document.body).getPropertyValue('--photo').includes('backgrounds/bridge.jpg')")
    with PageServer() as server:
        sign = open_sign(browser, server)
        assert sign.evaluate("document.body.dataset.bg") == "skyline"      # the default
        sign.wait_for_function("getComputedStyle(document.body).getPropertyValue('--photo').includes('backgrounds/skyline.jpg')", timeout=5000)
        table = open_page(browser, server)
        open_door_sign_screen(table)
        assert table.locator("#sign-picker button[aria-pressed='true']").inner_text() == "Austin skyline"
        table.click("#sign-picker button:has-text('Pennybacker Bridge')")
        table.wait_for_function("document.querySelector(\"#sign-picker button[aria-pressed='true']\").innerText === 'Pennybacker Bridge'", timeout=5000)
        sign.wait_for_function(shows_bridge, timeout=8000)                  # the sign's own poll (5 s) brings it
        sign.reload(wait_until="networkidle")
        sign.wait_for_function(shows_bridge, timeout=5000)                  # and a reload keeps it: it is the room's, not the page's
        table.close()
        sign.close()


@pytest.mark.parametrize("key", SIGN_BACKGROUNDS)
def test_each_background_loads_its_photograph_and_plain_shows_the_glow(browser, key):
    has_file = key in PHOTOGRAPHS
    with PageServer() as server:
        choose(server, sign_background=key)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        asked = photo_request_methods(page)
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function(f"document.body.dataset.state === 'free' && document.body.dataset.bg === '{key}'", timeout=5000)
        if has_file:
            page.wait_for_function(f"document.body.dataset.photo === '{key}'", timeout=5000)     # shown once it has decoded
        elif key != "plain":
            page.wait_for_function("'photoMissing' in document.body.dataset", timeout=5000)
        page.wait_for_load_state("networkidle")
        layer = lambda q: page.evaluate(f"getComputedStyle(document.querySelector('.sign'), '{q}').backgroundImage")
        glow = page.locator(".glow").evaluate("e => getComputedStyle(e).display")
        if has_file:
            assert asked == [("GET", 200)]                                 # one request, answered: the style sheet draws the picture already loaded
            assert f"backgrounds/{key}.jpg" in layer("::before")           # the photograph, as a layer of its own ...
            assert layer("::after").count("linear-gradient") == 2          # ... under the navy overlay (the left-to-right one and the top-to-bottom one)
            assert glow == "none"
        else:
            assert page.evaluate("document.body.dataset.photo === undefined")
            assert layer("::before") == "none" and layer("::after") == "none"      # no photograph, no overlay: the navy, with the glow
            assert glow == "block"
            assert asked == ([] if key == "plain" else [("GET", 404)])     # plain asks for nothing at all; a file that is not there, once
        assert page.evaluate("document.body.dataset.bg") == key             # whichever it shows, the page says which was chosen
        assert fits(page)
        page.close()


@pytest.mark.parametrize("width, height", [(1280, 800), (800, 1280)])
def test_the_overlay_over_a_white_photograph_reads_back_at_fixed_pixels_and_the_status_stays_solid(browser, width, height):
    """What the sign draws, measured on the screen: a white photograph under the overlay, read back at ten points (seven at the edges
    and the corners, three more at 1280 by 800 where nothing is written), each within 3 of the white and navy that the two gradients
    mix; and the rail and the chip exactly the status colour, which they are only while they are stacked above the overlay."""
    with PageServer(calendar_events=[event("e1", "Design review", 45)], room_name="Boardroom") as server:
        choose(server, sign_background="skyline")
        page = browser.new_page(viewport={"width": width, "height": height})
        page.route("**/static/backgrounds/skyline.jpg", lambda route: route.fulfill(status=200, body=white_photograph(), content_type="image/png"))
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free' && document.body.dataset.photo === 'skyline'", timeout=8000)
        wait_for_the_photograph_to_paint(page)
        points = [(40, 20), (width // 2, 20), (width - 40, 20), (40, height - 20), (width // 2, height - 20), (width - 40, height - 20), (width - 40, height - 60)]
        if height == 800:
            points += [(40, 600), (576, 600), (width - 40, 600)]             # clear of the words at 1280 by 800
        for x, y in points:
            got, want = screenshot_pixel(page, x, y), white_under_the_overlay(x, y, width, height)
            assert all(abs(g - w) <= 3 for g, w in zip(got, want)), ((x, y), got, tuple(round(v) for v in want))
        chip = page.locator(".chip").bounding_box()
        assert screenshot_pixel(page, 10, height // 2) == (31, 169, 113)    # the rail: #1FA971 itself, not a mix with anything
        assert screenshot_pixel(page, int(chip["x"]) + 5, int(chip["y"] + chip["height"] / 2)) == (31, 169, 113)   # and the chip
        page.close()


def test_the_booking_rows_sit_on_translucent_navy_over_a_photograph_and_on_a_pale_veil_over_plain(browser):
    skip_if_day_ends_within(1)
    with PageServer(calendar_events=[event("e1", "Design review", 40)]) as server:
        for key, row in (("capitol", "rgba(0, 22, 54, 0.55)"), ("plain", "rgba(255, 255, 255, 0.06)")):
            choose(server, sign_background=key)
            page = open_sign(browser, server)
            page.wait_for_selector("#events .event", timeout=5000)
            page.wait_for_function(f"document.body.dataset.bg === '{key}' && (document.body.dataset.photo === 'capitol') === {str(key == 'capitol').lower()}", timeout=5000)
            assert page.locator("#events .event").first.evaluate("e => getComputedStyle(e).backgroundColor") == row, key
            page.close()


@pytest.mark.parametrize("key", PHOTOGRAPHS)
def test_the_words_stay_readable_over_every_photograph(browser, key):
    skip_if_day_ends_within(1)
    with PageServer(calendar_events=[event("e1", "Design review", 40)]) as server:
        choose(server, sign_background=key)
        page = open_sign(browser, server)
        page.wait_for_function(f"document.body.dataset.photo === '{key}'", timeout=5000)
        page.wait_for_load_state("networkidle")
        wait_for_the_photograph_to_paint(page)
        assert colour(page, "#room-name") == "rgb(255, 255, 255)"          # the words and the chip keep their colours, as on plain navy
        assert page.locator(".chip").evaluate("e => getComputedStyle(e).color") == SIGN_NAVY
        assert page.locator(".chip").evaluate("e => getComputedStyle(e).backgroundColor") == SIGN_GREEN
        overlay = page.evaluate("getComputedStyle(document.querySelector('.sign'), '::after').backgroundImage")
        red, green, blue, alpha = re.search(r"rgba\((\d+), (\d+), (\d+), ([\d.]+)\) 0%", overlay).groups()   # the left edge of the left-to-right gradient
        assert (int(red), int(green), int(blue)) == NAVY and float(alpha) >= 0.9
        # So at the left edge, where the words are, the screen is navy whatever the photograph: at 90 % the photograph moves a channel
        # by 26 at most (a tenth of 255), and the overlay is 94 %.
        pixel = screenshot_pixel(page, 40, 400)
        assert all(abs(c - n) <= 26 for c, n in zip(pixel, NAVY)), (key, pixel)
        page.close()


SIGN_WORDS = {"company": "#company", "room name": "#room-name", "location": "#room-location", "detail": "#detail", "clock": "#clock", "date": "#date",
              "Today": ".today-head h2", "This week": "#this-week", "booking time": "#events .event time", "booking title": "#events .event .title", "booking platform": "#events .event .who"}
BRIGHTEST_BEHIND = """async ([png, boxes]) => {
    const image = new Image();
    image.src = 'data:image/png;base64,' + png;
    await image.decode();
    const canvas = document.createElement('canvas');
    canvas.width = image.width;
    canvas.height = image.height;
    const context = canvas.getContext('2d');
    context.drawImage(image, 0, 0);
    const linear = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return boxes.map((b) => {
        const data = context.getImageData(Math.floor(b.x), Math.floor(b.y), Math.ceil(b.width), Math.ceil(b.height)).data;
        const lights = [];
        for (let i = 0; i < data.length; i += 4) lights.push(0.2126 * linear(data[i]) + 0.7152 * linear(data[i + 1]) + 0.0722 * linear(data[i + 2]));
        lights.sort((a, b) => a - b);
        return lights[Math.floor(lights.length * 0.99)];
    });
}"""


def luminance(colour):
    """The WCAG relative luminance of a colour given as (red, green, blue), each 0 to 255."""
    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    red, green, blue = (channel(v) for v in colour)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


# The week view's words, found when it is open on a week with every kind of day (a busy one with "+N more", an empty one); a hidden
# one (a booking left out, a "+N more" not needed) has no box to measure.
WEEK_WORDS = {"week heading": ".week-head h2", "Back": "#week-back", "day name": "#week .day-name", "date": "#week .day-date",
              "booking time": "#week .slot:not([hidden]) time", "booking title": "#week .slot:not([hidden]) .title", "booking platform": "#week .slot:not([hidden]) .who",
              "nothing booked": "#week .none", "more": "#week .more:not([hidden])"}
# The scoreboard strip's words (spec 2.5), found while a school is on: the score line, its ranks (burnt orange) and the next game.
SCORE_WORDS = {"score line": "#score-last", "score rank": "#score .rank", "next game": "#score-next"}
LABELS = {"This week", "Back"}    # a button's label: measured over its own text, so that a fill behind it counts
HIDE_TODAYS_WORDS = (".top *, .status *, .today-head h2, .event > *, .score .last, .score .next { visibility: hidden !important; } "     # the rows keep their own veil, the buttons their border and fill, the strip its dark band
                     "#this-week { color: transparent !important; }")
HIDE_THE_WEEKS_WORDS = (".top *, .status *, .week-head h2, .day-head *, .slot > *, .none, .more, .score .last, .score .next { visibility: hidden !important; } "
                        "#week-back { color: transparent !important; }")


def contrast_of_every_word(page, browser, selectors, hide):
    """The words named in `selectors` (name -> CSS selector), each with its WCAG contrast against the brightest 1 % of the picture behind it
    (the screen with the words hidden by the CSS in `hide`): [(name, contrast, least allowed)]. A label's box is its text alone, and
    only its colour is hidden, so that whatever the button has behind the label is part of what the label is read against."""
    words = page.evaluate("""([selectors, labels]) => Object.entries(selectors).flatMap(([name, selector]) => [...document.querySelectorAll(selector)].map((e) => {
        let r = e.getBoundingClientRect();
        if (labels.includes(name)) {                                                   // the box of the label's text, not of the button around it
            const range = document.createRange();
            range.selectNodeContents([...e.childNodes].find((n) => n.nodeType === 3 && n.textContent.trim()));
            r = range.getBoundingClientRect();
        }
        const s = getComputedStyle(e);
        return { name: name, x: r.x, y: r.y, width: r.width, height: r.height, color: s.color, size: parseFloat(s.fontSize), weight: parseInt(s.fontWeight) };
    }))""", [selectors, sorted(LABELS)])
    assert {w["name"] for w in words} == set(selectors)                                # every kind of word was found
    hiding = page.add_style_tag(content=hide)
    picture = base64.b64encode(page.screenshot()).decode()
    hiding.evaluate("e => e.remove()")
    reader = browser.new_page()
    try:
        lights = reader.evaluate(BRIGHTEST_BEHIND, [picture, words])
    finally:
        reader.close()
    measured = []
    for word, light in zip(words, lights):
        ink = tuple(int(v) for v in re.findall(r"\d+", word["color"])[:3])
        large = word["size"] >= 24 or (word["size"] >= 18.66 and word["weight"] >= 700)
        measured.append((word["name"], (luminance(ink) + 0.05) / (light + 0.05), 3.0 if large else 4.5))
    return measured


@pytest.mark.parametrize("width, height", [(1280, 800), (800, 1280)])
@pytest.mark.parametrize("key", PHOTOGRAPHS)
def test_every_word_keeps_its_contrast_over_every_photograph(browser, key, width, height):
    """Measured, not looked at: with the words hidden, the brightest 1 % of the picture behind each word (so each word is judged at the
    hardest it is to read over this photograph) against the word's own colour: 4.5 to 1 for text, 3 to 1 for large text (WCAG AA). A
    photograph swapped in later that is too bright where the words are fails here, by name. At both sizes (the photograph is cropped
    differently on a tall sign, and the words sit elsewhere on it), on the Today list with the scoreboard strip up along the bottom (its score line,
    its orange ranks and its next game) and on the week view.

    The page's clock is pinned. The date and the clock are right-aligned, so a short one is a narrow box at the very edge, where the
    brightest part of a photograph is (the stadium's floodlights are under the date); a long one reaches over darker picture. Friday,
    May 1, 2026 at 1:05 AM is among the shortest strings the sign shows (the date within 2 px of the shortest), so the test measures
    the worst case, and measures it on every day, not only on the days of the calendar that happen to be short."""
    skip_if_day_ends_within(2)
    bookings = [event("e1", "Customer call with Lone Star Eye Care", 40), event("e2", "Design review", 70), event("e3", "Sales pipeline", 100)]
    bookings += [event_on(f"w{n}", f"Planning review {n}", 1, 8 + n, 0, url=None, platform=MeetingPlatform.UNKNOWN if n % 2 else MeetingPlatform.ZOOM) for n in range(8)]   # a busy tomorrow
    with PageServer(calendar_events=bookings, room_name="Boardroom", room_location="2nd floor, by the kitchen", schedules=every_school()) as server:
        choose(server, sign_background=key, sign_scores=["texas"])        # with the scoreboard strip up along the bottom
        scoreboard_has(server, ["texas"])
        page = browser.new_page(viewport={"width": width, "height": height})
        page.clock.install(time=datetime(2026, 5, 1, 1, 5))
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.locator("#date").inner_text() == "Friday, May 1, 2026" and page.locator("#clock").inner_text() == "1:05\u00a0AM"   # the pin took
        page.wait_for_function(f"document.body.dataset.photo === '{key}'", timeout=5000)
        page.wait_for_selector("#events .event", timeout=5000)
        page.wait_for_selector("#score:not([hidden])", timeout=5000)
        wait_for_the_photograph_to_paint(page)
        for name, contrast, least in contrast_of_every_word(page, browser, {**SIGN_WORDS, **SCORE_WORDS}, HIDE_TODAYS_WORDS):
            assert contrast >= least, (key, width, height, "Today", name, round(contrast, 2))
        open_the_week(page)
        wait_for_the_photograph_to_paint(page)
        for name, contrast, least in contrast_of_every_word(page, browser, WEEK_WORDS, HIDE_THE_WEEKS_WORDS):
            assert contrast >= least, (key, width, height, "week", name, round(contrast, 2))
        page.close()


def test_the_picker_on_the_table_offers_every_background_and_posts_the_one_pressed(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        open_door_sign_screen(page)
        picker = page.locator("#sign-picker")
        assert picker.locator("button").all_inner_texts() == BACKGROUND_LABELS     # the spec's order
        assert picker.locator("button[aria-pressed='true']").all_inner_texts() == ["Austin skyline"]
        for key, label in zip(SIGN_BACKGROUNDS, BACKGROUND_LABELS):
            page.click(f"#sign-picker button:has(:text-is('{label}'))")
            page.wait_for_function(f"document.querySelector(\"#sign-picker button[aria-pressed='true']\").innerText === {label!r}", timeout=5000)
            assert page.request.get(f"http://127.0.0.1:{server.port}/api/settings").json()["sign_background"] == key
            assert picker.locator("button[aria-pressed='true']").count() == 1
        page.close()


def test_the_school_switches_round_trip(browser):
    with PageServer() as server:
        base = f"http://127.0.0.1:{server.port}"
        page = open_page(browser, server)
        schools = lambda: page.request.get(base + "/api/settings").json()["sign_scores"]
        open_door_sign_screen(page)
        switches = page.locator("#scores-picker button")
        assert "FOOTBALL SCORES" in page.locator(".door-section").inner_text().upper()   # the label the four switches sit under
        assert switches.all_inner_texts() == SCHOOL_LABELS
        assert page.locator("#scores-picker button[aria-pressed='true']").count() == 0 and schools() == []   # off until someone turns a school on
        pressed = lambda: page.locator("#scores-picker button[aria-pressed='true']").all_inner_texts()
        page.click("#scores-picker button:has(:text-is('Texas A&M'))")
        page.wait_for_function("document.querySelectorAll(\"#scores-picker button[aria-pressed='true']\").length === 1", timeout=5000)
        assert schools() == ["texas_am"] and pressed() == ["Texas A&M"]
        page.click("#scores-picker button:has(:text-is('Texas'))")
        page.wait_for_function("document.querySelectorAll(\"#scores-picker button[aria-pressed='true']\").length === 2", timeout=5000)
        assert schools() == ["texas", "texas_am"] and pressed() == ["Texas", "Texas A&M"]      # in the list's own order, not the order of the taps
        assert page.request.get(base + "/api/status").json()["settings"]["sign_scores"] == ["texas", "texas_am"]   # what the sign reads
        page.click("#scores-picker button:has(:text-is('Texas A&M'))")                                  # a switch turns off as it turned on
        page.wait_for_function("document.querySelectorAll(\"#scores-picker button[aria-pressed='true']\").length === 1", timeout=5000)
        assert schools() == ["texas"]
        page.reload(wait_until="networkidle")                                                      # and the page shows what the room remembers
        open_door_sign_screen(page)
        assert pressed() == ["Texas"]
        page.click("#scores-picker button:has(:text-is('Texas'))")
        page.wait_for_function("document.querySelectorAll(\"#scores-picker button[aria-pressed='true']\").length === 0", timeout=5000)
        assert schools() == []
        refused = page.request.post(base + "/api/settings", data='{"sign_scores": ["ohio_state"]}', headers=JSON)   # a school nobody listed
        assert refused.status == 400 and "ohio_state" in refused.json()["error"] and schools() == []
        page.close()


def test_the_door_sign_buttons_are_not_rebuilt_while_nothing_changes(browser):
    """As the screensaver picker's: a tap is never lost to a re-render, so the buttons stay while the page polls."""
    with PageServer() as server:
        page = open_page(browser, server)
        open_door_sign_screen(page)
        handles = [page.locator("#sign-picker button").first.element_handle(), page.locator("#scores-picker button").first.element_handle()]
        page.wait_for_timeout(2600)                                          # the page polls every two seconds
        assert all(h.evaluate("e => e.isConnected") for h in handles)
        page.close()


def test_options_leads_to_the_door_sign_screen_and_back(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.click("#open-options")
        assert page.locator("#open-door-sign").inner_text() == "Door sign" and page.locator("#open-door-sign").is_visible()
        page.click("#open-door-sign")
        assert page.evaluate("document.body.dataset.view") == "door-sign"
        assert page.locator("#sign-picker").is_visible() and page.locator("#scores-picker").is_visible()
        assert page.locator("#link-form").is_hidden()                       # the idle screen is gone
        page.click("#back-from-door-sign")
        assert page.evaluate("document.body.dataset.view") == "options"
        page.click("#back-from-options")
        assert page.evaluate("document.body.dataset.view") == "idle"
        page.close()


def test_the_door_sign_screen_is_a_sub_screen_of_idle_mode_like_the_screensaver(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        open_door_sign_screen(page)
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}', headers=JSON)
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=8000)   # a call that begins takes the page back to its own screen
        page.close()


def test_the_door_sign_choices_are_hidden_while_the_room_cannot_be_reached(browser):
    """What the page shows there would be a guess: it says so as the other pickers do, and comes back with the room."""
    port = free_port()
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    with PageServer(port=port):
        page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        open_door_sign_screen(page)
        assert page.locator(".door-section").is_visible()
    page.wait_for_function("document.body.dataset.state === 'offline'", timeout=10000)
    assert page.locator(".door-section").is_hidden()
    with PageServer(port=port):
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=10000)
        assert page.locator(".door-section").is_visible()
    page.close()


def test_the_sign_fits_with_a_photograph_at_both_sizes_and_on_a_full_hd_screen(browser):
    skip_if_day_ends_within(1)
    bookings = [event("e1", "Quarterly planning review with the regional leadership teams and the partners", 30),
                event("e2", "Design review", 70), event("e3", "Offsite lunch", 100, url=None, platform=MeetingPlatform.UNKNOWN)]
    for key in ("capitol", "star"):                                          # a pale photograph and a busy one
        with PageServer(calendar_events=bookings, room_name="Conference Room Twelve", room_location="2nd floor, by the kitchen") as server:
            choose(server, sign_background=key)
            for width, height in ((1280, 800), (800, 1280), (1920, 1080)):
                page = open_sign(browser, server, width, height)
                page.wait_for_function(f"document.body.dataset.photo === '{key}'", timeout=5000)
                assert fits(page), (key, width, height)
                page.close()


def test_a_long_room_name_gets_smaller_type_on_the_sign(browser):
    short, edge, long = "Second Floor Conf Room 2", "Second Floor Conf Room 22", "Executive Conference Room North Wing"
    assert (len(short), len(edge), len(long)) == (24, 25, 36)               # a name of 24 characters keeps the full size; 25 is "long"
    sizes = {}
    for width, height in ((1280, 800), (800, 1280)):
        for name in (short, edge, long):
            with PageServer(room_name=name) as server:
                page = open_sign(browser, server, width, height)
                sizes[(width, name)] = page.locator("#room-name").evaluate("e => parseFloat(getComputedStyle(e).fontSize)")
                assert page.locator("#room-name").evaluate("e => e.classList.contains('long')") == (name != short)
                assert page.locator("#room-name").inner_text() == name and fits(page), (width, name)
                page.close()
        assert sizes[(width, edge)] < sizes[(width, short)] and sizes[(width, long)] == sizes[(width, edge)]
        assert sizes[(width, edge)] <= 0.7 * sizes[(width, short)]          # clearly smaller, not a point less


def test_the_sign_header_wraps_instead_of_breaking_the_company_name(browser):
    """The clock and date used to squeeze the brand row until "Management" broke mid-word. At every width from a small phone to a
    desktop, in steps of four pixels (the clock and date share the row from about 584 px; a brand row allowed down to 14 rem still
    broke the word at 552 to 560), every word of the company name stays whole on one line and nothing runs off the sign."""
    with PageServer(room_company="Crystal Practice Management") as server:
        page = open_sign(browser, server)
        for width in [*range(320, 700, 4), 768, 1024, 1280]:
            page.set_viewport_size({"width": width, "height": 800})
            lines = page.evaluate("""() => {
                const node = document.getElementById('company').firstChild, text = node.textContent, range = document.createRange();
                const tops = [...text].map((_, i) => { range.setStart(node, i); range.setEnd(node, i + 1); return Math.round(range.getBoundingClientRect().top); });
                let at = 0; return text.split(' ').map((word) => { const t = tops.slice(at, at + word.length); at += word.length + 1; return new Set(t).size; });
            }""")
            assert lines == [1, 1, 1], (width, lines)                        # each word on one line: none is split
            clock, date = page.locator("#clock").bounding_box(), page.locator("#date").bounding_box()
            assert clock["x"] >= 0 and clock["x"] + clock["width"] <= width and date["x"] + date["width"] <= width, width
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"), width
        page.close()


def test_a_poll_that_changes_nothing_rewrites_nothing_on_the_sign(browser):
    """The status words are aria-live: writing the same words again is announced again, so they are written when they change. The
    backdrop is drawn once, when the room's choice is first known or changes, not at every poll."""
    skip_if_day_ends_within(1)
    with PageServer(calendar_events=[event("e1", "Design review", 40)]) as server:
        page = sign_on_a_stopped_clock(browser, server)
        page.wait_for_function("document.body.dataset.photo === 'skyline'", timeout=5000)
        page.evaluate("""() => {
            window.records = [];
            const watch = (node, options, label) => new MutationObserver((list) => list.forEach((r) => window.records.push(label + ':' + r.type + ':' + (r.attributeName || ''))))
                .observe(node, options);
            watch(document.querySelector('[aria-live]'), { subtree: true, childList: true, characterData: true, attributes: true }, 'status');
            watch(document.querySelector('.backdrop'), { subtree: true, childList: true, characterData: true, attributes: true }, 'backdrop');
            watch(document.body, { attributes: true, attributeFilter: ['style', 'data-bg', 'data-photo', 'data-photo-missing'] }, 'body');
        }""")
        poll(page)
        assert page.evaluate("window.records") == []                         # the same status and the same choice: nothing is written
        choose(server, sign_background="capitol")
        poll(page)
        page.wait_for_function("document.body.dataset.photo === 'capitol'", timeout=5000)
        records = page.evaluate("window.records")
        assert any(r.startswith("body:attributes:") for r in records)        # the observers do see a real change ...
        assert not any(r.startswith(("status:", "backdrop:")) for r in records)   # ... which is the choice and nothing else
        page.close()


@pytest.mark.parametrize("width, height, root", [(1280, 800, 16), (1920, 1080, 24)])
def test_the_dot_the_calendar_icon_and_the_row_padding_scale_with_the_text(browser, width, height, root):
    skip_if_day_ends_within(1)
    with PageServer(calendar_events=[event("e1", "Design review", 40)]) as server:
        page = open_sign(browser, server, width, height)
        px = lambda selector, prop: page.locator(selector).first.evaluate(f"e => parseFloat(getComputedStyle(e).{prop})")
        assert page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)") == root     # the sign grows with a bigger screen
        assert px(".chip .dot", "width") == px(".chip .dot", "height") == 0.875 * root
        assert px("#this-week svg", "width") == px("#this-week svg", "height") == 1.125 * root
        assert px("#events .event", "paddingTop") == px("#events .event", "paddingBottom") == 0.625 * root
        assert px("#events .event", "paddingLeft") == px("#events .event", "paddingRight") == 0.875 * root
        assert fits(page)
        page.close()


def test_a_photograph_that_is_there_is_asked_for_once_and_not_again_at_a_poll(browser):
    with PageServer() as server:
        choose(server, sign_background="capitol")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        asked = photo_request_methods(page)
        page.clock.install()
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free' && document.body.dataset.photo === 'capitol'", timeout=8000)
        page.clock.pause_at(datetime.now() + timedelta(seconds=2))
        page.wait_for_timeout(300)
        assert asked == [("GET", 200)]                                       # one request, answered: the picture is held, and the style sheet draws that one
        for _ in range(3):
            poll(page)
        assert asked == [("GET", 200)] and page.evaluate("document.body.dataset.photo") == "capitol"      # nothing across polls
        page.close()


def test_a_reload_asks_for_the_photograph_once_and_the_room_may_say_it_is_unchanged(browser):
    with PageServer() as server:
        choose(server, sign_background="tower")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        asked = photo_request_methods(page)
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.photo === 'tower'", timeout=8000)
        assert asked == [("GET", 200)]
        page.reload(wait_until="networkidle")
        page.wait_for_function("document.body.dataset.photo === 'tower'", timeout=8000)
        assert len(asked) == 2 and asked[1][0] == "GET" and asked[1][1] in (200, 304)                   # a 304 is the browser's copy, still good
        page.close()


def test_a_new_choice_keeps_the_old_photograph_on_screen_until_its_own_has_decoded(browser):
    held = []
    with PageServer() as server:
        choose(server, sign_background="capitol")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.clock.install()
        page.route("**/static/backgrounds/bridge.jpg", lambda route: held.append(route))                # not answered until the test says
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free' && document.body.dataset.photo === 'capitol'", timeout=8000)
        page.clock.pause_at(datetime.now() + timedelta(seconds=2))
        choose(server, sign_background="bridge")
        poll(page)
        page.wait_for_function("document.body.dataset.bg === 'bridge'", timeout=5000)                   # the sign knows what was chosen ...
        page.wait_for_timeout(300)
        assert [route.request.method for route in held] == ["GET"]                                      # the picture itself is the one request
        assert page.evaluate("document.body.dataset.photo") == "capitol"                                # ... and the sign shows the old photograph, as it was
        assert "backgrounds/capitol.jpg" in page.evaluate("getComputedStyle(document.body).getPropertyValue('--photo')")
        assert "backgrounds/capitol.jpg" in page.evaluate("getComputedStyle(document.querySelector('.sign'), '::before').backgroundImage")
        assert page.evaluate("getComputedStyle(document.querySelector('.glow')).display") == "none"      # not the glow, not bare navy
        held[0].fulfill(status=200, body=(STATIC_DIR / "backgrounds" / "bridge.jpg").read_bytes(), content_type="image/jpeg")
        page.wait_for_function("document.body.dataset.photo === 'bridge'", timeout=8000)
        assert "backgrounds/bridge.jpg" in page.evaluate("getComputedStyle(document.body).getPropertyValue('--photo')")
        page.close()


@pytest.mark.parametrize("failure", ["not found", "not a picture", "connection refused"])
def test_a_photograph_that_fails_to_load_shows_plain_and_is_asked_for_once(browser, failure):
    """A file the room has no copy of, one that is not an image, a request that fails on the way: the same to the sign, which shows
    the navy with the glow, keeps saying which background was chosen, and does not ask again at a poll."""
    asked = []

    def answer(route):
        asked.append(failure)
        if failure == "not found":
            route.fulfill(status=404, body="Not Found", content_type="text/plain")
        elif failure == "not a picture":
            route.fulfill(status=200, body=b"<html>this is not a picture</html>", content_type="image/jpeg")
        else:
            route.abort()

    with PageServer() as server:
        choose(server, sign_background="capitol")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.clock.install()
        page.route("**/static/backgrounds/capitol.jpg", answer)
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free' && 'photoMissing' in document.body.dataset", timeout=8000)
        page.clock.pause_at(datetime.now() + timedelta(seconds=2))
        assert page.evaluate("getComputedStyle(document.querySelector('.glow')).display") == "block"
        assert page.evaluate("document.body.dataset.photo === undefined && document.body.dataset.bg") == "capitol"
        assert page.evaluate("getComputedStyle(document.querySelector('.sign'), '::before').backgroundImage") == "none"
        poll(page)
        poll(page)
        assert asked == [failure] and page.evaluate("'photoMissing' in document.body.dataset")           # once, and not at every poll
        page.close()


def test_a_photograph_that_failed_is_asked_for_again_after_an_outage(browser):
    """The room going away and coming back is an update, which may have brought the photograph: the sign asks once more."""
    photograph = (STATIC_DIR / "backgrounds" / "bridge.jpg").read_bytes()
    there, answers = {"now": False}, []

    def answer(route):
        answers.append(200 if there["now"] else 404)
        if there["now"]:
            route.fulfill(status=200, body=photograph, content_type="image/jpeg")
        else:
            route.fulfill(status=404, body="Not Found", content_type="text/plain")

    with PageServer() as server:
        choose(server, sign_background="bridge")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.clock.install()
        page.route("**/static/backgrounds/bridge.jpg", answer)
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free' && 'photoMissing' in document.body.dataset", timeout=8000)
        page.clock.pause_at(datetime.now() + timedelta(seconds=2))
        assert page.evaluate("document.body.dataset.photo === undefined") and answers == [404]      # plain's look; the room was asked once
        poll(page)
        poll(page)
        assert answers == [404] and page.evaluate("document.body.dataset.bg") == "bridge"          # and not at every poll
        there["now"] = True                                                                          # an update brings the file ...
        page.route("**/api/status", lambda route: route.abort())                                      # ... and restarts the room
        with page.expect_event("requestfailed"):
            page.clock.run_for(5000)
        page.wait_for_function("document.body.dataset.state === 'offline'", timeout=5000)
        page.unroute("**/api/status")
        poll(page)                                                                                    # the room is back
        page.wait_for_function("document.body.dataset.photo === 'bridge'", timeout=8000)
        assert answers.count(404) == 1 and 200 in answers
        assert page.evaluate("getComputedStyle(document.querySelector('.glow')).display") == "none"
        assert page.evaluate("'photoMissing' in document.body.dataset") is False
        page.close()


def test_a_photograph_whose_request_never_answers_is_given_up_on_after_twenty_seconds(browser, monkeypatch):
    """A connection that went half open neither answers nor fails. The sign waits twenty seconds and then counts the photograph as
    failed (plain, with the glow); until then it waits on bare navy, and it never asks again at a poll. The request is held by the
    server itself (the test's own middleware answers nothing until told to), and the page's clock runs the twenty seconds in a moment, one
    status poll at a time (the page's clock also drives AbortSignal.timeout, so a poll inside a longer jump could abort under load, which
    shows the sign as outage and has it ask for the photograph again). Giving up also lets go of the request: the server sees the
    connection close, which it does not while the sign still waits."""
    release, held, dropped = threading.Event(), [], []
    served = control_service._static_no_cache

    @web.middleware
    async def stalling(request, handler):
        if request.path == "/static/backgrounds/capitol.jpg":
            held.append(request.method)
            while not release.is_set():
                dropped.append(request.transport is None or request.transport.is_closing())   # aiohttp drops the transport once the browser has closed the connection
                await asyncio.sleep(0.05)
        return await served(request, handler)

    monkeypatch.setattr(control_service, "_static_no_cache", stalling)
    with PageServer() as server:
        choose(server, sign_background="capitol")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        try:
            page.clock.install()
            page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="commit")           # the held picture would keep "networkidle" waiting
            page.wait_for_function("document.body.dataset.state === 'free' && document.body.dataset.bg === 'capitol'", timeout=8000)
            page.wait_for_timeout(300)
            assert held == ["GET"]                                                             # the picture was asked for, and is not answered
            page.clock.pause_at(datetime.now() + timedelta(seconds=2))
            for _ in range(3):
                poll(page)                                                                     # about 17 seconds in: still waiting
            assert page.evaluate("document.body.dataset.photo === undefined && !('photoMissing' in document.body.dataset)")
            assert page.evaluate("getComputedStyle(document.querySelector('.glow')).display") == "none"            # bare navy: not given up yet
            assert dropped and not any(dropped)                                                # and the request is still held open: the sign has not let go
            poll(page)                                                                         # about 22 seconds in: given up
            page.wait_for_function("'photoMissing' in document.body.dataset", timeout=5000)
            assert page.evaluate("getComputedStyle(document.querySelector('.glow')).display") == "block"           # plain, with the glow
            assert page.evaluate("getComputedStyle(document.querySelector('.sign'), '::before').backgroundImage") == "none"
            assert page.evaluate("document.body.dataset.bg") == "capitol"                      # and it still says which was chosen
            for _ in range(50):                                                                # the let-go reaches the server a moment after the give-up
                if dropped[-1]:
                    break
                page.wait_for_timeout(100)
            assert dropped[-1], "the sign gave the photograph up but left its request open"   # the request was released, not just forgotten
            poll(page)
            poll(page)                                                                         # two polls more
            assert held == ["GET"]                                                             # one request, while it stalled and after it was given up
        finally:
            release.set()                                                                       # the server may stop: its held request goes
            page.close()


# --- the week view: This week replaces the Today list with seven days, today first (spec 2026-10-13, section 2.3) ---

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def event_on(event_id, title, days_ahead, hour, minute, duration=45, url="https://zoom.us/j/98765432100?pwd=abc", platform=MeetingPlatform.ZOOM):
    """A booking `days_ahead` days from today at hour:minute on the device's clock (event() counts minutes from now instead)."""
    start = datetime.combine(datetime.now().astimezone().date() + timedelta(days=days_ahead), time_of_day(hour, minute)).astimezone()
    return CalendarEvent(id=event_id, title=title, start_time=start, end_time=start + timedelta(minutes=duration), meeting_url=url, meeting_platform=platform)


def the_next_seven_days():
    """What the week view heads its columns with, from the test's own clock: the day names and the dates ("Oct 14")."""
    today = datetime.now().astimezone().date()
    days = [today + timedelta(days=n) for n in range(7)]
    return [WEEKDAYS[d.weekday()] for d in days], [f"{d:%b} {d.day}" for d in days]


def open_the_week(page):
    page.click("#this-week")
    page.wait_for_function("document.body.dataset.view === 'week' && document.querySelectorAll('#week .day').length === 7", timeout=5000)


def visible_buttons(page):
    return page.locator("button:visible").evaluate_all("buttons => buttons.map(b => b.id)")


def test_this_week_shows_seven_columns_and_back_returns(browser):
    skip_if_day_ends_within(1)                                              # the first booking is 40 minutes out, on today's list
    names, dates = the_next_seven_days()
    bookings = [event("e1", "Customer call", 40),
                event_on("e2", "Design review", 1, 10, 0, url="https://meet.google.com/abc-defg-hij", platform=MeetingPlatform.GOOGLE_MEET),
                event_on("e3", "Sales pipeline", 1, 14, 30),
                event_on("e4", "Offsite lunch", 3, 12, 0, url=None, platform=MeetingPlatform.UNKNOWN)]
    for width, height in ((1280, 800), (800, 1280)):
        with PageServer(calendar_events=bookings, room_name="Boardroom") as server:
            page = open_sign(browser, server, width, height)
            errors, asked = [], []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("request", lambda r: asked.append(r.url) if r.url.endswith("/api/calendar/week") else None)
            page.wait_for_selector("#events .event", timeout=5000)
            assert page.locator("#week").is_hidden() and page.evaluate("document.body.dataset.view") == "today"
            assert asked == [] and visible_buttons(page) == ["this-week"]    # the week is asked for when it is wanted; a sign has one thing to press
            today_head = page.locator(".today-head").bounding_box()
            open_the_week(page)
            assert page.locator("#today").is_hidden() and page.locator("#week").is_visible() and len(asked) == 1
            days = page.locator("#week .day")
            assert page.locator("#week .day-name").all_text_contents() == names           # today first, then the six days after it
            assert page.locator("#week .day-date").all_text_contents() == dates
            assert page.locator("#week .day[aria-current='date']").count() == 1 and days.first.get_attribute("aria-current") == "date"
            rows = lambda selector: [days.nth(n).locator(selector).all_text_contents() for n in range(7)]
            assert rows(".slot .title") == [["Customer call"], ["Design review", "Sales pipeline"], [], ["Offsite lunch"], [], [], []]
            assert rows(".slot .who") == [["Zoom"], ["Google Meet", "Zoom"], [], [], [], [], []]       # the platform in small type; none for a booking with no link
            assert rows(".slot time")[1] == ["10:00 AM", "2:30 PM"] and rows(".slot time")[3] == ["12:00 PM"]
            assert all(re.match(TWELVE_HOUR, t) for column in rows(".slot time") for t in column)
            assert rows(".none") == [[], [], ["Nothing booked"], [], ["Nothing booked"], ["Nothing booked"], ["Nothing booked"]]   # a day with no bookings says so
            assert page.locator("#chip-text").inner_text().startswith("Free until") and page.locator("#room-name").inner_text() == "Boardroom"   # the status stays
            assert page.locator("#week-back").inner_text() == "Back" and visible_buttons(page) == ["week-back"]
            boxes = [days.nth(n).bounding_box() for n in range(7)]
            week_head = page.locator(".week-head").bounding_box()
            assert all(abs(week_head[k] - today_head[k]) <= 1 for k in ("x", "y", "width")), (width, height, week_head, today_head)   # the head stands where the Today list's did, in both orientations: Back is where This week was
            if width > height:                                              # landscape: side by side in one row, the same width
                assert all(abs(b["y"] - boxes[0]["y"]) <= 0.5 and abs(b["height"] - boxes[0]["height"]) <= 0.5 for b in boxes)
                assert all(abs(b["width"] - boxes[0]["width"]) <= 1 for b in boxes)
                assert all(a["x"] + a["width"] <= b["x"] for a, b in zip(boxes, boxes[1:]))
            else:                                                           # portrait: stacked, one under the next; the days with bookings share the height, a day with nothing booked is as tall as its words
                assert all(abs(b["x"] - boxes[0]["x"]) <= 0.5 and abs(b["width"] - boxes[0]["width"]) <= 0.5 for b in boxes)
                booked = days.evaluate_all("days => days.map((d) => d.querySelector('.slots') !== null)")
                busy = [b["height"] for b, has in zip(boxes, booked) if has]
                empty = [b["height"] for b, has in zip(boxes, booked) if not has]
                assert busy and empty and max(busy) - min(busy) <= 1 and max(empty) - min(empty) <= 1 and max(empty) < min(busy), (busy, empty)
                assert all(a["y"] + a["height"] <= b["y"] for a, b in zip(boxes, boxes[1:]))
                assert abs(boxes[0]["y"] - (week_head["y"] + week_head["height"]) - 10) <= 1       # the head and the days, ten pixels apart
            assert all(b["x"] >= 0 and b["x"] + b["width"] <= width and b["y"] >= 0 and b["y"] + b["height"] <= height for b in boxes)
            assert fits(page)
            page.click("#week-back")                                         # Back: the Today list again
            assert page.locator("#week").is_hidden() and page.locator("#today").is_visible() and page.evaluate("document.body.dataset.view") == "today"
            assert page.locator("#events .event").count() == 1 and visible_buttons(page) == ["this-week"] and fits(page)
            open_the_week(page)                                              # and again: asked for afresh, shown afresh
            assert len(asked) == 2 and page.locator("#week .day").count() == 7
            assert errors == []
            page.close()


def test_the_week_view_returns_to_today_after_thirty_idle_seconds(browser):
    """On the page's own clock, stopped: each jump below lets every timer that falls due in it fire once, at its end."""
    skip_if_day_ends_within(1)
    with PageServer(calendar_events=[event("e1", "Design review", 40), event_on("e2", "Sales pipeline", 2, 9, 0)]) as server:
        page = sign_on_a_stopped_clock(browser, server)
        view = lambda: page.evaluate("document.body.dataset.view")

        def pass_seconds(seconds):
            page.clock.fast_forward(seconds * 1000)
            page.wait_for_timeout(300)

        open_the_week(page)
        pass_seconds(29)
        assert view() == "week"                                              # 29 seconds without a tap: still there ...
        pass_seconds(2)
        assert view() == "today" and page.locator("#today").is_visible() and page.locator("#week").is_hidden()   # ... and at 31 it is back on Today
        assert page.locator("#events .event").count() == 1 and visible_buttons(page) == ["this-week"]

        open_the_week(page)                                                  # a tap inside the week view starts the 30 seconds again
        pass_seconds(25)
        page.click("#week .day >> nth=2")
        pass_seconds(29)
        assert view() == "week"                                              # 54 seconds since it opened, 29 since the tap
        pass_seconds(2)
        assert view() == "today"                                             # 31 since the tap

        open_the_week(page)                                                  # Back puts the wait away: the next visit has its own
        pass_seconds(10)
        page.click("#week-back")
        assert view() == "today"
        pass_seconds(10)
        open_the_week(page)
        pass_seconds(29)
        assert view() == "week"                                              # 49 seconds after the first visit began: its wait would have closed this one at 30
        pass_seconds(2)
        assert view() == "today"
        page.close()


def test_a_week_that_is_loading_or_cannot_be_loaded_says_so_and_back_still_works(browser):
    skip_if_day_ends_within(1)
    with PageServer(calendar_events=[event("e1", "Design review", 40)]) as server:
        page = open_sign(browser, server)
        errors, held = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/api/calendar/week", lambda route: held.append(route))           # not answered until the test says
        page.click("#this-week")
        page.wait_for_function("document.body.dataset.view === 'week'", timeout=5000)
        for _ in range(50):                                                  # the request reaches the test a moment after the tap
            if held:
                break
            page.wait_for_timeout(100)
        assert len(held) == 1 and page.locator("#week-note").inner_text() == "Loading the week." and page.locator("#week .day").count() == 0
        assert visible_buttons(page) == ["week-back"] and page.locator("#week-days").is_hidden() and fits(page)    # Back is there while the days are not
        held[0].abort()                                                              # the request fails
        page.wait_for_function("document.getElementById('week-note').innerText === 'The week could not be loaded.'", timeout=5000)
        assert page.locator("#week .day").count() == 0
        assert page.evaluate("document.body.dataset.view") == "week" and visible_buttons(page) == ["week-back"] and fits(page)
        page.click("#week-back")
        assert page.locator("#today").is_visible() and page.locator("#week").is_hidden()
        page.unroute("**/api/calendar/week")
        open_the_week(page)                                                  # the next visit tries again, and the note is gone
        assert page.locator("#week-note").inner_text() == "" and page.locator("#week-note").is_hidden()
        assert errors == []
        page.close()


def test_the_weeks_first_day_leaves_out_what_has_ended_and_rings_what_is_on(browser):
    skip_if_day_ends_within(2)
    now = datetime.now().astimezone()
    if now - now.replace(hour=0, minute=0, second=0, microsecond=0) < timedelta(hours=1):
        pytest.skip("less than an hour has passed in the local day")        # the finished booking began 50 minutes ago
    ended, on, later = event("e1", "Finished standup", -50, duration=30), event("e2", "Design review", -10, duration=40), event("e3", "Sales pipeline", 90)
    with PageServer(calendar_events=[ended, on, later]) as server:
        page = open_sign(browser, server, state="occupied")
        open_the_week(page)
        today = page.locator("#week .day").first
        assert today.locator(".slot .title").all_text_contents() == ["Design review", "Sales pipeline"]   # what is over is not on the sign's week
        assert today.locator(".slot.now .title").all_text_contents() == ["Design review"] and page.locator("#week .slot.now").count() == 1
        assert today.locator(".slot.now").evaluate("e => getComputedStyle(e).boxShadow") == "rgb(229, 72, 77) 0px 0px 0px 2px inset"   # ringed as on the Today list
        assert today.locator(".slot:not(.now)").evaluate("e => getComputedStyle(e).boxShadow") == "none"
        page.close()
    with PageServer(calendar_events=[ended]) as server:                      # nothing left today
        page = open_sign(browser, server)
        open_the_week(page)
        assert page.locator("#week .day").first.locator(".none").all_text_contents() == ["Nothing booked"]
        page.close()


def test_the_week_view_goes_when_the_calendar_does_and_comes_back_with_it(browser):
    """The week needs the calendar as the Today list does: with it gone the sign shows neither, and the view a person left open is
    there again when the calendar returns."""
    skip_if_day_ends_within(1)
    with PageServer(calendar_events=[event("e1", "Design review", 40)]) as server:
        page = sign_on_a_stopped_clock(browser, server)
        open_the_week(page)

        def without_the_calendar(route):
            body = route.fetch().json()
            body["calendar"]["connected"] = False
            route.fulfill(json=body)

        page.route("**/api/status", without_the_calendar)
        poll(page)
        assert page.locator("#week").is_hidden() and page.locator("#today").is_hidden() and page.evaluate("document.body.dataset.view") == "today"
        page.unroute("**/api/status")
        poll(page)
        assert page.locator("#week").is_visible() and page.locator("#week .day").count() == 7 and page.locator("#today").is_hidden()
        page.close()


def week_fit(page):
    """What each day of the open week view shows: [bookings, shown, the "+N more" text or None, clipped], clipped when a day's
    bookings stick out of its box."""
    return page.evaluate("""() => [...document.querySelectorAll('#week .day')].map((day) => {
        const slots = day.querySelector('.slots'), rows = [...day.querySelectorAll('.slot')], more = day.querySelector('.more');
        return [rows.length, rows.filter((row) => !row.hidden).length, more && !more.hidden ? more.textContent : null, slots ? slots.scrollHeight > slots.clientHeight : false];
    })""")


FULL_WEEK_TITLES = ["Standup", "Design review", "Quarterly planning review with the regional leadership teams", "Customer call", "Sales pipeline", "Vendor demo",
                    "Board meeting", "Wrap up"]


def a_full_week():
    """Three bookings today, an hour apart from 40 minutes out, and eight on each of the six days after it."""
    return ([event(f"t{n}", f"Today {n}", 40 + 60 * n) for n in range(3)]
            + [event_on(f"d{d}{n}", FULL_WEEK_TITLES[n], d, 8 + n, 0) for d in range(1, 7) for n in range(8)])


def test_the_week_view_fits_a_full_week_at_every_size_and_counts_what_it_leaves_out(browser):
    """A day shows the bookings that fit its box, whole (none cut across), and "+N more" says how many it left out. A day with the
    three bookings the Today list holds shows all three, at both sizes of the sign."""
    skip_if_day_ends_within(3)
    for width, height, least in ((1280, 800, 3), (800, 1280, 2), (1920, 1080, 2)):
        with PageServer(calendar_events=a_full_week(), room_name="Boardroom", room_location="2nd floor, by the kitchen") as server:
            page = open_sign(browser, server, width, height)
            open_the_week(page)
            assert fits(page), (width, height)
            week = week_fit(page)
            assert week[0] == [3, 3, None, False], (width, height, week[0])                  # today's three: all of them, and no "+N more"
            for rows, shown, more, clipped in week[1:]:
                assert rows == 8 and shown >= least and not clipped, (width, height, rows, shown, clipped)
                assert more == f"+{rows - shown} more", (width, height, shown, more)         # what is left out is counted
            page.close()


def test_the_week_view_fits_a_full_week_with_long_names_on_every_sign(browser):
    """The worst case for room: a room name over two lines, a long location, company and titles, on the signs from a small kiosk to a
    full HD one. Fewer bookings show, and none is cut across or runs off the sign."""
    skip_if_day_ends_within(3)
    with PageServer(calendar_events=a_full_week(), room_name="Conference Room Twelve", room_location="2nd floor, by the kitchen, past the elevators",
                    room_company="Lone Star Eye Care and Surgery Partners") as server:
        for width, height in ((1280, 800), (800, 1280), (1920, 1080), (1024, 600), (1080, 1920)):
            page = open_sign(browser, server, width, height)
            open_the_week(page)
            assert fits(page), (width, height)
            for n, (rows, shown, more, clipped) in enumerate(week_fit(page)):
                assert not clipped, (width, height, n)
                assert more == (f"+{rows - shown} more" if shown < rows else None), (width, height, n, rows, shown, more)
            boxes = page.locator("#week .day").evaluate_all("days => days.map((d) => { const b = d.getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom]; })")
            assert all(left >= 0 and top >= 0 and right <= width and bottom <= height for left, top, right, bottom in boxes), (width, height)
            page.close()


def test_the_week_view_refits_when_the_sign_is_turned(browser):
    """A day's bookings are fitted to the box it has: turned on its stand, the sign shows what fits the new box, more or fewer than
    before, as a sign loaded in that position would."""
    skip_if_day_ends_within(3)
    landscape, portrait = {"width": 1280, "height": 800}, {"width": 800, "height": 1280}
    with PageServer(calendar_events=a_full_week(), room_name="Boardroom") as server:
        fresh = {}
        for name, size in (("landscape", landscape), ("portrait", portrait)):
            page = open_sign(browser, server, **size)
            open_the_week(page)
            fresh[name] = week_fit(page)
            page.close()
        assert fresh["landscape"] != fresh["portrait"]                          # the two boxes do hold different numbers
        page = open_sign(browser, server, **landscape)
        open_the_week(page)
        for name, size in (("portrait", portrait), ("landscape", landscape), ("portrait", portrait)):
            page.set_viewport_size(size)
            for _ in range(50):
                if week_fit(page) == fresh[name]:
                    break
                page.wait_for_timeout(100)
            assert week_fit(page) == fresh[name], name
            assert fits(page), name
        page.close()


@pytest.mark.parametrize("width, height", [(1280, 800), (800, 1280)])
def test_the_week_view_gives_way_to_the_scoreboard_strip_when_it_comes_up(browser, width, height, central_time):
    """The strip (spec section 2.5) takes the bottom of the sign when a school is on and has a score: the days refit above it and nothing
    runs under it, while the status, the head and the tops of the days stay exactly where they were."""
    skip_if_day_ends_within(3)
    with PageServer(calendar_events=a_full_week(), room_name="Boardroom", room_location="2nd floor, by the kitchen", schedules=every_school()) as server:
        page = sign_on_a_stopped_clock(browser, server, width, height)      # the poll that brings the strip is the test's to bring
        open_the_week(page)
        before, kept = week_fit(page), boxes(page, ".top", ".status", ".week-head")
        tops = page.locator("#week .day").evaluate_all("days => days.map((d) => d.getBoundingClientRect().top)")
        choose(server, sign_scores=["texas"])
        scoreboard_has(server, ["texas"])
        poll(page)
        assert page.evaluate("!document.getElementById('score').hidden")
        for _ in range(50):                                                  # the box shrinks at once; the days are fitted to it a moment later
            after = week_fit(page)
            if after != before and not any(clipped for _, _, _, clipped in after):
                break
            page.wait_for_timeout(100)
        assert sum(shown for _, shown, _, _ in after) < sum(shown for _, shown, _, _ in before)           # less room, so fewer bookings
        assert not any(clipped for _, _, _, clipped in after) and fits(page)
        assert boxes(page, ".top", ".status", ".week-head") == kept          # nothing above the days moved
        now = page.locator("#week .day").evaluate_all("days => days.map((d) => d.getBoundingClientRect().top)")
        assert now[0] == tops[0] and (now == tops if width > height else True)   # the first day starts where it did; side by side they all do (stacked, the rows share what is left)
        assert page.locator("#week-days").evaluate("e => e.getBoundingClientRect().bottom") <= page.locator("#score").evaluate("e => e.getBoundingClientRect().top")
        page.close()


def test_a_poll_that_changes_nothing_redraws_nothing_in_the_week(browser):
    skip_if_day_ends_within(1)
    with PageServer(calendar_events=[event("e1", "Design review", 40), event_on("e2", "Sales pipeline", 2, 9, 0)]) as server:
        page = sign_on_a_stopped_clock(browser, server)
        open_the_week(page)
        page.evaluate("""() => {
            window.records = [];
            new MutationObserver((list) => list.forEach((r) => window.records.push(r.type + ':' + (r.attributeName || '') + ':' + r.target.nodeName)))
                .observe(document.getElementById('week'), { subtree: true, childList: true, characterData: true, attributes: true });
        }""")
        poll(page)
        poll(page)
        assert page.evaluate("window.records") == []                             # the same week: the days are not drawn again
        assert page.locator("#week .day").count() == 7
        page.close()


@pytest.mark.parametrize("zone", ["America/Los_Angeles", "Pacific/Auckland"])
def test_the_weeks_dates_are_the_rooms_days_whatever_zone_the_browser_keeps(browser, zone):
    """"2026-10-14" is a day, not a moment: read as midnight UTC it would be the evening of the 13th in Los Angeles."""
    skip_if_day_ends_within(1)                                              # the names and dates below are today's, and the page asks for its own
    names, dates = the_next_seven_days()
    with PageServer() as server:
        page = open_sign(browser, server, timezone_id=zone)
        open_the_week(page)
        assert page.locator("#week .day-name").all_text_contents() == names
        assert page.locator("#week .day-date").all_text_contents() == dates
        page.close()


def test_the_week_view_is_opened_kept_and_left_by_touch(browser):
    """The hallway screen is a touchscreen: its taps are touches, which must open the week, keep it open and put it away."""
    skip_if_day_ends_within(1)
    with PageServer(calendar_events=[event("e1", "Design review", 40)]) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800}, has_touch=True)
        page.clock.install()
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        page.clock.pause_at(datetime.now() + timedelta(seconds=2))
        view = lambda: page.evaluate("document.body.dataset.view")
        page.tap("#this-week")
        page.wait_for_function("document.body.dataset.view === 'week' && document.querySelectorAll('#week .day').length === 7", timeout=5000)
        page.clock.fast_forward(25_000)
        page.wait_for_timeout(300)
        page.tap("#week .day >> nth=1")                                      # a touch inside the week view starts the 30 seconds again
        page.clock.fast_forward(29_000)
        page.wait_for_timeout(300)
        assert view() == "week"                                              # 54 seconds since it opened, 29 since the touch
        page.clock.fast_forward(2_000)
        page.wait_for_timeout(300)
        assert view() == "today"
        page.tap("#this-week")
        page.wait_for_function("document.body.dataset.view === 'week'", timeout=5000)
        page.tap("#week-back")
        assert view() == "today" and page.locator("#today").is_visible()
        page.close()


def test_the_week_view_can_be_opened_and_left_from_the_keyboard(browser):
    """Focus goes where the person is: to Back when the week opens, and to This week when it closes."""
    with PageServer() as server:
        page = open_sign(browser, server)
        page.focus("#this-week")
        page.keyboard.press("Enter")
        page.wait_for_function("document.body.dataset.view === 'week'", timeout=5000)
        assert page.evaluate("document.activeElement.id") == "week-back"
        page.keyboard.press("Enter")
        assert page.evaluate("document.body.dataset.view") == "today" and page.evaluate("document.activeElement.id") == "this-week"
        page.close()


def test_the_weeks_ring_is_on_the_one_booking_that_ends_first_and_only_todays_column_has_the_outline(browser):
    """Two bookings in progress together (neither has a video link, so the sign itself decides which one the room is in): the ring goes
    to the one that ends first, once, in today's column, as it does on the Today list; the bookings of the other days, three of which
    have some, are never ringed; and today's column alone has the thin outline."""
    skip_if_day_ends_within(3)
    now = datetime.now().astimezone()
    if now - now.replace(hour=0, minute=0, second=0, microsecond=0) < timedelta(hours=1):
        pytest.skip("less than an hour has passed in the local day")        # the long booking began 50 minutes ago
    in_person = {"url": None, "platform": MeetingPlatform.UNKNOWN}
    bookings = [event("long", "Long review", -50, duration=180, **in_person),   # 50 minutes in, 130 to go
                event("short", "Short sync", -20, duration=40, **in_person),    # 20 minutes in, 20 to go: it ends first
                event("later", "Sales pipeline", 100),
                event_on("t1", "Design review", 1, 10, 0), event_on("t2", "Planning", 1, 14, 0),
                event_on("w1", "Offsite lunch", 2, 12, 0, **in_person), event_on("w2", "Vendor demo", 4, 9, 30)]
    with PageServer(calendar_events=bookings) as server:
        page = open_sign(browser, server, state="occupied")
        assert page.locator("#events .event.now .title").all_text_contents() == ["Short sync"]      # the Today list rings it ...
        open_the_week(page)
        days = page.locator("#week .day")
        assert days.first.locator(".slot .title").all_text_contents() == ["Long review", "Short sync", "Sales pipeline"]
        assert [days.nth(n).locator(".slot").count() for n in range(7)] == [3, 2, 1, 0, 1, 0, 0]   # bookings on today and on three other days
        assert page.locator("#week .slot.now").count() == 1                 # ... and the week rings it, once, whatever the other days hold
        assert days.first.locator(".slot.now .title").all_text_contents() == ["Short sync"]
        outlines = days.evaluate_all("""days => days.map((day) => {
            const style = getComputedStyle(day);
            return [style.outlineStyle, style.outlineWidth, style.outlineColor, style.outlineOffset];
        })""")
        assert outlines[0] == ["solid", "1px", "rgba(255, 255, 255, 0.4)", "-1px"]    # today's own: 1 px, drawn inside the panel
        assert [outline[0] for outline in outlines[1:]] == ["none"] * 6                # and no other day has one
        page.close()


def test_a_second_visit_to_the_week_whose_request_fails_shows_the_failure_and_none_of_the_first_visits_days(browser):
    """What the first visit drew is not kept for the next: a visit that cannot be loaded says so, with no days under the note."""
    skip_if_day_ends_within(1)
    with PageServer(calendar_events=[event("e1", "Design review", 40), event_on("e2", "Sales pipeline", 2, 9, 0)]) as server:
        page = open_sign(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_the_week(page)
        assert page.locator("#week .slot").count() == 2                      # the first visit has bookings to show
        page.click("#week-back")
        page.route("**/api/calendar/week", lambda route: route.abort())
        page.click("#this-week")
        page.wait_for_function("document.getElementById('week-note').innerText === 'The week could not be loaded.'", timeout=5000)
        assert page.locator("#week .day").count() == 0 and page.locator("#week .slot").count() == 0
        assert page.evaluate("document.body.dataset.view") == "week" and visible_buttons(page) == ["week-back"] and fits(page)
        page.click("#week-back")
        page.unroute("**/api/calendar/week")
        open_the_week(page)                                                  # and the visit after it, with its request answered, is whole again
        assert page.locator("#week-note").inner_text() == "" and page.locator("#week .slot").count() == 2
        assert errors == []
        page.close()


def test_the_week_stays_up_while_the_sign_cannot_reach_the_room_and_back_and_the_return_still_work(browser):
    """Under "Sign not connected" the week that was on screen stays (nothing it shows has changed), Back puts the Today list in its
    place, and a week opened then goes back to Today after thirty seconds, all without the room."""
    skip_if_day_ends_within(1)
    with PageServer(calendar_events=[event("e1", "Design review", 40), event_on("e2", "Sales pipeline", 2, 9, 0)]) as server:
        page = sign_on_a_stopped_clock(browser, server)
        view = lambda: page.evaluate("document.body.dataset.view")

        def pass_seconds(seconds):
            page.clock.fast_forward(seconds * 1000)
            page.wait_for_timeout(300)

        open_the_week(page)
        page.route("**/api/status", lambda route: route.abort())
        with page.expect_event("requestfailed"):
            page.clock.run_for(5000)                                         # the next poll fails: the room has gone
        page.wait_for_function("document.body.dataset.state === 'offline'", timeout=5000)
        assert page.locator("#chip-text").inner_text() == "Sign not connected"
        assert view() == "week" and page.locator("#week .day").count() == 7 and page.locator("#week").is_visible()   # the week it had stays
        assert visible_buttons(page) == ["week-back"]
        page.click("#week-back")                                             # Back works without the room
        assert view() == "today" and page.locator("#today").is_visible() and visible_buttons(page) == ["this-week"]
        open_the_week(page)                                                  # and so does a new visit, which the thirty seconds close
        pass_seconds(29)
        assert view() == "week" and page.locator("#chip-text").inner_text() == "Sign not connected"
        pass_seconds(2)
        assert view() == "today" and page.locator("#today").is_visible() and page.locator("#week").is_hidden()
        page.close()


@pytest.mark.parametrize("width, height", [(390, 844), (600, 1000), (800, 1280), (1080, 1920)])
def test_in_portrait_the_weeks_head_stands_where_todays_did_at_every_width(browser, width, height):
    """The space between the status and what follows it grows with the width (24 px up to 64): the week's head keeps to that same
    space, so that Back is where This week was, and the days start ten pixels under the head."""
    skip_if_day_ends_within(1)
    with PageServer(calendar_events=[event("e1", "Design review", 40)], room_name="Boardroom") as server:
        page = open_sign(browser, server, width, height)
        today_head = page.locator(".today-head").bounding_box()
        open_the_week(page)
        head, days = page.locator(".week-head").bounding_box(), page.locator("#week-days").bounding_box()
        assert all(abs(head[k] - today_head[k]) <= 1 for k in ("x", "y", "width")), (head, today_head)
        assert abs(days["y"] - (head["y"] + head["height"]) - 10) <= 1, (head, days)
        page.close()


def test_a_weeks_booking_has_one_size_of_type_for_its_time_and_title_and_the_signs_panel_fill(browser):
    skip_if_day_ends_within(1)
    with PageServer(calendar_events=[event("e1", "Design review", 40)]) as server:
        page = open_sign(browser, server)
        open_the_week(page)
        slot = page.locator("#week .slot").first
        style = lambda selector, prop: slot.locator(selector).evaluate(f"e => getComputedStyle(e).{prop}")
        assert style("time", "fontSize") == style(".title", "fontSize") == "14px"      # one size, not 13 beside 14
        assert style(".who", "fontSize") == "12px"                                      # the platform stays in small type
        assert slot.evaluate("e => getComputedStyle(e).backgroundColor") == "rgba(255, 255, 255, 0.06)"   # the sign's one panel fill
        page.close()


def test_a_weeks_day_with_nothing_booked_keeps_its_words_on_one_line_on_a_small_screen(browser):
    """At 800 by 480 a day is about 95 px wide, less than "Nothing booked" asks for: the words are cut with an ellipsis, not wrapped."""
    with PageServer(room_name="Boardroom") as server:
        page = open_sign(browser, server, 800, 480)
        open_the_week(page)
        nones = page.locator("#week .none")
        assert nones.count() == 7
        assert nones.first.evaluate("e => { const s = getComputedStyle(e); return [s.whiteSpace, s.overflow, s.textOverflow]; }") == ["nowrap", "hidden", "ellipsis"]
        lines = nones.evaluate_all("els => els.map((e) => e.getBoundingClientRect().height / parseFloat(getComputedStyle(e).lineHeight))")
        assert all(abs(n - 1) < 0.01 for n in lines), lines                 # one line each, though the words are wider than the day
        assert nones.first.evaluate("e => e.scrollWidth > e.clientWidth")
        assert fits(page)
        page.close()


@pytest.mark.parametrize("room, location", [("Boardroom", "2nd floor, by the kitchen"), ("Conference Room Twelve", "2nd floor, by the kitchen, past the elevators")])
def test_on_a_short_screen_the_week_gives_up_the_status_detail_and_the_dates_to_keep_its_plus_n_more(browser, room, location):
    """Up to 560 px of height the status's detail line and the days' dates are not shown while the week is, so that the days have the room for the count of
    what each leaves out ("+8 more"), which a day's box clips when it has none; the Today list brings the detail back. The dates come back at 561 px, and
    the detail (with the location and the name's size, by height alone: the next test) stays away to 700."""
    skip_if_day_ends_within(3)
    with PageServer(calendar_events=a_full_week(), room_name=room, room_location=location) as server:
        for width, height in ((800, 480), (900, 500), (1024, 560)):
            page = open_sign(browser, server, width, height)
            assert page.locator("#detail").is_visible()                      # on the Today list the detail is there
            open_the_week(page)
            assert page.locator("#detail").is_hidden() and page.locator("#week .day-date").first.is_hidden(), (width, height)
            assert fits(page), (width, height)
            counts = page.evaluate("""() => [...document.querySelectorAll('#week .day')].map((day) => {
                const more = day.querySelector('.more');
                if (!more || more.hidden) return null;
                const inside = more.getBoundingClientRect(), box = day.getBoundingClientRect();
                return [inside.top >= box.top && inside.bottom <= box.bottom, more.textContent];
            })""")
            assert all(count is not None for count in counts[1:]) and all(count[0] for count in counts[1:]), (room, width, height, counts)   # every busy day shows its count, whole
            page.click("#week-back")
            assert page.locator("#detail").is_visible(), (width, height)
            page.close()
        page = open_sign(browser, server, 1024, 561)                         # one pixel more height and the dates are back; the detail is not
        open_the_week(page)
        assert page.locator("#week .day-date").first.is_visible() and page.locator("#detail").is_hidden() and fits(page)
        page.close()
        page = open_sign(browser, server, 1024, 701)                         # and above 700 px the detail stays
        open_the_week(page)
        assert page.locator("#detail").is_visible() and fits(page)
        page.close()


# --- the week on a short landscape sign, and with empty days in portrait (the whole-branch review) ---

WEEK_SIGNS = ((800, 480), (900, 500), (1024, 600), (1280, 600), (1366, 620))
ROOM_WITH_A_LONG_LOCATION = {"room_name": "Conference Room Twelve", "room_location": "2nd floor, by the kitchen, past the elevators"}


def day_names_are_whole(page):
    """Whether every day's name is drawn whole: not cut at its end with an ellipsis."""
    return page.locator("#week .day-name").evaluate_all("names => names.every((n) => n.scrollWidth <= n.clientWidth)")


@pytest.mark.parametrize("scores", [[], ["texas"]], ids=["strip down", "strip up"])
def test_on_a_short_landscape_sign_the_week_shows_todays_bookings_and_the_days_names_whole(browser, scores, central_time):
    """At 800 by 480 and 1024 by 600 today, with three bookings, showed "+3 more" and not one of them (strip up), or one in full (strip down), and "Wednesday"
    was cut to "Wedne...". By height alone, up to 700 px, the status gives up its detail, its location and some of the name's size, a booking is the time
    and a title of one line, and a day under 1000 px wide is named by its first three letters. Today's column shows its bookings, in every one of five
    sizes, and the sign is whole."""
    skip_if_day_ends_within(3)
    names, _ = the_next_seven_days()
    with PageServer(calendar_events=a_full_week(), schedules=every_school(), **ROOM_WITH_A_LONG_LOCATION) as server:
        choose(server, sign_scores=scores)
        if scores:
            scoreboard_has(server, scores)
        for width, height in WEEK_SIGNS:
            page = open_sign(browser, server, width, height)
            if scores:
                strip_is_up(page)
            open_the_week(page)
            page.wait_for_timeout(200)
            where = (bool(scores), width, height)
            assert fits(page), (*where, page.evaluate("document.documentElement.scrollHeight") - height)
            today = week_fit(page)[0]
            assert today[0] == 3 and today[1] >= 1 and not today[3], (*where, today)                      # at least one of today's three bookings shows, whole
            assert today[2] == (f"+{3 - today[1]} more" if today[1] < 3 else None), (*where, today)
            assert page.locator("#week .day-name").all_text_contents() == [n[:3] if width < 1000 else n for n in names], where
            assert day_names_are_whole(page), where
            page.close()


def test_the_days_are_named_by_three_letters_on_a_landscape_sign_narrower_than_1000_px_and_in_full_on_every_other(browser, central_time):
    """"Wednesday" is wider than a seventh of a sign under 1000 px wide, in landscape: there the days are "Mon" to "Sun", every one of them whole, with the
    whole name for a screen reader. Every other sign, upright ones included, names them in full, and the names follow the sign when it is resized."""
    skip_if_day_ends_within(1)
    names, _ = the_next_seven_days()
    short = [n[:3] for n in names]
    with PageServer(calendar_events=[event("e1", "Design review", 40)], room_name="Boardroom") as server:
        for width, height, abbreviated in ((800, 480, True), (999, 600, True), (1000, 600, False), (1024, 600, False), (1280, 800, False), (800, 1280, False), (999, 1000, False)):      # an upright sign names its days in full: they are rows there
            page = open_sign(browser, server, width, height)
            open_the_week(page)
            assert page.locator("#week .day-name").all_text_contents() == (short if abbreviated else names), (width, height)
            assert day_names_are_whole(page), (width, height)
            assert page.locator("#week .day-name").evaluate_all("names => names.map((n) => n.getAttribute('aria-label'))") == (names if abbreviated else [None] * 7)
            page.close()
        page = sign_on_a_stopped_clock(browser, server, 800, 480, init_script="document.fonts.addEventListener = () => {};")
        open_the_week(page)
        for width, height, abbreviated in ((1280, 800, False), (800, 480, True), (1000, 480, False), (999, 480, True)):
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(150)                                                       # the page's own timers are stopped: this is the test's time
            assert page.locator("#week .day-name").all_text_contents() == (short if abbreviated else names), (width, height)
        page.close()


@pytest.mark.parametrize("width, height, compact", [(1280, 700, True), (1280, 701, False), (800, 480, True), (1024, 600, True), (1920, 1080, False), (800, 1280, False)])
def test_up_to_700_px_high_a_landscape_weeks_booking_is_the_time_and_one_line_of_title_and_has_no_platform(browser, width, height, compact, central_time):
    """The full booking (the time, a title of two lines, the platform) is 86 px; the compact one is 46. A sign standing upright keeps the platform at any height,
    and its days are rows, with the title on the time's line."""
    skip_if_day_ends_within(3)
    title = "Quarterly planning review with the regional leadership teams and the partners, the board and the finance group"
    with PageServer(calendar_events=[event_on(f"d{n}", title, 1, 9 + n, 0) for n in range(2)], room_name="Boardroom") as server:
        page = open_sign(browser, server, width, height)
        open_the_week(page)
        page.wait_for_timeout(200)
        slot = page.locator("#week .slot:not([hidden])").first
        assert slot.locator(".who").is_hidden() == compact, (width, height)                                           # the platform: not in the compact slot
        lines = lines_of(page, "#week .slot:not([hidden]) .title")
        assert set(lines) == {1 if compact or width < height else 2}, (width, height, lines)                         # a title of one line, but in the full slot of a landscape sign
        if compact:
            box = slot.bounding_box()
            assert 40 <= box["height"] <= 52, (width, height, box)                                                    # the time over one line of title, in its own pill
        page.close()


@pytest.mark.parametrize("scores", [[], ["texas"]], ids=["strip down", "strip up"])
def test_a_long_room_name_in_the_week_on_a_short_sign_steps_down_and_the_sign_fits(browser, scores, central_time):
    """The week sets the name in a size of its own on a short sign; a name of 67 characters there is stepped down as it is in Today, to two lines."""
    skip_if_day_ends_within(3)
    with PageServer(calendar_events=a_full_week(), room_name=FLOOR_NAMES[67], room_location=TWO_LINE_LOCATION, schedules=every_school()) as server:
        choose(server, sign_scores=scores)
        if scores:
            scoreboard_has(server, scores)
        page = open_sign(browser, server, 800, 480)
        if scores:
            strip_is_up(page)
        open_the_week(page)
        page.wait_for_timeout(200)
        size, stepped, lines = room_name_type(page)
        assert lines <= 2 and fits(page) and week_fit(page)[0][1] >= 1, (size, stepped, lines, week_fit(page)[0])
        assert page.locator("#room-name").inner_text() == FLOOR_NAMES[67]                       # whole: smaller, never cut
        page.close()


@pytest.mark.parametrize("scores", [[], ["texas"]], ids=["strip down", "strip up"])
def test_in_portrait_the_weeks_days_with_nothing_booked_take_one_line_and_the_others_share_the_rest(browser, scores, central_time):
    """Seven rows of one seventh each gave "Nothing booked" a whole row and a day with bookings a seventh of the height, "+N more" and little else. Now a day
    with nothing booked is the height of its own words (the name and the date), and the days with bookings share what is left, equally."""
    skip_if_day_ends_within(3)
    bookings = [event(f"t{n}", f"Today {n}", 40 + 60 * n) for n in range(3)] + [event_on(f"m{d}{n}", f"Meeting {n}", d, 9 + n, 0) for d in (2, 5) for n in range(5)]
    with PageServer(calendar_events=bookings, room_name="Boardroom", schedules=every_school()) as server:
        choose(server, sign_scores=scores)
        if scores:
            scoreboard_has(server, scores)
        page = open_sign(browser, server, 800, 1280)
        if scores:
            strip_is_up(page)
        open_the_week(page)
        page.wait_for_timeout(200)
        heights = page.locator("#week .day").evaluate_all("days => days.map((d) => [d.getBoundingClientRect().height, d.querySelector('.slots') === null])")
        empty = [h for h, nothing in heights if nothing]
        busy = [h for h, nothing in heights if not nothing]
        assert len(empty) == 4 and len(busy) == 3, heights
        assert max(empty) <= 64 and max(empty) - min(empty) < 1, heights                                  # the name over the date: 40 px of words in a pill with its padding
        assert max(busy) - min(busy) < 1 and min(busy) > 135, heights                                     # three days share the rest of the height: 105 px each when all seven were equal
        assert fits(page) and week_fit(page)[0][1] == 3, (week_fit(page)[0], heights)                     # today's three bookings are shown in full
        page.close()


# --- the scoreboard strip (spec 2026-10-13, section 2.5): the last score and the next game of each school the room has on, along the bottom ---

BURNT_ORANGE = "rgb(255, 179, 102)"      # #FFB366, the rank: the one colour the strip adds to the sign's
PERIWINKLE = "rgb(189, 206, 255)"         # #BDCEFF
WHITE = "rgb(255, 255, 255)"
TEXAS_LAST, TEXAS_NEXT = "Texas (#4) 45 \u2013 Texas A&M 42", "Next: 10/19 \u00b7 Texas (#4) @ Missouri (#9)"          # the spec's own words
MICHIGAN_LAST, MICHIGAN_NEXT = "Michigan (#12) 31 \u2013 Ohio State 24", "Next: 10/24 \u00b7 Michigan (#12) @ Purdue"


def status_of(server):
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/api/status", timeout=5) as response:
        return json.loads(response.read())


def wait_for(what, condition, seconds=5):
    for _ in range(int(seconds / 0.05)):
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def scoreboard_has(server, schools):
    """Waits until exactly `schools` have something to show (the server's own loop reads a school a moment after it is turned on)."""
    wait_for(f"the scoreboard to hold {schools}", lambda: [e["school"] for e in status_of(server).get("scores", []) if e["last"] or e["next"]] == schools)


def strip_text(page):
    return [page.locator("#score-last").inner_text(), page.locator("#score-next").inner_text()]


def strip_is_up(page):
    page.wait_for_function("!document.getElementById('score').hidden", timeout=8000)      # the sign's own poll (5 s) brings it


def boxes(page, *selectors):
    """Where each element of each selector is, as [x, y, width, height] in whole tenths of a pixel."""
    return page.evaluate("""(selectors) => selectors.map((s) => [...document.querySelectorAll(s)].map((e) => {
        const r = e.getBoundingClientRect();
        return [r.x, r.y, r.width, r.height].map((v) => Math.round(v * 10) / 10);
    }))""", list(selectors))


def test_the_strip_is_hidden_until_a_school_is_on_and_nothing_is_read_for_a_room_that_has_none(browser):
    with PageServer(schedules=every_school()) as server:
        page = open_sign(browser, server)
        page.wait_for_timeout(300)                                           # the scoreboard's loop has looked a good many times
        assert page.locator("#score").is_hidden() and server.feed.asked == []
        assert "scores" not in status_of(server) and fits(page)
        page.close()


def test_the_strip_shows_the_last_score_and_the_next_game_in_the_specs_words_with_the_ranks_in_burnt_orange(browser, central_time):
    with PageServer(schedules=every_school()) as server:
        page = open_sign(browser, server)
        errors, urls = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: urls.append(r.url))
        choose(server, sign_scores=["texas"])
        strip_is_up(page)
        assert strip_text(page) == [TEXAS_LAST, TEXAS_NEXT]                  # one school: no name leading
        ranks = page.locator("#score .rank")
        assert ranks.all_inner_texts() == ["(#4)", "(#4)", "(#9)"]            # the ranks of both lines, and only a rank
        assert set(ranks.evaluate_all("els => els.map((e) => getComputedStyle(e).color)")) == {BURNT_ORANGE}
        inks = page.locator("#score").evaluate("""strip => { const seen = new Set(), walk = (node) => { for (const child of node.childNodes) {
            if (child.nodeType === 3 && child.textContent.trim()) seen.add(getComputedStyle(node).color); else if (child.nodeType === 1) walk(child); } };
            walk(strip); return [...seen]; }""")
        assert sorted(inks) == sorted([WHITE, PERIWINKLE, BURNT_ORANGE])     # white and periwinkle as elsewhere: the orange of the ranks is the one new colour
        assert page.locator("#score-last").evaluate("e => getComputedStyle(e).color") == WHITE
        assert page.locator("#score-next").evaluate("e => getComputedStyle(e).color") == PERIWINKLE
        assert page.locator("#score").evaluate("e => getComputedStyle(e).textTransform") == "none"      # no capitals
        assert fits(page) and errors == []
        assert urls and all(u.startswith(f"http://127.0.0.1:{server.port}/") for u in urls)             # the sign asks nobody but its own room
        page.close()


@pytest.mark.parametrize("width, height, root", [(1280, 800, 16), (1920, 1080, 24)])
def test_the_strip_is_set_in_the_signs_own_type_and_sits_flush_along_the_bottom_edge(browser, width, height, root, central_time):
    with PageServer(schedules=every_school()) as server:
        choose(server, sign_scores=["texas"])
        scoreboard_has(server, ["texas"])
        page = open_sign(browser, server, width, height)
        strip_is_up(page)
        style = lambda selector, prop: page.locator(selector).evaluate(f"e => getComputedStyle(e).{prop}")
        assert page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)") == root
        assert float(style("#score", "fontSize").removesuffix("px")) == pytest.approx(1.1 * root) and style("#score", "fontFamily").startswith("Lexend")   # 1.1 rem: between the sign's 1.05 and 1.15
        assert (style("#score-last", "fontWeight"), style("#score-next", "fontWeight")) == ("600", "400")                   # the score in the sign's bold, the next game in its regular
        assert style("#score", "backgroundColor") == "rgba(0, 0, 0, 0.35)"
        box = page.locator("#score").bounding_box()
        assert box["x"] == 0 and abs(box["x"] + box["width"] - width) <= 0.5 and abs(box["y"] + box["height"] - height) <= 0.5      # across the sign, on its bottom edge
        last, following = page.locator("#score-last").bounding_box(), page.locator("#score-next").bounding_box()
        assert abs(last["x"] - page.locator("#room-name").bounding_box()["x"]) <= 1                          # its words start where the others do, past the rail
        assert following["x"] >= last["x"] + last["width"]                                                  # the last game on the left, the next on the right ...
        margin = float(style("#score", "paddingRight").removesuffix("px"))
        assert abs(following["x"] + following["width"] - (width - margin)) <= 1                              # ... to the sign's right margin
        page.wait_for_function("document.body.dataset.photo === 'skyline'", timeout=5000)
        wait_for_the_photograph_to_paint(page)
        assert screenshot_pixel(page, 10, height - 10) == (31, 169, 113)    # the status rail runs on, solid, past the strip to the bottom edge (#1FA971)
        assert screenshot_pixel(page, 10, height // 2) == (31, 169, 113)
        page.close()


TEXAS_AM_LAST, TEXAS_AM_NEXT = "Texas A&M (#20) 21 \u2013 LSU (#18) 24", "Next: 10/17 \u00b7 Arkansas @ Texas A&M (#20)"
MISSOURI_LAST, MISSOURI_NEXT = "Missouri (#9) 35 \u2013 Vanderbilt 10", "Next: 10/19 \u00b7 Texas (#4) @ Missouri (#9)"
SCHOOL_LINES = {"Texas": ["Texas: " + TEXAS_LAST, TEXAS_NEXT], "Michigan": ["Michigan: " + MICHIGAN_LAST, MICHIGAN_NEXT],
                "Texas A&M": ["Texas A&M: " + TEXAS_AM_LAST, TEXAS_AM_NEXT], "Missouri": ["Missouri: " + MISSOURI_LAST, MISSOURI_NEXT]}
EVERY_SCHOOL_ON = ["texas", "michigan", "texas_am", "missouri"]                        # in the order the table lists them


def sign_whose_turns_can_be_stepped(browser, server, width=1280, height=800):
    """The sign on a stopped clock that has noted when it set its 8-second timer (the strip's turns from school to school), so that a test can
    step the page's clock to the very instant of a turn. Returns the page and that moment, in the page's own milliseconds."""
    page = browser.new_page(viewport={"width": width, "height": height})
    page.clock.install()
    page.add_init_script("""(() => {
        const set = window.setInterval;
        window.timersSetAt = [];
        window.setInterval = function (handler, delay, ...rest) { window.timersSetAt.push([delay, Date.now()]); return set.call(this, handler, delay, ...rest); };
    })()""")
    page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
    page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
    strip_is_up(page)
    page.clock.pause_at(datetime.now() + timedelta(seconds=2))
    return page, page.evaluate("window.timersSetAt.find(([delay]) => delay === 8000)[1]")


def next_turn(page, set_at):
    """The next instant after now at which the strip turns: the timer fires 8000 ms after it was set, and every 8000 ms after that."""
    now = page.evaluate("Date.now()")
    turn = set_at + 8000 * ((now - set_at) // 8000 + 1)
    return turn if turn - now >= 500 else turn + 8000


def run_to(page, instant):
    """Runs the page's clock to `instant` (its milliseconds), firing every timer due by then, and lets the page draw."""
    page.clock.run_for(instant - page.evaluate("Date.now()"))
    page.wait_for_timeout(150)


def school_on_the_strip(page):
    return strip_text(page)[0].split(":")[0]


def test_the_strip_turns_to_the_next_school_at_exactly_eight_seconds_on_the_pages_own_clock_with_the_schools_name_leading(browser, central_time):
    """The page's clock is stepped to the millisecond before the timer's next firing and to the firing itself: a timer set for 7999, 8001 or
    7000 ms, or one that does not run on the page's clock, turns at the wrong step."""
    with PageServer(schedules=every_school()) as server:
        choose(server, sign_scores=["texas", "michigan"])
        scoreboard_has(server, ["texas", "michigan"])
        page, set_at = sign_whose_turns_can_be_stepped(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        first = school_on_the_strip(page)
        if page.evaluate("Date.now()") - set_at < 8000:                          # the pause came before the first turn, as it does unless the machine is very slow
            assert strip_text(page) == SCHOOL_LINES["Texas"]                     # the first school in the settings' order is first, its name leading
        order = ["Texas", "Michigan"]
        turn = next_turn(page, set_at)
        for step in range(3):
            run_to(page, turn - 1)
            assert school_on_the_strip(page) == order[(order.index(first) + step) % 2], step          # a millisecond short: still the school it was
            run_to(page, turn)
            assert strip_text(page) == SCHOOL_LINES[order[(order.index(first) + step + 1) % 2]], step      # on the turn: the next, whole
            turn += 8000
        assert page.locator("#score .rank").count() >= 2 and fits(page) and errors == []
        page.close()


def test_with_four_schools_on_the_strip_goes_round_them_in_the_settings_order(browser, central_time):
    with PageServer(schedules=every_school()) as server:
        choose(server, sign_scores=EVERY_SCHOOL_ON)
        scoreboard_has(server, EVERY_SCHOOL_ON)
        page, set_at = sign_whose_turns_can_be_stepped(browser, server)
        order = ["Texas", "Michigan", "Texas A&M", "Missouri"]
        shown = school_on_the_strip(page)
        turn, seen = next_turn(page, set_at), [shown]
        for _ in range(6):
            run_to(page, turn)
            assert strip_text(page) == SCHOOL_LINES[school_on_the_strip(page)]      # each school's own two lines, whole
            seen.append(school_on_the_strip(page))
            turn += 8000
        start = order.index(shown)
        assert seen == [order[(start + n) % 4] for n in range(7)], seen             # each once, in the settings' order, and round again: none skipped
        page.close()


@pytest.mark.parametrize("width, height", [(800, 1280), (800, 480), (1024, 600), (1099, 800)])
def test_in_a_sign_under_1100_px_wide_the_strip_is_two_lines_for_every_school_so_that_its_height_and_the_weeks_rows_never_change_on_a_turn(browser, width, height, central_time):
    """Four schools whose lines differ: Texas has only a last game (its season is over), Michigan only a next one (its season has not begun),
    Texas A&M the longest names an SEC opponent gives (Mississippi State, Louisiana State, with ranks) and Missouri the usual pair."""
    skip_if_day_ends_within(3)
    schedules = every_school()
    schedules[245] = longest_names()
    schedules[251]["events"] = [e for e in schedules[251]["events"] if e["competitions"][0]["status"]["type"]["completed"]]
    schedules[130]["events"] = [e for e in schedules[130]["events"] if not e["competitions"][0]["status"]["type"]["completed"]]
    with PageServer(calendar_events=a_full_week(), room_name="Boardroom", room_location="2nd floor, by the kitchen", schedules=schedules) as server:
        choose(server, sign_scores=EVERY_SCHOOL_ON)
        scoreboard_has(server, EVERY_SCHOOL_ON)
        page, set_at = sign_whose_turns_can_be_stepped(browser, server, width, height)
        turn = next_turn(page, set_at)
        shapes, seen = [], []
        for step in range(4):
            if step:
                run_to(page, turn)
                turn += 8000
            strip, last, following = (page.locator(selector).bounding_box() for selector in ("#score", "#score-last", "#score-next"))
            if last and following:                                                  # a school with both lines: the next game is under the last one, never beside it
                assert following["y"] >= last["y"] + last["height"] - 1, (step, last, following)
            if following and not last:                                              # one with only a next game has it at the left, where the lines start, not pushed right
                assert abs(following["x"] - page.locator("#room-name").bounding_box()["x"]) <= 1, (step, following)
            shapes.append((round(strip["y"], 1), round(strip["height"], 1)))
            seen.append(school_on_the_strip(page))
        assert len(set(seen)) == 4                                                  # every school was drawn
        assert len(set(shapes)) == 1, (seen, shapes)                                # and the strip's box is the same for each: no school is a line shorter
        line = 1.1 * 16 * 1.35
        assert abs(shapes[0][1] - (14 + 14 + 1 + 4 + 2 * line)) <= 1                 # two lines, a gap and the band's padding
        open_the_week(page)
        weeks = []
        for step in range(4):
            if step:
                run_to(page, turn)
                turn += 8000
            weeks.append(page.locator("#week .day").evaluate_all("days => days.map((d) => { const b = d.getBoundingClientRect(); return [Math.round(b.top * 10) / 10, Math.round(b.height * 10) / 10]; })"))
        assert all(rows == weeks[0] for rows in weeks), weeks                      # the seven days keep their rows across the turns
        choose(server, sign_scores=["michigan"])                                    # one school on, with only a next game: no name leads and the next game has the strip to itself
        scoreboard_has(server, ["michigan"])
        poll(page)
        assert page.locator("#score-last").evaluate("e => e.textContent") == ""
        alone, strip = page.locator("#score-next").bounding_box(), page.locator("#score").bounding_box()
        assert abs(alone["x"] - page.locator("#room-name").bounding_box()["x"]) <= 1, alone      # at the left, where the lines start, not pushed to the right
        assert abs(strip["height"] - shapes[0][1]) <= 0.5, strip                    # and the band is as high as ever
        page.close()


def test_a_school_with_nothing_to_show_is_left_out_of_the_rotation_and_the_strip_goes_when_none_has_anything(browser, central_time):
    with PageServer(schedules={**every_school(), 130: {"events": []}}) as server:       # Michigan's feed has no games in it
        choose(server, sign_scores=["michigan", "texas"])
        scoreboard_has(server, ["texas"])
        page = sign_on_a_stopped_clock(browser, server)
        strip_is_up(page)
        assert strip_text(page) == ["Texas: " + TEXAS_LAST, TEXAS_NEXT]      # two schools are on, so the name leads; only Texas has anything to say
        page.clock.run_for(8500)
        page.wait_for_timeout(150)
        assert strip_text(page)[0] == "Texas: " + TEXAS_LAST                  # and the rotation has nowhere else to go
        choose(server, sign_scores=["michigan"])
        scoreboard_has(server, [])
        poll(page)
        assert page.locator("#score").is_hidden()                            # one school on, with nothing to say: no strip
        page.close()


def test_a_school_with_only_a_last_game_or_only_a_next_one_shows_that_one(browser, central_time):
    schedules = every_school()
    schedules[251]["events"] = [e for e in schedules[251]["events"] if e["competitions"][0]["status"]["type"]["completed"]]          # the season is over
    schedules[130]["events"] = [e for e in schedules[130]["events"] if not e["competitions"][0]["status"]["type"]["completed"]]      # it has not begun
    with PageServer(schedules=schedules) as server:
        choose(server, sign_scores=["texas"])
        scoreboard_has(server, ["texas"])
        page = sign_on_a_stopped_clock(browser, server)                     # the polls that change the strip are the test's to bring
        strip_is_up(page)
        assert strip_text(page) == [TEXAS_LAST, ""] and page.locator("#score-next").evaluate("e => getComputedStyle(e).display") == "none"      # a half with nothing in it takes no room
        choose(server, sign_scores=["michigan"])
        scoreboard_has(server, ["michigan"])
        poll(page)
        assert strip_text(page) == ["", MICHIGAN_NEXT] and page.locator("#score-last").evaluate("e => getComputedStyle(e).display") == "none"
        box, nxt = page.locator("#score").bounding_box(), page.locator("#score-next").bounding_box()
        assert nxt["x"] + nxt["width"] > box["x"] + box["width"] * 0.6      # the next game stays on the right when it is alone
        choose(server, sign_scores=["texas", "michigan"])                    # with two on, the name leads a school that has only a next game to give
        scoreboard_has(server, ["texas", "michigan"])
        poll(page)                                                           # Michigan stays up: a poll does not move the strip on (the turn is eight seconds of the page's time)
        assert strip_text(page) == ["Michigan", MICHIGAN_NEXT]
        page.close()


def test_a_game_lost_on_the_road_reads_with_the_school_first_and_the_next_one_with_the_visitor_first(browser, central_time):
    with PageServer(schedules=every_school()) as server:
        choose(server, sign_scores=["texas_am"])                             # Texas A&M lost at LSU, 24 to 21: LSU is the home side, listed first in the feed
        scoreboard_has(server, ["texas_am"])
        page = open_sign(browser, server)
        strip_is_up(page)
        assert strip_text(page) == ["Texas A&M (#20) 21 \u2013 LSU (#18) 24", "Next: 10/17 \u00b7 Arkansas @ Texas A&M (#20)"]   # the school's own score first; the visitor "@" the host
        page.close()


def test_the_strip_keeps_the_last_score_through_a_feed_that_fails_or_has_nothing_to_say_and_goes_with_the_schools(browser, central_time):
    with PageServer(schedules=every_school()) as server:
        choose(server, sign_scores=["texas"])
        scoreboard_has(server, ["texas"])
        page = sign_on_a_stopped_clock(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        strip_is_up(page)

        def reads_after(change):
            asked = len(server.feed.asked)
            change()
            wait_for("three more reads", lambda: len(server.feed.asked) >= asked + 3)

        reads_after(lambda: setattr(server.feed, "down", ScheduleUnavailable("ESPN answered 503")))     # the feed goes down: the scoreboard keeps what it has ...
        assert status_of(server)["scores"][0]["last"]["home_score"] == 45
        poll(page)
        assert page.locator("#score").is_visible() and strip_text(page) == [TEXAS_LAST, TEXAS_NEXT]    # ... and so does the strip
        server.feed.down = None
        reads_after(lambda: server.schedules.__setitem__(251, {"events": []}))                         # the feed answers, with no games in it: that is no news about Texas
        poll(page)
        assert page.locator("#score").is_visible() and strip_text(page) == [TEXAS_LAST, TEXAS_NEXT] and fits(page)
        changed = texas_sample()                                                                       # and a schedule with games in it is an answer again, with a new score
        changed["events"][3]["competitions"][0]["competitors"][0]["score"] = {"value": 52.0, "displayValue": "52"}
        server.schedules[251] = changed
        wait_for("the new score", lambda: status_of(server)["scores"][0]["last"]["home_score"] == 52)
        poll(page)
        assert strip_text(page) == ["Texas (#4) 52 \u2013 Texas A&M 42", TEXAS_NEXT]
        choose(server, sign_scores=[])                                                                 # every school off: the strip goes, and nothing is read after
        poll(page)
        assert page.locator("#score").is_hidden() and "scores" not in status_of(server)
        asked = len(server.feed.asked)
        time.sleep(0.3)
        assert len(server.feed.asked) == asked and errors == []
        page.close()


def test_a_feed_that_is_down_from_the_start_leaves_the_sign_with_no_strip_and_no_error(browser):
    with PageServer(calendar_events=[event("e1", "Design review", 40)], schedules=every_school()) as server:
        server.feed.down = ScheduleUnavailable("ESPN could not be reached: no route to host")
        choose(server, sign_scores=["texas", "michigan"])
        wait_for("the first reads", lambda: len(server.feed.asked) >= 2)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        problems = []
        page.on("pageerror", lambda e: problems.append(str(e)))
        page.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        scores = status_of(server)["scores"]                                  # the status answers, listing the schools with nothing to show
        assert [(e["school"], e["last"], e["next"]) for e in scores] == [("texas", None, None), ("michigan", None, None)]
        assert page.locator("#score").is_hidden() and fits(page)
        assert "ESPN" not in page.locator("body").inner_text() and "score" not in page.locator("body").inner_text().lower()      # no error to read on the sign
        assert page.locator("#chip-text").inner_text().startswith("Free until") and problems == []
        page.close()


@pytest.mark.parametrize("width, height", [(1280, 800), (800, 1280)])
def test_the_strip_coming_up_moves_nothing_on_the_today_list_and_the_sign_still_fits(browser, width, height, central_time):
    skip_if_day_ends_within(3)
    bookings = [event("e1", "Customer call with Lone Star Eye Care", 40), event("e2", "Design review", 100), event("e3", "Sales pipeline", 160)]
    with PageServer(calendar_events=bookings, room_name="Conference Room Twelve", room_location="2nd floor, by the kitchen", schedules=every_school()) as server:
        page = sign_on_a_stopped_clock(browser, server, width, height)      # the polls that bring and take the strip are the test's to bring
        page.wait_for_selector("#events .event", timeout=5000)
        page.wait_for_function("document.body.dataset.photo === 'skyline'", timeout=5000)
        parts = (".top", ".brand", ".when", ".status", ".chip", "#detail", "#today", ".today-head", "#events .event")
        before = boxes(page, *parts)
        assert fits(page)
        choose(server, sign_scores=["texas"])
        scoreboard_has(server, ["texas"])
        poll(page)
        assert page.evaluate("!document.getElementById('score').hidden")
        assert boxes(page, *parts) == before                                  # every box of the sign above the strip is where it was
        strip, today = page.locator("#score").bounding_box(), page.locator("#today").bounding_box()
        assert today["y"] + today["height"] <= strip["y"]                    # the strip takes room of its own and covers nothing
        assert abs(strip["y"] + strip["height"] - height) <= 0.5 and fits(page)
        choose(server, sign_scores=[])                                       # and its going moves nothing either
        poll(page)
        assert page.evaluate("document.getElementById('score').hidden")
        assert boxes(page, *parts) == before and fits(page)
        page.close()


LONGEST_LINES = ["Texas A&M: Texas A&M (#12) 45 \u2013 Mississippi State (#15) 42", "Next: 10/19 \u00b7 Louisiana State (#9) @ Texas A&M (#12)"]   # with the name leading, as it does with two schools on


def what_a_side_of_the_strip_is(page, selector):
    """[its height in lines, scrollHeight == clientHeight, whether its text runs past its box] for #score-last or #score-next."""
    return page.locator(selector).evaluate("""(e) => { const s = getComputedStyle(e);
        return [e.getBoundingClientRect().height / parseFloat(s.lineHeight), e.scrollHeight === e.clientHeight, e.scrollWidth > e.clientWidth, s.whiteSpace, s.overflow, s.textOverflow]; }""")


@pytest.mark.parametrize("width, height", [(1280, 800), (1920, 1080), (1100, 800), (1099, 800), (1024, 600), (800, 1280), (420, 900)])
def test_the_strip_never_wraps_so_its_height_is_the_same_for_the_longest_names_as_for_a_short_pair_at_every_width_and_school_turn(browser, width, height, central_time):
    """Each side of the strip is one clipped line (no wrapping, hidden overflow, an ellipsis), so the strip is the same height for Texas's short pair
    and for the longest names an SEC opponent gives (Texas A&M (#12) 45 - Mississippi State (#15) 42 over Next: 10/19 - Louisiana State (#9) @ Texas A&M
    (#12)), at every width: one line from 1100 px wide, the two sides sharing it, and two lines, one side to a line, below it. The text is clipped where it
    does not fit, the final score last."""
    with PageServer(schedules={**every_school(), 245: longest_names()}) as server:
        choose(server, sign_scores=["texas", "texas_am"])                         # two on: the name leads, the longest line the strip can have
        scoreboard_has(server, ["texas", "texas_am"])
        page, set_at = sign_whose_turns_can_be_stepped(browser, server, width, height)
        root = page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")
        one_line, two_lines = 29 + 1.485 * root, 33 + 2 * 1.485 * root              # the band's padding and rule, one line of 1.1 rem at 1.35, or two with the gap
        seen = {}
        for step in range(2):
            if step:
                run_to(page, next_turn(page, set_at))
            school = school_on_the_strip(page)
            seen[school] = {"text": strip_text(page), "strip": page.locator("#score").bounding_box(),
                            "last": what_a_side_of_the_strip_is(page, "#score-last"), "next": what_a_side_of_the_strip_is(page, "#score-next")}
        assert set(seen) == {"Texas", "Texas A&M"}
        assert seen["Texas A&M"]["text"] == LONGEST_LINES, seen["Texas A&M"]["text"]                         # the whole text is there, whatever is shown of it
        expected = one_line if width >= 1100 else two_lines
        for school, drawn in seen.items():
            assert abs(drawn["strip"]["height"] - expected) <= 1, (school, drawn["strip"], expected)         # the same height for the short pair and the long one
            for side in ("last", "next"):
                lines, whole, _, white_space, overflow, ellipsis = drawn[side]
                assert abs(lines - 1) < 0.01 and whole, (school, side, drawn[side])                         # one line, never a second
                assert (white_space, overflow, ellipsis) == ("nowrap", "hidden", "ellipsis"), (school, side, drawn[side])
        assert abs(seen["Texas"]["strip"]["y"] - seen["Texas A&M"]["strip"]["y"]) <= 0.5                     # so the top edge does not move on a turn
        short_last, short_next = seen["Texas"]["last"][2], seen["Texas"]["next"][2]
        assert not short_last and not short_next                                 # Texas's pair is never clipped
        if width == 1100:                                                       # the narrowest one-line strip: the longest pair does not fit, and the next game gives way
            assert not seen["Texas A&M"]["last"][2] and seen["Texas A&M"]["next"][2], seen["Texas A&M"]
        if width == 420:                                                        # a phone: each side is clipped to its own line
            assert seen["Texas A&M"]["last"][2] and seen["Texas A&M"]["next"][2], seen["Texas A&M"]
        sides = page.locator("#score-last").bounding_box(), page.locator("#score-next").bounding_box()
        if width >= 1100:
            assert abs(sides[0]["y"] - sides[1]["y"]) <= 2 and sides[1]["x"] >= sides[0]["x"] + sides[0]["width"]     # side by side
        else:
            assert sides[1]["y"] >= sides[0]["y"] + sides[0]["height"] - 1                                          # one under the other
        if width != 420:
            assert fits(page)
        page.close()


def test_the_scoreboards_last_game_is_clipped_inside_the_strip_when_a_team_name_is_wider_than_the_whole_strip(browser, central_time):
    """The strip's last-game side is `max-width: 100%` of the strip's content: without it, a side that does not shrink is as wide as its text and runs
    off the sign (the next game with it). A feed can send a name of any length, so a team name of 167 characters, wider than a 1280 px strip (the line of
    one of 90 is 961 px of the strip's 1151, and fits), is cut with an ellipsis at the strip's own edge, is whole in the page's text, and leaves the strip
    the height of one line."""
    name = ("Louisiana State University and Agricultural and Mechanical College Fighting Tigers of 1860, "
            "the flagship campus on the bluff above the Mississippi River in Baton Rouge")
    assert len(name) == 167
    long_name = schedule("Texas", [game("Texas", name, "2026-10-12T00:00Z", home_score=45, away_score=42, home_rank=4, state="post"),
                                   game("Texas", "Rice", "2026-10-19T19:00Z", home_rank=4)])
    with PageServer(schedules={**every_school(), 251: long_name}) as server:
        choose(server, sign_scores=["texas"])
        scoreboard_has(server, ["texas"])
        page = open_sign(browser, server, 1280, 800)
        strip_is_up(page)
        assert strip_text(page)[0] == f"Texas (#4) 45 \u2013 {name} 42"                  # every character is in the page, whatever is shown of it
        drawn = page.evaluate("""() => { const strip = document.getElementById('score'), last = document.getElementById('score-last'), s = getComputedStyle(strip);
            const box = strip.getBoundingClientRect(), side = last.getBoundingClientRect(), root = document.documentElement;
            return {inside_left: box.left + parseFloat(s.paddingLeft), inside_right: box.right - parseFloat(s.paddingRight), left: side.left, right: side.right,
                    height: box.height, clipped: last.scrollWidth > last.clientWidth, wider_than_the_sign: root.scrollWidth > root.clientWidth}; }""")
        assert drawn["clipped"], drawn                                                      # the line is wider than the strip, so ...
        assert drawn["left"] >= drawn["inside_left"] - 0.5 and drawn["right"] <= drawn["inside_right"] + 0.5, drawn      # ... its side stops at the strip's content edge
        assert what_a_side_of_the_strip_is(page, "#score-last")[3:] == ["nowrap", "hidden", "ellipsis"]       # by being cut, not wrapped
        assert abs(drawn["height"] - (29 + 1.485 * 16)) <= 1 and not drawn["wider_than_the_sign"] and fits(page), drawn      # one line of it, on a sign that does not move
        page.close()


def test_the_scoreboard_reads_vs_for_a_next_game_at_a_neutral_site_in_the_order_espn_lists_it_and_a_last_game_is_unchanged(browser, central_time):
    """The Red River game is marked neutralSite with Oklahoma listed home: "Texas (#4) @ Oklahoma" would call Oklahoma the host. A bowl or a
    championship game would read the same way. The line of the last game is the same at a neutral site as anywhere."""
    red_river = schedule("Texas", [game("Oklahoma", "Texas", "2026-10-10T19:30Z", home_score=27, away_score=24, away_rank=1, state="post", neutral=True),
                                   game("Oklahoma", "Texas", "2026-10-17T19:00Z", away_rank=4, neutral=True)])
    with PageServer(schedules={251: red_river}) as server:
        choose(server, sign_scores=["texas"])
        scoreboard_has(server, ["texas"])
        page = open_sign(browser, server)
        strip_is_up(page)
        assert strip_text(page) == ["Texas (#1) 24 \u2013 Oklahoma 27", "Next: 10/17 \u00b7 Texas (#4) vs Oklahoma"]     # the visitor first, "vs" and not "@"
        assert page.locator("#score .rank").all_inner_texts() == ["(#1)", "(#4)"]
        page.close()


def test_the_scoreboards_next_game_reads_at_unless_the_status_says_neutral_is_true(browser):
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        flag = {}

        def status(route):
            body = route.fetch().json()
            game = {"date": "2026-10-17", "home": "Oklahoma", "home_rank": None, "away": "Texas", "away_rank": 4}
            body["scores"] = [{"school": "texas", "name": "Texas", "last": None, "next": {**game, **flag}}]
            route.fulfill(json=body)

        page.route("**/api/status", status)
        for flag, shown in (({"neutral": True}, "vs"), ({"neutral": False}, "@"), ({}, "@"), ({"neutral": "yes"}, "@"), ({"neutral": 1}, "@"), ({"neutral": None}, "@")):
            page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
            strip_is_up(page)
            assert page.locator("#score-next").inner_text() == f"Next: 10/17 \u00b7 Texas (#4) {shown} Oklahoma", flag
        page.close()


def test_the_scoreboard_date_is_the_month_and_the_day_without_leading_zeros(browser):
    """The strip's date is "10/19" for the 19th of October and "9/5" for the 5th of September, as Americans write it."""
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        dates = ["2026-09-05", "2026-12-31", "2027-01-09", "2026-10-19"]

        def status(route):
            body = route.fetch().json()
            body["scores"] = [{"school": "texas", "name": "Texas", "last": None,
                               "next": {"date": dates[0], "home": "Texas", "home_rank": None, "away": "Rice", "away_rank": None}}]
            route.fulfill(json=body)

        page.route("**/api/status", status)
        for date, shown in zip(dates, ["9/5", "12/31", "1/9", "10/19"]):
            dates[0] = date
            page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
            strip_is_up(page)
            assert page.locator("#score-next").inner_text() == f"Next: {shown} \u00b7 Rice @ Texas", date
        page.close()


def test_a_poll_that_changes_nothing_rewrites_nothing_on_the_scoreboard_strip(browser, central_time):
    with PageServer(schedules=every_school()) as server:
        choose(server, sign_scores=["texas"])
        scoreboard_has(server, ["texas"])
        page = sign_on_a_stopped_clock(browser, server)
        strip_is_up(page)
        page.evaluate("""() => {
            window.records = [];
            new MutationObserver((list) => list.forEach((r) => window.records.push(r.type + ':' + (r.attributeName || '') + ':' + r.target.nodeName)))
                .observe(document.getElementById('score'), { subtree: true, childList: true, characterData: true, attributes: true });
        }""")
        poll(page)
        poll(page)
        assert page.evaluate("window.records") == []                         # the same score: the strip is not drawn again
        page.close()


def a_sign_served_this_status(browser, server, scores):
    """A sign on a room whose status route is made to carry `scores` (whatever the test says: the scoreboard behind it is not asked); returns
    the page, the list of what went wrong on it (page errors and console errors) and a function that serves something else at the next load."""
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    problems, served = [], {"scores": scores}

    def status(route):
        body = route.fetch().json()
        if served["scores"] is not None:
            body["scores"] = served["scores"]
        route.fulfill(json=body)

    page.on("pageerror", lambda e: problems.append(str(e)))
    page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
    page.route("**/api/status", status)

    def load(scores_now):
        served["scores"] = scores_now
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)

    return page, problems, load


def test_a_scores_list_with_junk_in_it_draws_what_it_can_and_throws_nothing(browser):
    """The status is the room's own, but the sign never takes its shape on trust."""
    def michigan(**ranks):
        return {"school": "michigan", "name": "Michigan", "next": {"date": "2026-10-24", "home": "", "away": "Purdue"},
                "last": {"home": "Michigan", "home_score": 3, "away": "Ohio State", "away_score": 0, **ranks}}

    junk = [None, 7, "texas", {}, {"school": "texas"}, {"school": "texas", "name": "Texas", "last": {"home": "Texas"}, "next": {"date": "soon", "home": "A", "away": "B"}},
            michigan(home_rank="x", away_rank=99.5)]
    with PageServer() as server:
        page, problems, load = a_sign_served_this_status(browser, server, junk)
        load(junk)
        strip_is_up(page)
        assert strip_text(page) == ["Michigan: Michigan 3 \u2013 Ohio State 0", ""]      # the one entry that holds a whole game; a next game with no host is left out
        for ranks, drawn in (({"home_rank": 25, "away_rank": 1}, "Michigan (#25) 3 \u2013 Ohio State (#1) 0"),                       # the Top 25 are ranks, ...
                             ({"home_rank": 26, "away_rank": 99}, "Michigan 3 \u2013 Ohio State 0"),                               # ... and nothing past it is, 99 being ESPN's "no rank"
                             ({"home_rank": 0, "away_rank": -3}, "Michigan 3 \u2013 Ohio State 0"),
                             ({"home_rank": True, "away_rank": "7"}, "Michigan 3 \u2013 Ohio State 0"),
                             ({"home_rank": 4.5, "away_rank": None}, "Michigan 3 \u2013 Ohio State 0")):
            load([michigan(**ranks), None, 7])                                                    # one whole entry among the junk: no name leads
            strip_is_up(page)
            assert strip_text(page)[0] == drawn, ranks
        for nothing in ([None, 7, "x", {}], "texas", {"school": "texas"}, [], None):
            load(nothing)                                                      # nothing a strip can be drawn from
            assert page.locator("#score").is_hidden(), nothing
        assert problems == []
        page.close()


def test_a_scoreboard_team_name_with_markup_in_it_is_drawn_as_the_text_it_is_and_nothing_is_injected(browser):
    """Names come from ESPN and are written to the sign's own page: a name with markup in it must show as typed and run nothing."""
    onerror, script, bold, italic = '<img src=x onerror="window.pwned = 1">', "</span><script>window.pwned = 2</script>", "<b>Ohio State</b>", "a &amp; b <i>c</i>"
    scores = [{"school": "michigan", "name": onerror,
               "last": {"home": script, "home_score": 3, "away": bold, "away_score": 0},
               "next": {"date": "2026-10-24", "home": "Purdue", "away": italic}},
              {"school": "texas", "name": "<u>Texas</u>", "last": None, "next": {"date": "2026-10-19", "home": "Missouri", "away": "Texas"}}]
    with PageServer() as server:
        page, problems, load = a_sign_served_this_status(browser, server, scores)
        load(scores)
        strip_is_up(page)
        assert strip_text(page) == [f"{onerror}: {script} 3 \u2013 {bold} 0", f"Next: 10/24 \u00b7 {italic} @ Purdue"]      # as typed, tags and entities and all
        assert page.locator("#score img, #score script, #score b, #score i, #score u").count() == 0         # no element came of it
        assert page.evaluate("window.pwned") is None
        page.wait_for_timeout(300)
        assert page.evaluate("window.pwned") is None and problems == []                                   # and nothing ran, or failed to load, afterwards
        page.close()


def test_the_strip_stays_as_it_was_while_the_sign_cannot_reach_the_room(browser, central_time):
    with PageServer(schedules=every_school()) as server:
        choose(server, sign_scores=["texas"])
        scoreboard_has(server, ["texas"])
        page = sign_on_a_stopped_clock(browser, server)
        strip_is_up(page)
        page.route("**/api/status", lambda route: route.abort())
        with page.expect_event("requestfailed"):
            page.clock.run_for(5000)
        page.wait_for_function("document.body.dataset.state === 'offline'", timeout=5000)
        assert page.locator("#score").is_visible() and strip_text(page) == [TEXAS_LAST, TEXAS_NEXT]    # as the rest of what the sign last knew stays
        page.close()


@pytest.mark.parametrize("room, location", [("Boardroom", "2nd floor, by the kitchen"), ("Conference Room Twelve", "2nd floor, by the kitchen, past the elevators")])
def test_on_a_short_sign_with_a_school_on_the_week_counts_the_strip_and_keeps_every_days_plus_n_more_whole(browser, room, location, central_time):
    """The strip takes 53 to 81 px of a short screen's height, which the short-screen rules did not count: on a 1024 by 600 sign with a long room
    name the days were 74 px tall and their "+N more" cut off, on 900 by 500 they were 43 and on 800 by 480, 20. While a school is on, up to
    700 px of height the week gives the days room (the strip is 81 px tall under 1100 px wide, where it is two lines, and a 1024 by 641 sign's
    "+N more" was cut off by it, as a 1280 by 641 sign's was before; on a wide panel the root font grows with the long side, so with the rule at
    680 a 1366 by 681 sign's and a 1400 by 690 one's were cut off too, and the rule's edge is pinned at 700 and 701): the status gives up its detail
    and its location and shrinks the room's name (the days keep their dates down to 561 px, where the rule for short signs takes them); and up to
    560 the Today list's status gives up its detail too. Every busy day's "+N more" is then whole: inside its day and inside the box its bookings
    are in."""
    skip_if_day_ends_within(3)
    with PageServer(calendar_events=a_full_week(), room_name=room, room_location=location, schedules=every_school()) as server:
        choose(server, sign_scores=["texas", "texas_am"])                      # two on: the name leads, the longest line the strip has
        scoreboard_has(server, ["texas", "texas_am"])
        for width, height in ((1024, 700), (1024, 641), (1280, 641), (1024, 600), (1024, 561), (900, 500), (800, 480), (1024, 560), (1280, 561), (1024, 701), (1280, 720),
                              (1366, 681), (1400, 690)):
            page = open_sign(browser, server, width, height)
            strip_is_up(page)
            today_size = page.locator("#room-name").evaluate("e => parseFloat(getComputedStyle(e).fontSize)")
            assert fits(page), ("Today", width, height)                       # the Today list's status fits above the strip, the long name included
            assert page.locator("#detail").is_hidden() == (height <= 560), ("Today", width, height)     # and on the shortest screens does without its detail line
            open_the_week(page)
            page.wait_for_timeout(200)
            assert fits(page), ("week", width, height)
            short = height <= 700
            assert page.locator("#detail").is_hidden() == short, (width, height)                       # up to 700 px of height the detail goes while the strip is up ...
            assert page.locator("#room-location").is_hidden() == short, (width, height)                # ... and the location ...
            assert page.locator("#week .day-date").first.is_hidden() == (height <= 560), (width, height)        # while the days keep their dates until 560
            week_size = page.locator("#room-name").evaluate("e => parseFloat(getComputedStyle(e).fontSize)")
            root = page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")           # 16 px, and more on a panel wider than 1280
            assert (week_size == pytest.approx(min(max(1.8 * root, 0.036 * width), 2.6 * root))) if short else (week_size == today_size), (width, height, week_size)   # ... and the name is smaller
            counts = page.evaluate("""() => [...document.querySelectorAll('#week .day')].slice(1).map((day) => {
                const more = day.querySelector('.more'), slots = day.querySelector('.slots');
                if (!more || more.hidden) return null;
                const a = more.getBoundingClientRect(), b = slots.getBoundingClientRect(), c = day.getBoundingClientRect();
                return [a.top >= b.top - 0.5 && a.bottom <= b.bottom + 0.5 && a.bottom <= c.bottom && more.scrollWidth <= more.clientWidth, more.textContent];
            })""")
            assert all(count is not None and count[0] for count in counts), (room, width, height, counts)         # every busy day says how many it leaves out, whole
            strip, days = page.locator("#score").bounding_box(), page.locator("#week-days").bounding_box()
            assert days["y"] + days["height"] <= strip["y"] + 0.5, (width, height)                            # and the days end above the strip
            page.close()


def test_the_weeks_short_sign_rules_are_by_height_alone_and_the_strip_coming_and_going_changes_none_of_them(browser, central_time):
    """At 1024 by 600 the week has no detail line, no location and the smaller name whether the strip is up or not (the days were 100 px tall without it, with
    "+3 more" for today and no booking), and has them all, in full, at 1024 by 701. The page's polls are stopped here: the test brings each one."""
    skip_if_day_ends_within(3)
    with PageServer(calendar_events=a_full_week(), room_name="Conference Room Twelve", room_location="2nd floor, by the kitchen", schedules=every_school()) as server:
        page = sign_on_a_stopped_clock(browser, server, 1024, 600)
        open_the_week(page)
        hidden = lambda: [page.locator(selector).is_hidden() for selector in ("#detail", "#room-location")]
        size = lambda: page.locator("#room-name").evaluate("e => parseFloat(getComputedStyle(e).fontSize)")
        week_size = size()
        assert hidden() == [True, True] and week_size < 45 and page.evaluate("'strip' in document.body.dataset") is False
        choose(server, sign_scores=["texas"])
        scoreboard_has(server, ["texas"])
        poll(page)                                                                                      # the next poll brings the strip
        assert page.evaluate("'strip' in document.body.dataset") and hidden() == [True, True] and size() == week_size
        choose(server, sign_scores=[])
        poll(page)                                                                                      # and the next takes it away
        assert page.evaluate("'strip' in document.body.dataset") is False
        assert hidden() == [True, True] and size() == week_size and page.locator("#week .day").count() == 7
        page.close()
        page = open_sign(browser, server, 1024, 701)
        open_the_week(page)
        assert [page.locator(selector).is_visible() for selector in ("#detail", "#room-location")] == [True, True]
        assert page.locator("#room-name").evaluate("e => parseFloat(getComputedStyle(e).fontSize)") > 60 and fits(page)
        page.close()


# --- what a long booking title and a long room name do to a short sign (the task 5 review) ---

BOOKING_TITLES = ("Customer call with Lone Star Eye Care", "Quarterly planning review with the regional leadership teams")   # 37 and 60 characters
SHORT_SIGNS = ((800, 480), (900, 500), (1024, 480), (1024, 500), (1280, 480), (1366, 500))
THREE_LINE_NAMES = ("Sales Conference Room", "Large Conference Room", "Executive Conference Roo")   # 21, 21 and 24 characters: three lines at the large size on a 1280 px sign


def lines_of(page, selector):
    """How many lines each element of the selector takes as it is drawn now, whatever is cut off."""
    return page.evaluate("""(selector) => [...document.querySelectorAll(selector)].map((e) =>
        Math.round(e.getBoundingClientRect().height / parseFloat(getComputedStyle(e).lineHeight)))""", selector)


def room_name_is_long(page):
    return page.locator("#room-name").evaluate("e => e.classList.contains('long')")


def room_name_needs_the_smaller_type(page):
    """Whether the name takes more than two lines at the large size, measured by taking the smaller type off for a moment."""
    return page.evaluate("""() => { const r = document.getElementById('room-name'), had = r.classList.contains('long'); r.classList.remove('long');
        const lines = Math.round(r.getBoundingClientRect().height / parseFloat(getComputedStyle(r).lineHeight)); r.classList.toggle('long', had); return lines > 2; }""")


@pytest.mark.parametrize("scores", [[], ["texas"]], ids=["strip down", "strip up"])
@pytest.mark.parametrize("title", BOOKING_TITLES, ids=["37 characters", "60 characters"])
def test_on_a_short_sign_a_long_booking_title_is_clamped_in_today_so_that_the_sign_fits_with_the_strip_up_or_down(browser, title, scores, central_time):
    """A booking's title is two lines and an ellipsis in the Today list, and one line on a sign 560 px high or less, where the list has no height to
    spare: with the strip up, three bookings whose titles take two lines (what a two-line clamp alone leaves) pushed the strip 33 to 58 px past the bottom
    of an 800 by 480, 900 by 500, 1024 by 480 or 1024 by 500 sign. Every character is still in the page, and a sign with room to spare keeps two lines."""
    skip_if_day_ends_within(3)
    assert (len(BOOKING_TITLES[0]), len(BOOKING_TITLES[1])) == (37, 60)
    bookings = [event(f"t{n}", title, 40 + 60 * n) for n in range(3)]
    with PageServer(calendar_events=bookings, room_name="Boardroom", room_location="2nd floor, by the kitchen", schedules=every_school()) as server:
        choose(server, sign_scores=scores)
        if scores:
            scoreboard_has(server, scores)
        for width, height in (*SHORT_SIGNS, (1280, 800)):
            page = open_sign(browser, server, width, height)
            if scores:
                strip_is_up(page)
                strip = page.locator("#score").bounding_box()
                assert strip["y"] >= 0 and strip["y"] + strip["height"] <= height + 0.5, ("the strip is not wholly on the sign", width, height, strip)
            assert fits(page), (width, height)
            assert page.locator("#events .title").all_text_contents() == [title] * 3, (width, height)        # the whole title is in the page, whatever is drawn of it
            most = 1 if height <= 560 else 2
            title_lines = lines_of(page, "#events .title")
            assert max(title_lines) <= most, (width, height, title_lines)
            if len(title) > 40 and height <= 560:                                # the long one fills the one line it has, and is cut after it
                assert title_lines == [1] * 3 and page.locator("#events .title").first.evaluate("e => e.scrollHeight > e.clientHeight"), (width, height, title_lines)
            page.close()


@pytest.mark.parametrize("height, lines", [(560, 1), (561, 2)])
def test_on_the_sign_a_long_booking_title_in_today_is_one_line_at_560_px_high_and_two_lines_at_561(browser, height, lines, central_time):
    """The edge of the short sign's rule for the Today list, pinned to the pixel: the title is cut after one line on a sign 560 px high or less and after two on a
    sign one pixel taller, with every character in the page either way."""
    skip_if_day_ends_within(3)
    title = BOOKING_TITLES[1]
    with PageServer(calendar_events=[event(f"t{n}", title, 40 + 60 * n) for n in range(3)], room_name="Boardroom") as server:
        page = open_sign(browser, server, 1280, height)
        assert page.locator("#events .title").all_text_contents() == [title] * 3
        assert lines_of(page, "#events .title") == [lines] * 3, (height, lines_of(page, "#events .title"))
        page.close()


def test_a_long_booking_title_is_two_lines_and_an_ellipsis_in_today_and_in_the_week_and_one_line_in_the_portrait_week(browser, central_time):
    skip_if_day_ends_within(3)
    title = "Quarterly planning review with the regional leadership team and the partners, the board and the finance group"   # long enough to be cut
    bookings = [event(f"t{n}", title, 40 + 60 * n) for n in range(3)]
    with PageServer(calendar_events=bookings, room_name="Boardroom", room_location="2nd floor") as server:
        for width, height, week_lines in ((1280, 800, 2), (800, 1280, 1)):
            page = open_sign(browser, server, width, height)
            assert page.locator("#events .title").all_text_contents() == [title] * 3                          # whole in the page, whatever is drawn
            assert max(lines_of(page, "#events .title")) <= 2, (width, height)
            if width > height:                                                    # beside the status the list is a narrow column: the title is cut after two lines
                assert lines_of(page, "#events .title") == [2] * 3 and page.locator("#events .title").first.evaluate("e => e.scrollHeight > e.clientHeight")
            open_the_week(page)
            page.wait_for_timeout(200)
            shown = "#week .slot:not([hidden]) .title"
            assert page.locator(shown).count() >= 1 and set(lines_of(page, shown)) == {week_lines}, (width, height, lines_of(page, shown))
            assert set(page.locator(shown).all_text_contents()) == {title}                                   # whole in the page
            assert page.locator(shown).first.evaluate("e => e.scrollHeight > e.clientHeight"), (width, height)    # and cut where it does not fit
            page.close()


@pytest.mark.parametrize("scores", [[], ["texas"]], ids=["strip down", "strip up"])
@pytest.mark.parametrize("name", THREE_LINE_NAMES)
def test_a_room_name_that_would_take_three_lines_takes_the_smaller_type_so_that_the_chip_stays_on_a_short_sign(browser, name, scores, central_time):
    """"Sales Conference Room" is three lines at the large size on a 1280 px sign, and three lines of that type pushed the chip, the strip or both past
    the bottom of a 1280 by 480, 1366 by 500, 1024 by 500 or (with the strip up) 800 by 480 sign by 7 to 100 px. A name that would take more than two
    lines takes the smaller type that a name of 25 characters takes, and never more than two lines of it, so the chip is whole on every one of them."""
    skip_if_day_ends_within(3)
    bookings = [event(f"t{n}", f"Today {n}", 40 + 60 * n) for n in range(3)]
    with PageServer(calendar_events=bookings, room_name=name, room_location="2nd floor, by the kitchen", schedules=every_school()) as server:
        choose(server, sign_scores=scores)
        if scores:
            scoreboard_has(server, scores)
        assert len(name) <= 24                                                    # it is not the length that makes them small
        for width, height in SHORT_SIGNS:
            page = open_sign(browser, server, width, height)
            if scores:
                strip_is_up(page)
            assert fits(page), (width, height)
            assert room_name_is_long(page) and lines_of(page, "#room-name") == [2], (width, height, lines_of(page, "#room-name"))
            chip = page.locator(".chip").bounding_box()
            floor = page.locator("#score").bounding_box()["y"] if scores else height
            assert chip["y"] + chip["height"] <= floor + 0.5, ("the chip is not wholly on the sign", width, height, chip, floor)
            assert page.locator("#room-name").inner_text() == name                # the name is whole: smaller, never cut
            page.close()


@pytest.mark.parametrize("width, height", [(1280, 800), (1280, 480), (1366, 500), (800, 480), (800, 1280), (1920, 1080)])
def test_a_short_room_name_keeps_the_large_type_on_the_sign(browser, width, height):
    root = min(max(16, 0.0125 * max(width, height)), 24)                          # the sign's rem: 16 px up to 1280, then growing with the long side
    large = min(max(2.6 * root, 0.11 * width), 4.6 * root) if height > width else min(max(3 * root, 0.072 * width), 5.4 * root)
    for name in ("Boardroom", "Room 1"):
        with PageServer(room_name=name) as server:
            page = open_sign(browser, server, width, height)
            assert not room_name_is_long(page) and lines_of(page, "#room-name") == [1], (name, width, height)
            assert page.locator("#room-name").evaluate("e => parseFloat(getComputedStyle(e).fontSize)") == pytest.approx(large), (name, width, height)
            page.close()


def test_a_room_name_is_fitted_again_when_the_sign_is_turned_or_its_width_or_height_alone_changes(browser, central_time):
    """Two lines at the large size on a sign standing upright, three on one lying down: turned, the name takes the smaller type, and takes the large
    one back when it is turned again, whether the width and the height both change or only one of them does."""
    with PageServer(room_name="Sales Conference Room", room_location="2nd floor") as server:
        # Nothing but the sign being resized may fit the name here: its polls are stopped, and the font events (which this Chromium fires again when the
        # viewport changes) are switched off, so that the resize is what is tested and not a font that seems to arrive.
        page = sign_on_a_stopped_clock(browser, server, 800, 1280, init_script="document.fonts.addEventListener = () => {};")
        assert not room_name_is_long(page) and lines_of(page, "#room-name") == [2]
        for width, height, long in ((1280, 800, True), (800, 1280, False), (1024, 500, True), (800, 1280, False),
                                    (1280, 1000, True), (900, 1000, False),             # only the width changes, and the sign is turned
                                    (1280, 800, True), (1280, 1400, False)):            # only the height changes, and the sign is turned
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(150)                                                  # the page's own timers are stopped: this is the test's time
            lines = lines_of(page, "#room-name")[0]
            assert room_name_is_long(page) == long and lines <= 2 and (lines == 2 or not long) and fits(page), (width, height, lines)
        page.close()


def test_the_room_names_type_is_fitted_again_when_the_week_is_left_on_a_short_sign_with_the_strip_up(browser, central_time):
    """In the week with the strip up, a short sign sets the name in a size of its own, in which "Sales Conference Room" takes one line: measured there,
    it loses the smaller type that Today needs. Back in Today it has it again."""
    skip_if_day_ends_within(3)
    bookings = [event(f"t{n}", f"Today {n}", 40 + 60 * n) for n in range(3)]
    with PageServer(calendar_events=bookings, room_name="Sales Conference Room", room_location="2nd floor", schedules=every_school()) as server:
        choose(server, sign_scores=["texas"])
        scoreboard_has(server, ["texas"])
        page = open_sign(browser, server, 1280, 480)
        strip_is_up(page)
        assert room_name_is_long(page) and fits(page)
        open_the_week(page)
        page.set_viewport_size({"width": 1281, "height": 480})                    # the name is measured again, at the size the week gives it
        page.wait_for_function("!document.getElementById('room-name').classList.contains('long')", timeout=3000)
        page.click("#week-back")
        page.wait_for_function("document.body.dataset.view === 'today'", timeout=3000)
        assert room_name_is_long(page) and lines_of(page, "#room-name") == [2] and fits(page)
        page.close()


def test_the_weeks_name_keeps_its_size_when_the_strip_goes_while_the_week_is_up_and_has_todays_type_back_in_today(browser, central_time):
    """The week sets the name in a size of its own on a short sign by height alone, with the strip up or down, so the strip going changes nothing in the week; the
    name is set as it is in Today once the week is left. The page's polls are stopped here: the test brings the one that takes the strip away."""
    skip_if_day_ends_within(3)
    bookings = [event(f"t{n}", f"Today {n}", 40 + 60 * n) for n in range(3)]
    with PageServer(calendar_events=bookings, room_name="Sales Conference Room", room_location="2nd floor", schedules=every_school()) as server:
        choose(server, sign_scores=["texas"])
        scoreboard_has(server, ["texas"])
        page = sign_on_a_stopped_clock(browser, server, 1280, 480)
        strip_is_up(page)
        open_the_week(page)
        week_type = room_name_type(page)
        assert not room_name_is_long(page) and week_type[2] == 1                   # at the week's own size the name takes one line: the large type is fitted to it
        choose(server, sign_scores=[])
        poll(page)                                                                 # the next poll takes the strip away
        assert page.evaluate("'strip' in document.body.dataset") is False
        assert room_name_type(page) == week_type and fits(page)                    # and the week's size, being by height alone, has not moved
        page.click("#week-back")
        page.wait_for_function("document.body.dataset.view === 'today'", timeout=3000)
        assert room_name_is_long(page) and lines_of(page, "#room-name") == [2] and fits(page)
        page.close()


def a_narrow_fallback_font():
    """An init script that puts a font in the sign's font stack after Lexend, for the time Lexend takes to arrive: Lexend's own file under another name, set at 60 % of its
    size (size-adjust), so that the fallback is narrower than Lexend by construction and is the same on every machine, whatever fonts it has."""
    data = base64.b64encode((STATIC_DIR / "fonts" / "lexend-400.woff2").read_bytes()).decode()
    css = ('@font-face { font-family: "Narrow Fallback"; src: url("data:font/woff2;base64,' + data + '") format("woff2"); size-adjust: 60%; } '
           ':root { --font: "Lexend", "Narrow Fallback", sans-serif !important; }')
    return ("(() => { const css = " + json.dumps(css) + "; const add = () => { const style = document.createElement('style'); style.textContent = css; document.documentElement.append(style); };"
            " if (document.documentElement) add(); else new MutationObserver((records, observer) => { if (document.documentElement) { observer.disconnect(); add(); } }).observe(document, { childList: true }); })();")


def test_a_room_names_type_is_fitted_again_when_the_signs_font_arrives(browser):
    """Until Lexend arrives the name is set in a fallback font, whose lines are not Lexend's: "Sales Conference Room" takes three lines of Lexend at the large size
    on a 1280 px sign and one of the fallback here (a narrow one, the same on every machine), so it needs the smaller type in Lexend and not before. The font's
    files are held back until the test lets them go: the name must end with the type its lines in Lexend need."""
    held = []
    with PageServer(room_name="Sales Conference Room", room_location="") as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.add_init_script(a_narrow_fallback_font())
        try:
            page.route("**/static/fonts/*.woff2", lambda route: held.append(route))
            page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="domcontentloaded")
            page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
            assert page.evaluate("document.fonts.status") == "loading" and held                  # Lexend is on its way, held
            page.wait_for_function("[...document.fonts].some((f) => f.family.includes('Narrow Fallback') && f.status === 'loaded')", timeout=5000)
            page.set_viewport_size({"width": 1281, "height": 800})                                # fitted again, now that the fallback is on the screen (while Lexend is held
            page.wait_for_function("!document.getElementById('room-name').classList.contains('long')", timeout=3000)          # no font event comes: the first fit may have been before it)
            assert not room_name_needs_the_smaller_type(page) and lines_of(page, "#room-name") == [1]
            while held:
                held.pop().continue_()
            page.wait_for_function("document.fonts.status === 'loaded'", timeout=5000)
            assert room_name_needs_the_smaller_type(page)                                       # in Lexend it takes three lines at the large size
            page.wait_for_function("document.getElementById('room-name').classList.contains('long')", timeout=3000)           # and has the type for them
            assert lines_of(page, "#room-name") == [2]
        finally:
            while held:                                                                         # a failure before the files were let go leaves no request hanging
                held.pop().continue_()
            page.close()


def test_a_poll_that_changes_nothing_rewrites_nothing_on_the_signs_room_name(browser):
    with PageServer(room_name="Sales Conference Room") as server:
        page = sign_on_a_stopped_clock(browser, server)
        page.evaluate("""() => {
            window.records = [];
            new MutationObserver((list) => list.forEach((r) => window.records.push(r.type + ':' + (r.attributeName || ''))))
                .observe(document.getElementById('room-name'), { subtree: true, childList: true, characterData: true, attributes: true });
        }""")
        poll(page)
        poll(page)
        assert page.evaluate("window.records") == [] and room_name_is_long(page)      # the same name in the same place: neither measured nor written again
        page.close()


# --- the chip and the room name on a sign 560 px high or less (the task 5 review, round 1b) ---

TWO_LINE_LOCATION = "2nd floor, by the kitchen, past the elevators"
LONGEST_TWO_LINE_NAME = "North Wing Training Room 2"          # 26 characters: two lines of the smaller type at 800, 900, 1024 and 1280 px wide; 31 take three on the first three
STATUSES = ("free", "free all day", "starting soon", "booked", "in use", "not connected")


def join_the_booking(server, event_id):
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/meeting/join", data=json.dumps({"event_id": event_id}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 202                                              # accepted: the join goes on in the background


@pytest.mark.parametrize("width, height, short", [(800, 480, True), (1366, 500, True), (1280, 560, True), (1280, 561, False), (1280, 800, False), (800, 1280, False)])
def test_on_a_sign_560_px_high_or_less_the_chip_before_a_booking_carries_the_status_words_and_not_its_title(browser, width, height, short, central_time):
    """Today's rows beside the chip say which booking it is, and a long title in the chip made it three lines: 72 px past the bottom of an 800 by 480
    sign with the strip up. On a sign 560 px high or less the chip says "Starts in 8 minutes" or "Starting now"; a taller one keeps its title."""
    skip_if_day_ends_within(3)
    title = BOOKING_TITLES[1]
    for minutes, short_words, tall_words in ((8, r"Starts in \d+ minutes?", r"{} starts in \d+ minutes?"), (0.4, "Starting now", r"{} is starting")):
        with PageServer(calendar_events=[event(f"t{n}", title, minutes + 60 * n) for n in range(2)], room_name="Boardroom") as server:
            page = open_sign(browser, server, width, height, state="soon")
            chip = page.locator("#chip-text").inner_text()
            assert re.fullmatch(short_words if short else tall_words.format(re.escape(title)), chip), (width, height, chip)
            assert page.locator("#events .title").first.inner_text() == title            # and the title is in Today, beside it, on every sign
            assert fits(page), (width, height)
            page.close()


@pytest.mark.parametrize("width, height", [(800, 480), (1280, 561), (1280, 800)])
def test_the_chip_says_the_same_words_on_every_sign_when_the_room_is_free_booked_or_in_use(browser, width, height, central_time):
    skip_if_day_ends_within(3)
    title = BOOKING_TITLES[0]
    with PageServer(calendar_events=[event(f"t{n}", title, 40 + 60 * n) for n in range(2)], room_name="Boardroom") as server:
        page = open_sign(browser, server, width, height)
        assert re.fullmatch(r"Free until \d+:\d\d.[AP]M", page.locator("#chip-text").inner_text())
        page.close()
    with PageServer(room_name="Boardroom") as server:
        page = open_sign(browser, server, width, height)
        assert page.locator("#chip-text").inner_text() == "Free for the rest of the day"
        page.close()
    with PageServer(calendar_events=[event("now", title, -10, duration=50)], room_name="Boardroom") as server:
        page = sign_on_a_stopped_clock(browser, server, width, height, state="occupied")     # the poll that says "In use" is the test's to bring
        assert re.fullmatch(r"Booked until \d+:\d\d.[AP]M", page.locator("#chip-text").inner_text())
        join_the_booking(server, "now")
        wait_for("the meeting to be joined", lambda: status_of(server)["meeting"]["state"] == "connected")
        poll(page)
        assert re.fullmatch(r"In use until \d+:\d\d.[AP]M", page.locator("#chip-text").inner_text())
        page.close()


def test_the_chip_and_the_room_name_follow_the_sign_across_560_px_without_waiting_for_a_poll(browser, central_time):
    """The chip's words and the name's type are those of the sign's height now: its polls are stopped here and the font events switched off, so only the
    media query's change, and the resize, can bring the new words and type, and they do, both ways."""
    skip_if_day_ends_within(3)
    with PageServer(calendar_events=[event("t0", "Standup", 8)], room_name="Conference Room Twelve", room_location="2nd floor") as server:
        page = sign_on_a_stopped_clock(browser, server, 1280, 800, init_script="document.fonts.addEventListener = () => {};", state="soon")
        words = lambda: page.locator("#chip-text").inner_text()
        assert re.fullmatch(r"Standup starts in \d+ minutes?", words()) and not room_name_is_long(page)
        for width, height, short in ((800, 480, True), (1280, 800, False), (1280, 560, True), (1280, 561, False)):
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(150)                                                  # the page's own timers are stopped: this is the test's time
            assert bool(re.fullmatch(r"Starts in \d+ minutes?", words())) == short and room_name_is_long(page) == short, (width, height, words())
            assert short or re.fullmatch(r"Standup starts in \d+ minutes?", words()), (width, height, words())
        page.close()


@pytest.mark.parametrize("name", ["Conference Room Twelve", "Second Floor Conf Room 2"])
def test_on_a_sign_560_px_high_or_less_a_room_name_that_would_take_two_lines_takes_the_smaller_type(browser, name, central_time):
    """Two lines of the large type, a location, a chip and a detail are more than a sign 560 px high or less has, with the strip up or down: by 1 to 6
    px at 1280 by 480 and 1366 by 500 with the strip up, 14 to 21 px with a long title under the chip and none. At 561 px and above the name keeps
    the large type, as it does in portrait."""
    with PageServer(room_name=name, room_location="2nd floor, by the kitchen") as server:
        for width, height, short in ((1280, 480, True), (1366, 500, True), (800, 480, True), (900, 500, True), (1024, 500, True), (1280, 560, True),
                                     (1280, 561, False), (1280, 800, False), (800, 1280, False)):
            page = open_sign(browser, server, width, height)
            assert room_name_is_long(page) == short, (width, height)
            assert lines_of(page, "#room-name")[0] <= 2 and fits(page), (width, height, lines_of(page, "#room-name"))
            if not short:
                assert lines_of(page, "#room-name") == [2], (width, height)                              # the large type: two lines, as before
            page.close()


def how_far_past_the_bottom(browser, name, location, state, sizes, title=BOOKING_TITLES[1]):
    """{(strip up, width, height): px the sign runs past the bottom (0 when it is whole)} for a room that is free all day, starting soon, or booked."""
    skip_if_day_ends_within(3)
    bookings = {"free all day": [], "starting soon": [event(f"t{n}", title, 8 + 60 * n) for n in range(3)],
                "booked": [event("now", title, -10, duration=50)] + [event(f"t{n}", "Later", 60 + 60 * n) for n in range(2)]}[state]
    found = {}
    with PageServer(calendar_events=bookings, room_name=name, room_location=location, schedules=every_school()) as server:
        for scores in ([], ["texas"]):
            choose(server, sign_scores=scores)
            if scores:
                scoreboard_has(server, scores)
            for width, height in sizes:
                page = open_sign(browser, server, width, height, state={"free all day": "free", "starting soon": "soon", "booked": "occupied"}[state])
                if scores:
                    strip_is_up(page)
                found[(bool(scores), width, height)] = max(0, page.evaluate("document.documentElement.scrollHeight") - height)
                page.close()
    return found


def a_room_in_a_status(status, title=BOOKING_TITLES[1]):
    """(the bookings, the state the sign reaches) for a room that is in `status`, one of STATUSES."""
    now_booking = [event("now", title, -10, duration=50)] + [event(f"t{n}", "Later", 60 + 60 * n) for n in range(2)]
    bookings = {"free": [event(f"t{n}", title, 40 + 60 * n) for n in range(3)], "free all day": [], "starting soon": [event(f"t{n}", title, 8 + 60 * n) for n in range(3)],
                "booked": now_booking, "in use": now_booking, "not connected": [event(f"t{n}", title, 40 + 60 * n) for n in range(3)]}[status]
    reached = {"free": "free", "free all day": "free", "starting soon": "soon", "booked": "occupied", "in use": "occupied", "not connected": "free"}[status]
    return bookings, reached


def sign_in_a_status(browser, server, status, reached, width, height):
    """The sign at the size with the room in `status` (for "not connected": the room is lost after the sign has seen it, and the name and the strip stay)."""
    if status == "not connected":
        page = sign_on_a_stopped_clock(browser, server, width, height)
        page.route("**/api/status", lambda route: route.abort())
        with page.expect_event("requestfailed"):
            page.clock.run_for(5000)
        page.wait_for_function("document.body.dataset.state === 'offline'", timeout=5000)
        return page
    return open_sign(browser, server, width, height, state=reached)


@pytest.mark.parametrize("status", STATUSES)
def test_nothing_runs_off_a_sign_480_to_500_px_high_in_any_status(browser, status, central_time):
    """800 by 480 is the Raspberry Pi's 7-inch panel: whatever the room is doing, with the strip up or down, a name of up to two lines of the smaller type
    (the longest is 26 characters), a location of two lines and a booking title of 60 characters, at 800 by 480, 900 by 500, 1024 by 480, 1024 by 500,
    1280 by 480 and 1366 by 500 the sign stays whole: the chip, the status under it and the strip are on the screen. (Before this, a two-line name and a
    long title in the chip pushed it up to 72 px off.) A name that would take more lines goes down a size at a time, as the next tests say."""
    skip_if_day_ends_within(3)
    bookings, reached = a_room_in_a_status(status)
    for name in ("Conference Room Twelve", LONGEST_TWO_LINE_NAME):                                   # two lines in the large type, and the longest of two in the smaller
        with PageServer(calendar_events=bookings, room_name=name, room_location=TWO_LINE_LOCATION, schedules=every_school()) as server:
            if status == "in use":
                join_the_booking(server, "now")
            for scores in ([], ["texas"]):
                choose(server, sign_scores=scores)
                if scores:
                    scoreboard_has(server, scores)
                for width, height in SHORT_SIGNS:
                    page = sign_in_a_status(browser, server, status, reached, width, height)
                    if scores:
                        strip = page.locator("#score").bounding_box()
                        assert strip["y"] >= 0 and strip["y"] + strip["height"] <= height + 0.5, ("the strip is not wholly on the sign", name, width, height, strip)
                    chip = page.locator(".chip").bounding_box()
                    assert chip["y"] + chip["height"] <= (page.locator("#score").bounding_box()["y"] if scores else height) + 0.5, ("the chip", name, width, height, chip)
                    assert fits(page), (status, name, bool(scores), width, height, page.evaluate("document.documentElement.scrollHeight") - height)
                    page.close()


# --- the status column's free text takes two lines at most (the whole-branch review) ---

def skip_if_the_day_began_within(minutes):
    """A booking that began `minutes` ago is yesterday's, and not on today's list, until the local day is that old: what needs it there skips till then."""
    now = datetime.now().astimezone()
    if now - now.replace(hour=0, minute=0, second=0, microsecond=0) < timedelta(minutes=minutes):
        pytest.skip(f"less than {minutes} minutes have passed in the local day")


LONG_TITLE = ("Quarterly planning review with the regional leadership teams and the partners, " * 3)[:230]
LONG_LOCATION = ("2nd floor, by the kitchen, past the elevators and through the double doors, " * 4)[:230]


@pytest.mark.parametrize("status", ["free", "starting soon", "booked", "in use"])
def test_a_booking_title_and_a_location_of_230_characters_take_two_lines_in_the_status_and_the_sign_fits(browser, status, central_time):
    """The detail line, the chip's words and the location had no limit: a title of 230 characters ran 7 px off an 800 by 480 sign (strip down), 92 px off a
    1366 by 500 one, and in "starting soon" the chip pushed the strip 36 to 68 px off at 1280 by 561 and 1024 by 600. Each is two lines and an ellipsis; every
    character is still in the page, and the sign is whole at the four sizes, with the strip up or down."""
    skip_if_day_ends_within(3)
    if status in ("booked", "in use"):
        skip_if_the_day_began_within(15)                                      # the booking in progress began 10 minutes ago: today's list has it only if that was today
    assert len(LONG_TITLE) == len(LONG_LOCATION) == 230
    bookings, reached = a_room_in_a_status(status, title=LONG_TITLE)
    with PageServer(calendar_events=bookings, room_name="Boardroom", room_location=LONG_LOCATION, schedules=every_school()) as server:
        if status == "in use":
            join_the_booking(server, "now")
        for scores in ([], ["texas"]):
            choose(server, sign_scores=scores)
            if scores:
                scoreboard_has(server, scores)
            for width, height in ((800, 480), (1024, 600), (1280, 561), (1366, 500)):
                page = open_sign(browser, server, width, height, state=reached)
                if scores:
                    strip_is_up(page)
                where = (status, bool(scores), width, height)
                assert fits(page), (*where, page.evaluate("document.documentElement.scrollHeight") - height)
                for selector in ("#detail", "#chip-text", "#room-location"):
                    assert max(lines_of(page, selector)) <= 2, (*where, selector, lines_of(page, selector))
                assert page.locator("#room-location").text_content() == LONG_LOCATION                              # whole in the page, whatever is drawn
                words = page.locator("#chip-text").text_content() + " | " + page.locator("#detail").text_content()
                assert (LONG_TITLE in words) == (status != "starting soon" or height > 560), (*where, words)       # the status carries the title, but the short chip
                assert page.locator("#events .title").first.text_content() == LONG_TITLE, where
                page.close()


# --- a long name steps down on a short sign until it takes two lines (the task 5 review, round 1c) ---

STEPPED_NAMES = {36: "Executive Conference Room North Wing", 45: "Executive Conference Room North Wing, Floor 2",
                 55: "The Executive Conference Room, North Wing, Second Floor"}
STEPPED_SIZES = ((800, 480), (1024, 500), (1280, 480), (1366, 500))
FLOOR_REM = 1.3                                                                        # the smallest a name is set on a short sign, and the step is 0.1 rem


def room_name_type(page):
    """(its size in rem, the inline size it was stepped to or "", how many lines it takes)."""
    return page.evaluate("""() => { const r = document.getElementById('room-name'), rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
        return [parseFloat(getComputedStyle(r).fontSize) / rem, r.style.fontSize, Math.round(r.getBoundingClientRect().height / parseFloat(getComputedStyle(r).lineHeight))]; }""")


@pytest.mark.parametrize("status", STATUSES)
def test_on_a_sign_560_px_high_or_less_a_name_that_still_takes_more_than_two_lines_steps_down_until_it_takes_two(browser, status, central_time):
    """After the smaller type a name of 36, 45 or 55 characters still takes three lines on the narrow signs, and three lines of it ran off the bottom of an
    800 by 480 sign (8 px, free all day, with the strip up and a location of two lines). It goes down 0.1 rem at a time, to 1.3 rem, until it takes two
    lines: in every status, with the strip up and a location of two lines, at the four sizes, the name is two lines or less (or is at the floor), is
    whole in the page, sits on the 0.1 rem steps, and the sign is whole."""
    skip_if_day_ends_within(3)
    bookings, reached = a_room_in_a_status(status)
    for length, name in STEPPED_NAMES.items():
        assert len(name) == length
        with PageServer(calendar_events=bookings, room_name=name, room_location=TWO_LINE_LOCATION, schedules=every_school()) as server:
            choose(server, sign_scores=["texas"])
            scoreboard_has(server, ["texas"])
            if status == "in use":
                join_the_booking(server, "now")
            for width, height in STEPPED_SIZES:
                page = sign_in_a_status(browser, server, status, reached, width, height)
                size, stepped, lines = room_name_type(page)
                assert page.locator("#room-name").inner_text() == name, (length, width, height)              # whole: smaller, never cut
                assert lines <= 2 or size == pytest.approx(FLOOR_REM, abs=0.01), (status, length, width, height, size, lines)
                assert not stepped or abs(size * 10 - round(size * 10)) < 0.02 and size >= FLOOR_REM - 0.01, (length, width, height, size, stepped)
                assert lines > 2 or fits(page), (status, length, width, height, size, page.evaluate("document.documentElement.scrollHeight") - height)
                page.close()


def test_a_stepped_name_goes_back_to_the_stylesheets_size_when_the_sign_grows_past_560_px_and_steps_again_when_it_shrinks(browser, central_time):
    """The steps are for a short sign only, so they are undone when the sign is a pixel taller than 560 and made again when it is not: with its polls
    stopped and the font events off, only the resize can do it."""
    with PageServer(room_name=STEPPED_NAMES[55], room_location="2nd floor") as server:
        page = sign_on_a_stopped_clock(browser, server, 800, 480, init_script="document.fonts.addEventListener = () => {};")
        size, stepped, lines = room_name_type(page)
        assert stepped and lines == 2 and size < 2.4, (size, stepped, lines)                    # 2.4 rem is the smaller type at 800 px wide
        for width, height, short in ((1280, 800, False), (800, 480, True), (1280, 561, False), (1280, 560, True), (1280, 561, False)):
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(150)                                                  # the page's own timers are stopped: this is the test's time
            size, stepped, lines = room_name_type(page)
            rem = min(max(16, 0.0125 * max(width, height)), 24)
            smaller = min(max(2.2 * rem, 0.048 * width), 3.6 * rem) / rem                  # the stylesheet's .long size in rem
            assert bool(stepped) == short and (size < smaller - 0.05 if short else size == pytest.approx(smaller)), (width, height, size, stepped)
            assert lines <= 2 or not short, (width, height, lines)
        page.close()


@pytest.mark.parametrize("length", [45, 55, 67])
def test_a_stepped_name_resized_from_one_short_sign_to_another_ends_where_a_fresh_load_at_the_new_size_puts_it(browser, length, central_time):
    """The steps begin from the stylesheet's size every time, so a name stepped for 800 by 480 and then resized to 1280 by 480 (and on to 1024 by 500, and round the
    four short sizes and back) has the size and the lines of a sign loaded at the new size. The page's polls are stopped here and its font events off."""
    name = {45: STEPPED_NAMES[45], 55: STEPPED_NAMES[55], 67: FLOOR_NAMES[67]}[length]
    assert len(name) == length
    walk = ((800, 480), (1280, 480), (1024, 500), (1366, 500), (800, 480), (1366, 500), (1024, 500), (1280, 480))
    with PageServer(room_name=name, room_location="2nd floor") as server:
        fresh = {}
        for size in STEPPED_SIZES:
            page = open_sign(browser, server, *size)
            page.wait_for_function("document.fonts.status === 'loaded'", timeout=5000)
            page.wait_for_timeout(100)                                                          # the font's arrival is fitted a moment later
            size_in_rem, stepped, lines = room_name_type(page)
            fresh[size] = (round(size_in_rem, 2), lines)
            page.close()
        page = sign_on_a_stopped_clock(browser, server, *walk[0], init_script="document.fonts.addEventListener = () => {};")
        for width, height in walk[1:]:
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(150)                                                          # the page's own timers are stopped: this is the test's time
            size_in_rem, stepped, lines = room_name_type(page)
            assert (round(size_in_rem, 2), lines) == fresh[(width, height)], (width, height, size_in_rem, stepped, lines)
        page.close()


@pytest.mark.parametrize("width, height, sizes", [(800, 480, {36: 2.3, 45: 1.9, 55: 1.5, 67: 1.3}), (1280, 480, {36: 3.6, 45: 3.1, 55: 2.4, 67: 2.1})])
def test_a_name_goes_down_to_the_largest_tenth_of_a_rem_at_which_it_takes_two_lines_on_a_short_sign(browser, width, height, sizes):
    """From the smaller type (2.4 rem at 800 px wide, 3.6 rem at 1280) in steps of 0.1 rem, and it stops at the first size at which it takes two lines, so
    the steps are those and not 0.2 rem, and it does not go on to one line. (36 characters are two lines of the smaller type at 1280: no step.)"""
    for length, rem_wanted in sizes.items():
        with PageServer(room_name=FLOOR_NAMES[67] if length == 67 else STEPPED_NAMES[length], room_location="2nd floor") as server:
            page = open_sign(browser, server, width, height)
            size, stepped, lines = room_name_type(page)
            assert size == pytest.approx(rem_wanted, abs=0.001) and lines == 2, (width, length, size, stepped, lines)
            assert bool(stepped) == (length != 36 or width == 800), (width, length, stepped)
            page.close()


def test_a_name_that_needs_the_smaller_type_keeps_it_when_a_tall_sign_is_fitted_again(browser, central_time):
    """Fitted again with the smaller type on (the week opens and closes, so the view changes twice), "Sales Conference Room" is measured with it taken off, as it
    is the first time: it needs the type, and has it, as it does on a short sign, where the limit is one line and a name of two lines of it keeps it anyway."""
    skip_if_day_ends_within(3)
    with PageServer(calendar_events=[event("t0", "Standup", 40)], room_name="Sales Conference Room", room_location="2nd floor") as server:
        page = open_sign(browser, server, 1280, 800)
        assert room_name_is_long(page) and lines_of(page, "#room-name") == [2]
        open_the_week(page)
        assert room_name_is_long(page) and lines_of(page, "#room-name") == [2]
        page.click("#week-back")
        page.wait_for_function("document.body.dataset.view === 'today'", timeout=3000)
        assert room_name_is_long(page) and lines_of(page, "#room-name") == [2] and fits(page)
        page.close()


def test_a_name_too_long_for_two_lines_even_at_the_floor_stays_at_the_floor_and_whole_on_a_short_sign(browser):
    """5 x 36 characters is seven lines at 1.3 rem on an 800 px sign and four on a 1280 px one: it goes down to the floor and no further, and none of it is cut."""
    name = " ".join([STEPPED_NAMES[36]] * 5)
    with PageServer(room_name=name, room_location="2nd floor") as server:
        for width, height in ((800, 480), (1280, 480)):
            page = open_sign(browser, server, width, height)
            size, stepped, lines = room_name_type(page)
            assert size == pytest.approx(FLOOR_REM, abs=0.01) and stepped == "1.3rem" and lines > 2, (width, height, size, stepped, lines)
            assert page.locator("#room-name").inner_text() == name
            page.close()


@pytest.mark.parametrize("name", ["Boardroom", "Conference Room Twelve", "Sales Conference Room", "North Wing Training Room 2"])
def test_a_name_that_takes_two_lines_or_less_is_not_stepped_on_a_short_sign(browser, name):
    """One line at the large size, or two lines at the large or the smaller size: the sizes of the stylesheet are the sizes it has, with no step."""
    with PageServer(room_name=name, room_location="2nd floor") as server:
        for width, height in SHORT_SIGNS:
            page = open_sign(browser, server, width, height)
            size, stepped, lines = room_name_type(page)
            assert stepped == "" and lines <= 2, (name, width, height, size, stepped, lines)
            page.close()


def test_a_poll_that_changes_nothing_rewrites_nothing_on_the_signs_stepped_name(browser):
    with PageServer(room_name=STEPPED_NAMES[55]) as server:
        page = sign_on_a_stopped_clock(browser, server, 800, 480)
        assert room_name_type(page)[1]                                                      # it is stepped
        page.evaluate("""() => {
            window.records = [];
            new MutationObserver((list) => list.forEach((r) => window.records.push(r.type + ':' + (r.attributeName || ''))))
                .observe(document.getElementById('room-name'), { subtree: true, childList: true, characterData: true, attributes: true });
        }""")
        poll(page)
        poll(page)
        assert page.evaluate("window.records") == []                                        # neither measured nor stepped again
        page.close()


FLOOR_NAMES = {67: "The Executive Conference Room, North Wing, Second Floor, East Annex",
               87: "The Executive Conference Room, North Wing, Second Floor, East Annex, Building B, Austin",
               110: " ".join([STEPPED_NAMES[36]] * 3), 147: " ".join([STEPPED_NAMES[36]] * 4), 184: " ".join([STEPPED_NAMES[36]] * 5)}


def test_the_limit_the_docs_state_for_a_name_at_the_floor_is_the_one_the_sign_has(browser, central_time):
    """What the guide and the README say is left on a screen 480 to 500 px high: a name goes down to 1.3 rem and no further. A name of up to 67 characters takes
    two lines at that size and one of 87 to 147 characters three to five (at 800 by 480), and the sign is whole at the six sizes, in every status, with a location
    of two lines and the strip up or down. A room name over about 150 characters runs off the sign (the docs say to use a shorter one, and no number of pixels). When
    one of these stops being so, the docs change."""
    assert {length: len(name) for length, name in FLOOR_NAMES.items()} == {length: length for length in FLOOR_NAMES}
    for length in (67, 87, 110, 147):
        found = how_far_past_the_bottom(browser, FLOOR_NAMES[length], TWO_LINE_LOCATION, "free all day", SHORT_SIGNS)
        assert set(found.values()) == {0}, (length, found)
    for length, lines in ((67, 2), (87, 3), (147, 5)):
        with PageServer(room_name=FLOOR_NAMES[length], room_location=TWO_LINE_LOCATION) as server:
            page = open_sign(browser, server, 800, 480)
            size, stepped, taken = room_name_type(page)
            assert (round(size, 2), taken) == (FLOOR_REM if length > 67 else size, lines), (length, size, stepped, taken)
            page.close()
    too_long = how_far_past_the_bottom(browser, FLOOR_NAMES[184], TWO_LINE_LOCATION, "free all day", ((800, 480),))
    assert max(too_long.values()) > 0, too_long                                  # a name of 184 characters (seven lines at the floor) runs off the sign


# --- a long name steps down at any height when the status would not fit (the whole-branch review) ---

BAND_SIGNS = ((1024, 600), (1280, 561), (1280, 600), (1366, 620))            # 561 to 700 px high, where the steps were not made


@pytest.mark.parametrize("status", ["free", "starting soon"])
@pytest.mark.parametrize("scores", [[], ["texas"]], ids=["strip down", "strip up"])
def test_a_long_name_steps_down_on_a_sign_taller_than_560_px_until_the_status_fits(browser, status, scores, central_time):
    """The steps were made for a sign 560 px high or less only. Above it, a name of 67 or 87 characters (four or five lines of the smaller type) ran the chip, the
    detail and the strip 9 to 112 px off a sign 561 to 620 px high. At any height the name now goes down 0.1 rem at a time, to 1.3 rem, until the sign is whole:
    in free and in starting soon, with a location of two lines and the strip up or down, at four sizes between 561 and 620 px, with every character of the name in
    the page."""
    skip_if_day_ends_within(3)
    bookings, reached = a_room_in_a_status(status)
    for length in (67, 87):
        name = FLOOR_NAMES[length]
        with PageServer(calendar_events=bookings, room_name=name, room_location=TWO_LINE_LOCATION, schedules=every_school()) as server:
            choose(server, sign_scores=scores)
            if scores:
                scoreboard_has(server, scores)
            for width, height in BAND_SIGNS:
                page = open_sign(browser, server, width, height, state=reached)
                if scores:
                    strip_is_up(page)
                size, stepped, lines = room_name_type(page)
                where = (status, length, bool(scores), width, height, size, stepped, lines)
                assert fits(page), (*where, page.evaluate("document.documentElement.scrollHeight") - height)
                assert page.locator("#room-name").inner_text() == name, where                                      # whole: smaller, never cut
                assert size >= FLOOR_REM - 0.01 and (not stepped or abs(size * 10 - round(size * 10)) < 0.02), where       # on the 0.1 rem steps, to the floor
                page.close()


@pytest.mark.parametrize("width, height", [(1280, 700), (1280, 800), (1920, 1080)])
def test_a_long_name_keeps_the_stylesheets_size_on_a_sign_that_it_does_not_run_off(browser, width, height, central_time):
    """The steps are for a sign that would run off: a name of 67 characters on a sign with room for it is the smaller type of the stylesheet, as it was."""
    skip_if_day_ends_within(3)
    bookings, reached = a_room_in_a_status("starting soon")
    with PageServer(calendar_events=bookings, room_name=FLOOR_NAMES[67], room_location=TWO_LINE_LOCATION) as server:
        page = open_sign(browser, server, width, height, state=reached)
        size, stepped, lines = room_name_type(page)
        assert stepped == "" and room_name_is_long(page) and fits(page), (width, height, size, stepped, lines)
        page.close()


def test_a_name_stepped_above_560_px_is_fitted_again_when_the_status_changes_and_when_the_sign_is_resized(browser, central_time):
    """Fitted again by the triggers a short sign's steps have (here the resize, with the page's polls stopped and its font events off), and by a status that
    changes too: at 1280 by 597 with the strip up, the 67-character name fits beside "Next: Standup" (one line of detail) and does not beside the two lines
    of "Sign not connected". It goes down when the room is lost, goes back to the stylesheet's size when the sign has room to spare, is stepped again to the
    size it had, and has the stylesheet's size once the room is back."""
    skip_if_day_ends_within(3)
    with PageServer(calendar_events=[event("t0", "Standup", 40), event("t1", "Later", 100)], room_name=FLOOR_NAMES[67], room_location=TWO_LINE_LOCATION,
                    schedules=every_school()) as server:
        choose(server, sign_scores=["texas"])
        scoreboard_has(server, ["texas"])
        page = sign_on_a_stopped_clock(browser, server, 1280, 597, init_script="document.fonts.addEventListener = () => {};")
        strip_is_up(page)
        assert room_name_type(page)[1] == "" and fits(page)                                   # free, with one line of detail: the stylesheet's size
        page.route("**/api/status", lambda route: route.abort())
        with page.expect_event("requestfailed"):
            page.clock.run_for(5000)
        page.wait_for_function("document.body.dataset.state === 'offline'", timeout=5000)
        size, stepped, lines = room_name_type(page)
        assert stepped and fits(page), (size, stepped, lines)                                  # two lines of detail: the name has gone down to make the room
        for width, height, steps in ((1280, 900, False), (1280, 597, True)):
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(150)                                                         # the page's own timers are stopped: this is the test's time
            assert (room_name_type(page)[1] != "") == steps and fits(page), (width, height)
            assert not steps or room_name_type(page)[0] == pytest.approx(size)                 # the same steps as before
        page.unroute("**/api/status")
        poll(page)
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert room_name_type(page)[1] == "" and fits(page)                                    # the room is back: one line of detail, and the stylesheet's size
        page.close()


def test_a_name_is_not_stepped_for_a_sign_that_runs_off_because_of_the_today_list(browser, central_time):
    """The Today list beside the status is not the name's to cure: with a list taller than the status the name keeps the stylesheet's size, however far
    the sign runs off. (The list is made 600 px taller here, with a style rule of the test's own, so that it is the one that runs off.)"""
    skip_if_day_ends_within(3)
    with PageServer(calendar_events=[event(f"t{n}", f"Today {n}", 40 + 60 * n) for n in range(3)], room_name=FLOOR_NAMES[67], room_location="2nd floor") as server:
        page = sign_on_a_stopped_clock(browser, server, 1280, 800, init_script="document.fonts.addEventListener = () => {};")
        assert fits(page) and room_name_type(page)[1] == ""
        page.add_style_tag(content="#events { margin-bottom: 600px; }")
        page.set_viewport_size({"width": 1281, "height": 800})                                  # fitted again, with a list that runs the sign off
        page.wait_for_timeout(150)
        size, stepped, lines = room_name_type(page)
        assert not fits(page) and stepped == "" and lines == 4, (size, stepped, lines)         # the name is as the stylesheet has it: smaller type, four lines
        page.close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_door_sign_goes_offline_and_recovers(browser):
    port = free_port()
    page = browser.new_page(viewport={"width": 1024, "height": 600})
    with PageServer(calendar_events=[event("e1", "Design review", 25)], room_name="Room 1", port=port):
        page.goto(f"http://127.0.0.1:{port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
    page.wait_for_function("document.body.dataset.state === 'offline'", timeout=10000)
    assert page.locator("#chip-text").inner_text() == "Sign not connected"
    rail, chip, body = sign_colors(page)
    assert rail == chip and rail not in (SIGN_GREEN, SIGN_AMBER, SIGN_RED) and body == SIGN_NAVY   # a sign that lost its room claims no status
    with PageServer(calendar_events=[event("e1", "Design review", 25)], room_name="Room 1", port=port):
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=10000)
    page.close()


def test_the_screen_picker_posts_the_style_and_marks_the_current_one(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        open_screensaver_screen(page)
        picker = page.locator("#screen-picker")
        assert picker.is_visible()
        assert picker.locator("button[aria-pressed='true']").inner_text() == "Information"
        page.click("#screen-picker button:has-text('Bounce')")
        page.wait_for_function("document.querySelector(\"#screen-picker button[aria-pressed='true']\").innerText === 'Bounce'", timeout=5000)
        assert page.request.get(f"http://127.0.0.1:{server.port}/api/screensaver").json()["style"] == "bounce"
        assert picker.locator("button[aria-pressed='true']").count() == 1
        page.close()


# --- icons: one Lucide sprite, and an icon before every button's label ---

SPRITE_ICONS = ["mic", "mic-off", "video", "video-off", "hand", "smile", "captions", "layout", "user", "users", "message", "phone-off",
                "record", "lock", "unlock", "door", "x-circle", "volume", "move", "monitor", "settings", "calendar", "back", "check"]
TV_REMOTE_ICONS = ["tv", "power", "home", "menu", "up", "down", "left", "right", "volume-x", "hash", "info", "list", "play", "pause",
                   "stop", "rewind", "fast-forward", "exit", "return", "pointer", "touchpad", "keyboard"]   # for the TV remote's buttons, which come with another branch
CAMERA_ICONS = ["zoom-in", "zoom-out", "save", "locate"]   # the camera screen's Zoom in, Zoom out, Save and Find the stops
REACTION_ICONS = ["thumbs-up", "heart", "laugh", "party-popper"]   # the reaction row's: Thumbs up, Heart, Laugh and Celebrate (Clap is the hand, Wow the smile)
DRAWABLE = {"path", "line", "circle", "rect", "polyline", "polygon", "ellipse"}


def test_the_icon_sprite_holds_every_symbol_and_each_one_draws_something():
    from xml.etree import ElementTree
    from croom.control.service import STATIC_DIR
    svg = "{http://www.w3.org/2000/svg}"
    symbols = list(ElementTree.parse(STATIC_DIR / "icons.svg").getroot().iter(svg + "symbol"))
    ids = [s.get("id") for s in symbols]
    assert len(ids) == len(set(ids)), "a symbol is there twice"
    assert {f"icon-{name}" for name in SPRITE_ICONS + TV_REMOTE_ICONS + CAMERA_ICONS + REACTION_ICONS} <= set(ids)
    for symbol in symbols:
        who = symbol.get("id")
        assert [symbol.get(a) for a in ("viewBox", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin")] == \
            ["0 0 24 24", "none", "currentColor", "2", "round", "round"], who   # Lucide's own: drawn in the text's colour, whatever the size
        shapes = [child.tag.replace(svg, "") for child in symbol]
        assert shapes and set(shapes) <= DRAWABLE, f"{who} draws {shapes}"      # at least one shape, and nothing but shapes


def test_the_icons_licence_says_where_they_come_from():
    from croom.control.service import STATIC_DIR
    text = (STATIC_DIR / "LICENSES.md").read_text(encoding="utf-8")
    assert "Lucide" in text and "https://lucide.dev" in text and "ISC" in text and "lucide-react 0.303.0" in text


def drawn_icons(page, scope):
    """What each button inside the scope says and draws, in order: "Mute: #icon-mic" (a button with no icon draws nothing)."""
    return page.evaluate("""(scope) => [...document.querySelectorAll(scope + ' .button')].map(b =>
        b.textContent.trim() + ': ' + [...b.querySelectorAll('svg.icon use')].map(u => u.getAttribute('href')).join(' '))""", scope)


def test_every_button_on_the_meeting_screen_has_an_icon_and_the_label_stays_its_name(browser):
    primary = ["Mute", "Turn camera off", "Raise hand", "Reactions", "Leave"]
    secondary = ["Captions on", "Gallery view", "Hide self view", "Participants (3)", "Chat", "Host", "TV"]
    with PageServer(role="host") as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button svg use')", timeout=5000)
        assert texts(page, "#meeting-actions") == primary and texts(page, "#meeting-secondary") == secondary
        for b in page.locator("#meeting-actions .button, #meeting-secondary .button").all():
            assert b.locator("svg.icon use").count() == 1 and b.locator("svg").get_attribute("aria-hidden") == "true"
        assert page.locator("#open-options svg use").count() == 0 or page.locator("#open-options").is_hidden()
        for name in primary + secondary:                                     # a screen reader still reads the label alone
            assert page.get_by_role("button", name=name, exact=True).count() == 1, name
        page.close()


def test_the_idle_buttons_carry_icons_too(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.wait_for_function("document.querySelector('#open-options svg use')", timeout=5000)
        assert page.locator("#open-options").inner_text().strip() == "Options"
        assert page.locator("#link-form button[type=submit] svg use").count() == 1
        page.close()


def test_the_mute_and_camera_icons_show_the_state_and_their_labels_the_action(browser):
    """As Zoom's own buttons do: the picture is what the microphone and the camera are doing now, the words what a tap will do."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-actions .button svg use')", timeout=5000)
        assert drawn_icons(page, "#meeting-actions") == ["Mute: #icon-mic", "Turn camera off: #icon-video", "Raise hand: #icon-hand",
                                                         "Reactions: #icon-smile", "Leave: #icon-phone-off"]   # both are live
        page.click("#meeting-actions button:has-text('Mute')")
        page.wait_for_selector("#meeting-actions button:has-text('Unmute')", timeout=5000)
        page.click("#meeting-actions button:has-text('Turn camera off')")
        page.wait_for_selector("#meeting-actions button:has-text('Turn camera on')", timeout=5000)
        assert drawn_icons(page, "#meeting-actions") == ["Unmute: #icon-mic-off", "Turn camera on: #icon-video-off", "Raise hand: #icon-hand",
                                                         "Reactions: #icon-smile", "Leave: #icon-phone-off"]   # both are off
        page.click("#meeting-actions button:has-text('Leave')")
        assert drawn_icons(page, "#meeting-actions")[-1] == "Tap again to leave: #icon-phone-off"   # the second tap keeps its picture
        page.close()


def test_join_cancel_and_dismiss_carry_icons(browser):
    skip_if_day_ends_within(1)                                               # the bookings start in 5 and 40 minutes, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Design review", 5), event("e2", "Retro", 40)]) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        page.wait_for_function("document.querySelector('#actions .button svg use')", timeout=5000)
        assert drawn_icons(page, "#actions") == ["Join now: #icon-video"]
        assert drawn_icons(page, "#events") == ["Join: #icon-video"]         # the row of the booking that is inside its ten minutes
        server.meeting.fail_next_join = True
        page.fill("#link-input", "98765432100")
        page.click("#link-form button[type=submit]")
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=5000)
        assert drawn_icons(page, "#actions") == ["Dismiss: #icon-x-circle"]
        page.click("#actions button:has-text('Dismiss')")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        join_by_id(page)
        server.meeting.set_state(MeetingState.JOINING)
        page.wait_for_function("document.querySelector('#meeting-actions button').innerText.trim() === 'Cancel'", timeout=5000)
        assert drawn_icons(page, "#meeting-actions") == ["Cancel: #icon-x-circle"]
        page.close()


def test_join_again_and_a_join_that_waits_for_its_window_carry_icons(browser):
    skip_if_day_ends_within(1)                                               # the booking starts in 40 minutes, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Retro", 40)]) as server:
        server.meeting.fail_before_joining = RuntimeError(PASSCODE_ASKED)
        page = open_page(browser, server)
        page.wait_for_function("document.querySelector('#actions .button svg use')", timeout=5000)
        waiting = drawn_icons(page, "#actions")
        assert len(waiting) == 1 and waiting[0].startswith("Join opens at ") and waiting[0].endswith(": #icon-video")
        assert page.locator("#actions .button").is_disabled()                # the picture is there on a button that cannot be pressed yet
        page.fill("#link-input", "5590000559")
        submit_and_wait_for_the_card(page)
        assert drawn_icons(page, "#actions") == ["Dismiss: #icon-x-circle", "Join again: #icon-video"]
        page.close()


# What index.html ships: each button's symbol, and the label it keeps.
STATIC_BUTTON_ICONS = [
    ("#open-options", "settings", "Options"), ("#open-camera-setup", "move", "Camera framing"), ("#open-screensaver", "monitor", "Screensaver"),
    ("#open-camera-live", "move", "Camera"), ("#open-upcoming", "calendar", "Upcoming meetings"),
    ("#link-form button[type=submit]", "video", "Join"),
    ("#quieter", "volume", "Quieter"), ("#louder", "volume", "Louder"), ("#speaker-mute", "volume", "Mute speaker"),
    ("#zoom-out", "zoom-out", "Zoom out"), ("#zoom-in", "zoom-in", "Zoom in"), ("#preset-save-mode", "save", "Save"),
    ("#find-stops", "locate", "Find the stops"), ("#save-home", "home", "Save as Home"),
    ("#key-click-on", "volume", "Key click on"), ("#key-click-off", "volume-x", "Key click off"),
    ("#keyboard-toggle", "keyboard", ""),                                    # the toggle is the picture alone: its name is its aria-label
]


def test_the_buttons_in_the_page_carry_their_icons_and_keep_their_labels(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.wait_for_function("document.querySelector('#open-options svg use')", timeout=5000)
        for selector, name, label in STATIC_BUTTON_ICONS:
            b = page.locator(selector)
            assert b.count() == 1, selector
            assert b.locator("svg.icon").count() == 1 and b.locator("svg.icon use").get_attribute("href") == f"#icon-{name}", selector
            assert b.locator("svg.icon").get_attribute("aria-hidden") == "true", selector
            assert b.text_content().strip() == label, selector
        backs = page.locator(".back").all()
        assert backs and all(b.locator("svg.icon use").get_attribute("href") == "#icon-back" and b.text_content().strip() == "Back" for b in backs)
        assert page.get_by_role("button", name="Show keyboard", exact=True).count() == 1       # the toggle still has its name
        page.close()


def test_the_presets_the_pad_and_the_screensaver_picker_keep_their_plain_labels(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        page.evaluate("window.croomPage.iconsReady")
        assert page.locator("#arrow-pad .button").count() == 5 and page.locator("#arrow-pad svg").count() == 0   # the pad keeps its arrows
        presets = page.locator("#preset-row .button")
        assert presets.count() >= 1 and page.locator("#preset-row svg").count() == 0
        assert all(label.strip() for label in presets.all_inner_texts())
        page.click("#back-from-camera")
        page.click("#open-screensaver")
        assert page.locator("#screen-picker .button").count() == 4 and page.locator("#screen-picker svg").count() == 0
        page.close()


def test_the_save_button_keeps_its_icon_when_it_changes_its_words(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        assert drawn_icons(page, "#camera-setup") == ["Find the stops: #icon-locate", "Save as Home: #icon-home"]
        assert drawn_icons(page, ".zoom-buttons") == ["Zoom out: #icon-zoom-out", "Zoom in: #icon-zoom-in"]
        page.click("#preset-save-mode")
        page.wait_for_function("document.getElementById('preset-save-mode').innerText.trim() === 'Tap a preset to save it here'", timeout=5000)
        assert page.locator("#preset-save-mode svg.icon use").get_attribute("href") == "#icon-save"
        assert page.locator("#preset-save-mode svg.icon").count() == 1
        page.close()


def test_the_speaker_icon_shows_the_state_and_its_label_the_action(browser):
    """The rule of the microphone and the camera: the picture is what the speaker is doing now, the words what a tap will do."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#speaker-mute svg use')", timeout=5000)
        assert drawn_icons(page, ".sound-buttons")[-1] == "Mute speaker: #icon-volume"             # the speaker is live
        page.click("#speaker-mute")
        page.wait_for_function("document.getElementById('speaker-mute').innerText.trim() === 'Unmute speaker'", timeout=5000)
        assert drawn_icons(page, ".sound-buttons")[-1] == "Unmute speaker: #icon-volume-x"         # and now muted
        assert page.locator("#speaker-mute").get_attribute("aria-pressed") == "true"
        assert page.locator("#speaker-mute svg.icon").count() == 1                                 # one picture, not a second beside the first
        page.click("#speaker-mute")
        page.wait_for_function("document.getElementById('speaker-mute').innerText.trim() === 'Mute speaker'", timeout=5000)
        assert drawn_icons(page, ".sound-buttons")[-1] == "Mute speaker: #icon-volume"             # live again
        page.close()


@pytest.mark.parametrize("button, redraw", [
    ("#speaker-mute", "window.croomPage.model.status.audio.level += 5"),             # a volume change draws the speaker button again
    ("#preset-save-mode", "window.croomPage.model.status.camera.zoom.level += 25"),  # a camera change draws Save again
])
def test_a_tap_is_not_lost_when_the_page_draws_the_button_again_with_the_same_words_and_icon(browser, button, redraw):
    """The browser drops a tap whose finger went down on a part of the button (the icon, the words) that is gone at the lift. A button
    the page draws again with nothing changed must keep its parts: the speaker button is drawn at every volume change, Save at every
    camera change."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page) if button == "#speaker-mute" else open_camera_screen(page)
        page.evaluate("window.croomPage.iconsReady")
        page.evaluate("(s) => { window.__taps = 0; document.querySelector(s).addEventListener('click', () => { window.__taps += 1; }); }", button)
        icon = page.locator(f"{button} svg.icon").bounding_box()
        page.mouse.move(icon["x"] + icon["width"] / 2, icon["y"] + icon["height"] / 2)
        page.mouse.down()                                                    # the finger lands on the picture
        page.evaluate(f"() => {{ window.__icon = document.querySelector('{button} svg.icon'); {redraw}; window.croomPage.render(); }}")
        assert page.evaluate(f"document.querySelector('{button} svg.icon') === window.__icon"), "the picture under the finger was replaced"
        page.mouse.up()
        page.wait_for_function(f"document.querySelector('{button}').getAttribute('aria-pressed') === 'true'", timeout=5000)   # the tap did its work
        assert page.evaluate("window.__taps") == 1
        page.close()


def test_a_button_drawn_again_changes_only_what_changed(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.evaluate("window.croomPage.iconsReady")
        seen = page.evaluate("""() => { const P = window.croomPage, b = P.button('Mute', 'quiet', () => {}, false, 'mic');
            const parts = () => [b.querySelector('svg'), b.querySelector('span')], first = parts(), same = () => parts().map((p, i) => p === first[i]);
            P.labelWithIcon(b, 'Mute', 'mic');                      const untouched = same(), text0 = b.textContent;
            P.labelWithIcon(b, 'Unmute', 'mic');                    const words = [same(), b.textContent];     // only the words: the span stays, its text changes
            P.labelWithIcon(b, 'Unmute', 'mic-off');                const picture = [same(), b.querySelector('use').getAttribute('href')];   // only the picture: the use stays, its href changes
            P.labelWithIcon(b, '', 'mic-off');                      const alone = [b.querySelectorAll('span').length, b.querySelectorAll('svg').length];
            P.labelWithIcon(b, 'Mute', '');                         const plain = [b.querySelectorAll('span').length, b.querySelectorAll('svg').length, b.textContent];
            const raw = document.createElement('button'); raw.textContent = 'Options'; P.labelWithIcon(raw, 'Options', 'settings');   // the markup's own text is replaced, not kept beside
            return [untouched, text0, words, picture, alone, plain, raw.textContent, raw.childNodes.length]; }""")
        assert seen == [[True, True], "Mute", [[True, True], "Unmute"], [[True, True], "#icon-mic-off"], [0, 1], [1, 0, "Mute"], "Options", 2]
        page.close()


# Each icon showing, with its symbol, its size in ems, and the height of its button with it and for a moment without it.
ICON_BOXES = """() => [...document.querySelectorAll('svg.icon')].filter(s => s.getClientRects().length).map(s => {
    const use = s.querySelector('use'), shapes = use.getBBox(), box = s.getBoundingClientRect(), b = s.closest('button');
    const em = parseFloat(getComputedStyle(b).fontSize), parent = s.parentNode, next = s.nextSibling, tall = b.getBoundingClientRect().height;
    s.remove();
    const bare = b.getBoundingClientRect().height;
    parent.insertBefore(s, next);
    return { name: use.getAttribute('href'), drawn: shapes.width > 0 && shapes.height > 0, wanted: b.classList.contains('menu-item') ? 1.6 : 1.3,
             em: [box.width / em, box.height / em], symbol: Boolean(document.querySelector(use.getAttribute('href'))), heights: [tall, bare] }; })"""


def shown_icons(page):
    """The symbols showing on this screen, in order, each checked: it draws, at the size of its text, and costs its button no height."""
    page.evaluate("window.croomPage.iconsReady")
    found = page.evaluate(ICON_BOXES)
    for icon in found:
        assert icon["symbol"] and icon["drawn"], f"{icon['name']} draws nothing: the sprite has no such symbol"
        assert all(abs(size - icon["wanted"]) < 0.05 for size in icon["em"]), f"{icon['name']} is {icon['em']} em, not {icon['wanted']}"
        assert abs(icon["heights"][0] - icon["heights"][1]) < 0.01, f"{icon['name']} made its button {icon['heights'][0]} px tall, not {icon['heights'][1]}"
    return [icon["name"] for icon in found]


def assert_includes(shown, wanted):
    """Every wanted symbol is showing, in this order, with room between them for others: Options and the meeting screen get more buttons on other branches."""
    rest = iter(shown)
    assert all(name in rest for name in wanted), f"{wanted} are not all showing, in this order, among {shown}"


@pytest.mark.parametrize("width, height", [(1280, 800), (1920, 1200), (390, 844)])
def test_every_icon_on_every_screen_draws_at_the_size_of_its_text_and_costs_no_height(browser, width, height):
    def settled(page):
        """The kiosk's screens still fit without scrolling, and the phone's still do not scroll sideways."""
        if width >= 1024:
            assert fits(page)
        else:
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")

    with PageServer() as server:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        bar = ["#icon-settings"] if width > 640 else []                      # Options, in the top bar of every idle screen: not up to 640 px, where the bar is full (style.css)
        assert shown_icons(page) == bar + ["#icon-keyboard", "#icon-video"]  # and the keyboard button and Join beside the link box
        settled(page)
        page.click("#open-options")
        assert_includes(shown_icons(page), bar + ["#icon-back", "#icon-move", "#icon-monitor", "#icon-door", "#icon-tv", "#icon-volume", "#icon-volume-x"])   # the four menu items, then Key click on and off
        assert page.locator(".view[data-view=options] .menu-item svg.icon").count() == 4      # each of the four has its icon: Camera framing, Screensaver, Door sign, TV
        settled(page)
        page.click("#open-tv-options")
        assert shown_icons(page) == bar + ["#icon-back", "#icon-locate", "#icon-touchpad"]     # this room has no TV: the card that says so, with Find TVs, and the touchpad for the room Pi's mouse
        settled(page)
        page.click("#back-from-tv")
        page.click("#open-camera-setup")
        assert shown_icons(page) == bar + ["#icon-back", "#icon-zoom-out", "#icon-zoom-in", "#icon-save"]
        settled(page)
        page.click("#setup-link")
        assert shown_icons(page) == bar + ["#icon-back", "#icon-zoom-out", "#icon-zoom-in", "#icon-save", "#icon-locate", "#icon-home"]   # with Set up open
        settled(page)
        page.click("#back-from-camera")
        page.click("#open-screensaver")
        assert shown_icons(page) == bar + ["#icon-back"]
        settled(page)
        page.click("#back-from-screensaver")
        page.click("#back-from-options")
        join_by_id(page)
        assert_includes(shown_icons(page), ["#icon-mic", "#icon-video", "#icon-phone-off", "#icon-tv", "#icon-volume", "#icon-volume", "#icon-volume", "#icon-move", "#icon-calendar"])   # the TV is the last of the second row, above Sound
        settled(page)
        page.click("#open-camera-live")
        assert shown_icons(page) == ["#icon-back", "#icon-zoom-out", "#icon-zoom-in"]    # a meeting's camera screen has no Save or Set up
        settled(page)
        page.click("#back-from-camera")
        page.click("#open-upcoming")
        assert shown_icons(page) == ["#icon-back"]
        settled(page)
        page.close()


@pytest.mark.parametrize("width, room", [(390, "Lab"), (600, "Conference Room Alpha"), (640, "Executive Boardroom East")])
def test_a_narrow_top_bar_keeps_the_room_name_on_one_line_because_options_drops_its_icon(browser, width, room):
    """The icon in Options takes 32 px from the name beside it. Up to 640 px the bar is too full for that: "Lab" would break as "La" and
    "b" at 390, "Conference Room Alpha" takes two lines at 600 with the icon and one without. Options keeps to its word there."""
    with PageServer(room_name=room) as server:
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        page.evaluate("window.croomPage.iconsReady")
        assert page.locator("#open-options svg.icon").is_hidden() and page.locator("#open-options").inner_text().strip() == "Options"
        assert page.locator("#room-name").bounding_box()["height"] < 30     # one line of the name
        page.set_viewport_size({"width": 641, "height": 900})               # from 641 px the bar has the room: the icon is back, and the name still has its line
        assert page.locator("#open-options svg.icon").is_visible()
        assert page.locator("#room-name").bounding_box()["height"] < 30
        page.close()


def test_the_icon_helpers_are_there_for_the_other_scripts(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        assert page.evaluate("window.croomPage.iconsReady") is True
        assert not page.evaluate("document.body.classList.contains('no-icons')")
        assert page.evaluate("getComputedStyle(document.getElementById('keyboard-toggle'), '::after').content") != '"\u2328"'   # the picture is there: no glyph beside it
        made = page.evaluate("""() => { const P = window.croomPage, b = P.button('Raise hand', 'quiet', () => {}, false, 'hand');
            const bare = P.button('Bounce', 'quiet', () => {}, false);
            return [b.textContent, b.querySelector('svg.icon use').getAttribute('href'), b.querySelector('svg').getAttribute('aria-hidden'),
                    bare.textContent, bare.querySelectorAll('svg').length, typeof P.icon, typeof P.labelWithIcon]; }""")
        assert made == ["Raise hand", "#icon-hand", "true", "Bounce", 0, "function", "function"]
        page.close()


SPRITE_ANSWERS = {
    "fails": lambda route: route.abort(),                                                              # the request never gets an answer
    "is missing": lambda route: route.fulfill(status=404, body="no"),                                  # the server has no such file
    "is empty": lambda route: route.fulfill(status=200, content_type="image/svg+xml", body=""),        # it answers, with nothing in it
}


@pytest.mark.parametrize("answer", list(SPRITE_ANSWERS))
def test_a_page_whose_icon_sprite_does_not_come_keeps_its_labels(browser, answer):
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        errors, warnings = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: warnings.append(m.text) if m.type == "warning" else None)
        page.route("**/static/icons.svg", SPRITE_ANSWERS[answer])
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.evaluate("window.croomPage.iconsReady") is False
        assert page.locator("#open-options").inner_text().strip() == "Options"
        assert page.locator("#link-form button[type=submit]").inner_text().strip() == "Join"
        assert page.get_by_role("button", name="Show keyboard", exact=True).count() == 1   # the keyboard button is its name alone, and is still there
        assert len(warnings) == 1 and "icon" in warnings[0].lower(), warnings              # the page says once why it has no pictures
        assert page.evaluate("getComputedStyle(document.getElementById('keyboard-toggle'), '::after').content") == '"\u2328"'   # the keyboard button has the glyph it had before the icons
        join_by_id(page)
        assert page.locator("#meeting-actions .button").all_inner_texts() == ["Mute", "Turn camera off", "Raise hand", "Reactions", "Leave"]
        assert page.locator("#meeting-secondary .button").all_inner_texts() == ["Captions on", "Gallery view", "Hide self view", "Participants (3)", "Chat", "TV"]
        assert page.evaluate("document.body.classList.contains('no-icons')")
        assert page.evaluate("[...document.querySelectorAll('svg.icon')].filter(s => s.getClientRects().length).length") == 0, "an empty icon box still takes room"
        for selector in ("#meeting-actions .button", "#meeting-secondary .button", "#link-form button[type=submit]"):    # the words sit in the middle of their buttons again
            off = page.evaluate("""(s) => [...document.querySelectorAll(s)].map(b => { const w = b.querySelector('span').getBoundingClientRect(), r = b.getBoundingClientRect();
                return Math.abs((w.left + w.right) / 2 - (r.left + r.right) / 2); })""", selector)
            assert off and max(off) < 1.5, (selector, off)
        assert errors == []
        page.close()


# --- the page's own keyboard (the table Pi's kiosk browser has no keyboard of its own) ---

def open_kiosk_page(browser, server, width=1280, height=800, touchscreen=False):
    page = browser.new_page(viewport={"width": width, "height": height}, has_touch=touchscreen)
    page.goto(f"http://127.0.0.1:{server.port}/?keyboard=1", wait_until="networkidle")
    page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
    return page


def touch(page, selector, pointer_id=1, release=True):
    """A finger on an element: pointerdown (and pointerup unless release is False), the way a touchscreen fires them."""
    box = page.locator(selector).bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.evaluate("""([x, y, id, release]) => {
        const target = document.elementFromPoint(x, y);
        const init = { pointerId: id, pointerType: 'touch', isPrimary: id === 1, bubbles: true, cancelable: true, clientX: x, clientY: y, buttons: 1 };
        target.dispatchEvent(new PointerEvent('pointerdown', init));
        if (release) target.dispatchEvent(new PointerEvent('pointerup', Object.assign({}, init, { buttons: 0 })));
    }""", [x, y, pointer_id, release])


def lift(page, selector, pointer_id=1):
    box = page.locator(selector).bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.evaluate("""([x, y, id]) => { document.elementFromPoint(x, y).dispatchEvent(new PointerEvent('pointerup',
        { pointerId: id, pointerType: 'touch', isPrimary: id === 1, bubbles: true, cancelable: true, clientX: x, clientY: y, buttons: 0 })); }""", [x, y, pointer_id])


def tap(page, *keys):
    """A finger on each key in turn, by its data-key: it lands and lifts, as a thumb's tap does."""
    for key in keys:
        touch(page, f"#keyboard .key[data-key='{key}']")


def test_two_thumbs_typing_fast_lose_nothing_and_the_keyboard_stays(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        text = "zoomusjmeeting"
        for i, ch in enumerate(text):                              # the next finger lands before the last one lifts
            touch(page, f"#keyboard .key[data-key='{ch}']", pointer_id=1 + (i % 2), release=False)
            if i:
                lift(page, f"#keyboard .key[data-key='{text[i - 1]}']", pointer_id=1 + ((i - 1) % 2))
        lift(page, f"#keyboard .key[data-key='{text[-1]}']", pointer_id=1 + ((len(text) - 1) % 2))
        assert page.input_value("#link-input") == text
        assert not page.evaluate("document.getElementById('keyboard').hidden")
        page.close()


def test_switching_layers_under_a_finger_replaces_no_key(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        page.evaluate("""() => { const k = document.querySelector("#keyboard .key[data-key='q']"); window.__q = k; return true; }""")
        touch(page, "#keyboard .key[data-key='q']", pointer_id=1, release=False)
        touch(page, "#keyboard .key[data-key='layer:symbols']", pointer_id=2)
        assert page.evaluate("document.getElementById('keyboard').dataset.layer") == "symbols"
        lift(page, "#keyboard .key[data-key='1']", pointer_id=1)     # the finger lifts where q was: the element is still the same node
        assert page.evaluate("window.__q.isConnected")                # the key under the finger was never replaced
        assert page.input_value("#link-input") == "q"
        page.close()


def test_a_lost_focus_while_a_key_is_down_does_not_close_the_keys(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        touch(page, "#keyboard .key[data-key='a']", pointer_id=1, release=False)
        page.evaluate("document.getElementById('link-input').dispatchEvent(new FocusEvent('focusout', { relatedTarget: null }))")
        assert not page.evaluate("document.getElementById('keyboard').hidden")
        lift(page, "#keyboard .key[data-key='a']", pointer_id=1)
        assert page.evaluate("document.activeElement.id") == "link-input"
        page.close()


def test_backspace_repeats_while_held_and_letters_do_not(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, kiosk=True)   # a count of repeats must not depend on how long the test's own calls take
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        page.fill("#link-input", "abcdefghijklmnopqrstuvwxyz")
        touch(page, "#keyboard .key[data-key='backspace']", pointer_id=1, release=False)
        page.clock.run_for(400 + 1000)
        lift(page, "#keyboard .key[data-key='backspace']", pointer_id=1)
        removed = 26 - len(page.input_value("#link-input"))
        assert removed == 13, removed                                # one at the press, then 12 a second
        page.clock.run_for(1000)
        assert 26 - len(page.input_value("#link-input")) == removed  # stopped when lifted
        touch(page, "#keyboard .key[data-key='x']", pointer_id=1, release=False)
        page.clock.run_for(1500)
        lift(page, "#keyboard .key[data-key='x']", pointer_id=1)
        assert page.input_value("#link-input").endswith("x") and not page.input_value("#link-input").endswith("xx")
        page.close()


def test_shift_once_double_locks_and_a_tap_releases(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, kiosk=True)   # a double tap is a matter of milliseconds: the clock is held still
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        touch(page, "#keyboard .key[data-key='shift']")
        touch(page, "#keyboard .key[data-key='a']")
        touch(page, "#keyboard .key[data-key='b']")
        assert page.input_value("#link-input") == "Ab"
        touch(page, "#keyboard .key[data-key='shift']")
        touch(page, "#keyboard .key[data-key='shift']")               # a double tap locks
        assert page.evaluate("document.getElementById('keyboard').dataset.shift") == "lock"
        touch(page, "#keyboard .key[data-key='c']")
        touch(page, "#keyboard .key[data-key='d']")
        touch(page, "#keyboard .key[data-key='shift']")
        touch(page, "#keyboard .key[data-key='e']")
        assert page.input_value("#link-input") == "AbCDe"
        page.close()


def test_a_slow_second_shift_tap_releases_and_a_quick_one_locks(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, kiosk=True)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)

        def shift():
            return page.evaluate("document.getElementById('keyboard').dataset.shift")

        touch(page, "#keyboard .key[data-key='shift']")
        touch(page, "#keyboard .key[data-key='shift']")               # the clock does not move between the taps: a double tap
        assert shift() == "lock"
        touch(page, "#keyboard .key[data-key='shift']")
        assert shift() == "off"
        touch(page, "#keyboard .key[data-key='shift']")
        page.clock.run_for(400)
        touch(page, "#keyboard .key[data-key='shift']")
        assert shift() == "off"                                       # the second tap came too late: it released the capital
        touch(page, "#keyboard .key[data-key='f']")
        assert page.input_value("#link-input") == "f"                 # and the next letter is a small one
        page.close()


def test_a_space_pressed_and_held_types_once(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, kiosk=True)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        touch(page, "#keyboard .key[data-key='space']", pointer_id=1, release=False)
        page.clock.run_for(2000)
        lift(page, "#keyboard .key[data-key='space']", pointer_id=1)
        assert page.input_value("#link-input") == " "
        page.close()


# What the tests above cannot show: they dispatch pointer events themselves, and those are not trusted: no focus moves for
# them, and the browser makes no click of its own. These press the keys the way a person does, with Playwright's mouse and
# with real fingers through CDP (what the browser makes of a tap: the click it follows it with, the capture it gives a finger).

A_LITTLE_TYPING = ("a", "shift", "b", "layer:symbols", "1", "layer:letters", "backspace")    # types a, B, 1 and takes the 1 away: "aB"


def open_kiosk_page_with_a_touchscreen(browser, server, stopped=False):
    """The kiosk page on a touchscreen, and the CDP session that puts real fingers on it (stopped: its timers wait for page.clock.run_for)."""
    page = page_on_a_stopped_clock(browser, server, kiosk=True, touchscreen=True) if stopped else open_kiosk_page(browser, server, touchscreen=True)
    return page, page.context.new_cdp_session(page)


def finger_down(page, cdp, selector, finger_id=1):
    """A real finger lands in the middle of an element and stays; the point comes back for finger_up."""
    box = page.locator(selector).bounding_box()
    point = {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2, "id": finger_id}
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
    return point


def finger_down_at(cdp, x, y, finger_id=1):
    """A real finger lands at a point of the page and stays; the point comes back for finger_up."""
    point = {"x": x, "y": y, "id": finger_id}
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
    return point


def finger_up(cdp, point):
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": [point]})


def finger_tap(page, cdp, selector):
    finger_up(cdp, finger_down(page, cdp, selector))


def test_a_real_mouse_press_acts_once_and_leaves_the_focus_in_the_field(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        for key in A_LITTLE_TYPING:
            page.locator(f"#keyboard .key[data-key='{key}']").click()     # a press, a release and the click the browser adds: one action
            assert page.evaluate("document.activeElement.id") == "link-input", key
        assert page.input_value("#link-input") == "aB"
        page.close()


def test_a_lone_real_finger_acts_once_and_leaves_the_focus_in_the_field(browser):
    with PageServer() as server:
        page, cdp = open_kiosk_page_with_a_touchscreen(browser, server)
        finger_tap(page, cdp, "#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        for key in A_LITTLE_TYPING:
            finger_tap(page, cdp, f"#keyboard .key[data-key='{key}']")
            page.wait_for_timeout(60)                              # room for the click a lone tap gets
            assert page.evaluate("document.activeElement.id") == "link-input", key
        assert page.input_value("#link-input") == "aB"
        page.close()


def test_two_real_fingers_typing_fast_lose_nothing_and_the_keyboard_stays(browser):
    """The bug as Ben met it: the browser makes a click only of a tap that stands alone, so keys that act on click drop
    letters when the fingers overlap. The old keys typed nothing here."""
    with PageServer() as server:
        page, cdp = open_kiosk_page_with_a_touchscreen(browser, server)
        finger_tap(page, cdp, "#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        text = "zoomusjmeetingabcdef"
        previous = None
        for i, ch in enumerate(text):                              # each finger lands before the one before it lifts
            point = finger_down(page, cdp, f"#keyboard .key[data-key='{ch}']", finger_id=1 + (i % 2))
            if previous:
                finger_up(cdp, previous)
            previous = point
        finger_up(cdp, previous)
        assert page.input_value("#link-input") == text
        assert not page.evaluate("document.getElementById('keyboard').hidden")
        page.close()


@pytest.mark.parametrize("key, effect", [
    ("hide", "document.getElementById('keyboard').hidden"),
    ("join", "document.getElementById('keyboard').hidden"),
    ("layer:symbols", "document.getElementById('keyboard').dataset.layer === 'symbols'"),
])
def test_a_tap_on_a_key_that_leaves_the_tray_leaves_no_click_under_the_finger(browser, key, effect):
    """The browser follows a tap with a click where the finger was. Hide and Join take the keys away as the finger lands, and
    a layer key goes to the shelf with its row: that click would reach the page beneath, or the key that is under the finger
    now. A tap on the field is counted first, so that the check is not an empty one."""
    with PageServer() as server:
        page, cdp = open_kiosk_page_with_a_touchscreen(browser, server)
        page.evaluate("window.__clicks = 0; document.addEventListener('click', () => { window.__clicks += 1; }, true)")
        finger_tap(page, cdp, "#link-input")                       # a real tap: its click is counted, and the keys come up
        page.wait_for_function("window.__clicks === 1 && !document.getElementById('keyboard').hidden", timeout=5000)
        page.fill("#link-input", "98765432100")                    # something for Join to send
        finger_tap(page, cdp, f"#keyboard .key[data-key='{key}']")
        page.wait_for_function(effect, timeout=5000)
        page.wait_for_timeout(500)                                 # long enough for a click to come, as it does to the field's tap
        assert page.evaluate("window.__clicks") == 1
        page.close()


def test_a_cancelled_touch_ends_the_press(browser):
    """The browser takes a touch back (a palm, a gesture): the pointer is cancelled, not lifted."""
    with PageServer() as server:
        page, cdp = open_kiosk_page_with_a_touchscreen(browser, server, stopped=True)
        finger_tap(page, cdp, "#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        page.fill("#link-input", "a" * 60)
        finger_down(page, cdp, "#keyboard .key[data-key='backspace']")
        page.clock.run_for(400 + 300)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchCancel", "touchPoints": []})
        removed = 60 - len(page.input_value("#link-input"))
        assert removed >= 2                                         # it was repeating
        assert page.evaluate("document.querySelectorAll('#keyboard .key.down').length") == 0
        page.clock.run_for(2000)
        assert 60 - len(page.input_value("#link-input")) == removed # and stopped there
        page.close()


def test_a_key_is_let_go_when_its_finger_lifts_over_another_key(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        page.evaluate("window.__q = document.querySelector(\"#keyboard .key[data-key='q']\")")
        touch(page, "#keyboard .key[data-key='q']", pointer_id=1, release=False)
        touch(page, "#keyboard .key[data-key='layer:symbols']", pointer_id=2)    # the layer changes under the first finger
        lift(page, "#keyboard .key[data-key='1']", pointer_id=1)                 # and it lifts over a key of the new one
        assert not page.evaluate("window.__q.classList.contains('down')")        # q is not left looking pressed
        page.close()


def test_a_held_backspace_stops_when_its_finger_lifts_off_the_keys(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, kiosk=True)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        page.fill("#link-input", "a" * 60)
        touch(page, "#keyboard .key[data-key='backspace']", pointer_id=1, release=False)
        page.clock.run_for(400 + 300)
        page.evaluate("document.body.dispatchEvent(new PointerEvent('pointerup', { pointerId: 1, bubbles: true }))")   # it slid off the keys
        removed = 60 - len(page.input_value("#link-input"))
        assert removed >= 2                                         # it was repeating
        page.clock.run_for(2000)
        assert 60 - len(page.input_value("#link-input")) == removed # and stopped at the lift, wherever that was
        page.close()


def test_a_held_backspace_stops_when_the_keys_go(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, kiosk=True)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        page.fill("#link-input", "a" * 60)
        touch(page, "#keyboard .key[data-key='backspace']", pointer_id=1, release=False)
        page.clock.run_for(400 + 300)
        page.evaluate("croomKeyboard.hide()")                       # a view change, Hide or Join, with the finger still down
        removed = 60 - len(page.input_value("#link-input"))
        assert removed >= 2
        page.clock.run_for(2000)
        assert 60 - len(page.input_value("#link-input")) == removed
        page.close()


def test_a_window_that_loses_its_focus_ends_every_press(browser):
    """No lift comes to a window nobody is looking at: a held key must not repeat on, nor stay looking pressed."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, kiosk=True)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        page.fill("#link-input", "a" * 60)
        touch(page, "#keyboard .key[data-key='backspace']", pointer_id=1, release=False)
        page.clock.run_for(400 + 300)
        page.evaluate("window.dispatchEvent(new Event('blur'))")
        removed = 60 - len(page.input_value("#link-input"))
        assert removed >= 2                                         # it was repeating
        assert page.evaluate("document.querySelectorAll('#keyboard .key.down').length") == 0
        page.clock.run_for(2000)
        assert 60 - len(page.input_value("#link-input")) == removed
        page.close()


def test_a_press_by_a_finger_whose_last_press_never_lifted_leaves_no_repeat_behind(browser):
    """A lift can be lost. The same pointer pressing again must end its old press first, or that press's repeat is left
    running with nothing in the keyboard that can stop it, not even a lift or Hide."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, kiosk=True)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        page.fill("#link-input", "a" * 100)
        touch(page, "#keyboard .key[data-key='backspace']", pointer_id=7, release=False)
        page.clock.run_for(400 + 300)                               # repeating
        touch(page, "#keyboard .key[data-key='backspace']", pointer_id=7, release=False)    # the same pointer lands again: no lift came between
        lift(page, "#keyboard .key[data-key='backspace']", pointer_id=7)
        page.evaluate("croomKeyboard.hide()")
        removed = 100 - len(page.input_value("#link-input"))
        page.clock.run_for(2000)
        assert 100 - len(page.input_value("#link-input")) == removed
        page.close()


@pytest.mark.parametrize("key", ["hide", "join"])
def test_hide_and_join_do_not_leave_their_own_key_looking_pressed(browser, key):
    """They take the keys away as the finger lands; that finger's press is over with them, whether or not its lift ever comes."""
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        page.fill("#link-input", "98765432100")                    # something for Join to send
        touch(page, f"#keyboard .key[data-key='{key}']", pointer_id=1, release=False)
        assert page.evaluate("document.getElementById('keyboard').hidden")
        assert page.evaluate("document.querySelectorAll('.key.down').length") == 0
        page.close()


def test_a_key_pressed_while_the_focus_is_on_the_keyboard_button_puts_it_back_in_the_field(browser):
    """The keys stay up while the focus is on their button (a Tab, on a laptop); a key then types into the field and has the focus go back to it."""
    with PageServer() as server:
        page = open_page(browser, server)                           # no ?keyboard=1: the keys come from the button
        page.click("#keyboard-toggle")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        page.evaluate("document.getElementById('keyboard-toggle').focus()")
        assert not page.evaluate("document.getElementById('keyboard').hidden")
        tap(page, "a")
        assert page.input_value("#link-input") == "a"
        assert page.evaluate("document.activeElement.id") == "link-input"
        page.close()


def test_a_press_hook_that_throws_costs_the_person_no_key(browser):
    """A press hook (what a test hangs on onPress; the click is not one, onDown makes it) runs after the key: whatever one of them
    does, the key it is for is typed and let go."""
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        page.evaluate("croomKeyboard.onPress(() => { throw new Error('a hook that is broken'); })")
        tap(page, "a", "b")
        assert page.input_value("#link-input") == "ab" and errors == []
        assert page.evaluate("document.querySelectorAll('#keyboard .key.down').length") == 0
        page.close()


def test_only_the_main_mouse_button_presses_a_key(browser):
    """The keys work with a mouse too (the keyboard button shows them on any device): the other buttons are not key presses,
    and a menu does not open over the keys."""
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        page.locator("#keyboard .key[data-key='c']").click(button="right")
        page.locator("#keyboard .key[data-key='d']").click(button="middle")
        assert page.input_value("#link-input") == ""
        assert page.evaluate("""() => { const menu = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
            document.querySelector("#keyboard .key[data-key='c']").dispatchEvent(menu); return menu.defaultPrevented; }""")
        page.locator("#keyboard .key[data-key='e']").click()
        assert page.input_value("#link-input") == "e"               # and the main button still types
        assert page.evaluate("document.activeElement.id") == "link-input"
        page.close()


def test_the_keyboard_tray_takes_no_scroll_zoom_or_text_selection(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        assert page.evaluate("""() => { const s = getComputedStyle(document.getElementById('keyboard')); return [s.touchAction, s.userSelect]; }""") == ["none", "none"]
        page.close()


TRAY_PLACES = ("between two keys", "between two rows", "the edge of the tray")


def a_place_in_the_tray_that_is_no_key(page, place):
    """A point in the tray that belongs to no key: the 6 px between the first two keys of the first row (the point is on the row), the 6 px
    between the first two rows, or the empty edge of the tray beside the keys (the last two are on the tray itself). What is under the point
    is checked, so that a test cannot pass by pressing a key."""
    def box(key):
        return page.locator(f"#keyboard .key[data-key='{key}']").bounding_box()
    if place == "between two keys":
        q, w = box("q"), box("w")
        x, y, under = (q["x"] + q["width"] + w["x"]) / 2, q["y"] + q["height"] / 2, "row"
    elif place == "between two rows":
        q, a = box("q"), box("a")
        x, y, under = q["x"] + q["width"] / 2, (q["y"] + q["height"] + a["y"]) / 2, "keyboard"
    else:
        tray = page.locator("#keyboard").bounding_box()
        x, y, under = 8, tray["y"] + tray["height"] / 2, "keyboard"
    assert page.evaluate("([x, y]) => document.elementFromPoint(x, y).className", [x, y]) == under, place
    return x, y


@pytest.mark.parametrize("place", TRAY_PLACES)
def test_a_mouse_click_where_the_tray_has_no_key_keeps_the_keys_up_and_the_focus_in_the_field(browser, place):
    """The gaps between the keys belong to no key. A mouse pressed in one would take the focus from the field, and the keys would close
    with it, were it not for the tray's own pointerdown, which keeps the focus as a key's does (a mouse has no touchstart to cancel)."""
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        x, y = a_place_in_the_tray_that_is_no_key(page, place)
        page.mouse.click(x, y)
        assert not page.evaluate("document.getElementById('keyboard').hidden")
        assert page.evaluate("document.activeElement.id") == "link-input"
        page.close()


def test_a_finger_at_the_edge_of_the_tray_keeps_the_keys_up_and_the_focus_in_the_field(browser):
    """The same with a real finger. Chromium takes a touch put in a 6 px gap to the nearest key (it presses it: the gaps cannot be
    tested with a finger), so the finger goes where no key is near, the empty edge of the tray. Two things keep it from taking the
    focus, and either is enough: the page cancels the touch (no mouse events follow it) and the tray's pointerdown does not let the
    focus go. The browser's own mouse events and click for a tap come after the lift, so the test waits for them."""
    with PageServer() as server:
        page, cdp = open_kiosk_page_with_a_touchscreen(browser, server)
        finger_tap(page, cdp, "#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        page.evaluate("window.__landed = []; document.addEventListener('pointerdown', (e) => window.__landed.push(e.target.className), true)")
        x, y = a_place_in_the_tray_that_is_no_key(page, "the edge of the tray")
        finger_up(cdp, finger_down_at(cdp, x, y))
        page.wait_for_timeout(150)
        assert page.evaluate("window.__landed") == ["keyboard"]         # on the tray, not snapped to a key
        assert not page.evaluate("document.getElementById('keyboard').hidden")
        assert page.evaluate("document.activeElement.id") == "link-input"
        page.close()


# --- how the keyboard looks: three layers, key caps, the pop above a pressed key (spec 2026-10-13, sections 3.2 to 3.4) ---

LAYER_ROWS = {      # the iPad's layers, row by row, by data-key
    "letters": ["q w e r t y u i o p", "a s d f g h j k l", "shift z x c v b n m backspace", "layer:symbols . - / space join hide"],
    "symbols": ["1 2 3 4 5 6 7 8 9 0", '- / : ; ( ) $ & @ "', "layer:more . , ? ! ' backspace", "layer:letters space join hide"],
    "more": ["[ ] { } # % ^ * + =", "_ \\ | ~ < > \N{EURO SIGN} \N{POUND SIGN} \N{YEN SIGN} \N{BULLET}", "layer:symbols . , ? ! ' backspace", "layer:letters space join hide"],
}


LAYER_CAPS = {      # what the keys say, row by row: the iPad's caps (the three that are a picture, Shift, delete and hide, by their names in brackets)
    "letters": ["q w e r t y u i o p", "a s d f g h j k l", "[Shift] z x c v b n m [Delete]", "123 . - / space Join [Hide keyboard]"],
    "symbols": [LAYER_ROWS["symbols"][0], LAYER_ROWS["symbols"][1], "#+= . , ? ! ' [Delete]", "ABC space Join [Hide keyboard]"],
    "more": [LAYER_ROWS["more"][0], LAYER_ROWS["more"][1], "123 . , ? ! ' [Delete]", "ABC space Join [Hide keyboard]"],
}


def keyboard_rows(page):
    """The keys of the layer in the tray, row by row, as their data-key values."""
    return page.evaluate("Array.from(document.querySelectorAll('#keyboard .row')).map(r => Array.from(r.querySelectorAll('.key')).map(k => k.dataset.key).join(' '))")


def keyboard_caps(page):
    """What the keys of the layer in the tray say, row by row: their text, and for the three that are a picture their accessible name in brackets."""
    return page.evaluate("""Array.from(document.querySelectorAll('#keyboard .row')).map(r => Array.from(r.querySelectorAll('.key')).map(
        k => k.querySelector('svg') ? '[' + k.getAttribute('aria-label') + ']' : k.querySelector('.cap').textContent).join(' '))""")


def keys_showing_outside_the_tray(page):
    return page.evaluate("Array.from(document.querySelectorAll('.key')).filter(k => !k.closest('#keyboard') && k.getClientRects().length > 0).length")


def key_heights(page):
    return page.evaluate("Array.from(document.querySelectorAll('#keyboard .key')).map(k => k.getBoundingClientRect().height)")


def pop(page):
    """What the preview above a pressed key shows, or None while it is not on the screen (its display, not its hidden attribute: a style rule can beat the attribute)."""
    return page.evaluate("(() => { const p = document.getElementById('key-preview'); return getComputedStyle(p).display === 'none' ? null : p.textContent; })()")


def test_the_three_layers_hold_the_ipad_keys_and_fit_the_kiosk(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        for layer, key in (("letters", None), ("symbols", "layer:symbols"), ("more", "layer:more")):
            if key:
                touch(page, f"#keyboard .key[data-key='{key}']")
            assert keyboard_rows(page) == LAYER_ROWS[layer], layer
            heights = key_heights(page)
            assert min(heights) >= 50 and max(heights) <= 56, (layer, heights)   # a thumb's width: 52 px at the kiosk's 1.5x scale
            keyboard = page.locator("#keyboard").bounding_box()
            assert keyboard["y"] + keyboard["height"] <= 800 and keyboard["height"] <= 290, layer   # on the screen, in the room the page leaves under its content
            assert page.evaluate("document.querySelector('#link-input').getBoundingClientRect().bottom <= document.querySelector('#keyboard').getBoundingClientRect().top"), layer
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"), layer
        page.close()


@pytest.mark.parametrize("width, height", [(390, 844), (1024, 700), (1280, 800), (1920, 1200)])
def test_the_rows_of_every_layer_end_together_and_the_block_stays_in_the_middle(browser, width, height):
    """Ten keys at their widest (118 px, 6 px apart) are the widest a row gets. Every row is as wide as the first, so the ends
    line up (the letters' middle row, nine keys, is half a key in from each end, as on the iPad), and on a screen wider than
    that the block sits in the middle of the tray. On a narrower one the rows run edge to edge."""
    with PageServer() as server:
        page = open_kiosk_page(browser, server, width=width, height=height)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        block = min(width - 12, 10 * 118 + 9 * 6)                       # the tray pads its sides by 6 px
        left = (width - block) / 2
        for layer, key in (("letters", None), ("symbols", "layer:symbols"), ("more", "layer:more")):
            if key:
                touch(page, f"#keyboard .key[data-key='{key}']")
            ends = page.evaluate("Array.from(document.querySelectorAll('#keyboard .row')).map(r => [r.firstElementChild.getBoundingClientRect().left, r.lastElementChild.getBoundingClientRect().right])")
            for i, (start, end) in enumerate(ends):
                inset = 62 if layer == "letters" and i == 1 and block == 1234 else 0   # nine letters of 118 px with their gaps: half a key in from each end
                assert abs(start - (left + inset)) <= 1 and abs(end - (left + block - inset)) <= 1, (layer, i, ends)
            if layer != "letters":                                       # no key is capped here: a row shares its width in the proportions it is given, a wide key being 1.5 keys
                third = page.evaluate("Array.from(document.querySelectorAll('#keyboard .row')[2].querySelectorAll('.key')).map(k => k.getBoundingClientRect().width)")
                assert abs(third[0] / third[1] - 1.5) <= 0.03, (layer, third)
            assert page.evaluate("document.querySelector('#link-input').getBoundingClientRect().bottom <= document.querySelector('#keyboard').getBoundingClientRect().top"), layer
        if width >= 1024:                                                # the fixed screen: once the keys are gone nothing may need to scroll
            tap(page, "hide")
            assert fits(page)
        page.close()


def test_the_keys_say_what_the_ipad_says_on_each_layer(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        for layer, key in (("letters", None), ("symbols", "layer:symbols"), ("more", "layer:more")):
            if key:
                touch(page, f"#keyboard .key[data-key='{key}']")
            assert keyboard_caps(page) == LAYER_CAPS[layer], layer
        touch(page, "#keyboard .key[data-key='layer:symbols']")          # the layer keys of each layer lead where their caps say
        assert page.evaluate("document.getElementById('keyboard').dataset.layer") == "symbols"
        assert page.locator("#keyboard .key", has_text="ABC").get_attribute("data-key") == "layer:letters"
        page.close()


def test_the_layers_that_are_not_showing_do_not_show(browser):
    """They wait on the shelf, out of the tray: not one of their keys is on the page, and the page is no taller for them."""
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        for key in (None, "layer:symbols", "layer:more", "layer:letters"):
            if key:
                touch(page, f"#keyboard .key[data-key='{key}']")
            assert keys_showing_outside_the_tray(page) == 0, key
            assert page.evaluate("document.documentElement.scrollHeight <= document.documentElement.clientHeight"), key
        page.close()


def test_the_keyboard_keys_are_44_px_tall_on_a_phone(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server, width=390, height=844)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        heights = key_heights(page)
        assert min(heights) >= 43.5 and max(heights) <= 44.5, heights
        page.close()


def test_a_pressed_letter_shows_its_preview_and_a_wide_key_does_not(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        touch(page, "#keyboard .key[data-key='g']", pointer_id=1, release=False)
        assert page.locator("#key-preview").is_visible() and page.locator("#key-preview").inner_text() == "g"
        lift(page, "#keyboard .key[data-key='g']", pointer_id=1)
        page.wait_for_function("document.getElementById('key-preview').hidden", timeout=2000)
        touch(page, "#keyboard .key[data-key='shift']", pointer_id=1, release=False)
        assert page.evaluate("document.getElementById('key-preview').hidden")
        lift(page, "#keyboard .key[data-key='shift']", pointer_id=1)
        page.close()


@pytest.mark.parametrize("key", ["shift", "backspace", "layer:symbols", "space", "join", "hide"])
def test_a_wide_key_space_and_join_show_no_preview(browser, key):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, kiosk=True)   # a pop that came would still be waiting for its 60 ms to go
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        page.fill("#link-input", "98765432100")                        # something for Join to send
        page.evaluate("""() => { window.__popMoves = 0; new MutationObserver((records) => { window.__popMoves += records.length; })
            .observe(document.getElementById('key-preview'), { childList: true, attributes: true, attributeFilter: ['style'] }); }""")
        touch(page, f"#keyboard .key[data-key='{key}']")
        assert pop(page) is None
        assert page.evaluate("window.__popMoves") == 0                 # and none was made ready: Hide and Join hide a pop as the keys go, so its end state alone would show nothing
        page.close()


def test_the_preview_sits_centred_above_its_key(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        for key in ("g", "q", "p", "m"):                                # the middle of the tray, the edges of its first row, a key of its third
            touch(page, f"#keyboard .key[data-key='{key}']", release=False)
            above, cap = page.locator("#key-preview").bounding_box(), page.locator(f"#keyboard .key[data-key='{key}']").bounding_box()
            assert abs((above["x"] + above["width"] / 2) - (cap["x"] + cap["width"] / 2)) <= 1, key   # centred on the key
            assert above["width"] >= 80 and above["y"] >= 0, key                                      # a bubble wider than a key, on the screen
            assert abs(cap["y"] - (above["y"] + above["height"]) - 2) <= 0.5, key                     # 2 px above the key: joined to it, as the iPad's is
            lift(page, f"#keyboard .key[data-key='{key}']")
            page.wait_for_function("document.getElementById('key-preview').hidden", timeout=2000)
        page.close()


def test_the_preview_stays_on_the_screen_at_the_edges_of_a_phone(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server, width=390, height=844)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        for key in ("q", "p"):                                          # the keys at the edges are narrower than their pop
            touch(page, f"#keyboard .key[data-key='{key}']", release=False)
            above = page.locator("#key-preview").bounding_box()
            assert above["x"] >= 0 and above["x"] + above["width"] <= 390, key
            lift(page, f"#keyboard .key[data-key='{key}']")
            page.wait_for_function("document.getElementById('key-preview').hidden", timeout=2000)
        page.close()


def test_the_preview_shows_what_is_typed_and_goes_60_ms_after_the_lift(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, kiosk=True)   # the 60 ms are told by the clock the test holds
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        assert pop(page) is None                                       # there from the start, and hidden
        touch(page, "#keyboard .key[data-key='g']", release=False)
        assert pop(page) == "g"
        lift(page, "#keyboard .key[data-key='g']")
        page.clock.run_for(50)
        assert pop(page) == "g"                                        # it stays a moment after the finger, as the iPad's does
        page.clock.run_for(20)
        assert pop(page) is None
        tap(page, "shift")
        touch(page, "#keyboard .key[data-key='g']", release=False)
        assert pop(page) == "G"                                        # a capital under Shift, as the key showed it before the press spent the Shift
        lift(page, "#keyboard .key[data-key='g']")
        tap(page, "layer:symbols")
        touch(page, "#keyboard .key[data-key='@']", release=False)
        assert pop(page) == "@"
        lift(page, "#keyboard .key[data-key='@']")
        assert page.evaluate("document.querySelectorAll('#key-preview').length") == 1   # one pop, moved from key to key
        assert page.evaluate("document.getElementById('key-preview').getAttribute('aria-hidden')") == "true"   # that a screen reader skips: the key is read
        page.close()


def test_a_key_pressed_within_the_60_ms_keeps_the_pop_for_itself(browser):
    """Fast typing: the pop of the last letter is still waiting to go when the next one lands, and must not take it away."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, kiosk=True)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        touch(page, "#keyboard .key[data-key='g']")
        page.clock.run_for(30)
        touch(page, "#keyboard .key[data-key='h']", release=False)
        assert pop(page) == "h"
        page.clock.run_for(50)                                          # past the 60 ms the first letter's pop was to stay
        assert pop(page) == "h"                                         # and h is still down
        lift(page, "#keyboard .key[data-key='h']")
        page.clock.run_for(100)
        assert pop(page) is None
        page.close()


def test_a_finger_lifting_leaves_the_preview_of_the_key_another_still_holds(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, kiosk=True)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        touch(page, "#keyboard .key[data-key='a']", pointer_id=1, release=False)
        touch(page, "#keyboard .key[data-key='b']", pointer_id=2, release=False)
        assert pop(page) == "b"                                        # the pop follows the latest finger
        lift(page, "#keyboard .key[data-key='a']", pointer_id=1)
        page.clock.run_for(200)
        assert pop(page) == "b"                                        # the first finger lifting does not take it from the second
        lift(page, "#keyboard .key[data-key='b']", pointer_id=2)
        page.clock.run_for(100)
        assert pop(page) is None
        page.close()


def test_the_pop_of_the_second_finger_is_over_its_own_key_and_does_not_take_a_touch(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        touch(page, "#keyboard .key[data-key='a']", pointer_id=1, release=False)
        touch(page, "#keyboard .key[data-key='l']", pointer_id=2, release=False)
        above, cap = page.locator("#key-preview").bounding_box(), page.locator("#keyboard .key[data-key='l']").bounding_box()
        assert pop(page) == "l" and abs((above["x"] + above["width"] / 2) - (cap["x"] + cap["width"] / 2)) <= 1   # over the key the latest finger is on
        style = page.evaluate("(() => { const p = getComputedStyle(document.getElementById('key-preview')), t = getComputedStyle(document.getElementById('keyboard')); return [p.pointerEvents, Number(p.zIndex) > Number(t.zIndex)]; })()")
        assert style == ["none", True]                                  # over the keys, and never in a finger's way
        lift(page, "#keyboard .key[data-key='l']", pointer_id=2)
        lift(page, "#keyboard .key[data-key='a']", pointer_id=1)
        page.close()


def test_a_second_finger_lands_through_the_pop_of_the_first(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        touch(page, "#keyboard .key[data-key='x']", pointer_id=1, release=False)       # its pop covers keys of the row above
        above = page.locator("#key-preview").bounding_box()
        under = page.evaluate("""(pop) => { for (const k of document.querySelectorAll('#keyboard .row:nth-child(2) .key')) { const r = k.getBoundingClientRect(), cx = r.x + r.width / 2, cy = r.y + r.height / 2;
            if (cx > pop.x && cx < pop.x + pop.width && cy > pop.y && cy < pop.y + pop.height) return k.dataset.key; } return null; }""", above)
        assert under, "no key of the second row under the pop"
        touch(page, f"#keyboard .key[data-key='{under}']", pointer_id=2)
        lift(page, "#keyboard .key[data-key='x']", pointer_id=1)
        assert page.input_value("#link-input") == "x" + under
        page.close()


def test_the_preview_goes_at_once_with_the_keys(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, kiosk=True)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        touch(page, "#keyboard .key[data-key='g']", release=False)
        assert pop(page) == "g"
        page.evaluate("croomKeyboard.hide()")                           # Hide, Join or a view change, the finger still down
        assert pop(page) is None                                        # not left over the page: nothing waited for, the clock being held
        page.close()


def test_a_layer_switched_under_a_held_letter_takes_its_pop_with_it(browser):
    """The pop is the held key's, and the key goes to the shelf with its layer: it would be left over the new layer's keys until
    the finger lifts. It goes with the layer, and the lift that comes later finds nothing to do."""
    with PageServer() as server:
        page, cdp = open_kiosk_page_with_a_touchscreen(browser, server, stopped=True)
        finger_tap(page, cdp, "#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        held = finger_down(page, cdp, "#keyboard .key[data-key='j']", finger_id=1)
        assert pop(page) == "j"
        finger_up(cdp, finger_down(page, cdp, "#keyboard .key[data-key='layer:symbols']", finger_id=2))
        assert page.evaluate("document.getElementById('keyboard').dataset.layer") == "symbols"
        assert page.evaluate("document.querySelectorAll('.key.down').length") == 1      # the first finger is still down, on the shelf's key
        assert pop(page) is None
        finger_up(cdp, held)
        page.clock.run_for(200)
        assert pop(page) is None and page.input_value("#link-input") == "j"
        page.close()


def test_a_press_that_returns_the_focus_to_the_field_keeps_its_pop(browser):
    """With the focus on the keyboard's own button (a Tab, on a laptop) a key's press puts it back in the field. The layer is
    the same one, so the pop of the key stays: it goes when the layer changes, not when the focus comes back."""
    with PageServer() as server:
        page = open_page(browser, server)                           # no ?keyboard=1: the keys come from the button
        page.click("#keyboard-toggle")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        page.evaluate("document.getElementById('keyboard-toggle').focus()")
        touch(page, "#keyboard .key[data-key='g']", release=False)
        assert page.evaluate("document.activeElement.id") == "link-input"   # the press put the focus back in the field ...
        assert pop(page) == "g"                                             # ... and the pop is still over its key
        lift(page, "#keyboard .key[data-key='g']")
        page.close()


def test_the_keys_wear_the_rooms_colours_and_show_pressed_and_shift_on(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, kiosk=True)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)

        def style(selector, prop, pseudo=None):
            return page.evaluate("([s, p, ps]) => getComputedStyle(document.querySelector(s), ps)[p]", [selector, prop, pseudo])

        assert style("#keyboard", "backgroundColor") == "rgb(11, 18, 32)"                    # the tray: #0B1220
        assert style("#keyboard .key[data-key='g']", "backgroundColor") == "rgb(31, 41, 55)"  # a key: surface-800
        assert style("#keyboard .key[data-key='join']", "backgroundColor") == "rgb(27, 82, 229)"   # Join: the room's blue
        touch(page, "#keyboard .key[data-key='g']", release=False)
        assert style("#keyboard .key[data-key='g']", "backgroundColor") == "rgb(22, 36, 79)"  # pressed: navy-800
        assert style("#key-preview", "backgroundColor") == "rgb(22, 36, 79)" and style("#key-preview", "color") == "rgb(255, 255, 255)"
        assert style("#key-preview", "fontSize") == "25.6px"                                  # the glyph at 1.6 rem
        lift(page, "#keyboard .key[data-key='g']")
        assert style("#keyboard .key[data-key='g']", "backgroundColor") == "rgb(31, 41, 55)"  # and back
        tap(page, "shift")
        assert style("#keyboard .key[data-key='shift']", "backgroundColor") == "rgb(255, 255, 255)"   # Shift on shows white
        assert page.locator("#keyboard .key[data-key='q']").inner_text() == "Q"              # and the caps are capitals
        assert "\N{BULLET}" not in style("#keyboard .key[data-key='shift'] .cap", "content", "::after")
        tap(page, "shift")                                                                    # the clock is held: a double tap, so a lock
        assert "\N{BULLET}" in style("#keyboard .key[data-key='shift'] .cap", "content", "::after")   # the lock mark
        page.close()


def test_the_keyboard_key_caps_have_the_specs_radius_gaps_edge_and_colours(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)

        def style(selector, prop):
            return page.evaluate("([s, p]) => getComputedStyle(document.querySelector(s))[p]", [selector, prop])

        assert style("#keyboard .key[data-key='g']", "borderTopLeftRadius") == "8px"
        assert style("#keyboard .row", "columnGap") == "6px" and style("#keyboard", "rowGap") == "6px"
        assert "rgba(255, 255, 255, 0.08)" in style("#keyboard .key[data-key='g']", "boxShadow")        # the lighter top edge
        assert style("#keyboard .key[data-key='g']", "fontSize") == "18.4px"                           # Lexend 1.15 rem
        assert style("#keyboard .key[data-key='space']", "backgroundColor") == "rgb(31, 41, 55)"
        assert style("#keyboard .key[data-key='shift']", "backgroundColor") == "rgb(23, 32, 51)"       # the function keys a shade down
        page.close()


def test_the_three_picture_keys_of_the_keyboard_are_drawn_in_the_keys_own_colour_and_named(browser):
    """Shift, delete and hide are inline SVG, 24 px with a 2 px round stroke in the key's own colour (white on a dark key, dark
    on the white Shift key), so that no font has to supply them; the buttons carry their names."""
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)

        def picture(key):
            return page.evaluate("""(key) => { const k = document.querySelector(`#keyboard .key[data-key='${key}']`), s = k.querySelector('.cap svg');
                if (!s) return null;
                const r = s.getBoundingClientRect(), b = k.getBoundingClientRect(), c = getComputedStyle(s);
                return { size: [r.width, r.height], dx: r.x + r.width / 2 - (b.x + b.width / 2), dy: r.y + r.height / 2 - (b.y + b.height / 2),
                         stroke: c.stroke, color: getComputedStyle(k).color, width: c.strokeWidth, cap: c.strokeLinecap,
                         hidden: s.getAttribute('aria-hidden'), name: k.getAttribute('aria-label'), text: k.textContent }; }""", key)

        for key, name in (("shift", "Shift"), ("backspace", "Delete"), ("hide", "Hide keyboard")):
            drawn = picture(key)
            assert drawn, key
            assert drawn["size"] == [24, 24] and abs(drawn["dx"]) <= 1 and abs(drawn["dy"]) <= 1, (key, drawn)   # 24 px, in the middle of its key
            assert drawn["width"] == "2px" and drawn["cap"] == "round", (key, drawn)
            assert drawn["hidden"] == "true" and drawn["name"] == name and drawn["text"] == "", (key, drawn)        # drawn, not written; named for a screen reader
            assert drawn["stroke"] == drawn["color"] == "rgb(255, 255, 255)", (key, drawn)                          # currentColor
        tap(page, "shift")                                                                                          # Shift on: the key turns white
        assert picture("shift")["stroke"] == "rgb(0, 22, 54)"                                                       # and its picture follows, dark (navy-900)
        page.close()


def touchstart_listeners_on_the_document(cdp):
    """The touchstart listeners the keyboard has put on the document, as the browser lists them. A listener that may cancel
    (not passive) makes the browser wait for the script before it scrolls on any touch, anywhere on the page."""
    document = cdp.send("Runtime.evaluate", {"expression": "document"})["result"]["objectId"]
    return [l for l in cdp.send("DOMDebugger.getEventListeners", {"objectId": document})["listeners"] if l["type"] == "touchstart"]


def test_the_keyboards_touch_listener_is_on_the_page_only_while_the_keyboard_is_up(browser):
    """Cancelling a tap's touch is what keeps a tap on Hide or Join from reaching the page beneath. It is needed only while
    the keys are up, and a tick after they go (the touch that took them away is still to come)."""
    with PageServer() as server:
        page, cdp = open_kiosk_page_with_a_touchscreen(browser, server)
        assert touchstart_listeners_on_the_document(cdp) == []          # the page loads without one: the other screens scroll at once
        finger_tap(page, cdp, "#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        assert [l["passive"] for l in touchstart_listeners_on_the_document(cdp)] == [False]   # the keys are up: one that may cancel
        finger_tap(page, cdp, "#keyboard .key[data-key='hide']")
        page.wait_for_function("document.getElementById('keyboard').hidden", timeout=5000)
        for _ in range(50):                                             # a tick is all it takes; the wait is for a slow machine
            if not touchstart_listeners_on_the_document(cdp):
                break
            page.wait_for_timeout(20)
        assert touchstart_listeners_on_the_document(cdp) == []
        page.evaluate("window.__clicks = 0; document.addEventListener('click', () => { window.__clicks += 1; }, true)")
        finger_tap(page, cdp, "#open-options")
        page.wait_for_function("document.body.dataset.view === 'options'", timeout=5000)
        assert page.evaluate("window.__clicks") == 1                    # a real touch on a button of the page clicks it, the keys being down
        page.close()


def test_the_keyboard_hidden_and_shown_in_the_same_breath_keeps_its_touch_listener(browser):
    with PageServer() as server:
        page, cdp = open_kiosk_page_with_a_touchscreen(browser, server)
        finger_tap(page, cdp, "#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        page.evaluate("croomKeyboard.hide(); croomKeyboard.open()")     # the tick that takes the listener away comes after the keys are back
        page.wait_for_timeout(100)
        assert not page.evaluate("document.getElementById('keyboard').hidden")
        assert len(touchstart_listeners_on_the_document(cdp)) == 1
        page.close()


# --- the key click (spec 2026-10-13, section 3.3): a soft tick for each key a finger presses, on or off in Options > Keyboard ---

# A stand-in for the browser's audio that counts what the click asks of it: contexts made, resumes asked for, and each tick sounded (the
# pitch it was given, and when its oscillator was started and stopped). Its state and what fails are the test's to set, through
# window.__audio, in a script that follows this one.
RECORDED_AUDIO = """window.__audio = { made: 0, resumed: 0, state: 'running', resumeFails: false, oscillatorFails: false, ticks: [] };
    window.AudioContext = class { constructor() { window.__audio.made += 1; window.__audio.context = this; this.currentTime = 0; this.destination = {}; this.state = window.__audio.state; }
        createOscillator() { if (window.__audio.oscillatorFails) throw new Error('no oscillator'); const osc = { frequency: { value: 0 }, connect() {},
            start(at) { osc.tick = { hz: osc.frequency.value, at: at }; window.__audio.ticks.push(osc.tick); }, stop(at) { osc.tick.until = at; } }; return osc; }
        createGain() { return { connect() {}, gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} } }; }
        resume() { window.__audio.resumed += 1; return window.__audio.resumeFails ? Promise.reject(new Error('the browser will not start the audio')) : Promise.resolve(); } };
"""


def open_kiosk_page_with(browser, server, script, stopped=False, touchscreen=False):
    """The kiosk page with its keys up, loaded with a script that runs before the page's own (a stand-in for the browser's audio or
    vibration). stopped: its timers wait for page.clock.run_for, as page_on_a_stopped_clock's do. touchscreen: a touch device."""
    page = browser.new_page(viewport={"width": 1280, "height": 800}, has_touch=touchscreen)
    if stopped:
        page.clock.install()
    page.add_init_script(script)
    page.goto(f"http://127.0.0.1:{server.port}/?keyboard=1", wait_until="networkidle")
    page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
    if stopped:
        page.clock.pause_at(datetime.now() + timedelta(seconds=2))
    page.click("#link-input")
    page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
    return page


def ticks(page):
    """The ticks the recording audio has been asked to sound, one for each: {hz, at, until}."""
    return page.evaluate("window.__audio.ticks")


def test_the_key_click_plays_once_per_key_and_the_options_card_turns_it_off(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.add_init_script("""window.__ticks = 0; window.AudioContext = class { constructor() { this.currentTime = 0; this.destination = {}; }
            createOscillator() { return { connect() {}, start() { window.__ticks += 1; }, stop() {}, frequency: { value: 0 } }; }
            createGain() { return { connect() {}, gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} } }; } resume() { return Promise.resolve(); } };""")
        page.reload(wait_until="networkidle")
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        tap(page, "a", "b", "c")
        assert page.evaluate("window.__ticks") == 3
        page.click("#keyboard-toggle")                              # hide, then Options › Keyboard
        page.click("#open-options")
        page.locator("#key-click-off").click()
        page.wait_for_function("window.croomPage.model.status.settings.key_click === false", timeout=5000)
        page.click("#back-from-options")
        page.click("#link-input")
        tap(page, "d")
        assert page.evaluate("window.__ticks") == 3
        page.close()


def test_the_click_fails_quietly_without_audio(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.add_init_script("window.AudioContext = class { constructor() { throw new Error('no audio'); } }; window.webkitAudioContext = undefined;")
        page.reload(wait_until="networkidle")
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        tap(page, "a", "b")
        assert page.input_value("#link-input") == "ab" and errors == []
        page.close()


@pytest.mark.parametrize("breakage", [
    "window.__audio.state = 'suspended'; window.__audio.resumeFails = true;",                                     # the browser will not start the audio
    "window.__audio.oscillatorFails = true;",                                                                     # a context that cannot play
    "window.__audio.state = 'closed'; window.__audio.resumeFails = true;",                                        # one the system closed
    "Object.defineProperty(navigator, 'vibrate', { configurable: true, value: () => { throw new Error('no motor'); } });",   # a motor that will not run
], ids=["audio that will not start", "no oscillator", "a closed context", "no motor"])
def test_the_click_fails_quietly_whichever_part_of_it_fails(browser, breakage):
    """No audio at all is one way for the click to fail (the test above). A context that will not start or will not play, one the
    system has closed, and a device whose motor throws are others: the keys type, and nothing is thrown, not even by the promise
    that resume() hands back."""
    with PageServer() as server:
        page = open_kiosk_page_with(browser, server, RECORDED_AUDIO + breakage)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        tap(page, "a", "b", "c")
        page.wait_for_timeout(100)                                  # a rejected promise reports itself a moment after the press
        assert page.input_value("#link-input") == "abc" and errors == []
        assert page.evaluate("document.querySelectorAll('#keyboard .key.down').length") == 0
        page.close()


def test_the_click_sounds_once_for_each_finger_that_lands_and_for_nothing_else(browser):
    """A tick is for a finger landing on a key, any key: not the repeat of a held Backspace, not a mouse's other buttons, not a
    press made from code. And nothing is made of the audio before the first key."""
    with PageServer() as server:
        page = open_kiosk_page_with(browser, server, RECORDED_AUDIO, stopped=True)
        assert page.evaluate("window.__audio.made") == 0               # lazily: the first key makes it
        page.fill("#link-input", "x" * 20)                             # something for Backspace to take
        touch(page, "#keyboard .key[data-key='backspace']", release=False)
        page.clock.run_for(400 + 83 * 5)                               # it repeats ...
        lift(page, "#keyboard .key[data-key='backspace']")
        assert len(page.input_value("#link-input")) < 18 and len(ticks(page)) == 1   # ... and sounds once
        page.locator("#keyboard .key[data-key='c']").click(button="right")
        page.locator("#keyboard .key[data-key='d']").click(button="middle")
        page.evaluate("croomKeyboard.press('e')")                      # a press from code is no finger
        assert len(ticks(page)) == 1
        tap(page, "shift", "a", "layer:symbols", "layer:more", "layer:letters", "space")
        assert len(ticks(page)) == 7                                   # every key a finger lands on, the wide ones and the layer keys too
        assert page.evaluate("window.__audio.made") == 1               # in the one context
        assert all(1000 <= tick["hz"] <= 2500 for tick in ticks(page))   # a tick has a pitch, in the range the table's speaker serves
        assert all(0.002 <= tick["until"] - tick["at"] <= 0.01 for tick in ticks(page))   # and its oscillator is stopped within 10 ms: none is left running
        page.close()


def test_a_real_finger_clicks_once_per_tap_and_two_overlapping_fingers_click_twice(browser):
    """Not the synthetic events of the tests above: a real touchscreen's finger makes a pointerdown, and the browser follows a
    lone tap with a click of its own that the keys cancel. Neither may make a second tick."""
    with PageServer() as server:
        page = open_kiosk_page_with(browser, server, RECORDED_AUDIO, touchscreen=True)
        cdp = page.context.new_cdp_session(page)
        finger_tap(page, cdp, "#keyboard .key[data-key='a']")
        page.wait_for_timeout(100)                                  # room for the click a lone tap gets
        assert len(ticks(page)) == 1 and page.input_value("#link-input") == "a"
        first = finger_down(page, cdp, "#keyboard .key[data-key='s']", finger_id=1)
        second = finger_down(page, cdp, "#keyboard .key[data-key='d']", finger_id=2)   # lands before the first lifts
        finger_up(cdp, first)
        finger_up(cdp, second)
        assert page.input_value("#link-input") == "asd" and len(ticks(page)) == 3
        page.close()


def test_the_click_uses_the_prefixed_audio_where_that_is_all_there_is(browser):
    """Safari up to 14 (an older iPad opening the page, with the keyboard button) has webkitAudioContext and no AudioContext."""
    with PageServer() as server:
        page = open_kiosk_page_with(browser, server, RECORDED_AUDIO + "window.webkitAudioContext = window.AudioContext; window.AudioContext = undefined;")
        tap(page, "a", "b")
        assert len(ticks(page)) == 2
        page.close()


def test_the_click_makes_its_audio_on_the_first_key_and_piles_no_ticks_up_while_the_browser_holds_it_back(browser):
    """A browser may keep audio from starting until a tap has led to it. Until it lets the context run, every tick scheduled on it
    waits, and all of them would sound at once the moment it did: so the context is asked to start on each key, and only the key
    that made it is queued (it may start a moment later, and that tick is then heard). Once it runs, every key sounds."""
    with PageServer() as server:
        page = open_kiosk_page_with(browser, server, RECORDED_AUDIO + "window.__audio.state = 'suspended';")
        assert page.evaluate("window.__audio.made") == 0
        tap(page, "a", "b", "c")
        assert page.evaluate("window.__audio.made") == 1                # one context for the page, made by the first key
        assert page.evaluate("window.__audio.resumed") >= 1             # and asked to start
        assert len(ticks(page)) == 1                                    # the first key's tick is queued; the others are not piled up behind it
        page.evaluate("window.__audio.context.state = 'running'")       # a tap has led to it: the browser lets it run
        tap(page, "d", "e")
        assert len(ticks(page)) == 3 and page.input_value("#link-input") == "abcde"
        page.close()


def test_a_finger_lifting_asks_the_held_back_audio_to_start_so_a_keyboard_that_opened_by_itself_is_heard_from_its_first_key(browser):
    """Chromium counts a touch's lift, not its pointerdown, as the gesture that lets audio start. A keyboard that came up by itself
    (a reload onto the passcode card brings it up for the code) has its context made by a press that no gesture has led to, and the
    ask that press makes goes unanswered: the ask that comes with the lift is the one the browser answers. So the lift asks again,
    and the next key is heard."""
    with PageServer() as server:
        page = open_kiosk_page_with(browser, server, RECORDED_AUDIO + "window.__audio.state = 'suspended';")
        touch(page, "#keyboard .key[data-key='a']", release=False)      # the finger lands: the context is made, and asked to start
        asked = page.evaluate("window.__audio.resumed")
        assert asked >= 1
        lift(page, "#keyboard .key[data-key='a']")                      # and lifts: the browser's gesture, so it is asked again
        assert page.evaluate("window.__audio.resumed") > asked
        page.close()


def test_a_finger_lifting_leaves_the_clicks_audio_alone_while_it_is_running(browser):
    """The ask at the lift is for a context the browser holds back: one that is running is asked by neither the press nor the lift."""
    with PageServer() as server:
        page = open_kiosk_page_with(browser, server, RECORDED_AUDIO)
        touch(page, "#keyboard .key[data-key='a']", release=False)
        lift(page, "#keyboard .key[data-key='a']")
        assert page.evaluate("window.__audio.made") == 1                # there was a context to ask ...
        assert page.evaluate("window.__audio.resumed") == 0             # ... and nobody did
        page.close()


def test_the_click_is_a_short_soft_burst_in_a_real_audio_graph(browser):
    """The tick as the browser's own audio graph makes it, rendered offline (no speaker needed): not silent, not loud, starting with
    the key and over in 2 to 4 milliseconds, a sine that fades out and is not cut off (a cut, a square wave or a sawtooth is a harsh
    click, and twice the level is not soft). The tests with a recording stand-in cannot tell that a real graph accepts what the click
    gives it (a ramp to zero, say, is refused by a real one)."""
    with PageServer() as server:
        page = open_kiosk_page_with(browser, server, "window.AudioContext = class extends OfflineAudioContext { constructor() { super(1, 4800, 48000); window.__context = this; } };")
        tap(page, "a")
        samples = page.evaluate("window.__context.startRendering().then((buffer) => Array.from(buffer.getChannelData(0)))")   # 100 ms at 48 kHz
        peak = max(abs(s) for s in samples)
        audible = [i for i, s in enumerate(samples) if abs(s) > 0.001]  # louder than -60 dB
        assert 0.05 <= peak <= 0.14, peak                               # a tick you can hear, and a quiet one (0.105 now)
        assert audible and audible[0] < 48, audible[:1]                 # it begins at the key (inside the first millisecond)
        assert 2 <= audible[-1] / 48 <= 4, audible[-1] / 48             # and is over in 2 to 4 milliseconds
        assert max(abs(s) for s in samples[144:192]) <= 0.05 * peak     # faded out between 3 and 4 ms: no cut
        assert max(abs(b - a) for a, b in zip(samples, samples[1:])) <= 0.3 * peak   # no hard corner (a 1.2 kHz sine makes 0.22)
        page.close()


def test_the_click_buzzes_where_the_device_can_and_off_silences_that_too(browser):
    """Spec 3.3: the Vibration API is also called when it is there (a tablet may have a motor; the table's panel has none)."""
    script = RECORDED_AUDIO + "window.__buzzes = []; Object.defineProperty(navigator, 'vibrate', { configurable: true, value: (ms) => { window.__buzzes.push(ms); return true; } });"
    with PageServer() as server:
        page = open_kiosk_page_with(browser, server, script, stopped=True)   # a poll between setClick and the tap would hand the room's setting back
        tap(page, "a", "b")
        buzzes = page.evaluate("window.__buzzes")
        assert len(buzzes) == 2 and all(1 <= ms <= 30 for ms in buzzes), buzzes   # a short buzz for each key
        page.evaluate("croomKeyboard.setClick(false)")
        tap(page, "c")
        assert len(page.evaluate("window.__buzzes")) == 2 and len(ticks(page)) == 2   # off is off: no tick and no buzz
        page.close()


def test_the_click_follows_the_rooms_setting_as_the_status_changes(browser):
    """The setting is the room's: another page, or the service's file, changes it, and this page's keys follow when the status says
    so, with no tap on this page's own buttons. (setClick is what the page hands the keyboard.)"""
    with PageServer() as server:
        page = open_kiosk_page_with(browser, server, RECORDED_AUDIO, stopped=True)   # no poll but the ones the test asks for
        settings = f"http://127.0.0.1:{server.port}/api/settings"
        tap(page, "a")
        assert len(ticks(page)) == 1                                   # on, before the status has said anything about it
        assert page.request.post(settings, data={"key_click": False}).status == 200
        page.evaluate("window.croomPage.refreshStatus()")
        tap(page, "b")
        assert len(ticks(page)) == 1                                   # off: the status said so
        assert page.request.post(settings, data={"key_click": True}).status == 200
        page.evaluate("window.croomPage.refreshStatus()")
        tap(page, "c")
        assert len(ticks(page)) == 2 and page.input_value("#link-input") == "abc"
        page.evaluate("croomKeyboard.setClick(false)")                 # and the keyboard's own switch works by itself
        tap(page, "d")
        page.evaluate("croomKeyboard.setClick(true)")
        tap(page, "e")
        assert len(ticks(page)) == 3
        page.close()


def test_the_options_screen_has_a_keyboard_group_that_shows_and_changes_the_click(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        group, on, off = page.locator(".keyboard-section"), page.locator("#key-click-on"), page.locator("#key-click-off")
        settings = f"http://127.0.0.1:{server.port}/api/settings"
        assert group.is_hidden()                                         # a part of the Options screen: the idle screen has none
        page.click("#open-options")
        assert group.is_visible() and group.locator(".kicker").text_content() == "Keyboard"
        assert page.get_by_role("group", name="Key click").is_visible()  # the two buttons are one group, named for what they set
        assert (on.inner_text(), off.inner_text()) == ("Key click on", "Key click off")
        assert (on.get_attribute("aria-pressed"), off.get_attribute("aria-pressed")) == ("true", "false")   # on, until the room says otherwise
        off.click()
        page.wait_for_function("document.getElementById('key-click-off').getAttribute('aria-pressed') === 'true'", timeout=5000)
        assert on.get_attribute("aria-pressed") == "false"
        assert page.request.get(settings).json()["key_click"] is False   # the service has it
        page.reload(wait_until="networkidle")                            # and it is the room's: it holds across a reload
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        page.click("#open-options")
        assert (on.get_attribute("aria-pressed"), off.get_attribute("aria-pressed")) == ("false", "true")
        on.click()
        page.wait_for_function("document.getElementById('key-click-on').getAttribute('aria-pressed') === 'true'", timeout=5000)
        assert off.get_attribute("aria-pressed") == "false" and page.request.get(settings).json()["key_click"] is True
        assert page.request.post(settings, data={"key_click": False}).status == 200   # changed from elsewhere: the buttons follow the next status
        page.evaluate("window.croomPage.refreshStatus()")
        assert (on.get_attribute("aria-pressed"), off.get_attribute("aria-pressed")) == ("false", "true")
        for opener in ("#open-camera-setup", "#open-screensaver"):       # no other screen has the group
            page.click(opener)
            assert group.is_hidden(), opener
            page.click("button:visible:has-text('Back')")
        assert group.is_visible()
        page.close()


def test_a_tap_on_the_key_click_buttons_while_the_page_is_busy_is_refused_like_every_other_button(browser):
    """The buttons post through the page's act(): one request at a time, and a tap while another is on its way is refused."""
    with PageServer() as server:
        page = open_page(browser, server)
        posts = []
        page.on("request", lambda r: posts.append(r.post_data) if r.method == "POST" and r.url.endswith("/api/settings") else None)
        page.click("#open-options")
        page.evaluate("window.croomPage.model.busy = true")             # what act() sets while a request is on its way
        page.click("#key-click-off")
        page.wait_for_timeout(200)
        assert posts == [] and page.get_attribute("#key-click-on", "aria-pressed") == "true"
        page.evaluate("window.croomPage.model.busy = false")
        page.click("#key-click-off")
        page.wait_for_function("document.getElementById('key-click-off').getAttribute('aria-pressed') === 'true'", timeout=5000)
        assert posts == ['{"key_click":false}']
        page.close()


def test_the_keyboard_group_goes_while_the_room_cannot_be_reached_and_comes_back(browser):
    """Its buttons need the room, as the screensaver's picker does: the page hides both while it cannot reach it."""
    with PageServer() as server:
        page = open_page(browser, server)
        page.click("#open-options")
        assert page.locator(".keyboard-section").is_visible()
        page.route("**/api/status", lambda route: route.abort())
        page.wait_for_function("document.body.dataset.state === 'offline'", timeout=8000)
        assert page.locator(".keyboard-section").is_hidden()
        page.unroute("**/api/status")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=8000)
        assert page.locator(".keyboard-section").is_visible()
        page.close()


@pytest.mark.parametrize("width, height", [(1280, 800), (1920, 1200), (800, 1280)])
def test_the_options_screen_with_its_four_items_and_its_keyboard_group_fits_the_kiosk_and_the_screens_either_side(browser, width, height):
    """The kiosk's 1280 by 800, the biggest screen's 1920 by 1200 and a portrait tablet's 800 by 1280 keep fits() on the Options screen with
    its four items (Camera framing, Screensaver, Door sign and TV) and the Key click group on it, the group right under the items."""
    with PageServer() as server:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        page.click("#open-options")
        group = page.locator(".keyboard-section")
        buttons_end = "Math.max(...Array.from(document.querySelectorAll('.view[data-view=options] .menu-item')).map(b => b.getBoundingClientRect().bottom))"
        assert page.locator(".view[data-view=options] .menu-item").count() == 4
        assert group.is_visible() and fits(page), "the options screen scrolls"
        box = group.bounding_box()
        assert 0 <= box["y"] - page.evaluate(buttons_end) <= 60 and box["y"] + box["height"] <= height   # right under the menu's buttons, on the screen
        page.close()


@pytest.mark.parametrize("width, height", [(1280, 800), (1024, 700), (1920, 1200)])
def test_the_four_items_of_the_options_screen_sit_two_by_two(browser, width, height):
    """Three in a row left the TV alone on a second row. Two by two reads as a set, and takes the same height."""
    with PageServer() as server:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        page.click("#open-options")
        boxes = page.locator(".view[data-view=options] .menu-item").evaluate_all(
            "bs => bs.map(b => { const r = b.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)]; })")
        (x0, y0, w0, h0), (x1, y1, w1, h1), (x2, y2, w2, h2), (x3, y3, w3, h3) = boxes
        assert y0 == y1 and y2 == y3 and y2 > y0 and x0 == x2 and x1 == x3 and x1 > x0, boxes      # two rows of two
        assert w0 == w1 == w2 == w3 and h0 == h1 == h2 == h3 == 160, boxes                         # the same size, as before
        assert y3 + h3 - y0 == 2 * 160 + 24, boxes                                                 # and the height three and one took
        page.close()


def test_the_menu_of_the_meeting_screen_keeps_its_two_in_a_row(browser):
    """Camera and Upcoming meetings, each as wide as a menu button is (the TV is in the second row, not here)."""
    with PageServer(tv=RemoteTv()) as server:
        page = open_page(browser, server)
        join_by_id(page)
        boxes = page.locator(".view[data-view=meeting] .menu-item").evaluate_all("bs => bs.map(b => { const r = b.getBoundingClientRect(); return [Math.round(r.top), Math.round(r.width)]; })")
        assert len(boxes) == 2 and len({top for top, _ in boxes}) == 1 and all(width == 420 for _, width in boxes), boxes
        page.close()


@pytest.mark.parametrize("width, height", [(1280, 800), (1920, 1200)])
def test_the_meeting_screens_rows_are_a_row_gap_apart_with_the_notice_up_as_without_it(browser, width, height):
    """The meeting view has no row template (style.css), so its rows are the ones showing. The room page had one of five rows with a 1fr for the last
    (the menu) from before the notice, the card and the second row: with the notice up the 1fr fell on the Sound strip, and the screen's free height
    became a gap above the menu."""
    with PageServer(role="host") as server:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        join_by_id(page)
        server.meeting.current_meeting.notice = "This meeting is being recorded."
        page.wait_for_function("document.getElementById('meeting-notice').textContent !== ''", timeout=5000)
        gaps = page.evaluate("""() => { const view = document.querySelector('.view[data-view=meeting]'), top = (s) => view.querySelector(s).getBoundingClientRect().top,
            rows = ['#meeting-actions', '#meeting-secondary', '.sound-section', '.menu'],
            end = (s) => { const kids = [...view.querySelector(s).children].filter(k => k.getClientRects().length); return kids[kids.length - 1].getBoundingClientRect().bottom; };   // where its content ends (a row given the free height has a box that goes on past it)
            return { rowGap: parseFloat(getComputedStyle(view).rowGap), between: rows.slice(1).map((s, i) => Math.round((top(s) - end(rows[i])) * 10) / 10) }; }""")
        assert gaps["between"] == [gaps["rowGap"]] * 3, gaps
        assert fits(page)
        page.close()


def test_the_four_items_of_the_options_screen_stack_in_one_column_below_1024_wide(browser):
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 800, "height": 1280})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        page.click("#open-options")
        boxes = page.locator(".view[data-view=options] .menu-item").evaluate_all("bs => bs.map(b => { const r = b.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.top)]; })")
        assert len({x for x, _ in boxes}) == 1 and [y for _, y in boxes] == sorted(y for _, y in boxes) and len({y for _, y in boxes}) == 4, boxes
        page.close()


def test_a_status_without_settings_leaves_the_click_on_and_the_buttons_saying_so(browser):
    """The page keeps working against a status that carries no settings (an agent from before them): on, as it is by default."""
    with PageServer() as server:
        def without_settings(route):
            body = route.fetch().json()
            body.pop("settings", None)
            route.fulfill(json=body)

        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.route("**/api/status", without_settings)
        page.add_init_script(RECORDED_AUDIO)
        page.goto(f"http://127.0.0.1:{server.port}/?keyboard=1", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert "settings" not in page.evaluate("window.croomPage.model.status")
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        tap(page, "a")
        assert len(ticks(page)) == 1
        page.click("#keyboard-toggle")
        page.click("#open-options")
        assert (page.get_attribute("#key-click-on", "aria-pressed"), page.get_attribute("#key-click-off", "aria-pressed")) == ("true", "false")
        page.close()


def test_the_page_keyboard_opens_on_the_kiosk_and_types_a_meet_link(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        assert page.locator("#keyboard").is_hidden()
        page.click("#link-input")
        assert page.locator("#keyboard").is_visible()
        tap(page, *"meet.google.com/abc-defg-hij")
        assert page.input_value("#link-input") == "meet.google.com/abc-defg-hij"
        tap(page, "hide")
        assert page.locator("#keyboard").is_hidden()
        page.close()


def test_the_page_keyboard_shifts_and_switches_layers(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        tap(page, *"zoom.us/j/")
        tap(page, "layer:symbols", *"9876?", "layer:letters", *"pwd", "layer:symbols", "layer:more", "=", "layer:letters", "a", "shift", "b", "layer:symbols", "1")
        assert page.input_value("#link-input") == "zoom.us/j/9876?pwd=aB1"
        tap(page, "backspace", "backspace")
        assert page.input_value("#link-input") == "zoom.us/j/9876?pwd=a"
        page.close()


def test_the_page_keyboard_joins_and_hides(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        tap(page, "layer:symbols", *"98765432100", "join")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert page.locator("#keyboard").is_hidden()
        assert server.meeting.joins[0][0].endswith("98765432100")
        page.close()


def test_the_keyboard_button_toggles_it_on_any_device(browser):
    with PageServer() as server:
        page = open_page(browser, server)                       # no ?keyboard=1
        page.click("#link-input")
        assert page.locator("#keyboard").is_hidden()            # a tablet has its own keyboard
        page.click("#keyboard-toggle")
        assert page.locator("#keyboard").is_visible()
        tap(page, *"abc")
        assert page.input_value("#link-input") == "abc"
        tap(page, "hide")
        assert page.locator("#keyboard").is_hidden()
        page.close()


def test_the_page_keyboard_fits_a_phone_width(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server, width=390, height=844)
        page.click("#link-input")
        assert page.locator("#keyboard").is_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        box = page.locator("#link-input").bounding_box()
        keyboard = page.locator("#keyboard").bounding_box()
        assert box["y"] >= 0 and box["y"] + box["height"] <= keyboard["y"]   # the field stays visible above the keys
        page.close()


def test_the_page_keyboard_keeps_the_field_above_the_keys_on_the_kiosk(browser):
    """The kiosk's screen does not scroll, but the keys cover its lower half: with the tall card of a booking that
    starts soon, the field must still come up above them, and the screen must fit again once they are gone."""
    skip_if_day_ends_within(1)                                 # the booking starts in 5 minutes, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Design review with a title long enough for two lines", 5)]) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/?keyboard=1", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        page.click("#link-input")
        assert page.locator("#keyboard").is_visible()
        box, keyboard = page.locator("#link-input").bounding_box(), page.locator("#keyboard").bounding_box()
        assert box["y"] >= 0 and box["y"] + box["height"] <= keyboard["y"]
        tap(page, "hide")
        assert fits(page) and page.evaluate("window.scrollY") == 0
        page.close()


def test_the_keyboard_opens_on_the_idle_screen_only_and_hides_when_a_meeting_starts(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_selector("#keyboard:not([hidden])", timeout=5000)
        assert page.locator(".schedule").is_hidden()                           # the bookings make room for the keys
        assert page.locator("#link-input").is_visible()
        tap(page, "layer:symbols", *"98765432100")
        tap(page, "join")
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        assert page.locator("#keyboard").is_hidden() and not page.evaluate("document.body.classList.contains('keyboard-open')")
        page.click("#meeting-actions button:has-text('Leave')")
        page.wait_for_timeout(600)                                            # a tap within half a second of the ask is a bounce, not the second tap
        page.click("#meeting-actions button:has-text('Tap again to leave')")
        page.wait_for_function("document.body.dataset.view === 'idle'", timeout=5000)
        assert page.locator(".schedule").is_visible()
        # A call that starts from elsewhere while the keys are up: the Join key is not what closes them this time, and
        # the hidden field's blur is not either (a browser need not report it), so only the view change is left to do it.
        page.click("#link-input")
        page.wait_for_selector("#keyboard:not([hidden])", timeout=5000)
        page.evaluate("window.addEventListener('focusout', (e) => e.stopImmediatePropagation(), true)")
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}',
                          headers={"Content-Type": "application/json"})
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=8000)
        assert page.locator("#keyboard").is_hidden() and not page.evaluate("document.body.classList.contains('keyboard-open')")
        page.click("#meeting-actions button:has-text('Leave')")
        page.wait_for_timeout(600)                                            # a tap within half a second of the ask is a bounce, not the second tap
        page.click("#meeting-actions button:has-text('Tap again to leave')")
        page.wait_for_function("document.body.dataset.view === 'idle'", timeout=5000)
        page.click("#open-options")
        page.evaluate("document.getElementById('keyboard-toggle').click()")   # out of sight on Options: a finger cannot reach the toggle, a script can
        assert page.locator("#keyboard").is_hidden()                          # nothing to type into on Options
        assert not page.evaluate("document.body.classList.contains('keyboard-open')")
        page.close()


# --- a Zoom meeting's passcode: the field under the link box, and the retry when Zoom asks for one ---

PASSCODE_ASKED = f"{ZOOM_PASSCODE_ASKED} (Wrong password (code 3004))"   # what the agent says when Zoom refuses a join over the passcode: its own prefix
ZOOM_LINK = "https://zoom.us/j/5590000559"                                        # what the service makes of the bare meeting ID 5590000559


def submit_and_wait_for_the_card(page):
    """Tap Join (the keyboard toggle sits before it) and wait for the idle card of a join that failed."""
    page.click("#link-form button[type=submit]")
    page.wait_for_function("document.body.dataset.state === 'error'", timeout=5000)


def card_buttons(page):
    return page.locator("#actions button").all_inner_texts()


def count_join_requests(page):
    """From now on, count the join requests the page sends (window.__joinRequests): a request that was not sent leaves no trace to wait for."""
    page.evaluate("""() => { window.__joinRequests = 0; const send = window.fetch;
        window.fetch = (path, ...rest) => { if (String(path).includes('/api/meeting/join')) window.__joinRequests += 1; return send(path, ...rest); }; }""")


def test_the_passcode_field_shows_for_a_zoom_meeting_whose_link_has_no_passcode(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        field = page.locator("#passcode-input")
        assert field.is_hidden()                                                # an empty link box
        page.fill("#link-input", "5590000559")
        assert field.is_visible()                                               # a bare meeting ID
        page.fill("#link-input", "https://zoom.us/j/5590000559?pwd=abc")
        assert field.is_hidden()                                                # the link carries its own passcode
        page.fill("#link-input", "https://meet.google.com/abc-defg-hij")
        assert field.is_hidden()                                                # and a Meet link has none to ask for
        page.fill("#link-input", "https://zoom.us/j/5590000559")
        assert field.is_visible()                                               # a Zoom link without one
        page.close()


def test_the_passcode_field_follows_what_the_link_box_holds(browser):
    cases = [
        ("5590000559", True), ("559 000 0559", True), ("123456789", True), ("12345678901", True),   # a meeting ID: 9 to 11 digits, spaces or none
        ("12345678", False), ("123456789012", False), ("5590000559x", False),
        ("zoom.us/j/5590000559", True), ("https://zoom.us/j/5590000559", True),
        ("https://us02web.zoom.us/j/5590000559", True), ("https://acme.zoomgov.com/j/1600000000", True),
        ("https://zoom.us/j/5590000559?pwd=abc", False), ("https://zoom.us/j/5590000559?from=addon&pwd=abc", False),
        ("https://meet.google.com/abc-defg-hij", False), ("https://teams.microsoft.com/l/meetup-join/19", False),
        ("https://notzoom.us/j/5590000559", False), ("https://zoom.us.example.com/j/5590000559", False),   # look-alike hosts
        ("not a meeting link", False), ("   ", False), ("", False),
        ("https://zoom.us/j/5590000559#pwd=abc", True),                       # a pwd in the fragment is not the query's
        ("HTTPS://ZOOM.US/j/5590000559", True), ("https://ZOOM.US/j/5590000559?pwd=abc", False),   # hosts in capitals
        ("https://zoom.us/wc/5590000559/join", True), ("https://zoom.us/wc/5590000559/join?pwd=abc", False),   # the web client's links
        ("abc-defg-hij", False),                                               # a Meet code on its own
        ("5551234567", True),                                                  # 10 digits are a meeting ID, whatever else they might be
    ]
    with PageServer() as server:
        page = open_page(browser, server)
        for text, shown in cases:
            page.fill("#link-input", text)
            assert page.locator("#passcode-input").is_visible() == shown, f"{text!r}: the field should be {'shown' if shown else 'hidden'}"
        page.close()


def test_the_passcode_field_is_judged_with_every_key_and_emptied_when_it_goes(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        field = page.locator("#passcode-input")
        page.fill("#link-input", "zoom.us/j/5590000559")
        page.fill("#passcode-input", "940104")
        link = page.locator("#link-input")
        link.focus()
        link.press("End")                                                       # a field takes the focus with its caret at the start
        link.press_sequentially("?pwd=abc")                                     # a link that now carries its own passcode
        assert page.input_value("#link-input") == "zoom.us/j/5590000559?pwd=abc"
        assert field.is_hidden() and page.input_value("#passcode-input") == ""
        page.fill("#link-input", "zoom.us/j/5590000559")
        assert field.is_visible() and page.input_value("#passcode-input") == "", "the code typed for the other link came back"
        page.close()


def test_join_sends_the_passcode_with_the_link_and_both_boxes_clear(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.fill("#link-input", "5590000559")
        page.fill("#passcode-input", " Ab9@-_* ")                              # trimmed; the agent's alphabet is letters, digits and @ - _ *
        page.click("#link-form button[type=submit]")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert server.meeting.joins == [(ZOOM_LINK, "Lab", "Ab9@-_*")]
        assert page.input_value("#link-input") == "" and page.input_value("#passcode-input") == ""
        assert page.locator("#passcode-input").is_hidden()
        page.close()


def test_a_meeting_id_written_with_spaces_is_sent_as_its_digits(browser):
    """An invitation prints the ID in groups ("559 000 0559"), and the agent takes the digits only."""
    with PageServer() as server:
        page = open_page(browser, server)
        page.fill("#link-input", " 559 000 0559 ")
        assert page.locator("#passcode-input").is_visible()
        page.fill("#passcode-input", "940104")
        page.click("#link-form button[type=submit]")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert server.meeting.joins == [(ZOOM_LINK, "Lab", "940104")]
        page.close()


def test_the_join_request_carries_a_passcode_only_when_the_field_has_one(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        bodies = []

        def record(route):
            bodies.append(route.request.post_data_json)
            route.continue_()

        page.route("**/api/meeting/join", record)
        page.fill("#link-input", "5590000559")
        page.fill("#passcode-input", "   ")                                    # spaces are no passcode
        page.click("#link-form button[type=submit]")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        page.click("#meeting-actions button:has-text('Leave')")
        page.wait_for_timeout(600)                                            # a tap within half a second of the ask is a bounce, not the second tap
        page.click("#meeting-actions button:has-text('Tap again to leave')")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        page.fill("#link-input", "https://meet.google.com/abc-defg-hij")       # no field for this one
        page.click("#link-form button[type=submit]")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        page.click("#meeting-actions button:has-text('Leave')")
        page.wait_for_timeout(600)                                            # a tap within half a second of the ask is a bounce, not the second tap
        page.click("#meeting-actions button:has-text('Tap again to leave')")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        page.fill("#link-input", "5590000559")
        page.fill("#passcode-input", "940104")
        page.click("#link-form button[type=submit]")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert bodies == [{"url": "5590000559"}, {"url": "https://meet.google.com/abc-defg-hij"},
                          {"url": "5590000559", "passcode": "940104"}]
        page.close()


def test_a_passcode_the_agent_refuses_shows_its_words_and_keeps_both_boxes(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.fill("#link-input", "5590000559")
        page.fill("#passcode-input", "94 0104")                                 # the page does not judge it: the agent does
        page.click("#link-form button[type=submit]")
        page.wait_for_function("document.getElementById('message').innerText.startsWith('Passcode must be')", timeout=5000)
        assert page.input_value("#link-input") == "5590000559" and page.input_value("#passcode-input") == "94 0104"
        assert page.locator("#passcode-input").is_visible()
        assert server.meeting.joins == []
        page.close()


def test_a_join_zoom_refuses_over_the_passcode_offers_to_join_again_with_the_link_kept(browser):
    with PageServer() as server:
        server.meeting.fail_before_joining = RuntimeError(PASSCODE_ASKED)
        page = open_page(browser, server)
        page.fill("#link-input", "5590000559")
        submit_and_wait_for_the_card(page)
        assert page.locator("#kicker").inner_text().upper() == "COULDN'T JOIN"
        assert page.locator("#detail").inner_text() == PASSCODE_ASKED
        assert card_buttons(page) == ["Dismiss", "Join again"]
        assert page.input_value("#link-input") == "5590000559"                  # the link that failed is back in the box
        assert page.locator("#passcode-input").is_visible()
        assert page.evaluate("document.activeElement.id") != "passcode-input", "the field was grabbed on a page with a keyboard of its own"
        server.meeting.fail_before_joining = None                               # Zoom takes it this time
        page.fill("#passcode-input", "940104")
        page.click("#actions button:has-text('Join again')")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert server.meeting.joins == [(ZOOM_LINK, "Lab", None), (ZOOM_LINK, "Lab", "940104")]
        assert page.input_value("#link-input") == "" and page.locator("#passcode-input").is_hidden()
        page.close()


def test_a_second_refusal_over_the_passcode_gives_the_link_back_and_asks_again(browser):
    with PageServer() as server:
        server.meeting.fail_before_joining = RuntimeError(PASSCODE_ASKED)
        page = open_page(browser, server)
        page.fill("#link-input", "5590000559")
        submit_and_wait_for_the_card(page)
        page.fill("#passcode-input", "000000")
        page.click("#actions button:has-text('Join again')")
        # The accepted join emptied both boxes; the card that comes back gives the link again and waits for another code.
        page.wait_for_function("document.getElementById('passcode-input').value === '' && document.getElementById('link-input').value === '5590000559'", timeout=5000)
        assert server.meeting.joins == [(ZOOM_LINK, "Lab", None), (ZOOM_LINK, "Lab", "000000")]
        assert card_buttons(page) == ["Dismiss", "Join again"] and page.locator("#passcode-input").is_visible()
        page.close()


@pytest.mark.parametrize("refusal", ["Meet refused the room", "The host changed the meeting passcode", "Zoom did not connect; the page says: Enter the passcode",
                                     f"The room's screen said: {ZOOM_PASSCODE_ASKED} (code 3004)"])   # the words, but not at the start
def test_any_other_failure_is_the_card_it_always_was(browser, refusal):
    with PageServer() as server:
        server.meeting.fail_before_joining = RuntimeError(refusal)
        page = open_page(browser, server)
        page.fill("#link-input", "5590000559")
        submit_and_wait_for_the_card(page)
        assert page.locator("#detail").inner_text() == refusal
        assert card_buttons(page) == ["Dismiss"]                                # no Join again
        assert page.locator("#passcode-input").is_hidden()                      # and no field: the link left the box with the join
        page.close()


def test_a_booking_that_needs_a_passcode_gets_the_same_retry_and_stays_the_booking(browser):
    """Nothing was typed: the status says which meeting failed, and the box shows its link. Join again joins the booking
    again with the code, so the call keeps its title: the meeting line names it, and the bar does not call it next."""
    skip_if_day_ends_within(1)                                                 # the booking starts in 5 minutes, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Design review", 5, url=ZOOM_LINK)]) as server:
        server.meeting.fail_with = RuntimeError(PASSCODE_ASKED)                # a provider that joined, and was refused
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        bodies = []

        def record(route):
            bodies.append(route.request.post_data_json)
            route.continue_()

        page.route("**/api/meeting/join", record)
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        page.click("#actions button:has-text('Join now')")
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=8000)
        assert page.locator("#headline").inner_text() == "Couldn't join Design review"
        assert page.input_value("#link-input") == ZOOM_LINK and page.locator("#passcode-input").is_visible()
        assert card_buttons(page) == ["Dismiss", "Join again"]
        server.meeting.fail_with = None
        page.fill("#passcode-input", "940104")
        page.click("#actions button:has-text('Join again')")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert server.meeting.joins[-1] == (ZOOM_LINK, "Lab", "940104")
        assert bodies == [{"event_id": "e1"}, {"event_id": "e1", "passcode": "940104"}]
        assert page.locator("#meeting-detail").inner_text() == "Design review, Zoom"
        assert page.locator("#next-warning").is_hidden()                        # the booking the room is in is not next
        page.close()


def test_a_booking_card_whose_link_the_person_edits_joins_the_link_they_typed(browser):
    """The box is the person's own once they edit it: Join again sends what it holds, not the booking."""
    skip_if_day_ends_within(1)                                                 # the booking starts in 5 minutes, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Design review", 5, url=ZOOM_LINK)]) as server:
        server.meeting.fail_with = RuntimeError(PASSCODE_ASKED)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        bodies = []

        def record(route):
            bodies.append(route.request.post_data_json)
            route.continue_()

        page.route("**/api/meeting/join", record)
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        page.click("#actions button:has-text('Join now')")
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=8000)
        server.meeting.fail_with = None
        page.fill("#link-input", "https://zoom.us/j/33333333333")
        page.fill("#passcode-input", "940104")
        page.click("#actions button:has-text('Join again')")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert bodies[-1] == {"url": "https://zoom.us/j/33333333333", "passcode": "940104"}
        assert server.meeting.joins[-1] == ("https://zoom.us/j/33333333333", "Lab", "940104")
        page.close()


@pytest.mark.parametrize("joined", ["here", "from another page"])
def test_the_row_of_the_booking_whose_passcode_card_is_up_does_not_join_it_again(browser, joined):
    """The row's Join sends the booking alone and ignores the typed code: the card's Join again is the way to retry. The
    other bookings' rows stay as they were, and the row is back when the card goes."""
    skip_if_day_ends_within(1)                                                 # the bookings start in 5 and 8 minutes, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Design review", 5, url=ZOOM_LINK),
                                     event("e2", "Budget review", 8, url="https://zoom.us/j/22222222222")]) as server:
        server.meeting.fail_with = RuntimeError(PASSCODE_ASKED)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)

        def row(title):
            return page.locator("#events li", has_text=title).locator("button")

        assert row("Design review").is_enabled() and row("Budget review").is_enabled()
        if joined == "here":
            page.click("#actions button:has-text('Join now')")                  # the next booking's
        else:
            page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"event_id": "e1"}', headers=JSON)
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=8000)
        assert card_buttons(page) == ["Dismiss", "Join again"]
        assert row("Design review").is_disabled()
        assert row("Budget review").is_enabled()
        page.click("#actions button:has-text('Dismiss')")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        assert row("Design review").is_enabled() and row("Budget review").is_enabled()
        page.close()


def test_two_bookings_with_one_title_lose_only_the_row_of_the_one_this_page_joined(browser):
    skip_if_day_ends_within(1)                                                 # the bookings start in 5 and 8 minutes, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Interview", 5, url=ZOOM_LINK),
                                     event("e2", "Interview", 8, url="https://zoom.us/j/22222222222")]) as server:
        server.meeting.fail_with = RuntimeError(PASSCODE_ASKED)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        page.click("#actions button:has-text('Join now')")
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=8000)
        assert card_buttons(page) == ["Dismiss", "Join again"]
        assert page.locator("#events li").nth(0).locator("button").is_disabled()
        assert page.locator("#events li").nth(1).locator("button").is_enabled()
        page.close()


def test_a_passcode_card_that_replaces_another_error_card_disables_its_bookings_row(browser):
    """Both cards are the error state, so nothing else about the row changes when the second comes."""
    skip_if_day_ends_within(1)                                                 # the booking starts in 5 minutes, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Design review", 5, url=ZOOM_LINK)]) as server:
        server.meeting.fail_with = RuntimeError("Meet refused the room")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        row = page.locator("#events li", has_text="Design review").locator("button")
        page.click("#actions button:has-text('Join now')")
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=8000)
        assert card_buttons(page) == ["Dismiss"] and row.is_enabled()           # a refusal that is not about the passcode
        server.meeting.fail_with = RuntimeError(PASSCODE_ASKED)
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"event_id": "e1"}', headers=JSON)   # joined again, from another page
        page.wait_for_function(f"document.getElementById('detail').innerText === {PASSCODE_ASKED!r}", timeout=8000)
        assert card_buttons(page) == ["Dismiss", "Join again"]
        assert row.is_disabled()
        page.close()


@pytest.mark.parametrize("booking", [False, True])
def test_join_again_with_an_empty_field_asks_for_the_code_instead_of_joining_again(browser, booking):
    """Zoom has just refused the join for want of a passcode: sending it again without one is pointless. The field takes
    the focus, and the next Join again carries what was typed (a booking's card joins the booking, as before)."""
    if booking:
        skip_if_day_ends_within(1)                                             # the booking starts in 5 minutes, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Design review", 5, url=ZOOM_LINK)] if booking else []) as server:
        server.meeting.fail_with = RuntimeError(PASSCODE_ASKED)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        if booking:
            page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
            page.click("#actions button:has-text('Join now')")
        else:
            page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
            page.fill("#link-input", "5590000559")
            page.click("#link-form button[type=submit]")
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=8000)
        assert card_buttons(page) == ["Dismiss", "Join again"] and page.locator("#passcode-input").is_visible()
        count_join_requests(page)
        page.click("#actions button:has-text('Join again')")                    # no code typed
        assert page.evaluate("window.__joinRequests") == 0
        assert page.evaluate("document.activeElement.id") == "passcode-input"
        server.meeting.fail_with = None
        page.fill("#passcode-input", "940104")
        page.click("#actions button:has-text('Join again')")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert page.evaluate("window.__joinRequests") == 1
        assert server.meeting.joins[-1] == (ZOOM_LINK, "Lab", "940104")
        page.close()


def test_join_again_with_no_field_to_fill_joins_the_link_in_the_box(browser):
    """The person put a link with a passcode of its own in the box: the field has gone, and the join has nothing to wait for."""
    with PageServer() as server:
        server.meeting.fail_before_joining = RuntimeError(PASSCODE_ASKED)
        page = open_page(browser, server)
        page.fill("#link-input", "5590000559")
        submit_and_wait_for_the_card(page)
        server.meeting.fail_before_joining = None
        page.fill("#link-input", "https://zoom.us/j/33333333333?pwd=abc")
        assert page.locator("#passcode-input").is_hidden()
        page.click("#actions button:has-text('Join again')")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert server.meeting.joins[-1] == ("https://zoom.us/j/33333333333?pwd=abc", "Lab", None)
        page.close()


def test_the_passcode_card_has_the_field_even_for_a_link_that_carries_a_passcode(browser):
    """Zoom refused over the passcode and the link has a pwd= of its own (gone stale, say): the field is judged by the
    link only while the person edits it, so the card still offers it, with Join again."""
    skip_if_day_ends_within(1)                                                 # the booking starts in 5 minutes, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Design review", 5)]) as server:   # the helper's default link carries ?pwd=abc
        server.meeting.fail_with = RuntimeError(PASSCODE_ASKED)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        page.click("#actions button:has-text('Join now')")
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=8000)
        assert page.input_value("#link-input") == "https://zoom.us/j/98765432100?pwd=abc"
        assert page.locator("#passcode-input").is_visible() and card_buttons(page) == ["Dismiss", "Join again"]
        page.fill("#link-input", "https://zoom.us/j/98765432100")               # edited to drop the stale pwd: now it is judged by what it says
        assert page.locator("#passcode-input").is_visible()
        page.fill("#link-input", "https://zoom.us/j/98765432100?pwd=other")
        assert page.locator("#passcode-input").is_hidden()
        page.close()


@pytest.mark.parametrize("typed, shown", [
    ("https://meet.google.com/abc-defg-hij", False), ("not a meeting link", False),
    ("https://zoom.us/j/22222222222?pwd=abc", False),                          # a Zoom link with a passcode of its own needs no field either
    ("https://zoom.us/j/22222222222", True), ("22222222222", True),
    ("", True), ("   ", True),                                                # an emptied box (or one of spaces), and no link to put back: the field is there to be filled
])
def test_the_card_forces_the_passcode_field_only_on_a_box_it_makes_sense_for(browser, typed, shown):
    """The join took its time and the person began another meeting in the box meanwhile: the card forces the field only on
    a box that is empty, holds the link the card put back, or holds a Zoom meeting without a passcode."""
    with PageServer() as server:
        server.meeting.fail_before_joining = RuntimeError(PASSCODE_ASKED)     # the status names no link, so the card has none to put back
        gate, asked = threading.Event(), threading.Event()
        join = server.meeting.join_meeting

        async def held(*args, **kwargs):                                        # the provider has not got to the join yet
            asked.set()
            while not gate.is_set():
                await asyncio.sleep(0.01)
            return await join(*args, **kwargs)

        server.meeting.join_meeting = held
        page = open_page(browser, server)
        page.fill("#link-input", "5590000559")
        page.click("#link-form button[type=submit]")
        assert asked.wait(5)
        page.wait_for_function("document.getElementById('link-input').value === ''", timeout=5000)   # the accepted join emptied the box
        page.fill("#link-input", "x")                                           # the box is the person's own from the first key
        page.fill("#link-input", typed)
        gate.set()
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=8000)
        assert card_buttons(page) == ["Dismiss", "Join again"]
        assert page.input_value("#link-input") == typed                         # the card puts nothing over what the person typed
        assert page.locator("#passcode-input").is_visible() == shown
        page.close()


def test_a_link_from_an_earlier_join_is_not_put_back_for_another_meeting(browser):
    """The card gives back the link of the join this page started and lost. Once a call has taken, that link is spent:
    a later call that Zoom refuses (started from elsewhere) must not bring it back."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)                                                        # 98765432100, a call that works
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/leave", data="{}", headers=JSON)   # over from elsewhere: the page's own Leave lets go of its link too
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=8000)
        server.meeting.fail_before_joining = RuntimeError(PASSCODE_ASKED)
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/22222222222"}', headers=JSON)
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=8000)
        assert card_buttons(page) == ["Dismiss", "Join again"]
        assert page.input_value("#link-input") == ""
        page.close()


def test_the_link_of_a_join_that_failed_another_way_is_not_put_back_either(browser):
    with PageServer() as server:
        server.meeting.fail_before_joining = RuntimeError("Meet refused the room")
        page = open_page(browser, server)
        page.fill("#link-input", "5590000559")
        submit_and_wait_for_the_card(page)                                      # refused, but not over the passcode: the card stays up
        server.meeting.fail_before_joining = RuntimeError(PASSCODE_ASKED)
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/22222222222"}', headers=JSON)
        page.wait_for_function(f"document.getElementById('detail').innerText === {PASSCODE_ASKED!r}", timeout=8000)   # another device's call, refused over the passcode
        assert card_buttons(page) == ["Dismiss", "Join again"]
        assert page.input_value("#link-input") == ""
        page.close()


def test_giving_a_join_up_lets_go_of_its_link(browser):
    """What the page remembers of a join lasts until the join is given up: a Cancel or Dismiss must not leave a link behind for a later card."""
    with PageServer() as server:
        server.meeting.fail_before_joining = RuntimeError(PASSCODE_ASKED)
        page = open_page(browser, server)
        page.fill("#link-input", "5590000559")
        submit_and_wait_for_the_card(page)
        assert page.input_value("#link-input") == "5590000559"
        page.click("#actions button:has-text('Dismiss')")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        # Another device's join, refused over the passcode before it began: the status names no link, and this page's is given up.
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/22222222222"}', headers=JSON)
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=8000)
        assert card_buttons(page) == ["Dismiss", "Join again"]
        assert page.input_value("#link-input") == ""
        page.close()


@pytest.mark.parametrize("dismissed", ["here", "from another page"])
def test_the_link_the_card_put_back_goes_with_the_card(browser, dismissed):
    """A link with its own pwd= that Zoom refused over the passcode: the card puts it back in plain view, with the field.
    When the card goes, the link goes with it and the field follows the empty box."""
    with PageServer() as server:
        server.meeting.fail_before_joining = RuntimeError(PASSCODE_ASKED)
        page = open_page(browser, server)
        page.fill("#link-input", "https://zoom.us/j/5590000559?pwd=abc")
        assert page.locator("#passcode-input").is_hidden()
        submit_and_wait_for_the_card(page)
        assert page.input_value("#link-input") == "https://zoom.us/j/5590000559?pwd=abc"
        assert page.locator("#passcode-input").is_visible()
        if dismissed == "here":
            page.click("#actions button:has-text('Dismiss')")
        else:
            page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/leave", data="{}", headers=JSON)
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=8000)
        assert page.input_value("#link-input") == ""
        assert page.locator("#passcode-input").is_hidden()
        page.close()


def test_a_join_cancelled_from_another_page_lets_go_of_its_link(browser):
    """This page's join began and was cancelled elsewhere (joining, then idle, no error): its link is spent. A later join
    that Zoom refuses over the passcode puts its own link in the box, not this one."""
    with PageServer() as server:
        page = open_page(browser, server)
        join = server.meeting.join_meeting

        async def begins(url, display_name=None, camera_on=None, mic_on=None, passcode=None):   # joining, until someone cancels
            server.meeting.joins.append((url, display_name, passcode))
            server.meeting.set_state(MeetingState.JOINING)

        server.meeting.join_meeting = begins
        page.fill("#link-input", "5590000559")
        page.click("#link-form button[type=submit]")
        page.wait_for_function("document.getElementById('meeting-state').innerText === 'Joining'", timeout=5000)
        server.meeting.set_state(MeetingState.IDLE)                             # cancelled from another page
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=8000)
        server.meeting.join_meeting = join
        server.meeting.fail_with = RuntimeError(PASSCODE_ASKED)                 # another page's join, which Zoom refuses
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/22222222222"}', headers=JSON)
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=8000)
        assert card_buttons(page) == ["Dismiss", "Join again"]
        assert page.input_value("#link-input") == "https://zoom.us/j/22222222222"   # the link of the join that failed, from the status
        page.close()


def test_a_card_dismissed_from_another_page_lets_go_of_its_link(browser):
    """This page's join was refused over the passcode and the card dismissed on another page (error, then idle): its link is
    spent. A later join that Zoom refuses the same way puts its own link in the box, not this one."""
    with PageServer() as server:
        server.meeting.fail_with = RuntimeError(PASSCODE_ASKED)
        page = open_page(browser, server)
        page.fill("#link-input", "5590000559")
        submit_and_wait_for_the_card(page)
        assert page.input_value("#link-input") == "5590000559"
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/leave", data="{}", headers=JSON)   # Dismiss, tapped elsewhere
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=8000)
        assert page.input_value("#link-input") == ""
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/22222222222"}', headers=JSON)   # another device's join, refused the same way
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=8000)
        assert card_buttons(page) == ["Dismiss", "Join again"]
        assert page.input_value("#link-input") == "https://zoom.us/j/22222222222"   # the link of the join that failed, from the status
        page.close()


def test_a_poll_after_the_card_has_landed_takes_neither_the_focus_nor_the_field_back(browser):
    """The card lands once: the field is shown and, on the kiosk, takes the focus. The polls after that leave both to the person."""
    with PageServer() as server:
        server.meeting.fail_with = RuntimeError(PASSCODE_ASKED)
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        tap(page, "layer:symbols", *"5590000559", "join")
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=8000)
        page.wait_for_function("document.activeElement.id === 'passcode-input'", timeout=5000)
        page.click("#link-input")                                               # the person goes back to the ID
        page.evaluate("window.croomPage.refreshStatus()")                       # a poll, and the render after it
        assert page.evaluate("document.activeElement.id") == "link-input"
        page.fill("#link-input", "https://zoom.us/j/5590000559?pwd=abc")        # a link that brings its own passcode
        assert page.locator("#passcode-input").is_hidden()
        page.evaluate("window.croomPage.refreshStatus()")
        assert page.locator("#passcode-input").is_hidden()
        assert card_buttons(page) == ["Dismiss", "Join again"]
        page.close()


def test_a_link_box_the_person_has_emptied_stays_empty_under_the_card(browser):
    with PageServer() as server:
        server.meeting.fail_before_joining = RuntimeError(PASSCODE_ASKED)
        page = open_page(browser, server)
        page.fill("#link-input", "5590000559")
        submit_and_wait_for_the_card(page)
        assert page.input_value("#link-input") == "5590000559"
        page.fill("#link-input", "")                                            # the person wants another meeting
        page.evaluate("window.croomPage.refreshStatus()")                       # a poll, and the render after it
        assert page.input_value("#link-input") == ""
        page.fill("#link-input", "https://meet.google.com/abc-defg-hij")
        page.evaluate("window.croomPage.refreshStatus()")
        assert page.input_value("#link-input") == "https://meet.google.com/abc-defg-hij"
        assert page.locator("#passcode-input").is_hidden()                      # the field follows the link again, though the card is up
        page.close()


def test_the_old_card_still_up_as_a_retry_begins_leaves_no_link_behind(browser):
    """A provider keeps its error state until it has begun the next join, so the poll right after Join again can still
    show the old card, and the card gives the link back. The call that follows must not leave it in the box."""
    with PageServer() as server:
        server.meeting.fail_with = RuntimeError(PASSCODE_ASKED)
        page = open_page(browser, server)
        page.fill("#link-input", "5590000559")
        submit_and_wait_for_the_card(page)
        gate, asked = threading.Event(), threading.Event()
        join = server.meeting.join_meeting

        async def held(*args, **kwargs):                                        # the provider has not got to the next join yet
            asked.set()
            while not gate.is_set():
                await asyncio.sleep(0.01)
            return await join(*args, **kwargs)

        server.meeting.join_meeting = held
        server.meeting.fail_with = None
        page.fill("#passcode-input", "940104")
        page.click("#actions button:has-text('Join again')")
        assert asked.wait(5)
        # The accepted join emptied both boxes; the old card, still shown, gives the link back.
        page.wait_for_function("document.getElementById('passcode-input').value === '' && document.getElementById('link-input').value === '5590000559'", timeout=5000)
        gate.set()                                                              # now the provider takes it
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/leave", data="{}", headers=JSON)
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=8000)
        assert page.input_value("#link-input") == "" and page.locator("#passcode-input").is_hidden()
        page.close()


def test_the_page_keyboard_types_into_whichever_field_had_the_focus_last(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        tap(page, *"zoom.us/j/", "layer:symbols", *"5590000559", "layer:letters")  # the link, with the keys left on the letters
        assert page.locator("#passcode-input").is_visible()
        tap(page, "hide")
        assert page.locator("#keyboard").is_hidden()
        page.click("#passcode-input")
        assert page.locator("#keyboard").is_visible()                           # a tap on the field brings the keys up ...
        assert page.locator("#keyboard button[data-key='9']").is_visible()      # ... on the digits,
        assert page.locator("#keyboard button[data-key='q']").count() == 0      # not the letters they were left on
        tap(page, *"940104")
        assert page.input_value("#passcode-input") == "940104"
        assert page.input_value("#link-input") == "zoom.us/j/5590000559"
        page.click("#link-input")                                               # and back: the keys follow the focus
        assert page.locator("#keyboard").is_visible()
        assert page.locator("#keyboard button[data-key='q']").is_visible()      # on the letters the link was left on, not the passcode's digits
        tap(page, "backspace")
        assert page.input_value("#link-input") == "zoom.us/j/559000055"
        assert page.input_value("#passcode-input") == "940104"
        page.close()


def test_each_field_keeps_its_own_layer(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        assert page.locator("#keyboard button[data-key='q']").is_visible()      # a link opens on the letters
        tap(page, "layer:symbols", *"5590000559")                               # and is left on the digits
        page.click("#passcode-input")
        tap(page, "layer:letters", "a", "layer:symbols", "1", "layer:letters")  # the passcode is left on the letters
        assert page.input_value("#passcode-input") == "a1"
        page.click("#link-input")
        assert page.locator("#keyboard button[data-key='9']").is_visible()      # the link's own layer
        page.click("#passcode-input")
        assert page.locator("#keyboard button[data-key='q']").is_visible()      # and the passcode's
        page.close()


def keyboard_changes(page):
    """From now on, count every time the keys are shown or hidden."""
    page.evaluate("""() => { window.__keyboardChanges = 0;
        new MutationObserver(() => { window.__keyboardChanges += 1; })
          .observe(document.getElementById('keyboard'), { attributes: true, attributeFilter: ['hidden'] }); }""")


def test_on_the_kiosk_the_keys_stay_up_while_the_person_moves_between_the_fields(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        tap(page, "layer:symbols", *"5590000559")
        keyboard_changes(page)
        page.click("#passcode-input")
        page.click("#link-input")
        page.click("#passcode-input")
        assert page.locator("#keyboard").is_visible()
        assert page.evaluate("window.__keyboardChanges") == 0, "the keys went away and came back between the fields"
        page.close()


def test_off_the_kiosk_the_keys_from_the_button_stay_up_while_the_person_moves_between_the_fields(browser):
    with PageServer() as server:
        page = open_page(browser, server)                                       # no ?keyboard=1: a tablet with the keys from the button
        page.fill("#link-input", "5590000559")
        page.click("#link-input")
        page.click("#keyboard-toggle")
        assert page.locator("#keyboard").is_visible()
        page.click("#passcode-input")
        assert page.locator("#keyboard").is_visible()
        assert page.locator("#keyboard button[data-key='9']").is_visible()      # on the passcode's digits
        tap(page, "1", "2")
        assert page.input_value("#passcode-input") == "12" and page.input_value("#link-input") == "5590000559"
        page.click("#link-input")
        assert page.locator("#keyboard").is_visible()
        assert page.locator("#keyboard button[data-key='q']").is_visible()      # and the link's letters
        page.close()


def test_the_keys_go_when_the_passcode_field_loses_the_focus_to_the_page(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        tap(page, "layer:symbols", *"5590000559")
        page.click("#passcode-input")
        assert page.locator("#keyboard").is_visible()
        page.click("#headline")                                                 # a tap anywhere else on the page
        assert page.locator("#keyboard").is_hidden()
        page.close()


def test_on_the_kiosk_a_tap_on_a_field_that_kept_the_focus_brings_the_keys_back(browser):
    """After a Join the agent refused, or Hide, the field still has the focus, so a tap on it fires no focus event."""
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        tap(page, "layer:symbols", *"5590000559")
        page.click("#passcode-input")
        tap(page, "9", "space", "4", "join")                                    # a space: the agent refuses the code
        page.wait_for_function("document.getElementById('message').innerText.startsWith('Passcode must be')", timeout=5000)
        assert page.locator("#keyboard").is_hidden() and page.evaluate("document.activeElement.id") == "passcode-input"
        page.click("#passcode-input")                                           # the person taps it to put the code right
        assert page.locator("#keyboard").is_visible()
        assert page.locator("#keyboard button[data-key='9']").is_visible()
        tap(page, "backspace", "backspace", "backspace")
        assert page.input_value("#passcode-input") == ""
        page.click("#link-input")
        tap(page, "hide")
        assert page.locator("#keyboard").is_hidden() and page.evaluate("document.activeElement.id") == "link-input"
        page.click("#link-input")
        assert page.locator("#keyboard").is_visible()
        page.close()


def test_the_page_keyboard_join_key_sends_the_passcode_from_its_field(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        tap(page, "layer:symbols", *"5590000559")
        page.click("#passcode-input")
        tap(page, *"940104", "join")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert server.meeting.joins == [(ZOOM_LINK, "Lab", "940104")]
        assert page.locator("#keyboard").is_hidden()
        page.close()


def test_the_page_keyboard_does_not_type_into_a_passcode_field_that_has_gone(browser):
    """After a join the passcode field hides with the link. A tap on the keyboard button then must aim at the link box."""
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        tap(page, "layer:symbols", *"5590000559")
        page.click("#passcode-input")
        tap(page, *"940104", "join")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        page.click("#meeting-actions button:has-text('Leave')")
        page.wait_for_timeout(600)                                            # a tap within half a second of the ask is a bounce, not the second tap
        page.click("#meeting-actions button:has-text('Tap again to leave')")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        page.click("#keyboard-toggle")
        assert page.locator("#keyboard").is_visible()
        tap(page, *"123")                                                       # on the link box's own layer: the digits it was left on for the ID
        assert page.input_value("#link-input") == "123" and page.input_value("#passcode-input") == ""
        page.close()


def test_in_the_kiosk_the_passcode_card_hands_the_keys_to_the_passcode_field(browser):
    with PageServer() as server:
        server.meeting.fail_before_joining = RuntimeError(PASSCODE_ASKED)
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        tap(page, "layer:symbols", *"5590000559", "layer:letters", "join")      # the keys go away with Join, left on the letters
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=5000)
        page.wait_for_selector("#keyboard:not([hidden])", timeout=5000)         # Zoom asked: the keys are up again, for the code
        assert page.evaluate("document.activeElement.id") == "passcode-input"
        assert page.input_value("#link-input") == "5590000559"
        assert page.locator("#keyboard button[data-key='9']").is_visible()      # on the digits
        box, keyboard = page.locator("#passcode-input").bounding_box(), page.locator("#keyboard").bounding_box()
        assert box["y"] >= 0 and box["y"] + box["height"] <= keyboard["y"], "the passcode field is under the keys"   # the card is taller than what it replaced
        server.meeting.fail_before_joining = None
        tap(page, *"940104", "join")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert server.meeting.joins[-1] == (ZOOM_LINK, "Lab", "940104")
        page.close()


def test_a_kiosk_page_that_opens_on_the_passcode_card_brings_up_the_keys_for_the_code(browser):
    """A page opened (or reloaded) while the card is up lands it with its first answer, over a screen that was still
    loading. Nothing was typed on this page: the status names the link."""
    with PageServer() as server:
        server.meeting.fail_with = RuntimeError(PASSCODE_ASKED)                # a provider that joined, was refused, and keeps the link
        first = open_page(browser, server)
        first.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/5590000559"}', headers=JSON)
        first.wait_for_function("document.body.dataset.state === 'error'", timeout=8000)
        first.close()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/?keyboard=1", wait_until="networkidle")
        page.wait_for_selector("#keyboard:not([hidden])", timeout=5000)
        assert page.evaluate("document.activeElement.id") == "passcode-input"
        assert page.input_value("#link-input") == ZOOM_LINK
        box, keyboard = page.locator("#passcode-input").bounding_box(), page.locator("#keyboard").bounding_box()
        assert box["y"] >= 0 and box["y"] + box["height"] <= keyboard["y"], "the passcode field is under the keys"
        server.meeting.fail_with = None
        tap(page, *"940104", "join")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert server.meeting.joins[-1] == (ZOOM_LINK, "Lab", "940104")
        page.close()


def land_the_card_on_the_kiosk(browser, server):
    """The kiosk page after a join Zoom refused over the passcode: the card is up, the passcode field has the focus, and the
    keys are up with the page scrolled for them. This is the page the person taps on next."""
    server.meeting.fail_before_joining = RuntimeError(PASSCODE_ASKED)
    page = open_kiosk_page(browser, server)
    page.click("#link-input")
    tap(page, "layer:symbols", *"5590000559", "join")
    page.wait_for_function("document.body.dataset.state === 'error'", timeout=5000)
    page.wait_for_selector("#keyboard:not([hidden])", timeout=5000)
    assert page.evaluate("document.activeElement.id") == "passcode-input"
    assert page.evaluate("window.scrollY") > 0, "the page is not scrolled for the keys: a tap below would prove nothing"
    return page


@pytest.mark.parametrize("button", ["#actions button:has-text('Join again')", "#link-form button[type=submit]"])
def test_on_the_kiosk_one_tap_on_a_join_button_joins_with_the_keys_up(browser, button):
    """A press that takes the focus off the field hides the keys: the page shrinks, the button moves from under the finger
    before it lifts, and there is no click. The buttons leave the focus where it is, so the keys stay put for the tap."""
    with PageServer() as server:
        page = land_the_card_on_the_kiosk(browser, server)
        server.meeting.fail_before_joining = None
        tap(page, *"940104")
        page.click(button)                                                      # one click
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert server.meeting.joins[-1] == (ZOOM_LINK, "Lab", "940104")
        page.close()


def test_on_the_kiosk_one_tap_on_dismiss_gives_the_card_up_with_the_keys_up(browser):
    with PageServer() as server:
        page = land_the_card_on_the_kiosk(browser, server)
        page.click("#actions button:has-text('Dismiss')")                      # one click
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        page.close()


def test_on_the_kiosk_join_again_with_an_empty_field_leaves_the_keys_up_or_brings_them_back_for_the_code(browser):
    with PageServer() as server:
        page = land_the_card_on_the_kiosk(browser, server)
        count_join_requests(page)
        page.click("#actions button:has-text('Join again')")                    # no code typed yet, and the keys are up
        assert page.locator("#keyboard").is_visible() and page.evaluate("document.activeElement.id") == "passcode-input"
        tap(page, "hide")                                                       # the field keeps the focus, the keys are gone
        assert page.locator("#keyboard").is_hidden()
        page.click("#actions button:has-text('Join again')")                    # still no code
        assert page.locator("#keyboard").is_visible() and page.evaluate("document.activeElement.id") == "passcode-input"
        assert page.evaluate("window.__joinRequests") == 0
        server.meeting.fail_before_joining = None
        tap(page, *"940104")
        page.click("#actions button:has-text('Join again')")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert server.meeting.joins[-1] == (ZOOM_LINK, "Lab", "940104")
        page.close()


def test_the_join_buttons_work_where_the_form_has_no_requestsubmit(browser):
    """Old iOS Safari has no HTMLFormElement.requestSubmit: the keys' Join and the card's Join again send the submit event themselves."""
    with PageServer() as server:
        server.meeting.fail_before_joining = RuntimeError(PASSCODE_ASKED)
        page = open_kiosk_page(browser, server)
        page.evaluate("delete HTMLFormElement.prototype.requestSubmit")
        assert page.evaluate("typeof document.getElementById('link-form').requestSubmit") == "undefined"
        page.click("#link-input")
        tap(page, "layer:symbols", *"5590000559", "join")                       # the keys' Join
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=5000)
        server.meeting.fail_before_joining = None
        tap(page, *"940104")
        page.click("#actions button:has-text('Join again')")                    # the card's
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert server.meeting.joins == [(ZOOM_LINK, "Lab", None), (ZOOM_LINK, "Lab", "940104")]
        page.close()


def test_the_passcode_field_gets_letters_without_capitals_or_corrections(browser):
    """A passcode may hold letters and @ - _ *, so no number pad; it is case-sensitive, so no capitals or corrections from a
    phone's keyboard. The kiosk's own keys still open on the digits."""
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        field = page.locator("#passcode-input")
        assert field.get_attribute("inputmode") is None
        assert field.get_attribute("autocapitalize") == "off" and field.get_attribute("autocorrect") == "off"
        assert field.get_attribute("spellcheck") == "false" and field.get_attribute("type") == "text"
        page.click("#link-input")
        tap(page, "layer:symbols", *"5590000559")
        page.click("#passcode-input")
        assert page.locator("#keyboard button[data-key='9']").is_visible()
        page.close()


def test_the_idle_screen_fits_the_kiosk_with_the_passcode_field_and_the_retry_card(browser):
    skip_if_day_ends_within(1)                                                 # the booking starts in 8 minutes, and the service lists today's only
    title = "Quarterly planning review with the regional leadership team and the product owners of every office"
    with PageServer(calendar_events=[event("e-long", title, 8, url=ZOOM_LINK)]) as server:
        server.meeting.fail_with = RuntimeError(PASSCODE_ASKED)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)   # the tallest card: a long title, Join now
        page.fill("#link-input", "5590000559")
        assert page.locator("#passcode-input").is_visible()
        assert fits(page), "the idle screen scrolls with the passcode field under a long booking's card"
        page.click("#actions button:has-text('Join now')")                      # which Zoom refuses over the passcode: two buttons on the card
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=8000)
        assert card_buttons(page) == ["Dismiss", "Join again"]
        assert fits(page), "the idle screen scrolls with the retry card and the passcode field"
        form = page.locator("#link-form").bounding_box()
        assert form["y"] >= 0 and form["y"] + form["height"] <= 800
        page.close()


def test_the_passcode_field_stays_above_the_keys_on_the_kiosk(browser):
    skip_if_day_ends_within(1)                                                 # the booking starts in 5 minutes, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Design review with a title long enough for two lines", 5)]) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/?keyboard=1", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        page.click("#link-input")
        tap(page, "layer:symbols", *"5590000559")
        page.click("#passcode-input")
        box, keyboard = page.locator("#passcode-input").bounding_box(), page.locator("#keyboard").bounding_box()
        assert box["y"] >= 0 and box["y"] + box["height"] <= keyboard["y"]
        link = page.locator("#link-input").bounding_box()                       # and the link box above it is on the screen too
        assert link["y"] >= 0 and link["y"] + link["height"] <= keyboard["y"]
        tap(page, "hide")
        assert fits(page) and page.evaluate("window.scrollY") == 0
        page.close()


@pytest.mark.parametrize("width, height", [(960, 600), (390, 844)])
def test_the_passcode_field_stays_inside_a_screen_that_scrolls(browser, width, height):
    """Below 1024 by 700 the page scrolls down instead of fitting; across, nothing may run past the edge."""
    with PageServer() as server:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        page.fill("#link-input", "5590000559")
        assert page.locator("#passcode-input").is_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        for selector in ("#link-form", "#link-input", "#passcode-input", "#keyboard-toggle", "#link-form button[type=submit]"):
            box = page.locator(selector).bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= width, f"{selector} runs past the edge of a {width} px screen"
        page.close()


# --- the Sound and Camera panels (spec 2026-10-08 sound and camera, section 4.6) ---

from tests.unit.control.test_service import StubCamera, StubVolume  # noqa: E402


def press(page, selector):
    """Mouse down on the middle of the element, scrolled into view first: a mouse outside the viewport reaches nothing."""
    page.locator(selector).scroll_into_view_if_needed()
    box = page.locator(selector).bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    return box


def hold(page, selector, ms=120):
    press(page, selector)
    page.wait_for_timeout(ms)
    page.mouse.up()


def page_on_a_stopped_clock(browser, server, width=1280, height=800, kiosk=False, touchscreen=False):
    """The room page, loaded, with its timers stopped: from here they run only when the test calls page.clock.run_for.
    kiosk opens it as the table Pi's browser does (?keyboard=1); touchscreen gives it a touch device."""
    page = browser.new_page(viewport={"width": width, "height": height}, has_touch=touchscreen)
    page.clock.install()
    page.goto(f"http://127.0.0.1:{server.port}/" + ("?keyboard=1" if kiosk else ""), wait_until="networkidle")
    page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
    page.clock.pause_at(datetime.now() + timedelta(seconds=2))
    return page


def test_sound_panel_shows_the_speaker_and_changes_the_level(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        assert "MeetUp" in page.locator("#sound-device").inner_text()
        assert page.locator("#volume-level").inner_text() == "40"
        page.click("#louder")
        page.wait_for_function("document.getElementById('volume-level').innerText === '45'", timeout=5000)
        page.click("#quieter")
        page.wait_for_function("document.getElementById('volume-level').innerText === '40'", timeout=5000)
        page.locator("#volume-slider").evaluate("el => { el.value = 70; el.dispatchEvent(new Event('change', {bubbles: true})); }")
        page.wait_for_function("document.getElementById('volume-level').innerText === '70'", timeout=5000)
        page.click("#speaker-mute")
        page.wait_for_function("document.getElementById('speaker-mute').getAttribute('aria-pressed') === 'true'", timeout=5000)
        assert server.devices.volume.calls == [("level", 45), ("level", 40), ("level", 70), ("muted", True)]
        page.close()


def test_holding_an_arrow_sends_start_and_stop(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        hold(page, "#arrow-pad button[data-pan='1'][data-tilt='0']", ms=900)
        page.wait_for_timeout(300)
        moves = server.devices.camera.moves
        assert moves[0] == (1, 0) and moves[-1] == (0, 0)
        assert moves.count((1, 0)) >= 2            # re-sent while held
        page.close()


def test_zoom_home_and_presets(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        page.click("#zoom-in")
        page.wait_for_function("document.getElementById('camera-status').innerText.includes('125')", timeout=5000)
        page.click("#camera-home")
        page.click("#preset-row button[data-slot='2']")
        page.click("#preset-save-mode")
        page.click("#preset-row button[data-slot='3']")
        page.wait_for_function("document.querySelector(\"#preset-row button[data-slot='3']\").classList.contains('saved')", timeout=5000)
        camera = server.devices.camera
        assert camera.zooms == [125] and camera.homes == 1 and camera.recalls == [2] and camera.saves == [3]
        assert page.locator("#preset-save-mode").get_attribute("aria-pressed") == "false"   # one save, then back
        page.close()


def test_the_setup_flow_until_a_home_is_saved(browser):
    with PageServer(devices=StubDevices(camera=StubCamera(home_saved=False, position_known=False, saved=()))) as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        assert page.locator("#preset-row").is_hidden() and page.locator("#camera-home").is_disabled()
        assert page.locator("#save-home").is_disabled()
        page.click("#find-stops")
        page.wait_for_function("!document.getElementById('save-home').disabled", timeout=5000)
        page.click("#save-home")
        page.wait_for_function("!document.getElementById('preset-row').hidden", timeout=5000)
        assert server.devices.camera.setups == ["find_stops", "save_home"]
        assert page.locator("#camera-setup").is_hidden()
        page.click("#setup-link")
        assert page.locator("#camera-setup").is_visible()
        page.close()


def test_panels_hide_with_a_note_when_the_devices_are_missing(browser):
    with PageServer(devices=StubDevices(volume=StubVolume(available=False), camera=StubCamera(available=False))) as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        assert page.locator("#camera-panel").is_hidden() and page.locator("#camera-note").inner_text() == "No controllable camera found"
        page.click("#back-from-camera")
        page.click("#back-from-options")
        join_by_id(page)
        assert page.locator("#sound-panel").is_hidden() and page.locator("#sound-note").inner_text() == "No speaker found"
        page.close()


def test_opening_the_camera_panel_while_idle_asks_for_the_preview(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        page.wait_for_timeout(300)
        assert server.devices.camera.previews == [True]
        page.click("#back-from-camera")
        page.wait_for_timeout(300)
        assert server.devices.camera.previews == [True, False]
        page.close()


def test_the_panels_fit_a_phone_width(browser):
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        open_camera_screen(page)
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.close()


def test_options_leads_to_camera_framing_and_screensaver_and_back(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        assert page.evaluate("document.body.dataset.view") == "idle"
        assert page.locator("#open-options").is_visible()
        page.click("#open-options")
        assert page.evaluate("document.body.dataset.view") == "options"
        assert page.locator("#link-form").is_hidden()                       # the idle screen is gone
        page.click("#open-camera-setup")
        assert page.evaluate("document.body.dataset.view") == "camera"
        assert page.locator("#arrow-pad").is_visible()
        page.click("#back-from-camera")
        assert page.evaluate("document.body.dataset.view") == "options"
        page.click("#open-screensaver")
        assert page.evaluate("document.body.dataset.view") == "screensaver"
        assert page.locator("#screen-picker button").count() == 4
        page.click("#back-from-screensaver")
        page.click("#back-from-options")
        assert page.evaluate("document.body.dataset.view") == "idle" and page.locator("#link-form").is_visible()
        page.close()


def test_the_page_switches_to_the_meeting_screen_and_back_on_its_own(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.click("#open-options")                                          # on a sub-screen when the meeting starts
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}',
                          headers={"Content-Type": "application/json"})      # a join from another screen, or the calendar
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=8000)
        assert page.locator("#meeting-actions button:has-text('Mute')").is_visible()
        assert page.locator("#open-options").is_hidden()
        page.click("#open-camera-live")
        assert page.evaluate("document.body.dataset.view") == "camera"
        page.click("#back-from-camera")
        assert page.evaluate("document.body.dataset.view") == "meeting"      # Back from the camera returns to the mode's screen
        page.click("#meeting-actions button:has-text('Leave')")
        page.wait_for_timeout(600)                                            # a tap within half a second of the ask is a bounce, not the second tap
        page.click("#meeting-actions button:has-text('Tap again to leave')")
        page.wait_for_function("document.body.dataset.view === 'idle'", timeout=5000)
        assert page.locator("#link-form").is_visible() and page.locator("#open-options").is_visible()
        page.close()


def test_a_short_day_keeps_each_booking_at_its_own_height(browser):
    with PageServer(calendar_events=[event("e1", "Design review", 25)]) as server:
        page = open_page(browser, server)
        page.wait_for_function("document.querySelectorAll('#events .event').length === 1", timeout=5000)
        row, box = page.locator("#events .event").bounding_box(), page.locator("#events").bounding_box()
        assert row["height"] < 120 < box["height"]                           # a row, not the whole bookings box
        page.close()


def test_a_booking_in_progress_keeps_its_row_inside_the_today_list(browser):
    """The row of the booking in progress carries the class "now", which once also placed the status card in the idle
    grid: the row left its list. It must stay in the list's one column with the other rows, and the card in the grid's
    first cell, beside the bookings and above the link box."""
    skip_if_day_ends_within(1)                                               # the others start in 20 and 35 minutes, and the service lists today's only
    now = datetime.now().astimezone()
    into_the_day = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 60
    bookings = [event("b1", "Standup", -min(5, into_the_day), duration=30),  # begun a few minutes ago, never before today's midnight; has a link
                event("b2", "Design review", 20), event("b3", "Retro", 35)]
    with PageServer(calendar_events=bookings) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon' && document.querySelectorAll('#events .event').length === 3", timeout=5000)
        assert page.locator("#kicker").inner_text().upper() == "HAPPENING NOW"
        assert page.locator("#events .event.now").count() == 1 and "Standup" in page.locator("#events .event.now").inner_text()
        box = page.locator("#events").bounding_box()
        rows = [page.locator("#events .event").nth(i).bounding_box() for i in range(3)]
        assert rows[0]["y"] < rows[1]["y"] < rows[2]["y"], rows              # in the order of the day: the one in progress stays first
        for row in rows:                                                     # every row, the one in progress too, is inside the list, in the same column
            assert box["x"] - 0.5 <= row["x"] and row["x"] + row["width"] <= box["x"] + box["width"] + 0.5, rows
            assert box["y"] - 0.5 <= row["y"] and row["y"] + row["height"] <= box["y"] + box["height"] + 0.5, rows
            assert abs(row["x"] - rows[0]["x"]) < 1 and abs(row["width"] - rows[0]["width"]) < 1, rows
        view = page.locator(".view[data-view='idle']").bounding_box()
        card, link, schedule = (page.locator(selector).bounding_box() for selector in (".status-card", ".link", ".schedule"))
        assert abs(card["x"] - view["x"]) < 1 and abs(card["y"] - view["y"]) < 1       # still the first cell of the idle grid
        assert card["x"] + card["width"] <= schedule["x"] and card["y"] + card["height"] <= link["y"] + 1   # beside the bookings, above the link box
        page.close()


def test_offline_says_so_on_every_screen_and_clears_when_the_room_answers(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        note = page.locator("#offline-note")
        assert note.is_hidden()
        page.route("**/api/status", lambda route: route.abort())
        page.wait_for_function("document.body.dataset.state === 'offline'", timeout=8000)
        assert page.evaluate("document.body.dataset.view") == "meeting"      # offline keeps the screen
        assert note.is_visible() and note.inner_text() == "Can't reach the room"
        page.click("#open-camera-live")
        assert page.evaluate("document.body.dataset.view") == "camera" and note.is_visible()
        page.click("#back-from-camera")
        assert page.evaluate("document.body.dataset.view") == "meeting" and note.is_visible()
        page.unroute("**/api/status")
        page.wait_for_function("document.getElementById('offline-note').hidden", timeout=8000)
        assert page.locator("#meeting-actions button:has-text('Mute')").is_visible()   # and the controls are back
        page.close()


def test_going_offline_on_the_camera_screen_keeps_back_working_and_releases_the_preview(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        asked = preview_requests(page)
        open_camera_screen(page)
        page.wait_for_timeout(300)
        assert asked == [True]
        page.route("**/api/status", lambda route: route.abort())
        page.wait_for_function("document.body.dataset.state === 'offline'", timeout=8000)
        assert page.locator("#offline-note").is_visible()                      # the top bar says so on the camera screen too
        assert page.locator("#back-from-camera").is_enabled()
        page.click("#back-from-camera")
        assert page.evaluate("document.body.dataset.view") == "options"
        page.wait_for_timeout(300)
        assert asked == [True, False]                                          # the off call went out, even if it could not be answered
        page.click("#back-from-options")
        assert page.evaluate("document.body.dataset.view") == "idle"
        page.close()


def test_a_long_title_and_a_refusal_leave_the_link_box_on_the_kiosk_screen(browser):
    title = "Quarterly planning review with the regional leadership team and the product owners of every office"
    skip_if_day_ends_within(1)                                               # the booking starts in 5 minutes, and the service lists today's only
    with PageServer(calendar_events=[event("e1", title, 5)]) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)   # Starting soon, with Join now
        page.fill("#link-input", "not a meeting link")
        page.click("#link-form button[type=submit]")
        page.wait_for_function("document.getElementById('message').innerText === 'Unsupported meeting link'", timeout=5000)
        assert fits(page)
        form = page.locator("#link-form").bounding_box()
        assert form["y"] >= 0 and form["y"] + form["height"] <= 800
        page.close()


def test_a_screen_that_cannot_fit_scrolls_under_the_top_bar_instead_of_being_cut_off(browser):
    """A last resort that real text never needs at 1280 by 800: an endless refusal on the idle card."""
    with PageServer() as server:
        page = open_page(browser, server)
        page.evaluate("(t) => { window.croomPage.model.error = t; window.croomPage.render(); }", "A refusal that will not end. " * 50)
        assert not fits(page)                                                # the screen is too long for the kiosk
        assert page.evaluate("document.documentElement.scrollHeight <= document.documentElement.clientHeight")   # the page stays put
        page.locator("#link-form").scroll_into_view_if_needed()
        form = page.locator("#link-form").bounding_box()
        assert form["y"] >= 0 and form["y"] + form["height"] <= 800          # and the link box can still be reached
        page.close()


def test_every_idle_screen_fits_the_kiosk_without_scrolling(browser):
    skip_if_day_ends_within(1.5)                                             # the service lists today's bookings only
    bookings = [event(f"e{i}", f"Booking {i}", 20 + 5 * i) for i in range(8)]
    with PageServer(calendar_events=bookings) as server:
        page = open_page(browser, server)
        page.wait_for_function("document.querySelectorAll('#events .event').length === 8", timeout=5000)
        assert fits(page), "the idle screen scrolls with eight bookings"
        assert page.evaluate("const b = document.querySelector('#events'); b.scrollHeight > b.clientHeight")   # the box scrolls instead
        assert page.locator("#link-form").is_visible()
        page.click("#open-options")
        assert fits(page), "the options screen scrolls"
        page.click("#open-camera-setup")
        assert page.locator("#setup-link").is_visible()                      # a home is saved: Set up is a link
        page.click("#setup-link")
        assert page.locator("#camera-setup").is_visible()
        assert fits(page), "the camera screen scrolls with Set up open"
        page.click("#back-from-camera")
        page.click("#open-screensaver")
        assert fits(page), "the screensaver screen scrolls"
        page.click("#back-from-screensaver")
        page.click("#open-door-sign")
        assert page.locator("#scores-picker").is_visible()
        assert fits(page), "the door sign screen scrolls"
        page.close()


def test_every_meeting_screen_fits_the_kiosk_without_scrolling(browser):
    skip_if_day_ends_within(1.5)                                             # the service lists today's bookings only; the last starts in 55 minutes
    title = "Quarterly planning review with the regional leadership team and the product owners of every office"
    assert len(title) == 98                                                  # as long as a booking's title gets
    bookings = [event("e-long", title, 8)] + [event(f"e{i}", f"Booking {i}", 20 + 5 * i) for i in range(8)]
    with PageServer(calendar_events=bookings) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)   # the long-titled booking is inside its ten minutes
        page.click("#actions button:has-text('Join now')")                   # this booking, so the call carries its title
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert page.locator("#meeting-detail").inner_text() == f"{title}, Zoom"
        assert fits(page), "the meeting screen scrolls with a 98-character title"
        for state, words in ((MeetingState.JOINING, "Joining"), (MeetingState.IN_LOBBY, "In the lobby")):   # the first line is longest while the call joins
            server.meeting.set_state(state)
            page.wait_for_function(f"document.getElementById('meeting-state').innerText === '{words}'", timeout=5000)
            assert fits(page), f"the meeting screen scrolls while {words.lower()} with a 98-character title"
        server.meeting.set_state(MeetingState.CONNECTED)
        page.wait_for_function("document.getElementById('meeting-state').innerText === 'In a meeting'", timeout=5000)
        page.click("#open-camera-live")
        assert fits(page), "the meeting's camera screen scrolls"
        page.click("#back-from-camera")
        page.click("#open-upcoming")
        page.wait_for_function("document.querySelectorAll('#upcoming-list .event').length === 8", timeout=5000)
        assert fits(page), "the upcoming screen scrolls"
        assert page.evaluate("const b = document.querySelector('#upcoming-list'); b.scrollHeight > b.clientHeight")   # the list scrolls instead
        page.close()


def test_a_long_refusal_on_the_meeting_screen_stays_above_the_meeting_buttons(browser):
    """#meeting-message is a grid item: its height must follow its text. With an explicit minimum (the idle card's
    .message has one), the row shrinks under a long refusal in the shortest kiosk window and the text runs over the buttons."""
    refusal = "The call did not answer the request to change the microphone. Wait a moment and try again, or leave the meeting and join it one more time by the link."
    assert len(refusal) == 150
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 1280, "height": 700})   # the shortest window that still gets the fixed screen
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        join_by_id(page)
        page.route("**/api/meeting/mute", lambda route: route.fulfill(status=409, json={"error": refusal}))
        page.click("#meeting-actions button:has-text('Mute')")
        page.wait_for_function("document.getElementById('meeting-message').innerText.length === 150", timeout=5000)
        placed = page.evaluate("""() => {
            const message = document.getElementById('meeting-message'), range = document.createRange();
            range.selectNodeContents(message);
            return { text: range.getBoundingClientRect().bottom, box: message.getBoundingClientRect().bottom,
                     holds: message.scrollHeight <= message.clientHeight, actions: document.getElementById('meeting-actions').getBoundingClientRect().top };
        }""")
        assert placed["box"] <= placed["actions"], "the message's box runs into the buttons"
        assert placed["holds"] and placed["text"] <= placed["actions"], "the refusal's text runs over the buttons"
        page.close()


# --- what the panels do when a device refuses, is busy, is not there, or answers in a way the poll must not undo ---

class UnwritableCamera(StubCamera):
    """The settings file cannot be written: saving a preset or the home answers 500 with the reason."""

    reason = "disk full"

    async def save(self, slot):
        raise OSError(self.reason)

    async def save_home(self):
        raise OSError(self.reason)


class InterruptedCamera(StubCamera):
    """A newer command always cuts Home short."""

    async def home(self):
        raise Interrupted("the camera move was interrupted")


class SlowHomeCamera(StubCamera):
    """Home takes a moment, busy while it runs, as on the Pi."""

    async def home(self):
        self.busy = True
        try:
            await asyncio.sleep(1.5)
        finally:
            self.busy = False
        return await super().home()


class BusySpeaker(StubVolume):
    async def set_level(self, level):
        raise NotReady("the speaker is busy")


class ZoomingOnRenewalCamera(StubCamera):
    """The second request for the preview (the first renewal) leaves the zoom at 300: a change only that answer carries."""

    asked = 0

    def set_preview(self, on):
        if on:
            self.asked += 1
            if self.asked == 2:
                self.zoom_level = 300
        return super().set_preview(on)


class PreviewRefusingCamera(StubCamera):
    """Asking for the preview is refused once `refusing` is set, as when a meeting has just started."""

    refusing = False

    def set_preview(self, on):
        if on and self.refusing:
            raise NotReady("the TV is in a meeting")
        return super().set_preview(on)


def test_a_refused_camera_command_shows_its_reason_and_the_next_one_clears_it(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        page.click("#preset-row button[data-slot='3']")                       # nothing is saved there
        page.wait_for_function("document.getElementById('camera-note').innerText === 'nothing saved in this slot'", timeout=5000)
        assert "saved" not in page.locator("#preset-row button[data-slot='3']").get_attribute("class").split()
        page.click("#zoom-in")                                                # the next command that works clears the note
        page.wait_for_function("document.getElementById('camera-note').innerText === ''", timeout=5000)
        assert server.devices.camera.zooms == [125] and server.devices.camera.recalls == []
        page.close()


def test_a_failed_preset_save_is_not_marked_saved(browser):
    with PageServer(devices=StubDevices(camera=UnwritableCamera())) as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        page.click("#preset-save-mode")
        page.click("#preset-row button[data-slot='3']")
        page.wait_for_function("document.getElementById('camera-note').innerText.includes('disk full')", timeout=5000)
        assert page.locator("#camera-note").inner_text() == "could not save the camera settings: disk full"
        assert "saved" not in page.locator("#preset-row button[data-slot='3']").get_attribute("class").split()
        assert page.locator("#preset-save-mode").get_attribute("aria-pressed") == "false"   # one tap, one attempt
        page.close()


def test_a_long_refusal_does_not_overflow_a_phone(browser):
    camera = UnwritableCamera()
    camera.reason = "[Errno 36] File name too long: '" + "control_settings_" * 6 + "tmp'"    # a hundred characters with no place to break
    with PageServer(devices=StubDevices(camera=camera)) as server:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        open_camera_screen(page)
        page.click("#preset-save-mode")
        page.click("#preset-row button[data-slot='3']")
        page.wait_for_function("document.getElementById('camera-note').innerText.includes('File name too long')", timeout=5000)
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.close()


def test_a_refusal_comes_into_view_beside_the_controls_that_caused_it(browser):
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 1024, "height": 600})      # smaller than the kiosk's 1280 by 800
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        open_camera_screen(page)
        page.click("#preset-row button[data-slot='3']")                       # nothing is saved there
        page.wait_for_function("document.getElementById('camera-note').innerText !== ''", timeout=5000)
        box = page.locator("#camera-note").bounding_box()
        assert box["y"] >= 0 and box["y"] + box["height"] <= 600             # the reason is in view on the camera screen
        preset = page.locator("#preset-row button[data-slot='3']").bounding_box()
        assert preset["y"] + preset["height"] <= 600                          # and so is the button that caused it
        page.close()


def test_a_refusal_comes_into_view_for_a_phone_scrolled_down_to_the_controls(browser):
    """The note sits above the controls. No screen starts scrolled any more, so nothing else pins the scroll in
    devices.js: a phone that has scrolled down to the presets must be shown why its tap did nothing. A phone 560 px
    tall, because at 844 the whole camera screen is all but in view and the note never leaves it."""
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 390, "height": 560})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        open_camera_screen(page)
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        assert page.evaluate("window.scrollY") > 0
        assert page.locator(".camera-section .kicker").bounding_box()["y"] < 0   # the top of the screen, where the note will be, is out of sight
        page.click("#preset-row button[data-slot='3']")                       # nothing is saved there
        page.wait_for_function("document.getElementById('camera-note').innerText === 'nothing saved in this slot'", timeout=5000)
        box = page.locator("#camera-note").bounding_box()
        assert box["y"] >= -1 and box["y"] + box["height"] <= 560             # and the page has scrolled back up to it (a pixel of rounding)
        page.close()


def test_a_failed_save_as_home_keeps_set_up_open(browser):
    with PageServer(devices=StubDevices(camera=UnwritableCamera())) as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        page.click("#setup-link")
        page.click("#save-home")
        page.wait_for_function("document.getElementById('camera-note').innerText.includes('disk full')", timeout=5000)
        assert page.locator("#camera-setup").is_visible()          # closed only by a home that really was saved
        page.close()


def test_a_refused_volume_change_shows_its_reason_and_the_slider_goes_back(browser):
    with PageServer(devices=StubDevices(volume=BusySpeaker())) as server:
        page = open_page(browser, server)
        join_by_id(page)
        for level in (70, 80):                                     # the second refusal says the same words: the slider must still go back
            with page.expect_response("**/api/audio/volume"):
                page.locator("#volume-slider").evaluate(f"el => {{ el.value = {level}; el.dispatchEvent(new Event('change', {{bubbles: true}})); }}")
            page.wait_for_function("document.getElementById('volume-slider').value === '40'", timeout=5000)
            assert page.locator("#sound-note").inner_text() == "the speaker is busy"
            assert page.locator("#volume-level").inner_text() == "40"
        page.close()


def test_a_long_move_cut_short_by_a_newer_tap_says_nothing(browser):
    with PageServer(devices=StubDevices(camera=InterruptedCamera())) as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        with page.expect_response("**/api/camera/home") as answered:
            page.click("#camera-home")
        assert answered.value.status == 409
        page.wait_for_timeout(300)
        assert page.locator("#camera-note").inner_text() == ""
        page.close()


def test_a_busy_camera_says_moving_and_locks_the_arrows(browser):
    camera = SlowHomeCamera()
    with PageServer(devices=StubDevices(camera=camera)) as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        page.click("#preset-row button[data-slot='3']")                       # refused: nothing is saved there
        page.wait_for_function("document.getElementById('camera-note').innerText !== ''", timeout=5000)
        page.click("#camera-home")
        # a long move clears the old note as it starts, and the status is asked for the camera's busy flag at once,
        # not left to the two-second poll
        page.wait_for_function("document.getElementById('camera-note').innerText === ''", timeout=1000)
        page.wait_for_function("document.getElementById('camera-status').innerText === 'Moving\u2026'", timeout=1000)
        assert page.locator("#arrow-pad button[data-pan='1']").is_disabled()
        page.wait_for_function("document.getElementById('camera-status').innerText.startsWith('Zoom')", timeout=5000)
        assert page.locator("#arrow-pad button[data-pan='1']").is_enabled()
        assert camera.homes == 1
        page.close()


def test_the_answer_updates_the_page_without_waiting_for_the_poll(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)                                           # Sound is on the meeting screen
        snapshot = page.evaluate("fetch('/api/status').then((r) => r.text())")
        page.route("**/api/status", lambda route: route.fulfill(body=snapshot, content_type="application/json"))   # polls can no longer help
        page.click("#louder")
        page.wait_for_function("document.getElementById('volume-level').innerText === '45'", timeout=1000)
        page.close()


def test_a_poll_already_on_its_way_does_not_undo_the_answer(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)                                           # Sound is on the meeting screen
        page.evaluate("""() => {
            const real = window.fetch;
            window.fetch = (url, init) => String(url).endsWith('/api/status')
                ? real(url, init).then((r) => new Promise((done) => setTimeout(() => done(r), 1200)))   // the poll's answer arrives late
                : real(url, init);
            window.__levels = [];
            new MutationObserver(() => window.__levels.push(document.getElementById('volume-level').textContent))
                .observe(document.getElementById('volume-level'), {childList: true, characterData: true, subtree: true});
        }""")
        with page.expect_request("**/api/status"):
            pass                                                   # a poll leaves now, with the level as it is: 40
        page.click("#louder")                                      # and the answer to this click (45) comes back before it
        page.wait_for_timeout(1800)                                # past the late poll's arrival
        assert page.evaluate("window.__levels") == ["45"]
        page.close()


def test_the_panel_buttons_are_not_rebuilt_while_nothing_changes(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        page.evaluate("window.__keep = [document.querySelector('#preset-row button'), document.getElementById('zoom-in'), document.getElementById('louder')]")
        page.wait_for_timeout(2600)                                # a whole poll and more
        assert page.evaluate("""document.querySelector('#preset-row button') === window.__keep[0]
            && document.getElementById('zoom-in') === window.__keep[1] && document.getElementById('louder') === window.__keep[2]""")
        page.close()


def test_holding_a_zoom_button_repeats_until_it_is_released(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        hold(page, "#zoom-in", ms=1000)
        page.wait_for_timeout(300)
        zooms = server.devices.camera.zooms
        assert zooms[:3] == [125, 150, 175] and len(zooms) <= 4    # a step at once, then one every 400 ms
        sent = len(zooms)
        page.wait_for_timeout(900)
        assert len(server.devices.camera.zooms) == sent            # nothing after the release
        page.close()


def test_sliding_off_an_arrow_stops_the_camera_like_lifting_the_finger(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        camera = server.devices.camera
        arrow = "#arrow-pad button[data-pan='1'][data-tilt='0']"
        page.evaluate("(sel) => { window.__lost = 0; document.querySelector(sel).addEventListener('lostpointercapture', () => window.__lost++); }", arrow)
        box = press(page, arrow)
        middle = box["y"] + box["height"] / 2
        page.mouse.move(box["x"] + 4, box["y"] + 4)                         # a nudge that stays on the button is still a hold
        page.mouse.move(box["x"] + box["width"] + 8, middle)               # 8 px past its edge is a finger's jitter: still a hold
        page.wait_for_timeout(100)
        assert camera.moves == [(1, 0)] and page.evaluate("window.__lost") == 0
        page.mouse.move(box["x"] + box["width"] + 40, middle, steps=5)     # 40 px off it, well past the margin, still down
        page.wait_for_timeout(300)
        assert camera.moves == [(1, 0), (0, 0)]                             # the stop went out at once
        assert page.evaluate("window.__lost") == 1                          # and the pointer was let go with it, before any lift
        page.wait_for_timeout(900)                                          # longer than one re-send interval, still held
        assert camera.moves == [(1, 0), (0, 0)]                             # nothing was re-sent
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)               # back on the button, still down
        page.wait_for_timeout(900)
        page.mouse.up()
        page.wait_for_timeout(300)
        assert camera.moves == [(1, 0), (0, 0)]                             # no new press, and lifting sends no second stop
        hold(page, arrow, ms=300)                                           # a plain press and release works as before
        page.wait_for_timeout(300)
        assert camera.moves == [(1, 0), (0, 0), (1, 0), (0, 0)]
        page.close()


def test_sliding_off_a_zoom_button_ends_its_repeat(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        camera = server.devices.camera
        box = press(page, "#zoom-in")
        page.wait_for_timeout(100)
        assert camera.zooms == [125]
        page.mouse.move(box["x"] - 40, box["y"] + box["height"] / 2, steps=5)                   # 40 px off to the side, still down
        page.wait_for_timeout(900)                                          # two repeat intervals would have gone by
        assert camera.zooms == [125]
        page.mouse.up()
        page.wait_for_timeout(300)
        assert camera.zooms == [125]
        hold(page, "#zoom-in", ms=300)                                      # a plain press and release still steps once
        page.wait_for_timeout(300)
        assert camera.zooms == [125, 150]
        page.close()


def test_an_arrow_held_while_the_camera_goes_busy_stops_repeating_and_a_late_lift_sends_no_stop(browser):
    with PageServer() as server:
        camera = server.devices.camera
        page = open_page(browser, server)
        open_camera_screen(page)
        press(page, "#arrow-pad button[data-pan='1'][data-tilt='0']")
        page.wait_for_timeout(100)
        camera.busy = True                                         # a Home or a preset from another screen
        page.wait_for_function("document.querySelector(\"#arrow-pad button[data-pan='1']\").disabled", timeout=5000)
        page.wait_for_timeout(900)                                 # the repeat that was due notices and gives up
        sent = len(camera.moves)
        page.wait_for_timeout(1600)
        page.mouse.up()
        page.wait_for_timeout(300)
        assert len(camera.moves) == sent and set(camera.moves) == {(1, 0)}    # no more starts, and no stop to cut that move short
        page.close()


def test_lifting_an_arrow_right_after_the_camera_went_busy_sends_no_stop(browser):
    with PageServer() as server:
        camera = server.devices.camera
        page = page_on_a_stopped_clock(browser, server)
        open_camera_screen(page)
        press(page, "#arrow-pad button[data-pan='1'][data-tilt='0']")
        page.wait_for_timeout(100)
        camera.busy = True                                         # a Home or a preset from another screen
        page.clock.run_for(2000)                                   # the poll sees it and the pad goes disabled; the repeat's next tick is 250 ms of page time away
        page.wait_for_function("document.querySelector(\"#arrow-pad button[data-pan='1']\").disabled", timeout=5000)
        page.mouse.up()                                            # the lift beats that tick, and must not cut the long move short
        page.wait_for_timeout(300)
        assert (1, 0) in camera.moves and (0, 0) not in camera.moves
        page.close()


def test_the_panels_degrade_without_a_devices_service(browser):
    with PageServer(devices=SimpleNamespace()) as server:          # no volume and no camera: the status falls back to unavailable blocks
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        open_camera_screen(page)
        assert page.locator("#camera-panel").is_hidden() and page.locator("#camera-note").inner_text() == "No controllable camera found"
        page.click("#back-from-camera")
        page.click("#back-from-options")
        join_by_id(page)
        assert page.locator("#sound-panel").is_hidden() and page.locator("#sound-note").inner_text() == "No speaker found"
        assert errors == []
        page.close()


def test_the_panels_are_drawn_even_when_their_script_arrives_after_the_first_poll(browser):
    """On a slow link the first poll can be answered before devices.js has run: it is given that status as it registers."""
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        held = []
        page.route("**/static/devices.js", lambda route: held.append(route))      # the request stalls until the test lets it go
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="commit")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)   # app.js has polled and rendered
        for _ in range(50):
            if held:
                break
            page.wait_for_timeout(100)
        assert len(held) == 1
        drawn = "[!document.getElementById('sound-panel').hidden, !document.getElementById('camera-panel').hidden]"
        assert page.evaluate(drawn) == [False, False]                             # nobody was there to draw them
        held[0].continue_()
        page.wait_for_load_state("load")
        # No waiting for the next poll, two seconds away: the panels were drawn as the script registered.
        assert page.evaluate(drawn) == [True, True]
        assert page.locator("#volume-level").inner_text() == "40"
        assert errors == []
        page.close()


def test_save_and_the_set_up_link_wait_for_a_saved_home(browser):
    with PageServer(devices=StubDevices(camera=StubCamera(home_saved=False, position_known=False, saved=()))) as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        assert page.locator("#preset-save-mode").is_hidden() and page.locator("#setup-link").is_hidden()
        assert page.locator("#camera-setup").is_visible()          # Set up is what the panel offers until then
        page.close()


def preview_requests(page):
    """What the page asks of /api/camera/preview, as a list of its "on" values (the stub only records what it accepted)."""
    asked = []
    page.on("request", lambda r: asked.append(r.post_data_json["on"]) if r.url.endswith("/api/camera/preview") else None)
    return asked


def run_until_the_page_shows(page, state):
    """Run the page's fake clock two seconds at a time, until its status poll has seen the room in `state`."""
    for _ in range(5):
        page.clock.run_for(2000)
        try:
            page.wait_for_function(f"document.body.dataset.state === '{state}'", timeout=1000)
            return
        except playwright.TimeoutError:
            continue
    raise AssertionError(f"the page never showed {state}")


def test_the_preview_is_renewed_every_30_seconds_but_not_in_a_meeting(browser):
    with PageServer() as server:
        base = f"http://127.0.0.1:{server.port}"
        page = page_on_a_stopped_clock(browser, server)
        asked = preview_requests(page)
        open_camera_screen(page)
        page.wait_for_timeout(300)
        assert asked == [True]
        page.clock.run_for(30000)
        page.wait_for_timeout(300)
        assert asked == [True, True]
        page.request.post(base + "/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}',
                          headers={"Content-Type": "application/json"})
        run_until_the_page_shows(page, "meeting")
        page.clock.run_for(60000)
        page.wait_for_timeout(300)
        assert asked == [True, True, False]                        # the page left the camera screen: off, once; then
                                                                   # nothing asked for while the TV is in a meeting
        page.close()


def test_the_camera_screen_opened_in_a_meeting_asks_for_no_preview_and_closes_when_the_room_is_idle(browser):
    with PageServer() as server:
        base = f"http://127.0.0.1:{server.port}"
        page = page_on_a_stopped_clock(browser, server)
        asked = preview_requests(page)
        page.request.post(base + "/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}',
                          headers={"Content-Type": "application/json"})
        run_until_the_page_shows(page, "meeting")
        page.click("#open-camera-live")
        page.wait_for_timeout(300)
        assert asked == []                                         # the TV shows the meeting: no preview to ask for
        page.request.post(base + "/api/meeting/leave", data="{}", headers={"Content-Type": "application/json"})
        run_until_the_page_shows(page, "free")
        assert page.evaluate("document.body.dataset.view") == "idle"   # the meeting's camera screen does not carry over
        page.clock.run_for(30000)
        page.wait_for_timeout(300)
        assert asked == []                                         # and nothing renews behind it
        open_camera_screen(page)                                   # Camera framing in the idle room asks at once
        page.wait_for_timeout(300)
        assert asked == [True]
        page.close()


def test_a_preview_renewal_that_works_does_not_clear_the_users_last_refusal(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, width=1024, height=600)
        open_camera_screen(page)
        page.click("#preset-row button[data-slot='3']")                       # the user's own refusal: nothing is saved there
        page.wait_for_function("document.getElementById('camera-note').innerText === 'nothing saved in this slot'", timeout=5000)
        page.clock.run_for(30000)                                             # the renewal goes out and the camera accepts it
        page.wait_for_timeout(300)
        assert server.devices.camera.previews == [True, True]
        assert page.locator("#camera-note").inner_text() == "nothing saved in this slot"
        page.close()


def test_a_preview_renewal_updates_the_page_from_its_answer(browser):
    camera = ZoomingOnRenewalCamera()
    with PageServer(devices=StubDevices(camera=camera)) as server:
        page = page_on_a_stopped_clock(browser, server)
        open_camera_screen(page)
        page.wait_for_timeout(300)
        assert camera.previews == [True] and "Zoom 100" in page.locator("#camera-status").inner_text()
        held = []
        page.route("**/api/status", lambda route: held.append(route))         # no poll comes back from here on: the answer is all there is
        page.clock.run_for(30000)
        page.wait_for_function("document.getElementById('camera-status').innerText.includes('Zoom 300')", timeout=5000)
        for route in held:
            route.continue_()
        page.close()


def test_a_refused_preview_renewal_writes_no_note_and_does_not_scroll_the_page(browser):
    camera = PreviewRefusingCamera()
    with PageServer(devices=StubDevices(camera=camera)) as server:
        page = page_on_a_stopped_clock(browser, server, width=1024, height=600)
        open_camera_screen(page)
        page.click("#preset-row button[data-slot='3']")
        page.wait_for_function("document.getElementById('camera-note').innerText === 'nothing saved in this slot'", timeout=5000)
        # The camera screen fits without scrolling, so the note cannot be put above the fold: count scroll requests.
        page.evaluate("""() => {
            window.__scrolls = 0;
            const real = Element.prototype.scrollIntoView;
            Element.prototype.scrollIntoView = function (...args) { window.__scrolls += 1; return real.apply(this, args); };
        }""")
        scrolled_to = page.evaluate("window.scrollY")
        asked = preview_requests(page)
        camera.refusing = True                                                # a meeting has just started: the page's status is behind
        page.clock.run_for(30000)
        page.wait_for_timeout(300)
        assert asked == [True] and camera.previews == [True]                  # the renewal went out and was refused
        assert page.locator("#camera-note").inner_text() == "nothing saved in this slot"
        assert page.evaluate("window.scrollY") == scrolled_to and page.evaluate("window.__scrolls") == 0
        page.close()


def test_a_zoom_hold_does_not_hold_back_the_speakers_status(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        page.evaluate("""() => {
            const real = window.fetch;
            window.fetch = (url, init) => String(url).endsWith('/api/status')
                ? real(url, init).then((r) => new Promise((done) => setTimeout(() => done(r), 1200)))   // each poll is on its way for 1.2 s
                : real(url, init);
        }""")
        press(page, "#zoom-in")                                               # the camera answers every 400 ms from here on
        server.devices.volume.level = 55                                      # and somebody else turns the speaker up
        page.wait_for_function("document.getElementById('volume-level').innerText === '55'", timeout=6000)
        page.mouse.up()
        page.close()


PANEL_OPEN = "document.body.dataset.view === 'camera'"


# --- the idle return: a screen other than the idle screen returns to it after ten minutes without a tap (spec 2026-10-12, sections 3 and 4.2) ---

def test_an_idle_screen_returns_to_the_idle_screen_after_ten_minutes_and_ends_the_preview(browser):
    """The kiosk never reloads and nobody presses Back: without this, a room left on Camera framing keeps the TV on
    the camera for good. Each timer that falls due during a fast-forward fires once, at the end."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        asked = preview_requests(page)
        page.click("#open-options")
        page.click("#open-camera-setup")
        page.wait_for_timeout(300)
        assert asked == [True]
        page.clock.run_for(60_000)
        page.clock.fast_forward(8 * 60_000)                                   # nine minutes: still there, still renewing
        page.wait_for_timeout(300)
        assert page.evaluate("document.body.dataset.view") == "camera" and asked == [True] * 4
        page.clock.fast_forward(90_000)                                       # past ten minutes without a tap
        page.wait_for_timeout(300)
        assert page.evaluate("document.body.dataset.view") == "idle"
        assert asked == [True] * 4 + [False] and server.devices.camera.preview_on is False   # the renewal due in the same jump never went out: the return is the older timer
        page.clock.run_for(60_000)
        page.wait_for_timeout(300)
        assert asked == [True] * 4 + [False]                                  # no renewal after the return
        page.close()


def test_a_tap_keeps_an_idle_screen_another_ten_minutes(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        page.click("#open-options")
        page.clock.fast_forward(9 * 60_000)
        page.wait_for_timeout(200)
        page.click("#open-screensaver")                                       # a tap at nine minutes
        page.clock.fast_forward(9 * 60_000)
        page.wait_for_timeout(200)
        assert page.evaluate("document.body.dataset.view") == "screensaver"   # eighteen minutes in, still here
        page.clock.fast_forward(90_000)
        page.wait_for_timeout(200)
        assert page.evaluate("document.body.dataset.view") == "idle"
        page.close()


def test_a_screen_opened_with_one_tap_and_left_alone_returns_too(browser):
    """The tap that opens a screen lands while the idle screen is still up, so the wait for the new screen starts with
    the view change, not with the tap."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        page.click("#open-options")
        page.clock.fast_forward(11 * 60_000)
        page.wait_for_timeout(200)
        assert page.evaluate("document.body.dataset.view") == "idle"
        page.close()


def test_a_tap_that_goes_nowhere_keeps_an_idle_screen_another_ten_minutes_too(browser):
    """A tap on a navigation button restarts the wait through the view change. A camera nudge changes no screen:
    only the page's own pointerdown listener can see it."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        open_camera_screen(page)
        page.wait_for_timeout(300)
        page.clock.fast_forward(9 * 60_000)
        hold(page, "#arrow-pad button[data-pan='1'][data-tilt='0']", ms=100)  # a nudge at nine minutes
        page.wait_for_timeout(300)
        assert server.devices.camera.moves == [(1, 0), (0, 0)]
        page.clock.fast_forward(9 * 60_000)                                   # eighteen minutes in, nine since the nudge
        page.wait_for_timeout(300)
        assert page.evaluate("document.body.dataset.view") == "camera"
        page.clock.fast_forward(90_000)                                       # ten and a half since the nudge
        page.wait_for_timeout(300)
        assert page.evaluate("document.body.dataset.view") == "idle" and server.devices.camera.previews[-1] is False
        page.close()


def test_a_meeting_screen_never_returns_on_its_own(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        page.click("#open-upcoming")
        page.clock.fast_forward(30 * 60_000)
        page.wait_for_timeout(200)
        assert page.evaluate("document.body.dataset.view") == "upcoming"
        page.close()


def test_a_call_that_starts_under_an_open_sub_screen_leaves_no_wait_behind(browser):
    """Options is open, so the wait is running, when a call starts from elsewhere: the page lands on the meeting screen,
    and the wait that was running must not fire ten minutes into the call. A stale wait would never show the idle screen
    (the page lands on the meeting screen again at once), but it would pull a person off the Upcoming screen."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        page.click("#open-options")
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}',
                          headers={"Content-Type": "application/json"})
        run_until_the_page_shows(page, "meeting")
        assert page.evaluate("document.body.dataset.view") == "meeting"
        page.click("#open-upcoming")
        page.clock.fast_forward(11 * 60_000)
        page.wait_for_timeout(300)
        assert page.evaluate("document.body.dataset.view") == "upcoming"
        page.close()


@pytest.mark.parametrize("leaves", ["a meeting begins", "ten minutes pass"])
def test_an_automatic_close_during_an_outage_leaves_no_note(browser, leaves):
    """The page leaving the camera screen on its own is nobody's action: when the agent cannot be reached at that
    moment, the page must not show "Failed to fetch" on a screen nobody touched. A meeting beginning and the idle
    return are the two ways the page leaves on its own; both go through the same automatic path."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        open_camera_screen(page)
        page.wait_for_timeout(300)
        asked = preview_requests(page)
        page.route("**/api/camera/preview", lambda route: route.abort())
        if leaves == "a meeting begins":
            page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}',
                              headers={"Content-Type": "application/json"})
            run_until_the_page_shows(page, "meeting")
        else:
            page.clock.fast_forward(11 * 60_000)
        page.wait_for_timeout(300)
        assert page.evaluate(PANEL_OPEN) is False                             # left on its own
        assert asked[-1:] == [False] and asked.count(False) == 1              # and the one off call failed
        note = page.evaluate("() => document.querySelector('#camera-note').textContent.trim()")
        assert "fetch" not in note.lower() and "failed" not in note.lower()
        page.close()


def test_closing_the_page_with_the_camera_panel_open_turns_the_preview_off(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        page.wait_for_timeout(300)
        assert server.devices.camera.previews == [True]
        page.goto("about:blank")                                              # the page goes away with the panel still open
        for _ in range(50):
            if len(server.devices.camera.previews) == 2:
                break
            page.wait_for_timeout(100)
        assert server.devices.camera.previews == [True, False]
        page.close()


def colour(page, selector):
    return page.evaluate(f"getComputedStyle(document.querySelector({selector!r})).color")


def test_the_section_labels_and_quiet_buttons_keep_their_colour_when_the_status_card_changes(browser):
    periwinkle, white, card_blue, dark_blue = "rgb(189, 206, 255)", "rgb(255, 255, 255)", "rgb(27, 82, 229)", "rgb(0, 62, 188)"
    with PageServer(calendar_events=[event("c1", "Standup", -2)]) as server:   # happening now: the card turns light
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        assert colour(page, ".sound-section .kicker") == periwinkle and colour(page, ".camera-section .kicker") == periwinkle
        assert colour(page, "#kicker") == card_blue                          # the card's own label still follows its state
        page.close()
    with PageServer() as server:
        server.meeting.fail_before_joining = RuntimeError("Could not open the meeting")
        page = open_page(browser, server)
        page.fill("#link-input", "98765432100")
        page.click("#link-form button[type=submit]")
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=5000)
        assert colour(page, ".sound-section .kicker") == periwinkle and colour(page, ".camera-section .kicker") == periwinkle
        assert colour(page, "#louder") == white and colour(page, "#screen-picker button") == white
        assert colour(page, "#kicker") == dark_blue and colour(page, "#actions button") == dark_blue
        page.close()


# --- the meeting screen's content (spec 2026-10-12, sections 4.1 and 4.2) ---

def test_the_meeting_screen_shows_the_state_the_time_and_what_is_next(browser):
    skip_if_day_ends_within(2)                                 # the service lists today's bookings only; the last starts in 90 minutes
    bookings = [event("e1", "Design review", 8), event("e2", "Sales call", 90)]   # the first starts in 8 minutes
    with PageServer(calendar_events=bookings) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)   # the first booking is inside its ten minutes
        join_by_id(page)
        assert page.locator("#meeting-state").inner_text() == "In a meeting"
        page.wait_for_function("/^\\d+:\\d\\d$/.test(document.getElementById('meeting-elapsed').innerText)", timeout=5000)
        warning = page.locator("#next-warning")
        assert warning.is_visible() and warning.inner_text().startswith("Next: ") and "Design review" in warning.inner_text()
        assert page.locator("#open-options").is_hidden()
        page.click("#open-upcoming")
        assert page.evaluate("document.body.dataset.view") == "upcoming"
        titles = page.locator("#upcoming-list .title").all_inner_texts()
        assert titles == ["Design review", "Sales call"]
        assert page.locator("#upcoming-empty").is_hidden()
        page.click("#back-from-upcoming")
        assert page.evaluate("document.body.dataset.view") == "meeting"
        page.close()


def test_upcoming_says_so_when_nothing_else_is_booked(browser):
    with PageServer(calendar_events=[event("e0", "Earlier", -60)]) as server:   # already started: not upcoming
        page = open_page(browser, server)
        join_by_id(page)
        assert page.locator("#next-warning").is_hidden()
        page.click("#open-upcoming")
        assert page.locator("#upcoming-list .event").count() == 0 and page.locator("#upcoming-empty").is_visible()
        assert page.locator("#upcoming-empty").inner_text() == "No more meetings today"
        page.close()


def test_the_camera_during_a_meeting_recalls_and_nudges_but_never_sets_up(browser):
    with PageServer(devices=StubDevices(camera=StubCamera(home_saved=False, position_known=False))) as server:   # no home yet: Set up is on the screen
        page = open_page(browser, server)
        open_camera_screen(page)
        page.click("#find-stops")
        page.wait_for_function("!document.getElementById('save-home').disabled", timeout=5000)
        page.click("#save-home")
        page.wait_for_function("document.querySelectorAll('#preset-row button').length === 3", timeout=5000)
        page.click("#back-from-camera")
        page.click("#back-from-options")
        join_by_id(page)
        asked = preview_requests(page)
        page.click("#open-camera-live")
        assert page.locator("#preset-row button").count() == 3
        assert page.locator("#preset-save-mode").is_hidden() and page.locator("#setup-link").is_hidden() and page.locator("#camera-setup").is_hidden()
        page.click("#preset-row button[data-slot='1']")
        page.wait_for_function("document.body.dataset.view === 'camera'", timeout=5000)
        page.wait_for_timeout(300)
        assert asked == []                                     # no preview during a meeting
        assert server.devices.camera.recalls == [1] and server.devices.camera.saves == []   # the tap recalled the preset and saved nothing
        hold(page, "#arrow-pad button[data-pan='1']")
        page.wait_for_timeout(300)                             # the stop is a request still on its way when the release returns
        assert server.devices.camera.moves[-2:] == [(1, 0), (0, 0)]
        page.close()


def test_a_save_left_on_in_idle_does_not_turn_a_recall_into_a_save_in_a_meeting(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        page.click("#preset-save-mode")                        # Save is on, and the screen is left without a preset tapped
        page.click("#back-from-camera")
        page.click("#back-from-options")
        join_by_id(page)
        page.click("#open-camera-live")
        assert page.locator("#preset-save-mode").is_hidden()   # nobody can see it or undo it from here
        page.click("#preset-row button[data-slot='1']")
        page.wait_for_timeout(300)
        assert server.devices.camera.recalls == [1] and server.devices.camera.saves == []   # a recall, not an overwrite of the preset
        page.close()


def test_leaving_the_camera_screen_puts_save_and_set_up_away(browser):
    """A Save left on, or Set up left open, is not the next person's: Camera framing opened again finds both off, and
    a tap on a preset recalls it."""
    with PageServer() as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        page.click("#preset-save-mode")                        # Save is on ...
        page.click("#setup-link")                              # ... and Set up is open, and the screen is left
        assert page.locator("#preset-save-mode").get_attribute("aria-pressed") == "true" and page.locator("#camera-setup").is_visible()
        page.click("#back-from-camera")
        page.click("#open-camera-setup")
        page.wait_for_function("document.body.dataset.view === 'camera'", timeout=5000)
        assert page.locator("#preset-save-mode").get_attribute("aria-pressed") == "false" and page.locator("#preset-save-mode").inner_text() == "Save"
        assert page.locator("#camera-setup").is_hidden() and page.locator("#setup-link").is_visible()
        page.click("#preset-row button[data-slot='1']")
        page.wait_for_timeout(300)
        assert server.devices.camera.recalls == [1] and server.devices.camera.saves == []   # a recall, not an overwrite of the preset
        page.close()


def test_a_meeting_that_ends_under_a_held_arrow_still_stops_the_camera(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.click("#open-camera-live")
        page.wait_for_timeout(600)                                            # the view changed in a meeting: the page ignores taps for half a second
        press(page, "#arrow-pad button[data-pan='1']")
        page.wait_for_timeout(200)
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/leave", data="{}", headers={"Content-Type": "application/json"})
        page.wait_for_function("document.body.dataset.view === 'idle'", timeout=8000)
        page.wait_for_timeout(300)
        page.mouse.up()
        page.wait_for_timeout(300)
        assert server.devices.camera.moves[-1] == (0, 0)       # the pad vanished under the finger; the stop still went out
        page.close()


def test_a_failed_join_brings_the_page_back_to_the_idle_screen(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.click("#open-options")
        server.meeting.fail_next_join = True
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/1"}', headers={"Content-Type": "application/json"})
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=8000)
        assert page.evaluate("document.body.dataset.view") == "idle"
        assert page.locator("#kicker").inner_text().upper() == "COULDN'T JOIN"
        page.close()


def test_the_failed_join_card_lets_the_page_go_on_to_other_screens(browser):
    """The landing on the idle screen happens once, when the state turns to error: with the card still up, a tap on
    Options stays on Options through the polls that follow."""
    with PageServer() as server:
        page = open_page(browser, server)
        page.click("#open-options")
        server.meeting.fail_next_join = True
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/1"}', headers={"Content-Type": "application/json"})
        page.wait_for_function("document.body.dataset.state === 'error' && document.body.dataset.view === 'idle'", timeout=8000)
        page.click("#open-options")
        page.wait_for_timeout(2600)                            # a whole poll and more
        assert page.evaluate("document.body.dataset.view") == "options" and page.evaluate("document.body.dataset.state") == "error"
        page.close()


def test_the_top_bar_names_each_state_of_the_call(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        assert page.locator("#meeting-state").is_hidden() and page.locator("#meeting-elapsed").is_hidden()   # nothing of a call in the idle bar
        for state, words, timed in ((MeetingState.JOINING, "Joining", False), (MeetingState.IN_LOBBY, "In the lobby", False),
                                    (MeetingState.CONNECTED, "In a meeting", True), (MeetingState.LEAVING, "Leaving", False)):
            server.meeting.set_state(state)
            page.wait_for_function(f"document.getElementById('meeting-state').innerText === '{words}'", timeout=5000)
            assert page.evaluate("document.body.dataset.view") == "meeting" and page.locator("#meeting-state").is_visible()
            assert page.locator("#meeting-elapsed").is_visible() == timed   # only while connected, though a leaving call still has its join time
        server.meeting.set_state(MeetingState.IDLE)
        page.wait_for_function("document.body.dataset.view === 'idle'", timeout=5000)
        assert page.locator("#meeting-state").is_hidden() and page.locator("#meeting-elapsed").is_hidden()
        page.close()


@pytest.mark.parametrize("minutes, close", [(45, False), (8, True)])
def test_the_next_booking_warning_is_for_a_call_and_only_when_it_is_close(browser, minutes, close):
    skip_if_day_ends_within(1.5)                               # the booking is today's only: 45 minutes away at most
    with PageServer(calendar_events=[event("e1", "Design review", minutes)]) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function(f"document.body.dataset.state === '{'soon' if close else 'free'}'", timeout=5000)
        assert page.locator("#next-warning").is_hidden()         # idle: the status card says it, the bar does not
        server.meeting.set_state(MeetingState.JOINING)           # any state of a call
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        assert page.locator("#next-warning").is_visible() == close
        page.close()


# --- what the meeting screen's refactor rebuilt, and the meeting you are in ---

def test_a_booking_reads_the_same_in_the_today_list_and_the_upcoming_list(browser):
    """One builder makes both rows: the Today row, less its Join button, is the Upcoming row."""
    skip_if_day_ends_within(1.5)                               # the last booking starts in 40 minutes, and the service lists today's only
    bookings = [event("e1", "Design review", 8), event("e2", "Board lunch", 40, url=None)]   # one with a link, one without
    with PageServer(calendar_events=bookings) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.querySelectorAll('#events .event').length === 2 && document.querySelectorAll('#upcoming-list .event').length === 2", timeout=5000)
        rows = page.evaluate("""() => {
            const read = (li) => {
                const bare = li.cloneNode(true);
                bare.querySelectorAll('button').forEach((b) => b.remove());
                const platform = li.querySelector('.platform');
                return { html: bare.outerHTML, time: li.querySelector('time').textContent, title: li.querySelector('.title').textContent, platform: platform && platform.textContent };
            };
            return { today: [...document.querySelectorAll('#events .event')].map(read), upcoming: [...document.querySelectorAll('#upcoming-list .event')].map(read) };
        }""")
        assert rows["today"] == rows["upcoming"]
        assert [row["title"] for row in rows["upcoming"]] == ["Design review", "Board lunch"]
        assert [row["platform"] for row in rows["upcoming"]] == ["Zoom", None]      # the platform line is there when the booking has a link
        assert page.locator("#events .event button").count() == 1                   # Join is Today's own, on the booking inside its window
        assert page.locator("#upcoming-list button").count() == 0
        page.close()


def test_the_elapsed_time_in_the_top_bar_moves_between_status_polls(browser):
    """The once-a-second timer moves the time; a status poll every two seconds alone would skip a value."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("/^\\d+:\\d\\d$/.test(document.getElementById('meeting-elapsed').innerText)", timeout=5000)
        held = []
        page.route("**/api/status", lambda route: held.append(route))             # no answer from here on, so nothing is drawn again ...
        page.route("**/api/calendar/events", lambda route: held.append(route))    # ... and only the timer can move the time
        for _ in range(50):
            if held:
                break
            page.wait_for_timeout(100)                         # a poll has come and been held: every earlier one was answered
        assert held
        before = page.locator("#meeting-elapsed").inner_text()
        page.wait_for_timeout(2200)                            # two ticks at least
        assert page.locator("#meeting-elapsed").inner_text() != before
        for route in held:
            route.continue_()
        page.close()


def test_a_long_unbroken_title_in_the_warning_wraps_inside_the_top_bar(browser):
    skip_if_day_ends_within(1)                                 # the booking starts in 8 minutes, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Q" * 100, 8)]) as server:   # a title with no space or hyphen to break at
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        join_by_id(page)                                       # by link: the booking is still the next one
        page.wait_for_function("!document.getElementById('next-warning').hidden", timeout=5000)
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        clock = page.locator("#clock").bounding_box()
        assert clock["x"] >= 0 and clock["x"] + clock["width"] <= 1280      # the clock is still on the bar
        assert fits(page)
        page.close()


@pytest.mark.parametrize("width, height", [(390, 844), (1280, 800)])
@pytest.mark.parametrize("room_name, room_location", [("Lab", "2nd floor"), ("Room 2", "2nd floor, by the kitchen")])   # the second is the deploy templates' own example
def test_a_long_next_booking_title_gets_room_on_a_phone_and_leaves_the_kiosk_bar_alone(browser, width, height, room_name, room_location):
    """overflow-wrap: anywhere lets a flex item shrink to one letter. On a phone the warning takes a row of its own, the
    width of the bar, under the name: a pill of 200 px or more and a bar under 290 px whatever the room is called (the
    date line under the clock is 24 of them; a pill collapsed to a letter a column would make the bar several hundred).
    On the kiosk the bar stays one row, with the pill beside the clock."""
    skip_if_day_ends_within(1)                                 # the booking starts in 8 minutes, and the service lists today's only
    title = "Quarterly planning review with the regional leadership teams"
    assert len(title) == 60
    with PageServer(calendar_events=[event("e1", title, 8)], room_name=room_name, room_location=room_location) as server:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        join_by_id(page)                                       # by link: the booking is still the next one
        page.wait_for_function("!document.getElementById('next-warning').hidden", timeout=5000)
        pill, bar, clock = (page.locator(s).bounding_box() for s in ("#next-warning", ".top", "#clock"))
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        assert clock["x"] >= 0 and clock["x"] + clock["width"] <= width     # the clock is still on the bar
        if width < 1024:
            assert pill["width"] >= 200, f"the pill is {pill['width']:.0f} px wide"
            assert bar["height"] < 290, f"the top bar is {bar['height']:.0f} px tall"
        else:
            assert bar["height"] < 150, f"the kiosk's bar is {bar['height']:.0f} px tall"
            assert pill["y"] < clock["y"] + clock["height"] and clock["y"] < pill["y"] + pill["height"]   # one row: the pill beside the clock
        page.close()


def test_a_phone_bar_without_a_warning_keeps_its_one_row(browser):
    """The wrap is for the warning: on the idle bar the clock and Options still share a row beside the name."""
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        clock, options = page.locator("#clock").bounding_box(), page.locator("#open-options").bounding_box()
        assert clock["y"] < options["y"] + options["height"] and options["y"] < clock["y"] + clock["height"]
        page.close()


def test_the_meeting_you_are_in_is_not_next(browser):
    """Joined from its window, a booking has not started yet: the bar and the Upcoming list leave it out, and
    the list goes on showing a later one."""
    skip_if_day_ends_within(2)                                 # the last booking starts in 90 minutes, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Design review", 8), event("e2", "Sales call", 90)]) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        page.click("#actions button:has-text('Join now')")    # this booking, not a link
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        assert page.locator("#meeting-state").inner_text() == "In a meeting"
        assert page.locator("#next-warning").is_hidden()       # eight minutes away, but it is the meeting we are in
        page.click("#open-upcoming")
        assert page.locator("#upcoming-list .title").all_inner_texts() == ["Sales call"]
        assert page.locator("#upcoming-empty").is_hidden()
        page.click("#back-from-upcoming")
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/leave", data="{}", headers={"Content-Type": "application/json"})
        page.wait_for_function("document.body.dataset.view === 'idle'", timeout=8000)
        assert page.locator("#upcoming-list .title").all_text_contents() == ["Design review", "Sales call"]   # a booking again once the call is over
        page.close()


def test_a_booking_whose_platform_changes_is_redrawn_in_both_lists(browser):
    """An organiser swaps the link on a booking: at the next poll its rows say so, in the Today list and in the Upcoming list."""
    skip_if_day_ends_within(1.5)                               # the booking starts in 40 minutes, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Design review", 40)]) as server:
        page = page_on_a_stopped_clock(browser, server)
        swapped = []

        def answer(route):
            data = route.fetch().json()
            if swapped:
                for booking in data["events"]:
                    booking["meeting_platform"] = "teams"
            route.fulfill(json=data)

        page.route("**/api/calendar/events", answer)
        platforms = "[...document.querySelectorAll('#events .platform, #upcoming-list .platform')].map((p) => p.textContent).join()"
        assert page.evaluate(platforms) == "Zoom,Zoom"
        swapped.append(True)
        page.clock.run_for(30000)                              # the bookings are polled every 30 seconds
        page.wait_for_function(f"{platforms} === 'Teams,Teams'", timeout=5000)
        page.close()


# --- the meeting screen says which meeting, and why the room waits (final fix pass, spec 2026-10-12 section 4.1) ---

def test_the_meeting_screen_names_the_meeting_and_says_why_the_room_waits(browser):
    """The idle status card says which meeting and why the room waits, but it is hidden in a meeting: the meeting
    screen carries the same facts in #meeting-detail, whatever the call is doing."""
    skip_if_day_ends_within(1)                                 # the booking starts in 5 minutes, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Design review", 5)]) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        page.click("#actions button:has-text('Join now')")      # this booking, not a link: the call has its title
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        detail = page.locator("#meeting-detail")
        assert detail.is_visible() and detail.inner_text() == "Design review, Zoom"   # the title and the platform; the elapsed time is the bar's
        for state, bar, words in ((MeetingState.JOINING, "Joining", "Joining Design review\nThe room's screen is connecting."),
                                  (MeetingState.IN_LOBBY, "In the lobby", "Waiting for the host to let the room in."),
                                  (MeetingState.LEAVING, "Leaving", "Leaving"),
                                  (MeetingState.CONNECTED, "In a meeting", "Design review, Zoom")):
            server.meeting.set_state(state)
            page.wait_for_function(f"document.getElementById('meeting-state').innerText === '{bar}'", timeout=5000)
            assert detail.is_visible() and detail.inner_text() == words, state
        page.close()


def test_a_call_joined_by_link_is_named_by_its_platform_while_it_joins(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        server.meeting.set_state(MeetingState.JOINING)
        page.wait_for_function("document.getElementById('meeting-state').innerText === 'Joining'", timeout=5000)
        assert page.locator("#meeting-detail").inner_text() == "Joining Zoom meeting\nThe room's screen is connecting."
        page.close()


# --- what a mode change leaves behind: the last action's error and a Leave waiting for its second tap ---

JSON = {"Content-Type": "application/json"}


def test_a_refused_idle_action_does_not_follow_the_page_into_the_meeting_screen(browser):
    """model.error is the last action's answer, and it is written to both screens' lines: a call that begins from
    elsewhere must not open on "Unsupported meeting link"."""
    with PageServer() as server:
        page = open_page(browser, server)
        page.fill("#link-input", "not a meeting link")
        page.click("#link-form button[type=submit]")
        page.wait_for_function("document.getElementById('message').innerText === 'Unsupported meeting link'", timeout=5000)
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}', headers=JSON)
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=8000)
        assert page.evaluate("document.getElementById('meeting-message').textContent") == ""
        assert page.locator("#meeting-message").is_hidden()
        page.close()


def test_a_refused_meeting_action_does_not_follow_the_page_back_to_the_idle_screen(browser):
    refusal = "The call did not answer the request to change the microphone."
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.route("**/api/meeting/mute", lambda route: route.fulfill(status=409, json={"error": refusal}))
        page.click("#meeting-actions button:has-text('Mute')")
        page.wait_for_function("document.getElementById('meeting-message').innerText.length > 0", timeout=5000)
        assert page.locator("#meeting-message").inner_text() == refusal
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/leave", data="{}", headers=JSON)
        page.wait_for_function("document.body.dataset.view === 'idle'", timeout=8000)
        assert page.evaluate("document.getElementById('message').textContent") == ""
        assert page.locator("#message").is_hidden()
        page.close()


def test_a_leave_waiting_for_its_second_tap_does_not_outlive_the_call(browser):
    """The confirm step lasts five seconds and the call can end inside them: the next call's Leave must not arrive
    already asking "Tap again to leave". The clock is stopped, so the five seconds cannot run out by themselves."""
    with PageServer() as server:
        base = f"http://127.0.0.1:{server.port}"
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        page.click("#meeting-actions button:has-text('Leave')")
        assert page.locator("#meeting-actions button.danger").inner_text() == "Tap again to leave"
        # With the clock stopped no poll comes by itself: each wait asks for the status again until the page shows what it expects.
        page.request.post(base + "/api/meeting/leave", data="{}", headers=JSON)        # the call ends from elsewhere ...
        page.wait_for_function("window.croomPage.refreshStatus().then(() => document.body.dataset.view === 'idle')", timeout=5000)
        page.request.post(base + "/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}', headers=JSON)   # ... and another begins
        page.wait_for_function("window.croomPage.refreshStatus().then(() => document.body.dataset.state === 'meeting')", timeout=5000)
        assert page.locator("#meeting-actions button.danger").inner_text() == "Leave"
        page.close()


def test_a_camera_fault_shows_its_words_on_the_meeting_screen(browser):
    """The test meeting's "error 500": a control Zoom cannot carry out answers in words, and the table shows them."""
    from croom.meeting.providers.base import MeetingControlError
    with PageServer() as server:
        meeting = server.meeting
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        join_by_id(page)
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        meeting.fail_camera = MeetingControlError("Zoom's camera button is not on the screen yet; try again in a moment")
        page.locator("#meeting-actions button", has_text="camera").click()
        page.wait_for_function("document.getElementById('meeting-message').textContent.includes('camera button')", timeout=5000)
        assert page.locator("#meeting-actions button", has_text="camera").inner_text() == "Turn camera off"   # Zoom's state, unchanged
        meeting.fail_camera = None
        page.locator("#meeting-actions button", has_text="camera").click()
        page.wait_for_function("document.getElementById('meeting-message').textContent === ''", timeout=5000)
        page.close()


TWELVE_HOUR = r"^\d{1,2}:\d{2}\u00a0[AP]M$"   # the no-break space is the pin: a time never splits across lines on the sign or the TV
AMERICAN_DATE = r"^(January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}$"
SIGN_DATE = r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), (January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}$"   # the sign adds the weekday: "Monday, October 13, 2026"


def test_the_clock_the_date_and_the_booking_times_read_the_american_way_under_a_twenty_four_hour_locale(browser):
    """The Pi's Chromium follows the system locale, which showed 24-hour times; the pages pin the format instead."""
    skip_if_day_ends_within(2)                                 # the booking is 90 minutes out, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Design review", 90)]) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800}, locale="en-GB")
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert re.match(TWELVE_HOUR, page.locator("#clock").inner_text())
        assert re.match(AMERICAN_DATE, page.locator("#date").inner_text())   # the room page's date has no weekday: only the sign's does
        page.wait_for_selector("#events .event time", timeout=5000)
        assert re.match(TWELVE_HOUR, page.locator("#events .event time").first.inner_text())
        assert fits(page)
        page.close()


def test_the_door_sign_clock_and_date_read_the_american_way_under_a_twenty_four_hour_locale(browser):
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 1024, "height": 600}, locale="en-GB")
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert re.match(TWELVE_HOUR, page.locator("#clock").inner_text())
        shown = page.locator("#date").inner_text()
        assert re.match(SIGN_DATE, shown)                                       # "Monday, October 13, 2026"
        earlier, later = datetime.now().astimezone() - timedelta(seconds=2), datetime.now().astimezone()   # the date can change between the tick and here
        assert shown in {d.strftime("%A, %B ") + str(d.day) + d.strftime(", %Y") for d in (earlier, later)}   # the weekday is today's
        page.close()


# --- the TV remote on the table (spec 2026-10-13 TV remote and touchpad, section 5) ---

class RemoteTv(StubTv):
    """The StubTv of these page tests. What it shows can change while a page is open (`shown` overrides keys of the status), and the
    controls do to the status what the real service's do: Power on brings the TV on and Power off takes it down, Mute switches the sound.
    A pairing waits for the person at the TV when `pair_gate` is set, and forget() ends that wait in words, as the real one does."""

    def __init__(self, configured=True, paired=True, **shown):
        super().__init__(configured=configured, paired=paired)
        self.shown = dict(shown)
        self.stopped = False

    def status(self):
        shown = super().status()
        if not self.paired:   # a TV that is not paired is not connected: the service shows it blank
            shown.update(on=False, reachable=False, name="", volume=None, muted=False, input=None, inputs=[], app=None, channel=None)
        return {**shown, **self.shown}

    async def control(self, name, value=None):
        await super().control(name, value)          # records it, waits for the gate, and fails when told to
        if name == "power":
            self.shown.update(on=value == "on", reachable=value == "on")
            self.shown.pop("volume", None) if value == "on" else self.shown.update(volume=None)
        elif name == "mute":
            self.shown["muted"] = not self.shown.get("muted", False)
        return self.status()

    async def pair(self):
        self.calls.append(("pair",))
        if self.pair_gate is not None:
            await self.pair_gate.wait()
        if self.stopped:
            self.stopped = False
            raise TvError("Pairing with the TV at 10.0.0.41 was stopped before it finished")
        if self.fail_pair is not None:
            raise self.fail_pair
        self.paired = True
        self.shown.pop("refused", None)

    async def forget(self):
        await super().forget()
        if self.pair_gate is not None and not self.pair_gate.is_set():
            self.stopped = True
            self.pair_gate.set()


LOBBY = {"host": "10.0.0.57", "name": "Lobby TV", "location": "http://10.0.0.57:1234/udhisapi.xml"}
UNNAMED = {"host": "10.0.0.60", "name": "", "location": ""}
REFUSAL = ("The TV did not accept that: invalid channel, and a good many more words than fit on the one line of the status, so that they wrap "
           "onto a second line, as the words of a TV may")
WONT_TURN_ON = ("The TV at 10.0.0.41 did not turn on: check that Wake-on-LAN is enabled on it (Mobile TV On) and that tv.mac is its MAC address")
AMBER = "rgb(232, 160, 19)"   # --warn-bar

# Every key of the remote by its data-tv attribute ("name:value", or the name alone), and what its tap asks of the control API. Power and Input
# are the two whose tap asks nothing at once (Power's first tap arms it, Input opens the row), so each has a test of its own. OK and Go are
# both ENTER. The playback row is rewind, play, pause, stop and fast forward.
REMOTE_KEYS = ["key:HOME", "key:MENU", "volume_down", "volume_up", "mute", "channel_up", "channel_down",
               "key:UP", "key:RIGHT", "key:DOWN", "key:LEFT", "key:ENTER", "key:BACK", "key:EXIT", "key:INFO", "key:GUIDE",
               *[f"digit:{n}" for n in range(1, 10)], "key:DASH", "digit:0", "key:ENTER",
               "key:REWIND", "key:PLAY", "key:PAUSE", "key:STOP", "key:FASTFORWARD", "key:RED", "key:GREEN", "key:YELLOW", "key:BLUE"]


def meeting_tv_button(page):
    """The TV's button on the meeting screen: the last of the secondary row, after Host when the room is host or co-host. The buttons of the
    row have no ids (a button is found by its words), and the exact name keeps Options' TV button, which is not on this screen, out of it."""
    return page.locator("#meeting-secondary").get_by_role("button", name="TV", exact=True)


def open_tv_screen(page):
    """Idle mode: Options, then TV. Waits until the TV view is up."""
    page.click("#open-options")
    page.click("#open-tv-options")
    page.wait_for_function("document.body.dataset.view === 'tv'", timeout=5000)


def tv_page(browser, server, width=1280, height=800):
    page = browser.new_page(viewport={"width": width, "height": height})
    page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
    page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
    open_tv_screen(page)
    return page


def tv_calls(page, server, count, timeout=5.0):
    """What the stub TV has been asked, once it has been asked `count` times: the page's request is on its way when the tap is made."""
    waited = 0.0
    while len(server.tv.calls) < count and waited < timeout:
        page.wait_for_timeout(25)
        waited += 0.025
    assert len(server.tv.calls) >= count, f"the TV was asked {server.tv.calls}, not {count} things"
    return list(server.tv.calls)


def status_line(page):
    """What the TV's status line says, on one line: its parts as a person reads them, with a dot between."""
    return " ".join(page.locator("#tv-status").inner_text().split())


def tv_requests(page):
    """The page's requests to the TV routes, as they are made: (method, route, content type, body)."""
    seen = []
    page.on("request", lambda r: seen.append((r.method, r.url.split("/api/", 1)[1], r.headers.get("content-type"), r.post_data)) if "/api/tv/" in r.url else None)
    return seen


def refresh_now(page):
    page.evaluate("window.croomPage.refreshStatus()")


def test_the_tv_view_opens_from_options_and_from_a_meeting_and_returns_to_each(browser):
    with PageServer(tv=RemoteTv()) as server:
        page = open_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.click("#open-options")
        page.click("#open-tv-options")
        assert page.evaluate("document.body.dataset.view") == "tv"
        assert page.locator("#tv-remote").is_visible() and page.locator("#open-options").is_visible()
        page.click("#back-from-tv")
        assert page.evaluate("document.body.dataset.view") == "options"          # Back returns to the screen that opened it
        page.click("#back-from-options")
        join_by_id(page)
        meeting_tv_button(page).click()
        assert page.evaluate("document.body.dataset.view") == "tv"
        assert page.locator("#tv-remote").is_visible() and page.locator("#open-options").is_hidden()   # in a meeting the bar has no Options
        page.click("#back-from-tv")
        assert page.evaluate("document.body.dataset.view") == "meeting"
        assert errors == []
        page.close()


def test_the_options_screen_has_the_tv_beside_the_others_and_the_meeting_screen_has_it_last_in_its_second_row(browser):
    with PageServer(tv=RemoteTv()) as server:
        page = open_page(browser, server)
        page.click("#open-options")
        assert page.locator(".view[data-view=options] .menu button").evaluate_all("bs => bs.map(b => b.id)") == \
            ["open-camera-setup", "open-screensaver", "open-door-sign", "open-tv-options"]
        assert page.locator("#open-tv-options").inner_text().strip() == "TV" and page.locator("#open-tv-options svg use").get_attribute("href") == "#icon-tv"
        page.click("#back-from-options")
        join_by_id(page)
        assert page.locator(".view[data-view=meeting] .menu button").evaluate_all("bs => bs.map(b => b.id)") == \
            ["open-camera-live", "open-upcoming"]                                # the menu has its two again: the TV is in the second row
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        assert texts(page, "#meeting-secondary")[-1] == "TV"                     # the last of the second row, after Host when there is one
        assert meeting_tv_button(page).locator("svg use").get_attribute("href") == "#icon-tv"
        assert page.locator("#meeting-secondary .button").evaluate_all("bs => bs.filter(b => b.id).length") == 0   # (no ids of its own: the row's buttons are found by their words)
        page.close()


@pytest.mark.parametrize("tv, card", [
    (lambda: None, "tv-not-set-up"),                      # a room with no TV set up at all
    (lambda: RemoteTv(paired=False), "tv-pairing"),       # a TV that has not been paired yet
    (lambda: RemoteTv(refused=True), "tv-refused"),       # a TV that no longer accepts this table
    (lambda: RemoteTv(), "tv-remote"),                    # a TV that answers
])
def test_the_meeting_screens_tv_button_is_drawn_whatever_the_state_of_the_tv_and_its_view_has_the_cards(browser, tv, card):
    """The button is the second row's, drawn when the row is: it does not ask whether a TV is set up or paired, because the view it opens has
    a card for each of those (a room with no TV still has the room Pi's mouse). Back returns to the meeting screen."""
    with PageServer(tv=tv()) as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        assert texts(page, "#meeting-secondary")[-1] == "TV" and meeting_tv_button(page).is_enabled()
        meeting_tv_button(page).click()
        page.wait_for_function("document.body.dataset.view === 'tv'", timeout=5000)
        page.wait_for_selector(f"#{card}", state="visible", timeout=5000)
        page.click("#back-from-tv")
        assert page.evaluate("document.body.dataset.view") == "meeting"
        page.close()


def test_the_tv_button_follows_host_when_there_is_one_and_is_last_when_there_is_not(browser):
    with PageServer(role="co-host", tv=RemoteTv()) as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        assert texts(page, "#meeting-secondary")[-2:] == ["Host", "TV"]
        server.meeting.current_meeting.controls["host"]["role"] = "participant"
        page.wait_for_function("!document.getElementById('meeting-secondary').textContent.includes('Host')", timeout=5000)
        assert texts(page, "#meeting-secondary")[-2:] == ["Chat", "TV"]
        page.close()


@pytest.mark.parametrize("width, height", [(1280, 800), (1920, 1080), (1920, 1200)])
def test_the_seven_buttons_of_the_second_row_sit_in_one_row_with_the_tv_last_and_the_screen_fits(browser, width, height):
    """The second row at its widest (Captions off, Participants (120), Chat (12), Host, TV) at the kiosk's 1280 by 800, at 1920 by 1080 and
    at the roomy 1920 by 1200: one row, the TV's button last, with room to the right of it, and nothing on the screen scrolls."""
    with PageServer(role="host", tv=RemoteTv()) as server:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        join_by_id(page)
        meeting = server.meeting.current_meeting
        meeting.controls["captions"]["on"] = True
        set_people(server, a_roster(9), count=120)
        meeting.chat = {"unread": 12, "messages": []}
        page.wait_for_function("document.getElementById('meeting-secondary').textContent.includes('Chat (12)')", timeout=5000)
        assert texts(page, "#meeting-secondary") == ["Captions off", "Gallery view", "Hide self view", "Participants (120)", "Chat (12)", "Host", "TV"]
        assert in_one_row(page, "#meeting-secondary")
        spare = page.evaluate("""() => { const row = document.getElementById('meeting-secondary').getBoundingClientRect(),
            last = [...document.querySelectorAll('#meeting-secondary .button')].pop().getBoundingClientRect();
            return Math.round(row.right - last.right); }""")
        assert spare >= 0, f"the TV's button runs {-spare} px past the row"
        assert fits(page), "the meeting screen scrolls with the seven buttons of the second row"
        page.close()


def test_a_call_that_starts_or_ends_under_the_tv_view_lands_on_the_screen_of_the_new_mode(browser):
    """The TV view belongs to the mode that opened it, as the camera view does: when the room changes mode under it, the page is on that
    mode's own screen, not on a remote that Back would take to a screen of the other mode."""
    with PageServer(tv=RemoteTv()) as server:
        page = open_page(browser, server)
        open_tv_screen(page)                                                     # from Options: an idle screen
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}',
                          headers={"Content-Type": "application/json"})
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=8000)
        meeting_tv_button(page).click()                                           # from the meeting
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/leave", data="{}", headers={"Content-Type": "application/json"})
        page.wait_for_function("document.body.dataset.view === 'idle'", timeout=8000)
        assert page.locator("#link-form").is_visible()
        page.close()


def test_the_tv_view_returns_by_itself_after_ten_idle_minutes_when_idle(browser):
    with PageServer(tv=RemoteTv()) as server:
        page = page_on_a_stopped_clock(browser, server)
        open_tv_screen(page)
        page.clock.fast_forward(9 * 60_000)
        page.wait_for_timeout(200)
        assert page.evaluate("document.body.dataset.view") == "tv"
        page.click('[data-tv="key:HOME"]')                                       # a tap at nine minutes keeps it another ten
        page.clock.fast_forward(9 * 60_000)
        page.wait_for_timeout(200)
        assert page.evaluate("document.body.dataset.view") == "tv"
        page.clock.fast_forward(90_000)
        page.wait_for_timeout(200)
        assert page.evaluate("document.body.dataset.view") == "idle"
        page.close()


def test_the_tv_view_returns_by_itself_after_two_idle_minutes_in_a_meeting(browser):
    with PageServer(tv=RemoteTv()) as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        meeting_tv_button(page).click()
        page.clock.fast_forward(100_000)
        page.wait_for_timeout(200)
        assert page.evaluate("document.body.dataset.view") == "tv"
        page.click('[data-tv="key:HOME"]')                                       # a tap at 100 s keeps it another two minutes
        page.clock.fast_forward(100_000)
        page.wait_for_timeout(200)
        assert page.evaluate("document.body.dataset.view") == "tv"
        page.clock.fast_forward(30_000)
        page.wait_for_timeout(200)
        assert page.evaluate("document.body.dataset.view") == "meeting"          # back on the meeting screen, where the call is
        page.close()


def test_the_remote_sends_the_right_controls(browser):
    """The remote has every key, and each asks the control API for what its data-tv attribute names: "key:HOME" is {"name": "key", "value": "HOME"}, "mute" has no value."""
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        keys = page.locator("#tv-remote [data-tv]")
        attributes = keys.evaluate_all("els => els.map(e => e.dataset.tv)")
        assert sorted(attributes) == sorted(REMOTE_KEYS + ["power:toggle", "input:menu"])
        assert all(k.is_visible() for k in keys.all())
        asked = 0
        for index, attribute in enumerate(attributes):
            if attribute in ("power:toggle", "input:menu"):
                continue
            name, _, value = attribute.partition(":")
            keys.nth(index).click()
            asked += 1
            assert tv_calls(page, server, asked)[-1] == ("control", name, value or None), attribute
        page.close()


def test_every_request_the_remote_makes_is_json_with_the_bodies_the_routes_take(browser):
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        seen = tv_requests(page)
        page.click('[data-tv="key:HOME"]')
        page.click('[data-tv="mute"]')
        page.click('[data-tv="digit:7"]')
        tv_calls(page, server, 3)
        assert seen == [("POST", "tv/control", "application/json", '{"name":"key","value":"HOME"}'),
                        ("POST", "tv/control", "application/json", '{"name":"mute"}'),
                        ("POST", "tv/control", "application/json", '{"name":"digit","value":"7"}')]
        page.close()


def test_the_status_line_names_the_tv_its_state_and_its_input_and_marks_the_pi(browser):
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        assert status_line(page) == "Boardroom TV · On · HDMI 1 (Crystal Meet)"
        assert page.locator("#tv-status").evaluate("s => getComputedStyle(s).whiteSpace") != "pre"
        tv.shown.update(input="HDMI_2", app="com.webos.app.hdmi2")               # another input: not the Pi's, and not marked
        refresh_now(page)
        page.wait_for_function("document.getElementById('tv-status').textContent.includes('HDMI 2')", timeout=5000)
        assert status_line(page) == "Boardroom TV · On · HDMI 2"
        tv.shown.update(input=None, app="com.webos.app.livetv", channel="11")
        refresh_now(page)
        page.wait_for_function("document.getElementById('tv-status').textContent.includes('Live TV')", timeout=5000)
        assert status_line(page) == "Boardroom TV · On · Live TV · Channel 11"
        tv.shown.update(input="HDMI_3", pi_input="HDMI_3", inputs=[{"id": "HDMI_3", "label": "HDMI 3"}], channel=None)   # the Pi's input is the config's
        refresh_now(page)
        page.wait_for_function("document.getElementById('tv-status').textContent.includes('HDMI 3')", timeout=5000)
        assert status_line(page) == "Boardroom TV · On · HDMI 3 (Crystal Meet)"
        assert page.locator("#tv-status .tv-dot").is_visible()                   # the dot beside On: drawn, no text
        page.close()


def test_the_volume_level_and_the_mute_key_follow_the_tv(browser):
    """Mute follows the speaker's button on the meeting screen: its picture is the TV's sound (a speaker while it is live, a crossed one while it
    is muted) and its words are what a tap does ("Mute", then "Unmute"), in a key as wide as "Unmute" so that nothing moves."""
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        assert page.locator("#tv-level").inner_text() == "24"
        mute = page.locator("#tv-mute")
        assert mute.inner_text().strip() == "Mute" and mute.get_attribute("aria-pressed") == "false"
        assert mute.locator("svg use").get_attribute("href") == "#icon-volume"          # the sound is live
        assert "muted" not in page.locator("#tv-level").get_attribute("class").split()
        live_width = mute.bounding_box()["width"]
        tv.volume = 31
        mute.click()
        tv_calls(page, server, 1)
        page.wait_for_function("document.getElementById('tv-mute').getAttribute('aria-pressed') === 'true'", timeout=5000)
        assert mute.inner_text().strip() == "Unmute" and mute.locator("svg use").get_attribute("href") == "#icon-volume-x"   # muted: the crossed speaker, and a tap now unmutes
        assert mute.bounding_box()["width"] == live_width                               # as wide in both states
        assert "current" in mute.get_attribute("class").split() and "muted" in page.locator("#tv-level").get_attribute("class").split()
        assert page.locator("#tv-level").inner_text() == "31"                           # the answer to the tap carries the volume too
        mute.click()                                                                    # and it is the same control: the second tap unmutes
        assert tv_calls(page, server, 2)[-1] == ("control", "mute", None)
        tv.shown.update(muted=False, volume=None)
        refresh_now(page)
        page.wait_for_function("document.getElementById('tv-level').textContent.trim() === '\u2013'", timeout=5000)   # a TV that says no volume: a dash
        assert mute.inner_text().strip() == "Mute" and mute.locator("svg use").get_attribute("href") == "#icon-volume"
        assert mute.get_attribute("aria-pressed") == "false" and "current" not in mute.get_attribute("class").split()
        page.close()


def test_no_key_moves_when_the_tv_changes_what_it_shows_or_power_asks_to_confirm(browser):
    """A key that moves a moment before the finger lifts is a key that is missed: the sound being switched, a volume of 100, Power's confirm
    and a long name in the status line leave every key where it was."""
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        where = lambda: page.evaluate("[...document.querySelectorAll('#tv-remote [data-tv]')].map(k => { const r = k.getBoundingClientRect(); return [k.dataset.tv, r.left, r.top, r.width, r.height]; })")
        resting = where()
        tv.shown.update(muted=True)
        refresh_now(page)
        page.wait_for_function("document.getElementById('tv-mute').getAttribute('aria-pressed') === 'true'", timeout=5000)
        assert where() == resting, "muting moved a key"
        tv.shown.update(volume=100, name="A TV with a name that is a good deal longer than the line has room for, to see the line cut it short")
        refresh_now(page)
        page.wait_for_function("document.getElementById('tv-level').textContent === '100'", timeout=5000)
        assert where() == resting, "a volume of 100 or a long name moved a key"
        page.click("#tv-power")
        page.wait_for_function("document.getElementById('tv-power').textContent.includes('Tap again')", timeout=5000)
        assert where() == resting, "Power asking to confirm moved a key"
        page.close()


def test_power_off_needs_a_second_tap(browser):
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = page_on_a_stopped_clock(browser, server)
        open_tv_screen(page)
        power = page.locator("#tv-power")
        assert power.inner_text().strip() == "Power" and power.locator("svg use").get_attribute("href") == "#icon-power"
        power.click()
        page.wait_for_timeout(200)
        assert tv.calls == []                                                    # the first tap asks nothing of the TV
        assert power.inner_text().strip() == "Tap again to turn off"
        assert page.evaluate("getComputedStyle(document.getElementById('tv-power')).borderTopColor") == AMBER   # amber, as Leave is
        assert fits(page)                                                        # the longer words take no more room: the row of keys is where it was
        page.clock.fast_forward(5_100)                                           # nobody tapped again: it goes back to Power
        page.wait_for_timeout(200)
        assert power.inner_text().strip() == "Power" and tv.calls == []
        power.click()
        page.clock.run_for(600)                                                  # (a person's second tap: after more than half a second)
        power.click()                                                            # the second tap, within five seconds
        assert tv_calls(page, server, 1) == [("control", "power", "off")]
        page.wait_for_function("document.getElementById('tv-turn-on') && !document.getElementById('tv-turn-on').hidden", timeout=5000)
        assert power.inner_text().strip() == "Power"                             # the TV is off now: the button is just Power, and waits for a tap
        page.close()


def test_a_bounced_double_tap_on_power_does_not_turn_the_tv_off(browser):
    """A touch screen can report one tap twice a moment apart: the first arms Power, and the second would be taken for the confirmation.
    After arming, Power ignores a tap for half a second (CONFIRM_GUARD_MS); a person's own second tap comes after that, within the five seconds."""
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = page_on_a_stopped_clock(browser, server)
        open_tv_screen(page)
        power = page.locator("#tv-power")
        power.click()                                                            # armed
        page.clock.run_for(150)
        power.click()                                                            # the bounce, 150 ms after the first tap
        page.wait_for_timeout(200)
        assert power.inner_text().strip() == "Tap again to turn off" and tv.calls == []          # still armed, and nothing was asked of the TV
        page.clock.run_for(349)                                                  # 499 ms after the first tap: the guard has a millisecond to go
        power.click()
        page.wait_for_timeout(200)
        assert power.inner_text().strip() == "Tap again to turn off" and tv.calls == []
        page.clock.run_for(201)                                                  # 700 ms after the first tap: a person's own second tap
        power.click()
        assert tv_calls(page, server, 1) == [("control", "power", "off")]        # confirms
        page.close()


def test_a_second_tap_after_the_guard_still_has_the_five_seconds_of_the_first(browser):
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = page_on_a_stopped_clock(browser, server)
        open_tv_screen(page)
        power = page.locator("#tv-power")
        power.click()
        page.clock.run_for(4_900)
        power.click()                                                            # nearly five seconds on: it still confirms
        assert tv_calls(page, server, 1) == [("control", "power", "off")]
        page.close()


def test_a_tv_that_is_off_shows_turn_on_and_one_tap_on_power_turns_it_on(browser):
    tv = RemoteTv(on=False, reachable=False, volume=None)
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        assert status_line(page) == "Boardroom TV · Turn on"                     # Turn on in the place of the state
        turn_on = page.locator("#tv-turn-on")
        assert turn_on.is_visible() and turn_on.is_enabled()
        assert page.locator("#tv-power").is_enabled() and page.locator("#tv-power").evaluate("b => getComputedStyle(b).backgroundColor") == "rgb(27, 82, 229)"   # the one filled button
        for key in page.locator("#tv-remote [data-tv]").all():                   # nothing else can do anything while the TV is off
            if key.get_attribute("data-tv") != "power:toggle":
                assert key.is_disabled(), key.get_attribute("data-tv")
        page.click("#tv-power")                                                  # on at once: no second tap to turn a TV on
        assert tv_calls(page, server, 1) == [("control", "power", "on")]
        page.wait_for_function("document.getElementById('tv-status').textContent.includes('HDMI 1')", timeout=5000)
        assert status_line(page) == "Boardroom TV · On · HDMI 1 (Crystal Meet)" and page.locator("#tv-turn-on").is_hidden()
        assert all(k.is_enabled() for k in page.locator("#tv-remote [data-tv]").all())
        page.mouse.move(0, 0)                                                    # (off the key: a hovered one is tinted)
        assert page.locator("#tv-power").evaluate("b => getComputedStyle(b).backgroundColor") == "rgba(0, 0, 0, 0)"   # outlined again
        page.close()


ASKING = "The TV is asking to pair again \u2014 Forget TV, then Pair"


def test_a_tv_that_is_asking_to_pair_again_says_so_in_place_of_turn_on_and_follows_the_status(browser):
    """A TV that has forgotten this table shows its pairing prompt (the status says `prompting`): the table's line says what to do about it,
    where an off TV says Turn on, and Forget TV is on the head row as it always is. Turning it on would not help, and the line goes when
    the TV stops asking (a Turn on that is with the agent still says Turning on)."""
    tv = RemoteTv(on=False, reachable=False, volume=None, prompting=True)
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        assert status_line(page) == "Boardroom TV \u00b7 " + ASKING
        assert page.locator("#tv-turn-on").is_hidden()
        forget = page.locator("#tv-forget-head")
        assert forget.is_visible() and forget.is_enabled()
        assert page.locator("#tv-remote").is_visible() and page.locator("#tv-words").is_hidden()      # it is still the remote, and nothing was refused
        assert fits(page)
        tv.shown.update(prompting=False)                                         # the person accepted it with the TV's own remote, or it went away
        refresh_now(page)
        page.wait_for_function("document.getElementById('tv-status').textContent.includes('Turn on')", timeout=5000)
        assert status_line(page) == "Boardroom TV \u00b7 Turn on" and page.locator("#tv-turn-on").is_visible()
        page.close()


def test_a_tv_that_is_on_says_what_it_shows_even_when_the_status_says_it_is_asking(browser):
    """Asking is for a TV that is not connected: one that is on and answering says where it is, as it always did."""
    with PageServer(tv=RemoteTv(prompting=True)) as server:
        page = tv_page(browser, server)
        assert status_line(page) == "Boardroom TV \u00b7 On \u00b7 HDMI 1 (Crystal Meet)"
        page.close()


def test_a_tv_that_is_asking_to_pair_again_and_is_turned_on_says_turning_on_meanwhile(browser):
    tv = RemoteTv(on=False, reachable=False, volume=None, prompting=True)
    tv.gate = asyncio.Event()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.click("#tv-power")                                                  # Power is there, as it is for any TV that is not on
        tv_calls(page, server, 1)
        page.wait_for_function("document.getElementById('tv-status').textContent.includes('Turning on')", timeout=5000)
        assert status_line(page) == "Boardroom TV \u00b7 Turning on\u2026"
        server.release(tv.gate)
        page.close()


def test_turn_on_in_the_status_line_does_the_same_and_standby_counts_as_off(browser):
    tv = RemoteTv(on=False, reachable=True)                                      # in standby: it answers, and says it is not on
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        assert status_line(page) == "Boardroom TV · Turn on"
        page.click("#tv-turn-on")
        assert tv_calls(page, server, 1) == [("control", "power", "on")]
        page.wait_for_function("document.getElementById('tv-status').textContent.includes('HDMI 1')", timeout=5000)
        assert page.locator("#tv-turn-on").is_hidden()
        page.close()


def test_turn_on_holds_the_remote_and_says_turning_on_until_the_tv_answers(browser):
    """A Turn on takes up to 23 seconds, and a key sent meanwhile would wait behind it in the agent: the page holds the keys instead."""
    tv = RemoteTv(on=False, reachable=False, volume=None)
    tv.gate = asyncio.Event()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.click("#tv-turn-on")
        tv_calls(page, server, 1)
        page.wait_for_function("document.getElementById('tv-status').textContent.includes('Turning on')", timeout=5000)
        assert status_line(page) == "Boardroom TV · Turning on…"
        assert page.locator("#tv-turn-on").is_hidden()
        assert all(k.is_disabled() for k in page.locator("#tv-remote [data-tv]").all())      # all of them: Power too
        refresh_now(page)                                                        # a poll meanwhile does not let go of them
        page.wait_for_timeout(300)
        assert status_line(page) == "Boardroom TV · Turning on…"
        server.release(tv.gate)
        page.wait_for_function("document.getElementById('tv-status').textContent.includes('HDMI 1')", timeout=5000)
        assert status_line(page) == "Boardroom TV · On · HDMI 1 (Crystal Meet)"
        assert all(k.is_enabled() for k in page.locator("#tv-remote [data-tv]").all())
        page.close()


def test_a_turn_on_the_tv_refuses_says_why_and_lets_the_keys_go(browser):
    tv = RemoteTv(on=False, reachable=False, volume=None)
    tv.fail_control = TvError(WONT_TURN_ON)
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.click("#tv-turn-on")
        page.wait_for_selector("#tv-words:not([hidden])", timeout=5000)
        assert page.locator("#tv-words").inner_text() == WONT_TURN_ON
        assert status_line(page).startswith("Boardroom TV · Turn on")             # it is still off, and still can be turned on
        assert page.locator("#tv-turn-on").is_enabled() and page.locator("#tv-power").is_enabled()
        page.close()


def test_the_input_row_marks_the_pi(browser):
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        assert page.locator("#tv-inputs").is_hidden() and page.locator("#tv-keys").is_visible()
        page.click("#tv-input")
        assert page.locator("#tv-inputs").is_visible() and page.locator("#tv-keys").is_hidden()      # the row takes the top keys' place
        assert tv.calls == []                                                    # opening it asks nothing of the TV
        inputs = page.locator("#tv-input-list button")
        assert inputs.all_inner_texts() == ["HDMI 1 Crystal Meet", "HDMI 2"]     # the Pi's input says whose it is
        assert inputs.nth(0).locator("svg use").get_attribute("href") == "#icon-check"    # and the one showing now carries a check
        assert inputs.nth(1).locator("svg").count() == 0
        assert [b.get_attribute("aria-pressed") for b in inputs.all()] == ["true", "false"]
        tv.shown.update(input="HDMI_2", app="com.webos.app.hdmi2")              # the TV is on the other input: the check moves, the name stays
        refresh_now(page)
        page.wait_for_function("document.querySelectorAll('#tv-input-list button')[1].querySelector('svg')", timeout=5000)
        assert inputs.nth(0).locator("svg").count() == 0 and inputs.nth(1).locator("svg use").get_attribute("href") == "#icon-check"
        assert inputs.all_inner_texts() == ["HDMI 1 Crystal Meet", "HDMI 2"]
        page.close()


def test_the_input_row_posts_a_pick_and_closes_on_it_on_close_and_after_ten_seconds(browser):
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = page_on_a_stopped_clock(browser, server)
        open_tv_screen(page)
        page.click("#tv-input")
        page.click("#tv-input-list button:has-text('HDMI 2')")
        assert tv_calls(page, server, 1) == [("control", "input", "HDMI_2")]
        assert page.locator("#tv-inputs").is_hidden() and page.locator("#tv-keys").is_visible()   # a pick closes the row
        page.click("#tv-input")
        page.click("#tv-inputs-close")
        assert page.locator("#tv-inputs").is_hidden() and len(tv.calls) == 1     # Close asks nothing
        page.click("#tv-input")
        page.clock.fast_forward(9_000)
        page.wait_for_timeout(100)
        assert page.locator("#tv-inputs").is_visible()
        page.clock.fast_forward(1_500)
        page.wait_for_timeout(100)
        assert page.locator("#tv-inputs").is_hidden() and page.locator("#tv-keys").is_visible()   # nobody picked: it puts itself away
        page.close()


def test_a_refusal_shows_the_tvs_words_in_the_status_line_and_flashes_the_key_amber_once(browser):
    tv = RemoteTv()
    tv.fail_control = TvError(REFUSAL)
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.evaluate("""() => { window.flashes = []; const key = document.querySelector('[data-tv="digit:5"]');
            new MutationObserver(() => window.flashes.push([key.classList.contains('refused'), getComputedStyle(key).borderTopColor]))
                .observe(key, { attributes: true, attributeFilter: ['class'] }); }""")
        page.click('[data-tv="digit:5"]')
        page.wait_for_selector("#tv-words:not([hidden])", timeout=5000)
        words = page.locator("#tv-words")
        assert words.inner_text() == REFUSAL
        # wrapped, never cut: every word is there, on as many lines as it takes, and nothing runs out of its box
        assert words.evaluate("w => { const s = getComputedStyle(w); return [s.whiteSpace, s.textOverflow, w.scrollWidth <= w.clientWidth + 1]; }") == ["normal", "clip", True]
        assert words.bounding_box()["height"] > 30                              # two lines of the long refusal
        page.wait_for_timeout(1500)
        flashes = page.evaluate("window.flashes")
        assert [on for on, _ in flashes] == [True, False]                       # once: it came on, and went
        assert flashes[0][1] == AMBER
        refresh_now(page)                                                       # a poll does not take the words away: the next control does
        page.wait_for_timeout(300)
        assert words.is_visible()
        tv.fail_control = None
        page.click('[data-tv="digit:6"]')
        tv_calls(page, server, 2)
        page.wait_for_selector("#tv-words", state="hidden", timeout=5000)
        page.close()


def test_the_words_go_when_the_screen_is_left(browser):
    tv = RemoteTv()
    tv.fail_control = TvError(REFUSAL)
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.click('[data-tv="key:HOME"]')
        page.wait_for_selector("#tv-words:not([hidden])", timeout=5000)
        page.click("#back-from-tv")
        page.click("#open-tv-options")
        assert page.locator("#tv-words").is_hidden()                            # the next person does not open the screen on the last one's refusal
        page.close()


def test_words_the_tv_sends_are_text_and_never_markup(browser):
    attack = '<img src=x onerror="window.pwned = 1">'
    tv = RemoteTv(name=attack, input="HDMI_<b>1", pi_input="HDMI_2", app="<script>window.pwned = 1</script>", channel="<i>7</i>",
                  inputs=[{"id": "HDMI_<b>1", "label": "<b>HDMI</b> 1"}, {"id": "HDMI_2", "label": attack}])
    tv.fail_control = TvError(attack)
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        assert status_line(page) == f"{attack} \u00b7 On \u00b7 <b>HDMI</b> 1 \u00b7 Channel <i>7</i>"   # the TV's name, input, channel, as they came
        page.click("#tv-input")
        assert page.locator("#tv-input-list").inner_text().replace("\n", " ").startswith("<b>HDMI</b> 1")
        page.click("#tv-input-list button:has-text('<img')")
        page.wait_for_selector("#tv-words:not([hidden])", timeout=5000)
        assert page.locator("#tv-words").inner_text() == attack
        for scope in ("#tv-status", "#tv-input-list", "#tv-inputs", "#tv-words"):
            assert page.locator(f"{scope} img, {scope} b, {scope} i:not(.tv-dot), {scope} script").count() == 0, scope   # (the green dot beside On is the page's own)
        assert page.evaluate("window.pwned") is None and errors == []
        page.close()


def test_a_tv_that_says_odd_things_never_stops_the_page_drawing(browser):
    """What a device on the network says is not trusted to have the shape it should: a render that threw would stop the sound and camera
    panels drawing too."""
    odd = [None, 5, {"label": "no id"}, {"id": "HDMI_1", "label": None}, "HDMI_2"]
    tv = RemoteTv(name=5, volume="loud", channel=7, inputs=odd, input="HDMI_1", app=["x"])
    found = [None, "text", {"host": 7, "name": ["a"]}, {"host": "10.0.0.9"}]

    async def odd_search():
        return found
    with PageServer(tv=tv, discover=odd_search) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        open_tv_screen(page)
        page.click("#tv-input")
        assert page.locator("#tv-input-list button").all_inner_texts() == ["HDMI_1 Crystal Meet"]    # the one input that can be drawn (and it is the Pi's)
        server.devices.volume.level = 55                                                # the panels beside it go on drawing
        refresh_now(page)
        page.wait_for_function("document.getElementById('volume-level').textContent === '55'", timeout=5000)
        tv.paired = False
        refresh_now(page)
        page.wait_for_selector("#tv-pairing", state="visible", timeout=5000)
        page.click("#tv-find")
        page.wait_for_selector("#tv-pairing .tv-found li", timeout=5000)
        assert page.locator("#tv-pairing .tv-found li").count() == 1                    # only the entry with a usable host can be selected
        assert errors == []
        page.close()


@pytest.mark.parametrize("route, trigger, seconds, setup", [
    ("**/api/tv/control", '[data-tv="key:HOME"]', 30, lambda tv: None),
    ("**/api/tv/pair", "#tv-pair", 70, lambda tv: setattr(tv, "paired", False)),
    ("**/api/tv/discover", "#tv-find", 10, lambda tv: setattr(tv, "paired", False)),
])
def test_a_request_the_tv_never_answers_is_given_up_on_when_its_time_is_up(browser, route, trigger, seconds, setup):
    """The page waits as long as the agent might: a Turn on up to 23 seconds (30), the search 6 (10), a pairing a minute and some (70)."""
    tv = RemoteTv()
    setup(tv)
    with PageServer(tv=tv) as server:
        page = page_on_a_stopped_clock(browser, server)
        open_tv_screen(page)
        page.route(route, lambda r: None)                                        # the request is made, and never answered
        page.click(trigger)
        page.clock.fast_forward((seconds - 1) * 1000)
        page.wait_for_timeout(200)
        assert page.locator("#tv-words").is_hidden()
        page.clock.fast_forward(2_000)
        page.wait_for_selector("#tv-words:not([hidden])", timeout=5000)
        assert "did not answer" in page.locator("#tv-words").inner_text()
        page.close()


def test_a_page_that_cannot_reach_the_agent_says_so_and_not_a_browser_error(browser):
    with PageServer(tv=RemoteTv()) as server:
        page = tv_page(browser, server)
        page.route("**/api/tv/control", lambda r: r.abort())
        page.click('[data-tv="key:HOME"]')
        page.wait_for_selector("#tv-words:not([hidden])", timeout=5000)
        assert page.locator("#tv-words").inner_text() == "Can't reach the room"
        page.close()


def test_an_unpaired_room_shows_the_pairing_card_not_a_dead_remote(browser):
    tv = RemoteTv(paired=False)
    with PageServer(tv=tv, discover=StubSearch([BOARDROOM, LOBBY])) as server:
        page = tv_page(browser, server)
        seen = tv_requests(page)
        card = page.locator("#tv-pairing")
        assert card.is_visible() and page.locator("#tv-remote").is_hidden() and page.locator("#tv-not-set-up").is_hidden()
        assert page.locator("[data-tv]:visible").count() == 0                    # no key a person could tap to no effect
        assert card.locator("h2").inner_text() == "Pair the TV"
        assert " ".join(card.inner_text().split()).startswith(
            "Pair the TV This room is set up for the TV at 10.0.0.41. Tap Pair, then accept the request that appears on the TV, using the TV's own remote. You only do this once.")
        assert "Find TVs and Connect" in card.inner_text()
        assert status_line(page) == "TV at 10.0.0.41 · Not paired yet"
        assert card.locator("#tv-pair").inner_text().strip() == "Pair" and card.locator("#tv-find").inner_text().strip() == "Find TVs"
        page.click("#tv-pair")
        assert tv_calls(page, server, 1) == [("pair",)]
        page.wait_for_selector("#tv-remote", state="visible", timeout=5000)      # paired: the remote
        assert page.locator("#tv-pairing").is_hidden() and status_line(page) == "Boardroom TV · On · HDMI 1 (Crystal Meet)"
        assert seen == [("POST", "tv/pair", "application/json", "{}")]
        page.close()


def test_a_pairing_waits_for_the_person_at_the_tv_and_cancel_ends_it(browser):
    tv = RemoteTv(paired=False)
    tv.pair_gate = asyncio.Event()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        seen = tv_requests(page)
        page.click("#tv-pair")
        tv_calls(page, server, 1)
        page.wait_for_selector("#tv-pair-cancel", state="visible", timeout=5000)
        assert "Accept the request on the TV with its own remote" in page.locator("#tv-pairing").inner_text()
        assert page.locator("#tv-pair").is_hidden() and page.locator("#tv-find").is_hidden()   # one pairing at a time: nothing to tap but Cancel
        page.click("#tv-pair-cancel")
        assert tv_calls(page, server, 2) == [("pair",), ("forget",)]             # Cancel is the agent's Forget: it ends a waiting pairing at once
        page.wait_for_selector("#tv-pair", state="visible", timeout=5000)
        assert page.locator("#tv-pair-cancel").is_hidden() and page.locator("#tv-words").is_hidden()    # a pairing given up is no error
        assert [(m, r, body) for m, r, _, body in seen] == [("POST", "tv/pair", "{}"), ("POST", "tv/forget", "{}")]
        tv.pair_gate = None                                                      # and Pair can be tried again, and this time accepted
        page.click("#tv-pair")
        page.wait_for_selector("#tv-remote", state="visible", timeout=5000)
        page.close()


def test_a_pairing_that_fails_says_why_and_the_card_stays(browser):
    tv = RemoteTv(paired=False)
    tv.fail_pair = TvError("Nobody accepted the prompt on the TV within 60 seconds: tap Pair to try again")
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.click("#tv-pair")
        page.wait_for_selector("#tv-words:not([hidden])", timeout=5000)
        assert page.locator("#tv-words").inner_text() == "Nobody accepted the prompt on the TV within 60 seconds: tap Pair to try again"
        assert page.locator("#tv-pairing").is_visible() and page.locator("#tv-pair").is_enabled() and page.locator("#tv-pair-cancel").is_hidden()
        page.close()


def test_find_tvs_lists_the_tvs_with_their_addresses_and_tags_this_rooms_own(browser):
    with PageServer(tv=RemoteTv(paired=False), discover=StubSearch([BOARDROOM, LOBBY, UNNAMED])) as server:
        page = tv_page(browser, server)
        seen = tv_requests(page)
        card = page.locator("#tv-pairing")
        assert card.locator(".tv-found li").count() == 0
        page.click("#tv-find")
        page.wait_for_selector("#tv-pairing .tv-found li", timeout=5000)
        rows = card.locator(".tv-found li")
        assert rows.count() == 3 and "Found on the network" in card.inner_text()
        for row, name, address in zip(rows.all(), ["Boardroom TV", "Lobby TV", "LG TV"], ["10.0.0.41", "10.0.0.57", "10.0.0.60"]):
            assert row.locator(".tv-who").inner_text().startswith(name)
            assert row.locator(".tv-addr").inner_text() == address
        assert card.locator(".tv-found .tv-tag").count() == 1 and card.locator(".tv-found li").nth(0).locator(".tv-tag").count() == 1
        assert card.locator(".tv-found li:last-child .tv-addr").inner_text() == "10.0.0.60"
        assert card.locator(".tv-found button").count() == 3
        assert page.locator("#tv-pairing a").count() == 0
        assert "udhisapi" not in page.locator("#tv-pairing").inner_text()        # and the address of its description is nobody's business
        assert [(m, r) for m, r, *_ in seen] == [("GET", "tv/discover")]
        page.close()


def test_find_tvs_says_when_nothing_answers_and_when_the_search_fails(browser):
    search = StubSearch()
    with PageServer(tv=RemoteTv(paired=False), discover=search) as server:
        page = tv_page(browser, server)
        page.click("#tv-find")
        page.wait_for_selector("#tv-pairing .tv-found-label", timeout=5000)
        assert page.locator("#tv-pairing .tv-found").inner_text().startswith("No TVs answered")
        assert page.locator("#tv-pairing .tv-found li").count() == 0
        search.fail = TvError("The search for TVs could not be started: no route to the network")
        page.click("#tv-find")
        page.wait_for_selector("#tv-words:not([hidden])", timeout=5000)
        assert page.locator("#tv-words").inner_text() == "The search for TVs could not be started: no route to the network"
        assert page.locator("#tv-find").is_enabled()                             # and it can be tried again
        search.fail = None
        search.tvs = [dict(BOARDROOM)]
        page.click("#tv-find")
        page.wait_for_selector("#tv-pairing .tv-found li", timeout=5000)
        assert page.locator("#tv-words").is_hidden()
        page.close()


def test_what_the_search_finds_is_text_and_its_address_is_never_a_link(browser):
    attack = '<img src=x onerror="window.pwned = 1">'
    found = [{"host": "10.0.0.9", "name": attack, "location": "javascript:window.pwned = 1"}]
    with PageServer(tv=RemoteTv(paired=False), discover=StubSearch(found)) as server:
        page = tv_page(browser, server)
        page.click("#tv-find")
        page.wait_for_selector("#tv-pairing .tv-found li", timeout=5000)
        assert attack in page.locator("#tv-pairing .tv-found li").inner_text()
        assert page.locator("#tv-pairing img, #tv-pairing a, #tv-pairing .tv-found *[href]").count() == 0
        assert page.evaluate("window.pwned") is None
        page.close()


def test_a_room_with_no_tv_set_up_says_so_and_offers_find_tvs(browser):
    for tv in (None, RemoteTv(configured=False, paired=False)):                  # no TV service at all, and one that is not configured
        with PageServer(tv=tv, discover=StubSearch([BOARDROOM])) as server:
            page = tv_page(browser, server)
            card = page.locator("#tv-not-set-up")
            assert card.is_visible() and page.locator("#tv-remote").is_hidden() and page.locator("#tv-pairing").is_hidden()
            assert card.locator("h2").inner_text() == "No TV is set up for this room"
            assert "Tap Connect to save your selection" in card.inner_text()
            assert page.locator("#tv-pair").is_hidden()                          # nothing to pair yet
            assert page.locator("[data-tv]:visible").count() == 0
            assert status_line(page) == "TV · Not set up"
            page.click("#tv-find-unset")
            page.wait_for_selector("#tv-not-set-up .tv-found li", timeout=5000)
            assert card.locator(".tv-found li .tv-who").inner_text().startswith("Boardroom TV")
            assert card.locator(".tv-found li .tv-addr").inner_text() == "10.0.0.41"
            assert card.get_by_role("button", name="Connect", exact=True).is_enabled()
            assert card.locator(".tv-tag").count() == 0
            page.close()


def test_a_tv_that_no_longer_accepts_this_table_offers_forget_and_pair(browser):
    tv = RemoteTv(refused=True)
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        seen = tv_requests(page)
        card = page.locator("#tv-refused")
        assert card.is_visible() and page.locator("#tv-remote").is_hidden() and page.locator("#tv-pairing").is_hidden()
        assert "The TV no longer accepts this table: Forget TV, then Pair" in card.inner_text()
        assert card.locator("#tv-forget").inner_text().strip() == "Forget TV" and card.locator("#tv-refused-pair").inner_text().strip() == "Pair"
        assert status_line(page) == "Boardroom TV · Not accepting this table"
        page.click("#tv-forget")
        assert tv_calls(page, server, 1) == [("forget",)]
        page.wait_for_selector("#tv-pairing", state="visible", timeout=5000)     # refused stays set after Forget until the next pairing: read with paired
        assert page.locator("#tv-refused").is_hidden() and "10.0.0.41" in page.locator("#tv-pairing").inner_text()
        page.click("#tv-pair")
        page.wait_for_selector("#tv-remote", state="visible", timeout=5000)
        assert tv.calls == [("forget",), ("pair",)]
        assert [(m, r, body) for m, r, _, body in seen] == [("POST", "tv/forget", "{}"), ("POST", "tv/pair", "{}")]
        page.close()


def test_the_refused_card_has_pair_too_and_a_refusal_with_no_pairing_is_the_pairing_card(browser):
    with PageServer(tv=RemoteTv(refused=True)) as server:
        page = tv_page(browser, server)
        page.click("#tv-refused-pair")                                          # Pair without Forget: the TV shows its prompt, and a new key replaces the old
        page.wait_for_selector("#tv-remote", state="visible", timeout=5000)
        page.close()
    with PageServer(tv=RemoteTv(paired=False, refused=True)) as server:          # the flag outlives Forget: alone it is not the refused card
        page = tv_page(browser, server)
        assert page.locator("#tv-pairing").is_visible() and page.locator("#tv-refused").is_hidden()
        page.close()


def test_the_dpad_is_one_round_control_with_four_sectors_and_ok_in_the_middle(browser):
    with PageServer(tv=RemoteTv()) as server:
        page = tv_page(browser, server)
        box = page.locator(".tv-dpad").bounding_box()
        assert abs(box["width"] - box["height"]) < 1 and 290 <= box["width"] <= 310     # round: a square box, the mockup's 300
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

        def at(dx, dy):
            return page.evaluate("([x, y]) => { const e = document.elementFromPoint(x, y); return e && e.dataset ? e.dataset.tv || null : null; }", [cx + dx, cy + dy])
        assert [at(0, -100), at(100, 0), at(0, 100), at(-100, 0)] == ["key:UP", "key:RIGHT", "key:DOWN", "key:LEFT"]
        assert at(0, 0) == "key:ENTER" and at(40, 40) == "key:ENTER"             # OK is the middle (a circle of 116 px)
        assert [at(40, -100), at(-40, -100), at(100, 40), at(100, -40)] == ["key:UP", "key:UP", "key:RIGHT", "key:RIGHT"]   # the sectors are quarters of the disc
        assert [at(-140, -140), at(140, -140), at(140, 140), at(-140, 140)] == [None] * 4   # the corners of its box are past the rim: no key
        sector = page.locator('.tv-dpad [data-tv="key:LEFT"]')
        assert sector.get_attribute("role") == "button" and sector.get_attribute("aria-label") == "Left"
        page.close()


def test_the_dpads_sectors_take_the_keyboard_too(browser):
    """A sector is not a button to the browser: it is told it is one (role, tab stop, label), and Enter and Space press it."""
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.focus('.tv-dpad [data-tv="key:UP"]')
        page.keyboard.press("Enter")
        page.focus('.tv-dpad [data-tv="key:LEFT"]')
        page.keyboard.press(" ")
        assert tv_calls(page, server, 2) == [("control", "key", "UP"), ("control", "key", "LEFT")]
        page.close()


def test_two_lines_of_words_leave_every_key_where_it_was(browser):
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        where = lambda: page.evaluate("[...document.querySelectorAll('#tv-remote [data-tv]')].map(k => { const r = k.getBoundingClientRect(); return [k.dataset.tv, r.left, r.top]; })")
        resting = where()
        tv.fail_control = TvError(REFUSAL)
        page.click('[data-tv="digit:5"]')
        page.wait_for_selector("#tv-words:not([hidden])", timeout=5000)
        assert page.locator("#tv-words").bounding_box()["height"] > 30           # (it is two lines)
        assert where() == resting
        page.close()


def test_a_long_press_on_a_key_opens_no_menu_and_selects_nothing(browser):
    """The table's touch screen turns a long press into a context menu or a selection; a person holding Volume must get neither."""
    with PageServer(tv=RemoteTv()) as server:
        page = tv_page(browser, server)
        for selector in ('[data-tv="volume_up"]', '.tv-dpad [data-tv="key:UP"]', ".tv-ok"):
            assert page.evaluate("""(s) => { const e = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
                document.querySelector(s).dispatchEvent(e); return e.defaultPrevented; }""", selector), selector
        assert page.locator("#tv-remote").evaluate("r => getComputedStyle(r).userSelect") == "none"
        page.close()


def test_a_tv_that_lists_no_inputs_says_so_in_the_row(browser):
    with PageServer(tv=RemoteTv(inputs=[])) as server:
        page = tv_page(browser, server)
        page.click("#tv-input")
        assert page.locator("#tv-input-list button").count() == 0 and page.locator("#tv-input-list").inner_text() == "The TV lists no inputs"
        page.click("#tv-inputs-close")
        assert page.locator("#tv-keys").is_visible()
        page.close()


def test_a_dpad_sector_lights_while_it_is_pressed(browser):
    with PageServer(tv=RemoteTv()) as server:
        page = tv_page(browser, server)
        sector = page.locator('.tv-dpad [data-tv="key:UP"]')
        fill = lambda: sector.evaluate("s => getComputedStyle(s).fill")
        resting = fill()
        box = sector.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        pressed = fill()
        page.mouse.up()
        assert pressed != resting and pressed == "rgb(22, 36, 79)"               # navy, the room page's pressed colour
        page.close()


def test_the_remotes_columns_and_rows_line_up_and_the_top_row_ends_at_the_content_edge(browser):
    def box(page, selector):
        return page.locator(selector).bounding_box()

    for width, height in ((1280, 800), (1920, 1200)):
        with PageServer(tv=RemoteTv()) as server:
            page = tv_page(browser, server, width, height)
            assert abs(box(page, ".tv-dpad")["y"] - box(page, ".tv-numbers")["y"]) <= 1, (width, "the columns start together")
            nav, play = box(page, ".tv-nav"), box(page, ".tv-play")
            assert abs((nav["y"] + nav["height"]) - (play["y"] + play["height"])) <= 1, (width, "and end together")
            keys = box(page, "#tv-keys")
            last = page.locator("#tv-keys button").last.bounding_box()
            edge = box(page, "#open-options")
            assert abs((last["x"] + last["width"]) - (edge["x"] + edge["width"])) <= 1, (width, "the top row ends where the bar does")
            assert abs(box(page, "#tv-power")["x"] - box(page, "#back-from-tv")["x"]) <= 1
            assert abs(box(page, "#back-from-tv")["x"] - box(page, ".logo")["x"]) <= 1
            assert abs(keys["y"] + keys["height"] - box(page, ".tv-middle")["y"]) <= 40, "the keys sit right above the middle"
            page.close()


def failing(tv, words):
    tv.fail_control = TvError(words)
    return tv


def pairing(tv):
    tv.pair_gate = asyncio.Event()
    return tv


FIT_STATES = {
    "the remote": (lambda: RemoteTv(), lambda page, server: None),
    "the remote with its inputs open": (lambda: RemoteTv(), lambda page, server: page.click("#tv-input")),
    "the remote with a long refusal in words": (lambda: failing(RemoteTv(), REFUSAL + ", " + REFUSAL), lambda page, server: page.click('[data-tv="key:HOME"]')),
    "the remote with Power asked to confirm": (lambda: RemoteTv(), lambda page, server: page.click("#tv-power")),
    "the remote of a TV that is off": (lambda: RemoteTv(on=False, reachable=False, volume=None), lambda page, server: None),
    "the pairing card": (lambda: RemoteTv(paired=False), lambda page, server: None),
    "the pairing card with a list of TVs": (lambda: RemoteTv(paired=False), lambda page, server: page.click("#tv-find")),
    "the pairing card waiting for the person at the TV": (lambda: pairing(RemoteTv(paired=False)), lambda page, server: page.click("#tv-pair")),
    "the card of a room with no TV": (lambda: None, lambda page, server: None),
    "the card of a TV that refuses the table": (lambda: RemoteTv(refused=True), lambda page, server: None),
}


@pytest.mark.parametrize("width, height", [(1280, 800), (1920, 1200)])
@pytest.mark.parametrize("state", list(FIT_STATES))
def test_every_state_of_the_tv_view_fits_the_kiosk_without_scrolling(browser, state, width, height):
    make, reach = FIT_STATES[state]
    many = [{"host": f"10.0.0.{n}", "name": f"Lobby TV {n}", "location": ""} for n in range(41, 49)]   # a floor of TVs: the list scrolls in its card, not the page
    with PageServer(tv=make(), discover=StubSearch(many)) as server:
        page = tv_page(browser, server, width, height)
        reach(page, server)
        if "refusal" in state:
            page.wait_for_selector("#tv-words:not([hidden])", timeout=5000)
        if "list of TVs" in state:
            page.wait_for_selector("#tv-pairing .tv-found li", timeout=5000)
        if "waiting" in state:
            page.wait_for_selector("#tv-pair-cancel", state="visible", timeout=5000)
        page.wait_for_timeout(100)
        assert fits(page), state
        page.close()


def test_the_pairing_card_keeps_a_long_list_of_tvs_inside_itself_and_in_view(browser):
    many = [{"host": f"10.0.0.{n}", "name": f"Lobby TV {n}", "location": ""} for n in range(41, 61)]
    with PageServer(tv=RemoteTv(paired=False), discover=StubSearch(many)) as server:
        page = tv_page(browser, server)
        page.click("#tv-find")
        page.wait_for_selector("#tv-pairing .tv-found li", timeout=5000)
        card, main = page.locator("#tv-pairing").bounding_box(), page.locator("main").bounding_box()
        assert card["y"] + card["height"] <= main["y"] + main["height"] + 1      # the card ends inside the screen
        assert page.locator("#tv-pair").is_visible() and page.locator("#tv-pair").bounding_box()["y"] < main["y"] + main["height"]
        assert page.locator("#tv-pairing .tv-found-list").evaluate("l => l.scrollHeight > l.clientHeight")   # and the list is what scrolls
        page.close()


LIST_ROWS = """card => { const list = document.querySelector(card + ' .tv-found-list'), li = list.querySelector('li');
    return list.clientHeight / li.getBoundingClientRect().height; }"""


@pytest.mark.parametrize("card", ["#tv-pairing", "#tv-not-set-up"])
@pytest.mark.parametrize("width, height", [(1024, 700), (1280, 720), (1280, 760), (1024, 768), (1280, 800)])
def test_the_list_of_tvs_a_search_found_has_two_rows_at_least_at_every_size_of_the_kiosk(browser, card, width, height):
    """At 1024 by 700 and 1280 by 720 the card's hint under the list took the room of a row and a half, and the list was one row high (it
    scrolls in its card). The hint repeats the guide: at the screens that are short it goes while a list is shown, and the list has the rows."""
    many = [{"host": f"10.0.0.{n}", "name": f"Lobby TV {n}", "location": ""} for n in range(41, 49)]
    with PageServer(tv=RemoteTv(paired=False) if card == "#tv-pairing" else None, discover=StubSearch(many)) as server:
        page = tv_page(browser, server, width, height)
        laid_out(page)
        hint = page.locator(f"{card} .tv-hint")
        assert hint.is_visible()                                                     # (with no list on the card it is the card's instruction)
        page.click(f"{card} .tv-find")
        page.wait_for_selector(f"{card} .tv-found li", timeout=5000)
        laid_out(page)
        rows = page.evaluate(LIST_ROWS, card)
        assert rows >= 2, (card, width, height, rows)
        assert hint.is_hidden() == (height <= 760), (card, width, height)             # gone on the short screens only
        assert fits(page) and page.locator("#tv-modes").is_visible()
        page.close()


@pytest.mark.parametrize("state", ["the remote", "the remote with its inputs open", "the pairing card with a list of TVs", "the card of a TV that refuses the table"])
def test_the_tv_view_stacks_on_a_phone_without_scrolling_sideways(browser, state):
    make, reach = FIT_STATES[state]
    with PageServer(tv=make(), discover=StubSearch([BOARDROOM, LOBBY])) as server:
        page = tv_page(browser, server, 390, 844)
        reach(page, server)
        page.wait_for_timeout(200)
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"), state
        for key in page.locator("#tv-remote [data-tv]:visible").all():          # every key is on the screen, left to right
            box = key.bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= 390, key.get_attribute("data-tv")
        page.close()


def test_every_icon_in_the_tv_view_draws_at_the_size_of_its_text_and_costs_no_height(browser):
    with PageServer(tv=RemoteTv(), discover=StubSearch([BOARDROOM])) as server:
        page = tv_page(browser, server)
        assert shown_icons(page) == ["#icon-tv", "#icon-settings", "#icon-back", "#icon-locate", "#icon-x-circle", "#icon-power", "#icon-monitor", "#icon-home", "#icon-menu",
                                     "#icon-volume", "#icon-up", "#icon-down",
                                     "#icon-return", "#icon-exit", "#icon-info", "#icon-list",
                                     "#icon-rewind", "#icon-play", "#icon-pause", "#icon-stop", "#icon-fast-forward",
                                     "#icon-pointer", "#icon-monitor", "#icon-touchpad"]
        page.click("#tv-input")
        assert shown_icons(page) == ["#icon-tv", "#icon-settings", "#icon-back", "#icon-locate", "#icon-x-circle", "#icon-check", "#icon-exit",
                                     "#icon-return", "#icon-exit", "#icon-info", "#icon-list",
                                     "#icon-rewind", "#icon-play", "#icon-pause", "#icon-stop", "#icon-fast-forward",
                                     "#icon-pointer", "#icon-monitor", "#icon-touchpad"]
        page.close()
    with PageServer(tv=RemoteTv(paired=False), discover=StubSearch([BOARDROOM])) as server:
        page = tv_page(browser, server)
        assert shown_icons(page) == ["#icon-settings", "#icon-back", "#icon-locate", "#icon-tv", "#icon-locate", "#icon-touchpad"]    # (the touchpad alone: no TV to point at)
        page.close()
    with PageServer(tv=RemoteTv(refused=True)) as server:
        page = tv_page(browser, server)
        assert shown_icons(page) == ["#icon-tv", "#icon-settings", "#icon-back", "#icon-locate", "#icon-x-circle", "#icon-tv", "#icon-touchpad"]
        page.close()
    with PageServer(tv=RemoteTv(on=False, reachable=False, volume=None)) as server:
        page = tv_page(browser, server)
        assert shown_icons(page)[:7] == ["#icon-tv", "#icon-settings", "#icon-back", "#icon-power", "#icon-locate", "#icon-x-circle", "#icon-power"]   # TV Remote, Options, Back, Turn on, Change TV, Forget TV, and Power
        assert shown_icons(page)[-3:] == ["#icon-pointer", "#icon-monitor", "#icon-touchpad"]                              # (Pointer works on a TV that is off: what it cannot take comes back in words)
        page.close()
    with PageServer() as server:
        page = tv_page(browser, server)
        assert shown_icons(page) == ["#icon-settings", "#icon-back", "#icon-locate", "#icon-touchpad"]
        page.close()


def test_a_tv_view_whose_icon_sprite_does_not_come_keeps_every_key_readable(browser):
    with PageServer(tv=RemoteTv()) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.route("**/static/icons.svg", SPRITE_ANSWERS["is missing"])
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        open_tv_screen(page)
        unreadable = page.evaluate("""() => [...document.querySelectorAll('#tv-remote button')].filter(b => !b.innerText.trim()
            && getComputedStyle(b, '::after').content === 'none' && !b.querySelector('i')).map(b => b.dataset.tv)""")
        assert unreadable == []                                                  # a key with a picture and no words says its name instead
        glyphs = page.evaluate("[...document.querySelectorAll('.tv-dpad .tv-glyph')].map(g => getComputedStyle(g).display)")
        assert len(glyphs) == 4 and "none" not in glyphs                         # the disc's arrows too: a glyph for each, where the picture is missing
        assert fits(page)
        page.close()


def test_the_remote_wears_the_same_colours_in_a_meeting_as_outside_one(browser):
    """The meeting state outlines every button in the page, filled ones included: the remote's own colours must win over that."""
    colours = []
    for meeting in (False, True):
        with PageServer(tv=RemoteTv(on=False, reachable=False, volume=None)) as server:
            page = open_page(browser, server)
            if meeting:
                join_by_id(page)
                meeting_tv_button(page).click()
            else:
                open_tv_screen(page)
            page.wait_for_selector("#tv-remote", state="visible")
            colours.append(page.evaluate("""() => ['#tv-power', '[data-tv="key:HOME"]'].map(s => { const c = getComputedStyle(document.querySelector(s));
                return [c.backgroundColor, c.borderTopColor, c.color]; })"""))
            page.close()
    assert colours[0] == colours[1] == [["rgb(27, 82, 229)", "rgb(27, 82, 229)", "rgb(255, 255, 255)"],      # Power, filled, as the TV is off
                                        ["rgba(0, 0, 0, 0)", "rgb(189, 206, 255)", "rgb(255, 255, 255)"]]   # a quiet key: outlined in periwinkle


def test_the_remotes_buttons_are_not_replaced_while_the_status_polls_or_the_state_changes(browser):
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.click("#tv-input")
        page.evaluate("""() => { window.kept = [...document.querySelectorAll('#tv-remote button, #tv-remote [data-tv], #tv-input-list button, #tv-turn-on, #tv-modes button, #tv-trackpad, #touchpad button')]; }""")
        tv.shown.update(muted=True, volume=33, input="HDMI_2", app="com.webos.app.hdmi2")   # (somebody used the TV's own remote, too)
        refresh_now(page)
        page.wait_for_function("document.getElementById('tv-level').textContent === '33'", timeout=5000)
        page.wait_for_timeout(2300)                                              # and one of the page's own polls
        assert page.evaluate("window.kept.every(n => n.isConnected)")            # a finger that went down on one finds it still there at the lift
        assert page.locator("#tv-mute").get_attribute("aria-pressed") == "true"  # and it shows the new state, in place
        page.close()


# --- the TV remote, fix round 1: Stop, Forget TV on the remote, re-pairing from the refused card, the remote's width, the guards ---

def test_the_playback_row_has_stop_between_pause_and_fast_forward(browser):
    with PageServer(tv=RemoteTv()) as server:
        page = tv_page(browser, server)
        assert page.locator(".tv-transport [data-tv]").evaluate_all("ks => ks.map(k => k.dataset.tv)") == \
            ["key:REWIND", "key:PLAY", "key:PAUSE", "key:STOP", "key:FASTFORWARD"]
        stop = page.locator('[data-tv="key:STOP"]')
        assert stop.get_attribute("aria-label") == "Stop" and stop.locator("svg use").get_attribute("href") == "#icon-stop"
        stop.click()
        assert tv_calls(page, server, 1) == [("control", "key", "STOP")]
        page.close()


def test_forget_tv_on_the_remote_needs_a_second_tap_like_power_off(browser):
    """A quiet Forget TV at the right end of the head row: the first tap turns it amber ("Tap again to forget the TV") for five seconds and asks
    nothing of the TV; the second posts /api/tv/forget, and the table is a room with a TV to pair again."""
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = page_on_a_stopped_clock(browser, server)
        open_tv_screen(page)
        seen = tv_requests(page)
        forget = page.locator("#tv-forget-head")
        assert forget.is_visible() and forget.inner_text().strip() == "Forget TV" and forget.locator("svg use").get_attribute("href") == "#icon-x-circle"
        head, button = page.locator(".tv-head").bounding_box(), forget.bounding_box()
        assert button["x"] + button["width"] > head["x"] + head["width"] - 2         # at the right end of the head row, where the content ends
        resting = forget.bounding_box()
        forget.click()
        page.wait_for_timeout(200)
        assert tv.calls == [] and seen == []                                         # the first tap asks nothing
        assert forget.inner_text().replace("\n", " ").strip() == "Tap again to forget the TV"
        assert page.evaluate("getComputedStyle(document.getElementById('tv-forget-head')).borderTopColor") == AMBER
        assert forget.bounding_box() == resting and fits(page)                       # as big as it was: nothing moves
        page.clock.fast_forward(5_100)                                               # nobody tapped again: it goes back
        page.wait_for_timeout(200)
        assert forget.inner_text().strip() == "Forget TV" and tv.calls == []
        forget.click()
        page.clock.run_for(600)                                                      # (a person's second tap: after more than half a second)
        forget.click()                                                               # the second tap, within five seconds
        assert tv_calls(page, server, 1) == [("forget",)]
        assert [(m, r, body) for m, r, _, body in seen] == [("POST", "tv/forget", "{}")]
        page.wait_for_selector("#tv-pairing", state="visible", timeout=5000)         # forgotten: the pairing card, and no Forget TV on it
        assert forget.is_hidden() and status_line(page) == "TV at 10.0.0.41 \u00b7 Not paired yet"
        page.close()


def test_a_bounced_double_tap_on_forget_tv_does_not_forget_the_tv(browser):
    """The same rule as Power's: the head's Forget TV ignores a tap for half a second after it is armed, so that a touch that is reported
    twice does not forget the pairing (which costs the table its TV until the TV's own remote has accepted a new one)."""
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = page_on_a_stopped_clock(browser, server)
        open_tv_screen(page)
        seen = tv_requests(page)
        forget = page.locator("#tv-forget-head")
        forget.click()                                                               # armed
        page.clock.run_for(150)
        forget.click()                                                               # the bounce, 150 ms after the first tap
        page.wait_for_timeout(200)
        assert forget.inner_text().replace("\n", " ").strip() == "Tap again to forget the TV" and tv.calls == [] and seen == []
        page.clock.run_for(550)                                                      # 700 ms after the first tap
        forget.click()
        assert tv_calls(page, server, 1) == [("forget",)]                            # the person's second tap confirms
        assert [(m, r, body) for m, r, _, body in seen] == [("POST", "tv/forget", "{}")]
        page.close()


def test_forget_tv_is_on_the_remote_only_waits_for_a_turn_on_and_goes_with_the_screen(browser):
    tv = RemoteTv(on=False, reachable=False, volume=None)
    tv.gate = asyncio.Event()
    with PageServer(tv=tv) as server:
        page = page_on_a_stopped_clock(browser, server)
        open_tv_screen(page)
        forget = page.locator("#tv-forget-head")
        assert forget.is_visible() and forget.is_enabled()                           # a TV that is off can be forgotten too
        forget.click()                                                               # armed ...
        assert forget.inner_text().replace("\n", " ").strip() == "Tap again to forget the TV"
        page.click("#back-from-tv")                                                  # ... and the screen is left: the next person finds it asleep
        page.click("#open-tv-options")
        assert forget.inner_text().strip() == "Forget TV"
        page.click("#tv-turn-on")                                                    # a Turn on is with the TV: nothing is forgotten meanwhile
        page.wait_for_function("document.getElementById('tv-status').textContent.includes('Turning on')", timeout=5000)
        assert forget.is_disabled()
        server.release(tv.gate)
        page.wait_for_function("document.getElementById('tv-status').textContent.includes('HDMI 1')", timeout=5000)
        assert forget.is_enabled()
        page.close()
    for make in (lambda: RemoteTv(paired=False), lambda: RemoteTv(refused=True), lambda: None):     # the cards have their own words and buttons
        with PageServer(tv=make()) as server:
            page = tv_page(browser, server)
            assert page.locator("#tv-forget-head").is_hidden()
            page.close()


def test_a_forget_the_agent_refuses_says_why_and_flashes_the_key(browser):
    tv = RemoteTv()
    tv.fail_forget = TvError("The TV could not be forgotten: its key could not be removed from /var/lib/croom/tv-client-key")
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.click("#tv-forget-head")
        page.wait_for_timeout(600)                                                   # (a person's second tap: after more than half a second)
        page.click("#tv-forget-head")
        page.wait_for_selector("#tv-words:not([hidden])", timeout=5000)
        assert page.locator("#tv-words").inner_text().startswith("The TV could not be forgotten")
        assert page.locator("#tv-remote").is_visible() and page.locator("#tv-forget-head").is_enabled()     # still the remote, and it can be tried again
        page.close()


def test_a_pairing_from_the_refused_card_shows_the_wait_and_a_cancel(browser):
    """Pair on the refused card waits for the person at the TV as the pairing card does: the same words, "Pairing..." in the status line, and a
    Cancel, which ends the wait at once (it is the agent's Forget). A pairing that is given up is no error."""
    tv = RemoteTv(refused=True)
    tv.pair_gate = asyncio.Event()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        seen = tv_requests(page)
        page.click("#tv-refused-pair")
        tv_calls(page, server, 1)
        page.wait_for_selector("#tv-refused-cancel", state="visible", timeout=5000)
        card = page.locator("#tv-refused")
        assert "Accept the request on the TV with its own remote" in card.inner_text()
        assert "no longer accepts this table" not in card.inner_text()               # the wait takes the place of the old words
        assert status_line(page) == "Boardroom TV \u00b7 Pairing\u2026"
        assert page.locator("#tv-refused-pair").is_hidden() and page.locator("#tv-forget").is_visible()
        page.click("#tv-refused-cancel")
        assert tv_calls(page, server, 2) == [("pair",), ("forget",)]
        page.wait_for_selector("#tv-pairing", state="visible", timeout=5000)         # the key is gone: the pairing card, ready for another try
        assert page.locator("#tv-words").is_hidden()                                 # the pairing's "stopped" answer is no news
        assert [(m, r, body) for m, r, _, body in seen] == [("POST", "tv/pair", "{}"), ("POST", "tv/forget", "{}")]
        page.close()


def test_forget_tv_during_a_pairing_from_the_refused_card_ends_it_without_an_error(browser):
    tv = RemoteTv(refused=True)
    tv.pair_gate = asyncio.Event()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.click("#tv-refused-pair")
        page.wait_for_selector("#tv-refused-cancel", state="visible", timeout=5000)
        page.click("#tv-forget")
        assert tv_calls(page, server, 2) == [("pair",), ("forget",)]
        page.wait_for_selector("#tv-pairing", state="visible", timeout=5000)
        page.wait_for_timeout(300)
        assert page.locator("#tv-words").is_hidden(), page.locator("#tv-words").inner_text()    # not "Pairing ... was stopped before it finished"
        page.close()


def test_a_pairing_from_the_refused_card_that_is_accepted_shows_the_remote(browser):
    tv = RemoteTv(refused=True)
    tv.pair_gate = asyncio.Event()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.click("#tv-refused-pair")
        page.wait_for_selector("#tv-refused-cancel", state="visible", timeout=5000)
        server.release(tv.pair_gate)                                                 # the person accepted it on the TV
        page.wait_for_selector("#tv-remote", state="visible", timeout=5000)
        assert page.locator("#tv-refused").is_hidden() and status_line(page) == "Boardroom TV \u00b7 On \u00b7 HDMI 1 (Crystal Meet)"
        page.close()


LAYOUT_PROBE = """() => { const d = document.documentElement, vis = (e) => e.getClientRects().length;
  const keys = [...document.querySelectorAll('#tv-remote [data-tv], #tv-inputs button, .tv-head button, #tv-modes button')].filter(vis)
    .map(e => ({ id: e.dataset.tv || e.id || e.textContent.trim().slice(0, 12), el: e, r: e.getBoundingClientRect() }));
  const outside = keys.filter(k => k.r.left < -0.5 || k.r.right > d.clientWidth + 0.5 || k.r.top < -0.5 || k.r.bottom > innerHeight + 0.5).map(k => k.id);
  const overlapping = [];
  for (let i = 0; i < keys.length; i++) for (let j = i + 1; j < keys.length; j++) { const a = keys[i].r, b = keys[j].r;
    const w = Math.min(a.right, b.right) - Math.max(a.left, b.left), h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    if (w > 1 && h > 1 && !(keys[i].el.closest('.tv-dpad') && keys[j].el.closest('.tv-dpad'))) overlapping.push([keys[i].id, keys[j].id]); }
  const modes = document.getElementById('tv-modes').getBoundingClientRect().top;
  const above = [...keys.filter(k => !k.el.closest('#tv-modes')).map(k => k.r),     // the keys, and what they sit in: the columns, the trackpad, the card
    ...['.tv-left', '.tv-right', '#tv-trackpad', '.tv-card'].map(s => document.querySelector(s + ':not([hidden])')).filter(e => e && vis(e)).map(e => e.getBoundingClientRect())];
  const rows = ['.tv-nav', '.tv-play', '.tv-numbers'].map(s => { const e = document.querySelector(s).getBoundingClientRect(), c = document.querySelector(s).closest('.tv-left, .tv-right').getBoundingClientRect();
    return [s, e.width <= c.width + 0.5]; });
  return { outside, overlapping, belowTheKeys: Math.max(...above.map(r => r.bottom)) - modes, rowsInTheirColumns: rows.filter(([s, ok]) => !ok).map(([s]) => s) }; }"""


def assert_the_remote_is_laid_out(page, where):
    """Every key on the screen (the pointer modes' too), none on another, nothing above the modes row reaching into it, each row inside its column, and no scrolling."""
    found = page.evaluate(LAYOUT_PROBE)
    assert found["outside"] == [], (where, "off the screen")
    assert found["overlapping"] == [], (where, "overlapping")
    assert found["belowTheKeys"] <= 0.5, (where, "runs into the pointer modes' row by", found["belowTheKeys"])
    assert found["rowsInTheirColumns"] == [], (where, "wider than its column")
    assert fits(page), (where, "scrolls")


@pytest.mark.parametrize("width, height", [(1024, 768), (1100, 800), (1180, 800), (1250, 800), (1280, 800), (1920, 1200)])
def test_the_remote_with_stop_fits_from_1024_to_1920_wide_with_every_key_on_the_screen(browser, width, height):
    """The nav row (Back, Exit, Info, Guide) and the playback row (five keys and the four colours) are wider than the half of the screen they
    are in at 1024 wide; they give way, and the d-pad and the number pad give way in height, so that nothing is off the screen, on another key,
    or in the row of the pointer modes. The widest state is checked too: the sound off ("Unmute"), Power and Forget TV asking to confirm."""
    for label, tv, reach in (
            ("the remote", RemoteTv(), lambda page: None),
            ("the widest labels", RemoteTv(muted=True), lambda page: (page.click("#tv-power"), page.click("#tv-forget-head"))),
            ("the inputs open", RemoteTv(inputs=[{"id": f"HDMI_{n}", "label": f"HDMI {n}"} for n in (1, 2, 3, 4)]), lambda page: page.click("#tv-input")),
            ("a TV that is off", RemoteTv(on=False, reachable=False, volume=None), lambda page: None)):
        with PageServer(tv=tv) as server:
            page = tv_page(browser, server, width, height)
            reach(page)
            page.wait_for_timeout(150)
            assert_the_remote_is_laid_out(page, f"{label} at {width} by {height}")
            page.close()


def test_the_dpad_shrinks_as_a_square_when_the_screen_is_short_and_is_the_mockups_300_when_it_is_not(browser):
    sizes = {}
    for width, height in ((1280, 800), (1024, 768), (1280, 720)):
        with PageServer(tv=RemoteTv()) as server:
            page = tv_page(browser, server, width, height)
            box = page.locator(".tv-dpad").bounding_box()
            assert abs(box["width"] - box["height"]) < 1, (width, height, "round: a square box")
            sizes[(width, height)] = round(box["height"])
            ok = page.locator(".tv-ok").bounding_box()
            assert abs(ok["width"] / box["width"] - 0.3867) < 0.01                    # OK keeps its share of the disc
            page.close()
    assert sizes[(1280, 800)] == 300 and sizes[(1024, 768)] < 300 and sizes[(1280, 720)] < sizes[(1024, 768)] + 100, sizes


@pytest.mark.parametrize("width, height", [(1024, 768), (1100, 800), (1280, 800)])
def test_the_nav_and_playback_rows_keep_their_keys_big_enough_to_press_at_every_width(browser, width, height):
    with PageServer(tv=RemoteTv()) as server:
        page = tv_page(browser, server, width, height)
        for key in page.locator(".tv-nav [data-tv], .tv-play [data-tv]").all():
            box = key.bounding_box()
            assert box["width"] >= 40 and box["height"] >= 48, (key.get_attribute("data-tv"), box)
        page.close()


def test_a_finger_down_on_an_input_is_not_lost_when_the_tv_changes_the_input_under_it(browser):
    """The row of inputs is drawn again only when its list changes. When only the input the TV shows changes (somebody used the TV's own
    remote) the check and the "current" look move in place, so the pick under the finger is the same button at the lift."""
    inputs = [{"id": f"HDMI_{n}", "label": f"HDMI {n}"} for n in (1, 2, 3)]
    tv = RemoteTv(inputs=inputs)
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.click("#tv-input")
        seen = tv_requests(page)
        pick = page.locator("#tv-input-list button:has-text('HDMI 2')")
        box = pick.bounding_box()
        page.evaluate("window.picks = [...document.querySelectorAll('#tv-input-list button')]")
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        tv.shown.update(input="HDMI_3", app="com.webos.app.hdmi3")                   # the TV changes input under the finger
        refresh_now(page)
        page.wait_for_function("document.querySelectorAll('#tv-input-list button')[2].querySelector('svg')", timeout=5000)
        assert page.evaluate("window.picks.every(n => n.isConnected)")               # not one pick was replaced
        assert [b.get_attribute("aria-pressed") for b in page.locator("#tv-input-list button").all()] == ["false", "false", "true"]
        assert page.locator("#tv-input-list button svg").count() == 1                # the check moved: one of them, on the input showing
        page.mouse.up()
        assert tv_calls(page, server, 1) == [("control", "input", "HDMI_2")]
        page.wait_for_timeout(200)
        assert [(m, r, body) for m, r, _, body in seen] == [("POST", "tv/control", '{"name":"input","value":"HDMI_2"}')]    # one POST, for the one that was pressed
        page.close()


def test_a_finger_down_on_the_check_of_the_input_that_was_current_is_not_lost_when_the_input_changes(browser):
    """The check is the picture of the input the TV shows, and it comes off that input's button when the TV changes input. A finger that went
    down on the picture must still find something to lift on, or the browser drops the tap: the row's pictures take no press of their own, so
    the button does."""
    tv = RemoteTv(inputs=[{"id": f"HDMI_{n}", "label": f"HDMI {n}"} for n in (1, 2, 3)])               # (it shows HDMI 1)
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.click("#tv-input")
        seen = tv_requests(page)
        check = page.locator("#tv-input-list button:has-text('HDMI 1') svg")
        assert check.count() == 1
        box = check.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()                                                            # the finger is on the check
        tv.shown.update(input="HDMI_3", app="com.webos.app.hdmi3")                   # and the TV changes input under it
        refresh_now(page)
        page.wait_for_function("document.querySelectorAll('#tv-input-list button')[2].querySelector('svg')", timeout=5000)
        assert page.locator("#tv-input-list button:has-text('HDMI 1') svg").count() == 0   # the check has gone from the button that was pressed
        page.mouse.up()
        assert tv_calls(page, server, 1) == [("control", "input", "HDMI_1")]         # the tap was not lost
        page.wait_for_timeout(200)
        assert [(m, r, body) for m, r, _, body in seen] == [("POST", "tv/control", '{"name":"input","value":"HDMI_1"}')]
        page.close()


def test_the_row_of_inputs_is_drawn_again_when_the_tv_lists_another_input(browser):
    tv = RemoteTv(inputs=[{"id": "HDMI_1", "label": "HDMI 1"}, {"id": "HDMI_2", "label": "HDMI 2"}])
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.click("#tv-input")
        tv.shown.update(inputs=[{"id": "HDMI_1", "label": "HDMI 1"}, {"id": "HDMI_2", "label": "HDMI 2"}, {"id": "HDMI_3", "label": "HDMI 3"}])
        refresh_now(page)
        page.wait_for_function("document.querySelectorAll('#tv-input-list button').length === 3", timeout=5000)
        assert page.locator("#tv-input-list button").all_inner_texts() == ["HDMI 1 Crystal Meet", "HDMI 2", "HDMI 3"]
        page.close()


def test_a_poll_that_was_on_its_way_does_not_undo_the_answer_to_a_tap(browser):
    """deviceEpoch.tv: the status the agent gave before a tap was answered reaches the page after it, and must not put the TV back as it was."""
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.wait_for_timeout(300)
        held = []

        def hold_the_first_poll(route):
            if held:
                route.continue_()
                return
            held.append((route, route.fetch()))                                      # the agent answers now, with the TV as it is now (not muted) ...

        page.route("**/api/status", hold_the_first_poll)
        page.evaluate("() => { window.croomPage.refreshStatus(); }")                  # (not waited for: the poll is held until the test lets it go)
        for _ in range(100):
            if held:
                break
            page.wait_for_timeout(20)
        assert held, "the poll never went out"
        page.click("#tv-mute")                                                       # ... the tap is answered: muted ...
        page.wait_for_function("document.getElementById('tv-mute').getAttribute('aria-pressed') === 'true'", timeout=5000)
        route, response = held[0]
        route.fulfill(response=response)                                             # ... and the older poll arrives late
        page.wait_for_timeout(500)
        assert page.get_attribute("#tv-mute", "aria-pressed") == "true" and page.locator("#tv-mute").inner_text().strip() == "Unmute"
        page.close()


def test_a_poll_that_shows_the_tv_on_does_not_free_the_keys_while_turn_on_is_still_with_the_agent(browser):
    tv = RemoteTv(on=False, reachable=False, volume=None)
    tv.gate = asyncio.Event()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.click("#tv-turn-on")
        tv_calls(page, server, 1)
        page.wait_for_function("document.getElementById('tv-status').textContent.includes('Turning on')", timeout=5000)
        tv.shown.update(on=True, reachable=True)                                     # the TV has come on, and the agent has not answered yet
        tv.shown.pop("volume")
        refresh_now(page)
        page.wait_for_timeout(400)
        assert status_line(page) == "Boardroom TV \u00b7 Turning on\u2026"             # the poll says on; the page goes on waiting for the answer
        assert all(k.is_disabled() for k in page.locator("#tv-remote [data-tv]").all())
        server.release(tv.gate)
        page.wait_for_function("document.getElementById('tv-status').textContent.includes('HDMI 1')", timeout=5000)
        assert all(k.is_enabled() for k in page.locator("#tv-remote [data-tv]").all())
        page.close()


def test_the_words_of_a_turn_on_that_fails_after_the_person_left_are_not_left_for_the_next_visit(browser):
    tv = RemoteTv(on=False, reachable=False, volume=None)
    tv.gate = asyncio.Event()
    tv.fail_control = TvError(WONT_TURN_ON)
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.click("#tv-turn-on")
        tv_calls(page, server, 1)
        page.click("#back-from-tv")                                                  # the person leaves while the Turn on is still with the TV
        server.release(tv.gate)                                                      # and it fails now
        page.wait_for_timeout(500)
        page.click("#open-tv-options")                                               # the next person opens the remote
        page.wait_for_timeout(300)
        assert page.locator("#tv-words").is_hidden(), page.locator("#tv-words").inner_text()
        page.close()


def test_a_double_tap_on_find_tvs_makes_one_search(browser):
    search = StubSearch([BOARDROOM])
    search.gate = asyncio.Event()
    with PageServer(tv=RemoteTv(paired=False), discover=search) as server:
        page = tv_page(browser, server)
        seen = tv_requests(page)
        page.locator("#tv-find").dblclick()
        page.wait_for_function("document.getElementById('tv-find').disabled", timeout=5000)
        page.wait_for_timeout(300)
        assert search.calls == 1 and [(m, r) for m, r, *_ in seen] == [("GET", "tv/discover")]      # the second tap found a search already looking
        page.evaluate("() => { const b = document.getElementById('tv-find'); b.disabled = false; b.click(); }")    # and a tap that gets past the key's own
        page.wait_for_timeout(300)                                                   # disabling (a render that has not run yet) is turned away by the search itself
        assert search.calls == 1 and len(seen) == 1
        server.release(search.gate)
        page.wait_for_selector("#tv-pairing .tv-found li", timeout=5000)
        assert page.locator("#tv-find").is_enabled() and search.calls == 1
        page.close()


@pytest.mark.parametrize("app, said", [
    ("com.webos.app.livetv", "Live TV"), ("netflix", "Netflix"), ("youtube.leanback.v4", "YouTube"), ("amazon", "Prime Video"),
    ("com.webos.app.settings", "Settings"), ("com.webos.app.home", "Home screen"),
    ("com.example.unheard.of", "an app"), ("com.webos.app.hdmi9", "an app")])
def test_the_status_line_names_the_apps_it_knows_and_calls_any_other_an_app(browser, app, said):
    """An app's id is the TV's word, not a person's: the few that are known have names, and the rest are "an app"."""
    with PageServer(tv=RemoteTv(input=None, app=app)) as server:
        page = tv_page(browser, server)
        assert status_line(page) == f"Boardroom TV \u00b7 On \u00b7 {said}"
        assert app not in page.locator("#tv-status").inner_text()
        page.close()


class RealTvServer(PageServer):
    """The page's server with a real TvService (on the TV tests' fake library client) as the room's TV: its real status and its real controls."""

    def __init__(self, service, **kwargs):
        super().__init__(tv=service, **kwargs)

    async def _main(self):
        await self.tv.start()
        try:
            await super()._main()
        finally:
            await self.tv.stop()


def test_the_remote_works_through_the_real_tv_service_and_the_library_gets_its_calls_as_it_wants_them(browser, tmp_path):
    """A digit, a key and the volume, through the real routes and the real TvService: the library is asked for button("5"), button("HOME") and
    volume_up(), and the level the page shows is the one the TV reports after it."""
    from tests.unit.tv.fake_client import FakeTvClient
    from tests.unit.tv.helpers import DoingTv, service_for
    FakeTvClient.instances.clear()
    try:
        service = service_for(tmp_path, factory=DoingTv, key="key-9")
        with RealTvServer(service) as server:
            page = tv_page(browser, server)
            page.wait_for_function("document.getElementById('tv-level').textContent.trim() === '24'", timeout=8000)
            assert status_line(page) == "Boardroom TV \u00b7 On \u00b7 HDMI 1 (Crystal Meet) \u00b7 Channel 7"      # the real service's own status
            client = FakeTvClient.instances[-1]
            commands = lambda: [c for c in client.calls if c[0] in ("button", "volume_up", "volume_down", "set_mute")]
            for selector, wanted in (('[data-tv="digit:5"]', ("button", "5")), ('[data-tv="key:HOME"]', ("button", "HOME")),
                                     ('[data-tv="volume_up"]', ("volume_up",)), ('[data-tv="key:STOP"]', ("button", "STOP"))):
                before = len(commands())
                page.click(selector)
                for _ in range(200):
                    if len(commands()) > before:
                        break
                    page.wait_for_timeout(25)
                assert commands()[before:] == [wanted], (selector, commands())
            page.wait_for_function("document.getElementById('tv-level').textContent.trim() === '25'", timeout=5000)     # volume_up, read back
            assert page.locator("#tv-words").is_hidden()                              # no refusal anywhere
            page.close()
    finally:
        FakeTvClient.instances.clear()


SURFACE_PROBE = """() => { const vis = (e) => e.getClientRects().length, W = document.documentElement.clientWidth;
  const things = [...document.querySelectorAll('#touchpad button, #touchpad-title, #touchpad-sub, #touchpad-link')].filter(vis).map(e => ({ id: e.id, r: e.getBoundingClientRect() }));
  const off = things.filter(t => t.r.left < -0.5 || t.r.right > W + 0.5 || t.r.top < -0.5 || t.r.bottom > innerHeight + 0.5).map(t => t.id);
  const overlapping = [];
  for (let i = 0; i < things.length; i++) for (let j = i + 1; j < things.length; j++) { const a = things[i].r, b = things[j].r;
    if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1) overlapping.push([things[i].id, things[j].id]); }
  return { off, overlapping, stripHeight: document.querySelector('.touchpad-strip').getBoundingClientRect().height,
           padHeight: document.getElementById('touchpad-surface').getBoundingClientRect().height }; }"""


# --- the pointer modes and the full-screen touchpad (spec 2026-10-13 TV remote and touchpad, sections 3 to 5) ---

# The pad's numbers, as the plan gives them (touchpad.js has them as constants, each with a line on why): the tests are written in them, so a
# change of a number is a change of a test. (The distance of a tap, 8 px, the wheel's notch, 30 px, and the surface's ten minutes are in the tests as they are.)
GAIN, ACCEL_PX, TAP_MS, HOLD_MS = 1.6, 40, 180, 350
FRAME_MS = 16                       # one animation frame on the page's stopped clock
PAD, SURFACE = "#tv-trackpad", "#touchpad-surface"
TOUCH = """([selector, kind, id, x, y, stamp]) => {
    const e = new PointerEvent(kind === 'lostcapture' ? 'lostpointercapture' : 'pointer' + kind, {
        pointerId: id, pointerType: 'touch', isPrimary: id === 1, clientX: x, clientY: y, bubbles: true, cancelable: true,
        button: kind === 'move' ? -1 : 0, buttons: kind === 'up' || kind === 'cancel' || kind === 'lostcapture' ? 0 : 1 });
    Object.defineProperty(e, 'timeStamp', { value: stamp === null ? performance.now() : stamp });   // the page's own clock, which the stamp of an event made by script does not follow when the test stops it
    return document.querySelector(selector).dispatchEvent(e); }"""


def touch_event(page, selector, kind, x, y, finger=1, stamp=None):
    """A synthetic finger on `selector`: `kind` is down, move, up, cancel or lostcapture, and `finger` is the pointer's id (two fingers are two ids). The
    event is stamped with the page's clock, as a panel's are (the page times a touch by the stamps), or with `stamp`, when the test wants the stamps and the
    clock to disagree, as they do when the page is slow to get to an event."""
    page.evaluate(TOUCH, [selector, kind, finger, x, y, stamp])


def frame(page, count=1):
    """The page's animation frames, one at a time: on the stopped clock a frame is only drawn when the test runs its clock for one."""
    for _ in range(count):
        page.clock.run_for(FRAME_MS)


def scaled(dx, dy=0):
    """What one frame's travel (dx, dy) becomes: the gain, and the acceleration that follows the speed of the frame."""
    k = GAIN * (1 + min(math.hypot(dx, dy), ACCEL_PX) / ACCEL_PX)
    return dx * k, dy * k


def drag_path(page, selector, steps, start=(100, 100), finger=1):
    """A finger goes down at `start` and moves by each (dx, dy) of `steps`, one animation frame for each. It is left down; returns where it is."""
    x, y = start
    touch_event(page, selector, "down", x, y, finger)
    for dx, dy in steps:
        x, y = x + dx, y + dy
        touch_event(page, selector, "move", x, y, finger)
        frame(page)
    return x, y


class PointerLog:
    """The agent's end of /ws/pointer, for a page that is not to reach the real one: it takes every socket the page opens, and notes the sockets
    and the frames the page sends, in order. The page's messages come through the test browser's driver, so `got()` lets them arrive."""

    def __init__(self, page):
        self.page, self.sockets, self.frames, self.closed = page, [], [], []
        page.route_web_socket("**/ws/pointer", self._accept)

    def _accept(self, ws):
        self.sockets.append(ws)
        ws.on_message(lambda text: self.frames.append(json.loads(text)))
        ws.on_close(lambda code, reason: self.closed.append(ws))              # (the sockets that the page closed)

    def got(self):
        self.page.wait_for_timeout(80)
        return list(self.frames)

    def say(self, words):
        self.sockets[-1].send(json.dumps({"error": words}))

    def drop(self):
        self.sockets[-1].close(code=1011, reason="the agent stopped")


def the_socket(page, state="up"):
    """Waits for the pointer socket to be as the page's client says (it is a small public handle: window.croomTouchpad)."""
    page.wait_for_function(f"window.croomTouchpad.link.state() === '{state}'", timeout=5000)


def pad_page(browser, server, mode=None, width=1280, height=800):
    """The TV view on a stopped clock, with a PointerLog in the agent's place; `mode` is the id of a button of the modes row to tap, and the socket is
    up when this returns."""
    page = page_on_a_stopped_clock(browser, server, width, height)
    log = PointerLog(page)
    open_tv_screen(page)
    if mode:
        page.click(mode)
        the_socket(page)
    return page, log


def view_of(page):
    return page.evaluate("document.body.dataset.view")


def test_the_modes_row_turns_the_disc_into_a_trackpad_for_each_target_and_back(browser):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page = tv_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        modes = page.locator("#tv-modes button")
        assert modes.evaluate_all("bs => bs.map(b => b.id)") == ["tv-pointer", "tv-pi-mouse", "tv-fullscreen"]
        assert modes.all_inner_texts() == ["Pointer", "Room Pi mouse", "Full-screen touchpad"]
        assert modes.evaluate_all("bs => bs.map(b => b.querySelector('svg use').getAttribute('href'))") == ["#icon-pointer", "#icon-monitor", "#icon-touchpad"]
        assert modes.evaluate_all("bs => bs.every(b => !('tv' in b.dataset))")                  # not keys of the remote: they send no control
        disc = page.locator("#tv-dpad")
        assert disc.get_attribute("data-mode") == "keys" and page.locator("#tv-middle").is_visible() and page.locator("#tv-trackpad").is_hidden()

        def filled(selector):
            return page.locator(selector).evaluate("b => getComputedStyle(b).backgroundColor")

        page.mouse.move(0, 0)                                                                    # (a hovered key is another colour)
        assert filled("#tv-pointer") == "rgba(0, 0, 0, 0)"                                       # off: outlined

        page.click("#tv-pointer")                                                                # the disc becomes the TV's trackpad
        page.mouse.move(0, 0)
        assert disc.get_attribute("data-mode") == "pointer" and page.locator("#tv-trackpad").get_attribute("data-target") == "tv"
        assert page.locator("#tv-trackpad").is_visible() and page.locator("#tv-middle").is_hidden()
        assert [page.locator(s).get_attribute("aria-pressed") for s in ("#tv-pointer", "#tv-pi-mouse")] == ["true", "false"]
        assert filled("#tv-pointer") == "rgb(27, 82, 229)" and filled("#tv-pi-mouse") == "rgba(0, 0, 0, 0)"    # blue when on
        assert page.locator("#tv-trackpad-lead").inner_text() == "Move one finger to move the TV's cursor"
        assert page.locator("#tv-trackpad-how").inner_text() == "Tap to click · Two fingers to scroll · Press and hold, then move, to drag"
        assert status_line(page) == "Boardroom TV · On · HDMI 1 (Crystal Meet) · Pointer on"

        page.click("#tv-pi-mouse")                                                               # a tap on the other moves the trackpad to its target
        assert disc.get_attribute("data-mode") == "pointer" and page.locator("#tv-trackpad").get_attribute("data-target") == "pi"
        assert [page.locator(s).get_attribute("aria-pressed") for s in ("#tv-pointer", "#tv-pi-mouse")] == ["false", "true"]
        assert page.locator("#tv-trackpad-lead").inner_text() == "Move one finger to move the mouse on the room's screen"
        assert "Two-finger tap to right-click" in page.locator("#tv-trackpad-how").inner_text()
        assert status_line(page) == "Boardroom TV · On · HDMI 1 (Crystal Meet)"         # nothing for the room Pi: the trackpad's own words say which it moves

        page.click("#tv-pi-mouse")                                                               # a second tap on the one that is on brings the d-pad back
        assert disc.get_attribute("data-mode") == "keys" and page.locator("#tv-middle").is_visible() and page.locator("#tv-trackpad").is_hidden()
        assert [page.locator(s).get_attribute("aria-pressed") for s in ("#tv-pointer", "#tv-pi-mouse")] == ["false", "false"]
        page.click("#tv-pointer")
        page.click("#tv-pointer")
        assert disc.get_attribute("data-mode") == "keys" and status_line(page) == "Boardroom TV · On · HDMI 1 (Crystal Meet)"
        assert errors == []
        page.close()


def test_the_trackpad_takes_the_middle_and_the_keys_above_and_the_modes_below_stay_put(browser):
    """The disc grows into the whole middle of the remote (the mockup's tv-pointer.png), and nothing else on the screen moves for it."""
    with PageServer(tv=RemoteTv()) as server:
        page = tv_page(browser, server)

        def where():
            return page.evaluate("[...document.querySelectorAll('.tv-head button, #tv-keys button, #tv-modes button')].map(k => { const r = k.getBoundingClientRect(); return [k.id || k.dataset.tv, r.left, r.top, r.width, r.height]; })")

        resting = where()
        middle = page.locator("#tv-middle").bounding_box()
        page.click("#tv-pointer")
        pad = page.locator("#tv-trackpad").bounding_box()
        assert all(abs(pad[k] - middle[k]) <= 1 for k in ("x", "y", "width", "height")), (pad, middle)   # the trackpad is where the middle was
        assert where() == resting
        page.close()


def test_the_dpads_sectors_and_ok_take_no_press_in_pointer_mode(browser):
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.dispatch_event('.tv-dpad [data-tv="key:UP"]', "click")                              # (as a press does: it reaches the TV while the d-pad is the d-pad)
        assert tv_calls(page, server, 1) == [("control", "key", "UP")]
        page.click("#tv-pointer")
        for selector in ('.tv-dpad [data-tv="key:UP"]', '.tv-dpad [data-tv="key:LEFT"]', ".tv-ok"):
            page.dispatch_event(selector, "click")
            page.dispatch_event(selector, "keydown", {"key": "Enter"})
            page.dispatch_event(selector, "keydown", {"key": " "})
        page.wait_for_timeout(200)
        assert tv.calls == [("control", "key", "UP")]
        page.close()


@pytest.mark.parametrize("tv, said", [(lambda: RemoteTv(paired=False), "Pair the TV to move its cursor"), (lambda: None, "No TV is set up for this room"),
                                      (lambda: RemoteTv(refused=True), "Pair the TV to move its cursor")], ids=["not paired", "no TV set up", "no longer accepted"])
def test_the_modes_row_under_a_card_has_only_the_full_screen_touchpad_and_it_moves_the_room_pi(browser, tv, said):
    with PageServer(tv=tv(), mouse=StubMouse()) as server:
        page = tv_page(browser, server)
        assert page.locator("#tv-remote").is_hidden()
        assert [b.is_visible() for b in page.locator("#tv-modes button").all()] == [False, False, True]
        assert page.locator("#tv-fullscreen").inner_text() == "Full-screen touchpad"
        page.click("#tv-fullscreen")
        assert page.locator("#touchpad").is_visible()
        assert page.locator("#touchpad-tv").is_disabled() and page.locator("#touchpad-pi").get_attribute("aria-pressed") == "true"   # the room Pi, and no TV to switch to
        assert page.locator("#touchpad-title").inner_text() == "Moving the mouse on the room's screen"
        assert page.locator("#touchpad-sub").inner_text() == said                                # (what is to be done, for a TV that is there; that there is none, for a room with none)
        page.click("#touchpad-tv", force=True)                                                   # (a disabled half does nothing)
        assert page.locator("#touchpad-title").inner_text() == "Moving the mouse on the room's screen"
        page.click("#touchpad-done")
        assert page.locator("#touchpad").is_hidden() and view_of(page) == "tv"
        page.close()


def test_a_pointer_mode_goes_off_when_the_tv_stops_being_one_the_table_can_move(browser):
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        page.click("#tv-pointer")
        tv.paired = False                                                                        # (forgotten from another page)
        refresh_now(page)
        page.wait_for_selector("#tv-pairing", state="visible", timeout=5000)
        assert page.locator("#tv-pointer").is_hidden() and page.locator("#tv-fullscreen").is_visible()
        tv.paired = True
        refresh_now(page)
        page.wait_for_selector("#tv-remote", state="visible", timeout=5000)
        assert page.locator("#tv-dpad").get_attribute("data-mode") == "keys" and page.locator("#tv-pointer").get_attribute("aria-pressed") == "false"
        page.close()


def test_a_finger_path_on_the_disc_sends_moves_and_a_tap_clicks(browser):
    """A finger drawn across the trackpad, a frame at a time, and a tap, as the agent's stub is sent them. The path is twelve frames of (5, 2): what
    it sends adds up to what the gain and the acceleration make of it, in whole numbers, one message for each frame at most; the tap is one
    message and nothing else."""
    with PageServer(tv=RemoteTv()) as server:
        page, log = pad_page(browser, server, "#tv-pointer")
        x, y = drag_path(page, PAD, [(5, 2)] * 12)
        touch_event(page, PAD, "up", x, y)
        moves = log.got()
        assert moves and all(m["target"] == "tv" and m["type"] == "move" for m in moves), moves
        assert all(type(m["dx"]) is int and type(m["dy"]) is int for m in moves)                   # whole numbers, which is what the agent takes
        assert len(moves) == 11                                                                    # one for each frame but the first, whose travel was held (see below)
        want_x, want_y = (12 * v for v in scaled(5, 2))
        assert abs(sum(m["dx"] for m in moves) - want_x) <= 1 and abs(sum(m["dy"] for m in moves) - want_y) <= 1
        touch_event(page, PAD, "down", 300, 300)
        page.clock.run_for(50)
        touch_event(page, PAD, "up", 300, 300)
        assert log.got()[len(moves):] == [{"target": "tv", "type": "tap"}]
        page.close()


def test_several_moves_in_one_frame_are_one_message_and_the_speed_is_the_frames(browser):
    """A touch panel reports faster than the screen draws: what comes between two frames is added up, and the acceleration follows the speed of that
    travel, not of each report (four reports of 2 px are 8 px of travel in the frame: 1.6 x 1.2 of it, and not 1.6 x 1.05)."""
    with PageServer(tv=RemoteTv()) as server:
        page, log = pad_page(browser, server, "#tv-pointer")
        touch_event(page, PAD, "down", 100, 100)
        for x in (102, 104, 106, 108):
            touch_event(page, PAD, "move", x, 100)
        assert log.got() == []                                                                     # (nothing goes before the frame)
        frame(page)
        touch_event(page, PAD, "up", 108, 100)
        assert log.got() == [{"target": "tv", "type": "move", "dx": int(scaled(8)[0]), "dy": 0}]    # 15 (15.36), in one message
        page.close()


def test_the_travel_of_a_touch_is_held_until_it_has_gone_eight_pixels_or_lasted_a_tap(browser):
    with PageServer(tv=RemoteTv()) as server:
        page, log = pad_page(browser, server, "#tv-pointer")
        touch_event(page, PAD, "down", 100, 100)
        touch_event(page, PAD, "move", 105, 100)
        frame(page)
        assert log.got() == []                                                                     # 5 px, 16 ms: it could still be a tap
        touch_event(page, PAD, "move", 110, 100)
        frame(page)
        assert log.got() == [{"target": "tv", "type": "move", "dx": 18, "dy": 0}]                   # 10 px: both frames' travel, as if it had gone out as it came (9.1 and 9.1)
        touch_event(page, PAD, "up", 110, 100)
        touch_event(page, PAD, "down", 200, 200)                                                         # and one that stays within 8 px but lasts
        touch_event(page, PAD, "move", 203, 200)
        frame(page)
        assert len(log.got()) == 1
        page.clock.run_for(TAP_MS)
        touch_event(page, PAD, "move", 204, 200)
        frame(page)
        assert log.got()[1:] == [{"target": "tv", "type": "move", "dx": 6, "dy": 0}]               # 3 px and 1 px: 5.16 and 1.64 of the cursor, held and then sent together
        touch_event(page, PAD, "up", 204, 200)
        assert len(log.got()) == 2                                                                 # (it lasted: no tap)
        page.close()


def test_a_slow_path_loses_nothing_to_whole_numbers_and_a_fast_flick_goes_further_than_the_gain_alone(browser):
    with PageServer(tv=RemoteTv()) as server:
        page, log = pad_page(browser, server, "#tv-pointer")
        x, y = drag_path(page, PAD, [(-1, -1)] * 80)                                               # 1.41 px a frame: 1.66 times as far, and 1 or 2 whole pixels each time
        touch_event(page, PAD, "up", x, y)
        slow = log.got()
        want = 80 * scaled(-1, -1)[0]
        assert abs(sum(m["dx"] for m in slow) - want) <= 1 and abs(sum(m["dy"] for m in slow) - want) <= 1, (sum(m["dx"] for m in slow), want)
        assert {m["dx"] for m in slow[1:]} <= {-1, -2} and len(slow) >= 70                          # (the first message has the held frames' travel: 6 of them, 8.5 px, went past 8)
        x, y = drag_path(page, PAD, [(60, 0)], start=(10, 500))                                    # one frame of 60 px: the acceleration at its top, twice the gain
        touch_event(page, PAD, "up", x, y)
        flick = log.got()[len(slow):]
        assert len(flick) == 1 and flick[0]["dy"] == 0 and 191 <= flick[0]["dx"] <= 192, flick
        assert flick[0]["dx"] > 60 * GAIN * 1.5
        page.close()


def test_a_quick_short_touch_is_a_tap_and_sends_only_the_tap_however_it_wobbles(browser):
    with PageServer(tv=RemoteTv()) as server:
        page, log = pad_page(browser, server, "#tv-pointer")
        touch_event(page, PAD, "down", 100, 100)
        touch_event(page, PAD, "move", 103, 101)                                                         # a fingertip rolls
        frame(page)
        touch_event(page, PAD, "move", 100, 99)
        frame(page, 3)
        touch_event(page, PAD, "up", 100, 99)
        assert log.got() == [{"target": "tv", "type": "tap"}]
        touch_event(page, PAD, "down", 100, 100)                                                         # slower than a tap is no tap
        page.clock.run_for(TAP_MS + 20)
        touch_event(page, PAD, "up", 100, 100)
        touch_event(page, PAD, "down", 100, 100)                                                         # and further than a tap is a move
        touch_event(page, PAD, "move", 112, 100)
        frame(page)
        touch_event(page, PAD, "up", 112, 100)
        frames = log.got()
        assert [f["type"] for f in frames] == ["tap", "move"]
        page.close()


def test_a_finger_held_still_and_then_moved_drags_and_a_long_press_that_does_not_move_sends_nothing(browser):
    with PageServer(tv=RemoteTv()) as server:
        page, log = pad_page(browser, server, "#tv-pointer")
        touch_event(page, PAD, "down", 100, 100)                                                         # held still for 400 ms, then moved: a drag
        page.clock.run_for(HOLD_MS + 50)
        assert log.got() == []                                                                     # (nothing is sent for the holding itself)
        touch_event(page, PAD, "move", 120, 100)
        frame(page)
        touch_event(page, PAD, "move", 140, 100)
        frame(page)
        touch_event(page, PAD, "up", 140, 100)
        frames = log.got()
        assert frames[0] == {"target": "tv", "type": "down"} and frames[-1] == {"target": "tv", "type": "up"}
        moves = frames[1:-1]
        assert moves and all(f["type"] == "move" for f in moves)
        assert abs(sum(m["dx"] for m in moves) - 2 * scaled(20)[0]) <= 1                           # (the 20 px that took it past 8 px are not lost)
        touch_event(page, PAD, "down", 100, 100)                                                         # a long press that ends where it began: nothing at all
        page.clock.run_for(HOLD_MS + 250)
        touch_event(page, PAD, "up", 100, 100)
        assert log.got() == frames
        touch_event(page, PAD, "down", 100, 100)                                                         # a finger that moves at once does not drag
        page.clock.run_for(40)
        touch_event(page, PAD, "move", 130, 100)
        frame(page)
        touch_event(page, PAD, "up", 130, 100)
        assert [f["type"] for f in log.got()[len(frames):]] == ["move"]
        page.close()


def test_a_drag_that_the_browser_takes_away_lets_go_too(browser):
    with PageServer(tv=RemoteTv()) as server:
        page, log = pad_page(browser, server, "#tv-pi-mouse")
        touch_event(page, PAD, "down", 100, 100)
        page.clock.run_for(HOLD_MS + 50)
        touch_event(page, PAD, "move", 130, 100)
        frame(page)
        touch_event(page, PAD, "cancel", 130, 100)                                                       # (a palm, a system gesture: no lift will come)
        frames = log.got()
        assert [f["type"] for f in frames][0] == "down" and frames[-1]["type"] == "up" and all(f["target"] == "pi" for f in frames)
        page.close()


def test_a_touch_the_browser_lets_go_of_without_a_lift_ends_like_a_cancelled_one(browser):
    """No up and no cancel came, but the pad lost its hold on the finger: a drag must not be left with the button down on the room Pi."""
    with PageServer(tv=RemoteTv()) as server:
        page, log = pad_page(browser, server, "#tv-pi-mouse")
        touch_event(page, PAD, "down", 100, 100)
        page.clock.run_for(HOLD_MS + 50)
        touch_event(page, PAD, "move", 140, 100)
        frame(page)
        touch_event(page, PAD, "lostcapture", 140, 100)
        assert [f["type"] for f in log.got()] == ["down", "move", "up"]
        touch_event(page, PAD, "up", 140, 100)                                                       # (the lift that was lost turns up late: nothing more)
        assert [f["type"] for f in log.got()] == ["down", "move", "up"]
        touch_event(page, PAD, "down", 300, 300)                                                     # and a tap that ends normally is one, with the lost capture after it
        page.clock.run_for(50)
        touch_event(page, PAD, "up", 300, 300)
        touch_event(page, PAD, "lostcapture", 300, 300)
        assert [f["type"] for f in log.got()] == ["down", "move", "up", "tap"]
        page.close()


def test_a_drag_under_a_mode_that_is_switched_or_put_away_ends_on_the_target_it_began_on(browser):
    with PageServer(tv=RemoteTv()) as server:
        page, log = pad_page(browser, server, "#tv-pi-mouse")
        touch_event(page, PAD, "down", 100, 100)
        page.clock.run_for(HOLD_MS + 50)
        touch_event(page, PAD, "move", 140, 100)
        frame(page)
        page.click("#tv-pointer")                                                                    # (a second finger on the other mode)
        frames = log.got()
        assert [(f["target"], f["type"]) for f in frames] == [("pi", "down"), ("pi", "move"), ("pi", "up")]
        touch_event(page, PAD, "move", 180, 100)                                                     # the first finger is still down: it is nobody's now
        frame(page)
        touch_event(page, PAD, "up", 180, 100)
        assert log.got() == frames
        page.close()


def test_a_drag_the_socket_lost_is_forgotten_and_nothing_is_sent_late_for_it(browser):
    """The agent let the button go when the socket ended, so the page does not send an up for the drag, though the finger is still down when the socket
    is back: what the finger does from there is an ordinary move."""
    with PageServer(tv=RemoteTv()) as server:
        page, log = pad_page(browser, server, "#tv-pi-mouse")
        touch_event(page, PAD, "down", 100, 100)
        page.clock.run_for(HOLD_MS + 50)
        touch_event(page, PAD, "move", 140, 100)
        frame(page)
        assert [f["type"] for f in log.got()] == ["down", "move"]
        log.drop()
        the_socket(page, "down")
        touch_event(page, PAD, "move", 180, 100)                                                     # (dropped, never kept)
        frame(page)
        page.clock.run_for(1001)
        the_socket(page)
        touch_event(page, PAD, "move", 200, 100)
        frame(page)
        touch_event(page, PAD, "up", 200, 100)
        frames = log.got()
        assert [f["type"] for f in frames] == ["down", "move", "move"]                               # no up, then or now, and nothing from the time it was away
        assert frames[2]["dx"] == int(scaled(20)[0])
        page.close()


def test_two_fingers_scroll_and_two_finger_tap_right_clicks_the_pi(browser):
    """The room Pi's wheel turns a notch for each 30 px of the two fingers' mean travel, the remainder carried, the content following the fingers
    (up for a finger drawn up); the TV is sent the pixels. A quick touch of both fingers is a right click, for the room Pi only."""
    with PageServer(tv=RemoteTv()) as server:
        page, log = pad_page(browser, server, "#tv-pi-mouse")
        touch_event(page, PAD, "down", 100, 300, 1)
        touch_event(page, PAD, "down", 220, 300, 2)
        for step in range(1, 5):                                                                   # both fingers drawn up, 20 px a frame: 80 px, 2.67 notches
            touch_event(page, PAD, "move", 100, 300 - 20 * step, 1)
            touch_event(page, PAD, "move", 220, 300 - 20 * step, 2)
            frame(page)
        touch_event(page, PAD, "up", 100, 220, 1)
        touch_event(page, PAD, "up", 220, 220, 2)
        scrolls = log.got()
        assert scrolls and all(f == {"target": "pi", "type": "scroll", "dx": 0, "dy": f["dy"]} and f["dy"] < 0 for f in scrolls), scrolls
        assert sum(f["dy"] for f in scrolls) == -2                                                 # (and 0.67 of a notch is carried, not sent)
        touch_event(page, PAD, "down", 100, 300, 1)                                                      # fingers drawn down, and the other way
        touch_event(page, PAD, "down", 220, 300, 2)
        for step in range(1, 5):
            touch_event(page, PAD, "move", 100, 300 + 20 * step, 1)
            touch_event(page, PAD, "move", 220, 300 + 20 * step, 2)
            frame(page)
        touch_event(page, PAD, "up", 100, 380, 1)
        touch_event(page, PAD, "up", 220, 380, 2)
        assert sum(f["dy"] for f in log.got()[len(scrolls):]) == 2
        before = len(log.got())
        touch_event(page, PAD, "down", 100, 300, 1)                                                      # a two-finger tap: both down and up again within 180 ms
        touch_event(page, PAD, "down", 220, 300, 2)
        page.clock.run_for(60)
        touch_event(page, PAD, "up", 100, 300, 1)
        touch_event(page, PAD, "up", 220, 300, 2)
        assert log.got()[before:] == [{"target": "pi", "type": "right"}]
        page.close()


def test_a_second_finger_landing_during_a_drag_lets_the_button_go_and_the_two_fingers_scroll(browser):
    """Once a second finger lands the gesture is two-finger until every finger is up: a drag that was going on ends first, so that no scroll runs with
    the button held, and the up is not sent again at the lifts."""
    with PageServer(tv=RemoteTv()) as server:
        page, log = pad_page(browser, server, "#tv-pi-mouse")
        touch_event(page, PAD, "down", 100, 300, 1)
        page.clock.run_for(HOLD_MS + 50)
        touch_event(page, PAD, "move", 140, 300, 1)
        frame(page)
        touch_event(page, PAD, "down", 260, 300, 2)
        for step in range(1, 5):
            touch_event(page, PAD, "move", 140, 300 - 20 * step, 1)
            touch_event(page, PAD, "move", 260, 300 - 20 * step, 2)
            frame(page)
        touch_event(page, PAD, "up", 140, 220, 1)
        touch_event(page, PAD, "up", 260, 220, 2)
        frames = log.got()
        assert [f["type"] for f in frames[:3]] == ["down", "move", "up"]                             # the drag, and the button let go as the second finger lands
        assert frames[3:] and all(f["type"] == "scroll" and f["dy"] < 0 for f in frames[3:]) and sum(f["dy"] for f in frames[3:]) == -2
        page.close()


def test_two_fingers_on_the_tv_scroll_in_pixels_and_a_two_finger_tap_sends_nothing(browser):
    with PageServer(tv=RemoteTv()) as server:
        page, log = pad_page(browser, server, "#tv-pointer")
        touch_event(page, PAD, "down", 100, 300, 1)
        touch_event(page, PAD, "down", 220, 300, 2)
        for step in range(1, 5):
            touch_event(page, PAD, "move", 100 + 6 * step, 300 - 20 * step, 1)
            touch_event(page, PAD, "move", 220 + 6 * step, 300 - 20 * step, 2)
            frame(page)
        touch_event(page, PAD, "up", 124, 220, 1)
        touch_event(page, PAD, "up", 244, 220, 2)
        scrolls = log.got()
        assert scrolls and all(f["target"] == "tv" and f["type"] == "scroll" and type(f["dx"]) is int and type(f["dy"]) is int for f in scrolls)
        assert (sum(f["dx"] for f in scrolls), sum(f["dy"] for f in scrolls)) == (24, -80)         # the pixels, with the signs of the fingers' own travel
        touch_event(page, PAD, "down", 100, 300, 1)
        touch_event(page, PAD, "down", 220, 300, 2)
        touch_event(page, PAD, "move", 103, 302, 1)                                                # (the fingertips roll a little)
        touch_event(page, PAD, "move", 222, 301, 2)
        frame(page)
        page.clock.run_for(40)
        touch_event(page, PAD, "up", 103, 302, 1)
        touch_event(page, PAD, "up", 222, 301, 2)
        assert log.got() == scrolls                                                                # (the TV's cursor has no right button, and the rolling scrolled nothing)
        page.close()


def test_a_slow_two_finger_touch_is_no_tap_and_a_third_finger_and_the_last_finger_of_two_do_nothing(browser):
    with PageServer(tv=RemoteTv()) as server:
        page, log = pad_page(browser, server, "#tv-pi-mouse")
        touch_event(page, PAD, "down", 100, 300, 1)
        touch_event(page, PAD, "down", 220, 300, 2)
        page.clock.run_for(TAP_MS + 20)                                                            # slower than a tap
        touch_event(page, PAD, "up", 100, 300, 1)
        touch_event(page, PAD, "up", 220, 300, 2)
        assert log.got() == []
        touch_event(page, PAD, "down", 100, 300, 1)                                                      # a third finger is ignored, wherever it goes
        touch_event(page, PAD, "down", 220, 300, 2)
        touch_event(page, PAD, "down", 340, 300, 3)
        for step in range(1, 5):
            touch_event(page, PAD, "move", 100, 300 - 20 * step, 1)
            touch_event(page, PAD, "move", 220, 300 - 20 * step, 2)
            touch_event(page, PAD, "move", 340 + 60 * step, 300 + 60 * step, 3)                      # (last, so that it would be the one the frame found)
            frame(page)
        touch_event(page, PAD, "up", 340, 300, 3)
        scrolled = log.got()
        assert sum(f["dy"] for f in scrolled) == -2 and all(f["type"] == "scroll" for f in scrolled)
        touch_event(page, PAD, "up", 100, 220, 1)                                                        # the last finger of two lifting is not a tap, and its travel is not a move
        touch_event(page, PAD, "down", 400, 300, 4)                                                      # and a finger that lands after one of the two has lifted joins nothing
        for step in range(1, 5):
            touch_event(page, PAD, "move", 400, 300 - 20 * step, 4)
            touch_event(page, PAD, "move", 220, 220 - 20 * step, 2)
            frame(page)
        touch_event(page, PAD, "up", 400, 220, 4)
        touch_event(page, PAD, "up", 220, 140, 2)
        assert log.got() == scrolled
        page.close()


def test_the_full_screen_touchpad_opens_on_the_target_of_the_mode_that_is_on_else_the_room_pi(browser):
    for mode, target, title, small in (
            (None, "pi", "Moving the mouse on the room's screen", "The room Pi behind the TV"),
            ("#tv-pointer", "tv", "Moving the TV's cursor", "Boardroom TV"),
            ("#tv-pi-mouse", "pi", "Moving the mouse on the room's screen", "The room Pi behind the TV")):
        with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
            page = tv_page(browser, server)
            if mode:
                page.click(mode)
            page.click("#tv-fullscreen")
            assert page.locator("#touchpad").is_visible()
            assert page.locator("#touchpad-title").inner_text() == title and page.locator("#touchpad-sub").inner_text() == small
            assert [page.locator(s).get_attribute("aria-pressed") for s in ("#touchpad-tv", "#touchpad-pi")] == [str(target == "tv").lower(), str(target == "pi").lower()]
            assert page.locator("#touchpad-tv").inner_text() == "TV" and page.locator("#touchpad-pi").inner_text() == "Room Pi" and page.locator("#touchpad-done").inner_text() == "Done"
            assert [page.locator(f"#touchpad-{k} svg use").get_attribute("href") for k in ("tv", "pi", "done")] == ["#icon-tv", "#icon-monitor", "#icon-check"]
            page.close()


def test_the_surface_covers_the_whole_page_and_the_switch_moves_it_between_the_targets(browser):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page = tv_page(browser, server)
        page.click("#tv-pointer")
        page.click("#tv-fullscreen")
        box = page.locator("#touchpad").bounding_box()
        assert (box["x"], box["y"], box["width"], box["height"]) == (0, 0, 1280, 800)                # the header too
        assert page.evaluate("document.elementFromPoint(1200, 50).closest('#touchpad') !== null")    # where Options is, and nothing of the page is under a finger
        assert page.locator("#touchpad-surface").evaluate("s => s.getBoundingClientRect().bottom") == 800
        page.click("#touchpad-pi")
        assert page.locator("#touchpad-title").inner_text() == "Moving the mouse on the room's screen"
        assert page.locator("#touchpad-pi").get_attribute("aria-pressed") == "true" and page.locator("#touchpad-tv").get_attribute("aria-pressed") == "false"
        page.click("#touchpad-tv")
        assert page.locator("#touchpad-title").inner_text() == "Moving the TV's cursor"
        page.click("#touchpad-pi")
        page.click("#touchpad-done")                                                                 # Done: the TV view, with the mode that was on
        assert page.locator("#touchpad").is_hidden() and view_of(page) == "tv"
        assert page.locator("#tv-dpad").get_attribute("data-mode") == "pointer" and page.locator("#tv-trackpad").get_attribute("data-target") == "tv"
        assert page.locator("#tv-pointer").get_attribute("aria-pressed") == "true"                   # (the surface's own switch is its own)
        page.close()


def test_a_tv_that_is_forgotten_under_the_surface_hands_the_surface_to_the_room_pi_and_ends_a_drag_on_the_tv(browser):
    tv = RemoteTv()
    with PageServer(tv=tv, mouse=StubMouse()) as server:
        page, log = pad_page(browser, server, "#tv-pointer")
        page.click("#tv-fullscreen")
        assert page.locator("#touchpad-title").inner_text() == "Moving the TV's cursor" and page.locator("#touchpad-tv").is_enabled()
        touch_event(page, SURFACE, "down", 400, 400)                                                 # a finger on the surface, holding for a drag
        page.clock.run_for(HOLD_MS + 50)
        touch_event(page, SURFACE, "move", 440, 400)
        frame(page)
        tv.paired = False                                                                            # (forgotten from another page)
        page.clock.run_for(2500)                                                                     # (the page's poll sees it)
        page.wait_for_function("document.getElementById('touchpad-title').textContent === \"Moving the mouse on the room's screen\"", timeout=5000)
        assert page.locator("#touchpad-tv").is_disabled() and page.locator("#touchpad-pi").get_attribute("aria-pressed") == "true"
        assert page.locator("#touchpad-sub").inner_text() == "Pair the TV to move its cursor"
        assert page.locator("#touchpad").is_visible()                                                # (the surface stays: it is what the person is using)
        assert [(f["target"], f["type"]) for f in log.got()] == [("tv", "down"), ("tv", "move"), ("tv", "up")]   # the drag ended on the TV it began on
        page.close()


def test_the_surface_takes_the_focus_for_done_and_gives_it_back_to_the_button_that_opened_it(browser):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page = tv_page(browser, server)
        page.click("#tv-fullscreen")
        assert page.evaluate("document.activeElement.id") == "touchpad-done"
        assert page.locator("#touchpad").get_attribute("role") == "dialog" and page.locator("#touchpad").get_attribute("aria-modal") == "true"
        page.click("#touchpad-done")
        assert page.evaluate("document.activeElement.id") == "tv-fullscreen"
        page.close()


def test_a_drag_on_the_surface_ends_on_the_target_it_began_on_when_the_switch_is_tapped(browser):
    with PageServer(tv=RemoteTv()) as server:
        page, log = pad_page(browser, server, "#tv-pointer")
        page.click("#tv-fullscreen")
        touch_event(page, SURFACE, "down", 400, 400)
        page.clock.run_for(HOLD_MS + 50)
        touch_event(page, SURFACE, "move", 440, 400)
        frame(page)
        page.click("#touchpad-pi")                                                                   # (a second finger on the switch)
        frames = log.got()
        assert [(f["target"], f["type"]) for f in frames] == [("tv", "down"), ("tv", "move"), ("tv", "up")]
        touch_event(page, SURFACE, "move", 480, 400)                                                 # the first finger is still down: it is nobody's now
        frame(page)
        touch_event(page, SURFACE, "up", 480, 400)
        assert log.got() == frames
        page.close()


def test_the_touchpad_returns_to_the_screen_that_opened_it(browser):
    """Done puts the surface away and the page is on the TV view it was opened from, with the mode that was on; Back from there goes where the TV view
    came from: the meeting screen for a call, Options for a room that is not in one."""
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)                                                                             # in a call
        meeting_tv_button(page).click()
        page.clock.run_for(600)                                                                     # the screen settles after the view change (a tap in the half second is ignored)
        page.click("#tv-fullscreen")
        assert page.locator("#touchpad").is_visible() and view_of(page) == "tv"
        page.click("#touchpad-done")
        assert page.locator("#touchpad").is_hidden() and view_of(page) == "tv" and page.locator("#tv-remote").is_visible()
        page.click("#back-from-tv")
        assert view_of(page) == "meeting"
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/leave", data="{}", headers=JSON)
        page.clock.run_for(2500)                                                                     # (the page's poll sees the call has ended)
        page.wait_for_function("document.body.dataset.view === 'idle'", timeout=8000)
        page.click("#open-options")                                                                  # and in a room that is not
        page.click("#open-tv-options")
        page.click("#tv-pi-mouse")
        page.click("#tv-fullscreen")
        page.click("#touchpad-done")
        assert page.locator("#touchpad").is_hidden() and view_of(page) == "tv"
        assert page.locator("#tv-dpad").get_attribute("data-mode") == "pointer"
        page.click("#back-from-tv")
        assert view_of(page) == "options"
        page.close()


def test_the_touchpad_puts_itself_away_after_ten_idle_minutes_and_the_pages_own_return_waits_meanwhile(browser):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page = page_on_a_stopped_clock(browser, server)
        open_tv_screen(page)
        page.click("#tv-fullscreen")
        page.clock.fast_forward(9 * 60_000)
        touch_event(page, SURFACE, "down", 400, 400)                                                       # a touch at nine minutes keeps it another ten
        touch_event(page, SURFACE, "up", 400, 400)
        page.clock.fast_forward(90_000)
        assert page.locator("#touchpad").is_visible()                                                # (ten and a half minutes in: the page's own return, ten from the tap, was waiting)
        page.clock.fast_forward(8 * 60_000)
        assert page.locator("#touchpad").is_visible()
        page.clock.fast_forward(90_000)
        assert page.locator("#touchpad").is_hidden() and view_of(page) == "tv"                       # ten minutes with no touch: the TV view it was opened from
        page.clock.fast_forward(9 * 60_000)
        assert view_of(page) == "tv"                                                                 # and the page's own wait begins again: ten minutes from now
        page.clock.fast_forward(90_000)
        assert view_of(page) == "idle"
        page.close()


def test_a_meetings_tv_view_waits_two_minutes_from_the_surface_going_away_and_not_while_it_is_up(browser):
    """In a meeting the TV view returns to the meeting screen after two idle minutes; the tap that opened the surface starts that wait, and the surface
    must stop it: nobody touching the screen for two and a half minutes is what a person pointing at the TV looks like to the page."""
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        meeting_tv_button(page).click()
        page.clock.run_for(600)                                                                     # the screen settles after the view change (a tap in the half second is ignored)
        page.click("#tv-fullscreen")
        page.clock.fast_forward(150_000)
        assert page.locator("#touchpad").is_visible() and view_of(page) == "tv"
        page.click("#touchpad-done")
        page.clock.fast_forward(100_000)
        assert view_of(page) == "tv"                                                                 # the two minutes begin when the surface goes away
        page.clock.fast_forward(30_000)
        assert view_of(page) == "meeting"
        page.close()


@pytest.mark.parametrize("cause", ["Back", "the idle return", "the meeting ending", "the mode the TV view came from ending"])
def test_leaving_the_tv_view_in_the_middle_of_a_drag_lets_go_closes_the_socket_and_turns_the_mode_off(browser, cause):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page = page_on_a_stopped_clock(browser, server)
        log = PointerLog(page)
        if cause == "the meeting ending":
            join_by_id(page)
            meeting_tv_button(page).click()
            page.clock.run_for(600)                                                                     # the screen settles after the view change (a tap in the half second is ignored)
        else:
            open_tv_screen(page)
        page.click("#tv-pi-mouse")
        the_socket(page)
        touch_event(page, PAD, "down", 100, 100)
        page.clock.run_for(HOLD_MS + 50)
        touch_event(page, PAD, "move", 140, 100)
        frame(page)
        assert [f["type"] for f in log.got()] == ["down", "move"]                                    # a drag is going, with the button down on the room Pi
        if cause == "Back":
            page.click("#back-from-tv")
        elif cause == "the idle return":
            page.clock.fast_forward(10 * 60_000 + 1000)
        elif cause == "the meeting ending":
            page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/leave", data="{}", headers=JSON)
            page.clock.run_for(2500)                                                                 # (the page's poll sees the call has ended)
        else:
            page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}', headers=JSON)
            page.clock.run_for(2500)                                                                 # (and here, that one has begun)
        page.wait_for_function("document.body.dataset.view !== 'tv'", timeout=8000)
        frames = log.got()
        assert frames[-1] == {"target": "pi", "type": "up"}                                          # let go before the socket went
        assert len(log.closed) == 1 and log.closed[0] is log.sockets[0]
        the_socket(page, "closed")
        page.close()


def test_leaving_the_tv_view_puts_the_surface_away_and_turns_the_mode_off(browser):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page = page_on_a_stopped_clock(browser, server)
        log = PointerLog(page)
        join_by_id(page)
        meeting_tv_button(page).click()
        page.clock.run_for(600)                                                                     # the screen settles after the view change (a tap in the half second is ignored)
        page.click("#tv-pointer")
        page.click("#tv-fullscreen")
        the_socket(page)
        touch_event(page, SURFACE, "down", 400, 400)
        page.clock.run_for(HOLD_MS + 50)
        touch_event(page, SURFACE, "move", 440, 400)
        frame(page)
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/leave", data="{}", headers=JSON)    # the call ends under the surface
        page.clock.run_for(2500)
        page.wait_for_function("document.body.dataset.view === 'idle'", timeout=8000)
        assert page.locator("#touchpad").is_hidden()
        assert [f["type"] for f in log.got()] == ["down", "move", "up"] and log.got()[-1]["target"] == "tv"
        the_socket(page, "closed")
        page.click("#open-options")
        page.click("#open-tv-options")
        assert page.locator("#tv-dpad").get_attribute("data-mode") == "keys" and page.locator("#tv-pointer").get_attribute("aria-pressed") == "false"
        page.close()


def test_one_socket_is_open_while_a_pointer_mode_is_on_or_the_surface_is_up_and_closed_when_neither_is(browser):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page = page_on_a_stopped_clock(browser, server)
        log = PointerLog(page)
        open_tv_screen(page)
        page.wait_for_timeout(100)
        assert log.sockets == []                                                                     # the remote alone opens none
        page.click("#tv-pointer")
        the_socket(page)
        assert len(log.sockets) == 1 and log.sockets[0].url == f"ws://127.0.0.1:{server.port}/ws/pointer"   # (ws for a page that is not on https, on the page's own host)
        page.click("#tv-fullscreen")
        page.click("#touchpad-done")
        page.wait_for_timeout(100)
        assert len(log.sockets) == 1 and log.closed == []                                            # the same one, through the surface, and still there for the mode
        page.click("#tv-pi-mouse")
        page.wait_for_timeout(100)
        assert len(log.sockets) == 1 and log.closed == []                                            # (the other target, the same socket)
        page.click("#tv-pi-mouse")                                                                   # no mode, no surface
        the_socket(page, "closed")
        page.wait_for_timeout(100)
        assert log.closed == [log.sockets[0]]
        page.click("#tv-fullscreen")                                                                 # the surface alone
        the_socket(page)
        assert len(log.sockets) == 2
        page.click("#touchpad-done")
        the_socket(page, "closed")
        page.wait_for_timeout(100)
        assert log.closed == log.sockets
        page.close()


def test_the_touchpad_shows_a_mark_when_the_socket_drops(browser):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page = page_on_a_stopped_clock(browser, server)
        log = PointerLog(page)
        open_tv_screen(page)
        page.click("#tv-fullscreen")
        the_socket(page)
        mark = page.locator("#touchpad-link")
        assert mark.is_hidden()                                                                      # nothing to say while the socket is there
        log.drop()
        the_socket(page, "down")
        assert mark.is_visible() and mark.inner_text() == "Reconnecting…"
        assert page.locator("#touchpad-title").is_visible() and page.locator("#touchpad-done").is_visible()   # the strip keeps its place
        page.clock.run_for(900)
        page.wait_for_timeout(100)
        assert len(log.sockets) == 1 and mark.is_visible()                                           # it tries again after a second
        page.clock.run_for(200)
        the_socket(page)
        assert len(log.sockets) == 2 and mark.is_hidden()
        log.drop()                                                                                   # lost again, once it was back: the wait begins again at a second
        the_socket(page, "down")
        page.clock.run_for(1001)
        the_socket(page)
        assert len(log.sockets) == 3 and mark.is_hidden()
        page.close()


def test_the_trackpad_says_reconnecting_in_its_how_line_while_the_socket_is_down(browser):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page, log = pad_page(browser, server, "#tv-pointer")
        how = page.locator("#tv-trackpad-how")
        resting = how.inner_text()
        log.drop()
        the_socket(page, "down")
        assert how.inner_text() == "Reconnecting…"
        page.clock.run_for(1001)
        the_socket(page)
        assert how.inner_text() == resting
        page.close()


def until(page, done, timeout=5.0):
    """Waits, as the page's agent would take its time, for what the server's stubs are to have been told."""
    waited = 0.0
    while not done() and waited < timeout:
        page.wait_for_timeout(25)
        waited += 0.025
    assert done(), "the server was not told in time"


def test_the_socket_comes_back_after_one_second_two_and_then_every_five_and_nothing_done_meanwhile_is_sent_late(browser):
    """The agent goes away (its socket closes, and nothing listens on its port: every try fails) and comes back on the same port."""
    port = free_port()
    attempts = []
    with PageServer(tv=RemoteTv(), mouse=StubMouse(), port=port) as server:
        page = page_on_a_stopped_clock(browser, server)
        page.on("websocket", lambda ws: attempts.append(ws))
        open_tv_screen(page)
        page.click("#tv-pi-mouse")
        the_socket(page)
        assert len(attempts) == 1
    the_socket(page, "down")

    def after(ms):
        page.clock.run_for(ms)
        page.wait_for_timeout(100)
        for _ in range(200):                                                                         # a try that was made has failed, and the page has set its next wait, before the clock moves again
            if all(ws.is_closed() for ws in attempts):
                break
            page.wait_for_timeout(25)
        return len(attempts)

    assert (after(999), after(2)) == (1, 2)                                                          # a second after the loss
    assert (after(1998), after(2)) == (2, 3)                                                         # two after that
    assert (after(4998), after(2)) == (3, 4)                                                         # then five
    assert (after(4998), after(2)) == (4, 5)                                                         # and five again
    assert {ws.url for ws in attempts} == {f"ws://127.0.0.1:{port}/ws/pointer"}
    touch_event(page, PAD, "down", 100, 100)                                                               # a tap while the agent is away: dropped, never kept
    page.clock.run_for(50)
    touch_event(page, PAD, "up", 100, 100)
    mouse = StubMouse()
    with PageServer(tv=RemoteTv(), mouse=mouse, port=port):
        page.clock.run_for(5001)
        the_socket(page)
        until(page, lambda: mouse.opened >= 1)
        page.wait_for_timeout(300)
        assert mouse.calls == []                                                                     # (it would have clicked now)
        touch_event(page, PAD, "down", 100, 100)                                                           # and it works again
        page.clock.run_for(50)
        touch_event(page, PAD, "up", 100, 100)
        until(page, lambda: mouse.calls == [("click", "left")])
        assert page.locator("#tv-words").is_hidden()
    page.close()


def test_an_error_frame_shows_its_words_in_the_status_line_once_and_they_clear(browser):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page, log = pad_page(browser, server, "#tv-pointer")
        said = "The TV is not answering at 10.0.0.41"
        log.say(said)
        page.wait_for_function(f"document.getElementById('tv-words').textContent === {said!r}", timeout=5000)
        assert page.locator("#tv-words").is_visible()
        page.evaluate("""() => { window.writes = 0; const watch = new MutationObserver((records) => { window.writes += records.length; });
            for (const id of ['tv-words', 'tv-remote', 'tv-modes']) watch.observe(document.getElementById(id), { childList: true, characterData: true, subtree: true, attributes: true }); }""")
        for _ in range(20):                                                                          # every move the TV cannot take comes back as the same words
            log.say(said)
        page.wait_for_timeout(300)
        assert page.evaluate("window.writes") == 0                                                   # drawn once
        page.clock.run_for(4900)
        assert page.locator("#tv-words").inner_text() == said                                       # (the words came again: it is five seconds from the last of them)
        page.clock.run_for(200)
        assert page.locator("#tv-words").is_hidden()                                                 # five quiet seconds
        log.say("<b>It is text</b>")                                                                 # whatever it says is text, never markup
        page.wait_for_function("document.getElementById('tv-words').textContent === '<b>It is text</b>'", timeout=5000)
        assert page.locator("#tv-words b").count() == 0
        page.click('[data-tv="volume_up"]')                                                          # and a tap on a key clears them, as it does a refusal's
        assert page.locator("#tv-words").is_hidden()
        log.say(said)
        page.wait_for_selector("#tv-words:not([hidden])", timeout=5000)
        page.click("#back-from-tv")                                                                  # leaving the view clears them for the next person
        page.click("#open-tv-options")
        assert page.locator("#tv-words").is_hidden()
        page.close()


def test_an_error_frame_shows_in_the_strips_small_line_and_goes_after_five_quiet_seconds(browser):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page, log = pad_page(browser, server)
        page.click("#tv-fullscreen")
        the_socket(page)
        small = page.locator("#touchpad-sub")
        said = "The room Pi's mouse needs /dev/uinput, and it is not there"
        log.say(said)
        page.wait_for_function(f"document.getElementById('touchpad-sub').textContent === {said!r}", timeout=5000)
        page.evaluate("""() => { window.writes = 0; new MutationObserver((records) => { window.writes += records.length; }).observe(
            document.getElementById('touchpad'), { childList: true, characterData: true, subtree: true, attributes: true }); }""")
        for _ in range(20):
            log.say(said)
        page.wait_for_timeout(300)
        assert page.evaluate("window.writes") == 0
        page.clock.run_for(4900)
        assert small.inner_text() == said
        page.clock.run_for(200)
        assert small.inner_text() == "The room Pi behind the TV"
        page.close()


def test_the_pointer_socket_is_wss_on_a_page_served_over_https(browser):
    """A page over https may not open a plain ws socket, so the socket takes the protocol of the page (the other tests of it see ws, from plain http).
    The agent here speaks http: a route fetches the page for an https address from it, and a PointerLog stands in for the socket."""
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        agent = f"http://127.0.0.1:{server.port}"
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.route("https://room.test/**", lambda route: route.fulfill(response=route.fetch(url=route.request.url.replace("https://room.test", agent))))
        log = PointerLog(page)
        page.goto("https://room.test/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        open_tv_screen(page)
        page.click("#tv-pointer")
        the_socket(page)
        assert [s.url for s in log.sockets] == ["wss://room.test/ws/pointer"]
        page.close()


def test_what_the_agent_says_while_the_surface_is_up_goes_to_the_surface_and_not_to_the_remote_beneath_it(browser):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page, log = pad_page(browser, server, "#tv-pointer")
        page.click("#tv-fullscreen")
        said = "The TV is not answering at 10.0.0.41"
        log.say(said)
        page.wait_for_function(f"document.getElementById('touchpad-sub').textContent === {said!r}", timeout=5000)
        page.click("#touchpad-done")
        assert page.locator("#tv-words").is_hidden()                                                 # nothing is left under the status line for the person back on the remote
        page.close()


@pytest.mark.parametrize("width, height", [(1024, 700), (1280, 720)])
def test_the_remote_keeps_its_number_keys_and_its_disc_big_enough_to_press_on_the_lowest_screens(browser, width, height):
    with PageServer(tv=RemoteTv()) as server:
        page = tv_page(browser, server, width, height)
        laid_out(page)
        heights = [key.bounding_box()["height"] for key in page.locator(".tv-numbers button").all()]
        assert min(heights) >= 42, (width, height, min(heights))
        assert page.locator(".tv-dpad").bounding_box()["height"] >= 190
        page.close()


def test_the_trackpad_and_the_surface_answer_a_touch_within_the_frame_and_leave_it_to_the_page(browser):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page = tv_page(browser, server)

        def look(selector):
            return page.locator(selector).evaluate("n => [n.classList.contains('touching'), getComputedStyle(n).backgroundColor]")

        for selector, resting in ((PAD, "rgb(11, 18, 32)"), (SURFACE, "rgb(11, 18, 32)")):
            if selector == PAD:
                page.click("#tv-pointer")
            else:
                page.click("#tv-fullscreen")
            assert look(selector) == [False, resting]                                                # the tray
            touch_event(page, selector, "down", 100, 300)                                                  # (no frame has run: the page answers in the event itself)
            assert look(selector) == [True, "rgb(22, 36, 79)"]                                       # navy, the page's pressed colour
            touch_event(page, selector, "down", 200, 300, 2)
            touch_event(page, selector, "up", 100, 300)
            assert look(selector)[0] is True                                                         # (a finger is still down)
            touch_event(page, selector, "up", 200, 300, 2)
            assert look(selector) == [False, resting]
            style = page.locator(selector).evaluate("n => { const s = getComputedStyle(n); return [s.touchAction, s.userSelect]; }")
            assert style == ["none", "none"], selector                                               # the browser scrolls and selects nothing on it
            assert page.evaluate("""(s) => { const e = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
                document.querySelector(s).dispatchEvent(e); return e.defaultPrevented; }""", selector), selector     # and a long press opens no menu
        page.close()


def two_fingers(page, selector, steps, start=((100, 300), (220, 300))):
    """Two fingers down, then both moved by each (dx, dy) of `steps`, a frame for each, and both lifted."""
    (x1, y1), (x2, y2) = start
    touch_event(page, selector, "down", x1, y1, 1)
    touch_event(page, selector, "down", x2, y2, 2)
    for dx, dy in steps:
        x1, y1, x2, y2 = x1 + dx, y1 + dy, x2 + dx, y2 + dy
        touch_event(page, selector, "move", x1, y1, 1)
        touch_event(page, selector, "move", x2, y2, 2)
        frame(page)
    touch_event(page, selector, "up", x1, y1, 1)
    touch_event(page, selector, "up", x2, y2, 2)


def test_the_pages_frames_are_the_ones_the_server_takes_for_the_tvs_cursor(browser):
    """Through the real /ws/pointer route of the control service to the TV's cursor (the stub client of the service's own tests): what the page
    sends is what the agent reads, and it answers with no error."""
    tv = RemoteTv()
    with PageServer(tv=tv, mouse=StubMouse()) as server:
        page = page_on_a_stopped_clock(browser, server)
        open_tv_screen(page)
        page.click("#tv-pointer")
        the_socket(page)
        x, y = drag_path(page, PAD, [(5, 2)] * 12)                                                   # a path
        touch_event(page, PAD, "up", x, y)
        touch_event(page, PAD, "down", 300, 300)                                                           # a tap
        page.clock.run_for(50)
        touch_event(page, PAD, "up", 300, 300)
        two_fingers(page, PAD, [(0, -20)] * 4)                                                       # a scroll
        until(page, lambda: ("click",) in tv.cursor.calls and any(c[0] == "scroll" for c in tv.cursor.calls))
        calls = tv.cursor.calls
        moves = [c for c in calls if c[0] == "move"]
        assert all(c[3] == 0 for c in moves)                                                         # (no button is down)
        assert abs(sum(c[1] for c in moves) - 12 * scaled(5, 2)[0]) <= 1 and abs(sum(c[2] for c in moves) - 12 * scaled(5, 2)[1]) <= 1
        assert [c for c in calls if c[0] != "move"] == [("click",), ("scroll", 0, -20), ("scroll", 0, -20), ("scroll", 0, -20), ("scroll", 0, -20)]
        assert calls.index(("click",)) > max(i for i, c in enumerate(calls) if c[0] == "move")       # the order is the finger's
        touch_event(page, PAD, "down", 100, 100)                                                           # a drag: down, its moves with the button down, and up
        page.clock.run_for(HOLD_MS + 50)
        touch_event(page, PAD, "move", 130, 100)
        frame(page)
        touch_event(page, PAD, "up", 130, 100)
        until(page, lambda: calls[-1] == ("move", 0, 0, 0))
        dragged = calls[calls.index(("scroll", 0, -20)) + 4:]
        assert dragged[-1] == ("move", 0, 0, 0) and all(c[0] == "move" and c[3] == 1 for c in dragged[:-1]) and dragged[:-1]
        assert page.locator("#tv-words").is_hidden()
        page.close()


def test_the_pages_frames_are_the_ones_the_server_takes_for_the_room_pis_mouse(browser):
    mouse = StubMouse()
    with PageServer(tv=RemoteTv(), mouse=mouse) as server:
        page = page_on_a_stopped_clock(browser, server)
        open_tv_screen(page)
        page.click("#tv-pi-mouse")
        the_socket(page)
        until(page, lambda: mouse.opened >= 1)                                                       # the mouse is made as the socket opens
        x, y = drag_path(page, PAD, [(-5, 3)] * 12)
        touch_event(page, PAD, "up", x, y)
        touch_event(page, PAD, "down", 300, 300)
        page.clock.run_for(50)
        touch_event(page, PAD, "up", 300, 300)
        touch_event(page, PAD, "down", 100, 300, 1)                                                        # a two-finger tap: the right button
        touch_event(page, PAD, "down", 220, 300, 2)
        page.clock.run_for(50)
        touch_event(page, PAD, "up", 100, 300, 1)
        touch_event(page, PAD, "up", 220, 300, 2)
        two_fingers(page, PAD, [(0, -20)] * 4)                                                       # a scroll, in the wheel's notches
        touch_event(page, PAD, "down", 100, 100)                                                           # and a drag
        page.clock.run_for(HOLD_MS + 50)
        touch_event(page, PAD, "move", 130, 100)
        frame(page)
        touch_event(page, PAD, "up", 130, 100)
        until(page, lambda: mouse.calls and mouse.calls[-1] == ("release", "left"))
        calls = mouse.calls
        assert [c for c in calls if c[0] != "move"] == [("click", "left"), ("click", "right"), ("scroll", -1), ("scroll", -1), ("press", "left"), ("release", "left")]
        path = [c for c in calls[:calls.index(("click", "left"))] if c[0] == "move"]
        assert abs(sum(c[1] for c in path) - 12 * scaled(-5, 3)[0]) <= 1 and abs(sum(c[2] for c in path) - 12 * scaled(-5, 3)[1]) <= 1
        assert calls.index(("press", "left")) < max(i for i, c in enumerate(calls) if c[0] == "move") < calls.index(("release", "left"))   # the drag's moves are between
        assert page.locator("#tv-words").is_hidden()
        page.close()


def test_what_the_server_refuses_comes_back_to_the_page_in_words(browser):
    tv = RemoteTv()
    tv.fail_pointer = TvError("The TV is not answering at 10.0.0.41")
    with PageServer(tv=tv, mouse=StubMouse()) as server:
        page = tv_page(browser, server)
        page.click("#tv-pointer")
        the_socket(page)
        touch_event(page, PAD, "down", 100, 100)                                                           # (the page's own clock: the frames come as they do)
        touch_event(page, PAD, "move", 140, 100)
        page.wait_for_function("document.getElementById('tv-words').textContent === 'The TV is not answering at 10.0.0.41'", timeout=5000)
        touch_event(page, PAD, "up", 140, 100)
        assert page.locator("#tv-words").is_visible()
        page.close()


def test_the_page_never_asks_the_tvs_cursor_for_a_right_click(browser):
    """The TV's cursor has no right button: the server would refuse it in words, and the page has no reason to ask."""
    tv = RemoteTv()
    with PageServer(tv=tv, mouse=StubMouse()) as server:
        page = tv_page(browser, server)
        page.click("#tv-pointer")
        for finger, x in ((1, 100), (2, 220)):
            touch_event(page, PAD, "down", x, 300, finger)
        for finger, x in ((1, 100), (2, 220)):
            touch_event(page, PAD, "up", x, 300, finger)
        page.wait_for_timeout(400)
        assert tv.cursor.calls == [] and page.locator("#tv-words").is_hidden()
        page.close()


def test_the_modes_row_and_the_surface_wear_the_same_colours_in_a_meeting_as_outside_one(browser):
    """The meeting state outlines every button in the page, filled ones included: the modes' fills, and the surface's, must win over that."""
    colours = []
    for meeting in (False, True):
        with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
            page = open_page(browser, server)
            if meeting:
                join_by_id(page)
                meeting_tv_button(page).click()
            else:
                open_tv_screen(page)
            page.click("#tv-pointer")
            page.click("#tv-fullscreen")
            page.mouse.move(0, 0)                                                                    # (a hovered button is another colour)
            colours.append(page.evaluate("""() => ['#tv-pointer', '#tv-pi-mouse', '#tv-fullscreen', '#touchpad-done', '#touchpad-tv', '#touchpad-pi'].map(s => {
                const c = getComputedStyle(document.querySelector(s)); return [c.backgroundColor, c.borderTopColor, c.color]; })"""))
            page.close()
    assert colours[0] == colours[1]                                                                  # the same in a call as out of one
    blue, clear, periwinkle = "rgb(27, 82, 229)", "rgba(0, 0, 0, 0)", "rgb(189, 206, 255)"
    assert [c[0] for c in colours[0]] == [blue, clear, clear, blue, blue, clear]                     # Pointer is on, the other two are not; Done and the target that is on are blue
    assert [colours[0][i][1] for i in (1, 2)] == [periwinkle, periwinkle] and colours[0][3][1] == blue    # the two that are off are outlined; Done is filled to its edge
    assert all(c[2] == "rgb(255, 255, 255)" for c in colours[0])


def laid_out(page):
    """The page's font and icons are in, so that what is measured is the layout it keeps (a font that comes late changes how the words wrap)."""
    page.evaluate("document.fonts.ready.then(() => window.croomPage.iconsReady)")
    page.wait_for_timeout(100)


ALL_INPUTS = [{"id": f"HDMI_{n}", "label": f"HDMI {n}"} for n in range(1, 5)] + [{"id": "AV_1", "label": "AV 1"}, {"id": "COMP_1", "label": "Component"},
                                                                                      {"id": "LIVE_TV", "label": "Live TV"}, {"id": "SCART", "label": "Scart"}]
MODE_STATES = {
    "the remote": (lambda: RemoteTv(), lambda page: None),
    "the row of inputs open with eight inputs": (lambda: RemoteTv(inputs=ALL_INPUTS), lambda page: page.click("#tv-input")),
    "a two-line refusal": (lambda: failing(RemoteTv(), REFUSAL), lambda page: page.click('[data-tv="key:HOME"]')),
    "a refusal that wraps onto more lines": (lambda: failing(RemoteTv(), REFUSAL + ", " + REFUSAL), lambda page: page.click('[data-tv="key:HOME"]')),
    "the TV pointer on": (lambda: RemoteTv(), lambda page: page.click("#tv-pointer")),
    "the room Pi mouse on": (lambda: RemoteTv(), lambda page: page.click("#tv-pi-mouse")),
    "a TV that is asking to pair again": (lambda: RemoteTv(on=False, reachable=False, volume=None, prompting=True), lambda page: None),
    "a card with its full-screen touchpad": (lambda: RemoteTv(paired=False), lambda page: None),
    "the surface": (lambda: RemoteTv(), lambda page: page.click("#tv-fullscreen")),
}
LAYOUT_SIZES = [(1024, 700), (1024, 768), (1280, 720), (1280, 800), (1920, 1200)]


@pytest.mark.parametrize("width, height", LAYOUT_SIZES)
@pytest.mark.parametrize("state", list(MODE_STATES))
def test_the_modes_row_is_clear_of_everything_above_it_and_wholly_on_the_screen_at_every_size(browser, state, width, height):
    make, reach = MODE_STATES[state]
    with PageServer(tv=make(), mouse=StubMouse()) as server:
        page = tv_page(browser, server, width, height)
        reach(page)
        if "refusal" in state:
            page.wait_for_selector("#tv-words:not([hidden])", timeout=5000)
        laid_out(page)
        if state == "the surface":
            found = page.evaluate(SURFACE_PROBE)
            assert found["off"] == [], (state, width, height, "off the screen")
            assert found["overlapping"] == [] and found["stripHeight"] <= 100 and found["padHeight"] >= height - 100, (state, width, height, found)
            assert fits(page), (state, width, height, "the page under the surface scrolls")
        else:
            assert_the_remote_is_laid_out(page, f"{state} at {width} by {height}")
        page.close()


def test_the_longest_words_the_agent_sends_leave_the_modes_row_most_of_its_gap_at_the_lowest_screen(browser):
    """A refusal runs to about 300 characters (the room Pi's mouse that could not be made), which is four lines at 1024 wide: the d-pad and the number pad
    give way for it with room to spare, so that a line more, in the Pi's own Chromium, does not reach the row under them."""
    with PageServer(tv=failing(RemoteTv(), REFUSAL + ", " + REFUSAL)) as server:
        page = tv_page(browser, server, 1024, 700)
        page.click('[data-tv="key:HOME"]')
        page.wait_for_selector("#tv-words:not([hidden])", timeout=5000)
        laid_out(page)
        assert page.evaluate(LAYOUT_PROBE)["belowTheKeys"] <= -8
        page.close()


@pytest.mark.parametrize("state", ["the remote", "the TV pointer on", "a card with its full-screen touchpad", "the surface"])
def test_the_modes_row_and_the_surface_stack_on_a_phone_without_scrolling_sideways(browser, state):
    make, reach = MODE_STATES[state]
    with PageServer(tv=make(), mouse=StubMouse()) as server:
        page = tv_page(browser, server, 390, 844)
        reach(page)
        laid_out(page)
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"), state
        for button in page.locator("#tv-modes button:visible, #touchpad button:visible").all():
            box = button.bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= 390, button.inner_text()
        if state == "the TV pointer on":
            assert page.locator("#tv-trackpad").bounding_box()["height"] >= 200                      # a pad to put a finger on, and not a line of words
        if state == "the surface":
            assert page.locator("#touchpad-title").bounding_box()["width"] >= 300                    # the title has a line of its own, the switch and Done under it
        page.close()


# --- fix round 1: what a touch is, decided once (a tap, a move or a hold) ---

class RealTouch:
    """Real touches through the browser's own input pipeline (CDP Input.dispatchTouchEvent): real pointer events of type touch, with the capture and the
    timestamps a panel gives them. The page needs a touch screen (`has_touch`). The protocol lists every finger that is down with each call."""

    def __init__(self, page):
        self.cdp, self.down = page.context.new_cdp_session(page), {}

    def _send(self, kind):
        self.cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": [{"x": x, "y": y, "id": i} for i, (x, y) in sorted(self.down.items())]})

    def start(self, x, y, finger=1):
        self.down[finger] = (x, y)
        self._send("touchStart")

    def move(self, x, y, finger=1):
        self.down[finger] = (x, y)
        self._send("touchMove")

    def end(self, finger=1):
        self.down.pop(finger, None)
        self._send("touchEnd")


def real_pad_page(browser, server, mode, width=1280, height=800):
    """The TV view on the page's own clock, on a touch screen, with a PointerLog for the agent and `mode` on. Returns the page, the log and the middle of
    the trackpad."""
    page = browser.new_page(viewport={"width": width, "height": height}, has_touch=True)
    log = PointerLog(page)
    page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
    page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
    open_tv_screen(page)
    page.click(mode)
    the_socket(page)
    box = page.locator(PAD).bounding_box()
    return page, log, (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


def test_a_press_and_hold_that_starts_gently_still_sends_down_before_its_moves(browser):
    """Real touches. A finger rests for half a second and then goes, gently, 2 px at a frame: it is a press and hold, because it rested before it first
    moved, however gently it moves after; what it sends is `down` first, and its moves from there."""
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page, log, (x, y) = real_pad_page(browser, server, "#tv-pi-mouse")
        finger = RealTouch(page)
        finger.start(x, y)
        page.wait_for_timeout(HOLD_MS + 150)
        for _ in range(15):
            x += 2
            finger.move(x, y)
            page.wait_for_timeout(16)
        finger.end()
        types = [f["type"] for f in log.got()]
        assert types[:1] == ["down"] and types[-1:] == ["up"] and set(types[1:-1]) == {"move"} and len(types) > 4, types
        page.close()


def test_a_slow_move_from_the_landing_moves_the_cursor_and_never_presses_the_button(browser):
    """Real touches. A finger that creeps from the moment it lands (1 px every 40 ms) is a move from its first real travel, for good: slowly,
    carefully placing the cursor is not a drag, and must not end in a click."""
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page, log, (x, y) = real_pad_page(browser, server, "#tv-pi-mouse")
        finger = RealTouch(page)
        finger.start(x, y)
        for _ in range(25):
            page.wait_for_timeout(40)
            x += 1
            finger.move(x, y)
        finger.end()
        types = [f["type"] for f in log.got()]
        assert len(types) >= 8 and set(types) == {"move"}, types
        page.close()


def test_a_resting_finger_that_rolls_a_little_sends_nothing(browser):
    """Real touches. A finger that rests for most of a second and rolls by up to 2 px as it does moves nothing and clicks nothing."""
    rolls = random.Random(3)
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page, log, (x, y) = real_pad_page(browser, server, "#tv-pointer")
        finger = RealTouch(page)
        finger.start(x, y)
        for _ in range(24):
            page.wait_for_timeout(30)
            finger.move(x + rolls.choice((-2, 0, 2)), y + rolls.choice((-2, 0, 2)))
        finger.end()
        assert log.got() == []
        page.close()


def test_a_touch_that_has_moved_is_a_mover_for_good_and_a_rest_after_it_makes_no_drag(browser):
    """The kind of a touch is decided once, at its first real travel: this one moved after 100 ms, so it is a mover, and going on after a long rest
    does not turn it into a press and hold."""
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page, log = pad_page(browser, server, "#tv-pi-mouse")
        touch_event(page, PAD, "down", 100, 100)
        page.clock.run_for(100)
        touch_event(page, PAD, "move", 104, 100)
        frame(page)
        page.clock.run_for(500)
        touch_event(page, PAD, "move", 110, 100)
        frame(page)
        touch_event(page, PAD, "up", 110, 100)
        assert [f["type"] for f in log.got()] == ["move"]
        page.close()


def test_a_hold_that_starts_gently_holds_its_travel_to_eight_pixels_and_then_sends_down_and_the_travel(browser):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page, log = pad_page(browser, server, "#tv-pi-mouse")
        touch_event(page, PAD, "down", 100, 100)
        page.clock.run_for(HOLD_MS + 50)
        touch_event(page, PAD, "move", 104, 100)                                                   # the first real travel, after a rest: a press and hold
        frame(page)
        assert log.got() == []                                                                     # (4 px of the 8: nothing goes, not even as a move)
        touch_event(page, PAD, "move", 108, 100)
        frame(page)
        touch_event(page, PAD, "up", 108, 100)
        assert log.got() == [{"target": "pi", "type": "down"}, {"target": "pi", "type": "move", "dx": 14, "dy": 0}, {"target": "pi", "type": "up"}]   # 2 x 7.04
        page.close()


def test_a_finger_that_rolls_a_little_while_it_rests_sends_nothing_however_long_it_rests(browser):
    rolls = random.Random(5)
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page, log = pad_page(browser, server, "#tv-pointer")
        touch_event(page, PAD, "down", 100, 100)
        for _ in range(40):                                                                        # more than a second of it
            page.clock.run_for(30)
            touch_event(page, PAD, "move", 100 + rolls.choice((-2, 0, 2)), 100 + rolls.choice((-2, 0, 2)))
        page.clock.run_for(30)
        touch_event(page, PAD, "up", 100, 100)
        assert log.got() == []
        page.close()


# --- fix round 1: the hold shown, the touch timed by its events, the socket's words at once ---

def edge(page, selector):
    """Whether the pad shows a hold is armed, and the colours it draws it in: its border (the trackpad's) and its inset ring (the surface's)."""
    return page.locator(selector).evaluate("n => { const s = getComputedStyle(n); return [n.classList.contains('armed'), s.borderTopColor, s.boxShadow]; }")


@pytest.mark.parametrize("selector, open_surface", [(PAD, False), (SURFACE, True)], ids=["the trackpad", "the surface"])
def test_the_pad_shows_a_hold_is_armed_once_the_finger_has_rested_long_enough_and_not_before(browser, selector, open_surface):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page, log = pad_page(browser, server, "#tv-pointer")
        if open_surface:
            page.click("#tv-fullscreen")
        blue = "rgb(27, 82, 229)"
        resting = edge(page, selector)
        assert resting[0] is False and blue not in resting[1] + resting[2]
        touch_event(page, selector, "down", 300, 300)
        page.clock.run_for(HOLD_MS - 50)
        assert edge(page, selector)[0] is False                                                      # (a finger just down is a tap or a move in the making)
        page.clock.run_for(100)
        armed = edge(page, selector)
        assert armed[0] is True and blue in armed[1] + armed[2], armed                               # the blue of the mode that is on
        touch_event(page, selector, "move", 340, 300)                                                # the drag goes on, and the pad still says so
        frame(page)
        assert edge(page, selector)[0] is True
        touch_event(page, selector, "up", 340, 300)
        assert edge(page, selector) == resting                                                       # and it is as it was when the finger lifts
        page.close()


def test_a_touch_that_moved_in_time_or_that_became_two_fingers_is_never_shown_armed(browser):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page, log = pad_page(browser, server, "#tv-pi-mouse")
        touch_event(page, PAD, "down", 300, 300)
        page.clock.run_for(200)
        touch_event(page, PAD, "move", 306, 300)                                                     # a mover, from 200 ms
        frame(page)
        page.clock.run_for(500)
        assert edge(page, PAD)[0] is False
        touch_event(page, PAD, "up", 306, 300)
        touch_event(page, PAD, "down", 300, 300, 1)
        page.clock.run_for(200)
        touch_event(page, PAD, "down", 400, 300, 2)                                                  # a second finger: scrolling, not a hold
        page.clock.run_for(500)
        assert edge(page, PAD)[0] is False
        touch_event(page, PAD, "up", 300, 300, 1)
        touch_event(page, PAD, "up", 400, 300, 2)
        touch_event(page, PAD, "down", 300, 300)                                                     # and a hold that the browser takes away is over
        page.clock.run_for(HOLD_MS + 50)
        assert edge(page, PAD)[0] is True
        touch_event(page, PAD, "cancel", 300, 300)
        assert edge(page, PAD)[0] is False
        page.close()


def test_the_pad_stops_showing_a_hold_when_a_second_finger_lands_and_when_the_pad_is_put_away(browser):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page, log = pad_page(browser, server, "#tv-pi-mouse")
        touch_event(page, PAD, "down", 300, 300, 1)
        page.clock.run_for(HOLD_MS + 50)
        assert edge(page, PAD)[0] is True
        touch_event(page, PAD, "down", 400, 300, 2)                                                  # a second finger: it is a scroll now, and no hold is waiting
        assert edge(page, PAD)[0] is False
        touch_event(page, PAD, "up", 300, 300, 1)
        touch_event(page, PAD, "up", 400, 300, 2)
        touch_event(page, PAD, "down", 300, 300)                                                     # and the view is left with a hold armed: it is not armed when it is back
        page.clock.run_for(HOLD_MS + 50)
        assert edge(page, PAD)[0] is True
        page.click("#back-from-tv")
        assert edge(page, PAD)[0] is False
        page.close()


def test_a_tap_is_timed_by_the_events_and_not_by_when_the_page_gets_to_them(browser):
    """The Pi's main thread may be away when a finger lifts. The stamp of the event is when the panel saw it: a lift 100 ms after the landing is a tap
    though the page reaches it half a second later (here the page's clock runs on 500 ms between the two)."""
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page, log = pad_page(browser, server, "#tv-pointer")
        landed = page.evaluate("performance.now()")
        touch_event(page, PAD, "down", 300, 300, stamp=landed)
        page.clock.run_for(500)
        touch_event(page, PAD, "up", 300, 300, stamp=landed + 100)
        assert log.got() == [{"target": "tv", "type": "tap"}]
        page.close()


def test_a_hold_is_timed_by_the_events_too(browser):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page, log = pad_page(browser, server, "#tv-pi-mouse")
        landed = page.evaluate("performance.now()")
        touch_event(page, PAD, "down", 300, 300, stamp=landed)
        touch_event(page, PAD, "move", 340, 300, stamp=landed + HOLD_MS + 20)                        # the page's clock has not moved: the finger rested, by the stamps
        frame(page)
        assert edge(page, PAD)[0] is True                                                            # and the pad says so, though its own timer has not run out
        touch_event(page, PAD, "up", 340, 300, stamp=landed + HOLD_MS + 40)
        assert [f["type"] for f in log.got()] == ["down", "move", "up"]
        touch_event(page, PAD, "down", 300, 300, stamp=landed + 1000)                                # and one that moved at once, by the stamps, is a mover however long the page took
        page.clock.run_for(HOLD_MS + 200)
        touch_event(page, PAD, "move", 340, 300, stamp=landed + 1040)
        frame(page)
        touch_event(page, PAD, "up", 340, 300, stamp=landed + 1060)
        assert [f["type"] for f in log.got()][3:] == ["move"]
        page.close()


@pytest.mark.parametrize("roll", [0, 4], ids=["a finger that stays", "a finger that rolls 4 px"])
def test_a_tap_survives_the_pages_main_thread_being_busy_when_the_finger_lifts(browser, roll):
    """A busy page (the Pi's slow main thread) handles a lift late: a tap of 150 ms that is handled 220 ms after the landing is still a tap, and the roll of
    its fingertip (past the slop, within the tap's 8 px) does not go out as a move just because the page got to the lift late. The events are made as the
    panel made them, with their stamps, and the page is kept busy between the second and the third."""
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page, log, (x, y) = real_pad_page(browser, server, "#tv-pointer")
        page.evaluate("""([selector, x, y, roll]) => {
            const made = (kind, at, dx) => { const e = new PointerEvent(kind, { pointerId: 1, pointerType: 'touch', isPrimary: true, clientX: x + dx, clientY: y, bubbles: true,
                cancelable: true, button: 0, buttons: kind === 'pointerup' ? 0 : 1 }); Object.defineProperty(e, 'timeStamp', { value: at }); return e; };
            const node = document.querySelector(selector), t0 = performance.now();
            node.dispatchEvent(made('pointerdown', t0, 0));
            if (roll) node.dispatchEvent(made('pointermove', t0 + 60, roll));
            const end = performance.now() + 220;
            while (performance.now() < end) { /* the page is busy */ }
            node.dispatchEvent(made('pointerup', t0 + 150, roll)); }""", [PAD, x, y, roll])
        assert log.got() == [{"target": "tv", "type": "tap"}]
        page.close()


@pytest.mark.parametrize("where", ["the trackpad's how-line", "the surface's mark"])
def test_the_reconnecting_words_follow_the_socket_at_once_even_while_the_pages_own_status_is_out(browser, where):
    """The page draws the rest of itself from the status it polls, and stops short while that poll fails (the agent restarting is when that happens).
    The words about the socket do not wait for it: they are gone within 100 ms of the socket opening again."""
    disc = where.startswith("the trackpad")
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page, log = pad_page(browser, server, "#tv-pi-mouse" if disc else None)
        if not disc:
            page.click("#tv-fullscreen")
            the_socket(page)
        words = page.locator("#tv-trackpad-how") if disc else page.locator("#touchpad-link")
        page.route("**/api/status", lambda route: route.abort())                                     # the status does not come
        page.clock.run_for(2500)
        page.wait_for_function("document.body.dataset.state === 'offline'", timeout=5000)           # (the page is offline: its render stops short)
        log.drop()
        the_socket(page, "down")
        assert (words.inner_text() == "Reconnecting\u2026") if disc else words.is_visible()
        page.clock.run_for(1001)
        the_socket(page)
        page.wait_for_timeout(100)
        assert (words.inner_text() != "Reconnecting\u2026") if disc else words.is_hidden()
        page.close()


# --- fix round 1: the words, the colours and the focus of the surface ---

def contrast(foreground, background):
    """The WCAG contrast ratio of a text colour (rgba, over `background`) on a background colour (rgb), both as the browser writes them."""
    def lum(rgb):
        lin = [(c / 255 / 12.92) if c / 255 <= 0.03928 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    *ink, alpha = [float(v) for v in re.findall(r"[\d.]+", foreground)] + ([1.0] if foreground.startswith("rgb(") else [])
    ground = [float(v) for v in re.findall(r"[\d.]+", background)][:3]
    shown = [a * alpha + b * (1 - alpha) for a, b in zip(ink[:3], ground)]
    light, dark = sorted((lum(shown), lum(ground)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_the_surfaces_hint_reads_at_four_and_a_half_to_one_or_better_resting_and_pressed(browser):
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page = tv_page(browser, server)
        page.click("#tv-fullscreen")
        colours = lambda: page.evaluate("""() => [getComputedStyle(document.getElementById('touchpad-how')).color,
            getComputedStyle(document.getElementById('touchpad-surface')).backgroundColor]""")
        resting = colours()
        assert contrast(*resting) >= 4.5, resting
        touch_event(page, SURFACE, "down", 600, 500)                                                 # a finger on the surface lights it: the words must still read
        pressed = colours()
        assert pressed[1] != resting[1] and contrast(*pressed) >= 4.5, pressed
        touch_event(page, SURFACE, "up", 600, 500)
        page.close()


def test_the_page_is_inert_under_the_surface_so_that_focus_cannot_leave_it_and_takes_focus_again_when_it_goes(browser):
    """While the surface is up, a key can only land on its own controls (or on nothing, past the last of them: the test browser has no address bar to go
    to); never on a button of the page under it, out of sight."""
    with PageServer(tv=RemoteTv(), mouse=StubMouse()) as server:
        page = tv_page(browser, server)
        under = "[...document.querySelectorAll('header.top, main.screen')].map(n => n.hasAttribute('inert'))"
        assert page.evaluate(under) == [False, False]
        page.click("#tv-fullscreen")
        assert page.evaluate(under) == [True, True]
        seen = set()
        for _ in range(8):                                                                           # round the surface's own controls, and round again
            page.keyboard.press("Tab")
            seen.add(page.evaluate("document.activeElement.id || document.activeElement.tagName"))
            assert page.evaluate("document.activeElement.closest('header.top, main.screen') === null"), seen
        assert {"touchpad-tv", "touchpad-pi", "touchpad-done"} <= seen, seen                          # (its own controls are still reached)
        page.click("#touchpad-done")
        assert page.evaluate(under) == [False, False]
        assert page.evaluate("document.activeElement.id") == "tv-fullscreen"                         # the button that opened it, as before
        page.keyboard.press("Shift+Tab")
        assert page.evaluate("document.activeElement.id") == "tv-pi-mouse"                           # and the page's buttons take focus again
        page.close()


# --- the meeting screen, drawn from the control model (spec 2026-10-13, sections 3 and 6.1) ---

LONG_MEETING_TITLE = "Quarterly planning review with the regional leadership team and the product owners of every office"   # 98 characters: as long as a booking's title gets

MORE_PEOPLE = ["Priya Nair", "Tom Becker", "Maria Santos", "Chen Wei", "Olu Adeyemi", "Sara Lindqvist", "Dmitri Volkov", "Hana Kobayashi", "Luis Ortega"]


def a_roster(extra=0):
    """The stub's three people (the room, the host Ann Lee, Raj Patel with his hand up and muted), then `extra` more, each with an id of their own."""
    people = [dict(p) for p in STUB_PEOPLE]
    for i in range(extra):
        name = MORE_PEOPLE[i] if i < len(MORE_PEOPLE) else f"Guest {i + 1}"
        people.append({"id": 16777219 + i, "name": name, "muted": False, "video": True, "hand": False, "host": False, "me": False})
    return people


def set_people(server, people, count=None):
    server.meeting.current_meeting.people = {"count": len(people) if count is None else count, "list": people}


def a_chat(count, unread=0, first=1):
    """`count` messages of a chat, numbered from `first`, the first of all at 8:00 PM UTC and one a minute after another."""
    return {"unread": unread, "messages": [
        {"from": f"Person {n}", "text": f"Message number {n}", "at": f"2026-10-13T20:{n - 1:02d}:00Z"} for n in range(first, first + count)]}


def test_the_meeting_screen_offers_every_available_control_from_the_model(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        assert texts(page, "#meeting-actions") == ["Mute", "Turn camera off", "Raise hand", "Reactions", "Leave"]
        assert texts(page, "#meeting-secondary") == ["Captions on", "Gallery view", "Hide self view", "Participants (3)", "Chat", "TV"]   # a participant: no Host
        page.locator("#meeting-secondary .button", has_text="Captions on").click()
        page.wait_for_function("document.querySelector('#meeting-secondary .button').textContent.includes('Captions off')", timeout=5000)
        assert server.meeting.controls_called == [("captions", None)]
        page.locator("#meeting-actions .button", has_text="Reactions").click()
        page.wait_for_function("document.querySelector('#reaction-row')", timeout=5000)
        page.locator("#reaction-row .button", has_text="Clap").click()
        page.wait_for_function("!document.querySelector('#reaction-row')", timeout=5000)
        assert server.meeting.controls_called[-1] == ("reactions", "Clap")      # Zoom's own name for it: its picker has no emoji
        assert fits(page)
        page.close()


@pytest.mark.parametrize("width, height", [(1280, 800), (1920, 1080), (1920, 1200)])
def test_the_meeting_screen_with_everything_still_fits_and_hides_the_unavailable(browser, width, height):
    """The fixed screen (spec 2026-10-12): the host's whole screen, with the longest words every button can have, the notice, a waiting
    dialog's card, a 98-character title, the reaction row in place of the secondary row, and the TV's button after the host's six."""
    skip_if_day_ends_within(1)                                               # the booking starts in 5 minutes, and the service lists today's only
    with PageServer(role="host", calendar_events=[event("e1", LONG_MEETING_TITLE, 5)]) as server:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        page.click("#actions button:has-text('Join now')")                   # this booking, so the call carries its long title
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        assert page.locator("#meeting-detail").inner_text() == f"{LONG_MEETING_TITLE}, Zoom"
        meeting = server.meeting.current_meeting
        meeting.is_muted = True                                              # Unmute, Captions off, Participants (120), Chat (12): the widest of each
        meeting.controls["captions"]["on"] = True
        set_people(server, a_roster(9), count=120)                           # the page lists up to 50 of them, and the count is whole
        meeting.chat = {"unread": 12, "messages": []}
        meeting.notice = "This meeting is being recorded and transcribed."
        meeting.dialog = {"id": 1, "title": "Unmute?", "text": "The host would like you to unmute.", "buttons": ["Unmute", "Stay muted"]}
        page.wait_for_function("document.querySelector('#meeting-dialog:not([hidden])')", timeout=5000)
        assert texts(page, "#meeting-actions") == ["Unmute", "Turn camera off", "Raise hand", "Reactions", "Leave"]
        assert texts(page, "#meeting-secondary") == ["Captions off", "Gallery view", "Hide self view", "Participants (120)", "Chat (12)", "Host", "TV"]
        assert in_one_row(page, "#meeting-secondary"), "the seven buttons of the second row wrapped"
        assert page.locator("#meeting-notice").inner_text() == "This meeting is being recorded and transcribed."
        assert fits(page), "the meeting screen scrolls with every control, the notice, the dialog's card and a 98-character title"
        page.locator("#meeting-actions .button", has_text="Leave").click()   # the longest words of the row: "Tap again to leave"
        assert page.locator("#meeting-actions .button.danger").inner_text() == "Tap again to leave"
        assert fits(page), "the meeting screen scrolls while Leave asks for its second tap"
        page.locator("#meeting-actions .button", has_text="Reactions").click()
        page.wait_for_function("document.querySelector('#reaction-row')", timeout=5000)
        assert fits(page), "the meeting screen scrolls with the reaction row open"
        page.locator("#reaction-row .button", has_text="Cancel").click()
        page.wait_for_function("!document.querySelector('#reaction-row')", timeout=5000)
        assert in_one_row(page, "#meeting-secondary") and fits(page), "the second row wraps or the screen scrolls once the reaction row has closed"
        # What is not available is not drawn, and the host's screen still fits without it.
        meeting.controls["raise_hand"] = {"available": False}
        page.wait_for_function("document.querySelectorAll('#meeting-actions .button').length === 4", timeout=5000)
        assert texts(page, "#meeting-actions")[:3] == ["Unmute", "Turn camera off", "Reactions"]
        assert "Host" in texts(page, "#meeting-secondary")
        assert page.locator("#meeting-secondary .button", has_text="Chat (12)").count() == 1
        assert fits(page)
        page.close()


LONGEST_NOTICE = ("Zoom AI is on and the host has turned on AI Companion for this meeting and anyone who stays agrees to it " * 4)[:399] + "!"   # one sentence of the 400 characters the bridge sends at most


def test_a_notice_of_the_longest_sentence_is_two_lines_with_the_whole_of_it_in_its_tooltip(browser):
    """A one-sentence description, or one ended with a ! or a ?, is up to 400 characters under the title, where the 1280 by 800 screen has room for
    two lines (it has 16 px to spare with them): the line is cut at two with an ellipsis, and the whole of it is the line's tooltip."""
    assert len(LONGEST_NOTICE) == 400
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        notice = page.locator("#meeting-notice")
        height = "document.getElementById('meeting-notice').getBoundingClientRect().height"
        server.meeting.current_meeting.notice = "This meeting is being recorded."
        poll_until(page, "document.getElementById('meeting-notice').textContent !== ''")
        one_line = page.evaluate(height)
        assert one_line > 0 and notice.get_attribute("title") == "This meeting is being recorded."
        server.meeting.current_meeting.notice = LONGEST_NOTICE
        poll_until(page, "document.getElementById('meeting-notice').textContent.length === 400")
        assert 1.8 * one_line < page.evaluate(height) < 2.2 * one_line                          # two lines, not the seven the sentence needs at this width
        assert page.evaluate("(() => { const n = document.getElementById('meeting-notice'); return n.scrollHeight > n.clientHeight; })()")      # and the rest is cut
        assert notice.get_attribute("title") == LONGEST_NOTICE and page.evaluate("document.getElementById('meeting-notice').textContent") == LONGEST_NOTICE
        assert fits(page)
        server.meeting.current_meeting.notice = ""
        poll_until(page, "document.getElementById('meeting-notice').textContent === ''")
        assert notice.is_hidden() and notice.get_attribute("title") == ""                         # no line, no tooltip
        page.close()


@pytest.mark.parametrize("width, height", [(1280, 800), (1920, 1080), (1920, 1200)])
def test_the_busiest_meeting_screen_with_the_longest_notice_still_fits(browser, width, height):
    """The host's whole screen with the notice at its longest, the longest card text, a two-line refusal and Leave asking for its second tap: the
    state that ends 16 px above the bottom at the table's 1280 by 800 when the notice is two lines, and so when it is a sentence of 400 characters."""
    refusal = "Zoom's captions button is not on the screen yet; try again in a moment, and if it still does not work ask the host whether captions are allowed in this meeting."
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server, width, height)
        join_by_id(page)
        meeting = server.meeting.current_meeting
        meeting.is_muted = True
        meeting.controls["captions"]["on"] = True
        set_people(server, a_roster(9), count=120)
        meeting.chat = {"unread": 12, "messages": []}
        meeting.notice = LONGEST_NOTICE
        put_up(server, dict(ZOOMS_DIALOG, text=LONGEST_TEXT))
        poll_until(page, CARD)
        page.evaluate("(words) => { window.croomPage.model.error = words; window.croomPage.model.errorOn = 'meeting'; window.croomPage.render(); }", refusal)
        page.clock.run_for(700)                                                                  # the settling guard lapses
        page.locator("#meeting-actions .button", has_text="Leave").click()
        page.clock.run_for(700)
        assert page.locator("#meeting-actions .button.danger").inner_text() == "Tap again to leave"
        assert fits(page), "the meeting screen scrolls with the longest notice"
        assert in_one_row(page, "#meeting-actions") and in_one_row(page, "#meeting-secondary") and in_one_row(page, "#dialog-actions")
        page.close()


def test_in_the_shortest_window_that_still_gets_the_fixed_screen_the_notice_keeps_its_two_lines_and_main_scrolls_instead(browser):
    """1280 by 700 cannot hold the host's busiest screen, and the page scrolls inside main (nothing is squeezed to make it fit): the notice line, an item of
    the screen's grid, must not be what gives way. As a scroll container (overflow hidden) its minimum size in the grid is nothing, and the grid squeezed it to
    8 px, a sliver of the first line under the title with the card over the rest; clipped, it keeps its two lines."""
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server, 1280, 700)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        server.meeting.current_meeting.notice = "This meeting is being recorded."
        poll_until(page, "document.getElementById('meeting-notice').textContent !== ''")
        one_line = page.evaluate("document.getElementById('meeting-notice').getBoundingClientRect().height")
        server.meeting.current_meeting.notice = LONGEST_NOTICE
        put_up(server, dict(ZOOMS_DIALOG, text=LONGEST_TEXT))
        poll_until(page, CARD)
        page.clock.run_for(600)
        placed = page.evaluate("""() => { const n = document.getElementById('meeting-notice').getBoundingClientRect(), c = document.getElementById('meeting-dialog').getBoundingClientRect(),
                                           m = document.querySelector('main'); return { height: n.height, bottom: n.bottom, cardTop: c.top, scrolls: m.scrollHeight > m.clientHeight }; }""")
        assert 1.8 * one_line < placed["height"] < 2.2 * one_line, placed          # two lines, not a sliver
        assert placed["bottom"] <= placed["cardTop"] + 1, placed                     # and not under the card
        assert placed["scrolls"], placed                                              # the screen scrolls inside main instead, as it does with a two-line notice
        page.close()


@pytest.mark.parametrize("width, height, roomy", [
    (1280, 800, False),      # the room's table: the 1920 by 1200 panel lays out at 1280 by 800, the kiosk launches Chromium at 1.5x (installer/install-kiosk.sh)
    (1280, 1000, False),     # tall enough for the roomy stack to fit the host's screen, with nothing left for a dialog's card: the compact one has
    (1920, 1080, False),
    (768, 1024, False),      # a portrait tablet: the roomy sizes scroll it
    (820, 1180, False),      # a taller one, as tall as the roomy sizes ask: the width keeps it compact
    (1920, 1200, True),      # an unscaled tall wide browser
])
def test_the_roomy_sizes_of_the_meeting_screen_are_for_wide_tall_screens_only_and_the_rest_keep_the_compact_ones(browser, width, height, roomy):
    """The compact fit of the meeting screen (a tight stack, the Sound strip on two lines, 72 px menu buttons) is what 800 px needs, and
    what a dialog's card needs up to 1100 px (between 1000 and 1080 px tall the roomy stack leaves the card no room). From 1100 by
    1100 px up the screen has its roomy sizes: the rows apart, the Sound strip stacked, the menu buttons at their full height. A
    portrait tablet (768 by 1024 is tall and narrow) keeps the compact ones, which fit it, where the roomy ones scroll. fits() holds."""
    with PageServer(role="host") as server:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        sizes = page.evaluate("""() => ({
            menu: document.getElementById('open-camera-live').getBoundingClientRect().height,
            stacked: document.getElementById('volume-slider').getBoundingClientRect().bottom <= document.getElementById('quieter').getBoundingClientRect().top,
            gap: parseFloat(getComputedStyle(document.querySelector('.view[data-view=meeting]')).rowGap),
            above: parseFloat(getComputedStyle(document.getElementById('meeting-actions')).marginTop) })""")
        expected = {"menu": 160 if roomy else 72, "stacked": roomy, "gap": 24 if roomy else min(16, round(height * 0.0175)), "above": 22 if roomy else 0}
        if width < 1024:
            expected.pop("stacked")                                          # a phone's or a tablet's Sound strip wraps whatever the sizes
        assert {name: sizes[name] for name in expected} == expected, sizes
        assert fits(page)
        page.close()


def test_a_status_without_controls_keeps_the_three_buttons_and_the_tv_button(browser):
    """A platform that reports no model (the public Zoom web client, Teams, Webex) draws nothing on a guess, but the TV is the room's, not the
    platform's: its button is there in every meeting, the second row holding it alone, and it opens the remote as it does anywhere."""
    with PageServer(controls=False) as server:
        page = open_page(browser, server)
        join_by_id(page)
        assert texts(page, "#meeting-actions") == ["Mute", "Turn camera off", "Leave"]
        assert drawn_icons(page, "#meeting-actions") == ["Mute: #icon-mic", "Turn camera off: #icon-video", "Leave: #icon-phone-off"]
        assert texts(page, "#meeting-secondary") == ["TV"]                      # no model: no Participants, no Chat, nothing drawn on a guess; the TV's button alone
        assert page.locator("#meeting-secondary").is_visible()
        assert meeting_tv_button(page).count() == 1 and meeting_tv_button(page).locator("svg use").get_attribute("href") == "#icon-tv"
        assert texts(page, ".view[data-view=meeting] .menu") == ["Camera", "Upcoming meetings"]
        assert fits(page)
        meeting_tv_button(page).click()
        page.wait_for_function("document.body.dataset.view === 'tv'", timeout=5000)
        page.close()


def test_a_controls_words_show_on_the_meeting_screen(browser):
    from croom.meeting.providers.base import MeetingControlError
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-actions .button')", timeout=5000)
        server.meeting.fail_control = MeetingControlError("Zoom's raise hand button is not on the screen yet; try again in a moment")
        page.locator("#meeting-actions .button", has_text="Raise hand").click()
        page.wait_for_function("document.getElementById('meeting-message').textContent.includes('raise hand')", timeout=5000)
        assert page.locator("#meeting-actions .button", has_text="Raise hand").is_enabled()       # the tap is over: it may be made again
        assert page.locator("#meeting-actions .button", has_text="Raise hand").inner_text() == "Raise hand"   # as Zoom has it
        server.meeting.fail_control = None
        page.locator("#meeting-secondary .button", has_text="Captions on").click()                  # the next action clears the words
        page.wait_for_function("document.getElementById('meeting-message').textContent === ''", timeout=5000)
        page.close()


def test_each_control_sends_what_its_button_says_and_the_screen_shows_the_answer(browser):
    """The stub flips the obvious states as a provider reading them back from Zoom would: the label is the model's, so it changes with the answer."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        calls = server.meeting.controls_called

        def press(row, words, becomes):
            page.locator(f"#{row} .button", has_text=words).click()
            page.wait_for_function(f"document.getElementById('{row}').textContent.includes('{becomes}')", timeout=5000)

        press("meeting-secondary", "Gallery view", "Speaker view")
        press("meeting-secondary", "Speaker view", "Gallery view")
        press("meeting-secondary", "Hide self view", "Show self view")
        press("meeting-secondary", "Show self view", "Hide self view")
        press("meeting-actions", "Raise hand", "Lower hand")
        press("meeting-actions", "Lower hand", "Raise hand")
        assert calls == [("layout", "gallery"), ("layout", "speaker"), ("hide_self", None), ("hide_self", None), ("raise_hand", None), ("raise_hand", None)]
        assert texts(page, "#meeting-secondary")[:3] == ["Captions on", "Gallery view", "Hide self view"]
        page.close()


def test_the_screen_follows_the_model_at_every_poll(browser):
    """What is not available is not drawn, the counts and the words are the model's, and the host's button comes and goes with the role:
    the page draws what the status says, as it says it, and never a state the platform does not have."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        meeting = server.meeting.current_meeting
        meeting.controls["captions"] = {"available": False}
        meeting.controls["layout"]["value"] = "gallery"
        meeting.controls["hide_self"]["on"] = True
        set_people(server, a_roster(9))
        meeting.chat = {"unread": 2, "messages": []}
        page.wait_for_function("document.getElementById('meeting-secondary').textContent.includes('Chat (2)')", timeout=5000)
        assert texts(page, "#meeting-secondary") == ["Speaker view", "Show self view", "Participants (12)", "Chat (2)", "TV"]
        meeting.controls["host"]["role"] = "co-host"
        page.wait_for_function("document.getElementById('meeting-secondary').textContent.includes('Host')", timeout=5000)
        assert texts(page, "#meeting-secondary")[-2:] == ["Host", "TV"]
        meeting.controls["host"]["role"] = "participant"
        meeting.controls["raise_hand"]["on"] = True
        meeting.controls["reactions"] = {"available": False, "choices": ["Clap"]}                       # the client has no Reactions button: the configured names do not make one
        set_people(server, [])
        page.wait_for_function("document.getElementById('meeting-actions').textContent.includes('Lower hand')", timeout=5000)
        assert texts(page, "#meeting-actions") == ["Mute", "Turn camera off", "Lower hand", "Leave"]
        assert texts(page, "#meeting-secondary") == ["Speaker view", "Show self view", "Chat (2)", "TV"]      # no one listed: no Participants button
        assert drawn_icons(page, "#meeting-actions")[2] == "Lower hand: #icon-hand"                    # the words say the tap's action
        page.close()


def test_the_notice_the_dialogs_card_and_the_controls_are_drawn_only_while_connected(browser):
    """The block's title can outlive an end, and its model a join that has not finished: only a connected meeting draws any of it."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        meeting = server.meeting.current_meeting
        meeting.notice = "This meeting is being recorded."
        meeting.dialog = {"id": 1, "title": "Unmute?", "text": "The host would like you to unmute.", "buttons": ["Unmute", "Stay muted"]}
        page.wait_for_function("document.querySelector('#meeting-dialog:not([hidden])')", timeout=5000)
        assert page.locator("#meeting-notice").inner_text() == "This meeting is being recorded."
        for state, words in ((MeetingState.IN_LOBBY, "In the lobby"), (MeetingState.JOINING, "Joining")):
            server.meeting.set_state(state)
            page.wait_for_function(f"document.getElementById('meeting-state').innerText === '{words}'", timeout=5000)
            assert texts(page, "#meeting-actions") == ["Cancel"] and page.locator("#meeting-secondary .button").count() == 0, words
            assert page.locator("#meeting-notice").is_hidden() and page.locator("#meeting-dialog").is_hidden(), words
            assert fits(page), words
        server.meeting.set_state(MeetingState.CONNECTED)
        page.wait_for_function("document.querySelector('#meeting-dialog:not([hidden])')", timeout=5000)
        assert page.locator("#meeting-notice").inner_text() == "This meeting is being recorded." and page.locator("#meeting-secondary .button").count() == 6
        meeting.notice = ""
        meeting.dialog = None
        page.wait_for_function("document.querySelector('#meeting-dialog').hidden", timeout=5000)
        assert page.locator("#meeting-notice").is_hidden() and page.locator("#meeting-notice").inner_text() == ""       # no empty line's room
        page.close()


REACTIONS = [("Thumbs Up", "Thumbs up", "thumbs-up"), ("Clap", "Clap", "hand"), ("Heart", "Heart", "heart"),
             ("Joy", "Laugh", "laugh"), ("Open Mouth", "Wow", "smile"), ("Tada", "Celebrate", "party-popper")]   # Zoom's name, the table's words, the symbol


def open_reactions(page):
    page.locator("#meeting-actions .button", has_text="Reactions").click()
    page.wait_for_function("document.querySelector('#reaction-row')", timeout=5000)


def hold_requests(page, pattern):
    """The requests that match are taken in and left unanswered: the list holds their routes, for the test to answer when it chooses."""
    held = []
    page.route(pattern, lambda route: held.append(route))
    return held


def wait_for_held(page, held, count=1):
    for _ in range(250):
        if len(held) >= count:
            return
        page.wait_for_timeout(20)
    raise AssertionError(f"{len(held)} of {count} requests came")


def wait_for_calls(page, server, count):
    """Real time, whatever the page's clock: until the stub meeting has been asked for this many controls."""
    for _ in range(100):
        if len(server.meeting.controls_called) >= count:
            return
        page.wait_for_timeout(50)
    raise AssertionError(f"the meeting was asked for {server.meeting.controls_called}, not {count} controls")


def poll_until(page, condition):
    """Poll the status, answered and drawn, until the condition holds in the page: its timers are stopped, so nothing else polls. (A
    wait_for_function whose expression returns a promise is over at once: a promise is truthy.)"""
    for _ in range(100):
        page.evaluate("window.croomPage.refreshStatus()")
        if page.evaluate(condition):
            return
        page.wait_for_timeout(50)
    raise AssertionError(f"the page never showed: {condition}")


def test_the_reaction_row_offers_zooms_names_in_friendly_words_each_with_a_symbol(browser):
    """Zoom 6.5.0's picker has no emoji and the kiosk may have no emoji font: the choices are Zoom's own names, shown as words and a
    Lucide symbol, and sent back as they came."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        assert page.locator("#reaction-row").count() == 0                               # no row until Reactions opens it
        open_reactions(page)
        assert drawn_icons(page, "#reaction-row") == [f"{label}: #icon-{icon}" for _, label, icon in REACTIONS] + ["Cancel: #icon-x-circle"]
        assert page.locator("#meeting-secondary").is_hidden()                           # in the secondary row's place ...
        assert page.evaluate("document.getElementById('reaction-row').nextElementSibling.id") == "meeting-secondary"   # ... which it sits before
        assert page.get_by_role("group", name="Reactions").count() == 1                 # a screen reader is told what the row is
        assert texts(page, "#meeting-actions") == ["Mute", "Turn camera off", "Raise hand", "Reactions", "Leave"]       # the primary row stays
        shown_icons(page)                                                               # every symbol draws, at the size of its words, and costs no height
        assert fits(page)
        page.locator("#reaction-row .button", has_text="Cancel").click()                # backing out sends nothing
        page.wait_for_function("!document.querySelector('#reaction-row')", timeout=5000)
        assert page.locator("#meeting-secondary").is_visible() and server.meeting.controls_called == []
        for zoom_name, label, _ in REACTIONS:
            open_reactions(page)
            page.locator("#reaction-row .button", has_text=label).click()
            page.wait_for_function("!document.querySelector('#reaction-row') && !document.querySelector('#meeting-actions .button:disabled')", timeout=5000)
            assert server.meeting.controls_called[-1] == ("reactions", zoom_name), label
        assert len(server.meeting.controls_called) == 6
        page.close()


def test_a_reaction_the_table_has_no_words_for_shows_as_itself_with_a_smile(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        server.meeting.current_meeting.controls["reactions"]["choices"] = ["thumbs_up", "Confetti"]   # a configured name in other letters, and one never heard of
        page.wait_for_function("window.croomPage.model.status.meeting.controls.reactions.choices.length === 2", timeout=5000)
        open_reactions(page)
        assert drawn_icons(page, "#reaction-row") == ["Thumbs up: #icon-thumbs-up", "Confetti: #icon-smile", "Cancel: #icon-x-circle"]
        page.locator("#reaction-row .button", has_text="Confetti").click()
        page.wait_for_function("!document.querySelector('#reaction-row')", timeout=5000)
        assert server.meeting.controls_called == [("reactions", "Confetti")]
        server.meeting.current_meeting.controls["reactions"]["choices"] = []                 # nothing to offer: nothing to open
        page.wait_for_function("!document.getElementById('meeting-actions').textContent.includes('Reactions')", timeout=5000)
        page.close()


def test_the_reaction_row_goes_by_itself_after_ten_seconds_and_ends_with_the_meeting(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)                                  # the ten seconds are the test's to spend
        join_by_id(page)
        polls = hold_requests(page, "**/api/status")                                     # and no poll answers inside them to draw the screen again: only the timer can close the row
        open_reactions(page)
        page.clock.run_for(9000)
        assert page.locator("#reaction-row").count() == 1 and page.locator("#meeting-secondary").is_hidden()
        open_reactions(page)                                                             # Reactions again: the ten seconds start over
        page.clock.run_for(9000)
        assert page.locator("#reaction-row").count() == 1
        page.clock.run_for(2000)
        page.wait_for_function("!document.querySelector('#reaction-row')", timeout=5000)
        assert page.locator("#meeting-secondary").is_visible() and polls                 # the polls were on their way all the while
        for route in polls:                                                              # the held polls go on their way: unrouting alone leaves their route handlers waiting,
            route.continue_()                                                            # and a route left waiting is garbage that fails a later test (-W error)
        page.unroute("**/api/status")
        open_reactions(page)                                                             # an open row does not outlive its meeting
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/leave", data="{}", headers=JSON)
        poll_until(page, "document.body.dataset.view === 'idle'")
        assert page.locator("#reaction-row").count() == 0                                # it went with the meeting, and a new one does not begin with it
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}', headers=JSON)
        poll_until(page, "document.body.dataset.state === 'meeting'")
        assert page.locator("#reaction-row").count() == 0 and page.locator("#meeting-secondary").is_visible()
        page.close()


def test_a_slow_control_makes_only_its_own_button_inert_and_one_tap_is_one_request(browser):
    """A control can take 30 s inside the agent: the page is not held for it. The button that was pressed waits for its answer (a second
    tap on it goes nowhere), every other button works, and the answer is on the screen, and in the model, the moment it comes."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        held = hold_requests(page, "**/api/meeting/control")
        captions = page.locator("#meeting-secondary .button", has_text="Captions")
        hand = page.locator("#meeting-actions .button", has_text="hand")
        page.evaluate("() => { const b = document.querySelector('#meeting-secondary .button'); b.click(); b.click(); }")   # two taps, before the page can draw the button inert
        wait_for_held(page, held)
        assert held[0].request.post_data_json == {"name": "captions"}
        assert captions.is_disabled() and hand.is_enabled()
        assert page.locator("#meeting-actions .button, #meeting-secondary .button").evaluate_all("bs => bs.filter(b => b.disabled).length") == 1
        assert page.evaluate("window.croomPage.model.busy") is False                    # the page as a whole is not held
        captions.click(force=True)                                                      # a third tap, on the button that waits for its answer ...
        hand.click()                                                                    # ... and another control that goes out beside it
        wait_for_held(page, held, 2)
        page.wait_for_timeout(200)
        assert [r.request.post_data_json["name"] for r in held] == ["captions", "raise_hand"]
        assert server.meeting.controls_called == []                                     # the page held them all: none reached the meeting
        answer = page.evaluate("window.croomPage.model.status.meeting")
        answer["controls"]["captions"]["on"] = True
        held[0].fulfill(json={"meeting": answer})
        page.wait_for_function("document.querySelector('#meeting-secondary .button').textContent.includes('Captions off')", timeout=5000)
        assert captions.is_enabled() and hand.is_disabled()
        assert page.evaluate("window.croomPage.model.status.meeting.controls.captions.on") is True      # in the model, not only on the screen
        held[1].fulfill(status=409, json={"error": "Zoom's raise hand button is not on the screen yet; try again in a moment"})
        page.wait_for_function("document.getElementById('meeting-message').textContent.includes('raise hand')", timeout=5000)
        assert hand.is_enabled() and captions.inner_text() == "Captions off"
        page.close()


def test_a_poll_already_on_its_way_cannot_put_an_older_meeting_over_a_controls_answer(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        stale = page.request.get(f"http://127.0.0.1:{server.port}/api/status").json()      # what a poll sent now carries: the captions off
        stale["room"]["name"] = "Stale poll"                                                # so that the page's taking the poll in can be seen
        held = hold_requests(page, "**/api/status")
        page.evaluate("() => { window.__poll = window.croomPage.refreshStatus(); }")        # a poll goes out ...
        wait_for_held(page, held)
        page.locator("#meeting-secondary .button", has_text="Captions on").click()          # ... and the control is answered while it is out
        page.wait_for_function("document.querySelector('#meeting-secondary .button').textContent.includes('Captions off')", timeout=5000)
        held[0].fulfill(json=stale)                                                         # the old poll lands after the newer answer
        page.wait_for_function("document.getElementById('room-name').textContent === 'Stale poll'", timeout=5000)     # it was taken in ...
        assert page.locator("#meeting-secondary .button", has_text="Captions").inner_text() == "Captions off"           # ... but not its meeting
        page.unroute("**/api/status")
        poll_until(page, "document.getElementById('room-name').textContent === 'Lab'")
        assert page.locator("#meeting-secondary .button", has_text="Captions").inner_text() == "Captions off"           # and the next poll agrees
        server.meeting.current_meeting.controls["captions"]["on"] = False                                               # a poll that starts after the answer is the newest word again
        poll_until(page, "document.getElementById('meeting-secondary').textContent.includes('Captions on')")
        page.close()


def test_a_control_the_room_never_answers_is_given_up_after_35_seconds(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        held = hold_requests(page, "**/api/meeting/control")
        hand = page.locator("#meeting-actions .button", has_text="hand")
        hand.click()
        wait_for_held(page, held)
        page.clock.fast_forward(34000)
        assert hand.is_disabled() and page.locator("#meeting-message").is_hidden()          # still waiting at 34 seconds
        page.clock.fast_forward(2000)
        page.wait_for_function("document.getElementById('meeting-message').textContent.includes('did not answer')", timeout=5000)
        assert hand.is_enabled() and hand.inner_text() == "Raise hand" and len(held) == 1   # given up on: the tap may be made again, and was one request
        held[0].abort()                                                                     # the held route is let go: a route left waiting is garbage that fails a later test
        page.close()


def mute_and_camera_buttons(page):
    """The first two buttons of the primary row: Mute (or Unmute) and Turn camera off (or on)."""
    return page.locator("#meeting-actions .button").nth(0), page.locator("#meeting-actions .button").nth(1)


def test_mute_and_camera_send_the_state_their_buttons_name_and_the_answer_goes_into_the_model(browser):
    """The whole-branch review's race: a tap that asks the agent to flip whatever the room is when it arrives undoes what the host just did. The
    buttons say what a tap does (Mute, Unmute, Turn camera off, Turn camera on), and the request carries that state; the agent sets it. The answer
    is the meeting block as it is afterwards, taken into the model the moment it comes (so the button changes without waiting for a poll)."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        mute_asked, camera_asked = hold_requests(page, "**/api/meeting/mute"), hold_requests(page, "**/api/meeting/camera")
        mute, camera = mute_and_camera_buttons(page)
        assert (mute.inner_text(), camera.inner_text()) == ("Mute", "Turn camera off")
        for held, button, expected, then in (
            (mute_asked, mute, {"muted": True}, "Unmute"), (mute_asked, mute, {"muted": False}, "Mute"),
            (camera_asked, camera, {"on": False}, "Turn camera on"), (camera_asked, camera, {"on": True}, "Turn camera off"),
        ):
            asked = len(held)
            button.click()
            wait_for_held(page, held, asked + 1)
            assert held[asked].request.post_data_json == expected
            block = page.evaluate("window.croomPage.model.status.meeting")
            field, state = ("muted", expected["muted"]) if "muted" in expected else ("camera_on", expected["on"])
            block[field] = state
            block["controls"]["mute" if field == "muted" else "camera"]["on"] = (not state) if field == "muted" else state
            held[asked].fulfill(json={field: state, "meeting": block})
            page.wait_for_function("(words) => [...document.querySelectorAll('#meeting-actions .button')].some((b) => b.textContent.trim() === words)", arg=then, timeout=5000)
            assert page.evaluate(f"window.croomPage.model.status.meeting.{field}") is state          # in the model, not only on the screen
            assert page.evaluate("window.croomPage.model.busy") is False and page.locator("#meeting-actions .button:disabled").count() == 0
        page.close()


@pytest.mark.parametrize("which", ["mute", "camera"])
def test_a_tap_on_a_mute_or_camera_button_that_is_a_poll_behind_ends_as_the_button_said(browser, which):
    """The host muted the room, or the camera went off at the TV, and the page has not been told (its timers are stopped: no poll). A tap on the
    Mute it still shows asks for muted, and one on Turn camera off for off: the room stays muted and the camera off, where a toggle sent blind
    unmuted the room and turned the camera on. The answer brings the page up to date, and the next poll agrees."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        meeting = server.meeting
        buttons = mute_and_camera_buttons(page)
        button, was, now = ((buttons[0], "Mute", "Unmute") if which == "mute" else (buttons[1], "Turn camera off", "Turn camera on"))
        if which == "mute":
            meeting._mute_to(True)
        else:
            meeting._camera_to(False)
        meeting.presses.clear()
        assert button.inner_text() == was                                                               # a poll behind
        button.click()
        shows = f"[...document.querySelectorAll('#meeting-actions .button')].some((b) => b.textContent.trim() === {now!r})"
        page.wait_for_function(shows, timeout=5000)
        assert (meeting.muted, meeting.camera_on) == ((True, True) if which == "mute" else (False, False))
        assert meeting.presses == []                                                                    # nothing was pressed, and nothing flipped back
        poll_until(page, shows)                                                                         # the next poll agrees with the answer
        assert button.inner_text() == now
        page.close()


def test_a_double_tap_on_mute_is_one_request_and_only_that_button_waits_for_its_answer(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        held = hold_requests(page, "**/api/meeting/mute")
        mute, camera = mute_and_camera_buttons(page)
        page.evaluate("() => { const b = document.querySelector('#meeting-actions .button'); b.click(); b.click(); }")   # two taps, before the page can draw the button inert
        wait_for_held(page, held)
        page.wait_for_timeout(200)
        assert len(held) == 1 and held[0].request.post_data_json == {"muted": True}
        assert mute.is_disabled() and camera.is_enabled() and page.evaluate("window.croomPage.model.busy") is False    # the page as a whole is not held
        assert page.locator("#meeting-actions .button:disabled").count() == 1
        held[0].continue_()                                                                              # on to the agent
        page.wait_for_function("document.querySelector('#meeting-actions .button').textContent.trim() === 'Unmute'", timeout=5000)
        assert mute.is_enabled() and server.meeting.presses == ["mute"]
        page.close()


def test_a_slow_camera_switch_holds_only_its_own_button_and_the_dialogs_card_stays_answerable(browser):
    """The camera can take twenty seconds to switch in Zoom: it used to hold the whole page in model.busy, and with it every button on every
    screen, the waiting dialog's card included. It is a request of the meeting screen like the rest: its own button waits, and nothing else."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        held = hold_requests(page, "**/api/meeting/camera")
        mute, camera = mute_and_camera_buttons(page)
        camera.click()
        wait_for_held(page, held)
        assert held[0].request.post_data_json == {"on": False}
        assert camera.is_disabled() and mute.is_enabled() and page.evaluate("window.croomPage.model.busy") is False
        assert page.locator("#meeting-actions .button:disabled").count() == 1 and page.locator("#meeting-secondary .button:disabled").count() == 0
        put_up(server, ZOOMS_DIALOG)                                                                    # Zoom draws a dialog while the camera is switching
        poll_until(page, CARD)
        page.clock.run_for(600)                                                                         # the card's own half second of ignoring a tap
        assert page.locator("#dialog-actions .button:disabled").count() == 0
        page.locator("#dialog-actions .button", has_text="Got it").click()
        wait_for_calls(page, server, 1)
        assert server.meeting.controls_called == [("dialog", {"id": 7, "button": "Got it"})]
        page.wait_for_function("document.querySelector('#meeting-dialog').hidden", timeout=5000)
        assert camera.is_disabled()                                                                      # the camera still waits for its own answer
        block = page.evaluate("window.croomPage.model.status.meeting")
        block["camera_on"], block["controls"]["camera"]["on"] = False, False
        held[0].fulfill(json={"camera_on": False, "meeting": block})
        page.wait_for_function("document.querySelectorAll('#meeting-actions .button')[1].textContent.trim() === 'Turn camera on'", timeout=5000)
        assert camera.is_enabled()
        page.close()


def test_a_reaction_in_flight_keeps_the_reactions_button_inert_and_the_row_closed(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        held = hold_requests(page, "**/api/meeting/control")
        open_reactions(page)
        page.locator("#reaction-row .button", has_text="Clap").click()
        page.wait_for_function("!document.querySelector('#reaction-row')", timeout=5000)     # a pick closes the row at once
        wait_for_held(page, held)
        assert held[0].request.post_data_json == {"name": "reactions", "value": "Clap"}
        reactions = page.locator("#meeting-actions .button", has_text="Reactions")
        assert reactions.is_disabled() and page.locator("#meeting-secondary").is_visible()
        held[0].fulfill(json={"meeting": page.evaluate("window.croomPage.model.status.meeting")})
        page.wait_for_function("!document.querySelector('#meeting-actions .button:disabled')", timeout=5000)
        assert reactions.is_enabled()
        page.close()


@pytest.mark.parametrize("which, then", [(0, ("captions", None)), (3, ("hide_self", None)), (6, None)])   # Thumbs up over Captions, Laugh over Hide self view, Cancel over Host
def test_a_tap_on_what_a_closing_reaction_row_uncovers_is_ignored_for_half_a_second(browser, which, then):
    """The row closes at the pick and the secondary row is back under the finger: the second tap of a double tap (a thumb that lands twice,
    150 ms apart) is not meant for the button that is there now, nor is one a little slower. The clock is stopped: the half second is the
    test's to spend."""
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        open_reactions(page)
        box = page.locator("#reaction-row .button").nth(which).bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.mouse.click(x, y)                                                       # the pick (or Cancel): the row closes, and a secondary button is under this spot
        page.wait_for_function("!document.querySelector('#reaction-row')", timeout=5000)
        if then:
            wait_for_calls(page, server, 1)                                          # the pick's own request
        asked = list(server.meeting.controls_called)
        page.clock.run_for(150)
        page.mouse.click(x, y)                                                       # 150 ms after the pick: the second tap of a double tap
        page.clock.run_for(300)
        page.mouse.click(x, y)                                                       # 450 ms: a slow one
        page.wait_for_timeout(300)                                                   # (real time) long enough for a request to have gone out
        assert server.meeting.controls_called == asked and page.evaluate("document.body.dataset.view") == "meeting"
        page.clock.run_for(200)
        page.mouse.click(x, y)                                                       # 650 ms: a tap meant for the button works
        if then:
            wait_for_calls(page, server, len(asked) + 1)
            assert server.meeting.controls_called[-1] == then
        else:
            page.wait_for_function("document.body.dataset.view === 'host'", timeout=5000)
        page.close()


@pytest.mark.parametrize("how", ["its ten seconds are over", "its choices are gone"])
def test_a_row_that_goes_by_itself_uncovers_buttons_that_ignore_a_tap_for_half_a_second_too(browser, how):
    """Not only a pick: a thumb aiming at a reaction as the row lapses, or as a poll takes its choices away, meets the secondary row too."""
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        open_reactions(page)
        box = page.locator("#reaction-row .button").first.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2           # Thumbs up: over Captions when the row is gone
        if how == "its ten seconds are over":
            page.clock.run_for(9000)
            for _ in range(40):                                                    # to the tick that closes it
                if not page.locator("#reaction-row").count():
                    break
                page.clock.run_for(100)
        else:
            server.meeting.current_meeting.controls["reactions"] = {"available": False, "choices": ["Clap"]}
            poll_until(page, "!document.querySelector('#reaction-row')")
        assert page.locator("#reaction-row").count() == 0 and page.locator("#meeting-secondary").is_visible()
        left = page.evaluate("window.croomPage.model.ignoreUntil - Date.now()")      # how much of the half second the buttons have still to ignore
        assert 0 < left <= 500
        page.mouse.click(x, y)
        page.wait_for_timeout(300)
        assert server.meeting.controls_called == []
        page.clock.run_for(left + 50)
        page.mouse.click(x, y)                                                       # after it, a tap on Captions is a tap on Captions
        wait_for_calls(page, server, 1)
        assert server.meeting.controls_called == [("captions", None)]
        page.close()


@pytest.mark.parametrize("button, view", [("Participants", "participants"), ("Chat", "chat"), ("Host", "host")])
def test_participants_chat_and_host_open_their_own_screen_and_back_returns(browser, button, view):
    """Their contents come with the sub-screens; the screens themselves, and the way back, are here."""
    with PageServer(role="host") as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        page.locator("#meeting-secondary .button", has_text=button).click()
        page.wait_for_function(f"document.body.dataset.view === '{view}'", timeout=5000)
        back = page.locator(f"#back-from-{view}")
        assert back.is_visible() and back.inner_text().strip() == "Back" and back.locator("svg.icon use").get_attribute("href") == "#icon-back"
        assert page.locator("#meeting-actions").is_hidden() and fits(page)
        back.click()
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        assert page.locator("#meeting-actions").is_visible()
        page.locator("#meeting-secondary .button", has_text=button).click()                  # and the meeting ending while one is up lands on the idle screen
        page.wait_for_function(f"document.body.dataset.view === '{view}'", timeout=5000)
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/leave", data="{}", headers=JSON)
        page.wait_for_function("document.body.dataset.view === 'idle'", timeout=8000)
        page.close()


def test_the_rows_are_not_rebuilt_while_nothing_about_them_changes(browser):
    """A finger that is down on a button must find it still there at the lift: the timer ticks, the status is polled, and a row whose
    words, icons and availability are the same is left alone."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        page.evaluate("window.__second = document.querySelector('#meeting-secondary .button')")
        open_reactions(page)
        page.evaluate("window.__reaction = document.querySelector('#reaction-row .button'); window.__row = document.getElementById('reaction-row')")
        page.wait_for_timeout(2600)                                                       # a poll and two ticks, at least
        assert page.evaluate("document.querySelector('#meeting-secondary .button') === window.__second")
        assert page.evaluate("document.querySelector('#reaction-row .button') === window.__reaction && document.getElementById('reaction-row') === window.__row")
        page.close()


@pytest.mark.parametrize("row, words, sent, change, shows", [
    ("meeting-secondary", "Gallery view", ("layout", "gallery"), lambda m: setattr(m, "chat", {"unread": 1, "messages": []}), "Chat (1)"),          # a chat message comes in
    ("meeting-secondary", "Hide self view", ("hide_self", None), lambda m: setattr(m, "people", {"count": 4, "list": a_roster(1)}), "Participants (4)"),   # someone joins
    ("meeting-actions", "Raise hand", ("raise_hand", None), lambda m: setattr(m, "is_muted", True), "Unmute"),                                      # the host mutes the room
])
def test_a_finger_down_on_a_button_still_finds_it_when_the_words_of_another_in_the_row_change(browser, row, words, sent, change, shows):
    """The words of a neighbour change while a thumb is down (a status poll carries them): the row is patched where it stands, so the lift
    is a tap on the button the thumb is on, and it does what that button says. A browser drops the tap of a button that has been replaced."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        box = page.locator(f"#{row} .button", has_text=words).bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.evaluate("([row, words]) => { window.__under = [...document.querySelectorAll('#' + row + ' .button')].find((b) => b.textContent.includes(words)); }", [row, words])
        page.mouse.move(x, y)
        page.mouse.down()
        change(server.meeting.current_meeting)
        poll_until(page, f"document.getElementById('{row}').textContent.includes('{shows}')")
        assert page.evaluate("window.__under.isConnected"), "the button under the thumb was replaced"
        page.mouse.up()
        wait_for_calls(page, server, 1)
        assert server.meeting.controls_called == [sent]
        page.close()


def test_a_reaction_under_a_thumb_is_still_there_when_another_tap_holds_the_page_for_a_moment(browser):
    """A quick action (Mute, with another finger) holds every button inert while it runs and lets them go: the reaction row is patched
    both ways where it stands, and the thumb that was on Clap all the while is still a tap on Clap."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        open_reactions(page)
        box = page.locator("#reaction-row .button", has_text="Clap").bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        page.evaluate("window.__clap = [...document.querySelectorAll('#reaction-row .button')].find((b) => b.textContent.includes('Clap'))")
        page.mouse.move(x, y)
        page.mouse.down()
        page.evaluate("() => { window.croomPage.model.busy = true; window.croomPage.render(); }")
        assert page.evaluate("window.__clap.isConnected && window.__clap.disabled")
        page.evaluate("() => { window.croomPage.model.busy = false; window.croomPage.render(); }")
        assert page.evaluate("window.__clap.isConnected && !window.__clap.disabled")
        page.mouse.up()
        wait_for_calls(page, server, 1)
        assert server.meeting.controls_called == [("reactions", "Clap")]
        page.close()


def test_a_button_whose_own_words_change_is_the_same_button_and_runs_what_it_says_now(browser):
    """The View button's words say its next tap (Gallery view, then Speaker view): patched in place, it still goes to the handler the
    status has now, not the one it was drawn with."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        page.evaluate("window.__view = [...document.querySelectorAll('#meeting-secondary .button')][1]")
        page.locator("#meeting-secondary .button", has_text="Gallery view").click()
        page.wait_for_function("document.getElementById('meeting-secondary').textContent.includes('Speaker view')", timeout=5000)
        assert page.evaluate("window.__view.isConnected && window.__view.textContent.trim() === 'Speaker view'")
        page.locator("#meeting-secondary .button", has_text="Speaker view").click()
        page.wait_for_function("document.getElementById('meeting-secondary').textContent.includes('Gallery view')", timeout=5000)
        assert server.meeting.controls_called == [("layout", "gallery"), ("layout", "speaker")]
        page.close()


def test_a_lift_on_a_button_whose_words_changed_since_the_press_does_not_run_the_new_words(browser):
    """A thumb comes down on Mute as the host mutes the room: the poll turns the button into Unmute under it, and the lift must not turn the
    room's microphone live. The button is kept (its own words changed), but what the lift runs is what it said when the finger went down; a
    press made on the new words does what they say."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        box = page.locator("#meeting-actions .button", has_text="Mute").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        server.meeting.muted = True                                                  # the host muted the room: the agent's state ...
        server.meeting.current_meeting.is_muted = True                               # ... and what the status carries
        poll_until(page, "document.querySelector('#meeting-actions .button').textContent.includes('Unmute')")
        page.mouse.up()
        page.wait_for_timeout(700)                                                   # long enough for a request to have gone out and been answered
        assert server.meeting.muted is True, "the lift unmuted a room the host had just muted"
        page.locator("#meeting-actions .button", has_text="Unmute").click()
        page.wait_for_function("document.querySelector('#meeting-actions .button').textContent.trim() === 'Mute'", timeout=5000)
        assert server.meeting.muted is False
        page.close()


def test_a_row_whose_buttons_change_is_drawn_again_with_the_buttons_it_has_now(browser):
    """Another set of buttons (a host's Host button comes and goes with the role) is not a change of words: the row is built again, and
    every button in it goes to its own screen."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        meeting = server.meeting.current_meeting
        page.evaluate("window.__captions = document.querySelector('#meeting-secondary .button')")
        meeting.controls["host"]["role"] = "co-host"
        page.wait_for_function("document.getElementById('meeting-secondary').textContent.includes('Host')", timeout=5000)
        assert texts(page, "#meeting-secondary") == ["Captions on", "Gallery view", "Hide self view", "Participants (3)", "Chat", "Host", "TV"]
        assert not page.evaluate("window.__captions.isConnected")                       # a new set of buttons: the row was drawn again
        page.locator("#meeting-secondary .button", has_text="Host").click()
        page.wait_for_function("document.body.dataset.view === 'host'", timeout=5000)
        page.click("#back-from-host")
        meeting.controls["host"]["role"] = "participant"
        page.wait_for_function("!document.getElementById('meeting-secondary').textContent.includes('Host')", timeout=5000)
        assert texts(page, "#meeting-secondary") == ["Captions on", "Gallery view", "Hide self view", "Participants (3)", "Chat", "TV"]
        page.locator("#meeting-secondary .button", has_text="Chat").click()
        page.wait_for_function("document.body.dataset.view === 'chat'", timeout=5000)
        page.click("#back-from-chat")
        page.evaluate("window.__first = document.querySelector('#meeting-secondary .button')")
        meeting.controls["captions"] = {"available": False}                             # as many buttons as before, and not the same ones: Captions goes as Host comes
        meeting.controls["host"]["role"] = "host"
        page.wait_for_function("document.getElementById('meeting-secondary').textContent.includes('Host')", timeout=5000)
        assert texts(page, "#meeting-secondary") == ["Gallery view", "Hide self view", "Participants (3)", "Chat", "Host", "TV"]
        assert not page.evaluate("window.__first.isConnected")
        page.close()


def test_the_meeting_screen_on_a_phone_wraps_its_rows_and_never_scrolls_sideways(browser):
    with PageServer(role="host") as server:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        server.meeting.current_meeting.notice = "This meeting is being recorded and transcribed."
        page.wait_for_function("document.getElementById('meeting-notice').textContent !== ''", timeout=5000)
        wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        assert page.evaluate(wide)
        open_reactions(page)
        assert page.evaluate(wide)
        assert page.locator("#reaction-row .button").count() == 7 and page.locator("#reaction-row .button").last.is_visible()
        page.locator("#reaction-row .button", has_text="Cancel").click()
        page.locator("#meeting-secondary .button", has_text="Host").scroll_into_view_if_needed()      # the page scrolls on a phone: every button can be reached
        assert page.locator("#meeting-secondary .button", has_text="Host").is_visible()
        page.close()


# --- the dialog card (spec 2026-10-13, section 5): a dialog the agent has no policy for waits on the table to be answered ---

ZOOMS_DIALOG = {"id": 7, "title": "Recording consent", "text": "This meeting is being recorded.", "buttons": ["Leave Meeting", "Got it"]}   # Zoom 6.5.0 lists the cancel first and the affirmative last
CARD = "document.querySelector('#meeting-dialog:not([hidden])')"
LONGEST_TEXT = ("The host has started a recording and a transcript of this meeting, and anyone who stays agrees to both. " * 4)[:400]   # what the bridge sends at most
TWO_LINES_OF_NOTICE = "This meeting is being recorded and transcribed, and the host is using AI Companion for a summary of it."


def put_up(server, dialog):
    server.meeting.current_meeting.dialog = dict(dialog)


def a_meeting_with_a_dialog(browser, server, dialog=ZOOMS_DIALOG, width=1280, height=800, touchscreen=False):
    """The room page, in the meeting, on a stopped clock, with the dialog's card drawn and its half second of ignoring taps over."""
    page = page_on_a_stopped_clock(browser, server, width, height, touchscreen=touchscreen)
    join_by_id(page)
    put_up(server, dialog)
    poll_until(page, CARD)
    page.clock.run_for(600)
    return page


def centre(page, selector, has_text=None):
    box = page.locator(selector, has_text=has_text).bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def button_under(page, x, y):
    """What a finger at this point of the page is over now: the words of the button there, or "" for anything else."""
    return page.evaluate("([x, y]) => { const b = [...document.querySelectorAll('button')].find((n) => { const r = n.getBoundingClientRect(); return r.width && x >= r.left && x <= r.right && y >= r.top && y <= r.bottom; }); return b ? b.textContent.trim() : ''; }", [x, y])


def test_a_waiting_dialog_is_answered_from_the_table(browser):
    """The agent leaves a dialog it has no policy for in the status. The table shows it with its own words and one button for each of the
    client's, in the client's order and the last one primary (Zoom 6.5.0 lists the cancel first), and a tap answers it: the card closes
    with the answer, not with the next poll (nothing polls here)."""
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server)
        assert page.locator("#dialog-title").inner_text() == "Recording consent"
        assert page.locator("#dialog-text").inner_text() == "This meeting is being recorded."
        assert texts(page, "#dialog-actions") == ["Leave Meeting", "Got it"]
        classes = [b.get_attribute("class").split() for b in page.locator("#dialog-actions .button").all()]
        assert "primary" not in classes[0] and "quiet" in classes[0] and "primary" in classes[1]
        assert page.locator("#dialog-actions svg").count() == 0                       # the client's words are the whole meaning: no symbol for an unknown label
        page.locator("#dialog-actions .button", has_text="Got it").click()
        page.wait_for_function("document.querySelector('#meeting-dialog').hidden", timeout=5000)
        assert server.meeting.controls_called == [("dialog", {"id": 7, "button": "Got it"})]
        assert page.evaluate("window.croomPage.model.status.meeting.dialog") is None   # in the model, not only on the screen
        page.close()


@pytest.mark.parametrize("labels, classes", [
    (["Got it"], ["primary"]),                                      # a one-button dialog's only button
    (["Leave Meeting", "Got it"], ["quiet", "primary"]),
    (["No", "Maybe later", "Yes"], ["quiet", "quiet", "primary"]),
])
def test_the_last_button_of_a_dialog_is_the_primary_one(browser, labels, classes):
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server, dict(ZOOMS_DIALOG, buttons=labels))
        assert texts(page, "#dialog-actions") == labels
        assert [[c for c in b.get_attribute("class").split() if c in ("primary", "quiet")][0] for b in page.locator("#dialog-actions .button").all()] == classes
        page.close()


def test_the_card_draws_the_clients_words_as_text_and_names_a_dialog_that_has_no_title(browser):
    dialog = {"id": 4, "title": "  ", "text": "<b>The host</b> asks: unmute &amp; speak?", "buttons": ["<i>Unmute</i>", "STAY muted", "Leave Meeting"]}
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server, dialog)
        assert page.locator("#dialog-title").inner_text() == "Zoom asks"                      # only a title that is empty gets this
        assert page.locator("#dialog-text").inner_text() == dialog["text"]                    # as text, never as markup
        assert page.locator("#dialog-text").get_attribute("title") == dialog["text"]
        assert texts(page, "#dialog-actions") == dialog["buttons"]                            # the client's words, its capitals too
        assert page.locator("#meeting-dialog b, #meeting-dialog i").count() == 0
        page.locator("#dialog-actions .button", has_text="STAY muted").click()
        wait_for_calls(page, server, 1)
        assert server.meeting.controls_called == [("dialog", {"id": 4, "button": "STAY muted"})]
        page.close()


def test_a_dialog_with_no_text_has_no_empty_line_in_its_card(browser):
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server, dict(ZOOMS_DIALOG, text=""))
        left_over = page.evaluate("""() => document.querySelector('.dialog-words').getBoundingClientRect().height - document.getElementById('dialog-title').getBoundingClientRect().height""")
        assert left_over <= 6, left_over                                                      # the title's own 4 px under it, and nothing for the text that is not there
        assert page.locator("#dialog-message").is_hidden() and page.locator("#dialog-title").inner_text() == "Recording consent"
        page.close()


def test_a_tap_meant_for_the_controls_does_not_answer_a_dialog_that_appeared_under_the_finger(browser):
    """The card pushes both rows down and its buttons land where the primary row was, and Zoom's consent dialog puts "Leave Meeting" first:
    the second tap of a double tap on Leave (a thumb lands twice, 150 ms apart) must not answer the dialog that came between the two, nor
    one a little slower. The clock is stopped: the half second is the test's to spend."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, touchscreen=True)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        x, y = centre(page, "#meeting-actions .button", "Leave")
        page.touchscreen.tap(x, y)                                                       # the first tap: Leave asks for its second
        page.wait_for_function("document.querySelector('#meeting-actions .button.danger').textContent.includes('Tap again')", timeout=5000)
        put_up(server, ZOOMS_DIALOG)
        poll_until(page, CARD)                                                           # the dialog arrives between the two taps
        assert button_under(page, x, y) == "Leave Meeting", "the thumb is not over the card's button: this test means nothing"
        page.clock.run_for(150)
        page.touchscreen.tap(x, y)                                                       # 150 ms after the card came: the second tap of a double tap
        page.clock.run_for(300)
        page.touchscreen.tap(x, y)                                                       # 450 ms: a slow one
        page.wait_for_timeout(300)                                                       # (real time) long enough for a request to have gone out
        assert server.meeting.controls_called == [] and server.meeting.leaves == 0 and page.locator("#meeting-dialog").is_visible()
        page.clock.run_for(200)
        page.touchscreen.tap(x, y)                                                       # 650 ms: a tap meant for the card's button works
        wait_for_calls(page, server, 1)
        assert server.meeting.controls_called == [("dialog", {"id": 7, "button": "Leave Meeting"})]
        page.close()


@pytest.mark.parametrize("buttons", [["Stay muted", "Got it"], ["Got it"]])      # the next dialog has other words, or the same ones
def test_the_dialog_that_takes_the_place_of_the_one_answered_ignores_a_tap_for_half_a_second_too(browser, buttons):
    """Answering a dialog shows the next one waiting in the same answer, under the finger that answered: its buttons are new, and the second
    tap of a double tap on the first one's Got it is not an answer to the second."""
    first = {"id": 1, "title": "Recording", "text": "This meeting is being recorded.", "buttons": ["Got it"]}
    second = {"id": 2, "title": "Unmute?", "text": "The host would like you to unmute.", "buttons": buttons}
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server, first)
        meeting = server.meeting.current_meeting

        def as_the_provider_does(name, value):
            meeting.dialog = dict(second) if value["id"] == 1 else None                    # the answer shows the next dialog waiting, if there is one
        server.meeting.on_control = as_the_provider_does
        x, y = centre(page, "#dialog-actions .button", "Got it")
        page.mouse.click(x, y)
        page.wait_for_function("document.getElementById('dialog-title').textContent === 'Unmute?'", timeout=5000)   # in the answer itself
        assert button_under(page, x, y) == "Got it"                                          # the second dialog's, where the first one's was
        page.clock.run_for(150)
        page.mouse.click(x, y)
        page.clock.run_for(300)
        page.mouse.click(x, y)
        page.wait_for_timeout(300)
        assert server.meeting.controls_called == [("dialog", {"id": 1, "button": "Got it"})] and meeting.dialog["id"] == 2
        page.clock.run_for(200)
        page.mouse.click(x, y)
        wait_for_calls(page, server, 2)
        assert server.meeting.controls_called[-1] == ("dialog", {"id": 2, "button": "Got it"})
        page.close()


def test_a_tap_aimed_at_the_secondary_row_does_not_land_on_mute_when_a_card_comes_up_under_it(browser):
    """The card moves the whole screen, not only its own buttons' place: a thumb on its way to Captions meets Mute when the card comes up
    under it. The screen ignores taps for half a second after it moves (the clock is stopped: the half second is the test's to spend)."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, touchscreen=True)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        x, y = centre(page, "#meeting-secondary .button", "Captions")                      # where the thumb is going
        put_up(server, ZOOMS_DIALOG)
        poll_until(page, CARD)                                                              # the card pushes both rows down: Mute is under the thumb now
        assert button_under(page, x, y) == "Mute", "the thumb is not over Mute: this test means nothing"
        page.clock.run_for(150)
        page.touchscreen.tap(x, y)
        page.clock.run_for(300)
        page.touchscreen.tap(x, y)                                                          # a slow one, 450 ms after
        page.wait_for_timeout(300)                                                          # (real time) long enough for a request to have gone out
        assert server.meeting.muted is False and server.meeting.controls_called == []
        page.clock.run_for(300)
        page.touchscreen.tap(x, y)                                                          # 750 ms: a tap meant for what is there now works
        page.wait_for_function("document.querySelector('#meeting-actions .button').textContent.trim() === 'Unmute'", timeout=5000)
        assert server.meeting.muted is True
        page.close()


def test_a_tap_aimed_at_the_secondary_row_does_not_move_the_volume_when_the_card_goes_from_over_it(browser):
    """The other way: the card goes and everything moves up, so a thumb on its way to the lower part of Hide self view meets the volume slider."""
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server, touchscreen=True)
        box = page.locator("#meeting-secondary .button", has_text="Hide self").bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] - 8                    # where the thumb is going
        server.meeting.current_meeting.dialog = None
        poll_until(page, "document.querySelector('#meeting-dialog').hidden")
        assert page.evaluate("([x, y]) => { const r = document.getElementById('volume-slider').getBoundingClientRect(); return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom; }", [x, y]), \
            "the thumb is not over the volume slider: this test means nothing"
        page.clock.run_for(150)
        page.touchscreen.tap(x, y)
        page.clock.run_for(300)
        page.touchscreen.tap(x, y)
        page.wait_for_timeout(300)
        assert server.devices.volume.calls == []
        page.clock.run_for(300)
        page.touchscreen.tap(x, y)                                                          # 750 ms: a tap on what is there now is a tap on it
        for _ in range(100):
            if server.devices.volume.calls:
                break
            page.wait_for_timeout(50)
        assert server.devices.volume.calls, "a tap made after the screen has settled is a tap on what is there"
        page.close()


def test_a_tap_aimed_at_the_menu_does_not_set_the_volume_when_the_speaker_comes_back_above_it(browser):
    """Every renderer moves the screen, not only the card's: the Sound strip comes back (the speaker is plugged in) and the menu under it goes
    down, so a thumb on its way to Camera meets the volume slider. The screen is settling in the very render that moved it, not one poll later."""
    with PageServer(devices=StubDevices(volume=StubVolume(available=False))) as server:
        page = page_on_a_stopped_clock(browser, server, touchscreen=True)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        assert page.locator("#sound-panel").is_hidden()
        page.clock.run_for(600)                                                             # the screen has settled, without a speaker
        box = page.locator("#open-camera-live").bounding_box()
        x, y = box["x"] + box["width"] * 0.8, box["y"] + 15                                 # where the thumb is going: the upper right of Camera (a level of 40 is where the middle would set it)
        server.devices.volume.available, server.devices.volume.reason = True, None
        poll_until(page, "!document.getElementById('sound-panel').hidden")                  # the strip is back, and the menu is lower
        assert page.evaluate("([x, y]) => { const r = document.getElementById('volume-slider').getBoundingClientRect(); return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom; }", [x, y]), \
            "the thumb is not over the volume slider: this test means nothing"
        page.clock.run_for(150)
        page.touchscreen.tap(x, y)
        page.clock.run_for(300)
        page.touchscreen.tap(x, y)
        page.wait_for_timeout(300)
        assert server.devices.volume.calls == []
        page.clock.run_for(300)
        page.touchscreen.tap(x, y)                                                          # 750 ms: a tap on what is there now is a tap on it
        for _ in range(100):
            if server.devices.volume.calls:
                break
            page.wait_for_timeout(50)
        assert server.devices.volume.calls, "a tap made after the screen has settled is a tap on what is there"
        page.close()


def test_a_volume_drag_under_way_is_not_cut_short_when_the_screen_moves(browser):
    """The screen ignores taps while it settles, not a drag that began before it moved: the thumb on the slider (a notice comes in above it,
    and the slider goes down) is still dragging, and the level follows it to the end."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        page.clock.run_for(600)
        box = page.locator("#volume-slider").bounding_box()
        page.mouse.move(box["x"] + box["width"] * 0.40, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] * 0.55, box["y"] + box["height"] / 2, steps=5)
        server.meeting.current_meeting.notice = "This meeting is being recorded."
        poll_until(page, "document.getElementById('meeting-notice').textContent !== ''")     # the notice moves the slider down: the screen is settling
        assert page.evaluate("document.body.classList.contains('settling')")
        box = page.locator("#volume-slider").bounding_box()
        page.mouse.move(box["x"] + box["width"] * 0.80, box["y"] + box["height"] / 2, steps=8)
        page.mouse.up()
        for _ in range(100):
            if server.devices.volume.calls and server.devices.volume.calls[-1][1] >= 75:
                break
            page.wait_for_timeout(50)
        assert server.devices.volume.calls[-1][1] >= 75, server.devices.volume.calls         # it went on to the end of the drag (the slider was at 55 when the screen moved)
        page.close()


@pytest.mark.parametrize("width, height, scroll", [
    (1280, 700, "document.querySelector('main').scrollTop = 40"),       # the kiosk's fixed screen scrolls inside main when it cannot fit (the shortest window it still gets)
    (390, 844, "window.scrollTo(0, 120)"),                              # a phone's page scrolls
])
def test_scrolling_the_screen_is_not_the_screen_moving(browser, width, height, scroll):
    """Where a control is on the page does not change when the page is scrolled: the screen is not settling after the person has scrolled it."""
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server, width, height)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        meeting = server.meeting.current_meeting
        meeting.notice = TWO_LINES_OF_NOTICE
        put_up(server, dict(ZOOMS_DIALOG, text=LONGEST_TEXT))
        poll_until(page, CARD)
        page.clock.run_for(600)                                                             # settled
        assert not page.evaluate("document.body.classList.contains('settling')")
        page.evaluate(scroll)
        assert page.evaluate("document.querySelector('main').scrollTop + window.scrollY") > 0, "the screen did not scroll: this test means nothing"
        page.evaluate("window.croomPage.refreshStatus()")
        page.wait_for_timeout(300)
        assert not page.evaluate("document.body.classList.contains('settling')")
        page.close()


def test_each_move_of_the_screen_starts_its_half_second_again(browser):
    """A second move 300 ms after the first (the card, then a notice) keeps the screen settling until half a second after the second."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, touchscreen=True)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        put_up(server, ZOOMS_DIALOG)
        poll_until(page, CARD)                                                              # the first move
        page.clock.run_for(300)
        server.meeting.current_meeting.notice = "This meeting is being recorded."
        poll_until(page, "document.getElementById('meeting-notice').textContent !== ''")    # the second move, 300 ms after
        x, y = centre(page, "#meeting-actions .button", "Mute")
        page.clock.run_for(300)                                                             # 600 ms after the first move, 300 after the second
        page.touchscreen.tap(x, y)
        page.wait_for_timeout(300)
        assert server.meeting.muted is False
        page.clock.run_for(250)                                                             # 550 ms after the second
        page.touchscreen.tap(x, y)
        page.wait_for_function("document.querySelector('#meeting-actions .button').textContent.trim() === 'Unmute'", timeout=5000)
        assert server.meeting.muted is True
        page.close()


@pytest.mark.parametrize("pixels, settles", [(3, False), (8, True)])
def test_only_a_move_of_more_than_a_few_pixels_settles_the_screen(browser, pixels, settles):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        page.clock.run_for(600)
        page.evaluate("(px) => { document.getElementById('meeting-detail').style.marginTop = px + 'px'; window.croomPage.render(); }", pixels)
        assert page.evaluate("document.body.classList.contains('settling')") is settles
        page.close()


def test_coming_back_from_a_sub_screen_settles_the_screen_once_and_the_notice_that_came_meanwhile_is_not_a_second_move(browser):
    """The view change is the move: the screen ignores taps for half a second after Back (a thumb aimed at Back lands on Mute). The notice that
    came in while the person was away is not another: the screen was laid out as it is now at the return, and a poll after the half second finds
    nothing moving, so it is not settling again."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        page.clock.run_for(600)
        page.locator("#meeting-secondary .button", has_text="Participants").click()
        page.wait_for_function("document.body.dataset.view === 'participants'", timeout=5000)
        server.meeting.current_meeting.notice = "This meeting is being recorded."
        poll_until(page, "document.getElementById('meeting-notice').textContent !== ''")
        page.clock.run_for(600)                                                             # the half second of the way in
        page.click("#back-from-participants")
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        assert page.evaluate("document.body.classList.contains('settling')")
        page.clock.run_for(600)
        assert not page.evaluate("document.body.classList.contains('settling')")
        page.evaluate("window.croomPage.refreshStatus()")
        page.wait_for_timeout(300)
        assert not page.evaluate("document.body.classList.contains('settling')")
        page.close()


def test_the_buttons_of_a_card_that_changes_its_buttons_ignore_a_tap_for_half_a_second_and_are_drawn_again(browser):
    """New labels are a new card as far as a finger is concerned, whether or not Zoom kept the dialog's number."""
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server)
        page.evaluate("window.__first = document.querySelector('#dialog-actions .button')")
        put_up(server, dict(ZOOMS_DIALOG, buttons=["Stay", "Allow recording"]))
        poll_until(page, "document.querySelector('#dialog-actions .button').textContent === 'Stay'")
        assert not page.evaluate("window.__first.isConnected")                                # the buttons are new ones
        x, y = centre(page, "#dialog-actions .button", "Allow recording")
        page.clock.run_for(150)
        page.mouse.click(x, y)
        page.wait_for_timeout(300)
        assert server.meeting.controls_called == []
        page.clock.run_for(400)
        page.mouse.click(x, y)
        wait_for_calls(page, server, 1)
        assert server.meeting.controls_called == [("dialog", {"id": 7, "button": "Allow recording"})]
        page.close()


def test_a_cards_buttons_are_kept_when_only_its_text_changes_so_a_finger_on_one_is_not_lost(browser):
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server)
        x, y = centre(page, "#dialog-actions .button", "Got it")
        page.evaluate("window.__got = [...document.querySelectorAll('#dialog-actions .button')].find((b) => b.textContent === 'Got it')")
        page.mouse.move(x, y)
        page.mouse.down()
        put_up(server, dict(ZOOMS_DIALOG, text="This meeting is being recorded and transcribed."))
        poll_until(page, "document.getElementById('dialog-text').textContent.includes('transcribed')")
        assert page.evaluate("window.__got.isConnected"), "the button under the thumb was replaced"
        page.mouse.up()
        wait_for_calls(page, server, 1)
        assert server.meeting.controls_called == [("dialog", {"id": 7, "button": "Got it"})]
        page.close()


def test_a_finger_on_a_cards_button_is_still_there_when_another_tap_holds_the_page_for_a_moment(browser):
    """A quick action (Mute, with another finger) holds every button inert while it runs and lets them go: the card's buttons are patched
    both ways where they stand, and the thumb that was on Got it all the while is still a tap on Got it."""
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server)
        x, y = centre(page, "#dialog-actions .button", "Got it")
        page.evaluate("window.__got = [...document.querySelectorAll('#dialog-actions .button')].find((b) => b.textContent === 'Got it')")
        page.mouse.move(x, y)
        page.mouse.down()
        page.evaluate("() => { window.croomPage.model.busy = true; window.croomPage.render(); }")
        assert page.evaluate("window.__got.isConnected && window.__got.disabled")
        page.evaluate("() => { window.croomPage.model.busy = false; window.croomPage.render(); }")
        assert page.evaluate("window.__got.isConnected && !window.__got.disabled")
        page.mouse.up()
        wait_for_calls(page, server, 1)
        assert server.meeting.controls_called == [("dialog", {"id": 7, "button": "Got it"})]
        page.close()


def test_a_dialog_that_takes_the_place_of_another_with_the_same_words_has_buttons_of_its_own(browser):
    """A finger down on the first one's Got it must not lift as a tap on the second's, whose words are the same: it is another dialog, and
    the person has not seen it (the half second it ignores taps is spent first, so that this is the buttons' doing alone)."""
    first = dict(ZOOMS_DIALOG, id=1, buttons=["Got it"])
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server, first)
        x, y = centre(page, "#dialog-actions .button", "Got it")
        page.evaluate("window.__first = document.querySelector('#dialog-actions .button')")
        page.mouse.move(x, y)
        page.mouse.down()
        put_up(server, dict(first, id=2))
        poll_until(page, "window.croomPage.model.status.meeting.dialog.id === 2")
        page.clock.run_for(600)
        assert not page.evaluate("window.__first.isConnected")
        page.mouse.up()
        page.wait_for_timeout(300)
        assert server.meeting.controls_called == []
        page.locator("#dialog-actions .button", has_text="Got it").click()                    # and a tap made on the new one is an answer to it
        wait_for_calls(page, server, 1)
        assert server.meeting.controls_called == [("dialog", {"id": 2, "button": "Got it"})]
        page.close()


def test_the_answer_closes_the_card_at_once_and_a_poll_already_on_its_way_cannot_bring_it_back(browser):
    """The answer is the meeting block as it is after the press, taken into the model when it comes, with every poll held. While it is out the
    card's buttons are inert, and one tap is one request."""
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server)
        stale = page.request.get(f"http://127.0.0.1:{server.port}/api/status").json()      # what a poll sent now carries: the dialog waiting
        stale["room"]["name"] = "Stale poll"                                                # so that the page's taking the poll in can be seen
        assert stale["meeting"]["dialog"]["id"] == 7
        polls = hold_requests(page, "**/api/status")
        page.evaluate("() => { window.__poll = window.croomPage.refreshStatus(); }")        # a poll goes out ...
        wait_for_held(page, polls)
        held = hold_requests(page, "**/api/meeting/dialog")
        page.locator("#dialog-actions .button", has_text="Got it").click()                  # ... and the dialog is answered while it is out
        wait_for_held(page, held)
        assert held[0].request.post_data_json == {"id": 7, "button": "Got it"}
        assert page.locator("#dialog-actions .button:disabled").count() == 2                # every button of the card waits for the answer
        assert page.evaluate("window.croomPage.model.busy") is False                        # the page as a whole is not held
        page.evaluate("() => { const b = document.querySelectorAll('#dialog-actions .button')[1]; b.disabled = false; b.click(); }")   # a tap that gets past the inert button
        page.wait_for_timeout(200)
        assert len(held) == 1 and page.locator("#meeting-actions .button", has_text="Mute").is_enabled()
        answer = page.evaluate("window.croomPage.model.status.meeting")
        answer["dialog"] = None
        held[0].fulfill(json={"meeting": answer})
        page.wait_for_function("document.querySelector('#meeting-dialog').hidden", timeout=5000)
        polls[0].fulfill(json=stale)                                                         # the old poll lands after the answer, with the dialog in it
        page.wait_for_function("document.getElementById('room-name').textContent === 'Stale poll'", timeout=5000)     # it was taken in ...
        assert page.locator("#meeting-dialog").is_hidden()                                   # ... but not its dialog
        assert server.meeting.controls_called == []                                          # the page held the answer: it never reached the meeting
        page.close()


def test_an_answer_the_room_never_gives_is_given_up_after_35_seconds(browser):
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server)
        held = hold_requests(page, "**/api/meeting/dialog")
        page.locator("#dialog-actions .button", has_text="Got it").click()
        wait_for_held(page, held)
        page.clock.fast_forward(34000)
        assert page.locator("#dialog-actions .button:disabled").count() == 2 and page.locator("#dialog-message").is_hidden()      # still waiting at 34 seconds
        page.clock.fast_forward(2000)
        page.wait_for_function("document.getElementById('dialog-message').textContent.includes('did not answer')", timeout=5000)
        assert page.locator("#dialog-actions .button:disabled").count() == 0 and len(held) == 1      # given up on: the buttons are back, and it was one request
        assert page.locator("#meeting-dialog").is_visible()
        held[0].abort()                                                                      # the held route is let go: a route left waiting is garbage that fails a later test
        page.close()


def test_a_refusal_of_a_dialogs_answer_is_said_in_the_card_and_its_buttons_come_back(browser):
    from croom.meeting.providers.base import MeetingControlError
    words = "Zoom's dialog did not close when Got it was pressed; try again in a moment"
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server)
        server.meeting.fail_control = MeetingControlError(words)
        page.locator("#dialog-actions .button", has_text="Got it").click()
        page.wait_for_function("document.getElementById('dialog-message').textContent !== ''", timeout=5000)
        assert page.locator("#dialog-message").inner_text() == words
        assert page.locator("#meeting-message").is_hidden()                                  # not under the title as well: the two never stack
        assert page.locator("#meeting-dialog").is_visible() and texts(page, "#dialog-actions") == ["Leave Meeting", "Got it"]
        assert page.locator("#dialog-actions .button:disabled").count() == 0                 # the buttons are back
        server.meeting.fail_control = None
        page.clock.run_for(600)                                                              # the refusal's line moved the screen: it has settled
        page.locator("#dialog-actions .button", has_text="Got it").click()                   # the next action clears the words
        page.wait_for_function("document.querySelector('#meeting-dialog').hidden", timeout=5000)
        assert page.locator("#dialog-message").inner_text() == "" and page.locator("#meeting-message").inner_text() == ""
        page.close()


def test_while_a_card_shows_a_controls_refusal_is_said_in_the_card_and_not_under_the_title(browser):
    """A refusal while a dialog waits is almost always the dialog's own, and the card is where the eye is: the line under the title and the
    card's own would stack on a screen with no room for both."""
    from croom.meeting.providers.base import MeetingControlError
    words = "Zoom's raise hand button is not on the screen yet; try again in a moment"
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server)
        server.meeting.fail_control = MeetingControlError(words)
        page.locator("#meeting-actions .button", has_text="Raise hand").click()
        page.wait_for_function("document.getElementById('dialog-message').textContent !== ''", timeout=5000)
        assert page.locator("#dialog-message").inner_text() == words and page.locator("#meeting-message").is_hidden()
        page.close()


def test_a_refusal_that_is_showing_when_a_dialog_arrives_moves_into_its_card(browser):
    from croom.meeting.providers.base import MeetingControlError
    words = "Zoom's raise hand button is not on the screen yet; try again in a moment"
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        server.meeting.fail_control = MeetingControlError(words)
        page.locator("#meeting-actions .button", has_text="Raise hand").click()
        page.wait_for_function("document.getElementById('meeting-message').textContent !== ''", timeout=5000)     # under the title: no card yet
        put_up(server, ZOOMS_DIALOG)
        poll_until(page, CARD)
        assert page.locator("#dialog-message").inner_text() == words and page.locator("#meeting-message").is_hidden()   # now in the card, and only there
        page.close()


def test_a_refusal_is_about_the_dialog_it_was_said_of_and_does_not_stay_for_the_next_one(browser):
    from croom.meeting.providers.base import MeetingControlError
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server)
        server.meeting.fail_control = MeetingControlError("Zoom's dialog did not close when Got it was pressed; try again in a moment")
        page.locator("#dialog-actions .button", has_text="Got it").click()
        page.wait_for_function("document.getElementById('dialog-message').textContent !== ''", timeout=5000)
        put_up(server, {"id": 8, "title": "Unmute?", "text": "The host would like you to unmute.", "buttons": ["Stay muted", "Unmute"]})   # Zoom withdrew it, and drew this
        poll_until(page, "document.getElementById('dialog-title').textContent === 'Unmute?'")
        assert page.locator("#dialog-message").inner_text() == "" and page.locator("#meeting-message").is_hidden()
        assert page.locator("#dialog-actions .button:disabled").count() == 0
        page.close()


def test_a_dialog_that_goes_by_itself_closes_its_card_and_says_nothing(browser):
    """The host withdrew it, or Zoom did: the status carries no dialog any more. A refusal the card was showing was the dialog's own and goes
    with it, and does not move up under the title."""
    from croom.meeting.providers.base import MeetingControlError
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server)
        server.meeting.fail_control = MeetingControlError("Zoom's dialog did not close when Got it was pressed; try again in a moment")
        page.locator("#dialog-actions .button", has_text="Got it").click()
        page.wait_for_function("document.getElementById('dialog-message').textContent !== ''", timeout=5000)
        server.meeting.current_meeting.dialog = None
        poll_until(page, "document.querySelector('#meeting-dialog').hidden")
        assert page.locator("#meeting-message").is_hidden() and page.locator("#dialog-message").inner_text() == ""
        assert page.evaluate("window.croomPage.model.error") == ""
        assert page.locator("#dialog-actions .button").count() == 0                           # no buttons left to press
        assert server.meeting.controls_called == [("dialog", {"id": 7, "button": "Got it"})]  # and nothing was sent for it
        page.close()


CONTRAST = """() => {
    const numbers = (c) => c.match(/[\\d.]+/g).map(Number);
    const light = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
    const ground = (el) => { for (let n = el; n; n = n.parentElement) { const c = numbers(getComputedStyle(n).backgroundColor); if (c.length < 4 || c[3] > 0.99) return c; } return [255, 255, 255]; };
    const contrast = (el) => { const a = light(numbers(getComputedStyle(el).color)), b = light(ground(el)); return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); };
    const named = { 'title': '#dialog-title', 'text': '#dialog-text', 'refusal': '#dialog-message' };
    const out = Object.fromEntries(Object.entries(named).map(([name, selector]) => [name, contrast(document.querySelector(selector))]));
    document.querySelectorAll('#dialog-actions .button').forEach((b, i) => { out['button ' + (i + 1)] = contrast(b); });
    return out; }"""


def test_the_cards_words_and_buttons_can_be_read_on_its_cream_ground(browser):
    """The meeting screen's labels and buttons are for a dark page (periwinkle and white): on the cream card they would not be seen."""
    from croom.meeting.providers.base import MeetingControlError
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server, dict(ZOOMS_DIALOG, buttons=["Leave Meeting", "Ask me later", "Got it"]))
        server.meeting.fail_control = MeetingControlError("Zoom's dialog did not close when Got it was pressed; try again in a moment")
        page.locator("#dialog-actions .button", has_text="Got it").click()
        page.wait_for_function("document.getElementById('dialog-message').textContent !== ''", timeout=5000)
        page.mouse.move(0, 0)                                                                # no button is under the pointer: none shows its hover
        ratios = page.evaluate(CONTRAST)
        assert set(ratios) == {"title", "text", "refusal", "button 1", "button 2", "button 3"}
        assert all(ratio >= 4.5 for ratio in ratios.values()), ratios                        # WCAG AA for text of this size
        page.close()


@pytest.mark.parametrize("width, height, lines", [(1280, 800, 3), (1280, 1000, 4)])
def test_the_longest_text_is_cut_to_three_lines_and_to_four_where_the_screen_is_tall(browser, width, height, lines):
    """The words have the room the buttons leave: three buttons with long words leave the least, and the longest text (400 characters) is
    more than the card shows then. The whole of it is the tooltip, and is in the page."""
    assert len(LONGEST_TEXT) == 400
    dialog = dict(ZOOMS_DIALOG, text=LONGEST_TEXT, buttons=["Leave Meeting", "Remind me in five minutes", "Got it"])
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server, dialog, width, height)
        shown = page.evaluate("""() => { const t = document.getElementById('dialog-text'), line = parseFloat(getComputedStyle(t).lineHeight);
            return { lines: Math.round(t.clientHeight / line), cut: t.scrollHeight > t.clientHeight, words: t.textContent.length, whole: t.title.length }; }""")
        assert shown == {"lines": lines, "cut": True, "words": 400, "whole": 400}, shown
        page.close()


def test_a_title_that_runs_on_is_cut_to_two_lines(browser):
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server, dict(ZOOMS_DIALOG, title="Zoom Workplace would like to " + "record this meeting and keep a transcript of it " * 6))
        shown = page.evaluate("""() => { const t = document.getElementById('dialog-title'), line = parseFloat(getComputedStyle(t).lineHeight);
            return { lines: Math.round(t.clientHeight / line), cut: t.scrollHeight > t.clientHeight }; }""")
        assert shown == {"lines": 2, "cut": True}, shown
        page.close()


@pytest.mark.parametrize("width, height", [(1280, 800), (1920, 1080), (1920, 1200)])
def test_the_screen_of_a_host_with_everything_and_a_waiting_dialog_still_fits(browser, width, height):
    """The fixed screen holds everything at once (spec 2026-10-12): the host's widest words, a notice of two lines, the longest text a
    dialog can have (400 characters: three lines at 1280 by 800, four where the screen is taller), the longest words Leave can have, the
    TV's button after the host's six, and the reaction row in place of the secondary row. At the kiosk's size the card is 150 px at the most."""
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server, width, height)
        join_by_id(page)
        meeting = server.meeting.current_meeting
        meeting.is_muted = True                                              # Unmute, Captions off, Participants (120), Chat (12): the widest of each
        meeting.controls["captions"]["on"] = True
        set_people(server, a_roster(9), count=120)
        meeting.chat = {"unread": 12, "messages": []}
        meeting.notice = TWO_LINES_OF_NOTICE
        put_up(server, dict(ZOOMS_DIALOG, text=LONGEST_TEXT))
        poll_until(page, CARD)
        page.clock.run_for(600)                                              # the screen has settled
        assert texts(page, "#dialog-actions") == ["Leave Meeting", "Got it"]
        assert texts(page, "#meeting-secondary")[-2:] == ["Host", "TV"] and in_one_row(page, "#meeting-secondary")
        notice = page.evaluate("() => { const n = document.getElementById('meeting-notice'); return Math.round(n.getBoundingClientRect().height / parseFloat(getComputedStyle(n).lineHeight)); }")
        assert notice == 2, "the notice is not the two lines this test is for"
        if width == 1280:
            assert page.locator("#meeting-dialog").bounding_box()["height"] <= 150
        assert fits(page), "the meeting screen scrolls with the card, its longest text and a notice of two lines"
        page.locator("#meeting-actions .button", has_text="Leave").click()   # the longest words of the row: "Tap again to leave"
        assert fits(page), "the meeting screen scrolls while Leave asks for its second tap"
        page.locator("#meeting-actions .button", has_text="Reactions").click()
        page.wait_for_function("document.querySelector('#reaction-row')", timeout=5000)
        assert fits(page), "the meeting screen scrolls with the reaction row open"
        page.close()


def test_a_refusal_in_the_card_still_fits_the_kiosk_beside_a_one_line_text(browser):
    from croom.meeting.providers.base import MeetingControlError
    words = "Zoom's dialog did not close when Got it was pressed. Wait a moment and try again, or leave the meeting and join it once more."
    with PageServer(role="host") as server:
        page = a_meeting_with_a_dialog(browser, server)
        server.meeting.fail_control = MeetingControlError(words)
        page.locator("#dialog-actions .button", has_text="Got it").click()
        page.wait_for_function("document.getElementById('dialog-message').textContent !== ''", timeout=5000)
        refusal = page.locator("#dialog-message").bounding_box()
        assert refusal["height"] > 40, "the refusal is not the two lines this test is for"
        assert fits(page), "the meeting screen scrolls with a refusal of two lines in the card"
        page.close()


@pytest.mark.parametrize("width, height", [(1280, 800), (390, 844)])
def test_a_label_with_no_break_in_it_wraps_inside_the_card_and_does_not_push_it_off_the_screen(browser, width, height):
    """The client's words are not cut, so a label that is one long word (150 characters) is wrapped by its button: it may not make the card,
    or the page, wider than the screen."""
    label = "Unbroken" + "x" * 142
    assert len(label) == 150
    with PageServer() as server:
        page = a_meeting_with_a_dialog(browser, server, dict(ZOOMS_DIALOG, buttons=["Leave Meeting", label]), width, height)
        shown = page.evaluate("""() => { const card = document.getElementById('meeting-dialog').getBoundingClientRect(), d = document.documentElement, m = document.querySelector('main');
            return { cardRight: Math.round(card.right), screen: d.clientWidth, sideways: d.scrollWidth > d.clientWidth || m.scrollWidth > m.clientWidth }; }""")
        assert shown["cardRight"] <= shown["screen"] and not shown["sideways"], shown
        assert texts(page, "#dialog-actions") == ["Leave Meeting", label]                    # and every character of it is there
        page.close()


def test_the_dialog_card_on_a_phone_puts_its_buttons_under_its_words_and_never_scrolls_sideways(browser):
    dialog = dict(ZOOMS_DIALOG, text=LONGEST_TEXT, buttons=["Leave Meeting", "Ask me later", "Got it"])
    with PageServer(role="host") as server:
        page = a_meeting_with_a_dialog(browser, server, dialog, 390, 844)
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        words, actions = page.locator(".dialog-words").bounding_box(), page.locator("#dialog-actions").bounding_box()
        assert actions["y"] >= words["y"] + words["height"] - 1
        for b in page.locator("#dialog-actions .button").all():
            box = b.bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= 390, box
        page.close()


# --- the Participants, Chat and Host screens (spec 2026-10-13, section 6.1) ---

NASTY = '<b>Bold</b> <img src=x onerror="window.__pwned = 1"> &amp; <script>window.__pwned = 2</script>'      # other people's words, with markup in them


def open_sub_screen(page, words, view):
    page.locator("#meeting-secondary .button", has_text=words).click()
    page.wait_for_function(f"document.body.dataset.view === '{view}'", timeout=5000)


def without(people, *names):
    return [p for p in people if p["name"] not in names]


def row_button(page, person, words):
    """The button in this person's row of the participants list that has these words on it now."""
    return page.locator(f"#participant-list .participant:has-text('{person}') .button", has_text=words)


def person_under(page, x, y):
    """Whose row a finger at this point of the page is over now: the name in it, or "" for anything else."""
    return page.evaluate("""([x, y]) => { const r = [...document.querySelectorAll('.participant')].find((n) => { const b = n.getBoundingClientRect();
        return b.width && x >= b.left && x <= b.right && y >= b.top && y <= b.bottom; }); return r ? r.querySelector('.name').textContent : ''; }""", [x, y])


def a_meeting_with_a_roster(browser, server, extra, touchscreen=False, width=1280, height=800):
    """The room page on a stopped clock, in the meeting, with the room and `extra` + 2 other people listed and the Participants screen open and
    settled. Returns the page and the people."""
    page = page_on_a_stopped_clock(browser, server, width, height, touchscreen=touchscreen)
    join_by_id(page)
    page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
    people = a_roster(extra)
    set_people(server, people)
    poll_until(page, f"window.croomPage.model.status.meeting.participants.list.length === {len(people)}")
    page.clock.run_for(600)
    open_sub_screen(page, "Participants", "participants")
    page.clock.run_for(600)
    return page, people


def test_the_participants_screen_lists_the_room_and_lets_a_host_act(browser):
    with PageServer(role="host") as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_sub_screen(page, "Participants", "participants")
        rows = page.locator("#participant-list .participant")
        assert rows.count() == 3 and "Raj Patel" in rows.nth(2).inner_text()
        assert rows.nth(0).locator(".name").inner_text() == "Room 1 (this room)"
        assert rows.nth(1).locator(".mark").all_inner_texts() == ["Host"]
        assert rows.nth(2).locator(".mark-hand").count() == 1 and rows.nth(2).locator(".mark-muted").count() == 1
        assert rows.nth(2).locator(".mark").all_inner_texts() == ["Hand raised", "Muted", "Video off"]
        assert rows.nth(0).locator(".button").count() == 0                                   # the room's own row: nothing to do to oneself
        assert texts(page, "#participant-list li:nth-child(2)") == ["Mute", "Remove"]
        assert texts(page, "#participant-list li:nth-child(3)") == ["Remove"]                # Raj is muted already: Mute is for those who are not
        assert drawn_icons(page, "#participant-list li:nth-child(2)") == ["Mute: #icon-mic-off", "Remove: #icon-x-circle"]
        ends = page.evaluate("[...document.querySelectorAll('#participant-list .participant')].slice(1).map((r) => Math.round(r.querySelector('.marks').getBoundingClientRect().right))")
        assert len(set(ends)) == 1, ends                                                     # Ann has Mute and Remove, Raj only Remove: the marks end in one column all the same
        assert fits(page)
        rows.nth(1).locator(".button", has_text="Mute").click()
        wait_for_calls(page, server, 1)
        assert server.meeting.controls_called[-1] == ("host.mute_one", 16777217)
        server.meeting.current_meeting.controls["host"]["waiting_room"] = {"available": True, "waiting": [{"id": 9, "name": "Guest"}]}
        page.wait_for_function("document.querySelector('#waiting-list .participant')", timeout=5000)
        assert page.locator("#waiting-kicker").is_visible() and page.locator("#waiting-list .participant .name").inner_text() == "Guest"
        assert drawn_icons(page, "#waiting-list") == ["Admit: #icon-door"] and fits(page)
        page.locator("#waiting-list .button", has_text="Admit").click()
        wait_for_calls(page, server, 2)
        assert server.meeting.controls_called[-1] == ("host.admit", 9)
        page.locator("#back-from-participants").click()
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        page.close()


def test_a_participant_sees_no_host_buttons_on_the_participants_screen(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        server.meeting.current_meeting.controls["host"]["waiting_room"] = {"available": False, "waiting": [{"id": 9, "name": "Guest"}]}   # not for a participant to see
        open_sub_screen(page, "Participants", "participants")
        assert page.locator("#participant-list .participant").count() == 3
        assert page.locator("#participant-list .button").count() == 0
        page.evaluate("window.croomPage.refreshStatus()")                                    # a poll has been and gone
        assert page.locator("#waiting-list .participant").count() == 0 and page.locator("#waiting-kicker").is_hidden()
        gaps = page.evaluate("[...document.querySelectorAll('#participant-list .participant')].map((r) => Math.round(r.getBoundingClientRect().right - r.querySelector('.marks').getBoundingClientRect().right))")
        assert max(gaps) <= 30, gaps                                                         # no buttons in any row: the marks end at the row's edge, with no room kept for buttons
        assert fits(page)
        page.close()


def test_a_person_in_the_waiting_room_is_not_listed_among_those_in_the_meeting(browser):
    """Zoom's attendee list holds the people in the waiting room too (waiting: true): they are in the waiting list, with Admit, and not also in
    the meeting's."""
    with PageServer(role="host") as server:
        page = open_page(browser, server)
        join_by_id(page)
        guest = {"id": 9, "name": "Guest", "muted": False, "video": False, "hand": False, "host": False, "me": False, "waiting": True}
        set_people(server, a_roster(1) + [guest])
        server.meeting.current_meeting.controls["host"]["waiting_room"] = {"available": True, "waiting": [{"id": 9, "name": "Guest"}]}
        page.wait_for_function("window.croomPage.model.status.meeting.participants.list.length === 5", timeout=5000)
        open_sub_screen(page, "Participants", "participants")
        assert page.locator("#participant-list .participant .name").all_inner_texts() == ["Room 1 (this room)", "Ann Lee", "Raj Patel", "Priya Nair"]
        assert page.locator("#waiting-list .participant .name").all_inner_texts() == ["Guest"]
        page.close()


def test_the_chat_screen_shows_messages_and_clears_the_unread_count(browser):
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800}, timezone_id="UTC")   # the times read 8:00 PM, as the messages say
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        join_by_id(page)
        server.meeting.current_meeting.chat = {"unread": 2, "messages": [
            {"from": "Ann", "text": "Can you hear us?", "at": "2026-10-13T20:00:00Z"}, {"from": "Raj", "text": "Yes", "at": "2026-10-13T20:00:30Z"},
            {"from": "Sam", "text": "Loud and clear", "at": "some time ago"}]}         # the agent says "" when it does not know when; a time that cannot be read says nothing either
        page.wait_for_function("document.querySelector('#meeting-secondary .button').parentElement.textContent.includes('Chat (2)')", timeout=5000)
        open_sub_screen(page, "Chat", "chat")
        assert [m.inner_text() for m in page.locator("#chat-list .chat-message .chat-text").all()] == ["Can you hear us?", "Yes", "Loud and clear"]
        assert [m.inner_text() for m in page.locator("#chat-list .chat-message .chat-from").all()] == ["Ann, 8:00\u00a0PM", "Raj, 8:00\u00a0PM", "Sam"]
        assert page.locator("#chat-empty").is_hidden() and fits(page)
        wait_for_calls(page, server, 1)
        assert server.meeting.controls_called == [("chat.seen", None)]
        page.wait_for_function("window.croomPage.model.status.meeting.chat.unread === 0", timeout=5000)     # the answer is in the model at once
        page.click("#back-from-chat")
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        assert page.locator("#meeting-secondary .button", has_text="Chat").inner_text().strip() == "Chat"      # and the button no longer counts them
        page.close()


def test_the_chat_screen_says_when_there_is_nothing_yet(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_sub_screen(page, "Chat", "chat")
        assert page.locator("#chat-empty").is_visible() and page.locator("#chat-empty").inner_text() == "No messages yet"
        assert page.locator("#chat-list .chat-message").count() == 0 and server.meeting.controls_called == []     # nothing unread: nothing to mark
        server.meeting.current_meeting.chat = a_chat(1)
        page.wait_for_function("document.querySelector('#chat-list .chat-message')", timeout=5000)
        assert page.locator("#chat-empty").is_hidden()
        page.close()


def test_the_chat_list_opens_at_its_newest_message_and_only_grows_at_the_foot(browser):
    """Newest last. The reader who has scrolled up is not pulled down by a new message, and one who is at the foot follows it; the list never
    rebuilds the messages it has (the provider keeps the last twenty: the first goes as the next comes)."""
    at_foot = "() => { const l = document.getElementById('chat-list'); return l.scrollHeight - l.scrollTop - l.clientHeight < 2; }"
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        meeting = server.meeting.current_meeting
        meeting.chat = a_chat(20)
        page.wait_for_function("window.croomPage.model.status.meeting.chat.messages.length === 20", timeout=5000)
        open_sub_screen(page, "Chat", "chat")
        assert page.evaluate("document.getElementById('chat-list').scrollHeight > document.getElementById('chat-list').clientHeight")    # twenty do not fit: it scrolls inside itself
        assert page.evaluate(at_foot) and fits(page)
        page.evaluate("window.__old = [...document.querySelectorAll('#chat-list .chat-message')]; document.getElementById('chat-list').scrollTop = 0")
        meeting.chat = a_chat(20, first=2)                                    # message 21 comes in, and message 1 is the one that goes
        poll_until(page, "document.querySelectorAll('#chat-list .chat-text')[19].textContent === 'Message number 21'")
        assert page.locator("#chat-list .chat-message").count() == 20 and not page.evaluate("window.__old[0].isConnected")
        assert page.evaluate("window.__old.slice(1).every((li, i) => li === document.querySelectorAll('#chat-list .chat-message')[i])")    # the others are the same elements
        assert page.evaluate("document.getElementById('chat-list').scrollTop") == 0           # the reader at the top stays there
        page.evaluate("document.getElementById('chat-list').scrollTop = document.getElementById('chat-list').scrollHeight")
        meeting.chat = a_chat(20, first=3)
        poll_until(page, "document.querySelectorAll('#chat-list .chat-text')[19].textContent === 'Message number 22'")
        assert page.evaluate(at_foot)                                                         # the one at the foot follows it
        page.close()


def test_the_host_screen_offers_the_host_actions_with_a_second_tap_to_end(browser):
    with PageServer(role="host") as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_sub_screen(page, "Host", "host")
        labels = [b.inner_text().strip() for b in page.locator("#host-actions .button").all()]
        assert labels == ["Mute all", "Ask all to unmute", "Start recording", "Lock meeting", "End meeting for everyone"]
        assert drawn_icons(page, "#host-actions") == ["Mute all: #icon-mic-off", "Ask all to unmute: #icon-mic", "Start recording: #icon-record",
                                                      "Lock meeting: #icon-unlock", "End meeting for everyone: #icon-x-circle"]      # the picture is the state: an open padlock
        assert fits(page)
        page.locator("#host-actions .button", has_text="Mute all").click()
        wait_for_calls(page, server, 1)
        page.evaluate("window.__record = [...document.querySelectorAll('#host-actions .button')][2]")
        page.locator("#host-actions .button", has_text="Start recording").click()
        page.wait_for_function("document.querySelector('#host-actions').textContent.includes('Stop recording')", timeout=5000)
        assert page.evaluate("window.__record.isConnected && window.__record.textContent.trim() === 'Stop recording'")       # the same button, with the words of its state
        page.locator("#host-actions .button", has_text="Lock meeting").click()
        page.wait_for_function("document.querySelector('#host-actions').textContent.includes('Unlock meeting')", timeout=5000)
        assert drawn_icons(page, "#host-actions")[2:4] == ["Stop recording: #icon-record", "Unlock meeting: #icon-lock"]      # a closed padlock, while it is locked
        page.locator("#host-actions .button", has_text="Ask all to unmute").click()
        wait_for_calls(page, server, 4)
        page.locator("#host-actions .button", has_text="End meeting").click()
        assert page.locator("#host-actions .button", has_text="Tap again to end").count() == 1
        assert ("host.end", None) not in server.meeting.controls_called
        assert fits(page)
        page.wait_for_timeout(600)                                            # the second tap is a deliberate one: a tap within half a second of the ask would be a bounce
        page.locator("#host-actions .button", has_text="Tap again to end").click()
        wait_for_calls(page, server, 5)
        assert server.meeting.controls_called == [("host.mute_all", None), ("host.record", None), ("host.lock", None), ("host.ask_unmute", None), ("host.end", None)]
        page.close()


def test_the_host_screen_draws_only_what_the_model_offers(browser):
    with PageServer(role="host") as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_sub_screen(page, "Host", "host")
        host = server.meeting.current_meeting.controls["host"]
        host["mute_all"] = {"available": False}
        host["record"] = {"available": False, "on": False}
        page.wait_for_function("document.querySelectorAll('#host-actions .button').length === 3", timeout=5000)
        assert texts(page, "#host-actions") == ["Ask all to unmute", "Lock meeting", "End meeting for everyone"]
        page.close()


def test_a_co_host_has_the_host_screen_without_the_end_that_is_the_hosts(browser):
    with PageServer(role="co-host") as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_sub_screen(page, "Host", "host")
        assert texts(page, "#host-actions") == ["Mute all", "Ask all to unmute", "Start recording", "Lock meeting"]
        page.close()


def test_the_second_tap_to_end_lapses_after_five_seconds_and_is_forgotten_on_leaving_the_screen(browser):
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        page.clock.run_for(600)
        open_sub_screen(page, "Host", "host")
        page.clock.run_for(600)
        end = page.locator("#host-actions .button").last                          # End is the last of them, whatever it says
        end.click()
        assert end.inner_text().strip() == "Tap again to end"
        page.clock.run_for(4000)
        assert end.inner_text().strip() == "Tap again to end"                      # four seconds: still asking
        page.clock.run_for(1500)
        assert end.inner_text().strip() == "End meeting for everyone"             # five: it has lapsed, and nothing was sent
        end.click()
        page.click("#back-from-host")                                             # the ask is not carried to the next visit
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        page.clock.run_for(600)
        open_sub_screen(page, "Host", "host")
        assert end.inner_text().strip() == "End meeting for everyone"
        assert server.meeting.controls_called == []
        page.close()


def test_remove_asks_for_a_second_tap_on_its_own_row_and_the_ask_lapses_after_five_seconds(browser):
    """One stray tap must not throw someone out of the meeting: the first tap on Remove asks for a second, on that row alone, for five seconds."""
    with PageServer(role="host") as server:
        page, _ = a_meeting_with_a_roster(browser, server, 2)                     # Room, Ann, Raj, Priya, Tom
        priya, tom = row_button(page, "Priya", "Remove"), row_button(page, "Tom", "Remove")
        priya.click()
        assert priya.inner_text().strip() == "Tap again to remove" and tom.inner_text().strip() == "Remove"
        assert drawn_icons(page, "#participant-list li:nth-child(4)")[-1] == "Tap again to remove: #icon-x-circle"      # Priya's, the fourth
        assert server.meeting.controls_called == []                               # asked, not done
        tom.click()                                                               # another row's Remove: the ask moves there
        assert tom.inner_text().strip() == "Tap again to remove" and priya.inner_text().strip() == "Remove"
        page.clock.run_for(4000)
        assert tom.inner_text().strip() == "Tap again to remove"                  # four seconds: still asking
        page.clock.run_for(1500)
        assert tom.inner_text().strip() == "Remove" and server.meeting.controls_called == []      # five: lapsed
        tom.click()
        page.clock.run_for(600)                                                   # (a second tap within half a second of the ask is a bounce)
        tom.click()                                                               # the second tap, in time: Tom is removed
        wait_for_calls(page, server, 1)
        assert server.meeting.controls_called == [("host.remove", 16777220)]
        page.close()


def test_the_ask_to_remove_is_forgotten_on_leaving_the_screen_and_when_the_person_goes(browser):
    with PageServer(role="host") as server:
        page, people = a_meeting_with_a_roster(browser, server, 2)
        row_button(page, "Tom", "Remove").click()
        page.click("#back-from-participants")
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        page.clock.run_for(600)
        open_sub_screen(page, "Participants", "participants")
        page.clock.run_for(600)
        assert row_button(page, "Tom", "Remove").inner_text().strip() == "Remove"      # not carried to the next visit
        row_button(page, "Tom", "Remove").click()
        set_people(server, without(people, "Tom Becker"))                         # he leaves of his own accord ...
        poll_until(page, "document.querySelectorAll('#participant-list .participant').length === 4")
        set_people(server, people)                                                # ... and a person with his id is back
        poll_until(page, "document.querySelectorAll('#participant-list .participant').length === 5")
        assert row_button(page, "Tom", "Remove").inner_text().strip() == "Remove"      # nobody is asked about on his account
        assert server.meeting.controls_called == []
        page.close()


def test_a_slow_mute_makes_the_mute_buttons_inert_and_leaves_the_rest_of_the_screen_working(browser):
    with PageServer(role="host") as server:
        page, _ = a_meeting_with_a_roster(browser, server, 2)
        held = hold_requests(page, "**/api/meeting/control")
        remove = row_button(page, "Priya", "Remove")
        row_button(page, "Ann", "Mute").click()
        wait_for_held(page, held)
        assert held[0].request.post_data_json == {"name": "host.mute_one", "value": 16777217}
        assert row_button(page, "Ann", "Mute").is_disabled() and row_button(page, "Priya", "Mute").is_disabled() and remove.is_enabled()     # the control waits for its answer; nothing else does
        assert page.evaluate("window.croomPage.model.busy") is False
        held[0].fulfill(json={"meeting": page.evaluate("window.croomPage.model.status.meeting")})
        page.wait_for_function("!document.querySelector('#participant-list .button:disabled')", timeout=5000)
        server.meeting.current_meeting.controls["host"]["waiting_room"] = {"available": True, "waiting": [{"id": 9, "name": "Guest"}]}
        poll_until(page, "document.querySelector('#waiting-list .button')")
        page.locator("#waiting-list .button", has_text="Admit").click()          # Admit is a control of its own: inert until its answer, and the others work
        wait_for_held(page, held, 2)
        assert held[1].request.post_data_json == {"name": "host.admit", "value": 9}
        assert page.locator("#waiting-list .button").is_disabled() and row_button(page, "Ann", "Mute").is_enabled()
        held[1].fulfill(json={"meeting": page.evaluate("window.croomPage.model.status.meeting")})
        page.wait_for_function("!document.querySelector('#waiting-list .button:disabled')", timeout=5000)
        page.close()


def test_a_refusal_is_said_on_the_screen_it_happened_on_and_does_not_follow_the_person_back(browser):
    from croom.meeting.providers.base import MeetingControlError
    with PageServer(role="host") as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_sub_screen(page, "Host", "host")
        server.meeting.fail_control = MeetingControlError("Zoom's mute all button is not on the screen yet; try again in a moment")
        page.locator("#host-actions .button", has_text="Mute all").click()
        page.wait_for_function("document.getElementById('host-message').textContent !== ''", timeout=5000)
        assert page.locator("#host-message").inner_text() == "Zoom's mute all button is not on the screen yet; try again in a moment"
        assert page.locator("#host-actions .button:disabled").count() == 0 and fits(page)
        page.click("#back-from-host")
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        assert page.locator("#meeting-message").is_hidden()
        server.meeting.fail_control = MeetingControlError("Zoom's mute button is not on the screen yet; try again in a moment")
        open_sub_screen(page, "Participants", "participants")
        page.locator("#participant-list .participant:has-text('Ann') .button", has_text="Mute").click()
        page.wait_for_function("document.getElementById('participants-message').textContent !== ''", timeout=5000)
        assert page.locator("#participants-message").is_visible() and fits(page)
        page.close()


@pytest.mark.parametrize("words, view", [("Participants", "participants"), ("Chat", "chat"), ("Host", "host")])
def test_meeting_sub_screens_return_on_their_own_after_two_idle_minutes(browser, words, view):
    """Not ten, as the idle screens do: the meeting screen is the one a room needs. A tap anywhere starts the two minutes again."""
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        open_sub_screen(page, words, view)
        page.clock.fast_forward(100_000)                                      # a minute and 40 seconds: still here
        page.wait_for_timeout(200)
        assert page.evaluate("document.body.dataset.view") == view
        page.locator(f".view[data-view='{view}'] .kicker").first.click()      # a tap that goes nowhere ...
        page.clock.fast_forward(100_000)
        page.wait_for_timeout(200)
        assert page.evaluate("document.body.dataset.view") == view            # ... starts the two minutes again: 100 seconds in, since the tap
        page.clock.fast_forward(30_000)
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        page.close()


def test_chat_and_participants_are_offered_only_when_the_platform_has_them(browser):
    """Meet keeps no chat the agent reads (controls.chat.available is false) and reports a count with no roster: a count alone is not a screen,
    and a button that opens nothing is not drawn. Zoom's model has no chat key, and keeps the button."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        assert texts(page, "#meeting-secondary") == ["Captions on", "Gallery view", "Hide self view", "Participants (3)", "Chat", "TV"]
        meeting = server.meeting.current_meeting
        meeting.controls["chat"] = {"available": False}
        set_people(server, [], count=3)
        page.wait_for_function("!document.getElementById('meeting-secondary').textContent.includes('Chat')", timeout=5000)
        assert texts(page, "#meeting-secondary") == ["Captions on", "Gallery view", "Hide self view", "TV"]      # neither, and the rest as before
        meeting.controls["chat"] = {"available": True}
        set_people(server, a_roster(), count=3)
        page.wait_for_function("document.getElementById('meeting-secondary').textContent.includes('Chat')", timeout=5000)
        assert texts(page, "#meeting-secondary") == ["Captions on", "Gallery view", "Hide self view", "Participants (3)", "Chat", "TV"]
        page.close()


def test_a_meet_call_draws_what_the_meet_provider_reports_and_nothing_else(browser):
    """End to end through the real ControlService, with a stub meeting that reports the model the Meet provider builds (spec 2026-10-13,
    section 4.5): the buttons Meet's page has (microphone, camera, raise hand, captions), a count of people with no roster, no chat and no
    reactions. The primary row has no Reactions; the secondary row has Captions and the table's own TV, and nothing else: no View or Self view
    (Meet has no such controls here), no Participants (nothing to list), no Chat (nothing is read) and no Host."""
    from croom.meeting.providers.google_meet import meet_controls
    with PageServer() as server:
        server.meeting.controls = meet_controls({"camera": {"on": True}, "raise_hand": {"on": False}, "captions": {"on": False}}, muted=False)
        server.meeting.people = {"count": 3, "list": []}
        page = open_page(browser, server)
        page.fill("#link-input", "https://meet.google.com/abc-defg-hij")
        page.click("#link-form button[type=submit]")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        assert server.meeting.current_meeting.platform == "google_meet"
        assert texts(page, "#meeting-actions") == ["Mute", "Turn camera off", "Raise hand", "Leave"]
        assert texts(page, "#meeting-secondary") == ["Captions on", "TV"]
        page.locator("#meeting-secondary .button", has_text="Captions on").click()
        page.wait_for_function("document.getElementById('meeting-secondary').textContent.includes('Captions off')", timeout=5000)
        page.locator("#meeting-actions .button", has_text="Raise hand").click()
        page.wait_for_function("document.getElementById('meeting-actions').textContent.includes('Lower hand')", timeout=5000)
        assert server.meeting.controls_called == [("captions", None), ("raise_hand", None)]       # through the same control route as Zoom's
        assert texts(page, "#meeting-secondary") == ["Captions off", "TV"]
        assert fits(page)
        page.close()


@pytest.mark.parametrize("opener, view", [("#meeting-secondary .button:has-text('Participants')", "participants"), ("#meeting-secondary .button:has-text('Chat')", "chat"),
                                          ("#meeting-secondary .button:has-text('Host')", "host"), ("#open-camera-live", "camera"), ("#open-upcoming", "upcoming")])
def test_a_dialog_that_comes_up_brings_the_page_back_to_the_meeting_screen_once(browser, opener, view):
    """The card is on the meeting screen, and a person on one of the screens opened from it cannot see it. A dialog that arrives (or whose number
    or buttons change) brings them back, once: one that goes on waiting does not trap them on the meeting screen (they may look at Participants
    with one up), and one that goes by itself moves nobody. The return settles the screen as any view change does."""
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)

        def go_to_the_sub_screen():
            page.clock.run_for(600)                                           # the half second of the last view change is spent
            page.locator(opener).click()
            page.wait_for_function(f"document.body.dataset.view === '{view}'", timeout=5000)

        go_to_the_sub_screen()
        put_up(server, ZOOMS_DIALOG)
        poll_until(page, "window.croomPage.model.status.meeting.dialog !== null")
        assert page.evaluate("document.body.dataset.view") == "meeting" and page.locator("#meeting-dialog").is_visible()       # the edge: back to the card
        assert page.evaluate("document.body.classList.contains('settling')")  # and a thumb aimed at the sub-screen lands on nothing
        go_to_the_sub_screen()                                                # the person goes to look again, with the same dialog still waiting
        for _ in range(3):
            page.evaluate("window.croomPage.refreshStatus()")
        page.wait_for_timeout(300)
        assert page.evaluate("document.body.dataset.view") == view            # and nothing takes them back
        put_up(server, dict(ZOOMS_DIALOG, id=8))                              # another dialog: the edge again
        poll_until(page, "window.croomPage.model.status.meeting.dialog.id === 8")
        assert page.evaluate("document.body.dataset.view") == "meeting"
        go_to_the_sub_screen()
        put_up(server, dict(ZOOMS_DIALOG, id=8, buttons=["Stay", "Allow recording"]))    # the same number with other buttons is another dialog too
        poll_until(page, "window.croomPage.model.status.meeting.dialog.buttons[0] === 'Stay'")
        assert page.evaluate("document.body.dataset.view") == "meeting"
        go_to_the_sub_screen()
        server.meeting.current_meeting.dialog = None                          # one that goes by itself moves nobody
        poll_until(page, "window.croomPage.model.status.meeting.dialog === null")
        assert page.evaluate("document.body.dataset.view") == view and page.locator("#meeting-dialog").is_hidden()
        page.close()


def test_a_dialog_that_is_already_up_when_a_sub_screen_is_opened_does_not_take_it_away(browser):
    """The person who opens Participants with a card waiting wants the list: only a dialog that comes up while they are there is an edge."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        put_up(server, ZOOMS_DIALOG)
        poll_until(page, CARD)
        page.clock.run_for(600)
        open_sub_screen(page, "Participants", "participants")
        for _ in range(3):
            page.evaluate("window.croomPage.refreshStatus()")
        page.wait_for_timeout(300)
        assert page.evaluate("document.body.dataset.view") == "participants"
        page.close()


def test_the_screens_are_empty_while_the_meeting_is_not_connected_and_full_again_when_it_is(browser):
    """The block's lists can outlive a state (a reconnect puts the room in the lobby): only a connected meeting draws any of it."""
    with PageServer(role="host") as server:
        page = open_page(browser, server)
        join_by_id(page)
        server.meeting.current_meeting.chat = a_chat(2)
        open_sub_screen(page, "Participants", "participants")
        assert page.locator("#participant-list .participant").count() == 3
        server.meeting.set_state(MeetingState.IN_LOBBY)
        page.wait_for_function("document.querySelectorAll('#participant-list .participant').length === 0", timeout=5000)
        assert page.evaluate("document.body.dataset.view") == "participants" and page.locator("#chat-list .chat-message").count() == 0
        assert page.locator("#host-actions .button").count() == 0
        server.meeting.set_state(MeetingState.CONNECTED)
        page.wait_for_function("document.querySelectorAll('#participant-list .participant').length === 3", timeout=5000)
        assert page.locator("#chat-list .chat-message").count() == 2 and page.locator("#host-actions .button").count() == 5
        page.close()


def test_a_participants_row_follows_the_model_in_place_and_the_others_keep_their_elements(browser):
    """Rows are found by the person's id and patched: a hand goes up, a person is muted, a name changes, someone leaves and someone joins, and the
    rows of the others are the same elements, so that a finger on one of their buttons is not lost to a poll."""
    with PageServer(role="host") as server:
        page, people = a_meeting_with_a_roster(browser, server, 3)               # Room, Ann, Raj, Priya, Tom, Maria
        page.evaluate("window.__rows = [...document.querySelectorAll('#participant-list .participant')]")
        people[3].update(hand=True, name="Priya N.")
        poll_until(page, "document.querySelectorAll('#participant-list .mark-hand').length === 2")
        assert page.locator("#participant-list .participant").nth(3).locator(".name").inner_text() == "Priya N."
        people[3]["muted"] = True                                                # muted by someone else: Mute is for those who are not
        poll_until(page, "document.querySelectorAll('#participant-list .mark-muted').length === 2")
        assert texts(page, "#participant-list li:nth-child(4)") == ["Remove"]
        people = without(people, "Tom Becker") + [{"id": 16777300, "name": "Newcomer", "muted": False, "video": True, "hand": False, "host": False, "me": False}]
        set_people(server, people)                                               # Tom leaves, and Newcomer comes in at the foot
        poll_until(page, "[...document.querySelectorAll('#participant-list .name')].some((n) => n.textContent === 'Newcomer')")
        assert page.locator("#participant-list .participant .name").all_inner_texts() == ["Room 1 (this room)", "Ann Lee", "Raj Patel", "Priya N.", "Maria Santos", "Newcomer"]
        assert page.evaluate("""() => { const now = [...document.querySelectorAll('#participant-list .participant')];
            return [0, 1, 2, 3, 5].every((was, i) => now[i] === window.__rows[was]) && !window.__rows.includes(now[5]); }""")
        page.close()


def test_a_finger_down_on_a_participants_button_is_lifted_on_it_when_another_person_leaves_below(browser):
    """The finger is down on Maria's Mute as Chen's row, below hers, goes: nothing above him moved, and the lift is a tap on that button, for her."""
    with PageServer(role="host") as server:
        page, people = a_meeting_with_a_roster(browser, server, 4)               # Room, Ann, Raj, Priya, Tom, Maria, Chen
        x, y = centre(page, "#participant-list .participant:has-text('Maria') .button", "Mute")
        page.evaluate("window.__row = [...document.querySelectorAll('.participant')].find((r) => r.textContent.includes('Maria')); window.__mute = window.__row.querySelector('.button')")
        page.mouse.move(x, y)
        page.mouse.down()
        set_people(server, without(people, "Chen Wei"))
        poll_until(page, "document.querySelectorAll('#participant-list .participant').length === 6")
        assert page.evaluate("window.__mute.isConnected && window.__row.isConnected"), "the button under the finger was replaced"
        page.mouse.up()
        wait_for_calls(page, server, 1)
        assert server.meeting.controls_called == [("host.mute_one", 16777221)]
        page.close()


def test_a_row_that_moves_up_because_another_leaves_above_it_keeps_its_buttons_and_sends_its_own_person(browser):
    """Rows are found by the person, not by their place: when Priya leaves, Maria's row moves up into the place of hers, it is the same element,
    and its Mute is still Maria's."""
    with PageServer(role="host") as server:
        page, people = a_meeting_with_a_roster(browser, server, 4)
        page.evaluate("window.__row = [...document.querySelectorAll('.participant')].find((r) => r.textContent.includes('Maria')); window.__mute = window.__row.querySelector('.button')")
        set_people(server, without(people, "Priya Nair"))
        poll_until(page, "document.querySelectorAll('#participant-list .participant').length === 6")
        assert page.evaluate("window.__mute.isConnected && document.querySelectorAll('#participant-list .participant')[4] === window.__row")     # in the place Tom had
        page.evaluate("window.__mute.click()")                                   # a key on a keyboard: no pointer, so the settling screen does not stop it
        wait_for_calls(page, server, 1)
        assert server.meeting.controls_called == [("host.mute_one", 16777221)]
        page.close()


@pytest.mark.parametrize("direction", ["into a sub-screen", "out of a sub-screen"])
def test_a_tap_that_lands_as_the_view_changes_does_nothing(browser, direction):
    """The Host screen is where the meeting screen's rows were: a thumb that was aimed at the one lands on the other as the view changes under
    it (the second tap of a double tap on Host lands on Mute all, the second of one on Back lands on the meeting screen's Mute). The page ignores
    taps for half a second after a view change into or out of a sub-screen. The clock is stopped: the half second is the test's to spend."""
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server, touchscreen=True)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        page.clock.run_for(600)
        x, y = centre(page, "#meeting-secondary .button", "Host")
        page.touchscreen.tap(x, y)
        page.wait_for_function("document.body.dataset.view === 'host'", timeout=5000)
        if direction == "into a sub-screen":
            x, y = centre(page, "#host-actions .button", "Mute all")          # where the second tap lands
            assert button_under(page, x, y) == "Mute all", "the thumb is not over Mute all: this test means nothing"
        else:
            page.clock.run_for(600)
            back = page.locator("#back-from-host").bounding_box()
            x, y = back["x"] + back["width"] / 2, back["y"] + back["height"] - 12      # the lower edge of Back: Mute's top is under it on the meeting screen
            page.touchscreen.tap(x, y)
            page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
            assert button_under(page, x, y) == "Mute", "the thumb is not over Mute: this test means nothing"
        page.clock.run_for(150)
        page.touchscreen.tap(x, y)                                            # 150 ms after the view changed: the second tap of a double tap
        page.clock.run_for(300)
        page.touchscreen.tap(x, y)                                            # 450 ms: a slow one
        page.wait_for_timeout(300)                                            # (real time) long enough for a request to have gone out
        assert server.meeting.controls_called == [] and server.meeting.muted is False
        page.clock.run_for(200)
        page.touchscreen.tap(x, y)                                            # 650 ms: a tap meant for what is there now works
        if direction == "into a sub-screen":
            wait_for_calls(page, server, 1)
            assert server.meeting.controls_called == [("host.mute_all", None)]
        else:
            page.wait_for_function("document.querySelector('#meeting-actions .button').textContent.trim() === 'Unmute'", timeout=5000)
            assert server.meeting.muted is True
        page.close()


def test_a_tap_aimed_at_a_participants_button_does_not_land_on_another_when_a_row_above_goes(browser):
    """The rows below a person who leaves move up under the thumb that was aimed at one of them: Maria's Mute is where Tom's was a moment ago.
    The screen ignores taps for half a second after the list moves under it (the clock is stopped)."""
    with PageServer(role="host") as server:
        page, people = a_meeting_with_a_roster(browser, server, 3, touchscreen=True)      # Room, Ann, Raj, Priya, Tom, Maria
        x, y = centre(page, "#participant-list .participant:has-text('Tom') .button", "Mute")         # where the thumb is going
        set_people(server, without(people, "Priya Nair"))                         # Priya, above him, leaves: the rows below her move up
        poll_until(page, "document.querySelectorAll('#participant-list .participant').length === 5")
        assert person_under(page, x, y) == "Maria Santos" and button_under(page, x, y) == "Mute", "the thumb is not over Maria's Mute: this test means nothing"
        assert page.evaluate("document.body.classList.contains('settling')")
        page.clock.run_for(150)
        page.touchscreen.tap(x, y)
        page.clock.run_for(300)
        page.touchscreen.tap(x, y)
        page.wait_for_timeout(300)
        assert server.meeting.controls_called == []
        page.clock.run_for(200)
        page.touchscreen.tap(x, y)                                                # 650 ms: a tap on what is there now is a tap on it
        wait_for_calls(page, server, 1)
        assert server.meeting.controls_called == [("host.mute_one", 16777221)]
        page.close()


def test_a_tap_aimed_at_admit_does_not_admit_the_next_person_when_the_list_above_it_moves(browser):
    """The waiting room is below the meeting's people: when the last of them leaves, both Admit buttons move up under the thumb that was aimed
    at the first, and the second is where it was. The waiting rows are watched as the others are (the clock is stopped)."""
    with PageServer(role="host") as server:
        page, people = a_meeting_with_a_roster(browser, server, 1, touchscreen=True)      # Room, Ann, Raj, Priya
        server.meeting.current_meeting.controls["host"]["waiting_room"] = {"available": True, "waiting": [{"id": 90, "name": "Guest A"}, {"id": 91, "name": "Guest B"}]}
        poll_until(page, "document.querySelectorAll('#waiting-list .participant').length === 2")
        page.clock.run_for(600)
        x, y = centre(page, "#waiting-list .participant:has-text('Guest A') .button", "Admit")      # where the thumb is going
        set_people(server, without(people, "Priya Nair"))                         # Priya, the last of the meeting's people, leaves: the waiting room moves up
        poll_until(page, "document.querySelectorAll('#participant-list .participant').length === 3")
        assert person_under(page, x, y) == "Guest B" and button_under(page, x, y) == "Admit", "the thumb is not over Guest B's Admit: this test means nothing"
        page.clock.run_for(150)
        page.touchscreen.tap(x, y)
        page.clock.run_for(300)
        page.touchscreen.tap(x, y)
        page.wait_for_timeout(300)
        assert server.meeting.controls_called == []
        page.clock.run_for(200)
        page.touchscreen.tap(x, y)
        wait_for_calls(page, server, 1)
        assert server.meeting.controls_called == [("host.admit", 91)]             # what is there now, once the screen has settled
        page.close()


def test_the_participants_list_settles_when_a_row_moves_and_not_when_it_is_scrolled_or_grows_at_its_foot(browser):
    with PageServer(role="host") as server:
        page, people = a_meeting_with_a_roster(browser, server, 9)               # twelve: the list scrolls inside its box
        settling = "document.body.classList.contains('settling')"
        assert not page.evaluate(settling)
        page.evaluate("document.querySelector('.view[data-view=participants] .list-scroll').scrollTop = 150")
        page.evaluate("window.croomPage.refreshStatus()")
        page.wait_for_timeout(300)
        assert not page.evaluate(settling), "a scroll is not a move"
        people = people + [{"id": 16777400, "name": "Newcomer", "muted": False, "video": True, "hand": False, "host": False, "me": False}]
        set_people(server, people)
        poll_until(page, "document.querySelectorAll('#participant-list .participant').length === 13")
        assert not page.evaluate(settling), "a row that comes in below the rest moves none of them"
        set_people(server, without(people, "Tom Becker"))
        poll_until(page, "document.querySelectorAll('#participant-list .participant').length === 12")
        assert page.evaluate(settling), "a row that goes from among them moves those below it"
        page.clock.run_for(600)
        assert not page.evaluate(settling)
        page.close()


def test_a_message_that_comes_in_while_the_chat_is_open_is_marked_seen_too(browser):
    """Otherwise the count would climb on the button under the reader's nose."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        meeting = server.meeting.current_meeting
        meeting.chat = a_chat(2, unread=2)
        page.wait_for_function("document.getElementById('meeting-secondary').textContent.includes('Chat (2)')", timeout=5000)
        assert server.meeting.controls_called == []                              # not opened: nothing is marked seen
        open_sub_screen(page, "Chat", "chat")
        wait_for_calls(page, server, 1)
        meeting.chat = a_chat(3, unread=1)                                       # one more comes in while it is up (the agent had cleared the count)
        poll_until(page, "window.croomPage.model.status.meeting.chat.messages.length === 3")
        wait_for_calls(page, server, 2)
        for _ in range(3):
            page.evaluate("window.croomPage.refreshStatus()")
        page.wait_for_timeout(300)
        assert server.meeting.controls_called == [("chat.seen", None)] * 2       # once for each rise, and not once for each poll
        page.click("#back-from-chat")
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        meeting.chat = a_chat(4, unread=1)                                       # and one while the screen is not up: the button counts it
        page.wait_for_function("document.getElementById('meeting-secondary').textContent.includes('Chat (1)')", timeout=5000)
        assert len(server.meeting.controls_called) == 2
        open_sub_screen(page, "Chat", "chat")                                    # and opening the screen marks it
        wait_for_calls(page, server, 3)
        page.close()


def test_an_unread_count_the_agent_does_not_clear_is_marked_seen_once_and_again_when_it_rises(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        server.meeting.on_control = lambda name, value: None                     # an agent that leaves the count as it is
        meeting = server.meeting.current_meeting
        meeting.chat = a_chat(2, unread=2)
        page.wait_for_function("document.getElementById('meeting-secondary').textContent.includes('Chat (2)')", timeout=5000)
        open_sub_screen(page, "Chat", "chat")
        wait_for_calls(page, server, 1)
        for _ in range(3):
            page.evaluate("window.croomPage.refreshStatus()")
        page.wait_for_timeout(300)
        assert server.meeting.controls_called == [("chat.seen", None)]           # asked once, though the count is still 2 at every poll
        meeting.chat = a_chat(3, unread=3)
        poll_until(page, "window.croomPage.model.status.meeting.chat.messages.length === 3")
        wait_for_calls(page, server, 2)
        page.click("#back-from-chat")                                            # and the person who comes back to it finds the count still there: opening marks it
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        open_sub_screen(page, "Chat", "chat")
        wait_for_calls(page, server, 3)
        assert server.meeting.controls_called == [("chat.seen", None)] * 3
        page.close()


def test_the_meetings_words_are_shown_as_text_never_as_markup(browser):
    """Names and chat lines are other people's words: a name with markup in it is drawn as the characters it is made of. A 400-character line
    with no break in it wraps inside the screen."""
    unbroken = "x" * 400
    with PageServer(role="host") as server:
        page = open_page(browser, server)
        join_by_id(page)
        people = a_roster(1)
        people[3]["name"] = NASTY
        set_people(server, people)
        meeting = server.meeting.current_meeting
        meeting.controls["host"]["waiting_room"] = {"available": True, "waiting": [{"id": 9, "name": NASTY}]}
        meeting.chat = {"unread": 0, "messages": [{"from": NASTY, "text": NASTY, "at": ""}, {"from": "Ann", "text": unbroken, "at": "2026-10-13T20:00:00Z"}]}
        page.wait_for_function("window.croomPage.model.status.meeting.chat.messages.length === 2", timeout=5000)
        open_sub_screen(page, "Participants", "participants")
        assert page.locator("#participant-list .participant .name").nth(3).inner_text() == NASTY
        assert page.locator("#waiting-list .participant .name").inner_text() == NASTY
        assert page.locator("#participant-list b, #participant-list img, #participant-list script, #waiting-list b, #waiting-list img, #waiting-list script").count() == 0
        page.click("#back-from-participants")
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        open_sub_screen(page, "Chat", "chat")
        assert page.locator("#chat-list .chat-from").nth(0).inner_text() == NASTY      # no time to add: the message has none
        assert page.locator("#chat-list .chat-text").nth(0).inner_text() == NASTY
        assert page.locator("#chat-list b, #chat-list img, #chat-list script").count() == 0
        assert page.locator("#chat-list .chat-text").nth(1).inner_text() == unbroken
        shown = page.evaluate("""() => { const l = document.getElementById('chat-list'), t = l.querySelectorAll('.chat-text')[1].getBoundingClientRect();
            return { fits: l.scrollWidth <= l.clientWidth, inside: t.right <= l.getBoundingClientRect().right }; }""")
        assert shown == {"fits": True, "inside": True} and fits(page)
        assert page.evaluate("window.__pwned") is None
        page.close()


@pytest.mark.parametrize("width, height", [(1280, 800), (1920, 1200)])
def test_the_sub_screens_fit_with_twelve_people_twenty_messages_and_every_host_action(browser, width, height):
    """The fixed screen (spec 2026-10-12) holds each of them: the list box scrolls inside itself and not the page, and Back and the heading stay
    where they are."""
    with PageServer(role="host") as server:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        join_by_id(page)
        meeting = server.meeting.current_meeting
        people = a_roster(9)                                                     # twelve, with every mark somewhere
        people[3].update(muted=True, video=False, hand=True)
        people[4].update(host=True, hand=True)
        people[5]["name"] = "A name of a person who has a very long name indeed and a surname to match, Esq."
        set_people(server, people)
        meeting.controls["host"]["waiting_room"] = {"available": True, "waiting": [{"id": 90 + i, "name": f"Guest {i + 1}"} for i in range(3)]}
        meeting.chat = a_chat(20, unread=3)
        meeting.chat["messages"][19]["text"] = ("A long message that goes on and on to the end of the line and then some. " * 6)[:400]
        page.wait_for_function("document.getElementById('meeting-secondary').textContent.includes('Participants (12)')", timeout=5000)

        open_sub_screen(page, "Participants", "participants")
        assert page.locator("#participant-list .participant").count() == 12 and page.locator("#waiting-list .participant").count() == 3
        assert fits(page), "the participants screen scrolls with twelve people and a waiting room"
        shown = page.evaluate("""() => { const s = document.querySelector('.view[data-view=participants] .list-scroll'), back = document.getElementById('back-from-participants'),
            kicker = document.querySelector('.view[data-view=participants] .kicker'), top = () => [back.getBoundingClientRect().top, kicker.getBoundingClientRect().top];
            const before = top(); s.scrollTop = s.scrollHeight; return { scrolls: s.scrollHeight > s.clientHeight, before, after: top(), page: document.documentElement.scrollTop + document.querySelector('main').scrollTop }; }""")
        assert shown["before"] == shown["after"] and shown["page"] == 0, shown      # Back and the heading stay, and the page does not scroll
        if width == 1280:
            assert shown["scrolls"], "twelve people and a waiting room are more than 1280 by 800 has room for: the list should scroll inside its box"
        page.click("#back-from-participants")
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)

        open_sub_screen(page, "Chat", "chat")
        assert page.locator("#chat-list .chat-message").count() == 20 and fits(page), "the chat screen scrolls with twenty messages"
        shown = page.evaluate("""() => { const l = document.getElementById('chat-list'); return { scrolls: l.scrollHeight > l.clientHeight, page: document.documentElement.scrollTop + document.querySelector('main').scrollTop }; }""")
        assert shown["page"] == 0 and (shown["scrolls"] or width > 1280)
        page.click("#back-from-chat")
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)

        open_sub_screen(page, "Host", "host")
        assert len(page.locator("#host-actions .button").all()) == 5 and fits(page)
        page.locator("#host-actions .button", has_text="End meeting").click()
        assert page.locator("#host-actions .button", has_text="Tap again to end").count() == 1 and fits(page), "the host screen scrolls with End asking for its second tap"
        page.close()


def test_the_sub_screens_on_a_phone_never_scroll_sideways(browser):
    with PageServer(role="host") as server:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        join_by_id(page)
        meeting = server.meeting.current_meeting
        people = a_roster(9)
        people[5]["name"] = "A name of a person who has a very long name indeed and a surname to match, Esq."
        people[4].update(host=True, hand=True, video=False)                      # marks and both buttons: the widest row there is
        set_people(server, people)
        meeting.controls["host"]["waiting_room"] = {"available": True, "waiting": [{"id": 90, "name": "A guest in the waiting room with a name that goes on"}]}
        meeting.chat = {"unread": 0, "messages": [{"from": "Ann", "text": "x" * 400, "at": "2026-10-13T20:00:00Z"}, {"from": "Raj", "text": "Yes", "at": ""}]}
        page.wait_for_function("document.getElementById('meeting-secondary').textContent.includes('Participants (12)')", timeout=5000)
        wide = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        for words, view in (("Participants", "participants"), ("Chat", "chat"), ("Host", "host")):
            page.locator("#meeting-secondary .button", has_text=words).scroll_into_view_if_needed()
            open_sub_screen(page, words, view)
            assert page.evaluate(wide), view
            if view == "host":
                page.locator("#host-actions .button", has_text="End meeting").click()
                assert page.evaluate(wide) and page.locator("#host-actions .button").last.is_visible()
            page.click(f"#back-from-{view}")
            page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        page.close()


# --- fix round 1: every view change in a meeting settles the page, and an armed button cannot be confirmed by a bounce ---

@pytest.mark.parametrize("opener, back", [("#open-camera-live", "#back-from-camera"), ("#open-upcoming", "#back-from-upcoming")])
def test_a_double_tap_on_back_from_camera_or_upcoming_does_not_toggle_the_rooms_mute(browser, opener, back):
    """Back is 64 px high at the top of the screen, and the meeting screen's Mute begins under its lower edge at the kiosk's 1280 by 800: the second tap of
    a double tap lands on Mute. Every view change in a meeting settles the page, not only the three sub-screens."""
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server, touchscreen=True)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        page.clock.run_for(600)
        x, y = centre(page, opener)
        page.touchscreen.tap(x, y)
        page.wait_for_function("document.body.dataset.view !== 'meeting'", timeout=5000)
        page.clock.run_for(600)                                               # the half second of the way in
        box = page.locator(back).bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] - 10    # the lower edge of Back
        page.touchscreen.tap(x, y)
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        assert button_under(page, x, y) == "Mute", "the thumb is not over Mute: this test means nothing"
        page.clock.run_for(150)
        page.touchscreen.tap(x, y)                                            # the second tap of a double tap
        page.wait_for_timeout(300)
        assert server.meeting.muted is False
        page.clock.run_for(500)
        page.touchscreen.tap(x, y)                                            # a tap meant for Mute, once the screen has settled
        page.wait_for_function("document.querySelector('#meeting-actions .button').textContent.trim() === 'Unmute'", timeout=5000)
        assert server.meeting.muted is True
        page.close()


def test_each_view_change_in_a_meeting_starts_its_half_second_again(browser):
    """A second view change 300 ms after the first keeps the page settling until half a second after the second, not the first."""
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        page.clock.run_for(600)
        page.evaluate("window.croomPage.setView('camera')")
        page.clock.run_for(300)
        page.evaluate("window.croomPage.setView('meeting')")
        page.clock.run_for(300)                                               # 600 ms after the first change, 300 after the second
        assert page.evaluate("document.body.classList.contains('settling')")
        page.clock.run_for(250)                                               # 550 ms after the second
        assert not page.evaluate("document.body.classList.contains('settling')")
        page.close()


@pytest.mark.parametrize("what", ["remove", "end", "leave"])
def test_a_second_tap_within_half_a_second_of_the_ask_does_not_confirm(browser, what):
    """A bounce, or a double tap, is not the second tap that was asked for: the ask ignores a tap for half a second. A deliberate one after that confirms."""
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server, touchscreen=True)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        if what == "remove":
            set_people(server, a_roster(2))
            poll_until(page, "window.croomPage.model.status.meeting.participants.list.length === 5")
        page.clock.run_for(600)
        if what == "remove":
            open_sub_screen(page, "Participants", "participants")
            page.clock.run_for(600)
            ask = "#participant-list .participant:has-text('Priya') .button:last-child"
        elif what == "end":
            open_sub_screen(page, "Host", "host")
            page.clock.run_for(600)
            ask = "#host-actions .button:last-child"
        else:
            ask = "#meeting-actions .button.danger"
        x, y = centre(page, ask)
        page.touchscreen.tap(x, y)                                            # the ask
        assert "Tap again" in page.locator(ask).inner_text()
        x, y = centre(page, ask)                                              # where the asking button is now
        page.clock.run_for(150)
        page.touchscreen.tap(x, y)                                            # the bounce
        page.wait_for_timeout(300)
        assert server.meeting.controls_called == [] and page.evaluate("document.body.dataset.state") == "meeting"
        page.clock.run_for(400)                                               # 550 ms since the ask
        page.touchscreen.tap(x, y)
        if what == "leave":
            page.wait_for_function("document.body.dataset.state !== 'meeting'", timeout=5000)
        else:
            wait_for_calls(page, server, 1)
            assert server.meeting.controls_called == ([("host.remove", 16777219)] if what == "remove" else [("host.end", None)])      # Priya's id, or the meeting
        page.close()


@pytest.mark.parametrize("words, view, ask, back", [
    ("Host", "host", "#host-actions .button:last-child", "#back-from-host"),
    ("Participants", "participants", "#participant-list .participant:has-text('Priya') .button:last-child", "#back-from-participants"),
])
def test_each_button_that_asks_for_a_second_tap_has_its_own_five_seconds(browser, words, view, ask, back):
    """Leave asks on the meeting screen, and the person goes to Host (or Participants) and asks there, End (or Remove): the one's five seconds are not
    the other's, and Leave has lapsed by itself when they come back, though the other asked later."""
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        set_people(server, a_roster(2))
        poll_until(page, "window.croomPage.model.status.meeting.participants.list.length === 5")
        page.clock.run_for(600)
        page.locator("#meeting-actions .button.danger").click()               # Leave asks
        assert page.locator("#meeting-actions .button.danger").inner_text().strip() == "Tap again to leave"
        page.clock.run_for(1000)
        open_sub_screen(page, words, view)
        page.clock.run_for(600)
        page.locator(ask).click()                                             # and so does the other, 1.6 s after Leave
        assert "Tap again" in page.locator(ask).inner_text()
        page.clock.run_for(4000)                                              # 5.6 s after Leave asked: its five seconds are over, the other's have a second to go
        assert "Tap again" in page.locator(ask).inner_text()
        page.click(back)
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        assert page.locator("#meeting-actions .button.danger").inner_text().strip() == "Leave"
        assert server.meeting.controls_called == [] and server.meeting.leaves == 0
        page.close()


# --- fix round 1: the model's truth ---

def test_a_person_whose_video_is_not_known_is_not_marked_video_off(browser):
    """The bridge says null for a camera Zoom did not speak of; only an explicit false is Video off."""
    with PageServer(role="host") as server:
        page, people = a_meeting_with_a_roster(browser, server, 2)
        people[3]["video"] = None
        people[4]["video"] = False
        set_people(server, people)
        poll_until(page, "document.querySelectorAll('#participant-list .mark-video-off').length === 2")    # Raj (the stub's) and Tom
        assert page.locator("#participant-list .participant:has-text('Priya') .mark-video-off").count() == 0
        page.close()


# --- fix round 1: armed buttons keep their place, a long refusal stays in its box, the lists do not outlive the connection ---

def row_boxes(page, person):
    """Where the buttons of this person's row are, left to right: [x, right, y, height] of each, in whole pixels."""
    return page.evaluate("""(person) => [...[...document.querySelectorAll('#participant-list .participant')].find((r) => r.textContent.includes(person)).querySelectorAll('.button')]
        .map((b) => { const r = b.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.right), Math.round(r.top), Math.round(r.height)]; })""", person)


def test_asking_for_a_second_tap_on_remove_does_not_move_the_buttons_of_its_row(browser):
    """The ask has longer words than Remove: it must not grow leftwards over the place of the Mute beside it (a tap aimed at Mute, a moment after a
    mis-tap on Remove, would remove the person). Nothing in the row moves when Remove asks, and when it lapses."""
    with PageServer(role="host") as server:
        page, _ = a_meeting_with_a_roster(browser, server, 2, touchscreen=True)
        before = row_boxes(page, "Tom")
        assert len(before) == 2
        mute = page.locator("#participant-list .participant:has-text('Tom') .button", has_text="Mute").bounding_box()
        x, y = mute["x"] + mute["width"] / 2, mute["y"] + mute["height"] / 2
        row_button(page, "Tom", "Remove").click()
        assert row_button(page, "Tom", "Tap again").count() == 1
        assert row_boxes(page, "Tom") == before
        assert button_under(page, x, y) == "Mute"
        page.clock.run_for(5500)                                                  # the ask lapses
        assert row_button(page, "Tom", "Tap again").count() == 0 and row_boxes(page, "Tom") == before
        page.close()


@pytest.mark.parametrize("width, height", [(1280, 800), (1920, 1200)])
def test_asking_for_a_second_tap_to_end_does_not_move_the_button(browser, width, height):
    """At 1280 by 800 End, on the second line with the longer words, would jump up to the first line when it asks (the shorter words fit there): the
    button under the finger must not move."""
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server, width, height)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        page.clock.run_for(600)
        open_sub_screen(page, "Host", "host")
        page.clock.run_for(600)
        end = page.locator("#host-actions .button.danger")
        before = end.bounding_box()
        end.click()
        assert end.inner_text().strip() == "Tap again to end"
        assert end.bounding_box() == before
        page.clock.run_for(5500)
        assert end.inner_text().strip() == "End meeting for everyone" and end.bounding_box() == before
        page.close()


A_LONG_REFUSAL = ("Zoom's mute button is not on the screen yet, so the room could not do that; try again in a moment, or ask the person to do it themselves, "
                  "and if it still does not work tell the host, or leave the meeting and join it again.")


@pytest.mark.parametrize("width, height", [(1024, 700), (1280, 800), (1920, 1080), (2560, 1440)])
@pytest.mark.parametrize("words, view, ask", [("Participants", "participants", "#participant-list .participant:has-text('Ann') .button"),
                                              ("Host", "host", "#host-actions .button")])
def test_a_refusal_of_several_lines_stays_in_its_box_and_the_screen_still_fits(browser, width, height, words, view, ask):
    """The line at the foot of the list keeps its height when the list is long: the list gives way, not the words, and the page does not scroll."""
    from croom.meeting.providers.base import MeetingControlError
    assert len(A_LONG_REFUSAL) >= 200
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server, width, height)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        set_people(server, a_roster(9))                                            # twelve: the list is as long as a list gets on the kiosk
        poll_until(page, "window.croomPage.model.status.meeting.participants.list.length === 12")
        page.clock.run_for(600)
        open_sub_screen(page, words, view)
        page.clock.run_for(600)
        server.meeting.fail_control = MeetingControlError(A_LONG_REFUSAL)
        page.locator(ask).first.click()
        page.wait_for_function(f"document.getElementById('{view}-message').textContent.length > 150", timeout=5000)
        assert fits(page), "the screen scrolls under a refusal of several lines"
        assert page.locator(f"#{view}-message").is_visible()
        page.close()


def test_a_refusal_said_on_the_meeting_screen_is_not_said_again_on_the_sub_screens(browser):
    """A sub-screen's line shows the refusals of that screen's own controls: not the one the meeting screen's Raise hand drew, when the person opens
    Participants or Host, and not the one a control of the meeting screen draws after they have gone there."""
    from croom.meeting.providers.base import MeetingControlError
    words = "Zoom's raise hand button is not on the screen yet; try again in a moment"
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        page.clock.run_for(600)
        server.meeting.fail_control = MeetingControlError(words)
        page.locator("#meeting-actions .button", has_text="Raise hand").click()
        page.wait_for_function("document.getElementById('meeting-message').textContent !== ''", timeout=5000)
        for opener, view in (("Participants", "participants"), ("Host", "host")):
            page.clock.run_for(600)
            open_sub_screen(page, opener, view)
            assert page.locator(f"#{view}-message").inner_text() == "" and page.locator(f"#{view}-message").is_hidden()
            page.clock.run_for(600)
            page.click(f"#back-from-{view}")
            page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
            assert page.locator("#meeting-message").inner_text() == words              # still where it was said
        server.meeting.fail_control = None
        page.clock.run_for(600)
        held = hold_requests(page, "**/api/meeting/control")
        page.locator("#meeting-actions .button", has_text="Raise hand").click()        # a slow control of the meeting screen ...
        wait_for_held(page, held)
        page.clock.run_for(600)                                                        # (the old refusal's line went with the new request: the screen has moved)
        open_sub_screen(page, "Participants", "participants")                          # ... and the person looks at the participants meanwhile
        held[0].fulfill(status=409, json={"error": words})                             # ... when its refusal comes
        page.wait_for_function("!window.croomPage.model.pending.has('raise_hand')", timeout=5000)
        assert page.locator("#participants-message").inner_text() == ""
        page.clock.run_for(600)
        page.click("#back-from-participants")
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        assert page.locator("#meeting-message").inner_text() == words                  # it is said on the screen it is about
        page.unroute("**/api/meeting/control")                                         # (the held route has been answered: the next control goes through)
        server.meeting.fail_control = MeetingControlError("Zoom's mute all button is not on the screen yet; try again in a moment")
        page.clock.run_for(600)
        open_sub_screen(page, "Host", "host")
        page.clock.run_for(600)
        page.locator("#host-actions .button", has_text="Mute all").click()               # Host has a refusal of its own ...
        page.wait_for_function("document.getElementById('host-message').textContent !== ''", timeout=5000)
        page.click("#back-from-host")
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        server.meeting.fail_mute = MeetingControlError("Zoom's microphone button is not on the screen yet; try again in a moment")
        page.clock.run_for(600)
        page.locator("#meeting-actions .button", has_text="Mute").click()               # ... and a quick action of the meeting screen is refused after it
        page.wait_for_function("document.getElementById('meeting-message').textContent.includes('microphone')", timeout=5000)
        page.clock.run_for(600)
        open_sub_screen(page, "Host", "host")
        assert page.locator("#host-message").inner_text() == ""                        # and Host does not say that one either
        page.close()


@pytest.mark.parametrize("words, view, what", [("Participants", "participants", "#participant-list .participant"), ("Chat", "chat", "#chat-list .chat-message"),
                                               ("Host", "host", "#host-actions .button")])
def test_the_screens_empty_when_the_room_cannot_be_reached_and_fill_again_when_it_answers(browser, words, view, what):
    """Offline the meeting screen's rows are gone and these are too, with their buttons: a list that kept its rows would have live buttons for a
    meeting nobody can reach, and a Remove or an End that could only fail."""
    with PageServer(role="host") as server:
        page = open_page(browser, server)
        join_by_id(page)
        server.meeting.current_meeting.chat = a_chat(2)
        open_sub_screen(page, words, view)
        page.wait_for_function(f"document.querySelectorAll('{what}').length >= 2", timeout=8000)
        page.route("**/api/status", lambda route: route.abort())
        page.wait_for_function("document.body.dataset.state === 'offline'", timeout=8000)
        assert page.evaluate("document.body.dataset.view") == view and page.locator(what).count() == 0
        assert page.locator(".view[data-view='" + view + "'] .button:not(.back)").count() == 0
        page.unroute("**/api/status")
        page.wait_for_function(f"document.querySelectorAll('{what}').length >= 2", timeout=8000)
        page.close()


@pytest.mark.parametrize("words, view", [("Participants", "participants"), ("Chat", "chat"), ("Host", "host")])
def test_a_sub_screen_that_is_left_alone_returns_after_two_minutes_with_no_tap_at_all(browser, words, view):
    """The wait starts with the view change itself: the tap that opened the screen came before it, so a screen that is never touched again must still
    return. The return test above taps the screen midway, which starts a wait of its own, and cannot tell."""
    with PageServer(role="host") as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        open_sub_screen(page, words, view)
        page.clock.fast_forward(125_000)
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        page.close()


def test_remove_is_inert_while_its_request_is_out(browser):
    with PageServer(role="host") as server:
        page, _ = a_meeting_with_a_roster(browser, server, 2)
        held = hold_requests(page, "**/api/meeting/control")
        row_button(page, "Tom", "Remove").click()
        page.clock.run_for(600)
        row_button(page, "Tom", "Remove").click()                                  # the second tap, in time: the request is out
        wait_for_held(page, held)
        assert held[0].request.post_data_json == {"name": "host.remove", "value": 16777220}
        assert row_button(page, "Tom", "Remove").is_disabled() and row_button(page, "Priya", "Remove").is_disabled()       # one control: its buttons wait together
        assert row_button(page, "Ann", "Mute").is_enabled() and page.evaluate("window.croomPage.model.busy") is False
        held[0].fulfill(json={"meeting": page.evaluate("window.croomPage.model.status.meeting")})
        page.wait_for_function("!document.querySelector('#participant-list .button:disabled')", timeout=5000)
        page.close()


def test_two_entries_with_the_same_key_are_two_rows_and_two_messages_with_the_same_words_are_two_lines(browser):
    """A person is found by the id (or the name when there is none); the second of two with the same key is told apart by its turn, and is not the
    first drawn again."""
    with PageServer(role="host") as server:
        page = open_page(browser, server)
        join_by_id(page)
        people = a_roster(1)
        twin = dict(people[3])                                                     # the same id, and the same name
        nameless = [{"name": "Visitor", "muted": False, "video": True, "hand": False, "host": False, "me": False} for _ in range(2)]    # no id at all
        set_people(server, people + [twin] + nameless)
        server.meeting.current_meeting.chat = {"unread": 0, "messages": [{"from": "Ann", "text": "ok", "at": "2026-10-13T20:00:00Z"}] * 2}
        page.wait_for_function("window.croomPage.model.status.meeting.participants.list.length === 7", timeout=5000)
        open_sub_screen(page, "Participants", "participants")
        assert page.locator("#participant-list .participant .name").all_inner_texts() == ["Room 1 (this room)", "Ann Lee", "Raj Patel", "Priya Nair", "Priya Nair", "Visitor", "Visitor"]
        page.click("#back-from-participants")
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        open_sub_screen(page, "Chat", "chat")
        assert page.locator("#chat-list .chat-message").count() == 2
        page.evaluate("window.__lines = [...document.querySelectorAll('#chat-list .chat-message')]")
        server.meeting.current_meeting.chat = {"unread": 0, "messages": [{"from": "Ann", "text": "ok", "at": "2026-10-13T20:00:00Z"}] * 3}
        poll_until(page, "document.querySelectorAll('#chat-list .chat-message').length === 3")
        assert page.evaluate("window.__lines.every((li, i) => li === document.querySelectorAll('#chat-list .chat-message')[i])")     # the two there are the same elements
        page.close()


def test_the_rooms_own_marks_sit_in_the_marks_column_with_the_others(browser):
    """The room's own row has no buttons, and its marks (Host, Hand raised) ended at the edge of the row instead of in the column the other rows'
    marks are in."""
    with PageServer(role="host") as server:
        page = open_page(browser, server)
        join_by_id(page)
        people = a_roster(1)
        people[0].update(host=True, hand=True)
        set_people(server, people)
        page.wait_for_function("window.croomPage.model.status.meeting.participants.list[0].hand === true", timeout=5000)
        open_sub_screen(page, "Participants", "participants")
        ends = page.evaluate("[...document.querySelectorAll('#participant-list .participant')].map((r) => Math.round(r.querySelector('.marks').getBoundingClientRect().right))")
        assert len(ends) == 4 and len(set(ends)) == 1, ends
        page.close()
