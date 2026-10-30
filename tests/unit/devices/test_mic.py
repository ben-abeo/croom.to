"""
The room microphone's level (spec 2026-10-29, Mic level): the source found in pw-dump, its level 0 to 150 with
wpctl, the chosen level kept in the settings, and the devices service putting it back during a meeting.
"""

import asyncio
import json
import logging

import pytest

from croom.control.settings import SettingsStore
from croom.devices.camera import RoomCamera
from croom.devices.errors import DeviceUnavailable
from croom.devices.mic import RoomMic
from croom.devices.service import DevicesService
from croom.devices.volume import RoomVolume
from croom.meeting.providers.base import MeetingState
from tests.unit.devices.fake_v4l2 import FakeClock as CameraClock, FakeV4l2
from tests.unit.devices.test_volume import PW_DUMP, FakeClock, FakeRunner, SlowRunner

MEETUP_IN = "alsa_input.usb-Logitech_MeetUp-00.analog-stereo"
SOURCE_ID = "58"


def source_node(node_id, name, description=None):
    props = {"media.class": "Audio/Source", "node.name": name}
    if description:
        props["node.description"] = description
    return {"id": node_id, "type": "PipeWire:Interface:Node", "version": 3, "info": {"props": props}}


def dump_with(*extra, default=MEETUP_IN):
    objects = json.loads(PW_DUMP) + list(extra)
    objects[0]["metadata"][1]["value"]["name"] = default
    return json.dumps(objects)


WEBCAM = source_node(70, "alsa_input.usb-Webcam_C920-02.analog-stereo", "HD Pro Webcam C920 Analog Stereo")
NO_SOURCES = json.dumps([o for o in json.loads(PW_DUMP) if (o.get("info") or {}).get("props", {}).get("media.class") != "Audio/Source"])


def mic_for(preference="auto", store=None, runner_class=FakeRunner, **runner_args):
    runner_args.setdefault("volume", "Volume: 1.00\n")
    runner = runner_class(**runner_args)
    clock = FakeClock()
    return RoomMic(preference, runner=runner, clock=clock, store=store), runner, clock


def sets(runner):
    return [call for call in runner.calls if call[:2] == ["wpctl", "set-volume"]]


# --- finding the source ---

async def test_the_default_source_is_the_meetups_microphone_and_sinks_are_left_out():
    mic, runner, _ = mic_for()
    state = await mic.refresh()
    assert state == {"available": True, "device": "Logitech MeetUp Speakerphone Analog Stereo", "level": 100, "device_level": 100,
                     "reason": None}
    assert runner.calls == [["pw-dump"], ["wpctl", "get-volume", SOURCE_ID]]


async def test_without_a_preference_the_default_source_is_followed():
    mic, runner, _ = mic_for(dump=dump_with(WEBCAM, default=WEBCAM["info"]["props"]["node.name"]))
    assert (await mic.refresh())["device"] == "HD Pro Webcam C920 Analog Stereo"
    assert runner.calls[-1] == ["wpctl", "get-volume", "70"]
    assert not [call for call in runner.calls if call[:2] == ["wpctl", "set-default"]]


async def test_a_preference_picks_the_source_and_makes_it_pipewires_default(caplog):
    mic, runner, _ = mic_for("C920", dump=dump_with(WEBCAM))
    with caplog.at_level(logging.INFO):
        state = await mic.refresh()
    assert state["available"] and state["device"] == "HD Pro Webcam C920 Analog Stereo"
    assert runner.calls == [["pw-dump"], ["wpctl", "set-default", "70"], ["wpctl", "get-volume", "70"]]
    assert any("the default input, as audio.input_device asks" in r.getMessage() for r in caplog.records)


