"""
Recorded Google bookings flow through the provider, the calendar service, the
room page API and the check command without error. Added after the review:
all-day bookings once crashed every consumer with naive datetimes.
"""

import io
from datetime import datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from croom.calendar.check import check_calendar
from croom.calendar.providers.google import GoogleCalendarProvider
from croom.calendar.service import CalendarService
from croom.control.service import ControlService
from croom.core.config import Config
from tests.unit.calendar.test_google_provider import MEET, ROOM, ZOOM_ADDON, FakeGoogleService, item
from tests.unit.control.test_service import StubMeeting

pytest.importorskip("googleapiclient")


class OfflineProvider(GoogleCalendarProvider):
    """The real provider on a fake API client; signing in needs no network."""

    def __init__(self, service):
        super().__init__()
        self._service = service
        self._authenticated = True

    async def authenticate(self, credentials):
        return True


async def test_recorded_bookings_reach_the_room_page_and_the_check(tmp_path):
    now = datetime.now().astimezone().replace(microsecond=0)
    if (now + timedelta(hours=3)).date() != now.date():
        pytest.skip("less than 3 hours left in the local day")
    at = lambda hours: {"dateTime": (now + timedelta(hours=hours)).isoformat()}  # noqa: E731
    today, tomorrow = now.date().isoformat(), (now.date() + timedelta(days=1)).isoformat()
    items = [
        item("meet", "Design review", conference=MEET, start=at(1), end=at(1.5)),
        item("zoom", "Vendor call", conference=ZOOM_ADDON, start=at(2), end=at(2.5)),
        item("allday", "Office closed", all_day=True, start={"date": today}, end={"date": tomorrow}),
        item("declined", "Double booked", conference=MEET, response="declined", start=at(1), end=at(1.5)),
    ]
    provider = OfflineProvider(FakeGoogleService(items, calendar={"id": ROOM, "summary": "Room 1"}))

    calendar = CalendarService(config={"provider": "google", "calendar_ids": [ROOM]})
    calendar._provider = provider
    calendar._initialized = True
    calendar._calendar_ids = [ROOM]
    await calendar._fetch_events()
    assert calendar.connected is True
    assert {e.id for e in calendar.events} == {"meet", "zoom", "allday"}

    control = ControlService(config={"host": "127.0.0.1", "port": 0, "room_name": "Room 1", "room_location": ""},
                             meeting=StubMeeting(), calendar=calendar)
    client = TestClient(TestServer(control.create_app()))
    await client.start_server()
    try:
        status = await client.get("/api/status")
        assert status.status == 200, await status.text()
        body = await status.json()
        assert body["calendar"]["connected"] is True
        assert body["calendar"]["next"]["id"] == "meet"
        events = await client.get("/api/calendar/events")
        assert events.status == 200, await events.text()
        listed = {e["id"]: e for e in (await events.json())["events"]}
        assert set(listed) == {"allday", "meet", "zoom"}
        assert listed["zoom"]["joinable"] is True and "zoom" in str(listed["zoom"]["meeting_platform"]).lower()
        assert listed["allday"]["joinable"] is False
    finally:
        await client.close()

    config = Config()
    config.calendar.providers = ["google"]
    key = tmp_path / "key.json"
    key.write_text('{"client_email": "rooms@p.iam.gserviceaccount.com"}')
    config.calendar.google_credentials_path = str(key)
    config.calendar.google_calendar_id = ROOM
    out = io.StringIO()
    assert await check_calendar(config, out=out, provider_factory=lambda: provider) == 0, out.getvalue()
    text = out.getvalue()
    assert "Office closed" in text and "Vendor call" in text and "Double booked" not in text
