"""Settings › Check and Logs' routes (spec 2026-10-29, sections 3 and 5): POST /api/settings/checks/{name} runs one check inside the agent
and answers its words; GET /api/settings/logs reads the room software's log through the root helper's journal job. Both behind the PIN;
Logs needs the helper ("Not available on this device" without it), Check does not. The helper is its real jobs on a temporary folder,
with a fake journalctl."""

import asyncio
import getpass
import grp
import json
import os

import pytest

from croom.admin.jobs import Host, Jobs
from croom.meeting.providers.base import MeetingState
from croom.settings.checks import Checks
from croom.settings.config_edit import ConfigEditor
from croom.settings.pin import PinStore
from tests.unit.calendar.test_check import FakeProvider
from tests.unit.calendar.test_check import config_for as calendar_config
from tests.unit.control.test_service import StubMeeting, client_factory, make_service  # noqa: F401  (client_factory is a fixture)
from tests.unit.control.test_settings_credentials_api import CONFIG, JobsAdmin
from tests.unit.tv.helpers import service_for, until

PIN = "1234"
CHECKS = ("calendar", "tv", "laptop", "google", "zoom")


def journal_line(text, priority=6, n=1):
    return json.dumps({"MESSAGE": text, "PRIORITY": str(priority), "__REALTIME_TIMESTAMP": str(1_800_000_000_000_000 - n),
                       "__CURSOR": f"s=ab;i={n}"})


class Journal:
    """journalctl, as the helper runs it: what it was asked, and `lines` printed."""

    def __init__(self, lines=()):
        self.calls, self.lines, self.code, self.output = [], list(lines), 0, ""

    def __call__(self, args):
        self.calls.append(list(args))
        return (self.code, self.output) if self.code else (0, "\n".join(self.lines))


def helper_with(tmp_path, journal):
    admin = JobsAdmin(tmp_path)
    admin.config.write_text(CONFIG)
    admin.jobs = Jobs(Host(paths=admin.paths, owner=getpass.getuser(), group=grp.getgrgid(os.getgid()).gr_name, run=journal,
                           background=lambda work: None))
    return admin


@pytest.fixture
def pins(tmp_path):
    store = PinStore(tmp_path / "settings-pin.json")
    store.set_pin(PIN)
    return store


@pytest.fixture
def unlocked(pins):
    return {"X-Croom-Unlock": pins.unlock(PIN)}


@pytest.fixture
def journal():
    return Journal([journal_line("2026-10-30 10:42:13,512 - croom.tv.service - WARNING - The TV is not answering", 4, 2),
                    journal_line("Started croom.service.", 6, 1)])


@pytest.fixture
def admin(tmp_path, journal):
    return helper_with(tmp_path, journal)


async def test_check_and_logs_ask_for_the_pin_first(client_factory, admin, pins):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins, admin=admin, config_file=str(admin.config), meeting=StubMeeting()))
    for name in CHECKS:
        response = await client.post(f"/api/settings/checks/{name}", json={})
        assert response.status == 401 and (await response.json())["error"] == "Enter the PIN"
    response = await client.get("/api/settings/logs")
    assert response.status == 401 and admin.calls == []


async def test_a_check_is_json_and_one_there_is(client_factory, admin, pins, unlocked):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins, admin=admin, config_file=str(admin.config), meeting=StubMeeting()))
    response = await client.post("/api/settings/checks/zoom", data="{}", headers=unlocked)
    assert response.status == 415
    response = await client.post("/api/settings/checks/wifi", json={}, headers=unlocked)
    assert response.status == 404 and (await response.json())["error"] == "There is no check wifi"


async def test_the_rooms_own_checks_read_the_config_file_and_need_no_helper(client_factory, tmp_path, admin, pins, unlocked):  # noqa: F811
    admin.gone = True                                                       # a room without the helper: Check works all the same
    admin.config.write_text(CONFIG + "laptop:\n  enabled: false\n")
    meeting = StubMeeting()
    meeting._sign_ins()                                                     # (the stub's sign-in state, made)
    meeting.google = {"signed_in": True, "address": "conference-a@crystalpm.com"}
    client = await client_factory(make_service(pin_store=pins, admin=admin, config_file=str(admin.config), meeting=meeting))
    answer = await (await client.post("/api/settings/checks/zoom", json={}, headers=unlocked)).json()
    assert answer["ok"] is False and answer["text"].startswith("Zoom Meeting SDK not configured: ")
    answer = await (await client.post("/api/settings/checks/laptop", json={}, headers=unlocked)).json()
    assert answer == {"ok": False, "text": "Laptop mode is off (the Laptop switch page): the room page has no Laptop button."}
    answer = await (await client.post("/api/settings/checks/google", json={}, headers=unlocked)).json()
    assert answer == {"ok": True, "text": "Google profile: /var/lib/croom/meet-profile\nSigned in as conference-a@crystalpm.com"}
    assert admin.calls == []


