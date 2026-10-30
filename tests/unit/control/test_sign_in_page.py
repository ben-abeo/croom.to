"""Settings › Meetings › Sign in to Google, on the room page (spec 2026-10-29, section 3): the row says whom the room is signed in as; Sign in
to Google turns the screen into the touchpad for the room Pi, with Type on the TV (the page's keyboard, its keys reaching the room Pi's
keyboard through the pointer socket), Cancel and Done; a sign-in the agent ends by itself closes the touchpad; a room with no Google
profile folder is offered the room's own. The meeting service is the stub; driven with the Playwright Chromium, as test_page.py is."""

import pytest
import yaml

from croom.meeting.signin_session import LEAVE_FIRST, SignInError
from tests.unit.control.test_page import PageServer, browser, fits, touch, touch_event  # noqa: F401  (browser is a fixture)
from tests.unit.control.test_service import StubKeyboard, StubMouse
from tests.unit.control.test_settings_credentials_api import CONFIG, JobsAdmin
from tests.unit.control.test_settings_page import unlocked_page

playwright = pytest.importorskip("playwright.sync_api")

ROW = ".setting-row[data-path='meeting.google_profile_dir']"
SURFACE = "#touchpad-surface"


def helper(tmp_path):
    admin = JobsAdmin(tmp_path)
    admin.config.write_text(CONFIG + "meeting:\n  google_profile_dir: /var/lib/croom/meet-profile\n")
    return admin


def meetings_page(browser, server):  # noqa: F811
    page = unlocked_page(browser, server)
    page.click("#settings-menu [data-page='meetings']")
    page.wait_for_selector(f"#settings-page {ROW}", timeout=5000)
    return page


def wait_status(page, words):
    page.wait_for_function("(w) => document.querySelector('.signin-status').textContent === w", arg=words, timeout=8000)


def wait_until(page, predicate, timeout=5.0):
    """A condition on the server's side (the stubs), checked while the page goes on."""
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        page.wait_for_timeout(50)
    raise AssertionError("never happened")


def press(page, key):
    touch(page, f"#keyboard .key[data-key='{key}']")


def open_sign_in(page):
    page.locator(f"{ROW} .signin-start").click()
    page.wait_for_function("!document.getElementById('touchpad').hidden", timeout=8000)


def test_sign_in_opens_the_touchpad_for_the_room_pi_and_done_says_who_the_room_is_signed_in_as(browser, tmp_path):  # noqa: F811
    admin = helper(tmp_path)
    mouse = StubMouse()
    with PageServer(admin=admin, config_file=admin.config, mouse=mouse, keyboard=StubKeyboard()) as server:
        page = meetings_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        row = page.locator(ROW)
        wait_status(page, "Not signed in")
        assert row.locator(".signin-start").inner_text() == "Sign in to Google" and not row.locator(".signin-setup").is_visible()
        open_sign_in(page)
        assert server.meeting.sign_ins == ["begin"]
        assert page.locator("#touchpad-title").inner_text() == "Sign in to Google on the TV"
        assert page.locator("#touchpad-sub").inner_text().startswith("Google's sign-in is on the TV")
        assert not page.locator("#touchpad-target").is_visible()                  # the room Pi alone: Google's page is on it
        assert [page.locator(f"#touchpad-{b}").inner_text().strip() for b in ("keys", "cancel", "done")] == ["Type on the TV", "Cancel", "Done"]
        assert fits(page)
        touch_event(page, SURFACE, "down", 400, 400)                              # a tap: the room Pi's mouse clicks
        touch_event(page, SURFACE, "up", 400, 400)
        wait_until(page, lambda: mouse.calls == [("click", "left")])
        page.click("#touchpad-done")
        page.wait_for_function("document.getElementById('touchpad').hidden", timeout=8000)
        assert server.meeting.sign_ins == ["begin", "done"]
        page.wait_for_function("document.querySelector('.signin-message').textContent.startsWith('Signed in as')", timeout=5000)
        assert row.locator(".signin-message").inner_text() == "Signed in as conference-a@crystalpm.com"
        wait_status(page, "Signed in as conference-a@crystalpm.com")
        assert page.evaluate("document.body.dataset.view") == "settings" and errors == []
        page.close()


