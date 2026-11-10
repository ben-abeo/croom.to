"""
Frame people (spec 2026-11-01 camera view, section 5), on its risky paths only: no faces puts the camera back, a group at the
picture's edge gives a zoom within 100 to 500 and a turn toward it, one correction at most, only the zoom without Set up, and a
tap while the camera moves or frames is refused. The camera is RoomCamera on the V4L2 stand-in; the picture and the detector are
stand-ins too, except in the one test that runs the committed YuNet model.
"""

import asyncio

import pytest

from croom.devices import framing
from croom.devices.errors import NotReady
from croom.devices.v4l2 import V4L2_CID_PAN_SPEED, V4L2_CID_TILT_SPEED, V4L2_CID_ZOOM_ABSOLUTE
from tests.unit.devices.test_camera import camera_for

SIZE = (1280, 720)
RIGHT_EDGE = [(1180, 300, 60, 60)]       # one face at the picture's right edge (x, y, width, height in pixels)
RIGHT_OF_MIDDLE = [(760, 300, 60, 60)]   # one face a little right of the middle: in reach of the pan


class Stills:
    """CameraView.still as Frame people sees it: one JPEG per call, counted; `gate` holds the first one back until set."""

    def __init__(self, gate=None):
        self.taken = 0
        self.gate = gate

    async def __call__(self):
        self.taken += 1
        if self.gate is not None:
            await self.gate.wait()
        return b"jpeg"


def finds(faces):
    return lambda jpeg: (list(faces), SIZE)


def turns(v4l2):
    """The speed writes that start or keep a motor going: one per leg of a timed turn."""
    return [values for values in v4l2.set_many_calls if any(values.values())]


async def in_the_middle(camera, clock):
    """Set up (the stops found), then the camera half way along both axes."""
    await camera.find_stops()
    await camera.move(1, 1)
    await clock.sleep(4.0)
    await camera.stop()
    assert (camera._pan_s, camera._tilt_s) == (4.0, 4.0)


# --- the geometry ---

def test_a_group_at_the_pictures_edge_gives_a_zoom_in_range_and_a_turn_toward_it():
    right = framing.plan(RIGHT_EDGE, SIZE, travel_s=8.0, position=(4.0, 4.0))
    assert 100 <= right.zoom <= 500 and right.zoom > 100
    assert right.pan_s > 4.0                                   # pan +1 turns right, toward the face
    left = framing.plan([(40, 300, 60, 60)], SIZE, travel_s=8.0, position=(4.0, 4.0))
    assert 100 <= left.zoom <= 500 and left.pan_s < 4.0
    top = framing.plan([(610, 0, 60, 60)], SIZE, travel_s=8.0, position=(4.0, 4.0))
    assert top.tilt_s > 4.0                                    # tilt +1 turns up, toward a face at the top
    # Against the right stop the camera cannot turn there: the zoom keeps the face in the picture rather than fill it
    stuck = framing.plan(RIGHT_EDGE, SIZE, travel_s=8.0, position=(8.0, 4.0))
    assert stuck.pan_s == 8.0 and 100 <= stuck.zoom < right.zoom
    # A crowd wider than the picture: the widest zoom, never below it
    crowd = [(x, 300, 60, 60) for x in range(0, 1280, 100)]
    assert framing.plan(crowd, SIZE, travel_s=8.0, position=(4.0, 4.0)).zoom == 100


def test_the_committed_yunet_model_runs_and_finds_no_one_in_an_empty_picture():
    cv2 = pytest.importorskip("cv2")
    numpy = pytest.importorskip("numpy")
    ok, jpeg = cv2.imencode(".jpg", numpy.full((720, 1280, 3), 128, numpy.uint8))
    assert ok
    assert framing.detect_faces(jpeg.tobytes()) == ([], SIZE)


# --- the camera ---

async def test_no_one_found_puts_the_camera_back_where_it_was():
    camera, v4l2, clock = camera_for()
    await in_the_middle(camera, clock)
    await camera.zoom(250)
    speeds = len(v4l2.set_many_calls)
    answer = await camera.frame_people(Stills(), detect=finds([]))
    assert answer["framed"] == 0
    assert v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE)[-2:] == [100, 250]   # the widest to look, then back to its own zoom
    assert camera.state()["zoom"]["level"] == 250
    assert len(v4l2.set_many_calls) == speeds                     # pan and tilt never touched
    assert camera.position_known and (camera._pan_s, camera._tilt_s) == (4.0, 4.0)


