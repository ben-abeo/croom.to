"""Wi-Fi through nmcli (croom.admin.nmcli, room settings spec 2026-10-29, sections 3 and 7), which the TV Pi's helper and the touch Pi's
agent share: nmcli's terse output read (its escapes too), what a person types checked, the keyfile a new network is saved in (root's
alone: the password never on a command line), and a change with its safety net: kept when the checks pass within 90 seconds, undone when
the password is refused or the checks do not pass, and finished or undone after a power cut at any step. NetworkManager is a stand-in
(fake_nm.py); the 90 seconds run on a fake clock."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from croom.admin import nmcli
from croom.admin.nmcli import Check, Wifi, WifiError
from tests.unit.admin.fake_nm import FakeNM, PowerCut, read_keyfile

NEW_UUID = "11111111-2222-3333-4444-555555555555"


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Store:
    """The change's record (root's alone on a device): every save is kept, to see what was written before what."""

    def __init__(self):
        self.saved = []
        self.cut = None

    def load(self):
        return dict(self.saved[-1]) if self.saved else {}

    def save(self, record):
        if self.cut:
            self.cut(None)
        self.saved.append(dict(record))   # a whole record or none: the devices write it to a file beside it and rename it


@pytest.fixture
def nm(tmp_path):
    return FakeNM(tmp_path)


@pytest.fixture
def clock():
    return Clock()


def wifi_of(nm, clock, store=None):
    return Wifi(run=nm, store=store or Store(), connections=nm.dir, name="TV Pi", clock=clock, monotonic=clock, sleep=clock.sleep,
                new_uuid=lambda: NEW_UUID)


def gateway_only(wifi):
    return lambda device, since: ([wifi.gateway_check(device)], "")


# --- reading nmcli


def test_terse_fields_are_split_at_unescaped_colons():
    assert nmcli.split_terse(r"*:Cafe\: Free:30:WPA1 WPA2") == ["*", "Cafe: Free", "30", "WPA1 WPA2"]
    assert nmcli.split_terse(r"a\\:b") == ["a\\", "b"]
    assert nmcli.split_terse("eth0:ethernet:unavailable:") == ["eth0", "ethernet", "unavailable", ""]
    assert nmcli.parse_values("IP6.ADDRESS[1]:fe80\\:\\:1/64\nIP4.GATEWAY:10.0.0.1\n") == {"IP6.ADDRESS[1]": "fe80::1/64",
                                                                                           "IP4.GATEWAY": "10.0.0.1"}
    assert nmcli.parse_values("IP6.ADDRESS[1]:fe80::1/64\n") == {"IP6.ADDRESS[1]": "fe80::1/64"}   # an nmcli that does not escape


def test_a_scan_lists_each_network_once_strongest_first_without_hidden_ones():
    output = "\n".join([" :Office:40:WPA2", "*:Office:70:WPA2", " :Guest:55:", r" :Cafe\: Free:30:WPA1 WPA2", " ::90:WPA2",
                        " :Corp:60:WPA2 802.1X", " :Modern:20:WPA3", " :Mixed:21:WPA2 WPA3", " :Old:10:WEP", " :Enhanced:12:OWE",
                        "garbage"])
    networks = nmcli.parse_scan(output)
    assert [n["ssid"] for n in networks] == ["Office", "Corp", "Guest", "Cafe: Free", "Mixed", "Modern", "Enhanced", "Old"]
    office = networks[0]
    assert office == {"ssid": "Office", "signal": 70, "security": "WPA2", "kind": "psk", "open": False, "in_use": True}
    kinds = {n["ssid"]: n["kind"] for n in networks}
    assert kinds == {"Office": "psk", "Corp": "enterprise", "Guest": "open", "Cafe: Free": "psk", "Mixed": "psk", "Modern": "sae",
                     "Enhanced": "owe", "Old": "wep"}
    assert [n["open"] for n in networks if n["ssid"] in ("Guest", "Enhanced")] == [True, True]


def test_what_a_person_types_is_checked():
    for good in ("Office", "x" * 32, "Café 5G", "a:b\\c"):
        assert nmcli.check_ssid(good) == good
    for bad in ("", "   ", "x" * 33, "é" * 17, "tab\there", None, 5):
        with pytest.raises(WifiError, match="1 to 32 characters"):
            nmcli.check_ssid(bad)
    for good in ("", None, "12345678", "x" * 63, "a" * 64, "0123456789abcdefABCDEF" * 2 + "0123456789abcdefABCD", "with space ~!"):
        assert nmcli.check_password(good) == (good or "")
    for bad in ("1234567", "x" * 64, "x" * 65, "pässword", "line\nbreak", 12345678):
        with pytest.raises(WifiError, match="8 to 63"):
            nmcli.check_password(bad)


def test_the_keyfile_keeps_any_name_and_password_as_they_are(tmp_path):
    text = nmcli.keyfile("u-1", "Café: 5G", " pass\\word ", "psk", hidden=True)
    path = tmp_path / "k.nmconnection"
    path.write_text(text)
    sections = read_keyfile(path)
    assert sections["connection"]["autoconnect"] == "false" and sections["connection"]["uuid"] == "u-1"
    assert bytes(int(b) for b in sections["wifi"]["ssid"].split(";") if b).decode() == "Café: 5G"
    assert sections["wifi"]["hidden"] == "true"
    assert sections["wifi-security"] == {"key-mgmt": "wpa-psk", "psk": "\\spass\\\\word\\s"}
    assert "wifi-security" not in read_keyfile_text(nmcli.keyfile("u-2", "Guest", "", "open", hidden=False), tmp_path)
    assert read_keyfile_text(nmcli.keyfile("u-3", "Modern", "sae password", "sae", False), tmp_path)["wifi-security"]["key-mgmt"] == "sae"


def read_keyfile_text(text, folder):
    path = folder / "t.nmconnection"
    path.write_text(text)
    return read_keyfile(path)


def test_the_module_needs_only_the_standard_library():
    """The touch Pi loads it by its path with the system's python3, outside the venv."""
    path = Path(nmcli.__file__)
    code = ("import importlib.util, sys; s = importlib.util.spec_from_file_location('n', sys.argv[1]); m = importlib.util.module_from_spec(s);"
            " sys.modules['n'] = m; s.loader.exec_module(m); print(m.split_terse('a:b'))")
    done = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(path)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "['a', 'b']"


