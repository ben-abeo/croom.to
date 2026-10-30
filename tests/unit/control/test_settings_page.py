"""The Settings area of the room page (spec 2026-10-29, section 3): Options › Settings, the PIN pad (choosing a PIN the first time, with
a confirm), the Settings menu with Lock and the minutes left, the pages still to come, Security (change the PIN), and the way back to
the PIN pad when the token is refused or runs out. Driven with the Playwright Chromium, as test_page.py is."""

import json
import urllib.request
from pathlib import Path

import pytest

from croom.settings.pin import PinStore
from tests.unit.control.test_page import PageServer, RemoteTv, browser, fits, open_page, page_on_a_stopped_clock  # noqa: F401  (browser is a fixture)

playwright = pytest.importorskip("playwright.sync_api")

PAGES = ["Room", "Calendar", "Meetings", "Devices", "TV", "Laptop switch", "AI & dashboard", "Network", "Check", "Logs", "Software",
         "Security"]


def pins_of(server):
    """The server's PIN file, as a store of its own: the file is read again at every use, so a PIN set here is the room's."""
    return PinStore(Path(server._settings_dir.name) / "settings-pin.json")


def pin_state(server, token=""):
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/settings/pin", headers={"X-Croom-Unlock": token})
    with urllib.request.urlopen(request, timeout=5) as answer:
        return json.load(answer)


def view(page):
    return page.evaluate("document.body.dataset.view")


def token(page):
    return page.evaluate("sessionStorage.getItem('settings-unlock')")


def open_settings(page):
    page.click("#open-options")
    page.click("#open-settings")
    page.wait_for_function("document.body.dataset.view === 'settings-pin' || document.body.dataset.view === 'settings'", timeout=5000)


def tap(page, digits):
    for d in digits:
        page.click(f"#pin-pad [data-digit='{d}']")


def enter(page, digits):
    tap(page, digits)
    page.click("#pin-pad [data-key='ok']")


def wait_title(page, words):
    page.wait_for_function("(w) => document.getElementById('pin-title').textContent === w", arg=words, timeout=5000)


def unlocked_page(browser, server, pin="2468", stopped=False):  # noqa: F811
    """A page at the Settings menu, unlocked with `pin` (chosen beforehand at the TV Pi's file); `stopped`: on a stopped clock."""
    if not pins_of(server).is_set():
        pins_of(server).set_pin(pin)
    page = page_on_a_stopped_clock(browser, server) if stopped else open_page(browser, server)
    open_settings(page)
    wait_title(page, "Enter the PIN")
    enter(page, pin)
    page.wait_for_function("document.body.dataset.view === 'settings'", timeout=5000)
    return page


