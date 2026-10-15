"""
The README explains how to set up and develop Crystal Meet, points at both
guides, and links every design spec and implementation plan.
"""

import logging
from pathlib import Path

from croom.core.config import DEFAULT_ACKNOWLEDGE, DEFAULT_REACTIONS, load_config
from croom.meeting.providers.base import DialogPolicy, controls_from

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
        "pull the repository on the table Pi too, run the install-kiosk command again and reboot it so the new scale takes effect",
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
        "a first row of Mute, Turn camera off, Raise hand, **Reactions** and Leave",
        "a second row of **Captions**, the view (**Gallery view** or **Speaker view**), **Hide self view**, **Participants**, **Chat** and, only while the room is host or co-host, **Host**",
        "A Google Meet call offers Mute, Turn camera off, Raise hand and Captions, and no more",
        "a Zoom dialog that waits for a tap shows as a card above the controls with Zoom's own buttons",
        "**Participants**, **Chat** (read only) and **Host** are screens of their own, each with **Back**, and each returns to the meeting screen after two idle minutes",
        "Leave, Remove and End meeting for everyone ask for a second tap",
        "Every button on the meeting screen has a [Lucide](https://lucide.dev) icon before its label (the buttons of a Zoom dialog's card excepted: their words are Zoom's)",
    ):
        assert needle in text, needle
    assert "(Mute, Turn camera off, Leave, the room's volume" not in text


def test_readme_names_the_dialog_policys_and_the_reactions_defaults_as_the_code_has_them():
    """The config table names each default (the code's own lists, not a copy that can drift) and says what `ask_everything` and the leniency do."""
    text = README.read_text(encoding="utf-8")
    for phrase in DEFAULT_ACKNOWLEDGE + DEFAULT_REACTIONS:
        assert f"`{phrase}`" in text, phrase
    for needle in (
        "| `meeting.dialogs` | `acknowledge`, `ask_everything` |",
        "`ask_everything: true` (default `false`) makes every dialog wait for a tap",
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
        "Anywhere else in the file an unknown key makes the agent set the whole file aside (it prints `Warning: Failed to load config from ...`) and start on its built-in defaults.",
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
    # anywhere else an unknown key costs the whole file: the loader says so on its output, and the file's other settings are lost with it
    capsys.readouterr()
    config, said = loaded("meeting:\n  platfroms: [zoom]\n  dialogs:\n    ask_everything: true\n")
    assert "Warning: Failed to load config from" in capsys.readouterr().out
    assert config.meeting.dialogs.ask_everything is False and config.room.name == "Conference Room"


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
