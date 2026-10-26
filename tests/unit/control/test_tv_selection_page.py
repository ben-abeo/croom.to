"""The real remote UI against the TV HTTP boundary, without a television on the LAN."""

import pytest

from tests.unit.control.test_page import (
    PageServer, RemoteTv, StubSearch, browser, fits, open_tv_screen, refresh_now, tv_calls, tv_page,
)

pytest.importorskip("playwright.sync_api")

SAMSUNG = {
    "host": "10.0.0.57", "name": "Lobby Samsung", "brand": "samsung",
    "mac": "aa:bb:cc:dd:ee:57", "location": "http://10.0.0.57:8001/api/v2/",
}
SAMSUNG_STATUS = {
    "brand": "samsung", "mac": SAMSUNG["mac"], "name": SAMSUNG["name"], "host": SAMSUNG["host"],
    "capabilities": {"pointer": False, "direct_inputs": False, "mute_state": False},
    "volume": None, "muted": None, "input": None, "inputs": [], "app": None,
}


def hold_selection(page, tv):
    """Hold only the network response; the rendered UI and its request are real."""
    waiting = []

    def select(route):
        tv.configured = True
        tv.paired = False
        tv.shown.update(SAMSUNG_STATUS)
        waiting.append(route)

    page.route("**/api/tv/select", select)
    return waiting


def discover(page, card="#tv-not-set-up"):
    page.locator(f"{card} .tv-find").click()
    page.wait_for_selector(f"{card} .tv-found li", timeout=5000)
    return page.locator(f"{card} .tv-found li").first


def test_selecting_a_discovered_tv_pairs_immediately_and_waits_for_acceptance(browser):
    tv = RemoteTv(configured=False, paired=False)
    with PageServer(tv=tv, discover=StubSearch([SAMSUNG])) as server:
        page = tv_page(browser, server)
        waiting = hold_selection(page, tv)
        row = discover(page)
        assert row.get_by_role("button", name="Connect", exact=True).count() == 1
        row.get_by_role("button", name="Connect", exact=True).dblclick()
        page.wait_for_selector("#tv-pair-cancel", state="visible", timeout=5000)
        assert len(waiting) == 1
        assert waiting[0].request.post_data_json == {"host": "10.0.0.57"}
        assert "Accept the request on the TV" in page.locator("#tv-pairing-text").inner_text()
        assert "Lobby Samsung" in page.locator("#tv-facts").inner_text()
        assert page.locator("#tv-not-set-up").is_hidden()
        assert page.locator(".tv-find:visible").count() == 0
        tv.paired = True
        waiting.pop().fulfill(json={"tv": tv.status()})
        page.wait_for_selector("#tv-remote", state="visible", timeout=5000)
        assert page.locator("#tv-pair-cancel").is_hidden()
        assert fits(page)


def test_a_rejected_selection_refreshes_the_saved_tv_and_offers_pair_again(browser):
    tv = RemoteTv(configured=False, paired=False)
    with PageServer(tv=tv, discover=StubSearch([SAMSUNG])) as server:
        page = tv_page(browser, server)
        waiting = hold_selection(page, tv)
        row = discover(page)
        assert row.get_by_role("button", name="Connect", exact=True).count() == 1
        row.get_by_role("button", name="Connect", exact=True).click()
        page.wait_for_selector("#tv-pair-cancel", state="visible", timeout=5000)
        waiting.pop().fulfill(status=409, json={"error": "The TV rejected the connection request"})
        page.wait_for_selector("#tv-pair", state="visible", timeout=5000)
        assert "rejected" in page.locator("#tv-words").inner_text()
        assert "Lobby Samsung" in page.locator("#tv-facts").inner_text()
        assert page.locator("#tv-not-set-up").is_hidden()
        assert page.locator("#tv-find").is_enabled()