async def test_no_matching_source_and_no_source_at_all_are_unavailable_with_the_reason(caplog):
    mic, _, _ = mic_for("Jabra")
    with caplog.at_level(logging.WARNING):
        assert (await mic.refresh())["reason"] == "no source matches 'Jabra'"
    assert [r.getMessage() for r in caplog.records] == ["Microphone unavailable: no source matches 'Jabra'"]
    mic, _, _ = mic_for(dump=NO_SOURCES)
    state = await mic.refresh()
    assert state["available"] is False and state["reason"] == "no audio source"
    with pytest.raises(DeviceUnavailable, match="no audio source"):
        await mic.set_level(120)


async def test_levels_read_above_one_hundred_show_up_to_one_hundred_and_fifty():
    mic, _, _ = mic_for(volume="Volume: 1.30\n")
    assert (await mic.refresh())["level"] == 130
    mic, _, _ = mic_for(volume="Volume: 2.00\n")
    assert (await mic.refresh())["level"] == 150


# --- changing it ---

async def test_set_level_clamps_to_0_and_150_and_wpctl_takes_1_5():
    mic, runner, _ = mic_for()
    assert (await mic.set_level(200))["level"] == 150 and runner.calls[-1] == ["wpctl", "set-volume", SOURCE_ID, "1.50"]
    assert (await mic.set_level(-4))["level"] == 0 and runner.calls[-1] == ["wpctl", "set-volume", SOURCE_ID, "0.00"]
    assert (await mic.set_level(125))["level"] == 125 and runner.calls[-1] == ["wpctl", "set-volume", SOURCE_ID, "1.25"]


async def test_step_goes_from_the_chosen_level_and_stops_at_the_ends(tmp_path):
    mic, runner, _ = mic_for(store=SettingsStore(tmp_path / "s.json"))
    assert (await mic.step(5))["level"] == 105                 # nothing chosen yet: from what the source reads (1.00)
    runner.volume = "Volume: 0.60\n"                           # the call's gain control pulled it down since
    assert (await mic.step(5))["level"] == 110                 # Louder is louder than what was chosen, not than 60
    assert runner.calls[-1] == ["wpctl", "set-volume", SOURCE_ID, "1.10"]
    await mic.set_level(148)
    assert (await mic.step(5))["level"] == 150
    await mic.set_level(3)
    assert (await mic.step(-5))["level"] == 0


async def test_louder_from_above_150_percent_set_elsewhere_writes_nothing():
    mic, runner, _ = mic_for(volume="Volume: 1.80\n")
    assert (await mic.step(5))["level"] == 150 and sets(runner) == []
    assert (await mic.step(-5))["level"] == 145 and sets(runner) == [["wpctl", "set-volume", SOURCE_ID, "1.45"]]


async def test_the_chosen_level_is_kept_in_the_settings_and_read_back(tmp_path):
    store = SettingsStore(tmp_path / "control-settings.json")
    mic, _, _ = mic_for(store=store)
    await mic.set_level(130)
    assert json.loads((tmp_path / "control-settings.json").read_text())["mic_level"] == 130
    again, _, _ = mic_for(store=SettingsStore(tmp_path / "control-settings.json"), volume="Volume: 0.70\n")
    assert again.chosen == 130
    state = await again.refresh()
    assert state["level"] == 130 and state["device_level"] == 70   # the page shows what the room keeps


@pytest.mark.parametrize("stored", ["loud", 151, -1, True, 1.2, None])
async def test_a_stored_level_that_is_not_one_is_no_choice(tmp_path, stored):
    store = SettingsStore(tmp_path / "s.json")
    store.save("mic_level", stored)
    mic, _, _ = mic_for(store=store)
    assert mic.chosen is None


async def test_a_level_that_cannot_be_saved_is_held_and_warned_about(tmp_path, caplog):
    store = SettingsStore(tmp_path / "s.json")
    mic, _, _ = mic_for(store=store)

    def broken(key, value):
        raise OSError("read-only file system")
    store.save = broken
    with caplog.at_level(logging.WARNING):
        assert (await mic.set_level(120))["level"] == 120
    assert store.get("mic_level") == 120 and mic.chosen == 120
    assert any("read-only file system" in r.getMessage() for r in caplog.records)


