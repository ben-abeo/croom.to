"""
Every shipped room config must load through the agent's Config so a typo
surfaces here and not at first boot on a Pi.
"""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from croom.core.config import Config

ROOMS = sorted((Path(__file__).resolve().parents[3] / "deploy" / "rooms").glob("room-*.yaml"))


@pytest.mark.parametrize("path", ROOMS, ids=lambda p: p.name)
def test_room_configs_load(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = Config.from_dict(data)
    assert config.control.enabled is True
    assert config.control.port == 8080
    assert config.dashboard.enabled is True
    assert config.dashboard.enrollment_token == "REPLACE_WITH_TOKEN_FROM_DASHBOARD"
    assert "REPLACE_WITH_DASHBOARD_ADDRESS" in config.dashboard.url
    assert config.meeting.platforms == ["zoom", "google_meet"]
    assert config.ai.enabled is False
    assert config.calendar.providers == ["google"]
    assert config.calendar.google_credentials_path == "/etc/croom/google-service-account.json"
    assert config.calendar.google_calendar_id == "REPLACE_WITH_ROOM_CALENDAR_ID"
    assert config.meeting.zoom_credentials_path == "/etc/croom/zoom-credentials.json"


def test_there_are_three_rooms():
    assert [p.name for p in ROOMS] == ["room-1.yaml", "room-2.yaml", "room-3.yaml"]


@pytest.mark.parametrize("path", ROOMS, ids=lambda p: p.name)
def test_unfilled_room_configs_never_poll_google(path):
    # A device installed with the placeholders left in must log one line and serve the room page,
    # never poll Google (spec 2026-09-25 section 4.2).
    from croom.calendar.service import google_not_configured_reason
    config = Config.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert google_not_configured_reason(config.calendar) is not None


def test_git_ignores_a_real_service_account_key():
    repo = Path(__file__).resolve().parents[3]
    for name in ("google-service-account.json", "deploy/rooms/google-service-account.json"):
        assert subprocess.run(["git", "check-ignore", "-q", name], cwd=repo).returncode == 0, name


def test_zoom_credentials_template_is_all_placeholders():
    from croom.meeting.zoom_auth import zoom_not_configured_reason
    template = Path(__file__).resolve().parents[3] / "deploy" / "rooms" / "zoom-credentials.example.json"
    data = json.loads(template.read_text(encoding="utf-8"))
    assert set(data) == {"sdk_client_id", "sdk_client_secret", "account_id", "s2s_client_id", "s2s_client_secret", "room_user"}
    assert all(value.startswith("REPLACE_") for value in data.values())
    assert zoom_not_configured_reason(str(template)).startswith("sdk_client_id is missing or still REPLACE")


def test_git_ignores_real_zoom_credentials():
    repo = Path(__file__).resolve().parents[3]
    for name in ("zoom-credentials.json", "deploy/rooms/zoom-credentials.json"):
        assert subprocess.run(["git", "check-ignore", "-q", name], cwd=repo).returncode == 0, name
    assert subprocess.run(["git", "check-ignore", "-q", "deploy/rooms/zoom-credentials.example.json"], cwd=repo).returncode == 1
