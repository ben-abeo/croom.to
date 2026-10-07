"""
The google_meet platform is built from the config: the signed-in profile folder
and the room's name (spec 2026-10-07 Google Meet, section 4.2).
"""

from pathlib import Path

from croom.core.config import Config
from croom.meeting.providers import build_provider
from croom.meeting.providers.google_meet import GoogleMeetProvider


def test_config_round_trips_the_profile_dir():
    config = Config.from_dict({"meeting": {"google_profile_dir": "/var/lib/croom/meet-profile"}})
    assert config.meeting.google_profile_dir == "/var/lib/croom/meet-profile"
    assert Config.from_dict(config.to_dict()).meeting.google_profile_dir == "/var/lib/croom/meet-profile"
    assert Config().meeting.google_profile_dir == ""


def test_meet_provider_gets_the_profile_and_the_room_name():
    config = Config.from_dict({"room": {"name": "Room 3"}, "meeting": {"google_profile_dir": "/tmp/meet-profile"}})
    provider = build_provider("google_meet", config)
    assert isinstance(provider, GoogleMeetProvider)
    assert provider.profile_dir == Path("/tmp/meet-profile")
    assert provider.room_name == "Room 3"


def test_meet_provider_without_a_profile_is_a_guest_named_conference_room():
    provider = build_provider("google_meet", Config.from_dict({"room": {"name": ""}}))
    assert provider.profile_dir is None
    assert provider.room_name == "Conference Room"
