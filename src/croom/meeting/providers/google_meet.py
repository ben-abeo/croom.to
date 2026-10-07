"""
Google Meet provider.

Handles joining and controlling Google Meet meetings using browser automation.
"""

import asyncio
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional, Dict, Any

from croom.meeting.providers.base import MeetingProvider, MeetingInfo, MeetingState

logger = logging.getLogger(__name__)

# Playwright is optional - used for browser automation
try:
    from playwright.async_api import async_playwright, Browser, Page, BrowserContext
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


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
    CONNECT_TIMEOUT_MS = 30000       # for Meet's in-call controls after pressing Join
    ADMIT_TIMEOUT_MS = 300000        # how long a host may take to admit the room
    LOBBY_PHRASES = ("waiting for", "asking to join", "someone lets you in", "let you in")
    SIGN_IN_MESSAGE = ("The room's Google sign-in has expired or was never done; "
                       "stop the service and run croom --sign-in-meet on the device")
    BROWSER_ARGS = [
        "--use-fake-ui-for-media-stream",  # Auto-accept camera/mic
        "--disable-infobars",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-accelerated-2d-canvas",
        "--disable-gpu",
        "--window-size=1920,1080",
    ]

    @classmethod
    def context_options(cls) -> dict:
        """Browser context settings shared with `croom --check-meet`. No user-agent override:
        Meet refuses browsers it deems too old, and the bundled Chromium's own identity is current."""
        return {"permissions": ["camera", "microphone"], "viewport": {"width": 1920, "height": 1080}}

    def __init__(self, profile_dir: Optional[str] = None, room_name: str = "Conference Room",
                 headless: bool = False):
        super().__init__()
        # The signed-in Google profile (spec 2026-10-07, section 4.2); None means a guest browser.
        self._profile_dir: Optional[Path] = Path(profile_dir) if profile_dir else None
        self._room_name = room_name or "Conference Room"
        self._headless = headless
        # Where a screenshot goes when a join fails, so the TV need not be watched.
        self.failure_screenshot: Path = Path(tempfile.gettempdir()) / "croom-meet-failure.png"
        self._playwright = None
        self._browser: Optional["Browser"] = None
        self._context: Optional["BrowserContext"] = None
        self._page: Optional["Page"] = None

    @classmethod
    def from_config(cls, config) -> "GoogleMeetProvider":
        return cls(profile_dir=config.meeting.google_profile_dir or None,
                   room_name=config.room.name or "Conference Room")

    @property
    def profile_dir(self) -> Optional[Path]:
        return self._profile_dir

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
        """Open the browser: on the signed-in profile when one is configured, as a guest otherwise."""
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")

        logger.info("Initializing Google Meet provider...")
        self._playwright = await async_playwright().start()

        if self._profile_dir is not None:
            try:
                self._profile_dir.mkdir(parents=True, exist_ok=True)
                os.chmod(self._profile_dir, 0o700)
            except OSError as e:
                raise RuntimeError(f"Google Meet profile folder {self._profile_dir} is not usable: {e}") from e
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self._profile_dir), headless=self._headless, args=self.BROWSER_ARGS, **self.context_options(),
            )
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
            logger.info(f"Google Meet provider initialized with the signed-in profile at {self._profile_dir}")
            return

        self._browser = await self._playwright.chromium.launch(headless=self._headless, args=self.BROWSER_ARGS)
        self._context = await self._browser.new_context(**self.context_options())
        self._page = await self._context.new_page()
        logger.info("Google Meet provider initialized as a guest")

    async def shutdown(self) -> None:
        """Shutdown browser."""
        if self._state == MeetingState.CONNECTED:
            await self.leave_meeting()

        if self._page:
            await self._page.close()
            self._page = None

        if self._context:
            await self._context.close()
            self._context = None

        if self._browser:
            await self._browser.close()
            self._browser = None

        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

        logger.info("Google Meet provider shutdown")

    async def join_meeting(
        self,
        meeting_url: str,
        display_name: str = "Conference Room",
        camera_on: bool = True,
        mic_on: bool = True
    ) -> MeetingInfo:
        """Join a Google Meet meeting."""
        if not self._page:
            raise RuntimeError("Provider not initialized")

        meeting_id = self.extract_meeting_id(meeting_url)
        if not meeting_id:
            raise ValueError(f"Invalid Google Meet URL: {meeting_url}")

        # Construct full URL
        full_url = f"https://meet.google.com/{meeting_id}"

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
            await asyncio.sleep(2)

            # Handle pre-join screen
            await self._handle_prejoin(display_name, camera_on, mic_on)

            # Click join button
            await self._click_join_button()

            # Wait for connection
            await self._wait_for_connection()

            self._set_state(MeetingState.CONNECTED)
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
        elif self._profile_dir is None:
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
        await button.click()
        await asyncio.sleep(0.5)
        label = (await button.get_attribute("aria-label") or "").lower()
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

        self._set_state(MeetingState.LEAVING)
        logger.info("Leaving Google Meet...")

        try:
            # Click leave button
            leave_btn = await self._page.query_selector('[aria-label*="Leave" i]')
            if leave_btn:
                await leave_btn.click()
                await asyncio.sleep(1)

            # Navigate away
            await self._page.goto("about:blank")

        except Exception as e:
            logger.error(f"Error leaving meeting: {e}")

        self._current_meeting = None
        self._set_state(MeetingState.IDLE)
        logger.info("Left Google Meet")

    async def toggle_camera(self) -> bool:
        """Toggle camera on/off."""
        if not self._page or self._state != MeetingState.CONNECTED:
            return False

        try:
            # Keyboard shortcut: Ctrl+E
            await self._page.keyboard.press("Control+e")
            await asyncio.sleep(0.5)

            # Update state
            if self._current_meeting:
                self._current_meeting.is_camera_on = not self._current_meeting.is_camera_on

            return self._current_meeting.is_camera_on if self._current_meeting else False

        except Exception as e:
            logger.error(f"Failed to toggle camera: {e}")
            return False

    async def toggle_mute(self) -> bool:
        """Toggle microphone mute."""
        if not self._page or self._state != MeetingState.CONNECTED:
            return True

        try:
            # Keyboard shortcut: Ctrl+D
            await self._page.keyboard.press("Control+d")
            await asyncio.sleep(0.5)

            # Update state
            if self._current_meeting:
                self._current_meeting.is_muted = not self._current_meeting.is_muted

            return self._current_meeting.is_muted if self._current_meeting else True

        except Exception as e:
            logger.error(f"Failed to toggle mute: {e}")
            return True
