"""
The meeting controls spec says what was built where the build differs from what the spec first planned (the whole-branch review): `pin` is Zoom's operatePin call (Phase B), and the dialog card's buttons are the one exception to an icon on every button.
"""

import re
from pathlib import Path

from croom.meeting.providers.base import controls_from

REPO = Path(__file__).resolve().parents[3]
SPEC = (REPO / "docs" / "superpowers" / "specs" / "2026-10-13-meeting-controls-and-dialogs-design.md").read_text(encoding="utf-8")


def test_the_spec_lists_pin_as_the_sdks_operate_pin_and_never_as_a_button():
    """Zoom's pin is the SDK's operatePin (Phase B): the model says `pin` is available, the bridge has the controls `pin` and `unpin`, and the
    spec says so where it named what the table presses."""
    assert controls_from({}, [])["pin"] == {"available": True}
    bridge = (REPO / "src" / "croom" / "meeting" / "providers" / "zoom_sdk_page" / "meeting.js").read_text(encoding="utf-8")
    assert '"pin": ofPerson(pin)' in bridge and '"unpin": ofPerson(unpin)' in bridge and 'sdk("operatePin"' in bridge
    assert "pin a participant |" not in SPEC
    assert "pinning a participant is no button: the SDK has `operatePin`" in SPEC
    names = re.search(r"`name` is a key of the model above \((.*?)\);", SPEC).group(1)
    assert "`pin`" not in names and "`hide_self`" in names
    assert "`pin` and `unpin` (a participant id) are Zoom's `operatePin`" in SPEC
    dispatch = re.search(r"button presses for (.*?); dialog answers", SPEC).group(1)
    assert "`pin`" not in dispatch and "`hide_self`" in dispatch
    assert "| pin, unpin | the SDK's `operatePin`" in SPEC


def test_the_spec_says_the_dialog_cards_buttons_carry_no_icon():
    assert ("get an icon before the label, except the buttons of a dialog's card: their labels are the client's own words, "
            "and the sprite has no symbol for a label that is not known.") in SPEC
    app = (REPO / "src" / "croom" / "control" / "static" / "app.js").read_text(encoding="utf-8")
    assert "The card's buttons carry no icon, the one exception to an icon on every button" in app
