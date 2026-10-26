"""
Booking the room from its screens (spec 2026-10-25) as the room guide, the calendar guide and the README tell it: the hours, the steps
and the longest booking are the code's own, End now is the room page's alone, and the files the room keeps are named as they are.
"""

import re
from html import unescape
from pathlib import Path

from croom.calendar import bookings
from croom.core.config import CalendarConfig

REPO = Path(__file__).resolve().parents[3]
ROOM_GUIDE = REPO / "docs" / "guides" / "crystal-meet-room-setup" / "index.html"
CALENDAR_GUIDE = REPO / "docs" / "guides" / "crystal-meet-google-calendar" / "index.html"
README = REPO / "README.md"


def text(path):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", path.read_text(encoding="utf-8"))).split())


def test_the_room_guide_gives_the_codes_rules():
    t = text(ROOM_GUIDE)
    assert CalendarConfig().booking_start == "08:00" and CalendarConfig().booking_end == "18:00"
    assert "every 15 minutes from 8:00 AM to the last start before 6:00 PM" in t
    assert bookings.STEP_MINUTES == 15 and bookings.MAX_MINUTES == 240 and bookings.BOOK_DAYS == 7
    assert "15 minutes to 4 hours, in steps of 15" in t and "today and the six days after it" in t
    assert "never on the door sign" in t and "Tap again to end" in t
    assert "bookings.json" in t and "calendar-cache.json" in t and "Double-booked with" in t
    assert "booking_start" in t and "booking_end" in t


def test_the_calendar_guide_shares_the_rooms_for_changes():
    t = text(CALENDAR_GUIDE)
    assert "Make changes to events" in t and "Share all information, and outsiders can change calendars" in t
    assert "for reading only" in t   # what the check says of a room still shared for reading


def test_the_readme_names_the_settings_and_the_files():
    t = README.read_text(encoding="utf-8")
    for needle in ("booking_start", "booking_end", "bookings.json", "calendar-cache.json", "**Use now**", "**Book room**", "Make changes to events"):
        assert needle in t, needle
