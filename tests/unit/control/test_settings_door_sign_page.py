"""The door sign's cards on Settings › Software and Settings › Network (room settings spec 2026-10-29, sections 3, 6 and 7; Task 11): its
version (a version code and name), the newest GitHub release door-sign-<versionCode> and the releases between, Update (the APK fetched by
the TV Pi, then the panel's job, followed in its words), Reboot, when it last checked in; its Wi-Fi as the touch Pi's; and a panel whose
app is not its device owner, which says what it can't do. The registry is the real one; the panel's check-ins are made by the test;
GitHub is the real client on a fake transport and the APK download a fake. Driven with the Playwright Chromium."""

import time

import pytest

from croom.settings.apk import ApkStore
from croom.settings.checkin import Registry
from croom.settings.github import GitHub
from tests.unit.control.test_network_api import WifiHelper
from tests.unit.control.test_page import (  # noqa: F401  (browser is a fixture)
    PageServer,
    browser,
    fits,
)
from tests.unit.control.test_settings_page import unlocked_page
from tests.unit.control.test_settings_software_page import (
    ScriptedHelper,
    open_software,
    text,
    wait_text,
)
from tests.unit.settings.test_apk import APK, Download, a_release, url
from tests.unit.settings.test_checkin import Clock
from tests.unit.settings.test_github import NEWEST, REPO, Transport

playwright = pytest.importorskip("playwright.sync_api")

SIGN = "s" * 32
TOKEN = "k" * 43
CARD = f".device-card[data-device='{SIGN}']"
NET = f".network-card[data-device='{SIGN}']"
NOT_OWNER = "The door sign is not the panel's device owner: see its setup guide"
SIGN_NETWORK = {"address": "10.0.0.20", "wifi": True, "connection": "wifi", "ssid": "Office", "signal": 64, "saved": ["Office"],
                "change": None}


def checkin(registry, version="2", job=None, cannot=None, network=None):
    return registry.checkin({"id": SIGN, "kind": "sign", "version": version, "version_name": version + ".0", "name": "SMT1019",
                             "network": network or SIGN_NETWORK, "job": job, "cannot": cannot or {}}, TOKEN, "10.0.0.20")


def a_room(version="2", cannot=None):
    registry = Registry(None, clock=Clock(time.time()))
    checkin(registry, version, cannot=cannot)
    registry.allow(SIGN)
    return registry


def releases(tmp_path):
    items = [a_release(2), a_release(3, name="Wi-Fi from Settings"), a_release(4, name="Lock-task kiosk")]
    answers = dict(NEWEST)
    answers[f"{REPO}/releases?per_page=50"] = (200, items)
    for release in items:
        answers[f"{REPO}/releases/tags/{release['tag_name']}"] = (200, release)
    github = GitHub(fetch=Transport(answers))
    download = Download({url(n): APK for n in (2, 3, 4)})
    return github, ApkStore(github, tmp_path / "door-sign", download=download), download


def test_the_door_signs_card_and_its_update(browser, tmp_path):  # noqa: F811
    registry = a_room()
    github, apks, download = releases(tmp_path)
    with PageServer(admin=ScriptedHelper(), github=github, registry=registry, apks=apks) as server:
        page = unlocked_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_software(page)
        page.wait_for_selector(CARD, timeout=5000)
        assert text(page, f"{CARD} .software-title") == "Door signSMT1019"
        assert text(page, f"{CARD} .device-seen") == "Checked in just now"
        assert text(page, f"{CARD} .software-version") == "Version 2 (2.0), 22 Oct 2026"
        assert text(page, f"{CARD} .software-newest") == "Newest: version 4, 24 Oct 2026"
        assert [text(page, f"{CARD} .software-change >> nth={n}") for n in range(2)] == ["4\nLock-task kiosk", "3\nWi-Fi from Settings"]
        assert not page.locator(f"{CARD} .device-cannot").is_visible()
        assert page.locator(f"{CARD} .device-reboot").is_visible()
        assert fits(page)
        page.click(f"{CARD} .device-update")
        page.wait_for_function(f"document.querySelector(\"{CARD} .device-job\").textContent.startsWith("
                               "'Updating to version 4: waiting for the door sign to take it')", timeout=8000)
        assert download.asked == [url(4)]
        job = registry.get(SIGN)["job"]
        assert job["args"]["version_code"] == 4
        handed = checkin(registry)["job"]
        checkin(registry, job={"id": handed["id"], "type": "update", "state": "running", "words": "Installing version 4"})
        wait_text(page, f"{CARD} .device-job", "Updating to version 4: Installing version 4")
        checkin(registry, version="4", job={"id": handed["id"], "type": "update", "state": "done", "words": "Updated to version 4"})
        page.wait_for_function(f"document.querySelector(\"{CARD} .device-last\").textContent.startsWith('Updated to version 4 (')",
                               timeout=8000)
        wait_text(page, f"{CARD} .software-newest", "Up to date")
        assert not page.locator(f"{CARD} .device-update").is_visible()
        assert errors == []
        page.close()


