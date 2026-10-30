"""The routes of the devices that check in (room settings spec 2026-10-29, sections 3 and 7): POST /api/devices/checkin (not behind the PIN:
a device unknown waits to join, one allowed carries its token), and behind the PIN GET /api/settings/devices, POST
/api/settings/devices/{id}/allow|refuse|forget|jobs; the Software routes acting on them (update, reboot, Update all) and the Software
page's reading of them. The registry is the real one in memory on a fake clock; the TV Pi's helper is the real jobs on fakes; GitHub is
the real client on a fake transport."""

import pytest

from croom.meeting.providers.base import MeetingState
from croom.settings.checkin import Registry
from croom.settings.github import GitHub
from tests.unit.control.test_service import (  # noqa: F401  (client_factory is a fixture)
    StubMeeting,
    client_factory,
    make_service,
)
from tests.unit.control.test_settings_software_api import (  # noqa: F401  (fixtures)
    A,
    B,
    Helper,
    github_with_changes,
    pins,
    unlocked,
)
from tests.unit.settings.test_checkin import Clock, Ids
from tests.unit.settings.test_github import NEWEST, Transport

TOUCH = "t" * 32
TOKEN = "k" * 43
HEADER = {"X-Croom-Device-Token": TOKEN}
PROTECTED = (("get", "/api/settings/devices"), ("post", f"/api/settings/devices/{TOUCH}/allow"),
             ("post", f"/api/settings/devices/{TOUCH}/refuse"), ("post", f"/api/settings/devices/{TOUCH}/forget"),
             ("post", f"/api/settings/devices/{TOUCH}/jobs"), ("post", "/api/settings/software/update-all"))


@pytest.fixture
def registry():
    return Registry(None, clock=Clock(), new_id=Ids())


@pytest.fixture
def helper(tmp_path):
    return Helper(tmp_path)


def checkin_body(version=A, job=None):
    return {"id": TOUCH, "kind": "touch", "version": version, "name": "touch-pi", "network": {}, "job": job}


def service(pins, registry, admin=None, github=None, meeting=None):
    return make_service(pin_store=pins, registry=registry, admin=admin, github=github, meeting=meeting or StubMeeting())


async def joined(client, registry, version=A):
    response = await client.post("/api/devices/checkin", json=checkin_body(version), headers=HEADER)
    assert response.status == 200 and (await response.json())["state"] == "pending"
    registry.allow(TOUCH)


async def test_checkin_needs_no_pin_and_an_unknown_device_waits(client_factory, pins, registry, unlocked):  # noqa: F811
    client = await client_factory(service(pins, registry))
    response = await client.post("/api/devices/checkin", json=checkin_body(), headers=HEADER)
    assert response.status == 200 and await response.json() == {"state": "pending", "interval": 5}
    answer = await (await client.get("/api/settings/devices", headers=unlocked)).json()
    assert answer["devices"] == [] and [w["id"] for w in answer["waiting"]] == [TOUCH]
    assert answer["waiting"][0]["address"] == "127.0.0.1"


async def test_checkin_refusals(client_factory, pins, registry):  # noqa: F811
    client = await client_factory(service(pins, registry))
    assert (await client.post("/api/devices/checkin", data="id=x", headers=HEADER)).status == 415
    response = await client.post("/api/devices/checkin", json=[1], headers=HEADER)
    assert response.status == 400
    response = await client.post("/api/devices/checkin", json=checkin_body())
    assert response.status == 401 and (await response.json())["error"] == "Send the device's token"
    response = await client.post("/api/devices/checkin", data=b"{" + b" " * 20000 + b"}", headers=dict(HEADER, **{"Content-Type": "application/json"}))
    assert response.status == 400
    await joined(client, registry)
    response = await client.post("/api/devices/checkin", json=checkin_body(), headers={"X-Croom-Device-Token": "x" * 43})
    assert response.status == 403 and (await response.json())["error"] == "This device's token is not the one it joined with"


async def test_the_management_routes_ask_for_the_pin(client_factory, pins, registry):  # noqa: F811
    client = await client_factory(service(pins, registry))
    await client.post("/api/devices/checkin", json=checkin_body(), headers=HEADER)
    for method, path in PROTECTED:
        response = await (client.get(path) if method == "get" else client.post(path, json={"type": "reboot"}))
        assert response.status == 401 and (await response.json())["error"] == "Enter the PIN", path
    assert registry.waiting()[0]["state"] == "pending"


