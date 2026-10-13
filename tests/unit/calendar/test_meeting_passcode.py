"""
The passcode written in an invitation ("Passcode: 123456" under a Zoom link): the
parser, and the Microsoft provider that fills CalendarEvent.meeting_passcode from
a booking's text when its Zoom link carries no pwd of its own.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest

from croom.calendar.providers.base import CalendarEvent, MeetingPlatform, extract_meeting_passcode
from croom.calendar.providers.microsoft import MicrosoftCalendarProvider

INVITATION = "Join Zoom Meeting https://zoom.us/j/5590000559 Meeting ID: 559 000 0559 Passcode: 940104"
PLAIN_TEXT_INVITATION = """Ben is inviting you to a scheduled Zoom meeting.

Join Zoom Meeting
https://us02web.zoom.us/j/5590000559

Meeting ID: 559 000 0559
Passcode: 940104
---
One tap mobile
+13052224171,,5590000559#,,,,*940104# US
"""
HTML_INVITATION = (
    'Join Zoom Meeting<br><a href="https://zoom.us/j/5590000559">https://zoom.us/j/5590000559</a>'
    "<br><br>Meeting ID: 559 000 0559<br>Passcode:&nbsp;940104<br>"
)


class TestExtractMeetingPasscode:
    def test_a_zoom_invitation(self):
        assert extract_meeting_passcode(INVITATION) == "940104"

    def test_a_plain_text_invitation_with_its_own_lines(self):
        assert extract_meeting_passcode(PLAIN_TEXT_INVITATION) == "940104"

    def test_an_html_description(self):
        assert extract_meeting_passcode(HTML_INVITATION) == "940104"

    @pytest.mark.parametrize("text, expected", [
        ("Password: abc123", "abc123"),
        ("Passcode: 123456", "123456"),
        ("Meeting passcode 123456", "123456"),
        ("Passcode 123456", "123456"),
        ("PASSCODE: 123456", "123456"),
        ("meeting PassWord:123456", "123456"),
        ("Passcode:   123456", "123456"),
        ("Passcode: AbCd1234", "AbCd1234"),
        ("Passcode: a-b_c*9@", "a-b_c*9@"),                    # the characters Zoom allows besides letters and digits
        ("Meeting passcode: a-b_c*9@ (case sensitive)", "a-b_c*9@"),
        ("Passcode: 940104.", "940104"),                        # a sentence's full stop is not part of it
        ("Passcode: 940104 and more", "940104"),                # a space ends it
        ("Passcode: 940104/ext", "940104"),                     # so does a slash
    ])
    def test_the_forms_zoom_and_people_write(self, text, expected):
        assert extract_meeting_passcode(text) == expected

    def test_the_first_match_wins(self):
        assert extract_meeting_passcode("Passcode: 111111\nPassword: 222222") == "111111"

    def test_the_link_is_not_a_source(self):
        link = "Join https://us02web.zoom.us/j/5590000559?pwd=VmxPYWJjZGVmMTIz"
        assert extract_meeting_passcode(link) is None
        assert extract_meeting_passcode(link + " Meeting ID: 559 000 0559 Passcode: 940104") == "940104"

    @pytest.mark.parametrize("text", [
        "https://zoom.us/j/5590000559?passcode=940104",     # a query parameter is not written text
        "Meeting ID: 559 000 0559",
        "Passcode: 123",                                     # shorter than 4
        "Passcode: " + "1" * 33,                             # longer than 32
        "Passcode: ******",                                  # a masked passcode is not one
        "Passcode: ______",
        "Passcode: ----",
        "Passcode: 12 3456",                                 # a space ends it after 2 characters
        "Passcode: 12/3456",                                 # so does a slash
        "Passcode: 12.3456",
        "Passcode:",
        "",
        None,
    ])
    def test_no_passcode_found(self, text):
        assert extract_meeting_passcode(text) is None

    def test_the_longest_passcode_is_32_characters(self):
        assert extract_meeting_passcode("Passcode: " + "a1" * 16) == "a1" * 16

    def test_long_runs_after_the_label_are_read_in_linear_time(self):
        # Invitations come from outside the room: a label followed by a long run must not make parsing crawl.
        started = time.perf_counter()
        assert extract_meeting_passcode("Passcode" + " " * 30000) is None
        assert extract_meeting_passcode("Passcode:" + " \t" * 15000 + "940104") == "940104"
        assert extract_meeting_passcode("Passcode: " + "*" * 30000) is None
        assert extract_meeting_passcode("Passcode: " + "a-" * 15000) is None
        assert time.perf_counter() - started < 1.0


def graph_event(body="", location="", join_url=None, online_url=None):
    """One item as Microsoft Graph returns it (bodyPreview is the plain-text start of the invitation)."""
    data = {
        "id": "m1", "subject": "Vendor call", "isCancelled": False, "isAllDay": False,
        "start": {"dateTime": "2026-09-25T14:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-25T14:30:00", "timeZone": "UTC"},
        "bodyPreview": body, "location": {"displayName": location},
        "organizer": {"emailAddress": {"address": "ben@crystalpm.com"}},
    }
    if join_url:
        data["onlineMeeting"] = {"joinUrl": join_url}
    if online_url:
        data["onlineMeetingUrl"] = online_url
    return data


class TestCalendarEvent:
    def test_an_event_has_no_passcode_until_one_is_read(self):
        start = datetime(2026, 9, 25, 9, tzinfo=timezone.utc)
        event = CalendarEvent(id="e1", title="x", start_time=start, end_time=start + timedelta(minutes=30))
        assert event.meeting_passcode is None

    def test_the_passcode_stays_out_of_the_serialised_event(self):
        start = datetime(2026, 9, 25, 9, tzinfo=timezone.utc)
        event = CalendarEvent(id="e1", title="x", start_time=start, end_time=start + timedelta(minutes=30),
                              meeting_passcode="940104")
        assert "940104" not in str(event.to_dict())


class TestMicrosoftProvider:
    def parse(self, **fields):
        return MicrosoftCalendarProvider()._parse_event(graph_event(**fields), "cal")

    def test_zoom_link_without_pwd_takes_the_passcode_from_the_body(self):
        event = self.parse(body=INVITATION)
        assert event.meeting_platform == MeetingPlatform.ZOOM
        assert event.meeting_url == "https://zoom.us/j/5590000559"
        assert event.meeting_passcode == "940104"

    def test_the_location_counts_too(self):
        event = self.parse(body="Join https://zoom.us/j/5590000559", location="Passcode: 940104")
        assert event.meeting_passcode == "940104"

    def test_a_link_with_its_own_pwd_needs_none(self):
        event = self.parse(body="Join https://zoom.us/j/5590000559?pwd=abc123 Passcode: 940104")
        assert event.meeting_url.endswith("?pwd=abc123")
        assert event.meeting_passcode is None

    def test_teams_bookings_never_get_one(self):
        event = self.parse(body="Passcode: 940104", join_url="https://teams.microsoft.com/l/meetup-join/abc")
        assert event.meeting_platform == MeetingPlatform.MICROSOFT_TEAMS
        assert event.meeting_passcode is None

    def test_a_zoom_link_with_no_passcode_written_has_none(self):
        assert self.parse(body="Join https://zoom.us/j/5590000559").meeting_passcode is None
