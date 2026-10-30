"""Settings › Network on the room page (room settings spec 2026-10-29, sections 3 and 7): a card per device (the TV Pi through the root
helper, the touch Pi from its check-ins) with how it is connected, Change Wi-Fi (the networks in range, a password typed, an open network
warned of, a hidden one by its name, Join) and Forget (a second tap); the result in the device's words; refused in a meeting; "Not
available on this device" without the helper; reading again while a change is under way and stopping once a device has been away 10
minutes (Software too); fits the touch Pi's 1280 by 800. The helper is the real jobs on a stand-in NetworkManager; the registry the real
one on a clock a test moves; the touch Pi's check-ins are made by the test. Driven with the Playwright Chromium."""

import time

import pytest

from croom.meeting.providers.base import MeetingState
from croom.settings.checkin import Registry
from tests.unit.control.test_network_api import WifiHelper
from tests.unit.control.test_page import PageServer, browser, fits  # noqa: F401  (browser is a fixture)
from tests.unit.control.test_settings_page import unlocked_page
from tests.unit.control.test_settings_software_page import ScriptedHelper, github, open_software, text, wait_text
from tests.unit.settings.test_checkin import Clock

playwright = pytest.importorskip("playwright.sync_api")

TOUCH = "t" * 32
TOKEN = "k" * 43
TV = ".network-card[data-device='tv']"
CARD = f".network-card[data-device='{TOUCH}']"
TOUCH_NETWORK = {"address": "10.0.0.9", "wifi": True, "connection": "wifi", "ssid": "Office", "signal": 55, "saved": ["Office", "Guest"],
                 "change": None}


def checkin(registry, job=None, network=None):
    return registry.checkin({"id": TOUCH, "kind": "touch", "version": "a" * 40, "name": "touch-pi", "network": network or TOUCH_NETWORK,
                             "job": job}, TOKEN, "10.0.0.9")


def a_room():
    registry = Registry(None, clock=Clock(time.time()))
    checkin(registry)
    registry.allow(TOUCH)
    return registry


def open_network(page):
    page.click("#settings-menu [data-page='network']")
    page.wait_for_selector(f"{TV}", timeout=5000)


def test_a_card_for_each_device_says_how_it_is_connected(browser, tmp_path):  # noqa: F811
    with PageServer(admin=WifiHelper(tmp_path), registry=a_room()) as server:
        page = unlocked_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_network(page)
        wait_text(page, f"{TV} .network-connection", "Wi-Fi: Office, signal 70%")
        assert text(page, f"{TV} .software-title") == "TV Pi"
        assert text(page, f"{TV} .network-address") == "Address 192.168.1.42"
        assert page.locator(f"{TV} .network-change").is_visible() and page.locator(f"{TV} .network-forget").is_visible()
        page.wait_for_selector(CARD, timeout=5000)
        assert text(page, f"{CARD} .software-title") == "Touch Pitouch-pi"
        assert text(page, f"{CARD} .network-seen") == "Checked in just now"
        assert text(page, f"{CARD} .network-connection") == "Wi-Fi: Office, signal 55%"
        assert text(page, f"{CARD} .network-address") == "Address 10.0.0.9"
        assert not page.locator(f"{TV} .network-result").is_visible() and not page.locator(f"{TV} .network-panel").is_visible()
        assert fits(page)
        assert errors == []
        page.close()


