"""
`croom --check-meet URL` opens a Meet link the way the provider does and says
what Meet rendered: the name field, the join control, or the refusal.
"""

import asyncio
import io
import re
import subprocess
import sys

import pytest

playwright = pytest.importorskip("playwright.async_api")

from croom.meeting import meet_check  # noqa: E402
from croom.meeting.display import TvDisplay  # noqa: E402
from croom.meeting.meet_check import DEVICES_JS, check_meet  # noqa: E402

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


def test_check_opens_the_page_the_way_the_tv_display_does():
    import inspect
    from croom.meeting import meet_check
    assert "TvDisplay.context_options()" in inspect.getsource(meet_check)
    assert "TvDisplay.BASE_ARGS" in inspect.getsource(meet_check)
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


async def test_check_refuses_a_profile_a_live_chromium_holds(tmp_path):
    import os
    import socket
    profile = tmp_path / "profile"
    profile.mkdir()
    os.symlink(f"{socket.gethostname()}-{os.getpid()}", profile / "SingletonLock")
    out = io.StringIO()
    code = await check_meet("file:///nothing", out=out, headless=True, settle_ms=100, profile=profile)
    assert code == 1
    assert "sudo systemctl stop croom" in out.getvalue() and "--profile" in out.getvalue()


async def test_reports_the_devices_the_browser_sees(tmp_path):
    page = tmp_path / "prejoin.html"
    page.write_text(PREJOIN)
    out = io.StringIO()
    await check_meet(page.as_uri(), out=out, headless=True, settle_ms=200)
    text = out.getvalue()
    assert "Devices the browser sees:" in text
    assert "microphones:" in text and "speakers:" in text and "cameras:" in text


# --- The Devices line with data in it, and what it says when the listing fails ---

MEETUP_DEVICES = """
<!DOCTYPE html><html><head><script>
navigator.mediaDevices.getUserMedia = async () => { throw new Error('no device here'); };
navigator.mediaDevices.enumerateDevices = async () => [
  {kind: 'audioinput', label: 'MeetUp Microphone'}, {kind: 'audioinput', label: 'Jabra Speak 750'},
  {kind: 'audioinput', label: ''},
  {kind: 'audiooutput', label: 'MeetUp Speaker'},
  {kind: 'videoinput', label: 'Logitech MeetUp Camera'},
];
</script></head><body>
<h2>What's your name?</h2>
<input type="text" placeholder="Your name">
<button disabled><span>Ask to join</span></button>
</body></html>
"""

# getUserMedia never settles, as when the Pulse socket does not answer.
HUNG_LISTING = """
<!DOCTYPE html><html><head><script>
navigator.mediaDevices.getUserMedia = () => new Promise(() => {});
</script></head><body>
<h2>What's your name?</h2>
<input type="text" placeholder="Your name">
<button disabled><span>Ask to join</span></button>
</body></html>
"""

NOTHING_LISTED = "Devices the browser sees: microphones: none; speakers: none; cameras: none"


def devices_line(text):
    return next(line for line in text.splitlines() if line.startswith("Devices the browser sees:"))


def stub_the_devices_script(monkeypatch, behave):
    """Answer the Devices script with `behave()` instead of asking the page; every other script runs for real."""
    real_evaluate = playwright.Page.evaluate

    async def evaluate(self, expression, *args, **kwargs):
        if expression == DEVICES_JS:
            return await behave()
        return await real_evaluate(self, expression, *args, **kwargs)

    monkeypatch.setattr(playwright.Page, "evaluate", evaluate)


async def test_lists_the_devices_chromium_offers(tmp_path, monkeypatch):
    """Chromium's own fake camera and microphone: real labels from a real enumerateDevices, not three 'none's."""
    monkeypatch.setattr(TvDisplay, "BASE_ARGS", [*TvDisplay.BASE_ARGS, "--use-fake-device-for-media-stream"])
    code, text, shot = await run(tmp_path, PREJOIN)
    assert code == 0, text
    found = re.fullmatch(r"Devices the browser sees: microphones: (.+); speakers: (.+); cameras: (.+)", devices_line(text))
    assert found, devices_line(text)
    assert "none" not in found.groups()
    assert "fake" in found.group(3).lower()
    assert "Listing the devices failed" not in text


async def test_the_line_names_each_kind_and_leaves_out_devices_without_a_label(tmp_path):
    code, text, shot = await run(tmp_path, MEETUP_DEVICES)
    assert code == 0, text
    assert devices_line(text) == ("Devices the browser sees: microphones: MeetUp Microphone, Jabra Speak 750; "
                                  "speakers: MeetUp Speaker; cameras: Logitech MeetUp Camera")
    assert "Listing the devices failed" not in text


async def test_a_listing_that_raises_is_named_and_the_rest_of_the_report_follows(tmp_path, monkeypatch):
    async def raises():
        raise RuntimeError("the page has no media devices")

    stub_the_devices_script(monkeypatch, raises)
    code, text, shot = await run(tmp_path, PREJOIN)
    assert code == 0, text
    assert devices_line(text) == NOTHING_LISTED
    assert "Listing the devices failed: the page has no media devices" in text
    assert text.index("Listing the devices failed") > text.index("Devices the browser sees")
    assert "Name field: found" in text and "Join control: found" in text


async def test_a_listing_that_times_out_says_so_and_the_rest_of_the_report_follows(tmp_path, monkeypatch):
    async def times_out():
        raise asyncio.TimeoutError()

    stub_the_devices_script(monkeypatch, times_out)
    code, text, shot = await run(tmp_path, PREJOIN)
    assert code == 0, text
    assert devices_line(text) == NOTHING_LISTED
    assert "Listing the devices failed: timed out after 10 s" in text
    assert "Name field: found" in text and "Join control: found" in text


async def test_a_listing_that_comes_back_with_an_error_shows_what_it_found_and_the_error(tmp_path, monkeypatch):
    async def comes_back_with_an_error():
        return {"microphones": ["MeetUp Microphone"], "speakers": [], "cameras": [], "error": "getUserMedia was blocked"}

    stub_the_devices_script(monkeypatch, comes_back_with_an_error)
    code, text, shot = await run(tmp_path, PREJOIN)
    assert devices_line(text) == "Devices the browser sees: microphones: MeetUp Microphone; speakers: none; cameras: none"
    assert "Listing the devices failed: getUserMedia was blocked" in text


async def test_a_listing_that_never_comes_back_does_not_stall_the_check(tmp_path, monkeypatch):
    """The wait is real here: the page's getUserMedia never settles, the timeout is cut to a moment."""
    monkeypatch.setattr(meet_check, "DEVICES_TIMEOUT_S", 0.3)
    code, text, shot = await asyncio.wait_for(run(tmp_path, HUNG_LISTING), 30)
    assert code == 0, text
    assert devices_line(text) == NOTHING_LISTED
    assert "Listing the devices failed: timed out after 0.3 s" in text
    assert "Name field: found" in text and "Join control: found" in text
