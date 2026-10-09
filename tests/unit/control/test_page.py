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
from tests.unit.control.test_service import StubCalendar, StubDevices, StubMeeting, event

playwright = pytest.importorskip("playwright.sync_api")


class PageServer:
    """ControlService with stubs, served on an ephemeral port from a background thread."""

    def __init__(self, calendar_events=(), room_name="Lab", calendar_connected=True, port=0, devices=None):
        self.events = list(calendar_events)
        self.room_name = room_name
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
            config={"host": "127.0.0.1", "port": self.port_arg, "room_name": self.room_name, "room_location": "2nd floor",
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
        page.click("#camera-panel summary")
        hold(page, "#arrow-pad button[data-pan='1'][data-tilt='0']", ms=900)
        page.wait_for_timeout(300)
        moves = server.devices.camera.moves
        assert moves[0] == (1, 0) and moves[-1] == (0, 0)
        assert moves.count((1, 0)) >= 2            # re-sent while held
        page.close()


def test_zoom_home_and_presets(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.click("#camera-panel summary")
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
        page.click("#camera-panel summary")
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
        assert page.locator("#sound-panel").is_hidden() and page.locator("#sound-note").inner_text() == "No speaker found"
        assert page.locator("#camera-panel").is_hidden() and page.locator("#camera-note").inner_text() == "No controllable camera found"
        page.close()


def test_opening_the_camera_panel_while_idle_asks_for_the_preview(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.click("#camera-panel summary")
        page.wait_for_timeout(300)
        assert server.devices.camera.previews == [True]
        page.click("#camera-panel summary")
        page.wait_for_timeout(300)
        assert server.devices.camera.previews == [True, False]
        page.close()


def test_the_panels_fit_a_phone_width(browser):
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        page.click("#camera-panel summary")
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
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
        page.click("#camera-panel summary")
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
        page.click("#camera-panel summary")
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
        page.click("#camera-panel summary")
        page.click("#preset-save-mode")
        page.click("#preset-row button[data-slot='3']")
        page.wait_for_function("document.getElementById('camera-note').innerText.includes('File name too long')", timeout=5000)
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.close()


def test_a_refusal_comes_into_view_beside_the_controls_that_caused_it(browser):
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 1024, "height": 600})      # the table's touch screen
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        page.click("#camera-panel summary")
        page.evaluate("document.getElementById('preset-row').scrollIntoView({block: 'center'})")
        assert page.locator("#camera-panel summary").bounding_box()["y"] < 0   # the panel's top, where the note will appear, is above the fold
        page.click("#preset-row button[data-slot='3']")                       # nothing is saved there
        page.wait_for_function("document.getElementById('camera-note').innerText !== ''", timeout=5000)
        note, preset = page.locator("#camera-note").bounding_box(), page.locator("#preset-row button[data-slot='3']").bounding_box()
        assert note["y"] >= 0 and preset["y"] + preset["height"] <= 600       # the reason is on screen, and so is the button
        page.close()


def test_a_failed_save_as_home_keeps_set_up_open(browser):
    with PageServer(devices=StubDevices(camera=UnwritableCamera())) as server:
        page = open_page(browser, server)
        page.click("#camera-panel summary")
        page.click("#setup-link")
        page.click("#save-home")
        page.wait_for_function("document.getElementById('camera-note').innerText.includes('disk full')", timeout=5000)
        assert page.locator("#camera-setup").is_visible()          # closed only by a home that really was saved
        page.close()


def test_a_refused_volume_change_shows_its_reason_and_the_slider_goes_back(browser):
    with PageServer(devices=StubDevices(volume=BusySpeaker())) as server:
        page = open_page(browser, server)
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
        page.click("#camera-panel summary")
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
        page.click("#camera-panel summary")
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
        snapshot = page.evaluate("fetch('/api/status').then((r) => r.text())")
        page.route("**/api/status", lambda route: route.fulfill(body=snapshot, content_type="application/json"))   # polls can no longer help
        page.click("#louder")
        page.wait_for_function("document.getElementById('volume-level').innerText === '45'", timeout=1000)
        page.close()


def test_a_poll_already_on_its_way_does_not_undo_the_answer(browser):
    with PageServer() as server:
        page = open_page(browser, server)
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
        page.click("#camera-panel summary")
        page.evaluate("window.__keep = [document.querySelector('#preset-row button'), document.getElementById('zoom-in'), document.getElementById('louder')]")
        page.wait_for_timeout(2600)                                # a whole poll and more
        assert page.evaluate("""document.querySelector('#preset-row button') === window.__keep[0]
            && document.getElementById('zoom-in') === window.__keep[1] && document.getElementById('louder') === window.__keep[2]""")
        page.close()


def test_holding_a_zoom_button_repeats_until_it_is_released(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.click("#camera-panel summary")
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
        page.click("#camera-panel summary")
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
        page.click("#camera-panel summary")
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
        page.click("#camera-panel summary")
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
        page.click("#camera-panel summary")
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
        assert page.locator("#sound-panel").is_hidden() and page.locator("#sound-note").inner_text() == "No speaker found"
        assert page.locator("#camera-panel").is_hidden() and page.locator("#camera-note").inner_text() == "No controllable camera found"
        assert errors == []
        page.close()


def test_save_and_the_set_up_link_wait_for_a_saved_home(browser):
    with PageServer(devices=StubDevices(camera=StubCamera(home_saved=False, position_known=False, saved=()))) as server:
        page = open_page(browser, server)
        page.click("#camera-panel summary")
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
        page.click("#camera-panel summary")
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
        assert asked == [True, True]                               # nothing asked for while the TV is in a meeting
        page.close()


def test_a_panel_opened_in_a_meeting_asks_for_the_preview_once_the_room_is_idle(browser):
    with PageServer() as server:
        base = f"http://127.0.0.1:{server.port}"
        page = page_on_a_stopped_clock(browser, server)
        asked = preview_requests(page)
        page.request.post(base + "/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}',
                          headers={"Content-Type": "application/json"})
        run_until_the_page_shows(page, "meeting")
        page.click("#camera-panel summary")
        page.wait_for_timeout(300)
        assert asked == []                                         # the TV shows the meeting: no preview to ask for
        page.request.post(base + "/api/meeting/leave", data="{}", headers={"Content-Type": "application/json"})
        run_until_the_page_shows(page, "free")
        page.clock.run_for(30000)
        page.wait_for_timeout(300)
        assert asked == [True]                                     # the panel is still open: the next renewal asks
        page.close()


def test_a_preview_renewal_that_works_does_not_clear_the_users_last_refusal(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server, width=1024, height=600)
        page.click("#camera-panel summary")
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
        page.click("#camera-panel summary")
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
        page.click("#camera-panel summary")
        page.click("#preset-row button[data-slot='3']")
        page.wait_for_function("document.getElementById('camera-note').innerText === 'nothing saved in this slot'", timeout=5000)
        page.evaluate("document.getElementById('preset-row').scrollIntoView({block: 'center'})")
        assert page.locator("#camera-note").bounding_box()["y"] < 0           # the user's note is above the fold
        scrolled_to = page.evaluate("window.scrollY")
        asked = preview_requests(page)
        camera.refusing = True                                                # a meeting has just started: the page's status is behind
        page.clock.run_for(30000)
        page.wait_for_timeout(300)
        assert asked == [True] and camera.previews == [True]                  # the renewal went out and was refused
        assert page.locator("#camera-note").inner_text() == "nothing saved in this slot"
        assert page.evaluate("window.scrollY") == scrolled_to
        page.close()


def test_a_zoom_hold_does_not_hold_back_the_speakers_status(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.click("#camera-panel summary")
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


def test_closing_the_page_with_the_camera_panel_open_turns_the_preview_off(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.click("#camera-panel summary")
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
