"""
Meeting Service for Croom.

High-level service that manages meeting providers and handles
meeting lifecycle.
"""

import asyncio
import logging
from typing import Optional, Dict, Any, List, Callable

from croom.core.config import Config
from croom.core.service import Service
from croom.meeting.providers.base import (
    MeetingProvider,
    MeetingInfo,
    MeetingState,
    detect_platform,
)
from croom.meeting.providers import build_provider, get_all_providers
from croom.meeting.display import TvDisplay

logger = logging.getLogger(__name__)


class MeetingService(Service):
    """
    High-level meeting service.

    Manages meeting providers and provides unified interface
    for joining and controlling meetings across platforms.
    """

    def __init__(self, config: Config, display: Optional[TvDisplay] = None):
        super().__init__("meeting")
        self.config = config
        # The TV: one browser page shared by every provider (spec 2026-10-07 TV, section 4.3).
        self._display = display

        self._providers: Dict[str, MeetingProvider] = {}
        self._active_provider: Optional[MeetingProvider] = None
        self._state_callbacks: List[Callable[[MeetingState], None]] = []

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

    async def join_meeting(
        self,
        meeting_url: str,
        display_name: Optional[str] = None,
        camera_on: Optional[bool] = None,
        mic_on: Optional[bool] = None
    ) -> MeetingInfo:
        """
        Join a meeting.

        Automatically detects the platform from the URL and uses
        the appropriate provider.

        Args:
            meeting_url: Meeting URL or code
            display_name: Name to display (default: room name)
            camera_on: Start with camera (default: config setting)
            mic_on: Start with mic (default: config setting)

        Returns:
            MeetingInfo with connection details
        """
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

        # Setup state callback
        provider.add_state_callback(self._on_state_change)

        # Join meeting
        self._active_provider = provider
        meeting_info = await provider.join_meeting(
            meeting_url,
            display_name=display_name,
            camera_on=camera_on,
            mic_on=mic_on
        )

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
        if self._active_provider:
            return self._active_provider.state
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
