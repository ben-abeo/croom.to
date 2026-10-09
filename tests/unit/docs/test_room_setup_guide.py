"""
The setup guide builds to a multi-page PDF with the venv's Chromium.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

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
    assert "screensaver" in html and "TV when idle" in html
    assert "leave it open" not in html  # nobody touches the TV's browser any more


def test_guide_sets_up_the_table_pi_and_names_the_desktop_icons():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert "install-kiosk.sh --url" in html
    assert "Room controls" in html and "Start Crystal Meet" in html
    assert "on-screen keyboard" in html


def test_guide_sets_up_the_camera_and_the_sound():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert "Find the stops" in html and "Save as Home" in html and "Mute speaker" in html
    assert "Devices the browser sees" in html
