"""
Tests for croom.control.service against stub meeting, calendar, devices and TV services.

The stubs are plain classes that behave like the real services' public API
and record what the control service asked them to do.
"""

import asyncio
import base64
import contextlib
import json
import logging
import os
import socket
import sys
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import aiohttp
import pytest
from aiohttp import streams
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from croom.calendar.providers.base import CalendarEvent, MeetingPlatform
from croom.core.config import Config, TvConfig
from croom.core.service import Service
from croom.meeting.providers.base import MeetingInfo, MeetingState, detect_platform
from croom.control.service import ControlService
from croom.tv.discover import discover_tvs
from croom.tv.errors import TvError
from croom.tv.pointer import BAD_MESSAGE, BAD_TARGET, BAD_TYPE, CODES, PiMouse


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
        self.fail_next_join = False
        self.hold_join = None     # an asyncio.Event that a join waits for before it asks its provider, as the real one waits for the TV
        self.cancel_gate = None   # an asyncio.Event that a cancelled join waits for before it lets go (a provider that is slow to)
        self.order = []           # "join given up" and "leave", in the order they happened

    def get_available_platforms(self):
        return list(self._platforms)

    def add_state_callback(self, callback):
        self.callbacks.append(callback)

    def set_state(self, state):
        self.state = state
        for callback in self.callbacks:
            callback(state)

    async def join_meeting(self, url, display_name=None, camera_on=None, mic_on=None, passcode=None):
        self.joins.append((url, display_name, passcode))
        if self.fail_before_joining is not None:
            raise self.fail_before_joining
        if self.hold_join is not None:
            self.set_state(MeetingState.JOINING)        # (the real meeting service says joining while the join waits for the room)
            try:
                await self.hold_join.wait()
            except asyncio.CancelledError:
                if self.cancel_gate is not None:
                    await self.cancel_gate.wait()
                self.order.append("join given up")
                self.set_state(MeetingState.IDLE)       # nothing is left waiting, as in the real one
                raise
        if self.fail_next_join:
            # Refused at once, before any "joining" shows: the error state and its reason, as a provider reports them.
            self.fail_next_join = False
            failure = RuntimeError("Meet refused the room")
            self.current_meeting = MeetingInfo(
                platform=detect_platform(url) or "unknown", meeting_id="failed",
                meeting_url=url, error_message=str(failure),
            )
            self.set_state(MeetingState.ERROR)
            raise failure
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
        self.order.append("leave")
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
        self.available, self.home_saved = available, home_saved
        self.position_known = position_known and available      # a missing camera never knows its position
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
        if isinstance(pan, bool) or isinstance(tilt, bool) or pan not in (-1, 0, 1) or tilt not in (-1, 0, 1):
            raise ValueError("pan and tilt must each be -1, 0 or 1")
        self._check()
        pan, tilt = int(pan), int(tilt)
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
        if not self.home_saved:
            raise NotReady("save a home first")
        self._check()
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
        if not self.position_known:
            raise NotReady("home the camera first")
        self._check()
        self.setups.append("save_home")
        self.home_saved = True
        return self.state()

    @staticmethod
    def _slot(slot):
        """RoomCamera's rule: True is 1 and 2.0 is 2 to Python, but neither is a slot number."""
        if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 3:
            raise ValueError("preset slots are 1 to 3")

    async def save(self, slot):
        from croom.devices.errors import NotReady
        self._slot(slot)
        if not self.position_known:
            raise NotReady("home the camera first")
        self._check()
        self.saves.append(slot)
        self.saved.add(slot)
        return self.state()

    async def recall(self, slot):
        from croom.devices.errors import NotReady
        self._slot(slot)
        if slot not in self.saved:
            raise NotReady("nothing saved in this slot")
        if not self.position_known and not self.home_saved:
            raise NotReady("home the camera first")
        self._check()
        self.recalls.append(slot)
        self.position_known = True          # an unknown position is homed first: the camera knows where it is now
        return self.state()

    def set_preview(self, on):
        self.previews.append(bool(on))
        self.preview_on = bool(on)
        return self.preview_on


class StubDevices:
    def __init__(self, volume=None, camera=None):
        self.volume = volume if volume is not None else StubVolume()
        self.camera = camera if camera is not None else StubCamera()


class StubTv:
    """Behaves like TvService for the parts the control service uses, and records what it was asked: `calls` has the controls,
    pairings and forgettings in the order they came, and `order` the start and the end of each control (two that overlap show
    as two starts in a row)."""

    def __init__(self, configured=True, paired=True):
        self.configured, self.paired = configured, paired
        self.volume, self.name = 24, "Boardroom TV"
        self.calls, self.order = [], []
        self.fail_control = self.fail_pair = self.fail_forget = self.fail_status = None   # what each raises when it is asked
        self.gate = None          # an asyncio.Event that control() waits for: a control that takes a while, as a wake does
        self.pair_gate = None     # the same for pair(): the person at the TV has not accepted the prompt yet
        self.cursor = StubCursor()
        self.fail_pointer = None  # what pointer() raises: the TV cannot be told anything

    def status(self):
        if self.fail_status is not None:
            raise self.fail_status
        return {"configured": self.configured, "paired": self.paired, "keep_on": True, "held_off": False, "refused": False,
                "on": True, "reachable": True, "name": self.name, "volume": self.volume, "muted": False,
                "input": "HDMI_1", "inputs": [{"id": "HDMI_1", "label": "HDMI 1"}, {"id": "HDMI_2", "label": "HDMI 2"}],
                "app": "com.webos.app.hdmi1", "channel": None}

    async def control(self, name, value=None):
        self.calls.append(("control", name, value))
        self.order.append(("start", name))
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.fail_control is not None:
                raise self.fail_control
            if name == "volume":
                self.volume = value
            return self.status()
        finally:
            self.order.append(("end", name))

    async def pair(self):
        self.calls.append(("pair",))
        if self.pair_gate is not None:
            await self.pair_gate.wait()
        if self.fail_pair is not None:
            raise self.fail_pair
        self.paired = True

    async def forget(self):
        self.calls.append(("forget",))
        if self.fail_forget is not None:
            raise self.fail_forget
        self.paired = False

    def pointer(self):
        """The client for the cursor (or why not, as TvService.pointer() says it): what it is told is in `cursor.calls`, not in `calls`,
        which has what was asked of the controls."""
        if self.fail_pointer is not None:
            raise self.fail_pointer
        return self.cursor


class StubCursor:
    """The TV's client as the cursor uses it: notes what it is told, as the library names it, and waits for `gate` (an asyncio.Event) in
    a move when it has one, as a TV that does not take it. `cancelled` counts the moves that were ended while they waited."""

    def __init__(self):
        self.calls = []
        self.gate = None
        self.cancelled = 0

    async def move(self, d_x, d_y, down=0):
        self.calls.append(("move", d_x, d_y, down))
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise

    async def click(self):
        self.calls.append(("click",))

    async def scroll(self, d_x, d_y):
        self.calls.append(("scroll", d_x, d_y))


class StubDevice:
    """What evdev.UInput is to a real PiMouse: it notes the events written to it."""

    def __init__(self):
        self.writes = []

    def write(self, kind, code, value):
        self.writes.append((kind, code, value))

    def syn(self):
        self.writes.append("syn")

    def close(self):
        pass


class StubMouse:
    """Behaves like PiMouse: notes what it is told, in `calls`, and raises `fail` (an error) when it has one, as a mouse that cannot be
    made does. `closed` counts the times the service closed it."""

    def __init__(self):
        self.calls = []
        self.fail = None
        self.unavailable = None
        self.opened = 0           # the times the service made it ahead of the first event (a socket opened)
        self.closed = 0

    def open(self):
        self.opened += 1
        if self.fail is not None:
            raise self.fail

    def _note(self, *call):
        if self.fail is not None:
            raise self.fail
        self.calls.append(call)

    def move(self, dx, dy):
        self._note("move", dx, dy)

    def press(self, button="left"):
        self._note("press", button)

    def release(self, button="left"):
        self._note("release", button)

    def click(self, button="left"):
        self._note("click", button)

    def scroll(self, dy):
        self._note("scroll", dy)

    def close(self):
        self.closed += 1


class StubSearch:
    """Stands in for discover_tvs, which puts a packet on the network: it counts how often it was asked and answers `tvs`, or
    raises `fail`, once `gate` (an asyncio.Event; none: at once) is open."""

    def __init__(self, tvs=(), fail=None):
        self.tvs, self.fail = [dict(tv) for tv in tvs], fail
        self.gate = None
        self.calls = self.cancelled = 0

    async def __call__(self):
        self.calls += 1
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.fail is not None:
            raise self.fail
        return [dict(tv) for tv in self.tvs]


BOARDROOM = {"host": "10.0.0.41", "name": "Boardroom TV", "location": "http://10.0.0.41:1234/udhisapi.xml"}


def real_states():
    """What RoomVolume and RoomCamera report before they have looked for a device: the shape every block must
    have. Constructing them does no I/O."""
    from croom.devices.camera import RoomCamera
    from croom.devices.volume import RoomVolume
    return RoomVolume().state(), RoomCamera().state()


def event(event_id, title, starts_in_minutes, duration=30, url="https://zoom.us/j/98765432100?pwd=abc",
          platform=MeetingPlatform.ZOOM, meeting_passcode=None):
    start = datetime.now(timezone.utc) + timedelta(minutes=starts_in_minutes)
    return CalendarEvent(id=event_id, title=title, start_time=start, end_time=start + timedelta(minutes=duration),
                         meeting_url=url, meeting_platform=platform, meeting_passcode=meeting_passcode)


def skip_if_day_ends_within(hours: float) -> None:
    """Events are filtered to today; tests that schedule ahead skip near midnight."""
    now = datetime.now().astimezone()
    if (now + timedelta(hours=hours)).date() != now.date():
        pytest.skip(f"less than {hours} hours left in the local day")


def make_service(meeting=None, calendar=None, devices=None, tv=None, discover=None, mouse=None, **config):
    settings = {"host": "127.0.0.1", "port": 0, "room_name": "Lab", "room_location": "2nd floor"}
    settings.update(config)
    return ControlService(config=settings, meeting=meeting, calendar=calendar, devices=devices, tv=tv, discover=discover, mouse=mouse)


@pytest.fixture(autouse=True)
def no_packet_leaves_this_machine(monkeypatch):
    """The search for TVs puts a datagram on the network and asks the TVs it finds for a page: nothing in this module may
    (a test that wants a search gives make_service a `discover=` of its own, a StubSearch)."""
    from croom.tv import discover as discovery

    async def refuse(*args, **kwargs):
        raise AssertionError("a test reached the real network: give make_service a discover=")

    monkeypatch.setattr(discovery, "_multicast", refuse)
    monkeypatch.setattr(discovery, "_get", refuse)
    return refuse


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

    def test_from_config_hands_the_tv_service_on_and_searches_with_the_real_search(self):
        tv = StubTv()
        assert ControlService.from_config(Config(), tv=tv)._tv is tv
        assert ControlService.from_config(Config())._tv is None
        assert ControlService.from_config(Config(), tv=tv)._discover_tvs is discover_tvs

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
        assert meeting.joins == [("https://zoom.us/j/98765432100?pwd=abc", "Lab", None)]
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

    @pytest.mark.parametrize("typed", ["559 000 0559", "559\u00a0000\u00a00559", "  559 000 0559  ", "5590 000 559"])
    async def test_a_zoom_meeting_id_written_in_groups_becomes_a_link(self, client_factory, typed):
        """Invitations print the ID in groups ("559 000 0559"), with spaces or no-break spaces between them."""
        meeting = StubMeeting()
        client = await client_factory(make_service(meeting=meeting))
        resp = await client.post("/api/meeting/join", json={"url": typed})
        assert resp.status == 202
        assert (await resp.json())["url"] == "https://zoom.us/j/5590000559"
        await wait_until(lambda: meeting.joins)
        assert meeting.joins[0][0] == "https://zoom.us/j/5590000559"

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

    async def test_static_files_are_never_served_stale(self, client_factory):
        """index.html is no-cache; a kiosk holding an old app.js next to a new index.html would break, so every static
        file is: the browser keeps its copy and asks before using it."""
        from croom.control.service import STATIC_DIR
        client = await client_factory(make_service())
        names = sorted(p.relative_to(STATIC_DIR).as_posix() for p in STATIC_DIR.rglob("*") if p.is_file())
        assert {"app.js", "devices.js", "keyboard.js", "style.css"} <= set(names)
        for name in names:
            resp = await client.get("/static/" + name)
            assert resp.status == 200, name
            assert resp.headers["Cache-Control"] == "no-cache", name
        # Asking again is a revalidation: the answer to a copy that is still good says no-cache too.
        first = await client.get("/static/app.js")
        again = await client.get("/static/app.js", headers={"If-None-Match": first.headers["ETag"]})
        assert again.status == 304
        assert again.headers["Cache-Control"] == "no-cache"

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
        """Every page, stylesheet and script the service serves, found by looking, so a new file cannot be missed."""
        from croom.control.service import STATIC_DIR
        files = sorted(p for p in STATIC_DIR.rglob("*") if p.suffix in (".html", ".css", ".js"))
        assert {"index.html", "app.js", "style.css"} <= {p.name for p in files}   # the glob is looking at the right folder
        for path in files:
            name = path.relative_to(STATIC_DIR).as_posix()
            text = path.read_text(encoding="utf-8")
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


