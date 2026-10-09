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


# --- control calls are serialised ---

async def test_control_writes_never_interleave():
    camera, v4l2, _ = camera_for()
    for _ in range(20):
        await asyncio.gather(camera.move(1, 0), camera.stop())   # a repeat request and a release, at the same moment
    assert v4l2.overlapped is False                              # they queue: no two control calls were in flight at once
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 0
    assert camera.state()["moving"] is False
    await camera.stop()                                          # the last move's watchdog goes with it


async def test_zoom_reads_and_writes_never_interleave():
    camera, v4l2, clock = camera_for()
    await camera.discover()
    for _ in range(10):
        clock.now += 3.1                                         # the cached level has expired: the read goes to the camera
        await asyncio.gather(camera.read_zoom(), camera.zoom(250))
    assert v4l2.overlapped is False
    assert camera.state()["zoom"]["level"] == 250 and v4l2.values[(10, V4L2_CID_ZOOM_ABSOLUTE)] == 250


async def test_a_call_that_waited_for_the_lock_sees_that_the_camera_went_away():
    camera, v4l2, _ = camera_for()
    await camera.discover()
    async with camera._io_lock:                                  # a control call is in flight ...
        waiting = [asyncio.create_task(camera.zoom(200)), asyncio.create_task(camera.move(1, 0)),
                   asyncio.create_task(camera.read_zoom())]
        await asyncio.sleep(0.01)                                # ... three more requests queue behind it ...
        camera._fail("camera call failed: gone")                 # ... and that call fails, which closes the node
    for task in waiting:
        with pytest.raises(DeviceUnavailable) as failure:        # a clean refusal, not a write to a closed node
            await task
        assert "gone" in str(failure.value)
    assert v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE) == [] and v4l2.sets(V4L2_CID_PAN_SPEED) == []


# --- a corrupt saved camera value is ignored, not fatal ---

@pytest.mark.parametrize("saved", ["oops", ["home"], {"presets": ["a", "b"]}, {"home": [1, 2], "presets": {"1": "x", "2": 5}}],
                         ids=["camera is a string", "camera is a list", "presets is a list", "home and presets entries are not dicts"])
def test_corrupt_saved_settings_are_ignored(tmp_path, saved):
    store = SettingsStore(tmp_path / "control-settings.json")
    store.save("camera", saved)
    camera = RoomCamera(store=store, v4l2=FakeV4l2())
    assert camera.home_saved is False
    assert [preset["saved"] for preset in camera.state()["presets"]] == [False, False, False]


def test_valid_saved_settings_beside_corrupt_ones_are_kept(tmp_path):
    store = SettingsStore(tmp_path / "control-settings.json")
    store.save("camera", {"home": {"pan_s": 1.0, "tilt_s": 2.0},
                          "presets": {"3": {"pan_s": 0.5, "tilt_s": 0.5, "zoom": 300}, "2": "x"}})
    camera = RoomCamera(store=store, v4l2=FakeV4l2())
    assert camera.home_saved is True
    assert [preset["saved"] for preset in camera.state()["presets"]] == [False, False, True]


# --- behaviours the first tests left unpinned ---

async def test_a_node_that_cannot_be_opened_is_skipped_and_the_next_one_tried():
    camera, v4l2, _ = camera_for(nodes={"/dev/video0": MEETUP, "/dev/video2": MEETUP})
    v4l2.unopenable = {"/dev/video0"}                            # busy, or not readable by the service user
    assert await camera.discover() is True and camera.state()["device"] == "/dev/video2"
    assert v4l2.calls[:2] == [("open", "/dev/video0"), ("open", "/dev/video2")]
    only, v4l2, _ = camera_for(nodes={"/dev/video0": MEETUP}, device="/dev/video0")
    v4l2.unopenable = {"/dev/video0"}
    assert await only.discover() is False and only.state()["reason"] == "no controllable camera found"


async def test_a_camera_with_only_zoom_or_only_pan_and_tilt_is_still_found():
    zoom_only, _, _ = camera_for(nodes={"/dev/video0": {V4L2_CID_ZOOM_ABSOLUTE: (100, 500, 1, 100)}})
    assert await zoom_only.discover() is True
    moves_only, _, _ = camera_for(nodes={"/dev/video0": {V4L2_CID_PAN_SPEED: (-1, 1, 1, 1), V4L2_CID_TILT_SPEED: (-1, 1, 1, 1)}})
    assert await moves_only.discover() is True


