"""
The MeetUp's framing through V4L2 (spec 2026-10-08 sound and camera, section 4.3):
discovery, hold-to-move with a watchdog, zoom; then (Task 5) the position estimate,
homing, presets and the preview.
"""

import asyncio
import logging
import threading

import pytest

from croom.control.settings import SettingsStore
from croom.devices.errors import DeviceUnavailable, Interrupted, NotReady
from croom.devices.camera import RoomCamera
from croom.devices.v4l2 import V4L2_CID_PAN_SPEED, V4L2_CID_TILT_SPEED, V4L2_CID_ZOOM_ABSOLUTE
from tests.unit.devices.fake_v4l2 import MEETUP, FakeClock, FakeV4l2


def camera_for(tmp_path=None, nodes=None, device="auto", travel_s=8.0, names=None, latency_s=0.0):
    v4l2 = FakeV4l2(nodes, latency_s=latency_s)
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
    camera, v4l2, _ = camera_for(latency_s=0.005)
    for _ in range(20):
        await asyncio.gather(camera.move(1, 0), camera.stop())   # a repeat request and a release, at the same moment
    assert v4l2.overlapped is False                              # they queue: no two control calls were in flight at once
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 0
    assert camera.state()["moving"] is False
    await camera.stop()                                          # the last move's watchdog goes with it


async def test_zoom_reads_and_writes_never_interleave():
    camera, v4l2, clock = camera_for(latency_s=0.005)
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
    assert v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE) == []                # nothing was written for the queued requests ...
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [0]                   # ... only the failure's own stop write went out


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
    assert v4l2.set_many_calls == []                             # no speed control at all: no call
    v4l2.fail_next = 1
    with pytest.raises(DeviceUnavailable):
        await zoom_only.zoom(300)
    assert v4l2.set_many_calls == []                             # and nothing to stop when a call fails
    assert zoom_only.state()["moving"] is False                  # nothing can move, so nothing is counted as moving
    no_tilt, v4l2, _ = camera_for(nodes={"/dev/video0": {V4L2_CID_ZOOM_ABSOLUTE: (100, 500, 1, 100), V4L2_CID_PAN_SPEED: (-1, 1, 1, 1)}})
    await no_tilt.move(1, 1)
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [1] and v4l2.sets(V4L2_CID_TILT_SPEED) == []
    assert no_tilt.state()["moving"] is True
    v4l2.fail_next = 1
    with pytest.raises(DeviceUnavailable):
        await no_tilt.read_zoom()                                # the stop after a failure covers the same controls
    assert v4l2.set_many_calls == [{V4L2_CID_PAN_SPEED: 1}, {V4L2_CID_PAN_SPEED: 0}]
    await zoom_only.stop()
    await no_tilt.stop()


async def test_a_failed_call_leaves_nothing_moving_and_the_position_unknown():
    camera, v4l2, _ = camera_for()
    await camera.move(1, 0)
    camera._position_known = True                                # as after homing (Task 5)
    v4l2.fail_next = 1                                           # one call fails; the camera still answers after it
    with pytest.raises(DeviceUnavailable):
        await camera.read_zoom()
    state = camera.state()
    assert state["available"] is False and state["device"] is None
    assert state["reason"] == "camera call failed: [Errno 5] Input/output error"
    assert state["moving"] is False
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 0   # the motors were told to stop
    assert camera.position_known is False                        # it may have been unplugged, and moved, meanwhile
    assert v4l2.calls[-1] == ("close", 10)                       # and the node went last
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


# --- a failed call stops the motors; a node that was lost is found again to stop them ---

@pytest.mark.parametrize("failing", [lambda camera: camera.read_zoom(), lambda camera: camera.zoom(300), lambda camera: camera.move(-1, -1)],
                         ids=["a zoom read", "a zoom write", "a speed write"])
async def test_a_call_that_fails_mid_motion_stops_the_motors_before_the_node_is_closed(failing):
    camera, v4l2, _ = camera_for()
    await camera.move(1, 1)
    v4l2.fail_next = 1                                           # the next call fails once; the camera still answers after it
    with pytest.raises(DeviceUnavailable):
        await failing(camera)
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 0
    assert v4l2.set_many_calls[-1] == {V4L2_CID_PAN_SPEED: 0, V4L2_CID_TILT_SPEED: 0}   # both speeds in one call ...
    assert v4l2.calls[-1] == ("close", 10)                       # ... and before the node was closed
    await camera.stop()


