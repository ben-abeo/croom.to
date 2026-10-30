"""The door sign's routes on the TV Pi (room settings spec 2026-10-29, sections 6 and 7; Task 11): GET /api/devices/apk/<versionCode>
(the APK of a GitHub release door-sign-<versionCode>, only to a door sign that has joined), the door sign's card on Settings › Software
(its version, the newest release and those between), Update {device, version_code} (the APK fetched and checked first, then the job with
its size and SHA-256), never a commit for a door sign nor a version code for a touch Pi, Reboot, Update all, and what a panel that is not
its device owner can't do. The registry is the real one in memory; GitHub is the real client on a fake transport; the APK download is a
fake."""

import hashlib

import pytest

from croom.meeting.providers.base import MeetingState
from croom.settings.apk import ApkStore
from croom.settings.checkin import Registry
from croom.settings.github import CANT_CHECK, GitHub
from tests.unit.control.test_service import (  # noqa: F401  (client_factory is a fixture)
    StubMeeting,
    client_factory,
    make_service,
)
from tests.unit.control.test_settings_software_api import (  # noqa: F401  (fixtures)
    B,
    Helper,
    pins,
    unlocked,
)
from tests.unit.settings.test_apk import APK, Download, a_release, url
from tests.unit.settings.test_checkin import Clock, Ids
from tests.unit.settings.test_github import NEWEST, REPO, Transport

SIGN = "s" * 32
TOUCH = "t" * 32
TOKEN = "k" * 43
SHA = hashlib.sha256(APK).hexdigest()
NOT_OWNER = "The door sign is not the panel's device owner: see its setup guide"


@pytest.fixture
def registry():
    return Registry(None, clock=Clock(), new_id=Ids())


def github_and_apks(tmp_path, releases=None):
    if releases is None:
        releases = (a_release(2), a_release(3, name="Wi-Fi from Settings"))
    answers = dict(NEWEST)
    answers[f"{REPO}/releases?per_page=50"] = (200, list(releases))
    for release in releases:
        answers[f"{REPO}/releases/tags/{release['tag_name']}"] = (200, release)
    transport = Transport(answers)
    github = GitHub(fetch=transport)
    download = Download({url(int(r["tag_name"].rsplit("-", 1)[1])): APK for r in releases})
    return github, ApkStore(github, tmp_path / "door-sign", download=download), transport, download


def sign_body(version="2", cannot=None, job=None):
    return {"id": SIGN, "kind": "sign", "version": version, "version_name": version + ".0", "name": "SMT1019", "network": {},
            "job": job, "cannot": cannot or {}}


def join(registry, device=SIGN, kind="sign", version="2", cannot=None):
    body = sign_body(version, cannot) if kind == "sign" else {"id": device, "kind": kind, "version": "a" * 40, "name": "pi",
                                                              "network": {}, "job": None}
    registry.checkin(body, TOKEN, "10.0.0.20")
    registry.allow(device)


def service(pins, registry, tmp_path, meeting=None, apks=True, admin=None):  # noqa: F811
    github, store, _, _ = github_and_apks(tmp_path)
    return make_service(pin_store=pins, registry=registry, github=github, apks=store if apks else None, admin=admin,
                        meeting=meeting or StubMeeting())


def apk_headers(device=SIGN, token=TOKEN):
    return {"X-Croom-Device-Id": device, "X-Croom-Device-Token": token}


async def test_the_apk_is_served_only_to_a_door_sign_that_has_joined(client_factory, pins, registry, tmp_path):  # noqa: F811
    client = await client_factory(service(pins, registry, tmp_path))
    response = await client.get("/api/devices/apk/3")
    assert response.status == 401 and (await response.json())["error"] == "Send the device's id and token"
    registry.checkin(sign_body(), TOKEN)                         # waiting to join
    assert (await client.get("/api/devices/apk/3", headers=apk_headers())).status == 403
    registry.allow(SIGN)
    assert (await client.get("/api/devices/apk/3", headers=apk_headers(token="x" * 43))).status == 403
    join(registry, TOUCH, "touch")
    response = await client.get("/api/devices/apk/3", headers=apk_headers(TOUCH))
    assert response.status == 403 and (await response.json())["error"] == "Only a device that has joined this room may ask this"
    response = await client.get("/api/devices/apk/3", headers=apk_headers())
    assert response.status == 200 and await response.read() == APK
    assert response.headers["X-Croom-Apk-Sha256"] == SHA and response.headers["X-Croom-Apk-Size"] == str(len(APK))
    assert response.headers["Content-Type"] == "application/vnd.android.package-archive"


