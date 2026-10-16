"""
The meeting controls spec says what was built where the build differs from what the spec first planned (the whole-branch review): `pin` is never pressable in
this version, and the dialog card's buttons are the one exception to an icon on every button.
"""

import re
from pathlib import Path

from croom.meeting.providers.base import controls_from

REPO = Path(__file__).resolve().parents[3]
SPEC = (REPO / "docs" / "superpowers" / "specs" / "2026-10-13-meeting-controls-and-dialogs-design.md").read_text(encoding="utf-8")


def test_the_spec_lists_pin_as_unavailable_in_this_version_and_never_as_pressable():
    """Where the spec named what the table presses (the client's buttons, the control names, the bridge's dispatch), `pin` is no longer one."""
    assert controls_from({}, [])["pin"] == {"available": False}                                      # what the code reports, always
    bridge = (REPO / "src" / "croom" / "meeting" / "providers" / "zoom_sdk_page" / "meeting.js").read_text(encoding="utf-8")
    assert 'throw new Error("Unknown control " + name);' in bridge and '"pin"' not in bridge       # and the bridge has no control of that name: "Unknown control pin"
    assert "pin a participant |" not in SPEC
    assert ("(pinning a participant is not driven in this version: the model always says `pin` is unavailable, "
            'and the bridge answers "Unknown control pin")') in SPEC
    names = re.search(r"`name` is a key of the model above \((.*?)\);", SPEC).group(1)
    assert "`pin`" not in names and "`hide_self`" in names
    assert "`pin` is in the model and is always unavailable: no control of that name is pressable in this version." in SPEC
    dispatch = re.search(r"button presses for (.*?); dialog answers", SPEC).group(1)
    assert "`pin`" not in dispatch and "`hide_self`" in dispatch
    assert ("| pin | not driven in this version: the client's per-participant menu is not opened, so `pin` is always unavailable "
            'and the bridge answers "Unknown control pin" | — |') in SPEC


def test_the_spec_says_the_dialog_cards_buttons_carry_no_icon():
    assert ("get an icon before the label, except the buttons of a dialog's card: their labels are the client's own words, "
            "and the sprite has no symbol for a label that is not known.") in SPEC
    app = (REPO / "src" / "croom" / "control" / "static" / "app.js").read_text(encoding="utf-8")
    assert "The card's buttons carry no icon, the one exception to an icon on every button" in app
