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


def visible_text(html):
    """What a reader of the guide sees: the tags gone, the entities read, the lines joined."""
    return " ".join(unescape(re.sub(r"<[^>]+>", "", html)).split())


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
        "On webOS 25 they are under TV Management or External Devices",
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
        "Open the remote. Tap Options, then TV. During a meeting tap TV on the meeting screen, beside Camera and Upcoming meetings",
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
        "Tap Forget TV (on the remote it asks for a second tap), then Pair, and accept the new request on the TV with its own remote",
        "it stops waiting when the request is answered, when the TV goes away, or on Forget TV",
        'the table says the mouse "is not allowed to use /dev/uinput" or "needs /dev/uinput, and it is not there"',
        "which needs python3-dev and gcc on the device before the software is installed",
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
    steps = ("sudo apt-get install -y python3-dev gcc",
             "sudo /opt/croom/venv/bin/pip install -q --upgrade git+https://github.com/ben-abeo/croom.to.git",
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
    steps = ("  - `sudo apt-get install -y python3-dev gcc` first",
             "  - `sudo /opt/croom/venv/bin/pip install -q --upgrade git+https://github.com/ben-abeo/croom.to.git`",
             "  - `cd ~/croom.to && git pull && sudo bash -c 'source installer/install.sh && install_virtual_mouse'`",
             "  - `sudo systemctl restart croom`")
    positions = [item.index(step) for step in steps]
    assert positions == sorted(positions), item
    for needle in ("the touchpad's mouse library (`evdev`) is compiled when pip installs it, and the update stops without them",
                   "the installer's virtual-mouse step on its own (the udev rule for `/dev/uinput`, the `uinput` module at boot, the desktop user in the `input` group)",
                   "a new group only reaches a service that starts afresh",
                   "`Cache-Control: no-store`"):      # (the item's own reason stays)
        assert needle in item, needle