async def test_the_tv_check_looks_at_the_rooms_running_tv(client_factory, tmp_path, admin, pins, unlocked):  # noqa: F811
    tv = service_for(tmp_path, key="key-9", pi_input="HDMI_1")
    await tv.start()
    try:
        await until(lambda: tv.status()["reachable"])
        client = await client_factory(make_service(pin_store=pins, admin=admin, config_file=str(admin.config), meeting=StubMeeting(),
                                                   tv=tv))
        answer = await (await client.post("/api/settings/checks/tv", json={}, headers=unlocked)).json()
        assert answer["ok"] is True and answer["text"].endswith("Ready: the TV answers.")
    finally:
        await tv.stop()


async def test_checks_run_during_a_meeting(client_factory, tmp_path, admin, pins, unlocked):  # noqa: F811
    config = calendar_config(tmp_path)
    config.data_dir = str(tmp_path)
    meeting = StubMeeting()
    meeting.state = MeetingState.CONNECTED                                   # none of them disturbs a meeting
    checks = Checks(lambda: config, calendar_provider=FakeProvider, meeting=meeting)
    client = await client_factory(make_service(pin_store=pins, admin=admin, config_file=str(admin.config), meeting=meeting, checks=checks))
    answer = await (await client.post("/api/settings/checks/calendar", json={}, headers=unlocked)).json()
    assert answer["ok"] is True and answer["text"].startswith("Google Calendar: connected as ")


async def test_a_check_asked_for_again_while_it_runs_is_already_running(client_factory, admin, pins, unlocked):  # noqa: F811
    gate, started = asyncio.Event(), asyncio.Event()

    async def slow(out):
        print("Looking at the calendar...", file=out)
        started.set()
        await gate.wait()
        return 0
    checks = Checks(ConfigEditor(admin.config, None).config)
    checks._runs["calendar"] = slow
    client = await client_factory(make_service(pin_store=pins, admin=admin, config_file=str(admin.config), meeting=StubMeeting(),
                                               checks=checks))
    first = asyncio.ensure_future(client.post("/api/settings/checks/calendar", json={}, headers=unlocked))
    await started.wait()
    second = await client.post("/api/settings/checks/calendar", json={}, headers=unlocked)
    assert second.status == 409 and (await second.json())["error"] == "Already running"
    gate.set()
    assert await (await first).json() == {"ok": True, "text": "Looking at the calendar..."}


async def test_logs_are_the_journal_newest_first_with_a_cursor_for_load_more(client_factory, admin, journal, pins, unlocked):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins, admin=admin, config_file=str(admin.config), meeting=StubMeeting()))
    response = await client.get("/api/settings/logs", headers=unlocked)
    assert response.status == 200
    body = await response.json()
    assert [(e["level"], e["text"]) for e in body["entries"]] == [("warning", "croom.tv.service - The TV is not answering"),
                                                                 ("info", "Started croom.service.")]
    assert body["cursor"] == "s=ab;i=1" and body["more"] is False and body["entries"][0]["time"]
    assert journal.calls[-1] == ["journalctl", "--unit=croom", "--output=json", "--reverse", "--lines=200", "--no-pager", "--quiet"]
    await client.get("/api/settings/logs?level=warning&before=s%3Dab%3Bi%3D1", headers=unlocked)
    assert journal.calls[-1][-2:] == ["--priority=warning", "--after-cursor=s=ab;i=1"]


async def test_logs_refusals(client_factory, admin, journal, pins, unlocked):  # noqa: F811
    client = await client_factory(make_service(pin_store=pins, admin=admin, config_file=str(admin.config), meeting=StubMeeting()))
    response = await client.get("/api/settings/logs?level=debug", headers=unlocked)
    assert response.status == 400 and (await response.json())["error"] == 'The level is "all" or "warning"'
    response = await client.get("/api/settings/logs?before=--since%3Dtoday", headers=unlocked)
    assert response.status == 409 and (await response.json())["error"] == "That is not a place in the log"
    journal.code, journal.output = 1, "Failed to seek to cursor: Invalid argument"
    response = await client.get("/api/settings/logs?before=s%3D1", headers=unlocked)
    assert response.status == 409 and (await response.json())["error"].startswith("The log could not be read: Failed to seek")
    admin.gone = True
    response = await client.get("/api/settings/logs", headers=unlocked)
    assert response.status == 503 and (await response.json())["error"] == "Not available on this device"