async def test_a_speed_write_that_fails_from_rest_is_followed_by_a_stop_write():
    camera, v4l2, _ = camera_for()
    await camera.discover()
    v4l2.fail_next = 1                                           # the camera may have taken the request before it reported an error
    with pytest.raises(DeviceUnavailable):
        await camera.move(1, 1)
    assert v4l2.set_many_calls == [{V4L2_CID_PAN_SPEED: 0, V4L2_CID_TILT_SPEED: 0}]


async def test_stop_after_the_node_was_lost_reopens_it_and_writes_zeros():
    camera, v4l2, _ = camera_for()
    await camera.move(1, 1)
    v4l2.fail = True                                             # unplugged: calls fail and the node cannot be opened
    v4l2.unopenable = {"/dev/video0"}
    with pytest.raises(DeviceUnavailable):
        await camera.read_zoom()                                 # the failure closes the node; its stop write fails, silently
    assert camera.available is False
    assert (await camera.stop())["available"] is False           # nothing to find: no error, nothing written
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [1]
    v4l2.fail = False                                            # plugged back in
    v4l2.unopenable = set()
    state = await camera.stop()                                  # stop() looks for the node again before it gives up
    assert state["available"] is True and state["device"] == "/dev/video0"
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 0


# --- input that is not a direction or a zoom level is a ValueError, never a TypeError ---

@pytest.mark.parametrize("bad", [None, [250], {"level": 250}, "abc", float("inf"), float("-inf"), float("nan")],
                         ids=["None", "a list", "a dict", "text", "infinity", "minus infinity", "nan"])
async def test_zoom_refuses_input_that_is_not_a_finite_number(bad):
    camera, v4l2, _ = camera_for()
    with pytest.raises(ValueError):
        await camera.zoom(bad)
    with pytest.raises(ValueError):
        await camera.zoom_step(bad)
    assert v4l2.calls == []                                      # refused before the camera was even opened


@pytest.mark.parametrize("pan, tilt", [(True, False), (False, False), (1, True), (0, False), (True, 0)])
async def test_move_refuses_booleans(pan, tilt):
    camera, v4l2, _ = camera_for()
    with pytest.raises(ValueError):                              # True and False equal 1 and 0 but are not directions
        await camera.move(pan, tilt)
    assert v4l2.calls == []


# --- pan and tilt go out together; the time a write takes counts ---

async def test_move_writes_pan_and_tilt_in_one_control_call():
    camera, v4l2, _ = camera_for()
    await camera.move(1, 1)
    assert v4l2.set_many_calls == [{V4L2_CID_PAN_SPEED: 1, V4L2_CID_TILT_SPEED: 1}]
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [1] and v4l2.sets(V4L2_CID_TILT_SPEED) == [1]   # no separate write of either
    await camera.zoom(250)
    assert len(v4l2.set_many_calls) == 1 and v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE) == [250]     # the zoom is an ordinary write
    await camera.stop()
    assert v4l2.set_many_calls[-1] == {V4L2_CID_PAN_SPEED: 0, V4L2_CID_TILT_SPEED: 0}


async def test_the_time_a_write_takes_is_counted_in_the_estimate():
    camera, v4l2, clock = camera_for()
    write = v4l2.set_many
    round_trips = iter([0.5, 1.5])                               # the start write is quick, the stop write is slow

    def slow_write(fd, values):
        clock.now += next(round_trips)
        write(fd, values)

    v4l2.set_many = slow_write
    await camera.move(1, 0)                                      # lands at 100.5: the pan starts there
    await clock.sleep(2.0)                                       # 102.5
    await camera.stop()                                          # lands at 104.0: the pan ran until then, not until 102.5
    assert (camera._pan_s, camera._tilt_s) == (3.5, 0.0)


def test_a_motion_segment_can_be_closed_at_a_given_time():
    camera, _, clock = camera_for()
    camera._pan, camera._segment_started = 1, clock.now          # panning right since now
    camera._account(until=clock.now + 2.0)                       # the write that stops it landed two seconds on
    assert camera._pan_s == 2.0 and camera._segment_started is None
    camera._pan, camera._segment_started = -1, clock.now
    clock.now += 0.5
    camera._account()                                            # no argument: until now
    assert camera._pan_s == 1.5


# --- position, homing, presets, preview (part two) ---

async def test_find_stops_drives_both_axes_to_the_corner_and_the_position_becomes_known():
    camera, v4l2, clock = camera_for()
    before = clock.now
    await camera.find_stops()
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [-1, 0] and v4l2.sets(V4L2_CID_TILT_SPEED) == [-1, 0]
    assert clock.now - before == 8.0                      # ptz_travel_seconds
    assert camera.position_known and (camera._pan_s, camera._tilt_s) == (0.0, 0.0)


