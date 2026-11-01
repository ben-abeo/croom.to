"""
Google Meet provider.

Handles joining and controlling Google Meet meetings using browser automation.
"""

import asyncio
import logging
import re
import tempfile
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Dict, Any, Callable, List, Tuple

from croom.core.config import DEFAULT_ACKNOWLEDGE, DEFAULT_REACTIONS
from croom.core.logs import DIALOGS_LOGGER
from croom.meeting.meet_api import MeetParticipants
from croom.meeting.meet_capture import JOIN_MEET_FIRST, MeetCapture, save_capture
from croom.meeting.meet_page import (
    BLUR_CHAT_BOX,
    CHAT_BOX_STATE,
    FOCUS_CHAT_BOX,
    MEET,
    OPEN_POPUPS,
    PERSON_MENU,
    PRESS_BY_LABEL,
    PRESS_DIALOG,
    PRESS_REACTION,
    READ_CALL,
    READ_CHAT,
    READ_DIALOGS,
    READ_HOST_SWITCHES,
    READ_PANELS,
    READ_PEOPLE,
    READ_REACTIONS,
    STATE_BY_LABEL,
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

# The touch Pi's Meet controls (Meet controls plan Phase B, Task 3): what Chat Send is told when Meet's message box cannot be given the keyboard
# (nothing is typed then: keys typed elsewhere in Meet are its shortcuts), and the longest message it sends.
CHAT_BOX_WORDS = "Meet's message box isn't ready; nothing was sent"
CHAT_LONGEST = 500
# Each person's action: MEET's words for its button, its name in words, and whether it is for someone asking to join (the people panel's knocks).
PERSON_ACTIONS = {"pin": ("pin", "Pin", False), "unpin": ("unpin", "Unpin", False), "host.admit": ("admit", "Admit", True),
                  "host.deny": ("deny", "Deny", True), "host.remove": ("remove", "Remove", False)}
# The table's configured reactions (Zoom's names) and the emoji Meet's picker holds for each, for a tap made before the picker was first read.
REACTION_EMOJI = {"Thumbs Up": ["👍"], "Clap": ["👏"], "Heart": ["💖", "❤️"], "Joy": ["😂"], "Open Mouth": ["😮"], "Tada": ["🎉"]}


def _starts(text: Any, words: List[str]) -> bool:
    """Whether a label starts with one of the words, in any case and at a word's end (the page scripts' `starts`)."""
    said = " ".join(str(text or "").split()).lower()
    return any(said.startswith(word.lower()) and not re.match(r"[a-z0-9]", said[len(word):len(word) + 1]) for word in words)


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
    # The controls that press Meet's own buttons (Meet controls plan Phase B, Task 3), each with the method that carries it out: a press waits
    # this long at most for its change to show (a menu, a box, a pin, a sent message), and a menu the press should have closed this long
    # before it is closed with Escape.
    MEET_ACTIONS = {"chat.send": "_send_chat", "reactions": "_react", "layout": "_set_layout", "hide_self": "_set_hide_self",
                    "pin": "_on_person", "unpin": "_on_person", "host.admit": "_on_person", "host.deny": "_on_person",
                    "host.remove": "_on_person", "host.mute_all": "_mute_all", "host.end": "_end_for_everyone", "host.switch": "_host_switch"}
    # Who is in the call, from Google's Meet API when the organization has granted it (Phase B, Task 6): asked this often while connected.
    API_PEOPLE_INTERVAL_S = 15
    ACTION_WAIT_S = 3
    MENU_SETTLE_S = 0.5

    def __init__(self, display=None, room_name: str = "Conference Room", clock: Callable[[], float] = time.monotonic):
        super().__init__()
        self._clock = clock               # when the touch Pi last said which screen it has open (a test gives its own)
        self.reactions: List[str] = list(DEFAULT_REACTIONS)   # offered until Meet's picker has been seen
        # The TV display (spec 2026-10-07 TV, section 4.3) owns the browser and, when configured, the
        # signed-in Google profile (spec 2026-10-07 Google Meet, section 4.2); this provider borrows its page.
        self._display = display
        self._room_name = room_name or "Conference Room"
        self._name_in_call = ""   # the name the room joined the call under (join_meeting's display_name)
        # Where a screenshot goes when a join fails, so the TV need not be watched.
        self.failure_screenshot: Path = Path(tempfile.gettempdir()) / "croom-meet-failure.png"
        self._page: Optional["Page"] = None
        # What the provider starts on its own, kept so that the end of the call stops it: the regular reading of Meet's buttons.
        self._reader: Optional[asyncio.Task] = None
        self._people_reader: Optional[asyncio.Task] = None   # the Meet API's participants, read every API_PEOPLE_INTERVAL_S
        self._meet_api_domain = ""        # meeting.meet_api_domain; blank is off
        self._meet_api_key = ""           # the calendar's service-account key file
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
        provider._meet_api_domain = config.meeting.meet_api_domain
        provider._meet_api_key = config.calendar.google_credentials_path
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
        self._name_in_call = display_name   # the name the room joins under (a signed-in profile's own name may differ: the room's name is kept too)
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
            if self._meet_api_domain and self._meet_api_key:
                self._people_reader = asyncio.create_task(self._read_people_regularly(MeetParticipants(self._meet_api_domain, self._meet_api_key)))
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
        "screen", the touch Pi's Meet screen that is open ("chat", "participants", "host", or "" for none; _on_screen_said); and the controls
        that press Meet's own buttons (MEET_ACTIONS: chat.send, reactions, layout, hide_self, pin, unpin, host.*). Everything else Meet's page
        does not offer here is refused in words. The answer is the control model as it is afterwards ({} once host.end has left the call)."""
        if self._page is None or self._state != MeetingState.CONNECTED:
            raise MeetingControlError("Not in a meeting")
        if name == "screen":
            return await self._on_screen_said(value)
        if name in self.MEET_ACTIONS:
            return await getattr(self, self.MEET_ACTIONS[name])(name, value)
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
        if self._panel_people is None and self._api_people is not None:
            # The people panel is closed and the Meet API answered: its list is the touch Pi's Participants before the panel opens. The panel's
            # list, with Meet's own ids, wins while it is open (Pin and Remove find the person by it).
            info.people = {"count": len(self._api_people), "list": list(self._api_people)[:PEOPLE_LISTED]}
        else:
            info.people = {"count": int(people["count"] or 0) if people is not None else 0, "list": list(self._panel_people or [])[:PEOPLE_LISTED]}
        info.chat["unread"] = 0 if self._screen == "chat" else int(reading.get("chat_unread") or 0)

    async def _read_regularly(self) -> None:
        """For as long as the call lasts, read Meet's buttons again every READ_INTERVAL_S."""
        while True:
            await asyncio.sleep(self.READ_INTERVAL_S)
            if self._state != MeetingState.CONNECTED:
                return
            await self._read_controls()

    def _is_room(self, name: str) -> bool:
        """Whether a name the Meet API gives is the room's own in the call: the name it joined under, or the room's name (any case)."""
        said = " ".join(str(name).split()).casefold()
        return bool(said) and said in {" ".join(str(n).split()).casefold() for n in (self._name_in_call, self._room_name) if n}

    async def _read_people_regularly(self, api: MeetParticipants) -> None:
        """For as long as the call lasts, ask Google's Meet API who is in it every API_PEOPLE_INTERVAL_S (croom.meeting.meet_api: off unless the
        organizer is in the configured domain; a refusal is logged once and not asked again for 5 minutes). What it says is kept as the people
        list, with ids "name:<name>" (a person is found again by name at the moment of a press); when it says nothing the panel's list serves."""
        try:
            while self._state == MeetingState.CONNECTED:
                info = self._current_meeting
                names = await api.names(info.organizer, info.meeting_id) if info is not None else None
                if self._state != MeetingState.CONNECTED:
                    return
                self._api_people = None if names is None else [
                    {"id": f"name:{name}", "name": name, "me": self._is_room(name), "pinned": False, "muted": False} for name in names]
                self._publish()
                await asyncio.sleep(self.API_PEOPLE_INTERVAL_S)
        finally:
            await api.close()

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
            await self._press_dialog_held(key, label)

    async def _press_dialog_held(self, key: Tuple[str, str], label: str) -> None:
        """_press_dialog, for a caller that holds _pressing already (a control answering Meet's confirmation box of its own press)."""
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
        self._panel_close_warned = False                          # and a panel of the room's that will not close, too
        self._api_people: Optional[List[Dict[str, Any]]] = None   # who the Meet API says is in the call (ids "name:<name>"), None while it does not say

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

    async def _press_label(self, labels: List[str], container: Any = None, *, exact: bool = False, own: Optional[str] = None,
                           buttons: bool = False) -> Optional[str]:
        """Press what PRESS_BY_LABEL finds for the words, where the container says (`exact`: the whole label; `own`: only one naming the room
        or "you"; `buttons`: only a button, never a switch, check box, radio, option or menu item): the label pressed, or None (nothing was
        pressed)."""
        arg = self._by_label(labels, container, exact, own)
        if buttons:
            arg["buttons"] = True
        return await self._answered(self._page.evaluate(PRESS_BY_LABEL, arg))

    async def _label_state(self, labels: List[str], container: Any = None) -> Optional[Dict[str, Any]]:
        """What _press_label would press, not pressed: {"label", "on"}, or None."""
        return await self._answered(self._page.evaluate(STATE_BY_LABEL, self._by_label(labels, container, False, None)))

    @staticmethod
    def _by_label(labels: List[str], container: Any, exact: bool, own: Optional[str]) -> Dict[str, Any]:
        arg = {"meet": MEET, "labels": list(labels), "container": container}
        if exact:
            arg["exact"] = True
        if own is not None:
            arg["own"] = own
        return arg

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
        """Close the panel the room opened, if any (before the capture; the caller holds _dialog_turn). A failure is logged, not raised: a
        warning once for the call (the box loop tries again every 2 s), then at debug level."""
        if self._room_panel is None or self._page is None:
            return
        try:
            async with self._pressing:
                await self._close_panel(self._room_panel)
        except Exception as e:  # noqa: BLE001 - the page gone or a panel that stays: logged; the call goes on
            words = f"Could not close Meet's {PANEL_WORDS.get(self._room_panel, 'side')} panel {why} ({_first_line(e)})"
            if self._panel_close_warned:
                logger.debug(words)
            else:
                self._panel_close_warned = True
                logger.warning(words)

    async def _close_room_panel_at_the_end(self) -> None:
        """The call ends: the panel the room opened is closed before Leave is pressed. While the capture runs the room has none open (it was
        closed before the capture, and is not opened again once the call is no longer connected). The box loop has been stopped by now.
        Whenever _dialog_turn is still held, whoever holds it (an action such as host.end, which then leaves, or anything else that has it), it
        is skipped: the panel is left to go with the page (the TV goes back to the screensaver), rather than wait on a lock that may be held by
        the task that is leaving."""
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
    # The controls that press Meet's own buttons (Meet controls plan Phase B, Task 3; Review Focus 3, 4 and 5)
    # ------------------------------------------------------------------
    #
    # Each is one turn with both locks held (_action_turn: _dialog_turn, then _pressing), so that no box reading comes between its presses: a
    # box Meet draws for it (the Change layout box, a confirmation) is never queued on the table or answered by the policy, and is answered by
    # the action itself. It opens what it needs (a panel, a menu, the layout box), presses by MEET's words (PRESS_BY_LABEL), waits for the change
    # to show (ACTION_WAIT_S at most), and closes what it opened that was not open already, whether it succeeded or not (_then_tidy): a panel
    # the touch Pi's open screen wants stays open (_in_panel). A button Meet does not show is a refusal naming it. While the capture runs, each
    # is refused at once in words.

    @asynccontextmanager
    async def _action_turn(self):
        """A control's turn: refused at once while the capture runs, then both locks in their order. What the page throws (gone, navigating)
        becomes words for the table."""
        if self._capturing:
            raise MeetingControlError(CAPTURING_WORDS)
        async with self._panel_locks():
            if self._page is None or self._state != MeetingState.CONNECTED:
                raise MeetingControlError("Not in a meeting")
            try:
                yield
            except MeetingControlError:
                raise
            except Exception as e:  # noqa: BLE001 - the page gone or navigating: words for the table
                raise MeetingControlError(f"Meet's page did not answer ({_first_line(e)})") from e

    async def _then_tidy(self, body, tidy, after_success: bool = True, quiet: bool = False) -> Any:
        """Run the action's body, then tidy away what it opened, also when it failed (then a tidy that fails is logged, and the body's
        refusal is what the table is told). With `after_success` False, only a body that failed is tidied after (the call it ended is gone).
        With `quiet`, a tidy that fails after a body that succeeded is logged too, not raised (a refusal then would invite the touch Pi to
        do again what was done: a second chat message)."""
        try:
            result = await body()
        except BaseException:
            try:
                await tidy()
            except Exception as e:  # noqa: BLE001 - the body's own refusal is what the table is told
                logger.warning(f"Could not put Meet's page back after a control that failed ({_first_line(e)})")
            raise
        if after_success:
            try:
                await tidy()
            except Exception as e:  # noqa: BLE001 - quiet: what was done is done; the page is put back by the next screen or reading
                if not quiet:
                    raise
                logger.warning(f"Could not put Meet's page back after a control that was carried out ({_first_line(e)})")
        return result

    async def _in_panel(self, kind: str, body, quiet: bool = False) -> Any:
        """Run the body with Meet's side panel `kind` open: opened if it was not, and then closed again unless the touch Pi's open screen
        wants it; the screen's own panel is put back if this one took its place (Meet shows one at a time), also when this one did not open.
        `quiet`: as _then_tidy's."""
        try:
            opened = await self._open_panel(kind)
        except BaseException:
            try:
                await self._follow_screen()
            except Exception as e:  # noqa: BLE001 - the panel's own refusal is what the table is told
                logger.warning(f"Could not put the touch Pi's Meet panel back after {PANEL_WORDS[kind]} did not open ({_first_line(e)})")
            raise

        async def put_back():
            if opened and self._screen_panel() != kind:
                await self._close_panel(kind)
            await self._follow_screen()

        return await self._then_tidy(body, put_back, quiet=quiet)

    async def _until(self, look, wait: Optional[float] = None) -> Any:
        """Ask `look` (a coroutine function) every PRESS_POLL_S until it answers something true, for ACTION_WAIT_S at most: its last answer."""
        deadline = time.monotonic() + (self.ACTION_WAIT_S if wait is None else wait)
        while True:
            answer = await look()
            if answer or time.monotonic() >= deadline:
                return answer
            await asyncio.sleep(self.PRESS_POLL_S)

    async def _popups(self) -> Dict[str, List[str]]:
        """The menus and the dialogs on the screen that are not side panels (OPEN_POPUPS): {"menus": [...], "dialogs": [their titles]}."""
        found = await self._answered(self._page.evaluate(OPEN_POPUPS, MEET))
        return {"menus": [str(m) for m in found.get("menus") or []], "dialogs": [str(d) for d in found.get("dialogs") or []]}

    async def _close_new_menus(self, before: Dict[str, List[str]]) -> None:
        """Close the menus a control opened (those on the screen now that were not in `before`): given MENU_SETTLE_S to go by themselves (a
        menu goes once its item is pressed), then Escape, the way a menu is closed; one that stays is a refusal in words."""
        async def none_new() -> bool:
            return not list((Counter((await self._popups())["menus"]) - Counter(before["menus"])).elements())

        if await self._until(none_new, self.MENU_SETTLE_S):
            return
        await self._page.keyboard.press("Escape")
        if not await self._until(none_new, self.PRESS_WAIT_S):
            raise MeetingControlError("A menu of Meet's stayed open on the TV")

    async def _box_keys(self) -> set:
        """The boxes on the screen now, as the box reader knows them (by title and words)."""
        return {self._dialog_key(d) for d in await self._answered(self._page.evaluate(READ_DIALOGS)) or [] if isinstance(d, dict)}

    async def _confirm(self, before: set, words: List[str], what: str) -> None:
        """Answer Meet's confirmation box for the press just made: a box that was not on the screen before it (`before`) and has a button of
        `words` comes within ACTION_WAIT_S, and that button is pressed and the box goes. The caller has held _dialog_turn since before its first
        press, so the box reader never sees this box. A box that does not come is a refusal in words."""
        async def asked():
            for box in await self._answered(self._page.evaluate(READ_DIALOGS)) or []:
                if isinstance(box, dict) and self._dialog_key(box) not in before:
                    label = next((str(b) for b in box.get("buttons") or [] if _starts(b, words)), None)
                    if label is not None:
                        return self._dialog_key(box), label
            return None

        found = await self._until(asked)
        if not found:
            raise MeetingControlError(f"Meet did not ask to confirm {what}; nothing more was pressed")
        try:
            await self._press_dialog_held(*found)
        except _DialogGone:
            raise MeetingControlError(f"Meet's box asking to confirm {what} went before it was answered") from None

    # --- Chat Send (Review Focus 3)

    async def _chat_box(self) -> Optional[Dict[str, Any]]:
        return await self._answered(self._page.evaluate(CHAT_BOX_STATE, MEET))

    async def _send_chat(self, name: str, value: Any) -> Dict[str, Any]:
        """Send a message to the call's chat: Meet's chat panel opened if it is not, its message box given the keyboard (FOCUS_CHAT_BOX), and
        only then the text put in it without a key (keyboard.insert_text: no keydown, so no shortcut of Meet's can fire), and the panel's own
        send button pressed (Enter only when Meet shows none, and only while the box still has the keyboard). A box that cannot be given the
        keyboard is a refusal, and nothing is typed. Sent: the box is empty again."""
        text = value.strip() if isinstance(value, str) else ""
        if not 1 <= len(text) <= CHAT_LONGEST:
            raise MeetingControlError(f"Say a message of 1 to {CHAT_LONGEST} characters")

        async def send():
            if await self._answered(self._page.evaluate(FOCUS_CHAT_BOX, MEET)) is not True:
                raise MeetingControlError(CHAT_BOX_WORDS)
            await self._page.keyboard.insert_text(text)
            box = await self._chat_box()
            if box is None or " ".join(str(box.get("text") or "").split()) != " ".join(text.split()):
                raise MeetingControlError("Meet's message box did not take the message; nothing was sent")
            if await self._press_label(MEET["chat_send"], {"panel": "chat"}, buttons=True) is None:   # a button only (R1), never a switch
                box = await self._chat_box()
                if not (box and box.get("focused")):
                    raise MeetingControlError("Meet's message box lost the keyboard; nothing was sent")
                await self._page.keyboard.press("Enter")

            async def emptied():
                box = await self._chat_box()
                return box is not None and not str(box.get("text") or "").strip()

            if not await self._until(emptied):
                raise MeetingControlError("Meet did not send the message; try again")
            try:   # the box gives the keyboard back: while it holds it, Captions' bare C is refused (it would be typed into it)
                await self._answered(self._page.evaluate(BLUR_CHAT_BOX, MEET))
            except Exception as e:  # noqa: BLE001 - the message is sent: that is what the table is told
                logger.warning(f"Could not take the keyboard from Meet's message box after a send ({_first_line(e)})")

        async with self._action_turn():
            await self._in_panel("chat", send, quiet=True)   # sent: a panel that will not close is logged, not a refusal (no second send)
            await self._read_panels()
        return await self._read_controls()

    # --- Reactions, the layout, the room's own view

    async def _reactions_shown(self) -> List[str]:
        return [str(e) for e in await self._answered(self._page.evaluate(READ_REACTIONS, MEET)) or []]

    async def _react(self, name: str, value: Any) -> Dict[str, Any]:
        """Send a reaction: Meet's picker opened (unless it is open), its emoji taken into the model's choices, and the one asked for pressed.
        The value is an emoji, or one of the table's configured names (REACTION_EMOJI) until the picker's own have been read; one the picker
        does not hold is refused. The picker is closed again (its button) if this opened it."""
        wanted = value.strip() if isinstance(value, str) else ""
        if not wanted:
            raise MeetingControlError("Say which reaction")
        async with self._action_turn():
            open_before = bool(await self._reactions_shown())

            async def send():
                choices = await self._reactions_shown()
                if not choices:
                    if await self._press_label(MEET["reactions"]) is None:
                        raise MeetingControlError("Meet's reactions button isn't on the screen")
                    choices = await self._until(self._reactions_shown)
                    if not choices:
                        raise MeetingControlError("Meet did not open its reactions; try again")
                self._reaction_choices = list(choices)
                emoji = next((e for e in [wanted, *REACTION_EMOJI.get(wanted, [])] if e in choices), None)
                if emoji is None:
                    raise MeetingControlError(f"Meet's reactions have no {wanted}")
                if not await self._answered(self._page.evaluate(PRESS_REACTION, {"meet": MEET, "emoji": emoji})):
                    raise MeetingControlError(f"Meet's {emoji} reaction isn't on the screen")

            async def close_picker():
                if open_before or not await self._reactions_shown():
                    return
                await self._press_label(MEET["reactions"])

                async def closed():
                    return not await self._reactions_shown()

                if not await self._until(closed):
                    raise MeetingControlError("Meet's reactions stayed open on the TV")

            await self._then_tidy(send, close_picker)
        return await self._read_controls()

    async def _set_layout(self, name: str, value: Any) -> Dict[str, Any]:
        """Choose Meet's layout ("auto", "tiled", "spotlight", "sidebar"): More options (the bar's, by its whole label), its Change layout,
        the choice in that box, which must then say it is chosen; the box is closed (its Close, else Escape) and the menu too. The room
        remembers the choice as layout.value (Meet shows the current one only inside its box)."""
        if value not in LAYOUTS:
            raise MeetingControlError("Say which layout: " + ", ".join(LAYOUTS))
        box = {"dialog": MEET["change_layout"]}
        choice = MEET["layouts"][value]

        async def choose():
            if await self._press_label(MEET["more_options"], exact=True) is None:
                raise MeetingControlError("Meet's More options button isn't on the screen")
            if not await self._until(lambda: self._press_label(MEET["change_layout"], "menu")):
                raise MeetingControlError("Meet's More options menu has no Change layout")
            if not await self._until(lambda: self._press_label(choice, box)):
                raise MeetingControlError(f"Meet's Change layout box has no {choice[0]}")

            async def chosen():
                state = await self._label_state(choice, box)
                return bool(state and state.get("on"))

            if not await self._until(chosen):
                raise MeetingControlError("Meet did not change the layout; try again")
            self._layout = value

        async def layout_box_open():
            return any(_starts(title, MEET["change_layout"]) for title in (await self._popups())["dialogs"])

        async def close_box():
            async def gone():
                return not await layout_box_open()

            if await layout_box_open():
                if await self._press_label(CLOSE_WORDS, box) is None or not await self._until(gone, self.PRESS_WAIT_S):
                    await self._page.keyboard.press("Escape")
                    if not await self._until(gone, self.PRESS_WAIT_S):
                        raise MeetingControlError("Meet's Change layout box stayed open on the TV")
            await self._close_new_menus(before)

        async with self._action_turn():
            before = await self._popups()
            await self._then_tidy(choose, close_box)
        return await self._read_controls()

    async def _set_hide_self(self, name: str, value: Any) -> Dict[str, Any]:
        """Hide or show the room's own tile on the TV (True hides; no value turns it the other way from the room's last choice): the room's
        own tile menu (the one whose label names the room or "you"), and its Hide self view or Show self view. When the menu offers only the
        other, the tile is that way already and nothing is pressed. Remembered as hide_self.on."""
        if value is not None and not isinstance(value, bool):
            raise MeetingControlError("Say whether to hide the room's own view: true or false")
        hide = (not self._hide_self) if value is None else value

        async def which():
            for words, hidden in ((MEET["hide_self"], False), (MEET["show_self"], True)):
                if await self._label_state(words, "menu") is not None:
                    return words, hidden
            return None

        async def choose():
            if await self._press_label(MEET["tile_menu"], own=self._room_name) is None:
                raise MeetingControlError("Meet's menu for the room's own tile isn't on the screen")
            offered = await self._until(which)
            if not offered:
                raise MeetingControlError("Meet's menu for the room's own tile has no Hide self view")
            words, hidden = offered
            if hidden != hide and await self._press_label(words, "menu") is None:
                raise MeetingControlError(f"Meet's menu for the room's own tile has no {words[0]}")
            self._hide_self = hide

        async with self._action_turn():
            before = await self._popups()
            await self._then_tidy(choose, lambda: self._close_new_menus(before))
        return await self._read_controls()

    # --- A person: Pin, Unpin, Admit, Deny, Remove (Review Focus 4)

    async def _people_now(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """The people panel as it is now (READ_PEOPLE): the people in the call, and those asking to join."""
        reading = await self._answered(self._page.evaluate(READ_PEOPLE, MEET)) or {}
        return ([p for p in reading.get("people") or [] if isinstance(p, dict)],
                [p for p in reading.get("waiting") or [] if isinstance(p, dict)])

    async def _find_person(self, person: str, waiting: bool) -> Dict[str, str]:
        """The people panel's row for the id the touch Pi sent, read now: Meet's own id, or "name:" + a name, which matches only when exactly
        one row (in the call or asking to join) has that name. Two rows, or none (or not among those asking to join, for Admit and Deny), is a
        refusal in words. {"id": the row's id, as PRESS_BY_LABEL finds it, "name"}."""
        people, knocks = await self._people_now()
        if person.startswith("name:"):
            called = person[len("name:"):]
            matches = [p for p in people + knocks if p.get("name") == called]
        else:
            matches = [p for p in people + knocks if p.get("id") == person]
            called = str(matches[0].get("name") or "") if matches else ""
        if len(matches) > 1:
            raise MeetingControlError(f"Two people in the call are called {called}")
        row = matches[0] if matches else None
        if row is None or any(row is k for k in knocks) != waiting:
            who = called or "That person"
            raise MeetingControlError(f"{who} isn't waiting to join any more" if waiting else f"{who} isn't in the call any more")
        return {"id": str(row.get("id")), "name": str(row.get("name") or "")}

    async def _press_for(self, row: Dict[str, str], labels: List[str], words: str) -> None:
        """Press the button of `labels` for that person: inside their row (PRESS_BY_LABEL with the person's id), else in the menu their own row's
        menu button opens (PERSON_MENU), pressed in only when that menu can be tied to the row; the menu is closed again (Escape) if it stays."""
        if await self._press_label(labels, {"person": row["id"]}) is not None:
            return
        arg = {"meet": MEET, "id": row["id"], "labels": list(labels)}
        before = await self._popups()
        opened = await self._answered(self._page.evaluate(PERSON_MENU, dict(arg, step="open")))
        if opened == "gone":
            raise MeetingControlError(f"{row['name']} isn't in the call any more")
        if opened != "opened":
            raise MeetingControlError(f"Meet has no {words} for that person")

        async def in_the_menu():
            async def answered():
                result = await self._answered(self._page.evaluate(PERSON_MENU, dict(arg, step="press")))
                return None if result == "waiting" else result

            result = await self._until(answered)
            if isinstance(result, dict) and result.get("pressed"):
                return
            if result is None:
                raise MeetingControlError(f"Meet did not open its menu for {row['name']}; try again")
            if result == "untied":
                raise MeetingControlError(f"Meet's menu for {row['name']} could not be told apart from another person's; nothing was pressed")
            if result == "gone":
                raise MeetingControlError(f"{row['name']} isn't in the call any more")
            raise MeetingControlError(f"Meet has no {words} for that person")

        async def close_menu():
            await self._answered(self._page.evaluate(PERSON_MENU, dict(arg, step="done")))
            await self._close_new_menus(before)

        await self._then_tidy(in_the_menu, close_menu)

    async def _on_person(self, name: str, value: Any) -> Dict[str, Any]:
        """Pin, Unpin, Admit, Deny or Remove a person by the id the touch Pi sent (PERSON_ACTIONS): the people panel opened if it is not, the
        person found again in it at the moment of the press (_find_person), their button pressed in their row or their row's own menu only
        (_press_for), Remove's confirmation answered, and the change seen in the panel (pinned, unpinned, no longer asking to join, gone)."""
        key, words, waiting = PERSON_ACTIONS[name]
        person = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
        if not person:
            raise MeetingControlError("Say which person")

        async def done(row):
            people, knocks = await self._people_now()
            same = [p for p in (knocks if waiting else people) if p.get("id") == row["id"]]
            if name == "pin":
                return any(p.get("pinned") for p in same)
            if name == "unpin":
                return bool(same) and not any(p.get("pinned") for p in same)
            return not same

        async def act():
            row = await self._find_person(person, waiting)
            boxes = await self._box_keys() if name == "host.remove" else set()
            await self._press_for(row, MEET[key], words)
            if name == "host.remove":
                await self._confirm(boxes, MEET["confirm"]["remove"], f"removing {row['name']}")
            if not await self._until(lambda: done(row)):
                raise MeetingControlError(f"Meet did not {words.lower()} {row['name']}; try again")

        async with self._action_turn():
            await self._in_panel("people", act)
            await self._read_panels()
        return await self._read_controls()

    # --- The host's: Mute everyone, End the call for everyone, the host panel's switches (Review Focus 5)

    async def _mute_all(self, name: str, value: Any) -> Dict[str, Any]:
        """Mute everyone (the touch Pi has asked twice): the people panel's Mute all, then Meet's confirmation box's Mute all, with both locks
        held from before the first press to the end (the box reader never takes that box)."""
        async def act():
            before = await self._box_keys()
            if await self._press_label(MEET["mute_all"], {"panel": "people"}) is None:
                raise MeetingControlError("Meet's Mute all button isn't on the screen")
            await self._confirm(before, MEET["confirm"]["mute_all"], "Mute all")

        async with self._action_turn():
            await self._in_panel("people", act)
            await self._read_panels()
        return await self._read_controls()

    async def _end_for_everyone(self, name: str, value: Any) -> Dict[str, Any]:
        """End the call for everyone (the touch Pi has asked twice), the room being a host: Meet's End the call for everyone (in Meet's leave
        menu, opened by Leave call, unless it is on the screen already), then Meet's confirmation box's End call, with both locks held from
        before the first press to the end; then the room leaves as `leave` does. Leave call is pressed only when it says it opens a menu (a
        Leave call that leaves at once is refused before anything is pressed). A box that does not come is a refusal, and the room stays,
        unless Meet no longer shows the call after a press (Leave call left at once, or the call ended without asking): then the room leaves
        as `leave` does, and says so in words when only Leave call was pressed (the call may go on for the others)."""
        pressed: List[str] = []

        async def end():
            if not (await self._look()).get("hosting"):
                raise MeetingControlError("Meet does not show the room as a host here; the call was not ended")
            if await self._label_state(MEET["end_for_all"]) is None:
                leave = await self._label_state(MEET["leave"])
                if leave is None:
                    raise MeetingControlError("Meet's Leave call button isn't on the screen")
                if not leave.get("menu"):
                    raise MeetingControlError("Meet's Leave call button does not open a menu here; nothing was pressed")
                if await self._press_label(MEET["leave"]) is None:
                    raise MeetingControlError("Meet's Leave call button isn't on the screen")
                pressed.append("leave")
                if not await self._until(lambda: self._label_state(MEET["end_for_all"])):
                    raise MeetingControlError("Meet's leave menu has no End the call for everyone")
            boxes = await self._box_keys()
            if await self._press_label(MEET["end_for_all"]) is None:
                raise MeetingControlError("Meet's End the call for everyone isn't on the screen")
            pressed.append("end")
            await self._confirm(boxes, MEET["confirm"]["end_for_all"], "ending the call for everyone")

        words: Optional[str] = None
        async with self._action_turn():
            before = await self._popups()
            try:
                await self._then_tidy(end, lambda: self._close_new_menus(before), after_success=False)
            except Exception:
                # After a press, a page that no longer shows the call has left it: the room must not stay connected to a dead page.
                if not pressed or await self._call_shown():
                    raise
                logger.warning("Meet no longer shows the call after End the call for everyone was asked for: leaving")
                if "end" not in pressed:
                    words = "Meet left the call; it may not have ended for everyone"
        await self.leave_meeting()   # the locks are released: leave takes the box reader's turn to close the room's panel
        if words is not None:
            raise MeetingControlError(words)
        return {}

    async def _call_shown(self) -> bool:
        """Whether Meet's page still shows the call: its microphone button or its Leave call button is drawn. A page that does not answer is
        taken as not showing it (it is on its way off the call)."""
        try:
            if (await self._look()).get("mute") is not None:
                return True
            return await self._label_state(MEET["leave"]) is not None
        except Exception as e:  # noqa: BLE001 - see above
            logger.info(f"Meet's page did not answer whether it still shows the call ({_first_line(e)})")
            return False

    async def _host_switches(self) -> List[Dict[str, Any]]:
        self._switches = [dict(s) for s in await self._answered(self._page.evaluate(READ_HOST_SWITCHES, MEET)) or [] if isinstance(s, dict)]
        return self._switches

    async def _host_switch(self, name: str, value: Any) -> Dict[str, Any]:
        """Turn one of the host panel's switches on or off ({"key": a key of MEET.host_switches, "on": true or false}): the host panel opened if
        it is not, the switch pressed only when it is not that way already, and then seen to be."""
        key = value.get("key") if isinstance(value, dict) else None
        on = value.get("on") if isinstance(value, dict) else None
        if key not in MEET["host_switches"] or not isinstance(on, bool):
            raise MeetingControlError("Say which host switch and whether it is on")
        words = MEET["host_switches"][key]

        async def switch():
            return next((s for s in await self._host_switches() if s.get("key") == key), None)

        async def flip():
            now = await switch()
            if now is None:
                raise MeetingControlError(f"Meet's host controls have no {words[0]} switch")
            if bool(now.get("on")) == on:
                return
            if await self._press_label(words, {"panel": "host"}) is None:
                raise MeetingControlError(f"Meet's {words[0]} switch can't be pressed")

            async def changed():
                after = await switch()
                return after is not None and bool(after.get("on")) == on

            if not await self._until(changed):
                raise MeetingControlError(f"Meet did not change {now.get('label') or words[0]}; try again")

        async with self._action_turn():
            await self._in_panel("host", flip)
            await self._read_panels()
        return await self._read_controls()

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
        readers = [self._reader, self._dialog_reader, self._people_reader]
        self._reader = self._dialog_reader = self._people_reader = None
        readers = [reader for reader in readers if reader is not None and reader is not asyncio.current_task()]
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        self._reset_dialogs()
