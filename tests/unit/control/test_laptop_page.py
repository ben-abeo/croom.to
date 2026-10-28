"""The room page's Laptop button and screen (spec 2026-10-27), in the browser, against a stub Laptop mode."""
import pytest

from tests.unit.control.test_laptop_api import StubLaptop
from tests.unit.control.test_page import PageServer, browser, fits, tv_page  # noqa: F401  (browser is a fixture)
from tests.unit.control.test_page import RemoteTv

playwright = pytest.importorskip("playwright.sync_api")


class TimedLaptop(StubLaptop):
    def __init__(self, until=None):
        super().__init__()
        self.until = until

    def state(self):
        state = super().state()
        state["until"] = self.until if self.away else None
        return state


def room_page(browser, server):  # noqa: F811
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
    page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
    return page


def test_laptop_in_a_room_with_no_calendar_switches_in_one_tap_and_back(browser):  # noqa: F811
    laptop = TimedLaptop()
    with PageServer(calendar_connected=False, laptop=laptop) as server:
        page = room_page(browser, server)
        page.click("#actions button:has-text('Laptop')")
        page.wait_for_function("document.body.dataset.laptop === 'on'", timeout=5000)
        assert laptop.calls == [("laptop", None)]
        assert page.locator("#headline").inner_text() == "The camera, microphones and speaker are on the laptop"
        assert "Logitech MeetUp" in page.locator("#detail").inner_text()
        assert page.locator(".link").is_hidden() and page.locator("#open-options").is_hidden()
        assert fits(page)
        page.click("#actions button:has-text('Back to room')")
        page.wait_for_function("document.body.dataset.laptop !== 'on'", timeout=5000)
        assert laptop.calls[-1] == ("room", True)
        assert page.locator(".link").is_visible()


def test_laptop_in_a_free_room_asks_how_long(browser):  # noqa: F811
    laptop = TimedLaptop()
    with PageServer(bookings=True, laptop=laptop) as server:
        page = room_page(browser, server)
        page.click("#actions button:has-text('Laptop')")
        page.wait_for_selector(".bk[data-mode='laptop']")
        assert page.locator(".bk-title").inner_text() == "Use a laptop for"
        assert page.locator(".bk-name").is_hidden()
        page.select_option(".bk-length", "45")
        page.click(".bk-submit")
        page.wait_for_function("document.body.dataset.laptop === 'on'", timeout=5000)
        assert laptop.calls == [("laptop", 45)]


def test_the_laptop_screen_says_until_when(browser):  # noqa: F811
    laptop = TimedLaptop(until="2026-10-27T10:30:00-05:00")
    laptop.away = True
    with PageServer(calendar_connected=False, laptop=laptop) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.laptop === 'on'", timeout=5000)
        assert page.locator("#headline").inner_text().startswith("The camera, microphones and speaker are on the laptop until ")


def test_a_refusal_is_said_on_the_card(browser):  # noqa: F811
    from croom.laptop.service import LaptopError
    laptop = TimedLaptop()
    laptop.fail = LaptopError("The switch did not answer: press the button on the switch box")
    with PageServer(calendar_connected=False, laptop=laptop) as server:
        page = room_page(browser, server)
        page.click("#actions button:has-text('Laptop')")
        page.wait_for_function("document.getElementById('message').textContent.includes('switch box')", timeout=5000)


def test_a_room_without_the_switch_has_no_laptop_button(browser):  # noqa: F811
    with PageServer(calendar_connected=False) as server:
        page = room_page(browser, server)
        assert page.locator("#actions button:has-text('Laptop')").count() == 0


def test_the_laptop_input_is_chosen_beside_the_pis(browser):  # noqa: F811
    tv = RemoteTv()
    with PageServer(tv=tv) as server:
        page = tv_page(browser, server)
        saved = []

        def save(route):
            saved.append(route.request.post_data_json)
            tv.shown.update(saved[-1])
            route.fulfill(json={"tv": tv.status()})

        page.route("**/api/tv/settings", save)
        page.get_by_role("button", name="Change TV", exact=True).click()
        page.locator("#tv-connection-settings summary").click()
        assert page.get_by_label("Laptop input").input_value() == ""
        page.get_by_label("Laptop input").select_option("HDMI_2")
        page.get_by_role("button", name="Save connection", exact=True).click()
        page.wait_for_function("document.getElementById('tv-settings-result').textContent.includes('Saved')")
        assert saved[-1]["laptop_input"] == "HDMI_2" and saved[-1]["pi_input"] == "HDMI_1"
