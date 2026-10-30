"""
Croom Agent - Main coordinator for the Croom system.

The agent orchestrates all services and handles the main event loop.
"""

import asyncio
import logging
import signal
import sys
from typing import Optional, Dict, Any

from croom.core.config import Config, load_config
from croom.core.logs import setup_logging
from croom.core.service import ServiceManager, Service
from croom.platform.detector import PlatformDetector, PlatformInfo
from croom.platform.capabilities import CapabilityDetector, Capabilities

logger = logging.getLogger(__name__)


class CroomAgent:
    """
    Main Croom agent that coordinates all system components.

    The agent is responsible for:
    - Loading configuration
    - Detecting platform capabilities
    - Managing service lifecycle
    - Handling system signals
    - Coordinating between services
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize the Croom agent.

        Args:
            config_path: Optional path to configuration file.
        """
        self.config: Config = load_config(config_path)
        self.platform_info: PlatformInfo = PlatformDetector.detect()
        self.capabilities: Capabilities = CapabilityDetector.detect()
        self.service_manager = ServiceManager()

        self._running = False
        self._main_task: Optional[asyncio.Task] = None
        self._stop_task: Optional[asyncio.Task] = None

        logger.info(f"Croom Agent initialized on {self.platform_info.device.value}")
        logger.info(f"AI accelerators: {self.platform_info.ai_accelerators}")

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(self._handle_signal(s))
            )

    async def _handle_signal(self, sig: signal.Signals) -> None:
        """Handle shutdown signal."""
        logger.info(f"Received signal {sig.name}, initiating shutdown...")
        await self.stop()

    def _initialize_services(self) -> None:
        """Initialize and register all services based on configuration."""
        # Import services dynamically to avoid circular imports
        # and allow optional dependencies

        # AI Service (if enabled)
        if self.config.ai.enabled and not self.config.ai.privacy_mode:
            try:
                from croom.ai.service import AIService
                ai_service = AIService(self.config, self.capabilities)
                self.service_manager.register(ai_service)
                logger.info("AI service registered")
            except ImportError as e:
                logger.warning(f"AI service not available: {e}")

        # The room's TV (spec 2026-10-13 TV remote and touchpad): kept on, and woken before a meeting is joined. Registered
        # ahead of audio and video, and listed among the meeting's dependencies below, so that it starts before the meeting
        # (with the AI service on, audio and video are put right after it in the start order, and still come first). Its
        # start never holds the others for long: it waits half a second for the first look at the TV, and no more.
        tv_service = None
        # A quiet, unconfigured service lets Find TVs configure and pair at runtime.
        # from_config also restores a TV selected on the table before this boot.
        try:
            from croom.tv.service import TvService
            tv_service = TvService.from_config(self.config)
            self.service_manager.register(tv_service)
            logger.info("TV service registered")
        except ImportError as e:
            logger.warning(f"TV service not available: {e}")

        # Audio Service
        try:
            from croom.audio.service import AudioService
            audio_service = AudioService.from_config(self.config)
            self.service_manager.register(audio_service, dependencies=["ai"] if self.config.ai.enabled else None)
            logger.info("Audio service registered")
        except ImportError as e:
            logger.warning(f"Audio service not available: {e}")

        # Video Service
        try:
            from croom.video.service import VideoService
            video_service = VideoService.from_config(self.config)
            self.service_manager.register(video_service, dependencies=["ai"] if self.config.ai.enabled else None)
            logger.info("Video service registered")
        except ImportError as e:
            logger.warning(f"Video service not available: {e}")

        # Display Service
        try:
            from croom.display.service import DisplayService
            display_service = DisplayService.from_config(self.config)
            self.service_manager.register(display_service)
            logger.info("Display service registered")
        except ImportError as e:
            logger.warning(f"Display service not available: {e}")

        # Meeting Service
        try:
            from croom.meeting.service import MeetingService
            meeting_service = MeetingService(self.config)
            if tv_service is not None:
                meeting_service.add_before_join(tv_service.before_join)   # a join waits for the TV, then goes on without it
            self.service_manager.register(
                meeting_service, dependencies=["audio", "video"] + (["tv"] if tv_service is not None else [])
            )
            logger.info("Meeting service registered")
        except ImportError as e:
            logger.warning(f"Meeting service not available: {e}")

        # Calendar Service
        try:
            from croom.calendar.service import CalendarService
            calendar_service = CalendarService.from_config(self.config)
            self.service_manager.register(calendar_service)
            logger.info("Calendar service registered")
        except ImportError as e:
            logger.warning(f"Calendar service not available: {e}")

        # Dashboard Connection Service
        if self.config.dashboard.enabled and self.config.dashboard.url:
            try:
                from croom.dashboard.client import DashboardClient
                dashboard_client = DashboardClient.from_config(
                    self.config, self.platform_info, self.capabilities
                )
                self.service_manager.register(dashboard_client)
                logger.info("Dashboard client registered")
            except ImportError as e:
                logger.warning(f"Dashboard client not available: {e}")

        # Room devices: the speaker's volume, the microphone's level and the camera's framing (spec 2026-10-08, 2026-10-29)
        settings_store = None
        try:
            from croom.control.settings import SettingsStore
            from croom.devices.service import DevicesService
            settings_store = SettingsStore(self.config.resolve_data_dir() / "control-settings.json")
            self.service_manager.register(DevicesService.from_config(
                self.config, settings_store, meeting=self.service_manager.get_service("meeting")))
            logger.info("Room devices registered")
        except ImportError as e:
            logger.warning(f"Room devices not available: {e}")

        # Laptop mode (spec 2026-10-27): the MeetUp lent to a laptop at the table through the USB switch
        laptop_service = None
        if self.config.laptop.enabled:
            try:
                from croom.laptop.service import LaptopService
                laptop_service = LaptopService.from_config(
                    self.config,
                    calendar=self.service_manager.get_service("calendar"),
                    meeting=self.service_manager.get_service("meeting"),
                    devices=self.service_manager.get_service("devices"),
                    tv=tv_service,
                )
                self.service_manager.register(
                    laptop_service, dependencies=["meeting", "calendar", "devices"] + (["tv"] if tv_service is not None else [])
                )
                logger.info("Laptop mode registered")
            except ImportError as e:
                logger.warning(f"Laptop mode not available: {e}")

        # Room control page (local web UI for the room), with the door sign's football scoreboard it shows
        if self.config.control.enabled:
            try:
                from croom.control.service import ControlService
                scores_service = self._register_scores(settings_store)
                control_service = ControlService.from_config(
                    self.config,
                    meeting=self.service_manager.get_service("meeting"),
                    calendar=self.service_manager.get_service("calendar"),
                    devices=self.service_manager.get_service("devices"),
                    store=settings_store,
                    tv=tv_service,   # the room page's TV (None when the room has none enabled): its status and its controls
                    scores=scores_service,
                    laptop=laptop_service,
                )
                dependencies = (
                    ["meeting", "calendar", "devices"]
                    + (["tv"] if tv_service is not None else [])
                    + (["scores"] if scores_service is not None else [])
                    + (["laptop"] if laptop_service is not None else [])
                )
                self.service_manager.register(control_service, dependencies=dependencies)
                logger.info("Room control page registered")
            except ImportError as e:
                logger.warning(f"Room control page not available: {e}")

    def _register_scores(self, settings_store) -> Optional[Service]:
        """Register the door sign's football scoreboard (spec 2026-10-13, section 2.5) and return it; None when it cannot be had.

        It reads the schools the room has turned on from the control page's own settings store, each time they are needed and through
        the same check the page loads them with, so that a switch turned on from the table is picked up by the service's next look, and
        the page and the scoreboard cannot disagree. Until a school is on it reads nothing from ESPN.
        """
        if settings_store is None:
            logger.warning("Football scores not available: the room has no settings store, so the switches for the schools would do nothing")
            return None
        try:
            from croom.control.service import read_setting
            from croom.scores import espn
            from croom.scores.service import ScoresService
        except ImportError as e:
            logger.warning(f"Football scores not available: {e}")
            return None
        scores_service = ScoresService(espn.fetch, lambda: read_setting(settings_store, "sign_scores"))
        self.service_manager.register(scores_service)
        logger.info("Football scores registered")
        return scores_service

    async def start(self) -> None:
        """Start the Croom agent and all services."""
        if self._running:
            logger.warning("Agent already running")
            return

        logger.info("Starting Croom Agent...")
        self._running = True

        try:
            # Setup signal handlers
            self._setup_signal_handlers()

            # Initialize services
            self._initialize_services()

            # Start all services
            success = await self.service_manager.start_all()
            if not success:
                logger.error("Failed to start all services")
                self._running = False
                return

            logger.info("Croom Agent started successfully")

            # Wait for shutdown
            await self.service_manager.wait_for_shutdown()

        except Exception as e:
            logger.error(f"Agent error: {e}")
            raise
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stop the Croom agent and all services, once, however many callers ask.

        A signal's stop and start()'s finally both get here, usually together: stop_all sets the shutdown event, which
        wakes start(). The first call starts the one stop as a task and every call waits for that task, so start()
        (and with it the process) does not end while services are still stopping; returning early would let the
        event loop cancel the stop half way, leaving the services registered before the one being stopped (the
        devices service and its camera's motors among them) running when the process dies.
        """
        if self._stop_task is None:
            if not self._running:
                return  # never started: nothing to stop
            self._stop_task = asyncio.create_task(self._stop_everything())
        await self._stop_task

    async def _stop_everything(self) -> None:
        logger.info("Stopping Croom Agent...")
        self._running = False

        # Stop all services
        await self.service_manager.stop_all()

        logger.info("Croom Agent stopped")

    def get_status(self) -> Dict[str, Any]:
        """
        Get current agent status.

        Returns:
            Dictionary containing agent and service status.
        """
        return {
            "running": self._running,
            "platform": {
                "device": self.platform_info.device.value,
                "os": self.platform_info.os_name,
                "arch": self.platform_info.arch,
                "ai_accelerators": self.platform_info.ai_accelerators,
            },
            "capabilities": self.capabilities.to_dict(),
            "services": {
                name: status.__dict__
                for name, status in self.service_manager.get_status().items()
            },
            "config": {
                "room_name": self.config.room.name,
                "ai_enabled": self.config.ai.enabled,
                "platforms": self.config.meeting.platforms,
            }
        }

    def get_capabilities(self) -> Capabilities:
        """Get detected platform capabilities."""
        return self.capabilities

    def get_platform_info(self) -> PlatformInfo:
        """Get platform information."""
        return self.platform_info


async def run_agent(config_path: Optional[str] = None) -> None:
    """
    Run the Croom agent.

    Args:
        config_path: Optional path to configuration file.
    """
    agent = CroomAgent(config_path)
    await agent.start()


def main() -> None:
    """Main entry point for Croom agent."""
    import argparse

    parser = argparse.ArgumentParser(description="Croom Agent")
    parser.add_argument(
        "-c", "--config",
        help="Path to configuration file",
        default=None
    )
    parser.add_argument(
        "-v", "--verbose",
        help="Enable verbose logging",
        action="store_true"
    )
    parser.add_argument(
        "--debug",
        help="Enable debug logging",
        action="store_true"
    )
    parser.add_argument(
        "--check-calendar",
        help="Check the room's Google Calendar setup, print the next bookings and exit",
        action="store_true"
    )
    parser.add_argument(
        "--check-zoom",
        help="Check the room's Zoom Meeting SDK credentials and exit",
        action="store_true"
    )
    parser.add_argument(
        "--check-tv",
        help="Check that the room's LG TV answers (or, with no TV configured, list the LG TVs on the network) and exit",
        action="store_true"
    )
    parser.add_argument(
        "--check-laptop",
        help="Check the laptop switch's pin and where the MeetUp is, and exit (stop croom first: it holds the pin)",
        action="store_true"
    )
    parser.add_argument(
        "--switch",
        help="With --check-laptop: press the switch once and report where the MeetUp went",
        action="store_true"
    )

    parser.add_argument(
        "--check-meet",
        metavar="URL",
        help="Open a Google Meet link the way the room does, report what Meet shows, then exit",
    )
    parser.add_argument(
        "--profile",
        metavar="DIR",
        help="With --check-meet: use this persistent browser profile (one signed in to a Google account) instead of a guest session",
    )
    parser.add_argument(
        "--sign-in-meet",
        action="store_true",
        help="Open the room's browser on its screen to sign in to Google once (needs DISPLAY and meeting.google_profile_dir), then exit",
    )
    args = parser.parse_args()

    # Setup logging
    setup_logging(args.verbose, args.debug)

    if args.check_calendar:
        from croom.calendar.check import check_calendar
        raise SystemExit(asyncio.run(check_calendar(load_config(args.config))))

    if args.check_zoom:
        from croom.meeting.zoom_check import check_zoom
        raise SystemExit(asyncio.run(check_zoom(load_config(args.config))))

    if args.check_tv:
        from croom.tv.check import check_tv
        # It prints names that a TV, or a person's directory, chose: one that this terminal cannot encode is shown as a ? and does
        # not end the check in a traceback
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
        raise SystemExit(asyncio.run(check_tv(load_config(args.config))))

    if args.check_laptop:
        from croom.laptop.check import check_laptop
        raise SystemExit(asyncio.run(check_laptop(load_config(args.config), move=args.switch)))

    if args.check_meet:
        import tempfile
        from pathlib import Path
        from croom.meeting.meet_check import check_meet, resolve_profile
        profile = resolve_profile(args.profile, load_config(args.config).meeting.google_profile_dir)
        raise SystemExit(asyncio.run(check_meet(args.check_meet, screenshot=Path(tempfile.gettempdir()) / "croom-meet-check.png",
                                                profile=profile)))

    if args.sign_in_meet:
        import os
        from pathlib import Path
        from croom.meeting.meet_signin import sign_in_meet
        profile = load_config(args.config).meeting.google_profile_dir
        if not profile:
            print("Set meeting.google_profile_dir in the config first; the room configs use /var/lib/croom/meet-profile", file=sys.stderr)
            raise SystemExit(1)
        raise SystemExit(sign_in_meet(Path(profile), display=os.environ.get("DISPLAY")))

    # Run agent
    try:
        asyncio.run(run_agent(args.config))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Agent failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
