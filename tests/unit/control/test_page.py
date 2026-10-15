"""
Browser tests for the room page, driven with the Playwright Chromium in the venv.

The control service runs with stub services on its own event loop in a
thread; the sync Playwright API drives a headless page against it. Skipped
when Playwright or its browser is not installed.
"""

import asyncio
import json
import math
import random
import socket
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from croom.control.service import ControlService
from croom.devices.errors import Interrupted, NotReady
from croom.meeting.providers.base import MeetingState
from croom.meeting.providers.zoom_sdk import PASSCODE_ASKED as ZOOM_PASSCODE_ASKED
from croom.tv.errors import TvError
from tests.unit.control.test_service import (
    BOARDROOM, StubCalendar, StubDevices, StubMeeting, StubMouse, StubSearch, StubTv, event, skip_if_day_ends_within,
)

playwright = pytest.importorskip("playwright.sync_api")


class PageServer:
    """ControlService with stubs, served on an ephemeral port from a background thread."""

    def __init__(self, calendar_events=(), room_name="Lab", room_location="2nd floor", calendar_connected=True, port=0, devices=None,
                 tv=None, discover=None, mouse=None):
        self.events = list(calendar_events)
        self.room_name = room_name
        self.room_location = room_location
        self.calendar_connected = calendar_connected
        self.port_arg = port
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
        self.meeting = StubMeeting()
        service = ControlService(
            config={"host": "127.0.0.1", "port": self.port_arg, "room_name": self.room_name, "room_location": self.room_location,
                    "settings_file": str(Path(self._settings_dir.name) / "control-settings.json")},
            meeting=self.meeting, calendar=StubCalendar(events=self.events, connected=self.calendar_connected),
            devices=self.devices, tv=self.tv, discover=self.discover, mouse=self.mouse,
        )
        await service.start()
        self.port = service.bound_port
        self._stop = asyncio.Event()
        self._ready.set()
        await self._stop.wait()
        await service.stop()

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


def test_door_sign_follows_the_room_state(browser):
    with PageServer(calendar_events=[event("e1", "Design review", 25)], room_name="Room 1") as server:
        page = browser.new_page(viewport={"width": 1024, "height": 600})
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.locator("#headline").inner_text().startswith("Free until")
        assert page.locator("#kicker").inner_text().upper() == "AVAILABLE"
        assert page.locator("button").count() == 0  # a sign has nothing to press
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join",
                          data='{"url": "https://zoom.us/j/98765432100"}',
                          headers={"Content-Type": "application/json"})
        page.wait_for_function("document.body.dataset.state === 'occupied'", timeout=8000)
        assert page.locator("#headline").inner_text() == "In use"
        assert page.locator("#kicker").inner_text().upper() == "IN USE"
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
        assert page.locator("#headline").inner_text().startswith("Booked until")
        assert page.locator("#kicker").inner_text().upper() == "BOOKED"
        assert page.locator("#detail").inner_text() == "Board lunch"
        page.close()


def test_door_sign_warns_before_the_next_meeting(browser):
    with PageServer(calendar_events=[event("e1", "Standup", 6)], room_name="Room 1") as server:
        page = browser.new_page(viewport={"width": 1024, "height": 600})
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        assert page.locator("#headline").inner_text().startswith("Standup starts in")
        assert page.locator("#kicker").inner_text().upper() == "STARTING SOON"
        page.close()


def test_door_sign_hides_the_schedule_without_a_calendar(browser):
    with PageServer(calendar_connected=False) as server:
        page = browser.new_page(viewport={"width": 1024, "height": 600})
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.locator("#headline").inner_text() == "Free"
        assert page.locator("#upcoming").count() == 1 and not page.locator("#upcoming").is_visible()
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
    assert page.locator("#headline").inner_text() == "Sign not connected"
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
DRAWABLE = {"path", "line", "circle", "rect", "polyline", "polygon", "ellipse"}


