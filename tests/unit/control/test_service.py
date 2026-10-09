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
        self.fail_before_joining = None

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
        if self.fail_before_joining is not None:
            raise self.fail_before_joining
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


class StubVolume:
    def __init__(self, available=True, level=40, muted=False, device="Logitech MeetUp Speakerphone Analog Stereo"):
        self.available, self.level, self.muted, self.device = available, level, muted, device
        self.reason = None if available else "no audio sink"
        self.calls = []
        self.refreshes = 0

    def state(self):
        return {"available": self.available, "device": self.device if self.available else None,
                "level": self.level, "muted": self.muted, "reason": self.reason}

    async def refresh(self, force=False):
        self.refreshes += 1
        return self.state()

    def _check(self):
        from croom.devices.errors import DeviceUnavailable
        if not self.available:
            raise DeviceUnavailable(self.reason)

    async def set_level(self, level):
        self._check()
        self.level = max(0, min(100, int(level)))
        self.calls.append(("level", self.level))
        return self.state()

    async def step(self, delta):
        return await self.set_level(self.level + int(delta))

    async def set_muted(self, muted):
        self._check()
        self.muted = bool(muted)
        self.calls.append(("muted", self.muted))
        return self.state()


class StubCamera:
    def __init__(self, available=True, home_saved=True, position_known=True, saved=(1, 2)):
        self.available, self.home_saved, self.position_known = available, home_saved, position_known
        self.saved = set(saved)
        self.zoom_level, self.moving, self.busy = 100, False, False
        self.preview_on = False
        self.moves, self.zooms, self.homes, self.recalls, self.saves, self.setups, self.previews = [], [], 0, [], [], [], []
        self.names = ["Wide", "Table", "Whiteboard"]
        self.zoom_reads = 0

    @property
    def preview(self):
        return self.preview_on

    def state(self):
        return {"available": self.available, "device": "/dev/video0" if self.available else None,
                "reason": None if self.available else "no controllable camera found",
                "moving": self.moving, "busy": self.busy, "zoom": {"level": self.zoom_level, "min": 100, "max": 500},
                "position_known": self.position_known, "home_saved": self.home_saved,
                "presets": [{"slot": s, "name": self.names[s - 1], "saved": s in self.saved} for s in (1, 2, 3)],
                "preview": self.preview_on}

    def _check(self):
        from croom.devices.errors import DeviceUnavailable
        if not self.available:
            raise DeviceUnavailable("no controllable camera found")

    async def read_zoom(self):
        self.zoom_reads += 1
        return self.zoom_level

    async def move(self, pan, tilt):
        if pan not in (-1, 0, 1) or tilt not in (-1, 0, 1):
            raise ValueError("pan and tilt must each be -1, 0 or 1")
        self._check()
        self.moves.append((pan, tilt))
        self.moving = bool(pan or tilt)
        return self.state()

    async def zoom(self, level):
        self._check()
        self.zoom_level = max(100, min(500, int(level)))
        self.zooms.append(self.zoom_level)
        return self.zoom_level

    async def zoom_step(self, delta):
        return await self.zoom(self.zoom_level + int(delta))

    async def home(self):
        from croom.devices.errors import NotReady
        self._check()
        if not self.home_saved:
            raise NotReady("save a home first")
        self.homes += 1
        self.position_known = True
        return self.state()

    async def find_stops(self):
        self._check()
        self.setups.append("find_stops")
        self.position_known = True
        return self.state()

    async def save_home(self):
        from croom.devices.errors import NotReady
        self._check()
        if not self.position_known:
            raise NotReady("home the camera first")
        self.setups.append("save_home")
        self.home_saved = True
        return self.state()

    async def save(self, slot):
        from croom.devices.errors import NotReady
        if slot not in (1, 2, 3):
            raise ValueError("preset slots are 1 to 3")
        self._check()
        if not self.position_known:
            raise NotReady("home the camera first")
        self.saves.append(slot)
        self.saved.add(slot)
        return self.state()

    async def recall(self, slot):
        from croom.devices.errors import NotReady
        if slot not in (1, 2, 3):
            raise ValueError("preset slots are 1 to 3")
        self._check()
        if slot not in self.saved:
            raise NotReady("nothing saved in this slot")
        self.recalls.append(slot)
        return self.state()

    def set_preview(self, on):
        self.previews.append(bool(on))
        self.preview_on = bool(on)
        return self.preview_on


