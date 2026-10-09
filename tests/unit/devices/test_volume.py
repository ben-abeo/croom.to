"""
The room's sound level through PipeWire, against a fake pw-dump and wpctl in the
shape PiMeet-3 produces (spec 2026-10-08 sound and camera, section 4.2).
"""

import asyncio
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
        for key in (args[0], " ".join(args[:2])):  # a command fails whole, or one subcommand of it
            if key in self.fail:
                return 1, self.fail[key]
        if args == ["pw-dump"]:
            return 0, self.dump
        if args[:2] == ["wpctl", "get-volume"]:
            return 0, self.volume
        return 0, ""


class SlowRunner(FakeRunner):
    """Like the real thing: each command takes a moment, and a set-volume sticks."""

    async def __call__(self, args):
        await asyncio.sleep(0.01)
        code, text = await super().__call__(args)
        if code == 0 and args[:2] == ["wpctl", "set-volume"]:
            self.volume = f"Volume: {args[3]}\n"
        return code, text


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def volume_for(preference="auto", runner_class=FakeRunner, **runner_args):
    runner = runner_class(**runner_args)
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


async def test_a_change_drops_the_cache_so_the_next_refresh_reads_the_speaker_back():
    volume, runner, _ = volume_for()
    await volume.refresh()
    runner.volume = "Volume: 0.70\n"  # what wpctl reports is what the speaker did, whatever it was asked
    assert (await volume.set_level(80))["level"] == 80
    assert (await volume.refresh())["level"] == 70
    assert (await volume.set_muted(True))["muted"] is True
    assert (await volume.refresh())["muted"] is False


async def test_the_cache_still_holds_at_2_9_seconds_and_has_run_out_by_3_1():
    volume, runner, clock = volume_for()
    await volume.refresh()
    clock.now += 2.9
    await volume.refresh()
    assert runner.calls.count(["pw-dump"]) == 1
    clock.now += 0.2
    await volume.refresh()
    assert runner.calls.count(["pw-dump"]) == 2


@pytest.mark.parametrize("wanted", ["MEETUP", "SPEAKERPHONE", "LOGITECH_MEETUP"])
async def test_a_preference_matches_the_description_or_the_name_whatever_the_case(wanted):
    # MEETUP is in both; SPEAKERPHONE only in the description; LOGITECH_MEETUP only in the node name
    volume, _, _ = volume_for(wanted)
    state = await volume.refresh()
    assert state["available"] is True and state["device"] == "Logitech MeetUp Speakerphone Analog Stereo"


async def test_step_reads_the_level_first_even_inside_the_cache_window():
    volume, runner, _ = volume_for()
    await volume.refresh()
    runner.volume = "Volume: 0.60\n"  # someone used the speaker's own buttons since the last look
    assert (await volume.step(5))["level"] == 65
    assert runner.calls[-1] == ["wpctl", "set-volume", "57", "0.65"]


async def test_a_failing_wpctl_set_command_raises_and_makes_it_unavailable():
    volume, runner, _ = volume_for()
    runner.fail["wpctl set-volume"] = "no such node"
    with pytest.raises(DeviceUnavailable, match="wpctl set-volume failed: no such node"):
        await volume.set_level(50)
    assert runner.calls[-1] == ["wpctl", "set-volume", "57", "0.50"]
    assert volume.state()["available"] is False

    volume, runner, _ = volume_for()
    runner.fail["wpctl set-mute"] = "no such node"
    with pytest.raises(DeviceUnavailable, match="wpctl set-mute failed: no such node"):
        await volume.set_muted(True)
    assert runner.calls[-1] == ["wpctl", "set-mute", "57", "1"]
    assert volume.state()["available"] is False


def sink_node(node_id, name, description=None, nick=None):
    props = {"media.class": "Audio/Sink", "node.name": name}
    if description:
        props["node.description"] = description
    if nick:
        props["node.nick"] = nick
    return {"id": node_id, "type": "PipeWire:Interface:Node", "version": 3, "info": {"props": props}}


SINKS_NAMED_THREE_WAYS = json.dumps([
    sink_node(1, "alpha", description="Alpha described", nick="Alpha nick"),
    sink_node(2, "bravo", nick="Bravo nick"),
    sink_node(3, "charlie"),
])


@pytest.mark.parametrize("wanted, device", [
    ("alpha", "Alpha described"), ("bravo", "Bravo nick"), ("charlie", "charlie")])
async def test_the_device_is_named_by_description_then_nick_then_node_name(wanted, device):
    volume, _, _ = volume_for(wanted, dump=SINKS_NAMED_THREE_WAYS)
    assert (await volume.refresh())["device"] == device


async def test_a_second_outage_with_the_same_reason_warns_again(caplog):
    volume, runner, clock = volume_for()

    async def outage_then_recovery():
        runner.fail["pw-dump"] = "connection refused"
        for _ in range(2):
            clock.now += 5
            assert (await volume.refresh())["available"] is False
        del runner.fail["pw-dump"]
        clock.now += 5
        assert (await volume.refresh())["available"] is True

    with caplog.at_level(logging.WARNING):
        await outage_then_recovery()
        await outage_then_recovery()
    assert sum("pw-dump failed" in r.getMessage() for r in caplog.records) == 2


async def test_two_overlapping_steps_both_count():
    volume, runner, _ = volume_for(runner_class=SlowRunner)
    await asyncio.gather(volume.step(5), volume.step(5))
    sets = [call for call in runner.calls if call[:2] == ["wpctl", "set-volume"]]
    assert sets[-1] == ["wpctl", "set-volume", "57", "0.50"]
    assert volume.state()["level"] == 50


@pytest.mark.parametrize("call", ["refresh", "set_level", "set_muted"])
async def test_a_caller_arriving_during_a_reprobe_waits_for_it(call):
    args = {"refresh": (), "set_level": (60,), "set_muted": (True,)}[call]
    volume, runner, clock = volume_for(runner_class=SlowRunner)
    runner.fail["pw-dump"] = "connection refused"
    assert (await volume.refresh())["available"] is False
    del runner.fail["pw-dump"]  # PipeWire is back
    clock.now += 5  # and the cache has run out
    results = await asyncio.gather(getattr(volume, call)(*args), getattr(volume, call)(*args), return_exceptions=True)
    assert all(isinstance(result, dict) and result["available"] for result in results), results
