"""The TV Pi's Wi-Fi jobs in the root helper (croom.admin.wifi; room settings spec 2026-10-29, sections 3, 5 and 7): wifi_status,
wifi_scan, wifi_connect with the TV Pi's safety net (its gateway and the touch Pi's check-in, asked of the room page on the loopback; the
gateway alone, said so, on Ethernet or with no touch Pi joined), wifi_forget; one thing at a time with restarts, reboots and updates; the
record root's alone, without the password; a change cut off by the helper's stop finished or undone at its next start. NetworkManager is
a stand-in (fake_nm.py), the room page's answers a fake, the 90 seconds a fake clock."""

import getpass
import grp
import json
import os
import stat

import pytest

from croom.admin.jobs import WIFI_UNDER_WAY, Host, JobError, Jobs, Paths
from tests.unit.admin.fake_nm import FakeNM, PowerCut

NEW_UUID = "11111111-2222-3333-4444-555555555555"
B = "b" * 40


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Room:
    """The room page's GET /api/devices/seen-since, as the helper asks it: `touch_at` is when the touch Pi checks in again (None:
    never), `known` whether a touch Pi has joined, `down` no answer at all."""

    def __init__(self, clock):
        self.clock = clock
        self.known = True
        self.touch_at = None
        self.down = False
        self.asked = []

    def __call__(self, host, port, path):
        self.asked.append((host, port, path))
        if self.down:
            return None
        since = float(path.split("since=")[1])
        seen = self.touch_at is not None and self.clock.now >= self.touch_at and self.touch_at >= since
        return {"seen": seen, "known": self.known}


class Machine:
    def __init__(self, tmp_path, background=None):
        self.paths = Paths(config_dir=tmp_path / "etc", data_dir=tmp_path / "var", media_root=tmp_path / "media",
                           install_dir=tmp_path / "opt", nm_connections=tmp_path / "nm")
        for folder in (self.paths.config_dir, self.paths.data_dir, self.paths.media_root, self.paths.install_dir,
                       self.paths.nm_connections):
            folder.mkdir()
        self.paths.config.write_text("control:\n  port: 8099\n")
        self.nm = FakeNM(self.paths.nm_connections)
        self.clock = Clock()
        self.room = Room(self.clock)
        self.queued = []
        self.jobs = self.make(background)

    def make(self, background=None):
        return Jobs(Host(paths=self.paths, run=self.nm, owner=getpass.getuser(), group=grp.getgrgid(os.getgid()).gr_name,
                         probe=lambda host, port: True, clock=self.clock, sleep=self.clock.sleep, monotonic=self.clock,
                         background=background or (lambda work: work()), ask_room=self.room, new_uuid=self.new_uuid))

    def new_uuid(self):
        self.made = getattr(self, "made", 0) + 1
        return NEW_UUID if self.made == 1 else f"{self.made:08d}-2222-3333-4444-555555555555"

    def job(self, name, **args):
        return self.jobs.table[name](args)

    def state(self):
        return json.loads(self.paths.state.read_text())


@pytest.fixture
def m(tmp_path):
    return Machine(tmp_path)


def test_status_says_how_the_tv_pi_is_connected(m):
    answer = m.job("wifi_status")
    assert answer == {"wifi": True, "device": "wlan0", "connection": "wifi", "ssid": "Office", "signal": 70, "address": "192.168.1.42",
                      "saved": ["Office"], "change": None}


def test_scan_lists_the_networks_in_range(m):
    networks = m.job("wifi_scan")["networks"]
    assert [n["ssid"] for n in networks][:3] == ["New Net", "Office", "Modern"]
    assert next(n for n in networks if n["ssid"] == "Office")["saved"] is True


def test_a_change_is_kept_when_the_tv_pi_reaches_its_gateway_and_the_touch_pi_checks_in(m):
    m.room.touch_at = m.clock.now + 30                   # the touch Pi finds the TV Pi again half a minute after the switch
    assert m.job("wifi_connect", ssid="New Net", password="correct horse") == {"changing": True}
    change = m.job("wifi_status")["change"]
    assert change["state"] == "done" and change["ssid"] == "New Net"
    assert change["words"] == "Joined New Net. The TV Pi reached its gateway, and the touch Pi checked in"
    assert m.nm.ssid_on() == "New Net"
    host, port, path = m.room.asked[-1]
    assert (host, port) == ("127.0.0.1", 8099) and path.startswith("/api/devices/seen-since?kind=touch&since=")
    # The record is the helper's own, and the password is in none of it
    assert stat.S_IMODE(os.stat(m.paths.state).st_mode) == 0o600 and stat.S_IMODE(os.stat(m.paths.admin_dir).st_mode) == 0o700
    assert "correct horse" not in m.paths.state.read_text()
    assert m.state()["wifi"]["new"] == NEW_UUID


