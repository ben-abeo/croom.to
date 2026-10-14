"""
The redesigned door sign (spec 2026-10-13) in the docs outside the README and the room guide: the spec's own words for the two
rulings and for what the scoreboard was built to, the Zoom Marketplace page and test plan that describe the sign, and the
installer's stock config.
"""

import re
from html import unescape
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SPEC = REPO / "docs" / "superpowers" / "specs" / "2026-10-13-door-sign-design.md"
MARKETPLACE = REPO / "deploy" / "zoom-marketplace"


def visible_text(html):
    """The page as a reader sees it: tags dropped, entities decoded, white space collapsed."""
    return " ".join(unescape(re.sub(r"<[^>]+>", "", html)).split())


def test_the_spec_says_the_week_is_seven_days_across_in_landscape_and_rows_in_portrait():
    """The week view's layout as it was built and ruled (Task 3): the days do not fit inside the Today list's column."""
    text = SPEC.read_text(encoding="utf-8")
    assert "seven days across the width in landscape, as rows in portrait" in text
    assert "seven columns" not in text
    for needle in (
        "fetched once when the view opens",
        "each booking on the day it starts (an all-day booking shows 12:00 AM)",
        '"+N more" (the sign never scrolls)',
    ):
        assert needle in text, needle


def test_the_spec_says_a_fetch_failure_keeps_the_last_answer_and_only_nothing_held_hides_the_strip():
    """Ruled in Task 4: section 4 of the spec wins over the old sentence, which had a fetch failure hide the strip."""
    text = SPEC.read_text(encoding="utf-8")
    assert "a fetch failure keeps the last answer on it: only nothing held (no school read yet, or every school off) hides the strip" in text
    assert "No data, a fetch failure or every school off hide the strip" not in text


def test_the_spec_carries_what_the_scoreboard_was_built_to():
    text = SPEC.read_text(encoding="utf-8")
    for needle in (
        '"school_home": true',                                                    # the last game says whether the school was at home
        '"neutral": false',                                                       # and the next one whether it is at a neutral site
        'its own side first whether it played at home or away ("Texas (#4) 45 – Texas A&M 42", and "Texas (#1) 24 – Oklahoma 27" for a road loss)',
        'as the visitor @ the host ("Next: 10/19 · Texas (#4) @ Missouri (#9)", with "vs" in place of "@" at a neutral site',
        "each team named by ESPN's `location` (\"Texas\", \"Texas A&M\")",
        "a school turned on is read within 2 seconds and shows at the sign's next poll",
        "so the Pi needs outbound HTTPS to `site.api.espn.com` and nothing else",
        "The strip never wraps: each side is one line, clipped with an ellipsis where it does not fit",
        "set in 1.1 rem (17.6 px on a 1280 px sign)",
        "the final score never shrinking (the next game gives way first)",
        "failed reads are retried after 60 seconds, 2 minutes and 5 minutes and then every 10 minutes, starting over at the first success",
        "a WARNING when a school starts to fail, a change of reason and the recovery at INFO",
        "no `scores` key when none is",
    ):
        assert needle in text, needle


def test_the_marketplace_page_describes_the_sign_as_it_is_now():
    """The door sign paragraph of the Zoom Marketplace page: the status in a bar, a label and a ring, in words, not a coloured screen."""
    html = (MARKETPLACE / "docs" / "index.html").read_text(encoding="utf-8")
    text = visible_text(html)
    for needle in (
        "The sign by the door shows the room's name and its status, in words and in colour: a bar down the left edge, a label under the room's name and a ring round the booking in progress.",
        "They are green with Free until and the time of the next booking, amber in the ten minutes before a booking (for example Customer call starts in 8 minutes), "
        "and red while a booking is in progress (Booked until 2:00 PM) or while the room is in a meeting (In use until 2:00 PM).",
        "shows the time, the date and the practice's name over a photograph of Austin, and a This week button opens the next seven days of bookings.",
        "The sign only displays: it never joins or leaves a meeting.",
    ):
        assert needle in text, needle
    for gone in ("green background", "Available", "Starting soon"):
        assert gone not in text, gone


def test_the_marketplace_test_plan_says_what_the_sign_shows_while_in_a_meeting_and_after_it():
    text = (MARKETPLACE / "test-plan.md").read_text(encoding="utf-8")
    for needle in (
        "On the door sign the bar down the left edge and the label under the room's name turn red, and the label reads \"In use\" "
        "(followed by \"until\" and the booking's end time when the meeting is a booking).",
        "for example \"Room free\" on the room page and, on the door sign, a green bar and label reading \"Free until 2:00 PM\".",
    ):
        assert needle in text, needle
    assert 'The door sign turns red: "In use".' not in text
    assert '"Room free" and "Available"' not in text


def test_the_installers_stock_config_explains_the_company_line():
    text = (REPO / "installer" / "install.sh").read_text(encoding="utf-8")
    assert '  # The practice\'s name beside the logo on the door sign; "" shows the logo alone.\n  company: "Crystal Practice Management"' in text
