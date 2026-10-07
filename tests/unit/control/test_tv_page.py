"""
The TV screensaver page in the venv's Chromium: four styles from the status
and bookings, switched in place when the room page picks another one
(spec 2026-10-07 TV, section 4.1).
"""

import pytest

from tests.unit.control.test_page import PageServer, browser, event  # noqa: F401 - fixture and helpers

playwright = pytest.importorskip("playwright.sync_api")


def open_tv(browser, server, width=1280, height=720):
    page = browser.new_page(viewport={"width": width, "height": height})
    page.goto(f"http://127.0.0.1:{server.port}/tv", wait_until="networkidle")
    page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
    return page


def set_style(page, server, style):
    page.request.post(f"http://127.0.0.1:{server.port}/api/screensaver", data={"style": style},
                      headers={"Content-Type": "application/json"})
    page.wait_for_function(f"document.body.dataset.style === '{style}'", timeout=5000)


def test_info_style_shows_name_status_next_booking_and_clock(browser):
    with PageServer(calendar_events=[event("e1", "Design review", 25)], room_name="Room 3") as server:
        page = open_tv(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        assert page.locator("body").get_attribute("data-style") == "info"
        assert page.locator("#room-name").inner_text() == "Room 3"
        assert page.locator("#headline").inner_text().startswith("Free until")
        assert "Design review" in page.locator("#detail").inner_text()
        assert page.locator("#clock").inner_text() != ""
        assert "Press Join on the controller" in page.locator("#hint").inner_text()
        assert page.locator("button").count() == 0  # the TV has nothing to press
        assert errors == []
        page.close()


def test_quiet_style_shows_only_name_and_status(browser):
    with PageServer(room_name="Room 3") as server:
        page = open_tv(browser, server)
        set_style(page, server, "quiet")
        assert page.locator("#room-name").is_visible() and page.locator("#headline").is_visible()
        assert page.locator("#detail").is_hidden() and page.locator("#clock").is_hidden() and page.locator("#hint").is_hidden()
        page.close()


def test_brand_style_shows_the_logo_and_nothing_live(browser):
    with PageServer() as server:
        page = open_tv(browser, server)
        set_style(page, server, "brand")
        assert page.locator("#brand-logo").is_visible()
        assert page.locator("#headline").is_hidden() and page.locator("#room-name").is_hidden()
        page.close()


def test_bounce_moves_the_logo_and_changes_colour_at_an_edge(browser):
    with PageServer() as server:
        page = open_tv(browser, server, width=600, height=400)   # small, so an edge comes quickly
        set_style(page, server, "bounce")
        logo = page.locator("#bounce-logo")
        assert logo.is_visible()
        first = logo.bounding_box()
        colour_before = page.evaluate("getComputedStyle(document.getElementById('bounce-logo')).color")
        page.wait_for_timeout(600)
        second = logo.bounding_box()
        assert (first["x"], first["y"]) != (second["x"], second["y"])
        page.wait_for_function(
            f"getComputedStyle(document.getElementById('bounce-logo')).color !== '{colour_before}'", timeout=8000)
        assert page.locator("#bounce-logo svg path").count() > 0   # the real logo, inlined so it can take the colour
        page.close()


def test_style_change_re_renders_without_a_reload(browser):
    with PageServer() as server:
        page = open_tv(browser, server)
        loaded_at = page.evaluate("window.__tvLoadedAt")
        set_style(page, server, "bounce")
        set_style(page, server, "info")
        assert page.evaluate("window.__tvLoadedAt") == loaded_at
        page.close()