def test_a_change_the_touch_pi_does_not_check_in_after_is_undone(m):
    started = m.clock.now
    m.job("wifi_connect", ssid="New Net", password="correct horse")
    change = m.job("wifi_status")["change"]
    assert change["state"] == "undone"
    assert change["words"] == ("Joined New Net, but the touch Pi did not check in within 90 seconds. The TV Pi is back on Office")
    assert m.nm.ssid_on() == "Office" and NEW_UUID not in m.nm.profiles
    assert 90 <= m.clock.now - started <= 95
    m.room.touch_at = started                             # a check-in from before the switch does not count
    m.job("wifi_connect", ssid="New Net", password="correct horse")
    assert m.job("wifi_status")["change"]["state"] == "undone"


def test_on_ethernet_or_with_no_touch_pi_only_the_gateway_is_checked_and_the_words_say_so(m):
    m.nm.ethernet = True
    m.job("wifi_connect", ssid="New Net", password="correct horse")
    change = m.job("wifi_status")["change"]
    assert change["state"] == "done"
    assert change["words"] == "Joined New Net. The TV Pi is on Ethernet, so only its Wi-Fi gateway was checked"
    assert m.room.asked == []                              # the touch Pi was not waited for
    m.nm.ethernet = False
    m.room.known = False
    m.job("wifi_connect", ssid="Guest")
    change = m.job("wifi_status")["change"]
    assert change["words"] == "Joined Guest. No touch Pi has joined this room, so only the gateway was checked"


def test_a_room_page_that_does_not_answer_counts_as_a_touch_pi_to_wait_for(m):
    m.room.down = True
    m.job("wifi_connect", ssid="New Net", password="correct horse")
    assert m.job("wifi_status")["change"]["state"] == "undone"
    assert m.nm.ssid_on() == "Office"


def test_a_change_is_refused_with_words_before_anything_changes(m):
    with pytest.raises(JobError, match="Enter the password for New Net"):
        m.job("wifi_connect", ssid="New Net")
    with pytest.raises(JobError, match="does not take"):
        m.job("wifi_connect", ssid="New Net", password="correct horse", interface="eth0")
    with pytest.raises(JobError, match="Hidden must be true or false"):
        m.job("wifi_connect", ssid="New Net", password="correct horse", hidden="yes")
    assert not m.paths.state.exists() or "wifi" not in m.state()
    assert m.job("wifi_connect", ssid="Guest") == {"changing": True}       # the refusals left the helper free


def test_while_a_change_runs_nothing_that_restarts_or_changes_wifi_may_begin(tmp_path):
    m = Machine(tmp_path, background=lambda work: m.queued.append(work))
    m.job("wifi_connect", ssid="New Net", password="correct horse")
    for job, args in (("restart", {}), ("reboot", {}), ("update", {"commit": B}), ("wifi_connect", {"ssid": "Guest"}),
                      ("wifi_scan", {}), ("wifi_forget", {"ssid": "Office"})):
        with pytest.raises(JobError, match=WIFI_UNDER_WAY):
            m.jobs.table[job](args)
    m.queued.pop()()
    assert m.job("wifi_status")["change"]["state"] == "undone"
    m.job("restart")                                                      # and a change waits for a restart's watch
    with pytest.raises(JobError, match="The room is restarting"):
        m.job("wifi_connect", ssid="Guest")


def test_a_change_cut_off_by_a_power_cut_is_undone_at_the_helpers_next_start(m):
    def cut(args):
        if args[0] == "ping":                              # joined, and checking: the power goes
            raise PowerCut()

    m.nm.cut = cut
    with pytest.raises(PowerCut):
        m.job("wifi_connect", ssid="New Net", password="correct horse")
    m.nm.cut = None
    assert m.state()["wifi"]["state"] == "trying" and m.nm.ssid_on() == "New Net"
    m.nm.reboot()
    again = m.make()
    again.resume()
    record = json.loads(m.paths.state.read_text())["wifi"]
    assert record["state"] == "undone" and record["words"].startswith("The TV Pi restarted before New Net was proven")
    assert m.nm.ssid_on() == "Office"
    assert again.table["wifi_connect"]({"ssid": "Guest"}) == {"changing": True}   # free again


def test_forget(m):
    m.nm.profiles["u-guest"] = {"name": "Guest", "ssid": "Guest", "psk": None, "autoconnect": True, "file": None, "hidden": False,
                                "mgmt": None}
    assert m.job("wifi_forget", ssid="Guest") == {"words": "Forgot Guest"}
    with pytest.raises(JobError, match="The TV Pi is on Office: join another network first"):
        m.job("wifi_forget", ssid="Office")