def test_cancel_during_selection_forgets_the_pending_pairing_without_an_error(browser):
    tv = RemoteTv(configured=False, paired=False)
    with PageServer(tv=tv, discover=StubSearch([SAMSUNG])) as server:
        page = tv_page(browser, server)
        waiting = hold_selection(page, tv)
        row = discover(page)
        assert row.get_by_role("button", name="Connect", exact=True).count() == 1
        row.get_by_role("button", name="Connect", exact=True).click()
        page.wait_for_selector("#tv-pair-cancel", state="visible", timeout=5000)
        page.click("#tv-pair-cancel")
        assert tv_calls(page, server, 1) == [("forget",)]
        waiting.pop().fulfill(status=409, json={"error": "Pairing was stopped"})
        page.wait_for_selector("#tv-pair", state="visible", timeout=5000)
        assert page.locator("#tv-words").is_hidden()


@pytest.mark.parametrize("manual_mac", ["", "AA:BB:CC:DD:EE:99"])
def test_a_tv_without_a_mac_can_connect_or_accept_an_optional_mac(browser, manual_mac):
    tv = RemoteTv(configured=False, paired=False)
    with PageServer(tv=tv, discover=StubSearch([{**SAMSUNG, "mac": ""}])) as server:
        page = tv_page(browser, server)
        waiting = hold_selection(page, tv)
        row = discover(page)
        assert "wake" in row.inner_text().lower()
        assert row.get_by_role("button", name="Connect", exact=True).count() == 1
        if manual_mac:
            row.locator("summary").click()
            row.get_by_label("MAC address (optional)").fill(manual_mac)
            refresh_now(page)
            assert row.get_by_label("MAC address (optional)").input_value() == manual_mac
        row.get_by_role("button", name="Connect", exact=True).click()
        page.wait_for_selector("#tv-pair-cancel", state="visible", timeout=5000)
        expected = {"host": "10.0.0.57"}
        if manual_mac:
            expected["mac"] = manual_mac
        assert waiting[0].request.post_data_json == expected
        tv.paired = True
        waiting.pop().fulfill(json={"tv": tv.status()})
        page.wait_for_selector("#tv-remote", state="visible", timeout=5000)


def test_the_configured_remote_can_change_tv_and_the_results_scroll_inside_the_card(browser, tmp_path):
    tv = RemoteTv()
    many = [{**SAMSUNG, "host": f"10.0.0.{n}", "name": f"Samsung {n}", "mac": ""} for n in range(50, 70)]
    with PageServer(tv=tv, discover=StubSearch(many)) as server:
        page = tv_page(browser, server)
        assert page.get_by_role("button", name="Change TV", exact=True).count() == 1
        page.get_by_role("button", name="Change TV", exact=True).click()
        page.wait_for_selector("#tv-choose .tv-found li", timeout=5000)
        assert page.locator("#tv-choose .tv-found li").count() == 20
        assert page.locator("#tv-choose .tv-found-list").evaluate("e => e.scrollHeight > e.clientHeight")
        assert fits(page)
        page.locator("#tv-connection-settings summary").click()
        assert fits(page)
        page.screenshot(path=str(tmp_path / "tv-selection.png"))
        page.locator("#tv-choose").get_by_role("button", name="Done", exact=True).click()
        assert page.locator("#tv-remote").is_visible()
        assert page.get_by_role("button", name="Change TV", exact=True).is_visible()


def test_connection_settings_save_mac_and_pi_input_without_pairing(browser):
    tv = RemoteTv(**SAMSUNG_STATUS)
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        saved = []

        def save(route):
            saved.append(route.request.post_data_json)
            tv.shown.update(saved[-1])
            route.fulfill(json={"tv": tv.status()})

        page.route("**/api/tv/settings", save)
        assert page.get_by_role("button", name="Change TV", exact=True).count() == 1
        page.get_by_role("button", name="Change TV", exact=True).click()
        page.locator("#tv-connection-settings summary").click()
        page.get_by_label("TV MAC address").fill("AA:BB:CC:DD:EE:88")
        page.get_by_label("Room Pi input").select_option("HDMI_2")
        page.get_by_role("button", name="Save connection", exact=True).click()
        page.wait_for_function("document.getElementById('tv-settings-result').textContent.includes('Saved')")
        assert saved == [{"mac": "AA:BB:CC:DD:EE:88", "pi_input": "HDMI_2"}]
        assert tv.calls == []
        assert fits(page)


