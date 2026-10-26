"""
Book the room and Use now on the door sign and the room page (spec 2026-10-25), in the browser: the steps, End now and Cancel with
their second tap, the light bar's colour following, the room page's own keyboard typing the name, and the steps closing by
themselves. The room's calendar is a fake Google behind the real bookings (PageServer(bookings=True)).
"""

from datetime import date, datetime, time, timedelta

import pytest

from tests.unit.control.test_page import LIGHT_RECORDER, PageServer, browser, skip_if_day_ends_within, wait_for  # noqa: F401  (browser is a fixture)

playwright = pytest.importorskip("playwright.sync_api")


def google_titles(server):
    return sorted(e.title for e in server.google.events.values())


def sign(browser, server, init_script=None):  # noqa: F811
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    if init_script:
        page.add_init_script(init_script)
    page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
    page.wait_for_selector("#book", timeout=8000)
    return page


def test_use_now_on_the_sign_takes_the_room_and_only_the_room_page_ends_it(browser):  # noqa: F811
    with PageServer(room_name="Room 1", bookings=True) as server:
        page = sign(browser, server, init_script=LIGHT_RECORDER)
        page.click("#use-now")
        page.fill(".bk-name", "Ben")
        page.select_option(".bk-length", "45")
        page.click(".bk-submit")
        page.wait_for_selector(".bk-done:not([hidden])")
        assert page.locator(".bk-done").inner_text().startswith("The room is yours until")
        assert google_titles(server) == ["Ben"]
        page.click(".bk-back")                                                     # Done
        page.wait_for_function("document.body.dataset.state === 'occupied'", timeout=5000)
        assert page.locator("#chip-text").inner_text().startswith("Booked until") and page.locator("#detail").inner_text() == "Ben"
        assert page.evaluate("window.lights").count("occupied") == 1               # the light bar went red
        assert not page.locator("#use-now").is_visible()                           # the room is taken
        assert page.locator("text=End now").count() == 0                           # ended at the room's touch screen, not from the hallway
        room = browser.new_page(viewport={"width": 1280, "height": 800})
        room.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        room.click("#actions button:has-text('End now')")
        room.click("#actions button:has-text('Tap again to end')")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=8000)
        room.close()
        assert page.evaluate("window.lights")[-1] == "free"
        page.close()


def test_book_on_the_sign_picks_a_day_a_time_a_name_and_a_length(browser):  # noqa: F811
    with PageServer(room_name="Room 1", bookings=True) as server:
        page = sign(browser, server)
        page.click("#book")
        page.wait_for_selector(".bk-day")                                         # (not today's times: late in the evening there are none left)
        assert page.locator(".bk-day").count() == 7 and page.locator(".bk-day").first.get_attribute("aria-pressed") == "true"
        page.click(".bk-day >> text=Tomorrow")
        page.wait_for_function("document.querySelector('.bk-day[aria-pressed=true]').textContent.startsWith('Tomorrow')")
        page.click(".bk-time >> text=/^10:00\\sAM$/")
        assert page.locator(".bk-when").inner_text().endswith("at 10:00 AM")
        page.fill(".bk-name", "Private review")
        page.select_option(".bk-length", "90")
        assert page.locator(".bk-length option").first.inner_text() == "15 min" and page.locator(".bk-length option").last.inner_text() == "4 hours"
        page.click(".bk-submit")
        page.wait_for_selector(".bk-done:not([hidden])")
        assert page.locator(".bk-done").inner_text().startswith("Booked: Private review, 10:00 AM to 11:30 AM")
        [booked] = server.google.events.values()
        tomorrow = date.today() + timedelta(days=1)
        assert (booked.start_time, booked.end_time) == (datetime.combine(tomorrow, time(10)).astimezone(),
                                                        datetime.combine(tomorrow, time(11, 30)).astimezone())
        page.close()