class StubDevices:
    def __init__(self, volume=None, camera=None):
        self.volume = volume if volume is not None else StubVolume()
        self.camera = camera if camera is not None else StubCamera()


def event(event_id, title, starts_in_minutes, duration=30, url="https://zoom.us/j/98765432100?pwd=abc",
          platform=MeetingPlatform.ZOOM):
    start = datetime.now(timezone.utc) + timedelta(minutes=starts_in_minutes)
    return CalendarEvent(id=event_id, title=title, start_time=start, end_time=start + timedelta(minutes=duration),
                         meeting_url=url, meeting_platform=platform)


def skip_if_day_ends_within(hours: float) -> None:
    """Events are filtered to today; tests that schedule ahead skip near midnight."""
    now = datetime.now().astimezone()
    if (now + timedelta(hours=hours)).date() != now.date():
        pytest.skip(f"less than {hours} hours left in the local day")


def make_service(meeting=None, calendar=None, devices=None, **config):
    settings = {"host": "127.0.0.1", "port": 0, "room_name": "Lab", "room_location": "2nd floor"}
    settings.update(config)
    return ControlService(config=settings, meeting=meeting, calendar=calendar, devices=devices)


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


class TestCalendarEvents:
    async def test_events_without_calendar(self, client_factory):
        client = await client_factory(make_service(meeting=StubMeeting()))
        assert await (await client.get("/api/calendar/events")).json() == {"events": []}

    async def test_events_carry_the_joinable_flag(self, client_factory):
        skip_if_day_ends_within(3)
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
        resp = await client.post("/api/meeting/leave", json={})
        assert resp.status == 409
        assert (await resp.json())["error"] == "No meeting to leave"

    async def test_leave_runs_in_background_and_clears_the_title(self, client_factory):
        meeting = StubMeeting()
        calendar = StubCalendar(events=[event("e1", "Design review", starts_in_minutes=5)])
        client = await client_factory(make_service(meeting=meeting, calendar=calendar))
        await client.post("/api/meeting/join", json={"event_id": "e1"})
        await wait_until(lambda: meeting.state == MeetingState.CONNECTED)
        resp = await client.post("/api/meeting/leave", json={})
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
        resp = await client.post("/api/meeting/leave", json={})
        assert resp.status == 202
        await wait_until(lambda: meeting.state == MeetingState.IDLE)
        data = await (await client.get("/api/status")).json()
        assert data["meeting"]["error"] is None

    async def test_mute_and_camera_require_a_connected_meeting(self, client_factory):
        client = await client_factory(make_service(meeting=StubMeeting()))
        assert (await client.post("/api/meeting/mute", json={})).status == 409
        assert (await client.post("/api/meeting/camera", json={})).status == 409

    async def test_mute_and_camera_toggle_and_report(self, client_factory):
        meeting = StubMeeting()
        client = await client_factory(make_service(meeting=meeting))
        await client.post("/api/meeting/join", json={"url": "https://zoom.us/j/98765432100"})
        await wait_until(lambda: meeting.state == MeetingState.CONNECTED)
        assert await (await client.post("/api/meeting/mute", json={})).json() == {"muted": True}
        assert await (await client.post("/api/meeting/camera", json={})).json() == {"camera_on": False}
        data = await (await client.get("/api/status")).json()
        assert data["meeting"]["muted"] is True
        assert data["meeting"]["camera_on"] is False
        assert await (await client.post("/api/meeting/mute", json={})).json() == {"muted": False}

    async def test_without_meeting_service_controls_are_unavailable(self, client_factory):
        client = await client_factory(make_service())
        for path in ("/api/meeting/leave", "/api/meeting/mute", "/api/meeting/camera"):
            assert (await client.post(path, json={})).status == 503, path


