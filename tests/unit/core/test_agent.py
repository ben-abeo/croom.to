"""
Tests for croom.core.agent: service registration and startup without hardware.
"""

import asyncio
import signal
from unittest.mock import AsyncMock, patch

import pytest

from croom.core.agent import CroomAgent
from croom.core.config import Config
from croom.core.service import ServiceState


def make_config(tmp_path, dashboard_url: str = "", control: bool = False) -> Config:
    config = Config()
    config.ai.enabled = False
    config.dashboard.enabled = bool(dashboard_url)
    config.dashboard.url = dashboard_url
    config.data_dir = str(tmp_path)
    config.control.enabled = control
    config.control.host = "127.0.0.1"
    config.control.port = 0
    return config


@pytest.fixture
def no_hardware():
    """Make every hardware probe find nothing and keep meeting providers from launching browsers.

    The devices service finds nothing too: no pw-dump runs and no /dev/video* is opened here.
    """
    with patch("croom.audio.service.get_audio_devices", return_value=[]), \
         patch("croom.video.service.get_cameras", return_value=[]), \
         patch("croom.display.service.DisplayService.initialize", new=AsyncMock(return_value=False)), \
         patch("croom.calendar.service.CalendarService.initialize", new=AsyncMock(return_value=False)), \
         patch("croom.meeting.service.build_provider", return_value=None), \
         patch("croom.devices.volume.RoomVolume.refresh", new=AsyncMock(return_value=None)), \
         patch("croom.devices.camera.RoomCamera.discover", new=AsyncMock(return_value=False)):
        yield


def make_agent(config: Config) -> CroomAgent:
    with patch("croom.core.agent.load_config", return_value=config):
        return CroomAgent()


class TestServiceRegistration:
    def test_registers_every_optional_service_without_crashing(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path))
        agent._initialize_services()
        assert sorted(agent.service_manager.get_all_services()) == [
            "audio", "calendar", "devices", "display", "meeting", "video",
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

    async def test_a_signal_and_the_main_task_share_one_stop_that_runs_to_the_end(self, tmp_path, no_hardware):
        """SIGTERM's stop sets the shutdown event, which wakes start(): start() must not return (and the process end)
        while the signal's stop_all is still stopping services, the devices service with its camera among them."""
        agent = make_agent(make_config(tmp_path, control=True))
        stop_all, stops = agent.service_manager.stop_all, {"begun": 0, "ended": 0}

        async def counted_stop_all():
            stops["begun"] += 1
            await stop_all()
            stops["ended"] += 1

        agent.service_manager.stop_all = counted_stop_all
        services = agent.service_manager.get_all_services
        with patch.object(agent, "_setup_signal_handlers"):          # the test delivers the signal itself
            running = asyncio.create_task(agent.start())
            for _ in range(500):
                await asyncio.sleep(0.01)
                if services() and all(svc._state == ServiceState.RUNNING for svc in services().values()):
                    break
            assert services() and all(svc._state == ServiceState.RUNNING for svc in services().values())
            signalled = asyncio.create_task(agent._handle_signal(signal.SIGTERM))
            await asyncio.wait_for(running, 10)
            # start() returned: as in asyncio.run, whatever is still pending now would be cancelled
            assert stops == {"begun": 1, "ended": 1}
            assert signalled.done() and not signalled.cancelled() and signalled.exception() is None
        assert all(svc._state == ServiceState.STOPPED for svc in services().values()), \
            {name: svc._state for name, svc in services().items()}

    async def test_stop_before_start_does_nothing(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path))
        agent.service_manager.stop_all = AsyncMock()
        await agent.stop()
        agent.service_manager.stop_all.assert_not_called()


class TestControlRegistration:
    def test_control_is_registered_after_meeting_and_calendar(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path, control=True))
        agent._initialize_services()
        control = agent.service_manager.get_service("control")
        assert control is not None
        assert control._meeting is agent.service_manager.get_service("meeting")
        assert control._calendar is agent.service_manager.get_service("calendar")
        order = agent.service_manager._start_order
        assert order.index("control") > order.index("meeting")
        assert order.index("control") > order.index("calendar")

    def test_devices_are_registered_and_handed_to_the_control_page(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path, control=True))
        agent._initialize_services()
        devices = agent.service_manager.get_service("devices")
        control = agent.service_manager.get_service("control")
        assert devices is not None and devices.name == "devices"
        assert control._devices is devices
        assert control._store.path == agent.config.resolve_data_dir() / "control-settings.json"
        # one store for both: two stores on one file would each write only the keys they know
        assert devices.camera._store is control._store
        order = agent.service_manager._start_order
        assert order.index("video") < order.index("devices") < order.index("control")

    def test_control_is_absent_when_disabled(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path, control=False))
        agent._initialize_services()
        assert agent.service_manager.get_service("control") is None

    async def test_start_all_serves_the_page_on_an_ephemeral_port(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path, control=True))
        agent._initialize_services()
        try:
            assert await agent.service_manager.start_all() is True
            control = agent.service_manager.get_service("control")
            assert control._state == ServiceState.RUNNING
            assert control.bound_port is not None and control.bound_port > 0
        finally:
            await agent.service_manager.stop_all()
        assert agent.service_manager.get_service("control").bound_port is None