def test_a_name_is_needed_and_a_taken_time_is_refused_in_words(browser):  # noqa: F811
    with PageServer(room_name="Room 1", bookings=True) as server:
        page = sign(browser, server)
        page.click("#book")
        page.click(".bk-day >> text=Tomorrow")
        page.wait_for_function("document.querySelector('.bk-day[aria-pressed=true]').textContent.startsWith('Tomorrow')")
        page.click(".bk-time >> text=/^10:00\\sAM$/")
        page.click(".bk-submit")
        assert page.locator(".bk-error").inner_text() == "Type your name or the meeting's name."
        tomorrow = date.today() + timedelta(days=1)
        server.google.add("Booked in Google a moment ago", datetime.combine(tomorrow, time(10)).astimezone(),
                          datetime.combine(tomorrow, time(11)).astimezone())
        page.fill(".bk-name", "Private review")
        page.click(".bk-submit")
        page.wait_for_function("document.querySelector('.bk-error').textContent.includes('Booked in Google a moment ago')")
        assert google_titles(server) == ["Booked in Google a moment ago"]
        page.close()


def test_cancel_on_the_signs_today_list_needs_a_second_tap(browser):  # noqa: F811
    skip_if_day_ends_within(2)   # the booking is an hour from now, and the Today list is today's
    with PageServer(room_name="Room 1", bookings=True) as server:
        page = sign(browser, server)
        moment = datetime.now().astimezone() + timedelta(hours=1)
        start = moment.replace(minute=moment.minute - moment.minute % 15, second=0, microsecond=0)
        response = page.request.post(f"http://127.0.0.1:{server.port}/api/bookings",
                                     data={"start": start.isoformat(), "minutes": 30, "title": "Interview", "source": "room page"})
        assert response.status == 201
        page.reload(wait_until="networkidle")
        cancel = page.locator(".event.mine .cancel")
        cancel.wait_for(timeout=5000)
        cancel.click()
        assert cancel.inner_text() == "Tap again to cancel" and google_titles(server) == ["Interview"]
        cancel.click()
        wait_for("the booking to be cancelled", lambda: google_titles(server) == [])
        page.wait_for_function("!document.querySelector('.event.mine')", timeout=5000)
        page.close()


def test_the_steps_close_by_themselves_after_a_minute(browser):  # noqa: F811
    with PageServer(room_name="Room 1", bookings=True) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.clock.install()
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_selector("#book", timeout=8000)
        page.click("#book")
        assert page.evaluate("document.body.dataset.view") == "book"
        page.clock.run_for(59 * 1000)
        assert page.evaluate("document.body.dataset.view") == "book"
        page.clock.run_for(2 * 1000)
        assert page.evaluate("document.body.dataset.view") == "today" and page.locator("#booking").is_hidden()
        page.close()


def test_the_room_page_books_with_its_own_keyboard(browser):  # noqa: F811
    with PageServer(room_name="Room 1", bookings=True) as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/?keyboard=1", wait_until="networkidle")
        page.click("#actions button:has-text('Use now')")
        assert page.evaluate("document.body.dataset.view") == "booking"
        page.click(".bk-name")
        page.wait_for_selector("#keyboard:not([hidden])")
        assert page.locator("#keyboard .key.join").inner_text() == "Done"
        for key in ["shift", "b", "e", "n"]:
            page.evaluate("(k) => window.croomKeyboard.press(k)", key)
        assert page.input_value(".bk-name") == "Ben"
        page.evaluate("window.croomKeyboard.press('join')")                       # Done sends the form
        page.wait_for_selector(".bk-done:not([hidden])")
        assert google_titles(server) == ["Ben"]
        page.click(".bk-back")
        page.wait_for_function("document.getElementById('kicker').textContent === 'In use'", timeout=5000)
        assert page.locator("#headline").inner_text().startswith("Ben until")
        page.click("#actions button:has-text('End now')")
        page.click("#actions button:has-text('Tap again to end')")
        page.wait_for_function("document.getElementById('kicker').textContent === 'Room free'", timeout=5000)
        page.close()


def test_a_room_without_a_calendar_to_book_in_offers_no_booking(browser):  # noqa: F811
    with PageServer(room_name="Room 1") as server:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.getElementById('kicker').textContent === 'Room free'", timeout=5000)
        assert page.locator("#actions button").count() == 0
        page.close()
