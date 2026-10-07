"""
Playwright finds the bundled Chromium through PLAYWRIGHT_BROWSERS_PATH; the room
installer puts it under /opt/croom/browsers and the service unit sets the variable.
A command run by hand gets the same default when that folder exists, so only
DISPLAY has to be typed.
"""

import os
from pathlib import Path
from typing import MutableMapping, Optional

DEFAULT_BROWSERS_PATH = Path("/opt/croom/browsers")


def ensure_browsers_path(env: MutableMapping[str, str] = os.environ,
                         candidate: Path = DEFAULT_BROWSERS_PATH) -> Optional[str]:
    """The browsers folder Playwright will use, after defaulting it; None when unknown."""
    if env.get("PLAYWRIGHT_BROWSERS_PATH"):
        return env["PLAYWRIGHT_BROWSERS_PATH"]
    if candidate.is_dir():
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(candidate)
        return str(candidate)
    return None


def profile_holder(profile_dir: Path) -> Optional[int]:
    """The pid of a Chromium on this host that holds the profile, or None.

    Chromium's lock is a dangling symlink, SingletonLock -> "<hostname>-<pid>", so
    Path.exists() never sees it. A lock naming a dead process is what a crash leaves
    behind; Chromium replaces it on its next start, so it does not count as held."""
    import socket

    lock = profile_dir / "SingletonLock"
    if not lock.is_symlink():
        return None
    host, sep, pid = os.readlink(lock).rpartition("-")
    if not sep or host != socket.gethostname() or not pid.isdigit():
        return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # alive, owned by someone else
    return int(pid)