async def test_moves_are_counted_in_seconds_of_travel_from_the_stop():
    camera, v4l2, clock = camera_for()
    await camera.find_stops()
    await camera.move(1, 1)
    await clock.sleep(2.5)
    await camera.move(0, 1)        # pan stops after 2.5 s, tilt keeps going
    await clock.sleep(1.0)
    await camera.stop()
    assert (camera._pan_s, camera._tilt_s) == (2.5, 3.5)
    await camera.move(-1, 0)
    await clock.sleep(10.0)        # longer than the travel: clamped at the stop
    await camera.stop()
    assert camera._pan_s == 0.0


async def test_save_home_needs_a_known_position_and_home_returns_there(tmp_path):
    camera, v4l2, clock = camera_for(tmp_path)
    with pytest.raises(NotReady) as failure:
        await camera.save_home()
    assert str(failure.value) == "home the camera first"
    with pytest.raises(NotReady) as failure:
        await camera.home()
    assert str(failure.value) == "save a home first"
    await camera.find_stops()
    await camera.move(1, 1)
    await clock.sleep(3.0)
    await camera.move(0, 1)
    await clock.sleep(1.0)
    await camera.stop()
    await camera.zoom(200)
    await camera.save_home()
    assert camera.home_saved
    assert SettingsStore(tmp_path / "control-settings.json").get("camera")["home"] == {"pan_s": 3.0, "tilt_s": 4.0}
    v4l2.calls.clear()
    await camera.home()
    # to the stops, then 3 s of pan and 4 s of tilt at once (pan stops first), then zoom back to 100
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [-1, 0, 1, 0, 0]
    assert v4l2.sets(V4L2_CID_TILT_SPEED) == [-1, 0, 1, 1, 0]
    assert (camera._pan_s, camera._tilt_s) == (3.0, 4.0) and camera.position_known
    assert v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE)[-1] == 100


async def test_presets_are_saved_against_the_estimate_and_recalled_by_the_difference(tmp_path):
    camera, v4l2, clock = camera_for(tmp_path, names=["Wide", "Table", "Whiteboard"])
    with pytest.raises(NotReady) as failure:
        await camera.save(2)
    assert str(failure.value) == "home the camera first"
    await camera.find_stops()
    await camera.save_home()
    await camera.move(1, 1)
    await clock.sleep(4.0)
    await camera.stop()
    await camera.zoom(230)
    await camera.save(2)
    assert camera.state()["presets"][1] == {"slot": 2, "name": "Table", "saved": True}
    assert SettingsStore(tmp_path / "control-settings.json").get("camera")["presets"] == {"2": {"pan_s": 4.0, "tilt_s": 4.0, "zoom": 230}}
    await camera.move(-1, 0)        # elsewhere: pan back 1.5 s
    await clock.sleep(1.5)
    await camera.stop()
    await camera.zoom(100)
    v4l2.calls.clear()
    await camera.recall(2)
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [1, 0] and v4l2.sets(V4L2_CID_TILT_SPEED) == [0, 0]
    assert (camera._pan_s, camera._tilt_s) == (4.0, 4.0) and v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE) == [230]
    with pytest.raises(NotReady) as failure:
        await camera.recall(3)
    assert str(failure.value) == "nothing saved in this slot"
    with pytest.raises(ValueError):
        await camera.recall(4)


async def test_recall_from_an_unknown_position_homes_first(tmp_path):
    camera, v4l2, clock = camera_for(tmp_path)
    await camera.find_stops()
    await camera.save_home()
    await camera.move(1, 0)
    await clock.sleep(2.0)
    await camera.stop()
    await camera.save(1)
    camera._position_known = False         # what an interrupted move leaves behind
    v4l2.calls.clear()
    await camera.recall(1)
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[:2] == [-1, 0]   # homed first
    assert camera.position_known and camera._pan_s == 2.0
    other, _, _ = camera_for(tmp_path)
    await other.find_stops()
    other._home = None
    other._position_known = False
    other._presets = {"1": {"pan_s": 2.0, "tilt_s": 0.0, "zoom": 100}}
    with pytest.raises(NotReady) as failure:
        await other.recall(1)
    assert str(failure.value) == "home the camera first"


