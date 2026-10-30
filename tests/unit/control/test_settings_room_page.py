"""Settings › Room and the form every config page is drawn with (spec 2026-10-29, sections 3 and 4): the values from config.yaml, a save
through the root helper (a fake that writes the file as the helper would), the words beside a refused value, "Saved: applies after a
restart" with Restart now, Restart now refused in a meeting, the undo banner, and a room without the helper. Driven with the Playwright
Chromium, as test_page.py is."""

import yaml
import pytest

from croom.meeting.providers.base import MeetingState
from tests.unit.control.test_page import PageServer, browser, fits  # noqa: F401  (browser is a fixture)
from tests.unit.control.test_settings_page import unlocked_page
from tests.unit.settings.test_schema import FakeAdmin

playwright = pytest.importorskip("playwright.sync_api")

CONFIG = "room:\n  name: Lab\n  location: 2nd floor\n  timezone: Europe/London\ntv:\n  host: 10.0.0.40\n"


def room_server(tmp_path, admin=None):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(CONFIG)
    admin = admin if admin is not None else FakeAdmin(config_file)
    return PageServer(admin=admin, config_file=config_file), admin, config_file


def open_room(page):
    page.click("#settings-menu [data-page='room']")
    page.wait_for_selector("#settings-page .setting-row", timeout=5000)


def jobs(admin):
    """What the page asked the helper to do (it reads the last restart's record as well, each time a form opens)."""
    return [job for job, _ in admin.calls if job != "restart_status"]


def message(page):
    return page.locator(".settings-message").inner_text()


def test_the_room_page_shows_the_config_and_saves_the_name_for_after_a_restart(browser, tmp_path):  # noqa: F811
    server, admin, config_file = room_server(tmp_path)
    with server:
        page = unlocked_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_room(page)
        assert page.locator("#settings-title").inner_text() == "Room"
        assert page.input_value("#setting-room-name") == "Lab"
        assert page.input_value("#setting-room-location") == "2nd floor"
        assert page.input_value("#setting-room-company") == "Crystal Practice Management"
        assert page.input_value("#setting-room-timezone") == "Europe/London"
        assert page.locator("#setting-room-timezone option").count() > 300          # the system's zones
        assert page.locator("#setting-room-name").get_attribute("data-keyboard") == "setting"
        assert not page.locator(".settings-restart").is_visible()
        assert fits(page)
        page.fill("#setting-room-name", "Boardroom")
        page.click(".settings-save")
        page.wait_for_function("document.querySelector('.settings-message').textContent !== ''", timeout=5000)
        assert message(page) == "Saved: applies after a restart"
        assert page.locator(".settings-restart").is_visible() and page.locator(".settings-restart").inner_text() == "Restart now"
        data = yaml.safe_load(config_file.read_text())
        assert data["room"]["name"] == "Boardroom" and data["tv"]["host"] == "10.0.0.40"   # the rest of the file kept
        assert jobs(admin) == ["write_config"]
        page.click(".settings-save")                                                    # nothing changed since
        assert message(page) == "Nothing to save: nothing has changed"
        assert fits(page)
        assert errors == []
        page.close()


def test_a_refused_value_says_why_beside_it_and_nothing_is_saved(browser, tmp_path):  # noqa: F811
    server, admin, config_file = room_server(tmp_path)
    with server:
        page = unlocked_page(browser, server)
        open_room(page)
        page.fill("#setting-room-name", "   ")
        page.click(".settings-save")
        error = page.locator(".setting-row[data-path='room.name'] .setting-error")
        error.wait_for(state="visible", timeout=5000)
        assert error.inner_text() == "Name can't be blank"
        assert message(page) == "Not saved: see the settings marked"
        assert jobs(admin) == [] and config_file.read_text() == CONFIG
        page.fill("#setting-room-name", "Lab B")                                       # put right: the words go
        page.click(".settings-save")
        page.wait_for_function("document.querySelector('.settings-message').textContent.startsWith('Saved')", timeout=5000)
        assert not error.is_visible()
        page.close()


def test_restart_now_restarts_the_room_and_is_refused_in_a_meeting(browser, tmp_path):  # noqa: F811
    server, admin, config_file = room_server(tmp_path)
    with server:
        page = unlocked_page(browser, server, stopped=True)   # no status poll moves the page to the meeting screen meanwhile
        open_room(page)
        page.fill("#setting-room-location", "Floor 3")
        page.click(".settings-save")
        page.wait_for_function("document.querySelector('.settings-message').textContent === 'Saved: applies after a restart'", timeout=5000)
        server._loop.call_soon_threadsafe(server.meeting.set_state, MeetingState.CONNECTED)
        page.click(".settings-restart")
        page.wait_for_function("document.querySelector('.settings-message').textContent === 'Leave the meeting first'", timeout=5000)
        assert jobs(admin) == ["write_config"]
        server._loop.call_soon_threadsafe(server.meeting.set_state, MeetingState.IDLE)
        page.click(".settings-restart")
        page.wait_for_function("document.querySelector('.settings-message').textContent === 'Restarting the room software\u2026'", timeout=5000)
        assert jobs(admin) == ["write_config", "restart"]
        page.close()


