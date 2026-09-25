"""
Zoom through the Meeting SDK (spec 2026-09-25 Zoom, section 4.4). The room's
headed Chromium opens a page served on this device's loopback address, and
Zoom's web SDK renders the meeting there. The device mints the signature and,
when a room Zoom user is configured, fetches that user's ZAK first, so meetings
hosted by other Zoom accounts can be joined too.
"""

import asyncio
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from croom.core.config import Config
from croom.meeting.providers.base import MeetingInfo, MeetingProvider, MeetingState
from croom.meeting.providers.zoom import ZoomProvider
from croom.meeting.providers.zoom_sdk_site import SDK_VERSION, ZoomSdkSite
from croom.meeting.zoom_auth import (
    ZoomApi,
    ZoomCredentials,
    load_zoom_credentials,
    meeting_sdk_signature,
)

logger = logging.getLogger(__name__)

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class ZoomSdkProvider(MeetingProvider):
    """Joins Zoom meetings with Zoom's Meeting SDK in the room's browser."""

    SDK_VERSION = SDK_VERSION
    CONNECT_TIMEOUT_S = 90
    CONNECT_GRACE_MS = 5000  # after join's success callback, how long the page waits for a status event
    LOBBY_TIMEOUT_S = 300
    LEAVE_TIMEOUT_S = 5
    CLOSE_TIMEOUT_S = 5
    BUTTON_TIMEOUT_MS = 3000
    VIDEO_BUTTON = '[aria-label*="Start Video" i], [aria-label*="Stop Video" i]'
    BROWSER_ARGS = [
        "--use-fake-ui-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-infobars",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1920,1080",
    ]
    USER_AGENT = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    def __init__(self, credentials: ZoomCredentials, room_name: str = "Conference Room",
                 api: Optional[ZoomApi] = None, headless: bool = False,
                 extra_init_script: Optional[str] = None, block_sdk_cdn: bool = False,
                 site: Optional[ZoomSdkSite] = None):
        super().__init__()
        self._credentials = credentials
        self._room_name = room_name
        if api is None and credentials.has_room_user:
            api = ZoomApi(credentials.account_id, credentials.s2s_client_id, credentials.s2s_client_secret)
        self._api = api
        self._headless = headless
        self._extra_init_script = extra_init_script
        self._block_sdk_cdn = block_sdk_cdn
        self._site = site or ZoomSdkSite()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._events: Optional[asyncio.Queue] = None
        self._join_task: Optional[asyncio.Task] = None
        self._watcher: Optional[asyncio.Task] = None
        self._muted = False
        self._camera_on = True

    @classmethod
    def from_config(cls, config: Config) -> "ZoomSdkProvider":
        credentials = load_zoom_credentials(config.meeting.zoom_credentials_path)
        return cls(credentials, room_name=config.room.name or "Conference Room")

    @property
    def name(self) -> str:
        return "zoom"

    @property
    def display_name(self) -> str:
        return "Zoom"

    @classmethod
    def can_handle_url(cls, url: str) -> bool:
        return ZoomProvider.can_handle_url(url)

    @classmethod
    def extract_meeting_id(cls, url: str) -> Optional[str]:
        return ZoomProvider.extract_meeting_id(url)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright not installed")
        self._events = asyncio.Queue()
        await self._site.start()
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._headless, args=self.BROWSER_ARGS)
        self._context = await self._browser.new_context(
            permissions=["camera", "microphone"],
            viewport={"width": 1920, "height": 1080},
            user_agent=self.USER_AGENT,
        )
        if self._block_sdk_cdn:
            async def abort(route):
                await route.abort()
            await self._context.route("https://source.zoom.us/**", abort)
        if self._extra_init_script:
            await self._context.add_init_script(self._extra_init_script)
        self._page = await self._context.new_page()
        await self._page.expose_function("crystalMeetEvent", self._on_page_event)
        logger.info(f"Zoom Meeting SDK provider ready (page on http://127.0.0.1:{self._site.port}/meeting)")

    async def shutdown(self) -> None:
        await self._cancel_tasks()
        if self._state == MeetingState.CONNECTED:
            await self.leave_meeting()
        for attribute in ("_page", "_context", "_browser"):
            closer = getattr(self, attribute)
            if closer is not None:
                try:
                    await asyncio.wait_for(closer.close(), timeout=self.CLOSE_TIMEOUT_S)
                except Exception as e:  # noqa: BLE001 - a hung browser must not hang the agent
                    logger.warning(f"Zoom browser {attribute[1:]} did not close cleanly: {e}")
                setattr(self, attribute, None)
        if self._playwright is not None:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=self.CLOSE_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Playwright did not stop cleanly: {e}")
            self._playwright = None
        await self._site.stop()

    def _on_page_event(self, state: str, detail: str = "") -> None:
        """Called by the page (through the exposed function) on every bridge state change."""
        if self._events is not None:
            self._events.put_nowait((str(state), str(detail or "")))

    def _drain_events(self) -> None:
        while self._events is not None and not self._events.empty():
            self._events.get_nowait()

    async def _cancel_tasks(self) -> None:
        """Stop the meeting watcher and any join still in flight (never the task doing the cancelling)."""
        current = asyncio.current_task()
        for attribute in ("_watcher", "_join_task"):
            task = getattr(self, attribute)
            setattr(self, attribute, None)
            if task is None or task is current or task.done():
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - the task's own outcome is not ours
                pass

    async def _blank_page(self) -> None:
        try:
            await self._page.goto("about:blank")
        except Exception:  # noqa: BLE001 - the page may already be gone
            pass

    # ------------------------------------------------------------------
    # Joining
    # ------------------------------------------------------------------

    async def join_meeting(self, meeting_url: str, display_name: str = "Conference Room",
                           camera_on: bool = True, mic_on: bool = True) -> MeetingInfo:
        if self._page is None:
            raise RuntimeError("Provider not initialized")
        meeting_id = self.extract_meeting_id(meeting_url)
        if not meeting_id:
            raise ValueError(f"Invalid Zoom URL: {meeting_url}")
        passcode = parse_qs(urlparse(meeting_url).query).get("pwd", [""])[0]
        await self._cancel_tasks()
        self._join_task = asyncio.current_task()
        self._current_meeting = MeetingInfo(platform=self.name, meeting_id=meeting_id, meeting_url=meeting_url,
                                            is_camera_on=camera_on, is_muted=not mic_on)
        self._set_state(MeetingState.JOINING)
        logger.info(f"Joining Zoom meeting {meeting_id} through the Meeting SDK")
        try:
            zak = None
            if self._api is not None:
                zak = await self._api.user_zak(self._credentials.room_user)
            signature = meeting_sdk_signature(self._credentials.sdk_client_id, self._credentials.sdk_client_secret, meeting_id)
            params: Dict[str, Any] = {
                "meetingNumber": meeting_id, "passWord": passcode, "userName": display_name,
                "signature": signature, "zak": zak, "micOn": mic_on, "cameraOn": camera_on,
                "sdkVersion": self.SDK_VERSION, "connectGraceMs": self.CONNECT_GRACE_MS,
            }
            token = self._site.register_join(params)
            # A fragment-only change would not reload the page, so leave it first; then forget the old page's events.
            await self._blank_page()
            self._drain_events()
            await self._page.goto(self._site.url("/meeting") + "#" + token, wait_until="load")
            await self._wait_for_connection()
            await self._read_state_from_zoom()
            if not camera_on and self._camera_on:
                await self._press_video_button_quietly()
            self._current_meeting.is_muted = self._muted
            self._current_meeting.is_camera_on = self._camera_on
            self._watcher = asyncio.create_task(self._watch_meeting())
            self._set_state(MeetingState.CONNECTED)
            logger.info(f"Connected to Zoom meeting {meeting_id}")
            return self._current_meeting
        except asyncio.CancelledError:
            self._current_meeting = None
            raise
        except Exception as e:
            if self._current_meeting is not None:
                self._current_meeting.error_message = str(e)
            self._set_state(MeetingState.ERROR)
            logger.error(f"Zoom join failed: {e}")
            raise
        finally:
            if self._join_task is asyncio.current_task():
                self._join_task = None

    async def _wait_for_connection(self) -> None:
        """Follow the page's events until Zoom reports connected; a waiting room extends the wait."""
        deadline = time.monotonic() + self.CONNECT_TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if await self._camera_is_on() is not None:
                    logger.warning("Zoom reported no status, but its meeting controls are on screen; treating the join as connected")
                    return
                raise RuntimeError("Zoom did not connect; the page says: " + await self._page_words())
            try:
                state, detail = await asyncio.wait_for(self._events.get(), timeout=remaining)
            except asyncio.TimeoutError:
                continue
            if state == "connected":
                return
            if state == "waiting":
                if self._state != MeetingState.IN_LOBBY:
                    self._set_state(MeetingState.IN_LOBBY)
                    logger.info("Waiting in the Zoom waiting room")
                    deadline = time.monotonic() + self.LOBBY_TIMEOUT_S
            elif state == "error":
                raise RuntimeError(detail or "Zoom reported an error")
            elif state == "left":
                raise RuntimeError("Zoom ended the join before connecting")

    async def _watch_meeting(self) -> None:
        """After connecting: the host ending the meeting, or the SDK reporting an error, returns the room to idle."""
        while True:
            state, detail = await self._events.get()
            if state == "left":
                logger.info("Zoom meeting ended")
                await self._blank_page()
                self._current_meeting = None
                self._watcher = None
                self._set_state(MeetingState.IDLE)
                return
            if state == "error":
                if self._current_meeting is not None:
                    self._current_meeting.error_message = detail or "Zoom reported an error"
                logger.error(f"Zoom meeting error: {detail}")
                self._watcher = None
                self._set_state(MeetingState.ERROR)
                return

    async def _page_words(self) -> str:
        try:
            return await self._page.evaluate("() => document.body.innerText.replace(/\\s+/g, ' ').trim().slice(0, 240)")
        except Exception:  # noqa: BLE001
            return "(page text unavailable)"

    # ------------------------------------------------------------------
    # State as Zoom has it
    # ------------------------------------------------------------------

    async def _read_state_from_zoom(self) -> None:
        """Mute and camera as Zoom has them after joining: meetings often start muted or with video off."""
        try:
            self._muted = bool(await self._page.evaluate("() => !!(window.crystalMeet && window.crystalMeet.muted)"))
        except Exception:  # noqa: BLE001
            pass
        camera = await self._camera_is_on()
        if camera is not None:
            self._camera_on = camera

    async def _camera_is_on(self) -> Optional[bool]:
        """From the Client View's toolbar: 'Stop Video' means on, 'Start Video' means off; None when the button is absent."""
        try:
            label = await self._page.evaluate(
                "(selector) => { const b = document.querySelector(selector); "
                "return b ? (b.getAttribute('aria-label') || b.textContent || '') : null; }",
                self.VIDEO_BUTTON,
            )
        except Exception:  # noqa: BLE001
            return None
        if label is None:
            return None
        text = str(label).lower()
        if "stop video" in text:
            return True
        if "start video" in text:
            return False
        return None

    # ------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------

    async def leave_meeting(self) -> None:
        if self._page is None:
            return
        await self._cancel_tasks()
        self._set_state(MeetingState.LEAVING)
        try:
            await asyncio.wait_for(
                self._page.evaluate("() => (window.crystalMeet && window.crystalMeet.leave) ? window.crystalMeet.leave() : true"),
                timeout=self.LEAVE_TIMEOUT_S,
            )
        except Exception as e:  # noqa: BLE001 - the page may already be gone
            logger.warning(f"Zoom leave did not confirm: {e}")
        await self._blank_page()
        self._current_meeting = None
        self._set_state(MeetingState.IDLE)
        logger.info("Left the Zoom meeting")

    def _require_meeting(self) -> None:
        if self._state != MeetingState.CONNECTED or self._page is None:
            raise RuntimeError("Not in a meeting")

    async def toggle_mute(self) -> bool:
        self._require_meeting()
        muted = await self._page.evaluate("(muted) => window.crystalMeet.mute(muted)", not self._muted)
        self._muted = bool(muted)
        if self._current_meeting:
            self._current_meeting.is_muted = self._muted
        return self._muted

    async def toggle_camera(self) -> bool:
        self._require_meeting()
        await self._press_video_button()
        camera = await self._camera_is_on()
        self._camera_on = (not self._camera_on) if camera is None else camera
        if self._current_meeting:
            self._current_meeting.is_camera_on = self._camera_on
        return self._camera_on

    async def _press_video_button(self) -> None:
        """The SDK has no own-video call; press the Client View's toolbar button."""
        button = await self._page.query_selector(self.VIDEO_BUTTON)
        if button is None:
            raise RuntimeError("Zoom's video button was not found")
        await button.click(timeout=self.BUTTON_TIMEOUT_MS)

    async def _press_video_button_quietly(self) -> None:
        """At join time a missing or slow toolbar must not fail a connected meeting."""
        try:
            await self._press_video_button()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not set the camera at join: {e}")
        camera = await self._camera_is_on()
        if camera is not None:
            self._camera_on = camera
