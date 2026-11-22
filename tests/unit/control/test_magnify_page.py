"""Magnify on the room page (Ben, 2026-11-22): in a meeting, the TV's picture made larger from the table, to read a shared screen. The button is in
the top bar beside the Touchpad and the TV, while the meeting's page can be magnified (controls.magnify); its screen is a pad that stands for the
whole picture, with a box for the part the TV shows, and the zoom's buttons. The stub meeting keeps the bounds the provider and the TV's page keep.

And the Mute and Camera tiles of the meeting screen, which must show a muted microphone and a camera that is off at a glance."""
import asyncio

import pytest

from croom.meeting.providers.base import MeetingControlError
from tests.unit.control.test_page import PageServer, browser, fits, join_by_id, open_page  # noqa: F401  (browser is a fixture)
from tests.unit.control.test_service import event, skip_if_day_ends_within

pytest.importorskip("playwright.sync_api")

WHOLE = {"available": True, "zoom": 1.0, "x": 0.5, "y": 0.5, "max": 4.0}


def view(page):
    return page.evaluate("document.body.dataset.view")


def open_magnify(page):
    """Open the screen, and wait out the half second in which the page takes no tap after a screen changes under a finger (settle, app.js)."""
    page.click("#open-magnify")
    page.wait_for_function("document.body.dataset.view === 'magnify' && !document.body.classList.contains('settling')", timeout=5000)


def magnified(server):
    """What the TV is magnified to, as the meeting has it: (zoom, x, y) to two places."""
    state = server.meeting.current_meeting.controls["magnify"]
    return round(state["zoom"], 2), round(state["x"], 2), round(state["y"], 2)


def asked(server):
    """What the page asked the meeting for, in order: each "magnify" control's value."""
    return [value for name, value in server.meeting.controls_called if name == "magnify"]


def the_box(page):
    """The box on the pad, as fractions of the pad inside its border: (left, top, width, height) to two places."""
    return tuple(page.evaluate("""() => { const pad = document.getElementById('magnify-pad'), p = pad.getBoundingClientRect(),
        b = document.getElementById('magnify-window').getBoundingClientRect(), w = pad.clientWidth, h = pad.clientHeight;
        return [(b.left - p.left - pad.clientLeft) / w, (b.top - p.top - pad.clientTop) / h, b.width / w, b.height / h].map((n) => Math.round(n * 100) / 100); }"""))


def on_the_pad(page, x, y):
    """The point of the screen that is (x, y) of the way across and down the pad, inside its border."""
    left, top, width, height = page.evaluate("""() => { const pad = document.getElementById('magnify-pad'), p = pad.getBoundingClientRect();
        return [p.left + pad.clientLeft, p.top + pad.clientTop, pad.clientWidth, pad.clientHeight]; }""")
    return left + width * x, top + height * y


def settled(page, server, wanted):
    """Wait until the meeting has what the page asked for and the page has drawn the answer."""
    page.wait_for_function("(zoom) => document.getElementById('magnify-pad').dataset.zoom === String(zoom)", arg=wanted[0], timeout=5000)
    end = 50
    while magnified(server) != wanted and end:
        page.wait_for_timeout(50)
        end -= 1
    assert magnified(server) == wanted
    page.wait_for_timeout(150)


# --- the button in the top bar


