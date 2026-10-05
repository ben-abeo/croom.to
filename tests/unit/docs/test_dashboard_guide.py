"""
The dashboard guide builds to a multi-page PDF, is self-contained, and quotes
the real commands, names and paths.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GUIDES = REPO / "docs" / "guides"
GUIDE = GUIDES / "crystal-meet-dashboard"

pytest.importorskip("playwright.sync_api")


def test_dashboard_guide_builds_to_a_multi_page_pdf(tmp_path):
    out = tmp_path / "guide.pdf"
    result = subprocess.run([sys.executable, str(GUIDE / "build.py"), str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = out.read_bytes()
    assert data.startswith(b"%PDF")
    counts = [int(m) for m in re.findall(rb"/Count (\d+)", data)]
    assert counts and max(counts) >= 3, counts


def test_dashboard_guide_is_self_contained():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)=["']https?://""", html)
    assert "url(http" not in html and "@import" not in html
    assert "Crystal Meet" in html and "Croom " not in html
    for asset in ("crystalpm-logo-white.svg", "fonts/lexend-400.woff2", "fonts/lexend-600.woff2", "fonts/OFL.txt"):
        assert (GUIDE / asset).is_file(), asset


def test_dashboard_guide_quotes_the_real_commands_and_names():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    for needle in ("Raspberry Pi OS Lite (64-bit)", "crystal-meet", "Fixed IP", "hostname -I",
                   "install-dashboard.sh --admin-email", "/opt/croom-dashboard", "Settings",
                   "Provisioning", "dashboard.url", ":3001", "/var/backups/croom-dashboard",
                   "docker compose logs", "psql", "gunzip -c", "systemctl restart croom"):
        assert needle in html, needle


def test_dashboard_guide_uses_the_shared_renderer():
    assert "from render_guide import render" in (GUIDE / "build.py").read_text(encoding="utf-8")
