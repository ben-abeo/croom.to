"""The settings schema (spec 2026-10-29, section 4): what each kind of setting takes, the whole-config check the root helper runs, and
the ConfigEditor that reads a page's values from config.yaml and saves its changes through the helper."""

import asyncio
import zoneinfo

import pytest
import yaml

from croom.admin.client import AdminError, AdminUnavailable
from croom.admin.jobs import check_config_yaml
from croom.core.config import Config
from croom.settings.config_edit import ConfigEditor
from croom.settings.schema import (BY_PATH, KINDS, PAGES, SCHEMA, SettingError, check_config, config_errors, settings_of, time_zones,
                                   validate)


# --- the schema itself


def test_every_setting_is_a_key_the_config_loads_on_a_page_of_a_known_kind():
    config = Config()
    for s in SCHEMA:
        node = config
        for part in s.path.split("."):
            assert hasattr(node, part), s.path
            node = getattr(node, part)
        assert s.page in PAGES and s.kind in KINDS, s.path
        assert s.label and not s.label.endswith(".")


def test_the_schema_covers_every_setting_the_pages_edit():
    paths = set(BY_PATH)
    assert {"room.name", "room.location", "room.company", "room.timezone"} <= paths
    assert {"meeting.platforms", "meeting.camera_default_on", "meeting.mic_default_on", "meeting.kiosk", "meeting.dialogs.acknowledge",
            "meeting.dialogs.ask_everything", "meeting.controls.reactions"} <= paths
    assert {"calendar.google_calendar_id", "calendar.booking_start", "calendar.booking_end"} <= paths
    assert BY_PATH["calendar.google_credentials_path"].kind == "credential"
    assert {"audio.input_device", "audio.output_device", "video.device", "video.ptz_travel_seconds", "control.camera_presets"} <= paths
    assert {f"laptop.{name}" for name in ("enabled", "gpio_pin", "active_low", "press_ms", "usb_id", "switch_wait_s")} <= paths
    assert {f"ai.{name}" for name in ("enabled", "backend", "person_detection", "noise_reduction", "echo_cancellation", "auto_framing",
                                      "occupancy_counting", "speaker_detection", "hand_raise_detection", "privacy_mode")} <= paths
    assert {"dashboard.enabled", "dashboard.url", "dashboard.enrollment_token"} <= paths
    assert BY_PATH["dashboard.enrollment_token"].kind == "secret"
    assert not any(path.startswith("control.") and path != "control.camera_presets" for path in paths)   # never the room page's own


def test_live_settings_are_the_booking_hours_and_the_camera_preset_names():
    assert {s.path for s in SCHEMA if s.live} == {"calendar.booking_start", "calendar.booking_end", "control.camera_presets"}


def test_the_room_page_has_name_location_company_and_the_systems_time_zones():
    assert [s.path for s in settings_of("room")] == ["room.name", "room.location", "room.company", "room.timezone"]
    zones = [value for value, _ in time_zones()]
    assert zones == sorted(zoneinfo.available_timezones()) and "Europe/London" in zones
    entry = BY_PATH["room.timezone"].describe()
    assert entry["kind"] == "choice" and {"value": "America/New_York", "label": "America/New York"} in entry["choices"]


def test_a_described_setting_carries_what_the_form_needs():
    assert BY_PATH["laptop.gpio_pin"].describe() == {"path": "laptop.gpio_pin", "label": "GPIO pin", "kind": "number", "live": False,
                                                     "help": "BCM numbering: 17 is header pin 11", "min": 0, "max": 27, "integer": True}
    names = BY_PATH["control.camera_presets"].describe()
    assert names["live"] is True and names["max_items"] == 3 and names["required"] is True


# --- validate, kind by kind


def test_text_is_trimmed_one_line_and_bounded():
    assert validate("room.name", "  Conference A ") == "Conference A"
    assert validate("room.location", "") == ""
    for value, words in ((" ", "can't be blank"), ("A\nB", "one line"), ("x" * 81, "at most 80"), (5, "must be text")):
        with pytest.raises(SettingError, match=words):
            validate("room.name", value)