async def test_allow_then_a_job_reaches_the_device_and_its_result_comes_back(client_factory, pins, registry, unlocked):  # noqa: F811
    client = await client_factory(service(pins, registry))
    await client.post("/api/devices/checkin", json=checkin_body(), headers=HEADER)
    response = await client.post(f"/api/settings/devices/{TOUCH}/allow", json={}, headers=unlocked)
    assert response.status == 200 and await response.json() == {"allowed": True}
    response = await client.post(f"/api/settings/devices/{TOUCH}/jobs", json={"type": "reboot"}, headers=unlocked)
    assert response.status == 200
    job = (await response.json())["job"]
    answer = await (await client.post("/api/devices/checkin", json=checkin_body(), headers=HEADER)).json()
    assert answer["state"] == "allowed" and answer["job"] == {"id": job["id"], "type": "reboot", "args": {}}
    done = {"id": job["id"], "type": "reboot", "state": "done", "words": "Rebooted"}
    await client.post("/api/devices/checkin", json=checkin_body(job=done), headers=HEADER)
    device = (await (await client.get("/api/settings/devices", headers=unlocked)).json())["devices"][0]
    assert device["job"] is None and device["last"]["words"] == "Rebooted"


async def test_refuse_and_forget(client_factory, pins, registry, unlocked):  # noqa: F811
    client = await client_factory(service(pins, registry))
    await client.post("/api/devices/checkin", json=checkin_body(), headers=HEADER)
    response = await client.post(f"/api/settings/devices/{TOUCH}/refuse", json={}, headers=unlocked)
    assert response.status == 200 and await response.json() == {"refused": True}
    response = await client.post("/api/devices/checkin", json=checkin_body(), headers=HEADER)
    assert response.status == 403 and (await response.json())["error"] == "This device was refused"
    response = await client.post(f"/api/settings/devices/{TOUCH}/forget", json={}, headers=unlocked)
    assert response.status == 404 and (await response.json())["error"] == f"There is no device {TOUCH}"
    assert (await client.post(f"/api/settings/devices/{TOUCH}/allow", data="x", headers=unlocked)).status == 415


