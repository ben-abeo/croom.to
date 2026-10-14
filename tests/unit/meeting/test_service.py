"""
Tests for croom.meeting.service module.
"""

import asyncio
import inspect
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timedelta

import pytest

from croom.core.config import Config
from croom.meeting.providers import get_all_providers
from croom.meeting.providers.base import MeetingProvider, MeetingState, MeetingInfo
from croom.meeting.providers.zoom_sdk import ZoomSdkProvider
from croom.meeting.service import MeetingService


class TestMeetingState:
    """Tests for MeetingState enum."""

    def test_values(self):
        """Test meeting state enum values."""
        assert MeetingState.IDLE.value == "idle"
        assert MeetingState.JOINING.value == "joining"
        assert MeetingState.IN_LOBBY.value == "in_lobby"
        assert MeetingState.CONNECTED.value == "connected"
        assert MeetingState.LEAVING.value == "leaving"
        assert MeetingState.ERROR.value == "error"


class TestMeetingInfo:
    """Tests for MeetingInfo dataclass."""

    def test_default_values(self):
        """Test default meeting info values."""
        info = MeetingInfo(
            meeting_id="abc-defg-hij",
            platform="google_meet",
            meeting_url="https://meet.google.com/abc-defg-hij",
        )
        assert info.meeting_id == "abc-defg-hij"
        assert info.platform == "google_meet"
        assert info.title == ""
        assert info.start_time is None
        assert info.state == MeetingState.IDLE

    def test_with_schedule(self):
        """Test meeting info with scheduled time."""
        start_time = datetime.now() + timedelta(hours=1)
        info = MeetingInfo(
            meeting_id="123",
            platform="teams",
            meeting_url="https://teams.microsoft.com/...",
            title="Team Standup",
            start_time=start_time,
            end_time=start_time + timedelta(hours=1),
        )
        assert info.title == "Team Standup"
        assert info.start_time == start_time

    def test_to_dict(self):
        """Test converting meeting info to dict."""
        info = MeetingInfo(
            meeting_id="test-123",
            platform="zoom",
            meeting_url="https://zoom.us/j/test-123",
        )
        result = info.to_dict()
        assert "meeting_id" in result
        assert "platform" in result
        assert "meeting_url" in result


class TestMeetingServiceBasic:
    """Tests for MeetingService class basic functionality."""

    def test_service_creation(self):
        """Test meeting service can be created."""
        mock_config = MagicMock()
        mock_config.meeting.platforms = []
        service = MeetingService(mock_config)
        assert service is not None
        assert service.name == "meeting"

    def test_service_has_providers_dict(self):
        """Test service has providers dictionary."""
        mock_config = MagicMock()
        mock_config.meeting.platforms = []
        service = MeetingService(mock_config)
        assert hasattr(service, "_providers")
        assert isinstance(service._providers, dict)


class TestMeetingPlatformDetection:
    """Tests for meeting platform detection."""

    def test_detect_google_meet(self):
        """Test detection of Google Meet URL."""
        from croom.meeting.providers.base import detect_platform

        url = "https://meet.google.com/abc-defg-hij"
        platform = detect_platform(url)
        assert platform == "google_meet"

    def test_detect_teams(self):
        """Test detection of Microsoft Teams URL."""
        from croom.meeting.providers.base import detect_platform

        url = "https://teams.microsoft.com/l/meetup-join/123"
        platform = detect_platform(url)
        assert platform == "teams"

    def test_detect_zoom(self):
        """Test detection of Zoom URL."""
        from croom.meeting.providers.base import detect_platform

        url = "https://zoom.us/j/123456789"
        platform = detect_platform(url)
        assert platform == "zoom"

    def test_detect_webex(self):
        """Test detection of Webex URL."""
        from croom.meeting.providers.base import detect_platform

        url = "https://company.webex.com/meet/username"
        platform = detect_platform(url)
        assert platform == "webex"

    def test_detect_unknown(self):
        """Test detection of unknown URL."""
        from croom.meeting.providers.base import detect_platform

        url = "https://unknown-meeting.com/join"
        platform = detect_platform(url)
        assert platform is None or platform == "unknown"


