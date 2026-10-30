"""
The README explains how to set up and develop Crystal Meet, points at both
guides, and links every design spec and implementation plan.
"""

import dataclasses
import logging
import re
from pathlib import Path

from croom.core.config import DEFAULT_ACKNOWLEDGE, DEFAULT_REACTIONS, Config, load_config
from croom.meeting.providers.base import DialogPolicy, controls_from
from tests.unit.docs.documented_numbers import IDLE_RETURN_MIN, TAP_PX, TOUCHPAD_IDLE_MIN, TURN_ON, TV_RETURN_MIN, reconnect_words, words

REPO = Path(__file__).resolve().parents[3]
README = REPO / "README.md"
SERVICE = REPO / "src" / "croom" / "control" / "service.py"
SPRITE = REPO / "src" / "croom" / "control" / "static" / "icons.svg"


def test_readme_starts_with_crystal_meet_and_keeps_upstream():
    text = README.read_text(encoding="utf-8")
    assert text.lstrip().startswith("# Crystal Meet")
    assert "# 🎥 Croom" in text  # the upstream project's README stays below


def test_readme_names_the_setup_pieces():
    text = README.read_text(encoding="utf-8")
    for needle in (
        "installer/install.sh --config",
        "--credentials",
        "deploy/rooms/",
        ":8080/sign",
        "croom --check-calendar",
        "docs/guides/crystal-meet-room-setup.pdf",
        "docs/guides/crystal-meet-google-calendar.pdf",
        "docs/guides/crystal-meet-zoom.pdf",
        "--zoom-credentials",
        "croom --check-zoom",
        "SDK_VERSION",
        "Follow the five guides",
        "docs/guides/crystal-meet-google-meet.pdf",
        "--sign-in-meet",
        "docs/guides/crystal-meet-dashboard.pdf",
        "install-dashboard.sh --admin-email",
        "deploy/dashboard/",
        "not yet verified against a live outside-hosted meeting",
        "npm run dev",
        ".venv/bin/pytest",
        "screensaver",
        ":8080/tv",
        "**Options**, then **Screensaver**",
        "install-kiosk.sh --url",
        # Settings updates the table Pi through its agent now; a table Pi set up before has none, and takes the old steps once
        "From then on **Settings** updates the table Pi with the TV Pi: its agent runs the install-kiosk setup of the new version and reboots it",
        "once, pull the repository on the table Pi too, run the install-kiosk command again and reboot it",
        "?keyboard=1",
        "Start Crystal Meet",
        "audio.output_device",
        "ptz_travel_seconds",
        "camera_presets",
        "/api/audio/volume",
        "Find the stops",
        "also becomes PipeWire's default sink",
        "reads the same key as a PyAudio device id",
    ):
        assert needle in text, needle


def test_readme_links_every_spec_and_plan():
    text = README.read_text(encoding="utf-8")
    for folder in ("specs", "plans"):
        files = sorted((REPO / "docs" / "superpowers" / folder).glob("*.md"))
        assert files, folder
        for path in files:
            assert path.relative_to(REPO).as_posix() in text, path.name


def test_readme_describes_the_room_page_in_two_modes():
    text = README.read_text(encoding="utf-8")
    for needle in ("Options", "Screensaver", "Upcoming meetings", "meeting screen", "1280 by 800", "REPLACE_WITH_LOCATION"):
        assert needle in text, needle
    assert "TV when idle" not in text
    # the page does not scroll; the bookings boxes do, inside themselves
    assert "without the page scrolling" in text and "scroll inside themselves" in text
    assert "without scrolling" not in text and "never scrolls" not in text
    # the Sound and Camera panels became the meeting screen's Sound row and the Camera screens
    assert "the meeting screen's Sound row" in text and "the Camera screens" in text
    assert "Sound panel" not in text and "Camera panel" not in text and "Camera panels" not in text


def test_readme_describes_the_on_screen_keyboard_and_its_key_click():
    """The room page row says what the keyboard is and where its click is switched; the decisions say how it works and where the choice
    is kept; the features table links its spec."""
    text = README.read_text(encoding="utf-8")
    for needle in (
        "docs/superpowers/specs/2026-10-13-on-screen-keyboard-design.md",
        "Key click",
        # the room page row: the layers, Shift once or locked, fast typing with two thumbs, the pop, and the switch
        "three layers like an iPad's (letters, **123** for numbers and the symbols a link or a passcode needs, and **#+=**)",
        "Shift gives one capital and a double tap locks it",
        "so two thumbs typing fast lose nothing",
        "**Key click off** is chosen under **Options**, then **Keyboard**",
        # the decisions: a touch acts as it lands, no key under a finger is replaced, the click and where its choice is kept
        "never on the click a browser adds after a lift",
        "no key under a finger is ever replaced",
        "`key_click`",
        "`/api/settings`",
        # the features table's row says it in the README's own voice
        "the preview pop, a key click that can be switched off under **Options**, then **Keyboard** |",
    ):
        assert needle in text, needle
    assert "Options ›" not in text      # the README says "**Options**, then **Keyboard**", not the arrow


