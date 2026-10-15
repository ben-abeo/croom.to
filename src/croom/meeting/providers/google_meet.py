"""
Google Meet provider.

Handles joining and controlling Google Meet meetings using browser automation.
"""

import asyncio
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Optional, Dict, Any

from croom.meeting.providers.base import (
    MeetingControlError,
    MeetingInfo,
    MeetingProvider,
    MeetingState,
    controls_from,
)

logger = logging.getLogger(__name__)

# Playwright is optional - used for browser automation
try:
    from playwright.async_api import Page
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# One reading of Meet's call page (spec 2026-10-13, section 4.5). Each control's button is found by a label that names what pressing it does, and
# its state is what that label says: "Turn off camera" means the camera is on, "Lower hand" means the hand is up. A button the page does not have,
# or whose label names no such action, reads as null. `people` is the people button with the number in its label or its text; `taken` is whether
# something that takes typing has the keyboard (a text field, the chat box, an editable area, a frame, say).
READ_CALL = r"""() => {
  // Only what is drawn counts: a copy the page keeps out of sight is not the button. Two drawn buttons that say different things say nothing (null).
  const labelled = [...document.querySelectorAll("[aria-label]")].filter((el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden");
  const find = (pattern) => labelled.find((el) => pattern.test(el.getAttribute("aria-label")));
  const state = (pattern, said) => {
    const says = labelled.filter((el) => pattern.test(el.getAttribute("aria-label"))).map((el) => el.getAttribute("aria-label").match(pattern)[1].toLowerCase() === said);
    return says.length && says.every((on) => on === says[0]) ? { on: says[0] } : null;
  };
  const people = find(/show everyone|participants|^people\b/i);
  const count = people && (people.getAttribute("aria-label").match(/\d+/) || people.textContent.match(/\d+/));
  // Whether something that takes typing has the keyboard (the bare C would be typed into it): a text field, a list that selects by typing, an editable
  // area, an element that says it is one, or a frame (what it holds is not seen from here). A button, a link, a menu item, a container: the key goes on to the page.
  const held = document.activeElement;
  const kind = (held && held.getAttribute && (held.getAttribute("type") || "text").toLowerCase()) || "";
  const role = (held && held.getAttribute && (held.getAttribute("role") || "").toLowerCase()) || "";
  const typing = Boolean(held && held !== document.body && (
    held.isContentEditable || ["TEXTAREA", "SELECT", "IFRAME"].includes(held.tagName)
    || (held.tagName === "INPUT" && !["checkbox", "radio", "button", "submit", "reset", "image", "file", "range", "color", "hidden"].includes(kind))
    || ["textbox", "searchbox", "combobox", "spinbutton"].includes(role)));
  return {
    mute: state(/turn (on|off) microphone/i, "off"),
    camera: state(/turn (on|off) camera/i, "off"),
    raise_hand: state(/(raise|lower) hand/i, "lower"),
    captions: state(/turn (on|off) captions?/i, "off"),
    people: people ? { count: count ? Number(count[0]) : null } : null,
    taken: typing,
  };
}"""
# What the table is told when the bare C key cannot go to the page: it would be typed into whatever has the keyboard (the chat box).
KEYBOARD_TAKEN_WORDS = "Meet's chat box has the keyboard; close it on the TV first"


