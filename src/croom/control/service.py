"""
Room control page and API (spec docs/superpowers/specs/2026-09-24-room-control-page-design.md).

Serves the room page and a small JSON API on the local network and drives the
meeting and calendar services, the room's devices and the room's TV (spec
docs/superpowers/specs/2026-10-13-tv-remote-and-touchpad-design.md) on behalf of
whoever is in the room.
"""

import asyncio
import contextlib
import copy
import json
import logging
import mimetypes
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, NamedTuple, Optional, Set, Tuple
from urllib.parse import urlsplit

from aiohttp import WSCloseCode, WSMsgType, web

from croom.calendar.bookings import BookingError
from croom.control.settings import SettingsStore
from croom.core.config import DEFAULT_COMPANY, Config
from croom.core.service import Service
from croom.devices.errors import DeviceUnavailable, Interrupted, NotReady
from croom.laptop.errors import CAMERA_AWAY, LaptopError
from croom.meeting.providers.base import MeetingControlError, MeetingState
from croom.tv.discover import discover_tvs
from croom.tv.errors import TV_NOT_ENABLED, TvError
from croom.tv.names import one_line
from croom.tv.pointer import BAD_MESSAGE, PiMouse, PointerRelay

logger = logging.getLogger(__name__)


def _a_bool(label: str) -> Callable[[Any], Any]:
    """A check for a setting that is on or off: the bool, or ValueError saying what is allowed. Only true and false are taken: a
    string such as "no" or a number is refused, not guessed at."""
    def check(value: Any) -> Any:
        if not isinstance(value, bool):
            raise ValueError(f"The {label} setting must be true or false, not {value!r}")
        return value
    return check


STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 4096
ZOOM_MEETING_ID = re.compile(r"^\d{9,11}$")
ID_GROUP_GAPS = re.compile("[ \u00a0]+")   # between the groups of an ID as invitations print it: spaces, no-break spaces
# Zoom's own alphabet for a passcode: letters, digits and @ - _ * (Zoom allows 10 of them; this allows 32).
PASSCODE = re.compile(r"[A-Za-z0-9@_*-]{1,32}")
BAD_PASSCODE = "Passcode must be letters, digits or @-_*, up to 32"
IN_PROGRESS_STATES = {"joining", "in_lobby", "connected", "leaving"}
# What a control from the table is told when there is no meeting to carry it out in; the provider says the same in its own RuntimeError.
NOT_IN_A_MEETING = "Not in a meeting"
# Hosts (and their subdomains) a join link may point the room's browser at.
PLATFORM_HOSTS = {
    "zoom": ("zoom.us", "zoomgov.com"),
    "google_meet": ("meet.google.com", "g.co"),
    "teams": ("teams.microsoft.com", "teams.live.com"),
    "webex": ("webex.com",),
}
# The only calendar fields the page needs; the rest stays off the network.
EVENT_FIELDS = ("id", "title", "start_time", "end_time", "meeting_platform", "booking_id", "pending", "clash")
# The week the door sign shows (spec 2026-10-13, section 2.3): today and the six days after it, named in English whatever the
# machine's locale says (date.strftime would follow it).
WEEK_DAYS = 7
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# The TV's idle styles (spec 2026-10-07 TV, section 4.1), in the order the room page offers them.
STYLES = ("info", "quiet", "brand", "bounce")
TV_REASON_LIMIT = 160   # how much of a failure's own text goes to the table (the TV service's words are never cut)
# The door sign's backgrounds (spec 2026-10-13, section 2.4), in the order the room page offers them: a photograph of Austin
# each, from static/backgrounds/<key>.jpg ("hills" and "stadium" wait for theirs; the sign shows plain until a file is there), and
# plain, the navy alone. The schools whose football scores the sign may show (section 2.5).
SIGN_BACKGROUNDS = ("skyline", "capitol", "tower", "bridge", "pool", "star", "hills", "stadium", "plain")
SIGN_SCHOOLS = ("texas", "michigan", "texas_am", "missouri")


def _one_of(label: str, options: Tuple[str, ...]) -> Callable[[Any], Any]:
    """A check for a setting that is one of a few words: the word, or ValueError saying what is allowed."""
    def check(value: Any) -> Any:
        if not isinstance(value, str) or value not in options:
            raise ValueError(f"Unknown {label} {value!r}; use one of {', '.join(options)}")
        return value
    return check


def _some_of(label: str, options: Tuple[str, ...]) -> Callable[[Any], Any]:
    """A check for a setting that is a list of a few words, each listed once, in the order sent."""
    def check(value: Any) -> Any:
        if not isinstance(value, list):
            raise ValueError(f"{label}s must be a list; use any of {', '.join(options)}")
        for item in value:
            if not isinstance(item, str) or item not in options:
                raise ValueError(f"Unknown {label} {item!r}; use any of {', '.join(options)}")
        return list(dict.fromkeys(value))
    return check


