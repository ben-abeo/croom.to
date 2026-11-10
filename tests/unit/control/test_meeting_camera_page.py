"""The camera's quick controls on the meeting screen (Ben, 2026-11-09): Frame people and the saved presets, the arrows with Home in their
middle, and the zoom, under the call's buttons, so nobody has to open the Camera screen for them in a call. They are the Camera screen's own
commands (devices.js). Only the risky paths: what is drawn and when, a preset recall, a held arrow that stops with the finger, Frame people's
words (a framing that lost everyone among them), and that the strip fits the table's 1280 by 800 above the dock."""

import time

from croom.meeting.providers.base import MeetingState
from tests.unit.control.test_page import PageServer, browser, join_by_id, open_page  # noqa: F401  (browser is a fixture)
from tests.unit.control.test_service import StubCamera, StubDevices


class FramingView:
    """The TV's tap as the frame route asks it: nothing blocks the picture, and a still is there."""

    def blocked(self):
        return None

    async def still(self):
        return b"jpeg"


def eventually(condition, seconds=5):
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


def in_a_call(browser, server):  # noqa: F811
    page = open_page(browser, server)
    join_by_id(page)
    page.wait_for_selector("#meeting-camera:not([hidden])", timeout=5000)
    return page


def test_the_meeting_screen_has_frame_people_the_saved_presets_the_arrows_and_the_zoom(browser):  # noqa: F811
    with PageServer() as server:                          # the stub camera has Wide and Table saved, not Whiteboard
        page = in_a_call(browser, server)
        assert page.locator("#meeting-views button").all_inner_texts() == ["Frame people", "Wide", "Table"]
        pad = page.locator("#meeting-camera .arrow-pad")
        assert [pad.locator(".pad").nth(i).get_attribute("aria-label") for i in range(5)] == ["Tilt up", "Pan left", "Home", "Pan right", "Tilt down"]
        assert page.locator("#meeting-camera [data-zoom]").count() == 2
        page.close()


def test_a_preset_zoom_and_home_on_the_meeting_screen_move_the_camera(browser):  # noqa: F811
    with PageServer() as server:
        camera = server.devices.camera
        page = in_a_call(browser, server)
        page.click("#meeting-views button:has-text('Table')")
        assert eventually(lambda: camera.recalls == [2])
        page.click("#meeting-camera [aria-label='Zoom in']")
        assert eventually(lambda: camera.zooms == [125])
        page.click("#meeting-camera [aria-label='Home']")
        assert eventually(lambda: camera.homes == 1)
        page.close()


def test_an_arrow_held_on_the_meeting_screen_turns_the_camera_until_it_is_let_go(browser):  # noqa: F811
    with PageServer() as server:
        camera = server.devices.camera
        page = in_a_call(browser, server)
        box = page.locator("#meeting-camera [aria-label='Pan left']").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        assert eventually(lambda: (-1, 0) in camera.moves)
        page.mouse.up()
        assert eventually(lambda: camera.moves[-1] == (0, 0))
        page.close()


def test_frame_people_on_the_meeting_screen_says_what_it_did_and_when_it_lost_everyone(browser):  # noqa: F811
    with PageServer() as server:
        server.meeting.camera_view = FramingView()
        camera = server.devices.camera
        page = in_a_call(browser, server)
        note = "document.getElementById('meeting-camera-note').innerText"
        page.click("#meeting-frame-people")
        page.wait_for_function(f"{note} === 'Framed 2 people'", timeout=5000)
        camera.frame_answer = {"framed": 2, "turned": False, "lost": True}
        page.click("#meeting-frame-people")
        page.wait_for_function(f"{note} === 'Lost everyone after the turn: back to the wide picture'", timeout=5000)
        assert camera.frames == 2
        page.close()


def test_the_strip_is_not_drawn_without_a_camera(browser):  # noqa: F811
    with PageServer(devices=StubDevices(camera=StubCamera(available=False))) as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.getElementById('meeting-state').innerText === 'In a meeting'", timeout=5000)
        assert page.locator("#meeting-camera").is_hidden()
        page.close()


def test_the_strip_fits_between_the_calls_buttons_and_the_dock_and_gives_way_to_meets_box(browser):  # noqa: F811
    for role in ("participant", "host"):
        with PageServer(role=role) as server:
            page = in_a_call(browser, server)
            strip = page.locator("#meeting-camera").bounding_box()
            above = page.locator("#meeting-secondary").bounding_box()
            dock = page.locator(".meeting-dock").bounding_box()
            assert above["y"] + above["height"] <= strip["y"] and strip["y"] + strip["height"] <= dock["y"], role
            assert page.evaluate("document.documentElement.scrollHeight") <= 800, role
            server.meeting.current_meeting.dialog = {"id": 1, "title": "Unmute?", "text": "The host would like you to unmute.",
                                                     "buttons": ["Unmute", "Stay muted"]}
            page.wait_for_selector("#meeting-dialog:not([hidden])", timeout=5000)
            assert page.locator("#meeting-camera").is_hidden(), role      # Meet's box needs the room, and a decision first
            page.close()