async def test_a_new_command_interrupts_a_long_move_and_the_position_is_unknown(tmp_path):
    camera, v4l2, _ = camera_for(tmp_path)

    async def slow_sleep(seconds):          # a long move that takes real time, so it can be interrupted
        await asyncio.sleep(0.02 * seconds)

    camera._sleep = slow_sleep
    homing = asyncio.create_task(camera.find_stops())
    await asyncio.sleep(0.03)
    assert camera.busy
    await camera.move(0, 1)                   # a person presses an arrow while the camera is homing
    with pytest.raises(Interrupted):
        await homing
    assert not camera.busy and not camera.position_known
    assert v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 1 and v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0


async def test_preview_lasts_three_minutes_unless_renewed_or_cleared():
    camera, _, clock = camera_for()
    assert camera.preview is False
    assert camera.set_preview(True) is True and camera.preview is True
    clock.now += 170
    assert camera.preview is True
    camera.set_preview(True)
    clock.now += 170
    assert camera.preview is True
    clock.now += 11
    assert camera.preview is False
    camera.set_preview(True)
    assert camera.set_preview(False) is False and camera.preview is False


def test_saved_home_and_presets_are_read_back_from_the_store(tmp_path):
    store = SettingsStore(tmp_path / "control-settings.json")
    store.save("camera", {"home": {"pan_s": 1.0, "tilt_s": 2.0}, "presets": {"3": {"pan_s": 0.5, "tilt_s": 0.5, "zoom": 300}}})
    camera = RoomCamera(store=store, v4l2=FakeV4l2())
    assert camera.home_saved and camera.state()["presets"][2]["saved"] is True


# --- long moves: cut short, failed, asked for twice, or refused ---

def slow_moves(camera, seconds_per_second=0.02):
    """Make a long move take real time, so that a test can cut it short: the fake clock would finish it at once."""

    async def sleep(seconds):
        await asyncio.sleep(seconds_per_second * seconds)

    camera._sleep = sleep


async def test_close_cuts_a_long_move_short_and_leaves_the_motors_stopped():
    camera, v4l2, _ = camera_for()
    slow_moves(camera)
    homing = asyncio.create_task(camera.find_stops())
    await asyncio.sleep(0.03)
    assert camera.busy
    await camera.close()
    with pytest.raises(Interrupted):
        await homing
    assert not camera.busy and not camera.available
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 0
    assert v4l2.calls[-1] == ("close", 10)                       # stopped first, then the node was released


async def test_a_long_move_whose_control_call_fails_leaves_the_position_unknown_and_nothing_running():
    camera, v4l2, _ = camera_for()
    await camera.find_stops()
    assert camera.position_known
    v4l2.fail_next = 1                                           # the first speed write of the next homing fails once
    with pytest.raises(DeviceUnavailable):
        await camera.find_stops()
    assert not camera.busy and not camera.position_known and not camera.available
    assert v4l2.set_many_calls[-1] == {V4L2_CID_PAN_SPEED: 0, V4L2_CID_TILT_SPEED: 0}   # the failure's own stop write ...
    assert v4l2.calls[-1] == ("close", 10)                       # ... and nothing more went to the node that was closed


async def test_cancelling_a_long_move_stops_the_camera_and_leaves_the_position_unknown():
    camera, v4l2, _ = camera_for()
    await camera.find_stops()
    assert camera.position_known
    slow_moves(camera)
    homing = asyncio.create_task(camera.find_stops())
    await asyncio.sleep(0.03)
    assert camera.busy
    homing.cancel()                                              # a shutdown, or a request that went away
    with pytest.raises(asyncio.CancelledError):
        await homing
    assert not camera.busy and not camera.position_known
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 0


async def test_the_stop_after_a_cancelled_long_move_finishes_even_if_it_is_cancelled_again():
    camera, v4l2, _ = camera_for()
    await camera.find_stops()
    slow_moves(camera)
    stopping, resume = threading.Event(), threading.Event()
    write = v4l2.set_many

    def held_write(fd, values):                                  # the stop stays on its way to the camera until the test says so
        if not any(values.values()):
            stopping.set()
            assert resume.wait(5)
        write(fd, values)

    v4l2.set_many = held_write
    homing = asyncio.create_task(camera.find_stops())
    await asyncio.sleep(0.03)
    homing.cancel()
    assert await asyncio.to_thread(stopping.wait, 2)             # the stop that follows the cancel is in flight ...
    homing.cancel()                                              # ... when the request is cancelled again
    with pytest.raises(asyncio.CancelledError):
        await homing
    resume.set()
    for _ in range(200):                                         # the stop still finishes: nothing is left counted as moving
        if not camera.state()["moving"]:
            break
        await asyncio.sleep(0.005)
    assert camera.state()["moving"] is False and not camera.position_known
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 0


