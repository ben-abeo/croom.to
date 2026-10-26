"""
The Meet provider on the shared TV page: it joins by navigating the display's
page and hands it back to the screensaver when it leaves.
"""

import pytest

playwright = pytest.importorskip("playwright.async_api")

from croom.meeting.providers.base import MeetingState  # noqa: E402
from croom.meeting.providers.google_meet import GoogleMeetProvider  # noqa: E402
from tests.unit.meeting.fake_display import FakeDisplay  # noqa: E402


async def test_leaving_hands_the_page_back_to_the_screensaver(tmp_path):
    idle = tmp_path / "idle.html"
    idle.write_text("<title>Crystal Meet TV</title>")
    meeting = tmp_path / "meeting.html"
    meeting.write_text("<title>Meet</title><button aria-label='Leave call'>Leave</button>")
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        display = FakeDisplay(page, idle_url=idle.as_uri())
        provider = GoogleMeetProvider(display, room_name="Room 3")
        await provider.initialize()
        assert provider._page is page
        await page.goto(meeting.as_uri())
        provider._set_state(MeetingState.CONNECTED)
        await provider.leave_meeting()
        assert display.shown == 1 and page.url == idle.as_uri()
        await provider.shutdown()
        assert not page.is_closed()   # the display, not the provider, owns the page
        await browser.close()


async def test_the_provider_needs_a_display_to_initialize():
    with pytest.raises(RuntimeError) as failure:
        await GoogleMeetProvider(None).initialize()
    assert "display" in str(failure.value)


async def test_joining_claims_the_page_for_the_meeting(tmp_path):
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.route("**/*", lambda route: route.abort())    # no network: the join fails fast
        display = FakeDisplay(page)
        provider = GoogleMeetProvider(display, room_name="Room 3")
        await provider.initialize()
        with pytest.raises(Exception):  # noqa: B017 - any failure, the claim is what is tested
            await provider.join_meeting("https://meet.google.com/abc-defg-hij")
        assert display.claimed == 1
        assert display.shown == 1  # the real display retries an interrupted navigation
        assert provider.state == MeetingState.ERROR
        await browser.close()


@pytest.mark.parametrize("redirected", [False, True])
async def test_refused_join_returns_to_crystal_meet_and_keeps_the_reason(tmp_path, redirected):
    from tests.unit.meeting.test_meet_prejoin import REFUSAL_PAGE

    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        if redirected:
            await page.route("https://meet.google.com/**", lambda route: route.fulfill(
                body='<script>location.replace("https://workspace.google.com/")</script>'))
            await page.route("https://workspace.google.com/**", lambda route: route.fulfill(
                body="<h1>Google Workspace</h1>"))
        else:
            await page.route("https://meet.google.com/**", lambda route: route.fulfill(body=REFUSAL_PAGE))
        idle = tmp_path / "idle.html"
        idle.write_text("<title>Crystal Meet</title>")
        provider = GoogleMeetProvider(FakeDisplay(page, idle_url=idle.as_uri()))
        provider.failure_screenshot = tmp_path / "failure.png"
        provider.PAGE_SETTLE_S = 0
        provider.NAME_SELECTORS = []
        provider.TOGGLE_TIMEOUT_MS = 10
        provider.JOIN_FIND_TIMEOUT_MS = 10
        try:
            reason = "Google Workspace" if redirected else "You can't join this video call"
            with pytest.raises(RuntimeError, match=reason):
                await provider.join_meeting("abc-defg-hij")
            assert page.url == idle.as_uri()
            assert provider.state == MeetingState.ERROR
            assert reason in provider.current_meeting.error_message
            assert provider.failure_screenshot.stat().st_size > 0
        finally:
            await browser.close()