class TestPage:
    async def test_index_is_served_uncached(self, client_factory):
        client = await client_factory(make_service())
        resp = await client.get("/")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/html")
        assert resp.headers["Cache-Control"] == "no-cache"
        body = await resp.text()
        assert "<title>Crystal Meet</title>" in body
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
        with open("pyproject.toml", encoding="utf-8") as f:
            text = f.read()
        assert 'control/static/*' in text

    async def test_brand_assets_are_served(self, client_factory):
        client = await client_factory(make_service())
        for path, content_type in (
            ("/static/crystalpm-logo-white.svg", "image/svg+xml"),
            ("/static/fonts/lexend-400.woff2", "font/woff2"),
            ("/static/fonts/lexend-600.woff2", "font/woff2"),
            ("/static/fonts/OFL.txt", "text/plain"),
        ):
            resp = await client.get(path)
            assert resp.status == 200, path
            assert resp.headers["Content-Type"].startswith(content_type), (path, resp.headers["Content-Type"])

    def test_page_has_no_external_references(self):
        from croom.control.service import STATIC_DIR
        for name in ("index.html", "style.css", "app.js", "sign.html", "sign.css", "sign.js"):
            text = (STATIC_DIR / name).read_text(encoding="utf-8")
            assert "http://" not in text and "https://" not in text and "//fonts." not in text, name
            assert "Croom" not in text, name

    async def test_sign_is_served_uncached(self, client_factory):
        client = await client_factory(make_service())
        resp = await client.get("/sign")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/html")
        assert resp.headers["Cache-Control"] == "no-cache"
        body = await resp.text()
        assert "<title>Crystal Meet</title>" in body
        assert 'src="/static/sign.js"' in body and 'href="/static/sign.css"' in body
        assert (await client.get("/static/sign.js")).status == 200
        assert (await client.get("/static/sign.css")).status == 200



EVENT_FIELDS = {"id", "title", "start_time", "end_time", "meeting_platform", "joinable"}


class TestReviewFixes:
    """Findings from the branch review, each pinned before it was fixed."""

    async def test_events_and_next_are_limited_to_today(self, client_factory):
        skip_if_day_ends_within(1)
        tomorrow = event("t1", "Tomorrow standup", starts_in_minutes=24 * 60 + 5)
        today_later = event("e1", "Design review", starts_in_minutes=45)
        client = await client_factory(make_service(meeting=StubMeeting(),
                                                   calendar=StubCalendar(events=[today_later, tomorrow])))
        events = (await (await client.get("/api/calendar/events")).json())["events"]
        assert [e["id"] for e in events] == ["e1"]
        client2 = await client_factory(make_service(meeting=StubMeeting(), calendar=StubCalendar(events=[tomorrow])))
        data = await (await client2.get("/api/status")).json()
        assert data["calendar"]["next"] is None
        assert (await (await client2.get("/api/calendar/events")).json())["events"] == []

    async def test_event_fields_are_limited_to_what_the_page_needs(self, client_factory):
        ev = event("e1", "Design review", starts_in_minutes=20)
        ev.description = "Agenda: next year's budget"
        ev.organizer = "ceo@example.com"
        ev.location = "HQ 3rd floor"
        client = await client_factory(make_service(meeting=StubMeeting(), calendar=StubCalendar(events=[ev])))
        events = (await (await client.get("/api/calendar/events")).json())["events"]
        assert set(events[0]) == EVENT_FIELDS
        data = await (await client.get("/api/status")).json()
        assert set(data["calendar"]["next"]) == EVENT_FIELDS

    async def test_posts_require_json_content_type(self, client_factory):
        meeting = StubMeeting()
        client = await client_factory(make_service(meeting=meeting))
        resp = await client.post("/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}',
                                 headers={"Content-Type": "text/plain", "Origin": "https://evil.example"})
        assert resp.status == 415
        assert meeting.joins == []
        for path in ("/api/meeting/leave", "/api/meeting/mute", "/api/meeting/camera"):
            assert (await client.post(path)).status == 415, path

    async def test_join_rejected_by_the_provider_before_joining_is_visible(self, client_factory):
        meeting = StubMeeting()
        meeting.fail_before_joining = ValueError("Invalid Zoom URL: https://zoom.us/my/ben")
        client = await client_factory(make_service(meeting=meeting))
        resp = await client.post("/api/meeting/join", json={"url": "https://zoom.us/my/ben"})
        assert resp.status == 202
        await wait_until(lambda: meeting.joins)
        await asyncio.sleep(0.02)
        data = await (await client.get("/api/status")).json()
        assert data["meeting"]["state"] == "error"
        assert data["meeting"]["error"] == "Invalid Zoom URL: https://zoom.us/my/ben"
        resp = await client.post("/api/meeting/leave", json={})
        assert resp.status == 200
        assert await resp.json() == {"state": "idle"}
        data = await (await client.get("/api/status")).json()
        assert data["meeting"]["state"] == "idle"
        assert data["meeting"]["error"] is None

    @pytest.mark.parametrize("url", [
        "https://evil.example/?teams.microsoft.com",
        "https://zoom.us.evil.example/j/98765432100",
        "https://example.com/zoom.us/j/98765432100",
    ])
    async def test_join_checks_the_link_hostname(self, client_factory, url):
        meeting = StubMeeting(platforms=("zoom", "google_meet", "teams"))
        client = await client_factory(make_service(meeting=meeting))
        resp = await client.post("/api/meeting/join", json={"url": url})
        assert resp.status == 400
        assert meeting.joins == []

    @pytest.mark.parametrize("url, expected", [
        ("https://us02web.zoom.us/j/98765432100?pwd=abc", "https://us02web.zoom.us/j/98765432100?pwd=abc"),
        ("zoom.us/j/98765432100", "https://zoom.us/j/98765432100"),
        ("meet.google.com/abc-defg-hij", "https://meet.google.com/abc-defg-hij"),
    ])
    async def test_join_accepts_platform_hosts_and_adds_a_scheme(self, client_factory, url, expected):
        meeting = StubMeeting()
        client = await client_factory(make_service(meeting=meeting))
        resp = await client.post("/api/meeting/join", json={"url": url})
        assert resp.status == 202
        assert (await resp.json())["url"] == expected
        await wait_until(lambda: meeting.joins)
        assert meeting.joins[0][0] == expected


