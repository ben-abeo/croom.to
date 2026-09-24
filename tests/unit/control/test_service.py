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
