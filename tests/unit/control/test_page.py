"""
Browser tests for the room page, driven with the Playwright Chromium in the venv.

The control service runs with stub services on its own event loop in a
thread; the sync Playwright API drives a headless page against it. Skipped
when Playwright or its browser is not installed.
"""

import asyncio
import base64
import json
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

from croom.calendar.providers.base import CalendarEvent, MeetingPlatform
from croom.control import service as control_service
from croom.control.service import SIGN_BACKGROUNDS, STATIC_DIR, ControlService, read_setting
from croom.control.settings import SettingsStore
from croom.devices.errors import Interrupted, NotReady
from croom.meeting.providers.base import MeetingState
from croom.meeting.providers.zoom_sdk import PASSCODE_ASKED as ZOOM_PASSCODE_ASKED
from croom.scores.espn import ScheduleUnavailable
from croom.scores.service import ScoresService
from tests.unit.control.test_service import StubCalendar, StubDevices, StubMeeting, event, skip_if_day_ends_within
from tests.unit.scores.schedules import Feed, every_school, game, longest_names, schedule, texas_sample

playwright = pytest.importorskip("playwright.sync_api")


class PageServer:
    """ControlService with stubs, served on an ephemeral port from a background thread."""

    def __init__(self, calendar_events=(), room_name="Lab", room_location="2nd floor", calendar_connected=True, port=0, devices=None,
                 room_company=None, schedules=None):
        self.events = list(calendar_events)
        self.room_name = room_name
        self.room_location = room_location
        self.room_company = room_company   # None: the config says nothing, so the service's own default shows
        self.calendar_connected = calendar_connected
        self.port_arg = port
        self.schedules = schedules      # team id -> the schedule ESPN would answer; None leaves the room without a scoreboard
        self.feed = None                # the scoreboard's fake feed, once the server runs: what it was asked, and `down` to fail it
        self.devices = devices if devices is not None else StubDevices()
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
        self.meeting = StubMeeting()
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
        service = ControlService(
            config=config,
            meeting=self.meeting, calendar=StubCalendar(events=self.events, connected=self.calendar_connected),
            devices=self.devices, store=store, scores=scores,
        )
        await service.start()
        self.port = service.bound_port
        self._stop = asyncio.Event()
        self._ready.set()
        await self._stop.wait()
        await service.stop()
        if scores is not None:
            await scores.stop()

    def __enter__(self):
        self._thread.start()
        assert self._ready.wait(5), "page server did not start"
        return self

    def __exit__(self, *exc):
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(5)
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
        page = browser.new_page(viewport={"width": 1024, "height": 600})
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.locator("#chip-text").inner_text().startswith("Free until")
        assert sign_colors(page) == (SIGN_GREEN, SIGN_GREEN, SIGN_NAVY)   # a green rail and chip on navy, never a green wall
        assert page.locator(".chip").evaluate("e => getComputedStyle(e).color") == SIGN_NAVY   # navy words on green
        assert [b.get_attribute("id") for b in page.locator("button:visible").all()] == ["this-week"]  # a sign has one thing to press at a time (Back waits, hidden, for the week view)
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join",
                          data='{"url": "https://zoom.us/j/98765432100"}',
                          headers={"Content-Type": "application/json"})
        page.wait_for_function("document.body.dataset.state === 'occupied'", timeout=8000)
        assert page.locator("#chip-text").inner_text() == "In use"
        assert sign_colors(page) == (SIGN_RED, SIGN_RED, SIGN_NAVY)
        assert page.locator(".chip").evaluate("e => getComputedStyle(e).color") == "rgb(255, 255, 255)"   # white words on red
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/leave",
                          data="{}", headers={"Content-Type": "application/json"})
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=8000)
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
            page.click(f"#sign-picker button:text-is('{label}')")
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
        page.click("#scores-picker button:text-is('Texas A&M')")
        page.wait_for_function("document.querySelectorAll(\"#scores-picker button[aria-pressed='true']\").length === 1", timeout=5000)
        assert schools() == ["texas_am"] and pressed() == ["Texas A&M"]
        page.click("#scores-picker button:text-is('Texas')")
        page.wait_for_function("document.querySelectorAll(\"#scores-picker button[aria-pressed='true']\").length === 2", timeout=5000)
        assert schools() == ["texas", "texas_am"] and pressed() == ["Texas", "Texas A&M"]      # in the list's own order, not the order of the taps
        assert page.request.get(base + "/api/status").json()["settings"]["sign_scores"] == ["texas", "texas_am"]   # what the sign reads
        page.click("#scores-picker button:text-is('Texas A&M')")                                  # a switch turns off as it turned on
        page.wait_for_function("document.querySelectorAll(\"#scores-picker button[aria-pressed='true']\").length === 1", timeout=5000)
        assert schools() == ["texas"]
        page.reload(wait_until="networkidle")                                                      # and the page shows what the room remembers
        open_door_sign_screen(page)
        assert pressed() == ["Texas"]
        page.click("#scores-picker button:text-is('Texas')")
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
            else:                                                           # portrait: stacked, one under the next, the same height
                assert all(abs(b["x"] - boxes[0]["x"]) <= 0.5 and abs(b["width"] - boxes[0]["width"]) <= 0.5 for b in boxes)
                assert all(abs(b["height"] - boxes[0]["height"]) <= 1 for b in boxes)
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
        page = open_sign(browser, server, width, height)
        open_the_week(page)
        before, kept = week_fit(page), boxes(page, ".top", ".status", ".week-head")
        tops = page.locator("#week .day").evaluate_all("days => days.map((d) => d.getBoundingClientRect().top)")
        choose(server, sign_scores=["texas"])
        page.wait_for_function("!document.getElementById('score').hidden", timeout=8000)
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
    """Under 561 px of height the status's detail line is not shown while the week is, so that the days have the room for the count of
    what each leaves out ("+8 more"), which a day's box clips when it has none; the Today list brings the detail back."""
    skip_if_day_ends_within(3)
    with PageServer(calendar_events=a_full_week(), room_name=room, room_location=location) as server:
        for width, height in ((800, 480), (900, 500), (1024, 560)):
            page = open_sign(browser, server, width, height)
            assert page.locator("#detail").is_visible()                      # on the Today list the detail is there
            open_the_week(page)
            assert page.locator("#detail").is_hidden(), (width, height)
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
        page = open_sign(browser, server, 1024, 561)                         # one pixel more height and the detail stays
        open_the_week(page)
        assert page.locator("#detail").is_visible()
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
        page = open_sign(browser, server)
        strip_is_up(page)
        assert strip_text(page) == [TEXAS_LAST, ""] and page.locator("#score-next").evaluate("e => getComputedStyle(e).display") == "none"      # a half with nothing in it takes no room
        choose(server, sign_scores=["michigan"])
        scoreboard_has(server, ["michigan"])
        page.wait_for_function("document.getElementById('score-last').innerText === ''", timeout=8000)
        assert strip_text(page) == ["", MICHIGAN_NEXT] and page.locator("#score-last").evaluate("e => getComputedStyle(e).display") == "none"
        box, nxt = page.locator("#score").bounding_box(), page.locator("#score-next").bounding_box()
        assert nxt["x"] + nxt["width"] > box["x"] + box["width"] * 0.6      # the next game stays on the right when it is alone
        choose(server, sign_scores=["texas", "michigan"])                    # with two on, the name leads a school that has only a next game to give
        scoreboard_has(server, ["texas", "michigan"])
        page.wait_for_function("document.getElementById('score-last').innerText === 'Michigan'", timeout=8000)    # Michigan stays up: a poll does not move the strip on
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
        page = open_sign(browser, server, width, height)
        page.wait_for_selector("#events .event", timeout=5000)
        page.wait_for_function("document.body.dataset.photo === 'skyline'", timeout=5000)
        parts = (".top", ".brand", ".when", ".status", ".chip", "#detail", "#today", ".today-head", "#events .event")
        before = boxes(page, *parts)
        assert fits(page)
        choose(server, sign_scores=["texas"])
        strip_is_up(page)
        assert boxes(page, *parts) == before                                  # every box of the sign above the strip is where it was
        strip, today = page.locator("#score").bounding_box(), page.locator("#today").bounding_box()
        assert today["y"] + today["height"] <= strip["y"]                    # the strip takes room of its own and covers nothing
        assert abs(strip["y"] + strip["height"] - height) <= 0.5 and fits(page)
        choose(server, sign_scores=[])                                       # and its going moves nothing either
        page.wait_for_function("document.getElementById('score').hidden", timeout=8000)
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
            assert (week_size == pytest.approx(min(max(1.8 * root, 0.04 * width), 2.6 * root))) if short else (week_size == today_size), (width, height, week_size)   # ... and the name is smaller
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


