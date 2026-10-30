"""The registry of the devices that check in with the TV Pi (room settings spec 2026-10-29, section 7): pending, then allowed with the
token it joined with (kept hashed), refused, forgotten; one job at a time, handed out again when the answer was lost; last seen; the
registry kept in the data directory. A fake clock; the file is in a temporary folder."""

import json
import os
import stat

import pytest

from croom.settings.checkin import (
    MAX_PENDING,
    PENDING_FORGET_S,
    RESEND_S,
    SAVE_EVERY_S,
    CheckinError,
    Registry,
    check_job,
    hash_token,
)

TOUCH = "t" * 32
SIGN = "s" * 32
TOKEN = "k" * 43
OTHER = "o" * 43
COMMIT = "b" * 40


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Ids:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return f"{self.n:016x}"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def registry(tmp_path, clock):
    return Registry(tmp_path / "devices.json", clock=clock, new_id=Ids())


def body(device=TOUCH, kind="touch", version="a" * 40, job=None, **more):
    return dict({"id": device, "kind": kind, "version": version, "name": "touch-pi", "network": {"address": "10.0.0.9"}, "job": job},
                **more)


def joined(registry, device=TOUCH, token=TOKEN):
    assert registry.checkin(body(device), token, "10.0.0.9") == {"state": "pending", "interval": 5}
    registry.allow(device)
    return registry


def refusal(call):
    with pytest.raises(CheckinError) as caught:
        call()
    return caught.value.status, str(caught.value)


def test_an_unknown_device_waits_to_join_and_gets_no_job(registry):
    assert registry.checkin(body(), TOKEN, "10.0.0.9") == {"state": "pending", "interval": 5}
    waiting = registry.waiting()
    assert [(w["id"], w["kind"], w["label"], w["name"], w["address"], w["state"]) for w in waiting] == [
        (TOUCH, "touch", "touch Pi", "touch-pi", "10.0.0.9", "pending")]
    assert registry.devices() == []
    assert refusal(lambda: registry.queue(TOUCH, "reboot")) == (409, "This touch Pi has not joined yet: allow it first")
    assert registry.checkin(body(), TOKEN, "10.0.0.9")["state"] == "pending"


def test_allowed_it_must_carry_the_token_it_joined_with(registry):
    joined(registry)
    assert registry.checkin(body(), TOKEN, "10.0.0.9") == {"state": "allowed", "job": None, "interval": 5}
    assert refusal(lambda: registry.checkin(body(), OTHER, "10.0.0.66")) == (403, "This device's token is not the one it joined with")
    assert refusal(lambda: registry.checkin(body(), "", "10.0.0.66")) == (401, "Send the device's token")
    assert registry.waiting() == [] and [d["id"] for d in registry.devices()] == [TOUCH]
    # A pending device's token is fixed when it asks: another token cannot take its place in the list
    registry.checkin(body(SIGN, "sign"), TOKEN, "10.0.0.20")
    assert refusal(lambda: registry.checkin(body(SIGN, "sign"), OTHER, "10.0.0.66"))[0] == 403


def test_the_token_is_kept_only_as_a_hash_in_a_file_only_its_owner_reads(registry, tmp_path):
    joined(registry)
    text = (tmp_path / "devices.json").read_text()
    assert TOKEN not in text and hash_token(TOKEN) in text
    assert stat.S_IMODE(os.stat(tmp_path / "devices.json").st_mode) == 0o600
    again = Registry(tmp_path / "devices.json", clock=registry._clock)
    assert again.checkin(body(), TOKEN, "10.0.0.9")["state"] == "allowed"
    assert refusal(lambda: again.checkin(body(), OTHER, "10.0.0.9"))[0] == 403


@pytest.mark.parametrize("bad", [
    {"kind": "touch"}, {"id": "short", "kind": "touch"}, {"id": "../" * 8, "kind": "touch"}, {"id": TOUCH}, {"id": TOUCH, "kind": "tv"},
])
def test_a_check_in_that_is_not_one_is_refused(registry, bad):
    assert refusal(lambda: registry.checkin(bad, TOKEN, "10.0.0.9"))[0] == 400
    assert registry.waiting() == []


