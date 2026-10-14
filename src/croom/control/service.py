"""
Room control page and API (spec docs/superpowers/specs/2026-09-24-room-control-page-design.md).

Serves the room page and a small JSON API on the local network and drives the
meeting and calendar services, the room's devices and the room's TV (spec
docs/superpowers/specs/2026-10-13-tv-remote-and-touchpad-design.md) on behalf of
whoever is in the room.
"""

import asyncio
import logging
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set
from urllib.parse import urlsplit

from aiohttp import web

from croom.control.settings import SettingsStore
from croom.core.config import Config
from croom.core.service import Service
from croom.devices.errors import DeviceUnavailable, Interrupted, NotReady
from croom.meeting.providers.base import MeetingState
from croom.tv.discover import discover_tvs
from croom.tv.errors import TvError
from croom.tv.names import one_line

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 4096
ZOOM_MEETING_ID = re.compile(r"^\d{9,11}$")
ID_GROUP_GAPS = re.compile("[ \u00a0]+")   # between the groups of an ID as invitations print it: spaces, no-break spaces
# Zoom's own alphabet for a passcode: letters, digits and @ - _ * (Zoom allows 10 of them; this allows 32).
PASSCODE = re.compile(r"[A-Za-z0-9@_*-]{1,32}")
BAD_PASSCODE = "Passcode must be letters, digits or @-_*, up to 32"
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
# The TV's idle styles (spec 2026-10-07 TV, section 4.1), in the order the room page offers them.
STYLES = ("info", "quiet", "brand", "bounce")
TV_NOT_ENABLED = "The TV is not enabled on this device (tv.enabled in its config)"
TV_REASON_LIMIT = 160   # how much of a failure's own text goes to the table (the TV service's words are never cut)


def _register_font_types() -> None:
    """Serve the bundled fonts with their real media type (aiohttp's table lacks woff2)."""
    mimetypes.add_type("font/woff2", ".woff2")
    try:
        from aiohttp.web_fileresponse import CONTENT_TYPES
        CONTENT_TYPES.add_type("font/woff2", ".woff2")
    except (ImportError, AttributeError):  # pragma: no cover - older or newer aiohttp layouts
        pass


def normalize_link(value: str) -> str:
    """
    Trim, turn a bare Zoom meeting id into a link, and add https:// when no scheme is given. The id may be written in
    groups, as invitations print it ("559 000 0559"): the blanks between them go.
    """
    url = value.strip()
    digits = ID_GROUP_GAPS.sub("", url)
    if ZOOM_MEETING_ID.match(digits):
        return f"https://zoom.us/j/{digits}"
    if url and "://" not in url:
        return "https://" + url
    return url


