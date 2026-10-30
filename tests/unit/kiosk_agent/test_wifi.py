"""The touch Pi agent's Wi-Fi jobs (room settings spec 2026-10-29, sections 3 and 7): wifi_scan (its networks the TV Pi keeps),
wifi_connect with the touch Pi's safety net (it must reach the room's GET /api/status on the new network within 90 seconds, or it goes
back), wifi_forget, the Wi-Fi status in each check-in, and a change cut off by a power cut undone at the next start. The room is the real
registry behind a fake HTTP; NetworkManager a stand-in (tests/unit/admin/fake_nm.py); the 90 seconds a fake clock."""

import os
import stat

import pytest

from tests.unit.admin.fake_nm import PowerCut
from tests.unit.kiosk_agent.fakes import Device, agent


@pytest.fixture
def device(tmp_path):
    return Device(tmp_path)


def allowed(device):
    me = device.agent()
    me.start()
    me.checkin()
    device.room.registry.allow(me.identity["id"])
    return me


def give(device, me, job, **args):
    queued = device.room.registry.queue(me.identity["id"], job, args)
    me.checkin()
    me.wait(10)
    me.checkin()
    return queued


def last(device, me):
    return device.room.registry.get(me.identity["id"])["last"]


def test_a_scan_gives_the_room_the_networks_in_range(device):
    me = allowed(device)
    give(device, me, "wifi_scan")
    card = device.room.registry.get(me.identity["id"])
    assert card["last"]["state"] == "done" and card["last"]["words"] == "Found 6 networks"
    assert [n["ssid"] for n in card["scan"]["networks"]][:3] == ["New Net", "Office", "Modern"]
    office = next(n for n in card["scan"]["networks"] if n["ssid"] == "Office")
    assert office == {"ssid": "Office", "signal": 70, "security": "WPA2", "open": False, "saved": True, "in_use": True}
    _, body, _ = device.room.posts[-1]
    assert "result" in body["job"]
    device.clock.now += agent.RESULT_FOR_S
    me.checkin()
    _, body, _ = device.room.posts[-1]
    assert "result" not in body["job"]                    # the room has it: not sent with every check-in after


def test_joining_a_network_the_touch_pi_reaches_the_room_on(device):
    me = allowed(device)
    give(device, me, "wifi_connect", ssid="New Net", password="correct horse")
    assert last(device, me)["state"] == "done"
    assert last(device, me)["words"] == "Joined New Net. The touch Pi reaches the room"
    assert device.nm.ssid_on() == "New Net"
    new = next(p for p in device.nm.profiles.values() if p["ssid"] == "New Net")
    assert new["autoconnect"] and stat.S_IMODE(os.stat(new["file"]).st_mode) == 0o600
    assert not any("correct horse" in " ".join(call) for call in device.nm.calls)
    assert "correct horse" not in device.paths.wifi.read_text() and "correct horse" not in device.paths.job.read_text()
    assert stat.S_IMODE(os.stat(device.paths.wifi).st_mode) == 0o600
    _, body, _ = device.room.posts[-1]
    assert body["network"]["ssid"] == "New Net" and body["network"]["change"]["state"] == "done"   # read again after the job


def test_a_network_the_room_cannot_be_reached_on_is_left_within_90_seconds(device):
    me = allowed(device)
    device.room_up = lambda url: device.nm.ssid_on() != "New Net"
    started = device.clock.now
    give(device, me, "wifi_connect", ssid="New Net", password="correct horse")
    assert last(device, me)["state"] == "failed"
    assert last(device, me)["words"] == ("Joined New Net, but the touch Pi could not reach the room at http://pimeet-3.local:8080/ "
                                         "within 90 seconds. The touch Pi is back on Office")
    assert device.nm.ssid_on() == "Office" and all(p["ssid"] != "New Net" for p in device.nm.profiles.values())
    assert 90 <= device.clock.now - started <= 100


def test_on_ethernet_the_wifi_gateway_must_answer_as_well(device):
    me = allowed(device)
    device.nm.ethernet = True
    device.nm.reachable = lambda ssid: ssid != "New Net"
    give(device, me, "wifi_connect", ssid="New Net", password="correct horse")
    assert last(device, me)["words"] == ("Joined New Net, but the touch Pi could not reach its gateway within 90 seconds. "
                                         "The touch Pi is back on Office")
    device.nm.reachable = lambda ssid: True
    give(device, me, "wifi_connect", ssid="New Net", password="correct horse")
    assert last(device, me)["words"] == "Joined New Net. The touch Pi is on Ethernet, so its Wi-Fi gateway was checked as well"


