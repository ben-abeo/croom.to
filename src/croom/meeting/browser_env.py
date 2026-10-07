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