def test_the_rooms_readme_counts_what_to_replace_as_the_room_templates_do():
    """Each template's header says three REPLACE values and an optional location; the folder's README says the same."""
    readme = " ".join((REPO / "deploy" / "rooms" / "README.md").read_text(encoding="utf-8").split())   # the file is wrapped at 80 columns
    assert "three values to replace" in readme and "an optional location" in readme
    assert "four things" not in readme
    for n in (1, 2, 3):
        header = (REPO / "deploy" / "rooms" / f"room-{n}.yaml").read_text(encoding="utf-8").splitlines()[1]
        assert "three REPLACE values" in header and "location is optional" in header, header


def test_readme_says_where_a_zoom_meetings_passcode_comes_from():
    """The limitation no longer says the passcode must be in the link: it is read from the link or the invitation, or typed."""
    text = README.read_text(encoding="utf-8")
    for needle in (
        "A Zoom meeting's passcode is read from the link, from the booking's invitation text, or typed in the **Passcode** field",
        "that appears on the room page for a bare meeting ID or a link without one",
        # the retry, in one clause: a join Zoom refused over the passcode keeps its link and offers another go
        "keeps its link on the \"Couldn't join\" card, with the field and a **Join again** button",
        "letters, digits and `@ - _ *`, up to 32 characters",
    ):
        assert needle in text, needle
    assert "Zoom links need their passcode in the link" not in text
    # the Meet half of the bullet stays
    assert "A failed Meet join logs what Meet showed" in text


def test_readme_describes_the_meeting_screens_rows_sub_screens_and_second_taps():
    """The room page row says what the meeting screen draws (the first row, the second row, the three screens behind it, the card for a dialog, the
    second taps and the two idle minutes), for Zoom and for Meet; the stale row that stopped at Leave is gone."""
    text = README.read_text(encoding="utf-8")
    for needle in (
        "the meeting's card on the left (the time in the call in large type, how many people are in it, the chat that waits, whether the room is host, and **Leave** at its foot)",
        "on the right Mute, Turn camera off, Raise hand and **Reactions** as tiles, a second set of **Captions**, the view (**Gallery view** or **Speaker view**), **Hide self view**, **Participants**, **Chat** and **Host** (only while the room is host or co-host), "
        "and at the foot a dock of the room's volume, **Camera** for presets and nudges and **Upcoming meetings**, with **Touchpad** and **TV** in the top bar",
        "A Google Meet call offers Mute, Turn camera off, Raise hand and Captions, and no more (Leave, **TV** and the rest of the screen are as ever)",
        "A call on a platform that reports no controls at all keeps Mute, Turn camera off and Leave, and **TV**, which is the room's own and never depends on the platform",
        "a Zoom dialog that waits for a tap shows as a card above the controls with Zoom's own buttons",
        "**Participants**, **Chat** (read only) and **Host** are screens of their own, each with **Back**, and each returns to the meeting screen after two idle minutes",
        "Leave, Remove and End meeting for everyone ask for a second tap",
        "Every button on the meeting screen has a [Lucide](https://lucide.dev) icon before its label (the buttons of a Zoom dialog's card excepted: their words are Zoom's)",
    ):
        assert needle in text, needle
    assert "(Mute, Turn camera off, Leave, the room's volume" not in text
    assert "**Upcoming meetings** and **TV**" not in text and "**Upcoming meetings**, **TV**" not in text      # the TV is in the top bar, not beside Camera


def test_readme_names_the_dialog_policys_and_the_reactions_defaults_as_the_code_has_them():
    """The config table names each default (the code's own lists, not a copy that can drift) and says what `ask_everything` and the leniency do."""
    text = README.read_text(encoding="utf-8")
    for phrase in DEFAULT_ACKNOWLEDGE + DEFAULT_REACTIONS:
        assert f"`{phrase}`" in text, phrase
    for needle in (
        "| `meeting.dialogs` | `acknowledge`, `ask_everything` |",
        "`ask_everything: true` (default `false`) makes every dialog wait for a tap",
        "Every dialog Zoom draws is written to the journal (`journalctl -u croom`) with its title, words and buttons and what was done with it, whether or not the agent runs with `-v`, so a new one can be judged and added to the list",
        "| `meeting.controls` | `reactions` |",
        "(shown on the table as Thumbs up, Clap, Heart, Laugh, Wow and Celebrate); `reactions: []` offers none, and the **Reactions** button is not drawn",
    ):
        assert needle in text, needle


