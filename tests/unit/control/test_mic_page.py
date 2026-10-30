"""Mic level on the room page (spec 2026-10-29): its own screen, as the camera's, from Options and from the meeting screen's dock (the dock has no
height to spare at the table's 1280 by 800). 0 to 150, Quieter / Louder five at a time, "No microphone found" when there is none."""
import asyncio

import pytest

from croom.devices.errors import DeviceUnavailable
from tests.unit.control.test_page import PageServer, browser, fits, join_by_id, open_page  # noqa: F401  (browser is a fixture)
from tests.unit.control.test_service import StubDevices, StubMic

pytest.importorskip("playwright.sync_api")


class BusyMic(StubMic):
    async def set_level(self, level):
        raise DeviceUnavailable("the microphone is busy")


def box(page, selector):
    b = page.locator(selector).bounding_box()
    return b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"]


def view(page):
    return page.evaluate("document.body.dataset.view")


def test_from_the_meeting_screens_dock_the_mic_level_changes_and_back_returns_to_the_meeting(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("!document.getElementById('sound-panel').hidden", timeout=5000)
        assert page.locator("#open-mic").inner_text().strip() == "Mic"                          # short: the speaker's name keeps its line
        assert page.locator("#open-mic").get_attribute("aria-label") == "Mic level"
        assert page.locator("#open-mic svg use").get_attribute("href") == "#icon-mic"
        device, mic, camera = box(page, "#sound-device"), box(page, "#open-mic"), box(page, "#open-camera-live")
        assert abs(mic[1] - camera[1]) <= 1 and mic[2] < camera[0] and device[2] <= mic[0], (device, mic, camera)   # on the speaker's line, before Camera
        assert mic[2] - mic[0] < 160, mic                                                          # a word's width, not a menu item's
        page.click("#open-mic")
        page.wait_for_function("document.body.dataset.view === 'mic'", timeout=5000)
        page.wait_for_function("!document.getElementById('mic-panel').hidden", timeout=5000)
        assert "MeetUp" in page.locator("#mic-device").inner_text()
        assert page.locator("#mic-level").inner_text() == "100" and page.locator("#mic-slider").get_attribute("max") == "150"
        page.click("#mic-louder")
        page.wait_for_function("document.getElementById('mic-level').innerText === '105'", timeout=5000)
        page.click("#mic-quieter")
        page.wait_for_function("document.getElementById('mic-level').innerText === '100'", timeout=5000)
        page.locator("#mic-slider").evaluate("el => { el.value = 140; el.dispatchEvent(new Event('change', {bubbles: true})); }")
        page.wait_for_function("document.getElementById('mic-level').innerText === '140'", timeout=5000)
        assert server.devices.mic.calls == [("level", 105), ("level", 100), ("level", 140)]
        assert server.devices.volume.calls == []                                      # the speaker is left alone
        assert fits(page)
        page.click("#back-from-mic")
        assert view(page) == "meeting"
        page.close()


def test_outside_a_meeting_it_is_in_options_and_back_returns_there(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        page.click("#open-options")
        page.click("#open-mic-options")
        page.wait_for_function("document.body.dataset.view === 'mic'", timeout=5000)
        page.wait_for_function("document.getElementById('mic-level').innerText === '100'", timeout=5000)
        page.click("#mic-louder")
        page.wait_for_function("document.getElementById('mic-level').innerText === '105'", timeout=5000)
        assert server.devices.mic.calls == [("level", 105)] and fits(page)
        page.click("#back-from-mic")
        assert view(page) == "options"
        page.close()


def test_no_microphone_says_so(browser):  # noqa: F811
    with PageServer(devices=StubDevices(mic=StubMic(available=False))) as server:
        page = open_page(browser, server)
        page.click("#open-options")
        page.click("#open-mic-options")
        page.wait_for_function("document.getElementById('mic-note').innerText === 'No microphone found'", timeout=5000)
        assert page.locator("#mic-panel").is_hidden()
        server.devices.mic.available, server.devices.mic.reason = True, None          # plugged in: it comes back with the next poll
        page.wait_for_function("!document.getElementById('mic-panel').hidden", timeout=5000)
        assert page.locator("#mic-note").inner_text() == ""
        page.close()


def test_a_refused_mic_change_shows_its_reason_and_the_slider_goes_back(browser):  # noqa: F811
    with PageServer(devices=StubDevices(mic=BusyMic())) as server:
        page = open_page(browser, server)
        page.click("#open-options")
        page.click("#open-mic-options")
        page.wait_for_function("!document.getElementById('mic-panel').hidden", timeout=5000)
        for level in (130, 140):                                                      # the same words twice: the slider must still go back
            with page.expect_response("**/api/audio/mic"):
                page.locator("#mic-slider").evaluate(f"el => {{ el.value = {level}; el.dispatchEvent(new Event('change', {{bubbles: true}})); }}")
            page.wait_for_function("document.getElementById('mic-slider').value === '100'", timeout=5000)
            assert page.locator("#mic-note").inner_text() == "the microphone is busy"
        page.close()


def test_the_screen_opened_from_a_meeting_gives_way_when_the_meeting_ends(browser):  # noqa: F811
    """Mic level belongs to the mode that opened it, as the camera's screen does: a meeting that ends under it takes the page to the idle screen."""
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.click("#open-mic")
        page.wait_for_function("document.body.dataset.view === 'mic'", timeout=5000)
        asyncio.run_coroutine_threadsafe(server.meeting.leave_meeting(), server._loop).result(5)
        page.wait_for_function("document.body.dataset.view === 'idle'", timeout=5000)
        page.close()


@pytest.mark.parametrize("width, height", [(390, 844), (768, 1024)])
def test_a_phone_and_a_tablet_keep_the_dock_and_the_screen_within_their_width(browser, width, height):  # noqa: F811
    with PageServer() as server:
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        join_by_id(page)
        page.wait_for_function("!document.getElementById('sound-panel').hidden", timeout=5000)
        sideways = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        assert page.evaluate(sideways) and page.locator("#open-mic").is_visible()
        page.click("#open-mic")
        page.wait_for_function("!document.getElementById('mic-panel').hidden", timeout=5000)
        assert page.evaluate(sideways)
        page.close()