async def test_of_several_requests_waiting_for_a_long_move_the_last_one_wins():
    camera, _, _ = camera_for()
    slow_moves(camera)
    first = asyncio.create_task(camera.find_stops())
    await asyncio.sleep(0.03)
    second = asyncio.create_task(camera.find_stops())            # both wait for the first one to stop ...
    third = asyncio.create_task(camera.find_stops())
    results = await asyncio.gather(first, second, third, return_exceptions=True)
    assert isinstance(results[0], Interrupted)
    assert isinstance(results[1], Interrupted)                   # ... and the second is cut short by the third, not run beside it
    assert isinstance(results[2], dict) and camera.position_known and not camera.busy


async def test_a_recall_that_cuts_another_short_is_refused_cleanly_when_no_home_is_saved(tmp_path):
    camera, v4l2, clock = camera_for(tmp_path)
    await camera.find_stops()                                    # this room never saved a home
    await camera.move(1, 0)
    await clock.sleep(2.0)
    await camera.stop()
    await camera.save(1)                                         # pan 2 s
    await camera.move(1, 0)
    await clock.sleep(2.0)
    await camera.stop()
    await camera.save(2)                                         # pan 4 s
    slow_moves(camera, 0.1)
    first = asyncio.create_task(camera.recall(1))                # 2 s of pan back: 0.2 s of real time
    await asyncio.sleep(0.03)
    with pytest.raises(NotReady) as failure:
        await camera.recall(2)                                   # cuts the first one short, which leaves the position unknown ...
    assert str(failure.value) == "home the camera first"         # ... and with no home there is nothing to find it again from
    with pytest.raises(Interrupted):
        await first
    assert not camera.busy and not camera.position_known and camera.state()["moving"] is False
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 0


async def test_the_long_moves_need_a_camera():
    camera, v4l2, _ = camera_for(nodes={"/dev/video19": {}})
    camera._home = {"pan_s": 1.0, "tilt_s": 1.0}
    camera._presets = {"1": {"pan_s": 1.0, "tilt_s": 1.0, "zoom": 200}}
    for action in (camera.find_stops, camera.home, lambda: camera.recall(1)):
        with pytest.raises(DeviceUnavailable):
            await action()
    assert not camera.busy
    assert v4l2.set_many_calls == [] and v4l2.sets(V4L2_CID_ZOOM_ABSOLUTE) == []


async def test_a_recall_refused_for_want_of_a_home_leaves_a_homing_under_way_alone(tmp_path):
    camera, _, _ = camera_for(tmp_path)
    camera._presets = {"1": {"pan_s": 1.0, "tilt_s": 1.0, "zoom": 200}}
    slow_moves(camera)
    homing = asyncio.create_task(camera.find_stops())            # the position is unknown until this ends, and no home is saved
    await asyncio.sleep(0.03)
    with pytest.raises(NotReady) as failure:
        await camera.recall(1)
    assert str(failure.value) == "home the camera first"
    await homing                                                 # the refusal did not cut it short: it runs to the end
    assert camera.position_known and not camera.busy


async def test_a_long_move_is_not_stopped_by_the_watchdog_of_an_earlier_move():
    camera, v4l2, _ = camera_for()
    camera.WATCHDOG_S = 0.05
    await camera.move(1, 0)                                      # a move whose watchdog is armed ...
    watchdog = camera._watchdog
    slow_moves(camera)                                           # ... and a homing that takes 0.16 s, well past the 50 ms
    await camera.find_stops()
    written = (v4l2.sets(V4L2_CID_PAN_SPEED), v4l2.sets(V4L2_CID_TILT_SPEED))
    assert written == ([1, -1, 0], [0, -1, 0])                   # no stop in the middle: the homing ended at zero speeds
    await watchdog                                               # the watchdog was left alone: it has gone off, or goes off now ...
    assert (v4l2.sets(V4L2_CID_PAN_SPEED), v4l2.sets(V4L2_CID_TILT_SPEED)) == written   # ... and writes nothing
    assert camera.position_known