def normalize_passcode(value: Any) -> Optional[str]:
    """
    The passcode a join body carries, trimmed; None when it is absent, null or empty.

    Raises:
        ValueError: when it is not a string, or not 1 to 32 letters, digits and @ - _ *
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(BAD_PASSCODE)
    passcode = value.strip()
    if not passcode:
        return None
    if not PASSCODE.fullmatch(passcode):
        raise ValueError(BAD_PASSCODE)
    return passcode


class _BadBody(Exception):
    """A request body that is no use, in words for whoever sent it (a 400)."""


class _PageGone(Exception):
    """The page that sent a request has hung up: there is nobody to do it for."""


@web.middleware
async def _static_no_cache(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """
    Serve every /static/ file with Cache-Control: no-cache, as the pages are: the browser keeps its copy but asks
    before using it (a 304 when nothing changed), so a kiosk with a cached script never runs it next to a newer
    page that expects something else.
    """
    response = await handler(request)
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


class ControlService(Service):
    """
    Room control service ("control" under the ServiceManager).

    Args:
        config: dict with host, port, room_name, room_location, static_dir, screensaver
            (the default idle style) and settings_file (where the chosen style is kept).
        meeting: the MeetingService instance, or None.
        calendar: the CalendarService instance, or None.
        devices: the room devices the page controls (sound and camera), or None.
        store: the SettingsStore for the page's choices, or None to open settings_file.
        tv: the TvService for the room's TV, or None when the room has none enabled.
        discover: the search for TVs on the network (croom.tv.discover.discover_tvs unless a test stands in for it).
    """

    CANCEL_WAIT_S = 5.0   # a task that is cancelled (a join given up, the work that stop() ends) is waited for this long, no longer

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        meeting=None,
        calendar=None,
        devices=None,
        store=None,
        tv=None,
        discover=None,
    ):
        super().__init__("control")
        self.config = config or {}
        self._host = str(self.config.get("host", "0.0.0.0"))
        self._port = int(self.config.get("port", 8080))
        self._room_name = self.config.get("room_name", "Conference Room")
        self._room_location = self.config.get("room_location", "")
        self._static_dir = Path(self.config.get("static_dir", STATIC_DIR))
        self._store = store or SettingsStore(
            self.config.get("settings_file", "control-settings.json")
        )
        self._devices = devices
        self._read_failures: Dict[str, str] = {}
        self._default_style = str(self.config.get("screensaver", "info"))
        self._screensaver = self._load_style()
        self._meeting = meeting
        self._calendar = calendar
        self._tv = tv
        self._discover_tvs = discover or discover_tvs
        self._tv_busy = asyncio.Lock()                       # held by the control that is with the TV (see _handle_tv_control)
        self._search: Optional[asyncio.Task] = None          # the search for TVs that is running (see _handle_tv_discover)
        self._tv_requests: Set[asyncio.Task] = set()         # the requests that are with the TV service or wait for their turn

        self._runner: Optional[web.AppRunner] = None
        self._task: Optional[asyncio.Task] = None            # the running join or leave
        self._join_task: Optional[asyncio.Task] = None       # the last join: a Leave gives it up while it still runs
        self._last_error: Optional[str] = None
        self._title = ""
        self._joined_at: Optional[datetime] = None
        self._running = False
        self._callback_registered = False

    @classmethod
    def from_config(
        cls, config: Config, meeting=None, calendar=None, devices=None, store=None, tv=None
    ) -> "ControlService":
        """Build the service from the agent's Config plus the services it controls."""
        return cls(
            config={
                "host": config.control.host,
                "port": config.control.port,
                "room_name": config.room.name,
                "room_location": config.room.location,
                "static_dir": str(STATIC_DIR),
                "screensaver": config.control.screensaver,
                "settings_file": str(config.resolve_data_dir() / "control-settings.json"),
            },
            meeting=meeting,
            calendar=calendar,
            devices=devices,
            store=store,
            tv=tv,
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
        app = web.Application(client_max_size=MAX_BODY_BYTES, middlewares=[_static_no_cache])
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/sign", self._handle_sign)
        app.router.add_get("/tv", self._handle_tv)
        app.router.add_get("/api/status", self._handle_status)
        app.router.add_get("/api/calendar/events", self._handle_events)
        app.router.add_post("/api/meeting/join", self._handle_join)
        app.router.add_post("/api/meeting/leave", self._handle_leave)
        app.router.add_post("/api/meeting/mute", self._handle_mute)
        app.router.add_post("/api/meeting/camera", self._handle_camera)
        app.router.add_get("/api/screensaver", self._handle_screensaver)
        app.router.add_post("/api/screensaver", self._handle_set_screensaver)
        app.router.add_get("/api/audio/volume", self._handle_volume)
        app.router.add_post("/api/audio/volume", self._handle_set_volume)
        app.router.add_post("/api/camera/move", self._handle_camera_move)
        app.router.add_post("/api/camera/zoom", self._handle_camera_zoom)
        app.router.add_post("/api/camera/home", self._handle_camera_home)
        app.router.add_post("/api/camera/presets/{slot}", self._handle_camera_preset)
        app.router.add_post("/api/camera/setup", self._handle_camera_setup)
        app.router.add_post("/api/camera/preview", self._handle_camera_preview)
        app.router.add_post("/api/tv/control", self._handle_tv_control)
        app.router.add_post("/api/tv/pair", self._handle_tv_pair)
        app.router.add_post("/api/tv/forget", self._handle_tv_forget)
        app.router.add_get("/api/tv/discover", self._handle_tv_discover)
        if self._static_dir.is_dir():
            app.router.add_static("/static/", self._static_dir)
        app.on_shutdown.append(self._end_what_is_open)
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
        if self._runner is not None:
            # The listener closes its sockets, then ends what is still running (_end_what_is_open), and only then waits for the
            # requests that are in flight (a minute for them and another for what is left: more than the agent's shutdown is given)
            await self._runner.cleanup()
            self._runner = None
        self._task = self._join_task = self._search = None
        logger.info("Room control page stopped")

    async def _end_what_is_open(self, app: web.Application) -> None:
        """The application's on_shutdown, which aiohttp runs after the listening sockets are closed and before it waits for the
        requests in flight: so no request can arrive while the tasks that are ended here are let go of, and none is left for the wait
        to wait for. They are the join or the leave, the search for TVs, and the requests that are with the TV (a Turn on, a pairing
        that waits for a person) or wait their turn behind them. None of them is given time to finish: the pages that asked are going
        away with this service. It must not raise: an error here would end the listener's shutdown half way."""
        await self._cancel(self._task, self._search, *self._tv_requests)

    async def _cancel(self, *tasks: Optional[asyncio.Task]) -> None:
        """Cancel the tasks that are running among these and wait until they are over, for CANCEL_WAIT_S in all: what has not ended
        by then does not hold the caller up (a Leave, the agent's stop), and is warned about. asyncio.wait, not an await of the
        tasks: it does not raise a task's own cancellation here, and a cancel aimed at the caller itself still gets through."""
        running = [task for task in tasks if task is not None and not task.done()]
        if not running:
            return
        for task in running:
            task.cancel()
        _, left = await asyncio.wait(running, timeout=self.CANCEL_WAIT_S)
        for task in left:
            logger.warning(f"A task did not end within {self.CANCEL_WAIT_S:g} s of being cancelled, and is left to end by "
                           f"itself: {task!r}")

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
            "screensaver": self._screensaver,
            "audio": self._audio_state(),
            "camera": self._camera_state(),
            "tv": self._tv_status(),
        }

    # ------------------------------------------------------------------
    # The screensaver style
    # ------------------------------------------------------------------

    @property
    def screensaver(self) -> str:
        return self._screensaver

    def _load_style(self) -> str:
        """The stored style, else the configured default; a missing or broken file is not an error."""
        style = self._store.get("screensaver")
        if style in STYLES:
            return style
        return self._default_style if self._default_style in STYLES else "info"

    def _save_style(self, style: str) -> None:
        self._store.save("screensaver", style)

    def _screensaver_payload(self) -> Dict[str, Any]:
        return {"style": self._screensaver, "styles": list(STYLES)}

    async def _handle_screensaver(self, request: web.Request) -> web.Response:
        return web.json_response(self._screensaver_payload())

    async def _handle_set_screensaver(self, request: web.Request) -> web.Response:
        refused = self._require_json(request)
        if refused is not None:
            return refused
        data = await self._read_object(request)
        style = data.get("style") if data else None
        if style not in STYLES:
            return self._error_response(f"Unknown screensaver style {style!r}; use one of {', '.join(STYLES)}", 400)
        try:
            self._save_style(style)
        except OSError as e:
            logger.warning(f"Could not save the screensaver choice to {self._store.path}: {e}")
        self._screensaver = style
        logger.info(f"Screensaver style set to {style}")
        return web.json_response(self._screensaver_payload())

    # ------------------------------------------------------------------
    # Room devices: the speaker's volume and the camera (spec 2026-10-08)
    # ------------------------------------------------------------------

    NO_DEVICES = {"available": False, "reason": "no devices service"}

    def _volume(self):
        return getattr(self._devices, "volume", None)

    def _camera(self):
        return getattr(self._devices, "camera", None)

    async def _refresh_devices(self) -> None:
        """Cached reads, so a status poll costs nothing most of the time and never raises."""
        camera = self._camera()
        readers = [("speaker", getattr(self._volume(), "refresh", None))]
        if camera is not None and camera.available:
            # read_zoom() goes through discovery, which globs and opens /dev/video*: not on every poll of a room
            # without a camera. The devices service looks for a missing camera itself, every 30 s.
            readers.append(("camera zoom", getattr(camera, "read_zoom", None)))
        for label, reader in readers:
            if reader is None:
                continue
            try:
                await reader()
            except DeviceUnavailable as e:
                # A device lost in the middle of the read: its module has already warned about it, and it answers
                # with an unavailable state from now on.
                logger.debug(f"The {label} went away while it was read: {e}")
            except Exception as e:  # noqa: BLE001 - the status must always answer
                # Anything else is a bug in a device module, and this runs on every poll: warned about with its
                # traceback once per distinct message, as the devices service does, and per device so that two
                # failing readers do not take turns at resetting each other's message.
                message = f"Could not read the {label}: {type(e).__name__}: {e}"
                if message != self._read_failures.get(label):
                    self._read_failures[label] = message
                    logger.warning(message, exc_info=True)

    def _audio_state(self) -> Dict[str, Any]:
        volume = self._volume()
        return volume.state() if volume is not None else dict(self.NO_DEVICES, level=0, muted=False, device=None)

    def _camera_state(self) -> Dict[str, Any]:
        camera = self._camera()
        if camera is not None:
            return camera.state()
        return dict(self.NO_DEVICES, device=None, moving=False, busy=False, zoom={"level": 0, "min": 0, "max": 0},
                    position_known=False, home_saved=False, presets=[], preview=False)

    async def _device_call(self, request: web.Request, device, action) -> web.Response:
        """Run one device action: JSON in, the device's block out.

        400 for input the device refuses, 409 when it cannot do it now, 415 for a body that is not JSON, and 500,
        with the reason, when the camera's home or preset cannot be written to the settings file.
        """
        refused = self._require_json(request)
        if refused is not None:
            return refused
        if device is None:
            return self._error_response("no devices service", 409)
        data = await self._read_object(request)
        if data is None:
            return self._error_response("Send a JSON object", 400)
        try:
            return web.json_response(await action(data))
        except ValueError as e:
            return self._error_response(str(e), 400)
        except (DeviceUnavailable, NotReady, Interrupted) as e:
            return self._error_response(str(e), 409)
        except OSError as e:
            # The device modules turn their own OSErrors into DeviceUnavailable, so this is the settings store
            # failing to write the camera's home or a preset.
            logger.warning(f"Could not save the camera settings: {e}")
            return self._error_response(f"could not save the camera settings: {e}", 500)

    @staticmethod
    def _number_field(data: Dict[str, Any], key: str) -> float:
        """A number from the body exactly as sent. A boolean is not one, though True is 1 to Python."""
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        return value

    @classmethod
    def _int_field(cls, data: Dict[str, Any], key: str) -> int:
        """A whole number from the body, for the levels and steps the device modules clamp."""
        value = cls._number_field(data, key)
        try:
            return int(value)
        except (OverflowError, ValueError) as e:  # Python's JSON parser lets NaN, Infinity and 1e999 through
            raise ValueError(f"{key} must be a number") from e

    async def _handle_volume(self, request: web.Request) -> web.Response:
        await self._refresh_devices()
        return web.json_response(self._audio_state())

    async def _handle_set_volume(self, request: web.Request) -> web.Response:
        volume = self._volume()

        async def action(data):
            named = [key for key in ("level", "step", "muted") if key in data]
            if len(named) != 1:
                raise ValueError("Send one of level, step or muted")
            if named[0] == "level":
                return await volume.set_level(self._int_field(data, "level"))
            if named[0] == "step":
                return await volume.step(self._int_field(data, "step"))
            if not isinstance(data["muted"], bool):
                raise ValueError("muted must be true or false")
            return await volume.set_muted(data["muted"])

        return await self._device_call(request, volume, action)

    async def _handle_camera_move(self, request: web.Request) -> web.Response:
        camera = self._camera()

        async def action(data):
            if "pan" not in data or "tilt" not in data:
                raise ValueError("Send pan and tilt, each -1, 0 or 1")
            # As sent, not truncated: the camera refuses anything but -1, 0 or 1, so 0.5 or 1.9 is a 400 and not a
            # move in a direction nobody asked for.
            return await camera.move(self._number_field(data, "pan"), self._number_field(data, "tilt"))

        return await self._device_call(request, camera, action)

    async def _handle_camera_zoom(self, request: web.Request) -> web.Response:
        camera = self._camera()

        async def action(data):
            if "level" in data:
                await camera.zoom(self._int_field(data, "level"))
            elif "step" in data:
                await camera.zoom_step(self._int_field(data, "step"))
            else:
                raise ValueError("Send level or step")
            return camera.state()

        return await self._device_call(request, camera, action)

    async def _handle_camera_home(self, request: web.Request) -> web.Response:
        camera = self._camera()

        async def action(data):
            return await camera.home()

        return await self._device_call(request, camera, action)

    async def _handle_camera_preset(self, request: web.Request) -> web.Response:
        camera = self._camera()
        slot_text = request.match_info.get("slot", "")

        async def action(data):
            if not (slot_text.isascii() and slot_text.isdecimal()):   # isdigit() takes "²"; int() turns "٢" into 2
                raise ValueError("preset slots are 1 to 3")
            slot = int(slot_text)
            verb = data.get("action")
            if verb == "recall":
                return await camera.recall(slot)
            if verb == "save":
                return await camera.save(slot)
            raise ValueError("action must be recall or save")

        return await self._device_call(request, camera, action)

    async def _handle_camera_setup(self, request: web.Request) -> web.Response:
        camera = self._camera()

        async def action(data):
            verb = data.get("action")
            if verb == "find_stops":
                return await camera.find_stops()
            if verb == "save_home":
                return await camera.save_home()
            raise ValueError("action must be find_stops or save_home")

        return await self._device_call(request, camera, action)

    async def _handle_camera_preview(self, request: web.Request) -> web.Response:
        camera = self._camera()

        async def action(data):
            on = data.get("on")
            if not isinstance(on, bool):
                raise ValueError("on must be true or false")
            # Both refusals are for turning the preview on: turning it off always works, so closing the panel
            # brings the screensaver back whatever state the camera or the meeting is in.
            if on and not camera.available:
                raise DeviceUnavailable(camera.state()["reason"])
            if on and self._meeting_state() in IN_PROGRESS_STATES:
                raise NotReady("the TV is in a meeting")
            camera.set_preview(on)
            return camera.state()

        return await self._device_call(request, camera, action)

    # ------------------------------------------------------------------
    # The room's TV: its status, the controls, pairing and the search for TVs (spec 2026-10-13 TV remote and touchpad)
    # ------------------------------------------------------------------

    NO_TV = {"configured": False, "paired": False}

    def _tv_status(self) -> Dict[str, Any]:
        """The TV's block of the status: what the TV service shows, and a TV that is not set up when the room has no TV service.
        The status must always answer: a TV service that fails to report is warned about, with its traceback, once for each
        distinct message (as the devices are), and the meeting and the calendar are still told."""
        if self._tv is None:
            return dict(self.NO_TV)
        try:
            return self._tv.status()
        except Exception as e:  # noqa: BLE001 - the status must always answer
            message = f"Could not read the TV's status: {type(e).__name__}: {e}"
            if message != self._read_failures.get("TV"):
                self._read_failures["TV"] = message
                logger.warning(message, exc_info=True)
            return dict(self.NO_TV)

    @staticmethod
    def _reason(error: BaseException) -> str:
        """Why something failed, in a few words on one printable line: the error's own text, else the name of its class."""
        return one_line(str(error), TV_REASON_LIMIT) or type(error).__name__

    async def _tv_call(self, request: web.Request, action: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]) -> web.Response:
        """Run one thing asked of the TV service: JSON in, `{"tv": status}` out; `action(data)` asks the service and answers
        with the status it leaves.

        415 for a body that is not JSON, 503 in a room with no TV service, 400 for a body that is not an object or that the
        action finds no use for (_BadBody), and 409, in words for the table, when the TV could not do it: the TV service's own
        words for what it refuses (TvError), and a reason on one line for anything else (logged, as it is a bug or a library
        failing). A cancellation is not a failure to answer for and is not caught: the TV service keeps its own from leaking
        out, so what arrives here is this request's (stop() ends the requests that are with the TV, and none of them is
        answered as if it had been done).

        The request is on the books for as long as it lasts, which is how stop() finds it."""
        task = asyncio.current_task()
        self._tv_requests.add(task)
        try:
            return await self._tv_answer(request, action)
        finally:
            self._tv_requests.discard(task)

    async def _tv_answer(self, request: web.Request, action: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]) -> web.Response:
        """What _tv_call answers with (see there)."""
        refused = self._require_json(request)
        if refused is not None:
            return refused
        if self._tv is None:
            return self._error_response(TV_NOT_ENABLED, 503)
        data = await self._read_object(request)
        if data is None:
            return self._error_response("Send a JSON object", 400)
        try:
            return web.json_response({"tv": await action(data)})
        except _BadBody as e:
            return self._error_response(str(e), 400)
        except _PageGone:
            return web.Response(status=499)         # (nginx's "client closed request": nobody is there to read it)
        except TvError as e:
            return self._error_response(str(e), 409)
        except Exception as e:  # noqa: BLE001 - whatever it was, the table is told in words and not left with a 500
            logger.warning(f"The TV did not carry out the request to {request.path}: {type(e).__name__}: {e}", exc_info=True)
            return self._error_response("The TV did not do that: " + self._reason(e), 409)

    @staticmethod
    def _page_has_gone(request: web.Request) -> bool:
        """The page that made this request has hung up. The listener does not end a handler for that (the request's transport is
        None from then on, or is closing), so a request that waited has to look."""
        transport = request.transport
        return transport is None or transport.is_closing()

    async def _handle_tv_control(self, request: web.Request) -> web.Response:
        """One control of the TV, {"name", "value"?}, answered with the TV's status after it.

        One at a time: the TV service does not hold its controls apart (a second tap on Turn on during a wake would start a
        second wake, and a key sent during one goes down the socket the wake is using), so a control that comes while another
        is with the TV waits for it. There is no time limit of its own here: Turn on from the table waits for the TV to come
        on, up to 23 seconds, and the page's request is made to wait for that. The body is looked at before the wait, so that one
        that is no use is refused at once; and a control whose page has hung up by the time it has its turn is dropped (the
        listener does not end a handler for that, and a tap that waited behind a Turn on would be made for nobody)."""
        async def action(data: Dict[str, Any]) -> Dict[str, Any]:
            name = data.get("name")
            if not isinstance(name, str) or not name.strip():
                raise _BadBody('Send a JSON object with the "name" of the control, and its "value" when it has one')
            async with self._tv_busy:
                if self._page_has_gone(request):
                    logger.debug(f"The TV control {name!r} was dropped: the page that asked for it has gone")
                    raise _PageGone()
                return await self._tv.control(name, data.get("value"))

        return await self._tv_call(request, action)

    async def _handle_tv_pair(self, request: web.Request) -> web.Response:
        """Pair with the TV. The TV shows its prompt and a person accepts it with the TV's own remote, so this request waits
        for them: the TV service gives them a minute (TvService.PAIR_WAIT_S) and then says in words that nobody accepted the
        prompt, and a second pairing meanwhile is refused in words (it is the service's own single-flight). This route sets
        no time of its own. It is not held behind the controls' one-at-a-time, and holds none of them up."""
        async def action(data: Dict[str, Any]) -> Dict[str, Any]:
            await self._tv.pair()
            return self._tv.status()

        return await self._tv_call(request, action)

    async def _handle_tv_forget(self, request: web.Request) -> web.Response:
        """Let go of the TV: disconnect and delete the key."""
        async def action(data: Dict[str, Any]) -> Dict[str, Any]:
            await self._tv.forget()
            return self._tv.status()

        return await self._tv_call(request, action)

    async def _handle_tv_discover(self, request: web.Request) -> web.Response:
        """The LG TVs on the network. It answers in a room with no TV service as well: that is when it is needed.

        One search at a time. A search is a datagram on the network and a page read from each TV that answers, five or six
        seconds in all, so a request that comes while one is running waits for that one's answer and does not start another.
        The wait is shielded: this request going away (the page closed, the connection lost) does not end the search that
        others are waiting for."""
        if self._search is None or self._search.done():
            self._search = asyncio.create_task(self._discover_tvs())
            self._search.add_done_callback(self._search_ended)
        try:
            tvs = await asyncio.shield(self._search)
        except TvError as e:
            return self._error_response(str(e), 409)
        except Exception as e:  # noqa: BLE001 - said in _search_ended; the table is told in words and not left with a 500
            return self._error_response("The search for TVs did not finish: " + self._reason(e), 409)
        return web.json_response({"tvs": tvs})

    @staticmethod
    def _search_ended(search: asyncio.Task) -> None:
        """Said, once for a search however many requests waited for it, when it ends on an error that is not words for the table
        (a TvError is): a bug, or a library failing."""
        if search.cancelled():
            return
        error = search.exception()
        if error is not None and not isinstance(error, TvError):
            logger.warning(f"The search for TVs failed: {type(error).__name__}: {error}", exc_info=error)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_index(self, request: web.Request) -> web.StreamResponse:
        index = self._static_dir / "index.html"
        if not index.is_file():
            return web.Response(text="Room control page assets are missing.", status=500)
        return web.FileResponse(index, headers={"Cache-Control": "no-cache"})

    async def _handle_sign(self, request: web.Request) -> web.StreamResponse:
        sign = self._static_dir / "sign.html"
        if not sign.is_file():
            return web.Response(text="Door sign assets are missing.", status=500)
        return web.FileResponse(sign, headers={"Cache-Control": "no-cache"})

    async def _handle_tv(self, request: web.Request) -> web.StreamResponse:
        tv = self._static_dir / "tv.html"
        if not tv.is_file():
            return web.Response(text="TV page assets are missing.", status=500)
        return web.FileResponse(tv, headers={"Cache-Control": "no-cache"})

    async def _handle_status(self, request: web.Request) -> web.Response:
        await self._refresh_devices()
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
        try:
            passcode = normalize_passcode(data.get("passcode"))
        except ValueError as e:
            return self._error_response(str(e), 400)
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
            passcode = passcode or event.meeting_passcode   # a passcode sent with the join wins over the booking's own
        if not url:
            return self._error_response("Meeting link required", 400)
        url = normalize_link(url)
        if self._platform_for(url) is None:
            return self._error_response("Unsupported meeting link", 400)
        if self._meeting_state() in IN_PROGRESS_STATES:
            return self._error_response("A meeting is already in progress", 409)
        self._last_error = None
        self._title = title
        self._task = self._join_task = asyncio.create_task(self._run_join(url, passcode))
        return web.json_response({"state": "joining", "url": url}, status=202)

    async def _run_join(self, url: str, passcode: Optional[str] = None) -> None:
        try:
            await self._meeting.join_meeting(url, display_name=self._room_name, passcode=passcode)
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
        if self._join_task is not None and not self._join_task.done():
            # The join is still running: waiting for the room (the TV is woken first, for up to fifteen seconds) or asking its
            # provider. Left to go on it would finish after this Leave and put the room in the meeting that was walked away
            # from, so it ends first (a join that is given up leaves the meeting service as leave_meeting accepts).
            await self._give_up_join()
        elif self._meeting_state() == "idle":
            if self._last_error is not None:
                # Dismissing a join the provider refused before it started.
                self._last_error = None
                self._title = ""
                return web.json_response({"state": "idle"})
            return self._error_response("No meeting to leave", 409)
        self._last_error = None
        self._task = asyncio.create_task(self._run_leave())
        return web.json_response({"state": "leaving"}, status=202)

    async def _give_up_join(self) -> None:
        """End the join that is running and wait until it is over, for CANCEL_WAIT_S at most: a join that does not let go is left to
        finish in its own time (and said so), and does not hold the Leave up."""
        await self._cancel(self._join_task)

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
