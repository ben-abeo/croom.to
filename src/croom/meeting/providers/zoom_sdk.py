"""
Zoom through the Meeting SDK (spec 2026-09-25 Zoom, section 4.4). The TV's
page is navigated to a page served on this device's loopback address, and
Zoom's web SDK renders the meeting there. The device mints the signature and,
when a room Zoom user is configured, fetches that user's ZAK first, so meetings
hosted by other Zoom accounts can be joined too.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from croom.core.config import Config
from croom.meeting.providers.base import MeetingControlError, MeetingInfo, MeetingProvider, MeetingState
from croom.meeting.providers.zoom import ZoomProvider
from croom.meeting.providers.zoom_sdk_site import SDK_VERSION, ZoomSdkSite
from croom.meeting.zoom_auth import (
    ZoomApi,
    ZoomCredentials,
    load_zoom_credentials,
    meeting_sdk_signature,
)

logger = logging.getLogger(__name__)

# What Zoom's error carries when the passcode is the problem: its own words, or its wrong-passcode code (3004).
PASSCODE_REFUSAL = re.compile(r"pass(?:word|code)|\bcode 3004\b", re.IGNORECASE)
# How the join's error starts when Zoom refused it over the passcode; the room page looks for these words (app.js).
PASSCODE_ASKED = "Zoom needs the meeting passcode"
# How Playwright reports an Error the page's own code threw: "Page.evaluate: Error: <the page's words>".
PAGE_REFUSAL = re.compile(r"^(?:Page\.evaluate: )?Error: (?P<words>.+)$")


def page_failure(error: BaseException, doing: str) -> str:
    """The words for the table when a call into the page fails. A refusal the page put in words (Zoom's own reason, a
    camera button still starting) is passed through as it is; anything else (the page gone, a fault in a script) is
    named with what was being done."""
    text = next((line.strip() for line in str(error).splitlines() if line.strip()), "") or type(error).__name__
    refusal = PAGE_REFUSAL.match(text)
    if refusal:
        return refusal.group("words").strip()
    return f"{doing}: {re.sub(r'^Page[.]evaluate: ', '', text)}"


class ZoomSdkProvider(MeetingProvider):
    """Joins Zoom meetings with Zoom's Meeting SDK in the room's browser."""

    SDK_VERSION = SDK_VERSION
    CONNECT_TIMEOUT_S = 90
    PASSCODE_CHECK_S = 5  # at most this long between two readings of the page's words for Zoom's own passcode complaint
    CONNECT_GRACE_MS = 5000  # after join's success callback, how long the page waits for a status event
    LOBBY_TIMEOUT_S = 300
    LEAVE_TIMEOUT_S = 5
    CLOSE_TIMEOUT_S = 5
    PAGE_CALL_TIMEOUT_S = 10  # a call into the page's bridge (reading the state, muting) answers in this time, or never will
    VIDEO_READY_MS = 8000     # a camera press waits this long for Zoom to enable its video button (the encoder starting) ...
    VIDEO_SWITCH_MS = 10000   # ... and then this long for Zoom to switch the camera

    def __init__(self, credentials: ZoomCredentials, room_name: str = "Conference Room",
                 api: Optional[ZoomApi] = None, display=None,
                 extra_init_script: Optional[str] = None, block_sdk_cdn: bool = False,
                 site: Optional[ZoomSdkSite] = None):
        super().__init__()
        self._credentials = credentials
        self._room_name = room_name
        if api is None and credentials.has_room_user:
            api = ZoomApi(credentials.account_id, credentials.s2s_client_id, credentials.s2s_client_secret)
        self._api = api
        # The TV display (spec 2026-10-07 TV, section 4.3) owns the browser; this provider borrows its page.
        self._display = display
        self._extra_init_script = extra_init_script
        self._block_sdk_cdn = block_sdk_cdn
        self._site = site or ZoomSdkSite()
        self._page = None
        self._exposed_on = None   # the page object crystalMeetEvent was exposed on
        self._events: Optional[asyncio.Queue] = None
        self._join_task: Optional[asyncio.Task] = None
        self._watcher: Optional[asyncio.Task] = None
        self._muted = False
        self._camera_on = True

    @classmethod
    def from_config(cls, config: Config, display=None) -> "ZoomSdkProvider":
        credentials = load_zoom_credentials(config.meeting.zoom_credentials_path)
        return cls(credentials, room_name=config.room.name or "Conference Room", display=display)

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
        if self._display is None:
            raise RuntimeError("Zoom SDK provider needs the TV display")
        self._events = asyncio.Queue()
        await self._site.start()
        context = self._display.context
        if context is not None and self._block_sdk_cdn:
            async def abort(route):
                await route.abort()
            await context.route("https://source.zoom.us/**", abort)
        if context is not None and self._extra_init_script:
            await context.add_init_script(self._extra_init_script)
        await self._take_page()
        logger.info(f"Zoom Meeting SDK provider ready (page on http://127.0.0.1:{self._site.port}/meeting)")

    async def _take_page(self, for_meeting: bool = False) -> None:
        """Borrow the display's page (claim it for a meeting) and make sure the bridge can reach us from it."""
        self._page = await (self._display.claim() if for_meeting else self._display.page())
        if self._exposed_on is not self._page:
            await self._page.expose_function("crystalMeetEvent", self._on_page_event)
            self._exposed_on = self._page

    async def shutdown(self) -> None:
        await self._cancel_tasks()
        if self._state == MeetingState.CONNECTED:
            await self.leave_meeting()
        self._page = None
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
        """Hand the page back to the screensaver; the page may already be gone."""
        try:
            await self._display.show_idle()
        except Exception:  # noqa: BLE001
            pass

    async def _leave_meeting_page(self) -> None:
        """A fragment-only change would not reload the meeting page, so leave it first when still on it
        (after a failed join, for example). The page stays claimed for the meeting that follows."""
        if self._page.url.split("#")[0] != self._site.url("/meeting"):
            return
        try:
            await self._page.goto("about:blank")
        except Exception:  # noqa: BLE001 - the page may already be gone
            pass

    # ------------------------------------------------------------------
    # Joining
    # ------------------------------------------------------------------

    async def join_meeting(self, meeting_url: str, display_name: str = "Conference Room",
                           camera_on: bool = True, mic_on: bool = True,
                           passcode: Optional[str] = None) -> MeetingInfo:
        if self._display is None:
            raise RuntimeError("Provider not initialized")
        await self._take_page(for_meeting=True)
        meeting_id = self.extract_meeting_id(meeting_url)
        if not meeting_id:
            raise ValueError(f"Invalid Zoom URL: {meeting_url}")
        # A passcode given with the join wins over the link's own pwd: a person who types a code after Zoom refused the
        # link meant that code. Without one, the link's pwd (a blank pwd is none), else no passcode.
        link_passcode = parse_qs(urlparse(meeting_url).query).get("pwd", [""])[0]
        join_passcode = passcode or link_passcode or ""
        await self._cancel_tasks()
        self._join_task = asyncio.current_task()
        self._muted, self._camera_on = not mic_on, camera_on   # this join's own start until Zoom says otherwise, never the last meeting's
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
                "meetingNumber": meeting_id, "passWord": join_passcode, "userName": display_name,
                "signature": signature, "zak": zak, "micOn": mic_on, "cameraOn": camera_on,
                "sdkVersion": self.SDK_VERSION, "connectGraceMs": self.CONNECT_GRACE_MS,
            }
            token = self._site.register_join(params)
            await self._leave_meeting_page()
            self._drain_events()   # forget the old page's events
            await self._page.goto(self._site.url("/meeting") + "#" + token, wait_until="load")
            await self._wait_for_connection()
            zoom = await self._read_state_from_zoom()
            await self._apply_wanted_state(zoom, camera_on, mic_on)
            self._publish_state()
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
                if await self._toolbar_up():
                    logger.warning("Zoom reported no status, but its meeting controls are on screen; treating the join as connected")
                    return
                # Zoom may refuse a passcode with a dialog of its own ("Join parameters: password are invalid!") and no
                # error event: the page's words are read the way an error's are.
                words = await self._page_words()
                if PASSCODE_REFUSAL.search(words):
                    raise RuntimeError(f"{PASSCODE_ASKED} ({words})")
                raise RuntimeError("Zoom did not connect; the page says: " + words)
            try:
                state, detail = await asyncio.wait_for(self._events.get(), timeout=min(remaining, self.PASSCODE_CHECK_S))
            except asyncio.TimeoutError:
                # Zoom may put a passcode complaint of its own on the page and never send an error event, so the page's
                # words are read every few seconds, not only when the wait runs out. Not in the waiting room (Zoom took
                # the passcode to put the room there, and the meeting's title may say "password"), nor at the deadline,
                # which reads them below.
                if remaining > self.PASSCODE_CHECK_S and self._state != MeetingState.IN_LOBBY:
                    words = await self._page_words()
                    if PASSCODE_REFUSAL.search(words):
                        raise RuntimeError(f"{PASSCODE_ASKED} ({words})")
                continue
            if state == "connected":
                return
            if state == "waiting":
                if self._state != MeetingState.IN_LOBBY:
                    self._set_state(MeetingState.IN_LOBBY)
                    logger.info("Waiting in the Zoom waiting room")
                    deadline = time.monotonic() + self.LOBBY_TIMEOUT_S
            elif state == "error":
                raise self._join_error(detail)
            elif state == "left":
                raise RuntimeError("Zoom ended the join before connecting")

    @staticmethod
    def _join_error(detail: str) -> RuntimeError:
        """Zoom's reason for refusing the join; one over the passcode is named as such, whatever Zoom's wording."""
        if detail and PASSCODE_REFUSAL.search(detail):
            return RuntimeError(f"{PASSCODE_ASKED} ({detail})")
        return RuntimeError(detail or "Zoom reported an error")

    async def _watch_meeting(self) -> None:
        """After connecting: the host ending the meeting, or the SDK reporting an error, returns the room to idle."""
        while True:
            state, detail = await self._events.get()
            if state == "state":   # a change Zoom made: the host muting the room, video stopped
                try:
                    self._take_state(json.loads(detail))
                except ValueError:
                    logger.debug(f"Unreadable state event from the page: {detail!r}")
                continue
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

    async def _toolbar_up(self) -> bool:
        """Whether the client's own video button is on the screen, whatever it says (just "Video" when Zoom has no usable
        camera): the surest sign of a meeting in progress."""
        try:
            video = await self._page.evaluate("() => window.crystalMeet.video()")
        except Exception:  # noqa: BLE001
            return False
        return isinstance(video, dict) and video.get("label") is not None

    async def _page_words(self) -> str:
        try:
            return await self._page.evaluate("() => document.body.innerText.replace(/\\s+/g, ' ').trim().slice(0, 240)")
        except Exception:  # noqa: BLE001
            return "(page text unavailable)"

    # ------------------------------------------------------------------
    # State as Zoom has it
    # ------------------------------------------------------------------

    async def _page_call(self, script: str, arg: Any, doing: str, timeout: Optional[float] = None) -> Any:
        """One call into the page's bridge, bounded in time; a failure is a MeetingControlError in words for the table."""
        timeout = self.PAGE_CALL_TIMEOUT_S if timeout is None else timeout
        try:
            return await asyncio.wait_for(self._page.evaluate(script, arg), timeout)
        except asyncio.TimeoutError as e:
            raise MeetingControlError(f"{doing}: the meeting page did not answer within {timeout:g} seconds") from e
        except Exception as e:  # noqa: BLE001 - the page's refusal, or the page gone: words either way
            raise MeetingControlError(page_failure(e, doing)) from e

    async def _read_state_from_zoom(self) -> Dict[str, Any]:
        """Mute and camera as Zoom has them: the current user's record and the client's own video button. Meetings
        often start the room muted or with video off, and the host can change either at any time. Returns what the page
        said (empty when it could not say); what Zoom left unsaid (null) keeps the last value."""
        try:
            state = await self._page_call("() => window.crystalMeet.readState()", None, "Could not read the meeting state")
        except MeetingControlError as e:
            logger.warning(str(e))
            return {}
        self._take_state(state)
        return state if isinstance(state, dict) else {}

    def _take_state(self, state: Any) -> None:
        if not isinstance(state, dict):
            return
        if isinstance(state.get("muted"), bool):
            self._muted = state["muted"]
        if isinstance(state.get("videoOn"), bool):
            self._camera_on = state["videoOn"]
        self._publish_state()

    def _publish_state(self) -> None:
        if self._current_meeting is not None:
            self._current_meeting.is_muted = self._muted
            self._current_meeting.is_camera_on = self._camera_on

    async def _apply_wanted_state(self, zoom: Dict[str, Any], camera_on: bool, mic_on: bool) -> None:
        """The room's own defaults at join (the controller's ruling). The microphone towards muted only: a room that
        should start muted is muted, but a room Zoom has muted (the host's mute on entry) stays muted when the room wants
        its mic on, and the table offers Unmute, one tap away; unmuting it would hang on whether the host's mute landed
        before or after this look. The camera both ways: a meeting may start the room's video off, but the host cannot
        stop it on entry. What Zoom has afterwards is what counts."""
        if not mic_on and zoom.get("muted") is not True:
            try:
                await self._page_call("(muted) => window.crystalMeet.mute(muted)", True, "Could not mute the microphone")
            except MeetingControlError as e:
                logger.warning(f"The microphone was not muted at join: {e}")
        video = zoom.get("videoOn")
        if isinstance(video, bool) and video != camera_on:
            try:
                await self._set_video(camera_on)
            except MeetingControlError as e:
                logger.warning(f"The camera was not set at join: {e}")
        await self._read_state_from_zoom()

    async def _set_video(self, on: bool) -> bool:
        """Press the client's video button when Zoom's state differs. The page waits for Zoom to enable the button and
        to switch, and says in words what did not happen (no button yet, no usable camera, still starting, no switch)."""
        waits = {"readyMs": self.VIDEO_READY_MS, "switchMs": self.VIDEO_SWITCH_MS}
        timeout = (self.VIDEO_READY_MS + self.VIDEO_SWITCH_MS) / 1000 + 2   # the page's own waits, and a margin
        result = await self._page_call("([on, waits]) => window.crystalMeet.setVideo(on, waits)", [on, waits],
                                       "Could not switch the camera", timeout=timeout)
        return bool(result)

    async def diagnostics(self) -> Dict[str, Any]:
        """What the page sees: Zoom's field names and the client's buttons, for pinning the real wording."""
        self._require_meeting()
        return await self._page_call("() => window.crystalMeet.diagnostics()", None, "Could not read the meeting page")

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
        await self._read_state_from_zoom()   # the host may have muted the room since the last look
        muted = await self._page_call("(muted) => window.crystalMeet.mute(muted)", not self._muted, "Could not change the microphone")
        self._muted = bool(muted)
        await self._read_state_from_zoom()   # what Zoom has now, not what was asked
        self._publish_state()
        return self._muted

    async def toggle_camera(self) -> bool:
        self._require_meeting()
        await self._read_state_from_zoom()
        self._camera_on = await self._set_video(not self._camera_on)   # what the button says once Zoom has switched
        self._publish_state()
        return self._camera_on
