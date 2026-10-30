"""
Sign in to Google on the TV (spec 2026-10-29, sections 3 and 8): the room's own Google sign-in, done from the touch Pi.

The TV's meeting browser (TvDisplay) closes, and the bundled Chromium opens Google's sign-in page on the room's profile folder, plainly
and filling the TV, as `croom --sign-in-meet` does (no automation: Google refuses a sign-in it can tell is driven). The person moves the
room Pi's mouse and types with the touch Pi's touchpad and keyboard. Then:

- **Done**: the sign-in browser closes, the profile is looked at for Google's session cookies (meet_signin.session_saved) and the address
  it is signed in as (the profile's Preferences, `account_info`), and the TV's browser comes back.
- **Cancel**, 15 minutes without a pointer or key event, the sign-in browser closing by itself, or the agent stopping: the sign-in browser
  closes and nothing changes: the profile's cookies are put back as they were before the sign-in began, and the TV's browser comes back
  (at the agent's stop, the TV's browser stops with the agent).

One sign-in at a time. Whatever ends it, the TV gets its room browser back. The display, the process, the clock and the sleep are handed
in, so that the tests run on fakes.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from croom.meeting.browser_env import ensure_browsers_path, profile_holder
from croom.meeting.meet_signin import chromium_executable, session_saved, sign_in_command

logger = logging.getLogger(__name__)

LEAVE_FIRST = "Leave the meeting first"
SIGN_IN_FIRST = "Signing in to Google: finish or cancel it first"
SET_UP_FIRST = ("Set up Google Meet first: the room keeps its Google sign-in in a folder of its own (Settings, Meetings: Google "
                "sign-in), and starts using it after a restart")
NOT_SIGNING_IN = "The sign-in has ended already"
PROFILE_HELD = "The TV's browser did not let go of the room's Google profile: try again in a moment"
ROOM_ACCOUNT = "the room's Google account"
SIGNED_IN_AS = "Signed in as {address}"

# How a sign-in ended, and what the touch Pi says about it
DONE, CANCELLED, IDLE, CLOSED, STOPPED = "done", "cancelled", "idle", "closed", "stopped"
NO_SESSION = "Not signed in: Google did not keep a sign-in. Sign in again, and press Done once Google shows the account"
ENDED_WORDS = {
    CANCELLED: "Sign-in cancelled: nothing changed",
    IDLE: "The sign-in closed after 15 minutes without a touch: nothing changed",
    CLOSED: "The sign-in browser closed by itself: nothing changed",
    STOPPED: "The room software stopped during the sign-in: nothing changed",
}

Spawn = Callable[[List[str]], Awaitable[Any]]


class SignInError(Exception):
    """A sign-in that cannot begin or end now, in words for the person at the table, with the HTTP status the room page answers."""

    def __init__(self, words: str, status: int = 409):
        super().__init__(words)
        self.status = status


def signed_in_address(profile_dir: Path) -> Optional[str]:
    """The address the profile is signed in to Google as, from Chromium's Preferences (`account_info[].email`); None when it says none."""
    try:
        data = json.loads((Path(profile_dir) / "Default" / "Preferences").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    accounts = data.get("account_info") if isinstance(data, dict) else None
    if not isinstance(accounts, list):
        return None
    for account in accounts:
        email = account.get("email") if isinstance(account, dict) else None
        if isinstance(email, str) and "@" in email and len(email) <= 254 and email.isprintable():
            return email.strip()
    return None


def google_account(profile_dir: Path) -> Dict[str, Any]:
    """Whether the profile holds a Google session, and as whom: {"signed_in", "address"} (the address is ROOM_ACCOUNT when the profile
    holds a session and does not say whose)."""
    if not session_saved(Path(profile_dir)):
        return {"signed_in": False, "address": None}
    return {"signed_in": True, "address": signed_in_address(Path(profile_dir)) or ROOM_ACCOUNT}


async def _spawn(command: List[str]) -> Any:
    """The sign-in browser, as a process of the agent's (it inherits DISPLAY): its output is not the log's."""
    return await asyncio.create_subprocess_exec(*command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL)


async def _find_executable() -> str:
    """The bundled Chromium, found as `croom --sign-in-meet` finds it (Playwright's sync API, so in a thread)."""
    ensure_browsers_path()
    return await asyncio.to_thread(chromium_executable)


class SignInSession:
    """The room's sign-in to Google, on the TV. `display` is the TvDisplay (start(), stop(), and `started`: whether it was running, so a
    display that was not is not started by the sign-in's end); None is a room without one. `profile_dir` is the room's Google profile.

    spawn(command) starts the sign-in browser and gives back its process (asyncio's: pid, returncode, wait(), terminate(), kill());
    find_executable() is the bundled Chromium's path; clock and sleep count the 15 minutes; holder(profile) is the pid of a Chromium that
    holds the profile (the TV's, letting go of it)."""

    IDLE_S = 15 * 60            # a sign-in with no pointer or key event for this long is closed
    TERMINATE_WAIT_S = 5.0      # the sign-in browser is given this long to close (and write its cookies) before it is killed
    RELEASE_WAIT_S = 5.0        # the TV's browser is given this long to let go of the profile
    RELEASE_POLL_S = 0.1
    COOKIE_FILES = ("Cookies", "Cookies-journal")
    SNAPSHOT = ".croom-sign-in-before"   # in the profile folder: the cookies as they were, put back unless Done

    def __init__(self, display: Any, profile_dir: Path, spawn: Optional[Spawn] = None,
                 find_executable: Optional[Callable[[], Awaitable[str]]] = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, holder: Callable[[Path], Optional[int]] = profile_holder):
        self._display = display
        self.profile_dir = Path(profile_dir)
        self._spawn = spawn or _spawn
        self._find_executable = find_executable or _find_executable
        self._clock = clock
        self._sleep = sleep
        self._holder = holder
        self._phase = "idle"                     # idle, starting, open, ending
        self._lock = asyncio.Lock()              # a beginning and an end, one at a time: an end waits for the beginning
        self._process: Any = None
        self._display_was_on = False
        self._touched = 0.0
        self._watchers: List[asyncio.Task] = []
        self.last: Optional[Dict[str, Any]] = None   # how the last sign-in ended: {"outcome", "words", "at", "address"?}

    @property
    def active(self) -> bool:
        """From the moment a sign-in is asked for until the TV has its browser back."""
        return self._phase != "idle"

    def touch(self) -> None:
        """A pointer or key event on the room Pi: the 15 minutes start again."""
        if self._phase == "open":
            self._touched = self._clock()

    def state(self) -> Dict[str, Any]:
        answer: Dict[str, Any] = {"active": self.active, "last": self.last}
        if self._phase == "open":
            answer["idle_left_s"] = max(0, int(self._touched + self.IDLE_S - self._clock()))
        return answer

    # --- beginning

    async def begin(self) -> None:
        """Close the TV's browser and open Google's sign-in in its place. Nothing when a sign-in is on already. SignInError, in words,
        when it cannot be opened; the TV has its browser back then."""
        if self._phase != "idle":
            return
        self._phase = "starting"                 # (before anything is awaited: a join asked for meanwhile is refused)
        async with self._lock:
            try:
                await self._open()
            except BaseException as e:
                await self._close_browser()
                await self._give_back(keep=True)
                self._phase = "idle"
                if isinstance(e, SignInError) or not isinstance(e, Exception):
                    raise
                logger.error(f"Google's sign-in could not be opened on the TV: {type(e).__name__}: {e}")
                raise SignInError(f"Google's sign-in could not be opened on the TV: {e}", 500) from e
            self._phase = "open"
            self._touched = self._clock()
            process = self._process
            self._watchers = [asyncio.ensure_future(self._watch_idle()), asyncio.ensure_future(self._watch_exit(process))]
        logger.info(f"Signing in to Google on the TV (profile {self.profile_dir}, browser process {getattr(process, 'pid', '?')})")

    async def _open(self) -> None:
        display = self._display
        self._display_was_on = display is not None and bool(getattr(display, "started", True))
        if display is not None:
            await display.stop()
        await self._wait_for_release()
        await asyncio.to_thread(self._snapshot)
        executable = await self._find_executable()
        self._process = await self._spawn(sign_in_command(executable, self.profile_dir, kiosk=True))

    async def _wait_for_release(self) -> None:
        """The TV's browser has closed: Chromium lets go of the profile as it exits. A second Chromium on a profile that is held would
        only open a window in the first, and end at once."""
        deadline = self._clock() + self.RELEASE_WAIT_S
        while self._holder(self.profile_dir) is not None:
            if self._clock() >= deadline:
                raise SignInError(PROFILE_HELD)
            await self._sleep(self.RELEASE_POLL_S)

    # --- ending

    async def end(self, outcome: str, give_back: bool = True) -> Optional[str]:
        """End the sign-in: DONE keeps what Google saved and gives back the address it is signed in as (ROOM_ACCOUNT when the profile does
        not say), or None when Google kept no session; any other outcome puts the cookies back as they were and gives back None. The TV
        gets its browser back unless `give_back` is False (the agent is stopping, and the TV's browser with it). SignInError when there
        is no sign-in to end."""
        async with self._lock:
            if self._phase != "open":
                raise SignInError(NOT_SIGNING_IN)
            self._phase = "ending"
            address: Optional[str] = None
            try:
                self._stop_watchers()
                await self._close_browser()
                if outcome == DONE:
                    account = await asyncio.to_thread(google_account, self.profile_dir)
                    address = account["address"]
                await self._give_back(keep=outcome == DONE, start=give_back)
            finally:
                self._phase = "idle"
                if outcome == DONE:
                    words = SIGNED_IN_AS.format(address=address) if address else NO_SESSION
                else:
                    words = ENDED_WORDS.get(outcome, ENDED_WORDS[CANCELLED])
                self.last = {"outcome": outcome, "words": words, "address": address,
                             "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")}
                logger.info(f"Sign-in to Google ended ({outcome}): {words}")
            return address

    async def close(self) -> None:
        """The agent is stopping: a sign-in that is on ends (nothing changes), and the TV's browser is left to stop with the agent."""
        if self._phase == "starting":
            async with self._lock:   # (the beginning finishes first)
                pass
        if self._phase == "open":
            try:
                await self.end(STOPPED, give_back=False)
            except SignInError:
                pass

    def _stop_watchers(self) -> None:
        current = asyncio.current_task()
        for task in self._watchers:
            if task is not current and not task.done():
                task.cancel()
        self._watchers = []

    async def _close_browser(self) -> None:
        """The sign-in browser asked to close (it writes its cookies as it does), and killed when it has not within TERMINATE_WAIT_S."""
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), self.TERMINATE_WAIT_S)
            return
        except asyncio.TimeoutError:
            logger.warning(f"The sign-in browser did not close within {self.TERMINATE_WAIT_S:g} s; killing it")
        try:
            process.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), self.TERMINATE_WAIT_S)
        except asyncio.TimeoutError:
            logger.error("The sign-in browser did not end when killed")

    async def _give_back(self, keep: bool, start: bool = True) -> None:
        """The profile as it was unless `keep`, then the TV's browser back, if it was on before. Raises nothing: the TV's browser comes
        back (or keeps trying to, as TvDisplay does) whatever happened to the sign-in."""
        try:
            await asyncio.to_thread(self._restore if not keep else self._drop_snapshot)
        except Exception as e:  # noqa: BLE001 - the TV must come back whatever
            logger.error(f"The room's Google profile could not be put back as it was: {type(e).__name__}: {e}")
        if start and self._display is not None and self._display_was_on:
            try:
                await self._display.start()
            except Exception as e:  # noqa: BLE001 - the display tries again by itself at its next use
                logger.error(f"The TV's browser did not start again after the sign-in: {type(e).__name__}: {e}")

    # --- the profile's cookies, as they were

    def _snapshot(self) -> None:
        """Keep a copy of the profile's cookies (the TV's browser has closed: nobody is writing them), to put back unless Done."""
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.profile_dir, 0o700)
        folder = self.profile_dir / self.SNAPSHOT
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(mode=0o700)
        for name in self.COOKIE_FILES:
            live = self.profile_dir / "Default" / name
            if live.is_file():
                shutil.copy2(live, folder / name)
        (folder / "complete").write_text("")

    def _restore(self) -> None:
        """Put the cookies back as the snapshot has them (a file there was none of is removed), and drop the snapshot."""
        folder = self.profile_dir / self.SNAPSHOT
        if not (folder / "complete").is_file():
            return
        for name in self.COOKIE_FILES:
            kept, live = folder / name, self.profile_dir / "Default" / name
            if kept.is_file():
                live.parent.mkdir(parents=True, exist_ok=True)
                spare = live.with_name(live.name + ".croom-restore")
                shutil.copy2(kept, spare)
                os.replace(spare, live)
            else:
                live.unlink(missing_ok=True)
        self._drop_snapshot()

    def _drop_snapshot(self) -> None:
        shutil.rmtree(self.profile_dir / self.SNAPSHOT, ignore_errors=True)

    # --- what ends a sign-in by itself

    async def _watch_idle(self) -> None:
        while True:
            left = self._touched + self.IDLE_S - self._clock()
            if left <= 0:
                break
            await self._sleep(left)
        await self._end_by_itself(IDLE)

    async def _watch_exit(self, process: Any) -> None:
        await process.wait()
        await self._end_by_itself(CLOSED)

    async def _end_by_itself(self, outcome: str) -> None:
        try:
            await self.end(outcome)
        except SignInError:
            pass   # ended already
        except Exception:  # noqa: BLE001 - nobody waits for this task: logged
            logger.exception(f"The sign-in did not end cleanly ({outcome})")
