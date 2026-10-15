"""
The setup guide builds to a multi-page PDF with the venv's Chromium.
"""

import re
import subprocess
import sys
from html import unescape
from pathlib import Path

import pytest

from tests.unit.docs.test_door_sign_docs import squash

REPO = Path(__file__).resolve().parents[3]
GUIDE = REPO / "docs" / "guides" / "crystal-meet-room-setup"

pytest.importorskip("playwright.sync_api")


def test_guide_builds_to_a_multi_page_pdf(tmp_path):
    out = tmp_path / "guide.pdf"
    result = subprocess.run([sys.executable, str(GUIDE / "build.py"), str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = out.read_bytes()
    assert data.startswith(b"%PDF")
    counts = [int(m) for m in re.findall(rb"/Count (\d+)", data)]
    assert counts and max(counts) >= 3, counts


def test_guide_source_is_self_contained():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    # The guide prints the GitHub clone address as text, so check what the
    # browser would load rather than any https:// at all.
    assert not re.search(r"""(src|href)=["']https?://""", html)
    assert "url(http" not in html and "@import" not in html and "http://fonts" not in html
    assert "Crystal Meet" in html and "Croom " not in html
    for asset in ("crystalpm-logo-white.svg", "fonts/lexend-400.woff2", "fonts/lexend-600.woff2", "fonts/OFL.txt"):
        assert (GUIDE / asset).is_file(), asset


def test_guide_covers_the_time_zone_and_the_browser_check():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert "time zone" in html
    assert "No browser window" in html and "journalctl -u croom" in html


def test_guide_sends_the_reader_to_the_dashboard_guide_for_the_address():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert "Set up the Crystal Meet dashboard" in html
    assert "mirrored networking" not in html and "port 3000" not in html


def test_guide_sends_the_reader_to_the_meet_guide_for_the_sign_in():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert "Let Crystal Meet rooms join Google Meet" in html
    assert "joins as a guest" not in html
    assert "stop the service first" in html


def test_guide_describes_the_tv_screensaver():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert "screensaver" in html and "Screensaver" in html
    assert "leave it open" not in html  # nobody touches the TV's browser any more


def test_guide_sets_up_the_table_pi_and_names_the_desktop_icons():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert "install-kiosk.sh --url" in html
    assert "Room controls" in html and "Start Crystal Meet" in html
    assert "on-screen keyboard" in html
    # the scale lives in the kiosk command, and nothing relaunches the browser: a table Pi set up before needs both steps again
    assert "pull the repository on the table Pi too, run the install-kiosk command again and reboot it so the new scale takes effect" in html


def test_guide_sets_up_the_camera_and_the_sound():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert "Find the stops" in html and "Save as Home" in html and "Mute speaker" in html
    assert "Devices the browser sees" in html
    # what the page shows while the camera moves, and in place of a panel whose device is missing
    assert "Moving…" in html and "No controllable camera found" in html and "No speaker found" in html
    assert "audio.output_device: HDMI" in html
    assert "returns to the idle screen after ten minutes" in html   # the Camera framing screen does not keep the TV on the camera


def test_guide_says_what_to_do_when_the_browser_cannot_see_the_meetup():
    """The check starts its own Chromium, so a restart of the service does not answer a device missing from its list."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert "wpctl status" in html and "the browser cannot reach PipeWire" in html
    assert "libpulse0" in html and "the installer adds it" in html and "the desktop user" in html


def test_guide_describes_the_screens_and_the_options_button():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    # "meeting screen" is the table's screen during a call; the TV is "the room's TV". The exact phrases keep the needle from
    # being satisfied by a sentence that means the TV.
    for needle in ("Options", "Camera framing", "Screensaver", "Upcoming meetings", "the page switches to the meeting screen",
                   "the page shows the meeting screen on its own", "ten minutes", "Back", "The room's TV needs no keyboard or mouse"):
        assert needle in html, needle
    assert "room's meeting screen" not in html
    assert "TV when idle" not in html


def test_guide_lets_step_four_flow_across_pages_so_its_tip_is_not_left_alone():
    """Step 4 is longer than the room left on its page. Kept whole it sat on a page of its own and its Tip fell onto the
    next page, alone (7 pages); allowed to break between bullets it runs on from page 3 and ends with its Tip on page 4 (6 pages)."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert re.search(r'<section class="block flow">\s*<div class="step"><span class="badge">4</span>', html)
    assert ".block.flow { break-inside: auto; }" in html
    assert re.search(r"\.step \{[^}]*break-after: avoid;", html)    # a step's heading stays with its first bullet
    assert re.search(r"\bli \{[^}]*break-inside: avoid;", html)     # and no bullet is split


def test_guide_says_a_passcode_field_appears_for_a_zoom_meeting_id():
    """The test-call step and the card for a join that never happens name the Passcode field; neither says the passcode must be in the link."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    text = " ".join(unescape(re.sub(r"<[^>]+>", "", html)).split())
    for needle in (
        "A meeting ID, or a Zoom link without its passcode, makes a Passcode field appear under the box",
        "type the meeting's passcode there before you press Join",
        "on the table screen, tapping the field opens the on-screen keyboard on its digits",
        "type the passcode in the Passcode field that appears for a meeting ID or a link without one",
        "press Join again",
    ):
        assert needle in text, needle
    assert "Zoom needs the passcode inside the link" not in text


def visible_text(html):
    """The page as a reader sees it: tags dropped, entities decoded, white space collapsed."""
    return " ".join(unescape(re.sub(r"<[^>]+>", "", html)).split())


def test_guide_says_what_the_door_sign_shows_and_how_its_status_reads():
    """The door sign section says what is on the screen and gives the status three ways, with the words each state puts under the room's name."""
    text = visible_text((GUIDE / "index.html").read_text(encoding="utf-8"))
    for needle in (
        "A photograph of Austin fills the screen under a navy overlay.",
        "the practice's name beside the logo, the time and the date, the room's name and where it is, and today's next three bookings",
        "a coloured bar down the left edge, a coloured label under the room's name that says it in words, and a ring round the booking in progress",
        "Green is free (Free until 1:00 PM). Amber is the ten minutes before a booking (Customer call starts in 8 minutes).",
        "Red is in use or booked (In use until 2:00 PM while the room is in a meeting, Booked until 2:00 PM while a booking is under way).",
        "On a screen 560 pixels high or less the label before a booking leaves out the booking's title and says Starts in 8 minutes: Today shows the title beside it.",
        "The photograph itself is never tinted.",
        "A sign that cannot reach the device goes grey and says Sign not connected.",
        "To show another, type sudo nano /etc/croom/config.yaml and look under room:. If there is a company: line, change the name inside its quotes; if not, "
        "add a line under name: that begins with two spaces, company: \"Lone Star Eye Care\", so that it lines up with the lines around it. "
        "Save, and type sudo systemctl restart croom. A company: \"\" line leaves the logo on its own.",
        # step 4 points at the section, and no longer says the whole screen turns a colour
        "with a bar down the left edge and a label under the room's name that are green when the room is free, amber when a meeting is about to start, and red while the room is in use or booked",
        "the door sign's bar and label green",
    ):
        assert needle in text, needle
    assert "It shows green when the room is free, amber when a meeting is about to start, and red while the room is in use or booked." not in text
    # a stock room's config has no company line, and a line added at the wrong depth is not part of room: (the first wording said "add a company line under room:")
    assert "add a company line under room: (for example company: \"Lone Star Eye Care\")" not in text
    assert "the door sign green" not in text


def test_guide_names_the_nine_backgrounds_and_how_to_choose_one_on_the_table_screen():
    text = visible_text((GUIDE / "index.html").read_text(encoding="utf-8"))
    for needle in (
        "On the table screen tap Options, then Door sign. Under Door sign background are nine buttons: Austin skyline (the default), Capitol, UT Tower, "
        "Pennybacker Bridge, Hamilton Pool, Texas star, Hill Country, DKR stadium and Plain, which leaves the navy on its own.",
        "The door sign changes within a few seconds. The choice is kept on the device, so it survives a restart, and every door sign of the room follows it.",
        "If the sign is plain navy where a photograph should be, check first that Plain is not the one in use.",
        "A photograph is loaded when the sign opens, so a first load, or a reload, shows navy for a moment (about a third of a second, a second or more on a slow connection).",
        "A photograph that has not loaded after 20 seconds is given up on, and the sign stays navy, with a soft glow in the status colour in a corner, until the page is reloaded "
        "or the sign has lost the device and found it again.",
    ):
        assert needle in text, needle


def test_guide_describes_the_week_view():
    text = visible_text((GUIDE / "index.html").read_text(encoding="utf-8"))
    for needle in (
        "Tap This week beside today's bookings. The sign shows the next seven days, today first, each with its name, its date and its bookings "
        "(the time, the title and Zoom or Google Meet); a day with nothing booked says Nothing booked.",
        "The seven days run across the screen, or one under another on a sign standing upright, where a booking's title is cut after one line.",
        "On a screen lying down and 700 pixels high or less a booking is its time and one line of title, "
        "and on one narrower than 1000 pixels the days are named by three letters (Wed).",
        "A booking is listed once, on the day it starts, and an all-day booking shows 12:00 AM.",
        "The sign never scrolls: where a day has more bookings than fit, it shows the first ones and +3 more.",
        "The week is read from the device when you open it, so open it again to see a change.",
        "Back, or thirty seconds without a tap, returns to Today.",
        "If the week cannot be read the sign says The week could not be loaded. and Back still works.",
        "the door screen needs to be a touch screen to use it",
    ):
        assert needle in text, needle


def test_guide_says_how_the_football_scores_are_turned_on_and_where_they_come_from():
    text = visible_text((GUIDE / "index.html").read_text(encoding="utf-8"))
    for needle in (
        "Football scores, off by default. On the table screen tap Options, then Door sign, and under Football scores switch on any of Texas, Michigan, Texas A&M and Missouri",
        "On its left is the school's last result, with the school's own score first whether it played at home or away (Texas (#4) 45 \u2013 Texas A&M 42).",
        "On its right is the next game, the visiting team first (Next: 10/19 \u00b7 Texas (#4) @ Missouri (#9); a game at a neutral site reads vs in place of @).",
        "With more than one school on, the strip shows one at a time and moves to the next every 8 seconds, with the school's name at the start of the line "
        "(Michigan: Michigan (#12) 31 \u2013 Ohio State 24).",
        "Switch every school off and the strip goes.",
        "The strip never wraps: a name too long for its line ends in \"\u2026\", and on a screen narrower than 1100 pixels the last result sits above the next game.",
        # where the scores come from, and what the device needs
        "The device, not the door screen, asks ESPN's public schedule feed at site.api.espn.com for each chosen school every ten minutes, and asks for nothing while every school is off.",
        "There is nothing to set up: no account and no key.",
        "The device needs to reach site.api.espn.com over the internet on HTTPS (port 443); nothing else on the door sign needs the internet.",
        "ESPN does not document this feed or promise to keep it: it can change or stop, and the strip then keeps showing the last result it read until Crystal PM updates the software.",
    ):
        assert needle in text, needle


def test_guide_gives_the_short_screen_rules_the_stylesheet_has():
    """The thresholds in the guide are the ones in sign.css, so the one cannot change without the other."""
    text = visible_text((GUIDE / "index.html").read_text(encoding="utf-8"))
    for needle in (
        "The label under the room's name, the line under it and the room's location are cut with an ellipsis after two lines, "
        "and so is a booking's title (after one line in Today on a screen 560 pixels high or less).",
        "A room name that would take more than two lines is set in smaller type, as one of more than 24 characters is, so it is never cut; "
        "on a screen 560 pixels high or less, one that would take two lines is too, and one that still takes more than two goes on down 0.1 rem at a time, "
        "to 1.3 rem, until it takes two.",
        "On a screen of any height a name also goes down, to 1.3 rem at the most, while the status would run off the bottom.",
        "At 700 pixels high or less the week leaves out the line under the status label and the room's location and makes the room's name smaller, "
        "and at 560 pixels or less each day's date too; with football scores on, at 560 pixels high or less, Today's status leaves out the line under its label too.",
    ):
        assert needle in text, needle
    # the week's rules were for the strip once: "With football scores on, the strip takes some of the height: up to 700 pixels high the week view ..."
    assert "With football scores on, the strip takes some of the height" not in text
    assert "At 560 pixels high or less the week view leaves out the line under the status label and each day's date, with football scores on or off." not in text
    assert "the Today list leaves out the line under the label too" not in text       # it is the status in Today that does, not the list
    css = squash((REPO / "src" / "croom" / "control" / "static" / "sign.css").read_text(encoding="utf-8"))      # however the stylesheet is laid out
    for rule in (
        '@media (max-height: 700px) { body[data-view="week"] .detail, body[data-view="week"] .location { display: none; } body[data-view="week"] .room { font-size: clamp(1.8rem, 3.6vw, 2.6rem); } }',
        '@media (max-height: 560px) { body[data-view="week"] .detail, body[data-view="week"] .day-date { display: none; }',
        '@media (max-height: 560px) { body[data-strip] .detail { display: none; } .event .title { -webkit-line-clamp: 1; }',
        '.event .title { margin: 0; overflow: hidden; overflow-wrap: anywhere; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }',
        # the label, the line under it and the location: two lines at most
        '.location, #chip-text, .detail { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; }',
        # a landscape sign's booking in the week: the time with one line of title under it, and no platform
        '@media (orientation: landscape) and (max-height: 700px) { .slots { margin-top: 0.375rem; gap: 0.25rem; } .slot { padding: 0.25rem 0.5rem; } '
        '.slot .title { -webkit-line-clamp: 1; } .slot .who { display: none; } }',
    ):
        assert squash(rule) in css, rule
    assert 'body[data-view="week"][data-strip]' not in css                       # the week's rules are by height alone, strip up or down


def test_guide_repeats_the_photograph_credits_of_every_background():
    """The spec has the room guide repeat what CREDITS.md says: each background's author and licence, in the guide's table."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    credits = (REPO / "src" / "croom" / "control" / "static" / "backgrounds" / "CREDITS.md").read_text(encoding="utf-8")
    entries = re.findall(r"^- `(\w+)\.jpg`: .* by (.+?), (CC0|CC BY(?:-SA)? [\d.]+|Public domain), https://", credits, re.M)
    assert len(entries) == 8, entries
    rows = {(unescape(author), unescape(licence)) for _, author, licence in re.findall(r"<tr><td>(.*?)</td><td>(.*?)</td><td>(.*?)</td></tr>", html)}
    for key, author, licence in entries:
        assert (author.removeprefix("User:"), licence) in rows, (key, author, licence)
    assert "The eight photographs are from Wikimedia Commons." in visible_text(html)


def test_guide_troubleshooting_covers_the_football_strip_the_settings_file_and_the_short_screens():
    text = visible_text((GUIDE / "index.html").read_text(encoding="utf-8"))
    for needle in (
        "The football strip does not appear",
        "A school you switch on shows within a few seconds. If nothing comes, the device is not getting an answer from ESPN.",
        "prints 200 when it can",
        "a firewall or web filter that blocks site.api.espn.com on HTTPS",
        "the device tries again after 1, 2 and 5 minutes and then every 10, and the strip comes back by itself when a read works.",
        "While ESPN cannot be reached the strip keeps the last result it read, so a game played since can still show as next.",
        "A choice is lost when the device restarts",
        "are saved in control-settings.json in the device's data folder, /var/lib/croom",
        "the choices still work, but they are lost at the next restart until the folder can be written.",
        "says \"Could not save the room settings\" and why",
        "A name too long even at the smallest size",
        "A room name goes down to 1.3 rem and no further. Room names over about 150 characters (five lines of the smallest type on a 7-inch 800 by 480 sign) run off the sign; "
        "use a shorter name.",
    ):
        assert needle in text, needle
    # the settings card said the strip never came, and the name's card gave pixel counts: the scoreboard follows a switch until the restart, and the limit is one sentence
    for gone in ("The switch is on but the strip never comes", "a chosen background holds only until the next restart, and the strip never comes",
                 "On a screen only 480 to 500 pixels high", "runs past the bottom of the screen by 6 pixels", "and by 39 when the room is free"):
        assert gone not in text, gone
    # the first card said a two-line name and a title in the label were the trouble: the chip now carries no title on a short screen and the name takes the smaller type
    for gone in ("A long name does not fit a very short screen", "shorten the room's name to one line (Boardroom, not Conference Room Twelve)",
                 "A name on two lines with a location under it runs past the bottom of the screen at 1280 by 480 and 1366 by 500",
                 "In the ten minutes before a booking the label carries its title",
                 # the round-1b card: a name of three lines still ran off, by 1 to 14 px, and four lines by 2 to 72
                 "A very long name does not fit a very short screen", "A name of three lines (31 characters or more at 800 to 1024 pixels wide",
                 "A name of four lines (55 characters) runs past by 2 to 72 pixels"):
        assert gone not in text, gone


def test_guide_describes_the_on_screen_keyboard_its_layers_shift_lock_and_key_click():
    """The table screen's keyboard in the words of the spec's section 3: three layers, shift once or locked, every finger at once, and the
    click with the switch under Options that turns it off. Plain words: the keys are named as they look, in lower case, and the labels
    are written as the screen shows them."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    text = " ".join(unescape(re.sub(r"<[^>]+>", "", html)).split())
    for needle in (
        "Type on the table screen.",
        "It has three layers, like an iPad's: letters first, then 123 for numbers and the symbols a link or a passcode needs, then #+= for the rest; ABC goes back to the letters.",
        "Tap the shift key (the up arrow) once for one capital, or twice quickly to lock capitals on, as caps lock does, and a dot shows under its arrow; tap it again to let go.",
        "Every key answers the moment a finger lands, so both thumbs can type at once, and holding the delete key keeps deleting.",
        "To turn the click off, tap Options, then Key click off under Keyboard; Key click on brings it back.",
        "The room remembers the choice, for every screen that opens its page.",
    ):
        assert needle in text, needle
    assert "Shift key" not in text and "Delete key" not in text and "Key Click" not in text