async def test_a_watchdog_armed_after_a_long_move_began_does_not_stop_it():
    camera, v4l2, _ = camera_for()
    camera.WATCHDOG_S = 0.02
    release = asyncio.Event()

    async def held(seconds):                                     # a long move waits here until the test lets it go
        await release.wait()

    camera._sleep = held
    first = asyncio.create_task(camera.find_stops())
    while not camera.busy:
        await asyncio.sleep(0)
    arrow = asyncio.create_task(camera.move(1, 0))               # an arrow press and a second homing request both wait for the first ...
    homing = asyncio.create_task(camera.find_stops())
    with pytest.raises(Interrupted):
        await first
    await arrow                                                  # ... the arrow wakes first, so its watchdog is armed after the new homing began
    await camera._watchdog                                       # and goes off while that homing is waiting at the stops
    release.set()
    await homing
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [-1, 0, 1, -1, 0]    # first homing, its stop, the arrow, the new homing, its own stop: nothing between
    assert v4l2.sets(V4L2_CID_TILT_SPEED) == [-1, 0, 0, -1, 0]
    assert camera.position_known and not camera.busy


async def test_a_watchdog_that_is_already_writing_is_not_overtaken_by_a_long_move():
    camera, v4l2, _ = camera_for()
    camera.WATCHDOG_S = 0.01
    stopping, resume = threading.Event(), threading.Event()
    write = v4l2.set_many

    def held_write(fd, values):                                  # the watchdog's stop stays on its way to the camera until the test says so
        if not any(values.values()) and not stopping.is_set():
            stopping.set()
            assert resume.wait(5)
        write(fd, values)

    v4l2.set_many = held_write
    await camera.move(1, 0)
    assert await asyncio.to_thread(stopping.wait, 2)             # the page went quiet: the watchdog's stop is in flight ...
    homing = asyncio.create_task(camera.find_stops())            # ... when a homing is asked for
    await asyncio.sleep(0.05)                                    # time enough for a write that does not wait its turn to land first
    resume.set()
    await homing
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [1, 0, -1, 0]        # the stop landed first, then the homing began and ended
    assert v4l2.sets(V4L2_CID_TILT_SPEED) == [0, 0, -1, 0]
    assert camera.position_known


async def test_what_the_camera_cannot_do_yet_is_refused_before_it_is_touched():
    camera, v4l2, _ = camera_for()
    for action in (camera.save_home, lambda: camera.save(1), camera.home):
        with pytest.raises(NotReady):
            await action()
    assert v4l2.calls == []                                      # not even opened


# --- moving by the difference, saving a position, naming a slot ---

async def test_recall_moves_back_by_the_difference_and_the_shorter_leg_stops_first():
    camera, v4l2, _ = camera_for()
    await camera.find_stops()
    camera._pan_s, camera._tilt_s = 4.0, 3.0                     # up and to the right of a saved view ...
    camera._presets = {"1": {"pan_s": 1.0, "tilt_s": 2.0, "zoom": 100}}
    v4l2.calls.clear()
    await camera.recall(1)                                       # ... so both axes go back: the tilt has 1 s to go, the pan 3 s
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [-1, -1, 0] and v4l2.sets(V4L2_CID_TILT_SPEED) == [-1, 0, 0]
    assert (camera._pan_s, camera._tilt_s) == (1.0, 2.0)


async def test_a_recall_that_starts_while_the_camera_is_still_moving_lands_on_its_preset(tmp_path):
    camera, v4l2, clock = camera_for(tmp_path)
    await camera.find_stops()
    await camera.move(1, 0)
    await clock.sleep(1.0)
    await camera.stop()
    await camera.save(1)                                         # pan 1 s
    await camera.move(1, 0)                                      # an arrow press whose release never arrives ...
    await clock.sleep(1.0)                                       # ... the camera is a second on, and nothing has stopped it yet
    v4l2.calls.clear()
    await camera.recall(1)
    assert v4l2.sets(V4L2_CID_PAN_SPEED) == [-1, 0]              # back by that second, not "already there"
    assert camera._pan_s == 1.0 and camera.position_known
    await camera.stop()                                          # and the watchdog of that arrow press goes with it


@pytest.mark.parametrize("start, saved, travel_s",
                         [(0.0, 1e9, 8.0), (0.0, 7.5, 6.0), (3.0, -5.0, 8.0)],
                         ids=["a huge number typed by hand", "the travel was lowered after saving", "a negative number"])
async def test_a_saved_position_outside_the_travel_is_clamped_to_it(start, saved, travel_s):
    camera, _, clock = camera_for(travel_s=travel_s)
    await camera.find_stops()
    camera._pan_s = camera._tilt_s = start
    camera._presets = {"1": {"pan_s": saved, "tilt_s": saved, "zoom": 100}}
    before = clock.now
    await camera.recall(1)
    target = 0.0 if saved < 0 else travel_s
    assert clock.now - before == abs(target - start)             # both axes ran to the end of their travel, not for as long as the number says
    assert (camera._pan_s, camera._tilt_s) == (target, target)


