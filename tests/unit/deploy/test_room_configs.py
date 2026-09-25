"""
Every shipped room config must load through the agent's Config so a typo
surfaces here and not at first boot on a Pi.
"""

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
    assert config.calendar.providers == []


def test_there_are_three_rooms():
    assert [p.name for p in ROOMS] == ["room-1.yaml", "room-2.yaml", "room-3.yaml"]
