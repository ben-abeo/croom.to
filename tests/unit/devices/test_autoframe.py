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


async def framer_for(faces=TWO, mode="dynamic", follow=False):
    camera, v4l2, clock = camera_for(nodes={"/dev/video0": RALLY})
    await camera.discover()
    room = Room(faces)
    framer = AutoFramer(camera, room.meeting, mode=mode, detect=room.detect, clock=clock, sleep=clock.sleep, follow=follow)
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


# --- following (a room with the AI HAT): the same people, moved since the last framing

def moved(faces, by):
    return [(x + by, y, w, h) for x, y, w, h in faces]


async def test_with_the_hat_the_same_people_moved_across_the_picture_are_framed_again():
    framer, camera, room, clock, framings = await framer_for(follow=True)
    await framed_at_the_start(framer, clock)
    clock.now += framer.REST_S
    await looks(framer, clock, 4)                              # where the framing left them: nothing to follow
    assert framings == [False]
    room.faces = moved(TWO, 300)                               # both, a quarter of the picture to the right
    await looks(framer, clock, framer.MOVED_LOOKS - 1)
    assert framings == [False]                                 # not for a look or two: someone leaning over
    await looks(framer, clock, 1)
    assert framings == [False, False]
    await looks(framer, clock, 12)                             # and where that framing left them is the new place: no hunting
    assert framings == [False, False]


async def test_people_who_move_and_come_back_or_draw_apart_a_little_are_left_alone():
    framer, camera, room, clock, framings = await framer_for(follow=True)
    await framed_at_the_start(framer, clock)
    clock.now += framer.REST_S
    await looks(framer, clock, 1)
    room.faces = moved(TWO, 300)
    await looks(framer, clock, framer.MOVED_LOOKS - 1)
    room.faces = moved(TWO, 40)                                # back, near enough
    await looks(framer, clock, 6)
    assert framings == [False]


async def test_people_drawn_together_are_framed_closer():
    framer, camera, room, clock, framings = await framer_for(follow=True)
    await framed_at_the_start(framer, clock)
    clock.now += framer.REST_S
    await looks(framer, clock, 1)
    room.faces = [(570, 300, 60, 60), (650, 300, 60, 60)]      # the same middle, a fifth as far apart
    await looks(framer, clock, framer.MOVED_LOOKS)
    assert framings == [False, False]


async def test_without_the_hat_people_who_move_are_not_followed():
    framer, camera, room, clock, framings = await framer_for()
    await framed_at_the_start(framer, clock)
    clock.now += framer.REST_S
    await looks(framer, clock, 1)
    room.faces = moved(TWO, 300)
    await looks(framer, clock, 8)
    assert framings == [False]


# --- tracking ("track", a room with the AI HAT and a Rally): the camera steered by speed to keep the people in the middle

class LiveView(View):
    def watch(self):
        pass

    def unwatch(self):
        pass


async def tracking(faces=TWO):
    framer, camera, room, clock, framings = await framer_for(faces, mode="track", follow=True)
    room.meeting.camera_view = LiveView()
    await framer.tick()                                        # the call is seen
    clock.now += framer.SOON_S                                 # framed soon after it connects: it frames again as people settle
    await framer.tick()
    assert framings == [False]
    return framer, camera, room, clock, framings


def speeds(camera):
    return camera._pan, camera._tilt


async def see(framer, room, clock, faces, seconds=0.1):
    room.faces = faces
    clock.now += seconds
    await framer.steer(list(faces), SIZE, room.meeting.camera_view)


async def test_tracking_turns_toward_people_who_move_and_stops_when_they_are_in_the_middle_again():
    framer, camera, room, clock, framings = await tracking()
    await see(framer, room, clock, moved(TWO, 40))             # a step aside, within the middle: left alone
    assert speeds(camera) == (0, 0)
    await see(framer, room, clock, moved(TWO, 300))            # a quarter of the picture to the right: after them at once
    assert speeds(camera) == (1, 0) and camera.taken_over_at is None      # the room's own turn, not a person taking the camera
    await see(framer, room, clock, moved(TWO, 250))
    assert speeds(camera) == (1, 0)                            # still on its way
    for _ in range(4):                                         # in the middle again, for a few pictures (their place is smoothed)
        await see(framer, room, clock, moved(TWO, 10))
    assert speeds(camera) == (0, 0) and framings == [False]    # no framing from the widest picture: it only turned
    for _ in range(3):
        await see(framer, room, clock, moved(TWO, -300))
    assert speeds(camera) == (-1, 0)
    await camera.stop()


async def test_a_fast_turn_stops_early_for_what_is_still_to_come():
    framer, camera, room, clock, framings = await tracking()
    await see(framer, room, clock, moved(TWO, 500))
    assert speeds(camera) == (1, 0)
    for _ in range(6):
        await see(framer, room, clock, moved(TWO, 60), seconds=0.2)   # at full speed by now, and they are a few degrees off still
    assert speeds(camera) == (0, 0)                                # stopped: the camera comes to rest on them
    slow, camera2, room2, clock2, _ = await tracking()
    await see(slow, room2, clock2, moved(TWO, 150))
    slow._middle = None
    await see(slow, room2, clock2, moved(TWO, 60), seconds=0.1)    # the same place early in a turn, still slow: on it goes
    assert speeds(camera2) == (1, 0)
    await camera2.stop()


