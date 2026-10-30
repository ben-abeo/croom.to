"""The Network routes (room settings spec 2026-10-29, sections 3 and 7): behind the PIN, GET /api/settings/network (the TV Pi's
connection from the root helper, the devices' from their check-ins) and POST /api/settings/network/{device}/scan|connect|forget (the TV
Pi's through the helper, a device's as a job it takes at its next check-in); never a change in a meeting ("Leave the meeting first");
and GET /api/devices/seen-since, answered only to the TV Pi itself (the helper checking a new network has no PIN). The helper is the real
jobs on a stand-in NetworkManager; the registry the real one."""

import json
from unittest import mock

import pytest
from aiohttp.test_utils import make_mocked_request

from croom.admin.client import AdminError, AdminUnavailable
from croom.admin.jobs import JobError
from croom.meeting.providers.base import MeetingState
from croom.settings.checkin import Registry
from tests.unit.admin.test_wifi import Machine
from tests.unit.control.test_service import StubMeeting, client_factory, make_service  # noqa: F401  (client_factory is a fixture)
from tests.unit.control.test_settings_software_api import pins, unlocked  # noqa: F401  (fixtures)
from tests.unit.settings.test_checkin import Clock, Ids

TOUCH = "t" * 32
TOKEN = "k" * 43


class WifiHelper:
    """The root helper's client, answered by the helper's own jobs on a machine whose NetworkManager is a stand-in (`nm`); a Wi-Fi
    change's background work waits in `queued` until work() (or runs at once with run_now)."""

    def __init__(self, root, run_now=False):
        folder = root / "helper"
        folder.mkdir(parents=True, exist_ok=True)
        self.queued = []
        self.machine = Machine(folder, background=(lambda work: work()) if run_now else self.queued.append)
        self.nm = self.machine.nm
        self.room = self.machine.room
        self.calls = []
        self.gone = False

    def available(self):
        return not self.gone

    async def call(self, job, **args):
        if self.gone:
            raise AdminUnavailable("Not available on this device")
        self.calls.append((job, args))
        try:
            return {"ok": True, **self.machine.jobs.table[job](args)}
        except JobError as e:
            raise AdminError(str(e)) from e

    def work(self):
        while self.queued:
            self.queued.pop(0)()


@pytest.fixture
def registry():
    return Registry(None, clock=Clock(), new_id=Ids())


def joined(registry, network=None):
    registry.checkin({"id": TOUCH, "kind": "touch", "version": "a" * 40, "name": "touch-pi",
                      "network": network or {"address": "10.0.0.9", "connection": "wifi", "ssid": "Office", "signal": 60}, "job": None},
                     TOKEN, "10.0.0.9")
    registry.allow(TOUCH)


def service(pins, registry, admin=None, meeting=None):  # noqa: F811
    return make_service(pin_store=pins, registry=registry, admin=admin, meeting=meeting or StubMeeting())


ROUTES = (("get", "/api/settings/network"), ("post", "/api/settings/network/tv/scan"), ("post", "/api/settings/network/tv/connect"),
          ("post", "/api/settings/network/tv/forget"), ("post", f"/api/settings/network/{TOUCH}/connect"))


async def test_every_network_route_asks_for_the_pin_first(client_factory, pins, registry, tmp_path):  # noqa: F811
    helper = WifiHelper(tmp_path)
    client = await client_factory(service(pins, registry, helper))
    for method, path in ROUTES:
        response = await getattr(client, method)(path, json={"ssid": "Guest"}) if method == "post" else await client.get(path)
        assert response.status == 401 and (await response.json())["error"] == "Enter the PIN", path
    assert helper.calls == []


async def test_the_page_reads_the_tv_pis_connection_and_each_devices(client_factory, pins, registry, unlocked, tmp_path):  # noqa: F811
    joined(registry)
    client = await client_factory(service(pins, registry, WifiHelper(tmp_path)))
    answer = await (await client.get("/api/settings/network", headers=unlocked)).json()
    assert answer["tv"] == {"available": True, "wifi": True, "device": "wlan0", "connection": "wifi", "ssid": "Office", "signal": 70,
                            "address": "192.168.1.42", "saved": ["Office"], "change": None}
    [device] = answer["devices"]
    assert device["id"] == TOUCH and device["network"]["ssid"] == "Office" and device["scan"] is None
    assert answer["in_meeting"] is False


async def test_without_the_helper_the_tv_pi_is_not_available_and_the_devices_still_are(  # noqa: F811
        client_factory, pins, registry, unlocked):  # noqa: F811
    joined(registry)
    client = await client_factory(service(pins, registry))
    answer = await (await client.get("/api/settings/network", headers=unlocked)).json()
    assert answer["tv"] == {"available": False, "error": "Not available on this device"}
    assert len(answer["devices"]) == 1
    response = await client.post("/api/settings/network/tv/scan", json={}, headers=unlocked)
    assert response.status == 503 and (await response.json())["error"] == "Not available on this device"
    response = await client.post(f"/api/settings/network/{TOUCH}/scan", json={}, headers=unlocked)
    assert response.status == 200


