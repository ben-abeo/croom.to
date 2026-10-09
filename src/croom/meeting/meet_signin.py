"""
`croom --sign-in-meet`: open the room's own Chromium plainly, with no automation,
on the room's screen, so a person signs the room in to its Google Workspace
account once (spec 2026-10-07 Google Meet, section 4.4). The session stays in
the profile folder the Meet provider uses.
"""

import os
import pwd
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, TextIO

from croom.meeting.browser_env import ensure_browsers_path, profile_holder

SIGN_IN_URL = "https://accounts.google.com/"
# Google sets these only once someone has signed in; the sign-in page itself sets others.
SESSION_COOKIES = ("SID", "__Secure-1PSID", "__Secure-3PSID")


def chromium_executable() -> str:
    """The bundled Chromium's binary, found the way Playwright finds it."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        return p.chromium.executable_path


def session_saved(profile_dir: Path) -> bool:
    """True when the profile's cookie store holds a Google sign-in cookie (names are plain text)."""
    cookies = profile_dir / "Default" / "Cookies"
    if not cookies.is_file():
        return False
    try:
        db = sqlite3.connect(f"file:{cookies}?mode=ro", uri=True)
        try:
            rows = db.execute("SELECT name FROM cookies WHERE host_key LIKE '%google.com'").fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return False
    return any(name in SESSION_COOKIES for (name,) in rows)


def _username(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def sign_in_meet(profile_dir: Path, out: TextIO = sys.stdout, display: Optional[str] = None,
                 executable: Optional[str] = None, runner: Callable = subprocess.run,
                 uid: Optional[int] = None) -> int:
    uid = os.geteuid() if uid is None else uid
    if not display:
        print("Run this on the room device with its screen, for example: "
              "DISPLAY=:0 croom --sign-in-meet -c /etc/croom/config.yaml", file=out)
        return 1
    if uid == 0:
        print("Run this as the user the room service runs as, not as root: "
              "sudo -u <that user> DISPLAY=:0 croom --sign-in-meet -c /etc/croom/config.yaml "
              "(the installer's completion text prints the exact command)", file=out)
        return 1
    if profile_dir.exists():
        owner = profile_dir.stat().st_uid
        if owner != uid:
            print(f"{profile_dir} belongs to {_username(owner)}, the user the room service runs as; "
                  f"run this as that user: sudo -u {_username(owner)} DISPLAY=:0 croom --sign-in-meet -c /etc/croom/config.yaml", file=out)
            return 1
    holder = profile_holder(profile_dir)
    if holder is not None:
        print(f"The room service is using {profile_dir} (process {holder}); stop it first: sudo systemctl stop croom", file=out)
        return 1
    profile_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(profile_dir, 0o700)
    ensure_browsers_path()
    executable = executable or chromium_executable()
    print("A browser opens on the room's screen. Sign in there as the room's Google user, "
          "then close the browser window.", file=out)
    print(f"Profile: {profile_dir}", file=out)
    runner([executable, f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check",
            "--password-store=basic", "--window-size=1920,1080", SIGN_IN_URL],
           check=False, env={**os.environ, "DISPLAY": display})
    if session_saved(profile_dir):
        print("Profile saved. Test it with: croom --check-meet <meeting link> -c /etc/croom/config.yaml, "
              "then start the service: sudo systemctl start croom", file=out)
        return 0
    print("No session was saved: the browser closed before the sign-in finished. Run this again.", file=out)
    return 1
