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
