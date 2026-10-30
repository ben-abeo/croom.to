"""Laptop mode (spec 2026-10-27): the MeetUp lent and given back, the booking, the TV, the devices, a restart, the switch's own button."""
import asyncio
from types import SimpleNamespace

import pytest

from croom.calendar.bookings import RoomBookings, SlotTaken
from croom.control.settings import SettingsStore
from croom.laptop.service import CAMERA_AWAY, HOW_LONG, IN_A_MEETING, SWITCHING, LaptopError, LaptopService
from croom.laptop.switch import NO_ANSWER
from croom.meeting.providers.base import MeetingState
from tests.unit.calendar.test_bookings import ROOM_CAL, Clock, FakeGoogle, at
from tests.unit.laptop.fakes import FakeDevices, FakeTv, FakeUsbSwitch


class Room:
    """Laptop mode on fakes, with the real bookings on a fake Google and a clock the test moves."""

    def __init__(self, tmp_path, calendar=True, switch=None, tv_words=None):
        self.tmp_path = tmp_path
        self.clock = Clock(at(9, 30))
        self.google = FakeGoogle()
        self.rooms = None
        if calendar:
            self.rooms = RoomBookings(tmp_path / "bookings.json", tmp_path / "calendar-cache.json", "Room 1",
                                      booking_start="08:00", booking_end="18:00", clock=self.clock)
            self.rooms.attach(self.google, ROOM_CAL)
        self.calendar = SimpleNamespace(bookings=self.rooms)
        self.meeting = SimpleNamespace(state=MeetingState.IDLE)
        self.switch = switch or FakeUsbSwitch()
        self.devices, self.tv = FakeDevices(), FakeTv(tv_words)
        self.service = self.make()

    def make(self):
        return LaptopService(self.switch, SettingsStore(self.tmp_path / "laptop.json"), calendar=self.calendar, meeting=self.meeting,
                             devices=self.devices, tv=self.tv, clock=self.clock)

    async def look(self):
        await self.service._tick()


@pytest.fixture
def room(tmp_path):
    return Room(tmp_path)


async def test_laptop_with_nothing_booked_books_the_room_and_switches(room):
    state = await room.service.to_laptop(45)
    assert state["holder"] == "laptop" and state["made_booking"] and room.service.camera_away
    [booked] = room.google.events.values()
    assert booked.title == "Laptop" and booked.end_time == at(10, 15)
    assert state["until"] == at(10, 15).isoformat()
    assert room.devices.calls == ["release"] and room.switch.moves == ["laptop"] and room.tv.calls == ["laptop"]


async def test_laptop_during_a_booking_uses_it_and_books_nothing(room):
    room.google.add("Design review", at(9), at(11))
    room.rooms.remember(list(room.google.events.values()))
    state = await room.service.to_laptop()
    assert state["until"] == at(11).isoformat() and not state["made_booking"]
    assert len(room.google.events) == 1


async def test_laptop_with_nothing_booked_asks_how_long(room):
    with pytest.raises(LaptopError) as refused:
        await room.service.to_laptop()
    assert str(refused.value) == HOW_LONG and refused.value.status == 400 and room.switch.moves == []


async def test_a_time_already_taken_is_refused_before_any_press(room):
    room.google.add("Board", at(9, 35), at(10))
    room.rooms.remember(list(room.google.events.values()))
    with pytest.raises(SlotTaken):
        await room.service.to_laptop(30)
    assert room.switch.moves == [] and room.devices.calls == []


@pytest.mark.parametrize("state", [MeetingState.JOINING, MeetingState.CONNECTED])
async def test_laptop_is_refused_while_the_room_is_in_a_meeting(room, state):
    room.meeting.state = state
    with pytest.raises(LaptopError, match=IN_A_MEETING):
        await room.service.to_laptop(30)
    assert room.switch.moves == []


async def test_a_switch_that_does_not_answer_ends_the_booking_it_made(room):
    room.switch.answers = False
    with pytest.raises(LaptopError, match=NO_ANSWER):
        await room.service.to_laptop(30)
    assert room.google.events == {} and not room.service.camera_away
    assert room.devices.calls == ["release", "reclaim"] and room.service.state()["problem"] == NO_ANSWER