async def test_a_second_correction_is_never_made():
    camera, v4l2, clock = camera_for()
    await in_the_middle(camera, clock)
    before = len(turns(v4l2))
    stills = Stills()
    # The face stays off the middle whatever the camera does (the turn fell short): it corrects once, and then stops trying
    answer = await camera.frame_people(stills, detect=finds(RIGHT_OF_MIDDLE))
    assert answer["framed"] == 1 and answer["turned"] is True
    assert stills.taken == 2                                       # the first look and the one check
    started = [values for values in turns(v4l2)[before:] if values.get(V4L2_CID_PAN_SPEED) == 1]
    assert len(started) == 2                                       # the turn toward the face and one correction
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 0
    assert camera.position_known and camera._pan_s > 4.0
    assert not camera.busy and not camera.state()["framing"]


async def test_without_set_up_only_the_zoom_is_set():
    camera, v4l2, _ = camera_for()
    await camera.discover()
    assert not camera.position_known
    stills = Stills()
    answer = await camera.frame_people(stills, detect=finds(RIGHT_OF_MIDDLE))
    assert answer["framed"] == 1 and answer["turned"] is False
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [] and v4l2.sets(V4L2_CID_TILT_SPEED) == []
    zooms = v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE)
    assert zooms[0] == 100 and 100 < zooms[-1] <= 500 and len(zooms) == 2
    assert stills.taken == 1                                       # nothing turned: nothing to check


async def test_a_second_tap_while_framing_is_refused_and_nothing_moves_twice():
    camera, v4l2, clock = camera_for()
    await in_the_middle(camera, clock)
    gate = asyncio.Event()
    first = asyncio.create_task(camera.frame_people(Stills(gate), detect=finds(RIGHT_EDGE)))
    await asyncio.sleep(0.01)
    assert camera.busy and camera.state()["framing"]
    zooms = len(v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE))
    second = Stills()
    with pytest.raises(NotReady, match="^Already framing$"):
        await camera.frame_people(second, detect=finds(RIGHT_EDGE))
    assert second.taken == 0 and len(v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE)) == zooms   # the second touched nothing
    gate.set()
    assert (await first)["framed"] == 1                            # and did not cut the first short


async def test_a_tap_while_the_camera_moves_is_refused():
    camera, v4l2, _ = camera_for()

    async def slow_sleep(seconds):          # a homing that takes real time
        await asyncio.sleep(0.02 * seconds)

    camera._sleep = slow_sleep
    homing = asyncio.create_task(camera.find_stops())
    await asyncio.sleep(0.03)
    stills = Stills()
    with pytest.raises(NotReady, match="^Moving…$"):
        await camera.frame_people(stills, detect=finds(RIGHT_EDGE))
    await homing                                                   # not interrupted: it ends with the position known
    assert camera.position_known and stills.taken == 0
    await camera.move(1, 0)                                        # a finger dragging the picture, or holding an arrow
    with pytest.raises(NotReady, match="^Moving…$"):
        await camera.frame_people(stills, detect=finds(RIGHT_EDGE))
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 1 and stills.taken == 0
    await camera.stop()


async def test_frame_people_turns_a_camera_that_says_where_it_is_straight_to_both_people_and_not_after_one_of_them():
    # The Boardroom, 2026-11-04: two people either side of the table were framed together, then the camera went after one of them
    from tests.unit.devices.fake_v4l2 import RALLY
    from croom.devices.v4l2 import V4L2_CID_PAN_ABSOLUTE, V4L2_CID_TILT_ABSOLUTE
    camera, v4l2, _ = camera_for(nodes={"/dev/video0": RALLY})
    await camera.discover()
    looks = iter([EITHER_SIDE, [(600, 320, 120, 120)]])       # the second look finds only one of them
    answer = await camera.frame_people(Stills(), detect=lambda jpeg: (list(next(looks)), SIZE))
    assert answer["framed"] == 2 and answer["turned"] is True
    zoom, pan, tilt = framing.aim(EITHER_SIDE, SIZE, (0.0, 0.0), {}, 1500)
    assert v4l2.set_many_calls == [{V4L2_CID_PAN_ABSOLUTE: round(pan) * 3600, V4L2_CID_TILT_ABSOLUTE: round(tilt) * 3600}]  # no nudge
    assert v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE) == [100, zoom]
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [] and v4l2.sets(V4L2_CID_TILT_SPEED) == []