def test_refuse_removes_it_and_its_check_ins_are_refused_from_then_on(registry, tmp_path, clock):
    registry.checkin(body(), TOKEN, "10.0.0.9")
    registry.refuse(TOUCH)
    assert registry.waiting() == []
    assert refusal(lambda: registry.checkin(body(), TOKEN, "10.0.0.9")) == (403, "This device was refused")
    assert registry.waiting() == []
    assert refusal(lambda: Registry(tmp_path / "devices.json", clock=clock).checkin(body(), TOKEN, "10.0.0.9"))[0] == 403
    # An allowed one too
    joined(registry, SIGN)
    registry.refuse(SIGN)
    assert registry.devices() == []
    assert refusal(lambda: registry.refuse(SIGN)) == (404, f"There is no device {SIGN}")


def test_forget_removes_it_and_it_may_ask_again(registry):
    joined(registry)
    registry.forget(TOUCH)
    assert registry.devices() == []
    assert registry.checkin(body(), OTHER, "10.0.0.9")["state"] == "pending"


def test_no_more_than_ten_wait_and_those_gone_quiet_make_room(registry, clock):
    for n in range(MAX_PENDING):
        registry.checkin(body(f"{n:032x}"), TOKEN, "10.0.0.9")
    assert refusal(lambda: registry.checkin(body("f" * 32), TOKEN, "10.0.0.9")) == (429, "Too many devices are waiting to join")
    assert len(registry.waiting()) == MAX_PENDING
    # An allowed device is not held up by them
    registry.allow(f"{0:032x}")
    assert registry.checkin(body("f" * 32), TOKEN, "10.0.0.9")["state"] == "pending"
    assert refusal(lambda: registry.checkin(body("e" * 32), TOKEN, "10.0.0.9"))[0] == 429
    clock.now += PENDING_FORGET_S + 1
    registry.checkin(body("f" * 32), TOKEN, "10.0.0.9")     # this one keeps asking
    assert registry.checkin(body("e" * 32), TOKEN, "10.0.0.9")["state"] == "pending"
    assert sorted(w["id"] for w in registry.waiting()) == ["e" * 32, "f" * 32]


def test_a_job_is_handed_out_once_and_one_at_a_time(registry, clock):
    joined(registry)
    job = registry.queue(TOUCH, "update", {"commit": COMMIT})
    assert job["type"] == "update" and job["state"] == "queued" and job["args"] == {"commit": COMMIT}
    assert refusal(lambda: registry.queue(TOUCH, "reboot")) == (409, "The touch Pi is busy: wait for it to finish")
    answer = registry.checkin(body(), TOKEN, "10.0.0.9")
    assert answer["job"] == {"id": job["id"], "type": "update", "args": {"commit": COMMIT}}
    # Not handed out again while the device may not have answered yet
    assert registry.checkin(body(), TOKEN, "10.0.0.9")["job"] is None
    # The device says it is on it, then how it goes, then that it is done
    report = {"id": job["id"], "type": "update", "state": "running", "words": "Fetching main from GitHub"}
    assert registry.checkin(body(job=report), TOKEN, "10.0.0.9")["job"] is None
    device = registry.get(TOUCH)
    assert device["job"]["state"] == "running" and device["job"]["words"] == "Fetching main from GitHub" and device["last"] is None
    clock.now += 60
    assert registry.checkin(body(job=dict(report, words="Restarting on bbbbbbb")), TOKEN, "10.0.0.9")["job"] is None
    assert refusal(lambda: registry.queue(TOUCH, "reboot"))[0] == 409
    registry.checkin(body(job=dict(report, state="done", words="Updated to bbbbbbb")), TOKEN, "10.0.0.9")
    device = registry.get(TOUCH)
    assert device["job"] is None
    assert device["last"]["id"] == job["id"] and device["last"]["state"] == "done" and device["last"]["words"] == "Updated to bbbbbbb"
    assert device["last"]["args"] == {"commit": COMMIT}
    assert registry.queue(TOUCH, "reboot")["type"] == "reboot"


def test_a_job_whose_answer_was_lost_is_handed_out_again(registry, clock):
    joined(registry)
    job = registry.queue(TOUCH, "reboot")
    assert registry.checkin(body(), TOKEN, "10.0.0.9")["job"]["id"] == job["id"]
    clock.now += RESEND_S - 1
    assert registry.checkin(body(), TOKEN, "10.0.0.9")["job"] is None
    clock.now += 1
    assert registry.checkin(body(), TOKEN, "10.0.0.9")["job"]["id"] == job["id"]