class TestScreensaver:
    """The TV's idle style: chosen on the room page, remembered next to the agent's state (spec 2026-10-07 TV, 4.2)."""

    def settings(self, tmp_path, **overrides):
        config = {"settings_file": str(tmp_path / "control-settings.json")}
        config.update(overrides)
        return config

    async def test_screensaver_defaults_to_info_and_appears_in_status(self, client_factory, tmp_path):
        client = await client_factory(make_service(**self.settings(tmp_path)))
        data = await (await client.get("/api/screensaver")).json()
        assert data == {"style": "info", "styles": ["info", "quiet", "brand", "bounce"]}
        assert (await (await client.get("/api/status")).json())["screensaver"] == "info"

    async def test_a_posted_style_is_stored_and_survives_a_restart(self, client_factory, tmp_path):
        client = await client_factory(make_service(**self.settings(tmp_path)))
        response = await client.post("/api/screensaver", json={"style": "bounce"})
        assert response.status == 200 and (await response.json())["style"] == "bounce"
        assert (await (await client.get("/api/status")).json())["screensaver"] == "bounce"
        settings = tmp_path / "control-settings.json"
        assert oct(settings.stat().st_mode & 0o777) == "0o600"
        again = await client_factory(make_service(**self.settings(tmp_path)))
        assert (await (await again.get("/api/screensaver")).json())["style"] == "bounce"

    async def test_unknown_style_is_refused_and_nothing_changes(self, client_factory, tmp_path):
        client = await client_factory(make_service(**self.settings(tmp_path)))
        await client.post("/api/screensaver", json={"style": "quiet"})
        response = await client.post("/api/screensaver", json={"style": "disco"})
        assert response.status == 400 and "disco" in (await response.json())["error"]
        assert (await (await client.get("/api/screensaver")).json())["style"] == "quiet"
        assert (await client.post("/api/screensaver", data="style=brand")).status == 415
        assert (await client.post("/api/screensaver", json=["brand"])).status == 400

    async def test_corrupt_settings_file_falls_back_to_the_default(self, client_factory, tmp_path):
        (tmp_path / "control-settings.json").write_text("{not json")
        client = await client_factory(make_service(**self.settings(tmp_path, screensaver="brand")))
        assert (await (await client.get("/api/screensaver")).json())["style"] == "brand"

    def test_control_config_has_a_default_style(self):
        assert Config().control.screensaver == "info"
        config = Config.from_dict({"control": {"screensaver": "quiet"}})
        assert Config.from_dict(config.to_dict()).control.screensaver == "quiet"
        service = ControlService.from_config(config)
        assert service.config["screensaver"] == "quiet"
        assert service.config["settings_file"] == str(config.resolve_data_dir() / "control-settings.json")


