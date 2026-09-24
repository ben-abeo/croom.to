# Room Control Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve a room control page and JSON API from the Croom agent so anyone on the room's network can see today's meetings, join one or join from a pasted link, and control the meeting while it runs.

**Architecture:** A new `croom.control` package holds `ControlService`, a `Service` named `control` built on aiohttp. It exposes `create_app()` for tests and serves three static files (no build step) plus a small JSON API that calls the existing `MeetingService` and `CalendarService` directly. The agent registers it last, after meeting and calendar. Joins and leaves run as background tasks so the API answers immediately and the page follows the state by polling.

**Tech Stack:** Python 3.12 virtualenv at `.venv` (pytest 9, pytest-asyncio 1.4 in `asyncio_mode = "auto"`), aiohttp 3.14 (server and `aiohttp.test_utils.TestClient`), plain HTML, CSS and JavaScript for the page.

**Spec:** `docs/superpowers/specs/2026-09-24-room-control-page-design.md`

## Global Constraints

- Python `>=3.10`; no new runtime dependencies (aiohttp is already declared).
- The service is named `control`; its constructor is `__init__(self, config: Optional[Dict[str, Any]] = None, meeting=None, calendar=None)`; `from_config(config: Config, meeting=None, calendar=None)`.
- API paths and payloads exactly as spec section 4.3; `GET /api/status` is the page's only frequent poll.
- Joins are restricted to links whose platform, per `croom.meeting.providers.base.detect_platform`, is in `MeetingService.get_available_platforms()`.
- No authentication, and no reboot, shutdown, Wi-Fi or settings endpoints (spec 4.6).
- The page uses no external assets and ships as package data (`control/static/*` in `pyproject.toml`).
- Work on branch `room-control` (already created from `main` at 8f294dd, pushed to `origin`). Commit after each task with a `type(scope): summary` message and no attribution trailers.
- Run tests with `.venv/bin/pytest` from the repo root. Baseline on `main`: exactly 87 pre-existing upstream failures. After every task, run the whole suite with `.venv/bin/pytest -q --tb=no -p no:cacheprovider --deselect tests/unit/video/test_v4l2_camera.py::TestV4L2Camera::test_start` (that upstream test can stall the run) and confirm at most 87 failed and every test this plan adds passing.

## Review Focus

