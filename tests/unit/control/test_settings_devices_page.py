"""Settings › Software's cards of the devices that check in (room settings spec 2026-10-29, sections 3, 6 and 7): the touch Pi's card
(when it last checked in, "Not seen since <time>", its version, the newest and the changes, Update, Reboot, Forget, the job under way in
the device's words and what came of the last), Devices waiting to join with Allow and Refuse, and Update all (never in a meeting). The
registry is the real one, on a clock a test moves; the touch Pi's check-ins are made by the test; the TV Pi's helper is a scripted
stand-in; GitHub is the real client on a fake transport. Driven with the Playwright Chromium."""

import time

import pytest

from croom.meeting.providers.base import MeetingState
from croom.settings.checkin import Registry
from tests.unit.control.test_page import (  # noqa: F401  (browser is a fixture)
    PageServer,
    browser,
    fits,
)
from tests.unit.control.test_settings_page import unlocked_page
from tests.unit.control.test_settings_software_page import (
    A,
    B,
    ScriptedHelper,
    github,
    open_software,
    text,
    wait_text,
)
from tests.unit.settings.test_checkin import Clock

playwright = pytest.importorskip("playwright.sync_api")

TOUCH = "t" * 32
TOKEN = "k" * 43
CARD = f".device-card[data-device='{TOUCH}']"


def checkin(registry, device=TOUCH, version=A, job=None, name="touch-pi"):
    return registry.checkin({"id": device, "kind": "touch", "version": version, "name": name, "network": {}, "job": job}, TOKEN,
                            "10.0.0.9")


def a_room(version=A, waiting=0):
    """A registry with the touch Pi let in (at `version`) and `waiting` more asking to join."""
    registry = Registry(None, clock=Clock(time.time()))
    checkin(registry, version=version)
    registry.allow(TOUCH)
    for n in range(waiting):
        checkin(registry, device=f"{n:032x}", name=f"pi-{n}")
    return registry


def test_the_touch_pis_card_and_those_waiting_to_join(browser):  # noqa: F811
    registry = a_room(waiting=1)
    with PageServer(admin=ScriptedHelper(), github=github(), registry=registry) as server:
        page = unlocked_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_software(page)
        page.wait_for_selector(CARD, timeout=5000)
        assert text(page, f"{CARD} .software-title") == "Touch Pitouch-pi"
        assert text(page, f"{CARD} .device-seen") == "Checked in just now"
        assert text(page, f"{CARD} .software-version") == "Version aaaaaaa, 28 Oct 2026"
        assert text(page, f"{CARD} .software-newest") == "Newest: bbbbbbb, 30 Oct 2026"
        assert page.locator(f"{CARD} .software-change").count() == 2
        assert page.locator(f"{CARD} .device-update").is_visible()
        assert text(page, f"{CARD} .device-reboot") == "Reboot" and text(page, f"{CARD} .device-forget") == "Forget"
        assert not page.locator(f"{CARD} .device-job").is_visible() and not page.locator(f"{CARD} .device-last").is_visible()
        waiting = page.locator(".software-waiting")
        assert text(page, ".software-waiting .software-title") == "Devices waiting to join"
        assert text(page, ".waiting-title") == "Touch Pi pi-0"
        assert text(page, ".waiting-detail").startswith("10.0.0.9 · asked at ")
        assert waiting.locator(".waiting-allow").is_visible() and waiting.locator(".waiting-refuse").is_visible()
        assert page.locator(".software-update-all").is_visible()        # the TV Pi and the touch Pi are both behind
        assert fits(page)
        assert errors == []
        page.close()


def test_allow_lets_a_device_in_and_refuse_asks_twice(browser):  # noqa: F811
    registry = a_room(waiting=2)
    with PageServer(admin=ScriptedHelper(), github=github(), registry=registry) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        page.wait_for_selector(".waiting-device", timeout=5000)
        first, second = f"{0:032x}", f"{1:032x}"
        page.click(f".waiting-device[data-device='{first}'] .waiting-allow")
        page.wait_for_selector(f".device-card[data-device='{first}']", timeout=5000)
        assert [d["id"] for d in registry.devices()] == [TOUCH, first]
        refuse = page.locator(f".waiting-device[data-device='{second}'] .waiting-refuse")
        refuse.click()
        assert refuse.inner_text().strip() == "Tap again to refuse" and registry.waiting()
        refuse.click()
        page.wait_for_function("!document.querySelector('.software-waiting')", timeout=5000)
        assert registry.waiting() == []
        page.close()


def test_update_follows_the_touch_pis_own_words_to_its_result(browser):  # noqa: F811
    registry = a_room()
    with PageServer(admin=ScriptedHelper(), github=github(), registry=registry) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        page.wait_for_selector(CARD, timeout=5000)
        page.click(f"{CARD} .device-update")
        wait_text(page, f"{CARD} .device-job", "Updating to bbbbbbb: waiting for the touch Pi to take it")
        assert not page.locator(f"{CARD} .device-update").is_visible()
        assert page.locator(f"{CARD} .device-reboot").is_disabled()
        job = checkin(registry)["job"]
        assert job["type"] == "update" and job["args"] == {"commit": B}
        checkin(registry, job={"id": job["id"], "type": "update", "state": "running", "words": "Fetching main from GitHub"})
        wait_text(page, f"{CARD} .device-job", "Updating to bbbbbbb: Fetching main from GitHub")        # the page reads again by itself
        checkin(registry, version=B, job={"id": job["id"], "type": "update", "state": "done", "words": "Updated to bbbbbbb"})
        page.wait_for_function(f"document.querySelector(\"{CARD} .device-last\").textContent.startsWith('Updated to bbbbbbb (')",
                               timeout=8000)
        assert page.locator(f"{CARD} .device-last").evaluate("n => n.classList.contains('ok')")
        assert text(page, f"{CARD} .software-newest") == "Up to date"
        assert not page.locator(f"{CARD} .device-job").is_visible()
        page.close()


