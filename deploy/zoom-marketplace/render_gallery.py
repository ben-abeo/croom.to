"""
Render the App Gallery screenshots for the Zoom Marketplace listing from the real
Crystal Meet pages, 1200 by 780 each, next to this file:

  gallery-2-room-page.png   the room page: today's bookings, a Zoom meeting about to start
  gallery-3-in-meeting.png  the room page during a Zoom meeting: Mute, Turn camera off, Leave
  gallery-4-tv.png          the TV between meetings (Information style), its 1920 by 1080
                            picture fitted to the gallery's width on a dark background
  gallery-5-door-sign.png   the door sign while the room is in that meeting

The control service runs with the stub meeting, calendar and device services the
browser tests use (tests/unit/control), so no Zoom account or room device is
involved. One clock, fixed to a morning in the rooms' time zone, drives the stubs and
the browser, so every run draws the same day: the room is free at 10:05, the booked
Zoom call is six minutes away at 10:24, someone pastes a Zoom meeting ID at 11:31,
after that call, and the room is twelve minutes into that meeting at 11:43.

The meeting is an ad-hoc one on purpose: while a booking with a link is in progress
the room page marks its row with the class "now", which style.css's
`.now { grid-area: now; }` (meant for the status card) also matches, and the Today
list breaks into columns. Once that rule is scoped to the status card, the meeting
scene can be the booked call itself.

The TV is laid out at 1920 by 1080 and drawn at a device scale factor of 0.625, which
yields the 1200 by 675 picture that downscaling a 1080p screenshot would, with
sharper text and no image library; a second page letterboxes it onto the canvas.

Usage: python deploy/zoom-marketplace/render_gallery.py   (the project's venv, with
Playwright's Chromium installed)
"""

import base64
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]  # this checkout's croom and tests, ahead of any installed copy

ZONE_NAME = "America/Chicago"  # the rooms' time zone (deploy/rooms/room-N.yaml)
os.environ["TZ"] = ZONE_NAME   # the stubs read local time; so does the browser, through its context
time.tzset()

from playwright.sync_api import sync_playwright  # noqa: E402

import croom.calendar.providers.base as calendar_base  # noqa: E402
import croom.control.service as control_service  # noqa: E402
import tests.unit.control.test_service as stubs  # noqa: E402
from croom.calendar.providers.base import MeetingPlatform  # noqa: E402
from tests.unit.control.test_page import PageServer, event, join_by_id, open_page  # noqa: E402
from tests.unit.control.test_tv_page import open_tv, set_style  # noqa: E402

DAY = datetime(2026, 10, 13, tzinfo=ZoneInfo(ZONE_NAME))  # a Tuesday
GALLERY = {"width": 1200, "height": 780}                   # the size the App Listing page demands
TV = {"width": 1920, "height": 1080}
TV_SCALE = GALLERY["width"] / TV["width"]                  # 0.625: 1920 by 1080 becomes 1200 by 675
LETTERBOX = "#000000"


class SceneClock:
    """The one 'now' of the stub services and every browser context."""

    def __init__(self) -> None:
        self.moment = DAY

    def set(self, hour: int, minute: int, second: int = 0) -> None:
        self.moment = DAY.replace(hour=hour, minute=minute, second=second)


CLOCK = SceneClock()


class SceneDatetime(datetime):
    """datetime whose now() reads the scene's clock, patched into the modules that ask for the time."""

    @classmethod
    def now(cls, tz=None):
        return CLOCK.moment.astimezone(tz) if tz is not None else CLOCK.moment.astimezone().replace(tzinfo=None)


for module in (control_service, calendar_base, stubs):
    module.datetime = SceneDatetime


def booking(event_id: str, title: str, hour: int, minute: int, duration: int,
            url=None, platform=MeetingPlatform.UNKNOWN):
    """A booking at a wall-clock time of the scene's day (the tests' event() counts minutes from now)."""
    starts_in = (DAY.replace(hour=hour, minute=minute) - CLOCK.moment).total_seconds() / 60
    return event(event_id, title, starts_in, duration=duration, url=url, platform=platform)


