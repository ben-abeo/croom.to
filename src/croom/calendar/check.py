"""
`croom --check-calendar`: prove a room's Google Calendar setup works, in plain
words (spec 2026-09-25 section 4.4): the calendar is read, and the room can book in it
(spec 2026-10-25). Exit 0 when both are so, 1 otherwise. It also says what the room has
booked at its screens that has still to reach Google, and what is double-booked.
"""

import sys
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, TextIO

from croom.calendar.bookings import RoomBookings
from croom.calendar.providers.base import CalendarEvent, CalendarWriteRefused, MeetingPlatform
from croom.calendar.providers.google import GoogleCalendarProvider
from croom.calendar.service import google_not_configured_reason, service_account_email
from croom.core.config import Config

DAYS_AHEAD = 7
LINK_WORDS = {
    MeetingPlatform.ZOOM: "Zoom link",
    MeetingPlatform.GOOGLE_MEET: "Google Meet link",
    MeetingPlatform.MICROSOFT_TEAMS: "Teams link",
    MeetingPlatform.WEBEX: "Webex link",
}


def link_words(event: CalendarEvent) -> str:
    if not event.meeting_url:
        return "no video link"
    return LINK_WORDS.get(event.meeting_platform, "link: " + event.meeting_url)


def when_words(event: CalendarEvent) -> str:
    start, end = event.start_time.astimezone(), event.end_time.astimezone()
    return f"{start:%a %b %d}  all day" if event.is_all_day else f"{start:%a %b %d}  {start:%-I:%M %p} to {end:%-I:%M %p}"


def booking_line(event: CalendarEvent) -> str:
    return f"  {when_words(event):<32} {event.title:<28} {link_words(event)}"


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
    calendar_id = calendar.google_calendar_id.strip()
    try:
        info = await provider.get_calendar(calendar_id)
    except LookupError:
        print(f"Calendar {calendar_id} not found or not shared: in Google Calendar, "
              f"share the room's calendar with {account} (Make changes to events), and check the "
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
    code = 0
    try:
        await provider.check_booking(info["id"])
        print("Bookings: the door sign and the room page can book this calendar.", file=out)
    except CalendarWriteRefused:
        print(f"Bookings: the calendar is shared with {account} for reading only, so the door sign and the room page "
              "cannot book it. In Google Calendar, share it with Make changes to events.", file=out)
        code = 1
    report_room_bookings(config, out)
    return code


def report_room_bookings(config: Config, out: TextIO) -> None:
    """What the room booked at its screens and has still to send to Google (the agent sends it on its next poll), and its bookings
    that overlap another, as the device last had them."""
    data_dir = config.resolve_data_dir()
    if not (data_dir / "bookings.json").exists():
        return
    rooms = RoomBookings(data_dir / "bookings.json", data_dir / "calendar-cache.json", config.room.name)
    waiting = rooms.state()["pending"]
    if waiting:
        print(f"Waiting to be sent to Google: {waiting} booking{'s' if waiting != 1 else ''} made at the room's screens.", file=out)
    for event in rooms.merge():
        if event.clash and event.end_time > datetime.now(timezone.utc):
            print(f"Double-booked: {event.title}, {' '.join(when_words(event).split())}, overlaps {event.clash}.", file=out)
