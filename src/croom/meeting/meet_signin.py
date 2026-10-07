"""
`croom --sign-in-meet`: open the room's own Chromium plainly, with no automation,
on the room's screen, so a person signs the room in to its Google Workspace
account once (spec 2026-10-07 Google Meet, section 4.4). The session stays in
the profile folder the Meet provider uses.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, TextIO

from croom.meeting.browser_env import ensure_browsers_path

SIGN_IN_URL = "https://accounts.google.com/"


def chromium_executable() -> str:
    """The bundled Chromium's binary, found the way Playwright finds it."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        return p.chromium.executable_path


def sign_in_meet(profile_dir: Path, out: TextIO = sys.stdout, display: Optional[str] = None,
                 executable: Optional[str] = None, runner: Callable = subprocess.run) -> int:
    if not display:
        print("Run this on the room device with its screen, for example: "
              "DISPLAY=:0 croom --sign-in-meet -c /etc/croom/config.yaml", file=out)
        return 1
    if (profile_dir / "SingletonLock").exists():
        print(f"The room service is using {profile_dir}; stop it first: sudo systemctl stop croom", file=out)
        return 1
    profile_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(profile_dir, 0o700)
    ensure_browsers_path()
    executable = executable or chromium_executable()
    print("A browser opens on the room's screen. Sign in there as the room's Google user, "
          "then close the browser window.", file=out)
    print(f"Profile: {profile_dir}", file=out)
    runner([executable, f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check",
            "--window-size=1920,1080", SIGN_IN_URL], check=False, env={**os.environ, "DISPLAY": display})
    if (profile_dir / "Default" / "Cookies").exists():
        print("Profile saved. Test it with: croom --check-meet <meeting link> -c /etc/croom/config.yaml, "
              "then start the service: sudo systemctl start croom", file=out)
        return 0
    print("No session was saved: the browser closed before the sign-in finished. Run this again.", file=out)
    return 1