class TestRoomDevices:
    """Volume and camera routes (spec 2026-10-08 sound and camera, section 4.4)."""

    async def test_status_carries_both_blocks_and_nothing_breaks_without_a_devices_service(self, client_factory):
        client = await client_factory(make_service(devices=StubDevices()))
        data = await (await client.get("/api/status")).json()
        assert data["audio"]["level"] == 40 and data["camera"]["presets"][0]["name"] == "Wide"
        bare = await client_factory(make_service())
        data = await (await bare.get("/api/status")).json()
        assert data["audio"]["available"] is False and data["camera"]["available"] is False

    async def test_volume_get_and_the_three_ways_to_change_it(self, client_factory):
        devices = StubDevices()
        client = await client_factory(make_service(devices=devices))
        assert (await (await client.get("/api/audio/volume")).json())["level"] == 40
        assert (await (await client.post("/api/audio/volume", json={"level": 70})).json())["level"] == 70
        assert (await (await client.post("/api/audio/volume", json={"step": -5})).json())["level"] == 65
        assert (await (await client.post("/api/audio/volume", json={"muted": True})).json())["muted"] is True
        assert devices.volume.calls == [("level", 70), ("level", 65), ("muted", True)]
        assert (await client.post("/api/audio/volume", json={})).status == 400
        assert (await client.post("/api/audio/volume", json={"level": "loud"})).status == 400
        assert (await client.post("/api/audio/volume", data="level=5")).status == 415

    async def test_an_unavailable_speaker_answers_409_with_the_reason(self, client_factory):
        client = await client_factory(make_service(devices=StubDevices(volume=StubVolume(available=False))))
        response = await client.post("/api/audio/volume", json={"level": 10})
        assert response.status == 409 and (await response.json())["error"] == "no audio sink"

    async def test_camera_move_zoom_and_home(self, client_factory):
        devices = StubDevices()
        client = await client_factory(make_service(devices=devices))
        assert (await (await client.post("/api/camera/move", json={"pan": 1, "tilt": 0})).json())["moving"] is True
        assert (await client.post("/api/camera/move", json={"pan": 2, "tilt": 0})).status == 400
        assert (await client.post("/api/camera/move", json={"pan": 0})).status == 400
        assert (await (await client.post("/api/camera/zoom", json={"level": 300})).json())["zoom"]["level"] == 300
        assert (await (await client.post("/api/camera/zoom", json={"step": 25})).json())["zoom"]["level"] == 325
        assert (await client.post("/api/camera/zoom", json={})).status == 400
        assert (await client.post("/api/camera/home", json={})).status == 200
        assert devices.camera.moves == [(1, 0)] and devices.camera.zooms == [300, 325] and devices.camera.homes == 1

    async def test_presets_and_setup(self, client_factory):
        devices = StubDevices(camera=StubCamera(home_saved=False, position_known=False, saved=()))
        client = await client_factory(make_service(devices=devices))
        response = await client.post("/api/camera/home", json={})
        assert response.status == 409 and (await response.json())["error"] == "save a home first"
        response = await client.post("/api/camera/presets/1", json={"action": "save"})
        assert response.status == 409 and (await response.json())["error"] == "home the camera first"
        assert (await client.post("/api/camera/setup", json={"action": "find_stops"})).status == 200
        assert (await client.post("/api/camera/setup", json={"action": "save_home"})).status == 200
        assert (await client.post("/api/camera/setup", json={"action": "dance"})).status == 400
        assert (await (await client.post("/api/camera/presets/2", json={"action": "save"})).json())["presets"][1]["saved"] is True
        assert (await client.post("/api/camera/presets/2", json={"action": "recall"})).status == 200
        response = await client.post("/api/camera/presets/3", json={"action": "recall"})
        assert response.status == 409 and (await response.json())["error"] == "nothing saved in this slot"
        assert (await client.post("/api/camera/presets/9", json={"action": "recall"})).status == 400
        assert (await client.post("/api/camera/presets/x", json={"action": "recall"})).status == 400
        assert (await client.post("/api/camera/presets/2", json={"action": "eat"})).status == 400
        assert devices.camera.setups == ["find_stops", "save_home"] and devices.camera.saves == [2] and devices.camera.recalls == [2]

    async def test_an_unavailable_camera_answers_409_everywhere(self, client_factory):
        client = await client_factory(make_service(devices=StubDevices(camera=StubCamera(available=False))))
        for path, body in (("/api/camera/move", {"pan": 1, "tilt": 0}), ("/api/camera/zoom", {"step": 25}),
                           ("/api/camera/home", {}), ("/api/camera/presets/1", {"action": "recall"}),
                           ("/api/camera/setup", {"action": "find_stops"})):
            response = await client.post(path, json=body)
            assert response.status == 409, path
            assert (await response.json())["error"] == "no controllable camera found"

    async def test_preview_is_refused_during_a_meeting(self, client_factory):
        devices = StubDevices()
        meeting = StubMeeting()
        client = await client_factory(make_service(meeting=meeting, devices=devices))
        assert (await (await client.post("/api/camera/preview", json={"on": True})).json())["preview"] is True
        meeting.set_state(MeetingState.CONNECTED)
        response = await client.post("/api/camera/preview", json={"on": True})
        assert response.status == 409 and "meeting" in (await response.json())["error"]
        assert (await (await client.post("/api/camera/preview", json={"on": False})).json())["preview"] is False
        assert devices.camera.previews == [True, False]
        assert (await client.post("/api/camera/preview", json={"on": "yes"})).status == 400

    async def test_preview_is_refused_when_the_camera_is_unavailable(self, client_factory):
        devices = StubDevices(camera=StubCamera(available=False))
        client = await client_factory(make_service(devices=devices))
        response = await client.post("/api/camera/preview", json={"on": True})
        assert response.status == 409 and (await response.json())["error"] == "no controllable camera found"
        assert devices.camera.previews == []
        # turning it off is always possible, as in a meeting: closing the panel must bring the screensaver back
        response = await client.post("/api/camera/preview", json={"on": False})
        assert response.status == 200 and (await response.json())["preview"] is False
        assert devices.camera.previews == [False]

    @pytest.mark.parametrize("path, body, writer", [
        ("/api/camera/presets/1", {"action": "save"}, "save"),
        ("/api/camera/setup", {"action": "save_home"}, "save_home"),
    ])
    async def test_a_failed_settings_write_answers_500_with_the_reason(
            self, client_factory, caplog, path, body, writer):
        camera = StubCamera()

        async def disk_full(*args):
            raise OSError("No space left on device")

        setattr(camera, writer, disk_full)
        client = await client_factory(make_service(devices=StubDevices(camera=camera)))
        response = await client.post(path, json=body)
        assert response.status == 500
        assert (await response.json())["error"] == "could not save the camera settings: No space left on device"
        assert any(r.levelname == "WARNING" and "No space left on device" in r.getMessage() for r in caplog.records)

    async def test_the_status_poll_reads_the_devices_but_does_not_probe_a_missing_camera(self, client_factory):
        present = StubDevices()
        client = await client_factory(make_service(devices=present))
        await client.get("/api/status")
        assert present.volume.refreshes == 1 and present.camera.zoom_reads == 1
        missing = StubDevices(volume=StubVolume(available=False), camera=StubCamera(available=False))
        client = await client_factory(make_service(devices=missing))
        data = await (await client.get("/api/status")).json()
        # read_zoom() goes through discovery, which opens /dev/video*: not on every poll of a room without a camera
        assert missing.camera.zoom_reads == 0
        assert missing.volume.refreshes == 1       # the speaker's read is cached by RoomVolume, so it is always asked
        assert data["camera"]["available"] is False and data["camera"]["reason"] == "no controllable camera found"
        assert data["audio"]["available"] is False and data["audio"]["reason"] == "no audio sink"

    async def test_the_volume_is_read_before_it_is_reported(self, client_factory):
        devices = StubDevices()
        client = await client_factory(make_service(devices=devices))
        await client.get("/api/audio/volume")
        assert devices.volume.refreshes == 1

    async def test_a_failing_device_read_never_breaks_the_status(self, client_factory):
        devices = StubDevices()

        async def broken(*args, **kwargs):
            raise RuntimeError("the device went away")

        devices.volume.refresh = broken
        devices.camera.read_zoom = broken
        client = await client_factory(make_service(devices=devices))
        for path in ("/api/status", "/api/audio/volume"):
            response = await client.get(path)
            assert response.status == 200, path
        assert (await (await client.get("/api/status")).json())["audio"]["level"] == 40

    async def test_a_long_move_cut_short_answers_409_with_the_reason(self, client_factory):
        from croom.devices.errors import Interrupted
        camera = StubCamera()

        async def cut_short(slot):
            raise Interrupted("the camera move was interrupted")

        camera.recall = cut_short
        client = await client_factory(make_service(devices=StubDevices(camera=camera)))
        response = await client.post("/api/camera/presets/1", json={"action": "recall"})
        assert response.status == 409 and (await response.json())["error"] == "the camera move was interrupted"

    async def test_without_a_devices_service_the_blocks_have_the_same_keys_and_the_routes_say_so(self, client_factory):
        client = await client_factory(make_service())
        data = await (await client.get("/api/status")).json()
        assert set(data["audio"]) == set(StubVolume().state()) and set(data["camera"]) == set(StubCamera().state())
        assert data["audio"]["reason"] == data["camera"]["reason"] == "no devices service"
        assert (await (await client.get("/api/audio/volume")).json()) == data["audio"]
        for path, body in (("/api/audio/volume", {"level": 10}), ("/api/camera/move", {"pan": 1, "tilt": 0}),
                           ("/api/camera/zoom", {"step": 25}), ("/api/camera/home", {}),
                           ("/api/camera/presets/1", {"action": "recall"}),
                           ("/api/camera/setup", {"action": "find_stops"}), ("/api/camera/preview", {"on": True})):
            response = await client.post(path, json=body)
            assert response.status == 409, path
            assert (await response.json())["error"] == "no devices service"

    async def test_every_device_post_wants_a_json_object(self, client_factory):
        devices = StubDevices()
        client = await client_factory(make_service(devices=devices))
        paths = ("/api/audio/volume", "/api/camera/move", "/api/camera/zoom", "/api/camera/home",
                 "/api/camera/presets/1", "/api/camera/setup", "/api/camera/preview")
        for path in paths:
            assert (await client.post(path, data="{}", headers={"Content-Type": "text/plain"})).status == 415, path
            assert (await client.post(path, json=["not", "an", "object"])).status == 400, path
        assert devices.volume.calls == [] and devices.camera.moves == [] and devices.camera.previews == []

    @pytest.mark.parametrize("path, key", [("/api/audio/volume", "level"), ("/api/audio/volume", "step"),
                                           ("/api/camera/zoom", "level"), ("/api/camera/zoom", "step")])
    async def test_a_number_that_is_not_finite_is_a_400_not_a_crash(self, client_factory, path, key):
        # Python's JSON parser accepts NaN and Infinity, and a literal like 1e999 reads as infinity
        client = await client_factory(make_service(devices=StubDevices()))
        for text in ("Infinity", "-Infinity", "NaN", "1e999"):
            response = await client.post(path, data='{"%s": %s}' % (key, text),
                                         headers={"Content-Type": "application/json"})
            assert response.status == 400, (path, key, text)
            assert (await response.json())["error"] == f"{key} must be a number"