def test_a_running_job_the_device_stops_speaking_of_has_ended(registry):
    joined(registry)
    job = registry.queue(TOUCH, "reboot")
    registry.checkin(body(), TOKEN, "10.0.0.9")
    registry.checkin(body(job={"id": job["id"], "type": "reboot", "state": "running", "words": "Rebooting"}), TOKEN, "10.0.0.9")
    registry.checkin(body(job={"id": "9" * 16, "type": "reboot", "state": "done", "words": "Rebooted"}), TOKEN, "10.0.0.9")
    device = registry.get(TOUCH)
    assert device["job"] is None and device["last"]["state"] == "failed"
    assert device["last"]["words"] == "The touch Pi stopped before it finished"


def test_a_result_for_a_job_the_tv_pi_has_forgotten_is_kept(registry, tmp_path, clock):
    joined(registry)
    restarted = Registry(tmp_path / "devices.json", clock=clock)
    restarted.checkin(body(job={"id": "7" * 16, "type": "update", "state": "done", "words": "Updated to bbbbbbb"}), TOKEN, "10.0.0.9")
    last = restarted.get(TOUCH)["last"]
    assert last["state"] == "done" and last["words"] == "Updated to bbbbbbb" and last["type"] == "update"


def test_a_wifi_password_is_never_shown_back(registry):
    joined(registry)
    job = registry.queue(TOUCH, "wifi_connect", {"ssid": "Office", "password": "secret-words"})
    assert job["args"] == {"ssid": "Office", "hidden": False}
    assert registry.get(TOUCH)["job"]["args"] == {"ssid": "Office", "hidden": False}
    handed = registry.checkin(body(), TOKEN, "10.0.0.9")["job"]
    assert handed["args"] == {"ssid": "Office", "password": "secret-words", "hidden": False}
    registry.checkin(body(job={"id": handed["id"], "type": "wifi_connect", "state": "failed", "words": "Not yet"}), TOKEN, "10.0.0.9")
    assert registry.get(TOUCH)["last"]["args"] == {"ssid": "Office", "hidden": False}
    assert "secret-words" not in json.dumps(registry.devices())


@pytest.mark.parametrize("job, args", [
    ("format_disk", {}), ("update", {}), ("update", {"commit": "B" * 40}), ("update", {"commit": "b" * 39}),
    ("wifi_connect", {"ssid": "", "password": "12345678"}), ("wifi_connect", {"ssid": "x" * 33}),
    ("wifi_connect", {"ssid": "Office", "password": "short"}), ("wifi_connect", {"ssid": "Office", "hidden": "yes"}),
    ("wifi_connect", {"ssid": "Office", "password": "x" * 64}), ("wifi_forget", {}), ("reboot", "now"),
])
def test_job_arguments_are_checked(job, args):
    with pytest.raises(CheckinError) as caught:
        check_job(job, args)
    assert caught.value.status == 400


def test_job_arguments_are_cleaned():
    assert check_job("reboot", {"extra": 1}) == {}
    assert check_job("update", {"commit": COMMIT, "url": "x"}) == {"commit": COMMIT}
    assert check_job("wifi_connect", {"ssid": "Office"}) == {"ssid": "Office", "password": "", "hidden": False}
    assert check_job("wifi_connect", {"ssid": "Hidden", "password": "a" * 64, "hidden": True, "x": 1}) == \
        {"ssid": "Hidden", "password": "a" * 64, "hidden": True}


def test_last_seen_and_not_seen_since(registry, clock, tmp_path):
    joined(registry)
    device = registry.get(TOUCH)
    assert device["online"] is True and device["seen_ago"] == 0
    assert device["last_seen"] == "2027-01-15T08:00:00+00:00"
    clock.now += 21
    device = registry.get(TOUCH)
    assert device["online"] is False and device["seen_ago"] == 21
    # Last seen is written now and then, not at each check-in; the rest at once
    first = json.loads((tmp_path / "devices.json").read_text())["devices"][TOUCH]["last_seen"]
    registry.checkin(body(), TOKEN, "10.0.0.9")
    assert json.loads((tmp_path / "devices.json").read_text())["devices"][TOUCH]["last_seen"] == first
    clock.now += SAVE_EVERY_S
    registry.checkin(body(), TOKEN, "10.0.0.9")
    assert json.loads((tmp_path / "devices.json").read_text())["devices"][TOUCH]["last_seen"] == clock.now
    clock.now += 5
    registry.checkin(body(), TOKEN, "10.0.0.9")
    registry.flush()
    assert json.loads((tmp_path / "devices.json").read_text())["devices"][TOUCH]["last_seen"] == clock.now
    assert registry.seen_since("touch", clock.now) and not registry.seen_since("touch", clock.now + 1)
    assert not registry.seen_since("sign", 0)