class TestJoinPasscode:
    """A passcode for a protected Zoom meeting: from the page's body, or from the booking's own invitation."""

    LINK = "https://zoom.us/j/98765432100"
    WORDS = {"error": "Passcode must be letters, digits or @-_*, up to 32"}

    async def join(self, client_factory, body, events=()):
        meeting = StubMeeting()
        calendar = StubCalendar(events=list(events)) if events else None
        client = await client_factory(make_service(meeting=meeting, calendar=calendar))
        resp = await client.post("/api/meeting/join", json=body)
        return meeting, client, resp

    async def test_the_passcode_reaches_the_meeting_service(self, client_factory):
        meeting, _, resp = await self.join(client_factory, {"url": self.LINK, "passcode": "940104"})
        assert resp.status == 202
        assert await resp.json() == {"state": "joining", "url": self.LINK}      # the passcode is never echoed
        await wait_until(lambda: meeting.joins)
        assert meeting.joins == [(self.LINK, "Lab", "940104")]

    @pytest.mark.parametrize("sent, expected", [
        ("  940104  ", "940104"),
        ("AbC123", "AbC123"),
        ("1", "1"),
        ("9" * 32, "9" * 32),
        ("a-b_c*9@", "a-b_c*9@"),        # the four characters Zoom allows besides letters and digits
        ("  a-b_c*9@  ", "a-b_c*9@"),
    ])
    async def test_the_passcode_is_trimmed_and_may_be_1_to_32_of_the_characters_zoom_allows(
            self, client_factory, sent, expected):
        meeting, _, resp = await self.join(client_factory, {"url": self.LINK, "passcode": sent})
        assert resp.status == 202
        await wait_until(lambda: meeting.joins)
        assert meeting.joins[0][2] == expected

    @pytest.mark.parametrize("extra", [{}, {"passcode": None}, {"passcode": ""}, {"passcode": "   "}])
    async def test_an_absent_null_or_empty_passcode_means_none(self, client_factory, extra):
        meeting, _, resp = await self.join(client_factory, {"url": self.LINK, **extra})
        assert resp.status == 202
        await wait_until(lambda: meeting.joins)
        assert meeting.joins == [(self.LINK, "Lab", None)]

    @pytest.mark.parametrize("passcode", [
        "12 34", "12/34", "12.34", "12&34", "12=34", "12#34", "9" * 33, "a-b_c*9@" + "x" * 25,
        "1234\n5678", "caf\u00e9", "\u0663\u0664\u0665\u0666",
        5, 0, True, False, 12.5, ["940104"], {"code": "940104"},
    ])
    async def test_a_bad_passcode_is_refused(self, client_factory, passcode):
        meeting, _, resp = await self.join(client_factory, {"url": self.LINK, "passcode": passcode})
        assert resp.status == 400
        assert await resp.json() == self.WORDS
        assert meeting.joins == []

    async def test_a_bad_passcode_is_refused_before_the_event_is_looked_up(self, client_factory):
        meeting, _, resp = await self.join(client_factory, {"event_id": "missing", "passcode": "12 34"})
        assert resp.status == 400
        assert await resp.json() == self.WORDS
        assert meeting.joins == []

    async def test_a_booking_joins_with_its_own_passcode(self, client_factory):
        booking = event("e1", "Vendor call", starts_in_minutes=5, url=self.LINK, meeting_passcode="940104")
        meeting, _, resp = await self.join(client_factory, {"event_id": "e1"}, events=[booking])
        assert resp.status == 202
        await wait_until(lambda: meeting.joins)
        assert meeting.joins == [(self.LINK, "Lab", "940104")]

    async def test_a_passcode_in_the_body_wins_over_the_bookings(self, client_factory):
        booking = event("e1", "Vendor call", starts_in_minutes=5, url=self.LINK, meeting_passcode="940104")
        meeting, _, resp = await self.join(client_factory, {"event_id": "e1", "passcode": " 111222 "}, events=[booking])
        assert resp.status == 202
        await wait_until(lambda: meeting.joins)
        assert meeting.joins == [(self.LINK, "Lab", "111222")]

    @pytest.mark.parametrize("extra", [{"passcode": None}, {"passcode": ""}, {"passcode": "  "}])
    async def test_an_empty_passcode_in_the_body_leaves_the_bookings_in_place(self, client_factory, extra):
        booking = event("e1", "Vendor call", starts_in_minutes=5, url=self.LINK, meeting_passcode="940104")
        meeting, _, resp = await self.join(client_factory, {"event_id": "e1", **extra}, events=[booking])
        assert resp.status == 202
        await wait_until(lambda: meeting.joins)
        assert meeting.joins[0][2] == "940104"

    async def test_a_booking_without_a_passcode_joins_without_one(self, client_factory):
        booking = event("e1", "Design review", starts_in_minutes=5)
        meeting, _, resp = await self.join(client_factory, {"event_id": "e1"}, events=[booking])
        assert resp.status == 202
        await wait_until(lambda: meeting.joins)
        assert meeting.joins[0][2] is None

    async def test_a_passcode_refusal_reaches_the_status_in_the_providers_words(self, client_factory):
        meeting = StubMeeting()
        meeting.fail_with = RuntimeError("Zoom needs the meeting passcode (Wrong password (code 3004))")
        client = await client_factory(make_service(meeting=meeting))
        resp = await client.post("/api/meeting/join", json={"url": self.LINK, "passcode": "000000"})
        assert resp.status == 202
        await wait_until(lambda: meeting.state == MeetingState.ERROR)
        await asyncio.sleep(0.02)
        data = await (await client.get("/api/status")).json()
        assert data["meeting"]["state"] == "error"
        assert data["meeting"]["error"] == "Zoom needs the meeting passcode (Wrong password (code 3004))"

    async def test_the_passcode_stays_off_the_page(self, client_factory):
        skip_if_day_ends_within(3)
        booking = event("e1", "Vendor call", starts_in_minutes=20, url=self.LINK, meeting_passcode="940104")
        meeting, client, resp = await self.join(client_factory, {"event_id": "e1"}, events=[booking])
        answer = await resp.text()
        await wait_until(lambda: meeting.state == MeetingState.CONNECTED)
        status = await (await client.get("/api/status")).text()
        listing = await (await client.get("/api/calendar/events")).text()
        assert listing and status and answer
        assert all("940104" not in text for text in (answer, status, listing))


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
        assert (await client.post("/api/camera/move", json={"pan": 0.5, "tilt": 0})).status == 400
        assert (await client.post("/api/camera/move", json={"pan": 1.5, "tilt": 0})).status == 400
        assert (await client.post("/api/camera/move", json={"pan": 0, "tilt": 1.9})).status == 400
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
        for slot in ("٢", "²"):        # "٢" is 2 to int() and "²" is a digit to str.isdigit(); neither is a slot number
            response = await client.post(f"/api/camera/presets/{slot}", json={"action": "recall"})
            assert response.status == 400 and (await response.json())["error"] == "preset slots are 1 to 3", slot
        assert (await client.post("/api/camera/presets/2", json={"action": "eat"})).status == 400
        assert devices.camera.setups == ["find_stops", "save_home"] and devices.camera.saves == [2] and devices.camera.recalls == [2]

    async def test_an_unavailable_camera_answers_409_everywhere(self, client_factory):
        client = await client_factory(make_service(devices=StubDevices(camera=StubCamera(available=False))))
        missing, unknown = "no controllable camera found", "home the camera first"
        # a missing camera never knows its position, so saving one is refused for that first, as RoomCamera does
        for path, body, reason in (("/api/camera/move", {"pan": 1, "tilt": 0}, missing),
                                   ("/api/camera/zoom", {"step": 25}, missing),
                                   ("/api/camera/home", {}, missing),
                                   ("/api/camera/presets/1", {"action": "recall"}, missing),
                                   ("/api/camera/presets/1", {"action": "save"}, unknown),
                                   ("/api/camera/setup", {"action": "find_stops"}, missing),
                                   ("/api/camera/setup", {"action": "save_home"}, unknown)):
            response = await client.post(path, json=body)
            assert response.status == 409, (path, body)
            assert (await response.json())["error"] == reason, (path, body)

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

    async def test_without_a_devices_service_the_blocks_have_the_real_keys_and_the_routes_say_so(self, client_factory):
        client = await client_factory(make_service())
        data = await (await client.get("/api/status")).json()
        speaker, camera = real_states()
        assert set(data["audio"]) == set(speaker)
        assert set(data["camera"]) == set(camera) and set(data["camera"]["zoom"]) == set(camera["zoom"])
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

    def test_the_stubs_have_the_keys_of_the_real_modules(self):
        speaker, camera = real_states()
        for stub in (StubVolume(), StubVolume(available=False)):
            assert set(stub.state()) == set(speaker)
        for stub in (StubCamera(), StubCamera(available=False)):
            state = stub.state()
            assert set(state) == set(camera) and set(state["zoom"]) == set(camera["zoom"])
            assert [set(p) for p in state["presets"]] == [set(p) for p in camera["presets"]]
            assert [p["slot"] for p in state["presets"]] == [p["slot"] for p in camera["presets"]]

    @pytest.mark.parametrize("path, body, message", [
        ("/api/audio/volume", {"level": True}, "level must be a number"),
        ("/api/audio/volume", {"step": True}, "step must be a number"),
        ("/api/audio/volume", {"muted": "yes"}, "muted must be true or false"),
        ("/api/audio/volume", {"muted": 1}, "muted must be true or false"),
        ("/api/camera/move", {"pan": True, "tilt": 0}, "pan must be a number"),
        ("/api/camera/move", {"pan": 0, "tilt": True}, "tilt must be a number"),
        ("/api/camera/zoom", {"level": True}, "level must be a number"),
        ("/api/camera/zoom", {"step": True}, "step must be a number"),
    ])
    async def test_a_boolean_is_not_a_number_and_a_number_is_not_a_boolean(self, client_factory, path, body, message):
        devices = StubDevices()
        client = await client_factory(make_service(devices=devices))
        response = await client.post(path, json=body)
        assert response.status == 400 and (await response.json())["error"] == message
        assert devices.volume.calls == [] and devices.camera.moves == [] and devices.camera.zooms == []

    async def test_a_volume_body_names_one_change_only(self, client_factory):
        devices = StubDevices()
        client = await client_factory(make_service(devices=devices))
        for body in ({}, {"level": 50, "muted": True}, {"level": 50, "step": 5}, {"step": 5, "muted": False},
                     {"level": 50, "step": 5, "muted": True}):
            response = await client.post("/api/audio/volume", json=body)
            assert response.status == 400, body
            assert (await response.json())["error"] == "Send one of level, step or muted", body
        assert devices.volume.calls == []

    async def test_a_failing_device_read_is_warned_about_once_per_distinct_message(self, client_factory, caplog):
        devices = StubDevices()
        said = {"speaker": "pw-dump gave no answer", "camera": "the zoom control is gone"}

        async def broken_speaker(*args, **kwargs):
            raise RuntimeError(said["speaker"])

        async def broken_camera(*args, **kwargs):
            raise RuntimeError(said["camera"])

        devices.volume.refresh, devices.camera.read_zoom = broken_speaker, broken_camera
        client = await client_factory(make_service(devices=devices))

        def warnings_about(text):
            return [r for r in caplog.records if r.levelname == "WARNING" and text in r.getMessage()]

        for _ in range(3):
            assert (await client.get("/api/status")).status == 200
        # once each, with the traceback, though the two readers fail one after the other on every poll
        assert len(warnings_about("pw-dump gave no answer")) == 1
        assert len(warnings_about("the zoom control is gone")) == 1
        assert warnings_about("pw-dump gave no answer")[0].exc_info is not None
        said["speaker"] = "pw-dump timed out"
        await client.get("/api/status")
        assert len(warnings_about("pw-dump timed out")) == 1 and len(warnings_about("the zoom control is gone")) == 1

    async def test_a_device_lost_during_the_read_is_not_warned_about_again(self, client_factory, caplog):
        from croom.devices.errors import DeviceUnavailable
        devices = StubDevices()

        async def gone(*args, **kwargs):
            raise DeviceUnavailable("camera call failed: [Errno 19] No such device")

        devices.camera.read_zoom = gone      # RoomCamera warns about this itself, as it marks itself unavailable
        client = await client_factory(make_service(devices=devices))
        assert (await client.get("/api/status")).status == 200
        assert [r for r in caplog.records if r.levelname == "WARNING" and r.name == "croom.control.service"] == []


