"""
Meeting Service for Croom.

High-level service that manages meeting providers and handles
meeting lifecycle.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable, Awaitable

from croom.core.config import Config
from croom.core.service import Service
from croom.meeting.providers.base import (
    MeetingControlError,
    MeetingProvider,
    MeetingInfo,
    MeetingState,
    detect_platform,
)
from croom.meeting.providers import build_provider, get_all_providers
from croom.meeting.display import TvDisplay
from croom.meeting.signin_session import (
    CANCELLED, DONE, LEAVE_FIRST, SET_UP_FIRST, SIGN_IN_FIRST, SignInError, SignInSession, google_account,
)

logger = logging.getLogger(__name__)

# A meeting that has the TV's browser: Settings' sign-in to Google waits for it to end
IN_PROGRESS = (MeetingState.JOINING, MeetingState.IN_LOBBY, MeetingState.CONNECTED, MeetingState.LEAVING)


class MeetingService(Service):
    """
    High-level meeting service.

    Manages meeting providers and provides unified interface
    for joining and controlling meetings across platforms.
    """

    BEFORE_JOIN_TIMEOUT_S = 30.0   # a before-join hook that takes longer than this is given up on, and the join goes on

    def __init__(self, config: Config, display: Optional[TvDisplay] = None, sign_in: Optional[SignInSession] = None):
        super().__init__("meeting")
        self.config = config
        # The TV: one browser page shared by every provider (spec 2026-10-07 TV, section 4.3).
        self._display = display
        # Settings' Sign in to Google on the TV (spec 2026-10-29, section 3): made at the first sign-in, on the display's profile
        self._sign_in = sign_in
        self._courtesies: set = set()   # the before-join hooks run for a sign-in (the TV is woken), held until they are done

        self._providers: Dict[str, MeetingProvider] = {}
        self._active_provider: Optional[MeetingProvider] = None
        self._state_callbacks: List[Callable[[MeetingState], None]] = []
        # What the room does before a join (the TV is woken here), how many joins are waiting on it just now, and how many
        # have gone on to ask their provider to join
        self._before_join: List[Callable[[], Awaitable[Any]]] = []
        self._preparing = 0
        self._starting = 0

    def pointer_in_use(self) -> None:
        """The table's touchpad drove the room Pi's mouse (or typed on its keyboard): the TV shows its pointer for a moment
        (TvDisplay.pointer_in_use), and a sign-in to Google that is on starts its 15 minutes again."""
        if self._sign_in is not None:
            self._sign_in.touch()
        if self._display is not None:
            self._display.pointer_in_use()

    # ------------------------------------------------------------------
    # Sign in to Google on the TV (spec 2026-10-29, sections 3 and 8)
    # ------------------------------------------------------------------

    @property
    def signing_in(self) -> bool:
        """Whether a sign-in to Google has the TV: a join is refused meanwhile."""
        return self._sign_in is not None and self._sign_in.active

    def google_profile_dir(self) -> Optional[Path]:
        """The room's Google profile folder, as the TV's browser uses it (the config's, until the room restarts); None when there is none."""
        if self._display is not None:
            return self._display.profile_dir
        return Path(self.config.meeting.google_profile_dir) if self.config.meeting.google_profile_dir else None

    def _session(self, profile: Path) -> SignInSession:
        if self._sign_in is None:
            self._sign_in = SignInSession(self._display, profile)
        return self._sign_in

    async def begin_sign_in(self) -> Dict[str, Any]:
        """Close the TV's meeting browser and open Google's sign-in page there, on the room's profile. The answer is sign_in_state(). A
        sign-in that is on already is left as it is. SignInError, in words: in a meeting ("Leave the meeting first"), a room with no
        Google profile folder ("Set up Google Meet first"), or a browser that could not be opened (the TV has its browser back)."""
        if self.signing_in:
            return self.sign_in_state()
        if self.state in IN_PROGRESS:
            raise SignInError(LEAVE_FIRST)
        profile = self.google_profile_dir()
        if profile is None:
            raise SignInError(SET_UP_FIRST)
        session = self._session(profile)
        self._wake_the_tv()            # (tasks: they run once begin() first waits, and it has said it is on by then)
        await session.begin()          # (on from its first line, before it awaits anything: a join from here on is refused)
        return self.sign_in_state()

    async def end_sign_in(self, done: bool) -> Optional[str]:
        """Done (`done`): the address the room is signed in as now, or None when Google kept no sign-in; Cancel: None, and nothing
        changed. The TV has its browser back either way. SignInError when no sign-in is on."""
        if self._sign_in is None:
            raise SignInError("The sign-in has ended already")
        return await self._sign_in.end(DONE if done else CANCELLED)

    def sign_in_state(self) -> Dict[str, Any]:
        """For Settings' Meetings page: {"configured", "profile_dir", "active", "idle_left_s" (while on), "signed_in", "address"
        (while off: what the profile holds now), "last" (how the last sign-in ended: {"outcome", "words", "at", "address"})}."""
        profile = self.google_profile_dir()
        answer: Dict[str, Any] = {"configured": profile is not None, "profile_dir": str(profile) if profile else None,
                                  "active": False, "last": None, "signed_in": False, "address": None}
        if self._sign_in is not None:
            answer.update(self._sign_in.state())
        if profile is not None and not answer["active"]:
            answer.update(google_account(profile))
        return answer

    def _wake_the_tv(self) -> None:
        """The before-join hooks, as a courtesy to the sign-in (the TV is woken and put on the room Pi's input): not waited for."""
        async def courtesy(hook):
            name = getattr(hook, "__qualname__", None) or repr(hook)
            try:
                await asyncio.wait_for(hook(), self.BEFORE_JOIN_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the sign-in goes on without the TV's answer
                logger.warning(f"Before-join hook {name} failed for the sign-in ({type(e).__name__}: {e})")
        for hook in self._before_join:
            task = asyncio.ensure_future(courtesy(hook))
            self._courtesies.add(task)
            task.add_done_callback(self._courtesies.discard)

    async def start(self) -> None:
        """Start the TV display and the configured providers on it."""
        if self._display is None:
            self._display = TvDisplay.from_config(self.config)
        built = {}
        for platform in self.config.meeting.platforms:
            try:
                provider = build_provider(platform, self.config, self._display)
            except Exception as e:  # noqa: BLE001 - a bad credentials file must not stop the other platforms
                logger.error(f"Failed to build {platform} provider: {e}")
                continue
            if provider is not None:
                built[platform] = provider
        if not built:
            logger.warning("No meeting providers available")
            return
        try:
            await self._display.start()
        except Exception as e:  # noqa: BLE001 - without a browser there is nothing to show
            logger.error(f"The TV display could not start: {e}")
            return
        for platform, provider in built.items():
            try:
                await provider.initialize()
                self._providers[platform] = provider
                logger.info(f"Initialized meeting provider: {platform}")
            except Exception as e:
                logger.error(f"Failed to initialize {platform} provider: {e}")

        logger.info(f"Meeting service started with {len(self._providers)} providers")

    async def stop(self) -> None:
        """Stop meeting service."""
        # A sign-in to Google that is on ends (nothing changes); its browser goes, and the TV's browser stops below with the rest
        for task in list(self._courtesies):
            task.cancel()
        if self._sign_in is not None:
            try:
                await self._sign_in.close()
            except Exception as e:  # noqa: BLE001 - the agent stops whatever
                logger.error(f"The sign-in to Google did not end cleanly: {e}")

        # Leave any active meeting
        if self._active_provider and self._active_provider.state == MeetingState.CONNECTED:
            await self.leave_meeting()

        # Shutdown all providers
        for name, provider in self._providers.items():
            try:
                await provider.shutdown()
                logger.info(f"Shutdown provider: {name}")
            except Exception as e:
                logger.error(f"Error shutting down {name}: {e}")

        self._providers.clear()
        self._active_provider = None

        if self._display is not None:
            await self._display.stop()

        logger.info("Meeting service stopped")

    def add_state_callback(self, callback: Callable[[MeetingState], None]) -> None:
        """Add callback for meeting state changes."""
        self._state_callbacks.append(callback)

    def _on_state_change(self, state: MeetingState) -> None:
        """Handle state changes from provider."""
        for callback in self._state_callbacks:
            try:
                callback(state)
            except Exception:
                pass

    def add_before_join(self, hook: Callable[[], Awaitable[Any]]) -> None:
        """Add something the room does before every join: `await hook()` runs first, and the join waits for it (the TV
        service wakes the TV here, for up to fifteen seconds). A hook is a courtesy to the join, never a condition of it:
        one that raises is logged, and so is one that answers False (it could not do what it set out to) and one that
        takes longer than BEFORE_JOIN_TIMEOUT_S, and the join goes on."""
        self._before_join.append(hook)

    async def _run_before_join(self) -> None:
        self._preparing += 1
        try:
            for hook in self._before_join:
                name = getattr(hook, "__qualname__", None) or repr(hook)
                try:
                    if await asyncio.wait_for(hook(), self.BEFORE_JOIN_TIMEOUT_S) is False:
                        logger.warning(f"Before-join hook {name} did not get what it wanted; joining anyway")
                except asyncio.TimeoutError:
                    logger.warning(f"Before-join hook {name} did not finish in {self.BEFORE_JOIN_TIMEOUT_S:g} s; joining anyway")
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise                                # the join itself was cancelled (a Leave): that is not a hook failing
                    # a cancellation that came out of the hook (the TV library's, from a client cancelled earlier): it failed
                    logger.warning(f"Before-join hook {name} was cancelled; joining anyway")
                except Exception as e:  # noqa: BLE001 - a failing courtesy must not cost the room its meeting
                    logger.warning(f"Before-join hook {name} failed ({e}); joining anyway")
        finally:
            self._preparing -= 1

    async def join_meeting(
        self,
        meeting_url: str,
        display_name: Optional[str] = None,
        camera_on: Optional[bool] = None,
        mic_on: Optional[bool] = None,
        passcode: Optional[str] = None
    ) -> MeetingInfo:
        """
        Join a meeting.

        Automatically detects the platform from the URL and uses
        the appropriate provider. Once the link is known to be one this
        room can join, the before-join hooks run (see add_before_join).

        Args:
            meeting_url: Meeting URL or code
            display_name: Name to display (default: room name)
            camera_on: Start with camera (default: config setting)
            mic_on: Start with mic (default: config setting)
            passcode: The meeting's passcode (Zoom uses it, over the link's own pwd)

        Returns:
            MeetingInfo with connection details
        """
        if self.signing_in:
            raise RuntimeError(SIGN_IN_FIRST)

        # Apply defaults from config
        if display_name is None:
            display_name = self.config.room.name or "Conference Room"
        if camera_on is None:
            camera_on = self.config.meeting.camera_default_on
        if mic_on is None:
            mic_on = self.config.meeting.mic_default_on

        # Detect platform
        platform = detect_platform(meeting_url)

        if not platform:
            # Try each provider
            for name, provider in self._providers.items():
                if provider.can_handle_url(meeting_url):
                    platform = name
                    break

        if not platform:
            raise ValueError(f"Cannot determine meeting platform for: {meeting_url}")

        # Get provider
        provider = self._providers.get(platform)
        if not provider:
            raise RuntimeError(f"Provider not available: {platform}")

        # Leave any existing meeting
        if self._active_provider and self._active_provider.state == MeetingState.CONNECTED:
            await self.leave_meeting()

        # What the room does first (the TV is woken, and the join waits for it): after the link is known to be one this
        # room can join, so that a link that cannot be joined is not made to wait for a TV
        await self._run_before_join()

        # Setup state callback
        provider.add_state_callback(self._on_state_change)

        # Join meeting
        self._active_provider = provider
        self._starting += 1             # (see state: a provider can take a while before it says that it is joining)
        try:
            meeting_info = await provider.join_meeting(
                meeting_url,
                display_name=display_name,
                camera_on=camera_on,
                mic_on=mic_on,
                passcode=passcode
            )
        finally:
            self._starting -= 1

        return meeting_info

    async def leave_meeting(self) -> None:
        """Leave the current meeting."""
        if self._active_provider:
            await self._active_provider.leave_meeting()
            self._active_provider = None

    async def toggle_camera(self) -> bool:
        """
        Toggle camera on/off.

        Returns:
            New camera state (True = on)
        """
        if not self._active_provider:
            return False
        return await self._active_provider.toggle_camera()

    async def toggle_mute(self) -> bool:
        """
        Toggle microphone mute.

        Returns:
            New mute state (True = muted)
        """
        if not self._active_provider:
            return True
        return await self._active_provider.toggle_mute()

    async def control(self, name: str, value: Any = None) -> Dict[str, Any]:
        """Carry out a control from the table on the provider of the meeting (spec 2026-10-13, section 3); words for the table in a
        MeetingControlError when the platform will not or cannot just now."""
        if not self._active_provider:
            raise MeetingControlError("Not in a meeting")
        return await self._active_provider.control(name, value)

    async def diagnostics(self) -> Dict[str, Any]:
        """What the active provider's page sees, for pinning the platform's wording; empty without a meeting."""
        if not self._active_provider:
            return {}
        reader = getattr(self._active_provider, "diagnostics", None)
        return await reader() if reader is not None else {}

    async def set_camera(self, on: bool) -> bool:
        """Set camera state."""
        if not self._active_provider:
            return False
        return await self._active_provider.set_camera(on)

    async def set_mute(self, muted: bool) -> bool:
        """Set mute state."""
        if not self._active_provider:
            return True
        return await self._active_provider.set_mute(muted)

    @property
    def state(self) -> MeetingState:
        """Get current meeting state."""
        if self._preparing:
            # The join has begun and is waiting on the room (for the TV to come on). The room page draws from this: it
            # must not show an idle room, and let the join be tapped again, for the fifteen seconds that can take.
            return MeetingState.JOINING
        if self._active_provider:
            state = self._active_provider.state
            if self._starting and state == MeetingState.IDLE:
                # The provider has been asked to join and has not said so itself yet (the Zoom provider waits for the TV's
                # page first): the same, until it does
                return MeetingState.JOINING
            return state
        return MeetingState.IDLE

    @property
    def current_meeting(self) -> Optional[MeetingInfo]:
        """Get current meeting info."""
        if self._active_provider:
            return self._active_provider.current_meeting
        return None

    @property
    def is_in_meeting(self) -> bool:
        """Check if currently in a meeting."""
        return self.state == MeetingState.CONNECTED

    def get_available_platforms(self) -> List[str]:
        """Get list of available platforms."""
        return list(self._providers.keys())

    def get_status(self) -> Dict[str, Any]:
        """Get meeting service status."""
        return {
            "running": self.is_running,
            "state": self.state.value,
            "meeting": self.current_meeting.to_dict() if self.current_meeting else None,
            "available_platforms": self.get_available_platforms(),
        }