def test_text_with_a_pattern():
    assert validate("laptop.usb_id", "046d:0866") == "046d:0866"
    assert validate("dashboard.url", "https://dash.example.com/rooms") == "https://dash.example.com/rooms"
    assert validate("dashboard.url", "") == ""
    with pytest.raises(SettingError, match="lsusb"):
        validate("laptop.usb_id", "046d-0866")
    with pytest.raises(SettingError, match="http"):
        validate("dashboard.url", "dash.example.com")


def test_number_range_and_whole_numbers():
    assert validate("laptop.gpio_pin", 17) == 17 and validate("laptop.gpio_pin", 17.0) == 17
    assert validate("video.ptz_travel_seconds", 8) == 8 and validate("video.ptz_travel_seconds", 7.5) == 7.5
    for value, words in ((28, "from 0 to 27"), (-1, "from 0 to 27"), (17.5, "whole number"), ("17", "must be a number"), (True, "must be a number"),
                         (float("nan"), "must be a number")):
        with pytest.raises(SettingError, match=words):
            validate("laptop.gpio_pin", value)


def test_switch_takes_true_or_false_and_the_words_a_file_may_use():
    assert validate("meeting.kiosk", True) is True and validate("meeting.kiosk", False) is False
    assert validate("meeting.kiosk", "yes") is True and validate("meeting.kiosk", "Off") is False
    for value in (1, "maybe", None):
        with pytest.raises(SettingError, match="on or off"):
            validate("meeting.kiosk", value)


def test_choice_is_one_of_its_choices():
    assert validate("room.timezone", "Europe/London") == "Europe/London"
    assert validate("ai.backend", "hailo") == "hailo"
    for value in ("Mars/Olympus", "", None):
        with pytest.raises(SettingError, match="choose one"):
            validate("room.timezone", value)


def test_list_items_are_text_each_once_blank_lines_dropped():
    assert validate("meeting.dialogs.acknowledge", ["is being recorded", " ", "AI Companion", "is being recorded"]) == \
        ["is being recorded", "AI Companion"]
    assert validate("meeting.controls.reactions", "Clap") == ["Clap"]          # a bare word in a file is one item
    assert validate("meeting.platforms", ["zoom", "teams"]) == ["zoom", "teams"]
    for path, value, words in (("meeting.platforms", ["skype"], "not one of the choices"), ("meeting.platforms", [], "at least one"),
                               ("control.camera_presets", ["A", "B", "C", "D"], "at most 3"), ("control.camera_presets", ["x" * 25], "at most 24"),
                               ("meeting.controls.reactions", {"a": 1}, "must be a list"), ("meeting.controls.reactions", [3], "must be text")):
        with pytest.raises(SettingError, match=words):
            validate(path, value)


def test_time_is_hh_mm_on_the_quarter_hour():
    assert validate("calendar.booking_start", "8:00") == "08:00"
    assert validate("calendar.booking_end", "17:45") == "17:45"
    assert validate("calendar.booking_end", 1080) == "18:00"       # YAML 1.1 reads an unquoted 18:00 as 1080
    for value, words in (("8:10", "quarter hour"), ("24:00", "HH:MM"), ("noon", "HH:MM"), (None, "HH:MM")):
        with pytest.raises(SettingError, match=words):
            validate("calendar.booking_start", value)


def test_secret_and_credential():
    assert validate("dashboard.enrollment_token", " abc ") == "abc"
    assert validate("calendar.google_credentials_path", "/etc/croom/google-service-account.json").endswith(".json")
    with pytest.raises(SettingError, match="full path"):
        validate("calendar.google_credentials_path", "google.json")


def test_an_unknown_setting_is_refused():
    with pytest.raises(SettingError, match="no setting"):
        validate("control.port", 9000)


# --- the whole config


