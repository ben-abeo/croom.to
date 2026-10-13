"""
Browser tests for the room page, driven with the Playwright Chromium in the venv.

The control service runs with stub services on its own event loop in a
thread; the sync Playwright API drives a headless page against it. Skipped
when Playwright or its browser is not installed.
"""

import asyncio
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
from tests.unit.control.test_service import StubCalendar, StubDevices, StubMeeting, event, skip_if_day_ends_within

playwright = pytest.importorskip("playwright.sync_api")


class PageServer:
    """ControlService with stubs, served on an ephemeral port from a background thread."""

    def __init__(self, calendar_events=(), room_name="Lab", room_location="2nd floor", calendar_connected=True, port=0, devices=None):
        self.events = list(calendar_events)
        self.room_name = room_name
        self.room_location = room_location
        self.calendar_connected = calendar_connected
        self.port_arg = port
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
        service = ControlService(
            config={"host": "127.0.0.1", "port": self.port_arg, "room_name": self.room_name, "room_location": self.room_location,
                    "settings_file": str(Path(self._settings_dir.name) / "control-settings.json")},
            meeting=self.meeting, calendar=StubCalendar(events=self.events, connected=self.calendar_connected),
            devices=self.devices,
        )
        await service.start()
        self.port = service.bound_port
        self._stop = asyncio.Event()
        self._ready.set()
        await self._stop.wait()
        await service.stop()

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


# --- the page's own keyboard (the table Pi's kiosk browser has no keyboard of its own) ---

def open_kiosk_page(browser, server, width=1280, height=800):
    page = browser.new_page(viewport={"width": width, "height": height})
    page.goto(f"http://127.0.0.1:{server.port}/?keyboard=1", wait_until="networkidle")
    page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
    return page


def tap(page, *keys):
    for key in keys:
        page.click(f"#keyboard button[data-key='{key}']")


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
        tap(page, "symbols", *"9876?", "letters", *"pwd", "symbols", "=", "letters", "a", "shift", "b", "symbols", "1")
        assert page.input_value("#link-input") == "zoom.us/j/9876?pwd=aB1"
        tap(page, "backspace", "backspace")
        assert page.input_value("#link-input") == "zoom.us/j/9876?pwd=a"
        page.close()


def test_the_page_keyboard_joins_and_hides(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        tap(page, "symbols", *"98765432100", "join")
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
        tap(page, "symbols", *"98765432100")
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
        tap(page, "symbols", *"5590000559", "join")
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
        tap(page, *"zoom.us/j/", "symbols", *"5590000559", "letters")           # the link, with the keys left on the letters
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
        tap(page, "symbols", *"5590000559")                                     # and is left on the digits
        page.click("#passcode-input")
        tap(page, "letters", "a", "symbols", "1", "letters")                    # the passcode is left on the letters
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
        tap(page, "symbols", *"5590000559")
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
        tap(page, "symbols", *"5590000559")
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
        tap(page, "symbols", *"5590000559")
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
        tap(page, "symbols", *"5590000559")
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
        tap(page, "symbols", *"5590000559")
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
        tap(page, "symbols", *"5590000559", "letters", "join")                  # the keys go away with Join, left on the letters
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
    tap(page, "symbols", *"5590000559", "join")
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
        tap(page, "symbols", *"5590000559", "join")                             # the keys' Join
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
        tap(page, "symbols", *"5590000559")
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
        tap(page, "symbols", *"5590000559")
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


def page_on_a_stopped_clock(browser, server, width=1280, height=800):
    """The room page, loaded, with its timers stopped: from here they run only when the test calls page.clock.run_for."""
    page = browser.new_page(viewport={"width": width, "height": height})
    page.clock.install()
    page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
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