TV_NOT_ENABLED = "The TV is not enabled on this device (tv.enabled in its config)"


def json_request(path, body):
    """A POST for a handler to be called with directly, when what matters is what the handler raises: the server answers
    for an exception with something of its own."""
    raw = json.dumps(body).encode()
    payload = streams.StreamReader(mock.Mock(_reading_paused=False), 2 ** 16, loop=asyncio.get_running_loop())
    payload.feed_data(raw)
    payload.feed_eof()
    return make_mocked_request("POST", path, headers={"Content-Type": "application/json", "Content-Length": str(len(raw))},
                               payload=payload, transport=mock.Mock(is_closing=mock.Mock(return_value=False)))


def watching(service, handler_name):
    """What reaches a handler, noted as it happens: `.arrived` has a path for each request that has come, `.requests` the requests,
    `.cancelled` a path for each that was cancelled (the test server does that when a client goes away), `.responses` what the
    others were answered with and `.finished` a path for each that is over, however. A test that holds one request in the handler
    and sends another waits for this to say that the other has arrived, and does not guess at a time. Call it before the service
    is started: the routes are made then."""
    seen = SimpleNamespace(arrived=[], requests=[], cancelled=[], responses=[], finished=[])
    handler = getattr(service, handler_name)

    async def watched(request):
        seen.arrived.append(request.path)
        seen.requests.append(request)
        try:
            response = await handler(request)
            seen.responses.append(response)
            return response
        except asyncio.CancelledError:
            seen.cancelled.append(request.path)
            raise
        finally:
            seen.finished.append(request.path)

    setattr(service, handler_name, watched)
    return seen


class TestTvStatus:
    """The `tv` block of /api/status (spec 2026-10-13 TV remote and touchpad, section 6)."""

    async def test_the_status_carries_what_the_tv_service_reports_at_each_poll(self, client_factory):
        tv = StubTv()
        client = await client_factory(make_service(tv=tv))
        assert (await (await client.get("/api/status")).json())["tv"] == tv.status()
        tv.volume = 31
        assert (await (await client.get("/api/status")).json())["tv"]["volume"] == 31

    async def test_a_room_without_a_tv_service_has_a_tv_that_is_neither_configured_nor_paired(self, client_factory):
        client = await client_factory(make_service())
        assert (await (await client.get("/api/status")).json())["tv"] == {"configured": False, "paired": False}

    async def test_a_tv_block_that_cannot_be_read_never_breaks_the_status(self, client_factory, caplog):
        tv = StubTv()
        tv.fail_status = RuntimeError("the TV module has a bug")
        client = await client_factory(make_service(meeting=StubMeeting(), tv=tv))
        for _ in range(3):
            response = await client.get("/api/status")
            assert response.status == 200
        data = await response.json()
        assert data["tv"] == {"configured": False, "paired": False}
        assert data["platforms"] == ["zoom", "google_meet"] and data["meeting"]["state"] == "idle"     # the rest is all there
        said = [r for r in caplog.records if r.levelname == "WARNING" and "the TV module has a bug" in r.getMessage()]
        assert len(said) == 1 and said[0].exc_info is not None        # once, with its traceback, though every poll fails
        tv.fail_status = RuntimeError("a different bug")
        await client.get("/api/status")
        assert len([r for r in caplog.records if "a different bug" in r.getMessage()]) == 1

    async def test_what_the_tv_calls_itself_goes_out_as_it_is_and_not_as_markup(self, client_factory):
        tv = StubTv()
        tv.name = "Tom & Jerry's <TV>"
        client = await client_factory(make_service(tv=tv))
        text = await (await client.get("/api/status")).text()
        assert "Tom & Jerry's <TV>" in text and "&amp;" not in text and "&lt;" not in text

    def test_the_stub_has_the_keys_of_the_real_service(self, tmp_path):
        from croom.tv.keystore import KeyStore
        from croom.tv.service import TvService

        def nothing(*args):
            raise AssertionError("a status asks nothing of the TV")
        real = TvService(TvConfig(), KeyStore(tmp_path / "tv-client-key"), nothing, nothing).status()
        for stub in (StubTv(), StubTv(configured=False, paired=False)):
            assert set(stub.status()) == set(real)


class TestTvControl:
    """POST /api/tv/control answers like the meeting's controls: 200 with the state, 409 with words."""

    async def test_a_control_is_carried_out_and_answered_with_the_status_the_tv_shows_after_it(self, client_factory):
        tv = StubTv()
        client = await client_factory(make_service(tv=tv))
        response = await client.post("/api/tv/control", json={"name": "volume", "value": 40})
        assert response.status == 200
        body = await response.json()
        assert body == {"tv": tv.status()} and body["tv"]["volume"] == 40
        assert tv.calls == [("control", "volume", 40)]

    @pytest.mark.parametrize("body, value", [
        ({"name": "volume_up"}, None), ({"name": "volume_up", "value": None}, None), ({"name": "mute", "value": False}, False),
        ({"name": "volume", "value": 0}, 0), ({"name": "volume", "value": 12.5}, 12.5), ({"name": "channel", "value": "7-1"}, "7-1"),
        ({"name": "toast", "value": ""}, ""), ({"name": "x", "value": [1, "a"]}, [1, "a"]),
        ({"name": "x", "value": {"a": {"b": None}}}, {"a": {"b": None}}),
    ])
    async def test_the_value_goes_to_the_tv_service_as_it_was_sent(self, client_factory, body, value):
        tv = StubTv()
        client = await client_factory(make_service(tv=tv))
        assert (await client.post("/api/tv/control", json=body)).status == 200
        assert tv.calls == [("control", body["name"], value)]

    @pytest.mark.parametrize("body", [
        {}, {"value": 3}, {"name": ""}, {"name": "   "}, {"name": None}, {"name": 5}, {"name": True}, {"name": ["volume_up"]},
        {"name": {"volume_up": 1}},
    ])
    async def test_a_body_without_the_name_of_a_control_is_a_400(self, client_factory, body):
        tv = StubTv()
        client = await client_factory(make_service(tv=tv))
        response = await client.post("/api/tv/control", json=body)
        assert response.status == 400 and "name" in (await response.json())["error"]
        assert tv.calls == []

    @pytest.mark.parametrize("text", ['["volume_up"]', '"volume_up"', "5", "null", "not json", "{"])
    async def test_a_body_that_is_not_a_json_object_is_a_400(self, client_factory, text):
        tv = StubTv()
        client = await client_factory(make_service(tv=tv))
        response = await client.post("/api/tv/control", data=text, headers={"Content-Type": "application/json"})
        assert response.status == 400 and tv.calls == []

    async def test_a_body_that_is_not_declared_json_is_a_415(self, client_factory):
        """A form on another site can post to the room, and cannot post JSON."""
        tv = StubTv()
        client = await client_factory(make_service(tv=tv))
        for kwargs in ({"data": '{"name": "volume_up"}', "headers": {"Content-Type": "text/plain"}}, {"data": {"name": "volume_up"}}):
            assert (await client.post("/api/tv/control", **kwargs)).status == 415
        assert tv.calls == []

    async def test_what_the_tv_cannot_do_is_a_409_in_the_words_the_service_gave(self, client_factory):
        tv = StubTv()
        client = await client_factory(make_service(tv=tv))
        tv.fail_control = TvError("The TV is not answering at 10.0.0.41")
        response = await client.post("/api/tv/control", json={"name": "volume_up"})
        assert response.status == 409 and await response.json() == {"error": "The TV is not answering at 10.0.0.41"}
        words = "The TV at 10.0.0.41 did not turn on: " + "check that Wake-on-LAN is enabled on it " * 5     # (long: nothing is cut)
        tv.fail_control = TvError(words)
        assert (await (await client.post("/api/tv/control", json={"name": "power", "value": "on"})).json())["error"] == words
        tv.fail_control = None
        assert (await client.post("/api/tv/control", json={"name": "volume_up"})).status == 200      # and nothing is stuck

    async def test_any_other_failure_is_a_409_in_words_and_is_logged(self, client_factory, caplog):
        tv = StubTv()
        tv.fail_control = RuntimeError("socket closed")
        client = await client_factory(make_service(tv=tv))
        response = await client.post("/api/tv/control", json={"name": "volume_up"})
        assert response.status == 409 and await response.json() == {"error": "The TV did not do that: socket closed"}
        said = [r for r in caplog.records if r.levelname == "WARNING" and "socket closed" in r.getMessage()]
        assert len(said) == 1 and said[0].exc_info is not None

    @pytest.mark.parametrize("failure, reason", [
        (RuntimeError(""), "RuntimeError"), (KeyError(), "KeyError"), (RuntimeError("two\n   lines\x1b[2J"), "two lines[2J"),
        (RuntimeError("x" * 500), "x" * 160),
    ])
    async def test_the_reason_is_one_printable_line_and_never_nothing(self, client_factory, failure, reason):
        tv = StubTv()
        tv.fail_control = failure
        client = await client_factory(make_service(tv=tv))
        response = await client.post("/api/tv/control", json={"name": "volume_up"})
        assert response.status == 409 and (await response.json())["error"] == "The TV did not do that: " + reason

    async def test_a_cancellation_is_never_answered_as_a_failure(self, client_factory):
        tv = StubTv()
        tv.fail_control = asyncio.CancelledError()
        service = make_service(tv=tv)
        with pytest.raises(asyncio.CancelledError):
            await service._handle_tv_control(json_request("/api/tv/control", {"name": "volume_up"}))
        tv.fail_control = None
        client = await client_factory(service)
        assert (await client.post("/api/tv/control", json={"name": "volume_up"})).status == 200     # and the lock was let go of

    async def test_controls_are_carried_out_one_at_a_time(self, client_factory):
        """A second tap on Turn on during a wake must not start a second wake, and a volume key during one must not be sent
        down the TV's socket in the middle of it: the TV service does not hold controls apart, so the page's route does."""
        tv = StubTv()
        tv.gate = asyncio.Event()
        service = make_service(tv=tv)
        seen = watching(service, "_handle_tv_control")
        client = await client_factory(service)
        taps = [asyncio.ensure_future(client.post("/api/tv/control", json=body)) for body in (
            {"name": "power", "value": "on"}, {"name": "power", "value": "on"}, {"name": "volume_up"})]
        await wait_until(lambda: len(seen.arrived) == 3)
        await asyncio.sleep(0.05)                      # (time for a second control to begin, if one could)
        assert tv.order == [("start", "power")]        # one is with the TV, and the other two wait for it
        tv.gate.set()
        assert [response.status for response in await asyncio.gather(*taps)] == [200, 200, 200]
        assert [kind for kind, _ in tv.order] == ["start", "end"] * 3          # none began before the one before it ended
        assert sorted(name for kind, name in tv.order if kind == "start") == ["power", "power", "volume_up"]

    async def test_a_control_that_failed_does_not_hold_up_the_next(self, client_factory):
        tv = StubTv()
        client = await client_factory(make_service(tv=tv))
        for failure in (TvError("not now"), RuntimeError("boom"), None):
            tv.fail_control = failure
            response = await asyncio.wait_for(client.post("/api/tv/control", json={"name": "volume_up"}), 2.0)
            assert response.status == (409 if failure else 200)
        assert [kind for kind, _ in tv.order] == ["start", "end"] * 3

    async def test_a_body_that_is_no_use_is_refused_at_once_while_a_control_is_with_the_tv(self, client_factory):
        """It has nothing to wait for the TV for: the check is made before the control takes its turn."""
        tv = StubTv()
        tv.gate = asyncio.Event()
        client = await client_factory(make_service(tv=tv))
        try:
            holding = asyncio.ensure_future(client.post("/api/tv/control", json={"name": "power", "value": "on"}))
            await wait_until(lambda: tv.order == [("start", "power")])
            for body in ({}, {"name": ""}, {"value": 3}):
                response = await asyncio.wait_for(client.post("/api/tv/control", json=body), 2.0)
                assert response.status == 400, body
        finally:
            tv.gate.set()
        assert (await holding).status == 200
        assert tv.calls == [("control", "power", "on")]

    async def test_without_a_tv_service_a_control_is_a_503_before_its_body_is_looked_at(self, client_factory):
        client = await client_factory(make_service())
        for body in ({"name": "volume_up"}, {}, ["not", "an", "object"], []):
            response = await client.post("/api/tv/control", json=body)
            assert response.status == 503 and (await response.json())["error"] == TV_NOT_ENABLED