async def test_people_who_get_out_of_the_picture_are_found_by_zooming_out_where_the_camera_is_and_never_by_turning():
    framer, camera, room, clock, framings = await tracking()
    await camera.zoom(280, by_person=False)
    await see(framer, room, clock, moved(TWO, 150))            # a little to the right: after them
    assert speeds(camera) == (1, 0)
    await see(framer, room, clock, [], seconds=0.05)           # gone (the picture mid-turn is blurred): on to where they were ...
    assert speeds(camera) == (1, 0)
    await see(framer, room, clock, [], seconds=2.0)            # ... and no further
    assert speeds(camera) == (0, 0) and framings == [False]
    zooms = []
    for _ in range(12):
        await see(framer, room, clock, [], seconds=framer.TRACK_ZOOM_S)
        zooms.append(camera.state()["zoom"]["level"])
    assert zooms[0] < 280 and zooms == sorted(zooms, reverse=True) and zooms[-1] == camera.state()["zoom"]["min"]
    assert speeds(camera) == (0, 0) and framings == [False]    # found by the zoom: no turn, no framing from the widest
    await see(framer, room, clock, moved(TWO, 30))             # in the picture again: tracked as before
    await see(framer, room, clock, moved(TWO, 400))
    await see(framer, room, clock, moved(TWO, 400))
    assert speeds(camera) == (1, 0)
    await camera.stop()


async def test_a_turn_up_or_down_stops_the_moment_they_are_out_of_the_picture():
    framer, camera, room, clock, framings = await tracking()
    await see(framer, room, clock, [(x, y - 250, w, h) for x, y, w, h in TWO])     # they stood up
    await see(framer, room, clock, [(x, y - 250, w, h) for x, y, w, h in TWO])
    assert speeds(camera) == (0, 1)
    await see(framer, room, clock, [], seconds=0.05)
    assert speeds(camera) == (0, 0)                            # never up or down unseen: no ceiling


async def at_home(framer, camera):
    await camera.save_home()
    return camera.home_place


async def test_only_people_in_the_room_homes_picture_are_followed_and_the_camera_keeps_near_home():
    framer, camera, room, clock, framings = await tracking()
    home = await at_home(framer, camera)
    assert home is not None
    await camera.zoom(100, by_person=False)
    far = [(1270, 300, 8, 8)]                                  # at the very edge of Home's widest picture: in the room, just
    await see(framer, room, clock, TWO + far)
    assert len(framer._in_the_room(TWO + far, SIZE, 100, home, home)) == 3
    away = (home[0] + 60, home[1])                             # the camera 60 degrees off Home: what it sees there is not the room
    assert framer._in_the_room(TWO, SIZE, 100, away, home) == []
    assert framer._reach(0, 1, (home[0] + framer.TRACK_REACH[0], home[1]), home) == 0      # no further from Home than its reach
    assert framer._reach(0, -1, (home[0] + framer.TRACK_REACH[0], home[1]), home) == -1    # but back toward it
    assert framer._reach(1, 1, (home[0], home[1] + framer.TRACK_REACH[1]), home) == 0      # nor up to the ceiling


async def test_no_one_at_the_widest_for_a_while_sends_the_camera_home_once():
    framer, camera, room, clock, framings = await tracking()
    await at_home(framer, camera)
    homes = []
    home = camera.home

    async def counted(**kwargs):
        homes.append(kwargs)
        return await home(**kwargs)

    camera.home = counted
    await camera.zoom(100, by_person=False)
    await see(framer, room, clock, [], seconds=framer.TRACK_LOST_S)
    assert homes == []
    await see(framer, room, clock, [], seconds=framer.TRACK_HOME_S)
    await see(framer, room, clock, [], seconds=1.0)
    assert homes == [{"by_person": False}] and framings == [False]


async def test_with_room_to_spare_the_zoom_steps_in_and_out_again_when_they_reach_the_edge():
    framer, camera, room, clock, framings = await tracking()
    await camera.zoom(150, by_person=False)
    close = [(600, 330, 30, 30), (650, 330, 30, 30)]           # small in the middle of the picture
    await see(framer, room, clock, close, seconds=1.0)
    assert camera.state()["zoom"]["level"] == 150              # not at once
    await see(framer, room, clock, close, seconds=framer.TRACK_ROOMY_S)
    assert camera.state()["zoom"]["level"] > 150 and camera.taken_over_at is None
    level = camera.state()["zoom"]["level"]
    wide = [(40, 300, 200, 200), (1040, 300, 200, 200)]        # filling it, at its edges
    await see(framer, room, clock, wide, seconds=1.0)
    assert camera.state()["zoom"]["level"] < level


async def test_tracking_never_turns_toward_fewer_people_than_it_framed():
    framer, camera, room, clock, framings = await tracking()
    await see(framer, room, clock, [(1100, 300, 60, 60)])      # one of the two, far to the right: the other is not left out
    assert speeds(camera) == (0, 0)


async def test_track_without_the_hat_is_dynamic():
    framer, camera, room, clock, framings = await framer_for(mode="track", follow=False)
    await framed_at_the_start(framer, clock)
    assert not framer._tracks()
    room.faces = moved(TWO, 300)
    await looks(framer, clock, 6)
    assert speeds(camera) == (0, 0) and framings == [False]


async def test_at_the_start_of_calls_still_waits_for_people_to_sit_down_and_the_others_frame_soon():
    framer, camera, room, clock, framings = await framer_for(mode="call_start")
    await framer.tick()
    clock.now += framer.SOON_S
    await framer.tick()
    assert framings == []
    soon, camera, room, clock, framings = await framer_for(mode="dynamic")
    await soon.tick()
    clock.now += soon.SOON_S
    await soon.tick()
    assert framings == [False]
