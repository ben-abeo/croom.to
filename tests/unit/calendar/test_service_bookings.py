"""
The calendar service with the room's own bookings (spec 2026-10-25): the room's bookings in what it shows, the sync on every poll,
and the calendar kept for a restart while Google is out of reach.
"""

from datetime import datetime, timedelta

from croom.calendar.bookings import RoomBookings
from croom.calendar.service import CalendarService
from croom.core.config import Config
from tests.unit.calendar.test_bookings import ROOM_CAL, FakeGoogle


class FakeProvider(FakeGoogle):
    """The Google provider as the calendar service drives it: sign-in, the calendar lookup and the events."""

    name = "google"

    def __init__(self):
        super().__init__()
        self.can_sign_in = True

    async def authenticate(self, credentials):
        return self.can_sign_in and self.up

    async def get_calendar(self, calendar_id):
        self._check()
        return {"id": calendar_id, "name": "Room 1"}


def service(tmp_path, provider):
    calendar = CalendarService(config={
        "provider": "google", "credentials": {"service_account_file": "key.json"}, "calendar_ids": [ROOM_CAL],
        "poll_interval": 3600, "bookings": RoomBookings(tmp_path / "bookings.json", tmp_path / "calendar-cache.json", "Room 1"),
    })
    calendar.PROVIDERS = {"google": lambda: provider}
    return calendar


def soon(minutes):
    return (datetime.now().astimezone() + timedelta(minutes=minutes)).replace(second=0, microsecond=0)


async def test_the_rooms_bookings_are_in_what_the_room_shows(tmp_path):
    google = FakeProvider()
    google.add("Board meeting", soon(120), soon(180))
    calendar = service(tmp_path, google)
    await calendar.start()
    booking = await calendar.bookings.start_now(15, "Ben", "door sign")
    assert [e.title for e in calendar.events] == ["Ben", "Board meeting"]
    assert calendar.get_event_by_id(booking.google_id).booking_id == booking.id
    await calendar.stop()


async def test_a_poll_sends_what_waits_before_it_reads(tmp_path):
    google = FakeProvider()
    calendar = service(tmp_path, google)
    await calendar.start()
    google.up = False
    await calendar.bookings.start_now(15, "Ben", "door sign")
    await calendar.refresh()
    assert calendar.connected is False and calendar.bookings.state()["online"] is False
    google.up = True
    await calendar.refresh()
    assert calendar.connected is True and [e.title for e in google.events.values()] == ["Ben"]
    assert [(e.title, e.pending) for e in calendar.events] == [("Ben", False)]
    await calendar.stop()


async def test_a_restart_without_google_shows_the_last_calendar_read(tmp_path):
    google = FakeProvider()
    google.add("Board meeting", soon(120), soon(180))
    first = service(tmp_path, google)
    await first.start()
    await first.stop()
    google.up = False
    again = service(tmp_path, google)
    await again.start()
    assert again.connected is False
    assert [e.title for e in again.events] == ["Board meeting"]
    assert again.bookings.state()["offline_since"] is not None
    await again.bookings.start_now(15, "Ben", "door sign")   # and the room still books
    assert [e.title for e in again.events] == ["Ben", "Board meeting"]
    await again.stop()


def test_the_config_names_the_files_and_the_hours(tmp_path):
    config = Config.from_dict({
        "room": {"name": "Room 2"}, "data_dir": str(tmp_path),
        "calendar": {"providers": ["google"], "google_credentials_path": str(tmp_path / "key.json"),
                     "google_calendar_id": ROOM_CAL, "booking_start": "07:30", "booking_end": "19:00"},
    })
    bookings = CalendarService.from_config(config).bookings
    assert bookings.store_path == tmp_path / "bookings.json" and bookings.cache_path == tmp_path / "calendar-cache.json"
    assert bookings.room_name == "Room 2" and bookings.state()["hours"] == {"start": "07:30", "end": "19:00"}


def test_a_room_without_a_calendar_has_no_bookings(tmp_path):
    config = Config.from_dict({"data_dir": str(tmp_path), "calendar": {"providers": []}})
    assert CalendarService.from_config(config).bookings is None


def test_bad_booking_hours_fall_back_to_the_defaults(tmp_path, caplog):
    config = Config.from_dict({
        "data_dir": str(tmp_path),
        "calendar": {"providers": ["google"], "google_calendar_id": ROOM_CAL, "booking_start": "6pm", "booking_end": "08:00"},
    })
    bookings = CalendarService.from_config(config).bookings
    assert bookings.state()["hours"] == {"start": "08:00", "end": "18:00"}
    assert "booking hours" in caplog.text