def test_magnify_is_in_the_top_bar_in_a_meeting_before_the_touchpad_and_the_tv(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        assert page.locator("#open-magnify").is_hidden()                       # nothing to magnify outside a meeting
        join_by_id(page)
        page.wait_for_function("!document.getElementById('open-magnify').hidden", timeout=5000)
        button = page.locator("#open-magnify")
        assert button.inner_text().strip() == "Magnify" and button.locator("svg use").get_attribute("href") == "#icon-zoom-in"
        boxes = [page.locator(s).bounding_box() for s in ("#open-magnify", "#open-touchpad", "#open-tv-remote")]
        assert len({round(b["y"]) for b in boxes}) == 1                        # one row
        assert boxes[0]["x"] + boxes[0]["width"] <= boxes[1]["x"] and boxes[1]["x"] + boxes[1]["width"] <= boxes[2]["x"]   # the TV's stays last
        assert "current" not in (button.get_attribute("class") or "")
        assert fits(page)
        page.close()


def test_a_meeting_whose_page_cannot_be_magnified_has_no_button(browser):  # noqa: F811
    """A platform with no control model, or one whose provider does not offer the control: nothing to open."""
    with PageServer(controls=False) as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("!document.getElementById('open-touchpad').hidden", timeout=5000)
        assert page.locator("#open-magnify").is_hidden()
        page.close()
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("!document.getElementById('open-magnify').hidden", timeout=5000)
        server.meeting.current_meeting.controls["magnify"] = dict(WHOLE, available=False)
        page.wait_for_function("document.getElementById('open-magnify').hidden", timeout=5000)
        page.close()


def test_the_button_says_how_much_the_tv_is_magnified_and_is_filled_while_it_is(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_magnify(page)
        page.click("#magnify-presets .button[data-zoom='3']")
        page.wait_for_function("document.getElementById('open-magnify').textContent.trim() === 'Magnify 3×'", timeout=5000)
        assert "current" in page.locator("#open-magnify").get_attribute("class")
        assert page.evaluate("getComputedStyle(document.getElementById('open-magnify')).backgroundColor") == "rgb(27, 82, 229)"
        page.click("#back-from-magnify")
        assert view(page) == "meeting"
        assert page.locator("#open-magnify").inner_text().strip() == "Magnify 3×"      # on the meeting screen too: the TV is still magnified
        open_magnify(page)
        page.click("#magnify-reset")
        page.wait_for_function("document.getElementById('open-magnify').textContent.trim() === 'Magnify'", timeout=5000)
        assert "current" not in page.locator("#open-magnify").get_attribute("class")
        page.close()


def test_beside_the_next_bookings_warning_the_button_is_its_picture_alone_and_the_kiosks_bar_keeps_its_height(browser):  # noqa: F811
    """The kiosk's bar is one row, and the meeting screen under it is fixed: a bar that grew would push it off the screen. With the warning of
    the next booking in the bar there is no room for the word, so the button is its picture alone, named for a screen reader, and the warning
    is the narrower by it: the room's name and place keep their one line each, and the bar its height."""
    skip_if_day_ends_within(1)                                             # the booking starts in 8 minutes, and the service lists today's only
    title = "Quarterly planning review with the regional leadership teams"
    with PageServer(calendar_events=[event("e1", title, 8)], room_name="Room 2", room_location="2nd floor, by the kitchen") as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'soon'", timeout=5000)
        join_by_id(page)
        page.wait_for_function("!document.getElementById('next-warning').hidden && !document.getElementById('open-magnify').hidden", timeout=5000)
        button = page.locator("#open-magnify")
        box = button.bounding_box()
        assert round(box["width"]) == 64 and round(box["height"]) == 64
        assert button.locator("span").is_hidden() and button.locator("svg").is_visible() and button.get_attribute("aria-label") == "Magnify"
        bar, place, pill, clock = (page.locator(s).bounding_box() for s in (".top", "#room-location", "#next-warning", "#clock"))
        assert bar["height"] < 150 and place["height"] < 30                 # the bar as tall as it was, the place on its one line
        assert pill["y"] < clock["y"] + clock["height"] and clock["y"] < pill["y"] + pill["height"]     # one row: the warning beside the clock
        middles = {round(b["y"] + b["height"] / 2) for b in (page.locator(s).bounding_box() for s in ("#open-magnify", "#open-touchpad", "#open-tv-remote"))}
        assert len(middles) == 1 and fits(page)
        open_magnify(page)                                                  # and it opens the screen as ever
        page.click("#magnify-presets .button[data-zoom='2']")
        settled(page, server, (2.0, 0.5, 0.5))
        assert button.get_attribute("aria-label") == "Magnify 2×" and "current" in button.get_attribute("class")
        page.close()


# --- the screen


def test_the_screen_shows_the_whole_picture_at_first_and_fits_the_table(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_magnify(page)
        assert page.locator("#magnify-level").inner_text() == "1×"
        assert the_box(page) == (0.0, 0.0, 1.0, 1.0)                           # the box is the whole pad
        assert [b.inner_text().strip() for b in page.locator("#magnify-presets .button").all()] == ["1×", "1.5×", "2×", "3×", "4×"]
        assert page.locator("#magnify-presets .button[aria-pressed='true']").inner_text().strip() == "1×"
        assert page.locator("#magnify-out").is_disabled() and page.locator("#magnify-reset").is_disabled() and page.locator("#magnify-in").is_enabled()
        pad = page.locator("#magnify-pad").bounding_box()
        assert abs(pad["width"] / pad["height"] - 16 / 9) < 0.01               # the TV's shape
        assert fits(page) and asked(server) == []                              # opening it asks the TV for nothing
        page.close()


def test_a_step_button_magnifies_and_the_box_shrinks_to_the_part_shown(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_magnify(page)
        page.click("#magnify-presets .button[data-zoom='2']")
        settled(page, server, (2.0, 0.5, 0.5))
        assert asked(server) == [{"zoom": 2, "x": 0.5, "y": 0.5}]
        assert page.locator("#magnify-level").inner_text() == "2×" and the_box(page) == (0.25, 0.25, 0.5, 0.5)
        assert page.locator("#magnify-presets .button[aria-pressed='true']").inner_text().strip() == "2×"
        assert page.locator("#magnify-out").is_enabled() and page.locator("#magnify-reset").is_enabled()
        page.click("#magnify-presets .button[data-zoom='4']")
        settled(page, server, (4.0, 0.5, 0.5))
        assert the_box(page) == (0.38, 0.38, 0.25, 0.25) and page.locator("#magnify-in").is_disabled()      # the most there is
        assert fits(page)
        page.close()


def test_larger_and_smaller_go_from_one_step_to_the_next(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_magnify(page)
        for zoom in (1.5, 2.0, 3.0, 4.0):
            page.click("#magnify-in")
            settled(page, server, (zoom, 0.5, 0.5))
        assert page.locator("#magnify-level").inner_text() == "4×"
        for zoom in (3.0, 2.0, 1.5, 1.0):
            page.click("#magnify-out")
            settled(page, server, (zoom, 0.5, 0.5))
        assert page.locator("#magnify-level").inner_text() == "1×" and the_box(page) == (0.0, 0.0, 1.0, 1.0)
        page.close()


def test_a_finger_on_the_pad_moves_the_box_and_the_tv_follows(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_magnify(page)
        page.click("#magnify-presets .button[data-zoom='2']")
        settled(page, server, (2.0, 0.5, 0.5))
        page.mouse.move(*on_the_pad(page, 0.5, 0.5))                           # on the box: it is taken where it is held
        page.mouse.down()
        page.mouse.move(*on_the_pad(page, 0.6, 0.4), steps=6)
        page.wait_for_function("document.getElementById('magnify-window').getBoundingClientRect().width > 0", timeout=2000)
        assert the_box(page) == (0.35, 0.15, 0.5, 0.5)                         # it follows the finger while the finger is down
        page.mouse.up()
        settled(page, server, (2.0, 0.6, 0.4))
        assert the_box(page) == (0.35, 0.15, 0.5, 0.5)
        moves = asked(server)[1:]
        assert moves and all(move["zoom"] == 2 for move in moves) and moves[-1]["x"] == pytest.approx(0.6, abs=0.01)
        page.close()


def test_the_box_stops_at_the_pictures_edges(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_magnify(page)
        page.click("#magnify-presets .button[data-zoom='2']")
        settled(page, server, (2.0, 0.5, 0.5))
        page.mouse.move(*on_the_pad(page, 0.5, 0.5))
        page.mouse.down()
        page.mouse.move(*on_the_pad(page, 0.99, 0.01), steps=6)                # dragged past the top right corner
        page.mouse.up()
        settled(page, server, (2.0, 0.75, 0.25))
        assert the_box(page) == (0.5, 0.0, 0.5, 0.5)                           # in the corner, and no further
        page.close()


def test_a_touch_outside_the_box_brings_the_box_to_the_finger(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_magnify(page)
        page.click("#magnify-presets .button[data-zoom='4']")
        settled(page, server, (4.0, 0.5, 0.5))
        page.mouse.click(*on_the_pad(page, 0.2, 0.7))
        settled(page, server, (4.0, 0.2, 0.7))
        assert the_box(page) == (0.07, 0.58, 0.25, 0.25)
        page.close()


def test_a_tap_on_the_whole_picture_magnifies_about_the_point_tapped(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_magnify(page)
        page.mouse.click(*on_the_pad(page, 0.7, 0.4))
        settled(page, server, (2.0, 0.7, 0.4))
        assert page.locator("#magnify-level").inner_text() == "2×" and the_box(page) == (0.45, 0.15, 0.5, 0.5)
        page.close()


def test_a_drag_on_the_whole_picture_does_nothing(browser):  # noqa: F811
    """The box is the whole pad at 1: there is nowhere to move it, and a finger that slides across is not a tap."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_magnify(page)
        page.mouse.move(*on_the_pad(page, 0.3, 0.3))
        page.mouse.down()
        page.mouse.move(*on_the_pad(page, 0.7, 0.6), steps=5)
        page.mouse.up()
        page.wait_for_timeout(400)
        assert asked(server) == [] and page.locator("#magnify-level").inner_text() == "1×"
        page.close()


PINCH = """([spread]) => {
  const pad = document.getElementById('magnify-pad'), r = pad.getBoundingClientRect();
  const at = (id, x, type) => pad.dispatchEvent(new PointerEvent(type, { pointerId: id, pointerType: 'touch', isPrimary: id === 11, bubbles: true, cancelable: true,
    clientX: r.left + r.width * x, clientY: r.top + r.height * 0.5 }));
  at(11, 0.45, 'pointerdown'); at(12, 0.55, 'pointerdown');
  at(11, 0.5 - spread / 2, 'pointermove'); at(12, 0.5 + spread / 2, 'pointermove');
  at(11, 0.5 - spread / 2, 'pointerup'); at(12, 0.5 + spread / 2, 'pointerup');
}"""


def test_two_fingers_spreading_magnify_and_pinching_together_makes_it_smaller_again(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_magnify(page)
        page.evaluate(PINCH, [0.25])                                           # two fingers a tenth of the pad apart, spread to a quarter of it
        settled(page, server, (2.5, 0.5, 0.5))
        assert page.locator("#magnify-level").inner_text() == "2.5×"
        assert page.locator("#magnify-presets .button[aria-pressed='true']").count() == 0      # between two steps: none of them is it
        page.evaluate(PINCH, [0.05])                                           # and pinched to half as far apart
        settled(page, server, (1.25, 0.5, 0.5))
        page.evaluate(PINCH, [0.01])                                           # as far in as it goes: the whole picture, never less
        settled(page, server, (1.0, 0.5, 0.5))
        page.close()


def test_whole_picture_puts_the_tv_back(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_magnify(page)
        page.mouse.click(*on_the_pad(page, 0.8, 0.8))
        settled(page, server, (2.0, 0.75, 0.75))
        page.click("#magnify-reset")
        settled(page, server, (1.0, 0.5, 0.5))
        assert the_box(page) == (0.0, 0.0, 1.0, 1.0) and page.locator("#magnify-reset").is_disabled()
        page.close()


def test_the_screen_draws_what_the_tv_has_when_someone_else_changed_it(browser):  # noqa: F811
    """Another table screen, or a meeting that began magnified: the model is what is drawn, at the next poll."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_magnify(page)
        server.meeting.current_meeting.controls["magnify"] = dict(WHOLE, zoom=3.0, x=0.3, y=0.6)
        page.wait_for_function("document.getElementById('magnify-level').textContent === '3×'", timeout=5000)
        assert the_box(page) == (0.13, 0.43, 0.33, 0.33) and asked(server) == []
        page.close()


def test_a_refusal_is_said_on_the_screen_and_the_box_goes_back_to_what_the_tv_has(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_magnify(page)
        server.meeting.fail_control = MeetingControlError("Could not magnify the TV: the page is loading")
        page.click("#magnify-presets .button[data-zoom='2']")
        page.wait_for_function("document.getElementById('magnify-message').textContent !== ''", timeout=5000)
        assert page.locator("#magnify-message").inner_text() == "Could not magnify the TV: the page is loading"
        assert page.locator("#magnify-level").inner_text() == "1×" and the_box(page) == (0.0, 0.0, 1.0, 1.0)
        server.meeting.fail_control = None
        page.click("#magnify-presets .button[data-zoom='2']")                  # the next try clears the words
        settled(page, server, (2.0, 0.5, 0.5))
        assert page.locator("#magnify-message").inner_text() == ""
        page.close()


def test_back_returns_to_the_meeting_screen_and_the_tv_stays_as_it_is(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_magnify(page)
        page.click("#magnify-presets .button[data-zoom='2']")
        settled(page, server, (2.0, 0.5, 0.5))
        page.click("#back-from-magnify")
        assert view(page) == "meeting" and magnified(server) == (2.0, 0.5, 0.5)
        page.close()


def test_the_screen_gives_way_when_the_meeting_ends(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        open_magnify(page)
        asyncio.run_coroutine_threadsafe(server.meeting.leave_meeting(), server._loop).result(5)
        page.wait_for_function("document.body.dataset.view === 'idle'", timeout=5000)
        assert page.locator("#open-magnify").is_hidden()
        page.close()


@pytest.mark.parametrize("width, height", [(390, 844), (768, 1024)])
def test_a_phone_and_a_tablet_keep_the_screen_within_their_width(browser, width, height):  # noqa: F811
    with PageServer() as server:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        join_by_id(page)
        page.wait_for_function("!document.getElementById('open-magnify').hidden", timeout=5000)
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        if width == 390:
            # A phone's bar keeps the clock, the Touchpad and the TV in one row, as before; Magnify is under them
            rows = [round(page.locator(s).bounding_box()["y"]) for s in ("#open-touchpad", "#open-tv-remote", "#open-magnify")]
            assert rows[0] == rows[1] and rows[2] > rows[1]
        open_magnify(page)
        page.click("#magnify-presets .button[data-zoom='3']")
        settled(page, server, (3.0, 0.5, 0.5))
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        pad, side = page.locator("#magnify-pad").bounding_box(), page.locator(".magnify-side").bounding_box()
        assert side["y"] >= pad["y"] + pad["height"]                           # the buttons under the pad
        page.close()


# --- the Mute and Camera tiles say the state, and not only what a tap does


def tile(page, index):
    return page.evaluate("""(i) => { const b = document.querySelectorAll('#meeting-actions .button')[i], s = getComputedStyle(b), a = getComputedStyle(b, '::after');
        return { words: b.textContent.trim(), off: b.classList.contains('off'), badge: b.dataset.badge || '', fill: s.backgroundColor,
                 drawn: a.content === 'none' ? '' : a.content.replace(/"/g, ''), icon: b.querySelector('use').getAttribute('href') }; }""", index)


def test_a_muted_room_and_a_camera_that_is_off_have_red_tiles_that_say_so(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelectorAll('#meeting-actions .button').length >= 2", timeout=5000)
        mute, camera = tile(page, 0), tile(page, 1)
        assert (mute["words"], mute["off"], mute["badge"], mute["drawn"]) == ("Mute", False, "", "")           # live: the tile is as the others
        assert (camera["words"], camera["off"], camera["badge"]) == ("Turn camera off", False, "")
        assert mute["fill"] == camera["fill"] == tile(page, 2)["fill"]
        server.meeting.muted = True
        server.meeting.current_meeting.is_muted = True
        page.wait_for_function("document.querySelectorAll('#meeting-actions .button')[0].classList.contains('off')", timeout=5000)
        mute, camera = tile(page, 0), tile(page, 1)
        assert (mute["words"], mute["badge"], mute["drawn"], mute["icon"]) == ("Unmute", "Muted", "Muted", "#icon-mic-off")   # the label still says what a tap does
        assert mute["fill"] == "rgb(180, 35, 24)" and camera["off"] is False and camera["fill"] != mute["fill"]            # the microphone's tile alone
        server.meeting.camera_on = False
        server.meeting.current_meeting.is_camera_on = False
        page.wait_for_function("document.querySelectorAll('#meeting-actions .button')[1].classList.contains('off')", timeout=5000)
        camera = tile(page, 1)
        assert (camera["words"], camera["badge"], camera["drawn"], camera["icon"]) == ("Turn camera on", "Camera off", "Camera off", "#icon-video-off")
        assert camera["fill"] == "rgb(180, 35, 24)" and tile(page, 2)["off"] is False
        assert fits(page)                                                      # the badge is drawn on the tile, and moves nothing
        page.close()


def test_a_tap_on_a_red_tile_turns_the_microphone_on_and_the_tile_back(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelectorAll('#meeting-actions .button').length >= 2", timeout=5000)
        page.locator("#meeting-actions .button").nth(0).click()                # Mute
        page.wait_for_function("document.querySelectorAll('#meeting-actions .button')[0].classList.contains('off')", timeout=5000)
        assert server.meeting.muted is True and tile(page, 0)["badge"] == "Muted"
        page.wait_for_timeout(600)                                             # the page ignores a tap for half a second after its buttons change under a finger
        page.locator("#meeting-actions .button").nth(0).click()                # Unmute
        page.wait_for_function("!document.querySelectorAll('#meeting-actions .button')[0].classList.contains('off')", timeout=5000)
        assert server.meeting.muted is False and tile(page, 0) == dict(tile(page, 0), words="Mute", badge="", drawn="", icon="#icon-mic")
        page.close()


def test_the_badge_is_at_the_tiles_right_on_a_phone_where_a_tile_is_a_row(browser):  # noqa: F811
    with PageServer() as server:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        join_by_id(page)
        server.meeting.muted = True
        server.meeting.current_meeting.is_muted = True
        page.wait_for_function("document.querySelectorAll('#meeting-actions .button')[0].classList.contains('off')", timeout=5000)
        assert tile(page, 0)["drawn"] == "Muted"
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.close()
