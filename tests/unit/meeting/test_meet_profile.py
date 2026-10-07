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
