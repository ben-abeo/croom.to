"""
Room control page and API (spec docs/superpowers/specs/2026-09-24-room-control-page-design.md).

Serves the room page and a small JSON API on the local network and drives the
meeting and calendar services on behalf of whoever is in the room.
"""

import asyncio
import logging
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from aiohttp import web

from croom.core.config import Config
from croom.core.service import Service
from croom.meeting.providers.base import MeetingState

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 4096
ZOOM_MEETING_ID = re.compile(r"^\d{9,11}$")
IN_PROGRESS_STATES = {"joining", "in_lobby", "connected", "leaving"}
# Hosts (and their subdomains) a join link may point the room's browser at.
PLATFORM_HOSTS = {
    "zoom": ("zoom.us", "zoomgov.com"),
    "google_meet": ("meet.google.com", "g.co"),
    "teams": ("teams.microsoft.com", "teams.live.com"),
    "webex": ("webex.com",),
}
# The only calendar fields the page needs; the rest stays off the network.
EVENT_FIELDS = ("id", "title", "start_time", "end_time", "meeting_platform")


def _register_font_types() -> None:
    """Serve the bundled fonts with their real media type (aiohttp's table lacks woff2)."""
    mimetypes.add_type("font/woff2", ".woff2")
    try:
        from aiohttp.web_fileresponse import CONTENT_TYPES
        CONTENT_TYPES.add_type("font/woff2", ".woff2")
    except (ImportError, AttributeError):  # pragma: no cover - older or newer aiohttp layouts
        pass


def normalize_link(value: str) -> str:
    """Trim, turn a bare Zoom meeting id into a link, and add https:// when no scheme is given."""
    url = value.strip()
    if ZOOM_MEETING_ID.match(url):
        return f"https://zoom.us/j/{url}"
    if url and "://" not in url:
        return "https://" + url
    return url


