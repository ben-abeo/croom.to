"""
Magnifying what the TV shows in a meeting (croom.meeting.magnify; Ben, 2026-11-22), against pages laid out as a meeting's are: one that
fills the window and does not scroll, with the shared screen in the middle and the platform's bar fixed to the bottom. The pages are the
test's own, set in its own browser: nothing is loaded from anywhere.
"""

import struct
import zlib

import pytest

playwright = pytest.importorskip("playwright.async_api")

from croom.meeting.magnify import MAGNIFY, MAX_ZOOM, REFUSAL, Magnifier  # noqa: E402
from croom.meeting.providers.base import MeetingControlError  # noqa: E402

WIDTH, HEIGHT = 1280, 720

# A meeting as Zoom's client and Meet draw one: everything fixed to the window. `rooted` gives the page a root as tall as the window (as
# both platforms' pages have); without it the root has no height at all, since nothing in it is in the flow.
PAGE = """<!DOCTYPE html><html><head><style>
%s
body { margin: 0; background: #111; }
#app { position: fixed; inset: 0; }
#share { position: absolute; left: 240px; top: 110px; width: 800px; height: 450px; background: #2a6; }
#bar { position: fixed; left: 0; right: 0; bottom: 0; height: 60px; background: #333; }
</style></head><body><div id="app"><div id="share"></div></div><div id="bar"></div></body></html>"""
ROOTED = PAGE % "html, body { height: 100%; }"
ROOTLESS = PAGE % ""


@pytest.fixture
async def page():
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        yield page
        await browser.close()


async def box(page, selector):
    """Where an element is on the screen once the page has finished moving: (left, top, width, height), rounded."""
    await page.wait_for_timeout(250)        # the move takes 120 ms
    rect = await page.evaluate("(s) => { const r = document.querySelector(s).getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; }", selector)
    return tuple(round(value) for value in rect)


# --- what the table may ask for


def test_the_whole_picture_is_where_a_meeting_starts():
    assert Magnifier().state() == {"available": True, "zoom": 1.0, "x": 0.5, "y": 0.5, "max": MAX_ZOOM}
    assert MAX_ZOOM == 4.0


def test_what_is_asked_for_is_kept_within_bounds_and_what_is_left_out_stays_as_it_is():
    magnifier = Magnifier()
    assert magnifier.wanted({"zoom": 2, "x": 0.25, "y": 0.75}) == (2.0, 0.25, 0.75)
    assert magnifier.wanted({"zoom": 9}) == (MAX_ZOOM, 0.5, 0.5)
    assert magnifier.wanted({"zoom": 0.2, "x": -3, "y": 7}) == (1.0, 0.0, 1.0)
    magnifier.zoom, magnifier.x, magnifier.y = 3.0, 0.4, 0.6
    assert magnifier.wanted({"x": 0.1}) == (3.0, 0.1, 0.6)               # a move keeps the zoom
    assert magnifier.wanted({"zoom": 1.5}) == (1.5, 0.4, 0.6)            # and a zoom keeps the place


@pytest.mark.parametrize("value", [None, {}, [], "2", 2, {"zoom": "2"}, {"zoom": True}, {"zoom": None}, {"x": float("nan")}, {"y": float("inf")},
                                   {"zoom": 2, "scale": 3}, {"zoom": [2]}])
def test_anything_else_is_refused_in_words(value):
    with pytest.raises(MeetingControlError, match="Send the zoom"):
        Magnifier().wanted(value)
    assert "1 to 4" in REFUSAL


def test_a_meeting_that_ends_leaves_the_next_one_whole():
    magnifier = Magnifier()
    magnifier.zoom, magnifier.x, magnifier.y = 3.0, 0.2, 0.9
    magnifier.reset()
    assert magnifier.state() == Magnifier().state()


# --- what it does to the TV's page


async def test_the_picture_doubles_about_its_middle(page):
    await page.set_content(ROOTED)
    magnifier = Magnifier()
    assert await box(page, "#share") == (240, 110, 800, 450)
    state = await magnifier.set(page, {"zoom": 2, "x": 0.5, "y": 0.5})
    assert state == {"available": True, "zoom": 2.0, "x": 0.5, "y": 0.5, "max": MAX_ZOOM}
    # Twice the size, and the middle of the picture (640, 360) is still the middle of the screen: 640 + 2 × (240 − 640) = −160
    assert await box(page, "#share") == (-160, -140, 1600, 900)


async def test_the_table_moves_the_point_that_is_in_the_middle(page):
    await page.set_content(ROOTED)
    magnifier = Magnifier()
    await magnifier.set(page, {"zoom": 2, "x": 0.5, "y": 0.5})
    await magnifier.set(page, {"x": 0.3, "y": 0.6})
    assert (magnifier.zoom, magnifier.x, magnifier.y) == (2.0, 0.3, 0.6)
    # The point at 30% across and 60% down, (384, 432), is at the middle of the screen: left = 640 + 2 × (240 − 384)
    assert await box(page, "#share") == (352, -284, 1600, 900)