class TestMeetingProviders:
    """Tests for meeting providers."""

    def test_get_all_providers(self):
        """Test getting all provider types."""
        from croom.meeting.providers import get_all_providers

        providers = get_all_providers()
        assert isinstance(providers, dict)

    def test_get_provider(self):
        """Test getting a specific provider class."""
        from croom.meeting.providers import get_provider

        # Try to get Google Meet provider
        provider_cls = get_provider("google_meet")
        # May return None if not configured, just verify no exception
        assert provider_cls is None or callable(provider_cls)


class RecordingProvider:
    """Stands in for the provider a link picks: remembers what the service asked it to join."""

    def __init__(self):
        self.state = MeetingState.IDLE
        self.joins = []

    def add_state_callback(self, callback):
        pass

    async def join_meeting(self, meeting_url, display_name="Conference Room", camera_on=True, mic_on=True,
                           passcode=None):
        self.joins.append({"url": meeting_url, "display_name": display_name, "camera_on": camera_on,
                           "mic_on": mic_on, "passcode": passcode})
        return MeetingInfo(platform="zoom", meeting_id="98765432100", meeting_url=meeting_url)


class TestJoinMeeting:
    """What the service hands the provider it picks for a link."""

    def service_with_zoom(self):
        service = MeetingService(Config())
        provider = RecordingProvider()
        service._providers["zoom"] = provider
        return service, provider

    async def test_the_passcode_reaches_the_provider(self):
        service, provider = self.service_with_zoom()
        await service.join_meeting("https://zoom.us/j/98765432100", passcode="940104")
        assert [join["passcode"] for join in provider.joins] == ["940104"]

    async def test_without_a_passcode_the_provider_is_given_none(self):
        service, provider = self.service_with_zoom()
        await service.join_meeting("https://zoom.us/j/98765432100?pwd=abc")
        assert provider.joins[0]["passcode"] is None
        assert provider.joins[0]["url"] == "https://zoom.us/j/98765432100?pwd=abc"

    async def test_the_arguments_keep_their_positional_order_and_the_passcode_comes_last(self):
        service, provider = self.service_with_zoom()
        await service.join_meeting("https://zoom.us/j/98765432100", "Lab", False, True, "940104")
        assert provider.joins == [{"url": "https://zoom.us/j/98765432100", "display_name": "Lab",
                                   "camera_on": False, "mic_on": True, "passcode": "940104"}]


