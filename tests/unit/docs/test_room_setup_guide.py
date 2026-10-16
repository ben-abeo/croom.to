"""
The setup guide builds to a multi-page PDF with the venv's Chromium.
"""

import re
import subprocess
import sys
from html import unescape
from pathlib import Path

import pytest

from croom.core.config import Config
from tests.unit.docs.documented_numbers import (
    IDLE_RETURN_MIN, PAIR_FINISH, PAIR_WAIT, REWAKE, TAP_PX, TOUCHPAD_IDLE_MIN, TURN_ON, TV_RETURN_MIN, WAKE_RESEND, WAKE_WAIT,
    reconnect_words, spoken, words,
)
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


def test_guide_describes_the_meeting_screen_its_rows_sub_screens_dialog_card_and_return():
    """The meeting screen after step 4: what each button is called and does, the three screens behind it, the card for a dialog, the second taps and
    the two idle minutes."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    text = " ".join(unescape(re.sub(r"<[^>]+>", "", html)).split())
    assert html.index("First run and the screens") < html.index(">The meeting screen</h2>") < html.index("<h2>Troubleshooting</h2>")
    for needle in (
        "While a call lasts the page shows the meeting screen on its own, and draws only what the call's platform offers the room right now",
        "A Google Meet call offers Mute, Turn camera off, Raise hand, Captions and Leave, and no more (besides TV, which every call has)",
        "a host muting the room changes the button within a few seconds, and a tap asks for the state the button names, so a Mute tapped just after the host muted the room leaves it muted",
        "The same for the camera: a Turn camera off tapped after the camera went off at the TV leaves it off",
        "The remote and touchpad of the room's TV, always the last button of the row: the section The TV, below, sets it up",
        "Thumbs up", "Clap", "Heart", "Laugh", "Wow", "Celebrate",
        "Participants (7)", "Chat (2)", "Waiting room", "Admit", "No messages yet",
        "Mute all", "Ask all to unmute", "Start recording", "Lock meeting", "End meeting for everyone",
        "each goes back to the meeting screen on its own after two idle minutes; Camera and Upcoming meetings stay where you leave them",
        "A line under the meeting's title says what the meeting has disclosed of itself",
        "It is two lines at most, and a longer one is cut short with an ellipsis",
        "A Zoom dialog that waits for a tap is a card above the controls, with Zoom's own words and buttons in Zoom's order, the last one highlighted",
        "The card's buttons carry no icons",
        "Leave, Remove and End meeting for everyone ask for a second tap: Tap again to leave, Tap again to remove, Tap again to end",
        "Icons are from Lucide (lucide.dev), ISC licence",
    ):
        assert needle in text, needle


def test_guide_draws_the_meeting_screens_icons_from_the_rooms_own_sprite():
    """Every icon the guide draws is a symbol in its own inline sprite, and that symbol is the page's, path for path: what the guide shows is what the table shows."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    page = (REPO / "src" / "croom" / "control" / "static" / "icons.svg").read_text(encoding="utf-8")
    symbol = lambda source, name: re.search(rf'<symbol id="icon-{re.escape(name)}"[^>]*>(.*?)</symbol>', source, re.S)
    used = sorted(set(re.findall(r'<use href="#icon-([a-z-]+)"', html)))
    assert {"mic", "mic-off", "video", "video-off", "hand", "smile", "phone-off", "captions", "layout", "user", "users", "message", "lock", "unlock", "record",
            "door", "x-circle", "thumbs-up", "heart", "laugh", "party-popper", "tv"} <= set(used), used
    for name in used:
        ours, theirs = symbol(html, name), symbol(page, name)
        assert ours and theirs, name
        assert ours.group(1) == theirs.group(1), name
    assert not re.search(r"""(src|href)=["']https?://""", html)


def test_guide_step_four_lists_the_meeting_screens_rows_as_built_and_mute_only_for_someone_not_muted():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    text = " ".join(unescape(re.sub(r"<[^>]+>", "", html)).split())
    for needle in (
        "During a call the page shows the meeting screen on its own: a first row of Mute, Turn camera off, Raise hand, Reactions and Leave, a second row of Captions, the view, Hide self view, Participants, Chat, Host (for a host) and TV (the section after this step shows them all), then the volume, Camera for a preset or a nudge, and Upcoming meetings to see what is booked next",
        "A host or co-host also gets Mute (only for someone not already muted) and Remove on each other person (Tap again to remove)",
    ):
        assert needle in text, needle
    assert "During a call the page shows the meeting screen on its own: Mute, Turn camera off, Leave, the volume" not in text      # the row that stopped at Leave