async def test_controls_the_node_does_not_have_are_never_written():
    zoom_only, v4l2, _ = camera_for(nodes={"/dev/video0": {V4L2_CID_ZOOM_ABSOLUTE: (100, 500, 1, 100)}})
    await zoom_only.move(1, 1)
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [] and v4l2.sets(V4L2_CID_TILT_SPEED) == []
    assert zoom_only.state()["moving"] is False                  # nothing can move, so nothing is counted as moving
    no_tilt, v4l2, _ = camera_for(nodes={"/dev/video0": {V4L2_CID_PAN_SPEED: (-1, 1, 1, 1)}})
    await no_tilt.move(1, 1)
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [1] and v4l2.sets(V4L2_CID_TILT_SPEED) == []
    assert no_tilt.state()["moving"] is True
    await zoom_only.stop()
    await no_tilt.stop()


async def test_a_failed_call_leaves_nothing_moving_and_the_position_unknown():
    camera, v4l2, _ = camera_for()
    await camera.move(1, 0)
    camera._position_known = True                                # as after homing (Task 5)
    v4l2.fail = True
    with pytest.raises(DeviceUnavailable):
        await camera.read_zoom()
    state = camera.state()
    assert state["available"] is False and state["device"] is None
    assert state["reason"] == "camera call failed: [Errno 19] No such device"
    assert state["moving"] is False                              # the speeds are forgotten with the node
    assert camera.position_known is False                        # it may have been unplugged, and moved, meanwhile
    assert ("close", 10) in v4l2.calls
    await camera.stop()


async def test_a_missing_camera_is_warned_about_once(caplog):
    camera, _, _ = camera_for(nodes={"/dev/video19": {}})
    with caplog.at_level(logging.WARNING):
        for _ in range(3):                                       # the periodic re-check, again and again
            assert await camera.discover() is False
    assert len([r for r in caplog.records if "Camera unavailable" in r.getMessage()]) == 1


async def test_the_same_failure_is_warned_about_again_after_a_recovery(caplog):
    camera, v4l2, _ = camera_for()
    await camera.discover()
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            v4l2.fail = True
            with pytest.raises(DeviceUnavailable):
                await camera.move(1, 0)
            v4l2.fail = False
            assert await camera.discover() is True               # plugged back in
    assert len([r for r in caplog.records if "Camera unavailable" in r.getMessage()]) == 2


async def test_stop_on_a_missing_camera_changes_nothing_and_does_not_raise():
    camera, v4l2, _ = camera_for(nodes={"/dev/video19": {}})
    assert await camera.discover() is False
    state = await camera.stop()
    assert state["available"] is False and state["moving"] is False
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [] and v4l2.sets(V4L2_CID_TILT_SPEED) == []


async def test_zoom_updates_the_reported_level_and_the_cached_read():
    camera, v4l2, clock = camera_for()
    await camera.zoom(250)
    assert camera.state()["zoom"] == {"level": 250, "min": 100, "max": 500}
    v4l2.values[(10, V4L2_CID_ZOOM_ABSOLUTE)] = 400              # the camera moved on its own
    assert await camera.read_zoom() == 250                       # what was just set is trusted for ZOOM_CACHE_S
    clock.now += 3.1
    assert await camera.read_zoom() == 400


async def test_read_zoom_finds_the_camera_by_itself():
    camera, _, _ = camera_for()
    assert await camera.read_zoom() == 100                       # no discover() first
    assert camera.available is True
    missing, _, _ = camera_for(nodes={"/dev/video19": {}})
    with pytest.raises(DeviceUnavailable):
        await missing.read_zoom()


async def test_close_cancels_the_watchdog():
    camera, _, _ = camera_for()
    await camera.move(1, 0)
    watchdog = camera._watchdog
    await camera.close()
    assert watchdog.cancelled()                                  # not left sleeping for WATCHDOG_S


async def test_an_empty_device_setting_means_auto():
    camera, _, _ = camera_for(device="")
    assert await camera.discover() is True and camera.state()["device"] == "/dev/video0"
