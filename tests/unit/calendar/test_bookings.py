"""
Room bookings made at the door sign and the room page (spec 2026-10-25): the open start times and the durations each allows,
Use now, the bookings kept on the device and in the calendar cache, and their sync with Google, which may be down, may refuse
the write, or may have taken the time meanwhile.
"""

from datetime import date, datetime, time, timedelta

import pytest

from croom.calendar.bookings import (
    BookingError,
    RoomBookings,
    SlotTaken,
    UnknownBooking,
    now_ends,
    now_minutes,
    open_starts,
)
from croom.calendar.providers.base import CalendarEvent, CalendarWriteRefused

ROOM_CAL = "c_room@resource.calendar.google.com"
MONDAY = date(2026, 10, 26)


def at(hour, minute=0, day=MONDAY):
    """A time on the device's own clock, as the room keeps its day."""
    return datetime.combine(day, time(hour, minute)).astimezone()


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeGoogle:
    """The room's Google calendar: its events, and whether it is up, refuses writes, or loses the answer to a create."""

    def __init__(self):
        self.events = {}        # google id -> CalendarEvent
        self.up = True
        self.refuse = False
        self.lose_create_answer = False
        self.calls = []
        self._next = 0

    def add(self, title, start, end, booking_id=None):
        self._next += 1
        gid = f"g{self._next}"
        self.events[gid] = CalendarEvent(id=gid, title=title, start_time=start, end_time=end, calendar_id=ROOM_CAL, booking_id=booking_id)
        return gid

    def _check(self, writing=False):
        if not self.up:
            raise ConnectionError("Google is down")
        if writing and self.refuse:
            raise CalendarWriteRefused("403 Forbidden: the calendar is shared as See all event details")

    async def get_events(self, calendar_id, time_min, time_max, max_results=100):
        self.calls.append(("list",))
        self._check()
        return [e for e in self.events.values() if e.start_time < time_max and e.end_time > time_min]

    async def find_booking(self, calendar_id, booking_id):
        self.calls.append(("find", booking_id))
        self._check()
        return next((gid for gid, e in self.events.items() if e.booking_id == booking_id), None)

    async def create_booking(self, calendar_id, booking_id, title, start, end, description):
        self.calls.append(("create", title))
        self._check(writing=True)
        gid = self.add(title, start, end, booking_id=booking_id)
        self.events[gid].description = description
        if self.lose_create_answer:
            self.lose_create_answer = False
            raise ConnectionError("the answer was lost")
        return gid

    async def update_booking(self, calendar_id, google_id, title, start, end):
        self.calls.append(("update", google_id))
        self._check(writing=True)
        e = self.events[google_id]
        e.title, e.start_time, e.end_time = title, start, end

    async def delete_booking(self, calendar_id, google_id):
        self.calls.append(("delete", google_id))
        self._check(writing=True)
        self.events.pop(google_id, None)

    def writes(self):
        return [c for c in self.calls if c[0] in ("create", "update", "delete")]


@pytest.fixture
def clock():
    return Clock(at(9, 30))


@pytest.fixture
def google():
    return FakeGoogle()


def make(tmp_path, clock, google=None, start="08:00", end="18:00"):
    rooms = RoomBookings(tmp_path / "bookings.json", tmp_path / "calendar-cache.json", "Room 1",
                         booking_start=start, booking_end=end, clock=clock)
    if google is not None:
        rooms.attach(google, ROOM_CAL)
    return rooms


async def fetched(rooms, google):
    """A calendar poll: what Google has now, handed to the bookings as the calendar service does."""
    events = await google.get_events(ROOM_CAL, rooms.clock() - timedelta(hours=1), rooms.clock() + timedelta(days=7))
    rooms.remember(events)
    return rooms.merge(events)


# ----------------------------------------------------------------------------------------------------------------------
# The open start times and the durations


def starts_of(slots):
    return [s.strftime("%H:%M") for s, _ in slots]


def test_a_free_day_offers_every_quarter_hour_of_the_booking_hours():
    slots = open_starts(MONDAY, at(7), [], time(8), time(18))
    assert starts_of(slots)[0] == "08:00" and starts_of(slots)[-1] == "17:45" and len(slots) == 40
    first = dict(slots)[at(8)]
    assert first[0] == 15 and first[-1] == 240 and first == list(range(15, 241, 15))   # four hours at most
    assert dict(slots)[at(17)] == [15, 30, 45, 60]                                    # never past the end of the hours


