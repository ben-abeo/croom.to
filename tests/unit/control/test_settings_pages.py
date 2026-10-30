"""Settings' other config pages (spec 2026-10-29, section 3): Calendar, Meetings, Devices, Laptop switch and AI & dashboard, each saving a
setting through the root helper; the key files (Upload, From USB stick, Zoom typed) shown as what they are and never their secrets; the
room's devices as the Devices page's choices; Test the switch; and Restart now that never sees the room go away. The helper is its real
jobs on a temporary folder (JobsAdmin); driven with the Playwright Chromium, as test_page.py is."""

import json
from datetime import datetime

import pytest
import yaml

from croom.meeting.providers.base import MeetingState
from tests.unit.control.test_laptop_api import StubLaptop
from tests.unit.control.test_page import PageServer, browser, fits  # noqa: F401  (browser is a fixture)
from tests.unit.control.test_settings_credentials_api import CONFIG, JobsAdmin
from tests.unit.control.test_settings_page import unlocked_page
from tests.unit.settings.test_credentials import GOOGLE_KEY, ZOOM, ZOOM_S2S

playwright = pytest.importorskip("playwright.sync_api")


def helper(tmp_path):
    admin = JobsAdmin(tmp_path)
    admin.config.write_text(CONFIG)
    return admin


def open_settings_page(page, key):
    page.click(f"#settings-menu [data-page='{key}']")
    page.wait_for_selector(f"#settings-page [data-form='{key}'] .setting-row", timeout=5000)


def message(page):
    return page.locator(".settings-message").inner_text()


def wait_message(page, words):
    page.wait_for_function("(w) => document.querySelector('.settings-message').textContent === w", arg=words, timeout=8000)


def config_of(admin):
    return yaml.safe_load(admin.config.read_text())


def row(page, path):
    return page.locator(f".setting-row[data-path='{path}']")


def today_in_words():
    now = datetime.now()
    return f"{now:%B} {now.day}, {now.year}"


def test_the_calendar_page_uploads_the_google_key_and_saves_the_booking_hours(browser, tmp_path):  # noqa: F811
    admin = helper(tmp_path)
    with PageServer(admin=admin, config_file=admin.config) as server:
        page = unlocked_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_settings_page(page, "calendar")
        key = row(page, "calendar.google_credentials_path")
        assert key.locator(".credential-summary").inner_text() == "Not set"
        assert [b.inner_text() for b in key.locator(".credential-actions .button").all()] == ["Upload", "From USB stick"]
        assert fits(page)
        with page.expect_file_chooser() as chooser:
            key.locator(".credential-upload").click()
        chooser.value.set_files(files=[{"name": "room-key.json", "mimeType": "application/json", "buffer": json.dumps(GOOGLE_KEY).encode()}])
        page.wait_for_function("document.querySelector('.credential-summary').textContent.startsWith('Key for')", timeout=8000)
        assert key.locator(".credential-summary").inner_text() == f"Key for room@croom.iam.gserviceaccount.com, saved {today_in_words()}"
        assert message(page) == "Saved: applies after a restart" and page.locator(".settings-restart").is_visible()
        saved = admin.paths.config_dir / "google-service-account.json"
        assert config_of(admin)["calendar"]["google_credentials_path"] == str(saved)
        assert "MIIsecret" not in page.content()
        page.select_option("#setting-calendar-booking-end", "19:00")         # a live setting: Saved
        page.click(".settings-save")
        wait_message(page, "Saved")
        assert config_of(admin)["calendar"]["booking_end"] == "19:00"
        assert config_of(admin)["calendar"]["google_credentials_path"] == str(saved)
        assert fits(page) and errors == []
        page.close()


def test_a_file_that_is_not_the_key_is_refused_under_its_row(browser, tmp_path):  # noqa: F811
    admin = helper(tmp_path)
    with PageServer(admin=admin, config_file=admin.config) as server:
        page = unlocked_page(browser, server)
        open_settings_page(page, "calendar")
        key = row(page, "calendar.google_credentials_path")
        key.locator(".credential-file").set_input_files(files=[{"name": "zoom.json", "mimeType": "application/json",
                                                               "buffer": json.dumps(ZOOM).encode()}])
        refusal = key.locator(".credential-message")
        refusal.wait_for(state="visible", timeout=8000)
        assert refusal.inner_text() == "This is a Zoom credentials file, not a Google service-account key"
        assert key.locator(".credential-summary").inner_text() == "Not set" and admin.config.read_text() == CONFIG
        page.close()


