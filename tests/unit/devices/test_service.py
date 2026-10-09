"""
The devices service starts the speaker and the camera, homes the camera when a
home is saved, and keeps looking for devices that are missing.
"""

import asyncio
import json
import logging

import pytest

from croom.control.settings import SettingsStore
from croom.devices.camera import RoomCamera
from croom.devices.errors import NotReady
from croom.devices.service import DevicesService
from croom.devices.v4l2 import V4L2_CID_PAN_SPEED, V4L2_CID_TILT_SPEED
from croom.devices.volume import RoomVolume
from tests.unit.devices.fake_v4l2 import MEETUP, FakeClock, FakeV4l2
from tests.unit.devices.test_volume import FakeRunner

HOME = {"pan_s": 1.0, "tilt_s": 0.5}
# A sink node pw-dump never emits without an id: the volume module answers "output not understood" for it.
NODE_WITHOUT_ID = json.dumps([{"type": "PipeWire:Interface:Node",
                               "info": {"props": {"media.class": "Audio/Sink", "node.name": "odd-sink"}}}])


def service_for(tmp_path, home=None, nodes=None, sleep=None):
    store = SettingsStore(tmp_path / "control-settings.json")
    if home is not None:
        store.save("camera", {"home": home, "presets": {}})
    clock = FakeClock()
    v4l2 = FakeV4l2(nodes)
    camera = RoomCamera(store=store, v4l2=v4l2, clock=clock, sleep=sleep or clock.sleep)
    runner = FakeRunner()
    return DevicesService(RoomVolume(runner=runner), camera), v4l2, runner


def records(caplog, text):
    """The log records whose message contains the text."""
    return [r for r in caplog.records if text in r.getMessage()]


def levels(caplog, text):
    """The levels of those records."""
    return [r.levelno for r in records(caplog, text)]


async def parked(seconds):
    """A camera sleep that never ends by itself: a homing that reaches it stays under way until it is interrupted."""
    await asyncio.Event().wait()


async def bounded(awaitable, timeout=2.0):
    """A step that must finish while the camera is parked: a regression times out instead of hanging the run."""
    return await asyncio.wait_for(awaitable, timeout)