class TestTvPairAndForget:
    async def test_pair_answers_with_the_status_once_the_person_has_accepted_the_prompt(self, client_factory):
        tv = StubTv(paired=False)
        tv.pair_gate = asyncio.Event()                  # the prompt is up on the TV, and nobody has accepted it yet
        client = await client_factory(make_service(tv=tv))
        pairing = asyncio.ensure_future(client.post("/api/tv/pair", json={}))
        await wait_until(lambda: tv.calls == [("pair",)])
        await asyncio.sleep(0.05)
        assert not pairing.done()                       # the request waits with the person: the route sets no time of its own
        tv.pair_gate.set()
        response = await pairing
        assert response.status == 200 and await response.json() == {"tv": tv.status()} and tv.status()["paired"] is True
        assert tv.calls == [("pair",)]

    async def test_forget_answers_with_the_status(self, client_factory):
        tv = StubTv()
        client = await client_factory(make_service(tv=tv))
        response = await client.post("/api/tv/forget", json={})
        assert response.status == 200 and await response.json() == {"tv": tv.status()} and tv.status()["paired"] is False
        assert tv.calls == [("forget",)]

    @pytest.mark.parametrize("path, failing, words", [
        ("/api/tv/pair", "fail_pair", "Pairing is already waiting for the TV"),
        ("/api/tv/pair", "fail_pair", "The TV is not set up: use Find TVs and Pair under Options \u203a TV"),
        ("/api/tv/pair", "fail_pair", "Pairing with the TV at 10.0.0.41 did not finish: check that the TV is on"),
        ("/api/tv/forget", "fail_forget", "The TV could not be forgotten: its key could not be removed from /data/tv-client-key"),
    ])
    async def test_what_cannot_be_done_is_a_409_in_words(self, client_factory, path, failing, words):
        tv = StubTv()
        setattr(tv, failing, TvError(words))
        client = await client_factory(make_service(tv=tv))
        response = await client.post(path, json={})
        assert response.status == 409 and await response.json() == {"error": words}

    @pytest.mark.parametrize("path, failing", [("/api/tv/pair", "fail_pair"), ("/api/tv/forget", "fail_forget")])
    async def test_any_other_failure_is_a_409_in_words_and_is_logged_here_too(self, client_factory, caplog, path, failing):
        tv = StubTv()
        setattr(tv, failing, OSError("disk gone"))
        client = await client_factory(make_service(tv=tv))
        response = await client.post(path, json={})
        assert response.status == 409 and await response.json() == {"error": "The TV did not do that: disk gone"}
        assert [r for r in caplog.records if r.levelname == "WARNING" and "disk gone" in r.getMessage()]

    async def test_pairing_and_the_controls_do_not_hold_each_other_up(self, client_factory):
        """Pairing is the service's own single-flight (a second one is refused in words), and waits for a person: it is not
        held behind the controls' one-at-a-time, and the controls are not held behind it."""
        tv = StubTv()
        tv.gate, tv.pair_gate = asyncio.Event(), asyncio.Event()
        service = make_service(tv=tv)
        client = await client_factory(service)
        try:
            control = asyncio.ensure_future(client.post("/api/tv/control", json={"name": "power", "value": "on"}))
            await wait_until(lambda: tv.order == [("start", "power")])
            forgotten = await asyncio.wait_for(client.post("/api/tv/forget", json={}), 2.0)      # while the control is with the TV
            assert forgotten.status == 200
            pairing = asyncio.ensure_future(client.post("/api/tv/pair", json={}))
            await wait_until(lambda: ("pair",) in tv.calls)
            tv.gate.set()
            assert (await asyncio.wait_for(control, 2.0)).status == 200
            assert (await asyncio.wait_for(client.post("/api/tv/control", json={"name": "volume_up"}), 2.0)).status == 200  # while the pair waits
            assert not pairing.done()
        finally:
            tv.gate.set()
            tv.pair_gate.set()
        assert (await asyncio.wait_for(pairing, 2.0)).status == 200

    async def test_without_a_tv_service_pair_and_forget_are_a_503(self, client_factory):
        client = await client_factory(make_service())
        for path in ("/api/tv/pair", "/api/tv/forget"):
            for body in ({}, []):                       # (a body that is not an object is not looked at: there is no TV to ask)
                response = await client.post(path, json=body)
                assert response.status == 503 and (await response.json())["error"] == TV_NOT_ENABLED

    async def test_every_tv_post_wants_a_json_object(self, client_factory):
        tv = StubTv()
        client = await client_factory(make_service(tv=tv))
        for path in ("/api/tv/control", "/api/tv/pair", "/api/tv/forget"):
            assert (await client.post(path, data="{}", headers={"Content-Type": "text/plain"})).status == 415, path
            assert (await client.post(path, json=["not", "an", "object"])).status == 400, path
        assert tv.calls == []
        bare = await client_factory(make_service())             # the type of the body is looked at before the TV service is
        for path in ("/api/tv/control", "/api/tv/pair", "/api/tv/forget"):
            assert (await bare.post(path, data="{}", headers={"Content-Type": "text/plain"})).status == 415, path