def test_guide_has_an_optional_section_for_an_lg_tv_after_step_four_in_two_halves():
    """The TV is optional, so it is a section of its own between step 4 and the troubleshooting (not a step in the glance row), and the
    introduction says it is there. It has two small headings, Set it up and Using it, and the check comes right after Pair (the last of
    the setting up). The update recipe is for every room device, so it is a section of its own after this one."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert "the optional section The TV, after step 4, makes the table screen its remote and touchpad" in visible_text(html)
    marks = ('<span class="badge">4</span>', '<p class="kicker">Optional, for an LG TV</p>\n<h2>The TV</h2>', "<h3>Set it up</h3>",
             "<strong>On the TV, once.</strong>", "<strong>Find its address.</strong>", "<strong>Tell the device about the TV.</strong>",
             "<strong>What the lines mean.</strong>", "<strong>Pair from the table.</strong>", "<strong>Check it from the device.</strong>",
             "<strong>What else it says.</strong>", "<h3>Using it</h3>", "<strong>Open the remote.</strong>", "<strong>What is on it.</strong>",
             "<strong>Power, Input and the second taps.</strong>", "<strong>The touchpad modes.</strong>",
             "<strong>The pad's border and its link.</strong>", "<strong>The full-screen touchpad.</strong>", '<p class="kicker">For the operator</p>',
             "</section>", '<p class="kicker">For every room device</p>\n<h2>Updating a room device</h2>', "<h2>Troubleshooting</h2>")
    at = 0
    for mark in marks:                  # each one is found after the one before it: that is the order
        found = html.find(mark, at)
        assert found >= 0, f"{mark!r} is missing, or comes before the mark that should precede it"
        at = found
    assert '<span class="badge">5</span>' not in html
    # a room with no LG TV still has the touchpad: the section, headed "for an LG TV", says so
    assert ("A room with no LG TV still has the touchpad for the room device's mouse: under Options, then TV, the card that says No TV is set up "
            "for this room keeps the Full-screen touchpad button, and the touchpad's strip says the same") in visible_text(html)
    # the headings stay with what follows them, and a block of output or config is never split across two pages
    assert re.search(r"\.block > \.kicker, \.block > h2, \.block > h3 \{[^}]*break-after: avoid;", html)
    assert re.search(r"\bpre \{[^}]*break-inside: avoid;", html)


def test_guide_sets_the_tv_up_in_its_own_settings_in_the_config_and_by_pairing_from_the_table():
    """On the TV (the two settings to switch on, where they are, and the way out when the menu differs; Quick Start+ against
    Wake-on-LAN, and that the MAC address is needed either way), Find TVs for the address, the tv: block (changed where it is, with the
    example address and MAC to replace, the quotes an all-digit MAC address needs, and what each line does), and the pairing: once,
    within the service's wait, with the key kept where only the service's user can read it."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    text = visible_text(html)
    for needle in (
        "turn on Mobile TV On (some TVs call it Turn on via Wi-Fi) and LG Connect Apps",
        "On recent webOS versions (the rooms' LG C6 OLEDs run webOS 26) they are usually under TV Management or External Devices",
        "if yours differs, look through the TV's settings for those two names",
        # Quick Start+: the TV answers in standby and is told to turn on over its connection, so Mobile TV On may stay off; the MAC is needed either way
        "Turn on Quick Start+ as well if the TV has it: the TV then keeps its network connection while it is off, and the device can tell it to turn on "
        "over that connection, so Mobile TV On may stay off",
        "Without Quick Start+ a TV that is fully off can only be woken by Wake-on-LAN, a wake-up message sent over the network, which needs Mobile TV On",
        "The device always sends that message first, so the TV's MAC address must be in the config either way",
        # the address: Find TVs lists the name and the address, and not the MAC address
        "On the table screen tap Options, then TV, then Find TVs",
        "each with the name the TV announces on the network and its address",
        "The MAC address is not listed: read it in the TV's network settings or on your router's list of connected devices",
        # the config: change the block that is there, replace the examples, and what the lines do
        "On the device type sudo nano /etc/croom/config.yaml and change the tv: block that is there (the room files ship it with enabled: false) to read as below",
        "add the block at the end only if the file has none: with two, the last one silently wins and the other is ignored",
        "Replace 10.0.0.41 and AA:BB:CC:DD:EE:FF with your TV's address and MAC address: the example MAC address is a valid one, so a block pasted as "
        "it is warns nowhere and wakes nothing",
        "mac is its MAC address, which every Turn on needs, with Quick Start+ too; keep the quotes, because YAML reads a MAC address of digits only, "
        "such as 001122334455, as a number",
        "pi_input is the TV input the room device's HDMI cable is plugged into, HDMI_1 for the first socket and HDMI_2 for the second",
        "before every join it also switches the TV to pi_input, even from live TV or an app, and leaves a TV alone whose state it cannot read",
        f"keep_on turns the TV on when the service starts, before every join and about {REWAKE} seconds after anything but the table turns it off",
        f"A join waits up to {WAKE_WAIT} seconds for the TV to wake, with the page showing Joining, and then goes ahead without it",
        "An Off from the table holds until someone presses On or the next join begins, and restarting the service forgets it",
        # the pairing
        f"Tap Pair, then accept the request that appears on the TV, with the TV's own remote, within {spoken(PAIR_WAIT)}",
        "You do this once: the device keeps the key the TV hands out in its data folder, normally /var/lib/croom/tv-client-key, readable only by the user the service runs as",
    ):
        assert needle in text, needle
    assert "add the lines below at the end" not in text           # (a second tv: block would silently win over the one the room files ship)
    # the block as it is to be typed: the quotes stay, and a MAC address is never a bare number
    assert '<pre>tv:\n  enabled: true\n  host: "10.0.0.41"\n  mac: "AA:BB:CC:DD:EE:FF"\n  pi_input: "HDMI_1"\n  keep_on: true</pre>' in html


