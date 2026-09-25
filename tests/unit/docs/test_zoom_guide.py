"""
The Zoom guide builds to a multi-page PDF, is self-contained, quotes the real
commands and names, and the setup guide points at it.
"""

import re
import subprocess
import sys
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
                   "croom --check-zoom -c /etc/croom/config.yaml", "user:read:token:admin",
                   "Meeting SDK", "Server-to-Server OAuth", "room_user"):
        assert needle in html, needle


def test_setup_guide_points_to_the_zoom_guide():
    html = (GUIDES / "crystal-meet-room-setup" / "index.html").read_text(encoding="utf-8")
    assert "Connect Crystal Meet rooms to Zoom" in html


def test_zoom_guide_uses_the_shared_renderer():
    assert "from render_guide import render" in (GUIDE / "build.py").read_text(encoding="utf-8")