def test_a_booking_takes_its_starts_and_cuts_the_durations_before_it():
    busy = [(at(10), at(11), "Board meeting")]
    slots = dict(open_starts(MONDAY, at(7), busy, time(8), time(18)))
    assert at(10) not in slots and at(10, 45) not in slots and at(11) in slots
    assert slots[at(9, 15)] == [15, 30, 45]          # until the board meeting
    assert slots[at(9, 45)] == [15]


def test_today_starts_at_the_next_quarter_hour():
    assert starts_of(open_starts(MONDAY, at(10, 7), [], time(8), time(18)))[0] == "10:15"
    assert starts_of(open_starts(MONDAY, at(10, 15), [], time(8), time(18)))[0] == "10:15"
    assert open_starts(MONDAY, at(18, 1), [], time(8), time(18)) == []


def test_use_now_runs_until_the_next_booking_at_most_four_hours():
    assert now_minutes(at(9, 30), []) == (list(range(15, 241, 15)), None)
    assert now_minutes(at(9, 30), [(at(10, 20), at(11), "Standup")]) == ([15, 30, 45], at(10, 20))
    assert now_minutes(at(9, 30), [(at(9, 40), at(11), "Standup")]) == ([], at(9, 40))      # too soon: nothing to offer
    assert now_minutes(at(9, 30), [(at(9), at(10), "Standup")]) == ([], None)                # taken now


# ----------------------------------------------------------------------------------------------------------------------
# Booking with Google up


async def test_a_booking_goes_to_google_tagged_as_the_rooms_own(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google)
    booking = await rooms.book(at(14), 45, "Private review", "door sign")
    created = google.events[booking.google_id]
    assert (created.title, created.start_time, created.end_time) == ("Private review", at(14), at(14, 45))
    assert created.booking_id == booking.id and created.description == "Booked on the Room 1 door sign"
    merged = await fetched(rooms, google)
    assert [(e.title, e.booking_id, e.pending) for e in merged] == [("Private review", booking.id, False)]   # once, not twice
    assert rooms.state()["pending"] == 0


def test_use_now_ends_on_the_quarter_hour_nearest_the_length_asked():
    """At 12:03 half an hour is until 12:30, not 12:33 (Ben, 2026-10-26): Use now ends on a quarter hour, the one nearest the length asked."""
    assert [(m, e.strftime("%H:%M")) for m, e in now_ends(at(12, 3), [])][:4] == [(15, "12:15"), (30, "12:30"), (45, "12:45"), (60, "13:00")]
    assert [(m, e.strftime("%H:%M")) for m, e in now_ends(at(12, 8), [])][:2] == [(15, "12:30"), (30, "12:45")]   # 12:23 is nearer 12:30
    assert [e.strftime("%H:%M") for _, e in now_ends(at(12, 7, ) + timedelta(seconds=30), [])][:1] == ["12:30"]    # halfway goes on to the later one
    assert now_minutes(at(12, 3), [(at(12, 15), at(13), "Standup")]) == ([15], at(12, 15))   # until 12:15, just before the standup
    assert now_minutes(at(12, 9), [(at(12, 15), at(13), "Standup")]) == ([], at(12, 15))     # 12:24 is nearer 12:30: past it


async def test_use_now_starts_this_minute_and_ends_on_a_quarter_hour(tmp_path, clock, google):
    clock.now = at(9, 31) + timedelta(seconds=40)
    rooms = make(tmp_path, clock, google)
    booking = await rooms.start_now(30, "Ben", "room page")
    assert (booking.start, booking.end) == (at(9, 31), at(10, 0))
    assert google.events[booking.google_id].description == "Started on the Room 1 room page"


async def test_a_time_already_booked_is_refused(tmp_path, clock, google):
    google.add("Board meeting", at(14), at(15))
    rooms = make(tmp_path, clock, google)
    await fetched(rooms, google)
    with pytest.raises(SlotTaken):
        await rooms.book(at(14, 30), 30, "Private review", "door sign")
    with pytest.raises(SlotTaken):
        await rooms.book(at(13, 45), 30, "Private review", "door sign")   # runs into it
    assert google.writes() == []


async def test_a_time_taken_in_google_since_the_last_poll_is_refused_and_nothing_is_kept(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google)
    await fetched(rooms, google)
    google.add("Booked a moment ago", at(14), at(15))
    with pytest.raises(SlotTaken) as taken:
        await rooms.book(at(14), 30, "Private review", "door sign")
    assert "Booked a moment ago" in str(taken.value)
    assert google.writes() == [] and rooms.merge([]) == [] and rooms.state()["pending"] == 0


