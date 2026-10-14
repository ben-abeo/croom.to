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
        "answers in Zoom's words on the meeting screen, and the meeting goes on",
        "http://<device>:8080/api/meeting/diagnostics",
    ):
        assert needle in text, needle