async def test_back_to_room_by_hand_ends_the_booking_laptop_made(room):
    await room.service.to_laptop(60)
    room.clock.now = at(9, 50)
    state = await room.service.to_room()
    assert state["holder"] == "room" and not room.service.camera_away
    [booked] = room.google.events.values()
    assert booked.end_time == at(9, 50)                          # ended now, not left holding the room
    assert room.devices.calls == ["release", "reclaim"] and room.tv.calls == ["laptop", "pi"]


async def test_back_to_room_leaves_a_booking_someone_else_made(room):
    room.google.add("Design review", at(9), at(11))
    room.rooms.remember(list(room.google.events.values()))
    await room.service.to_laptop()
    await room.service.to_room()
    [booked] = room.google.events.values()
    assert booked.end_time == at(11)


async def test_the_meetup_goes_back_when_the_booking_ends(room):
    await room.service.to_laptop(30)
    room.clock.now = at(10, 0)
    await room.look()
    assert room.switch.moves == ["laptop", "room"] and not room.service.camera_away
    [booked] = room.google.events.values()
    assert booked.end_time == at(10, 0)                          # it ran its time; nothing was cut short


async def test_the_end_follows_the_booking_as_it_moves_in_google(room):
    gid = room.google.add("Design review", at(9), at(10))
    room.rooms.remember(list(room.google.events.values()))
    await room.service.to_laptop()
    room.google.events[gid].end_time = at(10, 30)
    room.rooms.remember(list(room.google.events.values()))
    await room.look()
    assert room.service.state()["until"] == at(10, 30).isoformat()
    room.clock.now = at(10, 5)
    await room.look()
    assert room.service.camera_away                              # not at the old end
    del room.google.events[gid]
    room.rooms.remember(list(room.google.events.values()))
    await room.look()
    assert room.service.state()["until"] == at(10, 30).isoformat()   # deleted: the last end known is kept


async def test_the_meetup_taken_at_the_switch_shows_laptop_mode_with_no_end(room):
    room.switch.on_pi = False
    await room.look()
    assert not room.service.camera_away and room.devices.calls == []   # one look only records it
    await room.look()
    state = room.service.state()
    assert state["holder"] == "laptop" and state["until"] is None and room.devices.calls == ["release"]


async def test_the_meetup_given_back_at_the_switch_ends_laptop_mode_and_keeps_the_booking(room):
    await room.service.to_laptop(60)
    room.switch.on_pi = True
    await room.look()
    assert room.service.camera_away                                 # one look only records it
    await room.look()
    assert not room.service.camera_away and room.tv.calls == ["laptop", "pi"]
    [booked] = room.google.events.values()
    assert booked.end_time == at(10, 30)


async def test_one_absent_look_during_a_meeting_changes_nothing(room):
    room.meeting.state = MeetingState.CONNECTED
    room.switch.on_pi = False                                     # a reading that lost the MeetUp for a moment
    await room.look()
    room.switch.on_pi = True
    await room.look()
    await room.look()
    assert not room.service.camera_away and room.devices.calls == [] and room.tv.calls == []


async def test_one_present_look_while_lent_changes_nothing(room):
    await room.service.to_laptop(60)
    room.devices.calls.clear()
    room.switch.on_pi = True
    await room.look()
    room.switch.on_pi = False
    await room.look()
    await room.look()
    assert room.service.camera_away and room.devices.calls == []


async def test_the_meetup_given_back_during_a_meeting_is_not_homed(room):
    await room.service.to_laptop(60)
    room.meeting.state = MeetingState.CONNECTED
    room.switch.on_pi = True
    await room.look()
    await room.look()
    assert not room.service.camera_away and room.devices.homes == [False]


async def test_the_meetup_given_back_when_idle_is_homed(room):
    await room.service.to_laptop(60)
    room.switch.on_pi = True
    await room.look()
    await room.look()
    assert room.devices.homes == [True]


async def test_a_new_hand_taken_session_starts_clean(room):
    room.service._problem = NO_ANSWER                             # left over from a session that went wrong
    room.switch.on_pi = False
    await room.look()
    await room.look()
    assert room.service.camera_away and room.service.state()["problem"] is None


async def test_a_restart_keeps_the_time_to_give_it_back(room):
    await room.service.to_laptop(30)
    again = room.make()
    assert again.camera_away and again.state()["until"] == at(10, 0).isoformat()


