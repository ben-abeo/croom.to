"""
The Google Calendar provider against recorded events.list item shapes
(spec 2026-09-25 section 4.3): link and platform detection, the worker
thread, and calendar lookup errors.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("googleapiclient")
import httplib2  # noqa: E402  (dependency of googleapiclient)
from googleapiclient.errors import HttpError  # noqa: E402

from croom.calendar.providers.base import MeetingPlatform  # noqa: E402
from croom.calendar.providers.google import GoogleCalendarProvider, choose_meeting_url  # noqa: E402

ROOM = "c_1885abc@resource.calendar.google.com"


def item(event_id, summary, conference=None, description="", location="", all_day=False,
         response="accepted", status="confirmed"):
    """One item as Google's events.list returns it for a room resource calendar."""
    if all_day:
        start, end = {"date": "2026-09-25"}, {"date": "2026-09-26"}
    else:
        start = {"dateTime": "2026-09-25T09:00:00-05:00", "timeZone": "America/Chicago"}
        end = {"dateTime": "2026-09-25T09:30:00-05:00", "timeZone": "America/Chicago"}
    data = {
        "kind": "calendar#event", "id": event_id, "status": status, "summary": summary,
        "start": start, "end": end, "description": description, "location": location,
        "organizer": {"email": "ben@crystalpm.com"},
        "attendees": [
            {"email": "ben@crystalpm.com", "organizer": True, "responseStatus": "accepted"},
            {"email": ROOM, "self": True, "resource": True, "responseStatus": response},
        ],
    }
    if conference:
        data["conferenceData"] = conference
    return data


MEET = {"conferenceSolution": {"name": "Google Meet"},
        "entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"},
                        {"entryPointType": "phone", "uri": "tel:+1-555-0100"}]}
ZOOM_ADDON = {"conferenceSolution": {"name": "Zoom Meeting"},
              "entryPoints": [{"entryPointType": "video", "uri": "https://us02web.zoom.us/j/98765432100?pwd=abc123"}]}


class FakeCall:
    """A googleapiclient request: called with kwargs, then .execute()."""

    def __init__(self, result=None, error=None, delay=0.0):
        self.result, self.error, self.delay = result, error, delay
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self

    def execute(self):
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


class FakeGoogleService:
    """Stands in for the built API client: events().list(...) and calendars().get(...)."""

    def __init__(self, items=(), calendar=None, list_error=None, get_error=None, delay=0.0):
        self.list = FakeCall({"items": list(items)}, list_error, delay)
        self.get = FakeCall(calendar or {"id": ROOM, "summary": "Room 1"}, get_error)

    def events(self):
        return self

    def calendars(self):
        return self


def provider_with(service):
    provider = GoogleCalendarProvider()
    provider._service = service
    provider._authenticated = True
    return provider


def http_error(status):
    return HttpError(httplib2.Response({"status": status}), b"error")


async def fetch(items):
    provider = provider_with(FakeGoogleService(items))
    now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
    return await provider.get_events(ROOM, now - timedelta(hours=1), now + timedelta(days=7))


class TestLinks:
    async def test_meet_booking(self):
        [event] = await fetch([item("e1", "Design review", conference=MEET)])
        assert event.meeting_url == "https://meet.google.com/abc-defg-hij"
        assert event.meeting_platform == MeetingPlatform.GOOGLE_MEET
        assert event.title == "Design review" and event.calendar_id == ROOM

    async def test_zoom_addon_booking_is_zoom(self):
        [event] = await fetch([item("e2", "Vendor call", conference=ZOOM_ADDON)])
        assert event.meeting_url == "https://us02web.zoom.us/j/98765432100?pwd=abc123"
        assert event.meeting_platform == MeetingPlatform.ZOOM

    async def test_typed_zoom_link_wins_over_automatic_meet(self):
        [event] = await fetch([item("e3", "Board call", conference=MEET,
                                    description="Join Zoom: https://zoom.us/j/12345678901?pwd=xyz")])
        assert event.meeting_url == "https://zoom.us/j/12345678901?pwd=xyz"
        assert event.meeting_platform == MeetingPlatform.ZOOM

    async def test_pasted_zoom_link_in_location(self):
        [event] = await fetch([item("e4", "Interview", location="https://zoom.us/j/55555555555?pwd=q")])
        assert event.meeting_platform == MeetingPlatform.ZOOM

    async def test_all_day_and_linkless_bookings_have_no_link(self):
        events = await fetch([item("e5", "Office closed", all_day=True), item("e6", "Board lunch")])
        assert [e.meeting_url for e in events] == [None, None]
        assert events[0].is_all_day is True

    async def test_room_declined_booking_is_marked(self):
        [event] = await fetch([item("e7", "Double booked", conference=MEET, response="declined")])
        assert event.response_status == "declined"

    def test_choose_meeting_url_rules(self):
        meet = "https://meet.google.com/abc-defg-hij"
        zoom = "https://zoom.us/j/1?pwd=a"
        teams = "https://teams.microsoft.com/l/meetup-join/x"
        assert choose_meeting_url(meet, zoom) == zoom
        assert choose_meeting_url(zoom, meet) == zoom
        assert choose_meeting_url(meet, meet) == meet
        assert choose_meeting_url(None, teams) == teams
        assert choose_meeting_url(zoom, None) == zoom
        assert choose_meeting_url(None, None) is None


class TestApiCalls:
    async def test_events_are_requested_for_the_configured_calendar_only(self):
        service = FakeGoogleService([item("e1", "x", conference=MEET)])
        provider = provider_with(service)
        now = datetime.now(timezone.utc)
        await provider.get_events(ROOM, now, now + timedelta(days=7))
        assert service.list.kwargs["calendarId"] == ROOM
        assert service.list.kwargs["singleEvents"] is True

    async def test_a_slow_api_call_does_not_stall_the_loop(self):
        provider = provider_with(FakeGoogleService([], delay=0.3))
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        now = datetime.now(timezone.utc)
        await provider.get_events(ROOM, now, now + timedelta(days=1))
        task.cancel()
        assert ticks >= 10

    async def test_api_errors_give_no_events(self):
        provider = provider_with(FakeGoogleService([], list_error=http_error(500)))
        now = datetime.now(timezone.utc)
        assert await provider.get_events(ROOM, now, now + timedelta(days=1)) == []

    async def test_get_calendar_returns_name_and_id(self):
        provider = provider_with(FakeGoogleService(calendar={"id": ROOM, "summary": "Room 1"}))
        assert await provider.get_calendar(ROOM) == {"id": ROOM, "name": "Room 1"}

    @pytest.mark.parametrize("status", [403, 404])
    async def test_unshared_or_unknown_calendar_raises_lookup_error(self, status):
        provider = provider_with(FakeGoogleService(get_error=http_error(status)))
        with pytest.raises(LookupError):
            await provider.get_calendar(ROOM)

    async def test_other_calendar_errors_propagate(self):
        provider = provider_with(FakeGoogleService(get_error=http_error(500)))
        with pytest.raises(HttpError):
            await provider.get_calendar(ROOM)