def test_what_a_device_says_of_itself_is_kept_short(registry):
    joined(registry)
    registry.checkin(body(version="v" * 500, name="n\x00" * 100, network={"x": "y" * 5000}), TOKEN, "10.0.0.9")
    device = registry.get(TOUCH)
    assert len(device["version"]) == 64 and "\x00" not in device["name"] and device["network"] == {}


def test_a_registry_file_that_is_not_one_is_set_aside(tmp_path, clock):
    (tmp_path / "devices.json").write_text("{not json")
    registry = Registry(tmp_path / "devices.json", clock=clock)
    assert registry.devices() == []
    aside = tmp_path / "devices.json.bad-20270115T080000Z"
    assert aside.read_text() == "{not json" and not (tmp_path / "devices.json").exists()   # moved aside, not written over
    joined(registry)
    assert aside.read_text() == "{not json" and TOUCH in json.loads((tmp_path / "devices.json").read_text())["devices"]
    (tmp_path / "devices.json").write_text("[1, 2]")
    clock.now += 1
    assert Registry(tmp_path / "devices.json", clock=clock).devices() == []
    assert (tmp_path / "devices.json.bad-20270115T080001Z").read_text() == "[1, 2]"
    (tmp_path / "devices.json").write_text(json.dumps({"devices": {"../x": {"kind": "touch"}, TOUCH: {"kind": "toaster"}}}))
    assert Registry(tmp_path / "devices.json", clock=clock).waiting() == []


def test_devices_waiting_to_join_never_force_a_write(registry, tmp_path, clock, monkeypatch):
    """Anyone on the LAN can ask to join, as often as they like: what they say is written only with the throttled writes."""
    joined(registry)                                                 # Allow writes at once
    writes = []
    real = os.replace
    monkeypatch.setattr("croom.settings.checkin.os.replace", lambda a, b: (writes.append(b), real(a, b)))
    for n in range(MAX_PENDING):
        clock.now += 1
        registry.checkin(body(f"{n:032x}", name=f"pi-{n}"), TOKEN, "10.0.0.9")
        registry.checkin(body(f"{n:032x}", name=f"renamed-{n}", version="c" * 40), TOKEN, "10.0.0.9")   # changes, too
    assert writes == [] and len(registry.waiting()) == MAX_PENDING
    clock.now += SAVE_EVERY_S
    registry.checkin(body(f"{0:032x}"), TOKEN, "10.0.0.9")
    assert len(writes) == 1
    assert len(json.loads((tmp_path / "devices.json").read_text())["devices"]) == MAX_PENDING + 1


def test_a_wifi_scan_result_is_kept_with_the_device_cleaned(registry):
    joined(registry)
    registry.queue(TOUCH, "wifi_scan")
    handed = registry.checkin(body(), TOKEN, "10.0.0.9")["job"]
    networks = [{"ssid": "Office", "signal": 70, "security": "WPA2", "open": False, "saved": True, "in_use": True, "extra": "x"},
                {"ssid": "x" * 40, "signal": 1}, {"ssid": 5}, "nonsense", {"ssid": "Guest", "signal": 900, "security": "", "open": True}]
    report = {"id": handed["id"], "type": "wifi_scan", "state": "done", "words": "Found 2 networks", "result": {"networks": networks}}
    registry.checkin(body(job=report), TOKEN, "10.0.0.9")
    scan = registry.get(TOUCH)["scan"]
    assert scan["at"] == "2027-01-15T08:00:00+00:00"
    assert scan["networks"] == [{"ssid": "Office", "signal": 70, "security": "WPA2", "open": False, "saved": True, "in_use": True},
                                {"ssid": "Guest", "signal": 0, "security": "", "open": True, "saved": False, "in_use": False}]
    assert registry.get(TOUCH)["last"]["words"] == "Found 2 networks"
    registry.forget(TOUCH)
    joined(registry)
    assert registry.get(TOUCH)["scan"] is None


def test_known_says_whether_a_device_of_a_kind_has_joined(registry):
    assert not registry.known("touch")
    registry.checkin(body(), TOKEN, "10.0.0.9")
    assert not registry.known("touch")                               # waiting is not joined
    registry.allow(TOUCH)
    assert registry.known("touch") and not registry.known("sign")
