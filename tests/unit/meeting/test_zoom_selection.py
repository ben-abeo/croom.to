"""
The zoom platform uses the Meeting SDK provider when its credentials are
configured, and the web client with exactly one warning otherwise (spec
2026-09-25 Zoom, section 4.2).
"""

import json
import logging
from unittest.mock import AsyncMock, patch

from croom.core.config import Config
from croom.meeting.providers import build_provider, get_provider
from croom.meeting.providers.google_meet import GoogleMeetProvider
from croom.meeting.providers.zoom import ZoomProvider
from croom.meeting.providers.zoom_sdk import ZoomSdkProvider
from croom.meeting.service import MeetingService


def config_with(tmp_path, credentials):
    config = Config()
    config.meeting.platforms = ["zoom", "google_meet"]
    if credentials is not None:
        path = tmp_path / "zoom-credentials.json"
        path.write_text(json.dumps(credentials))
        config.meeting.zoom_credentials_path = str(path)
    return config


def test_sdk_provider_when_credentials_are_configured(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        provider = build_provider("zoom", config_with(tmp_path, {"sdk_client_id": "a", "sdk_client_secret": "b"}))
    assert isinstance(provider, ZoomSdkProvider)
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_web_client_with_one_warning_otherwise(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        provider = build_provider("zoom", config_with(tmp_path, None))
    assert isinstance(provider, ZoomProvider) and not isinstance(provider, ZoomSdkProvider)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["Zoom Meeting SDK not configured: no zoom_credentials_path in the config; "
                        "using the web client, which Zoom blocks for automated guests"]


def test_other_platforms_and_unknown_names(tmp_path):
    assert isinstance(build_provider("google_meet", config_with(tmp_path, None)), GoogleMeetProvider)
    assert build_provider("nope", config_with(tmp_path, None)) is None
    assert get_provider("zoom") is ZoomProvider  # the config-free registry is unchanged


async def test_meeting_service_starts_the_sdk_provider(tmp_path):
    config = config_with(tmp_path, {"sdk_client_id": "a", "sdk_client_secret": "b"})
    service = MeetingService(config)
    with patch.object(ZoomSdkProvider, "initialize", new=AsyncMock()), \
         patch.object(GoogleMeetProvider, "initialize", new=AsyncMock()):
        await service.start()
    assert isinstance(service._providers["zoom"], ZoomSdkProvider)
    assert service.get_available_platforms() == ["zoom", "google_meet"]


async def test_meeting_service_falls_back_to_the_web_client(tmp_path):
    service = MeetingService(config_with(tmp_path, None))
    with patch.object(ZoomProvider, "initialize", new=AsyncMock()), \
         patch.object(GoogleMeetProvider, "initialize", new=AsyncMock()):
        await service.start()
    assert type(service._providers["zoom"]) is ZoomProvider