# --- status and scan


def test_status_on_wifi_on_ethernet_and_without_wifi(nm, clock):
    wifi = wifi_of(nm, clock)
    assert wifi.status() == {"wifi": True, "device": "wlan0", "connection": "wifi", "ssid": "Office", "signal": 70,
                             "address": "192.168.1.42", "saved": ["Office"], "change": None}
    nm.ethernet = True
    status = wifi.status()
    assert status["connection"] == "ethernet" and status["address"] == "192.168.50.20" and status["ssid"] == "Office"
    nm.active = {}
    nm.ethernet = False
    status = wifi.status()
    assert status["connection"] == "none" and status["ssid"] is None and status["address"] is None
    nm.wifi_device = None
    assert wifi.status()["wifi"] is False


def test_a_scan_is_reused_briefly_and_marks_the_saved_networks(nm, clock):
    wifi = wifi_of(nm, clock)
    first = wifi.scan()
    assert first["networks"][0]["ssid"] == "New Net" and {n["ssid"]: n["saved"] for n in first["networks"]}["Office"] is True
    lists = lambda: [c for c in nm.calls if "list" in c]   # noqa: E731
    assert lists()[-1][-4:] == ["--rescan", "yes", "ifname", "wlan0"]
    clock.now += 10
    wifi.scan()
    assert len(lists()) == 1
    clock.now += 15
    nm.rescan_refused = True
    again = wifi.scan()
    assert [c[-3] for c in lists()[1:]] == ["yes", "no"]     # refused a new scan so soon: what it found last
    assert again["networks"] == first["networks"]
    nm.scan_fails = True
    clock.now += 30
    with pytest.raises(WifiError, match="Could not look for networks: Wi-Fi is disabled."):
        wifi.scan()
    nm.wifi_device = None
    with pytest.raises(WifiError, match="This TV Pi has no Wi-Fi"):
        wifi.scan()


# --- a change