async def test_find_stops_runs_for_the_configured_travel_time():
    camera, _, clock = camera_for(travel_s=6.0)
    before = clock.now
    await camera.find_stops()
    assert clock.now - before == 6.0
    await camera.move(1, 0)
    await clock.sleep(10.0)
    await camera.stop()
    assert camera._pan_s == 6.0                                  # the estimate stops at the configured travel


async def test_a_saved_position_is_rounded_to_the_millisecond(tmp_path):
    camera, _, clock = camera_for(tmp_path)
    await camera.find_stops()
    await camera.move(1, 0)
    await clock.sleep(0.1)
    await clock.sleep(0.2)
    await camera.stop()
    assert camera._pan_s != 0.3                                  # floating point: the estimate itself is 0.29999999999999716
    await camera.save_home()
    assert SettingsStore(tmp_path / "control-settings.json").get("camera")["home"]["pan_s"] == 0.3


async def test_saving_while_the_camera_is_still_moving_does_not_lose_the_rest_of_the_move(tmp_path):
    camera, _, clock = camera_for(tmp_path)
    await camera.find_stops()
    await camera.move(1, 0)
    await clock.sleep(2.0)
    await camera.save_home()                                     # a second tablet saves while the arrow is still held
    await clock.sleep(1.0)
    await camera.save(1)
    await clock.sleep(1.0)
    await camera.stop()
    saved = SettingsStore(tmp_path / "control-settings.json").get("camera")
    assert saved["home"] == {"pan_s": 2.0, "tilt_s": 0.0} and saved["presets"]["1"]["pan_s"] == 3.0
    assert camera._pan_s == 4.0                                  # every second of the move is in the estimate


async def test_a_preset_is_not_saved_when_the_position_becomes_unknown_while_the_zoom_is_read(tmp_path):
    camera, v4l2, _ = camera_for(tmp_path)
    await camera.find_stops()
    reading, resume = threading.Event(), threading.Event()
    read = v4l2.get

    def held_read(fd, cid):                                      # the zoom read stays on its way to the camera until the test says so
        reading.set()
        assert resume.wait(5)
        return read(fd, cid)

    v4l2.get = held_read
    saving = asyncio.create_task(camera.save(1))                 # the zoom was never read, so this goes to the camera
    assert await asyncio.to_thread(reading.wait, 5)
    camera._position_known = False                               # a long move was cut short in the meantime
    resume.set()
    with pytest.raises(NotReady) as failure:
        await saving
    assert str(failure.value) == "home the camera first"
    assert camera.state()["presets"][0]["saved"] is False


async def test_the_position_commands_answer_with_the_state_they_leave_behind(tmp_path):
    camera, _, _ = camera_for(tmp_path)
    assert (await camera.find_stops())["position_known"] is True
    assert (await camera.save_home())["home_saved"] is True
    assert (await camera.save(1))["presets"][0]["saved"] is True
    state = await camera.home()
    assert state["busy"] is False and state["moving"] is False and state["position_known"] is True
    state = await camera.recall(1)
    assert state["busy"] is False and state["moving"] is False and state["zoom"]["level"] == 100


@pytest.mark.parametrize("slot", [0, 4, True, 2.0, "2"], ids=["zero", "four", "True", "2.0", "text"])
async def test_a_preset_slot_is_a_whole_number_from_1_to_3(tmp_path, slot):
    camera, _, _ = camera_for(tmp_path)
    await camera.find_stops()
    with pytest.raises(ValueError):                              # True == 1 and 2.0 == 2, but neither is a slot number
        await camera.save(slot)
    with pytest.raises(ValueError):
        await camera.recall(slot)
    assert [preset["saved"] for preset in camera.state()["presets"]] == [False, False, False]
    assert SettingsStore(tmp_path / "control-settings.json").get("camera") is None      # nothing was written


# --- what is read back from the store ---

def test_a_corrupt_saved_home_and_preset_are_ignored_and_a_good_preset_beside_them_is_kept(tmp_path, caplog):
    store = SettingsStore(tmp_path / "control-settings.json")
    store.save("camera", {"home": {"pan_s": "far", "tilt_s": 1.0},
                          "presets": {"1": {"pan_s": 1.0, "tilt_s": True, "zoom": 200},
                                      "2": {"pan_s": 0.5, "tilt_s": 1.5, "zoom": 300}}})
    with caplog.at_level(logging.DEBUG):
        camera = RoomCamera(store=store, v4l2=FakeV4l2())        # a damaged file does not stop the service
    assert camera.home_saved is False
    assert [preset["saved"] for preset in camera.state()["presets"]] == [False, True, False]
    assert [r.levelno for r in caplog.records if "ignor" in r.getMessage().lower()] == [logging.DEBUG, logging.DEBUG]
    assert not [r for r in caplog.records if r.levelno > logging.DEBUG]                  # quietly: it is not an error


