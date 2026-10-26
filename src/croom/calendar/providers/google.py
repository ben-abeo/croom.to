"""
Google Calendar provider.

Uses Google Calendar API to fetch events and meeting information.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from croom.calendar.providers.base import (
    CalendarProvider,
    CalendarEvent,
    CalendarWriteRefused,
    MeetingPlatform,
    detect_meeting_platform,
    extract_meeting_url,
    invitation_passcode,
)

logger = logging.getLogger(__name__)

# Google API dependencies
try:
    from google.oauth2.credentials import Credentials
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    GOOGLE_API_AVAILABLE = True
except ImportError:
    GOOGLE_API_AVAILABLE = False

# The private extended property that marks a booking made at the room's screens, with the room's id for it.
BOOKING_TAG = "croom_id"
# 403s that are Google asking the room to slow down, not a calendar that will not take the write.
BUSY_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded"}


def _status(error) -> Optional[int]:
    return getattr(error, 'status_code', None) or getattr(getattr(error, 'resp', None), 'status', None)


def _refusal(error) -> bool:
    """A 401, or a 403 that is not a rate limit: the calendar is not shared for changes with this account."""
    status = _status(error)
    if status == 401:
        return True
    if status != 403:
        return False
    try:
        reasons = {e.get('reason') for e in json.loads(error.content).get('error', {}).get('errors', [])}
    except (ValueError, AttributeError, TypeError):
        reasons = set()
    return not (reasons & BUSY_REASONS)


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


class GoogleCalendarProvider(CalendarProvider):
    """
    Google Calendar provider.

    Supports OAuth2 user credentials and service account authentication.
    """

    # API scopes needed: reading the room's calendar, and writing the bookings made at its screens (spec 2026-10-25)
    SCOPES = [
        'https://www.googleapis.com/auth/calendar.readonly',
        'https://www.googleapis.com/auth/calendar.events',
    ]

    def __init__(self):
        super().__init__()
        self._service = None
        self._creds = None

    @property
    def name(self) -> str:
        return "google"

    @property
    def display_name(self) -> str:
        return "Google Calendar"

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

    async def refresh_auth(self) -> bool:
        """Refresh authentication tokens."""
        if not self._creds:
            return False

        try:
            if self._creds.expired and self._creds.refresh_token:
                self._creds.refresh(Request())
                logger.info("Google Calendar tokens refreshed")
                return True
            return True
        except Exception as e:
            logger.error(f"Failed to refresh Google tokens: {e}")
            return False

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

    async def get_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        max_results: int = 100
    ) -> List[CalendarEvent]:
        """Get events from Google Calendar; the API call runs in a worker thread. API errors propagate."""
        if not self._authenticated or not self._service:
            return []
        if time_min.tzinfo is None:
            time_min = time_min.replace(tzinfo=timezone.utc)
        if time_max.tzinfo is None:
            time_max = time_max.replace(tzinfo=timezone.utc)
        # Errors propagate: the calendar service keeps its last good list and reports the problem.
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
        events = []
        for item in result.get('items', []):
            event = self._parse_event(item, calendar_id)
            if event:
                events.append(event)
        logger.debug(f"Fetched {len(events)} events from {calendar_id}")
        return events

    # The room's own bookings (spec 2026-10-25), written straight into the room's calendar: nobody is invited and there is no link.
    # Each carries the room's tag in a private extended property, which the room reads back to know its own. Errors propagate: a
    # calendar that will not take the write is CalendarWriteRefused, anything else is Google out of reach, and the booking waits.

    async def _write(self, call):
        try:
            return await asyncio.to_thread(lambda: call().execute())
        except HttpError as e:
            if _refusal(e):
                raise CalendarWriteRefused(str(e)) from e
            raise

    async def find_booking(self, calendar_id: str, booking_id: str) -> Optional[str]:
        """The Google id of the event that carries the room's tag, or None."""
        result = await asyncio.to_thread(lambda: self._service.events().list(
            calendarId=calendar_id, privateExtendedProperty=f"{BOOKING_TAG}={booking_id}", maxResults=1, showDeleted=False,
        ).execute())
        items = result.get('items', [])
        return items[0]['id'] if items else None

    async def create_booking(self, calendar_id: str, booking_id: str, title: str, start: datetime, end: datetime,
                             description: str) -> str:
        body = {
            'summary': title,
            'description': description,
            'start': {'dateTime': start.isoformat()},
            'end': {'dateTime': end.isoformat()},
            'extendedProperties': {'private': {BOOKING_TAG: booking_id}},
        }
        result = await self._write(lambda: self._service.events().insert(calendarId=calendar_id, body=body))
        return result['id']

    async def update_booking(self, calendar_id: str, google_id: str, title: str, start: datetime, end: datetime) -> None:
        body = {'summary': title, 'start': {'dateTime': start.isoformat()}, 'end': {'dateTime': end.isoformat()}}
        await self._write(lambda: self._service.events().patch(calendarId=calendar_id, eventId=google_id, body=body))

    async def check_booking(self, calendar_id: str) -> None:
        """Prove the room can book in its calendar (croom --check-calendar): a quarter hour on January 1, 2000, written and
        deleted at once, out of sight of any week anyone looks at. CalendarWriteRefused when the calendar is shared for reading."""
        start = datetime(2000, 1, 1, tzinfo=timezone.utc)
        google_id = await self.create_booking(calendar_id, "check", "Crystal Meet booking check", start,
                                              start.replace(minute=15), "Written and deleted by croom --check-calendar")
        await self.delete_booking(calendar_id, google_id)

    async def delete_booking(self, calendar_id: str, google_id: str) -> None:
        try:
            await self._write(lambda: self._service.events().delete(calendarId=calendar_id, eventId=google_id))
        except HttpError as e:
            if _status(e) not in (404, 410):   # already gone: cancelled all the same
                raise

    def _parse_event(self, item: Dict, calendar_id: str) -> Optional[CalendarEvent]:
        """Parse Google Calendar API event to CalendarEvent."""
        try:
            # Get start/end times
            start = item.get('start', {})
            end = item.get('end', {})

            # Handle all-day events
            is_all_day = 'date' in start
            if is_all_day:
                # Google gives dates only; make them aware in the device's zone so they sort with timed events
                local_tz = datetime.now().astimezone().tzinfo
                start_time = datetime.fromisoformat(start['date']).replace(tzinfo=local_tz)
                end_time = datetime.fromisoformat(end['date']).replace(tzinfo=local_tz)
            else:
                start_time = datetime.fromisoformat(
                    start.get('dateTime', '').replace('Z', '+00:00')
                )
                end_time = datetime.fromisoformat(
                    end.get('dateTime', '').replace('Z', '+00:00')
                )

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

            # Get organizer
            organizer = item.get('organizer', {}).get('email', '')

            # Get attendees
            attendees = [
                a.get('email', '')
                for a in item.get('attendees', [])
            ]

            # Get response status for this calendar
            response_status = 'accepted'
            for a in item.get('attendees', []):
                if a.get('self'):
                    response_status = a.get('responseStatus', 'accepted')
                    break

            event = CalendarEvent(
                id=item['id'],
                title=item.get('summary', 'No Title'),
                start_time=start_time,
                end_time=end_time,
                meeting_url=meeting_url,
                meeting_platform=meeting_platform,
                meeting_passcode=invitation_passcode(
                    meeting_url, meeting_platform, item.get('location', ''), item.get('description', '')
                ),
                organizer=organizer,
                description=item.get('description', ''),
                location=item.get('location', ''),
                calendar_id=calendar_id,
                is_all_day=is_all_day,
                is_recurring=bool(item.get('recurringEventId')),
                recurrence_id=item.get('recurringEventId'),
                status=item.get('status', 'confirmed'),
                attendees=attendees,
                response_status=response_status,
                booking_id=item.get('extendedProperties', {}).get('private', {}).get(BOOKING_TAG),
            )

            return event

        except Exception as e:
            logger.error(f"Failed to parse event: {e}")
            return None