def test_the_tv_pi_joins_a_network_from_the_list_with_its_password(browser, tmp_path):  # noqa: F811
    helper = WifiHelper(tmp_path, run_now=True)
    helper.room.touch_at = helper.machine.clock.now + 10
    with PageServer(admin=helper, registry=a_room()) as server:
        page = unlocked_page(browser, server)
        open_network(page)
        page.click(f"{TV} .network-change")
        page.wait_for_selector(f"{TV} .network-choice[data-ssid='New Net']", timeout=5000)
        assert ("wifi_scan", {}) in helper.calls
        names = [t.strip() for t in page.locator(f"{TV} .network-name").all_inner_texts()]
        assert names[:3] == ["New Net", "Office", "Modern"]
        assert text(page, f"{TV} .network-choice[data-ssid='Office'] .network-detail") == "70% · Secured · Saved · Connected"
        assert fits(page)
        page.click(f"{TV} .network-choice[data-ssid='New Net']")
        assert text(page, f"{TV} .network-join-title") == "New Net"
        password = page.locator(f"{TV} .network-password")
        assert password.is_visible() and password.get_attribute("type") == "password"
        assert password.get_attribute("data-keyboard") == "setting"          # the page's keyboard types in it on the kiosk
        assert not page.locator(f"{TV} .network-warning").is_visible()
        password.fill("short")
        page.click(f"{TV} .network-join-button")
        assert text(page, f"{TV} .network-message").startswith("A Wi-Fi password is 8 to 63")
        password.fill("correct horse")
        page.click(f"{TV} .network-show")
        assert password.get_attribute("type") == "text"
        page.click(f"{TV} .network-join-button")
        page.wait_for_function(f"document.querySelector(\"{TV} .network-result\").textContent.startsWith('Joined New Net. The TV Pi reached its gateway, and the touch Pi checked in (')",
                               timeout=8000)
        assert ("wifi_connect", {"ssid": "New Net", "password": "correct horse", "hidden": False}) in helper.calls
        assert page.locator(f"{TV} .network-result").evaluate("n => n.classList.contains('ok')")
        wait_text(page, f"{TV} .network-connection", "Wi-Fi: New Net, signal 80%")
        assert not page.locator(f"{TV} .network-panel").is_visible()
        assert page.locator(f"{TV} .network-password").input_value() == ""              # the password is gone from the page
        assert text(page, f"{TV} .network-message") == ""
        page.close()


def test_a_change_that_is_undone_says_why_in_the_tv_pis_words(browser, tmp_path):  # noqa: F811
    helper = WifiHelper(tmp_path, run_now=True)
    with PageServer(admin=helper, registry=a_room()) as server:
        page = unlocked_page(browser, server)
        open_network(page)
        page.click(f"{TV} .network-change")
        page.click(f"{TV} .network-choice[data-ssid='New Net']", timeout=5000)
        page.fill(f"{TV} .network-password", "wrong horse")
        page.click(f"{TV} .network-join-button")
        page.wait_for_function(f"document.querySelector(\"{TV} .network-result\").textContent.startsWith("
                               "'Could not join New Net: the password was not accepted. The TV Pi is back on Office (')", timeout=8000)
        assert not page.locator(f"{TV} .network-result").evaluate("n => n.classList.contains('ok')")
        # A network that hides its name, by its typed name
        page.click(f"{TV} .network-change")
        page.click(f"{TV} .network-hidden")
        assert page.locator(f"{TV} .network-ssid").is_visible() and page.locator(f"{TV} .network-warning").is_visible()
        page.fill(f"{TV} .network-ssid", "Nowhere")
        page.fill(f"{TV} .network-password", "12345678")
        assert not page.locator(f"{TV} .network-warning").is_visible()
        page.click(f"{TV} .network-join-button")
        page.wait_for_function(f"document.querySelector(\"{TV} .network-result\").textContent.startsWith("
                               "'Could not join Nowhere: the network was not found. The TV Pi is back on Office (')", timeout=8000)
        assert ("wifi_connect", {"ssid": "Nowhere", "password": "12345678", "hidden": True}) in helper.calls
        # A refusal before anything changes is said beside the button
        page.click(f"{TV} .network-change")
        page.click(f"{TV} .network-choice[data-ssid='Corp']", timeout=5000)
        page.fill(f"{TV} .network-password", "12345678")
        page.click(f"{TV} .network-join-button")
        wait_text(page, f"{TV} .network-message", "Corp asks for a user name: networks like it are not supported")
        assert page.locator(f"{TV} .network-panel").is_visible()
        page.close()