def test_a_door_sign_that_is_not_the_device_owner_says_what_it_cant_do(browser, tmp_path):  # noqa: F811
    cannot = {"reboot": NOT_OWNER, "update": NOT_OWNER, "wifi_scan": NOT_OWNER, "wifi_connect": NOT_OWNER, "wifi_forget": NOT_OWNER}
    registry = a_room(cannot=cannot)
    github, apks, _ = releases(tmp_path)
    with PageServer(admin=WifiHelper(tmp_path), github=github, registry=registry, apks=apks) as server:
        page = unlocked_page(browser, server)
        open_software(page)
        page.wait_for_selector(CARD, timeout=5000)
        assert text(page, f"{CARD} .device-cannot") == NOT_OWNER
        assert not page.locator(f"{CARD} .device-update").is_visible()
        assert not page.locator(f"{CARD} .device-reboot").is_visible()
        assert page.locator(f"{CARD} .device-forget").is_visible()
        assert fits(page)
        page.close()
        page = unlocked_page(browser, server)
        page.click("#settings-menu [data-page='network']")
        page.wait_for_selector(NET, timeout=5000)
        assert text(page, f"{NET} .network-connection") == "Wi-Fi: Office, signal 64%"
        assert text(page, f"{NET} .network-cannot") == NOT_OWNER
        assert not page.locator(f"{NET} .network-change").is_visible() and not page.locator(f"{NET} .network-forget").is_visible()
        page.close()


def test_the_door_signs_wifi_through_its_jobs(browser, tmp_path):  # noqa: F811
    registry = a_room()
    with PageServer(admin=WifiHelper(tmp_path), registry=registry) as server:
        page = unlocked_page(browser, server)
        page.click("#settings-menu [data-page='network']")
        page.wait_for_selector(NET, timeout=5000)
        assert text(page, f"{NET} .software-title") == "Door signSMT1019"
        assert text(page, f"{NET} .network-seen") == "Checked in just now"
        assert not page.locator(f"{NET} .network-cannot").is_visible()
        page.click(f"{NET} .network-change")
        page.wait_for_function(f"document.querySelector(\"{NET} .network-job\").textContent.startsWith('Looking for networks')",
                               timeout=8000)
        handed = checkin(registry)["job"]
        assert handed["type"] == "wifi_scan"
        checkin(registry, job={"id": handed["id"], "type": "wifi_scan", "state": "done", "words": "Found 2 networks",
                               "result": {"networks": [{"ssid": "New Net", "signal": 80, "security": "WPA2", "open": False},
                                                       {"ssid": "Office", "signal": 64, "security": "WPA2", "in_use": True,
                                                        "saved": True}]}})
        page.wait_for_selector(f"{NET} .network-choice[data-ssid='New Net']", timeout=8000)
        page.click(f"{NET} .network-choice[data-ssid='New Net']")
        page.fill(f"{NET} .network-password", "new-password")
        page.click(f"{NET} .network-join-button")
        page.wait_for_function(f"document.querySelector(\"{NET} .network-job\").textContent.startsWith('Joining New Net')",
                               timeout=8000)
        handed = checkin(registry)["job"]
        assert handed["type"] == "wifi_connect" and handed["args"] == {"ssid": "New Net", "password": "new-password", "hidden": False}
        checkin(registry, job={"id": handed["id"], "type": "wifi_connect", "state": "failed",
                               "words": "Joined New Net, but the door sign could not reach the TV Pi within 90 seconds. "
                                        "The door sign is back on Office"})
        page.wait_for_function(f"document.querySelector(\"{NET} .network-result\").textContent.startsWith("
                               "'Joined New Net, but the door sign could not reach the TV Pi within 90 seconds. "
                               "The door sign is back on Office (')", timeout=8000)
        assert fits(page)
        page.close()