async def test_a_failing_set_raises_and_keeps_the_choice_it_had(tmp_path):
    mic, runner, _ = mic_for(store=SettingsStore(tmp_path / "s.json"))
    await mic.set_level(110)
    runner.fail["wpctl set-volume"] = "no such node"
    with pytest.raises(DeviceUnavailable, match="wpctl set-volume failed: no such node"):
        await mic.set_level(140)
    assert mic.chosen == 110 and mic.state()["available"] is False


# --- putting it back ---

async def test_reapply_writes_the_chosen_level_only_when_the_source_has_moved(tmp_path):
    mic, runner, _ = mic_for(store=SettingsStore(tmp_path / "s.json"))
    await mic.reapply()
    assert sets(runner) == []                                  # nothing chosen: the room leaves the source alone
    await mic.set_level(120)
    runner.calls.clear()
    runner.volume = "Volume: 1.20\n"
    await mic.reapply()
    assert sets(runner) == [] and runner.calls[0] == ["pw-dump"]   # read afresh, and where it should be
    runner.volume = "Volume: 0.55\n"                           # the gain control moved it
    assert (await mic.reapply())["level"] == 120
    assert sets(runner) == [["wpctl", "set-volume", SOURCE_ID, "1.20"]]


async def test_reapply_without_a_source_does_not_raise():
    mic, runner, _ = mic_for()
    await mic.set_level(90)
    runner.fail["pw-dump"] = "pipewire is not running"
    assert (await mic.reapply())["available"] is False
    del runner.fail["pw-dump"]
    runner.fail["wpctl set-volume"] = "gone"
    assert (await mic.reapply())["available"] is False


async def test_reapply_puts_back_the_level_a_person_chose_while_it_waited():
    """A slider moved while a re-apply waits for its turn: the re-apply writes the new level, never the old one over it."""
    mic, runner, _ = mic_for(runner_class=SlowRunner)
    await mic.set_level(100)
    runner.volume = "Volume: 0.40\n"
    await asyncio.gather(mic.set_level(140), mic.reapply())
    assert sets(runner)[-1] == ["wpctl", "set-volume", SOURCE_ID, "1.40"] and mic.chosen == 140
    mic2, runner2, _ = mic_for(runner_class=SlowRunner)
    await mic2.set_level(100)
    runner2.volume = "Volume: 0.40\n"
    await asyncio.gather(mic2.reapply(), mic2.set_level(140))
    assert sets(runner2)[-1] == ["wpctl", "set-volume", SOURCE_ID, "1.40"] and mic2.chosen == 140


def test_from_config_takes_the_input_device_and_the_store(tmp_path):
    from croom.core.config import Config
    store = SettingsStore(tmp_path / "s.json")
    store.save("mic_level", 125)
    config = Config.from_dict({"audio": {"input_device": "MeetUp"}})
    service = DevicesService.from_config(config, store)
    assert service.mic._preference == "MeetUp" and service.mic.chosen == 125


# --- the devices service: at each join, and every few seconds while connected ---

class StubMeeting:
    def __init__(self):
        self.callbacks = []

    def add_state_callback(self, callback):
        self.callbacks.append(callback)

    def tell(self, state):
        for callback in self.callbacks:
            callback(state)


def service_with_mic(tmp_path, level=120):
    store = SettingsStore(tmp_path / "control-settings.json")
    if level is not None:
        store.save("mic_level", level)
    runner = FakeRunner(volume="Volume: 0.50\n")                # what the call's gain control left: never what was chosen
    mic = RoomMic(runner=runner, clock=FakeClock(), store=store)
    camera = RoomCamera(store=store, v4l2=FakeV4l2({}), clock=CameraClock(), sleep=CameraClock().sleep)
    meeting = StubMeeting()
    service = DevicesService(RoomVolume(runner=FakeRunner()), camera, mic=mic, meeting=meeting)
    service.MIC_REAPPLY_S = 0.02
    return service, runner, meeting