async def test_the_screen_never_shows_past_the_pictures_edges(page):
    await page.set_content(ROOTED)
    magnifier = Magnifier()
    state = await magnifier.set(page, {"zoom": 2, "x": 0, "y": 0})
    assert (state["x"], state["y"]) == (0.25, 0.25)                     # as near the corner as the picture goes: the table is told where it is
    assert await box(page, "#share") == (480, 220, 1600, 900)           # the picture's top left corner is the screen's
    assert await box(page, "#app") == (0, 0, WIDTH * 2, HEIGHT * 2)
    state = await magnifier.set(page, {"x": 1, "y": 1})
    assert (state["x"], state["y"]) == (0.75, 0.75)
    assert await box(page, "#app") == (-WIDTH, -HEIGHT, WIDTH * 2, HEIGHT * 2)
    state = await magnifier.set(page, {"zoom": 4, "x": 1, "y": 0})
    assert (state["zoom"], state["x"], state["y"]) == (4.0, 0.875, 0.125)
    assert await box(page, "#app") == (-WIDTH * 3, 0, WIDTH * 4, HEIGHT * 4)


async def test_back_at_one_the_page_is_exactly_as_it_was(page):
    await page.set_content(ROOTED)
    await page.evaluate("document.documentElement.style.overflow = 'clip'")      # a style of the page's own on its root
    before = await page.evaluate("document.documentElement.getAttribute('style')")
    magnifier = Magnifier()
    await magnifier.set(page, {"zoom": 3, "x": 0.2, "y": 0.2})
    assert await page.evaluate("document.documentElement.style.overflow") == "hidden"
    state = await magnifier.set(page, {"zoom": 1})
    assert state == Magnifier().state()                                           # whole again, and in the middle for the next time
    assert await page.evaluate("document.documentElement.getAttribute('style')") == before
    assert await box(page, "#share") == (240, 110, 800, 450)
    await magnifier.set(page, {"zoom": 1})                                        # asked again: nothing to put back, and nothing breaks
    assert await page.evaluate("document.documentElement.getAttribute('style')") == before


async def test_the_page_gets_no_scroll_bars_and_does_not_scroll(page):
    await page.set_content(ROOTED)
    await Magnifier().set(page, {"zoom": 3, "x": 0.9, "y": 0.9})
    await page.wait_for_timeout(250)
    assert await page.evaluate("[window.scrollX, window.scrollY, window.innerWidth, document.documentElement.clientWidth]") == [0, 0, WIDTH, WIDTH]
    await page.mouse.wheel(300, 300)
    await page.wait_for_timeout(100)
    assert await page.evaluate("[window.scrollX, window.scrollY]") == [0, 0]


@pytest.mark.parametrize("html", [ROOTED, ROOTLESS])
async def test_what_the_platform_fixed_to_the_window_keeps_its_layout(page, html):
    """A transform on the root makes the root the box its fixed children are laid out in. A page whose root has no height of its own
    (everything in it is fixed) would lose its whole picture: the root is kept the size of the window while it is magnified."""
    await page.set_content(html)
    layout = "['#app', '#bar'].map((s) => { const e = document.querySelector(s); return [e.offsetLeft, e.offsetTop, e.offsetWidth, e.offsetHeight]; })"
    before = await page.evaluate(layout)
    assert before == [[0, 0, WIDTH, HEIGHT], [0, HEIGHT - 60, WIDTH, 60]]
    await Magnifier().set(page, {"zoom": 2, "x": 0.5, "y": 1})
    assert await page.evaluate(layout) == before                                  # laid out as before: only drawn larger
    assert await box(page, "#bar") == (-WIDTH // 2, HEIGHT - 120, WIDTH * 2, 120)   # the bar, twice as tall, at the bottom of the screen


async def test_what_the_tv_shows_is_the_part_asked_for(page):
    """By the picture itself: at 2 about its middle the shared screen (green) fills the TV from corner to corner, where the page's dark
    surround was; back at 1 the surround is there again."""
    await page.set_content(ROOTED)

    async def colour(x, y):
        """The colour of one point of the screen, from a screenshot of that point alone (a PNG of one pixel: its filter byte, then red, green, blue)."""
        png = await page.screenshot(clip={"x": x, "y": y, "width": 1, "height": 1}, type="png")
        at, packed = 8, b""
        while at < len(png):
            length, kind = struct.unpack(">I4s", png[at:at + 8])
            if kind == b"IDAT":
                packed += png[at + 8:at + 8 + length]
            at += 12 + length
        return tuple(zlib.decompress(packed)[1:4])

    corners = ((5, 5), (WIDTH - 5, 5), (5, HEIGHT - 5), (WIDTH - 5, HEIGHT - 5), (WIDTH // 2, HEIGHT // 2))
    green, dark = (0x22, 0xAA, 0x66), (0x11, 0x11, 0x11)
    assert [await colour(x, y) for x, y in corners[:2]] == [dark, dark]
    magnifier = Magnifier()
    await magnifier.set(page, {"zoom": 2, "x": 0.5, "y": 0.5})          # the part of the page from (320, 180) to (960, 540): all of it the shared screen
    await page.wait_for_timeout(250)
    assert [await colour(x, y) for x, y in corners] == [green] * 5
    await magnifier.set(page, {"zoom": 1})
    await page.wait_for_timeout(250)
    assert [await colour(x, y) for x, y in corners[:2]] == [dark, dark]


async def test_a_page_that_cannot_be_reached_is_a_refusal_in_words_and_nothing_is_kept():
    class Gone:
        async def evaluate(self, script, arg=None):
            raise RuntimeError("Target page, context or browser has been closed\nCall log: ...")

    magnifier = Magnifier()
    with pytest.raises(MeetingControlError, match="^Could not magnify the TV: Target page, context or browser has been closed$"):
        await magnifier.set(Gone(), {"zoom": 2})
    assert magnifier.state() == Magnifier().state()


def test_the_page_is_given_one_script_that_names_no_address():
    assert "http" not in MAGNIFY and "documentElement" in MAGNIFY