async def test_apk_route_refusals(client_factory, pins, registry, tmp_path):  # noqa: F811
    join(registry)
    client = await client_factory(service(pins, registry, tmp_path))
    for bad in ("0", "x", "-1", "12345678901"):
        response = await client.get(f"/api/devices/apk/{bad}", headers=apk_headers())
        assert response.status == 400, bad
    response = await client.get("/api/devices/apk/9", headers=apk_headers())
    assert response.status == 404 and (await response.json())["error"] == "There is no door sign release 9 on GitHub"
    client = await client_factory(service(pins, registry, tmp_path, apks=False))
    response = await client.get("/api/devices/apk/3", headers=apk_headers())
    assert response.status == 503 and (await response.json())["error"] == CANT_CHECK


async def test_the_door_signs_software_card(client_factory, pins, registry, tmp_path, unlocked):  # noqa: F811
    join(registry)
    client = await client_factory(service(pins, registry, tmp_path))
    answer = await (await client.get("/api/settings/software", headers=unlocked)).json()
    [card] = answer["devices"]
    assert card["kind"] == "sign" and card["version"] == "2" and card["version_name"] == "2.0"
    assert card["newest"]["version_code"] == 3 and card["up_to_date"] is False and card["error"] is None
    assert [c["title"] for c in card["changes"]] == ["Wi-Fi from Settings"]
    client = await client_factory(service(pins, registry, tmp_path, apks=False))
    [card] = (await (await client.get("/api/settings/software", headers=unlocked)).json())["devices"]
    assert card["error"] == CANT_CHECK


async def test_update_fetches_the_apk_then_gives_the_door_sign_its_job(client_factory, pins, registry, tmp_path, unlocked):  # noqa: F811
    join(registry)
    client = await client_factory(service(pins, registry, tmp_path))
    response = await client.post("/api/settings/software/update", json={"device": SIGN, "version_code": 3}, headers=unlocked)
    assert response.status == 200
    answer = await response.json()
    assert answer["updating"] is True and answer["job"]["args"] == {"version_code": 3, "sha256": SHA, "size": len(APK)}
    assert (tmp_path / "door-sign" / "door-sign-3.apk").read_bytes() == APK
    handed = registry.checkin(sign_body(), TOKEN)["job"]
    assert handed["type"] == "update" and handed["args"] == {"version_code": 3, "sha256": SHA, "size": len(APK)}


async def test_a_door_sign_takes_a_version_code_and_a_touch_pi_a_commit(client_factory, pins, registry, tmp_path, unlocked):  # noqa: F811
    join(registry)
    join(registry, TOUCH, "touch")
    client = await client_factory(service(pins, registry, tmp_path))
    for body, words in (({"device": SIGN, "commit": B}, "A door sign is updated to a release of the app (a version code), not to a commit"),
                        ({"device": SIGN, "commit": B, "version_code": 3},
                         "A door sign is updated to a release of the app (a version code), not to a commit"),
                        ({"device": SIGN}, "Send the version of the door sign app to install (its version code)"),
                        ({"device": SIGN, "version_code": "3"}, "Send the version of the door sign app to install (its version code)"),
                        ({"device": TOUCH, "version_code": 3}, "Send the version to install (a commit)"),
                        ({"device": TOUCH, "version_code": 3, "commit": B},
                         "A touch Pi is updated to a commit of main, not to a door sign release")):
        response = await client.post("/api/settings/software/update", json=body, headers=unlocked)
        assert response.status == 400 and (await response.json())["error"] == words, body
    # The devices' own job route keeps the same rule
    response = await client.post(f"/api/settings/devices/{SIGN}/jobs", json={"type": "update", "args": {"commit": B}}, headers=unlocked)
    assert response.status == 400
    response = await client.post(f"/api/settings/devices/{TOUCH}/jobs", json={"type": "update", "args": {"version_code": 3}},
                                 headers=unlocked)
    assert response.status == 400 and (await response.json())["error"] == \
        "A touch Pi is updated to a commit of main, not to a door sign release"
    response = await client.post(f"/api/settings/devices/{SIGN}/jobs", json={"type": "update", "args": {"version_code": 3}},
                                 headers=unlocked)
    assert response.status == 200 and (await response.json())["job"]["args"]["sha256"] == SHA
    assert registry.get(TOUCH)["job"] is None


