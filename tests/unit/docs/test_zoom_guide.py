"""
The Zoom guide builds to a multi-page PDF, is self-contained, quotes the real
commands and names, and the setup guide points at it.
"""

import re
import subprocess
import sys
from html import unescape
from pathlib import Path

import pytest

from croom.core.config import DEFAULT_ACKNOWLEDGE, DEFAULT_REACTIONS
from croom.meeting.providers.base import DialogPolicy

REPO = Path(__file__).resolve().parents[3]
GUIDES = REPO / "docs" / "guides"
GUIDE = GUIDES / "crystal-meet-zoom"

pytest.importorskip("playwright.sync_api")


def test_zoom_guide_builds_to_a_multi_page_pdf(tmp_path):
    out = tmp_path / "guide.pdf"
    result = subprocess.run([sys.executable, str(GUIDE / "build.py"), str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = out.read_bytes()
    assert data.startswith(b"%PDF")
    counts = [int(m) for m in re.findall(rb"/Count (\d+)", data)]
    assert counts and max(counts) >= 3, counts


def test_zoom_guide_is_self_contained():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)=["']https?://""", html)
    assert "url(http" not in html and "@import" not in html
    assert "Crystal Meet" in html and "Croom " not in html
    for asset in ("crystalpm-logo-white.svg", "fonts/lexend-400.woff2", "fonts/lexend-600.woff2", "fonts/OFL.txt"):
        assert (GUIDE / asset).is_file(), asset


def test_zoom_guide_quotes_the_real_commands_and_names():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    for needle in ("--zoom-credentials", "zoom-credentials.json", "zoom-credentials.example.json",
                   "croom --check-zoom -c /etc/croom/config.yaml", "user:read:token:admin", "user:read:zak:admin",
                   "Meeting SDK", "Server-to-Server OAuth", "room_user",
                   "Zoom for developers", "Admin-managed", "Development", "Production",
                   "Set my app as 'Unlisted'", "Submit", "Scope Description",
                   "Request Anonymous Join Exception", "Use Device OAuth", "programmatic join",
                   "Request to publish", "Enable Publishing", "Technical Design Document", "External Zoom users",
                   "not yet been verified", "the first outside-hosted meeting proves it"):
        assert needle in html, needle


def test_setup_guide_points_to_the_zoom_guide():
    html = (GUIDES / "crystal-meet-room-setup" / "index.html").read_text(encoding="utf-8")
    assert "Connect Crystal Meet rooms to Zoom" in html


def test_zoom_guide_uses_the_shared_renderer():
    assert "from render_guide import render" in (GUIDE / "build.py").read_text(encoding="utf-8")


def visible_text(html):
    """The page as a reader sees it: tags dropped, entities decoded, white space collapsed."""
    return " ".join(unescape(re.sub(r"<[^>]+>", "", html)).split())


def test_zoom_guide_says_how_a_meeting_gets_its_passcode():
    """Step 6 joins by meeting id with the Passcode field, and the passcode card sends the reader to the field or to Join again."""
    text = visible_text((GUIDE / "index.html").read_text(encoding="utf-8"))
    for needle in (
        # step 6: the meeting id and the field, and a booking whose link has no passcode
        "To join with the meeting id instead, type it in the same box: a Passcode field appears under it",
        "type the meeting's passcode there and press Join",
        "A booking whose Zoom link has no passcode in it is joined with the one written in its invitation text",
        # the troubleshooting card: the field, or Join again on the "Couldn't join" card
        "type the passcode in the Passcode field that appears under the link box for a meeting id or a link without one",
        "After a \"Couldn't join\" that says \"Zoom needs the meeting passcode\"",
        "type the passcode there and tap Join again",
    ):
        assert needle in text, needle
    assert "A bare meeting id cannot open a passcode-protected meeting" not in text


def test_zoom_guide_says_how_the_room_joins_muted_or_dark():
    """The troubleshooting card states the join-time ruling: the camera setting both ways, the microphone setting only towards
    muted (a room Zoom has muted stays muted, Unmute one tap away), and a tap Zoom cannot carry out answers in words."""
    text = visible_text((GUIDE / "index.html").read_text(encoding="utf-8"))
    for needle in (
        "At join the room's camera setting (camera_default_on in the room's config) applies both ways: Zoom's video is started or stopped to match it",
        "The microphone setting (mic_default_on) applies only towards muted: a room set to join muted is muted",
        "when the room is set to join with the microphone on and Zoom has it muted (the host's mute on entry, for example), "
        "the room stays muted and the table shows Unmute, one tap away",
        "answers in words on the meeting screen, Zoom's own where Zoom gave them, and the meeting goes on",
        "http://<device>:8080/api/meeting/diagnostics",
    ):
        assert needle in text, needle


def test_zoom_guide_has_an_in_the_meeting_section_after_the_join_test_and_before_the_troubleshooting():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    text = visible_text(html)
    for heading in ("In the meeting", "The controls on the table", "Zoom's dialogs and notices", "The inventory run", "The diagnostics route"):
        assert heading in text, heading
    for word in ("ask_everything", "Participants", "Host"):                                   # what the section is about
        assert word in text, word
    assert html.index("Join a test meeting") < html.index(">In the meeting</h2>") < html.index("<h2>Troubleshooting</h2>")


def test_zoom_guide_says_what_the_tables_controls_do_and_the_limits_zoom_sets():
    """The meeting screen's rows, the host's screen, the second taps and the two idle minutes, and the words the table gives a refusal, in the
    names the page uses."""
    text = visible_text((GUIDE / "index.html").read_text(encoding="utf-8"))
    for needle in (
        "Mute or Unmute, Turn camera off or Turn camera on, Raise hand or Lower hand, Reactions and Leave",
        "A tap asks for the state its button names and does not flip what Zoom has by then: a tap on Mute made a moment after the host muted the room leaves it muted, and one on Turn camera off made after the camera went off at the TV leaves it off",
        "Reactions replaces the second row for ten seconds with Thumbs up, Clap, Heart, Laugh, Wow and Celebrate, and Cancel",
        "Captions on or Captions off, Gallery view or Speaker view, Hide self view or Show self view, Participants (7), Chat (2) with the number of messages not yet read, Host, drawn only while Zoom says the room is host or co-host, and TV, the room's own button for its TV's remote, which is always the last",
        "Mute (not shown for someone already muted) and Remove",
        "people in the waiting room are listed apart under Waiting room, each with Admit",
        "The last 20 messages other people sent",
        "It is read-only: there is no way to reply from the table in this version",
        "Mute all, Ask all to unmute, Start recording or Stop recording, Lock meeting or Unlock meeting, and, for the host only (a co-host has no End), End meeting for everyone",
        "each returns to the meeting screen by itself after two idle minutes (a tap restarts the wait)",
        "Leave becomes Tap again to leave, Remove becomes Tap again to remove and End meeting for everyone becomes Tap again to end: the second tap must come within five seconds, and a tap in the half second after the first does not count",
        "Zoom takes one mute a second, and one mute all, ask to unmute, remove, record or lock about every five seconds",
        "Zoom ignored muteAll (it takes one a few seconds after the last); try again in a moment",
        "Zoom's captions button is not on the screen yet; try again in a moment",
        "Zoom did not switch captions",
    ):
        assert needle in text, needle


def test_zoom_guide_says_how_zooms_dialogs_are_answered_and_names_the_defaults_as_the_code_has_them():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    text = visible_text(html)
    for phrase in DEFAULT_ACKNOWLEDGE + DEFAULT_REACTIONS:
        assert f'<span class="chip">{phrase}</span>' in html, phrase                       # the code's own lists, so the guide cannot drift from them
    for phrase in ("has been muted", "has been unmuted", "recording has stopped", "transcription has stopped"):
        assert phrase in DialogPolicy.STATE_ONLY, phrase                                    # the notices the guide says add nothing to the line are the policy's own
    for word in DialogPolicy.NEVER_PRESSED:
        assert f'<span class="chip">{word.capitalize()}</span>' in html, word                # and the buttons it says are never pressed are the policy's own
    for needle in (
        "A dialog with exactly one button whose title or words contain one of the phrases under dialogs › acknowledge in the room's config (in any case) is pressed at once",
        "A dialog whose one button leaves or ends the meeting (a label with the whole word Leave, End, Exit or Quit, in any case) is never pressed this way, whatever its words say: it waits for a tap like any other",
        "Every other dialog (more than one button, a single button that leaves or ends the meeting, or words that are not on the list, such as the host asking the room to unmute) shows on the table as a card above the controls",
        "a sentence ends at a full stop, an exclamation mark or a question mark, and the line shows two lines at most, cut short with an ellipsis",
        "Zoom puts the cancelling button first, so recording consent reads Leave Meeting, Got it",
        "Nothing is pressed until someone taps one, which presses it on the TV",
        "ask_everything: true under dialogs (default false) makes every dialog wait for a tap, the notices on the list too",
        "each disclosure is its own sentence, in the order they came, and one that is already there is not added again",
        "a notice that only says a state changed (has been muted, has been unmuted, recording has stopped, transcription has stopped) adds nothing to the line",
        "transcription has stopped is not on the default list, so it waits on the table until someone taps it; add it to the list to have it pressed at once",
        "A notice whose button the room cannot press is not left on the TV with nobody to answer it: it waits on the table like any other dialog",
        "A mistake under dialogs: or controls: costs only its own setting, with a warning in the journal: a value that is not a list (acknowledge: no, which YAML reads as false) is replaced by its default; an ask_everything that is not true or false is read as false (the words yes, no, on and off, in quotes, are read as they say); an unknown key is ignored; a section that is not a set of settings gives the defaults.",
        "Without a warning: a bare word is a one-item list, a setting with nothing after it takes its default, and acknowledge: [] acknowledges nothing, so every dialog waits.",
        "Inside any other section an unknown key makes the agent set the whole file aside: none of the file's settings take effect and the agent runs on its built-in defaults (the one line it prints about it is block-buffered and reaches the journal only when the service stops); an unknown key at the top of the file, outside every section, is ignored without a word.",
        "A room installed before this release has no such lines and runs the defaults",
    ):
        assert needle in text, needle


# What a meeting meets, in the order it meets it: the leads of the inventory's numbered checks.
INVENTORY = [
    "The join.", "The footer.", "The Reactions picker.", "The View menu.", "A person in the waiting room.", "A recording banner.", "The consent dialog.",
    "The host's request to unmute.", "A raised hand.", "The AI Companion notice.", "A breakout invitation.", "A modal with no words.", "Chat and its clock.",
    "Recording from the Host screen.", "A rename.", "The host's calls and the SDK's own words.", "The room's own record.", "Leave.",
]


def test_zoom_guide_numbers_the_inventory_checks_in_the_order_a_meeting_meets_them_on_a_page_of_their_own():
    """The inventory starts on a fresh page, says at its head that it is for the first real meeting and comes out once the labels are pinned, numbers its
    checks in meeting order (the join, the client's labels and menus, the notices and dialogs, the roster, chat, the host's calls, the room's own record,
    Leave), and ends with what to send back."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    head = html.index('<h3 class="sub lead">The inventory run for the first real meeting</h3>')
    assert html[:head].rstrip().endswith('<div class="page-break"></div>')                    # a page of its own
    assert re.search(r"ol\.checks > li::before \{[^}]*content: counter\(check\)", html)         # numbered
    checks = html[head:html.index("</ol>", head)]
    assert re.findall(r"<li><strong>(.*?)</strong>", checks) == INVENTORY
    assert html.index("<strong>What to send back.</strong>") > html.index("</ol>", head)       # and it comes last, out of the numbering
    text = visible_text(html[head:])
    for needle in (
        "This checklist is for the first real meeting a room joins on a device, and it comes out of this guide once the labels and wordings it collects are pinned in the code",
        "You need a second person on another Zoom account, who hosts the meeting: they start a recording, ask the room to unmute, turn on AI Companion and open breakout rooms, raise a hand and send a chat message, and make the room co-host for the checks that need it. The waiting room needs a guest who is not the host, from another browser or a third person.",
        "The checks are numbered in the order a meeting meets them",
    ):
        assert needle in text, needle


def test_zoom_guide_inventory_asks_for_what_spec_section_7_asks_for_and_what_the_code_does_not_yet_know():
    """Spec section 7's five: a recording, AI Companion, the host asking the room to unmute, a hand raised by another participant, a breakout room; and the
    hand's field name, which is unconfirmed."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    checks = visible_text(html[html.index('<ol class="checks">'):html.index("</ol>", html.index('<ol class="checks">'))])
    for needle in (
        "Have the second person start a recording: its real title and words appear in dialogs_seen",
        "Have the host turn on AI Companion: the notice's real title and words appear in dialogs_seen",
        "The default list holds AI Companion and Zoom AI is on: with one button it should be pressed at once and its sentence should stay under the meeting's title; with two buttons, or words the list does not hold, it waits on the table as a card",
        "Have the host ask the room to unmute. Zoom draws a dialog with a choice, whose words are not on the acknowledge list, so it should wait on the table as a card with Zoom's own buttons",
        "Have the second person raise a hand: in meeting › participants the person shows hand: true and the Participants screen marks them Hand raised",
        "The device reads an attendee's hand from raiseHand, bRaiseHand or bHandRaised, and which of them Zoom really uses is not confirmed: attendee_keys shows it",
        "Have the host open breakout rooms and invite the room: the invitation has a choice in it, so it should wait on the table as a card with Zoom's own buttons",
    ):
        assert needle in checks, needle


def test_zoom_guide_has_the_inventory_run_for_the_first_real_meeting_and_says_what_to_send_back():
    text = visible_text((GUIDE / "index.html").read_text(encoding="utf-8"))
    for needle in (
        "were read from Zoom's 6.5.0 web client, not from a live meeting, so that meeting is also an inventory",
        "Check that Chat and CC (closed captions) are among them",
        "a label with caption and show or hide (or closed caption)",
        "Speaker View, Gallery View and Hide Self View (Show Self View when hidden), the checked view having the class full-screen-widget__pop-menu--checked",
        "The View menu flashes on the TV for a moment just after the room joins",
        "in meeting › participants the guest shows waiting: true",
        "(the code reads isHold, waiting, bHold or isInWaitingRoom)",
        "its real title and words appear in dialogs_seen",
        "the stop notice's real wording too",
        "the room should show a card with two buttons, Leave Meeting then Got it, with Got it highlighted",
        "Double-tap Got it: the card answers once, and the second tap must not land on a control under it",
        "an entry with empty text; its label is the aria-label of the modal's root",
        "Zoom's record of a participant who is an assistant shows no userId and the name \"You are a assistant\"",
        "date -u",
        "RECORD_MS",
        "Zoom did not start recording",
        "The microphone was not muted at join",
        "Zoom answers a participant's own rename with an error (neither host nor co-host) and then with success",
        "Then double-tap a reaction on the touchscreen: the row closes at the pick, and the second tap must land on nothing",
        "Double-tap Leave quickly first: the second tap counts only half a second after the first, within five seconds, so a double tap confirms nothing; the same holds for Remove (on the Participants screen) and End meeting for everyone (on the Host screen)",
        "Zoom did not confirm muteAll in time",
        "Zoom did not answer muteAll in time",
        "What to send back",
        "the diagnostics JSON from each point and the journal lines",
        "the acknowledge list and the stop phrases are refined from what comes back",
    ):
        assert needle in text, needle


def test_zoom_guide_describes_the_diagnostics_route_and_what_the_journal_says():
    text = visible_text((GUIDE / "index.html").read_text(encoding="utf-8"))
    for needle in (
        "answers only while the room is in a meeting (otherwise 409, Not in a meeting)",
        "each call briefly opens and closes the Reactions picker and the View menu on the TV, so it is a support route, never polled",
        "user_keys", "attendee_keys", "dialogs_seen", "sdk_version",
        "the last 50: title, text, buttons, label, at, pressed, closed",
        "Zoom dialog:",
        "Acknowledged Zoom's dialog with",
        "The dialog waits on the table",
        "Could not press 'OK' on Zoom's dialog",
        "meeting.dialogs.acknowledge should be a list, not",
        "Could not read the meeting state:",
        "Could not read the meeting page:",
        "for a section that is not a set of settings: the line of the config it names is wrong, and the default is in force (an unknown key is simply ignored)",
        "Could not carry out raise hand:",
        "Could not take an event from the page:",
        "A step the provider took on its own failed:",
    ):
        assert needle in text, needle


def test_zoom_guide_says_which_lines_a_stock_installs_journal_holds_and_how_to_turn_the_rest_on():
    """The installed unit starts the agent with no -v, which logs warnings and errors only. The dialogs' lines are the exception (they come from a logger the
    agent keeps at INFO), and a refusal Zoom gave to a tap is an INFO line that needs -v, which the guide says how to turn on for a while."""
    text = visible_text((GUIDE / "index.html").read_text(encoding="utf-8"))
    for needle in (
        "A stock install's agent writes warnings and errors to the journal and nothing else, with one exception: every dialog.",
        "shows a line for every dialog, whether or not the agent runs with -v",
        "The dialogs of the call that is going on are also in dialogs_seen, with or without the journal.",
        "Zoom's own refusal of a tap, in Zoom's words. This is an INFO line, and the journal holds INFO lines only while the agent runs with -v; the same words are on the table.",
        "To see them for a while, add -v after croom.core.agent on the ExecStart line of /etc/systemd/system/croom.service, so that it reads ExecStart=/opt/croom/venv/bin/python -m croom.core.agent -v -c /etc/croom/config.yaml; then run sudo systemctl daemon-reload and sudo systemctl restart croom, and take the -v out the same way afterwards",
    ):
        assert needle in text, needle
    assert "Zoom's refusals are plain INFO lines" not in text                                      # which a stock install's journal does not hold
    # what the sentences rest on: the unit has no -v, the refusal is an INFO line, the dialogs' lines have a logger of their own
    install = (REPO / "installer" / "install.sh").read_text(encoding="utf-8")
    assert "ExecStart=$INSTALL_DIR/venv/bin/python -m croom.core.agent -c $CONFIG_DIR/config.yaml" in install
    service = (REPO / "src" / "croom" / "control" / "service.py").read_text(encoding="utf-8")
    assert 'logger.info(f"The {what} control was not carried out: {words}")' in service
    zoom = (REPO / "src" / "croom" / "meeting" / "providers" / "zoom_sdk.py").read_text(encoding="utf-8")
    assert zoom.count("dialog_log.info(") == 3 and "logger.info(\"The dialog waits" not in zoom


def test_zoom_guide_inventory_puts_the_waiting_room_before_the_checks_that_need_the_second_person_as_host():
    """The second person hosts: they start the recording, ask the room to unmute, turn AI Companion on and open the breakout rooms. A person in the waiting room
    is a guest who is not the host, and the check comes before the first of those."""
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    start = html.index('<ol class="checks">')
    checks = html[start:html.index("</ol>", start)]
    leads = re.findall(r"<li><strong>(.*?)</strong>", checks)
    waiting = leads.index("A person in the waiting room.")
    for later in ("A recording banner.", "The consent dialog.", "The host's request to unmute.", "The AI Companion notice.", "A breakout invitation."):
        assert waiting < leads.index(later), later
    text = visible_text(checks)
    for needle in (
        "Have the host turn the waiting room on and make the room co-host, then have a guest who is not the host join, from another browser or as a third person",
        "the guest shows waiting: true",
        "the table lists the guest under Waiting room with Admit and leaves them out of the Participants count",
        "Tap Admit, and the guest is in the meeting",
    ):
        assert needle in text, needle
    assert "have the second person join" not in text                                          # who waits is not the host