async def test_what_is_not_offered_cannot_be_booked(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google)
    for start, minutes, title in [
        (at(14, 5), 30, "Off the quarter hour"),
        (at(7, 45), 30, "Before the hours"),
        (at(17, 45), 30, "Past the end of the hours"),
        (at(9), 30, "In the past"),
        (at(14), 20, "Not a quarter hour"),
        (at(14), 255, "Longer than four hours"),
        (at(14, day=MONDAY + timedelta(days=7)), 30, "A week out"),
        (at(14), 30, ""),
        (at(14), 30, "x" * 61),
    ]:
        with pytest.raises(BookingError):
            await rooms.book(start, minutes, title, "door sign")
    with pytest.raises(BookingError):
        await rooms.book(at(14), 30, "Fine", "the hallway")   # a source the room does not have
    assert google.writes() == []


async def test_the_booking_hours_are_the_rooms_own(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google, start="07:00", end="19:30")
    tuesday = MONDAY + timedelta(days=1)
    slots = rooms.slots(tuesday)
    assert slots["date"] == tuesday.isoformat()
    assert slots["starts"][0] == {"start": at(7, day=tuesday).isoformat(), "durations": list(range(15, 241, 15))}
    assert slots["starts"][-1] == {"start": at(19, 15, day=tuesday).isoformat(), "durations": [15]}   # the last start before 7:30 PM
    with pytest.raises(ValueError):
        make(tmp_path, clock, google, start="18:00", end="08:00")


async def test_end_now_shortens_the_booking_in_progress(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google)
    booking = await rooms.start_now(60, "Ben", "door sign")
    clock.now = at(9, 52)
    await rooms.end(booking.id)
    assert google.events[booking.google_id].end_time == at(9, 52)
    assert now_minutes(clock.now, rooms.busy())[0][-1] == 240   # the room is free again


async def test_cancel_takes_a_future_booking_out_of_google(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google)
    booking = await rooms.book(at(15), 30, "Interview", "room page")
    await rooms.cancel(booking.id)
    assert google.events == {} and rooms.merge([]) == []


async def test_end_and_cancel_are_only_for_the_rooms_own_bookings(tmp_path, clock, google):
    gid = google.add("Booked in Google", at(9), at(10))
    rooms = make(tmp_path, clock, google)
    await fetched(rooms, google)
    with pytest.raises(UnknownBooking):
        await rooms.end(gid)
    with pytest.raises(UnknownBooking):
        await rooms.cancel("nothing-like-it")


async def test_a_booking_made_here_and_found_only_in_google_can_still_be_ended(tmp_path, clock, google):
    """The device lost its bookings file (a new card): the tag on the Google event is still the room's."""
    gid = google.add("Ben", at(9), at(10), booking_id="abc123")
    rooms = make(tmp_path, clock, google)
    merged = await fetched(rooms, google)
    assert merged[0].booking_id == "abc123"
    await rooms.end("abc123")
    assert google.events[gid].end_time == at(9, 30)


# ----------------------------------------------------------------------------------------------------------------------
# Booking with Google down, and the sync when it is back


async def test_a_booking_made_while_google_is_down_is_kept_and_sent_when_it_is_back(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google)
    google.up = False
    booking = await rooms.book(at(14), 30, "Private review", "door sign")
    assert booking.google_id is None
    merged = rooms.merge([])
    assert [(e.title, e.pending) for e in merged] == [("Private review", True)]
    assert rooms.state()["pending"] == 1
    google.up = True
    await rooms.sync()
    assert [e.title for e in google.events.values()] == ["Private review"] and rooms.state()["pending"] == 0


async def test_a_booking_survives_a_restart_before_it_is_sent(tmp_path, clock, google):
    google.up = False
    first = make(tmp_path, clock, google)
    await first.book(at(14), 30, "Private review", "door sign")
    again = make(tmp_path, clock, google)          # the device restarted
    assert [e.title for e in again.merge([])] == ["Private review"]
    google.up = True
    await again.sync()
    assert [e.title for e in google.events.values()] == ["Private review"]


async def test_changes_made_offline_reach_google_in_order(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google)
    google.up = False
    kept = await rooms.start_now(60, "Ben", "door sign")
    dropped = await rooms.book(at(15), 30, "Interview", "door sign")
    clock.now = at(9, 45)
    await rooms.end(kept.id)
    await rooms.cancel(dropped.id)
    assert [e.title for e in rooms.merge([])] == ["Ben"]
    google.up = True
    await rooms.sync()
    assert [(e.title, e.end_time) for e in google.events.values()] == [("Ben", at(9, 45))]
    assert [c[0] for c in google.writes()] == ["create"]   # the cancelled one never went, and the ended one went ended