def test_check_config_passes_a_good_config_and_the_defaults():
    check_config({})
    check_config(yaml.safe_load("room:\n  name: A\ncalendar:\n  booking_start: 9:00\n  booking_end: 17:00\nmeeting:\n  dialogs:\n"))


def test_check_config_names_every_setting_that_is_wrong():
    data = {"room": {"name": ""}, "laptop": {"gpio_pin": 40}, "meeting": {"platforms": ["skype"]}}
    with pytest.raises(SettingError) as raised:
        check_config(data)
    assert set(raised.value.errors) == {"room.name", "laptop.gpio_pin", "meeting.platforms"}
    assert "and 2 more" in str(raised.value)


def test_check_config_wants_the_booking_hours_in_order():
    assert config_errors({"calendar": {"booking_start": "18:00"}}) == {"calendar.booking_end": "Bookings until must be after Bookings from"}
    assert config_errors({"calendar": {"booking_end": "08:00"}}) == {"calendar.booking_end": "Bookings until must be after Bookings from"}


def test_check_config_refuses_what_would_lock_settings_out_of_the_room():
    assert "control.enabled" in config_errors({"control": {"enabled": False}})
    assert "control.enabled" in config_errors({"control": {"enabled": "no"}})
    assert config_errors({"control": {"enabled": True}}) == {}
    current = {"control": {"port": 8099}}
    assert set(config_errors({"control": {"port": 8098}}, current)) == {"control.port"}
    assert set(config_errors({}, current)) == {"control.port"}                       # leaving it out is 8080: a change
    assert set(config_errors({"control": {"port": 8099, "host": "127.0.0.1"}}, current)) == {"control.host"}
    assert config_errors({"control": {"port": 8099, "screensaver": "quiet"}}, current) == {}
    assert config_errors({"control": {"port": 9000}}) == {}                         # nothing on disk: nothing to move from


def test_the_helpers_check_runs_the_schema():
    with pytest.raises(Exception, match="from 0 to 27"):
        check_config_yaml("laptop:\n  gpio_pin: 99\n")
    with pytest.raises(Exception, match="can't be changed from Settings"):
        check_config_yaml("control:\n  port: 9000\n", {"control": {"port": 8080}})


# --- the ConfigEditor


class FakeAdmin:
    """The root helper's client: what was sent, and a refusal or no helper when a test asks for one."""

    def __init__(self, path=None):
        self.calls = []
        self.path = path          # write_config writes here, as the helper would
        self.refuse = None
        self.gone = False

    def available(self):
        return not self.gone

    async def call(self, job, **args):
        if self.gone:
            raise AdminUnavailable("Not available on this device")
        if self.refuse:
            raise AdminError(self.refuse)
        self.calls.append((job, args))
        if job == "write_config":
            check_config_yaml(args["yaml"])
            await asyncio.sleep(0)   # the helper takes its time: another save may come meanwhile
            if self.path is not None:
                self.path.write_text(args["yaml"])
        return {"ok": True}


HAND_WRITTEN = """\
# The room's settings
room:
  name: Conference A
  timezone: America/Chicago
calendar:
  google_calendar_id: c_1@resource.calendar.google.com
  booking_end: 17:00
tv:
  enabled: true
  host: 10.0.0.40
dashboard:
  enrollment_token: s3cret
"""


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(HAND_WRITTEN)
    return path


@pytest.fixture
def admin(config_file):
    return FakeAdmin(config_file)


def test_values_come_from_the_file_with_the_defaults_for_what_it_leaves_out(config_file, admin):
    editor = ConfigEditor(config_file, admin)
    assert editor.values("room") == {"room.name": "Conference A", "room.location": "", "room.company": "Crystal Practice Management",
                                     "room.timezone": "America/Chicago"}
    calendar = editor.values("calendar")
    assert calendar["calendar.booking_start"] == "08:00" and calendar["calendar.booking_end"] == "17:00"   # 17:00 unquoted is 1020 to YAML