def test_readme_says_exactly_what_the_config_loader_does_with_a_mistake_and_the_sentences_are_what_it_does(tmp_path, caplog, capsys):
    """The config row's leniency sentences, one claim at a time, against `load_config` itself: which mistakes are answered with a warning and which are
    not, what each gives, and what an unknown key anywhere else costs."""
    text = README.read_text(encoding="utf-8")
    for needle in (
        "A mistake under `dialogs:` or `controls:` costs only its own setting, with a warning in the journal: a value that is not a list (`acknowledge: no`, which YAML reads as false) is replaced by its default; an `ask_everything` that is not true or false is read as false (the words yes, no, on and off, in quotes, are read as they say); an unknown key is ignored; a section that is not a set of settings gives the defaults.",
        "Without a warning: a bare word is a one-item list, a setting with nothing after it takes its default, and `acknowledge: []` acknowledges nothing, so every dialog waits.",
        "Inside any other section an unknown key makes the agent set the whole file aside: none of the file's settings take effect and the agent runs on its built-in defaults (the one line it prints about it is block-buffered and reaches the journal only when the service stops); an unknown key at the top of the file, outside every section, is ignored without a word.",
    ):
        assert needle in text, needle
    caplog.set_level(logging.WARNING, logger="croom.core.config")

    def loaded(body):
        caplog.clear()
        (tmp_path / "config.yaml").write_text(body, encoding="utf-8")
        return load_config(str(tmp_path / "config.yaml")), [r.getMessage() for r in caplog.records]

    # each answered with a warning, and with what the sentence says it gives
    config, said = loaded("meeting:\n  dialogs:\n    acknowledge: no\n")
    assert config.meeting.dialogs.acknowledge == DEFAULT_ACKNOWLEDGE and len(said) == 1 and "should be a list, not False" in said[0]
    config, said = loaded("meeting:\n  dialogs:\n    ask_everything: maybe\n")
    assert config.meeting.dialogs.ask_everything is False and len(said) == 1 and "should be true or false" in said[0]
    config, said = loaded("meeting:\n  dialogs:\n    acknowledg: [x]\n    ask_everything: true\n")
    assert config.meeting.dialogs.ask_everything is True and config.meeting.dialogs.acknowledge == DEFAULT_ACKNOWLEDGE      # the key is ignored, the rest is read
    assert len(said) == 1 and "ignoring acknowledg" in said[0]
    config, said = loaded("meeting:\n  controls: reactions\n")
    assert config.meeting.controls.reactions == DEFAULT_REACTIONS and len(said) == 1 and "should be a mapping of settings" in said[0]
    for word, value in (("yes", True), ("no", False), ("on", True), ("off", False)):                 # the quoted words are read as they say, and without a warning
        config, said = loaded(f"meeting:\n  dialogs:\n    ask_everything: '{word}'\n")
        assert config.meeting.dialogs.ask_everything is value and said == [], word
    # each answered without one
    config, said = loaded("meeting:\n  dialogs:\n    acknowledge: recording\n")
    assert config.meeting.dialogs.acknowledge == ["recording"] and said == []
    config, said = loaded("meeting:\n  dialogs:\n    acknowledge:\n  controls:\n    reactions:\n")
    assert config.meeting.dialogs.acknowledge == DEFAULT_ACKNOWLEDGE and config.meeting.controls.reactions == DEFAULT_REACTIONS and said == []
    config, said = loaded("meeting:\n  dialogs:\n    acknowledge: []\n    ask_everything:\n  controls:\n    reactions: []\n")
    assert config.meeting.dialogs.acknowledge == [] and config.meeting.dialogs.ask_everything is False and config.meeting.controls.reactions == [] and said == []
    notice = {"title": "", "text": "This meeting is being recorded.", "buttons": ["OK"]}
    assert DialogPolicy(DEFAULT_ACKNOWLEDGE).decide(notice) == "acknowledge" and DialogPolicy(config.meeting.dialogs.acknowledge).decide(notice) == "ask"   # [] acknowledges nothing
    reading = {"buttons": {"reactions": {}}}                                                           # `reactions: []` offers none, so the Reactions button has nothing to open
    assert controls_from(reading, DEFAULT_REACTIONS)["reactions"]["available"] is True and controls_from(reading, config.meeting.controls.reactions)["reactions"]["available"] is False
    # inside any other section an unknown key costs the whole file, the settings beside it included
    for section in [f.name for f in dataclasses.fields(Config) if dataclasses.is_dataclass(getattr(Config(), f.name))]:
        capsys.readouterr()
        config, said = loaded(f"data_dir: /marker\nroom:\n  name: Lab\n{section}:\n  not_a_setting: 1\n")
        assert config.data_dir == "" and config.room.name == "Conference Room", section                    # the file's settings did not take effect: the built-in defaults
    config, said = loaded("meeting:\n  platfroms: [zoom]\n  dialogs:\n    ask_everything: true\n")
    assert config.meeting.dialogs.ask_everything is False and said == []                                     # the loader said nothing through the logger
    # ... and the one line it prints about it is a print, which a process whose output goes to a pipe (the journal's) holds in its buffer: the unit does not unbuffer it
    assert "Warning: Failed to load config from" in capsys.readouterr().out
    source = (REPO / "src" / "croom" / "core" / "config.py").read_text(encoding="utf-8")
    assert 'print(f"Warning: Failed to load config from {config_path}: {e}")' in source
    install = (REPO / "installer" / "install.sh").read_text(encoding="utf-8")
    assert "PYTHONUNBUFFERED" not in install and "python -u" not in install and "-m croom.core.agent -c $CONFIG_DIR/config.yaml" in install
    # a key at the top of the file that no section is called is ignored: the file is kept, nothing is printed
    capsys.readouterr()
    config, said = loaded("not_a_section: 1\nroom:\n  name: Lab\n")
    assert config.room.name == "Lab" and said == [] and capsys.readouterr().out == ""


