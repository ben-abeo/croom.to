"""
Settings › Check (spec 2026-10-29, section 3): the `croom --check-…` commands, run inside the agent from the touch Pi, one button each,
answered in the words the commands print.

Each check runs in-process, its lines written to a StringIO, and is answered {"ok", "text"} within CHECK_TIMEOUT_S: what it had said by
then, and that it ran out of time, when it takes longer. A check that is still running when it is asked for again is refused, "Already
running"; the others go on side by side.

What each one looks at:
- **calendar** and **zoom**: `check_calendar` and `check_zoom` on the config file as it is now, as the commands load it (a key saved a
  moment ago is the one checked, before the restart that puts it to use).
- **tv**: the agent's own TV service (`check_running_tv`), which is connected to the TV already: a second connection beside it could show
  the TV's pairing prompt or take its place, so the check opens none and reads what the service knows. Without a TV service (a room
  where it could not be made) it is `check_tv` itself, on the config file.
- **laptop**: the running Laptop service's switch, looked at and never pressed (`LaptopService.check`); none when Laptop mode is off.
- **google**: whether the room's Google profile holds a Google session (`MeetingService.sign_in_state`): the cookie store is read, no
  browser is opened.

None of them is refused during a meeting: none presses, switches, wakes or takes anything the meeting is using. (The TV and the Laptop
switch are only looked at; Google's session is read from the profile's cookie store, which the meeting's browser leaves readable; the
calendar and Zoom checks ask Google and Zoom over the network.)
"""

import asyncio
import io
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Set, TextIO

from croom.core.config import Config
from croom.settings.schema import SettingError

logger = logging.getLogger(__name__)

CHECK_TIMEOUT_S = 60.0
ALREADY_RUNNING = "Already running"
NAMES = ("calendar", "tv", "laptop", "google", "zoom")


class CheckBusy(Exception):
    """The check asked for is running already."""


class Checks:
    """The checks of one room. `load_config` gives the room's settings as the file has them now (ConfigEditor.config); `tv`, `laptop` and
    `meeting` are the agent's running services (None where the room has none). `calendar_provider`, `zoom_api`, `tv_service_factory` and
    `discover` are what the checks reach Google, Zoom and the TV with (the real ones unless a test stands in for them)."""

    def __init__(self, load_config: Callable[[], Config], tv=None, laptop=None, meeting=None, calendar_provider=None, zoom_api=None,
                 tv_service_factory=None, discover=None, timeout: float = CHECK_TIMEOUT_S):
        self._load_config = load_config
        self._tv, self._laptop, self._meeting = tv, laptop, meeting
        self._calendar_provider, self._zoom_api = calendar_provider, zoom_api
        self._tv_service_factory, self._discover = tv_service_factory, discover
        self._timeout = timeout
        self._running: Set[str] = set()
        self._runs: Dict[str, Callable[[TextIO], Awaitable[int]]] = {
            "calendar": self._calendar, "tv": self._tv_check, "laptop": self._laptop_check, "google": self._google, "zoom": self._zoom,
        }

    def running(self, name: str) -> bool:
        return name in self._running

    async def run(self, name: str) -> Dict[str, Any]:
        """{"ok": whether the check passed, "text": what it says}. KeyError for a check there is none of; CheckBusy while it runs."""
        check = self._runs[name]
        if name in self._running:
            raise CheckBusy(ALREADY_RUNNING)
        self._running.add(name)
        out = io.StringIO()
        try:
            try:
                code = await asyncio.wait_for(check(out), self._timeout)
            except asyncio.TimeoutError:
                print(f"The check did not finish within {self._timeout:g} seconds.", file=out)
                code = 1
            except SettingError as e:            # the config file could not be read
                print(e, file=out)
                code = 1
            except Exception as e:  # noqa: BLE001 - a check that breaks says so in its answer, as the command would on the screen
                logger.exception(f"Settings › Check › {name} failed")
                print(f"The check stopped: {type(e).__name__}: {e}", file=out)
                code = 1
        finally:
            self._running.discard(name)
        logger.info(f"Settings › Check › {name}: {'passed' if code == 0 else 'failed'}")
        return {"ok": code == 0, "text": out.getvalue().rstrip("\n")}

    # --- the checks

    # (Each command's module is imported when its check runs: the room page starts whether or not a check's libraries are there.)

    async def _calendar(self, out: TextIO) -> int:
        from croom.calendar.check import check_calendar
        return await check_calendar(self._load_config(), out, provider_factory=self._calendar_provider)

    async def _zoom(self, out: TextIO) -> int:
        from croom.meeting.zoom_check import check_zoom
        return await check_zoom(self._load_config(), out, api_factory=self._zoom_api)

    async def _tv_check(self, out: TextIO) -> int:
        from croom.tv.check import check_running_tv, check_tv
        if self._tv is not None:
            return check_running_tv(self._tv, out)
        return await check_tv(self._load_config(), out, service_factory=self._tv_service_factory, discover=self._discover)

    async def _laptop_check(self, out: TextIO) -> int:
        if self._laptop is not None:
            return await self._laptop.check(out)
        if self._load_config().laptop.enabled:
            print("Laptop mode is on in the settings, but the room software has not started it yet: restart the room software "
                  "(Restart now, on the Laptop switch page).", file=out)
        else:
            print("Laptop mode is off (the Laptop switch page): the room page has no Laptop button.", file=out)
        return 1

    async def _google(self, out: TextIO) -> int:
        if self._meeting is None or not hasattr(self._meeting, "sign_in_state"):
            print("The room's meetings are not running, so its Google sign-in cannot be looked at.", file=out)
            return 1
        state = await asyncio.to_thread(self._meeting.sign_in_state)
        if not state.get("configured"):
            print("The room has no Google profile folder (Google sign-in, on the Meetings page): set it, restart the room software, "
                  "then Sign in to Google.", file=out)
            return 1
        print(f"Google profile: {state.get('profile_dir')}", file=out)
        if state.get("active"):
            print("A sign-in to Google is open on the TV now: finish it with Done, or Cancel it.", file=out)
            return 1
        if not state.get("signed_in"):
            print("Not signed in: the profile holds no Google session. On the Meetings page, use Sign in to Google.", file=out)
            return 1
        print(f"Signed in as {state.get('address')}", file=out)
        platforms = _platforms(self._meeting)
        if platforms is not None and "google_meet" not in platforms:
            print("Google Meet is not one of the room's platforms (the Meetings page), so the room joins no Google Meet as this "
                  "account.", file=out)
        return 0


def _platforms(meeting: Any) -> Optional[list]:
    """The platforms the running meetings were started with, or None when the service does not say."""
    config = getattr(meeting, "config", None)
    platforms = getattr(getattr(config, "meeting", None), "platforms", None)
    return list(platforms) if isinstance(platforms, (list, tuple)) else None
