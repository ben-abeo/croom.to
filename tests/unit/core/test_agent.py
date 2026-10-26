"""
Tests for croom.core.agent: service registration and startup without hardware.
"""

import asyncio
import logging
import signal
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from croom.core.agent import CroomAgent
from croom.core.config import Config
from croom.control.settings import SettingsStore
from croom.core.service import ServiceState
from croom.scores import espn
from tests.unit.scores.schedules import Feed


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
            "audio", "calendar", "devices", "display", "meeting", "tv", "video",
        ]

    def test_registers_dashboard_client_when_enabled(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path, dashboard_url="http://localhost:3001"))
        agent._initialize_services()
        dashboard = agent.service_manager.get_service("dashboard")
        assert dashboard is not None
        assert dashboard.config["url"] == "http://localhost:3001"
        assert dashboard.config["state_file"] == str(tmp_path / "dashboard-state.json")
        assert dashboard.config["device_info"]["name"] == agent.config.room.name


class TestTvRegistration:
    def with_a_tv(self, tmp_path, enabled=True) -> Config:
        config = make_config(tmp_path)
        config.tv.enabled, config.tv.host = enabled, "10.0.0.41"
        return config

    def test_a_room_without_a_tv_can_select_one_without_restarting(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path))
        agent._initialize_services()
        tv = agent.service_manager.get_service("tv")
        assert not tv.configured
        assert agent.service_manager.get_service("meeting")._before_join == [tv.before_join]

    def test_a_tv_switched_off_in_the_config_stays_unconfigured_until_selected(self, tmp_path, no_hardware):
        agent = make_agent(self.with_a_tv(tmp_path, enabled=False))
        agent._initialize_services()
        assert not agent.service_manager.get_service("tv").configured

    def test_the_tv_is_registered_ahead_of_the_meeting_and_the_meeting_waits_for_it_before_a_join(self, tmp_path, no_hardware):
        config = self.with_a_tv(tmp_path)
        agent = make_agent(config)
        agent._initialize_services()
        tv = agent.service_manager.get_service("tv")
        meeting = agent.service_manager.get_service("meeting")
        assert tv is not None and tv.name == "tv" and tv._config is config.tv
        assert meeting._before_join == [tv.before_join]
        order = agent.service_manager._start_order
        assert order.index("tv") < order.index("audio") and order.index("tv") < order.index("meeting")

    def test_the_tv_starts_before_the_meeting_whatever_else_is_registered(self, tmp_path, no_hardware):
        config = self.with_a_tv(tmp_path)
        config.ai.enabled = True                          # the services that depend on ai are put right after it in the start order
        agent = make_agent(config)
        agent._initialize_services()
        order = agent.service_manager._start_order
        assert order.index("tv") < order.index("meeting")

    async def test_an_agent_whose_tv_is_not_paired_starts_and_stops_without_touching_it(self, tmp_path, no_hardware):
        agent = make_agent(self.with_a_tv(tmp_path))
        agent._initialize_services()
        try:
            assert await agent.service_manager.start_all() is True
            tv = agent.service_manager.get_service("tv")
            assert tv._state == ServiceState.RUNNING and tv.status()["paired"] is False and tv._client is None
        finally:
            await agent.service_manager.stop_all()
        assert agent.service_manager.get_service("tv")._state == ServiceState.STOPPED


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

    def test_the_tv_is_handed_to_the_control_page_and_starts_before_it(self, tmp_path, no_hardware):
        config = make_config(tmp_path, control=True)
        config.tv.enabled, config.tv.host = True, "10.0.0.41"
        agent = make_agent(config)
        agent._initialize_services()
        tv = agent.service_manager.get_service("tv")
        control = agent.service_manager.get_service("control")
        assert tv is not None and control._tv is tv
        order = agent.service_manager._start_order
        assert order.index("tv") < order.index("control")

    def test_a_room_without_a_tv_hands_the_control_page_a_service_for_selection(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path, control=True))
        agent._initialize_services()
        tv = agent.service_manager.get_service("tv")
        assert not tv.configured
        assert agent.service_manager.get_service("control")._tv is tv

    def test_a_disabled_tv_can_be_set_up_on_the_control_page(self, tmp_path, no_hardware):
        config = make_config(tmp_path, control=True)
        config.tv.enabled, config.tv.host = False, "10.0.0.41"
        agent = make_agent(config)
        agent._initialize_services()
        assert not agent.service_manager.get_service("control")._tv.configured

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