async def test_job_refusals(client_factory, pins, registry, unlocked):  # noqa: F811
    meeting = StubMeeting()
    client = await client_factory(service(pins, registry, meeting=meeting))
    await joined(client, registry)
    response = await client.post(f"/api/settings/devices/{TOUCH}/jobs", json={"type": "rm -rf"}, headers=unlocked)
    assert response.status == 400 and (await response.json())["error"] == "There is no such job"
    response = await client.post(f"/api/settings/devices/{TOUCH}/jobs", json={"type": "wifi_connect", "args": {"ssid": "Office",
                                                                                                             "password": "short"}},
                                 headers=unlocked)
    assert response.status == 400 and (await response.json())["error"] == "A Wi-Fi password is 8 to 63 characters"
    meeting.state = MeetingState.CONNECTED
    for job in ("reboot", "update"):
        response = await client.post(f"/api/settings/devices/{TOUCH}/jobs", json={"type": job, "args": {"commit": B}}, headers=unlocked)
        assert response.status == 409 and (await response.json())["error"] == "Leave the meeting first"
    response = await client.post(f"/api/settings/devices/{TOUCH}/jobs", json={"type": "wifi_scan"}, headers=unlocked)
    assert response.status == 200
    response = await client.post(f"/api/settings/devices/{TOUCH}/jobs", json={"type": "wifi_scan"}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == "The touch Pi is busy: wait for it to finish"


async def test_the_software_page_lists_the_touch_pi_with_what_github_says_and_those_waiting(client_factory, pins, registry, helper,
                                                                                         unlocked):  # noqa: F811
    client = await client_factory(service(pins, registry, helper, github_with_changes()))
    await joined(client, registry)
    other = dict(checkin_body(), id="w" * 32)
    await client.post("/api/devices/checkin", json=other, headers=HEADER)
    answer = await (await client.get("/api/settings/software", headers=unlocked)).json()
    assert answer["tv"]["version"] == A
    [touch] = answer["devices"]
    assert touch["id"] == TOUCH and touch["kind"] == "touch" and touch["version"] == A and touch["online"] is True
    assert touch["newest"]["commit"] == B and touch["up_to_date"] is False and touch["error"] is None
    assert [c["title"] for c in touch["changes"]] == ["feat(settings): the Software page", "fix: one"]
    assert [w["id"] for w in answer["waiting"]] == ["w" * 32]


async def test_without_github_the_touch_pi_says_it_cant_check(client_factory, pins, registry, unlocked):  # noqa: F811
    client = await client_factory(service(pins, registry))
    await joined(client, registry)
    answer = await (await client.get("/api/settings/software", headers=unlocked)).json()
    assert answer["tv"]["available"] is False
    assert answer["devices"][0]["error"] == "Can't check for updates right now" and answer["devices"][0]["version"] == A


async def test_update_and_reboot_of_the_touch_pi_are_jobs(client_factory, pins, registry, helper, unlocked):  # noqa: F811
    client = await client_factory(service(pins, registry, helper, github_with_changes()))
    await joined(client, registry)
    response = await client.post("/api/settings/software/update", json={"device": TOUCH, "commit": B}, headers=unlocked)
    assert response.status == 200
    answer = await response.json()
    assert answer["updating"] is True and answer["job"]["type"] == "update" and answer["job"]["args"] == {"commit": B}
    assert helper.calls == []
    handed = (await (await client.post("/api/devices/checkin", json=checkin_body(), headers=HEADER)).json())["job"]
    assert handed["type"] == "update" and handed["args"] == {"commit": B}
    response = await client.post("/api/settings/reboot", json={"device": TOUCH}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == "The touch Pi is busy: wait for it to finish"
    response = await client.post("/api/settings/software/rollback", json={"device": TOUCH}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == "Not available on this device"
    await client.post("/api/devices/checkin", json=checkin_body(job=dict(handed, state="failed", words="No")), headers=HEADER)
    response = await client.post("/api/settings/reboot", json={"device": TOUCH}, headers=unlocked)
    assert response.status == 200 and (await response.json())["rebooting"] is True


async def test_a_device_waiting_to_join_gets_no_job(client_factory, pins, registry, unlocked):  # noqa: F811
    client = await client_factory(service(pins, registry))
    await client.post("/api/devices/checkin", json=checkin_body(), headers=HEADER)
    response = await client.post("/api/settings/reboot", json={"device": TOUCH}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == "This touch Pi has not joined yet: allow it first"


@pytest.mark.parametrize("path", ["/api/settings/software/update", "/api/settings/reboot", "/api/settings/software/update-all"])
async def test_the_touch_pi_is_not_updated_or_rebooted_in_a_meeting(client_factory, pins, registry, helper, unlocked, path):  # noqa: F811
    meeting = StubMeeting()
    client = await client_factory(service(pins, registry, helper, github_with_changes(), meeting=meeting))
    await joined(client, registry)
    meeting.state = MeetingState.CONNECTED
    response = await client.post(path, json={"device": TOUCH, "commit": B}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == "Leave the meeting first"
    assert registry.get(TOUCH)["job"] is None
    assert [c for c in helper.calls if c[0] != "version"] == []


async def test_update_all_updates_the_touch_pi_then_the_tv_pi(client_factory, pins, registry, helper, unlocked):  # noqa: F811
    client = await client_factory(service(pins, registry, helper, github_with_changes()))
    await joined(client, registry)
    response = await client.post("/api/settings/software/update-all", json={}, headers=unlocked)
    assert response.status == 200
    answer = await response.json()
    assert answer == {"updating": [TOUCH, "tv"], "errors": [], "commit": B}
    assert registry.get(TOUCH)["job"]["args"] == {"commit": B}
    assert ("update", {"commit": B}) in helper.calls
    # Again: the touch Pi is busy and the TV Pi's update runs, so nothing more is asked, and the TV Pi's refusal is said
    response = await client.post("/api/settings/software/update-all", json={}, headers=unlocked)
    answer = await response.json()
    assert answer["updating"] == [] and answer["errors"] == ["An update is under way: wait for it to finish"]


async def test_update_all_leaves_what_is_up_to_date(client_factory, pins, registry, helper, unlocked):  # noqa: F811
    client = await client_factory(service(pins, registry, helper, github_with_changes()))
    await joined(client, registry, version=B)
    answer = await (await client.post("/api/settings/software/update-all", json={}, headers=unlocked)).json()
    assert answer["updating"] == ["tv"] and registry.get(TOUCH)["job"] is None


async def test_update_all_needs_github(client_factory, pins, registry, helper, unlocked):  # noqa: F811
    client = await client_factory(service(pins, registry, helper))
    response = await client.post("/api/settings/software/update-all", json={}, headers=unlocked)
    assert response.status == 503 and (await response.json())["error"] == "Can't check for updates right now"
    transport = Transport(NEWEST)
    transport.down = True
    client = await client_factory(service(pins, registry, helper, GitHub(fetch=transport)))
    response = await client.post("/api/settings/software/update-all", json={}, headers=unlocked)
    assert response.status == 503
    assert [c for c in helper.calls if c[0] == "update"] == []


async def test_update_all_without_the_helper_updates_the_touch_pi(client_factory, pins, registry, unlocked):  # noqa: F811
    client = await client_factory(service(pins, registry, None, github_with_changes()))
    await joined(client, registry)
    answer = await (await client.post("/api/settings/software/update-all", json={}, headers=unlocked)).json()
    assert answer["updating"] == [TOUCH] and answer["errors"] == []


def test_from_config_keeps_the_registry_in_the_data_directory(tmp_path):
    from croom.control.service import ControlService
    from croom.core.config import Config

    class Fake:
        def available(self):
            return False

    config = Config()
    config.data_dir = str(tmp_path)
    service = ControlService.from_config(config, admin=Fake())
    assert service._registry.path == tmp_path / "devices.json"