def test_samsung_uses_source_and_mute_keys_without_claiming_pointer_or_mute_state(browser):
    tv = RemoteTv(**{**SAMSUNG_STATUS, "muted": True})
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        assert page.locator("#tv-pointer").is_hidden()
        assert page.locator("#tv-pi-mouse").is_visible()
        assert page.locator("#tv-mute").inner_text().strip() == "Mute"
        assert page.locator("#tv-mute").get_attribute("aria-pressed") is None
        assert "muted" not in (page.locator("#tv-level").get_attribute("class") or "")
        page.click("#tv-input")
        assert tv_calls(page, server, 1) == [("control", "key", "INPUT_HUB")]
        assert page.locator("#tv-inputs").is_hidden()
        page.click("#tv-mute")
        assert tv_calls(page, server, 2)[-1] == ("control", "key", "MUTE")
        page.click("#tv-pi-mouse")
        assert page.locator("#tv-trackpad").is_visible()
        page.click("#tv-fullscreen")
        assert page.locator("#touchpad-tv").is_disabled()
        assert page.locator("#touchpad-pi").is_enabled()
        assert "does not support a pointer" in page.locator("#touchpad-sub").inner_text()


def test_an_explicit_supported_keys_list_disables_keys_the_tv_cannot_handle(browser):
    tv = RemoteTv(**{**SAMSUNG_STATUS, "capabilities": {
        **SAMSUNG_STATUS["capabilities"], "supported_keys": ["HOME", "ENTER", "INPUT_HUB", "MUTE"],
    }})
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        assert page.locator('[data-tv="key:DASH"]').is_disabled()
        assert page.locator('[data-tv="key:HOME"]').is_enabled()
        assert page.locator('[data-tv="key:UP"]').get_attribute("aria-disabled") == "true"
        page.locator('[data-tv="key:HOME"]').click()
        assert tv_calls(page, server, 1) == [("control", "key", "HOME")]


def test_a_reachable_samsung_with_unknown_power_state_still_accepts_keys_and_confirms_power_off(browser):
    tv = RemoteTv(**SAMSUNG_STATUS, on=False, reachable=True, power_known=False)
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        assert "Power state unavailable" in page.locator("#tv-facts").inner_text()
        assert page.locator("#tv-turn-on").is_visible()
        assert page.locator('[data-tv="key:HOME"]').is_enabled()
        page.click("#tv-turn-on")
        assert tv_calls(page, server, 1) == [("control", "power", "on")]
        page.locator('[data-tv="key:HOME"]').click()
        assert tv_calls(page, server, 2)[-1] == ("control", "key", "HOME")
        page.click("#tv-power")
        assert page.locator("#tv-power").inner_text() == "Tap again to turn off"
        assert len(tv.calls) == 2
        page.wait_for_timeout(600)
        page.click("#tv-power")
        assert tv_calls(page, server, 3)[-1] == ("control", "power", "off")


@pytest.mark.parametrize("location", ["discovery", "settings"])
def test_mac_addresses_can_be_entered_with_the_kiosk_keyboard(browser, location):
    tv = RemoteTv(configured=location == "settings", paired=location == "settings", mac="")
    with PageServer(tv=tv, discover=StubSearch([{**SAMSUNG, "mac": ""}])) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/?keyboard=1", wait_until="networkidle")
        open_tv_screen(page)
        if location == "discovery":
            row = discover(page)
            row.locator("summary").click()
            field = row.get_by_label("MAC address (optional)")
        else:
            page.get_by_role("button", name="Change TV", exact=True).click()
            page.locator("#tv-connection-settings summary").click()
            field = page.get_by_label("TV MAC address")
        field.click()
        assert page.locator("#keyboard").is_visible()
        keys = page.locator("#keyboard")
        for key in ("a", "a", "layer:symbols", ":", "1", "2", ":", "layer:letters", "b", "b"):
            keys.locator(f'[data-key="{key}"]').click()
        assert field.input_value() == "aa:12:bb"
        assert keys.locator('[data-key="join"]').inner_text() == "Done"
        keys.locator('[data-key="join"]').click()
        assert page.locator("#keyboard").is_hidden()
        assert page.locator("body").get_attribute("data-view") == "tv"
        assert field.input_value() == "aa:12:bb"
        assert fits(page)