class TestTvDiscover:
    """GET /api/tv/discover: the LG TVs on the network. Every search here is a StubSearch: nothing is put on the network."""

    async def test_the_tvs_that_answer_are_listed(self, client_factory):
        search = StubSearch([BOARDROOM])
        client = await client_factory(make_service(tv=StubTv(), discover=search))
        response = await client.get("/api/tv/discover")
        assert response.status == 200 and await response.json() == {"tvs": [BOARDROOM]}
        assert search.calls == 1

    async def test_it_answers_without_a_tv_service_because_that_is_when_it_is_needed(self, client_factory):
        search = StubSearch([BOARDROOM])
        client = await client_factory(make_service(discover=search))
        response = await client.get("/api/tv/discover")
        assert response.status == 200 and await response.json() == {"tvs": [BOARDROOM]}

    async def test_no_tv_answering_is_an_empty_list_and_not_a_failure(self, client_factory):
        client = await client_factory(make_service(discover=StubSearch()))
        response = await client.get("/api/tv/discover")
        assert response.status == 200 and await response.json() == {"tvs": []}

    async def test_what_a_device_calls_itself_goes_out_as_it_is_and_not_as_markup(self, client_factory):
        odd = {"host": "10.0.0.5", "name": "Tom & Jerry's <TV>", "location": "http://10.0.0.5/d.xml?a=1&b=2"}
        client = await client_factory(make_service(discover=StubSearch([odd])))
        text = await (await client.get("/api/tv/discover")).text()
        assert json.loads(text) == {"tvs": [odd]} and "Tom & Jerry's <TV>" in text and "&amp;" not in text

    async def test_the_default_search_is_the_real_one_and_this_module_keeps_it_off_the_network(
            self, client_factory, no_packet_leaves_this_machine):
        from croom.tv import discover as discovery
        assert make_service()._discover_tvs is discover_tvs
        # The real search sends its datagram through discovery._multicast: here that is the fixture's, which refuses. (Checked
        # first, because the test below it would otherwise be the one to put a packet on the network.)
        assert discovery._multicast is no_packet_leaves_this_machine and discovery._get is no_packet_leaves_this_machine
        client = await client_factory(make_service())
        response = await client.get("/api/tv/discover")
        assert response.status == 409 and "reached the real network" in (await response.json())["error"]

    async def test_two_requests_at_once_share_one_search(self, client_factory):
        search = StubSearch([BOARDROOM])
        search.gate = asyncio.Event()
        service = make_service(discover=search)
        seen = watching(service, "_handle_tv_discover")
        client = await client_factory(service)
        asked = [asyncio.ensure_future(client.get("/api/tv/discover")) for _ in range(2)]
        await wait_until(lambda: len(seen.arrived) == 2)   # the second has come while the first search is still running
        await wait_until(lambda: search.calls == 1)        # (and the search is a task of its own: it has begun a turn after the first came)
        assert search.calls == 1
        search.gate.set()
        for response in await asyncio.gather(*asked):
            assert response.status == 200 and await response.json() == {"tvs": [BOARDROOM]}
        assert search.calls == 1                           # one search answered both

    async def test_a_request_after_the_search_ended_starts_a_new_search(self, client_factory):
        search = StubSearch([BOARDROOM])
        client = await client_factory(make_service(discover=search))
        for expected in (1, 2, 3):
            assert (await client.get("/api/tv/discover")).status == 200
            assert search.calls == expected                # (the answer of a search is not kept: the network changes)

    async def test_a_search_that_cannot_be_sent_is_a_409_in_words_and_is_not_remembered(self, client_factory):
        words = "The search for TVs could not be sent: Network is unreachable"
        search = StubSearch(fail=TvError(words))
        client = await client_factory(make_service(discover=search))
        response = await client.get("/api/tv/discover")
        assert response.status == 409 and await response.json() == {"error": words}
        search.fail = None
        response = await client.get("/api/tv/discover")
        assert response.status == 200 and search.calls == 2

    async def test_requests_that_share_a_search_that_fails_all_hear_why(self, client_factory):
        words = "The search for TVs could not be started: no route"
        search = StubSearch(fail=TvError(words))
        search.gate = asyncio.Event()
        service = make_service(discover=search)
        seen = watching(service, "_handle_tv_discover")
        client = await client_factory(service)
        asked = [asyncio.ensure_future(client.get("/api/tv/discover")) for _ in range(2)]
        await wait_until(lambda: len(seen.arrived) == 2)
        search.gate.set()
        for response in await asyncio.gather(*asked):
            assert response.status == 409 and await response.json() == {"error": words}
        assert search.calls == 1

    async def test_any_other_failure_of_the_search_is_a_409_in_words_and_is_logged(self, client_factory, caplog):
        client = await client_factory(make_service(discover=StubSearch(fail=RuntimeError("socket exploded"))))
        response = await client.get("/api/tv/discover")
        assert response.status == 409
        assert await response.json() == {"error": "The search for TVs did not finish: socket exploded"}
        said = [r for r in caplog.records if r.levelname == "WARNING" and "socket exploded" in r.getMessage()]
        assert len(said) == 1 and said[0].exc_info is not None

    async def test_a_client_that_goes_away_does_not_end_the_search_for_the_others(self, client_factory):
        """The test server, as a person closing the page does, cancels the handler whose client has gone: the search it
        was waiting for is not the handler's to end while another request is waiting for it too."""
        search = StubSearch([BOARDROOM])
        search.gate = asyncio.Event()
        service = make_service(discover=search)
        seen = watching(service, "_handle_tv_discover")
        client = await client_factory(service)
        first = asyncio.ensure_future(client.get("/api/tv/discover"))
        second = asyncio.ensure_future(client.get("/api/tv/discover"))
        await wait_until(lambda: len(seen.arrived) == 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await wait_until(lambda: len(seen.cancelled) == 1)  # the server has ended the handler of the client that went away
        assert search.calls == 1 and search.cancelled == 0  # and the search it was waiting for is still going
        search.gate.set()
        response = await second
        assert response.status == 200 and await response.json() == {"tvs": [BOARDROOM]}
        assert search.calls == 1 and search.cancelled == 0

    async def test_stopping_the_service_ends_a_search_that_is_still_running(self, client_factory):
        search = StubSearch([BOARDROOM])
        search.gate = asyncio.Event()
        service = make_service(discover=search)
        await service.start()
        client = await client_factory(service)
        asked = asyncio.ensure_future(client.get("/api/tv/discover"))
        try:
            await wait_until(lambda: search.calls == 1)
            await asyncio.wait_for(service.stop(), 2.0)
            assert search.cancelled == 1                   # nothing is left running after the service
        finally:
            search.gate.set()
        with pytest.raises(aiohttp.ClientError):           # the request that waited for it is let go of, not answered with a list
            await asked


class TestPointerSocket:
    """WS /ws/pointer: the touchpad's events from the table (spec 2026-10-13 TV remote and touchpad, section 5). A message is a JSON text
    frame `{"target": "tv"|"pi", "type": "move"|"tap"|"right"|"scroll"|"down"|"up", "dx", "dy"}`. What cannot be done is answered as a
    `{"error": words}` frame, and a frame that is no use never closes the socket."""

    MOVE = {"target": "tv", "type": "move", "dx": 1, "dy": 0}

    @staticmethod
    async def says(ws):
        return await asyncio.wait_for(ws.receive_json(), 2.0)

    def test_the_stubs_have_the_calls_of_the_real_ones(self):
        import inspect
        from croom.tv.client import TvClient
        from croom.tv.service import TvService

        def shape(cls, name):
            """What a call takes, by the names and defaults of its parameters (not self)."""
            return [(p.name, p.default) for p in list(inspect.signature(getattr(cls, name)).parameters.values())[1:]]
        for name in ("move", "click", "scroll"):
            assert shape(StubCursor, name) == shape(TvClient, name)
        for name in ("open", "move", "press", "release", "click", "scroll", "close"):
            assert shape(StubMouse, name) == shape(PiMouse, name)
        assert shape(StubTv, "pointer") == shape(TvService, "pointer")

    async def test_events_reach_the_tvs_cursor_and_the_room_pis_mouse(self, client_factory):
        tv, mouse = StubTv(), StubMouse()
        client = await client_factory(make_service(tv=tv, mouse=mouse))
        async with client.ws_connect("/ws/pointer") as ws:
            for message in ({"target": "tv", "type": "move", "dx": 5, "dy": -3}, {"target": "tv", "type": "tap"},
                            {"target": "tv", "type": "scroll", "dy": 2},
                            {"target": "pi", "type": "move", "dx": 1, "dy": 2}, {"target": "pi", "type": "tap"},
                            {"target": "pi", "type": "right"}, {"target": "pi", "type": "scroll", "dy": -1},
                            {"target": "pi", "type": "down"}, {"target": "pi", "type": "up"}):
                await ws.send_json(message)
            await wait_until(lambda: tv.cursor.calls == [("move", 5, -3, 0), ("click",), ("scroll", 0, 2)])
            await wait_until(lambda: mouse.calls == [("move", 1, 2), ("click", "left"), ("click", "right"), ("scroll", -1),
                                                     ("press", "left"), ("release", "left")])
        assert tv.calls == []                                  # (the cursor is not a control)

    async def test_a_drag_on_the_tv_is_moves_with_the_button_down_and_an_end(self, client_factory):
        tv = StubTv()
        client = await client_factory(make_service(tv=tv, mouse=StubMouse()))
        async with client.ws_connect("/ws/pointer") as ws:
            for message in ({"target": "tv", "type": "down"}, {"target": "tv", "type": "move", "dx": 3, "dy": 4}, {"target": "tv", "type": "up"}):
                await ws.send_json(message)
            await wait_until(lambda: tv.cursor.calls == [("move", 3, 4, 1), ("move", 0, 0, 0)])

    @pytest.mark.parametrize("frame, words", [
        ("not json at all", BAD_MESSAGE), ("{", BAD_MESSAGE), ("", BAD_MESSAGE), ("[1, 2]", BAD_MESSAGE), ("3", BAD_MESSAGE),
        ("null", BAD_MESSAGE), ('"move"', BAD_MESSAGE), ("[" * 4000, BAD_MESSAGE),
        ('{"type": "move"}', BAD_TARGET), ('{"target": "x", "type": "move"}', BAD_TARGET),
        ('{"target": "tv"}', BAD_TYPE), ('{"target": "tv", "type": "wiggle"}', BAD_TYPE),
        ('{"target": "tv", "type": "move", "dx": "3"}', "dx is a number"), ('{"target": "pi", "type": "move", "dy": NaN}', "dy is a number"),
        ('{"target": "tv", "type": "right"}', "The TV's cursor has no right button"),
        ('{"target": "pi", "type": "keys", "text": "a"}', "Typing into the room Pi comes in a later version"),
    ])
    async def test_a_frame_that_is_no_use_is_answered_in_words_and_the_socket_goes_on(self, client_factory, frame, words):
        tv, mouse = StubTv(), StubMouse()
        client = await client_factory(make_service(tv=tv, mouse=mouse))
        async with client.ws_connect("/ws/pointer") as ws:
            await ws.send_str(frame)
            assert await self.says(ws) == {"error": words}
            assert not ws.closed
            await ws.send_json({"target": "pi", "type": "tap"})
            await wait_until(lambda: mouse.calls == [("click", "left")])                          # and it still works
        assert tv.cursor.calls == []

    async def test_a_frame_nested_deeper_than_the_parser_takes_is_answered_in_words_and_the_socket_goes_on(self, client_factory, monkeypatch):
        """Python 3.11's JSON parser stops with a RecursionError (not a ValueError) at about a thousand brackets, which a frame of up to
        4 KiB can hold; 3.12's takes far more, so the parser is made to say it, as 3.11's does."""
        def too_deep(text):
            raise RecursionError("maximum recursion depth exceeded while decoding a JSON array from a unicode string")
        mouse = StubMouse()
        client = await client_factory(make_service(mouse=mouse))
        monkeypatch.setattr("croom.control.service.json", SimpleNamespace(loads=too_deep))
        async with client.ws_connect("/ws/pointer") as ws:
            await ws.send_str("[" * 2000 + "]" * 2000)
            assert await self.says(ws) == {"error": BAD_MESSAGE}
            assert not ws.closed

    async def test_a_binary_frame_is_answered_in_words_and_the_socket_goes_on(self, client_factory):
        mouse = StubMouse()
        client = await client_factory(make_service(mouse=mouse))
        async with client.ws_connect("/ws/pointer") as ws:
            await ws.send_bytes(b'{"target": "pi", "type": "tap"}')
            assert await self.says(ws) == {"error": BAD_MESSAGE}
            await ws.send_json({"target": "pi", "type": "tap"})
            await wait_until(lambda: mouse.calls == [("click", "left")])

    async def test_a_tv_that_cannot_be_told_anything_is_answered_once_for_each_frame_and_the_socket_stays_open(self, client_factory):
        tv = StubTv()
        tv.fail_pointer = TvError("Pair the TV under Options › TV")
        client = await client_factory(make_service(tv=tv, mouse=StubMouse()))
        async with client.ws_connect("/ws/pointer") as ws:
            for message in (self.MOVE, {"target": "tv", "type": "tap"}, self.MOVE):
                await ws.send_json(message)
            for _ in range(3):
                assert await self.says(ws) == {"error": "Pair the TV under Options › TV"}
            tv.fail_pointer = None                                              # the person pairs the TV
            await ws.send_json({"target": "tv", "type": "tap"})
            await wait_until(lambda: tv.cursor.calls == [("click",)])
            assert not ws.closed

    async def test_a_move_the_tv_refused_on_its_way_is_answered_by_the_next_frame(self, client_factory):
        tv = StubTv()
        client = await client_factory(make_service(tv=tv, mouse=StubMouse()))
        async with client.ws_connect("/ws/pointer") as ws:
            async def refusing_move(d_x, d_y, down=0):
                raise ConnectionResetError("Cannot write to closing transport")
            tv.cursor.move = refusing_move
            await ws.send_json(self.MOVE)
            await asyncio.sleep(0.05)                                           # (it is sent in the background: nothing is answered yet)
            await ws.send_json(self.MOVE)
            assert await self.says(ws) == {"error": "The TV did not accept that: Cannot write to closing transport"}
            assert not ws.closed

    async def test_a_mouse_that_cannot_be_made_is_answered_in_its_words_and_the_socket_stays_open(self, client_factory):
        words = "The room Pi's mouse is not allowed to use /dev/uinput: the user the agent runs as is not in the input group"
        mouse = StubMouse()
        mouse.fail = TvError(words)
        client = await client_factory(make_service(tv=StubTv(), mouse=mouse))
        async with client.ws_connect("/ws/pointer") as ws:
            await ws.send_json({"target": "pi", "type": "tap"})
            await ws.send_json({"target": "pi", "type": "move", "dx": 1, "dy": 1})
            assert await self.says(ws) == {"error": words} and await self.says(ws) == {"error": words}
            mouse.fail = None
            await ws.send_json({"target": "pi", "type": "tap"})
            await wait_until(lambda: mouse.calls == [("click", "left")])

    async def test_without_a_mouse_of_its_own_the_service_has_the_real_one_and_says_what_it_lacks(self, client_factory, monkeypatch):
        monkeypatch.setitem(sys.modules, "evdev", None)                          # (nothing here may make a device on this machine)
        service = make_service(tv=StubTv())
        assert isinstance(service._mouse, PiMouse) and isinstance(ControlService.from_config(Config())._mouse, PiMouse)
        client = await client_factory(service)
        async with client.ws_connect("/ws/pointer") as ws:
            await ws.send_json({"target": "pi", "type": "tap"})
            assert "pip install evdev" in (await self.says(ws))["error"]
            assert not ws.closed

    async def test_no_test_can_make_a_real_device_on_the_machine_it_runs_on(self, client_factory, tmp_path, monkeypatch):
        """A service with no mouse of its own has the real one, which uses evdev wherever evdev is installed: a test that opened a
        pointer socket there would put a mouse on the machine's desktop. The control tests keep evdev out (tests/unit/control/conftest.py),
        so that with a stand-in evdev on the path, which would make a device and note it in a file, nothing is made."""
        package = tmp_path / "evdev"
        package.mkdir()
        made = tmp_path / "made-a-device"
        (package / "__init__.py").write_text(f"class UInput:\n    def __init__(self, *args, **kwargs):\n        open({str(made)!r}, 'w').close()\n"
                                             "    def write(self, *args): pass\n    def syn(self): pass\n    def close(self): pass\n")
        monkeypatch.syspath_prepend(str(tmp_path))
        client = await client_factory(make_service(tv=StubTv()))
        async with client.ws_connect("/ws/pointer") as ws:
            await ws.send_json({"target": "pi", "type": "tap"})
            assert "pip install evdev" in (await self.says(ws))["error"]
        assert not made.exists()

    async def test_a_socket_that_opens_makes_the_room_pis_mouse_off_the_loop_and_two_sockets_make_one(self, client_factory):
        """The device takes a moment to appear (evdev waits for it and looks through /dev/input), and the desktop takes it up a moment
        after: it is made when a socket opens, in a thread, so that it is there before a finger is on the pad."""
        made = []

        def factory():
            made.append(threading.get_ident())
            return StubDevice()
        client = await client_factory(make_service(mouse=PiMouse(uinput_factory=factory)))
        async with client.ws_connect("/ws/pointer") as first, client.ws_connect("/ws/pointer") as second:
            await wait_until(lambda: made)
            await first.send_json({"target": "pi", "type": "tap"})                  # (the sockets have been looked at by now)
            await second.send_json({"target": "pi", "type": "tap"})
            await asyncio.sleep(0.1)
        assert len(made) == 1 and made[0] != threading.get_ident()                   # one device, and not made on the loop's thread

    async def test_making_the_mouse_does_not_hold_the_loop_up(self, client_factory):
        gate, started = threading.Event(), threading.Event()

        def slow_factory():
            started.set()
            gate.wait(3.0)
            return StubDevice()
        client = await client_factory(make_service(mouse=PiMouse(uinput_factory=slow_factory)))
        loop = asyncio.get_running_loop()
        try:
            t0 = loop.time()
            async with client.ws_connect("/ws/pointer"):
                await wait_until(started.is_set)
                response = await asyncio.wait_for(client.get("/api/status"), 2.0)       # a request is served while the device is being made
                assert response.status == 200 and not gate.is_set()
                assert loop.time() - t0 < 1.5                                          # (it did not wait for the 3 s that the factory takes)
                gate.set()                                                             # (the socket's own end waits for the device)
        finally:
            gate.set()

    async def test_a_mouse_that_cannot_be_made_when_a_socket_opens_leaves_every_event_answered_in_words(self, client_factory, caplog):
        attempts = []

        def failing():
            attempts.append(1)
            raise PermissionError(13, "Permission denied", "/dev/uinput")
        tv = StubTv()
        client = await client_factory(make_service(tv=tv, mouse=PiMouse(uinput_factory=failing)))
        async with client.ws_connect("/ws/pointer") as ws:
            await wait_until(lambda: attempts)                                      # (made when the socket opened, and could not be)
            await ws.send_json({"target": "pi", "type": "tap"})
            answer = (await self.says(ws))["error"]
            assert "not allowed to use /dev/uinput" in answer and "sudo systemctl restart croom" in answer
            assert len(attempts) == 2 and not ws.closed                             # the event tried again, and the socket is open
            await ws.send_json({"target": "tv", "type": "tap"})                     # and the TV's side does not care
            await wait_until(lambda: tv.cursor.calls == [("click",)])
        # the mouse said why not, once (the reason is for the log as well as the page); the control service has nothing to add to it
        assert len([r for r in caplog.records if r.levelname == "WARNING" and r.name == "croom.tv.pointer"]) == 1
        assert [r for r in caplog.records if r.levelname == "WARNING" and r.name == "croom.control.service"] == []

    async def test_a_mouse_that_could_not_be_made_when_the_socket_opened_is_made_by_the_first_event_when_it_can_be(self, client_factory):
        devices = []

        def factory():
            if not devices:
                devices.append(None)
                raise PermissionError(13, "Permission denied", "/dev/uinput")
            devices.append(StubDevice())
            return devices[-1]
        client = await client_factory(make_service(mouse=PiMouse(uinput_factory=factory)))
        async with client.ws_connect("/ws/pointer") as ws:
            await wait_until(lambda: devices)
            await ws.send_json({"target": "pi", "type": "move", "dx": 1, "dy": 0})
            await wait_until(lambda: len(devices) == 2 and devices[1].writes == [(CODES["EV_REL"], CODES["REL_X"], 1), "syn"])

    async def test_a_mouse_that_blows_up_when_it_is_made_does_not_close_the_socket(self, client_factory, caplog):
        mouse = StubMouse()
        mouse.fail = RuntimeError("a bug in making the mouse")
        client = await client_factory(make_service(mouse=mouse))
        async with client.ws_connect("/ws/pointer") as ws:
            await wait_until(lambda: mouse.opened == 1)
            mouse.fail = None
            await ws.send_json({"target": "pi", "type": "tap"})
            await wait_until(lambda: mouse.calls == [("click", "left")])
        assert [r for r in caplog.records if r.levelname == "WARNING" and r.exc_info and "a bug in making the mouse" in str(r.exc_info[1])]

    async def test_a_room_without_a_tv_service_answers_for_the_tv_and_still_has_its_pi(self, client_factory):
        mouse = StubMouse()
        client = await client_factory(make_service(mouse=mouse))
        async with client.ws_connect("/ws/pointer") as ws:
            await ws.send_json(self.MOVE)
            assert await self.says(ws) == {"error": TV_NOT_ENABLED}
            await ws.send_json({"target": "pi", "type": "tap"})
            await wait_until(lambda: mouse.calls == [("click", "left")])

    async def test_an_error_that_is_a_bug_is_answered_in_words_logged_once_and_the_socket_goes_on(self, client_factory, caplog):
        mouse = StubMouse()
        mouse.fail = RuntimeError("the mouse has a bug")
        client = await client_factory(make_service(mouse=mouse))
        async with client.ws_connect("/ws/pointer") as ws:
            for _ in range(3):
                await ws.send_json({"target": "pi", "type": "tap"})
            for _ in range(3):
                assert await self.says(ws) == {"error": "The pointer did not do that: the mouse has a bug"}
            assert not ws.closed
        said = [r for r in caplog.records if r.levelname == "WARNING" and "the mouse has a bug" in r.getMessage()]
        assert len(said) == 1 and said[0].exc_info is not None

    async def test_the_cursor_does_not_wait_behind_a_control_that_is_with_the_tv(self, client_factory):
        tv = StubTv()
        tv.gate = asyncio.Event()                                              # a Turn on, which takes its time
        service = make_service(tv=tv, mouse=StubMouse())
        client = await client_factory(service)
        turn_on = asyncio.ensure_future(client.post("/api/tv/control", json={"name": "power", "value": "on"}))
        try:
            await wait_until(lambda: tv.order == [("start", "power")])
            assert service._tv_busy.locked()
            async with client.ws_connect("/ws/pointer") as ws:
                await ws.send_json({"target": "tv", "type": "move", "dx": 3, "dy": 4})
                await ws.send_json({"target": "tv", "type": "tap"})
                await wait_until(lambda: tv.cursor.calls == [("move", 3, 4, 0), ("click",)])
        finally:
            tv.gate.set()
            await turn_on
        assert tv.calls == [("control", "power", "on")] and service._tv_requests == set()    # (the cursor is not a request that stop() looks for)

    async def test_a_socket_that_closes_lets_a_held_button_go(self, client_factory):
        mouse = StubMouse()
        client = await client_factory(make_service(mouse=mouse))
        ws = await client.ws_connect("/ws/pointer")
        await ws.send_json({"target": "pi", "type": "down"})
        await wait_until(lambda: mouse.calls == [("press", "left")])
        await ws.close()                                                       # the page closed it
        await wait_until(lambda: mouse.calls == [("press", "left"), ("release", "left")])

    async def test_a_page_that_goes_quiet_without_a_word_is_let_go_of_and_its_button_released(self, client_factory, monkeypatch):
        """A page whose network went away leaves the connection open at this end: the sockets are pinged, and one that does not answer
        is closed (a button the page held would stay down on the room Pi's desktop for as long as it took the system to notice)."""
        monkeypatch.setattr(ControlService, "POINTER_HEARTBEAT_S", 0.2)
        mouse = StubMouse()
        client = await client_factory(make_service(mouse=mouse))
        async with aiohttp.ClientSession() as page:
            ws = await page.ws_connect(client.make_url("/ws/pointer"), autoping=False)       # (and nobody is reading: no pong is sent)
            await ws.send_json({"target": "pi", "type": "down"})
            await wait_until(lambda: mouse.calls == [("press", "left")])
            await wait_until(lambda: mouse.calls == [("press", "left"), ("release", "left")], timeout=3.0)

    async def test_the_socket_is_for_websockets_only(self, client_factory):
        client = await client_factory(make_service(mouse=StubMouse()))
        response = await client.get("/ws/pointer")
        assert response.status == 400

    async def test_a_socket_that_is_dropped_lets_a_held_button_go_too(self, client_factory):
        mouse = StubMouse()
        client = await client_factory(make_service(mouse=mouse))
        async with aiohttp.ClientSession() as page:
            ws = await page.ws_connect(client.make_url("/ws/pointer"))
            await ws.send_json({"target": "pi", "type": "down"})
            await wait_until(lambda: mouse.calls == [("press", "left")])
        # closing the session cuts the connection with no close frame: the page is gone without a word, and the button goes up all the same
        await wait_until(lambda: mouse.calls == [("press", "left"), ("release", "left")])

    @pytest.mark.parametrize("how", ["closed", "dropped"])
    async def test_on_the_real_listener_a_socket_that_ends_lets_a_held_button_go(self, how):
        """The listener's handlers are not ended when their client goes away, as the test server's are: the socket's end is seen by
        the handler as a frame (a close) or as the end of the stream."""
        mouse = StubMouse()
        service = make_service(mouse=mouse)
        await service.start()
        try:
            async with aiohttp.ClientSession() as page:
                ws = await page.ws_connect(f"http://127.0.0.1:{service.bound_port}/ws/pointer")
                await ws.send_json({"target": "pi", "type": "down"})
                await wait_until(lambda: mouse.calls == [("press", "left")])
                if how == "closed":
                    await ws.close()
            await wait_until(lambda: mouse.calls == [("press", "left"), ("release", "left")])
            assert service._pointers == {}
        finally:
            await service.stop()

    async def test_each_socket_holds_its_own_button(self, client_factory):
        mouse = StubMouse()
        client = await client_factory(make_service(mouse=mouse))
        first, second = await client.ws_connect("/ws/pointer"), await client.ws_connect("/ws/pointer")
        await first.send_json({"target": "pi", "type": "down"})
        await wait_until(lambda: mouse.calls == [("press", "left")])
        await second.send_json({"target": "pi", "type": "up"})                  # the other page had no button down: nothing is released
        await second.close()
        await asyncio.sleep(0.05)
        assert mouse.calls == [("press", "left")]
        await first.close()
        await wait_until(lambda: mouse.calls == [("press", "left"), ("release", "left")])

    async def test_stopping_the_service_closes_the_sockets_and_lets_go_of_what_is_held(self):
        tv, mouse = StubTv(), StubMouse()
        service = make_service(tv=tv, mouse=mouse)
        await service.start()
        async with aiohttp.ClientSession() as page:
            ws = await page.ws_connect(f"http://127.0.0.1:{service.bound_port}/ws/pointer")
            await ws.send_json({"target": "pi", "type": "down"})
            await wait_until(lambda: mouse.calls == [("press", "left")])
            await asyncio.wait_for(service.stop(), 3.0)
            closed = await asyncio.wait_for(ws.receive(), 2.0)
            assert closed.type == aiohttp.WSMsgType.CLOSE and closed.data == 1001          # going away
        assert mouse.calls == [("press", "left"), ("release", "left")] and mouse.closed == 1
        assert service._pointers == {}

    async def test_stopping_the_service_ends_a_move_that_the_tv_does_not_take(self):
        tv = StubTv()
        tv.cursor.gate = asyncio.Event()                                       # a TV that does not take what it is sent
        service = make_service(tv=tv, mouse=StubMouse())
        await service.start()
        loop = asyncio.get_running_loop()
        try:
            async with aiohttp.ClientSession() as page:
                ws = await page.ws_connect(f"http://127.0.0.1:{service.bound_port}/ws/pointer")
                await ws.send_json(self.MOVE)
                await wait_until(lambda: tv.cursor.calls == [("move", 1, 0, 0)])
                t0 = loop.time()
                await asyncio.wait_for(service.stop(), 3.0)
                assert loop.time() - t0 < 1.0                                    # not the time the call is given (CALL_TIMEOUT_S)
        finally:
            tv.cursor.gate.set()
        assert tv.cursor.cancelled == 1

    async def test_stopping_the_service_ends_the_move_of_a_socket_that_has_just_closed_too(self):
        """A socket that the page closed while a move was with the TV is let wait for that move (it finishes, as a dropped socket's does):
        the service's stop does not wait for it."""
        tv = StubTv()
        tv.cursor.gate = asyncio.Event()
        service = make_service(tv=tv, mouse=StubMouse())
        await service.start()
        loop = asyncio.get_running_loop()
        try:
            async with aiohttp.ClientSession() as page:
                ws = await page.ws_connect(f"http://127.0.0.1:{service.bound_port}/ws/pointer")
                await ws.send_json(self.MOVE)
                await wait_until(lambda: tv.cursor.calls == [("move", 1, 0, 0)])
                await ws.close()
                await asyncio.sleep(0.1)                                         # the socket's end is waiting for the move that is with the TV
                assert len(service._pointers) == 1
                t0 = loop.time()
                await asyncio.wait_for(service.stop(), 3.0)
                assert loop.time() - t0 < 1.0
        finally:
            tv.cursor.gate.set()
        assert tv.cursor.cancelled == 1 and service._pointers == {}

    @staticmethod
    async def a_page_that_floods_and_never_reads(service, seen):
        """A raw client: it opens a pointer socket, then sends garbage frames and reads no answer. Each is answered with an error, which
        piles up at the server's end (aiohttp lets 256 KiB of them pile up before a send waits) until the socket's handler is blocked in
        a send. The window is kept small at both ends so that it takes a few thousand frames, not millions. Returns its end of the
        connection (the test cuts it, and keeps it until then: a stream writer that is dropped open complains)."""
        loop = asyncio.get_running_loop()
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        sock.setblocking(False)
        await loop.sock_connect(sock, ("127.0.0.1", service.bound_port))
        reader, writer = await asyncio.open_connection(sock=sock)
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write((f"GET /ws/pointer HTTP/1.1\r\nHost: 127.0.0.1:{service.bound_port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")
        server_end = seen.requests[-1].transport
        server_end.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)
        server_end.set_write_buffer_limits(high=2048)
        mask = os.urandom(4)
        payload = b"not json"
        garbage = bytes([0x81, 0x80 | len(payload)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        for _ in range(100):
            writer.write(garbage * 50)
            await asyncio.sleep(0)
        await asyncio.sleep(0.3)
        return writer

    async def test_stopping_the_service_does_not_wait_for_a_page_that_floods_it_and_never_reads(self, monkeypatch):
        """Such a page cannot be closed politely (the close frame cannot be written either), and the listener would wait for it for
        two minutes (a minute for it to end, and another once it is cancelled). After the bounded wait the connection is cut."""
        monkeypatch.setattr(ControlService, "CANCEL_WAIT_S", 0.3)
        service = make_service(mouse=StubMouse())
        seen = watching(service, "_handle_pointer")
        await service.start()
        loop = asyncio.get_running_loop()
        page = await self.a_page_that_floods_and_never_reads(service, seen)
        try:
            t0 = loop.time()
            await asyncio.wait_for(service.stop(), 5.0)
            assert loop.time() - t0 < 2.0
        finally:
            page.transport.abort()

    async def test_the_bound_on_stop_is_one_for_the_tasks_and_the_closes_together(self, monkeypatch):
        """A join that is slow to let go takes the whole bound; the closes that are stuck behind a page that floods are cut at that
        moment, and not given a bound of their own after it."""
        monkeypatch.setattr(ControlService, "CANCEL_WAIT_S", 1.0)
        meeting = StubMeeting()
        meeting.hold_join, meeting.cancel_gate = asyncio.Event(), asyncio.Event()
        service = make_service(meeting=meeting, mouse=StubMouse())
        seen = watching(service, "_handle_pointer")
        await service.start()
        loop = asyncio.get_running_loop()
        try:
            async with aiohttp.ClientSession() as page:
                assert await self.post(page, f"http://127.0.0.1:{service.bound_port}/api/meeting/join", {"url": "https://zoom.us/j/98765432100"}) == 202
            await wait_until(lambda: meeting.state == MeetingState.JOINING)
            page = await self.a_page_that_floods_and_never_reads(service, seen)
            try:
                t0 = loop.time()
                await asyncio.wait_for(service.stop(), 8.0)
                assert 0.9 < loop.time() - t0 < 1.6                       # the join's second, not that and another
            finally:
                page.transport.abort()
        finally:
            meeting.cancel_gate.set()

    @staticmethod
    async def post(http, url, body):
        async with http.post(url, json=body) as response:
            return response.status

    async def test_the_mouse_is_closed_when_the_service_stops_whether_or_not_a_page_used_it(self):
        mouse = StubMouse()
        service = make_service(mouse=mouse)
        await service.start()
        await service.stop()
        assert mouse.closed == 1

    async def test_a_frame_bigger_than_a_request_body_is_not_a_pointer_message_and_ends_the_connection(self, client_factory):
        client = await client_factory(make_service(mouse=StubMouse()))
        async with client.ws_connect("/ws/pointer") as ws:
            await ws.send_str("x" * 5000)
            closed = await asyncio.wait_for(ws.receive(), 2.0)
            assert closed.type == aiohttp.WSMsgType.CLOSE and closed.data == 1009            # too big: the protocol's own answer

    def test_a_pointer_socket_that_has_been_quiet_for_five_seconds_is_pinged(self):
        assert ControlService.POINTER_HEARTBEAT_S == 5.0

    async def test_the_origin_is_compared_as_a_host_is_without_regard_to_case_and_one_that_cannot_be_read_is_refused(self, client_factory):
        mouse = StubMouse()
        client = await client_factory(make_service(mouse=mouse))
        async with client.ws_connect("/ws/pointer", headers={"Host": f"Room.Local:{client.port}", "Origin": f"http://ROOM.local:{client.port}"}) as ws:
            await ws.send_json({"target": "pi", "type": "tap"})
            await wait_until(lambda: mouse.calls == [("click", "left")])
        for origin in ("http://[::1", "http://[", "http://a b:1/"):
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await client.ws_connect("/ws/pointer", headers={"Origin": origin})
            assert refused.value.status == 403, origin

    async def test_an_answer_that_cannot_be_sent_because_the_page_has_gone_does_not_end_the_socket(self, client_factory, monkeypatch, caplog):
        async def gone(self, data, **kwargs):
            raise ConnectionResetError("Cannot write to closing transport")
        mouse = StubMouse()
        client = await client_factory(make_service(mouse=mouse))
        monkeypatch.setattr(aiohttp.web.WebSocketResponse, "send_json", gone)
        async with client.ws_connect("/ws/pointer") as ws:
            await ws.send_str("not json")                                            # answered with an error that cannot be sent
            await ws.send_json({"target": "pi", "type": "tap"})
            await wait_until(lambda: mouse.calls == [("click", "left")])            # the socket went on
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

    async def test_a_page_from_another_site_cannot_drive_the_room_through_the_socket(self, client_factory):
        """A browser lets any page open a WebSocket to any address, and tells the server where the page came from (Origin): a page from
        anywhere but the room's own may not move the room's mouse (the POSTs are kept from cross-site forms the same way)."""
        mouse = StubMouse()
        client = await client_factory(make_service(mouse=mouse))
        own = f"http://127.0.0.1:{client.port}"
        for origin in ("http://evil.example", "http://127.0.0.1:1", f"http://127.0.0.1.evil.example:{client.port}", "null", ""):
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await client.ws_connect("/ws/pointer", headers={"Origin": origin})
            assert refused.value.status == 403, origin
        assert mouse.calls == []
        async with client.ws_connect("/ws/pointer", headers={"Origin": own}) as ws:         # the room's own page, and tools that send none
            await ws.send_json({"target": "pi", "type": "tap"})
            await wait_until(lambda: mouse.calls == [("click", "left")])


class TestTvRequestsInFlight:
    """The agent's stop must not wait for what is with the TV: the listener's cleanup waits for every request that is in flight, a
    minute for them and another for what is left, and nothing after the control service in the agent's shutdown (the meeting's
    leave, the browser, the devices) starts before it is over. These run on the real listener (`service.start()`), whose handlers
    are not ended when a client goes away, as the test server's are."""

    @staticmethod
    async def post(http, url, body):
        async with http.post(url, json=body) as response:
            return response.status

    @pytest.mark.parametrize("path, body, gate", [("/api/tv/control", {"name": "power", "value": "on"}, "gate"),
                                                  ("/api/tv/pair", {}, "pair_gate")])
    async def test_stopping_the_service_does_not_wait_for_a_request_that_is_with_the_tv(self, path, body, gate):
        """A Turn on takes up to 23 s, and a pairing waits for a person (a minute at most, and it was for ever once)."""
        tv = StubTv()
        setattr(tv, gate, asyncio.Event())                 # never opened: the request is with the TV for as long as it is let to be
        service = make_service(tv=tv)
        await service.start()
        loop = asyncio.get_running_loop()
        async with aiohttp.ClientSession() as http:
            asked = asyncio.ensure_future(self.post(http, f"http://127.0.0.1:{service.bound_port}{path}", body))
            try:
                await wait_until(lambda: tv.calls)
                t0 = loop.time()
                await asyncio.wait_for(service.stop(), 3.0)
                assert loop.time() - t0 < 2.0
            finally:
                getattr(tv, gate).set()
            with pytest.raises(aiohttp.ClientError):       # the page is let go of; it is not told that a Turn on was done
                await asked

    async def test_stopping_the_service_lets_go_of_a_pairing_that_waits_for_a_person_cleanly(self, tmp_path):
        """With the real TV service: the pairing is cancelled with its request, the TV is hung up on, no key is kept, and the next
        tap on Pair is not told that a pairing is already waiting."""
        from tests.unit.tv.fake_client import FakeTvClient, PromptingTvClient
        from tests.unit.tv.helpers import build_service as build_tv_service, prompt_is_up
        FakeTvClient.instances.clear()                     # (the fake keeps its instances on the class; the TV tests clear them)
        tv = build_tv_service(tmp_path, PromptingTvClient, key=None)
        service = make_service(tv=tv)
        await service.start()
        try:
            async with aiohttp.ClientSession() as http:
                asked = asyncio.ensure_future(self.post(http, f"http://127.0.0.1:{service.bound_port}/api/tv/pair", {}))
                await wait_until(prompt_is_up)
                await asyncio.wait_for(service.stop(), 3.0)
                with pytest.raises(aiohttp.ClientError):
                    await asked
            client = FakeTvClient.instances[-1]
            assert ("disconnect",) in client.calls and client.connected is False
            assert tv._pairing is False and tv._client is None and not (tmp_path / "tv-client-key").exists()
            assert tv.status()["paired"] is False and tv.status()["reachable"] is False
            again = asyncio.ensure_future(tv.pair())       # the person taps Pair again, on a page that is back
            await wait_until(lambda: len(FakeTvClient.instances) == 2 and prompt_is_up())
            FakeTvClient.instances[-1].accept()
            await asyncio.wait_for(again, 2.0)
            assert tv.status()["paired"] is True
        finally:
            await tv.stop()
            FakeTvClient.instances.clear()

    async def test_stopping_the_service_does_not_wait_for_the_controls_queued_behind_a_turn_on(self):
        tv = StubTv()
        tv.gate = asyncio.Event()
        service = make_service(tv=tv)
        seen = watching(service, "_handle_tv_control")
        await service.start()
        url = f"http://127.0.0.1:{service.bound_port}/api/tv/control"
        async with aiohttp.ClientSession() as http:
            taps = [asyncio.ensure_future(self.post(http, url, body))
                    for body in ({"name": "power", "value": "on"}, {"name": "volume_up"}, {"name": "volume_down"})]
            try:
                await wait_until(lambda: len(seen.arrived) == 3)      # one is with the TV, and two wait for their turn
                await asyncio.wait_for(service.stop(), 3.0)
            finally:
                tv.gate.set()
            for tap in taps:
                with pytest.raises(aiohttp.ClientError):
                    await tap
        assert len(seen.cancelled) == 3 and seen.responses == []      # none was answered, as if it had been done
        assert tv.calls == [("control", "power", "on")]               # and the two that were waiting never had their turn

    async def test_a_control_whose_page_has_gone_is_not_carried_out_when_its_turn_comes(self, caplog):
        """The real listener does not end a handler when its client goes away, so a tap that waited behind a Turn on would be made
        when the Turn on ends, for a page that is no longer there: it is dropped when it has the TV's turn and finds the page
        gone (the request's transport is None, or is closing)."""
        caplog.set_level(logging.DEBUG, logger="croom.control.service")
        tv = StubTv()
        tv.gate = asyncio.Event()
        service = make_service(tv=tv)
        seen = watching(service, "_handle_tv_control")
        await service.start()
        url = f"http://127.0.0.1:{service.bound_port}/api/tv/control"
        try:
            async with aiohttp.ClientSession() as holder:
                turn_on = asyncio.ensure_future(self.post(holder, url, {"name": "power", "value": "on"}))
                await wait_until(lambda: tv.order == [("start", "power")])
                async with aiohttp.ClientSession() as other_page:
                    tap = asyncio.ensure_future(self.post(other_page, url, {"name": "volume_up"}))
                    await wait_until(lambda: len(seen.arrived) == 2)     # it waits behind the Turn on ...
                    tap.cancel()                                          # ... and its page goes away
                    with contextlib.suppress(asyncio.CancelledError):
                        await tap
                gone = seen.requests[1]
                await wait_until(lambda: gone.transport is None or gone.transport.is_closing())      # the server has seen it go
                assert seen.finished == []                    # (its handler is waiting still: the listener does not end it)
                tv.gate.set()
                assert await asyncio.wait_for(turn_on, 2.0) == 200
                await wait_until(lambda: len(seen.finished) == 2)
        finally:
            tv.gate.set()
            await service.stop()
        assert tv.calls == [("control", "power", "on")]               # the tap of the page that had gone was never made
        assert [response.status for response in seen.responses] == [200, 499]
        assert "dropped" in caplog.text

    @pytest.mark.parametrize("how, gone", [
        ("none", True),                  # as the listener leaves the request when the client has hung up
        ("closing", True),               # or as a request that kept its transport sees it, in a version that caches it
        ("open", False),
    ])
    def test_a_page_has_gone_when_its_transport_is_none_or_closing(self, how, gone):
        request = make_mocked_request("POST", "/api/tv/control",
                                      transport=mock.Mock(is_closing=mock.Mock(return_value=how == "closing")))
        if how == "none":
            request.protocol.transport = None
        assert ControlService._page_has_gone(request) is gone

    async def test_a_control_whose_page_is_there_is_carried_out_when_its_turn_comes(self):
        tv = StubTv()
        tv.gate = asyncio.Event()
        service = make_service(tv=tv)
        await service.start()
        url = f"http://127.0.0.1:{service.bound_port}/api/tv/control"
        try:
            async with aiohttp.ClientSession() as http:
                first = asyncio.ensure_future(self.post(http, url, {"name": "power", "value": "on"}))
                await wait_until(lambda: tv.order == [("start", "power")])
                second = asyncio.ensure_future(self.post(http, url, {"name": "volume_up"}))
                await asyncio.sleep(0.05)
                tv.gate.set()
                assert await asyncio.wait_for(first, 2.0) == 200 and await asyncio.wait_for(second, 2.0) == 200
        finally:
            tv.gate.set()
            await service.stop()
        assert tv.calls == [("control", "power", "on"), ("control", "volume_up", None)]


class TestStopEndsWhatIsOpen:
    """stop() ends the join, the search and the requests that are with the TV from the listener's own shutdown (the app's on_shutdown),
    which aiohttp runs after the listening sockets are closed and before it waits for the requests in flight: no request can arrive while
    a cancelled task is let go of. Before, the cancel came first and the listener stayed open for as long as it took, up to CANCEL_WAIT_S:
    a Turn on that arrived in that time was not cancelled, and the listener's cleanup waited for it (a minute, and another)."""

    URL = "https://zoom.us/j/98765432100"

    @staticmethod
    async def post(http, url, body):
        async with http.post(url, json=body) as response:
            return response.status

    def test_a_task_that_is_cancelled_is_waited_for_five_seconds_and_no_longer(self):
        assert ControlService.CANCEL_WAIT_S == 5.0

    async def test_a_tv_control_that_comes_while_a_join_is_let_go_of_is_not_served_and_stop_takes_no_longer_than_the_join(self, monkeypatch):
        """A join that takes 8 s to let go and a TV control that takes 20 s, scaled to 0.3 s for the wait (CANCEL_WAIT_S is 5 s) and for
        ever for the control. The control that is with the TV when stop() begins is cancelled; the one that comes after is refused
        (the listener is closed), and stop() is over when the join has been waited for."""
        monkeypatch.setattr(ControlService, "CANCEL_WAIT_S", 0.3)
        meeting = StubMeeting()
        meeting.hold_join, meeting.cancel_gate = asyncio.Event(), asyncio.Event()      # a join that is slow to let go
        tv = StubTv()
        tv.gate = asyncio.Event()                          # a control that does not end by itself
        service = make_service(meeting=meeting, tv=tv)
        await service.start()
        base = f"http://127.0.0.1:{service.bound_port}"
        loop = asyncio.get_running_loop()
        try:
            async with aiohttp.ClientSession() as joiner, aiohttp.ClientSession() as holder, aiohttp.ClientSession() as late:
                assert await self.post(joiner, base + "/api/meeting/join", {"url": self.URL}) == 202
                await wait_until(lambda: meeting.state == MeetingState.JOINING)
                turn_on = asyncio.ensure_future(self.post(holder, base + "/api/tv/control", {"name": "power", "value": "on"}))
                await wait_until(lambda: tv.order == [("start", "power")])
                t0 = loop.time()
                stopping = asyncio.ensure_future(service.stop())
                await asyncio.sleep(0.1)                   # stop() is waiting for the join to let go
                with pytest.raises(aiohttp.ClientError):   # and the listener does not take another request
                    await asyncio.wait_for(self.post(late, base + "/api/tv/control", {"name": "volume_up"}), 2.0)
                await asyncio.wait_for(stopping, 3.0)
                assert loop.time() - t0 < 2.0              # (the control the TV was with did not have to be waited for either)
                with pytest.raises(aiohttp.ClientError):   # the page that waited for the Turn on is let go of, not told that it was done
                    await asyncio.wait_for(turn_on, 2.0)
        finally:
            meeting.cancel_gate.set()
            tv.gate.set()
        assert tv.calls == [("control", "power", "on")] and tv.order == [("start", "power"), ("end", "power")]     # cancelled, and nothing after it
        assert service._tv_requests == set()
        await wait_until(lambda: meeting.order == ["join given up"])        # the join ends in its own time, as it did


class TestTvRequestsOnTheBooks:
    """The requests that are with the TV service are on the books (`_tv_requests`) for as long as they last, which is how stop() finds
    them; none stays on them once it is over, whichever way."""

    ROUTES = [("/api/tv/control", {"name": "volume_up"}, "fail_control"), ("/api/tv/pair", {}, "fail_pair"),
              ("/api/tv/forget", {}, "fail_forget")]

    @pytest.mark.parametrize("path, body, failing", ROUTES)
    async def test_an_answered_request_is_off_the_books(self, client_factory, path, body, failing):
        service = make_service(tv=StubTv())
        client = await client_factory(service)
        assert (await client.post(path, json=body)).status == 200
        assert service._tv_requests == set()

    @pytest.mark.parametrize("path, body, failing", ROUTES)
    async def test_a_refused_request_is_off_the_books(self, client_factory, path, body, failing):
        tv = StubTv()
        setattr(tv, failing, TvError("The TV is not answering"))
        service = make_service(tv=tv)
        client = await client_factory(service)
        assert (await client.post(path, json=body)).status == 409
        assert service._tv_requests == set()

    @pytest.mark.parametrize("has_tv, request_kwargs, status", [
        (True, {"json": {"not": "a control"}}, 400),
        (True, {"data": "[1]", "headers": {"Content-Type": "application/json"}}, 400),
        (True, {"data": "x", "headers": {"Content-Type": "text/plain"}}, 415),
        (False, {"json": {"name": "volume_up"}}, 503),
    ])
    async def test_a_request_that_is_turned_away_is_off_the_books(self, client_factory, has_tv, request_kwargs, status):
        service = make_service(tv=StubTv() if has_tv else None)
        client = await client_factory(service)
        assert (await client.post("/api/tv/control", **request_kwargs)).status == status
        assert service._tv_requests == set()

    async def test_a_request_is_on_the_books_while_it_lasts_and_a_cancelled_one_is_off_them(self, client_factory):
        tv = StubTv()
        tv.gate = asyncio.Event()
        service = make_service(tv=tv)
        seen = watching(service, "_handle_tv_control")
        client = await client_factory(service)
        asked = asyncio.ensure_future(client.post("/api/tv/control", json={"name": "power", "value": "on"}))
        try:
            await wait_until(lambda: tv.order == [("start", "power")])
            assert len(service._tv_requests) == 1                       # a Turn on that is with the TV: stop() will find it
            asked.cancel()                                              # and the page goes away
            with pytest.raises(asyncio.CancelledError):
                await asked
            await wait_until(lambda: len(seen.cancelled) == 1)          # the server has ended the handler that nobody waits for
            assert service._tv_requests == set()
        finally:
            tv.gate.set()


class TestLeaveWhileJoining:
    """A join waits for the room before it asks its provider (the TV is woken, up to fifteen seconds). A person who taps
    Leave then has changed their mind: the join must not go on, and must not finish after the Leave."""

    URL = "https://zoom.us/j/98765432100"

    async def test_a_leave_while_the_join_waits_for_the_room_gives_the_join_up_before_it_leaves(self, client_factory):
        meeting = StubMeeting()
        meeting.hold_join = asyncio.Event()
        calendar = StubCalendar(events=[event("e1", "Design review", starts_in_minutes=5)])
        client = await client_factory(make_service(meeting=meeting, calendar=calendar))
        assert (await client.post("/api/meeting/join", json={"event_id": "e1"})).status == 202
        await wait_until(lambda: meeting.state == MeetingState.JOINING)
        data = await (await client.get("/api/status")).json()
        assert data["meeting"]["state"] == "joining" and data["meeting"]["title"] == "Design review"
        response = await client.post("/api/meeting/leave", json={})
        assert response.status == 202 and await response.json() == {"state": "leaving"}
        await wait_until(lambda: meeting.leaves == 1)
        assert meeting.order == ["join given up", "leave"]       # the join was over when the leave began
        meeting.hold_join.set()                                  # the room answers late: there is no join left to go on
        await asyncio.sleep(0.05)
        data = await (await client.get("/api/status")).json()
        assert data["meeting"]["state"] == "idle" and data["meeting"]["title"] == ""
        assert meeting.current_meeting is None and len(meeting.joins) == 1

    async def test_a_join_that_takes_time_to_give_up_is_over_before_the_leave_begins(self, client_factory):
        meeting = StubMeeting()
        meeting.hold_join, meeting.cancel_gate = asyncio.Event(), asyncio.Event()
        client = await client_factory(make_service(meeting=meeting))
        assert (await client.post("/api/meeting/join", json={"url": self.URL})).status == 202
        await wait_until(lambda: meeting.state == MeetingState.JOINING)
        asyncio.get_running_loop().call_later(0.05, meeting.cancel_gate.set)       # the provider lets go of the join in 50 ms
        response = await client.post("/api/meeting/leave", json={})
        assert response.status == 202
        await wait_until(lambda: meeting.leaves == 1)
        assert meeting.order == ["join given up", "leave"]      # the leave waited for the join to be over, and did not begin beside it

    async def test_a_join_that_does_not_let_go_does_not_hold_the_leave_up(self, client_factory, monkeypatch, caplog):
        monkeypatch.setattr(ControlService, "CANCEL_WAIT_S", 0.05)
        meeting = StubMeeting()
        meeting.hold_join, meeting.cancel_gate = asyncio.Event(), asyncio.Event()      # (a provider that has not let go: yet)
        client = await client_factory(make_service(meeting=meeting))
        assert (await client.post("/api/meeting/join", json={"url": self.URL})).status == 202
        await wait_until(lambda: meeting.state == MeetingState.JOINING)
        try:
            response = await asyncio.wait_for(client.post("/api/meeting/leave", json={}), 3.0)
            assert response.status == 202
            await wait_until(lambda: meeting.leaves == 1)
            assert meeting.order == ["leave"]                   # the join was still giving itself up: it was not waited for any longer
            assert "did not end within" in caplog.text
        finally:
            meeting.cancel_gate.set()
        await wait_until(lambda: meeting.order == ["leave", "join given up"])      # and it ends in its own time

    async def test_a_leave_after_the_join_has_ended_gives_nothing_up(self, client_factory):
        meeting = StubMeeting()
        client = await client_factory(make_service(meeting=meeting))
        await client.post("/api/meeting/join", json={"url": self.URL})
        await wait_until(lambda: meeting.state == MeetingState.CONNECTED)
        assert (await client.post("/api/meeting/leave", json={})).status == 202
        await wait_until(lambda: meeting.leaves == 1)
        assert meeting.order == ["leave"]

    async def test_a_second_leave_does_not_give_up_the_first(self, client_factory):
        """The task that is running is a leave this time, and a leave is not given up: it is the way out."""
        meeting = StubMeeting()
        gate = asyncio.Event()
        service = make_service(meeting=meeting)
        client = await client_factory(service)
        await client.post("/api/meeting/join", json={"url": self.URL})
        await wait_until(lambda: meeting.state == MeetingState.CONNECTED)
        slow_leave = meeting.leave_meeting

        async def held_leave():
            await gate.wait()
            await slow_leave()
        meeting.leave_meeting = held_leave
        try:
            assert (await client.post("/api/meeting/leave", json={})).status == 202
            meeting.set_state(MeetingState.LEAVING)
            assert (await client.post("/api/meeting/leave", json={})).status == 202
            await asyncio.sleep(0.02)
            assert meeting.order == []                             # nothing was cancelled, and neither leave has finished
        finally:
            gate.set()
        await wait_until(lambda: meeting.leaves == 2)

    async def test_stopping_the_service_does_not_wait_for_a_join_that_does_not_let_go(self, client_factory, monkeypatch):
        monkeypatch.setattr(ControlService, "CANCEL_WAIT_S", 0.05)
        meeting = StubMeeting()
        meeting.hold_join, meeting.cancel_gate = asyncio.Event(), asyncio.Event()
        service = make_service(meeting=meeting)
        await service.start()
        client = await client_factory(service)
        try:
            assert (await client.post("/api/meeting/join", json={"url": self.URL})).status == 202
            await wait_until(lambda: meeting.state == MeetingState.JOINING)
            await asyncio.wait_for(service.stop(), 3.0)
        finally:
            meeting.cancel_gate.set()
        await wait_until(lambda: meeting.order == ["join given up"])

    async def test_stopping_the_service_gives_up_a_join_that_still_waits(self, client_factory):
        meeting = StubMeeting()
        meeting.hold_join = asyncio.Event()
        service = make_service(meeting=meeting)
        await service.start()
        client = await client_factory(service)
        try:
            assert (await client.post("/api/meeting/join", json={"url": self.URL})).status == 202
            await wait_until(lambda: meeting.state == MeetingState.JOINING)
        finally:
            await asyncio.wait_for(service.stop(), 2.0)
        assert meeting.order == ["join given up"]