async def test_a_restart_after_the_end_gives_the_meetup_back_at_once(room):
    await room.service.to_laptop(30)
    room.clock.now = at(10, 20)
    again = room.make()
    room.service = again
    await again.start()
    try:
        assert not again.camera_away and room.switch.moves == ["laptop", "room"]
    finally:
        await again.stop()


async def test_a_room_with_no_calendar_lends_the_meetup_with_no_end(tmp_path):
    room = Room(tmp_path, calendar=False)
    state = await room.service.to_laptop()
    assert state["holder"] == "laptop" and state["until"] is None and not state["made_booking"]
    await room.service.to_room()
    assert not room.service.camera_away


async def test_the_tvs_words_are_the_problem_and_the_switch_goes_on(tmp_path):
    room = Room(tmp_path, tv_words="Choose HDMI 2 on the TV with its Input button")
    state = await room.service.to_laptop(30)
    assert state["holder"] == "laptop" and state["problem"] == "Choose HDMI 2 on the TV with its Input button"


async def test_an_unavailable_switch_refuses_in_its_own_words(tmp_path):
    room = Room(tmp_path, switch=FakeUsbSwitch(available=False))
    with pytest.raises(LaptopError, match="busy"):
        await room.service.to_laptop(30)
    assert room.service.state()["available"] is False and "busy" in room.service.state()["problem"]


async def test_a_second_tap_while_switching_is_told_so(room):
    gate = asyncio.Event()
    moving = room.switch.move_to

    async def slow(holder):
        await gate.wait()
        return await moving(holder)
    room.switch.move_to = slow
    first = asyncio.ensure_future(room.service.to_laptop(30))
    for _ in range(100):
        if room.service.state()["switching"]:
            break
        await asyncio.sleep(0.01)
    assert room.service.state()["switching"]
    with pytest.raises(LaptopError, match="Switching"):
        await room.service.to_room()
    gate.set()
    await first


async def test_a_booking_made_is_withdrawn_even_when_the_switch_gave_up_a_minute_later(room):
    async def slow_and_failing(holder):
        room.clock.now = at(9, 31)                                # the press took long enough for the minute to turn
        return False
    room.switch.move_to = slow_and_failing
    with pytest.raises(LaptopError, match=NO_ANSWER):
        await room.service.to_laptop(30)
    assert room.google.events == {}


async def test_the_watch_holds_the_lock_while_it_changes_the_devices(room):
    gate = asyncio.Event()
    reclaiming = room.devices.reclaim

    async def slow_reclaim(home=True):
        await gate.wait()
        await reclaiming(home)
    await room.service.to_laptop(30)
    room.devices.reclaim = slow_reclaim
    room.devices.calls.clear()
    room.switch.on_pi = True                                      # given back at the switch itself
    await room.look()                                             # the first look agrees with nothing yet
    watching = asyncio.ensure_future(room.look())
    for _ in range(100):
        if room.service.state()["switching"]:
            break
        await asyncio.sleep(0.01)
    assert room.service.state()["switching"]
    with pytest.raises(LaptopError, match="Switching"):
        await room.service.to_laptop(30)
    assert room.devices.calls == [] and room.switch.moves == ["laptop"]
    gate.set()
    await watching
    assert room.devices.calls == ["reclaim"] and room.tv.calls == ["laptop", "pi"]


async def test_a_failed_automatic_return_is_tried_once_until_something_changes(room):
    await room.service.to_laptop(30)
    room.switch.answers = False
    room.clock.now = at(10, 0)
    for _ in range(4):
        await room.look()
    assert room.switch.moves == ["laptop", "room"] and room.service.camera_away
    assert room.service.state()["problem"] == NO_ANSWER
    with pytest.raises(LaptopError):                              # by hand still tries
        await room.service.to_room()
    assert room.switch.moves == ["laptop", "room", "room"]


def test_the_words_for_join_are_the_specs():
    assert CAMERA_AWAY == "The camera is on the laptop: press Back to room first"


def test_the_errors_import_without_the_control_package():
    """croom.control imports Laptop mode's errors; they must not import croom.control back."""
    import subprocess
    import sys
    code = "import croom.laptop.errors, sys; assert 'croom.control' not in sys.modules"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


