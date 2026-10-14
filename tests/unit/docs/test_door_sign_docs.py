"""
The redesigned door sign (spec 2026-10-13) in the docs outside the README and the room guide: the spec's own words for the two
rulings and for what the scoreboard was built to, the Zoom Marketplace page and test plan that describe the sign, and the
installer's stock config.
"""

import math
import re
from html import unescape
from pathlib import Path

from croom.scores.espn import SCHEDULE_URL, SCHOOLS, TOP_RANK
from croom.scores.service import BACKOFF, CHECK_S, INTERVAL_S, RETRY_S

REPO = Path(__file__).resolve().parents[3]
SPEC = REPO / "docs" / "superpowers" / "specs" / "2026-10-13-door-sign-design.md"
MARKETPLACE = REPO / "deploy" / "zoom-marketplace"
GUIDE = REPO / "docs" / "guides" / "crystal-meet-room-setup" / "index.html"
README = REPO / "README.md"
SIGN = REPO / "src" / "croom" / "control" / "static"


def visible_text(html):
    """The page as a reader sees it: tags dropped, entities decoded, white space collapsed."""
    return " ".join(unescape(re.sub(r"<[^>]+>", "", html)).split())


def squash(css):
    """A stylesheet's text with its comments gone, its white space made one space and none beside punctuation: a stylesheet laid out again says the same
    to a needle that is squashed the same way."""
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    return re.sub(r"\s*([{};:,()])\s*", r"\1", re.sub(r"\s+", " ", css)).strip()


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