async def test_the_tv_pi_scans_joins_and_forgets_through_the_helper(client_factory, pins, registry, unlocked, tmp_path):  # noqa: F811
    helper = WifiHelper(tmp_path)
    helper.room.touch_at = helper.machine.clock.now + 20
    client = await client_factory(service(pins, registry, helper))
    response = await client.post("/api/settings/network/tv/scan", json={}, headers=unlocked)
    networks = (await response.json())["networks"]
    assert response.status == 200 and networks[0]["ssid"] == "New Net"
    response = await client.post("/api/settings/network/tv/connect", json={"ssid": "New Net", "password": "correct horse"}, headers=unlocked)
    assert response.status == 200 and await response.json() == {"changing": True}
    assert helper.calls[-1] == ("wifi_connect", {"ssid": "New Net", "password": "correct horse", "hidden": False})
    helper.work()
    change = (await (await client.get("/api/settings/network", headers=unlocked)).json())["tv"]["change"]
    assert change["state"] == "done" and change["words"].startswith("Joined New Net")
    response = await client.post("/api/settings/network/tv/forget", json={"ssid": "Office"}, headers=unlocked)
    assert response.status == 200 and await response.json() == {"words": "Forgot Office"}
    # The helper's refusal, in its words
    response = await client.post("/api/settings/network/tv/connect", json={"ssid": "Corp", "password": "12345678"}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == "Corp asks for a user name: networks like it are not supported"
    response = await client.post("/api/settings/network/tv/connect", data="ssid=x", headers=unlocked)
    assert response.status == 415


async def test_a_devices_wifi_is_a_job_it_takes_at_its_next_check_in(client_factory, pins, registry, unlocked):  # noqa: F811
    joined(registry)
    client = await client_factory(service(pins, registry))
    response = await client.post(f"/api/settings/network/{TOUCH}/connect", json={"ssid": "New Net", "password": "correct horse"},
                                 headers=unlocked)
    answer = await response.json()
    assert response.status == 200 and answer["queued"] is True
    assert answer["job"]["type"] == "wifi_connect" and answer["job"]["args"] == {"ssid": "New Net", "hidden": False}   # never the password
    response = await client.post(f"/api/settings/network/{TOUCH}/scan", json={}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == "The touch Pi is busy: wait for it to finish"
    handed = registry.checkin({"id": TOUCH, "kind": "touch", "version": "a" * 40, "network": {}, "job": None}, TOKEN, "10.0.0.9")["job"]
    assert handed["args"] == {"ssid": "New Net", "password": "correct horse", "hidden": False}
    response = await client.post(f"/api/settings/network/{TOUCH}/connect", json={"ssid": "", "password": "x"}, headers=unlocked)
    assert response.status in (400, 409)
    for path, status in ((f"/api/settings/network/{'n' * 32}/scan", 404), ("/api/settings/network/tv/reboot", 404)):
        response = await client.post(path, json={}, headers=unlocked)
        assert response.status == status, path


async def test_a_wifi_change_is_refused_in_a_meeting_on_every_device(client_factory, pins, registry, unlocked, tmp_path):  # noqa: F811
    joined(registry)
    helper = WifiHelper(tmp_path)
    meeting = StubMeeting()
    meeting.state = MeetingState.CONNECTED
    client = await client_factory(service(pins, registry, helper, meeting))
    for device in ("tv", TOUCH):
        for action, body in (("connect", {"ssid": "Guest"}), ("forget", {"ssid": "Office"})):
            response = await client.post(f"/api/settings/network/{device}/{action}", json=body, headers=unlocked)
            assert response.status == 409 and (await response.json())["error"] == "Leave the meeting first", (device, action)
    for job in ("wifi_connect", "wifi_forget"):
        response = await client.post(f"/api/settings/devices/{TOUCH}/jobs", json={"type": job, "args": {"ssid": "Guest"}}, headers=unlocked)
        assert response.status == 409 and (await response.json())["error"] == "Leave the meeting first"
    assert [c for c in helper.calls if c[0] != "wifi_status"] == [] and registry.get(TOUCH)["job"] is None
    response = await client.post("/api/settings/network/tv/scan", json={}, headers=unlocked)     # looking changes nothing
    assert response.status == 200
    answer = await (await client.get("/api/settings/network", headers=unlocked)).json()
    assert answer["in_meeting"] is True


async def test_seen_since_answers_the_tv_pi_itself(client_factory, pins, registry):  # noqa: F811
    client = await client_factory(service(pins, registry))
    response = await client.get("/api/devices/seen-since?kind=touch&since=0")
    assert response.status == 200 and await response.json() == {"seen": False, "known": False}
    joined(registry)
    now = registry._clock()
    assert await (await client.get(f"/api/devices/seen-since?kind=touch&since={now}")).json() == {"seen": True, "known": True}
    assert await (await client.get(f"/api/devices/seen-since?kind=touch&since={now + 1}")).json() == {"seen": False, "known": True}
    for query in ("kind=toaster&since=0", "kind=touch&since=nan", "kind=touch&since=soon", "kind=touch"):
        assert (await client.get(f"/api/devices/seen-since?{query}")).status == 400, query


@pytest.mark.parametrize("peer, local, allowed", [
    (("127.0.0.1", 50000), ("127.0.0.1", 8080), True),
    (("::1", 50000, 0, 0), ("::1", 8080, 0, 0), True),
    (("::ffff:127.0.0.1", 50000, 0, 0), ("::ffff:127.0.0.1", 8080, 0, 0), True),
    (("192.168.1.20", 50000), ("192.168.1.20", 8080), True),       # the TV Pi to its own LAN address
    (("192.168.1.30", 50000), ("192.168.1.20", 8080), False),      # anyone else on the LAN
    (None, None, False),
])
async def test_seen_since_refuses_anyone_but_the_tv_pi(pins, registry, peer, local, allowed):  # noqa: F811
    svc = service(pins, registry)
    transport = mock.Mock()
    transport.get_extra_info = lambda name, default=None: {"peername": peer, "sockname": local}.get(name, default)
    request = make_mocked_request("GET", "/api/devices/seen-since?kind=touch&since=0", transport=transport,
                                  headers={"X-Forwarded-For": "127.0.0.1"})
    response = await svc._handle_seen_since(request)
    assert (response.status == 200) is allowed
    if not allowed:
        assert response.status == 403 and json.loads(response.body)["error"] == "Only the TV Pi itself may ask this"
