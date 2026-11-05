"""
Auto framing (the room's own RightSight): a call is framed once people have sat down; then it is framed again only when someone has
come in, reached the edge or left for long enough to believe, never sooner than a rest after the last time; a person who moves the
camera by hand pauses it for the rest of the call, and their Frame people gives the camera back. Driven a step (tick) at a time on the
Rally's stand-in, with the call, its picture and the face detector as fakes.
"""

import types

from croom.devices.autoframe import AutoFramer
from croom.meeting.providers.base import MeetingState
from tests.unit.devices.fake_v4l2 import RALLY
from tests.unit.devices.test_camera import camera_for

SIZE = (1280, 720)
TWO = [(400, 300, 60, 60), (800, 300, 60, 60)]
THREE = TWO + [(600, 320, 60, 60)]


class View:
    def __init__(self):
        self.blocked_by = None
        self.stills = 0

    def blocked(self):
        return self.blocked_by

    async def still(self):
        self.stills += 1
        return b"jpeg"


class Room:
    """The call (the meeting service as Auto framing sees it), its picture, and who is in it."""

    def __init__(self, faces=TWO):
        self.meeting = types.SimpleNamespace(state=MeetingState.CONNECTED, camera_view=View())
        self.faces = list(faces)

    def detect(self, jpeg):
        return list(self.faces), SIZE


async def framer_for(faces=TWO, mode="dynamic"):
    camera, v4l2, clock = camera_for(nodes={"/dev/video0": RALLY})
    await camera.discover()
    room = Room(faces)
    framer = AutoFramer(camera, room.meeting, mode=mode, detect=room.detect, clock=clock, sleep=clock.sleep)
    framings = []
    frame_people = camera.frame_people

    async def counted(*args, **kwargs):
        framings.append(kwargs.get("by_person", True))
        return await frame_people(*args, **kwargs)

    camera.frame_people = counted
    return framer, camera, room, clock, framings


async def looks(framer, clock, n):
    for _ in range(n):
        clock.now += framer.LOOK_S
        await framer.tick()


async def framed_at_the_start(framer, clock):
    await framer.tick()                                   # the call is seen: people sit down first
    clock.now += framer.START_S
    await framer.tick()


async def test_a_call_is_framed_once_people_have_sat_down_and_not_before():
    framer, camera, room, clock, framings = await framer_for()
    await framer.tick()
    assert framings == [] and camera.state()["auto_framing"] == "on"
    clock.now += framer.START_S
    await framer.tick()
    assert framings == [False]                            # Auto framing's own, not a person's
    await looks(framer, clock, 10)                        # the same two people: nothing more
    assert framings == [False]


async def test_someone_coming_in_is_framed_after_a_few_looks_and_not_for_one():
    framer, camera, room, clock, framings = await framer_for()
    await framed_at_the_start(framer, clock)
    clock.now += framer.REST_S
    room.faces = THREE
    await looks(framer, clock, 1)
    room.faces = TWO                                      # a passing glimpse
    await looks(framer, clock, 1)
    room.faces = THREE
    await looks(framer, clock, framer.MORE_LOOKS - 1)
    assert len(framings) == 1
    await looks(framer, clock, 1)
    assert len(framings) == 2


async def test_someone_leaving_takes_longer_to_believe_and_framing_rests_between():
    framer, camera, room, clock, framings = await framer_for(THREE)
    await framed_at_the_start(framer, clock)
    clock.now += framer.REST_S
    room.faces = TWO
    await looks(framer, clock, framer.FEWER_LOOKS - 1)
    assert len(framings) == 1
    await looks(framer, clock, 1)                         # long enough to believe, and past the rest: framed again
    assert len(framings) == 2
    room.faces = THREE                                    # back at once: believed after MORE_LOOKS, but not within the rest
    await looks(framer, clock, framer.MORE_LOOKS)
    assert len(framings) == 2
    await looks(framer, clock, int(framer.REST_S // framer.LOOK_S))
    assert len(framings) == 3


async def test_a_person_moving_the_camera_pauses_it_until_their_frame_people():
    framer, camera, room, clock, framings = await framer_for()
    await framed_at_the_start(framer, clock)
    await camera.move(1, 0)                               # an arrow during the call
    await camera.stop()
    clock.now += framer.LOOK_S
    await framer.tick()
    assert camera.state()["auto_framing"] == "paused"
    stills = room.meeting.camera_view.stills
    room.faces = THREE
    await looks(framer, clock, 20)
    assert len(framings) == 1 and room.meeting.camera_view.stills == stills   # not even a look
    await camera.frame_people(room.meeting.camera_view.still, detect=room.detect)   # the person's tap
    await looks(framer, clock, 1)
    assert camera.state()["auto_framing"] == "on"


async def test_a_new_call_is_framed_afresh_and_a_hand_move_before_it_does_not_count():
    framer, camera, room, clock, framings = await framer_for()
    await camera.move(0, 1)                               # someone set the camera up before the call
    await camera.stop()
    await framed_at_the_start(framer, clock)
    assert framings == [False]
    room.meeting.state = MeetingState.IDLE
    await looks(framer, clock, 1)
    assert camera.state()["auto_framing"] is None
    room.meeting.state = MeetingState.CONNECTED
    await framed_at_the_start(framer, clock)
    assert framings == [False, False]


async def test_it_does_nothing_when_off_outside_a_call_or_with_the_call_camera_off():
    framer, camera, room, clock, framings = await framer_for(mode="off")
    await framed_at_the_start(framer, clock)
    await looks(framer, clock, 5)
    assert framings == [] and room.meeting.camera_view.stills == 0 and camera.state()["auto_framing"] is None
    framer.set_mode("dynamic")
    room.meeting.camera_view.blocked_by = "camera_off"
    await framed_at_the_start(framer, clock)
    assert framings == [] and room.meeting.camera_view.stills == 0
    room.meeting.camera_view.blocked_by = None
    await framed_at_the_start(framer, clock)
    assert framings == [False]


async def test_at_the_start_of_calls_it_frames_once_per_call_and_then_leaves_the_camera_alone():
    framer, camera, room, clock, framings = await framer_for(mode="call_start")
    await framed_at_the_start(framer, clock)
    assert framings == [False] and camera.state()["auto_framing"] is None
    stills = room.meeting.camera_view.stills
    room.faces = THREE
    await looks(framer, clock, 20)
    assert framings == [False] and room.meeting.camera_view.stills == stills      # no more looks in this call
    room.meeting.state = MeetingState.IDLE
    await looks(framer, clock, 1)
    room.meeting.state = MeetingState.CONNECTED
    await framed_at_the_start(framer, clock)
    assert framings == [False, False]


def test_the_setting_is_one_of_the_modes_and_a_hand_written_switch_is_understood():
    from croom.devices.autoframe import mode_of
    assert [mode_of(v) for v in ("off", "call_start", "dynamic", True, False, None, "sometimes")] == \
        ["off", "call_start", "dynamic", "dynamic", "off", "off", "off"]
