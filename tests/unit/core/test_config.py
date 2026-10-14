"""
Tests for croom.core.config module.
"""

import logging
import os
import tempfile
from pathlib import Path

import pytest
import yaml

from croom.core.config import (
    Config,
    RoomConfig,
    MeetingConfig,
    CalendarConfig,
    AIConfig,
    AudioConfig,
    VideoConfig,
    DisplayConfig,
    DashboardConfig,
    UpdateConfig,
    SecurityConfig,
    load_config,
    get_config_path,
    CONFIG_PATHS,
    without_placeholder,
    ControlsConfig,
    DialogsConfig,
    DEFAULT_ACKNOWLEDGE,
    DEFAULT_REACTIONS,
)


class TestRoomConfig:
    """Tests for RoomConfig dataclass."""

    def test_default_values(self):
        """Test default room configuration values."""
        config = RoomConfig()
        assert config.name == "Conference Room"
        assert config.location == ""
        assert config.timezone == "UTC"

    def test_custom_values(self):
        """Test custom room configuration values."""
        config = RoomConfig(
            name="Board Room",
            location="Building A, Floor 3",
            timezone="America/New_York",
        )
        assert config.name == "Board Room"
        assert config.location == "Building A, Floor 3"
        assert config.timezone == "America/New_York"


class TestMeetingConfig:
    """Tests for MeetingConfig dataclass."""

    def test_default_platforms(self):
        """Test default meeting platforms."""
        config = MeetingConfig()
        assert "google_meet" in config.platforms
        assert "teams" in config.platforms
        assert "zoom" in config.platforms

    def test_default_settings(self):
        """Test default meeting settings."""
        config = MeetingConfig()
        assert config.default_platform == "auto"
        assert config.join_early_minutes == 1
        assert config.auto_leave is True
        assert config.camera_default_on is True
        assert config.mic_default_on is True


class TestAIConfig:
    """Tests for AIConfig dataclass."""

    def test_default_enabled(self):
        """Test AI is enabled by default."""
        config = AIConfig()
        assert config.enabled is True
        assert config.backend == "auto"

    def test_feature_toggles(self):
        """Test AI feature toggles."""
        config = AIConfig()
        assert config.person_detection is True
        assert config.noise_reduction is True
        assert config.auto_framing is True
        assert config.speaker_detection is False  # Requires accelerator
        assert config.hand_raise_detection is False  # Requires accelerator

    def test_privacy_mode_default(self):
        """Test privacy mode is disabled by default."""
        config = AIConfig()
        assert config.privacy_mode is False


class TestConfig:
    """Tests for main Config class."""

    def test_default_config(self):
        """Test default configuration creation."""
        config = Config()
        assert config.version == 2
        assert config.platform_type == "auto"
        assert isinstance(config.room, RoomConfig)
        assert isinstance(config.meeting, MeetingConfig)
        assert isinstance(config.ai, AIConfig)

    def test_from_dict(self, sample_config):
        """Test creating config from dictionary."""
        config = Config.from_dict(sample_config)
        assert config.room.name == "Test Conference Room"
        assert config.room.location == "Floor 2, Building A"
        assert config.meeting.join_early_minutes == 2
        assert config.ai.enabled is True

    def test_from_dict_partial(self):
        """Test creating config from partial dictionary."""
        partial_config = {
            "room": {"name": "Custom Room"},
            "ai": {"enabled": False},
        }
        config = Config.from_dict(partial_config)
        assert config.room.name == "Custom Room"
        assert config.ai.enabled is False
        # Other values should be defaults
        assert config.meeting.default_platform == "auto"

    def test_to_dict(self):
        """Test converting config to dictionary."""
        config = Config()
        config.room.name = "Test Room"
        data = config.to_dict()

        assert isinstance(data, dict)
        assert data["room"]["name"] == "Test Room"
        assert "meeting" in data
        assert "ai" in data
        assert "security" in data

    def test_roundtrip(self, sample_config):
        """Test config survives dict roundtrip."""
        config1 = Config.from_dict(sample_config)
        data = config1.to_dict()
        config2 = Config.from_dict(data)

        assert config1.room.name == config2.room.name
        assert config1.ai.enabled == config2.ai.enabled

    def test_save_config(self, temp_dir):
        """Test saving configuration to file."""
        config = Config()
        config.room.name = "Saved Room"
        config_path = temp_dir / "test_config.yaml"

        config.save(str(config_path))

        assert config_path.exists()
        with open(config_path) as f:
            saved_data = yaml.safe_load(f)
        assert saved_data["room"]["name"] == "Saved Room"

    def test_save_creates_directory(self, temp_dir):
        """Test save creates parent directories."""
        config = Config()
        config_path = temp_dir / "subdir" / "config.yaml"

        config.save(str(config_path))

        assert config_path.exists()


