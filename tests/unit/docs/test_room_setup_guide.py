"""
The setup guide builds to a multi-page PDF with the venv's Chromium.
"""

import re
import subprocess
import sys
from html import unescape
from pathlib import Path

import pytest

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
        "A Google Meet call offers Mute, Turn camera off, Raise hand, Captions and Leave, and no more",
        "Thumbs up", "Clap", "Heart", "Laugh", "Wow", "Celebrate",
        "Participants (7)", "Chat (2)", "Waiting room", "Admit", "No messages yet",
        "Mute all", "Ask all to unmute", "Start recording", "Lock meeting", "End meeting for everyone",
        "each goes back to the meeting screen on its own after two idle minutes; Camera and Upcoming meetings stay where you leave them",
        "A line under the meeting's title says what the meeting has disclosed of itself",
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
            "door", "x-circle", "thumbs-up", "heart", "laugh", "party-popper"} <= set(used), used
    for name in used:
        ours, theirs = symbol(html, name), symbol(page, name)
        assert ours and theirs, name
        assert ours.group(1) == theirs.group(1), name
    assert not re.search(r"""(src|href)=["']https?://""", html)
