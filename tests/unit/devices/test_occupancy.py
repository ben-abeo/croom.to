"""Occupancy (devices/occupancy.py): the people in the room counted from a still, only with the setting on, and told to the dashboard."""
import types
from datetime import datetime, timezone

from croom.devices.occupancy import Occupancy

AT = datetime(2026, 11, 16, 14, 0, tzinfo=timezone.utc)


class View:
    def __init__(self):
        self.blocked_by, self.stills, self.picture = None, 0, b"jpeg"

    def blocked(self):
        return self.blocked_by

    async def still(self):
        self.stills += 1
        return self.picture


def room(on=True, people=2):
    view = View()
    seen = types.SimpleNamespace(people=people, sent=[])

    async def report(count):
        seen.sent.append(count)

    occupancy = Occupancy(types.SimpleNamespace(available=True), types.SimpleNamespace(camera_view=view), on=on,
                          count=lambda jpeg: seen.people, report=report, clock=lambda: AT)
    return occupancy, view, seen


async def test_the_people_are_counted_and_the_dashboard_is_told():
    occupancy, view, seen = room()
    await occupancy.tick()
    assert occupancy.state() == {"enabled": True, "count": 2, "at": "2026-11-16T14:00:00+00:00"} and seen.sent == [2]


async def test_an_empty_room_is_told_once_and_not_at_every_look():
    occupancy, view, seen = room()
    await occupancy.tick()
    seen.people = 0
    await occupancy.tick()
    await occupancy.tick()
    assert seen.sent == [2, 0] and occupancy.state()["count"] == 0


async def test_off_it_never_looks_and_turning_it_off_forgets_the_count():
    occupancy, view, seen = room(on=False)
    await occupancy.tick()
    assert view.stills == 0 and occupancy.state() == {"enabled": False, "count": None, "at": None}
    occupancy.set_on(True)
    await occupancy.tick()
    assert occupancy.state()["count"] == 2
    occupancy.set_on(False)
    assert occupancy.state() == {"enabled": False, "count": None, "at": None}


async def test_no_picture_is_not_known_and_never_an_empty_room():
    occupancy, view, seen = room()
    await occupancy.tick()
    view.blocked_by = "laptop"
    await occupancy.tick()
    assert occupancy.state()["count"] is None and view.stills == 1 and seen.sent == [2]
    view.blocked_by, view.picture = None, None
    await occupancy.tick()
    assert occupancy.state()["count"] is None and seen.sent == [2]


# --- a booking nobody came to is released

class Bookings:
    def __init__(self):
        self.now = types.SimpleNamespace(id="g1", booking_id=None, title="Budget review")
        self.ended, self.refuse = [], None

    def in_progress(self):
        return self.now

    async def end(self, key):
        if self.refuse:
            raise ValueError(self.refuse)
        self.ended.append(key)


def releasing(people=0, minutes=3):
    occupancy, view, seen = room(people=people)
    occupancy.set_release(minutes)
    bookings = Bookings()
    occupancy.bookings = lambda: bookings
    return occupancy, view, seen, bookings


async def test_a_booking_nobody_came_to_is_ended_after_the_rooms_minutes_and_once():
    occupancy, view, seen, bookings = releasing()
    await occupancy.tick()
    await occupancy.tick()
    assert bookings.ended == []
    await occupancy.tick()
    assert bookings.ended == ["g1"]
    await occupancy.tick()
    assert bookings.ended == ["g1"]


async def test_anyone_seen_once_keeps_the_booking_even_if_they_step_out():
    occupancy, view, seen, bookings = releasing()
    await occupancy.tick()
    seen.people = 1
    await occupancy.tick()
    seen.people = 0
    for _ in range(6):
        await occupancy.tick()
    assert bookings.ended == []


async def test_a_call_joined_keeps_the_booking_and_a_look_with_no_picture_says_nothing():
    occupancy, view, seen, bookings = releasing()
    occupancy._meeting.state = "connected"                 # anything but idle: the room is in use
    await occupancy.tick()
    occupancy._meeting.state = occupancy_idle()
    for _ in range(5):
        await occupancy.tick()
    assert bookings.ended == []
    other, view, seen, bookings = releasing()
    view.blocked_by = "laptop"                             # Laptop mode: no picture, so nobody is known to be absent
    for _ in range(5):
        await other.tick()
    assert bookings.ended == []


async def test_the_next_booking_starts_its_own_count_and_nought_minutes_never_releases():
    occupancy, view, seen, bookings = releasing()
    await occupancy.tick()
    await occupancy.tick()
    bookings.now = types.SimpleNamespace(id="g2", booking_id="b2", title="Stand-up")
    await occupancy.tick()
    await occupancy.tick()
    assert bookings.ended == []
    await occupancy.tick()
    assert bookings.ended == ["b2"]                        # the room's own booking, by its own tag
    never, view, seen, bookings = releasing(minutes=0)
    for _ in range(10):
        await never.tick()
    assert bookings.ended == []


async def test_a_booking_that_cannot_be_ended_is_left_and_not_tried_again(caplog):
    occupancy, view, seen, bookings = releasing(minutes=1)
    bookings.refuse = "That meeting has already ended"
    await occupancy.tick()
    await occupancy.tick()
    assert bookings.ended == [] and occupancy.state()["count"] == 0


def occupancy_idle():
    from croom.meeting.providers.base import MeetingState
    return MeetingState.IDLE