class TestScoresRegistration:
    """The football scoreboard (spec 2026-10-13 door sign, section 2.5): a service the door sign's page needs, so it is registered when
    the control page is, before it, and reads the schools that are on from the control page's own settings store."""

    def test_the_scoreboard_is_registered_before_the_control_page_and_handed_to_it(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path, control=True))
        agent._initialize_services()
        scores, control = agent.service_manager.get_service("scores"), agent.service_manager.get_service("control")
        assert scores is not None and scores.name == "scores" and control._scores is scores
        order = agent.service_manager._start_order
        assert order.index("scores") < order.index("control")
        assert scores._fetcher is espn.fetch                                  # ESPN's feed, over a session of its own for each read

    def test_the_scoreboard_is_absent_when_the_control_page_is_disabled(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path, control=False))
        agent._initialize_services()
        assert agent.service_manager.get_service("scores") is None            # nothing would show it: the sign is the control page's

    def test_the_scoreboard_lists_the_schools_the_room_has_on_as_the_store_holds_them_at_each_ask(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path, control=True))
        agent._initialize_services()
        scores, store = agent.service_manager.get_service("scores"), agent.service_manager.get_service("control")._store
        assert scores.current() == []                                         # off until a room turns a school on
        store.save("sign_scores", ["texas", "michigan"])                      # what the page's switches do, through /api/settings
        assert [entry["school"] for entry in scores.current()] == ["texas", "michigan"]
        store.save("sign_scores", ["missouri"])                               # the next ask sees the change: the list is read, not kept
        assert [entry["school"] for entry in scores.current()] == ["missouri"]
        store.save("sign_scores", ["texas", "ohio_state"])                    # a value the page would not load is the default, as it is there
        assert scores.current() == []
        store.save("sign_scores", [])
        assert scores.current() == []

    async def test_a_school_turned_on_from_the_page_is_read_and_shown_in_the_status(self, tmp_path, no_hardware, monkeypatch):
        feed = Feed()
        monkeypatch.setattr(espn, "fetch", feed)                              # ESPN is never asked: this is the fake feed
        agent = make_agent(make_config(tmp_path, control=True))
        agent._initialize_services()
        scores, control = agent.service_manager.get_service("scores"), agent.service_manager.get_service("control")
        async with TestClient(TestServer(control.create_app())) as client:
            assert "scores" not in await (await client.get("/api/status")).json()
            await scores.refresh()
            assert feed.asked == []                                           # nothing is read while every school is off
            assert (await client.post("/api/settings", json={"sign_scores": ["texas"]})).status == 200
            await scores.refresh()
            assert feed.asked == [251]                                        # Texas's team id, and nobody else's
            entries = (await (await client.get("/api/status")).json())["scores"]
            assert [(e["school"], e["last"]["home_score"], e["last"]["away_score"]) for e in entries] == [("texas", 45, 42)]

    async def test_a_school_switched_on_while_the_settings_file_cannot_be_written_is_still_read_until_the_restart(self, tmp_path, no_hardware, monkeypatch, caplog):
        """The page's switch is answered and shown whether or not the choice reaches the disk, as the screensaver's is; the scoreboard reads the schools from the
        store, so the store has to go on with it too, or the sign would say a school is on and show nothing, until a restart."""
        agent = make_agent(make_config(tmp_path, control=True))
        agent._initialize_services()
        scores, control = agent.service_manager.get_service("scores"), agent.service_manager.get_service("control")

        def refuse(src, dst):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("croom.control.settings.os.replace", refuse)
        async with TestClient(TestServer(control.create_app())) as client:
            with caplog.at_level(logging.WARNING):
                response = await client.post("/api/settings", json={"sign_scores": ["texas"]})
            assert response.status == 200 and (await response.json())["sign_scores"] == ["texas"]
        assert [entry["school"] for entry in scores.current()] == ["texas"]    # the scoreboard follows the switch
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "Could not save the room settings" in r.getMessage()]
        assert len(warnings) == 1 and "No space left on device" in warnings[0]  # one WARNING, with the reason
        assert SettingsStore(control._store.path).get("sign_scores") is None   # and the file has not got it: a restart does not either

    def test_the_agent_says_so_when_the_scoreboard_cannot_be_had_for_want_of_a_settings_store(self, tmp_path, no_hardware, caplog):
        """Without the store the school switches would do nothing: one WARNING says why there is no scoreboard, as the ImportError path says it."""
        agent = make_agent(make_config(tmp_path, control=True))
        with caplog.at_level(logging.WARNING, logger="croom.core.agent"):
            assert agent._register_scores(None) is None
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and warnings[0].startswith("Football scores not available") and "settings store" in warnings[0], warnings
