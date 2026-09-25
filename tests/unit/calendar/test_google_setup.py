"""
Google Calendar setup on a room device (spec 2026-09-25 sections 4.2 and 4.3):
the config field, the calendar id wiring, the not-configured rule and the
declined-booking filter.
"""

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from croom.calendar.providers.base import CalendarEvent, MeetingPlatform
from croom.calendar.service import CalendarService, google_not_configured_reason
from croom.core.config import Config

ROOM = "c_1885abc@resource.calendar.google.com"


def google_config(tmp_path, calendar_id=ROOM, key=True):
    config = Config()
    config.calendar.providers = ["google"]
    if key:
        key_file = tmp_path / "key.json"
        key_file.write_text('{"type": "service_account", "client_email": "rooms@p.iam.gserviceaccount.com"}')
        config.calendar.google_credentials_path = str(key_file)
    config.calendar.google_calendar_id = calendar_id
    return config


class TestConfigField:
    def test_calendar_id_parses_and_round_trips(self):
        config = Config.from_dict({"calendar": {
            "providers": ["google"],
            "google_credentials_path": "/etc/croom/google-service-account.json",
            "google_calendar_id": ROOM,
        }})
        assert config.calendar.google_calendar_id == ROOM
        data = config.to_dict()["calendar"]
        assert data["google_calendar_id"] == ROOM
        assert data["google_credentials_path"] == "/etc/croom/google-service-account.json"

    def test_calendar_id_defaults_to_empty(self):
        assert Config().calendar.google_calendar_id == ""


class TestNotConfiguredRule:
    def test_ready_when_key_exists_and_id_is_real(self, tmp_path):
        assert google_not_configured_reason(google_config(tmp_path).calendar) is None

    def test_no_key_path(self, tmp_path):
        config = google_config(tmp_path, key=False)
        assert google_not_configured_reason(config.calendar) == "no google_credentials_path in the config"

    def test_missing_key_file(self, tmp_path):
        config = google_config(tmp_path)
        config.calendar.google_credentials_path = str(tmp_path / "nope.json")
        reason = google_not_configured_reason(config.calendar)
        assert reason.startswith("credentials file not found or unreadable: ") and reason.endswith("nope.json")

    def test_empty_calendar_id(self, tmp_path):
        reason = google_not_configured_reason(google_config(tmp_path, calendar_id="").calendar)
        assert reason == "google_calendar_id is empty; put the room's calendar address in the config"

    def test_placeholder_calendar_id(self, tmp_path):
        reason = google_not_configured_reason(google_config(tmp_path, calendar_id="REPLACE_WITH_ROOM_CALENDAR_ID").calendar)
        assert reason == ("google_calendar_id is still the placeholder REPLACE_WITH_ROOM_CALENDAR_ID; "
                          "put the room's calendar address in the config")


class TestFromConfig:
    def test_passes_the_calendar_id_and_no_reason_when_ready(self, tmp_path):
        service = CalendarService.from_config(google_config(tmp_path))
        assert service.config["calendar_ids"] == [ROOM]
        assert service.config["not_configured"] is None
        assert service.config["credentials"] == {"service_account_file": str(tmp_path / "key.json")}

    def test_carries_the_reason_when_not_ready(self, tmp_path):
        service = CalendarService.from_config(google_config(tmp_path, calendar_id="REPLACE_WITH_ROOM_CALENDAR_ID"))
        assert service.config["not_configured"].startswith("google_calendar_id is still the placeholder")
        assert service.config["calendar_ids"] == ["REPLACE_WITH_ROOM_CALENDAR_ID"]

    def test_other_providers_have_no_reason(self):
        config = Config()
        config.calendar.providers = ["microsoft"]
        assert CalendarService.from_config(config).config["not_configured"] is None


class TestStartWhenNotConfigured:
    async def test_logs_one_warning_and_never_polls(self, tmp_path, caplog):
        service = CalendarService.from_config(google_config(tmp_path, calendar_id="REPLACE_WITH_ROOM_CALENDAR_ID"))
        with patch("croom.calendar.service.GoogleCalendarProvider.authenticate",
                   new=AsyncMock(return_value=True)) as auth, \
             patch.object(service, "_fetch_events", new=AsyncMock()) as fetch, \
             caplog.at_level(logging.INFO):
            await service.start()
            await service.stop()
        auth.assert_not_awaited()
        fetch.assert_not_awaited()
        assert service.connected is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "still the placeholder REPLACE_WITH_ROOM_CALENDAR_ID" in warnings[0].getMessage()

    async def test_initialize_uses_only_the_configured_calendar(self, tmp_path):
        service = CalendarService.from_config(google_config(tmp_path))
        with patch("croom.calendar.service.GoogleCalendarProvider.authenticate", new=AsyncMock(return_value=True)), \
             patch("croom.calendar.service.GoogleCalendarProvider.get_calendars",
                   new=AsyncMock(return_value=[{"id": "primary", "name": "rooms@p", "primary": True}])) as listing:
            assert await service.initialize() is True
        assert service._calendar_ids == [ROOM]
        listing.assert_not_awaited()


def booking(event_id, minutes_from_now, status="confirmed", response="accepted"):
    start = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    return CalendarEvent(id=event_id, title=event_id, start_time=start, end_time=start + timedelta(minutes=30),
                         meeting_url="https://zoom.us/j/98765432100?pwd=abc", meeting_platform=MeetingPlatform.ZOOM,
                         status=status, response_status=response)


class TestDeclinedBookings:
    async def test_declined_and_cancelled_bookings_are_dropped(self):
        service = CalendarService(config={"provider": "google", "calendar_ids": [ROOM]})
        service._initialized = True
        service._calendar_ids = [ROOM]
        service._provider = AsyncMock()
        service._provider.get_events = AsyncMock(return_value=[
            booking("kept", 10),
            booking("double-booked", 40, response="declined"),
            booking("cancelled", 70, status="cancelled"),
        ])
        await service._fetch_events()
        assert [e.id for e in service.events] == ["kept"]
