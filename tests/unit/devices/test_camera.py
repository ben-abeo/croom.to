"""
The MeetUp's framing through V4L2 (spec 2026-10-08 sound and camera, section 4.3):
discovery, hold-to-move with a watchdog, zoom; then (Task 5) the position estimate,
homing, presets and the preview.
"""

import asyncio
import logging

import pytest

from croom.control.settings import SettingsStore
from croom.devices.errors import DeviceUnavailable
from croom.devices.camera import RoomCamera
from croom.devices.v4l2 import V4L2_CID_PAN_SPEED, V4L2_CID_TILT_SPEED, V4L2_CID_ZOOM_ABSOLUTE
from tests.unit.devices.fake_v4l2 import MEETUP, FakeClock, FakeV4l2


def camera_for(tmp_path=None, nodes=None, device="auto", travel_s=8.0, names=None):
    v4l2 = FakeV4l2(nodes)
    clock = FakeClock()
    store = SettingsStore(tmp_path / "control-settings.json") if tmp_path is not None else None
    camera = RoomCamera(device=device, travel_s=travel_s, preset_names=names, store=store,
                        v4l2=v4l2, clock=clock, sleep=clock.sleep)
    return camera, v4l2, clock


async def test_auto_picks_the_first_node_with_camera_controls(caplog):
    camera, v4l2, _ = camera_for()
    with caplog.at_level(logging.INFO):
        assert await camera.discover() is True
    state = camera.state()
    assert state["available"] is True and state["device"] == "/dev/video0"
    assert state["zoom"] == {"level": 100, "min": 100, "max": 500}
    assert v4l2.calls[0] == ("open", "/dev/video0")
    assert ("open", "/dev/video1") not in v4l2.calls   # found on the first node, the rest is left alone
    assert any("/dev/video0" in r.getMessage() for r in caplog.records)


async def test_a_node_without_controls_is_closed_again_and_the_next_one_tried():
    camera, v4l2, _ = camera_for(nodes={"/dev/video0": {}, "/dev/video2": MEETUP})
    assert await camera.discover() is True
    assert ("close", 10) in v4l2.calls and camera.state()["device"] == "/dev/video2"


async def test_a_configured_node_is_used_as_is():
    camera, v4l2, _ = camera_for(nodes={"/dev/video0": MEETUP, "/dev/video2": MEETUP}, device="/dev/video2")
    await camera.discover()
    assert camera.state()["device"] == "/dev/video2"


async def test_no_controllable_camera_is_unavailable_with_a_reason():
    camera, _, _ = camera_for(nodes={"/dev/video19": {}})
    assert await camera.discover() is False
    assert camera.state()["available"] is False and camera.state()["reason"] == "no controllable camera found"
    with pytest.raises(DeviceUnavailable):
        await camera.move(1, 0)


async def test_move_sets_the_speeds_and_stop_clears_them():
    camera, v4l2, _ = camera_for()
    await camera.move(1, 0)
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [1] and v4l2.sets(V4L2_CID_TILT_SPEED) == [0]
    assert camera.state()["moving"] is True
    await camera.move(0, -1)
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [1, 0] and v4l2.sets(V4L2_CID_TILT_SPEED) == [0, -1]
    await camera.stop()
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 0
    assert camera.state()["moving"] is False


async def test_move_refuses_other_directions():
    camera, _, _ = camera_for()
    with pytest.raises(ValueError):
        await camera.move(2, 0)
    with pytest.raises(ValueError):
        await camera.move(0, "up")


async def test_motion_stops_on_its_own_when_the_page_goes_quiet():
    camera, v4l2, _ = camera_for()
    camera.WATCHDOG_S = 0.05
    await camera.move(1, 1)
    await asyncio.sleep(0.03)
    await camera.move(1, 1)              # the page is still holding the button
    await asyncio.sleep(0.03)
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 1   # not stopped yet: the second request reset the watchdog
    await asyncio.sleep(0.06)
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 0
    assert camera.state()["moving"] is False


async def test_zoom_is_clamped_to_the_cameras_range():
    camera, v4l2, _ = camera_for()
    assert await camera.zoom(250) == 250
    assert await camera.zoom(900) == 500
    assert await camera.zoom(1) == 100
    assert v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE) == [250, 500, 100]
    assert await camera.zoom_step(25) == 125
    assert await camera.zoom_step(-500) == 100


async def test_zoom_is_read_from_the_camera_and_cached():
    camera, v4l2, clock = camera_for()
    await camera.discover()
    v4l2.values[(10, V4L2_CID_ZOOM_ABSOLUTE)] = 180
    assert await camera.read_zoom() == 180
    v4l2.values[(10, V4L2_CID_ZOOM_ABSOLUTE)] = 300
    assert await camera.read_zoom() == 180      # cached
    clock.now += 3.1
    assert await camera.read_zoom() == 300


async def test_a_failing_control_call_makes_the_camera_unavailable():
    camera, v4l2, _ = camera_for()
    await camera.discover()
    v4l2.fail = True
    with pytest.raises(DeviceUnavailable) as failure:
        await camera.move(1, 0)
    assert "No such device" in str(failure.value)
    assert camera.state()["available"] is False and ("close", 10) in v4l2.calls
    v4l2.fail = False
    assert await camera.discover() is True     # plugged back in: picked up again


async def test_close_stops_motion_and_releases_the_node():
    camera, v4l2, _ = camera_for()
    await camera.move(1, 0)
    await camera.close()
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.calls[-1] == ("close", 10)
    assert camera.state()["available"] is False


def test_from_config_reads_the_device_travel_time_and_preset_names(tmp_path):
    from croom.core.config import Config
    config = Config.from_dict({"video": {"device": "/dev/video2", "ptz_travel_seconds": 6.0},
                               "control": {"camera_presets": ["Room", "Desk"]}})
    camera = RoomCamera.from_config(config, SettingsStore(tmp_path / "s.json"))
    assert camera.state()["presets"] == [{"slot": 1, "name": "Room", "saved": False},
                                         {"slot": 2, "name": "Desk", "saved": False},
                                         {"slot": 3, "name": "Preset 3", "saved": False}]
    assert camera._device_pref == "/dev/video2" and camera._travel_s == 6.0
