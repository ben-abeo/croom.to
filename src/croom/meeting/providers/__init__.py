"""
Meeting platform providers.

Each provider implements the MeetingProvider interface for
a specific video conferencing platform.
"""

import logging

from croom.meeting.providers.base import MeetingProvider, MeetingInfo, MeetingState
from croom.meeting.providers.google_meet import GoogleMeetProvider
from croom.meeting.providers.teams import TeamsProvider
from croom.meeting.providers.zoom import ZoomProvider
from croom.meeting.providers.webex import WebexProvider

logger = logging.getLogger(__name__)


def get_provider(platform: str) -> type:
    """
    Get provider class for platform name.

    Args:
        platform: Platform name ('google_meet', 'teams', 'zoom', 'webex')

    Returns:
        Provider class
    """
    providers = {
        "google_meet": GoogleMeetProvider,
        "teams": TeamsProvider,
        "zoom": ZoomProvider,
        "webex": WebexProvider,
    }
    return providers.get(platform)


def build_provider(platform: str, config) -> "MeetingProvider | None":
    """
    The provider instance for a platform, given the agent's Config. Zoom uses
    the Meeting SDK when its credentials file is configured; otherwise the
    public web client, with one warning (spec 2026-09-25 Zoom, section 4.2).
    """
    if platform == "zoom":
        from croom.meeting.zoom_auth import zoom_not_configured_reason
        from croom.meeting.providers.zoom_sdk import ZoomSdkProvider

        reason = zoom_not_configured_reason(config.meeting.zoom_credentials_path)
        if reason is None:
            return ZoomSdkProvider.from_config(config)
        logger.warning(f"Zoom Meeting SDK not configured: {reason}; using the web client, "
                       "which Zoom blocks for automated guests")
        return ZoomProvider()
    provider_cls = get_provider(platform)
    return provider_cls() if provider_cls else None


def get_all_providers() -> dict:
    """Get all available provider classes."""
    return {
        "google_meet": GoogleMeetProvider,
        "teams": TeamsProvider,
        "zoom": ZoomProvider,
        "webex": WebexProvider,
    }


__all__ = [
    "MeetingProvider",
    "MeetingInfo",
    "MeetingState",
    "GoogleMeetProvider",
    "TeamsProvider",
    "ZoomProvider",
    "WebexProvider",
    "get_provider",
    "build_provider",
    "get_all_providers",
]