def test_the_icon_sprite_holds_every_symbol_and_each_one_draws_something():
    from xml.etree import ElementTree
    from croom.control.service import STATIC_DIR
    svg = "{http://www.w3.org/2000/svg}"
    symbols = list(ElementTree.parse(STATIC_DIR / "icons.svg").getroot().iter(svg + "symbol"))
    ids = [s.get("id") for s in symbols]
    assert len(ids) == len(set(ids)), "a symbol is there twice"
    assert {f"icon-{name}" for name in SPRITE_ICONS + TV_REMOTE_ICONS + CAMERA_ICONS} <= set(ids)
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
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-actions .button svg use')", timeout=5000)
        for b in page.locator("#meeting-actions .button").all():
            assert b.locator("svg.icon use").count() == 1 and b.locator("svg").get_attribute("aria-hidden") == "true"
            assert b.inner_text().strip() in ("Mute", "Unmute", "Turn camera off", "Turn camera on", "Leave")
        assert page.locator("#open-options svg use").count() == 0 or page.locator("#open-options").is_hidden()
        for name in ("Mute", "Turn camera off", "Leave"):                    # a screen reader still reads the label alone
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
        assert drawn_icons(page, "#meeting-actions") == ["Mute: #icon-mic", "Turn camera off: #icon-video", "Leave: #icon-phone-off"]   # both are live
        page.click("#meeting-actions button:has-text('Mute')")
        page.wait_for_selector("#meeting-actions button:has-text('Unmute')", timeout=5000)
        page.click("#meeting-actions button:has-text('Turn camera off')")
        page.wait_for_selector("#meeting-actions button:has-text('Turn camera on')", timeout=5000)
        assert drawn_icons(page, "#meeting-actions") == ["Unmute: #icon-mic-off", "Turn camera on: #icon-video-off", "Leave: #icon-phone-off"]   # both are off
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
        assert_includes(shown_icons(page), bar + ["#icon-back", "#icon-move", "#icon-monitor", "#icon-tv", "#icon-volume", "#icon-volume-x"])   # the last two are Key click on and off
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
        assert_includes(shown_icons(page), ["#icon-mic", "#icon-video", "#icon-phone-off", "#icon-volume", "#icon-volume", "#icon-volume", "#icon-move", "#icon-calendar", "#icon-tv"])
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
        assert page.locator("#meeting-actions .button").all_inner_texts() == ["Mute", "Turn camera off", "Leave"]
        assert page.evaluate("document.body.classList.contains('no-icons')")
        assert page.evaluate("[...document.querySelectorAll('svg.icon')].filter(s => s.getClientRects().length).length") == 0, "an empty icon box still takes room"
        for selector in ("#meeting-actions .button", "#link-form button[type=submit]"):    # the words sit in the middle of their buttons again
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


def test_the_clock_the_date_and_the_booking_times_read_the_american_way_under_a_twenty_four_hour_locale(browser):
    """The Pi's Chromium follows the system locale, which showed 24-hour times; the pages pin the format instead."""
    import re
    skip_if_day_ends_within(2)                                 # the booking is 90 minutes out, and the service lists today's only
    with PageServer(calendar_events=[event("e1", "Design review", 90)]) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800}, locale="en-GB")
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert re.match(TWELVE_HOUR, page.locator("#clock").inner_text())
        assert re.match(AMERICAN_DATE, page.locator("#date").inner_text())
        page.wait_for_selector("#events .event time", timeout=5000)
        assert re.match(TWELVE_HOUR, page.locator("#events .event time").first.inner_text())
        assert fits(page)
        page.close()


def test_the_door_sign_clock_and_date_read_the_american_way_under_a_twenty_four_hour_locale(browser):
    import re
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 1024, "height": 600}, locale="en-GB")
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert re.match(TWELVE_HOUR, page.locator("#clock").inner_text())
        assert re.match(AMERICAN_DATE, page.locator("#date").inner_text())
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
        page.click("#open-tv-meeting")
        assert page.evaluate("document.body.dataset.view") == "tv"
        assert page.locator("#tv-remote").is_visible() and page.locator("#open-options").is_hidden()   # in a meeting the bar has no Options
        page.click("#back-from-tv")
        assert page.evaluate("document.body.dataset.view") == "meeting"
        assert errors == []
        page.close()