def test_guides_tv_block_is_what_the_room_files_ship_switched_off_and_commented():
    """The guide's block is the room files' tv: block with enabled: true and the comments taken off the other lines: the same keys in the
    same order with the same example values, so that what the guide says to change is there to be changed. Each room file has the one
    block (two tv: keys load with the last silently winning) and its first line is a real enabled: false."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    guide = re.search(r"<pre>(tv:\n.*?)</pre>", html, re.S).group(1).splitlines()
    rooms = sorted((REPO / "deploy" / "rooms").glob("room-*.yaml"))
    assert len(rooms) == 3
    for path in rooms:
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines.count("tv:") == 1, path.name
        block = []
        for line in lines[lines.index("tv:") + 1:]:
            if not line.startswith("  "):
                break
            block.append(line)
        assert block[0] == "  enabled: false", path.name
        switched_on = ["  enabled: true"]
        for line in block[1:]:
            found = re.fullmatch(r"  # (\w+: \S.*?)(?:\s{2,}#.*)?", line)
            assert found, (path.name, line)
            switched_on.append("  " + found.group(1))
        assert ["tv:"] + switched_on == guide, path.name


def test_guide_describes_the_remote_on_the_table_and_the_touchpad_modes_and_how_they_end():
    """The remote's place (Options, then TV; the meeting screen's own button; the page's own waits), what is on it, the second taps, the
    amber flash and the status line's words; Pointer, Room Pi mouse and the full-screen touchpad, what a person meets on the pad (the armed
    border and the hold rule, Reconnecting… and its retries) and the way back from each."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    text = visible_text(html)
    for needle in (
        "Open the remote. Tap Options, then TV. During a meeting tap TV, the last button of the second row",
        f"the remote returns by itself after {words(IDLE_RETURN_MIN)} minutes with no touch, or {words(TV_RETURN_MIN)} minutes during a meeting",
        "The top line names the TV and says what it shows, for example Boardroom TV · On · HDMI 1 (Crystal Meet)",
        "so it can differ from the name Find TVs listed for the same TV",
        "a round d-pad with OK and, below it, Back, Exit, Info and Guide",
        "Power asks for a second tap to turn the TV off, as Leave does",
        f"the top line shows Turn on in place of its state, and turning it on takes up to about {TURN_ON} seconds",
        "Input opens a row of the TV's inputs, with the room device's marked Crystal Meet",
        "A key the TV refuses flashes amber once and the reason appears under the top line",
        "Forget TV, at the right end of the top line, asks for a second tap too",
        # the three modes; the Pi's pointer is not yet seen on a device, so the guide says what the desktop does and leaves it to the first use
        "Pointer turns the d-pad into a trackpad for the TV's own cursor: slide one finger to move it, tap to click, slide two fingers to scroll, and press and hold, then move, to drag",
        "Tap Pointer again to bring the d-pad back",
        "Room Pi mouse does the same for the Pi behind the TV: the Pi's desktop takes its virtual mouse like a USB mouse, so its pointer should appear "
        "on the TV (the room's first use confirms it)",
        "A two-finger tap there is a right-click",
        "Full-screen touchpad gives the whole table screen to the pointer, with the same gestures",
        "with a switch between TV and Room Pi and a Done button",
        f"It returns to the remote when you tap Done, or by itself after {words(TOUCHPAD_IDLE_MIN)} minutes with no touch",
        "is tuned on the table's own screen",
        # what a person meets on the pad
        "A blue border on the pad (on the full-screen touchpad, a thin blue line round the screen's edge) means a hold is armed",
        f"after a short rest the next move drags, a touch that moves sooner is a move for good, and after a rest a nudge under {TAP_PX} pixels does nothing",
        f"the pad (the full-screen strip too) says Reconnecting… and the page tries again {reconnect_words()}; a finger moves nothing until it is back",
    ):
        assert needle in text, needle
    assert "its mouse pointer shows on the TV" not in text