def test_a_secret_is_never_read_back_only_whether_it_is_set(config_file, admin):
    editor = ConfigEditor(config_file, admin)
    assert editor.values("ai")["dashboard.enrollment_token"] == ""
    token = next(f for f in editor.fields("ai") if f["path"] == "dashboard.enrollment_token")
    assert token["set"] is True and "s3cret" not in repr(editor.fields("ai")) + repr(editor.values("ai"))


def test_no_config_file_is_the_defaults(tmp_path, admin):
    editor = ConfigEditor(tmp_path / "none.yaml", admin)
    assert editor.values("room")["room.name"] == "Conference Room"


def test_an_unknown_page_is_a_key_error(config_file, admin):
    with pytest.raises(KeyError):
        ConfigEditor(config_file, admin).values("network")


async def test_save_sends_the_whole_file_with_the_change_and_keeps_what_no_page_shows(config_file, admin):
    editor = ConfigEditor(config_file, admin)
    answer = await editor.save("room", {"room.name": "  Boardroom ", "room.timezone": "America/Chicago"})
    assert answer == {"saved": ["room.name"], "restart": True, "pending_restart": True}   # the time zone did not change
    [(job, args)] = admin.calls
    assert job == "write_config"
    sent = yaml.safe_load(args["yaml"])
    assert sent["room"] == {"name": "Boardroom", "timezone": "America/Chicago"}
    assert sent["tv"] == {"enabled": True, "host": "10.0.0.40"} and sent["dashboard"] == {"enrollment_token": "s3cret"}
    assert sent["calendar"]["booking_end"] == 1020          # untouched, as the file had it
    assert list(sent) == ["room", "calendar", "tv", "dashboard"]    # in the file's order


async def test_a_save_that_changes_nothing_writes_nothing(config_file, admin):
    editor = ConfigEditor(config_file, admin)
    assert await editor.save("room", {"room.name": "Conference A"}) == {"saved": [], "restart": False, "pending_restart": False}
    assert admin.calls == []


async def test_a_refused_value_saves_nothing_and_says_why_by_path(config_file, admin):
    editor = ConfigEditor(config_file, admin)
    with pytest.raises(SettingError) as raised:
        await editor.save("room", {"room.name": "", "room.location": "Floor 2", "laptop.gpio_pin": 3})
    assert raised.value.errors == {"room.name": "Name can't be blank", "laptop.gpio_pin": "This is not a setting of this page"}
    assert admin.calls == []


async def test_a_change_that_breaks_the_whole_is_refused_under_the_setting_it_breaks(config_file, admin):
    editor = ConfigEditor(config_file, admin)
    with pytest.raises(SettingError) as raised:
        await editor.save("calendar", {"calendar.booking_start": "18:00"})
    assert raised.value.errors == {"calendar.booking_end": "Bookings until must be after Bookings from"}
    assert admin.calls == []


async def test_live_settings_go_to_their_hooks_at_once_and_need_no_restart(config_file, admin):
    editor = ConfigEditor(config_file, admin)
    hours, names = [], []
    editor.on_change(("calendar.booking_start", "calendar.booking_end"), hours.append)
    editor.on_change(("control.camera_presets",), names.append)
    answer = await editor.save("calendar", {"calendar.booking_start": "07:30", "calendar.booking_end": "19:00"})
    assert answer == {"saved": ["calendar.booking_start", "calendar.booking_end"], "restart": False, "pending_restart": False}
    assert hours == [{"calendar.booking_start": "07:30", "calendar.booking_end": "19:00"}] and names == []
    await editor.save("devices", {"control.camera_presets": ["Wide", "Door"]})
    assert names == [{"control.camera_presets": ["Wide", "Door"]}]
    assert yaml.safe_load(config_file.read_text())["control"]["camera_presets"] == ["Wide", "Door"]