async def test_frame_people_nudges_a_camera_that_says_where_it_is_once_toward_everyone_and_keeps_to_its_limits():
    from tests.unit.devices.fake_v4l2 import RALLY
    from croom.devices.v4l2 import V4L2_CID_PAN_ABSOLUTE, V4L2_CID_TILT_ABSOLUTE
    camera, v4l2, _ = camera_for(nodes={"/dev/video0": RALLY})
    await camera.discover()
    v4l2.values[(10, V4L2_CID_TILT_ABSOLUTE)] = -20 * 3600
    await camera.set_limit("down", True)                      # nothing lower than this view's bottom edge
    low = [(500, 600, 50, 50), (700, 610, 50, 50)]            # two people low in the picture
    right = [(900, 300, 80, 80), (1100, 300, 80, 80)]         # the second look: still both, right of the middle
    looks = iter([low, right])
    await camera.frame_people(Stills(), detect=lambda jpeg: (list(next(looks)), SIZE))
    assert len(v4l2.set_many_calls) == 2                      # the aim and one nudge
    assert v4l2.sets(V4L2_CID_PAN_ABSOLUTE)[-1] > v4l2.sets(V4L2_CID_PAN_ABSOLUTE)[0]   # toward the right
    zoom = v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE)[-1]
    for tilt in v4l2.sets(V4L2_CID_TILT_ABSOLUTE):            # the picture's bottom edge never below the limit
        assert tilt / 3600 - framing.half_view(zoom)[1] >= -20 - framing.half_view(100)[1] - 0.01


# --- the geometry of a camera that says where it points ---

EITHER_SIDE = [(150, 330, 60, 60), (1050, 300, 60, 60)]


def everyone_in(faces, at, zoom, pan, tilt):
    """Each face's head and shoulders inside the picture at `zoom` with the camera at pan and tilt, from a picture taken at zoom 100
    with the camera at `at`."""
    half_w, half_h = framing.half_view(zoom)
    for face in faces:
        left, top, right, bottom = framing.head_and_shoulders(face, SIZE)
        for place in ((left, top), (right, bottom)):
            by_pan, by_tilt = framing._angles(place, 100, framing.RALLY_TANGENTS)
            if abs(at[0] + by_pan - pan) > half_w + 1e-6 or abs(at[1] + by_tilt - tilt) > half_h + 1e-6:
                return False
    return True


def test_aim_puts_the_group_in_the_middle_and_everyone_in_the_picture():
    zoom, pan, tilt = framing.aim(EITHER_SIDE, SIZE, (10.0, -15.0), {}, 1500)
    assert 100 <= zoom <= 1500 and everyone_in(EITHER_SIDE, (10.0, -15.0), zoom, pan, tilt)
    far = [(700, 300, 30, 30)]                               # one person at the far end of the room
    zoom_far, _, _ = framing.aim(far, SIZE, (0.0, 0.0), {}, 1500)
    assert zoom_far > 500                                    # past the MeetUp's 500, as the Rally can


def test_aim_held_off_by_a_limit_zooms_out_to_keep_everyone_in():
    on_the_left = [(60, 300, 60, 60), (300, 300, 60, 60)]
    free = framing.aim(on_the_left, SIZE, (0.0, 0.0), {}, 1500)
    held = framing.aim(on_the_left, SIZE, (0.0, 0.0), {"left": -45.0}, 1500)
    zoom, pan, tilt = held
    assert zoom < free[0] and pan - framing.half_view(zoom)[0] >= -45.0 - 1e-6
    assert everyone_in(on_the_left, (0.0, 0.0), zoom, pan, tilt)


def test_the_lowest_limit_keeps_the_bottom_of_the_picture_where_it_was_set_at_any_zoom():
    limits = {"down": -56.1}
    assert framing.within(0.0, -60.0, 100, limits)[1] == pytest.approx(-30.0)
    assert framing.within(0.0, -60.0, 300, limits)[1] < -30.0          # a narrower picture may point lower
    assert framing.within(10.0, -5.0, 100, limits) == (10.0, -5.0)      # inside: as asked
    squeezed = framing.within(0.0, 0.0, 100, {"down": -10.0, "up": 10.0})   # too close for the picture: the lowest wins
    assert squeezed[1] == pytest.approx(-10.0 + framing.half_view(100)[1])


def test_the_check_after_the_turn_never_turns_toward_fewer_people_than_were_found():
    one = [(1000, 300, 60, 60)]
    assert framing.nudge(one, SIZE, 300, people=2) is None and framing.correction(one, SIZE, 300, 8.0, people=2) is None
    assert framing.nudge(one, SIZE, 300, people=1) is not None and framing.correction(one, SIZE, 300, 8.0, people=1) is not None


def test_how_far_the_rally_turns_by_speed_follows_what_was_measured_in_the_boardroom():
    # Holds against where it came to rest at zoom 100 (2026-11-05), and a second's hold at zoom 500 (10 deg)
    measured = {0.15: 1, 0.3: 2, 0.45: 3, 0.6: 5, 0.8: 9, 1.0: 14, 1.4: 23}
    assert {s: framing.turn_degrees(s, 100) for s in measured} == measured
    assert framing.turn_degrees(0, 100) == 0 and framing.turn_degrees(-1, 100) == 0
    assert framing.turn_degrees(2.3, 100) == 45 and framing.turn_degrees(1.0, 500) == 10
    assert framing.turn_degrees(1.0, 1500) == framing.turn_degrees(1.0, 1000)       # no slower past the table


