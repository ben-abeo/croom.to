"""
Tests for croom.core.config module.
"""

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