def test_readme_says_what_the_loader_does_with_the_platforms_and_with_a_section_with_nothing_under_it(tmp_path, caplog, capsys):
    """The two sentences of the config table, against `load_config` itself: the platforms setting is read as the dialog settings are, and an empty section is its
    defaults without a word (it used to set the whole file aside)."""
    text = README.read_text(encoding="utf-8")
    for needle in (
        "`platforms` is a list: a bare word (`platforms: zoom`) is that one platform, and a value that is not a list (`platforms: no`, which YAML reads as false) is replaced by the default list, "
        "`google_meet`, `teams` and `zoom`, with a warning in the journal",
        "A section with nothing under it (`meeting:` with its lines deleted) is that section's defaults, in every section of the file, with no warning and the rest of the file in force",
    ):
        assert needle in text, needle
    caplog.set_level(logging.WARNING, logger="croom.core.config")

    def loaded(body):
        caplog.clear()
        (tmp_path / "config.yaml").write_text(body, encoding="utf-8")
        return load_config(str(tmp_path / "config.yaml")), [r.getMessage() for r in caplog.records]

    config, said = loaded("meeting:\n  platforms: no\n")
    assert config.meeting.platforms == ["google_meet", "teams", "zoom"] and len(said) == 1 and "meeting.platforms should be a list, not False" in said[0]
    config, said = loaded("meeting:\n  platforms: zoom\n")
    assert config.meeting.platforms == ["zoom"] and said == []
    capsys.readouterr()
    config, said = loaded("data_dir: /marker\nroom:\nmeeting:\ncalendar:\n")
    assert config.data_dir == "/marker" and config.meeting == Config().meeting and said == [] and capsys.readouterr().out == ""


def test_the_notices_the_readme_says_disclose_nothing_are_the_ones_the_policy_treats_so():
    """"Except that a notice which only says a state changed ... discloses nothing": the three phrases it names are the policy's own state-only ones, and
    a notice with one of them as its only sentence has no line, where one that says what the meeting is doing has its sentence."""
    text = README.read_text(encoding="utf-8")
    assert "except that a notice which only says a state changed (`has been muted`, `has been unmuted`, `recording has stopped`) refreshes the state and discloses nothing" in text
    policy = DialogPolicy(DEFAULT_ACKNOWLEDGE)
    for phrase in ("has been muted", "has been unmuted", "recording has stopped"):
        assert phrase in DEFAULT_ACKNOWLEDGE and phrase in DialogPolicy.STATE_ONLY, phrase
        assert policy.disclosure({"text": f"Your microphone {phrase}.", "buttons": ["OK"]}) == "", phrase
    assert policy.disclosure({"text": "This meeting is being recorded.", "buttons": ["OK"]}) == "This meeting is being recorded."


