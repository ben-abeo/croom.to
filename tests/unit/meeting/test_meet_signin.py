"""
`croom --sign-in-meet` opens the room's own Chromium plainly, on the room's
screen, so a person signs the room in once (spec 2026-10-07, section 4.4).
"""

import io
import subprocess
import sys
from types import SimpleNamespace

from croom.meeting.browser_env import ensure_browsers_path
from croom.meeting.meet_signin import SIGN_IN_URL, sign_in_meet


def test_refuses_without_a_display(tmp_path):
    out = io.StringIO()
    assert sign_in_meet(tmp_path / "profile", out=out, display=None, executable="/opt/chrome", runner=lambda *a, **k: None) == 1
    assert "DISPLAY=:0" in out.getvalue()
    assert not (tmp_path / "profile").exists()


def test_refuses_while_the_service_holds_the_profile(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "SingletonLock").write_text("")
    out = io.StringIO()
    calls = []
    assert sign_in_meet(profile, out=out, display=":0", executable="/opt/chrome", runner=lambda *a, **k: calls.append(a)) == 1
    assert "sudo systemctl stop croom" in out.getvalue()
    assert calls == []


def test_runs_the_bundled_chromium_plainly_and_reports_success(tmp_path):
    profile = tmp_path / "profile"
    calls = []

    def runner(cmd, check=False, env=None):
        calls.append((cmd, env))
        (profile / "Default").mkdir(parents=True)
        (profile / "Default" / "Cookies").write_text("session")
        return SimpleNamespace(returncode=0)

    out = io.StringIO()
    code = sign_in_meet(profile, out=out, display=":0", executable="/opt/chrome", runner=runner)
    assert code == 0, out.getvalue()
    [(cmd, env)] = calls
    assert cmd[0] == "/opt/chrome" and cmd[-1] == SIGN_IN_URL == "https://accounts.google.com/"
    assert f"--user-data-dir={profile}" in cmd and "--no-first-run" in cmd
    assert not any(arg.startswith("--remote-debugging") or arg == "--enable-automation" for arg in cmd)
    assert env["DISPLAY"] == ":0"
    assert oct(profile.stat().st_mode & 0o777) == "0o700"
    text = out.getvalue()
    assert "Sign in there as the room's Google user" in text and "Profile saved" in text and "--check-meet" in text


def test_reports_when_no_session_was_saved(tmp_path):
    out = io.StringIO()
    code = sign_in_meet(tmp_path / "profile", out=out, display=":0", executable="/opt/chrome",
                        runner=lambda *a, **k: SimpleNamespace(returncode=0))
    assert code == 1
    assert "No session was saved" in out.getvalue()


def test_command_line_flag_exists():
    result = subprocess.run([sys.executable, "-m", "croom.core.agent", "--help"], capture_output=True, text=True)
    assert "--sign-in-meet" in result.stdout


def test_browsers_path_defaults_to_the_installed_folder(tmp_path):
    env = {}
    assert ensure_browsers_path(env, candidate=tmp_path) == str(tmp_path)
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path)
    env = {"PLAYWRIGHT_BROWSERS_PATH": "/elsewhere"}
    assert ensure_browsers_path(env, candidate=tmp_path) == "/elsewhere"
    assert ensure_browsers_path({}, candidate=tmp_path / "missing") is None
