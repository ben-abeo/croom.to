"""
The README explains how to set up and develop Crystal Meet, points at both
guides, and links every design spec and implementation plan.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
README = REPO / "README.md"


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


def test_readme_describes_the_tv_remote_and_the_touchpad_on_the_room_page():
    """The room page row says where the remote is opened from, what is on it, and the three touchpad modes with how each one ends; the
    features table's row links this spec and plan and names the shell check."""
    text = README.read_text(encoding="utf-8")
    for needle in (
        # the remote's place: the idle screen's Options, the meeting screen's own button; it belongs to the screen that opened it
        "the **TV** remote and the keyboard's **Key click**",
        "**Camera** for presets and nudges, **Upcoming meetings**, **TV**)",
        "The TV remote opens from **Options**, then **TV** on the idle screen and from **TV** on the meeting screen",
        "**Back** returns to the screen that opened it",
        # what is on it, and what a TV that is not paired shows in its place
        "**Power** (a second tap turns the TV off)",
        "the room Pi's input marked Crystal Meet",
        "shows **Find TVs** and **Pair** in its place",
        # the three touchpad modes, and the touchpad's way back
        "**Pointer** turns the d-pad into a trackpad for the TV's cursor",
        "**Room Pi mouse** moves the mouse of the Pi behind the TV",
        "**Full-screen touchpad** gives the whole table screen to either",
        "returns to the TV screen on **Done** or after ten idle minutes",
        "a two-finger tap right-clicks the Pi",
        # the features table: this spec and this plan, and the check that came with them
        "a full-screen touchpad and `croom --check-tv` | [spec](docs/superpowers/specs/2026-10-13-tv-remote-and-touchpad-design.md) "
        "| [plan](docs/superpowers/plans/2026-10-13-tv-remote-and-touchpad.md) |",
    ):
        assert needle in text, needle
    assert "Options ›" not in text      # the README says "**Options**, then **TV**", not the arrow


def test_readme_has_the_tv_row_in_the_config_table_and_says_what_each_key_does():
    """The `tv` keys, one clause each: the quoting that an all-digit MAC address needs, when pi_input is acted on, and what keep_on keeps
    and a manual Off holds (in memory only)."""
    text = README.read_text(encoding="utf-8")
    for needle in (
        "| `tv` | `enabled`, `host`, `mac`, `pi_input`, `keep_on` |",
        "**Mobile TV On** and **LG Connect Apps** switched on",
        "a `mac` of digits only, such as `001122334455` or `10:20:30:40:50:59`, is a number to YAML and must be quoted",
        "`pi_input` is the TV input the room Pi is on (the default, `HDMI_1`, is a guess)",
        "before every join it also switches the TV to `pi_input`, live TV and apps included, and leaves a TV whose state is not known alone",
        "`keep_on` (default `true`) wakes the TV when the agent starts, before every join and after an unexpected off",
        "a manual **Off** from the table holds until a manual **On** or the next join",
        "`held_off` lives in memory only, so a restart forgets it",
        "key the TV hands out at pairing is kept in `tv-client-key` under the data directory, mode 600, never in the config",
    ):
        assert needle in text, needle


def test_readme_names_the_tv_check_the_routes_the_pointer_socket_and_the_virtual_mouse():
    """The commands list has the shell check, as the agent's user; the decisions name the routes the remote calls, the one socket the
    touchpad's events travel on, and what the room Pi's virtual mouse needs on the device."""
    text = README.read_text(encoding="utf-8")
    for needle in (
        "/opt/croom/venv/bin/croom --check-tv -c /etc/croom/config.yaml",
        "`/api/tv/control`", "`/api/tv/pair`", "`/api/tv/forget`", "`/api/tv/discover`",
        "one WebSocket, `/ws/pointer`",
        # the open page: the pointer takes no sign-in and refuses a page from another site
        "anyone on the network who can open the page can drive the TV and the Pi's mouse",
        "a page from another site is refused the pointer",
        # the virtual mouse: where it is made, what lets the agent use it, and what building it needs
        "a virtual device on `/dev/uinput` (`evdev`)",
        "the installer's udev rule gives `/dev/uinput` to the `input` group, loads the `uinput` module at boot and puts the desktop user in that group",
        "`python3-dev` and `gcc` must be on the device before the agent is installed or updated",
        # the limitation: nothing here has met a real C6 or a real Pi yet
        "verified against a fake TV and a fake virtual mouse, not yet on an LG C6",
    ):
        assert needle in text, needle


def test_readme_says_how_to_update_a_room_device_for_the_virtual_mouse():
    """The update recipe, in its order: the build tools, the new software, the installer's virtual-mouse step on its own, the restart."""
    text = README.read_text(encoding="utf-8")
    steps = ("sudo apt-get install -y python3-dev gcc",
             "sudo /opt/croom/venv/bin/pip install -q --upgrade git+https://github.com/ben-abeo/croom.to.git",
             "cd ~/croom.to && git pull && sudo bash -c 'source installer/install.sh && install_virtual_mouse'",
             "sudo systemctl restart croom`; the room guide's The TV section walks through it")
    assert "To update a room device that is already installed, type these on it in this order:" in text
    positions = [text.index(step) for step in steps]
    assert positions == sorted(positions)


def test_readme_says_the_python_floor_the_package_asks_for():
    """requires-python is 3.11 (the TV service uses asyncio.timeout): the upstream badge said 3.10."""
    text = README.read_text(encoding="utf-8")
    for needle in ("python-3.11+-blue.svg", "[![Python 3.11+]"):
        assert needle in text, needle
    for stale in ("Python 3.10", "python-3.10"):
        assert stale not in text, stale