# OAuth2 helper for initial setup
class GoogleOAuthHelper:
    """Helper class for OAuth2 authentication flow."""

    SCOPES = GoogleCalendarProvider.SCOPES

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret

    def get_auth_url(self, redirect_uri: str = 'urn:ietf:wg:oauth:2.0:oob') -> str:
        """Get OAuth2 authorization URL."""
        if not GOOGLE_API_AVAILABLE:
            raise RuntimeError("Google API libraries not installed")

        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(
            {
                'installed': {
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
                    'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                    'token_uri': 'https://oauth2.googleapis.com/token',
                }
            },
            scopes=self.SCOPES,
            redirect_uri=redirect_uri
        )

        auth_url, _ = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            prompt='consent'
        )

        return auth_url

    def exchange_code(
        self,
        code: str,
        redirect_uri: str = 'urn:ietf:wg:oauth:2.0:oob'
    ) -> Dict[str, Any]:
        """Exchange authorization code for tokens."""
        if not GOOGLE_API_AVAILABLE:
            raise RuntimeError("Google API libraries not installed")

        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(
            {
                'installed': {
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
                    'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                    'token_uri': 'https://oauth2.googleapis.com/token',
                }
            },
            scopes=self.SCOPES,
            redirect_uri=redirect_uri
        )

        flow.fetch_token(code=code)
        creds = flow.credentials

        return {
            'access_token': creds.token,
            'refresh_token': creds.refresh_token,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'expiry': creds.expiry.isoformat() if creds.expiry else None,
        }