async def until(condition, timeout=2.0):
    """Let the loop run until the condition holds (the camera's control calls run in worker threads)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "gave up waiting"
        await asyncio.sleep(0.001)


async def test_start_finds_both_devices_and_homes_the_camera_when_a_home_is_saved(tmp_path):
    service, v4l2, runner = service_for(tmp_path, home=HOME)
    await service.start()
    try:
        await service.homing
        assert service.volume.available and ["pw-dump"] in runner.calls
        assert service.camera.available and service.camera.position_known
        assert v4l2.sets(V4L2_CID_PAN_SPEED)[:2] == [-1, 0]   # it went to the stops first
    finally:
        await service.stop()
    assert v4l2.calls[-1] == ("close", 10)


async def test_start_does_not_move_the_camera_without_a_saved_home(tmp_path):
    service, v4l2, _ = service_for(tmp_path)
    await service.start()
    try:
        assert service.camera.available and not service.camera.position_known
        assert service.homing is None
        assert not any(c[0] == "set" for c in v4l2.calls)
    finally:
        await service.stop()


async def test_a_missing_camera_is_looked_for_again(tmp_path):
    service, v4l2, _ = service_for(tmp_path, nodes={"/dev/video19": {}})
    service.REDISCOVER_S = 0.05
    await service.start()
    try:
        assert not service.camera.available
        v4l2._nodes["/dev/video0"] = dict(MEETUP)
        await until(lambda: service.camera.available)
    finally:
        await service.stop()


def test_from_config_builds_both_from_the_config(tmp_path):
    from croom.core.config import Config
    config = Config.from_dict({"audio": {"output_device": "HDMI"}, "video": {"device": "/dev/video2"}})
    service = DevicesService.from_config(config, SettingsStore(tmp_path / "s.json"))
    assert service.name == "devices"
    assert service.volume._preference == "HDMI" and service.camera._device_pref == "/dev/video2"


# ----------------------------------------------------------------------
# Homing at start runs in the background: the services after this one are not held back by it
# ----------------------------------------------------------------------

async def test_a_camera_that_is_not_there_is_not_homed_even_when_a_home_is_saved(tmp_path):
    service, v4l2, _ = service_for(tmp_path, home=HOME, nodes={"/dev/video19": {}})
    await service.start()
    try:
        assert not service.camera.available and service.homing is None
        assert not any(c[0] == "set" for c in v4l2.calls)
    finally:
        await service.stop()


async def test_start_does_not_wait_for_the_camera_to_reach_its_stops(tmp_path):
    service, v4l2, _ = service_for(tmp_path, home=HOME, sleep=parked)
    await bounded(service.start())          # the homing is parked: start() must not be waiting for it
    try:
        await until(lambda: v4l2.sets(V4L2_CID_PAN_SPEED) == [-1])   # the camera is on its way to the stops
        assert service.camera.busy and not service.homing.done()
    finally:
        await bounded(service.stop())


async def test_a_request_on_the_page_interrupts_the_homing_at_start_without_a_warning(tmp_path, caplog):
    service, _, _ = service_for(tmp_path, home=HOME, sleep=parked)
    with caplog.at_level(logging.INFO):
        await bounded(service.start())
        try:
            await until(lambda: service.camera.busy)
            await service.camera.stop()      # the stop button, or any other camera request
            await bounded(service.homing)    # returns: nothing escapes the task
        finally:
            await bounded(service.stop())
    assert levels(caplog, "interrupted") == [logging.INFO]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not service.camera.position_known


async def test_a_camera_that_fails_while_homing_at_start_is_warned_about(tmp_path, caplog):
    service, v4l2, _ = service_for(tmp_path, home=HOME)
    with caplog.at_level(logging.INFO):
        await service.start()
        try:
            v4l2.fail = True                 # unplugged between being found and the first move
            await service.homing             # returns: nothing escapes the task
            assert not service.camera.available
        finally:
            await service.stop()
    assert levels(caplog, "Could not home the camera at start") == [logging.WARNING]


@pytest.mark.parametrize("error, level", [(NotReady("save a home first"), logging.WARNING),
                                          (RuntimeError("boom"), logging.ERROR)],
                         ids=["not ready", "unexpected error"])
async def test_nothing_escapes_the_homing_task(tmp_path, caplog, error, level):
    service, _, _ = service_for(tmp_path, home=HOME)

    async def home():
        raise error

    service.camera.home = home
    with caplog.at_level(logging.INFO):
        await service.start()
        try:
            await service.homing
        finally:
            await service.stop()
    assert levels(caplog, "Could not home the camera at start") == [level]


# ----------------------------------------------------------------------
# stop() leaves nothing pending
# ----------------------------------------------------------------------

async def test_stop_interrupts_a_homing_that_is_under_way_and_leaves_nothing_pending(tmp_path):
    before = asyncio.all_tasks()
    service, v4l2, _ = service_for(tmp_path, home=HOME, sleep=parked)
    await bounded(service.start())
    await until(lambda: v4l2.sets(V4L2_CID_PAN_SPEED) == [-1])   # the motors are running towards the stops
    await bounded(service.stop())
    assert service.homing.done() and service.homing.exception() is None   # finished by itself, not cancelled
    assert not service.camera.busy and not service.camera.available
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 0   # stopped, not left running
    assert v4l2.calls[-1] == ("close", 10)
    assert asyncio.all_tasks() - before == set()


async def test_stop_right_after_start_leaves_nothing_pending(tmp_path):
    before = asyncio.all_tasks()
    service, v4l2, _ = service_for(tmp_path, home=HOME)
    await service.start()
    await service.stop()                     # as the manager does when a later service fails to start
    assert service.homing.done() and service.homing.exception() is None
    assert not service.camera.available
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.calls[-1] == ("close", 10)
    assert asyncio.all_tasks() - before == set()


async def test_a_homing_task_that_was_cancelled_directly_does_not_make_stop_fail(tmp_path):
    before = asyncio.all_tasks()
    service, v4l2, _ = service_for(tmp_path, home=HOME, sleep=parked)
    await bounded(service.start())
    await until(lambda: service.camera.busy)
    service.homing.cancel()                  # something other than stop() cancels it
    await bounded(service.stop())            # waiting for a cancelled task must not raise its cancellation ...
    await bounded(service.stop())            # ... now, or on any later stop
    assert service.homing.cancelled()
    assert not service.camera.busy and not service.camera.available
    assert v4l2.sets(V4L2_CID_PAN_SPEED)[-1] == 0 and v4l2.sets(V4L2_CID_TILT_SPEED)[-1] == 0   # the motors were stopped
    assert asyncio.all_tasks() - before == set()


async def test_a_cancel_aimed_at_stop_is_not_swallowed(tmp_path):
    before = asyncio.all_tasks()
    service, _, _ = service_for(tmp_path, nodes={"/dev/video19": {}})   # no camera: stop() is only waiting for the check
    await service.start()
    stopping = asyncio.create_task(service.stop())
    await asyncio.sleep(0)                   # stop() has cancelled the check and is waiting for it to finish
    stopping.cancel()
    await asyncio.wait({stopping})
    assert stopping.cancelled()
    await service.stop()                     # a stop that was cancelled can be run again
    assert asyncio.all_tasks() - before == set()


async def test_stop_while_a_device_check_is_under_way_cancels_it(tmp_path):
    before = asyncio.all_tasks()
    service, _, _ = service_for(tmp_path, nodes={"/dev/video19": {}})
    service.REDISCOVER_S = 0.01
    entered = asyncio.Event()

    async def slow():                        # a probe that takes a while, as pw-dump can (up to 2 s)
        entered.set()
        await asyncio.Event().wait()

    await service.start()
    service.camera.discover = slow
    await bounded(entered.wait())
    await bounded(service.stop())            # the cancel reaches the probe: the check does not swallow it and go on
    assert asyncio.all_tasks() - before == set()


async def test_stop_cancels_the_check_for_missing_devices_and_waits_for_it(tmp_path):
    before = asyncio.all_tasks()
    service, _, _ = service_for(tmp_path, nodes={"/dev/video19": {}})   # no camera, so closing it has nothing to wait for
    await service.start()
    assert asyncio.all_tasks() - before                  # the check is running
    await service.stop()
    assert asyncio.all_tasks() - before == set()


async def test_a_restart_finds_the_devices_again_and_homes_the_camera_again(tmp_path):
    before = asyncio.all_tasks()
    service, v4l2, _ = service_for(tmp_path, home=HOME)
    await service.start()
    first = service.homing
    await first
    await service.restart()                  # what the manager's restart_service() does
    try:
        await service.homing
        assert service.homing is not first   # a second homing, not the first one's result
        assert service.camera.available and service.camera.position_known
    finally:
        await service.stop()
    assert v4l2.calls.count(("open", "/dev/video0")) == 2 and v4l2.calls.count(("close", 10)) == 2
    assert v4l2.sets(V4L2_CID_PAN_SPEED).count(-1) == 2      # each start sent the camera to its stops
    assert asyncio.all_tasks() - before == set()


# ----------------------------------------------------------------------
# What it found, and looking again
# ----------------------------------------------------------------------

async def test_start_logs_what_it_found(tmp_path, caplog):
    service, _, _ = service_for(tmp_path)
    with caplog.at_level(logging.INFO):
        await service.start()
    await service.stop()
    found = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Devices found")]
    assert len(found) == 1
    assert "Logitech MeetUp Speakerphone Analog Stereo" in found[0] and "/dev/video0" in found[0]


async def test_start_logs_what_it_did_not_find(tmp_path, caplog):
    service, _, runner = service_for(tmp_path, nodes={"/dev/video19": {}})
    runner.fail["pw-dump"] = "pipewire is not running"
    with caplog.at_level(logging.INFO):
        await service.start()
    await service.stop()
    found = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Devices found")]
    assert found == ["Devices found: speaker none, camera none"]


async def test_a_missing_speaker_is_looked_for_again(tmp_path):
    service, _, runner = service_for(tmp_path)
    runner.fail["pw-dump"] = "pipewire is not running"
    service.REDISCOVER_S = 0.02
    await service.start()
    try:
        assert not service.volume.available
        del runner.fail["pw-dump"]
        await until(lambda: service.volume.available)
    finally:
        await service.stop()


async def test_devices_that_are_present_are_not_probed_again(tmp_path):
    service, _, runner = service_for(tmp_path)
    service.REDISCOVER_S = 0.02
    await service.start()
    try:
        await asyncio.sleep(0.15)            # several rounds of the check
    finally:
        await service.stop()
    assert runner.calls.count(["pw-dump"]) == 1


async def test_a_check_that_fails_is_warned_about_once_per_message_and_tried_again(tmp_path, caplog):
    service, v4l2, _ = service_for(tmp_path, nodes={"/dev/video19": {}})
    service.REDISCOVER_S = 0.02
    errors = ["boom", "boom", "boom", "crash"]      # a bug that repeats, then a different one
    attempts = []
    with caplog.at_level(logging.DEBUG):
        await service.start()
        try:
            discover = service.camera.discover

            async def flaky():
                attempts.append(1)
                if errors:
                    raise RuntimeError(errors.pop(0))
                return await discover()

            service.camera.discover = flaky
            v4l2._nodes["/dev/video0"] = dict(MEETUP)
            await until(lambda: service.camera.available)
        finally:
            await service.stop()
    failures = records(caplog, "Device check failed")
    assert [r.levelno for r in failures] == [logging.WARNING, logging.WARNING]
    assert [r.getMessage().rsplit(": ", 1)[1] for r in failures] == ["boom", "crash"]   # a repeat is not logged again
    assert all(r.exc_info for r in failures)                                            # each with its traceback
    assert len(attempts) == 5                                                           # four failures, then the camera


# ----------------------------------------------------------------------
# A check that raises outside the device modules' contracts must not take the room down
# ----------------------------------------------------------------------

async def test_a_device_check_that_raises_at_start_does_not_stop_the_service_starting(tmp_path, caplog):
    service, _, _ = service_for(tmp_path, home=HOME)
    service.REDISCOVER_S = 0.02
    attempts = []

    async def broken():
        attempts.append(1)
        raise RuntimeError("boom")

    service.camera.discover = broken
    with caplog.at_level(logging.INFO):
        await service.start()                # must complete: the manager would stop every service otherwise
        try:
            assert service.volume.available and not service.camera.available   # the speaker was found all the same
            assert service.homing is None                                       # and there is no camera to home
            await until(lambda: len(attempts) >= 3)                             # the check keeps looking
        finally:
            await service.stop()
    failures = records(caplog, "Device check failed")
    assert [r.levelno for r in failures] == [logging.WARNING]   # once: the same failure at start and in the check
    assert "RuntimeError: boom" in failures[0].getMessage() and failures[0].exc_info


async def test_a_check_that_raises_for_one_device_does_not_stop_the_other_being_looked_for(tmp_path):
    service, _, runner = service_for(tmp_path)
    runner.fail["pw-dump"] = "pipewire is not running"
    service.REDISCOVER_S = 0.02

    async def broken():
        raise RuntimeError("boom")

    service.camera.discover = broken         # a bug in the camera's discovery, round after round
    await service.start()
    try:
        assert not service.volume.available
        del runner.fail["pw-dump"]
        await until(lambda: service.volume.available)      # the speaker is still looked for
    finally:
        await service.stop()


async def test_a_pw_dump_node_without_an_id_does_not_stop_the_camera_being_found_and_homed(tmp_path, caplog):
    service, _, runner = service_for(tmp_path, home=HOME)
    runner.dump = NODE_WITHOUT_ID
    with caplog.at_level(logging.INFO):
        await service.start()
        try:
            await service.homing
            assert not service.volume.available
            assert service.volume.state()["reason"].startswith("pw-dump output not understood: ")
            assert service.camera.available and service.camera.position_known   # found and homed all the same
        finally:
            await service.stop()
    assert records(caplog, "Device check failed") == []         # the speaker's module handled it: no bug escaped


async def test_a_speaker_check_that_raises_does_not_stop_the_camera_being_found_and_homed(tmp_path, caplog):
    service, _, _ = service_for(tmp_path, home=HOME)

    async def broken(force=False):
        raise RuntimeError("boom")

    service.volume.refresh = broken          # a bug in the speaker's module, outside its contract
    with caplog.at_level(logging.INFO):
        await service.start()
        try:
            await service.homing
            assert service.camera.available and service.camera.position_known   # found and homed all the same
        finally:
            await service.stop()
    failures = records(caplog, "Device check failed")
    assert [r.levelno for r in failures] == [logging.WARNING] and failures[0].exc_info