class ControlService(Service):
    """
    Room control service ("control" under the ServiceManager).

    Args:
        config: dict with host, port, room_name, room_location, static_dir.
        meeting: the MeetingService instance, or None.
        calendar: the CalendarService instance, or None.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None, meeting=None, calendar=None):
        super().__init__("control")
        self.config = config or {}
        self._host = str(self.config.get("host", "0.0.0.0"))
        self._port = int(self.config.get("port", 8080))
        self._room_name = self.config.get("room_name", "Conference Room")
        self._room_location = self.config.get("room_location", "")
        self._static_dir = Path(self.config.get("static_dir", STATIC_DIR))
        self._meeting = meeting
        self._calendar = calendar

        self._runner: Optional[web.AppRunner] = None
        self._task: Optional[asyncio.Task] = None
        self._last_error: Optional[str] = None
        self._title = ""
        self._joined_at: Optional[datetime] = None
        self._running = False
        self._callback_registered = False

    @classmethod
    def from_config(cls, config: Config, meeting=None, calendar=None) -> "ControlService":
        """Build the service from the agent's Config plus the services it controls."""
        return cls(
            config={
                "host": config.control.host,
                "port": config.control.port,
                "room_name": config.room.name,
                "room_location": config.room.location,
                "static_dir": str(STATIC_DIR),
            },
            meeting=meeting,
            calendar=calendar,
        )

    @property
    def bound_port(self) -> Optional[int]:
        """The port the listener is bound to, or None when not listening."""
        if self._runner is None or not self._runner.addresses:
            return None
        return self._runner.addresses[0][1]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def create_app(self) -> web.Application:
        """The aiohttp application; tests drive it without a listener."""
        _register_font_types()
        app = web.Application(client_max_size=MAX_BODY_BYTES)
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/api/status", self._handle_status)
        app.router.add_get("/api/calendar/events", self._handle_events)
        app.router.add_post("/api/meeting/join", self._handle_join)
        app.router.add_post("/api/meeting/leave", self._handle_leave)
        app.router.add_post("/api/meeting/mute", self._handle_mute)
        app.router.add_post("/api/meeting/camera", self._handle_camera)
        if self._static_dir.is_dir():
            app.router.add_static("/static/", self._static_dir)
        return app

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._meeting is not None and not self._callback_registered:
            self._meeting.add_state_callback(self._on_meeting_state)
            self._callback_registered = True
        runner = web.AppRunner(self.create_app())
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        try:
            await site.start()
        except OSError as e:
            logger.error(f"Room control page cannot listen on {self._host}:{self._port}: {e}")
            await runner.cleanup()
            return
        self._runner = runner
        logger.info(f"Room control page listening on http://{self._host}:{self.bound_port}/")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        logger.info("Room control page stopped")

    def _on_meeting_state(self, state: MeetingState) -> None:
        if state == MeetingState.CONNECTED:
            if self._joined_at is None:
                self._joined_at = datetime.now(timezone.utc).astimezone()
        elif state in (MeetingState.IDLE, MeetingState.ERROR):
            self._joined_at = None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _meeting_state(self) -> str:
        if self._meeting is None:
            return "idle"
        return self._meeting.state.value

    def _platform_for(self, url: Optional[str]) -> Optional[str]:
        """The configured platform a link's hostname belongs to, or None."""
        if not url or self._meeting is None:
            return None
        try:
            host = urlsplit(normalize_link(url)).hostname
        except ValueError:
            return None
        if not host:
            return None
        for platform, domains in PLATFORM_HOSTS.items():
            if any(host == domain or host.endswith("." + domain) for domain in domains):
                return platform if platform in self._meeting.get_available_platforms() else None
        return None

    def _event_dict(self, event) -> Dict[str, Any]:
        full = event.to_dict()
        data = {key: full.get(key) for key in EVENT_FIELDS}
        data["joinable"] = self._platform_for(event.meeting_url) is not None
        return data

    def _today_events(self) -> List[Any]:
        """Today's events in start order, by the device's local date."""
        if self._calendar is None:
            return []
        today = datetime.now().astimezone().date()
        return sorted(
            (e for e in self._calendar.events if e.start_time.astimezone().date() == today),
            key=lambda e: e.start_time,
        )

    def _calendar_status(self) -> Dict[str, Any]:
        if self._calendar is None:
            return {"connected": False, "provider": None, "current": None, "next": None}
        provider = self._calendar.provider
        current = self._calendar.get_current_meeting()
        now = datetime.now(timezone.utc)
        upcoming = next((e for e in self._today_events() if e.start_time > now), None)
        return {
            "connected": bool(self._calendar.connected),
            "provider": getattr(provider, "name", None) if provider is not None else None,
            "current": self._event_dict(current) if current is not None else None,
            "next": self._event_dict(upcoming) if upcoming is not None else None,
        }

    def _status(self) -> Dict[str, Any]:
        state = self._meeting_state()
        current = self._meeting.current_meeting if self._meeting is not None else None
        error = self._last_error
        if error is None and state == "error" and current is not None and current.error_message:
            error = current.error_message
        if error and state == "idle":
            # The provider refused the link before it started joining.
            state = "error"
        platforms = list(self._meeting.get_available_platforms()) if self._meeting is not None else []
        return {
            "room": {"name": self._room_name, "location": self._room_location},
            "server_time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "platforms": platforms,
            "meeting": {
                "state": state,
                "platform": current.platform if current is not None else None,
                "meeting_id": current.meeting_id if current is not None else None,
                "title": self._title,
                "url": current.meeting_url if current is not None else None,
                "joined_at": self._joined_at.isoformat(timespec="seconds") if self._joined_at else None,
                "muted": bool(current.is_muted) if current is not None else False,
                "camera_on": bool(current.is_camera_on) if current is not None else False,
                "error": error,
            },
            "calendar": self._calendar_status(),
        }

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_index(self, request: web.Request) -> web.StreamResponse:
        index = self._static_dir / "index.html"
        if not index.is_file():
            return web.Response(text="Room control page assets are missing.", status=500)
        return web.FileResponse(index, headers={"Cache-Control": "no-cache"})

    async def _handle_status(self, request: web.Request) -> web.Response:
        return web.json_response(self._status())

    async def _handle_events(self, request: web.Request) -> web.Response:
        if self._calendar is None:
            return web.json_response({"events": []})
        return web.json_response({"events": [self._event_dict(e) for e in self._today_events()]})

    @staticmethod
    def _require_json(request: web.Request) -> Optional[web.Response]:
        """Refuse POSTs that are not JSON: a cross-site form cannot drive the room."""
        if request.content_type != "application/json":
            return web.json_response({"error": "Send JSON with Content-Type: application/json"}, status=415)
        return None

    @staticmethod
    async def _read_object(request: web.Request) -> Optional[Dict[str, Any]]:
        """The request body as a JSON object, or None when it is not one."""
        if not request.can_read_body:
            return {}
        try:
            data = await request.json()
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _error_response(message: str, status: int) -> web.Response:
        return web.json_response({"error": message}, status=status)

    async def _handle_join(self, request: web.Request) -> web.Response:
        rejection = self._require_json(request)
        if rejection is not None:
            return rejection
        if self._meeting is None:
            return self._error_response("Meeting service not available", 503)
        data = await self._read_object(request)
        if data is None:
            return self._error_response("Send a JSON object with url or event_id", 400)
        title = ""
        url = str(data.get("url") or "").strip()
        event_id = str(data.get("event_id") or "").strip()
        if event_id:
            event = self._calendar.get_event_by_id(event_id) if self._calendar is not None else None
            if event is None:
                return self._error_response("Unknown calendar event", 400)
            if not event.meeting_url:
                return self._error_response("That meeting has no video link", 400)
            url = event.meeting_url
            title = event.title
        if not url:
            return self._error_response("Meeting link required", 400)
        url = normalize_link(url)
        if self._platform_for(url) is None:
            return self._error_response("Unsupported meeting link", 400)
        if self._meeting_state() in IN_PROGRESS_STATES:
            return self._error_response("A meeting is already in progress", 409)
        self._last_error = None
        self._title = title
        self._task = asyncio.create_task(self._run_join(url))
        return web.json_response({"state": "joining", "url": url}, status=202)

    async def _run_join(self, url: str) -> None:
        try:
            await self._meeting.join_meeting(url, display_name=self._room_name)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Join failed for {url}: {e}")
            self._last_error = str(e)

    async def _handle_leave(self, request: web.Request) -> web.Response:
        rejection = self._require_json(request)
        if rejection is not None:
            return rejection
        if self._meeting is None:
            return self._error_response("Meeting service not available", 503)
        if self._meeting_state() == "idle":
            if self._last_error is not None:
                # Dismissing a join the provider refused before it started.
                self._last_error = None
                self._title = ""
                return web.json_response({"state": "idle"})
            return self._error_response("No meeting to leave", 409)
        self._last_error = None
        self._task = asyncio.create_task(self._run_leave())
        return web.json_response({"state": "leaving"}, status=202)

    async def _run_leave(self) -> None:
        try:
            await self._meeting.leave_meeting()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Leave failed: {e}")
            self._last_error = str(e)
        finally:
            self._title = ""
            self._joined_at = None

    async def _handle_mute(self, request: web.Request) -> web.Response:
        rejection = self._require_json(request)
        if rejection is not None:
            return rejection
        if self._meeting is None:
            return self._error_response("Meeting service not available", 503)
        if self._meeting_state() != "connected":
            return self._error_response("Not in a meeting", 409)
        muted = await self._meeting.toggle_mute()
        return web.json_response({"muted": bool(muted)})

    async def _handle_camera(self, request: web.Request) -> web.Response:
        rejection = self._require_json(request)
        if rejection is not None:
            return rejection
        if self._meeting is None:
            return self._error_response("Meeting service not available", 503)
        if self._meeting_state() != "connected":
            return self._error_response("Not in a meeting", 409)
        camera_on = await self._meeting.toggle_camera()
        return web.json_response({"camera_on": bool(camera_on)})