def test_a_touch_pi_gone_quiet_says_since_when(browser):  # noqa: F811
    registry = a_room()
    seen = registry._clock.now
    registry._clock.now += 600
    with PageServer(admin=ScriptedHelper(), github=github(), registry=registry) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        page.wait_for_selector(CARD, timeout=5000)
        expected = page.evaluate("(s) => new Date(s * 1000).toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit', hour12: true})",
                                 seen)
        assert text(page, f"{CARD} .device-seen") == f"Not seen since {expected}"
        page.close()


def test_reboot_asks_twice_and_is_refused_in_a_meeting(browser):  # noqa: F811
    registry = a_room()
    with PageServer(admin=ScriptedHelper(), github=github(), registry=registry) as server:
        page = unlocked_page(browser, server, stopped=True)
        open_software(page)
        page.wait_for_selector(CARD, timeout=5000)
        server._loop.call_soon_threadsafe(server.meeting.set_state, MeetingState.CONNECTED)
        reboot = page.locator(f"{CARD} .device-reboot")
        reboot.click()
        assert reboot.inner_text().strip() == "Tap again to reboot"
        reboot.click()
        wait_text(page, f"{CARD} .software-message", "Leave the meeting first")
        assert registry.get(TOUCH)["job"] is None
        server._loop.call_soon_threadsafe(server.meeting.set_state, MeetingState.IDLE)
        reboot.click()
        reboot.click()
        wait_text(page, f"{CARD} .device-job", "Rebooting: waiting for the touch Pi to take it")
        assert registry.get(TOUCH)["job"]["type"] == "reboot"
        page.close()


def test_update_all_updates_the_touch_pi_and_the_tv_pi(browser):  # noqa: F811
    registry = a_room()
    helper = ScriptedHelper()
    with PageServer(admin=helper, github=github(), registry=registry) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        page.wait_for_selector(CARD, timeout=5000)
        page.click(".software-update-all")
        wait_text(page, ".software-all-message", "Updating Touch Pi, TV Pi")
        assert ("update", {"commit": B}) in helper.calls and registry.get(TOUCH)["job"]["args"] == {"commit": B}
        page.wait_for_function("document.querySelector('.software-update-all').disabled", timeout=5000)   # while they work
        page.close()


def test_update_all_waits_for_the_meeting_to_end(browser):  # noqa: F811
    registry = a_room()
    helper = ScriptedHelper()
    with PageServer(admin=helper, github=github(), registry=registry) as server:
        page = unlocked_page(browser, server, stopped=True)   # no status poll moves the page to the meeting screen meanwhile
        server._loop.call_soon_threadsafe(server.meeting.set_state, MeetingState.CONNECTED)
        open_software(page)
        page.wait_for_selector(CARD, timeout=5000)
        assert page.locator(".software-update-all").is_disabled()
        assert text(page, ".software-all-message") == "Leave the meeting first"
        page.close()


def test_update_all_is_not_there_for_the_tv_pi_alone_or_when_all_are_up_to_date(browser):  # noqa: F811
    with PageServer(admin=ScriptedHelper(), github=github(), registry=Registry(None)) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        assert not page.locator(".software-update-all").is_visible()
        page.close()
    helper = ScriptedHelper()
    helper.version = B
    with PageServer(admin=helper, github=github(), registry=a_room(version=B)) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        page.wait_for_selector(CARD, timeout=5000)
        assert text(page, f"{CARD} .software-newest") == "Up to date"
        assert not page.locator(".software-update-all").is_visible()
        page.close()


def test_forget_removes_the_card(browser):  # noqa: F811
    registry = a_room()
    with PageServer(admin=ScriptedHelper(), github=github(), registry=registry) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        page.wait_for_selector(CARD, timeout=5000)
        page.click(f"{CARD} .device-forget")
        page.click(f"{CARD} .device-forget")
        page.wait_for_function(f"!document.querySelector(\"{CARD}\")", timeout=5000)
        assert registry.devices() == []
        page.close()


def test_the_page_with_every_card_fits_the_touch_pi_and_a_phone(browser):  # noqa: F811
    registry = a_room(waiting=3)
    job = registry.queue(TOUCH, "update", {"commit": B})
    checkin(registry, job={"id": job["id"], "type": "update", "state": "running", "words": "Getting bbbbbbb " + "x" * 200})
    with PageServer(admin=ScriptedHelper(), github=github(changes=30), registry=registry) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        page.wait_for_selector(".waiting-device", timeout=5000)
        assert fits(page)
        # The Settings page scrolls inside itself to the last waiting device, and the TV Pi's card keeps its height
        last = page.locator(".waiting-device").last
        last.scroll_into_view_if_needed()
        assert last.is_visible()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.close()