async def until(condition, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "gave up waiting"
        await asyncio.sleep(0.005)


async def test_the_level_is_put_back_at_join_and_every_few_seconds_until_the_meeting_ends(tmp_path):
    service, runner, meeting = service_with_mic(tmp_path)
    await service.start()
    try:
        assert len(meeting.callbacks) == 1
        await asyncio.sleep(0.06)
        assert sets(runner) == []                                     # no meeting: nothing is put back
        meeting.tell(MeetingState.JOINING)
        await asyncio.sleep(0.03)
        assert sets(runner) == []
        meeting.tell(MeetingState.CONNECTED)
        await until(lambda: len(sets(runner)) >= 1)                   # at once, on joining
        assert sets(runner)[0] == ["wpctl", "set-volume", SOURCE_ID, "1.20"]
        await until(lambda: len(sets(runner)) >= 3)                   # and again while it lasts
        meeting.tell(MeetingState.CONNECTED)                          # said again: still one loop
        meeting.tell(MeetingState.LEAVING)
        await asyncio.sleep(0.01)
        count = len(sets(runner))
        await asyncio.sleep(0.08)
        assert len(sets(runner)) == count                             # over: nothing more
    finally:
        await service.stop()


async def test_a_level_moved_on_the_page_is_what_is_put_back(tmp_path):
    service, runner, meeting = service_with_mic(tmp_path)
    await service.start()
    try:
        meeting.tell(MeetingState.CONNECTED)
        await until(lambda: len(sets(runner)) >= 1)
        await service.mic.set_level(80)
        runner.calls.clear()
        await until(lambda: len(sets(runner)) >= 2)
        assert set(map(tuple, sets(runner))) == {("wpctl", "set-volume", SOURCE_ID, "0.80")}
    finally:
        await service.stop()


async def test_nothing_is_put_back_while_the_laptop_has_the_meetup(tmp_path):
    service, runner, meeting = service_with_mic(tmp_path)
    await service.start()
    try:
        await service.release()
        runner.calls.clear()
        meeting.tell(MeetingState.CONNECTED)
        await asyncio.sleep(0.08)
        assert runner.calls == []                                     # not looked for, not written
        service._away = False                                         # (reclaim's camera search is the camera tests')
        await until(lambda: len(sets(runner)) >= 1)
    finally:
        await service.stop()


async def test_no_choice_means_the_source_is_left_alone_in_a_meeting(tmp_path):
    service, runner, meeting = service_with_mic(tmp_path, level=None)
    await service.start()
    try:
        meeting.tell(MeetingState.CONNECTED)
        await asyncio.sleep(0.08)
        assert sets(runner) == []
    finally:
        await service.stop()


async def test_stop_ends_the_loop_that_puts_the_level_back(tmp_path):
    service, runner, meeting = service_with_mic(tmp_path)
    await service.start()
    meeting.tell(MeetingState.CONNECTED)
    await until(lambda: len(sets(runner)) >= 1)
    task = service._mic_task
    await service.stop()
    assert task.done() and service._mic_task is None


async def test_start_looks_for_the_microphone_and_the_background_check_looks_again(tmp_path, caplog):
    service, runner, _ = service_with_mic(tmp_path)
    runner.fail["pw-dump"] = "pipewire is not running"
    service.REDISCOVER_S = 0.02
    with caplog.at_level(logging.INFO):
        await service.start()
    try:
        assert any(r.getMessage().endswith("microphone none") for r in caplog.records)
        del runner.fail["pw-dump"]
        await until(lambda: service.mic.available)
    finally:
        await service.stop()


async def test_a_meeting_that_connects_after_stop_starts_nothing(tmp_path):
    service, runner, meeting = service_with_mic(tmp_path)
    await service.start()
    await service.stop()
    meeting.tell(MeetingState.CONNECTED)
    await asyncio.sleep(0.05)
    assert service._mic_task is None and sets(runner) == []