def test_guide_checks_the_tv_from_the_device_as_the_services_user_and_says_what_it_prints():
    """croom --check-tv: the command with its config, who runs it (the desktop user the service runs as: there is no croom user) and why not
    root, what it prints when the TV answers, the warnings, the one change it can make (a new key the TV issued), its exit status, and what
    it does with no TV set up. For the operator: the waits are the service's own constants."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    text = visible_text(html)
    for needle in (
        "/opt/croom/venv/bin/croom --check-tv -c /etc/croom/config.yaml as the user the service runs as, the one who ran the installer",
        "from another account, put sudo -u pi in front",
        "and never as root: the TV's key file is private to that user (mode 600), and a check run as root would leave a renewed key where the service cannot read it",
        "The TV answers: LG C6, on.",
        "Inputs: HDMI 1 (HDMI_1), HDMI 2 (HDMI_2).",
        "The check only looks: it does not wake or pair the TV, and leaves nothing connected",
        "A line beginning Warning",
        "the last line then reads \"The TV answers, with the warnings above.\"",
        "The TV issued a new key (its pairing prompt was accepted during the check), which is kept.",
        "It exits with 0 when the TV answered and 1 when it did not",
        "With no TV in the config it lists the LG TVs it finds on the network instead, as Find TVs does",
        "You should now see the check end with \"Ready: the TV answers.\"",
    ):
        assert needle in text, needle
    assert "sudo -u croom" not in text          # (the installer runs the service as the desktop user: there is no croom user)
    assert "Ready: the TV answers.</pre>" in html          # the output is shown as the check prints it
    # for the operator: the waits, the stop that no longer waits on the TV, the words, the pointer's socket, and how to unpair
    for needle in (
        f"A pairing waits {PAIR_WAIT} seconds for the person at the TV and gives the TV {PAIR_FINISH} seconds more to finish",
        f"While a TV is waking, which a join gives up to {WAKE_WAIT} seconds, the device sends the wake-up message again every {WAKE_RESEND} seconds",
        "Restarting the service ends a pairing, a Turn on and an open touchpad within seconds, because it no longer waits for the TV",
        "reported in words under the remote's top line, for example \"The TV is not answering at 10.0.0.41\"",
        "Unpair with Forget TV, not by deleting the key file: a pointer that is already connected keeps working until its link drops",
        f"a WebSocket at /ws/pointer on port {Config().control.port}, which like the rest of the room page takes no sign-in",
    ):
        assert needle in text, needle


def test_guide_troubleshoots_the_tv_not_found_not_waking_not_accepted_and_the_mouse_that_does_nothing():
    """Four cards, in the guide's card style: a firewall that hides every TV and the one network the search and the wake-up share; the
    reasons a TV will not wake (a MAC address that is no use stops every wake; Mobile TV On and the right MAC matter when Quick Start+
    is off); Forget TV, then Pair; and the virtual mouse's four needs with how to check each."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    text = visible_text(html)
    for title in ("The TV is not found", "The TV will not wake", "The TV no longer accepts the table", "The room Pi's mouse does nothing"):
        assert f'<div class="card"><h3>{title}</h3>' in html, title
    for needle in (
        'Find TVs lists nothing, or the table says "The TV is not answering at 10.0.0.41"',
        "a firewall on the device that blocks incoming traffic by default hides every TV",
        "leave by the device's default network connection, so the device and the TV must be on that same network",
        "A MAC address in mac that is missing or is not one stops every wake, Quick Start+ or not, and the check warns about it",
        "A TV that is fully off, with Quick Start+ off, can only be woken by Wake-on-LAN: Mobile TV On must be on and the MAC address must be the TV's own",
        "With Quick Start+ on, a TV that answers is told to turn on over its connection, so Mobile TV On and a wrong but well-formed MAC address do not matter",
        f"A join waits up to {WAKE_WAIT} seconds for the TV, with the page showing Joining, and then goes ahead without it",
        'The table says "The TV no longer accepts this table" or, on the remote\'s top line, "The TV is asking to pair again", or the TV keeps showing a request to allow a device',
        "Tap Forget TV (on the remote it asks for a second tap), then Pair, and accept the new request on the TV with its own remote",
        "it stops waiting when the request is answered, when the TV goes away, or on Forget TV",
        'the table says the mouse "is not allowed to use /dev/uinput" or "needs /dev/uinput, and it is not there"',
        "which needs python3-dev, gcc and libc6-dev on the device before the software is installed",
        'KERNEL=="uinput", GROUP="input", MODE="0660"',
        "/etc/udev/rules.d/99-croom-uinput.rules",
        "the service's user in the input group, and the uinput module loaded",
        "ls -l /dev/uinput (group input), groups pi (your user name instead of pi) and lsmod | grep uinput",
        "The mouse is made when the first touchpad mode is turned on after the service starts; its pointer should then appear on the TV",
    ):
        assert needle in text, needle
    assert "so the Pi's own pointer shows on the TV from then on" not in text


