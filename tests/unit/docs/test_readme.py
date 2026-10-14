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


def test_readme_describes_the_redesigned_door_sign():
    """The door sign line says what the sign shows (status in a rail, a chip and a ring, over a photograph), the features row names what was built,
    and the config table has the practice's name and the two room settings."""
    text = README.read_text(encoding="utf-8")
    for needle in (
        "over a photograph of Austin under a navy overlay (nine backgrounds, chosen on the room page under **Options**, then **Door sign**)",
        "and an **Options** button for **Camera framing**, the **Screensaver** and the **Door sign**)",      # the Room page row lists the Door sign among the Options
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
        "`POST /api/settings` changes `sign_background` and `sign_scores`, and refuses a bad value with words.",
        # what is not known yet
        "The football scores come from a public ESPN feed that ESPN does not document for this use",
        "On a door screen only 480 to 500 px high a room name that takes three lines of the smaller type (31 characters or more at 800 to 1024 px wide, 41 or more at 1280) "
        "can run past the bottom of the screen: at 800 by 480 by 8 px with the strip up, a location of two lines and the room free for the rest of the day; at 1280 by 480, "
        "a name of 41 characters by 1 px with the strip up and by 14 px with no strip while a booking is under way; a name of four lines (55 characters) by 2 to 72 px. "
        "A name of up to 26 characters fits all six sizes measured (800 by 480, 900 by 500, 1024 by 480, 1024 by 500, 1280 by 480, 1366 by 500) in every status.",
    ):
        assert needle in text, needle
    # what the sign no longer is
    assert "| Green when free, amber ten minutes before a booking, red while in use or booked. |" not in text
    assert "a room name that takes two lines (Conference Room Twelve) with a location under it can push" not in text     # the limit before the chip lost its title
    for gone in ("tinted Austin scenery", "four backgrounds chosen from the table", "Longhorns scoreboard", "chosen from the table"):
        assert gone not in text, gone
