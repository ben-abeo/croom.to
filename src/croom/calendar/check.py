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