def test_guide_updates_every_room_device_in_the_order_the_virtual_mouse_needs():
    """The update recipe is for every room device, with an LG TV or not (evdev is a hard dependency), so it follows the TV section and is
    not part of it: the build tools first (pip compiles the mouse library), then the software, then the installer's virtual-mouse step on
    its own, then the restart."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    recipe = visible_text(html.split("<h2>Updating a room device</h2>", 1)[1].split("</section>", 1)[0])
    steps = ("sudo apt-get install -y python3-dev gcc libc6-dev",
             "sudo /opt/croom/venv/bin/pip install -q --force-reinstall --no-deps git+https://github.com/ben-abeo/croom.to.git",
             "sudo /opt/croom/venv/bin/pip install -q git+https://github.com/ben-abeo/croom.to.git",
             "cd ~/croom.to && git pull",
             "sudo bash -c 'source installer/install.sh && install_virtual_mouse'",
             "sudo systemctl restart croom")
    positions = [recipe.index(step) for step in steps]
    assert positions == sorted(positions), recipe
    for needle in (
        "Update a device that already runs Crystal Meet in the order below.",
        "Every room device needs these steps, with an LG TV or not, because the library behind the touchpad's mouse is a hard dependency "
        "and is compiled on the device",
        "The first and third steps are needed once and do no harm when repeated",
        "Without them the update stops with an error while it builds the mouse library",
        "the compiler's package only recommends libc6-dev, which has the C headers the build reads",
        "That is the installer's virtual-mouse step on its own",
        "a udev rule hands /dev/uinput to the input group, the uinput module is set to load at every boot, and your user joins the input group",
        "a new group only reaches a service that starts afresh",
    ):
        assert needle in recipe, needle
    # not inside the section that is headed "for an LG TV"
    tv_section_end = html.index("</section>", html.index('<p class="kicker">Optional, for an LG TV</p>'))
    assert tv_section_end < html.index('<h2>Updating a room device</h2>')


def test_the_marketplace_checklist_updates_a_room_device_in_the_same_order():
    """The review response's checklist item for the room devices gives the same four steps in the same order, in its checkbox style
    (one nested bullet each), and keeps its own reason: the no-store header."""
    checklist = (REPO / "deploy" / "zoom-marketplace" / "review-response.md").read_text(encoding="utf-8")
    item = checklist.split("- [ ] **Room devices get the no-store fix**", 1)[1].split("\n- [ ] ", 1)[0]
    steps = ("  - `sudo apt-get install -y python3-dev gcc libc6-dev` first",
             "  - `sudo /opt/croom/venv/bin/pip install -q --force-reinstall --no-deps git+https://github.com/ben-abeo/croom.to.git`",
             "  - `sudo /opt/croom/venv/bin/pip install -q git+https://github.com/ben-abeo/croom.to.git`",
             "  - `cd ~/croom.to && git pull && sudo bash -c 'source installer/install.sh && install_virtual_mouse'`",
             "  - `sudo systemctl restart croom`")
    positions = [item.index(step) for step in steps]
    assert positions == sorted(positions), item
    for needle in ("the touchpad's mouse library (`evdev`) is compiled when pip installs it, and the update stops without them",
                   "`gcc` only recommends `libc6-dev`, which has the C headers the build reads",
                   "the installer's virtual-mouse step on its own (the udev rule for `/dev/uinput`, the `uinput` module at boot, the desktop user in the `input` group)",
                   "a new group only reaches a service that starts afresh",
                   "`Cache-Control: no-store`"):      # (the item's own reason stays)
        assert needle in item, needle