1. A second join request while the room's browser is still joining: answered with `409`, never a second join. Pinned by Task 3 `test_join_is_refused_while_a_meeting_is_in_progress`.
2. A pasted link with surrounding whitespace or an upper-case host: accepted and normalised. Pinned by Task 3 `test_join_trims_and_accepts_mixed_case_links`.
3. A calendar event whose link is for a platform the agent does not have configured: shown as not joinable and refused with `400`. Pinned by Task 3 `test_event_for_unconfigured_platform_is_not_joinable`.
4. The meeting service raising during a join: the error shows in the status and a new join is allowed afterwards. Pinned by Task 3 `test_join_failure_is_reported_and_a_new_join_is_allowed`.
5. The configured port already in use: the agent keeps running and the control service logs the problem instead of raising. Pinned by Task 2 `test_port_in_use_is_logged_not_fatal`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/croom/core/config.py` | Adds `ControlConfig` (`enabled`, `host`, `port`) and its `from_dict`/`to_dict` handling. |
| `src/croom/calendar/service.py` | Adds the read-only `connected` property. |
| `src/croom/control/__init__.py` | Package marker exporting `ControlService`. |
| `src/croom/control/service.py` | `ControlService`: app factory, status, calendar events, join/leave/mute/camera, static page, start/stop. |
| `src/croom/control/static/index.html`, `style.css`, `app.js` | The room page. |
| `src/croom/core/agent.py` | Registers `control` after the dashboard client. |
| `pyproject.toml` | Ships `control/static/*`. |
| `tests/unit/control/__init__.py`, `tests/unit/control/test_service.py` | Stub services and API tests. |
| `tests/unit/core/test_config.py`, `tests/unit/calendar/test_service.py`, `tests/unit/core/test_agent.py` | Config, calendar and agent additions. |

---

### Task 1: `control` config section and `CalendarService.connected`

**Files:**
- Modify: `src/croom/core/config.py`
- Modify: `src/croom/calendar/service.py`
- Test: `tests/unit/core/test_config.py`, `tests/unit/calendar/test_service.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `ControlConfig` dataclass with `enabled: bool = True`, `host: str = "0.0.0.0"`, `port: int = 8080`; `Config.control: ControlConfig`; `CalendarService.connected -> bool`. Tasks 2 and 5 use these.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/core/test_config.py`:

```python
class TestControlConfig:
    """The control section configures the room control page (spec 4.1)."""

    def test_defaults(self):
        config = Config()
        assert config.control.enabled is True
        assert config.control.host == "0.0.0.0"
        assert config.control.port == 8080

    def test_round_trips_through_dict(self):
        config = Config.from_dict({"control": {"enabled": False, "host": "127.0.0.1", "port": 9090}})
        assert config.control.enabled is False
        assert config.control.host == "127.0.0.1"
        assert config.control.port == 9090
        assert config.to_dict()["control"] == {"enabled": False, "host": "127.0.0.1", "port": 9090}
        assert Config.from_dict(config.to_dict()).control.port == 9090
```

Append to `tests/unit/calendar/test_service.py`:

```python
class TestCalendarServiceConnected:
    def test_connected_is_false_until_initialized(self):
        service = CalendarService(config={"provider": None})
        assert service.connected is False

    def test_connected_reflects_initialization(self):
        service = CalendarService(config={"provider": "google"})
        service._initialized = True
        assert service.connected is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/core/test_config.py tests/unit/calendar/test_service.py -q -p no:cacheprovider -k "TestControlConfig or TestCalendarServiceConnected"`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'control'` and `AttributeError: 'CalendarService' object has no attribute 'connected'`.

- [ ] **Step 3: Implement**

In `src/croom/core/config.py`, directly before the line `@dataclass` that precedes `class UpdateConfig:` (that is, after `DashboardConfig`), add:

```python
@dataclass
class ControlConfig:
    """Room control page served by the agent on the local network."""
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080


```

In the `Config` dataclass, directly after the line `dashboard: DashboardConfig = field(default_factory=DashboardConfig)` add:

```python
    control: ControlConfig = field(default_factory=ControlConfig)
```

In `Config.from_dict`, directly after the two lines

```python
        if "dashboard" in data:
            config.dashboard = DashboardConfig(**data["dashboard"])
```

add:

```python
        if "control" in data:
            config.control = ControlConfig(**data["control"])
```

In `Config.to_dict`, directly after the `"dashboard": {...},` block (the block ending with `"metrics_interval_seconds": self.dashboard.metrics_interval_seconds,` and its closing `},`) add:

```python
            "control": {
                "enabled": self.control.enabled,
                "host": self.control.host,
                "port": self.control.port,
            },
```

In `src/croom/calendar/service.py`, directly after the `provider` property (the `@property def provider(self)` block), add:

```python
    @property
    def connected(self) -> bool:
        """True once a provider has authenticated and the service polls the calendar."""
        return self._initialized
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/core/test_config.py -q -p no:cacheprovider` and `.venv/bin/pytest tests/unit/calendar/test_service.py -q -p no:cacheprovider -k "TestCalendarServiceConnected or TestCalendarServiceAsService"`
Expected: all pass (the calendar module keeps its pre-existing upstream failures outside the `-k` selection).

- [ ] **Step 5: Run the whole suite** (command in Global Constraints). Expected: at most 87 failed.

- [ ] **Step 6: Commit**

```bash
git add src/croom/core/config.py src/croom/calendar/service.py tests/unit/core/test_config.py tests/unit/calendar/test_service.py
git commit -m "feat(config): add the control section and CalendarService.connected"
```

---

### Task 2: `ControlService` skeleton, status endpoint, start and stop

**Files:**
- Create: `src/croom/control/__init__.py`, `src/croom/control/service.py`, `tests/unit/control/__init__.py`, `tests/unit/control/test_service.py`

**Interfaces:**
- Consumes: `Service`; `Config`, `ControlConfig` (Task 1); `MeetingState`, `MeetingInfo`, `detect_platform` from `croom.meeting.providers.base`; `CalendarService.connected` (Task 1).
- Produces: `ControlService(Service)` named `control`; `create_app() -> aiohttp.web.Application`; `from_config(config, meeting=None, calendar=None)`; `bound_port -> Optional[int]`; `GET /api/status` per spec 4.3; module constants `STATIC_DIR`, `MAX_BODY_BYTES = 4096`, `ZOOM_MEETING_ID`, `IN_PROGRESS_STATES`. Task 3 adds the remaining handlers to this class; Task 4 adds the page; Task 5 wires it.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/control/__init__.py` (empty) and `tests/unit/control/test_service.py`:

```python
"""
Tests for croom.control.service against stub meeting and calendar services.

The stubs are plain classes that behave like the real services' public API
and record what the control service asked them to do.
"""

import asyncio
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from croom.calendar.providers.base import CalendarEvent, MeetingPlatform
from croom.core.config import Config
from croom.core.service import Service
from croom.meeting.providers.base import MeetingInfo, MeetingState, detect_platform
from croom.control.service import ControlService


class StubMeeting:
    """Behaves like MeetingService for the parts the control service uses."""

    def __init__(self, platforms=("zoom", "google_meet")):
        self._platforms = list(platforms)
        self.state = MeetingState.IDLE
        self.current_meeting = None
        self.joins = []
        self.leaves = 0
        self.muted = False
        self.camera_on = True
        self.callbacks = []
        self.fail_with = None

    def get_available_platforms(self):
        return list(self._platforms)

    def add_state_callback(self, callback):
        self.callbacks.append(callback)

    def set_state(self, state):
        self.state = state
        for callback in self.callbacks:
            callback(state)

    async def join_meeting(self, url, display_name=None, camera_on=None, mic_on=None):
        self.joins.append((url, display_name))
        self.set_state(MeetingState.JOINING)
        await asyncio.sleep(0)
        if self.fail_with is not None:
            self.current_meeting = MeetingInfo(
                platform=detect_platform(url) or "unknown", meeting_id="failed",
                meeting_url=url, error_message=str(self.fail_with),
            )
            self.set_state(MeetingState.ERROR)
            raise self.fail_with
        self.current_meeting = MeetingInfo(
            platform=detect_platform(url) or "unknown", meeting_id="123456789",
            meeting_url=url, is_muted=self.muted, is_camera_on=self.camera_on,
        )
        self.set_state(MeetingState.CONNECTED)
        return self.current_meeting

    async def leave_meeting(self):
        self.leaves += 1
        self.current_meeting = None
        self.set_state(MeetingState.IDLE)

    async def toggle_mute(self):
        self.muted = not self.muted
        if self.current_meeting:
            self.current_meeting.is_muted = self.muted
        return self.muted

    async def toggle_camera(self):
        self.camera_on = not self.camera_on
        if self.current_meeting:
            self.current_meeting.is_camera_on = self.camera_on
        return self.camera_on


class StubCalendar:
    """Behaves like CalendarService for the parts the control service uses."""

    def __init__(self, events=(), connected=True, provider_name="google"):
        self.events = list(events)
        self.connected = connected
        self.provider = SimpleNamespace(name=provider_name) if provider_name else None

    @property
    def next_meeting(self):
        now = datetime.now(timezone.utc)
        upcoming = [e for e in self.events if e.start_time > now]
        return upcoming[0] if upcoming else None

    def get_current_meeting(self):
        for event in self.events:
            if event.is_happening_now() and event.meeting_url:
                return event
        return None

    def get_event_by_id(self, event_id):
        return next((e for e in self.events if e.id == event_id), None)


def event(event_id, title, starts_in_minutes, duration=30, url="https://zoom.us/j/98765432100?pwd=abc",
          platform=MeetingPlatform.ZOOM):
    start = datetime.now(timezone.utc) + timedelta(minutes=starts_in_minutes)
    return CalendarEvent(id=event_id, title=title, start_time=start, end_time=start + timedelta(minutes=duration),
                         meeting_url=url, meeting_platform=platform)


def make_service(meeting=None, calendar=None, **config):
    settings = {"host": "127.0.0.1", "port": 0, "room_name": "Lab", "room_location": "2nd floor"}
    settings.update(config)
    return ControlService(config=settings, meeting=meeting, calendar=calendar)


@pytest.fixture
async def client_factory():
    clients = []

    async def factory(service):
        client = TestClient(TestServer(service.create_app()))
        await client.start_server()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        await client.close()


async def wait_until(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


class TestControlServiceBasics:
    def test_is_a_service_named_control(self):
        service = make_service()
        assert isinstance(service, Service)
        assert service.name == "control"

    def test_from_config_maps_room_and_listener(self):
        config = Config()
        config.room.name = "Board Room"
        config.room.location = "HQ 3rd floor"
        config.control.host = "127.0.0.1"
        config.control.port = 9090
        meeting, calendar = StubMeeting(), StubCalendar()
        service = ControlService.from_config(config, meeting=meeting, calendar=calendar)
        assert service.config["host"] == "127.0.0.1"
        assert service.config["port"] == 9090
        assert service.config["room_name"] == "Board Room"
        assert service.config["room_location"] == "HQ 3rd floor"
        assert service.config["static_dir"].endswith("croom/control/static")
        assert service._meeting is meeting
        assert service._calendar is calendar

    async def test_start_binds_an_ephemeral_port_and_stop_releases_it(self):
        service = make_service(meeting=StubMeeting())
        await service.start()
        try:
            assert service.bound_port is not None and service.bound_port > 0
            with socket.create_connection(("127.0.0.1", service.bound_port), timeout=1):
                pass
        finally:
            await service.stop()
        assert service.bound_port is None

    async def test_port_in_use_is_logged_not_fatal(self, caplog):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            service = make_service(port=port)
            await service.start()
            assert service.bound_port is None
            assert "cannot listen" in caplog.text
            await service.stop()
        finally:
            blocker.close()


class TestStatus:
    async def test_idle_status_without_services(self, client_factory):
        client = await client_factory(make_service())
        resp = await client.get("/api/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["room"] == {"name": "Lab", "location": "2nd floor"}
        assert data["platforms"] == []
        assert data["meeting"] == {
            "state": "idle", "platform": None, "meeting_id": None, "title": "", "url": None,
            "joined_at": None, "muted": False, "camera_on": False, "error": None,
        }
        assert data["calendar"] == {"connected": False, "provider": None, "current": None, "next": None}
        datetime.fromisoformat(data["server_time"])

    async def test_status_reports_platforms_and_calendar(self, client_factory):
        nxt = event("e1", "Design review", starts_in_minutes=20)
        calendar = StubCalendar(events=[nxt])
        client = await client_factory(make_service(meeting=StubMeeting(), calendar=calendar))
        data = await (await client.get("/api/status")).json()
        assert data["platforms"] == ["zoom", "google_meet"]
        assert data["calendar"]["connected"] is True
        assert data["calendar"]["provider"] == "google"
        assert data["calendar"]["current"] is None
        assert data["calendar"]["next"]["id"] == "e1"
        assert data["calendar"]["next"]["joinable"] is True

    async def test_status_reports_a_connected_meeting(self, client_factory):
        meeting = StubMeeting()
        service = make_service(meeting=meeting)
        await service.start()
        try:
            client = await client_factory(service)
            await meeting.join_meeting("https://zoom.us/j/98765432100", display_name="Lab")
            data = await (await client.get("/api/status")).json()
            assert data["meeting"]["state"] == "connected"
            assert data["meeting"]["platform"] == "zoom"
            assert data["meeting"]["meeting_id"] == "123456789"
            assert data["meeting"]["url"] == "https://zoom.us/j/98765432100"
            assert data["meeting"]["joined_at"] is not None
            assert data["meeting"]["muted"] is False
            assert data["meeting"]["camera_on"] is True
            await meeting.leave_meeting()
            data = await (await client.get("/api/status")).json()
            assert data["meeting"]["state"] == "idle"
            assert data["meeting"]["joined_at"] is None
        finally:
            await service.stop()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/control/test_service.py -q -p no:cacheprovider`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'croom.control'`.

- [ ] **Step 3: Implement the package and the service**

Create `src/croom/control/__init__.py`:

```python
"""
Room control: the local web page and JSON API served by the agent.
"""

from croom.control.service import ControlService

__all__ = ["ControlService"]
```

Create `src/croom/control/service.py`:

```python
"""
Room control page and API (spec docs/superpowers/specs/2026-09-24-room-control-page-design.md).

Serves the room page and a small JSON API on the local network and drives the
meeting and calendar services on behalf of whoever is in the room.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import web

from croom.core.config import Config
from croom.core.service import Service
from croom.meeting.providers.base import MeetingState, detect_platform

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 4096
ZOOM_MEETING_ID = re.compile(r"^\d{9,11}$")
IN_PROGRESS_STATES = {"joining", "in_lobby", "connected", "leaving"}


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
        app = web.Application(client_max_size=MAX_BODY_BYTES)
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/api/status", self._handle_status)
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
        """The configured platform a link belongs to, or None."""
        if not url or self._meeting is None:
            return None
        platform = detect_platform(url)
        if platform is None or platform not in self._meeting.get_available_platforms():
            return None
        return platform

    def _event_dict(self, event) -> Dict[str, Any]:
        data = event.to_dict()
        data["joinable"] = self._platform_for(event.meeting_url) is not None
        return data

    def _calendar_status(self) -> Dict[str, Any]:
        if self._calendar is None:
            return {"connected": False, "provider": None, "current": None, "next": None}
        provider = self._calendar.provider
        current = self._calendar.get_current_meeting()
        upcoming = self._calendar.next_meeting
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/control/test_service.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Run the whole suite** (command in Global Constraints). Expected: at most 87 failed.

- [ ] **Step 6: Commit**

```bash
git add src/croom/control tests/unit/control
git commit -m "feat(control): add ControlService with the status endpoint"
```

---

### Task 3: Join, leave, mute, camera and calendar events

**Files:**
- Modify: `src/croom/control/service.py` (`create_app`, new handlers)
- Test: `tests/unit/control/test_service.py`

**Interfaces:**
- Consumes: Task 2's class and stubs.
- Produces: `GET /api/calendar/events`, `POST /api/meeting/join`, `POST /api/meeting/leave`, `POST /api/meeting/mute`, `POST /api/meeting/camera` per spec 4.3 and 4.4. Task 4's page calls these.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/control/test_service.py`:

```python
class TestCalendarEvents:
    async def test_events_without_calendar(self, client_factory):
        client = await client_factory(make_service(meeting=StubMeeting()))
        assert await (await client.get("/api/calendar/events")).json() == {"events": []}

    async def test_events_carry_the_joinable_flag(self, client_factory):
        events = [
            event("e1", "Design review", starts_in_minutes=20),
            event("e2", "Vendor call", starts_in_minutes=90, url="https://teams.microsoft.com/l/meetup-join/abc",
                  platform=MeetingPlatform.MICROSOFT_TEAMS),
            event("e3", "Lunch", starts_in_minutes=150, url=None, platform=MeetingPlatform.UNKNOWN),
        ]
        client = await client_factory(make_service(meeting=StubMeeting(), calendar=StubCalendar(events=events)))
        data = await (await client.get("/api/calendar/events")).json()
        assert [e["id"] for e in data["events"]] == ["e1", "e2", "e3"]
        assert [e["joinable"] for e in data["events"]] == [True, False, False]
        assert data["events"][0]["title"] == "Design review"
        assert data["events"][0]["meeting_platform"] == "zoom"


class TestJoin:
    async def test_join_by_link_starts_in_background(self, client_factory):
        meeting = StubMeeting()
        service = make_service(meeting=meeting)
        client = await client_factory(service)
        resp = await client.post("/api/meeting/join", json={"url": "https://zoom.us/j/98765432100?pwd=abc"})
        assert resp.status == 202
        assert await resp.json() == {"state": "joining", "url": "https://zoom.us/j/98765432100?pwd=abc"}
        await wait_until(lambda: meeting.state == MeetingState.CONNECTED)
        assert meeting.joins == [("https://zoom.us/j/98765432100?pwd=abc", "Lab")]
        data = await (await client.get("/api/status")).json()
        assert data["meeting"]["state"] == "connected"
        assert data["meeting"]["title"] == ""

    async def test_join_trims_and_accepts_mixed_case_links(self, client_factory):
        meeting = StubMeeting()
        client = await client_factory(make_service(meeting=meeting))
        resp = await client.post("/api/meeting/join", json={"url": "   HTTPS://ZOOM.us/j/98765432100  "})
        assert resp.status == 202
        assert (await resp.json())["url"] == "HTTPS://ZOOM.us/j/98765432100"
        await wait_until(lambda: meeting.joins)

    async def test_bare_zoom_meeting_id_becomes_a_link(self, client_factory):
        meeting = StubMeeting()
        client = await client_factory(make_service(meeting=meeting))
        resp = await client.post("/api/meeting/join", json={"url": "98765432100"})
        assert resp.status == 202
        assert (await resp.json())["url"] == "https://zoom.us/j/98765432100"
        await wait_until(lambda: meeting.joins)
        assert meeting.joins[0][0] == "https://zoom.us/j/98765432100"

    async def test_join_by_event_uses_its_link_and_title(self, client_factory):
        meeting = StubMeeting()
        calendar = StubCalendar(events=[event("e1", "Design review", starts_in_minutes=5)])
        client = await client_factory(make_service(meeting=meeting, calendar=calendar))
        resp = await client.post("/api/meeting/join", json={"event_id": "e1"})
        assert resp.status == 202
        await wait_until(lambda: meeting.state == MeetingState.CONNECTED)
        assert meeting.joins[0][0] == "https://zoom.us/j/98765432100?pwd=abc"
        data = await (await client.get("/api/status")).json()
        assert data["meeting"]["title"] == "Design review"

    @pytest.mark.parametrize(
        "body, message",
        [
            ({}, "Meeting link required"),
            ({"url": "https://example.com/meeting"}, "Unsupported meeting link"),
            ({"event_id": "missing"}, "Unknown calendar event"),
            ({"event_id": "nolink"}, "That meeting has no video link"),
        ],
    )
    async def test_join_rejects_bad_requests(self, client_factory, body, message):
        calendar = StubCalendar(events=[event("nolink", "Lunch", starts_in_minutes=5, url=None,
                                              platform=MeetingPlatform.UNKNOWN)])
        client = await client_factory(make_service(meeting=StubMeeting(), calendar=calendar))
        resp = await client.post("/api/meeting/join", json=body)
        assert resp.status == 400
        assert (await resp.json())["error"] == message

    async def test_join_rejects_non_object_bodies(self, client_factory):
        client = await client_factory(make_service(meeting=StubMeeting()))
        resp = await client.post("/api/meeting/join", data="not json", headers={"Content-Type": "application/json"})
        assert resp.status == 400
        resp = await client.post("/api/meeting/join", json=["a", "list"])
        assert resp.status == 400

    async def test_event_for_unconfigured_platform_is_not_joinable(self, client_factory):
        meeting = StubMeeting(platforms=("zoom",))
        calendar = StubCalendar(events=[event("g1", "Meet call", starts_in_minutes=5,
                                              url="https://meet.google.com/abc-defg-hij",
                                              platform=MeetingPlatform.GOOGLE_MEET)])
        client = await client_factory(make_service(meeting=meeting, calendar=calendar))
        events = await (await client.get("/api/calendar/events")).json()
        assert events["events"][0]["joinable"] is False
        resp = await client.post("/api/meeting/join", json={"event_id": "g1"})
        assert resp.status == 400
        assert meeting.joins == []

    async def test_join_is_refused_while_a_meeting_is_in_progress(self, client_factory):
        meeting = StubMeeting()
        client = await client_factory(make_service(meeting=meeting))
        for state in (MeetingState.JOINING, MeetingState.IN_LOBBY, MeetingState.CONNECTED, MeetingState.LEAVING):
            meeting.set_state(state)
            resp = await client.post("/api/meeting/join", json={"url": "https://zoom.us/j/98765432100"})
            assert resp.status == 409, state
            assert (await resp.json())["error"] == "A meeting is already in progress"
        assert meeting.joins == []

    async def test_join_without_meeting_service(self, client_factory):
        client = await client_factory(make_service())
        resp = await client.post("/api/meeting/join", json={"url": "https://zoom.us/j/98765432100"})
        assert resp.status == 503

    async def test_join_failure_is_reported_and_a_new_join_is_allowed(self, client_factory):
        meeting = StubMeeting()
        meeting.fail_with = RuntimeError("Join button not found")
        client = await client_factory(make_service(meeting=meeting))
        resp = await client.post("/api/meeting/join", json={"url": "https://zoom.us/j/98765432100"})
        assert resp.status == 202
        await wait_until(lambda: meeting.state == MeetingState.ERROR)
        data = await (await client.get("/api/status")).json()
        assert data["meeting"]["state"] == "error"
        assert data["meeting"]["error"] == "Join button not found"
        meeting.fail_with = None
        resp = await client.post("/api/meeting/join", json={"url": "https://zoom.us/j/98765432100"})
        assert resp.status == 202
        await wait_until(lambda: meeting.state == MeetingState.CONNECTED)
        data = await (await client.get("/api/status")).json()
        assert data["meeting"]["error"] is None


class TestLeaveMuteCamera:
    async def test_leave_when_idle_is_a_conflict(self, client_factory):
        client = await client_factory(make_service(meeting=StubMeeting()))
        resp = await client.post("/api/meeting/leave")
        assert resp.status == 409
        assert (await resp.json())["error"] == "No meeting to leave"

    async def test_leave_runs_in_background_and_clears_the_title(self, client_factory):
        meeting = StubMeeting()
        calendar = StubCalendar(events=[event("e1", "Design review", starts_in_minutes=5)])
        client = await client_factory(make_service(meeting=meeting, calendar=calendar))
        await client.post("/api/meeting/join", json={"event_id": "e1"})
        await wait_until(lambda: meeting.state == MeetingState.CONNECTED)
        resp = await client.post("/api/meeting/leave")
        assert resp.status == 202
        assert await resp.json() == {"state": "leaving"}
        await wait_until(lambda: meeting.leaves == 1)
        data = await (await client.get("/api/status")).json()
        assert data["meeting"]["state"] == "idle"
        assert data["meeting"]["title"] == ""

    async def test_leave_clears_an_error_state(self, client_factory):
        meeting = StubMeeting()
        meeting.fail_with = RuntimeError("boom")
        client = await client_factory(make_service(meeting=meeting))
        await client.post("/api/meeting/join", json={"url": "https://zoom.us/j/98765432100"})
        await wait_until(lambda: meeting.state == MeetingState.ERROR)
        resp = await client.post("/api/meeting/leave")
        assert resp.status == 202
        await wait_until(lambda: meeting.state == MeetingState.IDLE)
        data = await (await client.get("/api/status")).json()
        assert data["meeting"]["error"] is None

    async def test_mute_and_camera_require_a_connected_meeting(self, client_factory):
        client = await client_factory(make_service(meeting=StubMeeting()))
        assert (await client.post("/api/meeting/mute")).status == 409
        assert (await client.post("/api/meeting/camera")).status == 409

    async def test_mute_and_camera_toggle_and_report(self, client_factory):
        meeting = StubMeeting()
        client = await client_factory(make_service(meeting=meeting))
        await client.post("/api/meeting/join", json={"url": "https://zoom.us/j/98765432100"})
        await wait_until(lambda: meeting.state == MeetingState.CONNECTED)
        assert await (await client.post("/api/meeting/mute")).json() == {"muted": True}
        assert await (await client.post("/api/meeting/camera")).json() == {"camera_on": False}
        data = await (await client.get("/api/status")).json()
        assert data["meeting"]["muted"] is True
        assert data["meeting"]["camera_on"] is False
        assert await (await client.post("/api/meeting/mute")).json() == {"muted": False}

    async def test_without_meeting_service_controls_are_unavailable(self, client_factory):
        client = await client_factory(make_service())
        for path in ("/api/meeting/leave", "/api/meeting/mute", "/api/meeting/camera"):
            assert (await client.post(path)).status == 503, path
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/control/test_service.py -q -p no:cacheprovider -k "TestCalendarEvents or TestJoin or TestLeaveMuteCamera"`
Expected: FAIL with `404` statuses (routes do not exist yet), for example `assert 404 == 202`.

- [ ] **Step 3: Implement the handlers**

In `src/croom/control/service.py`, replace the body of `create_app` with:

```python
    def create_app(self) -> web.Application:
        """The aiohttp application; tests drive it without a listener."""
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
```

Append these methods to the class, after `_handle_status`:

```python
    async def _handle_events(self, request: web.Request) -> web.Response:
        if self._calendar is None:
            return web.json_response({"events": []})
        return web.json_response({"events": [self._event_dict(e) for e in self._calendar.events]})

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
    def _error(message: str, status: int) -> web.Response:
        return web.json_response({"error": message}, status=status)

    async def _handle_join(self, request: web.Request) -> web.Response:
        if self._meeting is None:
            return self._error("Meeting service not available", 503)
        data = await self._read_object(request)
        if data is None:
            return self._error("Send a JSON object with url or event_id", 400)
        title = ""
        url = str(data.get("url") or "").strip()
        event_id = str(data.get("event_id") or "").strip()
        if event_id:
            event = self._calendar.get_event_by_id(event_id) if self._calendar is not None else None
            if event is None:
                return self._error("Unknown calendar event", 400)
            if not event.meeting_url:
                return self._error("That meeting has no video link", 400)
            url = event.meeting_url
            title = event.title
        if not url:
            return self._error("Meeting link required", 400)
        if ZOOM_MEETING_ID.match(url):
            url = f"https://zoom.us/j/{url}"
        if self._platform_for(url) is None:
            return self._error("Unsupported meeting link", 400)
        if self._meeting_state() in IN_PROGRESS_STATES:
            return self._error("A meeting is already in progress", 409)
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
        if self._meeting is None:
            return self._error("Meeting service not available", 503)
        if self._meeting_state() == "idle":
            return self._error("No meeting to leave", 409)
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
        if self._meeting is None:
            return self._error("Meeting service not available", 503)
        if self._meeting_state() != "connected":
            return self._error("Not in a meeting", 409)
        muted = await self._meeting.toggle_mute()
        return web.json_response({"muted": bool(muted)})

    async def _handle_camera(self, request: web.Request) -> web.Response:
        if self._meeting is None:
            return self._error("Meeting service not available", 503)
        if self._meeting_state() != "connected":
            return self._error("Not in a meeting", 409)
        camera_on = await self._meeting.toggle_camera()
        return web.json_response({"camera_on": bool(camera_on)})
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/control/test_service.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Run the whole suite** (command in Global Constraints). Expected: at most 87 failed.

- [ ] **Step 6: Commit**

```bash
git add src/croom/control/service.py tests/unit/control/test_service.py
git commit -m "feat(control): join, leave, mute, camera and calendar endpoints"
```

---

### Task 4: The room page

**Files:**
- Create: `src/croom/control/static/index.html`, `src/croom/control/static/style.css`, `src/croom/control/static/app.js`
- Modify: `pyproject.toml`
- Test: `tests/unit/control/test_service.py`

**Interfaces:**
- Consumes: the API from Tasks 2 and 3.
- Produces: `GET /` and `GET /static/...` serving the page; package data so the files ship. Task 5's acceptance uses the page.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/control/test_service.py`:

```python
class TestPage:
    async def test_index_is_served_uncached(self, client_factory):
        client = await client_factory(make_service())
        resp = await client.get("/")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/html")
        assert resp.headers["Cache-Control"] == "no-cache"
        body = await resp.text()
        assert "<title>Croom room</title>" in body
        assert 'src="/static/app.js"' in body
        assert 'href="/static/style.css"' in body

    async def test_assets_are_served(self, client_factory):
        client = await client_factory(make_service())
        assert (await client.get("/static/app.js")).status == 200
        assert (await client.get("/static/style.css")).status == 200

    async def test_missing_assets_explain_themselves(self, client_factory, tmp_path):
        client = await client_factory(make_service(static_dir=str(tmp_path)))
        resp = await client.get("/")
        assert resp.status == 500
        assert "assets are missing" in await resp.text()

    def test_static_files_are_package_data(self):
        text = open("pyproject.toml", encoding="utf-8").read()
        assert 'control/static/*' in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/control/test_service.py -q -p no:cacheprovider -k TestPage`
Expected: FAIL: `test_index_is_served_uncached` and `test_assets_are_served` with `500`/`404` (no files), `test_static_files_are_package_data` with an assertion error. `test_missing_assets_explain_themselves` already passes.

- [ ] **Step 3: Create the page**

Create `src/croom/control/static/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Croom room</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body data-state="loading">
<main class="screen">
  <header class="top">
    <div class="room">
      <h1 id="room-name">Room</h1>
      <p id="room-location" class="location"></p>
    </div>
    <p id="clock" class="clock"></p>
  </header>

  <section class="now" aria-live="polite">
    <p id="headline" class="headline">Connecting to the room</p>
    <p id="detail" class="detail"></p>
    <p id="message" class="message" role="status"></p>
    <div id="actions" class="actions"></div>
  </section>

  <section class="schedule">
    <h2>Today</h2>
    <ul id="events" class="events"></ul>
    <p id="calendar-note" class="note"></p>
  </section>

  <section class="link">
    <h2>Join with a link</h2>
    <form id="link-form" class="link-form">
      <label class="visually-hidden" for="link-input">Meeting link or Zoom meeting ID</label>
      <input id="link-input" type="text" inputmode="url" autocomplete="off" spellcheck="false"
             placeholder="Paste a meeting link or a Zoom meeting ID">
      <button type="submit" class="button primary">Join</button>
    </form>
  </section>
</main>
<script src="/static/app.js"></script>
</body>
</html>
```

Create `src/croom/control/static/style.css`:

```css
:root {
  --ink: #16202b;
  --ink-raised: #22303e;
  --paper: #f2efe8;
  --mist: #9aa7b5;
  --free: #2e8b6e;
  --soon: #d9a441;
  --busy: #c4524e;
  --joining: #6d6ab8;
  --focus: #8ec5ff;
  --radius: 14px;
  --gutter: clamp(20px, 4vw, 56px);
}

* { box-sizing: border-box; }

html, body { height: 100%; }

body {
  margin: 0;
  background: var(--ink);
  color: var(--paper);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans", "DejaVu Sans", sans-serif;
  font-size: clamp(17px, 1.2vw, 22px);
  line-height: 1.4;
  transition: background-color 600ms ease;
}

body[data-state="free"]    { background: #15302a; }
body[data-state="soon"]    { background: #3a2e12; }
body[data-state="joining"] { background: #2b2a44; }
body[data-state="meeting"] { background: #3e1f1f; }
body[data-state="error"]   { background: #3e1f1f; }
body[data-state="offline"] { background: var(--ink); }

@media (prefers-reduced-motion: reduce) {
  body { transition: none; }
}

.screen {
  min-height: 100%;
  padding: var(--gutter);
  display: grid;
  gap: clamp(28px, 4vh, 48px);
  grid-template-areas: "top" "now" "link" "schedule";
  align-content: start;
}

@media (min-width: 1024px) {
  .screen {
    grid-template-columns: 3fr 2fr;
    grid-template-areas:
      "top top"
      "now schedule"
      "link schedule";
    column-gap: clamp(40px, 5vw, 96px);
  }
}

.top { grid-area: top; display: flex; justify-content: space-between; align-items: flex-start; gap: 24px; }
.now { grid-area: now; }
.schedule { grid-area: schedule; }
.link { grid-area: link; }

h1 { margin: 0; font-size: clamp(1.4rem, 2.4vw, 2.2rem); font-weight: 600; letter-spacing: -0.01em; }
h2 { margin: 0 0 12px; font-size: 1rem; font-weight: 600; color: var(--mist); }
.location { margin: 4px 0 0; color: var(--mist); }

.clock {
  margin: 0;
  font-size: clamp(2.4rem, 6vw, 5.5rem);
  font-weight: 300;
  line-height: 1;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.headline {
  margin: 0;
  font-size: clamp(1.9rem, 5vw, 4.4rem);
  font-weight: 600;
  line-height: 1.08;
  letter-spacing: -0.02em;
  max-width: 18ch;
  overflow-wrap: anywhere;
}

.detail { margin: 14px 0 0; color: var(--paper); opacity: 0.85; max-width: 46ch; }
.message { margin: 14px 0 0; min-height: 1.4em; color: #ffd7d5; max-width: 46ch; }
.message:empty { display: none; }

.actions { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 24px; }

.button {
  min-height: 56px;
  padding: 0 28px;
  border-radius: var(--radius);
  border: 2px solid transparent;
  font: inherit;
  font-size: 1.125rem;
  font-weight: 600;
  color: var(--paper);
  background: var(--ink-raised);
  cursor: pointer;
  touch-action: manipulation;
}
.button.primary { background: var(--paper); color: var(--ink); }
.button.danger { background: var(--busy); }
.button.quiet { background: transparent; border-color: var(--mist); }
.button:disabled { opacity: 0.45; cursor: default; }
.button:focus-visible, input:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }

.events { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }
.event {
  display: grid;
  grid-template-columns: 6.5ch 1fr auto;
  gap: 16px;
  align-items: center;
  padding: 12px 0;
}
.event.past { opacity: 0.45; }
.event.now { font-weight: 600; }
.event time { font-variant-numeric: tabular-nums; color: var(--mist); }
.event .title { margin: 0; overflow-wrap: anywhere; }
.event .platform { margin: 2px 0 0; color: var(--mist); font-size: 0.9em; }
.event .button { min-height: 48px; padding: 0 20px; }

.note { margin: 12px 0 0; color: var(--mist); }
.note:empty { display: none; }

.link-form { display: flex; flex-wrap: wrap; gap: 12px; }
.link-form input {
  flex: 1 1 260px;
  min-height: 56px;
  padding: 0 18px;
  border-radius: var(--radius);
  border: 2px solid var(--mist);
  background: var(--ink-raised);
  color: var(--paper);
  font: inherit;
}

.visually-hidden {
  position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
}
```

Create `src/croom/control/static/app.js`:

```javascript
(function () {
  "use strict";

  const JOIN_WINDOW_MS = 10 * 60 * 1000;
  const STATUS_EVERY_MS = 2000;
  const EVENTS_EVERY_MS = 30000;
  const CONFIRM_MS = 5000;

  const el = (id) => document.getElementById(id);
  const model = { status: null, events: [], offline: false, busy: false, error: "", confirmLeave: false };
  let confirmTimer = null;

  const platformNames = { zoom: "Zoom", google_meet: "Google Meet", teams: "Teams", webex: "Webex" };
  const platformName = (key) => platformNames[key] || key || "";
  const fmtTime = (d) => d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const fmtRange = (a, b) => fmtTime(a) + " to " + fmtTime(b);
  const minutesUntil = (d) => Math.max(0, Math.round((d - Date.now()) / 60000));
  const plural = (n, word) => n + " " + word + (n === 1 ? "" : "s");

  function elapsedSince(iso) {
    const seconds = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    const mm = String(m).padStart(h ? 2 : 1, "0");
    const ss = String(s).padStart(2, "0");
    return (h ? h + ":" : "") + mm + ":" + ss;
  }

  async function api(path, options) {
    const init = Object.assign({ headers: { "Content-Type": "application/json" } }, options || {});
    const response = await fetch(path, init);
    let data = {};
    try { data = await response.json(); } catch (e) { data = {}; }
    if (!response.ok) throw new Error(data.error || "Request failed (" + response.status + ")");
    return data;
  }

  async function refreshStatus() {
    try {
      model.status = await api("/api/status");
      model.offline = false;
    } catch (e) {
      model.offline = true;
    }
    render();
  }

  async function refreshEvents() {
    try {
      model.events = (await api("/api/calendar/events")).events || [];
    } catch (e) {
      // keep the last list
    }
    render();
  }

  async function act(request) {
    if (model.busy) return;
    model.busy = true;
    model.error = "";
    render();
    try {
      await request();
    } catch (e) {
      model.error = e.message;
    }
    model.busy = false;
    await refreshStatus();
  }

  const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });
  const joinLink = (url) => act(() => post("/api/meeting/join", { url: url }));
  const joinEvent = (id) => act(() => post("/api/meeting/join", { event_id: id }));
  const leave = () => act(() => post("/api/meeting/leave"));
  const toggle = (kind) => act(() => post("/api/meeting/" + kind));

  function button(label, className, onClick, disabled) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "button " + className;
    b.textContent = label;
    b.disabled = Boolean(disabled) || model.busy;
    b.addEventListener("click", onClick);
    return b;
  }

  function leaveButton() {
    if (model.confirmLeave) {
      return button("Tap again to leave", "danger", () => {
        clearTimeout(confirmTimer);
        model.confirmLeave = false;
        leave();
      });
    }
    return button("Leave", "danger", () => {
      model.confirmLeave = true;
      clearTimeout(confirmTimer);
      confirmTimer = setTimeout(() => { model.confirmLeave = false; render(); }, CONFIRM_MS);
      render();
    });
  }

  function joinWindow(ev) {
    const start = new Date(ev.start_time).getTime();
    const end = new Date(ev.end_time).getTime();
    const now = Date.now();
    return { open: now >= start - JOIN_WINDOW_MS && now < end, opensAt: new Date(start - JOIN_WINDOW_MS), past: now >= end };
  }

  function render() {
    const body = document.body;
    const actions = el("actions");
    actions.replaceChildren();
    el("message").textContent = model.error;

    if (model.offline || !model.status) {
      body.dataset.state = "offline";
      el("headline").textContent = "Can't reach the room";
      el("detail").textContent = "Check that the Croom agent is running, then this page will reconnect on its own.";
      return;
    }

    const s = model.status;
    el("room-name").textContent = s.room.name;
    el("room-location").textContent = s.room.location;
    const m = s.meeting;
    const cal = s.calendar;
    const label = m.title || (m.platform ? platformName(m.platform) + " meeting" : "the meeting");

    if (m.state === "joining" || m.state === "in_lobby" || m.state === "leaving") {
      body.dataset.state = "joining";
      el("headline").textContent = m.state === "leaving" ? "Leaving" : "Joining " + label;
      el("detail").textContent = m.state === "in_lobby" ? "Waiting for the host to let the room in." : "The room's screen is connecting.";
      if (m.state !== "leaving") actions.append(button("Cancel", "quiet", leave));
    } else if (m.state === "connected") {
      body.dataset.state = "meeting";
      el("headline").textContent = "In a meeting";
      el("detail").textContent = (m.title ? m.title + ", " : "") + platformName(m.platform) + (m.joined_at ? ", " + elapsedSince(m.joined_at) : "");
      actions.append(
        button(m.muted ? "Unmute" : "Mute", "", () => toggle("mute")),
        button(m.camera_on ? "Turn camera off" : "Turn camera on", "", () => toggle("camera")),
        leaveButton()
      );
    } else if (m.state === "error") {
      body.dataset.state = "error";
      el("headline").textContent = "Couldn't join " + label;
      el("detail").textContent = m.error || "The room's screen could not join. Try again, or join from a different link.";
      actions.append(button("Dismiss", "quiet", leave));
    } else {
      renderIdle(cal);
    }

    renderEvents(cal);
  }

  function renderIdle(cal) {
    const body = document.body;
    const current = cal.current;
    const next = cal.next;

    if (current) {
      body.dataset.state = "soon";
      el("headline").textContent = current.title + " is happening now";
      el("detail").textContent = fmtRange(new Date(current.start_time), new Date(current.end_time)) + (current.joinable ? ", " + platformName(current.meeting_platform) : "");
      if (current.joinable) el("actions").append(button("Join now", "primary", () => joinEvent(current.id)));
      return;
    }

    if (next) {
      const start = new Date(next.start_time);
      const window = joinWindow(next);
      const minutes = minutesUntil(start);
      if (window.open) {
        body.dataset.state = "soon";
        el("headline").textContent = minutes === 0 ? next.title + " is starting" : next.title + " starts in " + plural(minutes, "minute");
      } else {
        body.dataset.state = "free";
        el("headline").textContent = "Free until " + fmtTime(start);
      }
      el("detail").textContent = "Next: " + next.title + ", " + fmtRange(start, new Date(next.end_time)) + (next.joinable ? ", " + platformName(next.meeting_platform) : "");
      if (next.joinable) {
        const b = button(window.open ? "Join now" : "Join opens at " + fmtTime(window.opensAt), "primary", () => joinEvent(next.id), !window.open);
        el("actions").append(b);
      }
      return;
    }

    body.dataset.state = "free";
    el("headline").textContent = cal.connected ? "Free for the rest of the day" : "Free";
    el("detail").textContent = cal.connected ? "Nothing else is booked in here today." : "No calendar is connected. Join with a link below.";
  }

  function renderEvents(cal) {
    const list = el("events");
    const note = el("calendar-note");
    list.replaceChildren();
    if (!cal.connected) {
      note.textContent = "Calendar not connected.";
      return;
    }
    if (model.events.length === 0) {
      note.textContent = "Nothing scheduled today.";
      return;
    }
    note.textContent = "";
    for (const ev of model.events) {
      const window = joinWindow(ev);
      const li = document.createElement("li");
      li.className = "event" + (window.past ? " past" : "") + (cal.current && cal.current.id === ev.id ? " now" : "");
      const time = document.createElement("time");
      time.dateTime = ev.start_time;
      time.textContent = fmtTime(new Date(ev.start_time));
      const text = document.createElement("div");
      const title = document.createElement("p");
      title.className = "title";
      title.textContent = ev.title;
      text.append(title);
      if (ev.joinable) {
        const platform = document.createElement("p");
        platform.className = "platform";
        platform.textContent = platformName(ev.meeting_platform);
        text.append(platform);
      }
      li.append(time, text);
      if (ev.joinable && window.open && (!model.status || model.status.meeting.state === "idle" || model.status.meeting.state === "error")) {
        li.append(button("Join", "primary", () => joinEvent(ev.id)));
      }
      list.append(li);
    }
  }

  function tick() {
    el("clock").textContent = fmtTime(new Date());
    if (model.status && model.status.meeting.state === "connected") render();
  }

  el("link-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = el("link-input");
    const value = input.value.trim();
    if (!value) return;
    joinLink(value).then(() => { if (!model.error) input.value = ""; });
  });

  tick();
  setInterval(tick, 1000);
  refreshStatus();
  refreshEvents();
  setInterval(refreshStatus, STATUS_EVERY_MS);
  setInterval(refreshEvents, EVENTS_EVERY_MS);
})();
```

In `pyproject.toml`, change

```toml
[tool.setuptools.package-data]
croom = ["py.typed"]
```

to

```toml
[tool.setuptools.package-data]
croom = ["py.typed", "control/static/*"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/control/test_service.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Look at the page**

Start a throwaway server on port 8765 with stub data and open it in a browser, or capture it headlessly with the Playwright Chromium already in the venv:

```bash
.venv/bin/python - <<'EOF'
import asyncio
from datetime import datetime, timedelta, timezone
from croom.control.service import ControlService
from tests.unit.control.test_service import StubMeeting, StubCalendar, event

async def main():
    calendar = StubCalendar(events=[event("e1", "Design review", 25), event("e2", "Vendor call", 120)])
    service = ControlService(config={"host": "127.0.0.1", "port": 8765, "room_name": "WSL Lab", "room_location": "2nd floor"},
                             meeting=StubMeeting(), calendar=calendar)
    await service.start()
    print("open http://127.0.0.1:8765/  (Ctrl-C to stop)")
    try:
        await asyncio.Event().wait()
    finally:
        await service.stop()
asyncio.run(main())
EOF
```

Expected: the page shows "WSL Lab", the clock, "Free until <time>" on a green background, "Next: Design review, ..." with a disabled "Join opens at ..." button, the two events listed, and the link form. Pasting `98765432100` and pressing Join turns the page red with "In a meeting", Mute, Turn camera off and Leave; Leave asks to tap again; after leaving the page returns to green. Fix anything that does not match before committing.

- [ ] **Step 6: Run the whole suite** (command in Global Constraints). Expected: at most 87 failed.

- [ ] **Step 7: Commit**

```bash
git add src/croom/control/static pyproject.toml tests/unit/control/test_service.py
git commit -m "feat(control): add the room page"
```

---

### Task 5: Agent wiring and acceptance

**Files:**
- Modify: `src/croom/core/agent.py` (`_initialize_services`)
- Test: `tests/unit/core/test_agent.py`

**Interfaces:**
- Consumes: `ControlService.from_config(config, meeting, calendar)` (Task 2), `Config.control` (Task 1), `ServiceManager.get_service()`.
- Produces: the agent serving the page. No new interfaces.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/core/test_agent.py`, change `make_config` so the control page is opt-in for the existing tests, and add the new tests. Replace the function with:

```python
def make_config(tmp_path, dashboard_url: str = "", control: bool = False) -> Config:
    config = Config()
    config.ai.enabled = False
    config.dashboard.enabled = bool(dashboard_url)
    config.dashboard.url = dashboard_url
    config.data_dir = str(tmp_path)
    config.control.enabled = control
    config.control.host = "127.0.0.1"
    config.control.port = 0
    return config
```

Append to the file:

```python
class TestControlRegistration:
    def test_control_is_registered_after_meeting_and_calendar(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path, control=True))
        agent._initialize_services()
        control = agent.service_manager.get_service("control")
        assert control is not None
        assert control._meeting is agent.service_manager.get_service("meeting")
        assert control._calendar is agent.service_manager.get_service("calendar")
        order = agent.service_manager._start_order
        assert order.index("control") > order.index("meeting")
        assert order.index("control") > order.index("calendar")

    def test_control_is_absent_when_disabled(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path, control=False))
        agent._initialize_services()
        assert agent.service_manager.get_service("control") is None

    async def test_start_all_serves_the_page_on_an_ephemeral_port(self, tmp_path, no_hardware):
        agent = make_agent(make_config(tmp_path, control=True))
        agent._initialize_services()
        try:
            assert await agent.service_manager.start_all() is True
            control = agent.service_manager.get_service("control")
            assert control._state == ServiceState.RUNNING
            assert control.bound_port is not None and control.bound_port > 0
        finally:
            await agent.service_manager.stop_all()
        assert agent.service_manager.get_service("control").bound_port is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/core/test_agent.py -q -p no:cacheprovider`
Expected: the three `TestControlRegistration` tests FAIL (`control` is never registered: `None` from `get_service`); the earlier tests keep passing.

- [ ] **Step 3: Register the service in the agent**

In `src/croom/core/agent.py`, inside `_initialize_services`, after the dashboard block that ends with

```python
            except ImportError as e:
                logger.warning(f"Dashboard client not available: {e}")
```

append:

```python

        # Room control page (local web UI for the room)
        if self.config.control.enabled:
            try:
                from croom.control.service import ControlService
                control_service = ControlService.from_config(
                    self.config,
                    meeting=self.service_manager.get_service("meeting"),
                    calendar=self.service_manager.get_service("calendar"),
                )
                self.service_manager.register(control_service, dependencies=["meeting", "calendar"])
                logger.info("Room control page registered")
            except ImportError as e:
                logger.warning(f"Room control page not available: {e}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/core/test_agent.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Run the whole suite** (command in Global Constraints). Expected: at most 87 failed.

- [ ] **Step 6: Commit**

```bash
git add src/croom/core/agent.py tests/unit/core/test_agent.py
git commit -m "feat(agent): serve the room control page"
```

- [ ] **Step 7: Acceptance run**

Preconditions: the dashboard backend on `localhost:3001` (optional for this run), `echo $DISPLAY` prints `:0`, and `~/.config/croom/config.yaml` lists `zoom` under `meeting.platforms` (add it if only `google_meet` is there). Each configured platform opens its own Chromium window at agent start.

```bash
cd ~/croom.to && .venv/bin/croom -v
```

In the Windows browser open `http://localhost:8080/`. Expected: "WSL Lab", the clock, "Free" with "No calendar is connected. Join with a link below." Paste a Zoom link for a meeting you host on another device and press Join: the page turns violet with "Joining Zoom meeting", the room's Chromium window navigates to the Zoom web client, and once the meeting connects the page turns red with "In a meeting", the elapsed time, Mute, Turn camera off and Leave. Press Mute and watch the label flip. Press Leave twice; the page returns to "Free". If the join stops at Zoom's prompt for a passcode, the link lacked an embedded `pwd`: that limitation is in the spec's notes. Ctrl-C the agent (it may hang closing the browsers; kill it if so).

- [ ] **Step 8: Push the branch**

```bash
git push origin room-control
```