def test_joining_a_new_network_keeps_it_once_the_checks_pass(nm, clock):
    store = Store()
    wifi = wifi_of(nm, clock, store)
    said = []
    plan = wifi.prepare("New Net", "correct horse")
    assert (plan.old, plan.old_name, plan.kind, plan.reuse) == ("u-old", "Office", "psk", None)
    before = len(nm.calls)
    words = wifi.change(plan, lambda device, since: ([wifi.gateway_check(device)], "It reached its gateway"), said.append)
    assert words == "Joined New Net. It reached its gateway"
    # The record was written before anything changed
    assert store.saved[0]["state"] == "trying" and store.saved[0]["old"] == "u-old" and store.saved[0]["new"] == NEW_UUID
    assert nm.calls[before][:3] == ["nmcli", "connection", "load"]
    assert [r["state"] for r in store.saved] == ["trying", "trying", "proven", "done"]
    assert wifi.public_record() == {"state": "done", "ssid": "New Net", "words": words, "at": "2027-01-15T08:00:00+00:00"}
    # On the new network, which now connects by itself; the old one kept as a remembered network
    assert nm.ssid_on() == "New Net" and nm.profiles[NEW_UUID]["autoconnect"] is True and "u-old" in nm.profiles
    keyfile = Path(nm.profiles[NEW_UUID]["file"])
    assert keyfile.parent == nm.dir and stat.S_IMODE(os.stat(keyfile).st_mode) == 0o600
    assert nm.profiles[NEW_UUID]["psk"] == "correct horse"
    assert not any("correct horse" in " ".join(call) for call in nm.calls)   # never on a command line
    assert ["ping", "-c", "1", "-W", "2", "-I", "wlan0", "172.16.0.1"] in nm.calls
    assert said == ["Joining New Net", "Joined New Net: checking that the TV Pi can do its job on it"]
    assert "correct horse" not in repr(store.saved)


def test_a_refused_password_goes_back_to_the_network_before(nm, clock):
    wifi = wifi_of(nm, clock)
    with pytest.raises(WifiError) as refused:
        wifi.change(wifi.prepare("New Net", "wrong horse"), gateway_only(wifi))
    assert str(refused.value) == "Could not join New Net: the password was not accepted. The TV Pi is back on Office"
    assert nm.ssid_on() == "Office" and set(nm.profiles) == {"u-old"} and list(nm.dir.iterdir()) == []
    assert wifi.public_record()["state"] == "undone" and wifi.public_record()["words"] == str(refused.value)


def test_a_network_that_does_not_pass_its_checks_in_90_seconds_is_left(nm, clock):
    wifi = wifi_of(nm, clock)
    nm.reachable = lambda ssid: ssid != "New Net"
    started = clock.now
    seen = []
    touch = Check(lambda: seen.append(clock.now) or True, "the touch Pi did not check in")
    with pytest.raises(WifiError) as refused:
        wifi.change(wifi.prepare("New Net", "correct horse"), lambda device, since: ([wifi.gateway_check(device), touch], ""))
    assert str(refused.value) == ("Joined New Net, but the TV Pi could not reach its gateway within 90 seconds. "
                                  "The TV Pi is back on Office")
    assert 90 <= clock.now - started <= 95
    assert len(seen) == 1                                   # a check that passed is not asked again
    assert nm.ssid_on() == "Office" and NEW_UUID not in nm.profiles and list(nm.dir.iterdir()) == []


def test_each_check_must_pass_and_the_first_that_did_not_is_named(nm, clock):
    wifi = wifi_of(nm, clock)
    late = Check(lambda: clock.now > 1_800_000_060, "late")
    never = Check(lambda: False, "the touch Pi did not check in within 90 seconds")
    broken = Check(lambda: 1 / 0, "broken")
    assert wifi.trial([late]) is None
    assert wifi.trial([late, never]) == "the touch Pi did not check in within 90 seconds"
    assert wifi.trial([broken]) == "broken"