def test_an_open_network_is_joined_with_a_warning_and_no_password(browser, tmp_path):  # noqa: F811
    helper = WifiHelper(tmp_path, run_now=True)
    helper.room.known = False
    with PageServer(admin=helper, registry=Registry(None, clock=Clock(time.time()))) as server:
        page = unlocked_page(browser, server)
        open_network(page)
        page.click(f"{TV} .network-change")
        page.click(f"{TV} .network-choice[data-ssid='Guest']", timeout=5000)
        assert page.locator(f"{TV} .network-warning").is_visible()
        assert text(page, f"{TV} .network-warning") == "This network is open: anyone nearby can see what the device sends over it."
        assert not page.locator(f"{TV} .network-password").is_visible()
        page.click(f"{TV} .network-join-button")
        page.wait_for_function(f"(document.querySelector(\"{TV} .network-result\").textContent || '').startsWith("
                               "'Joined Guest. No touch Pi has joined this room, so only the gateway was checked')", timeout=8000)
        assert ("wifi_connect", {"ssid": "Guest", "password": "", "hidden": False}) in helper.calls
        page.close()


def test_forget_asks_for_a_second_tap(browser, tmp_path):  # noqa: F811
    helper = WifiHelper(tmp_path)
    helper.nm.profiles["u-guest"] = {"name": "Guest", "ssid": "Guest", "psk": None, "autoconnect": True, "file": None, "hidden": False,
                                     "mgmt": None}
    with PageServer(admin=helper, registry=a_room()) as server:
        page = unlocked_page(browser, server)
        open_network(page)
        page.click(f"{TV} .network-forget")
        forget = page.locator(f"{TV} .network-forget-one[data-ssid='Guest']")
        forget.click()
        assert forget.inner_text().strip() == "Tap again to forget" and "u-guest" in helper.nm.profiles
        forget.click()
        wait_text(page, f"{TV} .network-message", "Forgot Guest")
        assert "u-guest" not in helper.nm.profiles
        page.click(f"{TV} .network-forget-one[data-ssid='Office']")
        page.click(f"{TV} .network-forget-one[data-ssid='Office']")
        wait_text(page, f"{TV} .network-message", "The TV Pi is on Office: join another network first, or it would be cut off")
        page.close()


def test_a_change_waits_for_the_meeting_to_end(browser, tmp_path):  # noqa: F811
    helper = WifiHelper(tmp_path)
    with PageServer(admin=helper, registry=a_room()) as server:
        page = unlocked_page(browser, server)
        server.meeting.state = MeetingState.CONNECTED
        open_network(page)
        wait_text(page, f"{TV} .network-message", "Leave the meeting first")
        page.click(f"{TV} .network-change")
        page.click(f"{TV} .network-choice[data-ssid='New Net']", timeout=5000)    # looking is allowed
        assert page.locator(f"{TV} .network-join-button").is_disabled()
        page.click(f"{TV} .network-forget")
        assert page.locator(f"{TV} .network-forget-one[data-ssid='Office']").is_disabled()
        assert not [c for c in helper.calls if c[0] in ("wifi_connect", "wifi_forget")]
        page.close()


