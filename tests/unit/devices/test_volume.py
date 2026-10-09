"""
The room's sound level through PipeWire, against a fake pw-dump and wpctl in the
shape PiMeet-3 produces (spec 2026-10-08 sound and camera, section 4.2).
"""

import json
import logging

import pytest

from croom.devices import volume as volume_module
from croom.devices.errors import DeviceUnavailable
from croom.devices.volume import RoomVolume, run_command

MEETUP = "alsa_output.usb-Logitech_MeetUp-00.analog-stereo"
PW_DUMP = json.dumps([
    {"id": 36, "type": "PipeWire:Interface:Metadata", "version": 3, "props": {"metadata.name": "default"},
     "metadata": [{"subject": 0, "key": "default.audio.sink", "type": "Spa:String:JSON", "value": {"name": MEETUP}},
                  {"subject": 0, "key": "default.audio.source", "type": "Spa:String:JSON",
                   "value": {"name": "alsa_input.usb-Logitech_MeetUp-00.analog-stereo"}}]},
    {"id": 35, "type": "PipeWire:Interface:Node", "version": 3,
     "info": {"props": {"media.class": "Audio/Sink", "node.name": "alsa_output.platform-fef00700.hdmi.hdmi-stereo",
                        "node.description": "Built-in Audio Digital Stereo (HDMI)"}}},
    {"id": 57, "type": "PipeWire:Interface:Node", "version": 3,
     "info": {"props": {"media.class": "Audio/Sink", "node.name": MEETUP,
                        "node.description": "Logitech MeetUp Speakerphone Analog Stereo"}}},
    {"id": 58, "type": "PipeWire:Interface:Node", "version": 3,
     "info": {"props": {"media.class": "Audio/Source", "node.name": "alsa_input.usb-Logitech_MeetUp-00.analog-stereo",
                        "node.description": "Logitech MeetUp Speakerphone Analog Stereo"}}},
])
NO_SINKS = json.dumps([{"id": 36, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"}, "metadata": []}])


class FakeRunner:
    def __init__(self, dump=PW_DUMP, volume="Volume: 0.40\n"):
        self.dump, self.volume, self.calls, self.fail = dump, volume, [], {}

    async def __call__(self, args):
        self.calls.append(args)
        if args[0] in self.fail:
            return 1, self.fail[args[0]]
        if args == ["pw-dump"]:
            return 0, self.dump
        if args[:2] == ["wpctl", "get-volume"]:
            return 0, self.volume
        return 0, ""


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def volume_for(preference="auto", **runner_args):
    runner = FakeRunner(**runner_args)
    clock = FakeClock()
    return RoomVolume(preference, runner=runner, clock=clock), runner, clock


async def test_default_sink_is_the_meetup_with_its_level():
    volume, runner, _ = volume_for()
    state = await volume.refresh()
    assert state == {"available": True, "device": "Logitech MeetUp Speakerphone Analog Stereo", "level": 40, "muted": False, "reason": None}
    assert runner.calls == [["pw-dump"], ["wpctl", "get-volume", "57"]]


async def test_a_preference_picks_the_sink_whose_name_contains_it():
    volume, runner, _ = volume_for("hdmi")
    state = await volume.refresh()
    assert state["device"] == "Built-in Audio Digital Stereo (HDMI)" and runner.calls[-1] == ["wpctl", "get-volume", "35"]


async def test_no_matching_sink_is_unavailable_with_the_reason():
    volume, _, _ = volume_for("Jabra")
    state = await volume.refresh()
    assert state["available"] is False and state["reason"] == "no sink matches 'Jabra'"
    with pytest.raises(DeviceUnavailable):
        await volume.set_level(50)


async def test_no_sinks_at_all():
    volume, _, _ = volume_for(dump=NO_SINKS)
    assert (await volume.refresh())["reason"] == "no audio sink"


async def test_muted_is_read_from_wpctl():
    volume, _, _ = volume_for(volume="Volume: 0.40 [MUTED]\n")
    assert (await volume.refresh())["muted"] is True


async def test_levels_above_one_show_as_one_hundred():
    volume, _, _ = volume_for(volume="Volume: 1.30\n")
    assert (await volume.refresh())["level"] == 100


async def test_set_level_clamps_and_calls_wpctl():
    volume, runner, _ = volume_for()
    state = await volume.set_level(150)
    assert state["level"] == 100 and runner.calls[-1] == ["wpctl", "set-volume", "57", "1.00"]
    state = await volume.set_level(-3)
    assert state["level"] == 0 and runner.calls[-1] == ["wpctl", "set-volume", "57", "0.00"]
    await volume.set_level(55)
    assert runner.calls[-1] == ["wpctl", "set-volume", "57", "0.55"]


async def test_step_moves_from_the_level_wpctl_reports():
    volume, runner, _ = volume_for()
    assert (await volume.step(5))["level"] == 45 and runner.calls[-1] == ["wpctl", "set-volume", "57", "0.45"]
    runner.volume = "Volume: 0.45\n"
    assert (await volume.step(-50))["level"] == 0


async def test_mute_and_unmute():
    volume, runner, _ = volume_for()
    assert (await volume.set_muted(True))["muted"] is True and runner.calls[-1] == ["wpctl", "set-mute", "57", "1"]
    assert (await volume.set_muted(False))["muted"] is False and runner.calls[-1] == ["wpctl", "set-mute", "57", "0"]


async def test_the_state_is_cached_for_three_seconds_and_refreshed_after_a_change():
    volume, runner, clock = volume_for()
    await volume.refresh()
    await volume.refresh()
    assert runner.calls.count(["pw-dump"]) == 1
    clock.now += 3.1
    await volume.refresh()
    assert runner.calls.count(["pw-dump"]) == 2
    runner.volume = "Volume: 0.80\n"
    await volume.set_level(80)
    assert (await volume.refresh())["level"] == 80


async def test_a_failing_command_makes_it_unavailable_and_warns_once(caplog):
    volume, runner, clock = volume_for()
    runner.fail["pw-dump"] = "connection refused"
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            clock.now += 5
            state = await volume.refresh()
    assert state["available"] is False and "pw-dump failed: connection refused" in state["reason"]
    assert sum("pw-dump failed" in r.getMessage() for r in caplog.records) == 1
    del runner.fail["pw-dump"]
    clock.now += 5
    assert (await volume.refresh())["available"] is True


async def test_run_command_reports_a_timeout_and_a_missing_command(monkeypatch):
    monkeypatch.setattr(volume_module, "COMMAND_TIMEOUT_S", 0.2)
    code, text = await run_command(["sleep", "5"])
    assert code == 124 and "timed out" in text
    code, text = await run_command(["no-such-command-crystal-meet"])
    assert code == 127
    code, text = await run_command(["echo", "hello"])
    assert (code, text.strip()) == (0, "hello")
