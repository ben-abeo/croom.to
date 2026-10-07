"""
`croom --check-meet URL` opens a Meet link the way the provider does and says
what Meet rendered: the name field, the join control, or the refusal.
"""

import io
import subprocess
import sys

import pytest

playwright = pytest.importorskip("playwright.async_api")

from croom.meeting.meet_check import check_meet  # noqa: E402

PREJOIN = """
<!DOCTYPE html><html><body>
<h2>What's your name?</h2>
<input type="text" placeholder="Your name">
<button disabled><span>Ask to join</span></button>
<script>
const input = document.querySelector('input'); const join = document.querySelector('button');
input.addEventListener('input', () => { join.disabled = !input.value.trim(); });
</script>
</body></html>
"""

REFUSAL = """
<!DOCTYPE html><html><body>
<h1>You can't join this video call</h1>
<p>Your meeting is safe. No one can join a meeting unless invited or admitted by the host.</p>
</body></html>
"""


async def run(tmp_path, html):
    page = tmp_path / "page.html"
    page.write_text(html, encoding="utf-8")
    out = io.StringIO()
    shot = tmp_path / "check.png"
    code = await check_meet(page.as_uri(), out=out, screenshot=shot, headless=True, settle_ms=300)
    return code, out.getvalue(), shot


async def test_reports_the_name_field_and_the_join_control_and_exits_zero(tmp_path):
    code, text, shot = await run(tmp_path, PREJOIN)
    assert code == 0, text
    assert "Name field: found" in text and "Your name" in text
    assert "Join control: found" in text and "Ask to join" in text
    assert "enabled after typing a name" in text
    assert shot.stat().st_size > 0 and str(shot) in text


async def test_reports_a_refusal_and_exits_one(tmp_path):
    code, text, shot = await run(tmp_path, REFUSAL)
    assert code == 1
    assert "Name field: not found" in text and "Join control: not found" in text
    assert "You can't join this video call" in text


def test_command_line_flag_exists():
    result = subprocess.run([sys.executable, "-m", "croom.core.agent", "--help"], capture_output=True, text=True)
    assert "--check-meet URL" in result.stdout
