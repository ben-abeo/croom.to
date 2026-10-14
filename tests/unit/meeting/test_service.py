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
from croom.meeting.providers.base import MeetingControlError, MeetingProvider, MeetingState, MeetingInfo
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

    def test_the_model_for_the_table_starts_empty_and_goes_out_under_its_names(self):
        """What the table draws from (spec 2026-10-13, section 3): nothing until the provider has read the meeting."""
        sent = MeetingInfo(meeting_id="1", platform="zoom", meeting_url="https://zoom.us/j/1").to_dict()
        assert sent["controls"] == {} and sent["people"] == {"count": 0, "list": []}
        assert sent["chat"] == {"unread": 0, "messages": []} and sent["notice"] == "" and sent["dialog"] is None
        assert sent["recording"] is False and sent["locked"] is False

    def test_each_meeting_has_a_model_of_its_own(self):
        first, second = (MeetingInfo(meeting_id=str(i), platform="zoom", meeting_url="u") for i in (1, 2))
        first.chat["messages"].append({"from": "Ann", "text": "hi", "at": ""})
        first.people["list"].append({"id": 1})
        first.controls["mute"] = {"available": True}
        assert second.chat == {"unread": 0, "messages": []} and second.people == {"count": 0, "list": []} and second.controls == {}


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


class ControllableProvider(RecordingProvider):
    """A provider that carries out controls: remembers what it was asked and answers with the state it left."""

    def __init__(self):
        super().__init__()
        self.controlled = []

    async def control(self, name, value=None):
        self.controlled.append((name, value))
        if name == "raise_hand" and value == "refuse":
            raise MeetingControlError("Zoom did not raise the hand")
        return {"raise_hand": {"available": True, "on": True}}

    async def leave_meeting(self):
        pass


class TestControl:
    """A control from the table goes to the provider of the meeting (spec 2026-10-13, section 3)."""

    async def joined(self):
        service = MeetingService(Config())
        provider = ControllableProvider()
        service._providers["zoom"] = provider
        await service.join_meeting("https://zoom.us/j/98765432100")
        return service, provider

    async def test_a_control_goes_to_the_provider_with_its_value_and_the_state_comes_back(self):
        service, provider = await self.joined()
        assert await service.control("raise_hand") == {"raise_hand": {"available": True, "on": True}}
        await service.control("reactions", "Clap")
        assert provider.controlled == [("raise_hand", None), ("reactions", "Clap")]

    async def test_the_providers_words_reach_the_caller(self):
        service, _ = await self.joined()
        with pytest.raises(MeetingControlError, match="Zoom did not raise the hand"):
            await service.control("raise_hand", "refuse")

    async def test_without_a_meeting_a_control_says_so(self):
        with pytest.raises(MeetingControlError) as failure:
            await MeetingService(Config()).control("raise_hand")
        assert str(failure.value) == "Not in a meeting"

    async def test_a_meeting_that_ended_is_no_meeting(self):
        service, provider = await self.joined()
        await service.leave_meeting()
        with pytest.raises(MeetingControlError, match="Not in a meeting"):
            await service.control("mute")
        assert provider.controlled == []


@pytest.mark.parametrize(
    "provider", [MeetingProvider, *get_all_providers().values(), ZoomSdkProvider], ids=lambda cls: cls.__name__
)
def test_every_provider_takes_the_passcode_after_the_existing_arguments(provider):
    """MeetingService passes passcode to whichever provider a link picks, so none may lack the parameter."""
    parameters = inspect.signature(provider.join_meeting).parameters
    assert list(parameters) == ["self", "meeting_url", "display_name", "camera_on", "mic_on", "passcode"]
    assert parameters["passcode"].default is None