def test_type_on_the_tv_brings_the_keyboard_over_the_touchpad_and_its_keys_reach_the_room_pi(browser, tmp_path):  # noqa: F811
    admin = helper(tmp_path)
    keyboard = StubKeyboard()
    with PageServer(admin=admin, config_file=admin.config, mouse=StubMouse(), keyboard=keyboard) as server:
        page = meetings_page(browser, server)
        open_sign_in(page)
        page.click("#touchpad-keys")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        assert page.locator("#touchpad-keys").get_attribute("aria-pressed") == "true"
        assert page.locator("#keyboard .key[data-key='join']").inner_text() == "Enter"
        press(page, "shift")
        press(page, "r")                                    # a capital, then Shift is spent
        press(page, "o")
        press(page, "layer:symbols")
        press(page, "@")
        press(page, "layer:letters")
        press(page, "x")
        press(page, "backspace")
        press(page, "space")
        press(page, "join")
        wait_until(page, lambda: keyboard.typed() == "Ro@x<Backspace> <Enter>")
        assert page.locator("#touchpad").is_visible() and page.locator("#keyboard").is_visible()   # Enter types, and the keys stay up
        assert fits(page)
        press(page, "hide")
        page.wait_for_function("document.getElementById('keyboard').hidden", timeout=5000)
        assert page.locator("#touchpad-keys").get_attribute("aria-pressed") == "false"
        page.click("#touchpad-keys")                        # up again, still typing on the TV
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        press(page, "q")
        wait_until(page, lambda: keyboard.typed().endswith("q"))
        page.click("#touchpad-cancel")
        page.wait_for_function("document.getElementById('touchpad').hidden && document.getElementById('keyboard').hidden", timeout=8000)
        page.wait_for_function("document.querySelector('.signin-message').textContent !== ''", timeout=5000)
        assert page.locator(f"{ROW} .signin-message").inner_text() == "Sign-in cancelled: nothing changed"
        assert server.meeting.sign_ins == ["begin", "cancel"]
        # the keys type into the page's fields again, not on the TV
        page.locator(".credential-type").click()
        page.locator(".credential-key").focus()
        page.evaluate("window.croomKeyboard.open()")
        press(page, "k")
        assert page.locator(".credential-key").input_value() == "k" and keyboard.typed().endswith("q")
        page.close()


def test_a_sign_in_the_agent_ends_by_itself_closes_the_touchpad_and_says_why(browser, tmp_path):  # noqa: F811
    admin = helper(tmp_path)
    with PageServer(admin=admin, config_file=admin.config, mouse=StubMouse(), keyboard=StubKeyboard()) as server:
        page = meetings_page(browser, server)
        open_sign_in(page)
        server.meeting.sign_in_ends_by_itself()
        page.wait_for_function("document.getElementById('touchpad').hidden", timeout=10000)
        page.wait_for_function("document.querySelector('.signin-message').textContent !== ''", timeout=5000)
        assert page.locator(f"{ROW} .signin-message").inner_text() == "The sign-in closed after 15 minutes without a touch: nothing changed"
        assert page.evaluate("document.body.dataset.view") == "settings"
        page.close()


def test_a_sign_in_that_is_refused_is_said_under_the_row(browser, tmp_path):  # noqa: F811
    admin = helper(tmp_path)
    with PageServer(admin=admin, config_file=admin.config, mouse=StubMouse(), keyboard=StubKeyboard()) as server:
        server.meeting.sign_in_refusal = SignInError(LEAVE_FIRST)
        page = meetings_page(browser, server)
        wait_status(page, "Not signed in")
        page.locator(f"{ROW} .signin-start").click()
        page.wait_for_function("document.querySelector('.signin-message').textContent !== ''", timeout=5000)
        assert page.locator(f"{ROW} .signin-message").inner_text() == "Leave the meeting first"
        assert page.locator("#touchpad").is_hidden()
        page.close()


def test_a_page_reloaded_during_a_sign_in_offers_to_go_back_to_it(browser, tmp_path):  # noqa: F811
    admin = helper(tmp_path)
    with PageServer(admin=admin, config_file=admin.config, mouse=StubMouse(), keyboard=StubKeyboard()) as server:
        server.meeting._sign_ins()
        server.meeting.signing_in = True
        page = meetings_page(browser, server)
        wait_status(page, "Signing in on the TV")
        assert page.locator(f"{ROW} .signin-start").inner_text() == "Back to the sign-in"
        open_sign_in(page)
        page.click("#touchpad-done")
        page.wait_for_function("document.getElementById('touchpad').hidden", timeout=8000)
        wait_status(page, "Signed in as conference-a@crystalpm.com")
        page.close()


def test_a_room_with_no_google_profile_folder_is_offered_the_rooms_own(browser, tmp_path):  # noqa: F811
    admin = JobsAdmin(tmp_path)
    admin.config.write_text(CONFIG)
    with PageServer(admin=admin, config_file=admin.config, mouse=StubMouse(), keyboard=StubKeyboard()) as server:
        server.meeting.google_profile = None
        page = meetings_page(browser, server)
        row = page.locator(ROW)
        wait_status(page, "Set up Google Meet first: the room keeps its Google sign-in in a folder of its own")
        assert not row.locator(".signin-start").is_visible()
        setup = row.locator(".signin-setup")
        assert setup.inner_text() == "Use /var/lib/croom/meet-profile"
        setup.click()
        page.wait_for_function("document.querySelector('.settings-message').textContent === 'Saved: applies after a restart'", timeout=8000)
        assert page.locator(".settings-restart").is_visible()
        assert yaml.safe_load(admin.config.read_text())["meeting"]["google_profile_dir"] == "/var/lib/croom/meet-profile"
        wait_status(page, "The room starts using /var/lib/croom/meet-profile for its Google sign-in after a restart")
        assert server.meeting._sign_ins() == []                 # (no sign-in was asked for: the room needs the restart first)
        page.close()