def meet_controls(buttons: Dict[str, Dict[str, Any]], muted: bool) -> Dict[str, Any]:
    """The table's control model (spec 2026-10-13, section 3) for what Meet's page offers a participant: the buttons it has (camera, raise_hand,
    captions, each with its state), the microphone's state, and nothing else. The room is never a host here, Meet's reactions, views and pinning are
    not driven, and the agent does not read Meet's chat: the table draws only what is available."""
    model = controls_from({"me": {"muted": muted}, "role": "participant", "buttons": buttons, "participants": [], "recording": False}, [])
    model["reactions"] = {"available": False}
    model["chat"] = {"available": False}
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

    def __init__(self, display=None, room_name: str = "Conference Room"):
        super().__init__()
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

    @classmethod
    def from_config(cls, config, display=None) -> "GoogleMeetProvider":
        return cls(display, room_name=config.room.name or "Conference Room")

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
            logger.info(f"Connected to Google Meet: {meeting_id}")

            return self._current_meeting

        except Exception as e:
            logger.error(f"Failed to join meeting: {e}")
            self._current_meeting.error_message = str(e)
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

    # ------------------------------------------------------------------
    # Controls from the table (spec 2026-10-13, sections 3 and 4.5)
    # ------------------------------------------------------------------

    async def control(self, name: str, value: Any = None) -> Dict[str, Any]:
        """A control from the table, carried out with Meet's own keyboard shortcut (see MeetingProvider.control): the microphone, the
        camera, the raised hand and the captions. Everything else Meet's page does not offer here is refused in words. The answer is the
        control model as it is afterwards."""
        if self._page is None or self._state != MeetingState.CONNECTED:
            raise MeetingControlError("Not in a meeting")
        if name == "mute":
            await self.toggle_mute()
        elif name in self.SHORTCUTS:
            await self._press(name)
        elif name == "leave":
            await self.leave_meeting()                    # the model says leave is available (the table's Leave has its own route): the same answer as Zoom's
            return {}
        elif name == "chat.seen":
            return {"chat": self._current_meeting.chat}   # the agent reads no chat from Meet: nothing to mark
        else:
            raise MeetingControlError("Not available in a Meet call")
        return await self._read_controls()

    async def _press(self, name: str) -> bool:
        """Press a control's shortcut and wait for the change to show in its button's label (for PRESS_WAIT_S at most), one press at a
        time; the answer is the state the label shows then (True is on: the microphone live, the camera on, the hand up, the captions on).
        A control whose button is not on the page is refused before anything is pressed, and so is the bare C key while something that takes
        typing has the keyboard (it would be typed into the chat box). A press that changes no label is a refusal in words, never a
        success."""
        words = self.CONTROL_WORDS[name]
        async with self._pressing:
            try:
                reading = await self._look()
                before = reading.get(name)
                if before is None:
                    raise MeetingControlError(f"Meet shows no {words} button right now")
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
        labels = await self._page.evaluate(
            "() => [...document.querySelectorAll('[aria-label]')].filter((e) => e.getClientRects().length > 0)"
            ".map((e) => ({ tag: e.tagName.toLowerCase(), role: e.getAttribute('role') || '', label: e.getAttribute('aria-label') }))")
        held = await self._page.evaluate("() => { const h = document.activeElement; return h ? h.tagName.toLowerCase() + (h.getAttribute('role') ? '[role=' + h.getAttribute('role') + ']' : '') : ''; }")
        return {"labels": labels, "reading": await self._look(), "focus": held}

    # ------------------------------------------------------------------
    # The model, as Meet's page has it
    # ------------------------------------------------------------------

    async def _look(self) -> Dict[str, Any]:
        """One reading of Meet's call page (READ_CALL), which the page must answer within LOOK_TIMEOUT_S."""
        try:
            return await asyncio.wait_for(self._page.evaluate(READ_CALL), self.LOOK_TIMEOUT_S)
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
        """Take a reading of the page into the meeting. A button the page does not show, or whose label says no state, leaves the
        microphone and camera as they last were (the model offers the control only when its button reads); the people count is the number the
        people button shows, and 0 without one: Meet's roster is not read."""
        info = self._current_meeting
        if info is None:
            return
        if reading["mute"] is not None:
            info.is_muted = not reading["mute"]["on"]
        if reading["camera"] is not None:
            info.is_camera_on = reading["camera"]["on"]
        buttons = {name: {"on": reading[name]["on"]} for name in ("camera", "raise_hand", "captions") if reading[name] is not None}
        info.controls = meet_controls(buttons, info.is_muted)
        people = reading["people"]
        info.people = {"count": int(people["count"] or 0) if people is not None else 0, "list": []}

    async def _read_regularly(self) -> None:
        """For as long as the call lasts, read Meet's buttons again every READ_INTERVAL_S."""
        while True:
            await asyncio.sleep(self.READ_INTERVAL_S)
            if self._state != MeetingState.CONNECTED:
                return
            await self._read_controls()

    async def _stop_reading(self) -> None:
        """Stop the regular reading and wait for it to be over: what the provider starts on its own never outlives the call."""
        reader, self._reader = self._reader, None
        if reader is None or reader is asyncio.current_task():
            return
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