async def test_update_refusals_for_a_door_sign(client_factory, pins, registry, tmp_path, unlocked):  # noqa: F811
    join(registry, version="3")
    client = await client_factory(service(pins, registry, tmp_path))
    response = await client.post("/api/settings/software/update", json={"device": SIGN, "version_code": 3}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == \
        "The door sign has version 3 already: Android installs only a newer one"
    response = await client.post("/api/settings/software/update", json={"device": SIGN, "version_code": 9}, headers=unlocked)
    assert response.status == 404 and (await response.json())["error"] == "There is no door sign release 9 on GitHub"
    registry.checkin(sign_body("2", cannot={"update": NOT_OWNER, "reboot": NOT_OWNER}), TOKEN)
    response = await client.post("/api/settings/software/update", json={"device": SIGN, "version_code": 3}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == NOT_OWNER
    response = await client.post("/api/settings/reboot", json={"device": SIGN}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == NOT_OWNER
    assert registry.get(SIGN)["job"] is None
    assert not (tmp_path / "door-sign").exists()          # nothing was downloaded for a panel that can't install it


async def test_in_a_meeting_and_while_busy(client_factory, pins, registry, tmp_path, unlocked):  # noqa: F811
    join(registry)
    meeting = StubMeeting()
    meeting.state = MeetingState.CONNECTED
    client = await client_factory(service(pins, registry, tmp_path, meeting=meeting))
    response = await client.post("/api/settings/software/update", json={"device": SIGN, "version_code": 3}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == "Leave the meeting first"
    client = await client_factory(service(pins, registry, tmp_path))
    assert (await client.post("/api/settings/reboot", json={"device": SIGN}, headers=unlocked)).status == 200
    response = await client.post("/api/settings/software/update", json={"device": SIGN, "version_code": 3}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == "The door sign is busy: wait for it to finish"


async def test_update_all_updates_a_door_sign_that_is_behind(client_factory, pins, registry, tmp_path, unlocked):  # noqa: F811
    join(registry)
    client = await client_factory(service(pins, registry, tmp_path))
    answer = await (await client.post("/api/settings/software/update-all", json={}, headers=unlocked)).json()
    assert answer["updating"] == [SIGN] and answer["errors"] == []
    assert registry.get(SIGN)["job"]["args"] == {"version_code": 3, "sha256": SHA, "size": len(APK)}
    # Up to date, or one that can't update itself: left alone
    registry2 = Registry(None, clock=Clock(), new_id=Ids())
    join(registry2, version="3")
    client = await client_factory(service(pins, registry2, tmp_path))
    assert (await (await client.post("/api/settings/software/update-all", json={}, headers=unlocked)).json())["updating"] == []
    registry3 = Registry(None, clock=Clock(), new_id=Ids())
    join(registry3, cannot={"update": NOT_OWNER})
    client = await client_factory(service(pins, registry3, tmp_path))
    answer = await (await client.post("/api/settings/software/update-all", json={}, headers=unlocked)).json()
    assert answer["updating"] == [] and answer["errors"] == []


async def test_the_network_page_lists_the_door_sign_and_its_wifi_jobs(client_factory, pins, registry, tmp_path, unlocked):  # noqa: F811
    join(registry)
    client = await client_factory(service(pins, registry, tmp_path))
    [card] = (await (await client.get("/api/settings/network", headers=unlocked)).json())["devices"]
    assert card["kind"] == "sign"
    response = await client.post(f"/api/settings/network/{SIGN}/connect", json={"ssid": "Office", "password": "secret-words"},
                                 headers=unlocked)
    assert response.status == 200 and (await response.json())["job"]["args"] == {"ssid": "Office", "hidden": False}
    handed = registry.checkin(sign_body(), TOKEN)["job"]
    assert handed["type"] == "wifi_connect" and handed["args"]["password"] == "secret-words"
    registry.checkin(sign_body(cannot={"wifi_scan": NOT_OWNER}, job={"id": handed["id"], "type": "wifi_connect", "state": "failed",
                                                                     "words": "x"}), TOKEN)
    response = await client.post(f"/api/settings/network/{SIGN}/scan", json={}, headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == NOT_OWNER