def test_a_bad_path_in_the_config_is_said_and_a_key_from_the_usb_stick_puts_it_right(browser, tmp_path):  # noqa: F811
    admin = helper(tmp_path)
    admin.config.write_text(CONFIG + "meeting:\n  zoom_credentials_path: zoom.json\n")
    admin.stick("conference-a-zoom.json", ZOOM_S2S)
    with PageServer(admin=admin, config_file=admin.config) as server:
        page = unlocked_page(browser, server)
        open_settings_page(page, "meetings")
        key = row(page, "meeting.zoom_credentials_path")
        page.wait_for_function("document.querySelector('.credential-summary').textContent.includes('upload the key again')", timeout=5000)
        assert key.locator(".credential-summary").inner_text() == "zoom.json is not a file's full path: upload the key again"
        key.locator(".credential-usb").click()
        pick = key.locator(".credential-pick")
        pick.wait_for(state="visible", timeout=5000)
        assert pick.inner_text() == "conference-a-zoom.json · KEYS"
        pick.click()
        page.wait_for_function("document.querySelector('.credential-summary').textContent.startsWith('Zoom')", timeout=8000)
        assert key.locator(".credential-summary").inner_text().startswith("Zoom Meeting SDK keys, joining as room@crystalpm.com, saved ")
        assert not key.locator(".credential-list").is_visible()
        assert config_of(admin)["meeting"]["zoom_credentials_path"] == str(admin.paths.config_dir / "zoom-credentials.json")
        page.close()


def test_no_key_on_a_usb_stick_is_said(browser, tmp_path):  # noqa: F811
    admin = helper(tmp_path)
    with PageServer(admin=admin, config_file=admin.config) as server:
        page = unlocked_page(browser, server)
        open_settings_page(page, "calendar")
        page.click(".credential-usb")
        page.wait_for_function("document.querySelector('.credential-list').textContent.startsWith('No key files')", timeout=5000)
        page.close()


def test_the_meetings_page_takes_typed_zoom_keys_and_saves_a_switch(browser, tmp_path):  # noqa: F811
    admin = helper(tmp_path)
    with PageServer(admin=admin, config_file=admin.config) as server:
        page = unlocked_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_settings_page(page, "meetings")
        assert fits(page)                                                  # the long page scrolls in its own box
        assert page.locator(".settings-save").is_visible()                 # and Save stays in sight at its foot
        key = row(page, "meeting.zoom_credentials_path")
        key.locator(".credential-type").click()
        assert key.locator(".credential-secret").get_attribute("type") == "password"
        assert key.locator(".credential-key").get_attribute("data-keyboard") == "setting"
        key.locator(".credential-save-typed").click()                    # blank: refused in words
        page.wait_for_function("document.querySelector('.credential-message').textContent !== ''", timeout=5000)
        assert "can't be blank" in key.locator(".credential-message").inner_text()
        key.locator(".credential-key").fill("typedid")
        key.locator(".credential-secret").fill("typedsecret")
        key.locator(".credential-save-typed").click()
        page.wait_for_function("document.querySelector('.credential-summary').textContent.startsWith('Zoom')", timeout=8000)
        assert not key.locator(".credential-typed").is_visible() and key.locator(".credential-message").inner_text() == ""
        assert json.loads((admin.paths.config_dir / "zoom-credentials.json").read_text()) == {"sdk_client_id": "typedid",
                                                                                              "sdk_client_secret": "typedsecret"}
        assert message(page) == "Saved: applies after a restart"
        page.click("#setting-meeting-mic-default-on")
        page.click(".settings-save")
        wait_message(page, "Saved: applies after a restart")
        assert config_of(admin)["meeting"]["mic_default_on"] is False                 # on by default: turned off
        assert errors == []
        page.close()


def test_the_devices_page_offers_the_rooms_devices_and_saves_the_speaker(browser, tmp_path):  # noqa: F811
    admin = helper(tmp_path)
    admin.config.write_text(CONFIG + "video:\n  device: /dev/video4\n")

    async def found():
        return {"audio.output_device": [("alsa_output.usb-meetup", "Logitech MeetUp Analog Stereo"), ("alsa_output.hdmi", "Built-in HDMI")],
                "audio.input_device": [("alsa_input.usb-meetup", "Logitech MeetUp Mic")],
                "video.device": [("/dev/video0", "Logitech MeetUp (/dev/video0)")]}

    with PageServer(admin=admin, config_file=admin.config, find_devices=found) as server:
        page = unlocked_page(browser, server)
        open_settings_page(page, "devices")
        speaker = page.locator("#setting-audio-output-device option")
        assert [o.inner_text() for o in speaker.all()] == ["Automatic", "Logitech MeetUp Analog Stereo", "Built-in HDMI"]
        assert page.input_value("#setting-audio-output-device") == "auto"
        assert page.input_value("#setting-video-device") == "/dev/video4"           # unplugged now, and kept
        assert [o.inner_text() for o in page.locator("#setting-video-device option").all()] == [
            "Automatic", "Logitech MeetUp (/dev/video0)", "/dev/video4 (not connected now)"]
        assert fits(page)
        page.select_option("#setting-audio-output-device", "alsa_output.usb-meetup")
        page.click(".settings-save")
        wait_message(page, "Saved: applies after a restart")
        data = config_of(admin)
        assert data["audio"]["output_device"] == "alsa_output.usb-meetup" and data["video"]["device"] == "/dev/video4"
        page.close()


