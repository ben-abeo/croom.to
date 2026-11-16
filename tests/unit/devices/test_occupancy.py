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
