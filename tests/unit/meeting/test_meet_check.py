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


def test_check_opens_the_page_with_the_providers_context_options():
    import inspect
    from croom.meeting import meet_check
    assert "GoogleMeetProvider.context_options()" in inspect.getsource(meet_check)
    assert "USER_AGENT" not in inspect.getsource(meet_check)


async def test_profile_option_opens_the_page_from_a_persistent_browser_profile(tmp_path):
    """A signed-in profile must be reused as-is, so the check can tell whether Meet
    accepts an automated browser that is signed in to a Workspace account."""
    page = tmp_path / "page.html"
    page.write_text(PREJOIN, encoding="utf-8")
    profile = tmp_path / "profile"
    out = io.StringIO()
    code = await check_meet(page.as_uri(), out=out, headless=True, settle_ms=300, profile=profile)
    assert code == 0, out.getvalue()
    assert f"Profile: {profile}" in out.getvalue()
    assert profile.is_dir() and any(profile.iterdir())  # Chromium wrote its profile files there


def test_command_line_takes_a_profile_directory():
    result = subprocess.run([sys.executable, "-m", "croom.core.agent", "--help"], capture_output=True, text=True)
    assert "--profile DIR" in result.stdout


def test_check_profile_comes_from_the_config_unless_given():
    from pathlib import Path
    from croom.meeting.meet_check import resolve_profile
    assert resolve_profile(None, "/var/lib/croom/meet-profile") == Path("/var/lib/croom/meet-profile")
    assert resolve_profile("/tmp/other", "/var/lib/croom/meet-profile") == Path("/tmp/other")
    assert resolve_profile(None, "") is None


async def test_check_without_a_configured_profile_is_a_guest(tmp_path):
    page = tmp_path / "page.html"
    page.write_text(PREJOIN, encoding="utf-8")
    out = io.StringIO()
    code = await check_meet(page.as_uri(), out=out, headless=True, settle_ms=300, profile=None)
    assert code == 0
    assert "as a guest" in out.getvalue() and "Profile:" not in out.getvalue()


def test_check_command_reads_the_profile_from_the_config():
    import inspect
    from croom.core import agent
    source = inspect.getsource(agent)
    assert "resolve_profile(args.profile, load_config(args.config).meeting.google_profile_dir)" in source
