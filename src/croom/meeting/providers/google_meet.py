"""
Google Meet provider.

Handles joining and controlling Google Meet meetings using browser automation.
"""

import asyncio
import logging
import re
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Dict, Any, Callable, List, Tuple

from croom.core.config import DEFAULT_ACKNOWLEDGE, DEFAULT_REACTIONS
from croom.core.logs import DIALOGS_LOGGER
from croom.meeting.meet_capture import JOIN_MEET_FIRST, MeetCapture, save_capture
from croom.meeting.meet_page import (
    MEET,
    PRESS_BY_LABEL,
    PRESS_DIALOG,
    READ_CALL,
    READ_CHAT,
    READ_DIALOGS,
    READ_HOST_SWITCHES,
    READ_PANELS,
    READ_PEOPLE,
)
from croom.meeting.providers.base import (
    DialogPolicy,
    MeetingControlError,
    MeetingInfo,
    MeetingProvider,
    MeetingState,
    controls_from,
)

logger = logging.getLogger(__name__)
# What Meet draws over the call, and what became of it, goes to the dialogs' own logger, as Zoom's does (core/logs.py).
dialog_log = logging.getLogger(DIALOGS_LOGGER)

# Playwright is optional - used for browser automation
try:
    from playwright.async_api import Page
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# What the table is told when the bare C key cannot go to the page: it would be typed into whatever has the keyboard (the chat box).
KEYBOARD_TAKEN_WORDS = "Meet's chat box has the keyboard; close it on the TV first"
# What a tap on a box that Meet has taken off the screen is told.
DIALOG_GONE_WORDS = "That box is no longer on Meet's screen"
# What a tap on a box is told while Settings' capture of Meet's page runs (it holds the boxes for up to a minute).
CAPTURING_WORDS = "The room is capturing Meet's page; try again in a minute"
# What a screen the touch Pi names that Meet has no panel for is told.
SCREEN_WORDS = "Say which Meet screen is open: chat, participants, host, or none"

# The touch Pi's Meet screens (Meet controls plan Phase B, Task 2) and the side panel of Meet's each shows on the TV; Meet shows one at a time.
SCREEN_PANELS = {"chat": "chat", "participants": "people", "host": "host"}
# Each panel's button, by MEET's words, and Meet's own shortcut for it where it has one (the host controls have none); its name in words.
PANEL_OPENERS = {"chat": MEET["chat"]["labels"], "people": MEET["people"]["labels"], "host": MEET["host_controls"]}
PANEL_SHORTCUTS = {"chat": MEET["chat"]["shortcut"], "people": MEET["people"]["shortcut"]}
PANEL_WORDS = {"chat": "chat", "people": "people", "host": "host controls"}
CLOSE_WORDS = ["Close"]           # the Close button inside a panel
LAYOUTS = ["auto", "tiled", "spotlight", "sidebar"]
PEOPLE_LISTED = 50                # the roster the table is given, at most
CHAT_KEPT = 20                    # the last messages the table is given


class _DialogGone(MeetingControlError):
    """The box a press was for is not on the screen (it went by itself, or someone at the TV answered it)."""


