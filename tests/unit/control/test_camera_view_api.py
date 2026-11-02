"""GET /api/camera/view (camera view spec 2026-11-01, section 4): the camera's picture to the touch Pi over a WebSocket. Only the risky
paths: who may open it (the socket's peer, never a header), a socket whose client has gone (it must not keep the tap running), and a
client that reads slowly (it gets the newest picture, never a queue). The CameraView is the real one with a watch counter, its frames
published by hand (no browser); the registry is the real one in memory on a fake clock."""

import asyncio

import aiohttp
import pytest

from croom.control.service import ControlService
from croom.meeting.camera_view import CameraView
from croom.settings.checkin import Registry
from tests.unit.control.test_devices_api import HEADER, TOUCH, checkin_body
from tests.unit.control.test_service import StubMeeting, client_factory, make_service, wait_until  # noqa: F401  (client_factory is a fixture)
from tests.unit.settings.test_checkin import Clock, Ids


class WatchedView(CameraView):
    """The real CameraView (its newest-frame-only frames()), counting watch() and unwatch(); no page, so nothing runs a tap."""

    def __init__(self):
        super().__init__()
        self.watches = 0
        self.unwatches = 0

    def watch(self):
        self.watches += 1
        super().watch()

    def unwatch(self):
        self.unwatches += 1
        super().unwatch()

    def show(self, jpeg: bytes):
        self._tap_on = True      # (the state is "on" while the tap gives frames)
        self._publish(jpeg)
        self._announce()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def registry(clock):
    return Registry(None, clock=clock, new_id=Ids())


@pytest.fixture
def view():
    return WatchedView()


async def room(client_factory, registry, view, allow=True, kind="touch"):  # noqa: F811
    meeting = StubMeeting()
    meeting.camera_view = view
    service = make_service(meeting=meeting, registry=registry)
    client = await client_factory(service)
    response = await client.post("/api/devices/checkin", json=dict(checkin_body(), kind=kind), headers=HEADER)
    assert response.status == 200
    if allow:
        registry.allow(TOUCH)
    return service, client


async def test_only_an_allowed_recently_seen_touch_pi_at_the_sockets_own_address_gets_the_picture(client_factory, registry, view, clock):  # noqa: F811
    forged = {"X-Forwarded-For": "10.0.0.9", "Forwarded": "for=10.0.0.9", "X-Real-IP": "10.0.0.9"}
    service, client = await room(client_factory, registry, view, allow=False)       # waiting to join, not allowed
    refused = []
    for case in ("pending", "other address", "stale", "door sign"):
        if case == "other address":
            registry.allow(TOUCH)
            registry._devices[TOUCH]["address"] = "192.168.1.50"   # (it last checked in from elsewhere: the socket's peer is 127.0.0.1)
        elif case == "stale":
            registry._devices[TOUCH]["address"] = "127.0.0.1"
            clock.now += 31
        elif case == "door sign":
            registry._devices[TOUCH].update(address="127.0.0.1", kind="sign", last_seen=clock.now)
        with pytest.raises(aiohttp.WSServerHandshakeError) as refusal:
            await client.ws_connect("/api/camera/view", headers=forged)
        refused.append(refusal.value.status)
        status = await (await client.get("/api/status", headers=forged)).json()
        assert status["camera"]["view"] == {"allowed": False}, case
    assert refused == [403, 403, 403, 403] and view.watches == 0

    registry._devices[TOUCH].update(address="::ffff:127.0.0.1", kind="touch", last_seen=clock.now)    # the same address in its IPv6 form
    status = await (await client.get("/api/status")).json()
    assert status["camera"]["view"] == {"allowed": True}
    ws = await client.ws_connect("/api/camera/view")
    assert (await ws.receive_json()) == {"state": "starting"} and view.watches == 1
    await ws.close()
    await wait_until(lambda: view.unwatches == 1)


async def test_a_socket_whose_client_stops_answering_pings_is_closed_and_the_tap_let_go_of(client_factory, registry, view, monkeypatch):  # noqa: F811
    assert ControlService.CAMERA_HEARTBEAT_S == 10      # (about 15 s in all: a ping after 10 s of quiet, and half as long for the answer)
    monkeypatch.setattr(ControlService, "CAMERA_HEARTBEAT_S", 0.2)
    service, client = await room(client_factory, registry, view)
    # A client that has gone: it reads nothing, so it answers no ping (a Pi that lost its power or its network).
    ws = await client.ws_connect("/api/camera/view", autoping=False, heartbeat=None)
    await wait_until(lambda: view.watches == 1)
    assert view.unwatches == 0
    await wait_until(lambda: view.unwatches == 1, timeout=3)
    assert view.watches == 1 and not service._camera_sockets


async def test_a_client_that_reads_slowly_gets_the_newest_picture_after_its_pause(client_factory, registry, view):  # noqa: F811
    service, client = await room(client_factory, registry, view)
    ws = await client.ws_connect("/api/camera/view", max_msg_size=0, compress=0)
    assert (await ws.receive_json()) == {"state": "starting"}
    size = 8 * 1024 * 1024
    # The client stops reading; the tap goes on. Each picture is bigger than the sockets' buffers, so the send of one is still in progress
    # when the next ones come.
    for n in range(1, 9):
        view.show(bytes([n]) * size)
        await asyncio.sleep(0.05)
    seen = []
    async with asyncio.timeout(10):
        while not seen or seen[-1] != 8:
            message = await ws.receive()
            if message.type == aiohttp.WSMsgType.BINARY:
                seen.append(message.data[0])
    assert seen == sorted(set(seen)) and seen[-1] == 8
    assert len(seen) < 8, f"every picture was sent: {seen}"
    await ws.close()
    await wait_until(lambda: view.unwatches == 1)
