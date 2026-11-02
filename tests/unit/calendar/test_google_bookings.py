"""
The Google Calendar provider writing the room's own bookings (spec 2026-10-25): the event it inserts into the room's calendar and
the tag on it, finding one by its tag, ending and cancelling one, and the refusal of a calendar shared for reading only, told
apart from Google being out of reach.
"""

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("googleapiclient")
import httplib2  # noqa: E402  (dependency of googleapiclient)
from googleapiclient.errors import HttpError  # noqa: E402

from croom.calendar.providers.base import CalendarWriteRefused  # noqa: E402
from croom.calendar.providers.google import GoogleCalendarProvider  # noqa: E402
from tests.unit.calendar.test_google_provider import ROOM, FakeCall, item  # noqa: E402

START = datetime(2026, 10, 26, 14, tzinfo=timezone(timedelta(hours=-5)))
END = START + timedelta(minutes=45)


class FakeEvents:
    """events().list / insert / patch / delete, each a FakeCall."""

    def __init__(self, items=(), error=None):
        self.list = FakeCall({"items": list(items)}, error)
        self.insert = FakeCall({"id": "new-google-id"}, error)
        self.patch = FakeCall({}, error)
        self.delete = FakeCall({}, error)

    def events(self):
        return self


def provider_with(service):
    provider = GoogleCalendarProvider()
    provider._service = service
    provider._authenticated = True
    return provider


def http_error(status, reason=""):
    content = b'{"error": {"errors": [{"reason": "%s"}]}}' % reason.encode() if reason else b"error"
    return HttpError(httplib2.Response({"status": status}), content)


def test_the_room_asks_google_for_its_events_and_to_change_them():
    assert "https://www.googleapis.com/auth/calendar.events" in GoogleCalendarProvider.SCOPES


async def test_a_booking_is_inserted_into_the_rooms_calendar_with_the_rooms_tag():
    service = FakeEvents()
    gid = await provider_with(service).create_booking(ROOM, "abc123", "Private review", START, END, "Booked on the Room 1 door sign")
    assert gid == "new-google-id"
    kwargs = service.insert.kwargs
    assert kwargs["calendarId"] == ROOM
    body = kwargs["body"]
    assert body["summary"] == "Private review" and body["description"] == "Booked on the Room 1 door sign"
    assert body["start"] == {"dateTime": START.isoformat()} and body["end"] == {"dateTime": END.isoformat()}
    assert body["extendedProperties"] == {"private": {"croom_id": "abc123"}}
    assert "attendees" not in body and "conferenceData" not in body   # an in-person booking: nobody is invited, there is no link


async def test_a_booking_is_found_by_its_tag():
    service = FakeEvents(items=[item("google-7", "Private review")])
    assert await provider_with(service).find_booking(ROOM, "abc123") == "google-7"
    assert service.list.kwargs["privateExtendedProperty"] == "croom_id=abc123"
    assert await provider_with(FakeEvents()).find_booking(ROOM, "abc123") is None


async def test_the_tag_is_read_back_from_an_event():
    tagged = item("google-7", "Private review")
    tagged["extendedProperties"] = {"private": {"croom_id": "abc123"}}
    service = FakeEvents(items=[tagged, item("google-8", "Booked in Google")])
    events = await provider_with(service).get_events(ROOM, START - timedelta(days=1), START + timedelta(days=1))
    assert [e.booking_id for e in events] == ["abc123", None]


async def test_end_now_moves_the_end_and_cancel_deletes():
    service = FakeEvents()
    provider = provider_with(service)
    await provider.update_booking(ROOM, "google-7", "Private review", START, START + timedelta(minutes=10))
    assert service.patch.kwargs["eventId"] == "google-7"
    assert service.patch.kwargs["body"] == {"summary": "Private review", "start": {"dateTime": START.isoformat()},
                                            "end": {"dateTime": (START + timedelta(minutes=10)).isoformat()}}
    await provider.delete_booking(ROOM, "google-7")
    assert service.delete.kwargs == {"calendarId": ROOM, "eventId": "google-7"}


GUESTS = [{"email": "ben@example.com", "organizer": True, "responseStatus": "accepted"},
          {"email": ROOM, "resource": True, "self": True, "responseStatus": "accepted"}]


async def test_a_meeting_the_room_is_a_guest_of_is_declined_with_nobody_told():
    """End now for a meeting people booked (spec 2026-10-25, section 2.1): the room's own answer becomes declined, the other guests are
    sent back as they were, and no email goes."""
    service = FakeEvents()
    service.get = FakeCall({"id": "google-9", "organizer": {"email": "ben@example.com"}, "attendees": GUESTS})
    await provider_with(service).end_meeting_early(ROOM, "google-9", START + timedelta(minutes=30))
    assert service.get.kwargs == {"calendarId": ROOM, "eventId": "google-9"}
    kwargs = service.patch.kwargs
    assert kwargs["eventId"] == "google-9" and kwargs["sendUpdates"] == "none"
    assert kwargs["body"] == {"attendees": [GUESTS[0], dict(GUESTS[1], responseStatus="declined")]}


async def test_a_meeting_in_the_rooms_own_calendar_with_no_guests_ends_now():
    service = FakeEvents()
    service.get = FakeCall({"id": "google-9", "organizer": {"email": ROOM, "self": True}})
    await provider_with(service).end_meeting_early(ROOM, "google-9", START + timedelta(minutes=30))
    assert service.patch.kwargs["body"] == {"end": {"dateTime": (START + timedelta(minutes=30)).isoformat()}}


async def test_a_meeting_gone_from_google_is_ended_already():
    for status in (404, 410):
        service = FakeEvents()
        service.get = FakeCall(error=http_error(status))
        await provider_with(service).end_meeting_early(ROOM, "google-9", START)
        assert service.patch.kwargs is None


async def test_a_booking_already_gone_from_google_is_cancelled_all_the_same():
    for status in (404, 410):
        await provider_with(FakeEvents(error=http_error(status))).delete_booking(ROOM, "google-7")


@pytest.mark.parametrize("status,reason", [(403, "forbidden"), (403, "requiredAccessLevel"), (401, "")])
async def test_a_calendar_shared_for_reading_only_refuses(status, reason):
    with pytest.raises(CalendarWriteRefused):
        await provider_with(FakeEvents(error=http_error(status, reason))).create_booking(ROOM, "a", "x", START, END, "")


@pytest.mark.parametrize("status,reason", [(403, "rateLimitExceeded"), (500, ""), (503, "")])
async def test_google_busy_or_failing_is_not_a_refusal(status, reason):
    with pytest.raises(HttpError):
        await provider_with(FakeEvents(error=http_error(status, reason))).create_booking(ROOM, "a", "x", START, END, "")