class TestBeforeJoinHooks:
    """What the room does before a join (the TV is woken, and the join waits for it): hooks that the join runs first."""

    def service_with_zoom(self):
        service = MeetingService(Config())
        provider = RecordingProvider()
        service._providers["zoom"] = provider
        return service, provider

    async def test_a_hook_runs_before_the_provider_joins_and_a_hook_that_raises_does_not_stop_the_join(self):
        service, provider = self.service_with_zoom()
        seen = []

        async def waits_for_the_tv():
            seen.append(("tv", len(provider.joins)))      # the provider has not been asked to join yet

        async def broken():
            seen.append(("broken", len(provider.joins)))
            raise RuntimeError("the TV library choked")
        service.add_before_join(broken)
        service.add_before_join(waits_for_the_tv)
        info = await service.join_meeting("https://zoom.us/j/98765432100", passcode="940104")
        assert seen == [("broken", 0), ("tv", 0)]         # in the order they were added, each one although the one before failed
        assert [join["passcode"] for join in provider.joins] == ["940104"] and info.meeting_id == "98765432100"

    async def test_a_hook_that_did_not_get_what_it_wanted_is_logged_and_the_join_goes_on(self, caplog):
        service, provider = self.service_with_zoom()

        async def the_tv_never_woke():
            return False
        service.add_before_join(the_tv_never_woke)
        await service.join_meeting("https://zoom.us/j/98765432100")
        assert len(provider.joins) == 1 and "the_tv_never_woke" in caplog.text

    async def test_a_hook_that_raises_is_logged(self, caplog):
        service, provider = self.service_with_zoom()

        async def broken():
            raise RuntimeError("the TV library choked")
        service.add_before_join(broken)
        await service.join_meeting("https://zoom.us/j/98765432100")
        assert len(provider.joins) == 1 and "the TV library choked" in caplog.text

    async def test_a_hook_that_never_comes_back_is_given_up_on_and_the_join_goes_on(self, caplog):
        service, provider = self.service_with_zoom()
        service.BEFORE_JOIN_TIMEOUT_S = 0.1
        ended = []

        async def hangs():
            try:
                await asyncio.Event().wait()
            finally:
                ended.append("cancelled")                 # given up on, not left running
        service.add_before_join(hangs)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await service.join_meeting("https://zoom.us/j/98765432100")
        assert loop.time() - t0 < 2.0 and len(provider.joins) == 1 and ended == ["cancelled"]
        assert "hangs did not finish in 0.1 s" in caplog.text and service.state == MeetingState.IDLE

    async def test_a_hook_that_succeeds_says_nothing(self, caplog):
        service, provider = self.service_with_zoom()

        async def the_tv_is_on():
            return True

        async def does_its_work_and_answers_nothing():
            pass
        service.add_before_join(the_tv_is_on)
        service.add_before_join(does_its_work_and_answers_nothing)         # no answer is no complaint
        await service.join_meeting("https://zoom.us/j/98765432100")
        assert len(provider.joins) == 1 and not [r for r in caplog.records if r.levelno >= 30]

    async def test_hooks_do_not_run_for_a_link_that_cannot_be_joined(self):
        service = MeetingService(Config())                # no providers: nothing can be joined
        ran = []

        async def hook():
            ran.append(1)
        service.add_before_join(hook)
        with pytest.raises(ValueError):
            await service.join_meeting("https://example.com/not-a-meeting")
        with pytest.raises(RuntimeError):
            await service.join_meeting("https://zoom.us/j/98765432100")
        assert ran == []                                  # nobody waits for a TV to be told that the link is no good

    async def test_the_service_says_joining_while_the_hooks_run(self):
        """The room page draws from this state: it must not show an idle room (and let the join be tapped again) while the
        TV is being woken, which takes up to fifteen seconds."""
        service, provider = self.service_with_zoom()
        states = []

        async def waits_for_the_tv():
            states.append(service.state)
        service.add_before_join(waits_for_the_tv)
        assert service.state == MeetingState.IDLE
        await service.join_meeting("https://zoom.us/j/98765432100")
        assert states == [MeetingState.JOINING] and service.state == MeetingState.IDLE

    async def test_a_join_that_is_cancelled_while_the_hooks_run_leaves_the_service_idle(self):
        service, provider = self.service_with_zoom()
        gate = asyncio.Event()

        async def slow():
            await gate.wait()
        service.add_before_join(slow)
        joining = asyncio.ensure_future(service.join_meeting("https://zoom.us/j/98765432100"))
        for _ in range(5):
            await asyncio.sleep(0)
        assert service.state == MeetingState.JOINING
        joining.cancel()
        with pytest.raises(asyncio.CancelledError):
            await joining
        assert service.state == MeetingState.IDLE and provider.joins == []

    async def test_two_joins_waiting_on_the_hooks_do_not_clear_each_others_state(self):
        service, provider = self.service_with_zoom()
        gates = [asyncio.Event(), asyncio.Event()]
        calls = []

        async def slow():
            calls.append(1)
            await gates[len(calls) - 1].wait()
        service.add_before_join(slow)
        first = asyncio.ensure_future(service.join_meeting("https://zoom.us/j/98765432100"))
        second = asyncio.ensure_future(service.join_meeting("https://zoom.us/j/98765432101"))
        for _ in range(5):
            await asyncio.sleep(0)
        gates[0].set()
        await first
        assert service.state == MeetingState.JOINING      # the second one is still waiting for the TV
        gates[1].set()
        await second
        assert service.state == MeetingState.IDLE and len(provider.joins) == 2


@pytest.mark.parametrize(
    "provider", [MeetingProvider, *get_all_providers().values(), ZoomSdkProvider], ids=lambda cls: cls.__name__
)
def test_every_provider_takes_the_passcode_after_the_existing_arguments(provider):
    """MeetingService passes passcode to whichever provider a link picks, so none may lack the parameter."""
    parameters = inspect.signature(provider.join_meeting).parameters
    assert list(parameters) == ["self", "meeting_url", "display_name", "camera_on", "mic_on", "passcode"]
    assert parameters["passcode"].default is None