# --- the Rally framed its own base (Ben, 2026-11-09): it looks once the camera has stopped, zooms no further than 5 times, and backs out
#     of a framing that has no one in it ---

class TimedStills(Stills):
    """Stills that note, as each is taken, the time and when the camera's last target is due to have got there."""

    def __init__(self, camera, clock):
        super().__init__()
        self.camera, self.clock, self.at = camera, clock, []

    async def __call__(self):
        self.at.append((self.clock(), self.camera._target_done_at))
        return await super().__call__()


async def test_frame_people_looks_only_once_a_rally_sent_back_inside_its_limits_has_got_there():
    """A Rally framed low and tight is sent up as it zooms out, so the wider picture stays inside its limits; it waits about 0.85 s for a
    target, then turns. The faces were looked for a fixed second after the zoom, before it had moved, then aimed at from where it was going."""
    from tests.unit.devices.fake_v4l2 import RALLY
    from croom.devices.v4l2 import V4L2_CID_TILT_ABSOLUTE
    camera, v4l2, clock = camera_for(nodes={"/dev/video0": RALLY})
    await camera.discover()
    v4l2.values[(10, V4L2_CID_TILT_ABSOLUTE)] = -20 * 3600
    await camera.set_limit("down", True)                      # the bottom edge of this view, at zoom 100
    v4l2.values[(10, V4L2_CID_TILT_ABSOLUTE)] = -40 * 3600    # a tight framing low down: inside the limit at zoom 500
    await camera.zoom(500)
    stills = TimedStills(camera, clock)
    await camera.frame_people(stills, detect=finds([]))
    assert v4l2.sets(V4L2_CID_TILT_ABSOLUTE)[0] > -40 * 3600   # sent up to keep the widest picture inside the limit
    taken, due = stills.at[0]
    assert taken >= due + camera.SETTLE_S


async def test_frame_people_zooms_a_rally_no_further_than_five_times():
    """The Rally zooms to 15 times: a picture a few degrees across, which a turn off by a degree or two (its backlash alone is 1.3 deg)
    leaves on the table under the person. Frame people stops at 5 times, as on the MeetUp."""
    from tests.unit.devices.fake_v4l2 import RALLY
    camera, v4l2, _ = camera_for(nodes={"/dev/video0": RALLY})
    await camera.discover()
    one_far_away = [(630, 340, 20, 20)]
    assert framing.aim(one_far_away, SIZE, (0.0, 0.0), {}, 1500)[0] > 500      # what it would have zoomed to
    await camera.frame_people(Stills(), detect=finds(one_far_away))
    assert max(v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE)) == 500


async def test_frame_people_puts_a_rally_back_where_it_was_when_the_framed_picture_has_no_one_in_it(caplog):
    """Framed, the picture is looked at once more: with no one in it, the camera goes back to where the faces were found, at the widest,
    and says it lost them ("framed" stays the people found: Auto framing, counting them, does not frame again and again); a warning says
    what was found and where it aimed, for the next time it happens."""
    import logging
    from tests.unit.devices.fake_v4l2 import RALLY
    from croom.devices.v4l2 import V4L2_CID_PAN_ABSOLUTE, V4L2_CID_TILT_ABSOLUTE
    camera, v4l2, _ = camera_for(nodes={"/dev/video0": RALLY})
    await camera.discover()
    v4l2.values[(10, V4L2_CID_PAN_ABSOLUTE)] = 10 * 3600
    v4l2.values[(10, V4L2_CID_TILT_ABSOLUTE)] = -25 * 3600
    looks = iter([EITHER_SIDE, []])
    with caplog.at_level(logging.WARNING, logger="croom.devices.camera"):
        answer = await camera.frame_people(Stills(), detect=lambda jpeg: (list(next(looks)), SIZE))
    assert answer["framed"] == 2 and answer["turned"] is False and answer["lost"] is True   # found, as Auto framing counts them
    assert v4l2.set_many_calls[-1] == {V4L2_CID_PAN_ABSOLUTE: 10 * 3600, V4L2_CID_TILT_ABSOLUTE: -25 * 3600}
    assert v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE)[-1] == 100
    said = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(said) == 1 and "no one in the framed picture" in said[0] and "2 found" in said[0]


async def test_frame_people_puts_a_meetup_back_where_it_was_when_the_framed_picture_has_no_one_in_it():
    camera, v4l2, clock = camera_for()
    await in_the_middle(camera, clock)
    looks = iter([RIGHT_OF_MIDDLE, []])
    answer = await camera.frame_people(Stills(), detect=lambda jpeg: (list(next(looks)), SIZE))
    assert answer["framed"] == 1 and answer["turned"] is False and answer["lost"] is True
    assert (camera._pan_s, camera._tilt_s) == (4.0, 4.0)
    assert v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE)[-1] == 100