def test_the_spec_says_what_a_long_title_and_a_long_room_name_do():
    """The layout's two rules for text that is too long for a sign, as built after the task 5 review."""
    text = " ".join(SPEC.read_text(encoding="utf-8").split())
    for needle in (
        "A name of more than 24 characters, or one that would take more than two lines at 5.4 rem",
        "(more than one on a sign 560 px high or less; \"Sales Conference Room\" takes three on a 1280 px sign)",
        "On a sign 560 px high or less (the Raspberry Pi's 7-inch 800 by 480 panel is one) the chip says only the status, without the booking's title, "
        "which the Today list shows beside it (\"Starts in 8 minutes\", \"Starting now\").",
        "is set in smaller type, 3.6 rem at most, so it is never cut and a short sign keeps its chip",
        "the lines are measured on the sign after layout, and again when the name, the sign's size or the view changes and when a font arrives",
        "a title cut after two lines with an ellipsis (after one on a sign 560 px high or less, and the week's bookings after two, one in portrait)",
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


WORDS = {1: "one", 2: "two", 3: "three", 5: "five", 8: "eight", 10: "ten", 20: "twenty", 30: "thirty"}


def js_constant(name):
    """A `const NAME = 30 * 1000;` of sign.js, as the number it is."""
    expression = re.search(rf"const {name} = ([\d* ]+);", (SIGN / "sign.js").read_text(encoding="utf-8")).group(1)
    return math.prod(int(factor) for factor in expression.split("*"))


def test_the_docs_state_the_numbers_the_code_has():
    """The numbers the docs give for the sign are read from the code they describe, and each must be where the docs say it: the schools' ids and the feed's
    address in the spec and the guide, how often the scores are read and how they are tried again, how often the strip turns, how long the week and a
    photograph wait, where the strip stacks, which rank counts, and where the config file is."""
    spec, readme = (" ".join(path.read_text(encoding="utf-8").split()) for path in (SPEC, README))      # markdown: white space is all there is to undo
    guide = visible_text(GUIDE.read_text(encoding="utf-8"))
    css = squash((SIGN / "sign.css").read_text(encoding="utf-8"))
    installer = (REPO / "installer" / "install.sh").read_text(encoding="utf-8")
    minutes = INTERVAL_S // 60
    waits = [RETRY_S * step for step in BACKOFF]                                          # 60, 120, 300 and 600 seconds
    stacked_below = int(re.search(r"@media\(max-width:(\d+)px\)\{\.score\{flex-direction:column", css).group(1)) + 1
    soon_minutes = js_constant("SOON_MS") // 60000
    config = re.search(r'^CONFIG_DIR="([^"]+)"', installer, re.M).group(1) + "/config.yaml"
    short_sign = re.search(r'matchMedia\("\(max-height: (\d+)px\)"\)', (SIGN / "sign.js").read_text(encoding="utf-8")).group(1)
    assert f"@media(max-height:{short_sign}px){{" in css                                   # the script's short sign is the stylesheet's
    large = re.search(r"\.room\{[^}]*font-size:clamp\([^,]+,[^,]+,([\d.]+)rem\)", css).group(1)           # the room name's largest type, in rem
    smaller = re.search(r"\.room\.long\{font-size:clamp\([^,]+,[^,]+,([\d.]+)rem\)", css).group(1)
    assert INTERVAL_S % 60 == 0 and all(wait % 60 == 0 for wait in waits[1:])
    facts = (
        ("the schools and their ids in the feed", spec, ", ".join(f"{name} {team}" for team, name in SCHOOLS.values())),
        ("the feed's address", spec, SCHEDULE_URL.removeprefix("https://").format(team_id="<id>")),
        ("the feed's address for Texas, in the check", guide, SCHEDULE_URL.format(team_id=SCHOOLS["texas"][0])),
        ("how often the scores are read", spec, f"every {WORDS[minutes]} minutes"),
        ("how often the scores are read", guide, f"every {WORDS[minutes]} minutes"),
        ("how often the scores are read", readme, f"every {WORDS[minutes]} minutes"),
        ("how soon a school that is turned on is read", spec, f"read within {CHECK_S} seconds"),
        ("how failed reads are tried again", spec,
         f"retried after {waits[0]} seconds, {waits[1] // 60} minutes and {waits[2] // 60} minutes and then every {waits[3] // 60} minutes"),
        ("how failed reads are tried again", guide, f"after {waits[0] // 60}, {waits[1] // 60} and {waits[2] // 60} minutes and then every {waits[3] // 60}"),
        ("how often the strip turns to the next school", spec, f"every {js_constant('SCORE_EVERY_MS') // 1000} seconds"),
        ("how often the strip turns to the next school", guide, f"every {js_constant('SCORE_EVERY_MS') // 1000} seconds"),
        ("where the strip's two sides stack", spec, f"below {stacked_below} px"),
        ("where the strip's two sides stack", guide, f"narrower than {stacked_below} pixels"),
        ("the ranks that show", guide, f"the Top {TOP_RANK}"),
        ("how long the week waits to go back", spec, f"{js_constant('WEEK_IDLE_MS') // 1000} idle seconds"),
        ("how long the week waits to go back", guide, f"{WORDS[js_constant('WEEK_IDLE_MS') // 1000]} seconds without a tap"),
        ("how long a photograph is waited for", spec, f"{WORDS[js_constant('PHOTO_TIMEOUT_MS') // 1000]} seconds"),
        ("how long a photograph is waited for", guide, f"{js_constant('PHOTO_TIMEOUT_MS') // 1000} seconds"),
        ("how many bookings Today lists", spec, f"the next {WORDS[js_constant('TODAY_ROWS')]} bookings"),
        ("how many bookings Today lists", guide, f"next {WORDS[js_constant('TODAY_ROWS')]} bookings"),
        ("when a booking is about to start", guide, f"the {WORDS[soon_minutes]} minutes before a booking"),
        ("where the config file is", guide, config),
        ("what a short sign is", spec, f"{short_sign} px high or less"),
        ("what a short sign is", guide, f"{short_sign} pixels high or less"),
        ("the length of a room name that takes the smaller type", guide, f"as one of more than {js_constant('LONG_NAME')} characters is"),
        ("how many lines a room name may take in the large type", guide, f"would take more than {WORDS[js_constant('NAME_LINES')]} lines"),
        ("the length of a room name that takes the smaller type", spec, f"A name of more than {js_constant('LONG_NAME')} characters"),
        ("how many lines a room name may take in the large type", spec, f"more than {WORDS[js_constant('NAME_LINES')]} lines at {large} rem"),
        ("the room name's smaller type", spec, f"{smaller} rem at most"),
    )
    for what, text, phrase in facts:
        assert phrase in text, (what, phrase)