def test_a_refused_password_and_other_refusals_say_why(device):
    me = allowed(device)
    give(device, me, "wifi_connect", ssid="New Net", password="wrong horse")
    assert last(device, me)["words"] == "Could not join New Net: the password was not accepted. The touch Pi is back on Office"
    give(device, me, "wifi_connect", ssid="New Net")
    assert last(device, me)["words"] == "Enter the password for New Net"
    me.take({"id": "f" * 16, "type": "wifi_connect", "args": {"ssid": "Guest", "hidden": "yes"}})
    me.wait(5)
    assert device.job()["words"] == "Hidden must be true or false"
    assert device.nm.ssid_on() == "Office"


def test_forget(device):
    me = allowed(device)
    device.nm.profiles["u-guest"] = {"name": "Guest", "ssid": "Guest", "psk": None, "autoconnect": True, "file": None, "hidden": False,
                                     "mgmt": None}
    give(device, me, "wifi_forget", ssid="Guest")
    assert last(device, me)["words"] == "Forgot Guest" and "u-guest" not in device.nm.profiles
    give(device, me, "wifi_forget", ssid="Office")
    assert last(device, me)["state"] == "failed"
    assert last(device, me)["words"] == "The touch Pi is on Office: join another network first, or it would be cut off"


def test_a_change_cut_off_by_a_power_cut_is_undone_at_the_next_start(device):
    me = allowed(device)
    job_id = "e" * 16

    me._set_job(id=job_id, type="wifi_connect", state="running", words="Starting")
    device.room_up = lambda url: (_ for _ in ()).throw(PowerCut())   # the power goes as it first asks the room
    with pytest.raises(PowerCut):
        me.do_wifi_connect(job_id, {"ssid": "New Net", "password": "correct horse"})
    device.room_up = lambda url: True
    assert device.nm.ssid_on() == "New Net"
    device.nm.reboot()                                      # the profile on trial does not connect by itself: back on the old one
    after = device.agent()
    after.start()
    after.checkin()
    result = last(device, after)
    assert result["id"] == job_id and result["state"] == "failed"
    assert result["words"] == "The touch Pi restarted before New Net was proven. The touch Pi is back on Office"
    assert device.nm.ssid_on() == "Office" and all(p["ssid"] != "New Net" for p in device.nm.profiles.values())
    assert list(device.nm.dir.iterdir()) == []


def test_the_wifi_status_in_the_check_ins_is_read_now_and_then(device):
    me = allowed(device)
    status_reads = lambda: sum(1 for c in device.nm.calls if c[-2:] == ["device", "status"])   # noqa: E731
    me.checkin()
    me.checkin()
    first = status_reads()
    assert first >= 1
    device.clock.now += agent.STATUS_EVERY_S
    me.checkin()
    assert status_reads() == first + 1
    device.nm.wifi_device = None
    device.nm.ethernet = True
    device.clock.now += agent.STATUS_EVERY_S
    me.checkin()
    _, body, _ = device.room.posts[-1]
    assert body["network"]["wifi"] is False and body["network"]["connection"] == "ethernet"
    assert body["network"]["address"] == "10.0.0.9"         # the address it reaches the room from, kept


def test_a_network_that_drops_the_touch_pi_during_its_trial_is_not_kept(device):
    """Dropped from the new network, NetworkManager puts the touch Pi back on the old one, where the room answers: not proof."""
    me = allowed(device)
    asked = []

    def room_up(url):
        asked.append(device.nm.ssid_on())
        if device.nm.ssid_on() == "New Net":
            device.nm.active["wlan0"] = "u-old"            # dropped as it first asks the room
            return False
        return True

    device.room_up = room_up
    give(device, me, "wifi_connect", ssid="New Net", password="correct horse")
    assert last(device, me)["state"] == "failed"
    assert last(device, me)["words"] == "Joined New Net, but the touch Pi did not stay on New Net. The touch Pi is back on Office"
    assert all(p["ssid"] != "New Net" for p in device.nm.profiles.values())
    assert device.nm.without_c_locale == []                # the agent's nmcli runs in the C locale too
