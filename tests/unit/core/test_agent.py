"""
Tests for croom.core.agent: service registration and startup without hardware.
"""

from unittest.mock import AsyncMock, patch

import pytest

from croom.core.agent import CroomAgent
from croom.core.config import Config
from croom.core.service import ServiceState


def make_config(tmp_path, dashboard_url: str = "") -> Config:
    config = Config()
    config.ai.enabled = False
    config.dashboard.enabled = bool(dashboard_url)
    config.dashboard.url = dashboard_url
    config.data_dir = str(tmp_path)
    return config


@pytest.fixture
def no_hardware():
    """Make every hardware probe find nothing and keep meeting providers from launching browsers."""
    with patch("croom.audio.service.get_audio_devices", return_value=[]), \
         patch("croom.video.service.get_cameras", return_value=[]), \
         patch("croom.display.service.DisplayService.initialize", new=AsyncMock(return_value=False)), \
         patch("croom.calendar.service.CalendarService.initialize", new=AsyncMock(return_value=False)), \
         patch("croom.meeting.service.get_provider", return_value=None):
        yield


def make_agent(config: Config) -> CroomAgent:
    with patch("croom.core.agent.load_config", return_value=config):
        return CroomAgent()


class TestServiceRegistration:
    def test_registers_every_optional_service_without_crashing(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path))
        agent._initialize_services()
        assert sorted(agent.service_manager.get_all_services()) == [
            "audio", "calendar", "display", "meeting", "video",
        ]

    def test_registers_dashboard_client_when_enabled(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path, dashboard_url="http://localhost:3001"))
        agent._initialize_services()
        dashboard = agent.service_manager.get_service("dashboard")
        assert dashboard is not None
        assert dashboard.config["url"] == "http://localhost:3001"
        assert dashboard.config["state_file"] == str(tmp_path / "dashboard-state.json")
        assert dashboard.config["device_info"]["name"] == agent.config.room.name


class TestStartup:
    async def test_start_all_runs_without_hardware(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path))
        agent._initialize_services()
        try:
            assert await agent.service_manager.start_all() is True
            # _state is the lifecycle field ServiceManager sets; DisplayService and
            # MeetingService shadow Service.state (and MeetingService.get_status()) with
            # their own domain status, so no public accessor is consistent here.
            states = {name: svc._state for name, svc in agent.service_manager.get_all_services().items()}
            assert all(state == ServiceState.RUNNING for state in states.values()), states
        finally:
            await agent.service_manager.stop_all()
        assert all(
            svc._state == ServiceState.STOPPED
            for svc in agent.service_manager.get_all_services().values()
        )