def test_what_a_change_refuses_before_it_changes_anything(nm, clock):
    wifi = wifi_of(nm, clock)
    cases = [(("Nowhere", "12345678"), "Nowhere was not found: look again, or join it as a hidden network"),
             (("Corp", ""), "Corp asks for a user name: networks like it are not supported"),
             (("Old Cafe", "12345678"), "Old Cafe uses WEP, which is not safe: networks like it are not supported"),
             (("Guest", "12345678"), "Guest is an open network: it takes no password"),
             (("New Net", ""), "Enter the password for New Net"),
             (("Modern", "a" * 64), "Modern takes its password, not a key of 64 characters"),
             (("Office", ""), "The TV Pi is on Office already"),
             (("", "12345678"), "A network's name is 1 to 32 characters"),
             (("New Net", "short"), "A Wi-Fi password is 8 to 63")]
    for (ssid, password), words in cases:
        with pytest.raises(WifiError, match=words.replace(".", r"\.")):
            wifi.prepare(ssid, password)
    assert not [c for c in nm.calls if c[1:3] in (["connection", "load"], ["connection", "up"], ["--wait", "45"])]
    Store.save(wifi.store, {"state": "trying", "ssid": "X"})
    with pytest.raises(WifiError, match="A Wi-Fi change is under way"):
        wifi.prepare("New Net", "correct horse")
    nm.wifi_device = None
    with pytest.raises(WifiError, match="This TV Pi has no Wi-Fi"):
        wifi_of(nm, clock).prepare("New Net", "correct horse")


def test_an_open_network_a_wpa3_one_and_a_hidden_one_by_its_typed_name(nm, clock):
    wifi = wifi_of(nm, clock)
    wifi.change(wifi.prepare("Guest", ""), gateway_only(wifi))
    assert nm.ssid_on() == "Guest" and nm.profiles[NEW_UUID]["psk"] is None and nm.profiles[NEW_UUID]["mgmt"] is None
    nm2 = FakeNM(nm.dir / "two")
    nm2.dir.mkdir()
    wifi2 = wifi_of(nm2, clock)
    wifi2.change(wifi2.prepare("Modern", "sae password"), gateway_only(wifi2))
    assert nm2.profiles[NEW_UUID]["mgmt"] == "sae"
    nm3 = FakeNM(nm.dir / "three")
    nm3.dir.mkdir()
    nm3.passwords["Hidden One"] = "hidden pass"
    wifi3 = wifi_of(nm3, clock)
    plan = wifi3.prepare("Hidden One", "hidden pass", hidden=True)
    assert plan.kind == "psk"
    wifi3.change(plan, gateway_only(wifi3))
    assert nm3.ssid_on() == "Hidden One" and nm3.profiles[NEW_UUID]["hidden"] is True


def test_a_saved_network_is_joined_without_its_password_and_not_deleted_when_it_fails(nm, clock):
    nm.profiles["u-guest"] = {"name": "Guest", "ssid": "Guest", "psk": None, "autoconnect": True, "file": None, "hidden": False, "mgmt": None}
    wifi = wifi_of(nm, clock)
    plan = wifi.prepare("Guest", "")
    assert plan.reuse == "u-guest"
    nm.reachable = lambda ssid: ssid != "Guest"
    with pytest.raises(WifiError, match="back on Office"):
        wifi.change(plan, gateway_only(wifi))
    assert "u-guest" in nm.profiles and nm.ssid_on() == "Office"
    assert not [c for c in nm.calls if c[1:2] == ["connection"] and c[2] == "load"]


def test_a_new_password_for_the_same_network_replaces_the_old_profile_only_once_proven(nm, clock):
    nm.passwords["Office"] = "new office password"      # the office changed its password; the profile has the old one
    wifi = wifi_of(nm, clock)
    plan = wifi.prepare("Office", "new office password")
    assert plan.superseded == ["u-old"]
    wifi.change(plan, gateway_only(wifi))
    assert set(nm.profiles) == {NEW_UUID} and nm.ssid_on() == "Office"
    # Not proven: the old profile stays and the device goes back to it
    nm2 = FakeNM(nm.dir / "two")
    nm2.dir.mkdir()
    wifi2 = wifi_of(nm2, clock)
    nm2.reachable = lambda ssid: False
    plan = wifi2.prepare("Office", "another password")
    with pytest.raises(WifiError):
        wifi2.change(plan, gateway_only(wifi2))
    assert set(nm2.profiles) == {"u-old"}


def test_a_device_that_was_not_on_wifi_goes_off_wifi_again(nm, clock):
    nm.active = {}
    nm.ethernet = True
    wifi = wifi_of(nm, clock)
    nm.reachable = lambda ssid: False
    with pytest.raises(WifiError) as refused:
        wifi.change(wifi.prepare("New Net", "correct horse"), gateway_only(wifi))
    assert str(refused.value).endswith("The TV Pi is off Wi-Fi again")
    assert nm.active == {} and NEW_UUID not in nm.profiles