def test_options_has_settings_with_its_icon_and_back_returns_to_options(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        page.click("#open-options")
        item = page.locator("#open-settings")
        assert item.is_visible() and item.inner_text().strip() == "Settings"
        assert item.locator("svg.icon use").get_attribute("href") == "#icon-settings"
        item.click()
        page.wait_for_function("document.body.dataset.view === 'settings-pin'", timeout=5000)
        page.click("#back-from-settings-pin")
        assert view(page) == "options"
        page.close()


def test_the_first_time_settings_asks_to_choose_a_pin_and_to_enter_it_again(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_settings(page)
        wait_title(page, "Choose a PIN")
        assert "4 to 8 digits" in page.locator("#pin-hint").inner_text()
        tap(page, "123")
        assert page.locator("#pin-pad [data-key='ok']").is_disabled()           # three digits are not a PIN
        assert page.locator("#pin-dots .dot.on").count() == 3
        tap(page, "4")
        page.click("#pin-pad [data-key='ok']")
        wait_title(page, "Enter the same PIN again")
        assert page.locator("#pin-dots .dot.on").count() == 0
        enter(page, "1235")                                                     # not the same: back to choosing, saying why
        wait_title(page, "Choose a PIN")
        assert page.locator("#pin-message").inner_text() == "The two PINs were not the same: choose a PIN again"
        assert not pins_of(server).is_set()
        enter(page, "1234")
        wait_title(page, "Enter the same PIN again")
        enter(page, "1234")
        page.wait_for_function("document.body.dataset.view === 'settings'", timeout=5000)
        assert pins_of(server).is_set()
        assert pin_state(server, token(page))["unlocked"] is True               # the page keeps its token for this tab
        assert [b.inner_text().strip() for b in page.locator("#settings-menu .menu-item").all()] == PAGES
        assert errors == []
        page.close()


def test_unlock_with_the_pin_and_a_wrong_one_says_so(browser):  # noqa: F811
    with PageServer() as server:
        pins_of(server).set_pin("2468")
        page = open_page(browser, server)
        open_settings(page)
        wait_title(page, "Enter the PIN")
        enter(page, "1111")
        page.wait_for_function("document.getElementById('pin-message').textContent === 'Wrong PIN: 4 tries left'", timeout=5000)
        assert view(page) == "settings-pin" and page.locator("#pin-dots .dot.on").count() == 0
        tap(page, "24")
        page.click("#pin-pad [data-key='delete']")
        assert page.locator("#pin-dots .dot.on").count() == 1
        tap(page, "468")
        page.click("#pin-pad [data-key='ok']")
        page.wait_for_function("document.body.dataset.view === 'settings'", timeout=5000)
        assert page.locator("#settings-time").inner_text() == "Locks in 5 min"
        page.close()


def test_a_laptop_types_the_pin_on_its_own_keyboard(browser):  # noqa: F811
    with PageServer() as server:
        pins_of(server).set_pin("2468")
        page = open_page(browser, server)
        open_settings(page)
        wait_title(page, "Enter the PIN")
        page.keyboard.type("24689")
        page.keyboard.press("Backspace")
        page.keyboard.press("Enter")
        page.wait_for_function("document.body.dataset.view === 'settings'", timeout=5000)
        page.close()


def test_five_wrong_pins_lock_the_pad_and_say_for_how_long(browser):  # noqa: F811
    with PageServer() as server:
        pins_of(server).set_pin("2468")
        page = open_page(browser, server)
        open_settings(page)
        wait_title(page, "Enter the PIN")
        for _ in range(5):
            enter(page, "0000")
            page.wait_for_function("document.getElementById('pin-message').textContent !== ''", timeout=5000)
        page.wait_for_function("document.getElementById('pin-message').textContent === 'Too many wrong PINs: try again in 10 minutes'",
                               timeout=5000)
        assert page.locator("#pin-pad [data-digit='2']").is_disabled()
        page.click("#back-from-settings-pin")                                   # and a second look says the same
        page.click("#open-settings")
        page.wait_for_function("document.getElementById('pin-message').textContent === 'Too many wrong PINs: try again in 10 minutes'",
                               timeout=5000)
        page.close()


def test_lock_ends_the_token_and_returns_to_the_pad(browser):  # noqa: F811
    with PageServer() as server:
        page = unlocked_page(browser, server)
        held = token(page)
        assert pin_state(server, held)["unlocked"] is True
        page.click("#settings-lock")
        wait_title(page, "Enter the PIN")
        assert view(page) == "settings-pin" and token(page) is None
        assert pin_state(server, held)["unlocked"] is False
        page.close()


def test_a_page_that_is_unlocked_opens_settings_without_the_pin_again(browser):  # noqa: F811
    with PageServer() as server:
        page = unlocked_page(browser, server)
        page.click("#back-from-settings")
        assert view(page) == "options"
        page.click("#open-settings")
        page.wait_for_function("document.body.dataset.view === 'settings'", timeout=5000)
        page.close()


def test_a_page_not_built_yet_says_coming_soon_and_back_returns_to_the_menu(browser):  # noqa: F811
    with PageServer() as server:
        page = unlocked_page(browser, server)
        page.click("#settings-menu .menu-item:has-text('Network')")
        assert page.locator("#settings-page").is_visible() and page.locator("#settings-menu").is_hidden()
        assert page.locator("#settings-title").inner_text() == "Network"
        assert "Coming soon" in page.locator("#settings-page").inner_text()
        page.click("#back-from-settings")
        assert view(page) == "settings" and page.locator("#settings-menu").is_visible()
        assert page.locator("#settings-title").inner_text() == "Settings"
        page.click("#back-from-settings")
        assert view(page) == "options"
        page.close()


def test_the_tv_page_opens_options_tv_and_its_back_returns_to_settings(browser):  # noqa: F811
    with PageServer(tv=RemoteTv()) as server:
        page = unlocked_page(browser, server)
        page.click("#settings-menu .menu-item:has-text('TV')")
        assert view(page) == "tv"
        page.click("#back-from-tv")
        assert view(page) == "settings"
        page.close()


def test_security_changes_the_pin(browser):  # noqa: F811
    with PageServer() as server:
        page = unlocked_page(browser, server)
        page.click("#settings-menu .menu-item:has-text('Security')")
        security = page.locator("#settings-page")
        page.wait_for_function("document.querySelector('#settings-page .pin-title').textContent === 'Enter the current PIN'", timeout=5000)
        enter_in = lambda digits: (  # noqa: E731
            [security.locator(f"[data-digit='{d}']").click() for d in digits], security.locator("[data-key='ok']").click())
        enter_in("2468")
        page.wait_for_function("document.querySelector('#settings-page .pin-title').textContent === 'Choose a new PIN'", timeout=5000)
        enter_in("13579")
        page.wait_for_function("document.querySelector('#settings-page .pin-title').textContent === 'Enter the new PIN again'", timeout=5000)
        enter_in("13579")
        page.wait_for_function("document.querySelector('#settings-page .pin-message').textContent === 'Saved'", timeout=5000)
        assert view(page) == "settings"
        page.click("#settings-lock")
        wait_title(page, "Enter the PIN")
        enter(page, "13579")
        page.wait_for_function("document.body.dataset.view === 'settings'", timeout=5000)
        page.close()


def test_security_says_a_wrong_current_pin_and_starts_again(browser):  # noqa: F811
    with PageServer() as server:
        page = unlocked_page(browser, server)
        page.click("#settings-menu .menu-item:has-text('Security')")
        security = page.locator("#settings-page")
        for digits in ("1111", "5555", "5555"):
            for d in digits:
                security.locator(f"[data-digit='{d}']").click()
            security.locator("[data-key='ok']").click()
            page.wait_for_timeout(50)
        page.wait_for_function("document.querySelector('#settings-page .pin-message').textContent === 'Wrong PIN: 4 tries left'", timeout=5000)
        assert security.locator(".pin-title").inner_text() == "Enter the current PIN"
        assert pins_of(server).is_set()
        page.close()


def test_a_refused_token_returns_to_the_pin_pad(browser):  # noqa: F811
    """The token ended elsewhere (it ran out on the TV Pi, or the room software restarted): the next request is answered 401, and the
    page asks for the PIN again rather than showing a page it cannot use."""
    with PageServer() as server:
        page = unlocked_page(browser, server)
        page.evaluate("(t) => fetch('/api/settings/lock', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Croom-Unlock': t}, body: '{}'})",
                      token(page))
        page.click("#settings-menu .menu-item:has-text('Security')")
        for digits in ("2468", "1357", "1357"):
            for d in digits:
                page.locator(f"#settings-page [data-digit='{d}']").click()
            page.locator("#settings-page [data-key='ok']").click()
        page.wait_for_function("document.body.dataset.view === 'settings-pin'", timeout=5000)
        wait_title(page, "Enter the PIN")
        assert page.locator("#pin-message").inner_text() == "Settings locked: enter the PIN again"
        assert token(page) is None
        page.close()


def test_with_no_request_for_five_minutes_the_page_locks_itself(browser):  # noqa: F811
    with PageServer() as server:
        page = unlocked_page(browser, server, stopped=True)
        page.clock.run_for(2 * 60_000 + 1500)                                  # the minutes are written each second: the last time well past two
        assert page.locator("#settings-time").inner_text() == "Locks in 3 min"
        page.clock.run_for(3 * 60_000)
        page.wait_for_function("document.body.dataset.view === 'settings-pin'", timeout=5000)
        wait_title(page, "Enter the PIN")
        assert token(page) is None
        page.close()


def test_the_settings_views_return_to_idle_after_ten_idle_minutes(browser):  # noqa: F811
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        open_settings(page)
        page.clock.fast_forward(9 * 60_000)
        page.wait_for_timeout(200)
        assert view(page) == "settings-pin"
        page.clock.fast_forward(90_000)
        page.wait_for_timeout(200)
        assert view(page) == "idle"
        page.close()


def test_leaving_settings_for_the_idle_screen_locks_it(browser):  # noqa: F811
    """The room's screen is shared: a person who walks away from Settings leaves it locked, not open for the next five minutes."""
    with PageServer() as server:
        page = unlocked_page(browser, server)
        held = token(page)
        page.click("#back-from-settings")
        page.click("#back-from-options")
        assert view(page) == "idle"
        page.wait_for_function("(t) => sessionStorage.getItem('settings-unlock') === null", timeout=5000)
        assert pin_state(server, held)["unlocked"] is False
        page.close()


def test_every_settings_screen_fits_the_kiosk(browser):  # noqa: F811
    with PageServer() as server:
        page = open_page(browser, server)
        open_settings(page)
        wait_title(page, "Choose a PIN")
        assert fits(page), "the PIN pad scrolls"
        enter(page, "2468")
        wait_title(page, "Enter the same PIN again")
        enter(page, "2468")
        page.wait_for_function("document.body.dataset.view === 'settings'", timeout=5000)
        assert fits(page), "the Settings menu scrolls"
        page.click("#settings-menu .menu-item:has-text('Security')")
        assert fits(page), "the Security page scrolls"
        page.click("#back-from-settings")
        page.click("#settings-menu .menu-item:has-text('Room')")
        assert fits(page), "a page to come scrolls"
        page.click("#back-from-settings")
        page.click("#back-from-settings")
        assert fits(page), "the Options screen scrolls with Settings on it"
        page.close()


def test_every_page_of_the_menu_has_an_icon_the_sprite_draws(browser):  # noqa: F811
    with PageServer() as server:
        page = unlocked_page(browser, server)
        page.wait_for_function("document.querySelector('#settings-menu .menu-item svg use')", timeout=5000)
        drawn = page.evaluate("""() => [...document.querySelectorAll('#settings-menu .menu-item')].map(b => {
            const href = b.querySelector('svg.icon use').getAttribute('href');
            return [b.textContent.trim(), href, Boolean(document.querySelector('symbol' + href))]; })""")
        assert [label for label, _, _ in drawn] == PAGES
        assert all(found for _, _, found in drawn), drawn
        page.close()