class SwitchLaptop(StubLaptop):
    def __init__(self):
        super().__init__()
        self.tests = 0

    async def test_switch(self):
        self.tests += 1
        return {"ok": True, "lines": ["Pin: GPIO17 (header pin 11), active high, held 200 ms", "GPIO chip: /dev/gpiochip4",
                                      "Pressed: the MeetUp is now on the laptop side (2.1 s).",
                                      "Pressed again: the MeetUp is back on the Pi (2.0 s)."]}


def test_the_laptop_page_saves_the_pin_and_tests_the_switch(browser, tmp_path):  # noqa: F811
    admin = helper(tmp_path)
    laptop = SwitchLaptop()
    with PageServer(admin=admin, config_file=admin.config, laptop=laptop) as server:
        page = unlocked_page(browser, server)
        open_settings_page(page, "laptop")
        page.fill("#setting-laptop-gpio-pin", "22")
        page.click(".settings-save")
        wait_message(page, "Saved: applies after a restart")
        assert config_of(admin)["laptop"]["gpio_pin"] == 22
        page.click(".laptop-test")
        page.wait_for_function("document.querySelector('.laptop-test-verdict') !== null", timeout=8000)
        assert page.locator(".laptop-test-verdict").inner_text() == "The switch works"
        assert page.locator(".laptop-test-line").nth(3).inner_text() == "Pressed again: the MeetUp is back on the Pi (2.0 s)."
        assert laptop.tests == 1 and fits(page)
        page.close()


def test_test_the_switch_in_a_meeting_or_a_room_without_it_says_why(browser, tmp_path):  # noqa: F811
    admin = helper(tmp_path)
    with PageServer(admin=admin, config_file=admin.config) as server:          # no Laptop service: the switch is off in this room
        page = unlocked_page(browser, server, stopped=True)
        open_settings_page(page, "laptop")
        page.click(".laptop-test")
        page.wait_for_function("document.querySelector('.laptop-test-verdict') !== null", timeout=8000)
        assert page.locator(".laptop-test-verdict").inner_text() == (
            "The Laptop switch is off: turn it on, save, and restart the room software to test it")
        server._loop.call_soon_threadsafe(server.meeting.set_state, MeetingState.CONNECTED)
        page.click(".laptop-test")
        page.wait_for_function("(document.querySelector('.laptop-test-verdict') || {}).textContent === 'Leave the meeting first'",
                               timeout=8000)
        page.close()


def test_the_ai_page_saves_a_switch(browser, tmp_path):  # noqa: F811
    admin = helper(tmp_path)
    with PageServer(admin=admin, config_file=admin.config) as server:
        page = unlocked_page(browser, server)
        open_settings_page(page, "ai")
        assert [o.get_attribute("value") for o in page.locator("#setting-ai-backend option").all()] == [
            "auto", "hailo", "coral", "nvidia", "intel", "cpu"]
        page.click("#setting-ai-noise-reduction")
        page.click(".settings-save")
        wait_message(page, "Saved: applies after a restart")
        assert config_of(admin)["ai"]["noise_reduction"] is False
        assert fits(page)
        page.close()


def test_restart_now_that_never_sees_the_room_go_away_says_it_did_not_restart(browser, tmp_path):  # noqa: F811
    admin = helper(tmp_path)                                                   # its restart does nothing: the room never goes away
    with PageServer(admin=admin, config_file=admin.config) as server:
        page = unlocked_page(browser, server, stopped=True)
        open_settings_page(page, "ai")
        page.click("#setting-ai-noise-reduction")
        page.click(".settings-save")
        wait_message(page, "Saved: applies after a restart")
        page.click(".settings-restart")
        wait_message(page, "Restarting the room software…")
        for _ in range(152):                                               # 5 minutes of looks, two seconds apart, the room up at each
            with page.expect_response(lambda r: r.url.endswith("/api/settings/restart") and r.request.method == "GET", timeout=5000):
                page.clock.run_for(2000)
        wait_message(page, "The room software did not restart")
        assert page.locator(".settings-restart").is_visible()                # for another go
        assert page.evaluate("document.body.dataset.view") == "settings"     # the looks kept Settings unlocked meanwhile
        assert [job for job, _ in admin.calls].count("restart") == 1
        page.close()


def test_the_tv_page_opens_options_tv(browser):  # noqa: F811
    with PageServer() as server:
        page = unlocked_page(browser, server)
        page.click("#settings-menu [data-page='tv']")
        page.wait_for_function("document.body.dataset.view === 'tv'", timeout=5000)
        page.close()


def test_a_key_files_row_fits_a_phone_with_no_sideways_scroll(browser, tmp_path):  # noqa: F811
    admin = helper(tmp_path)
    admin.stick("a-very-long-name-for-the-conference-room-zoom-credentials-file.json", ZOOM)
    with PageServer(admin=admin, config_file=admin.config) as server:
        page = unlocked_page(browser, server)
        page.set_viewport_size({"width": 390, "height": 844})
        open_settings_page(page, "meetings")
        key = row(page, "meeting.zoom_credentials_path")
        key.locator(".credential-usb").click()
        key.locator(".credential-pick").wait_for(state="visible", timeout=5000)
        key.locator(".credential-type").click()
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.close()