async def test_a_clash_made_in_google_meanwhile_is_written_and_flagged(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google)
    google.up = False
    future = await rooms.book(at(14), 30, "Private review", "door sign")
    started = await rooms.start_now(60, "Ben", "door sign")
    google.up = True
    google.add("Booked in Google meanwhile", at(14), at(15))
    google.add("Also meanwhile", at(9, 45), at(10, 15))
    await rooms.sync()
    assert {e.title for e in google.events.values()} >= {"Private review", "Ben"}   # the room's are written whatever Google has
    merged = {e.title: e for e in await fetched(rooms, google)}
    assert merged["Private review"].clash == "Booked in Google meanwhile"
    assert merged["Ben"].clash == "Also meanwhile"
    assert merged["Booked in Google meanwhile"].clash is None   # the flag is on the room's own booking
    assert future.id != started.id


async def test_a_create_whose_answer_was_lost_is_not_made_twice(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google)
    google.lose_create_answer = True
    await rooms.book(at(14), 30, "Private review", "door sign")
    assert rooms.state()["pending"] == 1
    await rooms.sync()
    assert [e.title for e in google.events.values()] == ["Private review"] and rooms.state()["pending"] == 0


async def test_a_calendar_that_refuses_changes_keeps_the_booking_and_says_why(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google)
    google.refuse = True
    await rooms.book(at(14), 30, "Private review", "door sign")
    state = rooms.state()
    assert state["pending"] == 1 and "Make changes to events" in state["problem"]
    google.refuse = False
    await rooms.sync()
    assert rooms.state() ["pending"] == 0 and rooms.state()["problem"] is None
    assert [e.title for e in google.events.values()] == ["Private review"]


# ----------------------------------------------------------------------------------------------------------------------
# What Google says wins for what the room has no change of its own waiting on, and the cache


async def test_a_booking_moved_or_deleted_in_google_follows_google(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google)
    moved = await rooms.book(at(14), 30, "Moved", "door sign")
    gone = await rooms.book(at(16), 30, "Deleted", "door sign")
    google.events[moved.google_id].start_time, google.events[moved.google_id].end_time = at(15), at(15, 30)
    del google.events[gone.google_id]
    merged = await fetched(rooms, google)
    assert [(e.title, e.start_time) for e in merged] == [("Moved", at(15))]


async def test_the_last_calendar_read_is_kept_for_a_restart_without_google(tmp_path, clock, google):
    google.add("Board meeting", at(14), at(15))
    rooms = make(tmp_path, clock, google)
    await fetched(rooms, google)
    again = make(tmp_path, clock)
    events, saved_at = again.cached()
    assert [(e.title, e.start_time, e.end_time) for e in events] == [("Board meeting", at(14), at(15))]
    assert saved_at == at(9, 30)


async def test_the_state_says_when_the_calendar_went_offline_and_what_use_now_offers(tmp_path, clock, google):
    google.add("Standup", at(10, 15), at(10, 30))
    rooms = make(tmp_path, clock, google)
    await fetched(rooms, google)
    rooms.set_online(False)
    clock.now = at(9, 40)
    state = rooms.state()
    assert state["online"] is False and state["offline_since"] == at(9, 30).isoformat()
    assert state["now"] == {"minutes": [15, 30], "ends": [at(10).isoformat(), at(10, 15).isoformat()], "until": at(10, 15).isoformat()}


async def test_the_booking_in_progress_is_googles_or_the_rooms_own(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google)
    assert rooms.in_progress() is None
    gid = google.add("Design review", at(9), at(10))
    rooms.remember(list(google.events.values()))
    assert rooms.in_progress().id == gid
    clock.now = at(10, 5)
    assert rooms.in_progress() is None                  # it ended at 10:00
    booking = await rooms.start_now(30, "Laptop", "room page")
    assert rooms.in_progress().booking_id == booking.id


async def test_a_declined_or_cancelled_booking_is_not_in_progress(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google)
    gid = google.add("Declined", at(9), at(10))
    google.events[gid].response_status = "declined"
    rooms.remember(list(google.events.values()))
    assert rooms.in_progress() is None


async def test_a_booking_is_found_by_the_rooms_tag_or_googles_id(tmp_path, clock, google):
    rooms = make(tmp_path, clock, google)
    booking = await rooms.start_now(30, "Laptop", "room page")
    assert rooms.find(booking.id).end_time == booking.end
    assert rooms.find(booking.google_id).booking_id == booking.id
    assert rooms.find("nothing-like-it") is None