def test_readme_says_a_button_that_leaves_or_ends_the_meeting_is_never_pressed_for_the_room_and_the_notice_line_is_two_lines():
    """The names are the policy's own (NEVER_PRESSED), and the page's line is clamped to the two lines the README says."""
    text = README.read_text(encoding="utf-8")
    assert "and that a dialog whose one button leaves or ends the meeting (the whole word `Leave`, `End`, `Exit` or `Quit` in its label, in any case) is never pressed for the room, whatever its words say" in text
    assert DialogPolicy.NEVER_PRESSED == ("leave", "end", "exit", "quit")
    assert "(recorded, transcribed; two lines at most, cut short with an ellipsis)" in text
    style = (REPO / "src" / "croom" / "control" / "static" / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\.notice \{[^}]*-webkit-line-clamp: 2;", style)


def test_readme_says_the_dialog_cards_buttons_are_the_one_exception_to_an_icon_on_every_button():
    text = README.read_text(encoding="utf-8")
    assert "a Lucide icon before the label of every button but the dialog card's (whose labels are Zoom's own words)" in text
    assert "a Lucide icon on every button," not in text


def test_readme_lists_the_meeting_routes_and_says_who_can_read_the_status():
    """The four routes this work added are named (and exist), a refusal is a 409 even for an unknown name, and the open status says what it now carries."""
    text = README.read_text(encoding="utf-8")
    source = SERVICE.read_text(encoding="utf-8")
    for route in ("/api/meeting/control", "/api/meeting/dialog", "/api/meeting/chat/seen", "/api/meeting/diagnostics"):
        assert f'"{route}"' in source, route
        assert f"`{route}`" in text, route
    for needle in (
        "answer `{\"meeting\": ...}`, the block `/api/status` carries",
        "a control the platform cannot carry out is a 409 with its words, an unknown name included",
        "on a Zoom call it briefly opens the Reactions and View menus on the TV",
        "`/api/status` is unauthenticated on the LAN, as the whole page is, and now carries the participants' names, the last twenty chat messages and the words of a waiting dialog, as it already carried the meeting link (its passcode too, when the link has one); `/api/meeting/diagnostics` is open the same way and lists more of the meeting (the whole roster with its flags, the words of every dialog met)",
    ):
        assert needle in text, needle


def test_readme_says_mute_and_camera_send_the_state_their_button_names_and_the_routes_take_it():
    """A tap decided by what the room is when it arrives flips what the host just did (the whole-branch review): the buttons send the state they name,
    the routes set it, and a body that names none still toggles."""
    text = README.read_text(encoding="utf-8")
    for needle in (
        "Mute and Camera keep their own routes and send the state their button names, `POST /api/meeting/mute` with `{\"muted\": true}` and `POST /api/meeting/camera` with `{\"on\": false}`: "
        "the agent sets that state (a microphone or camera that is as asked is left alone)",
        "a body that names no state still toggles, as the routes always did, and both answer with the meeting block as well as the state",
    ):
        assert needle in text, needle
    # what the sentences rest on: the routes read those two fields, and the page sends them
    source = SERVICE.read_text(encoding="utf-8")
    page = (REPO / "src" / "croom" / "control" / "static" / "app.js").read_text(encoding="utf-8")
    assert '_state_asked_for(request, "muted")' in source and '_state_asked_for(request, "on")' in source
    assert 'request("mute", "/api/meeting/mute", { muted: muted })' in page and 'request("camera", "/api/meeting/camera", { on: on })' in page


def test_readme_credits_lucide_and_ties_the_credit_to_the_sprite():
    """The sprite says which Lucide it was built from; the README's credit line says the same version and the licence."""
    text = README.read_text(encoding="utf-8")
    sprite = SPRITE.read_text(encoding="utf-8")
    assert "Lucide icons (https://lucide.dev), ISC License" in sprite and "lucide-react 0.303.0" in sprite
    assert "Icons are [Lucide](https://lucide.dev) (ISC licence, lucide-react 0.303.0), in one sprite, `src/croom/control/static/icons.svg`, with the licence text beside it in `LICENSES.md`" in text


def test_readme_says_the_wording_of_the_platforms_pages_is_not_yet_confirmed_on_a_live_meeting():
    text = README.read_text(encoding="utf-8")
    assert "were read from Zoom's 6.5.0 web client files, not yet from a live meeting, and the English labels it looks for on Google Meet's call page are the controls plan's wording" in text
    assert "the Zoom guide's **In the meeting** section lists the inventory to run" in text


def test_readme_describes_the_tv_remote_and_the_touchpad_on_the_room_page():
    """The room page row says where the remote is opened from, what is on it, what its place shows when there is no TV to control, the
    three touchpad modes with how each one ends, and what a person meets on the pad; the features table's row links this spec and plan
    and names the shell check. The waits are the page's own constants."""
    text = README.read_text(encoding="utf-8")
    for needle in (
        # the remote's place: the idle screen's Options, the meeting screen's top bar beside the Touchpad; it belongs to the screen that opened it
        "the **TV** remote, the keyboard's **Key click** and **Settings**",
        "**Chat** and **Host** (only while the room is host or co-host), and at the foot a dock of the room's volume",
        "The TV remote opens from **Options**, then **TV** on the idle screen and from **TV** in the top bar on the meeting screen, beside **Touchpad**",
        "**Back** returns to the screen that opened it",
        f"left alone it returns by itself after {words(IDLE_RETURN_MIN)} minutes ({words(TV_RETURN_MIN)} on the meeting screen)",
        # what is on it
        "**Power** (a second tap turns the TV off)",
        "the room Pi's input marked Crystal Meet",
        # what its place shows when there is no TV to control: three cards, and the touchpad stays under each of them
        "a room with no TV set up shows **Find TVs** and how to set one up",
        "a TV not yet paired shows **Pair** (and **Find TVs**)",
        "a TV that no longer accepts the table shows **Forget TV** and **Pair**",
        "under any of those cards **Full-screen touchpad** stays, so a room with no TV still has the room Pi's mouse",
        # the three touchpad modes, and the touchpad's way back
        "**Pointer** turns the d-pad into a trackpad for the TV's cursor",
        "**Room Pi mouse** moves the mouse of the Pi behind the TV",
        "(a virtual mouse that the Pi's desktop takes like a USB mouse, so its pointer should appear on the TV; the first use in a room confirms it)",
        "**Full-screen touchpad** gives the whole table screen to either",
        f"returns to the TV screen on **Done** or after {words(TOUCHPAD_IDLE_MIN)} idle minutes",
        "a two-finger tap right-clicks the Pi",
        # what a person meets on the pad: the armed hold and the lost link
        f"A blue border on the pad means a hold is armed: after a short rest the next move drags, a touch that moves sooner is a move for good, "
        f"and after a rest a nudge under {TAP_PX} pixels does nothing",
        f"the pad says **Reconnecting…** and the page tries again {reconnect_words()}",
        # the features table: this spec and this plan, and the check that came with them
        "a full-screen touchpad and `croom --check-tv` | [spec](docs/superpowers/specs/2026-10-13-tv-remote-and-touchpad-design.md) "
        "| [plan](docs/superpowers/plans/2026-10-13-tv-remote-and-touchpad.md) |",
    ):
        assert needle in text, needle
    assert "Options ›" not in text      # the README says "**Options**, then **TV**", not the arrow
    # the no-TV card has Find TVs and no Pair, and the Pi's pointer is not yet seen on a device
    assert "shows **Find TVs** and **Pair** in its place" not in text
    assert "its cursor then shows on the TV" not in text


def test_readme_has_the_tv_row_in_the_config_table_and_says_what_each_key_does():
    """The `tv` keys, one clause each: that the MAC address is needed with Quick Start+ too and the quoting an all-digit one needs, when
    pi_input is acted on, and what keep_on keeps and a manual Off holds (in memory only); the room files ship the block switched off."""
    text = README.read_text(encoding="utf-8")
    for needle in (
        "| `tv` | `enabled`, `brand`, `host`, `mac`, `pi_input`, `laptop_input`, `keep_on` |",
        "`laptop_input` is the TV input a laptop at the table is on, chosen under **Options**, then **TV**, as **Laptop input**; empty (the default) leaves the TV alone in Laptop mode",
        "the room files in `deploy/rooms/` carry the block with `enabled: false`",
        "**LG Connect Apps** switched on in its settings, and **Mobile TV On** too unless Quick Start+ is on",
        "`mac` is the TV's MAC address, which every Turn on needs (the wake-up message goes out first, so a missing or malformed one stops it even with Quick Start+)",
        "a `mac` of digits only, such as `001122334455` or `10:20:30:40:50:59`, is a number to YAML and must be quoted",
        "`pi_input` is the TV input the room Pi is on (the default, `HDMI_1`, is a guess)",
        "before every join it also switches the TV to `pi_input`, live TV and apps included, and leaves a TV whose state is not known alone",
        "`keep_on` (default `true`) wakes the TV when the agent starts, before every join and after an unexpected off",
        "a manual **Off** from the table holds until a manual **On** or the next join",
        "`held_off` lives in memory only, so a restart forgets it",
        "key the TV hands out at pairing is kept in `tv-client-key` under the data directory, mode 600, never in the config",
    ):
        assert needle in text, needle


def test_readme_names_the_tv_check_the_routes_the_pointer_socket_the_wake_up_and_the_virtual_mouse():
    """The commands list has the shell check, as the agent's user; the decisions name the routes the remote calls, the one socket the
    touchpad's events travel on, the wake-up message that goes out first whatever the TV keeps, and what the room Pi's virtual mouse
    needs on the device. A Turn on's longest wait is the service's own sum."""
    text = README.read_text(encoding="utf-8")
    for needle in (
        "/opt/croom/venv/bin/croom --check-tv -c /etc/croom/config.yaml",
        "`/api/tv/control`", "`/api/tv/pair`", "`/api/tv/forget`", "`/api/tv/discover`",
        "one WebSocket, `/ws/pointer`",
        # the status block names `prompting`: a TV that shows its pairing prompt for the key the table keeps, said on the page's status line
        "(`configured`, `paired`, `refused`, `prompting`, `on`, `reachable`, `name`, `volume`, `muted`, `input`, `inputs`, `app`, `channel`, `keep_on`, `held_off`, `host` and `pi_input`)",
        "`prompting` is true while the TV shows its pairing prompt for the key the table keeps (the agent has waited at it longer than a TV that does not answer takes: "
        "the TV has forgotten the table), and the page's status line then says to **Forget TV**, then **Pair**",
        f"(a Turn on waits up to {TURN_ON} seconds for the TV)",
        # the open page: the pointer takes no sign-in and refuses a page from another site
        "anyone on the network who can open the page can drive the TV and the Pi's mouse",
        "a page from another site is refused the pointer",
        # the wake-up: sent first in every case, so the MAC address is needed with Quick Start+ too; Mobile TV On only for a TV that is fully off
        "To turn a TV on the agent sends a Wake-on-LAN message first, whatever the TV keeps",
        "a limited broadcast from the default-route interface: the Pi and the TV must share that network",
        "so a missing or malformed `mac` stops a Turn on, a join's wake and the re-wake alike",
        "a TV that is fully off is woken by that message alone, which needs Mobile TV On, and a TV that keeps its socket in standby (Quick Start+) "
        "is then told to turn on over it, so it needs no Mobile TV On",
        # the virtual mouse: where it is made, what lets the agent use it, and what building it needs
        "a virtual device on `/dev/uinput` (`evdev`)",
        "so its own pointer should appear on the TV from then on; the first use in a room confirms it",
        "the installer's udev rule gives `/dev/uinput` to the `input` group, loads the `uinput` module at boot and puts the desktop user in that group",
        "`python3-dev`, `gcc` and `libc6-dev` must be on the device before the agent is installed or updated",
        # the limitation: nothing here has met a real C6 or a real Pi yet
        "verified against a fake TV and a fake virtual mouse, not yet on an LG C6",
    ):
        assert needle in text, needle
    assert "the Pi's own pointer shows on the TV" not in text
    assert "sudo -u croom" not in text          # (there is no croom user: the service runs as the desktop user who ran the installer)


def test_readme_says_how_to_update_a_room_device_now_that_settings_does_it():
    """Settings > Software updates the devices; a room from before room settings is upgraded once by hand, in the README's own section;
    the installer re-run is the by-hand route, and the old pip recipe (which would leave a release's recorded version wrong) is gone."""
    text = README.read_text(encoding="utf-8")
    for needle in (
        "To update a room device, open **Options**, then **Settings**, then **Software** on the table screen",
        "A room set up before room settings is moved to them once by hand, in the order [Upgrade a room to room settings](#upgrade-a-room-to-room-settings) gives",
        "`cd ~/croom.to && git pull && sudo bash installer/install.sh`, then `sudo reboot`",
        "## Upgrade a room to room settings",
    ):
        assert needle in text, needle
    assert "sudo /opt/croom/venv/bin/pip install -q --force-reinstall" not in text
    # the upgrade: the TV Pi, then the table Pi, then the door sign panel, each in its order
    upgrade = text.split("## Upgrade a room to room settings", 1)[1].split("\n## ", 1)[0]
    steps = ("sudo systemctl stop croom", "git checkout main && git pull", "sudo bash installer/install.sh", "sudo reboot",
             "sudo bash installer/install-kiosk.sh --url http://<TV Pi address>:8080/", "**Allow** beside the Touch Pi",
             "**Erase all data**", "adb install -r app-release.apk", "adb shell dpm set-device-owner to.croom.doorsign/.AdminReceiver",
             "adb shell cmd package set-home-activity to.croom.doorsign/.MainActivity", "**Allow** beside the door sign")
    positions = [upgrade.index(step) for step in steps]
    assert positions == sorted(positions), upgrade
    # the installer's names for what the upgrade runs are the installer's own
    install = (REPO / "installer" / "install.sh").read_text(encoding="utf-8")
    for name in ("migrate_venv", "set_keyboard_layout", "croom-admin.service", 'ADMIN_DIR="${ADMIN_DIR:-/var/lib/croom-admin}"'):
        assert name in install, name


def test_readme_describes_settings_the_pin_and_its_reset():
    """The Settings section: the PIN's rules as the PIN store has them, the reset at the TV Pi, the root helper's job list as its table has it."""
    from croom.settings import pin
    text = " ".join(README.read_text(encoding="utf-8").split())
    assert pin.UNLOCK_SECONDS == 5 * 60 and pin.MAX_TRIES == 5 and pin.LOCKOUT_SECONDS == 10 * 60
    for needle in (
        "## Settings on the table screen",
        "choose a PIN of 4 to 8 digits",
        "stays unlocked for 5 minutes after the last thing done in it",
        "Five wrong PINs lock the pad for 10 minutes",
        "**A forgotten PIN is reset only at the TV Pi**: over SSH, `sudo rm /var/lib/croom/settings-pin.json`",
        "`/run/croom-admin.sock`",
        "`/var/lib/croom-admin`",
        "Your last change was undone: the room did not start with it",
        "Not available on this device",
        "Can't check for updates right now",
        "Leave the meeting first",
        "A device cannot be moved to a separate LAN through the page",
        "The TV Pi's Wi-Fi cannot change while its joined table Pi is offline",
        "`nmcli` is run with `LC_ALL=C` and its words are matched in English",
        "[android/door-sign/README.md](android/door-sign/README.md)",
    ):
        assert needle in text, needle
    table = re.search(r"self\.table[^=]*= \{(.*?)\}", (REPO / "src" / "croom" / "admin" / "jobs.py").read_text(encoding="utf-8"), re.S)
    names = re.findall(r'"(\w+)": self\.', table.group(1)) if table else []
    assert names, "the helper's job table"
    helper = text.split("### The root helper", 1)[1].split("###", 1)[0]
    for name in names:
        assert f"`{name}`" in helper, name


def test_readme_says_the_python_floor_the_package_asks_for():
    """requires-python is 3.11 (the TV service uses asyncio.timeout): the upstream badge said 3.10."""
    text = README.read_text(encoding="utf-8")
    for needle in ("python-3.11+-blue.svg", "[![Python 3.11+]"):
        assert needle in text, needle
    for stale in ("Python 3.10", "python-3.10"):
        assert stale not in text, stale


def test_the_rooms_readme_says_the_tv_block_is_there_and_off_and_names_all_three_checks():
    """deploy/rooms/README.md: the room files are still complete configs with three values to replace and an optional location, and now
    carry a tv: block that is switched off until the room's LG TV is set up; the checks it names include the TV's."""
    readme = " ".join((REPO / "deploy" / "rooms" / "README.md").read_text(encoding="utf-8").split())   # the file is wrapped at 80 columns
    for needle in (
        "is a complete agent config with three values to replace",
        "an optional location",
        "`tv`: optional defaults for the room's LG or Samsung TV",
        "select it, and accept the request on the TV",
        "`/opt/croom/venv/bin/croom --check-calendar -c /etc/croom/config.yaml`",
        "`/opt/croom/venv/bin/croom --check-zoom -c /etc/croom/config.yaml`",
        "the TV (once it is set up) with `/opt/croom/venv/bin/croom --check-tv -c /etc/croom/config.yaml`",
        "each run as the desktop user the service runs as (the TV's key is private to that user)",
    ):
        assert needle in readme, needle
    assert "four things" not in readme


def test_readme_describes_the_redesigned_door_sign():
    """The door sign line says what the sign shows (status in a rail, a chip and a ring, over a photograph), the features row names what was built,
    and the config table has the practice's name and the two room settings."""
    text = README.read_text(encoding="utf-8")
    for needle in (
        "over a photograph of Austin under a navy overlay (nine backgrounds, chosen on the room page under **Options**, then **Door sign**)",
        "and an **Options** button for **Camera framing**, **Mic level**, the **Screensaver**, the **Door sign**, the **TV** remote, the keyboard's **Key click** and **Settings**, behind a PIN)",      # the Room page row lists every Options item
        "The status is a coloured rail down the left edge, a solid chip under the room's name and a ring round the booking in progress: green when free, amber ten minutes before a booking, red while in use or booked.",
        "**This week** shows the next seven days.",
        "A strip of football scores for Texas, Michigan, Texas A&M and Missouri can run along the bottom (off by default; the device reads ESPN's public schedule feed for it).",
        # the features row
        "the status as a rail, a solid chip and a ring on the current booking, photographs of Austin under a navy overlay (nine backgrounds, eight photographs and plain, chosen on the room page), the company name, a week view, optional football scores for four schools (off by default)",
        # the config table: the practice's name, and the two settings that are not in the config file
        "| `room` | `name`, `location`, `timezone`, `company` |",
        "`company` is the practice's name beside the logo on the door sign: \"Crystal Practice Management\" unless you change it, and `company: \"\"` shows the logo alone.",
        "| Door sign settings (not in the config file) | `sign_background` | Chosen on the room page under **Options**, then **Door sign**: `skyline` (the default), `capitol`, `tower`, `bridge`, `pool`, `star`, `hills`, `stadium` or `plain` (the navy alone).",
        "Kept in `control-settings.json` under the data directory, with the screensaver's choice, and read by the sign from `/api/status`, so every door sign of the room follows it.",
        "| Door sign settings (not in the config file) | `sign_scores` | The schools whose football scores run along the bottom of the sign, any of `texas`, `michigan`, `texas_am` and `missouri`; empty, the default, is off.",
        "the agent reads each school's schedule from ESPN's public feed (`site.api.espn.com`, no account or key) every ten minutes, so the device needs outbound HTTPS to it.",
        # the routes the sign uses
        "`/api/calendar/week` (the next seven days, today first, each `{date, label, events}` by the device's local day)",
        "`POST /api/settings` changes `sign_background`, `sign_scores` and `key_click`, and refuses a bad value with words.",
        "`settings` with `sign_background` and `sign_scores` (and the room page's `key_click`)",
        # what is not known yet
        "The football scores come from a public ESPN feed that ESPN does not document for this use",
        "A room name goes down to 1.3 rem and no further. Room names over about 150 characters (five lines of the smallest type on a 7-inch 800 by 480 sign) run off the sign; use a shorter name.",
    ):
        assert needle in text, needle
    # what the sign no longer is
    for gone in ("only 480 to 500 px high", "runs past the bottom by 6 px", "by 39 px when the room is free", "POST /api/settings` changes `sign_background` and `sign_scores`, and"):
        assert gone not in text, gone                                                                                      # the pixel counts of the limit, and the settings route's two keys
    assert "| Green when free, amber ten minutes before a booking, red while in use or booked. |" not in text
    assert "a room name that takes two lines (Conference Room Twelve) with a location under it can push" not in text     # the limit before the chip lost its title
    assert "a room name that takes three lines of the smaller type (31 characters or more" not in text                   # and the one before a name stepped down
    for gone in ("tinted Austin scenery", "four backgrounds chosen from the table", "Longhorns scoreboard", "chosen from the table"):
        assert gone not in text, gone