def test_the_page_says_when_the_last_change_was_undone(browser, tmp_path):  # noqa: F811
    admin = FakeAdmin()
    real = admin.call

    async def call(job, **args):
        if job == "restart_status":
            return {"ok": True, "state": "undone", "undone": True, "why": "The room did not answer within 2 minutes"}
        return await real(job, **args)

    admin.call = call
    server, _, _ = room_server(tmp_path, admin)
    with server:
        page = unlocked_page(browser, server)
        open_room(page)
        banner = page.locator(".settings-banner")
        banner.wait_for(state="visible", timeout=5000)
        assert banner.inner_text() == "Your last change was undone: the room did not start with it"
        assert fits(page)
        page.close()


def test_without_the_helper_the_room_page_is_not_available_on_this_device(browser):  # noqa: F811
    with PageServer() as server:
        page = unlocked_page(browser, server)
        page.click("#settings-menu [data-page='room']")
        page.wait_for_function("document.querySelector('.settings-loading') && "
                               "document.querySelector('.settings-loading').textContent === 'Not available on this device'", timeout=5000)
        page.click("#back-from-settings")                                              # the other pages work
        page.click("#settings-menu [data-page='security']")
        page.wait_for_selector("#settings-page .pin", timeout=5000)
        page.close()


def test_the_room_page_fits_a_phone_in_one_column(browser, tmp_path):  # noqa: F811
    server, _, _ = room_server(tmp_path)
    with server:
        page = unlocked_page(browser, server)
        page.set_viewport_size({"width": 390, "height": 844})
        open_room(page)
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        label, field = (page.locator(".setting-row[data-path='room.name'] " + s).bounding_box() for s in (".setting-label", "input"))
        assert field["y"] > label["y"] + label["height"] - 1                           # the field under its name
        page.close()


def draw_form(page, key):
    """A config page drawn with the form the later pages use (their menu items say Coming soon until they are built)."""
    if page.locator("#settings-menu").is_visible():
        page.click("#settings-menu [data-page='room']")
        page.wait_for_selector("#settings-page .setting-row", timeout=5000)
    page.evaluate("(key) => window.croomSettings.form(document.getElementById('settings-page'), key)", key)
    page.wait_for_selector(f"#settings-page [data-form='{key}'] .setting-row", timeout=5000)


def save_and_wait(page):
    page.click(".settings-save")
    page.wait_for_function("document.querySelector('.settings-message').textContent !== ''", timeout=5000)
    return message(page)


def test_the_form_draws_every_kind_of_setting_and_sends_what_changed(browser, tmp_path):  # noqa: F811
    server, admin, config_file = room_server(tmp_path)
    with server:
        page = unlocked_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        draw_form(page, "meetings")                                      # a list with choices, switches, free lists, a key file
        zoom = page.locator(".setting-choice[data-value='zoom']")
        webex = page.locator(".setting-choice[data-value='webex']")
        assert zoom.get_attribute("aria-pressed") == "true" and webex.get_attribute("aria-pressed") == "false"
        webex.click()
        kiosk = page.locator("#setting-meeting-kiosk")
        assert kiosk.get_attribute("role") == "switch" and kiosk.get_attribute("aria-checked") == "true"
        kiosk.click()
        reactions = page.locator(".setting-row[data-path='meeting.controls.reactions']")
        assert reactions.locator("input").count() == 6
        reactions.locator(".setting-remove").first.click()                # Thumbs Up goes
        reactions.locator(".setting-add").click()
        reactions.locator("input").last.fill("Wave")
        assert page.locator(".setting-row[data-path='meeting.zoom_credentials_path'] .credential-summary").inner_text() == "Not set"
        assert save_and_wait(page) == "Saved: applies after a restart"
        meeting = yaml.safe_load(config_file.read_text())["meeting"]
        assert meeting["platforms"] == ["zoom", "google_meet", "teams", "webex"] and meeting["kiosk"] is False
        assert meeting["controls"]["reactions"] == ["Clap", "Heart", "Joy", "Open Mouth", "Tada", "Wave"]
        assert "dialogs" not in meeting and "zoom_credentials_path" not in meeting   # only what changed is written

        draw_form(page, "calendar")                                      # times on the quarter hour: live
        assert page.input_value("#setting-calendar-booking-start") == "08:00"
        assert page.locator("#setting-calendar-booking-start option").count() == 96
        page.select_option("#setting-calendar-booking-start", "07:45")
        assert save_and_wait(page) == "Saved"
        assert page.locator(".settings-restart").is_visible()            # the meetings change still waits for one

        draw_form(page, "laptop")                                        # a number typed as text: refused in words beside it
        page.fill("#setting-laptop-gpio-pin", "40")
        page.fill("#setting-laptop-press-ms", "soon")
        assert save_and_wait(page) == "Not saved: see the settings marked"
        assert page.locator(".setting-row[data-path='laptop.gpio_pin'] .setting-error").inner_text() == "GPIO pin must be from 0 to 27"
        assert page.locator(".setting-row[data-path='laptop.press_ms'] .setting-error").inner_text() == "Press length (ms) must be a number"
        page.fill("#setting-laptop-gpio-pin", "22")
        page.fill("#setting-laptop-press-ms", "300")
        save_and_wait(page)
        assert yaml.safe_load(config_file.read_text())["laptop"] == {"gpio_pin": 22, "press_ms": 300}

        draw_form(page, "ai")                                            # a choice and a secret, never shown
        assert page.input_value("#setting-dashboard-enrollment-token") == ""
        assert page.locator("#setting-dashboard-enrollment-token").get_attribute("type") == "password"
        page.select_option("#setting-ai-backend", "cpu")
        page.fill("#setting-dashboard-enrollment-token", "tok-123")
        save_and_wait(page)
        data = yaml.safe_load(config_file.read_text())
        assert data["ai"] == {"backend": "cpu"} and data["dashboard"] == {"enrollment_token": "tok-123"}
        assert page.input_value("#setting-dashboard-enrollment-token") == ""
        assert "type a new one" in page.locator("#setting-dashboard-enrollment-token").get_attribute("placeholder")
        assert errors == []
        page.close()