def test_the_options_and_the_meeting_screen_each_have_the_tv_beside_the_others(browser):
    with PageServer(tv=RemoteTv()) as server:
        page = open_page(browser, server)
        page.click("#open-options")
        assert page.locator(".view[data-view=options] .menu button").evaluate_all("bs => bs.map(b => b.id)") == \
            ["open-camera-setup", "open-screensaver", "open-tv-options"]
        assert page.locator("#open-tv-options").inner_text().strip() == "TV" and page.locator("#open-tv-options svg use").get_attribute("href") == "#icon-tv"
        page.click("#back-from-options")
        join_by_id(page)
        assert page.locator(".view[data-view=meeting] .menu button").evaluate_all("bs => bs.map(b => b.id)") == \
            ["open-camera-live", "open-upcoming", "open-tv-meeting"]
        assert page.locator("#open-tv-meeting").inner_text().strip() == "TV" and page.locator("#open-tv-meeting svg use").get_attribute("href") == "#icon-tv"
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
        page.click("#open-tv-meeting")                                           # from the meeting
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
        page.click("#open-tv-meeting")
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
        page.wait_for_timeout(100)
        power.click()                                                            # the second tap, within five seconds
        assert tv_calls(page, server, 1) == [("control", "power", "off")]
        page.wait_for_function("document.getElementById('tv-turn-on') && !document.getElementById('tv-turn-on').hidden", timeout=5000)
        assert power.inner_text().strip() == "Power"                             # the TV is off now: the button is just Power, and waits for a tap
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
        assert page.locator("#tv-pairing .tv-found li").count() == 2                    # the two entries that are TVs
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
        assert "tv.host" in card.inner_text() and "/etc/croom/config.yaml on the room's Pi" in card.inner_text()     # to use another TV: the file, on the Pi
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
        assert [" ".join(t.split()) for t in rows.all_inner_texts()] == ["Boardroom TV This room's TV 10.0.0.41", "Lobby TV 10.0.0.57", "LG TV 10.0.0.60"]   # a TV with no name is an LG TV
        assert card.locator(".tv-found .tv-tag").count() == 1 and card.locator(".tv-found li").nth(0).locator(".tv-tag").count() == 1
        assert card.locator(".tv-found li:last-child").inner_text().split()[-1] == "10.0.0.60"
        assert page.locator("#tv-pairing a, #tv-pairing .tv-found button").count() == 0         # rows are not links or buttons: the address is for the config file
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
            assert "tv.host" in card.inner_text() and "/etc/croom/config.yaml on the room's Pi" in card.inner_text()    # the way to set one up: the file, on the Pi
            assert page.locator("#tv-pair").is_hidden()                          # nothing to pair yet
            assert page.locator("[data-tv]:visible").count() == 0
            assert status_line(page) == "TV · Not set up"
            page.click("#tv-find-unset")
            page.wait_for_selector("#tv-not-set-up .tv-found li", timeout=5000)
            assert " ".join(card.locator(".tv-found li").inner_text().split()) == "Boardroom TV 10.0.0.41"   # nothing is this room's: none is set up
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
        assert shown_icons(page) == ["#icon-settings", "#icon-back", "#icon-x-circle", "#icon-power", "#icon-monitor", "#icon-home", "#icon-menu",
                                     "#icon-volume", "#icon-up", "#icon-down",
                                     "#icon-return", "#icon-exit", "#icon-info", "#icon-list",
                                     "#icon-rewind", "#icon-play", "#icon-pause", "#icon-stop", "#icon-fast-forward",
                                     "#icon-pointer", "#icon-monitor", "#icon-touchpad"]
        page.click("#tv-input")
        assert shown_icons(page) == ["#icon-settings", "#icon-back", "#icon-x-circle", "#icon-check", "#icon-exit",
                                     "#icon-return", "#icon-exit", "#icon-info", "#icon-list",
                                     "#icon-rewind", "#icon-play", "#icon-pause", "#icon-stop", "#icon-fast-forward",
                                     "#icon-pointer", "#icon-monitor", "#icon-touchpad"]
        page.close()
    with PageServer(tv=RemoteTv(paired=False), discover=StubSearch([BOARDROOM])) as server:
        page = tv_page(browser, server)
        assert shown_icons(page) == ["#icon-settings", "#icon-back", "#icon-tv", "#icon-locate", "#icon-touchpad"]    # (the touchpad alone: no TV to point at)
        page.close()
    with PageServer(tv=RemoteTv(refused=True)) as server:
        page = tv_page(browser, server)
        assert shown_icons(page) == ["#icon-settings", "#icon-back", "#icon-x-circle", "#icon-tv", "#icon-touchpad"]
        page.close()
    with PageServer(tv=RemoteTv(on=False, reachable=False, volume=None)) as server:
        page = tv_page(browser, server)
        assert shown_icons(page)[:5] == ["#icon-settings", "#icon-back", "#icon-power", "#icon-x-circle", "#icon-power"]   # Turn on, Forget TV, and Power
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
                page.click("#open-tv-meeting")
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
        page.wait_for_timeout(100)
        forget.click()                                                               # the second tap, within five seconds
        assert tv_calls(page, server, 1) == [("forget",)]
        assert [(m, r, body) for m, r, _, body in seen] == [("POST", "tv/forget", "{}")]
        page.wait_for_selector("#tv-pairing", state="visible", timeout=5000)         # forgotten: the pairing card, and no Forget TV on it
        assert forget.is_hidden() and status_line(page) == "TV at 10.0.0.41 \u00b7 Not paired yet"
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


@pytest.mark.parametrize("tv", [lambda: RemoteTv(paired=False), lambda: None, lambda: RemoteTv(refused=True)], ids=["not paired", "no TV set up", "no longer accepted"])
def test_the_modes_row_under_a_card_has_only_the_full_screen_touchpad_and_it_moves_the_room_pi(browser, tv):
    with PageServer(tv=tv(), mouse=StubMouse()) as server:
        page = tv_page(browser, server)
        assert page.locator("#tv-remote").is_hidden()
        assert [b.is_visible() for b in page.locator("#tv-modes button").all()] == [False, False, True]
        assert page.locator("#tv-fullscreen").inner_text() == "Full-screen touchpad"
        page.click("#tv-fullscreen")
        assert page.locator("#touchpad").is_visible()
        assert page.locator("#touchpad-tv").is_disabled() and page.locator("#touchpad-pi").get_attribute("aria-pressed") == "true"   # the room Pi, and no TV to switch to
        assert page.locator("#touchpad-title").inner_text() == "Moving the mouse on the room's screen"
        assert page.locator("#touchpad-sub").inner_text() == "Pair the TV to move its cursor"
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
        page.click("#open-tv-meeting")
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
        page.click("#open-tv-meeting")
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
            page.click("#open-tv-meeting")
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
        page.click("#open-tv-meeting")
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
                page.click("#open-tv-meeting")
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