def test_the_touch_pi_looks_for_networks_and_joins_one_through_its_jobs(browser):  # noqa: F811
    registry = a_room()
    with PageServer(registry=registry) as server:
        page = unlocked_page(browser, server)
        open_network(page)
        assert text(page, f"{TV} .network-connection") == "Not available on this device"
        assert not page.locator(f"{TV} .network-change").is_visible()
        page.click(f"{CARD} .network-change")
        page.wait_for_function(f"document.querySelector(\"{CARD} .network-job\").textContent.startsWith('Looking for networks')", timeout=5000)
        job = checkin(registry)["job"]
        assert job["type"] == "wifi_scan"
        networks = [{"ssid": "New Net", "signal": 80, "security": "WPA2", "open": False, "saved": False, "in_use": False},
                    {"ssid": "Office", "signal": 55, "security": "WPA2", "open": False, "saved": True, "in_use": True}]
        checkin(registry, job={"id": job["id"], "type": "wifi_scan", "state": "done", "words": "Found 2 networks",
                               "result": {"networks": networks}})
        page.wait_for_selector(f"{CARD} .network-choice[data-ssid='New Net']", timeout=8000)
        page.click(f"{CARD} .network-choice[data-ssid='New Net']")
        page.fill(f"{CARD} .network-password", "correct horse")
        page.click(f"{CARD} .network-join-button")
        page.wait_for_function(f"document.querySelector(\"{CARD} .network-job\").textContent.startsWith('Joining New Net: waiting for the touch Pi to take it')",
                               timeout=5000)
        job = checkin(registry)["job"]
        assert job["type"] == "wifi_connect" and job["args"] == {"ssid": "New Net", "password": "correct horse", "hidden": False}
        checkin(registry, job={"id": job["id"], "type": "wifi_connect", "state": "running",
                               "words": "Joined New Net: checking that the touch Pi can do its job on it"})
        wait_text(page, f"{CARD} .network-job", "Joining New Net: Joined New Net: checking that the touch Pi can do its job on it")
        assert page.locator(f"{CARD} .network-change").is_disabled()
        checkin(registry, job={"id": job["id"], "type": "wifi_connect", "state": "done", "words": "Joined New Net. The touch Pi reaches the room"},
                network=dict(TOUCH_NETWORK, ssid="New Net", signal=80))
        page.wait_for_function(f"document.querySelector(\"{CARD} .network-result\").textContent.startsWith('Joined New Net. The touch Pi reaches the room (')",
                               timeout=8000)
        wait_text(page, f"{CARD} .network-connection", "Wi-Fi: New Net, signal 80%")
        assert not page.locator(f"{CARD} .network-job").is_visible()
        assert fits(page)
        page.close()


def reads_of(page, path):
    seen = []
    page.on("request", lambda r: seen.append(r.url) if r.url.endswith(path) else None)
    return seen


@pytest.mark.parametrize("away_s", [60, 11 * 60])
def test_the_network_page_stops_reading_while_the_touch_pi_is_away(browser, away_s):  # noqa: F811
    registry = a_room()
    registry.queue(TOUCH, "wifi_scan")
    registry._clock.now += away_s
    with PageServer(registry=registry) as server:
        page = unlocked_page(browser, server)
        seen = reads_of(page, "/api/settings/network")
        open_network(page)
        page.wait_for_selector(CARD, timeout=5000)
        page.wait_for_timeout(3000)
        assert len(seen) == 1                                       # not every 2 seconds: every 30, or not at all
        stopped = page.locator(".network-stopped")
        if away_s > 600:
            assert stopped.is_visible()
            assert text(page, ".network-stopped .note") == "The touch Pi has not checked in for 10 minutes: this page has stopped checking on it."
            page.click(".network-again")
            page.wait_for_timeout(1500)
            assert len(seen) == 2 and stopped.is_visible()          # read once more, and still away
        else:
            assert not stopped.is_visible()
        page.close()


def test_the_software_page_stops_reading_once_the_touch_pi_has_been_away_10_minutes(browser):  # noqa: F811
    registry = a_room()
    registry.queue(TOUCH, "reboot")
    registry._clock.now += 11 * 60
    with PageServer(admin=ScriptedHelper(), github=github(), registry=registry) as server:
        page = unlocked_page(browser, server)
        seen = reads_of(page, "/api/settings/software")
        open_software(page)
        page.wait_for_selector(".software-stopped:not([hidden])", timeout=5000)
        assert text(page, ".software-stopped .note") == "The touch Pi has not checked in for 10 minutes: this page has stopped checking on it."
        page.wait_for_timeout(3000)
        assert len(seen) == 1
        checkin(registry)                                           # back: Check again reads, and the page follows its job again
        page.click(".software-again")
        page.wait_for_function("document.querySelector('.software-stopped').hidden === true", timeout=5000)
        page.wait_for_timeout(2500)
        assert len(seen) >= 3
        page.close()


def test_every_card_fits_the_touch_screen_and_a_phone(browser, tmp_path):  # noqa: F811
    with PageServer(admin=WifiHelper(tmp_path), registry=a_room()) as server:
        page = unlocked_page(browser, server)
        open_network(page)
        page.click(f"{TV} .network-change")
        page.wait_for_selector(f"{TV} .network-choice", timeout=5000)
        page.click(f"{TV} .network-choice[data-ssid='New Net']")
        page.click(f"{CARD} .network-forget")
        assert fits(page)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.close()
