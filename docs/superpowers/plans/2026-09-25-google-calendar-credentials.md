# Google Calendar Credentials Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each Crystal Meet room device reads its own Google Workspace room calendar through a shared service account, so real bookings appear on the room page and door sign and the existing "Join now" button joins them; nothing joins by itself.

**Architecture:** The agent's existing `CalendarService` and `GoogleCalendarProvider` are made to work for real: a configured calendar id replaces the primary-calendar fallback, a "not configured" rule turns placeholders and missing key files into one clear log line, bookings the room declined are dropped, the Google client runs off the event loop, and links are detected from the conference entry itself. A `croom --check-calendar` command prints the setup's state in plain words. The installer gains `--credentials FILE`, the room configs gain the calendar section, and a second brand guide walks a Google admin through room resources, the service account, calendar sharing and the device steps.

**Tech Stack:** Python 3.12 (asyncio, aiohttp already present), google-api-python-client + google-auth (new dependencies), bash installer, Playwright Chromium for rendering the guide PDFs, pytest with `asyncio_mode = "auto"`.

**Spec:** `docs/superpowers/specs/2026-09-25-google-calendar-credentials-design.md`

## Global Constraints

- Internal names stay: the `croom` package and command, systemd units, `/etc/croom`. Only what a person reads says Crystal Meet.
- No secrets in the repo: `deploy/rooms/` and `docs/` carry placeholders only. The key file on a device is `/etc/croom/google-service-account.json`, owned by the service user, mode 600.
- The calendar id placeholder is exactly `REPLACE_WITH_ROOM_CALENDAR_ID`; any id starting with `REPLACE_` counts as a placeholder.
- The "not configured" rule (spec 4.2): provider `google` with no key path, a missing or unreadable key file, an empty calendar id, or a placeholder id means no polling, `connected == False`, and exactly one WARNING line `Google Calendar not configured: <reason>`.
- With a calendar id configured, only that calendar is read; the primary-calendar fallback is never used for it.
- The room page's ten-minute join window and the control API are unchanged. Nothing joins or leaves by itself.
- Brand for the new guide exactly as the setup guide: tokens navy-900 `#001636`, navy-800 `#16244F`, blue-600 `#1B52E5`, blue-700 `#003EBC`, periwinkle-300 `#BDCEFF`, tint-100 `#EDF2FE`, tint-50 `#F7F9FF`, slate-500 `#647087`, ink-900 `#1C2024`; Lexend with fallback `'Segoe UI', Arial, sans-serif`; kicker labels are the only uppercase text; headlines in sentence case; no external requests from the guide.
- Work on branch `google-calendar` (created from `main` at e4770d1; the spec commit c4218cc is on it). Commit after each task with a `type(scope): summary` message and no attribution trailers.
- Run tests with `.venv/bin/pytest` from the repo root. After every task run `bash /tmp/claude-1000/-home-cpm-ssh/f78b4b20-e6f0-4204-894b-193a8d8a2549/scratchpad/suite-gate.sh <label>` (runs the whole suite minus the stalling upstream v4l2 test and compares with the 87-entry baseline) and confirm `GATE: PASSED` and every test this plan adds passing. Note: the upstream tests in `tests/unit/calendar/test_service.py` classes `TestCalendarEvent`, `TestCalendarService` and `TestCalendarServiceAutoJoin` are among the 87 baseline failures; leave them alone.
- Google client libraries become declared dependencies (`google-api-python-client`, `google-auth`, `google-auth-httplib2`) and are installed into `.venv` in Task 1; tests that need them use `pytest.importorskip("googleapiclient")`.
- The check command is `croom --check-calendar [-c CONFIG]`, exit 0 when the calendar was read, 1 otherwise, output on stdout.

## Review Focus