def test_the_short_sign_rules_come_and_go_with_the_strip(browser, central_time):
    """At 1024 by 600 the week keeps its detail, location and room name until a school's strip is up, and has them again when it goes."""
    skip_if_day_ends_within(3)
    with PageServer(calendar_events=a_full_week(), room_name="Conference Room Twelve", room_location="2nd floor, by the kitchen", schedules=every_school()) as server:
        page = open_sign(browser, server, 1024, 600)
        open_the_week(page)
        plain = [page.locator(selector).is_visible() for selector in ("#detail", "#room-location")]
        size = page.locator("#room-name").evaluate("e => parseFloat(getComputedStyle(e).fontSize)")
        assert plain == [True, True] and size > 60 and page.evaluate("'strip' in document.body.dataset") is False
        choose(server, sign_scores=["texas"])
        page.wait_for_function("'strip' in document.body.dataset", timeout=8000)                      # the next poll brings the strip, and with it the rules
        assert [page.locator(selector).is_visible() for selector in ("#detail", "#room-location")] == [False, False]
        assert page.locator("#room-name").evaluate("e => parseFloat(getComputedStyle(e).fontSize)") < 45
        choose(server, sign_scores=[])
        page.wait_for_function("!('strip' in document.body.dataset)", timeout=8000)                   # and the next takes the strip away, and the rules with it
        assert [page.locator(selector).is_visible() for selector in ("#detail", "#room-location")] == [True, True]
        assert page.locator("#room-name").evaluate("e => parseFloat(getComputedStyle(e).fontSize)") == size and page.locator("#week .day").count() == 7
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


