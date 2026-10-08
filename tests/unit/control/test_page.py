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
from pathlib import Path

import pytest

from croom.control.service import ControlService
from tests.unit.control.test_service import StubCalendar, StubMeeting, event

playwright = pytest.importorskip("playwright.sync_api")


class PageServer:
    """ControlService with stubs, served on an ephemeral port from a background thread."""

    def __init__(self, calendar_events=(), room_name="Lab", calendar_connected=True, port=0):
        self.events = list(calendar_events)
        self.room_name = room_name
        self.calendar_connected = calendar_connected
        self.port_arg = port
        self._settings_dir = tempfile.TemporaryDirectory()   # the screensaver choice must not land in the repo
        self.meeting = None
        self.port = None
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self):
        self.meeting = StubMeeting()
        service = ControlService(
            config={"host": "127.0.0.1", "port": self.port_arg, "room_name": self.room_name, "room_location": "2nd floor",
                    "settings_file": str(Path(self._settings_dir.name) / "control-settings.json")},
            meeting=self.meeting, calendar=StubCalendar(events=self.events, connected=self.calendar_connected),
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


def test_join_by_id_mute_and_leave_flow(browser):
    with PageServer(calendar_events=[event("e1", "Design review", 25)]) as server:
        page = open_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        assert page.locator("#headline").inner_text().startswith("Free until")
        join_by_id(page)
        assert page.locator("#headline").inner_text() == "In a meeting"
        assert page.locator(".link").is_hidden()
        page.click("#actions button:has-text('Mute')")
        page.wait_for_selector("#actions button:has-text('Unmute')", timeout=5000)
        page.click("#actions button:has-text('Leave')")
        page.click("#actions button:has-text('Tap again to leave')")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert server.meeting.leaves == 1
        assert errors == []
        page.close()


def test_in_meeting_controls_are_not_rebuilt_while_the_timer_ticks(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.evaluate("window.__firstButton = document.querySelector('#actions button')")
        before = page.locator("#detail").inner_text()
        page.wait_for_timeout(2600)
        assert page.evaluate("document.querySelector('#actions button') === window.__firstButton"), \
            "the action buttons were replaced while nothing about them changed"
        assert page.locator("#detail").inner_text() != before, "the elapsed time should keep ticking"
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
    with PageServer(calendar_events=[event("b1", "Board lunch", -5, duration=20, url=None),
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