@pytest.mark.parametrize("bad", [None, "1", True, float("nan"), float("inf"), 10 ** 400],
                         ids=["None", "text", "a boolean", "nan", "infinity", "an integer too large for a float"])
def test_a_saved_value_that_is_not_a_finite_number_drops_its_entry(tmp_path, bad):
    store = SettingsStore(tmp_path / "control-settings.json")
    good = {"pan_s": 1.0, "tilt_s": 2.0, "zoom": 300}
    store.save("camera", {"home": {"pan_s": 1.0, "tilt_s": bad},
                          "presets": {"1": {**good, "pan_s": bad}, "2": {**good, "tilt_s": bad}, "3": {**good, "zoom": bad}}})
    camera = RoomCamera(store=store, v4l2=FakeV4l2())
    assert camera.home_saved is False
    assert [preset["saved"] for preset in camera.state()["presets"]] == [False, False, False]


def test_a_saved_home_or_preset_that_lacks_a_number_is_ignored(tmp_path):
    store = SettingsStore(tmp_path / "control-settings.json")
    store.save("camera", {"home": {"pan_s": 1.0},
                          "presets": {"1": {"pan_s": 1.0, "tilt_s": 2.0}, "2": {"tilt_s": 2.0, "zoom": 300}, "3": {"pan_s": 1.0, "zoom": 300}}})
    camera = RoomCamera(store=store, v4l2=FakeV4l2())
    assert camera.home_saved is False
    assert [preset["saved"] for preset in camera.state()["presets"]] == [False, False, False]


def test_whole_numbers_are_valid_saved_positions(tmp_path):
    store = SettingsStore(tmp_path / "control-settings.json")
    store.save("camera", {"home": {"pan_s": 1, "tilt_s": 2}, "presets": {"1": {"pan_s": 0, "tilt_s": 2, "zoom": 300}}})
    camera = RoomCamera(store=store, v4l2=FakeV4l2())
    assert camera.home_saved is True and camera.state()["presets"][0]["saved"] is True


# --- the store keeps what it is given, so the camera and the store never share a dict ---

def test_the_camera_keeps_its_own_copies_of_what_it_loads(tmp_path):
    store = SettingsStore(tmp_path / "control-settings.json")
    store.save("camera", {"home": {"pan_s": 1.0, "tilt_s": 2.0}, "presets": {"3": {"pan_s": 0.5, "tilt_s": 0.5, "zoom": 300}}})
    camera = RoomCamera(store=store, v4l2=FakeV4l2())
    camera._home["pan_s"] = 9.0                                  # an edit under way, not saved ...
    camera._presets["3"]["zoom"] = 999
    store.save("screensaver", "clock")                           # ... while another key is saved, which rewrites the whole file
    saved = SettingsStore(tmp_path / "control-settings.json").get("camera")
    assert saved == {"home": {"pan_s": 1.0, "tilt_s": 2.0}, "presets": {"3": {"pan_s": 0.5, "tilt_s": 0.5, "zoom": 300}}}


async def test_the_camera_gives_the_store_copies_of_its_home_and_presets(tmp_path):
    camera, _, _ = camera_for(tmp_path)
    await camera.find_stops()
    await camera.save(1)                                         # a preset before any home
    assert SettingsStore(tmp_path / "control-settings.json").get("camera")["home"] is None
    reloaded = RoomCamera(store=SettingsStore(tmp_path / "control-settings.json"), v4l2=FakeV4l2())
    assert reloaded.home_saved is False and reloaded.state()["presets"][0]["saved"] is True
    await camera.save_home()
    camera._home["pan_s"] = 9.0                                  # an edit under way, not saved ...
    camera._presets["1"]["zoom"] = 999
    camera._store.save("screensaver", "clock")                   # ... while another key is saved, which rewrites the whole file
    saved = SettingsStore(tmp_path / "control-settings.json").get("camera")
    assert saved == {"home": {"pan_s": 0.0, "tilt_s": 0.0}, "presets": {"1": {"pan_s": 0.0, "tilt_s": 0.0, "zoom": 100}}}
