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
    """ControlService with stubs, served on an ephemeral port from a background thread. The meeting it joins is a Zoom participant's (role
    "co-host" or "host" for the others) with the whole control model; controls=False is a meeting with none, as a platform that has no
    model yet would give."""

    def __init__(self, calendar_events=(), room_name="Lab", room_location="2nd floor", calendar_connected=True, port=0, devices=None,
                 role="participant", controls=True):
        self.role = role
        self.controls = controls
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
        self.meeting = StubMeeting(role=self.role, controls=self.controls)
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


def texts(page, scope):
    """What each button inside the scope says, in order."""
    return [b.inner_text().strip() for b in page.locator(f"{scope} .button").all()]


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
    secondary = ["Captions on", "Gallery view", "Hide self view", "Participants (3)", "Chat", "Host"]
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
        assert_includes(shown_icons(page), bar + ["#icon-back", "#icon-move", "#icon-monitor", "#icon-volume", "#icon-volume-x"])   # the last two are Key click on and off
        settled(page)
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
        assert_includes(shown_icons(page), ["#icon-mic", "#icon-video", "#icon-phone-off", "#icon-volume", "#icon-volume", "#icon-volume", "#icon-move", "#icon-calendar"])
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
        assert page.locator("#meeting-secondary .button").all_inner_texts() == ["Captions on", "Gallery view", "Hide self view", "Participants (3)", "Chat"]
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


# --- the meeting screen, drawn from the control model (spec 2026-10-13, sections 3 and 6.1) ---

LONG_TITLE = "Quarterly planning review with the regional leadership team and the product owners of every office"   # 98 characters: as long as a booking's title gets


def test_the_meeting_screen_offers_every_available_control_from_the_model(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        assert texts(page, "#meeting-actions") == ["Mute", "Turn camera off", "Raise hand", "Reactions", "Leave"]
        assert texts(page, "#meeting-secondary") == ["Captions on", "Gallery view", "Hide self view", "Participants (3)", "Chat"]   # a participant: no Host
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


@pytest.mark.parametrize("width, height", [(1280, 800), (1920, 1200)])
def test_the_meeting_screen_with_everything_still_fits_and_hides_the_unavailable(browser, width, height):
    """The fixed screen (spec 2026-10-12): the host's whole screen, with the longest words every button can have, the notice, a waiting
    dialog's card, a 98-character title, the reaction row in place of the secondary row, and room beside the six for the TV's button."""
    skip_if_day_ends_within(1)                                               # the booking starts in 5 minutes, and the service lists today's only
    with PageServer(role="host", calendar_events=[event("e1", LONG_TITLE, 5)]) as server:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        page.click("#actions button:has-text('Join now')")                   # this booking, so the call carries its long title
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        assert page.locator("#meeting-detail").inner_text() == f"{LONG_TITLE}, Zoom"
        meeting = server.meeting.current_meeting
        meeting.is_muted = True                                              # Unmute, Captions off, Participants (120), Chat (12): the widest of each
        meeting.controls["captions"]["on"] = True
        meeting.people = {"count": 120, "list": []}
        meeting.chat = {"unread": 12, "messages": []}
        meeting.notice = "This meeting is being recorded and transcribed."
        meeting.dialog = {"id": 1, "title": "Unmute?", "text": "The host would like you to unmute.", "buttons": ["Unmute", "Stay muted"]}
        page.wait_for_function("document.querySelector('#meeting-dialog:not([hidden])')", timeout=5000)
        assert texts(page, "#meeting-actions") == ["Unmute", "Turn camera off", "Raise hand", "Reactions", "Leave"]
        assert texts(page, "#meeting-secondary") == ["Captions off", "Gallery view", "Hide self view", "Participants (120)", "Chat (12)", "Host"]
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
        # The TV's button joins the six (the TV branch's, when it lands): one more of the table's own, in the same row.
        page.evaluate("document.getElementById('meeting-secondary').append(window.croomPage.button('TV controls', 'quiet', () => {}, false, 'tv'))")
        assert fits(page), "the meeting screen scrolls with a seventh secondary button"
        # What is not available is not drawn, and the host's screen still fits without it.
        meeting.controls["raise_hand"] = {"available": False}
        page.wait_for_function("document.querySelectorAll('#meeting-actions .button').length === 4", timeout=5000)
        assert texts(page, "#meeting-actions")[:3] == ["Unmute", "Turn camera off", "Reactions"]
        assert "Host" in texts(page, "#meeting-secondary")
        assert page.locator("#meeting-secondary .button", has_text="Chat (12)").count() == 1
        assert fits(page)
        page.close()


def test_a_status_without_controls_keeps_the_three_buttons(browser):
    with PageServer(controls=False) as server:
        page = open_page(browser, server)
        join_by_id(page)
        assert texts(page, "#meeting-actions") == ["Mute", "Turn camera off", "Leave"]
        assert drawn_icons(page, "#meeting-actions") == ["Mute: #icon-mic", "Turn camera off: #icon-video", "Leave: #icon-phone-off"]
        assert page.locator("#meeting-secondary .button").count() == 0       # no model: no Participants, no Chat, nothing drawn on a guess
        assert page.locator("#meeting-secondary").is_hidden()                  # and no empty row's room between the buttons and the Sound strip
        assert fits(page)
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
        meeting.people = {"count": 12, "list": []}
        meeting.chat = {"unread": 2, "messages": []}
        page.wait_for_function("document.getElementById('meeting-secondary').textContent.includes('Chat (2)')", timeout=5000)
        assert texts(page, "#meeting-secondary") == ["Speaker view", "Show self view", "Participants (12)", "Chat (2)"]
        meeting.controls["host"]["role"] = "co-host"
        page.wait_for_function("document.getElementById('meeting-secondary').textContent.includes('Host')", timeout=5000)
        assert texts(page, "#meeting-secondary")[-1] == "Host"
        meeting.controls["host"]["role"] = "participant"
        meeting.controls["raise_hand"]["on"] = True
        meeting.controls["reactions"] = {"available": False, "choices": ["Clap"]}                       # the client has no Reactions button: the configured names do not make one
        meeting.people = {"count": 0, "list": []}
        page.wait_for_function("document.getElementById('meeting-actions').textContent.includes('Lower hand')", timeout=5000)
        assert texts(page, "#meeting-actions") == ["Mute", "Turn camera off", "Lower hand", "Leave"]
        assert texts(page, "#meeting-secondary") == ["Speaker view", "Show self view", "Chat (2)"]      # no one listed: no Participants button
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
        assert page.locator("#meeting-notice").inner_text() == "This meeting is being recorded." and page.locator("#meeting-secondary .button").count() == 5
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
        page.unroute("**/api/status")                                                    # the held polls go on their way
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