def restarting_admin(config_file):
    """A helper whose record says the last change was undone once a restart has been asked for (the watch put config.yaml.1 back)."""
    admin = FakeAdmin(config_file)
    real = admin.call

    async def call(job, **args):
        if job == "restart_status":
            undone = any(j == "restart" for j, _ in admin.calls)
            return {"ok": True, "state": "undone" if undone else "none", "undone": undone}
        return await real(job, **args)

    admin.call = call
    return admin


def test_restart_now_waits_for_the_room_to_come_back_then_reads_how_the_restart_went(browser, tmp_path):  # noqa: F811
    config_file = tmp_path / "config.yaml"
    config_file.write_text(CONFIG)
    admin = restarting_admin(config_file)
    with PageServer(admin=admin, config_file=config_file) as server:
        page = unlocked_page(browser, server, stopped=True)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        away = {"now": False}
        page.route("**/api/status", lambda route: route.abort() if away["now"] else route.continue_())
        open_room(page)
        page.fill("#setting-room-name", "Boardroom")
        page.click(".settings-save")
        page.wait_for_function("document.querySelector('.settings-message').textContent === 'Saved: applies after a restart'", timeout=5000)
        page.click(".settings-restart")
        page.wait_for_function("document.querySelector('.settings-message').textContent === 'Restarting the room software\u2026'", timeout=5000)
        assert not page.locator(".settings-restart").is_visible()
        away["now"] = True                                   # the room software has stopped
        page.clock.run_for(2500)
        page.wait_for_timeout(200)
        assert message(page) == "Restarting the room software\u2026"   # a request that fails now is the restart, not an error
        assert not page.locator(".settings-banner").is_visible()
        away["now"] = False                                  # and it is back
        page.clock.run_for(2500)
        page.wait_for_function("document.querySelector('.settings-message').textContent === 'The room software has restarted'", timeout=5000)
        page.locator(".settings-banner").wait_for(state="visible", timeout=5000)
        assert errors == []
        page.close()


def test_restart_now_whose_answer_is_lost_still_says_the_room_is_restarting(browser, tmp_path):  # noqa: F811
    server, admin, _ = room_server(tmp_path)
    with server:
        page = unlocked_page(browser, server)
        open_room(page)
        page.fill("#setting-room-location", "Floor 3")
        page.click(".settings-save")
        page.wait_for_function("document.querySelector('.settings-message').textContent === 'Saved: applies after a restart'", timeout=5000)
        page.route("**/api/settings/restart", lambda route: route.abort() if route.request.method == "POST" else route.continue_())
        page.click(".settings-restart")
        page.wait_for_function("document.querySelector('.settings-message').textContent === 'Restarting the room software\u2026'", timeout=5000)
        page.close()


def test_a_refusal_held_on_another_page_names_that_page(browser, tmp_path):  # noqa: F811
    server, admin, config_file = room_server(tmp_path)
    config_file.write_text(CONFIG + "ai:\n  backend: quantum\n")       # hand-written, and not a backend the room has
    with server:
        page = unlocked_page(browser, server)
        open_room(page)
        page.fill("#setting-room-name", "Boardroom")
        page.click(".settings-save")
        page.wait_for_function("document.querySelector('.settings-message').textContent !== ''", timeout=5000)
        assert message(page) == "AI & dashboard: AI backend: choose one of the list"
        assert jobs(admin) == []
        page.close()