# The room settings the page may change through /api/settings: each key's default and the check that turns what was sent
# into what is kept (or raises ValueError, in words). Only these keys are served or changed; the settings file holds more.
ROOM_SETTINGS: Dict[str, Tuple[Any, Callable[[Any], Any]]] = {
    "key_click": (True, _a_bool("key click")),
    "sign_background": ("skyline", _one_of("door sign background", SIGN_BACKGROUNDS)),
    "sign_scores": ([], _some_of("school", SIGN_SCHOOLS)),
}


def read_setting(store, key: str) -> Any:
    """A room setting as the store holds it, else its default: a missing, broken or no longer valid value is not an error. What the page
    loads at start, and what the scoreboard reads each time it needs the schools that are on, so that they cannot disagree."""
    default, check = ROOM_SETTINGS[key]
    try:
        return check(store.get(key, default))
    except ValueError:
        return check(default)


def first_line(error: BaseException) -> str:
    """An exception's words for the table: its first line, never a stack or a second line."""
    text = str(error).strip()
    return text.splitlines()[0].strip() if text else type(error).__name__


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


class _Pointer(NamedTuple):
    """An open pointer socket's side of the service: the request that opened it (its connection, which stop() cuts when the page does not
    answer the close) and the relay that carries its messages out."""
    request: web.Request
    relay: PointerRelay


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
        config: dict with host, port, room_name, room_location, room_company (the practice's name on the
            door sign), static_dir, screensaver (the default idle style) and settings_file (where the chosen
            style is kept).
        meeting: the MeetingService instance, or None.
        calendar: the CalendarService instance, or None.
        devices: the room devices the page controls (sound and camera), or None.
        store: the SettingsStore for the page's choices, or None to open settings_file.
        tv: the TvService for the room's TV, or None when the room has none enabled.
        discover: the search for TVs on the network (croom.tv.discover.discover_tvs unless a test stands in for it).
        mouse: the room Pi's virtual mouse that the touchpad drives (a croom.tv.pointer.PiMouse, made when it is first used, unless a
            test stands in for it).
        scores: the scores service (current() lists the football scores of the schools that are on), or None.
        laptop: the Laptop service (the room's MeetUp lent to a laptop through the USB switch), or None in a room without the switch.
    """

    CANCEL_WAIT_S = 5.0   # a task that is cancelled (a join given up, the work that stop() ends) is waited for this long, no longer
    POINTER_HEARTBEAT_S = 5.0   # a pointer socket that has been quiet this long is pinged: a page that does not answer is let go of

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        meeting=None,
        calendar=None,
        devices=None,
        store=None,
        tv=None,
        discover=None,
        mouse=None,
        scores=None,
        laptop=None,
    ):
        super().__init__("control")
        self.config = config or {}
        self._host = str(self.config.get("host", "0.0.0.0"))
        self._port = int(self.config.get("port", 8080))
        self._room_name = self.config.get("room_name", "Conference Room")
        self._room_location = self.config.get("room_location", "")
        self._room_company = self.config.get("room_company", DEFAULT_COMPANY)
        self._static_dir = Path(self.config.get("static_dir", STATIC_DIR))
        self._store = store or SettingsStore(
            self.config.get("settings_file", "control-settings.json")
        )
        self._devices = devices
        self._read_failures: Dict[str, str] = {}
        self._default_style = str(self.config.get("screensaver", "info"))
        self._screensaver = self._load_style()
        self._settings = self._load_settings()
        self._meeting = meeting
        self._calendar = calendar
        self._tv = tv
        self._discover_tvs = discover or discover_tvs
        self._tv_busy = asyncio.Lock()                       # held by the control that is with the TV (see _handle_tv_control)
        self._search: Optional[asyncio.Task] = None          # the search for TVs that is running (see _handle_tv_discover)
        self._discovered_tvs: Dict[str, Dict[str, Any]] = {}
        self._discovered_at = 0.0
        self._tv_selecting = False
        self._tv_requests: Set[asyncio.Task] = set()         # the requests that are with the TV service or wait for their turn
        self._mouse = mouse if mouse is not None else PiMouse()
        self._pointers: Dict[web.WebSocketResponse, _Pointer] = {}       # the pointer sockets that are open
        self._mouse_waits: Set[asyncio.Task] = set()         # the sockets' handlers that wait for the room Pi's mouse to be made (stop() ends them)
        self._scores = scores
        self._scores_failure: Optional[str] = None
        self._laptop = laptop

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
        cls,
        config: Config,
        meeting=None,
        calendar=None,
        devices=None,
        store=None,
        tv=None,
        scores=None,
        laptop=None,
    ) -> "ControlService":
        """Build the service from the agent's Config plus the services it controls."""
        return cls(
            config={
                "host": config.control.host,
                "port": config.control.port,
                "room_name": config.room.name,
                "room_location": config.room.location,
                "room_company": config.room.company,
                "static_dir": str(STATIC_DIR),
                "screensaver": config.control.screensaver,
                "settings_file": str(config.resolve_data_dir() / "control-settings.json"),
            },
            meeting=meeting,
            calendar=calendar,
            devices=devices,
            store=store,
            tv=tv,
            scores=scores,
            laptop=laptop,
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
        app.router.add_get("/api/calendar/week", self._handle_week)
        app.router.add_get("/api/bookings/slots", self._handle_booking_slots)
        app.router.add_post("/api/bookings", self._handle_book)
        app.router.add_post("/api/bookings/now", self._handle_book_now)
        app.router.add_post("/api/bookings/{key}/end", self._handle_booking_end)
        app.router.add_post("/api/bookings/{key}/cancel", self._handle_booking_cancel)
        app.router.add_post("/api/meeting/join", self._handle_join)
        app.router.add_post("/api/meeting/leave", self._handle_leave)
        app.router.add_post("/api/meeting/mute", self._handle_mute)
        app.router.add_post("/api/meeting/camera", self._handle_camera)
        app.router.add_post("/api/meeting/control", self._handle_control)
        app.router.add_post("/api/meeting/dialog", self._handle_dialog)
        app.router.add_post("/api/meeting/chat/seen", self._handle_chat_seen)
        app.router.add_get("/api/meeting/diagnostics", self._handle_diagnostics)
        app.router.add_get("/api/screensaver", self._handle_screensaver)
        app.router.add_post("/api/screensaver", self._handle_set_screensaver)
        app.router.add_get("/api/settings", self._handle_settings)
        app.router.add_post("/api/settings", self._handle_set_settings)
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
        app.router.add_post("/api/tv/select", self._handle_tv_select)
        app.router.add_post("/api/tv/settings", self._handle_tv_settings)
        app.router.add_post("/api/tv/forget", self._handle_tv_forget)
        app.router.add_get("/api/tv/discover", self._handle_tv_discover)
        app.router.add_post("/api/laptop", self._handle_laptop)
        app.router.add_get("/ws/pointer", self._handle_pointer)
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
        self._mouse.close()
        logger.info("Room control page stopped")

    async def _end_what_is_open(self, app: web.Application) -> None:
        """The application's on_shutdown, which aiohttp runs after the listening sockets are closed and before it waits for the
        requests in flight: so no request can arrive while the tasks that are ended here are let go of, and none is left for the wait
        to wait for. They are the join or the leave, the search for TVs, the requests that are with the TV (a Turn on, a pairing that
        waits for a person) or wait their turn behind them, and the pointer sockets, which are closed (going away) with the move that is
        on its way to the TV and the handler that waits for the room Pi's mouse to be made (a device that never appears would hold the
        listener's wait for a handler up for a minute). None of them is given time to finish: the pages that asked are going away with
        this service. It must not raise: an error here would end the listener's shutdown half way.

        All of it is bounded by CANCEL_WAIT_S, the pointer sockets' closes included: a page that does not answer a close, or that sends
        frames and reads no answer (the socket's handler is then blocked in a send, and the close frame cannot be written either), would
        hold the listener's wait up for two minutes. The connection of a socket that is still open after the bound is cut."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.CANCEL_WAIT_S
        closing = {asyncio.ensure_future(ws.close(code=WSCloseCode.GOING_AWAY, message=b"The room page is stopping")): pointer.request
                   for ws, pointer in self._pointers.items()}
        moves = [pointer.relay.sending for pointer in self._pointers.values()]
        await self._cancel(self._task, self._search, *self._tv_requests, *moves, *self._mouse_waits)
        if closing:
            _, left = await asyncio.wait(list(closing), timeout=max(0.0, deadline - loop.time()))
            for task in left:
                transport = closing[task].transport
                if transport is not None:
                    transport.abort()                  # (what the handler is blocked on then fails, and the handler ends)

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

    def _local_today(self) -> date:
        """Today's date on the device's clock: the room's day, so a booking at 11:30 PM is that day's, not the next UTC day's."""
        return datetime.now().astimezone().date()

    def _today_events(self) -> List[Any]:
        """Today's events in start order, by the device's local date."""
        if self._calendar is None:
            return []
        today = self._local_today()
        return sorted(
            (e for e in self._calendar.events if e.start_time.astimezone().date() == today),
            key=lambda e: e.start_time,
        )

    def _week(self) -> List[Dict[str, Any]]:
        """The next seven days, today first, each with its events in start order, by the device's local date as today's list is.
        A day with nothing booked is still there, with an empty list. The calendar service holds the bookings from an hour ago to a week
        ahead, which covers the six days after today whole and what is left of today."""
        first = self._local_today()
        days = []
        for n in range(WEEK_DAYS):
            day = first + timedelta(days=n)
            days.append({"date": day.isoformat(), "label": DAY_NAMES[day.weekday()], "events": []})
        if self._calendar is not None:
            events_by_date = {day["date"]: day["events"] for day in days}
            for e in sorted(self._calendar.events, key=lambda e: e.start_time):
                bookings = events_by_date.get(e.start_time.astimezone().date().isoformat())
                if bookings is not None:
                    bookings.append(self._event_dict(e))
        return days

    def _calendar_status(self) -> Dict[str, Any]:
        if self._calendar is None:
            return {"connected": False, "provider": None, "current": None, "next": None, "booking": None}
        provider = self._calendar.provider
        current = self._calendar.get_current_meeting()
        now = datetime.now(timezone.utc)
        upcoming = next((e for e in self._today_events() if e.start_time > now), None)
        return {
            "connected": bool(self._calendar.connected),
            "provider": getattr(provider, "name", None) if provider is not None else None,
            "current": self._event_dict(current) if current is not None else None,
            "next": self._event_dict(upcoming) if upcoming is not None else None,
            "booking": self._bookings.state() if self._bookings is not None else None,
        }

    def _meeting_block(self) -> Dict[str, Any]:
        """The `meeting` block of /api/status: where the meeting stands, and the model the table draws from (spec 2026-10-13, section 3).
        The routes that carry a control, a dialog answer or the chat seen to the meeting answer with this same block, so the table renders
        from one shape."""
        state = self._meeting_state()
        current = self._meeting.current_meeting if self._meeting is not None else None
        joined = current if state != "error" else None     # what the table draws from is the joined meeting's: an error leaves nothing of it
        error = self._last_error
        if error is None and state == "error" and current is not None and current.error_message:
            error = current.error_message
        if error and state == "idle":
            # The provider refused the link before it started joining.
            state = "error"
        return {
            "state": state,
            "platform": current.platform if current is not None else None,
            "meeting_id": current.meeting_id if current is not None else None,
            "title": self._title,
            "url": current.meeting_url if current is not None else None,
            "joined_at": self._joined_at.isoformat(timespec="seconds") if self._joined_at else None,
            "muted": bool(current.is_muted) if current is not None else False,
            "camera_on": bool(current.is_camera_on) if current is not None else False,
            "error": error,
            "controls": dict(joined.controls) if joined is not None else {},
            "participants": dict(joined.people) if joined is not None else {"count": 0, "list": []},
            "chat": dict(joined.chat) if joined is not None else {"unread": 0, "messages": []},
            "notice": joined.notice if joined is not None else "",
            "dialog": dict(joined.dialog) if joined is not None and joined.dialog else None,
        }

    def _status(self) -> Dict[str, Any]:
        platforms = list(self._meeting.get_available_platforms()) if self._meeting is not None else []
        status = {
            "room": {"name": self._room_name, "location": self._room_location, "company": self._room_company},
            "server_time": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "platforms": platforms,
            "meeting": self._meeting_block(),
            "calendar": self._calendar_status(),
            "screensaver": self._screensaver,
            "settings": self._settings_payload(),
            "audio": self._audio_state(),
            "camera": self._camera_state(),
            "tv": self._tv_status(),
            "laptop": self._laptop_status(),
        }
        scores = self._scores_status()
        if scores:
            status["scores"] = scores   # only while a school is on: no key, not an empty list, when none is
        return status

    def _scores_status(self) -> List[Dict[str, Any]]:
        """The football scores of the schools that are on, one entry each (spec 2026-10-13, section 2.5); nothing when none is on, there
        is no scores service, or it fails: the status must always answer, and the door sign and the table both depend on it."""
        if self._scores is None:
            return []
        try:
            return list(self._scores.current())
        except Exception as e:  # noqa: BLE001 - the status must always answer
            message = f"Could not read the football scores: {type(e).__name__}: {e}"
            if message != self._scores_failure:   # warned about once with its traceback, per distinct message, as the devices' reads are
                self._scores_failure = message
                logger.warning(message, exc_info=True)
            return []

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
    # Room settings: the keyboard's click and the door sign's background and schools (spec 2026-10-13)
    # ------------------------------------------------------------------

    def _load_settings(self) -> Dict[str, Any]:
        """Each setting as stored, else its default: a missing, broken or no longer valid value is not an error."""
        return {key: read_setting(self._store, key) for key in ROOM_SETTINGS}

    def _settings_payload(self) -> Dict[str, Any]:
        """Every setting, as copies: what an answer carries is not the room's own list."""
        return copy.deepcopy(self._settings)

    async def _handle_settings(self, request: web.Request) -> web.Response:
        return web.json_response(self._settings_payload())

    async def _handle_set_settings(self, request: web.Request) -> web.Response:
        """Change one or more settings: a JSON object of keys to values. Every value is checked before any is kept, so a
        request with one bad value changes nothing; the answer is every setting, as the status carries them."""
        refused = self._require_json(request)
        if refused is not None:
            return refused
        data = await self._read_object(request)
        if not data:
            return self._error_response(f"Send a JSON object with a setting to change: {', '.join(ROOM_SETTINGS)}", 400)
        values: Dict[str, Any] = {}
        for key, value in data.items():
            if key not in ROOM_SETTINGS:
                return self._error_response(f"Unknown setting {key!r}; use {', '.join(ROOM_SETTINGS)}", 400)
            try:
                values[key] = ROOM_SETTINGS[key][1](value)
            except ValueError as e:
                return self._error_response(str(e), 400)
        try:
            for key, value in values.items():
                self._store.save(key, value)
        except OSError as e:
            # As the screensaver's choice: it holds until the restart, and the log says why it will not last. The store holds it too, so that what reads the
            # choice from there (the scoreboard, for the schools) goes on with the page's and not with the one on the disk.
            logger.warning(f"Could not save the room settings to {self._store.path}: {e}")
            for key, value in values.items():
                self._store.hold(key, value)
        self._settings.update(values)
        logger.info(f"Room settings changed: {', '.join(f'{key} = {value}' for key, value in values.items())}")
        return web.json_response(self._settings_payload())

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
    # The room's TV: its status, the controls, pairing, the search for TVs and the pointer (spec 2026-10-13 TV remote and touchpad)
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
        is with the TV waits for it. There is no time limit of its own here, because the TV service bounds what it does: a command
        is given ASK_TIMEOUT_S (a connection that has died without the library noticing frees the lock at that, not at the library's
        heartbeat), and Turn on from the table waits for the TV to come on, up to 23 seconds, which the page's request is made to wait
        for. The body is looked at before the wait, so that one
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

    async def _handle_tv_select(self, request: web.Request) -> web.Response:
        """Only a TV from this server's recent discovery can be selected by the browser."""
        async def action(data: Dict[str, Any]) -> Dict[str, Any]:
            host = data.get("host")
            recent = asyncio.get_running_loop().time() - self._discovered_at < 300
            found = self._discovered_tvs.get(host) if isinstance(host, str) and recent else None
            if found is None:
                raise _BadBody("Use Find TVs again, then select a TV from the list")
            if self._tv_selecting:
                raise TvError("A TV connection is already waiting; finish or cancel it first")
            selected = dict(found)
            for name in ("mac", "pi_input", "laptop_input"):
                if name in data:
                    selected[name] = data[name]
            self._tv_selecting = True
            try:
                async with self._tv_busy:
                    if self._page_has_gone(request):
                        raise _PageGone()
                    await self._tv.select(selected)
                    return self._tv.status()
            finally:
                self._tv_selecting = False

        return await self._tv_call(request, action)

    async def _handle_tv_settings(self, request: web.Request) -> web.Response:
        async def action(data: Dict[str, Any]) -> Dict[str, Any]:
            if self._tv_selecting:
                raise TvError("A TV connection is already waiting; finish or cancel it first")
            async with self._tv_busy:
                if self._page_has_gone(request):
                    raise _PageGone()
                await self._tv.update_settings(data)
                return self._tv.status()

        return await self._tv_call(request, action)

    # ------------------------------------------------------------------
    # Laptop mode (spec 2026-10-27): the room's MeetUp lent to a laptop at the table through the USB switch
    # ------------------------------------------------------------------

    NO_LAPTOP = "This room has no laptop switch"

    def _laptop_status(self) -> Dict[str, Any]:
        """Laptop mode's block of the status; {"enabled": False} in a room without the switch. The status must always answer."""
        if self._laptop is None:
            return {"enabled": False}
        try:
            return self._laptop.state()
        except Exception as e:  # noqa: BLE001 - the status must always answer
            message = f"Could not read Laptop mode: {type(e).__name__}: {e}"
            if message != self._read_failures.get("laptop"):
                self._read_failures["laptop"] = message
                logger.warning(message, exc_info=True)
            return {"enabled": False}

    async def _handle_laptop(self, request: web.Request) -> web.Response:
        """{"to": "laptop", "minutes"?: n} or {"to": "room"}, answered with Laptop mode's state, or 409 in words."""
        rejection = self._require_json(request)
        if rejection is not None:
            return rejection
        if self._laptop is None:
            return self._error_response(self.NO_LAPTOP, 503)
        data = await self._read_object(request)
        if data is None:
            return self._error_response("Send a JSON object", 400)
        to = data.get("to")
        try:
            if to == "laptop":
                minutes = data.get("minutes")
                if minutes is not None and (not isinstance(minutes, int) or isinstance(minutes, bool)):
                    return self._error_response("minutes is a whole number", 400)
                state = await self._laptop.to_laptop(minutes)
            elif to == "room":
                state = await self._laptop.to_room()
            else:
                return self._error_response('Send {"to": "laptop"} or {"to": "room"}', 400)
        except (LaptopError, BookingError) as e:
            return self._error_response(str(e), e.status)
        return web.json_response({"laptop": state})

    async def _handle_tv_discover(self, request: web.Request) -> web.Response:
        """The LG and Samsung TVs on the network. It also answers before a TV is configured.

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
        self._discovered_tvs = {
            tv["host"]: dict(tv) for tv in tvs if isinstance(tv, dict) and isinstance(tv.get("host"), str)
        }
        self._discovered_at = asyncio.get_running_loop().time()
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

    async def _handle_pointer(self, request: web.Request) -> web.StreamResponse:
        """WS /ws/pointer: the touchpad's events from the table (spec section 5). Each JSON text frame is one message for the TV's
        cursor or the room Pi's mouse, `{"target": "tv"|"pi", "type": "move"|"tap"|"right"|"scroll"|"down"|"up", "dx", "dy"}`, handed
        to this socket's PointerRelay; what cannot be done is answered as a `{"error": words}` frame. A frame that is no use never closes
        the socket (only one bigger than a request body does: aiohttp ends the connection for that). The cursor is not a control: it takes
        no turn behind them. However the socket ends, a button that was held goes up.

        A browser lets any page open a WebSocket to any address, and says where the page came from: only a page of this server may drive the
        room's mouse and TV (the POSTs are kept from cross-site forms by wanting JSON; a WebSocket has no such request to refuse)."""
        if not self._same_origin(request):
            return self._error_response("The pointer is for the room's own page", 403)
        ws = web.WebSocketResponse(heartbeat=self.POINTER_HEARTBEAT_S, max_msg_size=MAX_BODY_BYTES)
        await ws.prepare(request)
        relay = PointerRelay(self._tv, self._mouse, self._off_the_loop)
        self._pointers[ws] = _Pointer(request, relay)
        try:
            await self._make_the_mouse()
            async for frame in ws:
                if frame.type == WSMsgType.TEXT:
                    words = await self._pointer_message(relay, frame.data)
                elif frame.type == WSMsgType.BINARY:
                    words = BAD_MESSAGE
                else:
                    break
                if words is not None:
                    with contextlib.suppress(ConnectionError):         # (the page may have gone while it was answered)
                        await ws.send_json({"error": words})
        finally:
            try:
                await relay.release_all()
            finally:
                self._pointers.pop(ws, None)             # (not before: until the relay is done, stop() ends the move that it waits for)
        return ws

    async def _make_the_mouse(self) -> None:
        """Make the room Pi's virtual mouse now, as a pointer socket opens, so that the desktop has taken it up by the time a finger is
        on the pad. The device takes a moment to appear (evdev waits for it and looks through /dev/input): it is made in a thread, and
        the event loop goes on. It is kept until the service stops. One that cannot be made is no reason to refuse the socket (the TV's
        cursor does not need it): every Pi event says why not, in words (PiMouse.unavailable), and tries again."""
        try:
            await self._off_the_loop(self._mouse.open)
        except TvError:
            pass
        except Exception:  # noqa: BLE001 - a bug in making it: the first Pi event makes it, or says why not
            logger.warning("The room Pi's mouse could not be made as the pointer socket opened", exc_info=True)

    async def _off_the_loop(self, call: Callable[..., Any], *args: Any) -> Any:
        """Run a call that waits for the room Pi's mouse (making the device: evdev waits for it to appear) in a thread, and wait for it
        without holding the loop up. The task that waits is on the books while it does, which is how stop() finds it: a device that never
        appears must not keep a socket's handler, and with it the listener's shutdown, waiting."""
        task = asyncio.current_task()
        self._mouse_waits.add(task)
        try:
            return await asyncio.to_thread(call, *args)
        finally:
            self._mouse_waits.discard(task)

    def _pointer_in_use(self) -> None:
        """The touchpad drove the room Pi's mouse: the TV shows its pointer for a moment (it is hidden there otherwise; MeetingService and
        TvDisplay.pointer_in_use)."""
        hook = getattr(self._meeting, "pointer_in_use", None)
        if hook is None:
            return
        try:
            hook()
        except Exception:  # noqa: BLE001 - the pointer is a nicety: the mouse has moved whatever the TV makes of it
            logger.debug("The TV did not take the pointer's use", exc_info=True)

    async def _pointer_message(self, relay: PointerRelay, text: str) -> Optional[str]:
        """Hand one frame to the relay: None when it was carried out, else the words to answer it with. Whatever goes wrong is words
        and not the end of the socket: the relay's own refusals as they are, anything else (a bug, a library) as a reason on one line,
        logged once for each distinct message, as the sockets send many frames a second."""
        try:
            message = json.loads(text)
        except (ValueError, RecursionError):                           # (a parser that is given more brackets than it can count is no better)
            return BAD_MESSAGE
        try:
            await relay.handle(message)
            if isinstance(message, dict) and message.get("target") == "pi":
                self._pointer_in_use()
        except TvError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001 - whatever it was, the page is told in words and the socket goes on
            failure = f"The pointer did not do that: {self._reason(e)}"
            if failure != self._read_failures.get("pointer"):
                self._read_failures["pointer"] = failure
                logger.warning(f"The pointer could not carry out a message: {type(e).__name__}: {e}", exc_info=True)
            return failure
        return None

    @staticmethod
    def _same_origin(request: web.Request) -> bool:
        """Whether the page that opens a WebSocket is one of this server's. A request with no Origin is not from a browser page (a
        script, a test) and goes through."""
        origin = request.headers.get("Origin")
        if origin is None:
            return True
        try:
            return urlsplit(origin).netloc.lower() == request.host.lower()
        except ValueError:
            return False

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

    async def _handle_week(self, request: web.Request) -> web.Response:
        return web.json_response({"days": self._week()})

    # ------------------------------------------------------------------
    # The room's own bookings (spec 2026-10-25): Book and Use now at the door sign and the room page, End now and Cancel
    # ------------------------------------------------------------------

    @property
    def _bookings(self):
        """The calendar service's RoomBookings, or None: a room with no calendar to book in."""
        return getattr(self._calendar, "bookings", None) if self._calendar is not None else None

    NO_BOOKINGS = "Booking needs the room's Google Calendar, and none is set up"

    async def _handle_booking_slots(self, request: web.Request) -> web.Response:
        if self._bookings is None:
            return self._error_response(self.NO_BOOKINGS, 503)
        days = self._bookings.days()
        try:
            day = date.fromisoformat(request.query["date"]) if "date" in request.query else days[0]
        except ValueError:
            return self._error_response("date is YYYY-MM-DD", 400)
        labels = [{"date": d.isoformat(), "label": "Today" if n == 0 else "Tomorrow" if n == 1 else DAY_NAMES[d.weekday()]}
                  for n, d in enumerate(days)]
        return web.json_response(dict(self._bookings.slots(day), days=labels))

    async def _booking_request(self, request: web.Request, with_start: bool) -> Tuple[Optional[Dict[str, Any]], Optional[web.Response]]:
        """The body of a Book or Use now: its start (Book only), minutes, title and source, or the response that refuses it."""
        rejection = self._require_json(request)
        if rejection is not None:
            return None, rejection
        if self._bookings is None:
            return None, self._error_response(self.NO_BOOKINGS, 503)
        data = await self._read_object(request)
        if data is None:
            return None, self._error_response("Send a JSON object", 400)
        minutes = data.get("minutes")
        if not isinstance(minutes, int) or isinstance(minutes, bool):
            return None, self._error_response("minutes is a whole number", 400)
        asked = {"minutes": minutes, "title": data.get("title"), "source": data.get("source")}
        if with_start:
            try:
                start = datetime.fromisoformat(str(data.get("start")))
            except ValueError:
                return None, self._error_response("start is a time with its offset, as the open times give it", 400)
            if start.tzinfo is None:
                return None, self._error_response("start is a time with its offset, as the open times give it", 400)
            asked["start"] = start
        return asked, None

    def _booking_answer(self, booking, status: int = 200) -> web.Response:
        """A booking as the page shows it, whether it waits to reach Google, and the calendar's refusal when it refuses."""
        event = next((e for e in self._calendar.events if e.booking_id == booking.id), None) or booking.event()
        problem = self._bookings.state()["problem"]
        return web.json_response({"booking": self._event_dict(event), "pending": booking.pending,
                                  "warning": problem if booking.pending else None}, status=status)

    async def _handle_book(self, request: web.Request) -> web.Response:
        asked, refusal = await self._booking_request(request, with_start=True)
        if refusal is not None:
            return refusal
        try:
            booking = await self._bookings.book(asked["start"], asked["minutes"], asked["title"], asked["source"])
        except BookingError as e:
            return self._error_response(str(e), e.status)
        return self._booking_answer(booking, 201)

    async def _handle_book_now(self, request: web.Request) -> web.Response:
        asked, refusal = await self._booking_request(request, with_start=False)
        if refusal is not None:
            return refusal
        try:
            booking = await self._bookings.start_now(asked["minutes"], asked["title"], asked["source"])
        except BookingError as e:
            return self._error_response(str(e), e.status)
        return self._booking_answer(booking, 201)

    async def _booking_change(self, request: web.Request, change: str) -> web.Response:
        rejection = self._require_json(request)
        if rejection is not None:
            return rejection
        if self._bookings is None:
            return self._error_response(self.NO_BOOKINGS, 503)
        data = await self._read_object(request)
        if change == "end" and (data or {}).get("source") != "room page":
            # A meeting in progress is ended at the room's own touch screen, not from the hallway (spec 2026-10-25).
            return self._error_response("End now is on the room's touch screen", 403)
        try:
            booking = await getattr(self._bookings, change)(request.match_info["key"])
        except BookingError as e:
            return self._error_response(str(e), e.status)
        return self._booking_answer(booking)

    async def _handle_booking_end(self, request: web.Request) -> web.Response:
        return await self._booking_change(request, "end")

    async def _handle_booking_cancel(self, request: web.Request) -> web.Response:
        return await self._booking_change(request, "cancel")

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
        if self._laptop is not None and self._laptop.camera_away:
            return self._error_response(CAMERA_AWAY, 409)
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

    async def _state_asked_for(self, request: web.Request, field: str) -> Any:
        """The state a mute or camera request asks for: True or False when the body says it (`{"muted": true}`, `{"on": false}`), None when it says
        nothing of it (no body, `{}`: the toggle these two routes have always been), or the 400 for a body that is not an object or whose field is
        not true or false. The table sends the state its button names, so that a tap made a moment after the host muted the room, or after the
        camera went off at the TV, ends as the person asked and not the other way round."""
        data = await self._read_object(request)
        if data is None:
            return self._error_response(f"Send a JSON object with {field} true or false, or nothing to switch it", 400)
        if field not in data:
            return None
        if not isinstance(data[field], bool):
            return self._error_response(f"{field} must be true or false, not {data[field]!r}", 400)
        return data[field]

    async def _handle_mute(self, request: web.Request) -> web.Response:
        rejection = self._require_json(request)
        if rejection is not None:
            return rejection
        if self._meeting is None:
            return self._error_response("Meeting service not available", 503)
        wanted = await self._state_asked_for(request, "muted")
        if isinstance(wanted, web.Response):
            return wanted
        if self._meeting_state() != "connected":
            return self._error_response("Not in a meeting", 409)
        try:
            # A state asked for is set (a microphone that already is so is left alone); none asked for is the toggle.
            muted = await (self._meeting.toggle_mute() if wanted is None else self._meeting.set_mute(wanted))
        except MeetingControlError as e:
            logger.info(f"The microphone was not changed: {first_line(e)}")
            return self._error_response(first_line(e), 409)
        except Exception as e:  # noqa: BLE001 - a fault in the browser answers with its words, never a 500
            logger.warning(f"Could not change the microphone: {first_line(e)}", exc_info=True)
            return self._error_response(f"Could not change the microphone: {first_line(e)}", 409)
        return web.json_response({"muted": bool(muted), "meeting": self._meeting_block()})   # the block as the control routes answer: the table draws it at once

    async def _handle_camera(self, request: web.Request) -> web.Response:
        rejection = self._require_json(request)
        if rejection is not None:
            return rejection
        if self._meeting is None:
            return self._error_response("Meeting service not available", 503)
        wanted = await self._state_asked_for(request, "on")
        if isinstance(wanted, web.Response):
            return wanted
        if self._meeting_state() != "connected":
            return self._error_response("Not in a meeting", 409)
        try:
            camera_on = await (self._meeting.toggle_camera() if wanted is None else self._meeting.set_camera(wanted))
        except MeetingControlError as e:
            logger.info(f"The camera was not switched: {first_line(e)}")
            return self._error_response(first_line(e), 409)
        except Exception as e:  # noqa: BLE001 - a fault in the browser answers with its words, never a 500
            logger.warning(f"Could not switch the camera: {first_line(e)}", exc_info=True)
            return self._error_response(f"Could not switch the camera: {first_line(e)}", 409)
        return web.json_response({"camera_on": bool(camera_on), "meeting": self._meeting_block()})

    async def _run_control(self, name: str, value: Any) -> web.Response:
        """Carry out a control from the table (or "dialog", an answer to a dialog Zoom drew, or "chat.seen") in the meeting, and answer with the
        meeting as it is afterwards: the block /api/status carries, the same for all three routes. A failure is a 409 in words, never a 500
        and never a stack. The caller has seen that there is a meeting service."""
        if self._meeting_state() != "connected":
            return self._error_response(NOT_IN_A_MEETING, 409)
        what = re.sub(r"[^A-Za-z0-9]+", " ", name).strip() or "the control"     # "host.mute_all" in words, as the error line says it: "host mute all"
        try:
            await self._meeting.control(name, value)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a fault in the browser answers with its words, never a 500
            words = first_line(e)
            if isinstance(e, MeetingControlError) or words == NOT_IN_A_MEETING:
                # The platform's own words (Zoom's refusal, a button not ready, a name the bridge does not know), or the meeting that ended as
                # the tap came in: for the person at the table, and no fault of the agent's.
                logger.info(f"The {what} control was not carried out: {words}")
                return self._error_response(words, 409)
            logger.warning(f"Could not carry out {what}: {words}", exc_info=True)
            return self._error_response(f"Could not carry out {what}: {words}", 409)
        return web.json_response({"meeting": self._meeting_block()})

    async def _handle_control(self, request: web.Request) -> web.Response:
        rejection = self._require_json(request)
        if rejection is not None:
            return rejection
        if self._meeting is None:
            return self._error_response("Meeting service not available", 503)
        data = await self._read_object(request)
        if not data or not isinstance(data.get("name"), str) or not data["name"]:
            return self._error_response("Send a JSON object with name and, for some controls, value", 400)
        return await self._run_control(data["name"], data.get("value"))

    async def _handle_dialog(self, request: web.Request) -> web.Response:
        rejection = self._require_json(request)
        if rejection is not None:
            return rejection
        if self._meeting is None:
            return self._error_response("Meeting service not available", 503)
        data = await self._read_object(request)
        if not data or "id" not in data or not isinstance(data.get("button"), str):
            return self._error_response("Send a JSON object with the dialog's id and the button's label", 400)
        return await self._run_control("dialog", {"id": data["id"], "button": data["button"]})

    async def _handle_chat_seen(self, request: web.Request) -> web.Response:
        rejection = self._require_json(request)
        if rejection is not None:
            return rejection
        if self._meeting is None:
            return self._error_response("Meeting service not available", 503)
        return await self._run_control("chat.seen", None)

    async def _handle_diagnostics(self, request: web.Request) -> web.Response:
        """What the meeting page sees (Zoom's field names, the client's buttons): for pinning real wording from a device."""
        if self._meeting is None:
            return self._error_response("Meeting service not available", 503)
        if self._meeting_state() != "connected":
            return self._error_response("Not in a meeting", 409)
        try:
            return web.json_response(await self._meeting.diagnostics())
        except MeetingControlError as e:
            return self._error_response(first_line(e), 409)
        except Exception as e:  # noqa: BLE001 - the meeting ending as the request came in, say: words, never a 500
            logger.warning(f"Could not read the meeting page: {first_line(e)}", exc_info=True)
            return self._error_response(f"Could not read the meeting page: {first_line(e)}", 409)