1. A booking whose link comes from the Zoom for Google Workspace add-on (conference data with a zoom.us entry point) must show as Zoom and be joinable, not be mislabelled Meet. Pinned by Task 2 `test_zoom_addon_booking_is_zoom`.
2. A double-booked slot the room declined must never show the room as booked or joinable. Pinned by Task 1 `test_declined_and_cancelled_bookings_are_dropped` and Task 2 `test_room_declined_booking_is_marked`.
3. A freshly installed device with the placeholder calendar id must log one warning and keep serving the room page, not crash or log every minute. Pinned by Task 1 `test_logs_one_warning_and_never_polls` and Task 4 `test_unfilled_room_configs_never_poll_google`.
4. A valid key with a calendar that was not shared must make the check command say which address to share it with. Pinned by Task 3 `test_calendar_not_shared_names_the_account_to_share_with`.
5. A slow Google API response must never freeze the room page or the sign, which share the event loop with the calendar poll. Pinned by Task 2 `test_a_slow_api_call_does_not_stall_the_loop`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/croom/core/config.py` | `CalendarConfig.google_calendar_id`; both Google fields in `to_dict`. |
| `src/croom/calendar/service.py` | `google_not_configured_reason`, calendar id wiring in `from_config`, the one-warning rule in `initialize`, declined filter in `_fetch_events`. |
| `src/croom/calendar/providers/google.py` | `choose_meeting_url`, worker-thread API calls, `get_calendar`, platform from the link. |
| `src/croom/calendar/check.py` | The check command's logic and words. |
| `src/croom/core/agent.py` | `--check-calendar` flag. |
| `pyproject.toml` | Google client dependencies. |
| `installer/install.sh` | `--credentials FILE`, `install_credentials`, completion line. |
| `deploy/rooms/room-{1,2,3}.yaml`, `deploy/rooms/README.md` | Calendar section with placeholders; the fourth value to replace. |
| `docs/guides/render_guide.py` | Shared HTML-to-PDF renderer for both guides. |
| `docs/guides/crystal-meet-google-calendar/{index.html,build.py,crystalpm-logo-white.svg,fonts/*}`, `docs/guides/crystal-meet-google-calendar.pdf` | The calendar guide. |
| `docs/guides/crystal-meet-room-setup/{build.py,index.html}`, `docs/guides/crystal-meet-room-setup.pdf` | Uses the shared renderer; points at the calendar guide. |
| `tests/unit/calendar/test_google_setup.py` | Config field, not-configured rule, from_config wiring, one warning, declined filter. |
| `tests/unit/calendar/test_google_provider.py` | Recorded event shapes, worker thread, calendar lookup errors. |
| `tests/unit/calendar/test_check.py` | Check command words, exit codes, CLI flag. |
| `tests/unit/calendar/test_service.py` | One existing assertion updated to the new `from_config` dict. |
| `tests/unit/installer/test_install_script.py`, `tests/unit/deploy/test_room_configs.py`, `tests/unit/docs/test_google_calendar_guide.py` | Installer option, room configs, guides. |

---

### Task 1: Config field, calendar id wiring, the not-configured rule and the declined filter

**Files:**
- Modify: `src/croom/core/config.py` (CalendarConfig, `to_dict` calendar section)
- Modify: `src/croom/calendar/service.py` (imports, new module function, `from_config`, `initialize`, `start`, `_fetch_events`)
- Modify: `pyproject.toml` (`[project].dependencies`)
- Modify: `tests/unit/calendar/test_service.py::TestCalendarServiceAsService::test_from_config_google_with_service_account`
- Test: `tests/unit/calendar/test_google_setup.py`

**Interfaces:**
- Consumes: `Config`, `CalendarConfig` (existing dataclasses), `CalendarService` (existing).
- Produces: `CalendarConfig.google_calendar_id: str`; `croom.calendar.service.google_not_configured_reason(calendar: CalendarConfig) -> Optional[str]` (exact strings below; Tasks 3 and 4 use it); `CalendarService.from_config` config dict keys `provider, credentials, calendar_ids, poll_interval, auto_join_minutes, not_configured`.

- [ ] **Step 1: Declare the Google client libraries and install them**

In `pyproject.toml` replace

```toml
dependencies = [
    "pyyaml>=6.0",
    "playwright>=1.40.0",
    "aiohttp>=3.9.0",
    "numpy>=1.24.0",
    "opencv-python-headless>=4.8.0",
    "onnxruntime>=1.16.0",
]
```

with

```toml
dependencies = [
    "pyyaml>=6.0",
    "playwright>=1.40.0",
    "aiohttp>=3.9.0",
    "numpy>=1.24.0",
    "opencv-python-headless>=4.8.0",
    "onnxruntime>=1.16.0",
    "google-api-python-client>=2.100.0",
    "google-auth>=2.23.0",
    "google-auth-httplib2>=0.1.1",
]
```

Run: `.venv/bin/pip install -q -e ".[dev]" && .venv/bin/python -c "import googleapiclient, google.oauth2.service_account; print('ok')"`
Expected: `ok`.

- [ ] **Step 2: Write the failing tests**

Create `tests/unit/calendar/test_google_setup.py`:

```python
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
```

In `tests/unit/calendar/test_service.py` replace the body of `test_from_config_google_with_service_account`'s dict assertion

```python
        assert service.config == {
            "provider": "google",
            "credentials": {"service_account_file": "/etc/croom/google-sa.json"},
            "poll_interval": 120,
            "auto_join_minutes": 3,
        }
```

with

```python
        assert service.config == {
            "provider": "google",
            "credentials": {"service_account_file": "/etc/croom/google-sa.json"},
            "calendar_ids": [],
            "poll_interval": 120,
            "auto_join_minutes": 3,
            "not_configured": "credentials file not found or unreadable: /etc/croom/google-sa.json",
        }
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/calendar/test_google_setup.py tests/unit/calendar/test_service.py -q -p no:cacheprovider -k "google_setup or from_config_google_with_service_account"`
Expected: FAIL: `ImportError: cannot import name 'google_not_configured_reason'` for the new file; the updated existing test fails on the missing `calendar_ids` and `not_configured` keys.

- [ ] **Step 4: Add the config field**

In `src/croom/core/config.py`, in `class CalendarConfig`, after `google_credentials_path: str = ""` add:

```python
    google_calendar_id: str = ""  # the room's calendar address, e.g. c_18...@resource.calendar.google.com
```

In `to_dict` replace

```python
            "calendar": {
                "providers": self.calendar.providers,
                "sync_interval_seconds": self.calendar.sync_interval_seconds,
            },
```

with

```python
            "calendar": {
                "providers": self.calendar.providers,
                "sync_interval_seconds": self.calendar.sync_interval_seconds,
                "google_credentials_path": self.calendar.google_credentials_path,
                "google_calendar_id": self.calendar.google_calendar_id,
            },
```

- [ ] **Step 5: Wire the calendar service**

In `src/croom/calendar/service.py`:

Replace `import asyncio\nimport logging\n` at the top with `import asyncio\nimport logging\nimport os\n`.

After `logger = logging.getLogger(__name__)` add:

```python
PLACEHOLDER_PREFIX = "REPLACE_"


def google_not_configured_reason(calendar) -> Optional[str]:
    """
    Why Google Calendar cannot be read yet, in words for the person setting up
    the room (spec 2026-09-25 section 4.2); None when it can.
    """
    path = calendar.google_credentials_path
    if not path:
        return "no google_credentials_path in the config"
    if not (os.path.isfile(path) and os.access(path, os.R_OK)):
        return f"credentials file not found or unreadable: {path}"
    calendar_id = (calendar.google_calendar_id or "").strip()
    if not calendar_id:
        return "google_calendar_id is empty; put the room's calendar address in the config"
    if calendar_id.startswith(PLACEHOLDER_PREFIX):
        return f"google_calendar_id is still the placeholder {calendar_id}; put the room's calendar address in the config"
    return None
```

Replace the whole `from_config` classmethod with:

```python
    @classmethod
    def from_config(cls, config: Config) -> "CalendarService":
        """Build the service from the agent's Config (specs 2026-09-24 agent startup 4.2, 2026-09-25 calendar 4.2 and 4.3)."""
        calendar = config.calendar
        provider = calendar.providers[0] if calendar.providers else None
        credentials: Dict[str, Any] = {}
        calendar_ids: List[str] = []
        not_configured: Optional[str] = None
        if provider == "google":
            not_configured = google_not_configured_reason(calendar)
            if calendar.google_credentials_path:
                credentials = {"service_account_file": calendar.google_credentials_path}
            if calendar.google_calendar_id:
                calendar_ids = [calendar.google_calendar_id]
        elif provider == "microsoft":
            credentials = {
                "client_id": calendar.microsoft_client_id,
                "tenant_id": calendar.microsoft_tenant_id,
            }
        return cls(config={
            "provider": provider,
            "credentials": credentials,
            "calendar_ids": calendar_ids,
            "poll_interval": calendar.sync_interval_seconds,
            "auto_join_minutes": config.meeting.join_early_minutes,
            "not_configured": not_configured,
        })
```

In `initialize()`, directly after

```python
        if self._initialized:
            return True
```

add

```python
        reason = self.config.get('not_configured')
        if reason:
            logger.warning(f"Google Calendar not configured: {reason}")
            return False
```

In `start()` change `logger.warning("Calendar service running without a provider; polling disabled")` to `logger.info("Calendar service running without a provider; polling disabled")` so the reason line is the only warning.

In `_fetch_events()` replace

```python
                for event in events:
                    # Skip cancelled events
                    if event.status == 'cancelled':
                        continue
                    all_events[event.id] = event
```

with

```python
                for event in events:
                    # Skip cancelled bookings and ones this room declined (double bookings)
                    if event.status == 'cancelled' or event.response_status == 'declined':
                        continue
                    all_events[event.id] = event
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/calendar/test_google_setup.py tests/unit/calendar/test_service.py -q -p no:cacheprovider -k "google_setup or AsService or Connected"`
Expected: all pass (the upstream baseline failures in that file are deselected by `-k`).

- [ ] **Step 7: Run the whole suite** (Global Constraints). Expected: `GATE: PASSED`.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml src/croom/core/config.py src/croom/calendar/service.py tests/unit/calendar/test_google_setup.py tests/unit/calendar/test_service.py
git commit -m "feat(calendar): room calendar id, not-configured rule and declined-booking filter"
```

---

### Task 2: The Google provider reads the right link off the event loop

**Files:**
- Modify: `src/croom/calendar/providers/google.py` (imports, new module function, `authenticate`, `get_calendars`, new `get_calendar`, `get_events`, `_parse_event`)
- Test: `tests/unit/calendar/test_google_provider.py`

**Interfaces:**
- Consumes: `detect_meeting_platform`, `extract_meeting_url`, `MeetingPlatform` from `croom.calendar.providers.base` (existing).
- Produces: `croom.calendar.providers.google.choose_meeting_url(conference_url, typed_url) -> Optional[str]`; `GoogleCalendarProvider.get_calendar(calendar_id) -> Dict[str, str]` with keys `id`, `name`, raising `LookupError` on 403/404 (Task 3 uses it); `get_events` and `authenticate` unchanged in signature.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/calendar/test_google_provider.py`:

```python
"""
The Google Calendar provider against recorded events.list item shapes
(spec 2026-09-25 section 4.3): link and platform detection, the worker
thread, and calendar lookup errors.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("googleapiclient")
import httplib2  # noqa: E402  (dependency of googleapiclient)
from googleapiclient.errors import HttpError  # noqa: E402

from croom.calendar.providers.base import MeetingPlatform  # noqa: E402
from croom.calendar.providers.google import GoogleCalendarProvider, choose_meeting_url  # noqa: E402

ROOM = "c_1885abc@resource.calendar.google.com"


def item(event_id, summary, conference=None, description="", location="", all_day=False,
         response="accepted", status="confirmed"):
    """One item as Google's events.list returns it for a room resource calendar."""
    if all_day:
        start, end = {"date": "2026-09-25"}, {"date": "2026-09-26"}
    else:
        start = {"dateTime": "2026-09-25T09:00:00-05:00", "timeZone": "America/Chicago"}
        end = {"dateTime": "2026-09-25T09:30:00-05:00", "timeZone": "America/Chicago"}
    data = {
        "kind": "calendar#event", "id": event_id, "status": status, "summary": summary,
        "start": start, "end": end, "description": description, "location": location,
        "organizer": {"email": "ben@crystalpm.com"},
        "attendees": [
            {"email": "ben@crystalpm.com", "organizer": True, "responseStatus": "accepted"},
            {"email": ROOM, "self": True, "resource": True, "responseStatus": response},
        ],
    }
    if conference:
        data["conferenceData"] = conference
    return data


MEET = {"conferenceSolution": {"name": "Google Meet"},
        "entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"},
                        {"entryPointType": "phone", "uri": "tel:+1-555-0100"}]}
ZOOM_ADDON = {"conferenceSolution": {"name": "Zoom Meeting"},
              "entryPoints": [{"entryPointType": "video", "uri": "https://us02web.zoom.us/j/98765432100?pwd=abc123"}]}


class FakeCall:
    """A googleapiclient request: called with kwargs, then .execute()."""

    def __init__(self, result=None, error=None, delay=0.0):
        self.result, self.error, self.delay = result, error, delay
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self

    def execute(self):
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


class FakeGoogleService:
    """Stands in for the built API client: events().list(...) and calendars().get(...)."""

    def __init__(self, items=(), calendar=None, list_error=None, get_error=None, delay=0.0):
        self.list = FakeCall({"items": list(items)}, list_error, delay)
        self.get = FakeCall(calendar or {"id": ROOM, "summary": "Room 1"}, get_error)

    def events(self):
        return self

    def calendars(self):
        return self


def provider_with(service):
    provider = GoogleCalendarProvider()
    provider._service = service
    provider._authenticated = True
    return provider


def http_error(status):
    return HttpError(httplib2.Response({"status": status}), b"error")


async def fetch(items):
    provider = provider_with(FakeGoogleService(items))
    now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
    return await provider.get_events(ROOM, now - timedelta(hours=1), now + timedelta(days=7))


class TestLinks:
    async def test_meet_booking(self):
        [event] = await fetch([item("e1", "Design review", conference=MEET)])
        assert event.meeting_url == "https://meet.google.com/abc-defg-hij"
        assert event.meeting_platform == MeetingPlatform.GOOGLE_MEET
        assert event.title == "Design review" and event.calendar_id == ROOM

    async def test_zoom_addon_booking_is_zoom(self):
        [event] = await fetch([item("e2", "Vendor call", conference=ZOOM_ADDON)])
        assert event.meeting_url == "https://us02web.zoom.us/j/98765432100?pwd=abc123"
        assert event.meeting_platform == MeetingPlatform.ZOOM

    async def test_typed_zoom_link_wins_over_automatic_meet(self):
        [event] = await fetch([item("e3", "Board call", conference=MEET,
                                    description="Join Zoom: https://zoom.us/j/12345678901?pwd=xyz")])
        assert event.meeting_url == "https://zoom.us/j/12345678901?pwd=xyz"
        assert event.meeting_platform == MeetingPlatform.ZOOM

    async def test_pasted_zoom_link_in_location(self):
        [event] = await fetch([item("e4", "Interview", location="https://zoom.us/j/55555555555?pwd=q")])
        assert event.meeting_platform == MeetingPlatform.ZOOM

    async def test_all_day_and_linkless_bookings_have_no_link(self):
        events = await fetch([item("e5", "Office closed", all_day=True), item("e6", "Board lunch")])
        assert [e.meeting_url for e in events] == [None, None]
        assert events[0].is_all_day is True

    async def test_room_declined_booking_is_marked(self):
        [event] = await fetch([item("e7", "Double booked", conference=MEET, response="declined")])
        assert event.response_status == "declined"

    def test_choose_meeting_url_rules(self):
        meet = "https://meet.google.com/abc-defg-hij"
        zoom = "https://zoom.us/j/1?pwd=a"
        teams = "https://teams.microsoft.com/l/meetup-join/x"
        assert choose_meeting_url(meet, zoom) == zoom
        assert choose_meeting_url(zoom, meet) == zoom
        assert choose_meeting_url(meet, meet) == meet
        assert choose_meeting_url(None, teams) == teams
        assert choose_meeting_url(zoom, None) == zoom
        assert choose_meeting_url(None, None) is None


class TestApiCalls:
    async def test_events_are_requested_for_the_configured_calendar_only(self):
        service = FakeGoogleService([item("e1", "x", conference=MEET)])
        provider = provider_with(service)
        now = datetime.now(timezone.utc)
        await provider.get_events(ROOM, now, now + timedelta(days=7))
        assert service.list.kwargs["calendarId"] == ROOM
        assert service.list.kwargs["singleEvents"] is True

    async def test_a_slow_api_call_does_not_stall_the_loop(self):
        provider = provider_with(FakeGoogleService([], delay=0.3))
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        now = datetime.now(timezone.utc)
        await provider.get_events(ROOM, now, now + timedelta(days=1))
        task.cancel()
        assert ticks >= 10

    async def test_api_errors_give_no_events(self):
        provider = provider_with(FakeGoogleService([], list_error=http_error(500)))
        now = datetime.now(timezone.utc)
        assert await provider.get_events(ROOM, now, now + timedelta(days=1)) == []

    async def test_get_calendar_returns_name_and_id(self):
        provider = provider_with(FakeGoogleService(calendar={"id": ROOM, "summary": "Room 1"}))
        assert await provider.get_calendar(ROOM) == {"id": ROOM, "name": "Room 1"}

    @pytest.mark.parametrize("status", [403, 404])
    async def test_unshared_or_unknown_calendar_raises_lookup_error(self, status):
        provider = provider_with(FakeGoogleService(get_error=http_error(status)))
        with pytest.raises(LookupError):
            await provider.get_calendar(ROOM)

    async def test_other_calendar_errors_propagate(self):
        provider = provider_with(FakeGoogleService(get_error=http_error(500)))
        with pytest.raises(HttpError):
            await provider.get_calendar(ROOM)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/calendar/test_google_provider.py -q -p no:cacheprovider`
Expected: FAIL: `ImportError: cannot import name 'choose_meeting_url'`.

- [ ] **Step 3: Change the provider**

In `src/croom/calendar/providers/google.py`:

Replace `import logging\nfrom datetime import datetime, timezone\n` with `import asyncio\nimport logging\nfrom datetime import datetime, timezone\n`.

After the `GOOGLE_API_AVAILABLE` try/except block add:

```python
def choose_meeting_url(conference_url: Optional[str], typed_url: Optional[str]) -> Optional[str]:
    """
    The link a booking should join.

    The conference attached to the event wins, except when it is a Google Meet
    (Workspace adds one to every event by default) and someone typed a link for
    another platform into the location or description: that typed link wins.
    """
    if conference_url and typed_url:
        conference_is_meet = detect_meeting_platform(conference_url) == MeetingPlatform.GOOGLE_MEET
        typed_is_other = detect_meeting_platform(typed_url) not in (MeetingPlatform.GOOGLE_MEET, MeetingPlatform.UNKNOWN)
        return typed_url if conference_is_meet and typed_is_other else conference_url
    return conference_url or typed_url
```

Replace the whole `authenticate` method with these two methods:

```python
    async def authenticate(self, credentials: Dict[str, Any]) -> bool:
        """
        Authenticate with the Google Calendar API. The blocking work runs in a
        worker thread so the agent's event loop keeps serving.

        Args:
            credentials: Dict with either:
                - 'service_account_file': Path to service account JSON
                - 'service_account_info': Service account JSON dict
                - 'oauth_token': OAuth2 token info dict
              plus optional 'delegate_email' for domain-wide delegation.

        Returns:
            True if authentication successful
        """
        if not GOOGLE_API_AVAILABLE:
            logger.error("Google API libraries not installed")
            return False
        try:
            await asyncio.to_thread(self._connect, credentials)
        except Exception as e:
            logger.error(f"Google Calendar authentication failed: {e}")
            self._authenticated = False
            return False
        self._authenticated = True
        self._credentials = credentials
        logger.info("Google Calendar authentication successful")
        return True

    def _connect(self, credentials: Dict[str, Any]) -> None:
        """Blocking: build the credentials and the API client, then make one call to prove they work."""
        if 'service_account_file' in credentials:
            self._creds = ServiceAccountCredentials.from_service_account_file(
                credentials['service_account_file'], scopes=self.SCOPES)
        elif 'service_account_info' in credentials:
            self._creds = ServiceAccountCredentials.from_service_account_info(
                credentials['service_account_info'], scopes=self.SCOPES)
        elif 'oauth_token' in credentials:
            token_info = credentials['oauth_token']
            self._creds = Credentials(
                token=token_info.get('access_token'),
                refresh_token=token_info.get('refresh_token'),
                token_uri='https://oauth2.googleapis.com/token',
                client_id=token_info.get('client_id'),
                client_secret=token_info.get('client_secret'),
                scopes=self.SCOPES,
            )
        else:
            raise ValueError("No valid credentials provided")
        if 'delegate_email' in credentials and hasattr(self._creds, 'with_subject'):
            self._creds = self._creds.with_subject(credentials['delegate_email'])
        self._service = build('calendar', 'v3', credentials=self._creds, cache_discovery=False)
        self._service.calendarList().list(maxResults=1).execute()
```

Replace the whole `get_calendars` method with these two methods:

```python
    async def get_calendars(self) -> List[Dict[str, str]]:
        """Get list of available calendars."""
        if not self._authenticated or not self._service:
            return []
        try:
            result = await asyncio.to_thread(lambda: self._service.calendarList().list().execute())
        except HttpError as e:
            logger.error(f"Failed to list calendars: {e}")
            return []
        return [
            {'id': cal['id'], 'name': cal.get('summary', cal['id']), 'primary': cal.get('primary', False)}
            for cal in result.get('items', [])
        ]

    async def get_calendar(self, calendar_id: str) -> Dict[str, str]:
        """
        One calendar's id and name.

        Raises LookupError when Google answers 403 or 404: the calendar does not
        exist or is not shared with this account. Other API errors propagate.
        """
        if not self._service:
            raise RuntimeError("Not authenticated")
        try:
            result = await asyncio.to_thread(
                lambda: self._service.calendars().get(calendarId=calendar_id).execute())
        except HttpError as e:
            status = getattr(e, 'status_code', None) or getattr(getattr(e, 'resp', None), 'status', None)
            if status in (403, 404):
                raise LookupError(calendar_id) from e
            raise
        return {'id': result.get('id', calendar_id), 'name': result.get('summary', calendar_id)}
```

Replace the whole `get_events` method with:

```python
    async def get_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        max_results: int = 100
    ) -> List[CalendarEvent]:
        """Get events from Google Calendar; the API call runs in a worker thread."""
        if not self._authenticated or not self._service:
            return []
        if time_min.tzinfo is None:
            time_min = time_min.replace(tzinfo=timezone.utc)
        if time_max.tzinfo is None:
            time_max = time_max.replace(tzinfo=timezone.utc)
        try:
            result = await asyncio.to_thread(
                lambda: self._service.events().list(
                    calendarId=calendar_id,
                    timeMin=time_min.isoformat(),
                    timeMax=time_max.isoformat(),
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy='startTime',
                ).execute()
            )
        except HttpError as e:
            logger.error(f"Failed to fetch events: {e}")
            return []
        events = []
        for item in result.get('items', []):
            event = self._parse_event(item, calendar_id)
            if event:
                events.append(event)
        logger.debug(f"Fetched {len(events)} events from {calendar_id}")
        return events
```

In `_parse_event` replace everything from `            # Get meeting URL` through the end of the `if not meeting_url:` block (the lines that end with `meeting_platform = detect_meeting_platform(url)`) with:

```python
            # The booking's link: the attached conference (Meet or the Zoom add-on),
            # unless a link for another platform was typed into the event.
            conference_url = None
            for ep in item.get('conferenceData', {}).get('entryPoints', []):
                if ep.get('entryPointType') == 'video' and ep.get('uri'):
                    conference_url = ep['uri']
                    break
            typed_url = extract_meeting_url(item.get('location', '')) or extract_meeting_url(item.get('description', ''))
            meeting_url = choose_meeting_url(conference_url, typed_url)
            meeting_platform = detect_meeting_platform(meeting_url) if meeting_url else MeetingPlatform.UNKNOWN
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/calendar/test_google_provider.py tests/unit/calendar/test_google_setup.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Run the whole suite** (Global Constraints). Expected: `GATE: PASSED`.

- [ ] **Step 6: Commit**

```bash
git add src/croom/calendar/providers/google.py tests/unit/calendar/test_google_provider.py
git commit -m "fix(calendar): Google provider detects the link's platform, reads off the event loop and looks up one calendar"
```

---

### Task 3: The check command

**Files:**
- Create: `src/croom/calendar/check.py`
- Modify: `src/croom/core/agent.py` (`main`)
- Test: `tests/unit/calendar/test_check.py`

**Interfaces:**
- Consumes: `google_not_configured_reason` (Task 1), `GoogleCalendarProvider.authenticate/get_calendar/get_events` (Task 2), `load_config` (existing, already imported in agent.py).
- Produces: `croom.calendar.check.check_calendar(config: Config, out: TextIO = sys.stdout, provider_factory=None) -> int`; `service_account_email(path: str) -> str`; the `croom --check-calendar` flag. Task 4's completion message and Task 5's guide quote the command.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/calendar/test_check.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/calendar/test_check.py -q -p no:cacheprovider`
Expected: FAIL: `ModuleNotFoundError: No module named 'croom.calendar.check'`.

- [ ] **Step 3: Write the check module**

Create `src/croom/calendar/check.py`:

```python
"""
`croom --check-calendar`: prove a room's Google Calendar setup works, in plain
words (spec 2026-09-25 section 4.4). Exit 0 when the calendar was read, 1 otherwise.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, TextIO

from croom.calendar.providers.base import CalendarEvent, MeetingPlatform
from croom.calendar.providers.google import GoogleCalendarProvider
from croom.calendar.service import google_not_configured_reason
from croom.core.config import Config

DAYS_AHEAD = 7
LINK_WORDS = {
    MeetingPlatform.ZOOM: "Zoom link",
    MeetingPlatform.GOOGLE_MEET: "Google Meet link",
    MeetingPlatform.MICROSOFT_TEAMS: "Teams link",
    MeetingPlatform.WEBEX: "Webex link",
}


def service_account_email(path: str) -> str:
    """The client_email inside a service account key file, or '' when it cannot be read."""
    try:
        with open(path, encoding="utf-8") as f:
            return str(json.load(f).get("client_email", ""))
    except (OSError, ValueError, AttributeError):
        return ""


def link_words(event: CalendarEvent) -> str:
    if not event.meeting_url:
        return "no video link"
    return LINK_WORDS.get(event.meeting_platform, "link: " + event.meeting_url)


def booking_line(event: CalendarEvent) -> str:
    start, end = event.start_time.astimezone(), event.end_time.astimezone()
    when = f"{start:%a %b %d}  all day" if event.is_all_day else f"{start:%a %b %d}  {start:%-I:%M %p} to {end:%-I:%M %p}"
    return f"  {when:<32} {event.title:<28} {link_words(event)}"


async def check_calendar(
    config: Config,
    out: TextIO = sys.stdout,
    provider_factory: Optional[Callable[[], GoogleCalendarProvider]] = None,
) -> int:
    """Run the check against the configured Google Calendar and print the outcome."""
    calendar = config.calendar
    provider_name = calendar.providers[0] if calendar.providers else None
    if provider_name != "google":
        print("No Google Calendar configured: set calendar.providers to [google] in the config.", file=out)
        return 1
    reason = google_not_configured_reason(calendar)
    if reason:
        print(f"Google Calendar not configured: {reason}", file=out)
        return 1
    account = service_account_email(calendar.google_credentials_path) or "the service account"
    provider = (provider_factory or GoogleCalendarProvider)()
    if not await provider.authenticate({"service_account_file": calendar.google_credentials_path}):
        print(f"Could not sign in with {calendar.google_credentials_path}: the key file is not a "
              "service account key, or the project's Calendar API is not enabled.", file=out)
        return 1
    print(f"Google Calendar: connected as {account}", file=out)
    try:
        info = await provider.get_calendar(calendar.google_calendar_id)
    except LookupError:
        print(f"Calendar {calendar.google_calendar_id} not found or not shared: in Google Calendar, "
              f"share the room's calendar with {account} (See all event details), and check the "
              "address in the config.", file=out)
        return 1
    print(f"Calendar: {info['name']} ({info['id']})", file=out)
    now = datetime.now(timezone.utc)
    events = await provider.get_events(info["id"], now, now + timedelta(days=DAYS_AHEAD), max_results=10)
    events = sorted((e for e in events if e.status != "cancelled" and e.response_status != "declined"),
                    key=lambda e: e.start_time)
    if not events:
        print(f"No bookings in the next {DAYS_AHEAD} days.", file=out)
    else:
        print(f"Bookings in the next {DAYS_AHEAD} days:", file=out)
        for event in events:
            print(booking_line(event), file=out)
    return 0
```

- [ ] **Step 4: Add the flag to the agent's command line**

In `src/croom/core/agent.py`, in `main()`, after the `--debug` `parser.add_argument(...)` call add:

```python
    parser.add_argument(
        "--check-calendar",
        help="Check the room's Google Calendar setup, print the next bookings and exit",
        action="store_true"
    )
```

and directly after the `logging.basicConfig(...)` call (before `# Run agent`) add:

```python
    if args.check_calendar:
        from croom.calendar.check import check_calendar
        raise SystemExit(asyncio.run(check_calendar(load_config(args.config))))
```

Confirm the module runs as a script: the file must end with

```python
if __name__ == "__main__":
    main()
```

Add those two lines at the end if they are absent (the systemd unit already starts the agent with `python -m croom.core.agent`).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/calendar/test_check.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Try the command on this PC**

Run: `.venv/bin/croom --check-calendar -c deploy/rooms/room-1.yaml; echo "exit $?"`
Expected (Task 4 has not run yet, so the config still has no provider): `No Google Calendar configured: set calendar.providers to [google] in the config.` and `exit 1`. After Task 4 the same command prints the placeholder reason.

- [ ] **Step 7: Run the whole suite** (Global Constraints). Expected: `GATE: PASSED`.

- [ ] **Step 8: Commit**

```bash
git add src/croom/calendar/check.py src/croom/core/agent.py tests/unit/calendar/test_check.py
git commit -m "feat(calendar): croom --check-calendar explains the Google Calendar setup in plain words"
```

---

### Task 4: Installer `--credentials`, the room configs and the rooms README

**Files:**
- Modify: `installer/install.sh` (variables, new `install_credentials`, `main`, `run_installer`, `print_completion`)
- Modify: `deploy/rooms/room-1.yaml`, `deploy/rooms/room-2.yaml`, `deploy/rooms/room-3.yaml`, `deploy/rooms/README.md`
- Test: `tests/unit/installer/test_install_script.py`, `tests/unit/deploy/test_room_configs.py`

**Interfaces:**
- Consumes: `google_not_configured_reason` (Task 1) in the deploy test; the check command name (Task 3) in the completion message.
- Produces: `installer/install.sh --credentials FILE`, function `install_credentials` (sourced by tests), variable `CREDENTIALS_FILE`; the configs' calendar section that Task 5's guide describes.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/installer/test_install_script.py`:

```python


def test_missing_credentials_file_is_refused_before_install(tmp_path):
    result = run_bash(f"bash {SCRIPT} --credentials {tmp_path / 'nope.json'}")
    assert result.returncode == 1
    assert "not found" in (result.stdout + result.stderr).lower()


def test_help_mentions_credentials():
    result = run_bash(f"bash {SCRIPT} --help")
    assert "--credentials FILE" in result.stdout


def test_credentials_are_installed_for_the_service_user_only(tmp_path):
    key = tmp_path / "key.json"
    key.write_text('{"type": "service_account"}')
    result = run_bash(
        f"source {SCRIPT}; CONFIG_DIR={tmp_path / 'etc'}; CROOM_USER=$(id -un); CREDENTIALS_FILE={key}; install_credentials",
    )
    assert result.returncode == 0, result.stderr
    installed = tmp_path / "etc" / "google-service-account.json"
    assert installed.read_text() == key.read_text()
    assert oct(installed.stat().st_mode & 0o777) == "0o600"


def test_install_credentials_does_nothing_without_a_file(tmp_path):
    result = run_bash(f"source {SCRIPT}; CONFIG_DIR={tmp_path / 'etc'}; CROOM_USER=$(id -un); install_credentials")
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "etc" / "google-service-account.json").exists()


def test_completion_message_names_the_calendar_check_when_credentials_were_installed():
    result = run_bash(f"source {SCRIPT}; ROOM_CONFIG=/tmp/room.yaml; CREDENTIALS_FILE=/tmp/key.json; print_completion")
    assert result.returncode == 0, result.stderr
    assert "croom --check-calendar -c /etc/croom/config.yaml" in result.stdout
    result = run_bash(f"source {SCRIPT}; ROOM_CONFIG=/tmp/room.yaml; print_completion")
    assert "--check-calendar" not in result.stdout
```

In `tests/unit/deploy/test_room_configs.py` replace

```python
    assert config.ai.enabled is False
    assert config.calendar.providers == []
```

with

```python
    assert config.ai.enabled is False
    assert config.calendar.providers == ["google"]
    assert config.calendar.google_credentials_path == "/etc/croom/google-service-account.json"
    assert config.calendar.google_calendar_id == "REPLACE_WITH_ROOM_CALENDAR_ID"
```

and append:

```python


@pytest.mark.parametrize("path", ROOMS, ids=lambda p: p.name)
def test_unfilled_room_configs_never_poll_google(path):
    # A device installed with the placeholders left in must log one line and serve the room page,
    # never poll Google (spec 2026-09-25 section 4.2).
    from croom.calendar.service import google_not_configured_reason
    config = Config.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert google_not_configured_reason(config.calendar) is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/installer/test_install_script.py tests/unit/deploy -q -p no:cacheprovider`
Expected: FAIL: the `--credentials` run exits with the root error instead of "not found", `--help` lacks the option, `install_credentials` is not a function, the completion message lacks the check, and the room configs still have `providers: []`.

- [ ] **Step 3: Change the installer**

In `installer/install.sh`:

After `ROOM_CONFIG=""` add `CREDENTIALS_FILE=""`.

Directly before the line `# Write the systemd units (separate from create_service so tests can call it)` add:

```bash
# Install the Google service account key where the agent reads it (calendar spec 4.5)
install_credentials() {
    if [[ -z "$CREDENTIALS_FILE" ]]; then
        return
    fi
    mkdir -p "$CONFIG_DIR"
    cp "$CREDENTIALS_FILE" "$CONFIG_DIR/google-service-account.json"
    chown "$CROOM_USER:$CROOM_USER" "$CONFIG_DIR/google-service-account.json"
    chmod 600 "$CONFIG_DIR/google-service-account.json"
    log "Installed Google Calendar credentials at $CONFIG_DIR/google-service-account.json"
}

```

In `main()` replace `    create_config\n    create_service\n` with `    create_config\n    install_credentials\n    create_service\n`.

In `run_installer()` add this case directly after the `--config)` case block (after its `;;`):

```bash
            --credentials)
                CREDENTIALS_FILE="$2"
                if [[ -z "$CREDENTIALS_FILE" || ! -f "$CREDENTIALS_FILE" ]]; then
                    error "Credentials file not found: ${CREDENTIALS_FILE:-<missing>}"
                fi
                shift 2
                ;;
```

and in the `--help` text, directly after the `--config FILE` line, add:

```bash
                echo "  --credentials FILE  Install a Google service account key as /etc/croom/google-service-account.json"
```

In `print_completion()` replace the final

```bash
    fi
    echo ""
}
```

with

```bash
    fi
    if [[ -n "$CREDENTIALS_FILE" ]]; then
        echo ""
        echo "Check the calendar: $INSTALL_DIR/venv/bin/croom --check-calendar -c $CONFIG_DIR/config.yaml"
    fi
    echo ""
}
```

Run: `bash -n installer/install.sh`
Expected: no output.

- [ ] **Step 4: Change the room configs and README**

In each of `deploy/rooms/room-1.yaml`, `room-2.yaml`, `room-3.yaml` replace the header comment lines

```yaml
# Replace the three REPLACE values, then install with:
#   sudo bash installer/install.sh --config room-N.yaml
```

(N is the room number) with

```yaml
# Replace the four REPLACE values, then install with:
#   sudo bash installer/install.sh --config room-N.yaml --credentials google-service-account.json
```

and replace

```yaml
calendar:
  providers: []
```

with

```yaml
calendar:
  providers: [google]
  google_credentials_path: /etc/croom/google-service-account.json
  google_calendar_id: "REPLACE_WITH_ROOM_CALENDAR_ID"
  sync_interval_seconds: 60
```

Replace the whole `deploy/rooms/README.md` with:

```markdown
# Crystal Meet room devices

One device per conference room, one config per device, one shared dashboard,
one Google service account shared with all three room calendars.

Each `room-N.yaml` is a complete agent config with four things to replace
before installing it on that room's device:

- `room.name` and `room.location`: what people call the room.
- `dashboard.url`: the dashboard backend's address on the office network, for
  example `http://192.168.1.20:3001`. If the dashboard runs under WSL2 on a
  Windows PC, enable mirrored networking or forward ports 3000 and 3001 first.
- `dashboard.enrollment_token`: create it on the dashboard's Provisioning page
  for that room and paste it in. A token works once; if you reinstall, create a
  new one.
- `calendar.google_calendar_id`: the room's Google Calendar address, which looks
  like `c_1885...@resource.calendar.google.com` (the resource email in the
  Admin console). The guide "Connect Crystal Meet rooms to Google Calendar"
  covers creating the rooms, the service account key and sharing.

Install on the device with the room config and the service account key:

    sudo bash installer/install.sh --config /path/to/room-N.yaml --credentials /path/to/google-service-account.json

The key is copied to `/etc/croom/google-service-account.json`, readable only by
the service user. Without `--credentials` the room works with pasted links only
and logs one line saying the calendar is not configured.

The room page is then at `http://<device>:8080/` for anyone on the network,
and the door sign at `http://<device>:8080/sign`. Check the calendar with
`/opt/croom/venv/bin/croom --check-calendar -c /etc/croom/config.yaml`.
Never commit a real token or key to this folder.
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `bash -n installer/install.sh && .venv/bin/pytest tests/unit/installer/test_install_script.py tests/unit/deploy -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: See the check command's words on an unfilled config**

Run: `.venv/bin/croom --check-calendar -c deploy/rooms/room-1.yaml; echo "exit $?"`
Expected: `Google Calendar not configured: credentials file not found or unreadable: /etc/croom/google-service-account.json` and `exit 1` (on a Pi with the key installed it would name the placeholder instead).

- [ ] **Step 7: Run the whole suite** (Global Constraints). Expected: `GATE: PASSED`.

- [ ] **Step 8: Commit**

```bash
git add installer/install.sh deploy tests/unit/installer/test_install_script.py tests/unit/deploy/test_room_configs.py
git commit -m "feat(deploy): installer --credentials and the Google Calendar section in the room configs"
```

---

### Task 5: The Google Calendar guide and the shared renderer

**Files:**
- Create: `docs/guides/render_guide.py`, `docs/guides/crystal-meet-google-calendar/index.html`, `docs/guides/crystal-meet-google-calendar/build.py`, `docs/guides/crystal-meet-google-calendar/crystalpm-logo-white.svg`, `docs/guides/crystal-meet-google-calendar/fonts/{lexend-400.woff2,lexend-600.woff2,OFL.txt}`, `docs/guides/crystal-meet-google-calendar.pdf`, `tests/unit/docs/test_google_calendar_guide.py`
- Modify: `docs/guides/crystal-meet-room-setup/build.py`, `docs/guides/crystal-meet-room-setup/index.html`, `docs/guides/crystal-meet-room-setup.pdf`

**Interfaces:**
- Consumes: the installer option, config values and check command from Tasks 3 and 4 (the guide quotes them exactly: `--credentials`, `google_calendar_id`, `/etc/croom/google-service-account.json`, `/opt/croom/venv/bin/croom --check-calendar -c /etc/croom/config.yaml`).
- Produces: `docs/guides/render_guide.py` with `render(folder: Path, out: Path, footer_title: str) -> None`; both PDFs.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/docs/test_google_calendar_guide.py`:

```python
"""
The Google Calendar guide builds to a multi-page PDF, is self-contained, quotes
the real commands, and the setup guide points at it.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GUIDES = REPO / "docs" / "guides"
GUIDE = GUIDES / "crystal-meet-google-calendar"

pytest.importorskip("playwright.sync_api")


def test_calendar_guide_builds_to_a_multi_page_pdf(tmp_path):
    out = tmp_path / "guide.pdf"
    result = subprocess.run([sys.executable, str(GUIDE / "build.py"), str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = out.read_bytes()
    assert data.startswith(b"%PDF")
    counts = [int(m) for m in re.findall(rb"/Count (\d+)", data)]
    assert counts and max(counts) >= 3, counts


def test_calendar_guide_is_self_contained():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)=["']https?://""", html)
    assert "url(http" not in html and "@import" not in html
    assert "Crystal Meet" in html and "Croom " not in html
    for asset in ("crystalpm-logo-white.svg", "fonts/lexend-400.woff2", "fonts/lexend-600.woff2", "fonts/OFL.txt"):
        assert (GUIDE / asset).is_file(), asset


def test_calendar_guide_quotes_the_real_commands_and_names():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert "--credentials" in html and "google-service-account.json" in html
    assert "croom --check-calendar -c /etc/croom/config.yaml" in html
    assert "google_calendar_id" in html and "See all event details" in html


def test_setup_guide_points_to_the_calendar_guide():
    html = (GUIDES / "crystal-meet-room-setup" / "index.html").read_text(encoding="utf-8")
    assert "Connect Crystal Meet rooms to Google Calendar" in html


def test_both_guides_share_one_renderer():
    for guide in ("crystal-meet-room-setup", "crystal-meet-google-calendar"):
        assert "from render_guide import render" in (GUIDES / guide / "build.py").read_text(encoding="utf-8")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/docs -q -p no:cacheprovider`
Expected: the five new tests FAIL (no folder, no renderer, no pointer); the two existing setup-guide tests still pass.

- [ ] **Step 3: Add the shared renderer and switch the setup guide to it**

Create `docs/guides/render_guide.py`:

```python
"""
Render a Crystal Meet guide folder (index.html plus its assets) to PDF with the
venv's Chromium. Each guide's build.py calls render() with its own footer title.
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

ASSETS = ("index.html", "crystalpm-logo-white.svg", "fonts/lexend-400.woff2", "fonts/lexend-600.woff2")


def footer(title: str) -> str:
    return f"""
<div style="width:100%;font-family:Lexend,'Segoe UI',Arial,sans-serif;font-size:7.5pt;color:#647087;
            padding:0 0.6in;display:flex;justify-content:space-between;">
  <span>{title}</span>
  <span>Page <span class="pageNumber"></span></span>
</div>
"""


def render(folder: Path, out: Path, footer_title: str) -> None:
    for asset in ASSETS:
        if not (folder / asset).is_file():
            raise SystemExit(f"missing asset: {asset}")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto((folder / "index.html").as_uri(), wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        page.pdf(
            path=str(out),
            format="Letter",
            print_background=True,
            display_header_footer=True,
            header_template="<span></span>",
            footer_template=footer(footer_title),
            margin={"top": "0.6in", "bottom": "0.7in", "left": "0.6in", "right": "0.6in"},
        )
        browser.close()
    print(f"wrote {out} ({out.stat().st_size} bytes)")
```

Replace the whole `docs/guides/crystal-meet-room-setup/build.py` with:

```python
"""
Render the Crystal Meet room setup guide to PDF.

Usage: python build.py [output.pdf]   (default: ../crystal-meet-room-setup.pdf)
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from render_guide import render  # noqa: E402

if __name__ == "__main__":
    render(HERE, Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "crystal-meet-room-setup.pdf",
           "How-to guide · Set up a Crystal Meet room")
```

In `docs/guides/crystal-meet-room-setup/index.html`, in step 4's list, directly after the bullet that starts with `<li><strong>Leave.</strong>` add:

```html
  <li><strong>Connect the calendar.</strong> To show real bookings and let people join them, follow the guide <span class="ui">Connect Crystal Meet rooms to Google Calendar</span> next. Until then the room page works with pasted links only.</li>
```

- [ ] **Step 4: Create the calendar guide**

```bash
G=docs/guides/crystal-meet-google-calendar
mkdir -p "$G/fonts"
cp docs/guides/crystal-meet-room-setup/crystalpm-logo-white.svg "$G/"
cp docs/guides/crystal-meet-room-setup/fonts/lexend-400.woff2 docs/guides/crystal-meet-room-setup/fonts/lexend-600.woff2 docs/guides/crystal-meet-room-setup/fonts/OFL.txt "$G/fonts/"
```

Create `docs/guides/crystal-meet-google-calendar/build.py`:

```python
"""
Render the Google Calendar guide to PDF.

Usage: python build.py [output.pdf]   (default: ../crystal-meet-google-calendar.pdf)
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from render_guide import render  # noqa: E402

if __name__ == "__main__":
    render(HERE, Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "crystal-meet-google-calendar.pdf",
           "How-to guide · Connect Crystal Meet rooms to Google Calendar")
```

Create `docs/guides/crystal-meet-google-calendar/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Connect Crystal Meet rooms to Google Calendar</title>
<style>
@font-face { font-family: "Lexend"; src: url("fonts/lexend-400.woff2") format("woff2"); font-weight: 400; }
@font-face { font-family: "Lexend"; src: url("fonts/lexend-600.woff2") format("woff2"); font-weight: 600; }
:root {
  --navy-900: #001636; --navy-800: #16244F; --blue-600: #1B52E5; --blue-700: #003EBC;
  --periwinkle-300: #BDCEFF; --tint-100: #EDF2FE; --tint-50: #F7F9FF; --slate-500: #647087; --ink-900: #1C2024;
  --radius: 12px; --radius-sm: 8px;
}
@page { size: Letter; }
* { box-sizing: border-box; }
body { margin: 0; font-family: "Lexend", "Segoe UI", Arial, sans-serif; color: var(--ink-900); font-size: 10.5pt; line-height: 1.5; }
h1, h2, h3 { font-weight: 600; line-height: 1.15; margin: 0.2em 0; }
h2 { font-size: 16pt; color: var(--navy-800); }
p { margin: 0.4em 0; }
.kicker { font-size: 9pt; font-weight: 600; letter-spacing: 0.15em; text-transform: uppercase; color: var(--blue-600); margin: 0 0 6px; }
.banner { background: var(--navy-800); color: #fff; border-radius: var(--radius); padding: 28px 32px; margin-bottom: 20px; }
.banner .kicker { color: var(--periwinkle-300); }
.banner .kicker.top { color: #fff; margin-bottom: 2px; }
.banner h1 { font-size: 26pt; margin: 6px 0 8px; }
.banner p { color: var(--periwinkle-300); font-size: 11pt; margin: 0; max-width: 60ch; }
.banner img { height: 26px; display: block; margin-bottom: 18px; }
.intro { font-size: 11pt; margin: 0 0 18px; max-width: 78ch; }
.glance { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin: 0 0 22px; }
.glance .card { border-top: 4px solid var(--blue-600); padding: 12px 12px; }
.glance .card:nth-child(2) { border-top-color: #3B6DF0; }
.glance .card:nth-child(3) { border-top-color: #6C92F5; }
.glance .card:nth-child(4) { border-top-color: #8EABF8; }
.glance .card:nth-child(5) { border-top-color: var(--periwinkle-300); }
.card { border-radius: var(--radius); padding: 14px 16px; background: #fff; border: 1px solid var(--tint-100); }
.card .kicker { font-size: 8pt; }
.card h3 { font-size: 11pt; margin: 2px 0 4px; }
.card p { margin: 0; color: var(--slate-500); font-size: 9.5pt; }
.block { break-inside: avoid; }
.step { display: flex; align-items: center; gap: 12px; margin: 22px 0 8px; }
.badge { width: 30px; height: 30px; border-radius: 8px; background: var(--blue-600); color: #fff; font-weight: 600; display: inline-flex; align-items: center; justify-content: center; font-size: 12pt; }
ul { margin: 6px 0 10px; padding-left: 0; list-style: none; }
li { position: relative; padding-left: 18px; margin: 5px 0; }
li::before { content: ""; position: absolute; left: 2px; top: 0.6em; width: 7px; height: 7px; border-radius: 50%; background: var(--blue-600); }
.ui { font-weight: 600; color: var(--blue-600); }
.chip { font-weight: 600; color: var(--blue-600); background: var(--tint-100); border-radius: 6px; padding: 1px 8px; white-space: nowrap; }
.chip.wrap { white-space: normal; overflow-wrap: anywhere; }
.see { color: var(--slate-500); font-size: 9.5pt; margin: 4px 0 0 18px; }
.callout { border-radius: var(--radius-sm); padding: 12px 18px; margin: 12px 0; border-left: 4px solid var(--blue-600); background: var(--tint-100); break-inside: avoid; }
.callout.warn { background: #FFF8E1; border-left-color: #E8A013; }
.callout.tip { background: #EDFBF3; border-left-color: #1FA971; }
.callout .kicker { margin-bottom: 4px; }
.callout p { margin: 2px 0; }
.trouble { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.trouble .card { break-inside: avoid; }
.trouble .card:last-child:nth-child(odd) { grid-column: 1 / -1; }
.trouble .card h3 { font-size: 10.5pt; }
.page-break { break-before: page; }
pre { font: 600 9.5pt/1.5 "Lexend", "Segoe UI", Arial, sans-serif; color: var(--blue-600); background: var(--tint-100); border-radius: var(--radius-sm); padding: 10px 14px; margin: 6px 0 0 18px; white-space: pre-wrap; }
</style>
</head>
<body>

<section class="banner">
  <img src="crystalpm-logo-white.svg" alt="Crystal PM">
  <p class="kicker top">Crystal PM</p>
  <p class="kicker">How-to guide</p>
  <h1>Connect Crystal Meet rooms to Google Calendar</h1>
  <p>Give each room device read access to its own Google Workspace room calendar, so bookings show on the room page and the door sign and Join now joins them.</p>
</section>

<p class="intro">This guide has two halves. The Google half is done once for all rooms by someone with Google Workspace admin access and takes about 30 minutes. The device half takes about five minutes per room. Nothing here makes a room join a meeting by itself: someone in the room still presses Join now.</p>

<div class="glance">
  <div class="card"><p class="kicker">Step 1</p><h3>Create the rooms</h3><p>Room resources in the Admin console.</p></div>
  <div class="card"><p class="kicker">Step 2</p><h3>Create the key</h3><p>A service account and its JSON key.</p></div>
  <div class="card"><p class="kicker">Step 3</p><h3>Share the calendars</h3><p>Each room calendar with the account.</p></div>
  <div class="card"><p class="kicker">Step 4</p><h3>Put it on the device</h3><p>The key, the address, the check.</p></div>
  <div class="card"><p class="kicker">Step 5</p><h3>Book a test meeting</h3><p>See it on the room page and sign.</p></div>
</div>

<div class="callout warn">
  <p class="kicker">Before you begin</p>
  <p><strong>Access.</strong> A Google Workspace super admin, or someone who can manage Buildings and resources and create a Google Cloud project.</p>
  <p><strong>Devices.</strong> The room devices installed as in <span class="ui">Set up a Crystal Meet room</span>, and their room names.</p>
  <p><strong>A way to copy one file to each device</strong>, such as <span class="chip">scp</span> from your computer or a USB stick.</p>
  <p><strong>The key is a password for the room calendars.</strong> Keep it out of email and chat, copy it straight to the devices, and delete the downloaded copy afterwards.</p>
</div>

<section class="block">
<div class="step"><span class="badge">1</span><h2>Create the rooms in Google Workspace</h2></div>
<ul>
  <li><strong>Open the Admin console</strong> at <span class="chip">admin.google.com</span>, then <span class="ui">Directory › Buildings and resources › Manage resources</span>.</li>
  <li><strong>Add the building once.</strong> Open <span class="ui">Buildings</span>, press <span class="ui">Add building</span>, fill in the name, address and floors, and save.</li>
  <li><strong>Add each room.</strong> Open <span class="ui">Resources</span>, press <span class="ui">Add new resource</span>, choose category <span class="ui">Meeting space (room)</span> and type <span class="ui">Conference room</span>, pick the building and floor, and set the resource name to the same name the device uses, for example <span class="chip">Room 1</span>. Save, then repeat for Room 2 and Room 3.</li>
  <li><strong>Copy each room's calendar address.</strong> Open the room and copy its <span class="ui">Resource email</span>. It looks like <span class="chip wrap">c_1885a3b9c2d4e6f7g8h9i0j1k2l3m4n5@resource.calendar.google.com</span>. Keep the three addresses; step 4 needs them.</li>
</ul>
<p class="see">You should now see the three rooms under Resources, each with a resource email.</p>
<div class="callout">
  <p class="kicker">Good to know</p>
  <p>Staff book a room by adding it to an event under <span class="ui">Rooms</span>. New rooms can take up to 24 hours to appear in that list, but the room's calendar exists straight away, so you can continue.</p>
</div>
</section>

<section class="block">
<div class="step"><span class="badge">2</span><h2>Create the service account and its key</h2></div>
<ul>
  <li><strong>Open the Cloud console</strong> at <span class="chip">console.cloud.google.com</span>, open the project picker at the top, press <span class="ui">New project</span>, name it <span class="chip">Crystal Meet</span> and create it. Make sure it is selected.</li>
  <li><strong>Turn on the Calendar API.</strong> Go to <span class="ui">APIs &amp; Services › Library</span>, search for <span class="ui">Google Calendar API</span>, open it and press <span class="ui">Enable</span>.</li>
  <li><strong>Create the account.</strong> Go to <span class="ui">IAM &amp; Admin › Service accounts</span>, press <span class="ui">Create service account</span>, name it <span class="chip">crystal-meet-rooms</span>, press <span class="ui">Create and continue</span>, skip the roles, and press <span class="ui">Done</span>.</li>
  <li><strong>Create the key.</strong> Open the account, go to <span class="ui">Keys</span>, press <span class="ui">Add key › Create new key</span>, choose <span class="ui">JSON</span> and create it. A file downloads; rename it <span class="chip">google-service-account.json</span>.</li>
  <li><strong>Copy the account's email.</strong> It ends in <span class="chip">.iam.gserviceaccount.com</span>. Step 3 needs it.</li>
</ul>
<p class="see">You should now see the service account listed, and the key file on your computer.</p>
</section>

<section class="block">
<div class="step"><span class="badge">3</span><h2>Share each room calendar with the service account</h2></div>
<ul>
  <li><strong>Allow full sharing to outside addresses, once.</strong> In the Admin console go to <span class="ui">Apps › Google Workspace › Calendar › Sharing settings</span>. Under <span class="ui">External sharing options for secondary calendars</span> choose <span class="ui">Share all information, but outsiders cannot change calendars</span> and save. Room calendars count as secondary calendars, and the service account is an outside address.</li>
  <li><strong>Subscribe to the rooms.</strong> Open <span class="chip">calendar.google.com</span>. Next to <span class="ui">Other calendars</span> press <span class="ui">+</span>, choose <span class="ui">Browse resources</span> and tick the three rooms. They appear under Other calendars.</li>
  <li><strong>Share each room.</strong> Hover the room's name, open its menu, choose <span class="ui">Settings and sharing</span>, then under <span class="ui">Share with specific people or groups</span> press <span class="ui">Add people and groups</span>, paste the service account email, set the permission to <span class="ui">See all event details</span>, and send. Repeat for each room.</li>
</ul>
<p class="see">You should now see the service account listed under each room's sharing settings with See all event details.</p>
</section>

<section class="block">
<div class="step"><span class="badge">4</span><h2>Put the key and the calendar address on each device</h2></div>
<ul>
  <li><strong>Copy the key to the device.</strong> From your computer, type <span class="chip wrap">scp google-service-account.json pi@crystal-meet-room-1.local:~/</span> (use the device's name or IP address and your username), or carry it over on a USB stick.</li>
  <li><strong>A device you are installing now.</strong> Before running the installer, open <span class="chip">~/room.yaml</span> and put the room's calendar address in <span class="ui">google_calendar_id</span>, keeping the quotes. Then run <span class="chip wrap">sudo bash installer/install.sh --config ~/room.yaml --credentials ~/google-service-account.json</span> and start the service as the setup guide says.</li>
  <li><strong>A device that is already installed.</strong> Type <span class="chip wrap">sudo install -o pi -g pi -m 600 ~/google-service-account.json /etc/croom/google-service-account.json</span> (your username instead of pi), then <span class="chip">sudo nano /etc/croom/config.yaml</span>, set <span class="ui">google_calendar_id</span> to the room's address, save, and type <span class="chip">sudo systemctl restart croom</span>.</li>
  <li><strong>Check it.</strong> Type <span class="chip wrap">/opt/croom/venv/bin/croom --check-calendar -c /etc/croom/config.yaml</span>. It prints who it connected as, the room's calendar, and the next bookings:</li>
</ul>
<pre>Google Calendar: connected as crystal-meet-rooms@crystal-meet.iam.gserviceaccount.com
Calendar: Room 1 (c_1885a3b9c2d4e6f7g8h9i0j1k2l3m4n5@resource.calendar.google.com)
Bookings in the next 7 days:
  Thu Sep 25  9:00 AM to 9:30 AM   Design review                Zoom link
  Thu Sep 25  1:00 PM to 2:00 PM   Board lunch                  no video link</pre>
<ul>
  <li><strong>Delete the copy in your home folder.</strong> Type <span class="chip">rm ~/google-service-account.json</span>. The installed copy under <span class="chip">/etc/croom</span> is the one the room uses.</li>
</ul>
<p class="see">You should now see the check end with the room's name and its bookings, or "No bookings in the next 7 days", and the room page's schedule fills within a minute.</p>
</section>

<section class="block">
<div class="step"><span class="badge">5</span><h2>Book a test meeting</h2></div>
<ul>
  <li><strong>Create the booking.</strong> In Google Calendar, make an event a few minutes from now, add the room under <span class="ui">Rooms</span>, and add a video link: a Google Meet, the Zoom add-on, or a pasted Zoom invitation link with its passcode. Save it.</li>
  <li><strong>Watch the room.</strong> Within a minute the room page lists it under <span class="ui">Today</span> and the door sign shows it as next. Ten minutes before the start the sign turns amber and the room page shows <span class="ui">Join now</span>.</li>
  <li><strong>Join and leave.</strong> Press <span class="ui">Join now</span>; the TV joins the link. Press <span class="ui">Leave</span>, then <span class="ui">Tap again to leave</span>.</li>
</ul>
<p class="see">You should now see the booking on the room page and the sign, and the TV join when Join now is pressed.</p>
<div class="callout tip">
  <p class="kicker">Tip</p>
  <p>If the room declines a booking (the event shows the room as declined), the slot was already taken by another booking. The device ignores declined bookings, so the sign stays right.</p>
</div>
</section>

<div class="page-break"></div>
<section class="block">
<p class="kicker">If something is off</p>
<h2>Troubleshooting</h2>
<div class="trouble">
  <div class="card"><h3>The check says "not configured"</h3><p>It names the cause: no key path, a missing key file, or an empty or placeholder address. Check that <span class="chip">/etc/croom/google-service-account.json</span> exists, fix <span class="chip">/etc/croom/config.yaml</span>, then restart the service and run the check again.</p></div>
  <div class="card"><h3>The check says "not found or not shared"</h3><p>The address is wrong or step 3 was missed for this room. Copy the resource email again and share the room calendar with the exact service account email. If Google refuses "See all event details", do the sharing setting at the start of step 3 first.</p></div>
  <div class="card"><h3>The check says it could not sign in</h3><p>The file is not a service account JSON key (download a new one from the account's Keys page), the Calendar API is not enabled in that project, or the key was deleted in the Cloud console.</p></div>
  <div class="card"><h3>A booking is missing on the room page</h3><p>The room was not added to the event, the room declined it, or it is more than 7 days away. Cancelled bookings never show. Check the device's clock and time zone if today's bookings look shifted.</p></div>
  <div class="card"><h3>The booking shows but Join now is missing</h3><p>The event has no video link the device can join. Add a Google Meet, use the Zoom add-on, or paste the full Zoom invitation link including its passcode part.</p></div>
  <div class="card"><h3>Bookings show at the wrong time</h3><p>Set the device's time zone in <span class="ui">Preferences › Raspberry Pi Configuration › Localisation</span>, then type <span class="chip">sudo systemctl restart croom</span>.</p></div>
</div>
</section>
<div class="callout">
  <p class="kicker">Good to know</p>
  <p>The key can only read the calendars you shared with it. To take that away, delete the key on the account's Keys page. To replace it, create a new key and repeat step 4 on each device.</p>
</div>

</body>
</html>
```

- [ ] **Step 5: Build both PDFs and run the tests**

Run: `.venv/bin/python docs/guides/crystal-meet-google-calendar/build.py && .venv/bin/python docs/guides/crystal-meet-room-setup/build.py && .venv/bin/pytest tests/unit/docs -q -p no:cacheprovider`
Expected: two `wrote ...` lines and all docs tests pass.

- [ ] **Step 6: Look at the calendar guide**

Render each page to an image (pymupdf, 96 dpi) and check: kicker labels present, the five glance cards on one row and readable, no orphaned step headers, the check output block readable, six troubleshooting cards, footer with page numbers on every page, headlines in sentence case. Fix and rebuild before committing.

- [ ] **Step 7: Run the whole suite** (Global Constraints). Expected: `GATE: PASSED`.

- [ ] **Step 8: Commit**

```bash
git add docs/guides/render_guide.py docs/guides/crystal-meet-google-calendar docs/guides/crystal-meet-google-calendar.pdf docs/guides/crystal-meet-room-setup docs/guides/crystal-meet-room-setup.pdf tests/unit/docs/test_google_calendar_guide.py
git commit -m "docs: Google Calendar guide for Crystal Meet rooms, with one shared renderer"
```

---

### Task 6: Push and final check

**Files:** none new.

- [ ] **Step 1: Confirm nothing user-facing says Croom and no secret slipped in**

Run: `grep -rn "Croom" deploy docs/guides/crystal-meet-google-calendar/index.html docs/guides/crystal-meet-room-setup/index.html src/croom/calendar/check.py; grep -rln "private_key" deploy docs src`
Expected: no output from either grep.

- [ ] **Step 2: Run the whole suite** (Global Constraints). Expected: `GATE: PASSED`.

- [ ] **Step 3: Push**

```bash
git push -u origin google-calendar
```
