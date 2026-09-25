"""
Browser tests for the room page, driven with the Playwright Chromium in the venv.

The control service runs with stub services on its own event loop in a
thread; the sync Playwright API drives a headless page against it. Skipped
when Playwright or its browser is not installed.
"""

import asyncio
import threading

import pytest

from croom.control.service import ControlService
from tests.unit.control.test_service import StubCalendar, StubMeeting, event

playwright = pytest.importorskip("playwright.sync_api")


class PageServer:
    """ControlService with stubs, served on an ephemeral port from a background thread."""

    def __init__(self, calendar_events=(), room_name="Lab"):
        self.events = list(calendar_events)
        self.room_name = room_name
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
            config={"host": "127.0.0.1", "port": 0, "room_name": self.room_name, "room_location": "2nd floor"},
            meeting=self.meeting, calendar=StubCalendar(events=self.events),
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
    page.click("#link-form button")
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
        assert page.locator("#actions").count() == 0
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