class TestLoadConfig:
    """Tests for load_config function."""

    def test_load_from_path(self, temp_config_file):
        """Test loading config from specific path."""
        config = load_config(str(temp_config_file))
        assert config.room.name == "Test Room"

    def test_load_nonexistent_returns_default(self):
        """Test loading from nonexistent path returns default config."""
        config = load_config("/nonexistent/path/config.yaml")
        assert config.room.name == "Conference Room"  # Default value

    def test_load_invalid_yaml(self, temp_dir):
        """Test loading invalid YAML returns default config."""
        invalid_config = temp_dir / "invalid.yaml"
        invalid_config.write_text("invalid: yaml: content: {{")

        config = load_config(str(invalid_config))
        # Should return default config on error
        assert isinstance(config, Config)

    def test_load_empty_file(self, temp_dir):
        """Test loading empty file returns default config."""
        empty_config = temp_dir / "empty.yaml"
        empty_config.write_text("")

        config = load_config(str(empty_config))
        assert isinstance(config, Config)


class TestGetConfigPath:
    """Tests for get_config_path function."""

    def test_returns_none_when_no_config(self, monkeypatch):
        """Test returns None when no config file exists."""
        # Ensure none of the default paths exist
        monkeypatch.setattr(
            "croom.core.config.CONFIG_PATHS",
            ["/nonexistent/path1", "/nonexistent/path2"],
        )
        assert get_config_path() is None

    def test_returns_existing_path(self, temp_config_file, monkeypatch):
        """Test returns path when config exists."""
        monkeypatch.setattr(
            "croom.core.config.CONFIG_PATHS",
            [str(temp_config_file)],
        )
        assert get_config_path() == str(temp_config_file)


class TestSecurityConfig:
    """Tests for SecurityConfig dataclass."""

    def test_default_values(self):
        """Test default security settings."""
        config = SecurityConfig()
        assert config.admin_pin == ""
        assert config.ssh_enabled is True
        assert config.require_encryption is True


class TestDashboardConfig:
    """Tests for DashboardConfig dataclass."""

    def test_default_values(self):
        """Test default dashboard settings."""
        config = DashboardConfig()
        assert config.enabled is True
        assert config.url == ""
        assert config.heartbeat_interval_seconds == 30
        assert config.metrics_interval_seconds == 60