def test_going_back_that_fails_says_so(nm, clock):
    wifi = wifi_of(nm, clock)
    nm.reachable = lambda ssid: False
    plan = wifi.prepare("New Net", "correct horse")
    del nm.air["Office"]                                    # the old network went away meanwhile
    with pytest.raises(WifiError) as refused:
        wifi.change(plan, gateway_only(wifi))
    assert str(refused.value).endswith(", and going back to Office failed: the network was not found")


def every_step(nm, clock, nm_setup=lambda nm: None):
    """How many steps (commands and writes of the record) a whole change takes."""
    probe = FakeNM(nm.dir / "probe")
    probe.dir.mkdir()
    nm_setup(probe)
    store = Store()
    wifi = wifi_of(probe, clock, store)
    plan = wifi.prepare("New Net", "correct horse")
    count = {"n": 0}
    probe.cut = store.cut = lambda args: count.update(n=count["n"] + 1)
    try:
        wifi.change(plan, gateway_only(wifi))
    except WifiError:
        pass
    return count["n"]


@pytest.mark.parametrize("works", [True, False])
def test_a_power_cut_at_any_step_leaves_the_device_on_a_network_that_works(tmp_path, clock, works):
    """At every command of a change, and every write of its record, the power goes; the device boots (NetworkManager brings up what connects by itself) and the helper
    or the agent starts (recover): it ends on the new network only when that one was proven, else on the old one, with a record that
    says it is over, and no profile or keyfile left of a network that was not kept."""
    setup = (lambda nm: None) if works else (lambda nm: setattr(nm, "reachable", lambda ssid: ssid != "New Net"))
    total = every_step(FakeNM(tmp_path), clock, setup)
    assert total >= 9
    for cut_at in range(total):
        folder = tmp_path / f"cut-{works}-{cut_at}"
        folder.mkdir()
        nm = FakeNM(folder)
        setup(nm)
        store = Store()
        wifi = wifi_of(nm, clock, store)
        plan = wifi.prepare("New Net", "correct horse")
        count = {"n": 0}

        def cut(args, at=cut_at):
            if count["n"] == at:
                raise PowerCut()
            count["n"] += 1

        nm.cut = store.cut = cut
        with pytest.raises(PowerCut):
            wifi.change(plan, gateway_only(wifi))
        nm.cut = store.cut = None
        nm.reboot()
        after = wifi_of(nm, clock, store)
        after.recover()
        record = after.record()
        if not record:                                      # cut before the record was written: nothing had changed
            assert cut_at == 0 and nm.ssid_on() == "Office" and set(nm.profiles) == {"u-old"}
            continue
        assert record["state"] in ("done", "undone"), (cut_at, record)
        if record["state"] == "done":
            assert works and nm.ssid_on() == "New Net" and nm.profiles[NEW_UUID]["autoconnect"], cut_at
        else:
            assert nm.ssid_on() == "Office", (cut_at, record)
            assert NEW_UUID not in nm.profiles and list(folder.iterdir()) == [], cut_at
        assert after.recover() is None                       # a second start has nothing left to do


def test_forget_deletes_a_saved_network_but_not_the_one_the_device_depends_on(nm, clock):
    nm.profiles["u-g1"] = {"name": "Guest", "ssid": "Guest", "psk": None, "autoconnect": True, "file": None, "hidden": False, "mgmt": None}
    nm.profiles["u-g2"] = {"name": "Guest 1", "ssid": "Guest", "psk": None, "autoconnect": True, "file": None, "hidden": False, "mgmt": None}
    wifi = wifi_of(nm, clock)
    assert wifi.forget("Guest") == "Forgot Guest"
    assert set(nm.profiles) == {"u-old"}
    with pytest.raises(WifiError, match="Guest is not saved on the TV Pi"):
        wifi.forget("Guest")
    with pytest.raises(WifiError, match="The TV Pi is on Office: join another network first, or it would be cut off"):
        wifi.forget("Office")
    nm.ethernet = True
    assert wifi.forget("Office") == "Forgot Office" and nm.profiles == {}
