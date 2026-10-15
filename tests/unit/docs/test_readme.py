"""
The README explains how to set up and develop Crystal Meet, points at both
guides, and links every design spec and implementation plan.
"""

from pathlib import Path

from tests.unit.docs.documented_numbers import IDLE_RETURN_MIN, TAP_PX, TOUCHPAD_IDLE_MIN, TURN_ON, TV_RETURN_MIN, reconnect_words, words

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
    """The room page row says where the remote is opened from, what is on it, what its place shows when there is no TV to control, the
    three touchpad modes with how each one ends, and what a person meets on the pad; the features table's row links this spec and plan
    and names the shell check. The waits are the page's own constants."""
    text = README.read_text(encoding="utf-8")
    for needle in (
        # the remote's place: the idle screen's Options, the meeting screen's own button; it belongs to the screen that opened it
        "the **TV** remote and the keyboard's **Key click**",
        "**Camera** for presets and nudges, **Upcoming meetings**, **TV**)",
        "The TV remote opens from **Options**, then **TV** on the idle screen and from **TV** on the meeting screen",
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
        "| `tv` | `enabled`, `host`, `mac`, `pi_input`, `keep_on` |",
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


def test_readme_says_how_to_update_a_room_device_for_the_virtual_mouse():
    """The update recipe, in its order: the build tools, the new software, the installer's virtual-mouse step on its own, the restart."""
    text = README.read_text(encoding="utf-8")
    steps = ("sudo apt-get install -y python3-dev gcc libc6-dev",
             "sudo /opt/croom/venv/bin/pip install -q --upgrade git+https://github.com/ben-abeo/croom.to.git",
             "cd ~/croom.to && git pull && sudo bash -c 'source installer/install.sh && install_virtual_mouse'",
             "sudo systemctl restart croom`; the room guide's section Updating a room device walks through it")
    assert "To update a room device that is already installed, type these on it in this order:" in text
    positions = [text.index(step) for step in steps]
    assert positions == sorted(positions)
    # why the third package: the compiler's own package only recommends the C library's headers that evdev's build reads
    assert "(the room Pi's virtual mouse needs `evdev`, which pip compiles: it reads the kernel's headers, which `libc6-dev` brings and `gcc` only recommends)" in text


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
        "`tv`: the room's LG TV, switched off (`enabled: false`) until it is set up",
        "the room guide's section The TV says how to switch it on",
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
        "and an **Options** button for **Camera framing**, the **Screensaver**, the **Door sign**, the **TV** remote and the keyboard's **Key click**)",      # the Room page row lists the Door sign, the TV remote and the Key click among the Options
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