class TestDataDir:
    """Tests for the data_dir key and Config.resolve_data_dir() (spec 4.5)."""

    def test_defaults_to_empty(self):
        assert Config().data_dir == ""

    def test_round_trips_through_dict(self):
        config = Config.from_dict({"data_dir": "/srv/croom-data"})
        assert config.data_dir == "/srv/croom-data"
        assert config.to_dict()["data_dir"] == "/srv/croom-data"
        assert Config.from_dict(config.to_dict()).data_dir == "/srv/croom-data"

    def test_explicit_dir_is_created_and_returned(self, tmp_path):
        target = tmp_path / "explicit" / "nested"
        config = Config(data_dir=str(target))
        assert config.resolve_data_dir() == target
        assert target.is_dir()

    def test_system_dir_wins_when_writable(self, tmp_path, monkeypatch):
        system_dir = tmp_path / "var-lib-croom"
        system_dir.mkdir()
        monkeypatch.setattr("croom.core.config.SYSTEM_DATA_DIR", system_dir)
        assert Config().resolve_data_dir() == system_dir

    def test_unwritable_system_dir_is_skipped(self, tmp_path, monkeypatch):
        system_dir = tmp_path / "var-lib-croom"
        system_dir.mkdir()
        monkeypatch.setattr("croom.core.config.SYSTEM_DATA_DIR", system_dir)
        monkeypatch.setattr("croom.core.config._writable_dir", lambda path: False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        assert Config().resolve_data_dir() == tmp_path / "xdg" / "croom"

    def test_falls_back_to_xdg_data_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr("croom.core.config.SYSTEM_DATA_DIR", tmp_path / "does-not-exist")
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        assert Config().resolve_data_dir() == tmp_path / "xdg" / "croom"
        assert (tmp_path / "xdg" / "croom").is_dir()

    def test_falls_back_to_home_local_share(self, tmp_path, monkeypatch):
        monkeypatch.setattr("croom.core.config.SYSTEM_DATA_DIR", tmp_path / "does-not-exist")
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert Config().resolve_data_dir() == tmp_path / ".local" / "share" / "croom"


class TestAutoToDefault:
    """auto_to_default() translates the config's device selector for the services."""

    def test_auto_and_empty_become_default(self):
        from croom.core.config import auto_to_default
        assert auto_to_default("auto") == "default"
        assert auto_to_default("") == "default"

    def test_explicit_device_passes_through(self):
        from croom.core.config import auto_to_default
        assert auto_to_default("hw:1,0") == "hw:1,0"


class TestControlConfig:
    """The control section configures the room control page (spec 4.1)."""

    def test_defaults(self):
        config = Config()
        assert config.control.enabled is True
        assert config.control.host == "0.0.0.0"
        assert config.control.port == 8080

    def test_round_trips_through_dict(self):
        config = Config.from_dict({"control": {"enabled": False, "host": "127.0.0.1", "port": 9090}})
        assert config.control.enabled is False
        assert config.control.host == "127.0.0.1"
        assert config.control.port == 9090
        assert config.to_dict()["control"] == {"enabled": False, "host": "127.0.0.1", "port": 9090, "screensaver": "info",
                                               "camera_presets": ["Wide", "Table", "Whiteboard"]}
        assert Config.from_dict(config.to_dict()).control.port == 9090


class TestRoomDeviceConfig:
    """Volume and camera settings (spec 2026-10-08 sound and camera, section 4.5)."""

    def test_defaults(self):
        config = Config()
        assert config.audio.output_device == "auto"
        assert config.video.device == "auto"
        assert config.video.ptz_travel_seconds == 8.0
        assert config.control.camera_presets == ["Wide", "Table", "Whiteboard"]

    def test_round_trips_through_dict(self):
        config = Config.from_dict({"audio": {"output_device": "HDMI"},
                                   "video": {"device": "/dev/video2", "ptz_travel_seconds": 6.5},
                                   "control": {"camera_presets": ["Room", "Desk", "Board"]}})
        again = Config.from_dict(config.to_dict())
        assert again.audio.output_device == "HDMI"
        assert again.video.device == "/dev/video2" and again.video.ptz_travel_seconds == 6.5
        assert again.control.camera_presets == ["Room", "Desk", "Board"]


class TestRoomLocationPlaceholder:
    """The room templates ship location: "REPLACE_WITH_LOCATION"; a room that never set it shows its name alone."""

    def test_the_installer_placeholder_counts_as_no_location(self):
        assert RoomConfig(location="REPLACE_WITH_LOCATION").location == ""

    def test_blanks_count_as_no_location(self):
        assert RoomConfig(location="   ").location == ""

    def test_a_real_location_is_kept_and_trimmed(self):
        assert RoomConfig(location=" 2nd floor ").location == "2nd floor"

    def test_a_padded_placeholder_counts_as_no_location(self):
        """The spaces go first, then the prefix is looked for: a placeholder someone padded is still a placeholder."""
        assert RoomConfig(location="  REPLACE_WITH_ANYTHING ").location == ""
        assert without_placeholder("  REPLACE_WITH_ANYTHING ") == ""

    def test_from_dict_and_to_dict_carry_the_blank(self):
        config = Config.from_dict({"room": {"name": "Room 1", "location": "REPLACE_WITH_LOCATION"}})
        assert config.room.location == ""
        assert config.to_dict()["room"]["location"] == ""

    def test_without_placeholder_handles_none_and_numbers(self):
        assert without_placeholder(None) == ""
        assert without_placeholder(3) == "3"


class TestDialogAndControlConfig:
    """What the agent does with the dialogs Zoom draws over the TV, and the reactions the table offers (spec 2026-10-13, sections 5 and 8)."""

    def test_dialog_and_control_settings_round_trip_and_default(self):
        config = Config()
        assert "is being transcribed" in config.meeting.dialogs.acknowledge and config.meeting.dialogs.ask_everything is False
        assert config.meeting.controls.reactions[0] == "Thumbs Up"
        loaded = Config.from_dict({"meeting": {"dialogs": {"acknowledge": ["is being recorded"], "ask_everything": True}, "controls": {"reactions": ["Clap"]}}})
        assert loaded.meeting.dialogs.acknowledge == ["is being recorded"] and loaded.meeting.dialogs.ask_everything is True
        assert Config.from_dict(loaded.to_dict()).meeting.controls.reactions == ["Clap"]

    def test_the_reactions_are_zooms_own_names_not_emoji(self):
        """Zoom 6.5.0's picker has no emoji: its items are named Clap, Thumbs Up, Joy, Open Mouth, Heart and Tada."""
        assert sorted(DEFAULT_REACTIONS) == ["Clap", "Heart", "Joy", "Open Mouth", "Tada", "Thumbs Up"]
        assert Config().meeting.controls.reactions == DEFAULT_REACTIONS == ["Thumbs Up", "Clap", "Heart", "Joy", "Open Mouth", "Tada"]

    def test_the_defaults_are_written_out_and_are_copies(self):
        acknowledge, reactions = list(DEFAULT_ACKNOWLEDGE), list(DEFAULT_REACTIONS)       # what they are now, to compare with after the edits below
        assert acknowledge[:2] == ["is being recorded", "is being transcribed"]
        config = Config()
        written = config.to_dict()["meeting"]
        assert written["dialogs"] == {"acknowledge": acknowledge, "ask_everything": False} and written["controls"] == {"reactions": reactions}
        written["dialogs"]["acknowledge"].clear()                                         # what is written out is a copy of what the config holds
        written["controls"]["reactions"].clear()
        assert config.meeting.dialogs.acknowledge == acknowledge and config.meeting.controls.reactions == reactions
        first, second = Config(), Config()
        first.meeting.dialogs.acknowledge.append("one more")                              # and a config edits its own lists, not the module's
        first.meeting.controls.reactions.clear()
        assert second.meeting.dialogs.acknowledge == acknowledge and second.meeting.controls.reactions == reactions
        assert DEFAULT_ACKNOWLEDGE == acknowledge and DEFAULT_REACTIONS == reactions

    def test_a_section_given_in_part_keeps_the_defaults_of_the_rest(self):
        config = Config.from_dict({"meeting": {"dialogs": {"ask_everything": True}}})
        assert config.meeting.dialogs == DialogsConfig(acknowledge=list(DEFAULT_ACKNOWLEDGE), ask_everything=True)
        assert config.meeting.controls == ControlsConfig()

    def test_a_section_left_empty_in_the_file_is_the_default(self, caplog):
        """A person who deletes the lines under `dialogs:` leaves the key with nothing: the file still loads, and means the defaults, with
        nothing to say about it."""
        caplog.set_level(logging.WARNING, logger="croom.core.config")
        config = Config.from_dict({"meeting": {"dialogs": None, "controls": None}})
        assert config.meeting.dialogs == DialogsConfig() and config.meeting.controls == ControlsConfig()
        assert Config.from_dict(config.to_dict()).meeting.dialogs.acknowledge == DEFAULT_ACKNOWLEDGE
        assert not caplog.records

    def test_a_section_made_in_code_is_kept_as_it_is(self, caplog):
        caplog.set_level(logging.WARNING, logger="croom.core.config")
        meeting = MeetingConfig(dialogs=DialogsConfig(acknowledge=["is being recorded"], ask_everything=True), controls=ControlsConfig(reactions=["Clap"]))
        assert meeting.dialogs.acknowledge == ["is being recorded"] and meeting.dialogs.ask_everything is True and meeting.controls.reactions == ["Clap"]
        assert not caplog.records

    def test_the_settings_survive_the_config_file(self, temp_dir):
        config = Config()
        config.meeting.dialogs.acknowledge = ["is being recorded"]
        config.meeting.dialogs.ask_everything = True
        config.meeting.controls.reactions = ["Clap", "Tada"]
        path = str(temp_dir / "config.yaml")
        config.save(path)
        loaded = load_config(path)
        assert loaded.meeting.dialogs == DialogsConfig(acknowledge=["is being recorded"], ask_everything=True)
        assert loaded.meeting.controls == ControlsConfig(reactions=["Clap", "Tada"])

    def test_a_list_left_blank_in_the_file_is_the_default_and_the_config_still_writes_out(self):
        """`reactions:` or `acknowledge:` with nothing under it loads as None: the defaults stand (an empty list, written as `[]`, is a choice)."""
        config = Config.from_dict({"meeting": {"dialogs": {"acknowledge": None, "ask_everything": True}, "controls": {"reactions": None}}})
        assert config.meeting.dialogs == DialogsConfig(acknowledge=list(DEFAULT_ACKNOWLEDGE), ask_everything=True)
        assert config.meeting.controls == ControlsConfig(reactions=list(DEFAULT_REACTIONS))
        written = config.to_dict()["meeting"]
        assert written["dialogs"] == {"acknowledge": DEFAULT_ACKNOWLEDGE, "ask_everything": True} and written["controls"] == {"reactions": DEFAULT_REACTIONS}
        none = Config.from_dict({"meeting": {"dialogs": {"acknowledge": []}, "controls": {"reactions": []}}})
        assert none.meeting.dialogs.acknowledge == [] and none.meeting.controls.reactions == []
        assert DialogsConfig(acknowledge=None).acknowledge == DEFAULT_ACKNOWLEDGE and ControlsConfig(reactions=None).reactions == DEFAULT_REACTIONS

    def test_a_bare_word_is_one_item_not_its_letters(self):
        config = Config.from_dict({"meeting": {"dialogs": {"acknowledge": "is being recorded"}, "controls": {"reactions": "Clap"}}})
        assert config.meeting.dialogs.acknowledge == ["is being recorded"] and config.meeting.controls.reactions == ["Clap"]
        assert config.to_dict()["meeting"]["controls"] == {"reactions": ["Clap"]}

    @pytest.mark.parametrize("value", [False, True, 0, 7, 1.5, {"a": "b"}])
    def test_a_list_setting_that_is_not_a_list_is_the_defaults_with_a_warning_naming_the_key(self, value, caplog):
        """YAML reads `reactions: no` as False and `acknowledge: on` as True. The file still loads, the config still writes out (it crashed
        in to_dict), and the journal says which key was not a list."""
        caplog.set_level(logging.WARNING, logger="croom.core.config")
        config = Config.from_dict({"meeting": {"dialogs": {"acknowledge": value}, "controls": {"reactions": value}}})
        assert config.meeting.dialogs.acknowledge == DEFAULT_ACKNOWLEDGE and config.meeting.controls.reactions == DEFAULT_REACTIONS
        assert config.meeting.dialogs.acknowledge is not DEFAULT_ACKNOWLEDGE and config.meeting.controls.reactions is not DEFAULT_REACTIONS
        written = config.to_dict()["meeting"]
        assert written["dialogs"]["acknowledge"] == DEFAULT_ACKNOWLEDGE and written["controls"]["reactions"] == DEFAULT_REACTIONS
        warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warned) == 2 and "meeting.dialogs.acknowledge" in warned[0] and "meeting.controls.reactions" in warned[1]
        assert all(repr(value) in message and "defaults" in message for message in warned)

    def test_a_list_or_a_tuple_is_kept_as_it_is_without_a_word_in_the_journal(self, caplog):
        caplog.set_level(logging.WARNING, logger="croom.core.config")
        assert DialogsConfig(acknowledge=[]).acknowledge == [] and ControlsConfig(reactions=("Clap", "Tada")).reactions == ("Clap", "Tada")
        assert not caplog.records

    @pytest.mark.parametrize("words, expected", [
        ("true", True), ("True", True), ("yes", True), ("ON", True), (" on ", True),
        ("false", False), ("False", False), ("no", False), ("Off", False), (" off ", False),
    ])
    def test_ask_everything_written_as_a_quoted_word_reads_as_the_word_says(self, words, expected, caplog):
        """A quoted "false" is a string, which Python calls true: left as it was it made every dialog wait for a tap in a room whose admin
        wrote false."""
        caplog.set_level(logging.WARNING, logger="croom.core.config")
        config = Config.from_dict({"meeting": {"dialogs": {"ask_everything": words}}})
        assert config.meeting.dialogs.ask_everything is expected
        assert config.to_dict()["meeting"]["dialogs"]["ask_everything"] is expected
        assert not caplog.records

    @pytest.mark.parametrize("value", ["maybe", "", "  ", "1", "enabled", 1, 0, 2.5, [True], {"on": True}])
    def test_ask_everything_that_is_not_a_bool_or_one_of_those_words_is_the_default_with_a_warning(self, value, caplog):
        caplog.set_level(logging.WARNING, logger="croom.core.config")
        config = Config.from_dict({"meeting": {"dialogs": {"ask_everything": value}}})
        assert config.meeting.dialogs.ask_everything is False
        warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warned) == 1 and "meeting.dialogs.ask_everything" in warned[0] and repr(value) in warned[0]

    def test_ask_everything_left_blank_or_given_as_a_bool_is_taken_as_it_is(self, caplog):
        caplog.set_level(logging.WARNING, logger="croom.core.config")
        assert DialogsConfig(ask_everything=None).ask_everything is False
        assert DialogsConfig(ask_everything=True).ask_everything is True and DialogsConfig(ask_everything=False).ask_everything is False
        assert not caplog.records

    def test_a_key_the_section_does_not_know_is_ignored_with_a_warning_and_the_rest_of_the_section_loads(self, caplog):
        caplog.set_level(logging.WARNING, logger="croom.core.config")
        config = Config.from_dict({"meeting": {"dialogs": {"ask_evrything": True, "ask_everything": True}, "controls": {"reactons": ["Clap"], "reactions": ["Tada"]}}})
        assert config.meeting.dialogs.ask_everything is True and config.meeting.dialogs.acknowledge == DEFAULT_ACKNOWLEDGE
        assert config.meeting.controls.reactions == ["Tada"]
        warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warned) == 2 and "ask_evrything" in warned[0] and "dialogs" in warned[0] and "reactons" in warned[1] and "controls" in warned[1]

    def test_a_mistyped_key_does_not_cost_the_room_the_rest_of_its_config_file(self, temp_dir, caplog):
        """load_config falls back to the defaults for the whole file when anything in it raises: one typo under `dialogs:` must not."""
        caplog.set_level(logging.WARNING, logger="croom.core.config")
        path = temp_dir / "config.yaml"
        path.write_text("room:\n  name: Lab\nmeeting:\n  platforms: [zoom]\n  dialogs:\n    ask_evrything: true\n    acknowledge:\n  controls:\n    reactions:\n")
        loaded = load_config(str(path))
        assert loaded.room.name == "Lab" and loaded.meeting.platforms == ["zoom"]
        assert loaded.meeting.dialogs == DialogsConfig() and loaded.meeting.controls == ControlsConfig()
        assert [r.getMessage() for r in caplog.records if "ask_evrything" in r.getMessage()] and len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    @pytest.mark.parametrize("section", [[], "yes", 3, ["acknowledge"]])
    def test_a_section_that_is_not_a_mapping_is_the_defaults_with_a_warning(self, section, caplog):
        caplog.set_level(logging.WARNING, logger="croom.core.config")
        config = Config.from_dict({"meeting": {"dialogs": section, "controls": section}})
        assert config.meeting.dialogs == DialogsConfig() and config.meeting.controls == ControlsConfig()
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2
