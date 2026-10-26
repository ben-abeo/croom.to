"""
The room page's and the door sign's booking routes (spec 2026-10-25): the open times of a day, Book, Use now, End now and Cancel,
what they refuse and why, and the booking block of the status.
"""

from datetime import date, datetime, time, timedelta

import pytest

from croom.calendar.bookings import RoomBookings
from tests.unit.calendar.test_bookings import ROOM_CAL, FakeGoogle
from tests.unit.control.test_service import StubCalendar, client_factory, make_service  # noqa: F401  (client_factory is a fixture)


class BookingCalendar(StubCalendar):
    """The calendar service as the control service sees it, with the room's own bookings."""

    def __init__(self, bookings, events=()):
        super().__init__(events=events)
        self._google_events = list(events)
        self.bookings = bookings

    @property
    def events(self):
        return self.bookings.merge(self._google_events)

    @events.setter
    def events(self, value):
        self._google_events = list(value)

    def get_event_by_id(self, event_id):
        return next((e for e in self.events if e.id == event_id), None)


def tomorrow_at(hour):
    """A start that is open whatever the time the tests run at (a start today could be past the end of the booking hours)."""
    return datetime.combine(date.today() + timedelta(days=1), time(hour)).astimezone()


@pytest.fixture
def google():
    return FakeGoogle()


@pytest.fixture
def rooms(tmp_path, google):
    rooms = RoomBookings(tmp_path / "bookings.json", tmp_path / "calendar-cache.json", "Lab", booking_start="00:00", booking_end="23:45")
    rooms.attach(google, ROOM_CAL)
    return rooms


async def client_for(client_factory, rooms, events=()):  # noqa: F811
    return await client_factory(make_service(calendar=BookingCalendar(rooms, events)))


async def test_the_open_times_of_a_day(client_factory, rooms):  # noqa: F811
    client = await client_for(client_factory, rooms)
    data = await (await client.get("/api/bookings/slots")).json()
    today = date.today()
    assert [d["date"] for d in data["days"]] == [(today + timedelta(days=n)).isoformat() for n in range(7)]
    assert data["days"][0]["label"] == "Today" and data["date"] == today.isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    data = await (await client.get("/api/bookings/slots", params={"date": tomorrow})).json()
    assert data["date"] == tomorrow and data["starts"][0]["start"].startswith(tomorrow + "T00:00")
    assert (await client.get("/api/bookings/slots", params={"date": "next tuesday"})).status == 400


async def test_book_then_cancel(client_factory, rooms, google):  # noqa: F811
    client = await client_for(client_factory, rooms)
    start = tomorrow_at(10)
    response = await client.post("/api/bookings", json={"start": start.isoformat(), "minutes": 30, "title": "Private review",
                                                         "source": "door sign"})
    assert response.status == 201
    data = await response.json()
    assert data["booking"]["title"] == "Private review" and data["booking"]["booking_id"] and data["pending"] is False
    assert [e.title for e in google.events.values()] == ["Private review"]
    events = (await (await client.get("/api/calendar/week")).json())["days"]
    assert any(e["booking_id"] == data["booking"]["booking_id"] for day in events for e in day["events"])
    response = await client.post(f"/api/bookings/{data['booking']['booking_id']}/cancel", json={})
    assert response.status == 200 and google.events == {}


async def test_use_now_then_end_now(client_factory, rooms, google):  # noqa: F811
    client = await client_for(client_factory, rooms)
    response = await client.post("/api/bookings/now", json={"minutes": 45, "title": "Ben", "source": "room page"})
    assert response.status == 201
    booking = (await response.json())["booking"]
    status = await (await client.get("/api/status")).json()
    assert status["calendar"]["booking"]["now"]["minutes"] == []          # the room is taken now
    assert [e["title"] for e in (await (await client.get("/api/calendar/events")).json())["events"]] == ["Ben"]
    refused = await client.post(f"/api/bookings/{booking['booking_id']}/end", json={"source": "door sign"})
    assert refused.status == 403 and "touch screen" in (await refused.json())["error"]      # never from the hallway
    response = await client.post(f"/api/bookings/{booking['booking_id']}/end", json={"source": "room page"})
    assert response.status == 200
    status = await (await client.get("/api/status")).json()
    assert status["calendar"]["booking"]["now"]["minutes"][-1] == 240


async def test_what_is_refused_and_why(client_factory, rooms, google):  # noqa: F811
    client = await client_for(client_factory, rooms)
    start = tomorrow_at(10)
    good = {"start": start.isoformat(), "minutes": 30, "title": "Private review", "source": "door sign"}
    assert (await client.post("/api/bookings", json=good)).status == 201
    taken = await client.post("/api/bookings", json=dict(good, title="Second"))
    assert taken.status == 409 and "already booked" in (await taken.json())["error"]
    for bad in [dict(good, minutes="thirty"), dict(good, start="2pm"), dict(good, start=start.replace(tzinfo=None).isoformat()),
                dict(good, title=""), dict(good, source="hallway"), dict(good, minutes=20)]:
        response = await client.post("/api/bookings", json=bad)
        assert response.status == 400, bad
    assert (await client.post("/api/bookings", data="start=now")).status == 415   # JSON only, as every POST of the room's
    assert (await client.post("/api/bookings/nothing-like-it/end", json={"source": "room page"})).status == 404


async def test_a_booking_made_while_google_is_down_is_accepted_and_waits(client_factory, rooms, google):  # noqa: F811
    google.up = False
    client = await client_for(client_factory, rooms)
    response = await client.post("/api/bookings/now", json={"minutes": 15, "title": "Ben", "source": "door sign"})
    assert response.status == 201 and (await response.json())["pending"] is True
    booking = (await (await client.get("/api/status")).json())["calendar"]["booking"]
    assert booking["online"] is False and booking["pending"] == 1


async def test_a_calendar_that_refuses_says_so_with_the_booking(client_factory, rooms, google):  # noqa: F811
    google.refuse = True
    client = await client_for(client_factory, rooms)
    response = await client.post("/api/bookings/now", json={"minutes": 15, "title": "Ben", "source": "door sign"})
    data = await response.json()
    assert response.status == 201 and data["pending"] is True and "Make changes to events" in data["warning"]


async def test_a_room_without_bookings_says_so(client_factory):  # noqa: F811
    client = await client_factory(make_service(calendar=StubCalendar()))
    assert (await client.get("/api/bookings/slots")).status == 503
    assert (await client.post("/api/bookings/now", json={"minutes": 15, "title": "Ben", "source": "door sign"})).status == 503
    status = await (await client.get("/api/status")).json()
    assert status["calendar"]["booking"] is None