class Scene:
    """Moves the clock for the stubs and for every browser context at once."""

    def __init__(self, browser) -> None:
        self.browser = browser
        self.contexts = []

    def screen(self, **options) -> "Screen":
        context = self.browser.new_context(timezone_id=ZONE_NAME, locale="en-US", reduced_motion="reduce", **options)
        context.clock.set_fixed_time(CLOCK.moment)
        self.contexts.append(context)
        return Screen(context)

    def at(self, hour: int, minute: int, second: int = 0) -> None:
        CLOCK.set(hour, minute, second)
        for context in self.contexts:
            context.clock.set_fixed_time(CLOCK.moment)


class Screen:
    """Stands in for the tests' browser fixture: open_page and open_tv take their pages from here."""

    def __init__(self, context) -> None:
        self.context = context

    def new_page(self, viewport):
        page = self.context.new_page()
        page.set_viewport_size(viewport)
        return page


def clock_shows_now(page) -> None:
    """The TV redraws its clock every ten seconds; wait until it shows the scene's time."""
    page.wait_for_function(
        "document.getElementById('clock').textContent === "
        "new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })", timeout=15000)


def settle(page) -> None:
    page.evaluate("document.fonts.ready.then(() => true)")
    page.wait_for_timeout(300)


def shoot(page, name: str) -> None:
    page.set_viewport_size(GALLERY)
    settle(page)
    page.screenshot(path=str(HERE / name))
    print(f"wrote {name}")


def letterbox(scene: Scene, picture: bytes, name: str) -> None:
    """Centre the TV's picture on a dark 1200 by 780 canvas, pixel for pixel."""
    data = base64.b64encode(picture).decode("ascii")
    page = scene.browser.new_page(viewport=GALLERY, device_scale_factor=1)
    page.set_content(
        f'<body style="margin:0;background:{LETTERBOX}">'
        f'<div style="width:{GALLERY["width"]}px;height:{GALLERY["height"]}px;display:flex;align-items:center">'
        f'<img src="data:image/png;base64,{data}" alt="" style="display:block;width:{GALLERY["width"]}px;'
        f'height:{round(TV["height"] * TV_SCALE)}px"></div></body>')
    page.wait_for_function("document.images[0].complete && document.images[0].naturalWidth > 0")
    page.screenshot(path=str(HERE / name))
    page.close()
    print(f"wrote {name}")


def main() -> None:
    CLOCK.set(10, 5)
    bookings = [
        booking("standup", "Support team stand-up", 9, 0, 15,
                url="https://meet.google.com/abc-defg-hij", platform=MeetingPlatform.GOOGLE_MEET),
        booking("onboarding", "Customer onboarding call", 10, 30, 45,
                url="https://zoom.us/j/98765432100?pwd=abc", platform=MeetingPlatform.ZOOM),
        booking("planning", "Release planning", 14, 0, 60),  # in person: no link
    ]
    with sync_playwright() as p, PageServer(calendar_events=bookings, room_name="Room 1") as server:
        browser = p.chromium.launch()
        scene = Scene(browser)
        table = scene.screen(device_scale_factor=1)
        tv_screen = scene.screen(device_scale_factor=TV_SCALE)

        room = open_page(table, server)                  # 10:05, the room is free
        tv = open_tv(tv_screen, server, **TV)
        set_style(tv, server, "info")

        scene.at(10, 24)                                 # six minutes before the Zoom booking
        room.wait_for_function("document.body.dataset.state === 'soon' && document.querySelector('#events button')",
                               timeout=8000)
        tv.wait_for_function("document.body.dataset.state === 'soon'", timeout=8000)
        clock_shows_now(tv)
        shoot(room, "gallery-2-room-page.png")
        settle(tv)
        letterbox(scene, tv.screenshot(), "gallery-4-tv.png")

        scene.at(11, 31)                                 # after the call, someone pastes a Zoom meeting ID
        join_by_id(room)
        scene.at(11, 43, 27)                             # twelve minutes into that meeting
        room.wait_for_function("document.getElementById('detail').textContent.endsWith('12:27')", timeout=8000)
        shoot(room, "gallery-3-in-meeting.png")

        sign = table.new_page(GALLERY)
        sign.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        sign.wait_for_function("document.body.dataset.state === 'occupied'", timeout=8000)
        shoot(sign, "gallery-5-door-sign.png")
        browser.close()


if __name__ == "__main__":
    main()