def meet_controls(buttons: Dict[str, Dict[str, Any]], muted: bool, *, drawn: Optional[Dict[str, bool]] = None, people: bool = False,
                  hosting: bool = False, reactions: Optional[List[str]] = None, layout: Optional[str] = None, hide_self: bool = False,
                  switches: Optional[List[Dict[str, Any]]] = None, waiting: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The table's control model (spec 2026-10-13, section 3; Meet controls plan Phase B, Task 2) for what Meet's page offers: Zoom's shape,
    from the buttons the page has (camera, raise_hand, captions, each with its state) and the microphone's state, plus what Meet's call page
    says of itself: `drawn` (READ_CALL's: the chat, reactions and more options buttons on the screen), `people` (the people button is),
    `hosting` (the host controls button is). The room's own choices this call (`layout`, `hide_self`), the reactions the picker held the last
    time it was open (else the configured ones), the host panel's `switches` and the people panel's `waiting` are the provider's.

    Added to Zoom's shape: `chat_send` {"available"}; `layout.choices` Meet's four and `layout.value` the room's last choice or None (Meet shows
    the current one only inside its dialog); `host.waiting_room.deny` (Meet's knock has Deny); `host.switches` (the host panel's switches as
    read, [] until it has been open). Meet does not tell a host from a co-host on the call page (both have the host controls): the role is
    "host" with them. Recording, locking, asking to unmute and muting one person are not driven in Meet."""
    drawn = drawn or {}
    model = controls_from({"me": {"muted": muted}, "role": "host" if hosting else "participant", "buttons": buttons, "participants": [],
                           "recording": False}, [])
    choices = list(reactions or [])
    model["reactions"] = {"available": bool(drawn.get("reactions")) and bool(choices), "choices": choices}
    model["chat"] = {"available": bool(drawn.get("chat"))}
    model["chat_send"] = {"available": bool(drawn.get("chat"))}
    model["layout"] = {"available": bool(drawn.get("more_options")), "choices": list(LAYOUTS), "value": layout}
    model["hide_self"] = {"available": bool(drawn.get("more_options")), "on": bool(hide_self)}
    model["pin"] = {"available": bool(people)}
    host = model["host"]
    host["waiting_room"] = {"available": hosting, "deny": True,
                            "waiting": [{"id": p.get("id"), "name": p.get("name", "")} for p in waiting or [] if isinstance(p, dict)]}
    host["record"] = {"available": False, "on": False}
    host["lock"] = {"available": False, "on": False}
    host["ask_unmute"] = {"available": False}
    host["mute_one"] = {"available": False}
    host["switches"] = [dict(switch) for switch in switches or [] if isinstance(switch, dict)]
    return model


def _first_line(error: BaseException) -> str:
    """The first line of an error's words (Playwright's carry a call log after it)."""
    return next((line.strip() for line in str(error).splitlines() if line.strip()), "") or type(error).__name__


class GoogleMeetProvider(MeetingProvider):
    """
    Google Meet meeting provider.

    Uses Playwright for browser automation to join and control meetings.
    """

    # URL patterns for Google Meet
    MEET_URL_PATTERN = re.compile(
        r"(https?://)?meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})",
        re.IGNORECASE
    )
    MEET_CODE_PATTERN = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$", re.IGNORECASE)

    # Meet's guest pre-join page (October 2026, seen on a room Pi): the name field's only
    # hint is its placeholder, and "Ask to join" stays disabled until a name is typed.
    NAME_SELECTORS = ['input[aria-label="Your name"]', 'input[placeholder="Your name"]',
                      'input[placeholder*="name" i]', 'input[aria-label*="name" i]']
    JOIN_SELECTORS = ['button:has-text("Ask to join")', 'button:has-text("Join now")',
                      '[role="button"]:has-text("Ask to join")', '[role="button"]:has-text("Join now")',
                      '[aria-label*="join" i][role="button"]', 'button[jsname="Qx7uuf"]']
    JOIN_FIND_TIMEOUT_MS = 4000      # per selector while looking for the join control
    JOIN_ENABLE_TIMEOUT_MS = 10000   # how long the control may stay disabled after the name is typed
    TOGGLE_TIMEOUT_MS = 3000         # per selector while looking for a camera or microphone button
    PAGE_SETTLE_S = 2                # after the page has loaded, before the pre-join screen is looked at
    LEAVE_SETTLE_S = 1               # after the Leave button is pressed, before the TV goes back to the screensaver
    CONNECT_TIMEOUT_MS = 30000       # for Meet's in-call controls after pressing Join
    ADMIT_TIMEOUT_MS = 300000        # how long a host may take to admit the room
    LOBBY_PHRASES = ("waiting for", "asking to join", "someone lets you in", "let you in")
    SIGN_IN_MESSAGE = ("The room's Google sign-in has expired or was never done; "
                       "stop the service and run croom --sign-in-meet on the device")

    # The call's controls (spec 2026-10-13, section 4.5): each button is pressed with Meet's own keyboard shortcut and its state is read back
    # from its label (READ_CALL), never assumed. The labels are Meet's English as the controls plan words them, not yet confirmed on a room
    # Pi; a button whose label does not say its state is not offered.
    SHORTCUTS = {"mute": "Control+d", "camera": "Control+e", "raise_hand": "Control+Alt+h", "captions": "c"}
    CONTROL_WORDS = {"mute": "microphone", "camera": "camera", "raise_hand": "raise hand", "captions": "captions"}
    READ_INTERVAL_S = 5   # while connected the buttons are read again this often: the host may mute the room, captions may be turned on at the TV
    PRESS_WAIT_S = 2      # a press waits this long for its button's label to change ...
    PRESS_POLL_S = 0.05   # ... looking this often
    LOOK_TIMEOUT_S = 10   # one reading of the page answers within this, or the page is stuck (a frozen tab would hold the press lock for good)
    # Meet's boxes (spec 2026-10-30 Meet controls, section 3): read this often while connected, and once right after the join.
    DIALOG_INTERVAL_S = 2
    ANSWER_SPACING_S = 1   # the provider presses at most one notice's button in this time by itself, whatever Meet draws
    DIALOG_POLL_S = 0.2    # after a box's button is pressed, the boxes are read this often until it has gone
    # The touch Pi's Meet screens (Meet controls plan Phase B, Task 2): the page renews the one it has open every 60 s; a screen not renewed for
    # this long is taken as closed (the page went away) and the panel the room opened for it is closed.
    SCREEN_TIMEOUT_S = 180

    def __init__(self, display=None, room_name: str = "Conference Room", clock: Callable[[], float] = time.monotonic):
        super().__init__()
        self._clock = clock               # when the touch Pi last said which screen it has open (a test gives its own)
        self.reactions: List[str] = list(DEFAULT_REACTIONS)   # offered until Meet's picker has been seen
        # The TV display (spec 2026-10-07 TV, section 4.3) owns the browser and, when configured, the
        # signed-in Google profile (spec 2026-10-07 Google Meet, section 4.2); this provider borrows its page.
        self._display = display
        self._room_name = room_name or "Conference Room"
        # Where a screenshot goes when a join fails, so the TV need not be watched.
        self.failure_screenshot: Path = Path(tempfile.gettempdir()) / "croom-meet-failure.png"
        self._page: Optional["Page"] = None
        # What the provider starts on its own, kept so that the end of the call stops it: the regular reading of Meet's buttons.
        self._reader: Optional[asyncio.Task] = None
        self._read_warned = False         # a read that fails is said once for a call
        self._pressing = asyncio.Lock()   # one press at a time: two shortcuts sent at once could meet in the middle (Ctrl still down for the C)
        # Meet's boxes: the policy, the regular reading of them, and one reading or answer at a time (a stale reading taken while a tap
        # answered a box would see it as a new one).
        self.dialog_policy = DialogPolicy(DEFAULT_ACKNOWLEDGE)
        self._dialog_reader: Optional[asyncio.Task] = None
        self._dialog_turn = asyncio.Lock()
        self._capturing = False   # Settings' capture of Meet's page is running (it holds _dialog_turn for up to a minute)
        self._next_dialog_id = 0
        self._reset_dialogs()
        self._reset_panels()

    @classmethod
    def from_config(cls, config, display=None) -> "GoogleMeetProvider":
        provider = cls(display, room_name=config.room.name or "Conference Room")
        provider.dialog_policy = DialogPolicy(config.meeting.dialogs.acknowledge, config.meeting.dialogs.ask_everything)
        provider.reactions = list(config.meeting.controls.reactions)
        return provider

    @property
    def display(self):
        return self._display

    @property
    def _signed_in(self) -> bool:
        """Whether the TV browser runs on a signed-in profile; a guest sees Meet's name field."""
        return self._display is not None and getattr(self._display, "profile_dir", None) is not None

    @property
    def room_name(self) -> str:
        return self._room_name

    @property
    def name(self) -> str:
        return "google_meet"

    @property
    def display_name(self) -> str:
        return "Google Meet"

    @classmethod
    def can_handle_url(cls, url: str) -> bool:
        """Check if URL is a Google Meet link."""
        if cls.MEET_URL_PATTERN.match(url):
            return True
        if cls.MEET_CODE_PATTERN.match(url):
            return True
        return "meet.google.com" in url.lower()

    @classmethod
    def extract_meeting_id(cls, url: str) -> Optional[str]:
        """Extract meeting code from URL."""
        # Try URL pattern
        match = cls.MEET_URL_PATTERN.search(url)
        if match:
            return match.group(2).lower()

        # Try code pattern
        if cls.MEET_CODE_PATTERN.match(url):
            return url.lower()

        return None

    async def initialize(self) -> None:
        """Borrow the TV page; the display owns the browser and the signed-in profile."""
        if self._display is None:
            raise RuntimeError("Google Meet provider needs the TV display")
        self._page = await self._display.page()
        logger.info("Google Meet provider ready on the TV page")

    async def shutdown(self) -> None:
        """Leave the meeting if still in one; the display owns the browser."""
        await self._stop_reading()
        if self._state == MeetingState.CONNECTED:
            await self.leave_meeting()
        self._page = None
        logger.info("Google Meet provider shutdown")

    async def _show_idle(self) -> None:
        """Hand the page back to the screensaver."""
        if self._display is not None:
            await self._display.show_idle()
        elif self._page is not None:
            await self._page.goto("about:blank")

    async def join_meeting(
        self,
        meeting_url: str,
        display_name: str = "Conference Room",
        camera_on: bool = True,
        mic_on: bool = True,
        passcode: Optional[str] = None
    ) -> MeetingInfo:
        """Join a Google Meet meeting. A passcode is accepted and ignored: only Zoom uses one."""
        if self._display is not None:
            self._page = await self._display.claim()   # nothing parks the page again until the meeting ends
        if not self._page:
            raise RuntimeError("Provider not initialized")

        meeting_id = self.extract_meeting_id(meeting_url)
        if not meeting_id:
            raise ValueError(f"Invalid Google Meet URL: {meeting_url}")

        # Construct full URL
        full_url = f"https://meet.google.com/{meeting_id}"

        await self._stop_reading()   # never two readers: a join over a call still going stops that call's first
        self._read_warned = False
        self._reset_panels()
        self._current_meeting = MeetingInfo(
            platform=self.name,
            meeting_id=meeting_id,
            meeting_url=full_url,
            is_camera_on=camera_on,
            is_muted=not mic_on
        )

        self._set_state(MeetingState.JOINING)
        logger.info(f"Joining Google Meet: {meeting_id}")

        try:
            # Navigate to meeting
            await self._page.goto(full_url, wait_until="networkidle")

            # Wait for page to load
            await asyncio.sleep(self.PAGE_SETTLE_S)

            # Handle pre-join screen
            await self._handle_prejoin(display_name, camera_on, mic_on)

            # Click join button
            await self._click_join_button()

            # Wait for connection
            await self._wait_for_connection()

            # The table's model is in place before the call is announced, then kept up to date for as long as the call lasts.
            await self._read_controls()
            self._set_state(MeetingState.CONNECTED)
            self._reader = asyncio.create_task(self._read_regularly())
            self._dialog_reader = asyncio.create_task(self._read_dialogs_regularly())   # its first reading is at once
            logger.info(f"Connected to Google Meet: {meeting_id}")

            return self._current_meeting

        except Exception as e:
            logger.error(f"Failed to join meeting: {e}")
            self._current_meeting.error_message = str(e)
            # A kiosk has no address bar or Back button. Return the borrowed page
            # even when Google refuses the call or redirects to Workspace, keeping
            # the original failure (and any screenshot) for the room controls.
            try:
                await self._show_idle()
            except Exception as idle_error:
                logger.warning(f"Could not return the TV to Crystal Meet: {idle_error}")
            self._set_state(MeetingState.ERROR)
            raise

    async def _handle_prejoin(
        self,
        display_name: str,
        camera_on: bool,
        mic_on: bool
    ) -> None:
        """Handle pre-join screen settings."""
        # Type the room's name: "Ask to join" stays disabled without one.
        name_input = await self._find_first(self.NAME_SELECTORS, timeout=2500)
        if name_input is None:
            try:
                by_placeholder = self._page.get_by_placeholder(re.compile(r"your name", re.IGNORECASE))
                if await by_placeholder.count():
                    name_input = await by_placeholder.first.element_handle()
            except Exception:
                name_input = None
        if name_input is not None:
            await name_input.fill(display_name)
        elif not self._signed_in:
            logger.warning("Meet pre-join name field not found; trying to join without a name")
        else:
            logger.debug("Signed in: Meet shows no name field")

        await self._set_toggle("camera", camera_on)
        await self._set_toggle("microphone", mic_on)

    async def _set_toggle(self, device: str, wanted_on: bool) -> None:
        """Put Meet's pre-join camera or microphone button in the wanted state. The label
        says the current state: "Turn on microphone" means it is off, "Turn off" means on."""
        button = await self._find_first(
            [f'button[aria-label*="{device}" i]', f'[role="button"][aria-label*="{device}" i]'],
            timeout=self.TOGGLE_TIMEOUT_MS,
        )
        if button is None:
            logger.warning(f"Meet pre-join {device} button not found; joining with Meet's default")
            return
        label = (await button.get_attribute("aria-label") or "").lower()
        if "turn on" not in label and "turn off" not in label:
            logger.warning(f"Meet pre-join {device} button has an unexpected label {label!r}; leaving it alone")
            return
        if ("turn off" in label) == wanted_on:
            return
        try:
            await button.click(timeout=self.TOGGLE_TIMEOUT_MS)
            await asyncio.sleep(0.5)
            label = (await button.get_attribute("aria-label") or "").lower()
        except Exception as e:
            logger.warning(f"Meet pre-join {device} button could not be pressed ({type(e).__name__}); "
                           "joining with Meet's current setting")
            return
        logger.info(f"Meet pre-join {device} is now {'on' if 'turn off' in label else 'off'} "
                    f"(wanted {'on' if wanted_on else 'off'})")

    async def _find_first(self, selectors, timeout=3000):
        """The first element any of the selectors finds, or None."""
        for selector in selectors:
            try:
                element = await self._page.wait_for_selector(selector, timeout=timeout)
            except Exception:
                continue
            if element:
                return element
        return None

    async def _page_words(self) -> str:
        """The first words on the page, for an error message a person can act on."""
        try:
            text = await self._page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            return ""
        return " ".join(text.split())[:300]

    async def _needs_sign_in(self) -> bool:
        """True on Google's sign-in page: the profile's session is gone or was never made."""
        if "accounts.google.com" in (self._page.url or "").lower():
            return True
        return (await self._page_words()).lower().startswith("sign in")

    async def _failure(self, reason: str) -> RuntimeError:
        """An error that quotes what Meet shows and points at a screenshot of it."""
        words = await self._page_words()
        message = f'{reason}; Meet shows: "{words}"' if words else reason
        try:
            self.failure_screenshot.parent.mkdir(parents=True, exist_ok=True)
            await self._page.screenshot(path=str(self.failure_screenshot))
            message += f" (screenshot: {self.failure_screenshot})"
        except Exception as e:
            logger.debug(f"Could not save the Meet failure screenshot: {e}")
        return RuntimeError(message)

    async def _click_join_button(self) -> None:
        """Press Meet's join control once it is enabled; otherwise say what Meet shows instead."""
        button = await self._find_first(self.JOIN_SELECTORS, timeout=self.JOIN_FIND_TIMEOUT_MS)
        if button is None:
            if await self._needs_sign_in():
                raise RuntimeError(self.SIGN_IN_MESSAGE)
            raise await self._failure("Could not find Meet's join button")
        deadline = time.monotonic() + self.JOIN_ENABLE_TIMEOUT_MS / 1000
        while True:
            aria_disabled = await button.get_attribute("aria-disabled")
            if await button.is_enabled() and aria_disabled != "true":
                await button.click(timeout=self.JOIN_ENABLE_TIMEOUT_MS)
                return
            if time.monotonic() > deadline:
                raise await self._failure("Meet's join button stayed disabled")
            await asyncio.sleep(0.25)

    async def _wait_for_connection(self) -> None:
        """Wait for Meet's in-call controls; a knock that is waiting for the host is the lobby."""
        try:
            await self._page.wait_for_selector('[aria-label*="Leave" i]', timeout=self.CONNECT_TIMEOUT_MS)
            return
        except Exception:
            pass
        words = (await self._page_words()).lower()
        if any(phrase in words for phrase in self.LOBBY_PHRASES):
            self._set_state(MeetingState.IN_LOBBY)
            logger.info("Asked to join; waiting for the host to admit the room")
            try:
                await self._page.wait_for_selector('[aria-label*="Leave" i]', timeout=self.ADMIT_TIMEOUT_MS)
                return
            except Exception:
                raise await self._failure("The host did not admit the room in time")
        raise await self._failure("Failed to join meeting")

    async def leave_meeting(self) -> None:
        """Leave the current meeting."""
        if not self._page or self._state == MeetingState.IDLE:
            return

        await self._stop_reading()
        self._set_state(MeetingState.LEAVING)
        logger.info("Leaving Google Meet...")
        await self._close_room_panel_at_the_end()

        try:
            # Click leave button
            leave_btn = await self._page.query_selector('[aria-label*="Leave" i]')
            if leave_btn:
                await leave_btn.click()
                await asyncio.sleep(self.LEAVE_SETTLE_S)

            # Hand the TV back to the screensaver
            await self._show_idle()

        except Exception as e:
            logger.error(f"Error leaving meeting: {e}")

        self._current_meeting = None
        self._reset_panels()
        self._set_state(MeetingState.IDLE)
        logger.info("Left Google Meet")

    async def toggle_camera(self) -> bool:
        """Toggle the camera with Meet's shortcut (Ctrl+E). Returns what the button's label says afterwards (True = on); a press Meet
        did not take is a MeetingControlError in words, not a success."""
        if not self._page or self._state != MeetingState.CONNECTED:
            return False
        on = await self._press("camera")
        await self._read_controls()
        return on

    async def toggle_mute(self) -> bool:
        """Toggle the microphone with Meet's shortcut (Ctrl+D). Returns what the button's label says afterwards (True = muted); a press
        Meet did not take is a MeetingControlError in words, not a success."""
        if not self._page or self._state != MeetingState.CONNECTED:
            return True
        live = await self._press("mute")
        await self._read_controls()
        return not live

    async def set_camera(self, on: bool) -> bool:
        """Set the camera to the state asked for (True is on): the button's label is read under the press lock and Ctrl+E is pressed only when
        the label does not say so already, so a request made against a state that has changed since the table drew its button (the camera
        turned off at the TV a moment before a tap on Turn camera off) ends as the person asked. Returns what the label says afterwards."""
        if not self._page or self._state != MeetingState.CONNECTED:
            return False
        now = await self._press("camera", wanted=bool(on))
        await self._read_controls()
        return now

    async def set_mute(self, muted: bool) -> bool:
        """Set the microphone to the state asked for (True is muted), as set_camera does for the camera: the label is read under the lock and
        Ctrl+D is pressed only when it differs. Returns what the label says afterwards (True = muted)."""
        if not self._page or self._state != MeetingState.CONNECTED:
            return True
        live = await self._press("mute", wanted=not muted)
        await self._read_controls()
        return not live

    # ------------------------------------------------------------------
    # Controls from the table (spec 2026-10-13, sections 3 and 4.5)
    # ------------------------------------------------------------------

    async def control(self, name: str, value: Any = None) -> Dict[str, Any]:
        """A control from the table, carried out with Meet's own keyboard shortcut (see MeetingProvider.control): the microphone, the
        camera, the raised hand and the captions; "dialog", a tap on a button of the box waiting on the table ({"id", "button"}); and
        "screen", the touch Pi's Meet screen that is open ("chat", "participants", "host", or "" for none; _on_screen_said). Everything else
        Meet's page does not offer here is refused in words. The answer is the control model as it is afterwards."""
        if self._page is None or self._state != MeetingState.CONNECTED:
            raise MeetingControlError("Not in a meeting")
        if name == "screen":
            return await self._on_screen_said(value)
        if name == "mute":
            await self.toggle_mute()
        elif name in self.SHORTCUTS:
            await self._press(name)
        elif name == "leave":
            await self.leave_meeting()                    # the model says leave is available (the table's Leave has its own route): the same answer as Zoom's
            return {}
        elif name == "chat.seen":
            return {"chat": self._current_meeting.chat}   # the agent reads no chat from Meet: nothing to mark
        elif name == "dialog":
            return await self._answer_from_table(value)
        else:
            raise MeetingControlError("Not available in a Meet call")
        return await self._read_controls()

    async def _press(self, name: str, wanted: Optional[bool] = None) -> bool:
        """Press a control's shortcut and wait for the change to show in its button's label (for PRESS_WAIT_S at most), one press at a
        time; the answer is the state the label shows then (True is on: the microphone live, the camera on, the hand up, the captions on).
        With `wanted` the control is set to that state instead of flipped: the label is read here, under the lock, and when it says so
        already nothing is pressed and the answer is that state (a press that was in the way of this one has done the work). A control whose
        button is not on the page is refused before anything is pressed, and so is the bare C key while something that takes typing has the
        keyboard (it would be typed into the chat box). A press that changes no label is a refusal in words, never a success."""
        words = self.CONTROL_WORDS[name]
        async with self._pressing:
            try:
                reading = await self._look()
                before = reading.get(name)
                if before is None:
                    raise MeetingControlError(f"Meet shows no {words} button right now")
                if wanted is not None and bool(before["on"]) == wanted:
                    return wanted
                if name == "captions" and reading.get("taken"):
                    raise MeetingControlError(KEYBOARD_TAKEN_WORDS)
                await self._page.keyboard.press(self.SHORTCUTS[name])
                deadline = time.monotonic() + self.PRESS_WAIT_S
                while True:
                    after = (await self._look()).get(name)
                    if after is not None and after["on"] != before["on"]:
                        return bool(after["on"])
                    if time.monotonic() >= deadline:
                        raise MeetingControlError(f"Meet did not change the {words}; try again")
                    await asyncio.sleep(self.PRESS_POLL_S)
            except MeetingControlError:
                raise
            except Exception as e:  # noqa: BLE001 - the page gone or navigating: words for the table
                raise MeetingControlError(f"Could not change the {words}: {_first_line(e)}") from e

    async def diagnostics(self) -> Dict[str, Any]:
        """What Meet's call page shows, for pinning its real wording on a room Pi (GET /api/meeting/diagnostics during a call): every drawn label, what the reader
        makes of them, and what has the keyboard. Meet's labels are the controls plan's English until a real call confirms them."""
        if self._page is None:
            return {}
        labels = await self._answered(self._page.evaluate(
            "() => [...document.querySelectorAll('[aria-label]')].filter((e) => e.getClientRects().length > 0)"
            ".map((e) => ({ tag: e.tagName.toLowerCase(), role: e.getAttribute('role') || '', label: e.getAttribute('aria-label') }))"))
        held = await self._answered(self._page.evaluate("() => { const h = document.activeElement; return h ? h.tagName.toLowerCase() + (h.getAttribute('role') ? '[role=' + h.getAttribute('role') + ']' : '') : ''; }"))
        return {"labels": labels, "reading": await self._look(), "focus": held}

    # ------------------------------------------------------------------
    # The model, as Meet's page has it
    # ------------------------------------------------------------------

    async def _look(self) -> Dict[str, Any]:
        """One reading of Meet's call page (READ_CALL), which the page must answer within LOOK_TIMEOUT_S."""
        return await self._answered(self._page.evaluate(READ_CALL, MEET))

    async def _answered(self, evaluation) -> Any:
        """What the page answers to a call, or, when it has not within LOOK_TIMEOUT_S (a frozen tab), the words for the table."""
        try:
            return await asyncio.wait_for(evaluation, self.LOOK_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise MeetingControlError(f"Meet's page did not answer within {self.LOOK_TIMEOUT_S:g} seconds") from None

    async def _read_controls(self) -> Dict[str, Any]:
        """Read Meet's buttons into the model the table draws from (the meeting's controls, people, microphone and camera) and return the
        controls. A read that fails (the page gone, in the middle of a navigation) leaves the model as it was and is said once for the call;
        it never raises."""
        info = self._current_meeting
        if info is None or self._page is None:
            return {}
        try:
            self._take(await self._look())
        except Exception as e:  # noqa: BLE001 - see above
            words = _first_line(e)
            if self._read_warned:
                logger.debug(f"Could not read Meet's buttons again ({words})")
            else:
                self._read_warned = True
                logger.warning(f"Could not read Meet's buttons ({words}); the table keeps what it last had")
        return info.controls

    def _take(self, reading: Dict[str, Any]) -> None:
        """Take a reading of the page (READ_CALL) into the meeting. A button the page does not show, or whose label says no state, leaves the
        microphone and camera as they last were (the model offers the control only when its button reads). The rest is _publish's."""
        info = self._current_meeting
        if info is None:
            return
        if reading["mute"] is not None:
            info.is_muted = not reading["mute"]["on"]
        if reading["camera"] is not None:
            info.is_camera_on = reading["camera"]["on"]
        self._reading = reading
        self._publish()

    def _publish(self) -> None:
        """The model the table draws from, from the last reading of the call page and what the panels last read: the people count is the
        number the people button shows (0 without one) and the list is the people panel's while it is open (at most PEOPLE_LISTED); the chat's
        unread count is the chat button's, 0 while the touch Pi's chat screen is open (as chat.seen does for Zoom)."""
        info, reading = self._current_meeting, self._reading
        if info is None or reading is None:
            return
        buttons = {name: {"on": reading[name]["on"]} for name in ("camera", "raise_hand", "captions") if reading.get(name) is not None}
        people = reading.get("people")
        info.controls = meet_controls(
            buttons, info.is_muted, drawn=reading.get("drawn") or {}, people=people is not None, hosting=bool(reading.get("hosting")),
            reactions=self._reaction_choices if self._reaction_choices is not None else self.reactions, layout=self._layout,
            hide_self=self._hide_self, switches=self._switches, waiting=self._waiting)
        info.people = {"count": int(people["count"] or 0) if people is not None else 0, "list": list(self._panel_people or [])[:PEOPLE_LISTED]}
        info.chat["unread"] = 0 if self._screen == "chat" else int(reading.get("chat_unread") or 0)

    async def _read_regularly(self) -> None:
        """For as long as the call lasts, read Meet's buttons again every READ_INTERVAL_S."""
        while True:
            await asyncio.sleep(self.READ_INTERVAL_S)
            if self._state != MeetingState.CONNECTED:
                return
            await self._read_controls()

    # ------------------------------------------------------------------
    # Meet's boxes (spec 2026-10-30 Meet controls, section 3): its notices, "Someone wants to join", "Leave call?"
    # ------------------------------------------------------------------

    def _reset_dialogs(self) -> None:
        """What one call leaves of Meet's boxes that the next must not find."""
        self._on_screen: Dict[Tuple[str, str], Dict[str, Any]] = {}   # every box on the screen, by its title and words, as the table is given it
        self._dialogs: List[Dict[str, Any]] = []                      # the boxes waiting for a tap on the table, oldest first
        self._last_answer = float("-inf")                             # when the provider last pressed a notice's button by itself
        self._dialogs_warned = False                                  # a reading that fails is said once for a call
        self._dialog_loop_warned = False                              # and one that fails unexpectedly, too
        if self._current_meeting is not None:
            self._current_meeting.dialog = None

    @staticmethod
    def _dialog_key(dialog: Dict[str, Any]) -> Tuple[str, str]:
        """How a box is known while it is on the screen: Meet gives its boxes no ids, so by its title and its words."""
        return (str(dialog.get("title") or ""), str(dialog.get("text") or ""))

    async def _read_dialogs_regularly(self) -> None:
        """For as long as the call lasts, read Meet's boxes: at once (a notice Meet shows as the call opens), then every DIALOG_INTERVAL_S;
        and in the same turn, the panels the touch Pi's screens have open (_panel_turn). A reading that fails in a way nobody expected is said
        once for the call (with its trace), and the readings go on."""
        while self._state == MeetingState.CONNECTED:
            for turn in (self._read_dialogs, self._panel_turn):
                try:
                    await turn()
                except Exception:  # noqa: BLE001 - one bad reading must not end the readings for the rest of the call
                    if not self._dialog_loop_warned:
                        self._dialog_loop_warned = True
                        logger.exception("Reading Meet's boxes or panels failed; the readings go on")
            await asyncio.sleep(self.DIALOG_INTERVAL_S)

    async def _read_dialogs(self) -> None:
        """One reading of Meet's boxes, taken in: a box not seen before is judged by the policy (a notice is pressed for the room, any other
        waits on the table) and one no longer on the screen is forgotten. A box stays known by its words while it is on the screen, so a
        notice is pressed once, never again and again. A reading that fails changes nothing and is said once for the call; it never raises."""
        async with self._dialog_turn:
            if self._page is None or self._current_meeting is None:
                return
            try:
                reading = await self._answered(self._page.evaluate(READ_DIALOGS))
            except Exception as e:  # noqa: BLE001 - the page gone or navigating: the next reading will tell
                if not self._dialogs_warned:
                    self._dialogs_warned = True
                    logger.warning(f"Could not read Meet's boxes ({_first_line(e)}); trying again")
                return
            seen = {}
            for dialog in reading or []:
                if isinstance(dialog, dict):
                    seen.setdefault(self._dialog_key(dialog), dialog)
            for key in [key for key in self._on_screen if key not in seen]:
                self._forget_dialog(key)
            for key, dialog in seen.items():
                if key not in self._on_screen and self._state == MeetingState.CONNECTED:
                    await self._on_dialog(key, dialog)

    async def _on_dialog(self, key: Tuple[str, str], dialog: Dict[str, Any]) -> None:
        """A box Meet has just drawn. By the policy Zoom's dialogs have, a one-button notice whose words are on the list is pressed for the
        room (at most one press in ANSWER_SPACING_S) and its sentence kept on the notice line; any other, and a notice whose press fails,
        waits on the table for a tap."""
        info = self._current_meeting
        if info is None:
            return
        self._next_dialog_id += 1
        words = {"id": self._next_dialog_id, "title": key[0], "text": key[1],
                 "buttons": [str(label) for label in dialog.get("buttons") or []]}
        self._on_screen[key] = words
        dialog_log.info(f"Meet dialog: {words['title']!r} {words['text']!r} buttons {words['buttons']}")
        if self.dialog_policy.decide(words) != "acknowledge":
            self._wait_on_the_table(words)
            dialog_log.info("The dialog waits on the table")
            return
        label = self.dialog_policy.answer(words)
        sentence = self.dialog_policy.disclosure(words)
        if sentence and sentence not in info.notice:
            info.notice = f"{info.notice} {sentence}".strip()
        wait = self._last_answer + self.ANSWER_SPACING_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_answer = time.monotonic()
        try:
            await self._press_dialog(key, label)
        except _DialogGone:
            logger.debug(f"Meet's box {words['title']!r} went before {label!r} was pressed")
            self._forget_dialog(key)
        except MeetingControlError as e:
            logger.warning(f"Could not press {label!r} on Meet's box {words['title']!r} {words['text']!r} ({e}): it waits on the table")
            self._wait_on_the_table(words)
        else:
            dialog_log.info(f"Acknowledged Meet's dialog with {label!r}")

    def _wait_on_the_table(self, words: Dict[str, Any]) -> None:
        self._dialogs.append(words)
        if self._current_meeting is not None:
            self._current_meeting.dialog = dict(self._dialogs[0])

    def _forget_dialog(self, key: Tuple[str, str]) -> None:
        """The box is off the screen: the table shows the oldest of those still waiting."""
        words = self._on_screen.pop(key, None)
        if words is not None:
            self._take_off_the_table(words["id"])

    def _take_off_the_table(self, dialog_id: int) -> None:
        self._dialogs = [dialog for dialog in self._dialogs if dialog["id"] != dialog_id]
        if self._current_meeting is not None:
            self._current_meeting.dialog = dict(self._dialogs[0]) if self._dialogs else None

    async def _press_dialog(self, key: Tuple[str, str], label: str) -> None:
        """Press the button the label names in the box the key names, one press at a time with the shortcuts, and see the box go (for
        PRESS_WAIT_S at most): a press Meet does not take is a refusal in words, never a success. _DialogGone when the box is not there."""
        async with self._pressing:
            try:
                result = await self._answered(self._page.evaluate(PRESS_DIALOG, [key[0], key[1], label]))
            except MeetingControlError:
                raise
            except Exception as e:  # noqa: BLE001 - the page gone or navigating: words for the table
                raise MeetingControlError(f"Could not press {label}: {_first_line(e)}") from e
            if result == "gone":
                raise _DialogGone(DIALOG_GONE_WORDS)
            if result != "pressed":
                raise MeetingControlError(f"Meet's box has no {label} button")
            deadline = time.monotonic() + self.PRESS_WAIT_S
            while True:
                try:
                    reading = await self._answered(self._page.evaluate(READ_DIALOGS))
                except Exception:  # noqa: BLE001 - the page moved on (Leave call navigates): the box went with it
                    return
                if key not in {self._dialog_key(dialog) for dialog in reading or [] if isinstance(dialog, dict)}:
                    return
                if time.monotonic() >= deadline:
                    raise MeetingControlError(f"Meet did not close the box after {label}; try again")
                await asyncio.sleep(self.DIALOG_POLL_S)

    async def _answer_from_table(self, value: Any) -> Dict[str, Any]:
        """A tap on one of the waiting box's buttons: the page presses it, and the box is off the table. One that has gone is refused in
        words."""
        try:
            dialog_id, label = int(value["id"]), str(value["button"])
        except (KeyError, TypeError, ValueError):
            raise MeetingControlError("Say which dialog and which button") from None
        if self._capturing:
            raise MeetingControlError(CAPTURING_WORDS)   # at once: held for the capture's minute, the tap would land after the page gave up
        async with self._dialog_turn:
            key = next((key for key, words in self._on_screen.items() if words["id"] == dialog_id), None)
            if key is None:
                raise MeetingControlError(DIALOG_GONE_WORDS)
            try:
                await self._press_dialog(key, label)
            except _DialogGone:
                self._forget_dialog(key)
                raise
            self._take_off_the_table(dialog_id)   # the box itself is forgotten by the reading that no longer sees it
        info = self._current_meeting
        return {"dialog": info.dialog if info is not None else None}

    # ------------------------------------------------------------------
    # The touch Pi's Meet screens and Meet's side panels (Meet controls plan Phase B, Task 2; Review Focus 1 and 2)
    # ------------------------------------------------------------------
    #
    # The touch Pi says which of its Meet screens is open ("screen": chat, participants, host, or ""), and renews it every 60 s. The room
    # opens the matching side panel on the TV, and closes the one it opened (_room_panel: Meet shows one at a time) when the screen changes,
    # when no renewal comes for SCREEN_TIMEOUT_S, when the call ends and before the capture. A panel the room did not open is never closed.
    #
    # Opening: the panel's button, pressed by MEET's words (PRESS_BY_LABEL), or Meet's shortcut when the button is missing (the chat and
    # people panels have one) and nothing that takes typing has the keyboard. Closing: the Close button inside that panel (found inside the
    # panel by its structure, it can open nothing), else the panel's button again; never Escape. Whether a panel is open is read both ways
    # (READ_PANELS): by its structure, and by its button's pressed or expanded state, which Meet could leave stale. Nothing is pressed to close
    # a panel that neither says is open; one only its button says is open is pressed once, and pressed again if that press drew it (the state
    # was stale). Every press and wait here is made with _dialog_turn and then _pressing held (_panel_locks), so no box reading or other press
    # comes between.
    #
    # For Task 3's controls: `async with self._panel_locks():` then `opened = await self._open_panel(kind)` (True when the room opened it now),
    # and in a `finally`: `if opened and self._screen_panel() != kind: await self._close_panel(kind)`, then `await self._follow_screen()`
    # (opens again the screen's panel if the action's panel took its place). `_room_opened(kind)`: the room opened that panel and it is open
    # as far as the room knows. `_screen_panel()`: the panel the touch Pi's open screen wants now (None without one, or once it has lapsed).

    def _reset_panels(self) -> None:
        """What one call leaves of the touch Pi's screens and Meet's panels that the next must not find."""
        self._screen = ""                                         # the touch Pi's open Meet screen, as it last said ("" for none)
        self._screen_at = float("-inf")                           # when it last said so (self._clock)
        self._room_panel: Optional[str] = None                    # the panel the room opened and has not closed ("chat", "people", "host")
        self._reading: Optional[Dict[str, Any]] = None            # the last reading of the call page (READ_CALL)
        self._panel_people: Optional[List[Dict[str, Any]]] = None  # the people panel's list, while it is open
        self._waiting: List[Dict[str, Any]] = []                  # the people panel's knocks, while it is open
        self._switches: List[Dict[str, Any]] = []                 # the host panel's switches, as last read
        self._reaction_choices: Optional[List[str]] = None        # the emoji Meet's picker held the last time it was open (Task 3)
        self._layout: Optional[str] = None                        # the layout the room last chose this call (Task 3)
        self._hide_self = False                                   # whether the room hid its own tile this call (Task 3)
        self._panels_warned = False                               # a panel reading that fails is said once for a call

    def _screen_panel(self) -> Optional[str]:
        """The panel the touch Pi's open screen wants on the TV now: None with no screen open, or once it has not been renewed for
        SCREEN_TIMEOUT_S."""
        if not self._screen or self._clock() - self._screen_at >= self.SCREEN_TIMEOUT_S:
            return None
        return SCREEN_PANELS.get(self._screen)

    def _room_opened(self, kind: str) -> bool:
        """The room opened this panel and, as far as it knows, it is open still."""
        return self._room_panel == kind

    @asynccontextmanager
    async def _panel_locks(self):
        """The provider's two locks in their order (_dialog_turn, then _pressing), for a turn that opens or closes a panel."""
        async with self._dialog_turn:
            async with self._pressing:
                yield

    async def _on_screen_said(self, value: Any) -> Dict[str, Any]:
        """The touch Pi says which Meet screen it has open (or renews it): the room's panel follows it, the panel is read, and the answer is the
        control model. While the capture runs it is kept (the panel follows it after the capture) and refused at once in words."""
        screen = "" if value is None else value
        if not isinstance(screen, str) or (screen and screen not in SCREEN_PANELS):
            raise MeetingControlError(SCREEN_WORDS)
        self._screen, self._screen_at = screen, self._clock()
        if self._capturing:
            raise MeetingControlError(CAPTURING_WORDS)
        async with self._panel_locks():
            if self._state != MeetingState.CONNECTED:
                raise MeetingControlError("Not in a meeting")
            await self._follow_screen()
            await self._read_panels()
        return await self._read_controls()

    async def _follow_screen(self) -> None:
        """Put Meet's panels as the touch Pi's screen wants them: the room's panel closed if the screen wants another or none, and the screen's
        panel opened unless it is open already. The caller holds _panel_locks."""
        wanted = self._screen_panel()
        if self._room_panel is not None and self._room_panel != wanted:
            await self._close_panel(self._room_panel)
        if wanted is not None:
            await self._open_panel(wanted)

    async def _panel_state(self) -> Dict[str, List[str]]:
        """Which panels are open, both ways (READ_PANELS): {"open": by structure, "pressed": by their buttons}."""
        state = await self._answered(self._page.evaluate(READ_PANELS, MEET))
        return {"open": list(state.get("open") or []), "pressed": list(state.get("pressed") or [])}

    async def _press_label(self, labels: List[str], container: Any = None) -> Optional[str]:
        """Press what PRESS_BY_LABEL finds for the words, where the container says: the label pressed, or None (nothing was pressed)."""
        return await self._answered(self._page.evaluate(PRESS_BY_LABEL, {"meet": MEET, "labels": list(labels), "container": container}))

    async def _wait_panel(self, kind: str, open_: bool) -> bool:
        """Wait (PRESS_WAIT_S at most) for the panel's structure to show it open or closed; whether it did."""
        deadline = time.monotonic() + self.PRESS_WAIT_S
        while True:
            if (kind in (await self._panel_state())["open"]) == open_:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self.PRESS_POLL_S)

    async def _open_panel(self, kind: str) -> bool:
        """Open Meet's side panel `kind` ("chat", "people", "host") on the TV, and wait for it to show (PRESS_WAIT_S at most). True when the
        room opened it now (it is the room's to close), False when it was open already (by its structure, or its button says so: then it is
        not the room's, and nothing is pressed). Another panel the room had open is closed first. A panel whose button is not on the screen
        and that has no shortcut that may be pressed now, or that does not show, is a refusal in words. The caller holds _panel_locks."""
        words = PANEL_WORDS[kind]
        if self._room_panel is not None and self._room_panel != kind:
            await self._close_panel(self._room_panel)     # only forgotten when it has gone already (Meet shows one side panel at a time)
        state = await self._panel_state()
        if kind in state["open"] or kind in state["pressed"]:
            return False
        pressed = await self._press_label(PANEL_OPENERS[kind])
        if pressed is None:
            shortcut = PANEL_SHORTCUTS.get(kind)
            if shortcut is None or (await self._look()).get("taken"):
                raise MeetingControlError(f"Meet's {words} button isn't on the screen")
            await self._page.keyboard.press(shortcut)
        self._room_panel = kind        # from the press on, it is the room's to close, whatever the wait below finds
        logger.info(f"Opened Meet's {words} panel for the touch Pi")
        if not await self._wait_panel(kind, True) and kind not in (await self._panel_state())["pressed"]:
            raise MeetingControlError(f"Meet did not open its {words} panel; try again")
        return True

    async def _close_panel(self, kind: str) -> bool:
        """Close Meet's side panel `kind` if the room opened it (a panel the room did not open is never closed: False), and wait for it to go
        (PRESS_WAIT_S at most). It is closed by the Close button inside it, else by its button again; one that neither its structure nor its
        button says is open is only forgotten. A panel that stays is a refusal in words, and stays the room's to close. The caller holds
        _panel_locks."""
        if self._room_panel != kind:
            return False
        words = PANEL_WORDS[kind]
        state = await self._panel_state()
        if kind not in state["open"]:
            if kind in state["pressed"]:
                # Only its button says it is open: it may be a panel whose structure is not known, or the button's state may be stale. One
                # press; if that drew the panel, the state was stale, and the panel just drawn is closed as any other.
                await self._press_label(PANEL_OPENERS[kind])
                if await self._wait_panel(kind, True):
                    await self._close_drawn_panel(kind)
            self._room_panel = None
            logger.info(f"Closed Meet's {words} panel")
            return True
        await self._close_drawn_panel(kind)
        self._room_panel = None
        logger.info(f"Closed Meet's {words} panel")
        return True

    async def _close_drawn_panel(self, kind: str) -> None:
        """Close the panel `kind` that is drawn: its Close button, else its button; then wait for it to go."""
        words = PANEL_WORDS[kind]
        if await self._press_label(CLOSE_WORDS, {"panel": kind}) is None and await self._press_label(PANEL_OPENERS[kind]) is None:
            raise MeetingControlError(f"Meet's {words} panel has no Close button and its button isn't on the screen")
        if not await self._wait_panel(kind, False):
            raise MeetingControlError(f"Meet did not close its {words} panel; try again")

    async def _put_room_panel_away(self, why: str) -> None:
        """Close the panel the room opened, if any (before the capture; the caller holds _dialog_turn). A failure is logged, not raised."""
        if self._room_panel is None or self._page is None:
            return
        try:
            async with self._pressing:
                await self._close_panel(self._room_panel)
        except Exception as e:  # noqa: BLE001 - the page gone or a panel that stays: logged; the call goes on
            logger.warning(f"Could not close Meet's {PANEL_WORDS.get(self._room_panel, 'side')} panel {why} ({_first_line(e)})")

    async def _close_room_panel_at_the_end(self) -> None:
        """The call ends: the panel the room opened is closed before Leave is pressed. While the capture runs the room has none open (it was
        closed before the capture, and is not opened again once the call is no longer connected). The box loop has been stopped by now:
        when _dialog_turn is still held, the holder is an action that ends the call (host.end, which then leaves) and the panel is left to
        go with the page (the TV goes back to the screensaver), rather than wait on a lock its own task holds."""
        if self._room_panel is None or self._page is None or self._capturing or self._dialog_turn.locked():
            return
        async with self._dialog_turn:
            await self._put_room_panel_away("as the call ends")

    async def _panel_turn(self) -> None:
        """The panels' turn in the 2 s box loop, under _dialog_turn: a screen not renewed for SCREEN_TIMEOUT_S is taken as closed, the room's
        panel that no screen wants is closed, and the panels the screens have open are read."""
        async with self._dialog_turn:
            if self._page is None or self._current_meeting is None or self._state != MeetingState.CONNECTED:
                return
            if self._screen and self._screen_panel() is None:
                logger.info(f"The touch Pi's Meet {self._screen} screen was not renewed for {self.SCREEN_TIMEOUT_S} s: taken as closed")
                self._screen = ""
            if self._room_panel is not None and self._room_panel != self._screen_panel():
                await self._put_room_panel_away("that no screen wants")
            await self._read_panels()

    async def _read_panels(self) -> None:
        """Read the side panels that are open while a screen is open or the room has a panel open (the caller holds _dialog_turn): the people
        panel's list and knocks, the chat's last CHAT_KEPT messages, the host panel's switches; then the model. A panel the room opened that
        neither way says is open any more (closed at the TV) is forgotten. A reading that fails is said once for the call; it never raises."""
        info = self._current_meeting
        if info is None or self._page is None:
            return
        if not self._screen and self._room_panel is None:
            self._panel_people, self._waiting = None, []
            self._publish()
            return
        try:
            state = await self._panel_state()
            if "people" in state["open"]:
                people = await self._answered(self._page.evaluate(READ_PEOPLE, MEET))
                self._panel_people = [p for p in people.get("people") or [] if isinstance(p, dict)][:PEOPLE_LISTED]
                self._waiting = [p for p in people.get("waiting") or [] if isinstance(p, dict)]
            else:
                self._panel_people, self._waiting = None, []
            if "chat" in state["open"]:
                messages = await self._answered(self._page.evaluate(READ_CHAT, MEET))
                info.chat["messages"] = [dict(m) for m in messages or [] if isinstance(m, dict)][-CHAT_KEPT:]
            if "host" in state["open"]:
                self._switches = [dict(s) for s in await self._answered(self._page.evaluate(READ_HOST_SWITCHES, MEET)) or [] if isinstance(s, dict)]
            if self._room_panel is not None and self._room_panel not in state["open"] and self._room_panel not in state["pressed"]:
                logger.info(f"Meet's {PANEL_WORDS[self._room_panel]} panel was closed on the TV")
                self._room_panel = None
        except Exception as e:  # noqa: BLE001 - the page gone or navigating: the next reading will tell
            if not self._panels_warned:
                self._panels_warned = True
                logger.warning(f"Could not read Meet's panels ({_first_line(e)}); trying again")
            return
        self._publish()

    # ------------------------------------------------------------------
    # The capture (spec 2026-10-30 Meet controls, section 3)
    # ------------------------------------------------------------------

    async def capture(self, directory: Path, screenshots: bool = True) -> Dict[str, Any]:
        """Record Meet's panels on the TV (croom.meeting.meet_capture) into a zip in `directory`: {"file", "steps": [{"name", "opened",
        "why"?, "closed"?: true, false or "unknown"}]}. The box reader is held for the whole run, so a panel the capture opens that Meet draws as a dialog (Change
        layout) is neither judged nor put on the table; it is closed before the reader reads again."""
        if self._page is None or self._state != MeetingState.CONNECTED:
            raise MeetingControlError(JOIN_MEET_FIRST)
        # Its presses take the press lock one at a time (the order stays the box reader's, then the press lock's), so a touch Pi's mute is
        # never sent in the middle of one, and waits for one key, not the whole minute.
        # It opens nothing new once the call is no longer connected (a Leave, the agent's shutdown), and presses nothing once the TV has left
        # Meet's page.
        capture = MeetCapture(self._page, screenshots=screenshots, room_name=self._room_name, lock=self._pressing,
                              in_call=lambda: self._state == MeetingState.CONNECTED,
                              on_meet=lambda: self._state in (MeetingState.CONNECTED, MeetingState.LEAVING))
        self._capturing = True   # a tap on the table's box is refused in words meanwhile, not held for the minute
        try:
            async with self._dialog_turn:
                # The panel the room opened for a touch Pi screen is put away first (the capture opens Meet's panels itself and must find
                # none of the room's), and opened again after, if the screen is still open.
                await self._put_room_panel_away("before the capture")
                try:
                    result = await capture.run()
                finally:
                    if self._state == MeetingState.CONNECTED:
                        try:
                            async with self._pressing:
                                await self._follow_screen()
                        except Exception as e:  # noqa: BLE001 - the capture's own outcome is what the caller is told
                            logger.warning(f"Could not open Meet's panel again after the capture ({_first_line(e)})")
        finally:
            self._capturing = False
        path = await asyncio.to_thread(save_capture, result, capture.images, Path(directory))
        logger.info(f"Captured Meet's page into {path}")
        steps = [{key: step[key] for key in ("name", "opened", "why", "closed") if key in step} for step in result["steps"]]
        return {"file": path.name, "steps": steps}

    async def _stop_reading(self) -> None:
        """Stop the regular readings (the buttons and the boxes) and wait for them to be over: what the provider starts on its own never
        outlives the call. What it kept of the call's boxes goes with them."""
        readers = [self._reader, self._dialog_reader]
        self._reader = self._dialog_reader = None
        readers = [reader for reader in readers if reader is not None and reader is not asyncio.current_task()]
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        self._reset_dialogs()
