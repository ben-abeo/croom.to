"""
The check command's words and exit codes (spec 2026-09-25 section 4.4), with a
fake provider, plus the command-line flag.
"""

import io
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import yaml

from croom.calendar.check import check_calendar, service_account_email
from croom.calendar.providers.base import CalendarEvent, MeetingPlatform
from croom.core.config import Config

ROOM = "c_1885abc@resource.calendar.google.com"
ACCOUNT = "rooms@crystal-meet.iam.gserviceaccount.com"


class FakeProvider:
    def __init__(self, authenticates=True, calendar=None, missing=False, events=()):
        self.authenticates = authenticates
        self.calendar = calendar or {"id": ROOM, "name": "Room 1"}
        self.missing = missing
        self._events = list(events)
        self.credentials = None
        self.requested = None

    async def authenticate(self, credentials):
        self.credentials = credentials
        return self.authenticates

    async def get_calendar(self, calendar_id):
        self.requested = calendar_id
        if self.missing:
            raise LookupError(calendar_id)
        return self.calendar

    async def get_events(self, calendar_id, time_min, time_max, max_results=100):
        return list(self._events)


def config_for(tmp_path, calendar_id=ROOM, providers=("google",), key=True):
    config = Config()
    config.calendar.providers = list(providers)
    if key:
        key_file = tmp_path / "key.json"
        key_file.write_text(json.dumps({"type": "service_account", "client_email": ACCOUNT}))
        config.calendar.google_credentials_path = str(key_file)
    config.calendar.google_calendar_id = calendar_id
    return config


def booking(title, hours_from_now, url=None, platform=MeetingPlatform.UNKNOWN, status="confirmed", response="accepted"):
    start = datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
    return CalendarEvent(id=title, title=title, start_time=start, end_time=start + timedelta(minutes=30),
                         meeting_url=url, meeting_platform=platform, status=status, response_status=response)


async def run(config, provider):
    out = io.StringIO()
    code = await check_calendar(config, out=out, provider_factory=lambda: provider)
    return code, out.getvalue()


class TestSuccess:
    async def test_lists_bookings_with_their_links(self, tmp_path):
        provider = FakeProvider(events=[
            booking("Board lunch", 3),
            booking("Design review", 1, "https://zoom.us/j/98765432100?pwd=abc", MeetingPlatform.ZOOM),
            booking("Cancelled one", 2, status="cancelled"),
            booking("Double booked", 4, response="declined"),
        ])
        code, text = await run(config_for(tmp_path), provider)
        assert code == 0, text
        assert f"Google Calendar: connected as {ACCOUNT}" in text
        assert f"Calendar: Room 1 ({ROOM})" in text
        assert "Bookings in the next 7 days:" in text
        lines = [line for line in text.splitlines() if line.startswith("  ")]
        assert len(lines) == 2
        assert "Design review" in lines[0] and lines[0].rstrip().endswith("Zoom link")
        assert "Board lunch" in lines[1] and lines[1].rstrip().endswith("no video link")
        assert provider.credentials == {"service_account_file": str(tmp_path / "key.json")}
        assert provider.requested == ROOM

    async def test_says_when_nothing_is_booked(self, tmp_path):
        code, text = await run(config_for(tmp_path), FakeProvider())
        assert code == 0 and "No bookings in the next 7 days." in text


class TestFailures:
    async def test_no_google_provider(self, tmp_path):
        code, text = await run(config_for(tmp_path, providers=()), FakeProvider())
        assert code == 1 and "set calendar.providers to [google]" in text

    async def test_placeholder_without_any_network(self, tmp_path):
        provider = FakeProvider()
        code, text = await run(config_for(tmp_path, calendar_id="REPLACE_WITH_ROOM_CALENDAR_ID"), provider)
        assert code == 1
        assert ("Google Calendar not configured: google_calendar_id is still the placeholder "
                "REPLACE_WITH_ROOM_CALENDAR_ID") in text
        assert provider.credentials is None

    async def test_missing_key_file(self, tmp_path):
        config = config_for(tmp_path)
        config.calendar.google_credentials_path = str(tmp_path / "gone.json")
        code, text = await run(config, FakeProvider())
        assert code == 1 and "credentials file not found or unreadable" in text

    async def test_key_rejected(self, tmp_path):
        code, text = await run(config_for(tmp_path), FakeProvider(authenticates=False))
        assert code == 1
        assert "Could not sign in" in text and "Calendar API is not enabled" in text

    async def test_calendar_not_shared_names_the_account_to_share_with(self, tmp_path):
        code, text = await run(config_for(tmp_path), FakeProvider(missing=True))
        assert code == 1
        assert f"Calendar {ROOM} not found or not shared" in text
        assert f"share the room's calendar with {ACCOUNT}" in text


def test_service_account_email_reads_the_key(tmp_path):
    key = tmp_path / "k.json"
    key.write_text('{"client_email": "a@b.iam.gserviceaccount.com"}')
    assert service_account_email(str(key)) == "a@b.iam.gserviceaccount.com"
    assert service_account_email(str(tmp_path / "missing.json")) == ""
    key.write_text("not json")
    assert service_account_email(str(key)) == ""


class TestCommandLine:
    def test_help_names_the_flag(self):
        result = subprocess.run([sys.executable, "-m", "croom.core.agent", "--help"], capture_output=True, text=True)
        assert result.returncode == 0 and "--check-calendar" in result.stdout

    def test_flag_runs_the_check_and_exits_with_its_code(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.safe_dump({"calendar": {
            "providers": ["google"],
            "google_credentials_path": str(tmp_path / "key.json"),
            "google_calendar_id": "REPLACE_WITH_ROOM_CALENDAR_ID",
        }}))
        (tmp_path / "key.json").write_text("{}")
        result = subprocess.run([sys.executable, "-m", "croom.core.agent", "--check-calendar", "-c", str(cfg)],
                                capture_output=True, text=True)
        assert result.returncode == 1
        assert "still the placeholder REPLACE_WITH_ROOM_CALENDAR_ID" in result.stdout