async def test_a_hook_that_fails_leaves_the_change_for_a_restart(config_file, admin):
    editor = ConfigEditor(config_file, admin)

    def broken(values):
        raise ValueError("no")

    editor.on_change(("control.camera_presets",), broken)
    answer = await editor.save("devices", {"control.camera_presets": ["Wide"]})
    assert answer["restart"] is True and admin.calls


async def test_a_restart_waiting_is_remembered_by_later_saves(config_file, admin):
    editor = ConfigEditor(config_file, admin)
    await editor.save("room", {"room.location": "Floor 2"})
    answer = await editor.save("calendar", {"calendar.booking_start": "09:00"})
    assert answer["restart"] is False and answer["pending_restart"] is True and editor.pending_restart


async def test_a_secret_left_blank_stays_as_it_is(config_file, admin):
    editor = ConfigEditor(config_file, admin)
    assert (await editor.save("ai", {"dashboard.enrollment_token": "", "dashboard.enabled": False}))["saved"] == ["dashboard.enabled"]
    assert yaml.safe_load(config_file.read_text())["dashboard"] == {"enrollment_token": "s3cret", "enabled": False}


async def test_the_helpers_refusal_and_absence_pass_through(config_file, admin):
    editor = ConfigEditor(config_file, admin)
    admin.refuse = "The room is restarting: try again in a moment"
    with pytest.raises(AdminError, match="restarting"):
        await editor.save("room", {"room.location": "Floor 2"})
    admin.refuse, admin.gone = None, True
    with pytest.raises(AdminUnavailable):
        await editor.save("room", {"room.location": "Floor 2"})
    assert not editor.pending_restart


async def test_a_file_that_is_not_yaml_is_not_overwritten(config_file, admin):
    config_file.write_text("room: [unclosed\n")
    editor = ConfigEditor(config_file, admin)
    with pytest.raises(SettingError, match="not valid YAML"):
        await editor.save("room", {"room.name": "B"})
    assert admin.calls == []


async def test_saves_are_one_at_a_time(config_file, admin):
    editor = ConfigEditor(config_file, admin)
    await asyncio.gather(editor.save("room", {"room.location": "Floor 2"}), editor.save("room", {"room.company": "Acme"}))
    data = yaml.safe_load(config_file.read_text())
    assert data["room"]["location"] == "Floor 2" and data["room"]["company"] == "Acme"


# --- every value the room accepts, the schema accepts (a hand-written one must never block every save)


def test_the_ai_backends_offered_are_auto_and_the_name_of_each_backend():
    # AIService uses the backend whose `name` is ai.backend: those names (and auto) are the choices, and no other
    import importlib
    names = set()
    for module, cls in (("hailo", "HailoBackend"), ("coral", "CoralBackend"), ("openvino", "OpenVINOBackend"), ("nvidia", "NVIDIABackend"),
                        ("onnx_cpu", "ONNXCPUBackend")):
        backend = getattr(importlib.import_module(f"croom.ai.backends.{module}"), cls)
        names.add(backend.name.fget(object.__new__(backend)))   # the name is a constant: no need for the hardware __init__ looks for
    assert names == {"hailo", "coral", "intel", "nvidia", "cpu"}
    offered = {c["value"] for c in BY_PATH["ai.backend"].describe()["choices"]}
    assert offered == {"auto"} | names
    for name in offered:
        assert validate("ai.backend", name) == name
    for name in ("openvino", "onnx"):
        with pytest.raises(SettingError):
            validate("ai.backend", name)


def test_every_meeting_platform_the_room_builds_is_a_choice():
    from croom.core.config import DEFAULT_PLATFORMS
    from croom.meeting.providers import get_all_providers
    assert validate("meeting.platforms", list(get_all_providers())) == list(get_all_providers())
    assert validate("meeting.platforms", DEFAULT_PLATFORMS) == DEFAULT_PLATFORMS


def test_every_default_of_the_config_passes_its_setting():
    from croom.settings.config_edit import default_of
    for s in SCHEMA:
        validate(s.path, default_of(s.path))
    assert validate("room.timezone", "UTC") == "UTC"