def test_the_room_names_type_is_fitted_again_when_the_strip_goes_while_the_week_is_up(browser, central_time):
    """With the strip up the week sets the name in a size of its own on a short sign, and without it the name is set as it is in Today: the one it
    was fitted to a moment before is not the one it has now."""
    skip_if_day_ends_within(3)
    bookings = [event(f"t{n}", f"Today {n}", 40 + 60 * n) for n in range(3)]
    with PageServer(calendar_events=bookings, room_name="Sales Conference Room", room_location="2nd floor", schedules=every_school()) as server:
        choose(server, sign_scores=["texas"])
        scoreboard_has(server, ["texas"])
        page = open_sign(browser, server, 1280, 480)
        strip_is_up(page)
        open_the_week(page)
        assert not room_name_is_long(page)                                       # at the week's own size the name takes one line: the large type is fitted to it
        choose(server, sign_scores=[])
        page.wait_for_function("!('strip' in document.body.dataset)", timeout=8000)    # the next poll takes the strip away, and the week's own size with it
        assert room_name_is_long(page) and lines_of(page, "#room-name") == [2] and fits(page)
        page.close()


def test_a_room_names_type_is_fitted_again_when_the_signs_font_arrives(browser):
    """Until Lexend arrives the name is set in a fallback font, whose lines are not Lexend's: a name that takes three lines in one and two in the other
    must end with the type its lines in Lexend need. The font's files are held back, here, until the test lets them go. Which names wrap differently
    depends on the fallback the machine has, so the first of a few that do on this one is used."""
    for name in ("North Training Room", "Customer Success Room", "Project Review Room", "Client Meeting Room", "Small Meeting Room", "Partner Meeting Room",
                 "Design Studio Room", "Conference Room Twelve", "Second Floor Conf Room 2", "Sales Conference Room", "Strategy Planning Room"):
        with PageServer(room_name=name, room_location="") as server:
            held = []
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            try:
                page.route("**/static/fonts/*.woff2", lambda route: held.append(route))
                page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="domcontentloaded")
                page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
                assert page.evaluate("document.fonts.status") == "loading" and held
                in_fallback = room_name_is_long(page)
                assert in_fallback == room_name_needs_the_smaller_type(page)           # fitted to what is on the screen now
                while held:
                    held.pop().continue_()
                page.wait_for_function("document.fonts.status === 'loaded'", timeout=5000)
                in_lexend = room_name_needs_the_smaller_type(page)
                if in_lexend == in_fallback:
                    continue                                                           # wraps alike in both on this machine: try another
                page.wait_for_function(f"document.getElementById('room-name').classList.contains('long') === {str(in_lexend).lower()}", timeout=3000)
                return
            finally:
                while held:                                                            # a failure before the files were let go leaves no request hanging
                    held.pop().continue_()
                page.close()
    pytest.skip("no candidate wraps differently in this machine's fallback font and in Lexend")


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
        page = open_sign(browser, server, width, height, state="occupied")
        assert re.fullmatch(r"Booked until \d+:\d\d.[AP]M", page.locator("#chip-text").inner_text())
        join_the_booking(server, "now")
        page.wait_for_function("document.getElementById('chip-text').textContent.startsWith('In use')", timeout=8000)
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
    of two lines and the strip up or down. A name of 184 characters, seven lines at 800 by 480, runs past the bottom by 6 px with the strip up, and by 39 px when
    the room is free for the rest of the day (its label is two lines there). When one of these stops being so, the docs change."""
    assert {length: len(name) for length, name in FLOOR_NAMES.items()} == {length: length for length in FLOOR_NAMES}
    for length in (67, 87, 110, 147):
        found = how_far_past_the_bottom(browser, FLOOR_NAMES[length], TWO_LINE_LOCATION, "free all day", SHORT_SIGNS)
        assert set(found.values()) == {0}, (length, found)
    for length, lines in ((67, 2), (87, 3), (147, 5), (184, 7)):
        with PageServer(room_name=FLOOR_NAMES[length], room_location=TWO_LINE_LOCATION) as server:
            page = open_sign(browser, server, 800, 480)
            size, stepped, taken = room_name_type(page)
            assert (round(size, 2), taken) == (FLOOR_REM if length > 67 else size, lines), (length, size, stepped, taken)
            page.close()
    seven = how_far_past_the_bottom(browser, FLOOR_NAMES[184], TWO_LINE_LOCATION, "free all day", ((800, 480),))
    assert abs(seven[(True, 800, 480)] - 39) <= 4 and seven[(False, 800, 480)] == 0, seven
    booked = how_far_past_the_bottom(browser, FLOOR_NAMES[184], TWO_LINE_LOCATION, "booked", ((800, 480),))
    assert abs(booked[(True, 800, 480)] - 6) <= 3, booked


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


@pytest.mark.parametrize("width, height", [(1280, 800), (800, 1280)])
def test_the_options_screen_with_its_keyboard_group_fits_the_kiosk_and_a_portrait_screen(browser, width, height):
    """The kiosk's 1280 by 800 and a portrait tablet's 800 by 1280 keep fits() on the Options screen with the group on it."""
    with PageServer() as server:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        page.click("#open-options")
        group = page.locator(".keyboard-section")
        buttons_end = "Math.max(...Array.from(document.querySelectorAll('.view[data-view=options] .menu-item')).map(b => b.getBoundingClientRect().bottom))"
        assert group.is_visible() and fits(page), "the options screen scrolls"
        box = group.bounding_box()
        assert 0 <= box["y"] - page.evaluate(buttons_end) <= 60 and box["y"] + box["height"] <= height   # right under the menu's buttons, on the screen
        # The door sign's branch gives this menu a third entry: with it the group is still under the buttons and on the screen, and nothing scrolls.
        page.evaluate("""() => { const entry = document.createElement('button'); entry.className = 'button quiet menu-item'; entry.textContent = 'Door sign';
            document.querySelector('.view[data-view=options] .menu').append(entry); }""")
        box = group.bounding_box()
        assert 0 <= box["y"] - page.evaluate(buttons_end) <= 60 and box["y"] + box["height"] <= height
        assert fits(page), "the options screen scrolls with a third entry"
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
        page.click("#meeting-actions button:has-text('Tap again to leave')")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        page.fill("#link-input", "https://meet.google.com/abc-defg-hij")       # no field for this one
        page.click("#link-form button[type=submit]")
        page.wait_for_function("document.body.dataset.state === 'meeting'", timeout=5000)
        page.click("#meeting-actions button:has-text('Leave')")
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
