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
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qs, urlparse

from croom.core.config import DEFAULT_ACKNOWLEDGE, DEFAULT_REACTIONS, Config
from croom.meeting.providers.base import (
    DialogPolicy,
    MeetingControlError,
    MeetingInfo,
    MeetingProvider,
    MeetingState,
    controls_from,
)
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
# How the page begins its words when Zoom drops a mute made again inside the second it keeps for one (meeting.js, ignored()).
MUTE_IGNORED = "Zoom ignored mute"
# What the page says of a dialog it was asked to press that is no longer there (meeting.js, answerNow()).
DIALOG_GONE = "no longer on the screen"
# How Playwright says the page was navigated away from while a call into it was in flight: Zoom sends the page to its leaveUrl when a
# meeting ends.
CONTEXT_DESTROYED = "Execution context was destroyed"
# The calls into the page's bridge (meeting.js): the whole reading of the meeting (the first version of the bridge only had readState), a
# control by name, and a button of a dialog by its label.
READ_MEETING = "() => (window.crystalMeet.readMeeting || window.crystalMeet.readState)()"
CONTROL = "([name, value]) => window.crystalMeet.control(name, value)"
ANSWER_DIALOG = "([id, label]) => window.crystalMeet.answerDialog(id, label)"
# The control that has the page open the View menu, read it and close it, picking nothing (meeting.js, readViewMenu()).
READ_VIEW = "layout.read"
PEOPLE_LISTED = 50   # the model lists this many people at most (the count is whole)
CHAT_KEPT = 20       # and keeps this many of the last incoming chat messages


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
    VIDEO_PRESENT_MS = 1500          # a tap: the button must be there, naming a usable camera, within this
    VIDEO_PRESENT_AT_JOIN_MS = 6000  # at join the footer and Zoom's camera list arrive a moment after connected
    VIDEO_SWITCH_MS = 10000   # ... and then this long for Zoom to switch the camera
    CONTROL_TIMEOUT_S = 30    # a control from the table: the page's own bound is 12 s for each call of Zoom's SDK, and it runs them one at a time
    ANSWER_SPACING_S = 1      # the provider presses at most one notice's button in this time, whatever Zoom draws and however often
    LEFT_WAIT_S = 5           # once Zoom has taken the end of the meeting, how long to wait for it to report the meeting over

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
        # The model for the table (spec 2026-10-13, sections 3 to 5): the policy for the dialogs, the reactions offered, and what the provider
        # starts on its own and keeps (answering a dialog, reading the meeting again), so that leaving or shutting down can cancel it.
        self.dialog_policy = DialogPolicy(DEFAULT_ACKNOWLEDGE)
        self.reactions: List[str] = list(DEFAULT_REACTIONS)
        self._answering = asyncio.Lock()
        self._background: Set[asyncio.Task] = set()
        self._reset_meeting()

    @classmethod
    def from_config(cls, config: Config, display=None) -> "ZoomSdkProvider":
        credentials = load_zoom_credentials(config.meeting.zoom_credentials_path)
        provider = cls(credentials, room_name=config.room.name or "Conference Room", display=display)
        provider.dialog_policy = DialogPolicy(config.meeting.dialogs.acknowledge, config.meeting.dialogs.ask_everything)
        provider.reactions = list(config.meeting.controls.reactions)
        return provider

    def _reset_meeting(self) -> None:
        """What one meeting leaves that the next must not find: the page numbers its dialogs from 1 each time it loads."""
        self._reading: Dict[str, Any] = {}          # the page's last whole reading of the meeting
        self._dialogs: List[Dict[str, Any]] = []    # the dialogs waiting for a tap on the table, oldest first
        self._on_screen: Set[int] = set()           # every dialog the page has said is open: the waiting ones, and the notices being answered
        self._answered: Set[int] = set()            # the dialogs this provider has pressed a button of
        self._last_answer = float("-inf")           # when it last pressed a notice's button by itself
        self._view_read = False                     # whether the View menu has been looked at in this meeting

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
        """Stop the meeting watcher, any join still in flight and what the provider started on its own (never the task doing the cancelling)."""
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
        background = [task for task in self._background if task is not current]
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)

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
        self._reset_meeting()
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
            # Once the watcher has taken the events that waited for it (a dialog the client had open when the room came in, say).
            asyncio.get_running_loop().call_soon(self._look_at_the_view_once)
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
        """After connecting: what the page reports is taken into the model, and the host ending the meeting, or the SDK reporting an
        error, returns the room to idle."""
        events = {"state": self._take_state,            # a change Zoom made: the host muting the room, video stopped, a hand raised
                  "dialog": self._on_dialog, "dialog-closed": self._on_dialog_closed, "chat": self._on_chat}
        while True:
            state, detail = await self._events.get()
            if state in events:
                self._take_json(detail, events[state])
                continue
            if state == "left":
                logger.info("Zoom meeting ended")
                self._meeting_over()
                await self._blank_page()
                self._current_meeting = None
                self._watcher = None
                self._set_state(MeetingState.IDLE)
                return
            if state == "error":
                self._meeting_over()
                if self._current_meeting is not None:
                    self._current_meeting.error_message = detail or "Zoom reported an error"
                logger.error(f"Zoom meeting error: {detail}")
                self._watcher = None
                self._set_state(MeetingState.ERROR)
                return

    def _take_json(self, detail: str, handler) -> None:
        """Hand an event's JSON to its handler. An event the provider cannot take is dropped: a fault in one must never end the watcher,
        which is what sees the meeting end."""
        try:
            data = json.loads(detail)
        except ValueError:
            logger.debug(f"Unreadable event from the page: {detail!r}")
            return
        try:
            handler(data)
        except Exception:  # noqa: BLE001 - see above
            logger.warning(f"Could not take an event from the page: {detail!r}", exc_info=True)

    def _meeting_over(self) -> None:
        """The page says nothing of its dialogs when the meeting ends: what the provider kept of them goes with the meeting, and so does
        what it had started on its own (a press that has no page to go to)."""
        self._dialogs.clear()
        self._on_screen.clear()
        if self._current_meeting is not None:
            self._current_meeting.dialog = None
        for task in list(self._background):
            task.cancel()

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
        """The meeting as Zoom has it, taken into the model: mute and camera (the current user's record and the client's own video
        button; meetings often start the room muted or with video off, and the host can change either at any time), the roster, the
        client's buttons. Returns the current user's part of what the page said (empty when it could not say); what Zoom left unsaid
        (null) keeps the last value."""
        try:
            reading = await self._page_call(READ_MEETING, None, "Could not read the meeting state")
        except MeetingControlError as e:
            logger.warning(str(e))
            return {}
        self._take_state(reading)
        return self._me_of(reading) if isinstance(reading, dict) else {}

    @staticmethod
    def _me_of(reading: Dict[str, Any]) -> Dict[str, Any]:
        """The current user's fields of a reading: under `me` in the page's readMeeting(), at the top in the first version of its reading."""
        me = reading.get("me")
        return me if isinstance(me, dict) else reading

    def _take_state(self, reading: Any) -> None:
        """Take what the page read of the meeting, from a read or from the page's "state" event, into the model the table draws from."""
        if not isinstance(reading, dict):
            return
        me = self._me_of(reading)
        if isinstance(me.get("muted"), bool):
            self._muted = me["muted"]
        if isinstance(me.get("videoOn"), bool):
            self._camera_on = me["videoOn"]
        if "participants" in reading:
            self._reading = reading
        self._publish_state()
        self._look_at_the_view_once()

    def _publish_state(self) -> None:
        info = self._current_meeting
        if info is None:
            return
        info.is_muted, info.is_camera_on = self._muted, self._camera_on
        if self._reading:
            # Zoom may not have said whether the room is muted (it lists the room a moment after connecting): the status keeps what it
            # last knew, and the Mute control must say the same.
            known = dict(self._reading, me=dict(self._me_of(self._reading), muted=self._muted))
            info.controls = controls_from(known, self.reactions)
            people = self._reading.get("participants") or []
            count = self._reading.get("count")
            info.people = {"count": count if isinstance(count, int) else len(people), "list": people[:PEOPLE_LISTED]}
            info.recording = bool(self._reading.get("recording"))
            info.locked = bool(self._reading.get("locked"))

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
                if str(e).startswith(MUTE_IGNORED):
                    # The page's own mic-off default has just muted the room, and Zoom's record can lag that mute, so this look found
                    # the room live: Zoom drops a second mute inside the second it keeps for one, and the room is muted all the same.
                    logger.debug(f"The join's own mute was a second one, which Zoom ignores: {e}")
                else:
                    logger.warning(f"The microphone was not muted at join: {e}")
        video = zoom.get("videoOn")
        if isinstance(video, bool) and video != camera_on:
            try:
                await self._set_video(camera_on, at_join=True)
            except MeetingControlError as e:
                logger.warning(f"The camera was not set at join: {e}")
        # The watcher starts only after this read, so the page's "state" events from the join are still waiting for it: a read made now is
        # newer than they are, and taken after it they would put the status back (the room unmuted for a moment after its mic-off default).
        self._forget_state_events()
        await self._read_state_from_zoom()

    def _forget_state_events(self) -> None:
        """Drop the "state" events waiting for the watcher. What a read does not say (a dialog, a chat message, the meeting ending) stays."""
        if self._events is None:
            return
        waiting = []
        while not self._events.empty():
            waiting.append(self._events.get_nowait())
        for event in waiting:
            if event[0] != "state":
                self._events.put_nowait(event)

    async def _set_video(self, on: bool, at_join: bool = False) -> bool:
        """Press the client's video button when Zoom's state differs. The page waits for the button to be there with a
        usable camera (longer at join: the footer and Zoom's camera list arrive a moment after connected), for Zoom to
        enable it and to switch, and says in words what did not happen (no button yet, no usable camera, still
        starting, no switch)."""
        present_ms = self.VIDEO_PRESENT_AT_JOIN_MS if at_join else self.VIDEO_PRESENT_MS
        waits = {"readyMs": self.VIDEO_READY_MS, "switchMs": self.VIDEO_SWITCH_MS, "presentMs": present_ms}
        timeout = (present_ms + self.VIDEO_READY_MS + self.VIDEO_SWITCH_MS) / 1000 + 2   # the page's own waits, and a margin
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

    # ------------------------------------------------------------------
    # What the page tells of the meeting beyond mute and camera: the View menu, dialogs and chat (spec 2026-10-13, sections 3 to 5)
    # ------------------------------------------------------------------

    def _spawn(self, work) -> asyncio.Task:
        """Run `work` (a coroutine) on its own, kept so that leaving the meeting or shutting down cancels it."""
        task = asyncio.ensure_future(work)
        self._background.add(task)
        task.add_done_callback(self._background_done)
        return task

    def _background_done(self, task: asyncio.Task) -> None:
        self._background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning(f"A step the provider took on its own failed: {task.exception()!r}")

    def _look_at_the_view_once(self) -> None:
        """The view and self view can only be read from the View menu, and until it has been opened the page says its defaults (speaker,
        shown). Once the room is in the meeting and the client has drawn its View button, and while no dialog is open (one drawn inside the
        menu's window would add its buttons to the menu), the page is asked to open the menu, read it and close it with nothing picked, so
        the table starts from what the client has. It is asked once for a meeting."""
        if self._view_read or self._state != MeetingState.CONNECTED or self._page is None or self._on_screen:
            return
        if "layout" not in (self._reading.get("buttons") or {}):
            return
        self._view_read = True
        self._spawn(self._read_the_view())

    async def _read_the_view(self) -> None:
        try:
            reading = await self._page_call(CONTROL, [READ_VIEW, None], "Could not read the View menu", timeout=self.CONTROL_TIMEOUT_S)
        except MeetingControlError as e:
            logger.debug(f"The View menu was not read: {e}")
            return
        self._take_state(reading)

    @staticmethod
    def _dialog_id(data: Any) -> Optional[int]:
        try:
            return int(data["id"])
        except (KeyError, TypeError, ValueError):
            return None

    def _on_dialog(self, dialog: Dict[str, Any]) -> None:
        """A dialog Zoom drew over the meeting (the page says each one once). By the policy a notice is pressed for the room, and its
        words are kept for the table; any other waits for a tap."""
        info = self._current_meeting
        dialog_id = self._dialog_id(dialog)
        if info is None or dialog_id is None or dialog_id in self._on_screen or dialog_id in self._answered:
            return
        logger.info(f"Zoom dialog: {dialog.get('title')!r} {dialog.get('text')!r} buttons {dialog.get('buttons')}")
        self._on_screen.add(dialog_id)
        if self.dialog_policy.decide(dialog) == "acknowledge":
            self._answered.add(dialog_id)
            self._spawn(self._acknowledge(dialog_id, self.dialog_policy.answer(dialog)))
            sentence = self.dialog_policy.disclosure(dialog)
            if sentence and sentence not in info.notice:
                info.notice = f"{info.notice} {sentence}".strip()
            return
        self._dialogs.append({"id": dialog_id, "title": str(dialog.get("title") or ""), "text": str(dialog.get("text") or ""),
                              "buttons": [str(label) for label in dialog.get("buttons") or []]})
        info.dialog = dict(self._dialogs[0])
        logger.info("The dialog waits on the table")

    def _on_dialog_closed(self, data: Dict[str, Any]) -> None:
        dialog_id = self._dialog_id(data)
        if dialog_id is None:
            return
        self._forget_dialog(dialog_id)
        self._spawn(self._read_state_from_zoom())   # the host may have muted the room with that dialog

    def _forget_dialog(self, dialog_id: int) -> None:
        """The dialog is off the screen: the table shows the oldest of those still waiting."""
        self._on_screen.discard(dialog_id)
        self._dialogs = [dialog for dialog in self._dialogs if dialog["id"] != dialog_id]
        if self._current_meeting is not None:
            self._current_meeting.dialog = dict(self._dialogs[0]) if self._dialogs else None

    async def _acknowledge(self, dialog_id: int, label: str) -> None:
        """Press a notice's button for the room, at most one dialog in ANSWER_SPACING_S whatever Zoom draws. A press the page refuses is
        a line for the debug level and nothing more: the notice's words are on the table either way."""
        async with self._answering:
            wait = self._last_answer + self.ANSWER_SPACING_S - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_answer = time.monotonic()
            try:
                await self._press_dialog(dialog_id, label)
                logger.info(f"Acknowledged Zoom's dialog with {label!r}")
            except MeetingControlError as e:
                logger.debug(f"Could not press {label!r} on Zoom's dialog: {e}")

    async def _press_dialog(self, dialog_id: int, label: str) -> None:
        """Press the button the label names on a dialog of the page. The page's words when it will not (the dialog's button is not ready,
        it did not close); a dialog that is gone after this provider answered it is the answer done, as a second tap finds it."""
        try:
            await self._page_call(ANSWER_DIALOG, [dialog_id, label], "Could not answer the dialog", timeout=self.CONTROL_TIMEOUT_S)
        except MeetingControlError as e:
            if DIALOG_GONE in str(e) and dialog_id in self._answered:
                return
            raise
        self._answered.add(dialog_id)

    async def _answer_from_table(self, value: Any) -> Dict[str, Any]:
        """A tap on one of the waiting dialog's buttons: the page presses it, and the dialog is off the table."""
        try:
            dialog_id, label = int(value["id"]), str(value["button"])
        except (KeyError, TypeError, ValueError):
            raise MeetingControlError("Say which dialog and which button") from None
        await self._press_dialog(dialog_id, label)
        self._forget_dialog(dialog_id)
        info = self._current_meeting
        return {"dialog": info.dialog if info is not None else None}

    def _on_chat(self, message: Dict[str, Any]) -> None:
        info = self._current_meeting
        if info is None or not isinstance(message, dict):
            return
        kept = {"from": str(message.get("from", "")), "text": str(message.get("text", "")), "at": str(message.get("at", ""))}
        info.chat["messages"] = (info.chat["messages"] + [kept])[-CHAT_KEPT:]
        info.chat["unread"] += 1

    # ------------------------------------------------------------------
    # Controls from the table (spec 2026-10-13, section 3)
    # ------------------------------------------------------------------

    async def control(self, name: str, value: Any = None) -> Dict[str, Any]:
        """A control from the table, carried out through the page's bridge (see MeetingProvider.control). A refusal is a
        MeetingControlError in the bridge's words, unchanged. The answer is the control model as it is afterwards."""
        self._require_meeting()
        info = self._current_meeting
        if name == "chat.seen":
            info.chat["unread"] = 0
            return {"chat": info.chat}
        if name == "dialog":
            return await self._answer_from_table(value)
        if name == "leave":
            await self.leave_meeting()
            return {}
        if name == "host.end":
            return await self._end_meeting_for_everyone()
        if name == "mute":        # these two have paths of their own: the join's mic-off default and the reconnect rules are made for them
            await self.toggle_mute()
        elif name == "camera":
            await self.toggle_camera()
            await self._read_state_from_zoom()   # the model says the camera as Zoom has it now
        else:
            self._take_state(await self._page_call(CONTROL, [name, value], f"Could not carry out {name}", timeout=self.CONTROL_TIMEOUT_S))
        return info.controls

    async def _end_meeting_for_everyone(self) -> Dict[str, Any]:
        """The host's End for everyone. It is done when Zoom answers the call, when the navigation that follows the end of a meeting
        cuts the call off, or when the meeting is already over by the time the call fails: none of those is a failure, and the control
        waits (a while, at most) for the room to be idle. Zoom's own refusal is one, in its words."""
        watcher = self._watcher
        try:
            await self._page_call(CONTROL, ["host.end", None], "Could not end the meeting", timeout=self.CONTROL_TIMEOUT_S)
        except MeetingControlError as e:
            if CONTEXT_DESTROYED not in str(e) and self._state == MeetingState.CONNECTED:
                raise
        if watcher is not None and not watcher.done():
            await asyncio.wait({watcher}, timeout=self.LEFT_WAIT_S)
        return {}

