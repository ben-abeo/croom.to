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


def test_runs_the_bundled_chromium_plainly_and_reports_success(tmp_path):
    profile = tmp_path / "profile"
    calls = []

    def runner(cmd, check=False, env=None):
        calls.append((cmd, env))
        cookies_db(profile, ["SID", "__Secure-1PSID"])
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


# --- the fix pass after the whole-branch review ---

import os
import socket
import sqlite3
from pathlib import Path

from croom.meeting.browser_env import profile_holder
from croom.meeting.meet_signin import session_saved


def live_lock(profile: Path, pid=None, host=None):
    profile.mkdir(parents=True, exist_ok=True)
    os.symlink(f"{host or socket.gethostname()}-{pid or os.getpid()}", profile / "SingletonLock")


def test_profile_holder_sees_chromiums_dangling_symlink_lock(tmp_path):
    assert profile_holder(tmp_path / "none") is None
    live_lock(tmp_path / "live")
    assert profile_holder(tmp_path / "live") == os.getpid()
    live_lock(tmp_path / "stale", pid=999999)
    assert profile_holder(tmp_path / "stale") is None  # a crash left it; Chromium replaces it
    live_lock(tmp_path / "elsewhere", host="another-pi")
    assert profile_holder(tmp_path / "elsewhere") is None
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "SingletonLock").write_text("")
    assert profile_holder(tmp_path / "plain") is None


def test_refuses_while_a_live_chromium_holds_the_profile(tmp_path):
    profile = tmp_path / "profile"
    live_lock(profile)
    out = io.StringIO()
    calls = []
    assert sign_in_meet(profile, out=out, display=":0", executable="/opt/chrome", runner=lambda *a, **k: calls.append(a)) == 1
    assert "sudo systemctl stop croom" in out.getvalue()
    assert calls == []


def test_a_stale_lock_from_a_crash_does_not_block_the_sign_in(tmp_path):
    profile = tmp_path / "profile"
    live_lock(profile, pid=999999)
    calls = []
    out = io.StringIO()
    sign_in_meet(profile, out=out, display=":0", executable="/opt/chrome", runner=lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0))
    assert len(calls) == 1


def cookies_db(profile: Path, names):
    (profile / "Default").mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(profile / "Default" / "Cookies")
    db.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
    db.executemany("INSERT INTO cookies VALUES (?, ?, '')", [(".google.com", n) for n in names])
    db.commit()
    db.close()


def test_a_session_is_only_saved_when_google_set_its_sign_in_cookie(tmp_path):
    assert session_saved(tmp_path / "missing") is False
    cookies_db(tmp_path / "visited", ["NID", "AEC"])  # the sign-in page sets these before anyone signs in
    assert session_saved(tmp_path / "visited") is False
    cookies_db(tmp_path / "signed", ["NID", "__Secure-1PSID", "SID"])
    assert session_saved(tmp_path / "signed") is True


def test_closing_the_sign_in_page_without_signing_in_is_reported_as_such(tmp_path):
    profile = tmp_path / "profile"

    def runner(cmd, check=False, env=None):
        cookies_db(profile, ["NID"])
        return SimpleNamespace(returncode=0)

    out = io.StringIO()
    assert sign_in_meet(profile, out=out, display=":0", executable="/opt/chrome", runner=runner) == 1
    assert "No session was saved" in out.getvalue() and "Profile saved" not in out.getvalue()


def test_refuses_to_run_as_root_or_as_a_user_who_does_not_own_the_profile(tmp_path):
    out = io.StringIO()
    assert sign_in_meet(tmp_path / "profile", out=out, display=":0", executable="/opt/chrome", runner=lambda *a, **k: None, uid=0) == 1
    assert "sudo -u" in out.getvalue() and "service" in out.getvalue()
    profile = tmp_path / "owned"
    profile.mkdir()
    out = io.StringIO()
    assert sign_in_meet(profile, out=out, display=":0", executable="/opt/chrome", runner=lambda *a, **k: None, uid=12345) == 1
    import pwd
    assert pwd.getpwuid(os.getuid()).pw_name in out.getvalue()


def test_sign_in_pins_the_basic_password_store_like_the_service_does(tmp_path):
    profile = tmp_path / "profile"
    calls = []

    def runner(cmd, check=False, env=None):
        calls.append(cmd)
        cookies_db(profile, ["SID"])
        return SimpleNamespace(returncode=0)

    sign_in_meet(profile, out=io.StringIO(), display=":0", executable="/opt/chrome", runner=runner)
    assert "--password-store=basic" in calls[0]