async def test_join_is_refused_in_words_while_the_meetup_is_on_its_way(room):
    assert room.service.join_refusal() is None
    gate = asyncio.Event()
    moving = room.switch.move_to

    async def slow(holder):
        await gate.wait()
        return await moving(holder)
    room.switch.move_to = slow
    first = asyncio.ensure_future(room.service.to_laptop(30))
    for _ in range(100):
        if room.service.state()["switching"]:
            break
        await asyncio.sleep(0.01)
    assert room.service.join_refusal() == SWITCHING
    gate.set()
    await first
    assert room.service.join_refusal() == CAMERA_AWAY
    await room.service.to_room()
    assert room.service.join_refusal() is None


async def test_the_room_becoming_busy_while_the_booking_is_made_withdraws_it(room):
    starting = room.rooms.start_now

    async def start_then_a_join(minutes, title, source):
        booking = await starting(minutes, title, source)
        room.meeting.state = MeetingState.JOINING
        return booking
    room.rooms.start_now = start_then_a_join
    with pytest.raises(LaptopError, match=IN_A_MEETING):
        await room.service.to_laptop(30)
    assert room.google.events == {} and room.devices.calls == [] and room.switch.moves == [] and not room.service.camera_away


async def test_back_to_room_from_a_stale_screen_does_nothing(room):
    room.meeting.state = MeetingState.CONNECTED
    state = await room.service.to_room()
    assert state["holder"] == "room"
    assert room.devices.calls == [] and room.tv.calls == [] and room.switch.moves == []


async def test_an_unexpected_failure_while_lending_leaves_nothing_held(room, caplog):
    async def breaks(holder):
        raise RuntimeError("the pin went away")
    room.switch.move_to = breaks
    with pytest.raises(LaptopError, match=NO_ANSWER):
        await room.service.to_laptop(30)
    assert room.google.events == {} and not room.service.camera_away
    assert room.devices.calls == ["release", "reclaim"] and room.service.state()["problem"] == NO_ANSWER
    assert "the pin went away" in caplog.text


async def test_an_unexpected_failure_in_release_is_the_same(room):
    async def breaks():
        raise RuntimeError("the camera would not close")
    room.devices.release = breaks
    with pytest.raises(LaptopError, match=NO_ANSWER):
        await room.service.to_laptop(30)
    assert room.google.events == {} and room.devices.calls == ["reclaim"] and room.switch.moves == []


# --- Settings' Test the switch (spec 2026-10-29)


async def test_test_the_switch_moves_the_meetup_over_and_back_and_leaves_the_room_as_it_was(room):
    answer = await room.service.test_switch()
    assert answer["ok"] is True
    text = "\n".join(answer["lines"])
    assert "GPIO17" in text and "now on the laptop side" in text and "back on the Pi" in text
    assert room.switch.moves == ["laptop", "room"] and room.switch.on_pi
    assert room.devices.calls == ["release", "reclaim"]                # the room's devices let go of the MeetUp, and took it back
    assert not room.service.camera_away and not room.switch.closed and not room.switch.opened
    await room.look()
    await room.look()
    assert not room.service.camera_away                               # the watch did not take the test for a hand at the switch


async def test_test_the_switch_that_does_not_move_says_so_and_takes_the_devices_back(room):
    room.switch.answers = False
    answer = await room.service.test_switch()
    assert answer["ok"] is False and any("did not answer" in line for line in answer["lines"])
    assert room.devices.calls == ["release", "reclaim"]


async def test_test_the_switch_with_no_pin_presses_nothing(tmp_path):
    room = Room(tmp_path, switch=FakeUsbSwitch(available=False))
    answer = await room.service.test_switch()
    assert answer["ok"] is False and any("cannot be pressed" in line for line in answer["lines"])
    assert room.switch.moves == [] and room.devices.calls == []


@pytest.mark.parametrize("state", [MeetingState.JOINING, MeetingState.CONNECTED])
async def test_test_the_switch_is_refused_in_a_meeting_and_while_the_laptop_has_the_meetup(room, state):
    room.meeting.state = state
    with pytest.raises(LaptopError, match=IN_A_MEETING):
        await room.service.test_switch()
    room.meeting.state = MeetingState.IDLE
    await room.service.to_laptop(30)
    with pytest.raises(LaptopError, match=CAMERA_AWAY):
        await room.service.test_switch()
    assert room.switch.moves == ["laptop"]
