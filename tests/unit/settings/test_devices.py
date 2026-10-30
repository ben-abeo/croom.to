"""The Devices page's choices (spec 2026-10-29, section 3): the speakers and microphones in pw-dump, the cameras at /dev/video*, each with
Automatic, and the config's own value kept in its list when the room does not have that device now. pw-dump and the video nodes are fakes."""

import json

from croom.settings.devices import find_devices, offer
from croom.settings.schema import BY_PATH, validate


def node(id_, media_class, name, description):
    return {"id": id_, "type": "PipeWire:Interface:Node",
            "info": {"props": {"media.class": media_class, "node.name": name, "node.description": description}}}


PW_DUMP = json.dumps([
    node(50, "Audio/Sink", "alsa_output.usb-046d_Logitech_MeetUp-00.analog-stereo", "Logitech MeetUp Analog Stereo"),
    node(51, "Audio/Sink", "alsa_output.platform-hdmi.hdmi-stereo", "Built-in HDMI"),
    node(60, "Audio/Source", "alsa_input.usb-046d_Logitech_MeetUp-00.analog-stereo", "Logitech MeetUp Mic"),
    node(70, "Video/Source", "v4l2_input.usb-cam", "A camera PipeWire sees"),
    {"id": 30, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
     "metadata": [{"key": "default.audio.sink", "value": {"name": "alsa_output.platform-hdmi.hdmi-stereo"}}]},
])


class Runner:
    def __init__(self, code=0, out=PW_DUMP):
        self.code, self.out, self.calls = code, out, []

    async def __call__(self, args):
        self.calls.append(args)
        return self.code, self.out


class Cameras:
    """/dev/video*: the MeetUp's capture node and its metadata node, and the Pi's decoder."""

    def __init__(self, names=None):
        self.names = names if names is not None else {"/dev/video0": "Logitech MeetUp", "/dev/video1": None, "/dev/video19": None}

    def nodes(self):
        return list(self.names)

    def camera_name(self, path):
        return self.names[path]


async def test_the_room_s_speakers_microphones_and_cameras_are_found():
    runner = Runner()
    found = await find_devices(runner, Cameras())
    assert runner.calls == [["pw-dump"]]
    assert found == {
        "audio.output_device": [("alsa_output.usb-046d_Logitech_MeetUp-00.analog-stereo", "Logitech MeetUp Analog Stereo"),
                                ("alsa_output.platform-hdmi.hdmi-stereo", "Built-in HDMI")],
        "audio.input_device": [("alsa_input.usb-046d_Logitech_MeetUp-00.analog-stereo", "Logitech MeetUp Mic")],
        "video.device": [("/dev/video0", "Logitech MeetUp (/dev/video0)")],
    }


async def test_a_listing_that_fails_leaves_its_list_empty():
    found = await find_devices(Runner(code=1, out="pw-dump: no PipeWire"), Cameras())
    assert found["audio.output_device"] == [] and found["audio.input_device"] == [] and found["video.device"]
    found = await find_devices(Runner(out="not json"), Cameras({}))
    assert found == {"audio.output_device": [], "audio.input_device": [], "video.device": []}

    class Broken(Cameras):
        def nodes(self):
            raise OSError("no /dev")

    assert (await find_devices(Runner(), Broken()))["video.device"] == []


def fields_of_devices():
    return [BY_PATH[path].describe() for path in ("audio.output_device", "audio.input_device", "video.device", "video.ptz_travel_seconds")]


async def test_each_device_is_offered_with_automatic_first_and_the_config_value_kept():
    found = await find_devices(Runner(), Cameras())
    values = {"audio.output_device": "auto", "audio.input_device": "MeetUp", "video.device": "/dev/video4", "video.ptz_travel_seconds": 6}
    fields = offer(fields_of_devices(), values, found)
    speaker, mic, camera, travel = fields
    assert [c["value"] for c in speaker["choices"]] == ["auto", "alsa_output.usb-046d_Logitech_MeetUp-00.analog-stereo",
                                                        "alsa_output.platform-hdmi.hdmi-stereo"]
    assert speaker["choices"][0] == {"value": "auto", "label": "Automatic"}
    assert mic["choices"][-1] == {"value": "MeetUp", "label": "MeetUp (now Logitech MeetUp Mic)"}    # written by hand: a part of a name
    values["audio.input_device"] = "Jabra"
    assert offer(fields_of_devices(), values, found)[1]["choices"][-1] == {"value": "Jabra", "label": "Jabra (not connected now)"}
    assert camera["choices"][-1] == {"value": "/dev/video4", "label": "/dev/video4 (not connected now)"}
    assert "choices" not in travel
    assert offer(fields_of_devices(), values, None)[0]["choices"] == [{"value": "auto", "label": "Automatic"}]


def test_a_device_setting_takes_any_device_name_and_a_blank_is_automatic():
    # The helper checks a config with no way to see the room's devices: any one line of text is a device the room may have
    assert validate("audio.output_device", "alsa_output.usb-046d_Logitech_MeetUp-00.analog-stereo").startswith("alsa_output")
    assert validate("video.device", "/dev/video2") == "/dev/video2"
    assert validate("audio.input_device", "  ") == "auto"
    assert BY_PATH["video.device"].describe()["kind"] == "choice"
