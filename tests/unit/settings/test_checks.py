"""Settings › Check (spec 2026-10-29, section 3): each check run inside the agent, answered {"ok", "text"} in the words its `croom
--check-…` command prints, with the fakes each command's own tests use; one at a time per check ("Already running"), a minute at most."""

import asyncio
from types import SimpleNamespace

import pytest

from croom.core.config import Config
from croom.meeting.service import MeetingService
from croom.settings.checks import ALREADY_RUNNING, CHECK_TIMEOUT_S, NAMES, CheckBusy, Checks
from croom.settings.schema import SettingError
from tests.unit.calendar.test_check import ACCOUNT, FakeProvider
from tests.unit.calendar.test_check import config_for as calendar_config
from tests.unit.laptop.test_service import checked_room
from tests.unit.meeting.test_signin_session import write_cookies, write_preferences
from tests.unit.meeting.test_zoom_check import FULL, FakeApi
from tests.unit.meeting.test_zoom_check import config_for as zoom_config
from tests.unit.tv.fake_client import FakeTvClient
from tests.unit.tv.helpers import service_for, silent_tv, until


def test_the_checks_and_their_minute():
    assert NAMES == ("calendar", "tv", "laptop", "google", "zoom") and CHECK_TIMEOUT_S == 60.0


# --- each check, as its command says it

async def test_calendar_is_check_calendar_on_the_config_file(tmp_path):
    config = calendar_config(tmp_path)
    config.data_dir = str(tmp_path)
    answer = await Checks(lambda: config, calendar_provider=FakeProvider).run("calendar")
    assert answer["ok"] is True
    assert answer["text"].splitlines()[0] == f"Google Calendar: connected as {ACCOUNT}"
    assert "Bookings: the door sign and the room page can book this calendar." in answer["text"]
    assert not answer["text"].endswith("\n")


async def test_calendar_that_does_not_sign_in_fails_in_its_words(tmp_path):
    config = calendar_config(tmp_path)
    answer = await Checks(lambda: config, calendar_provider=lambda: FakeProvider(authenticates=False)).run("calendar")
    assert answer["ok"] is False and answer["text"].startswith("Could not sign in with ")


async def test_zoom_is_check_zoom_on_the_config_file(tmp_path):
    made = []

    def api(*args):
        made.append(FakeApi(*args))
        return made[-1]
    answer = await Checks(lambda: zoom_config(tmp_path, FULL), zoom_api=api).run("zoom")
    assert answer["ok"] is True and "Zoom room user: room1@crystalpm.com, ZAK obtained (valid 2 hours)" in answer["text"]
    answer = await Checks(lambda: zoom_config(tmp_path / "none", None)).run("zoom")
    assert answer["ok"] is False and answer["text"].startswith("Zoom Meeting SDK not configured: ")


async def test_tv_is_the_running_service_looked_at_with_no_connection_of_its_own(tmp_path):
    service = service_for(tmp_path, key="key-9", pi_input="HDMI_1")
    await service.start()
    try:
        await until(lambda: service.status()["reachable"])
        clients = len(FakeTvClient.instances)

        def no_second_service(config):
            raise AssertionError("the check made a TV service of its own")
        answer = await Checks(lambda: Config(), tv=service, tv_service_factory=no_second_service).run("tv")
        assert answer["ok"] is True and answer["text"].splitlines()[-1] == "Ready: the TV answers."
        assert "Looked at through the room's own connection to the TV" in answer["text"]
        assert len(FakeTvClient.instances) == clients
    finally:
        await service.stop()


async def test_tv_without_a_running_service_is_check_tv_on_the_config_file(tmp_path):
    from croom.core.config import TvConfig
    config = Config(data_dir=str(tmp_path))
    config.tv = TvConfig(enabled=True, host="10.0.0.41", mac="AA:BB:CC:DD:EE:FF")
    service = service_for(tmp_path, silent_tv, key="key-9")
    answer = await Checks(lambda: config, tv_service_factory=lambda given: service).run("tv")
    assert answer["ok"] is False and "Check that the TV is on" in answer["text"]


async def test_laptop_is_the_running_service_looked_at_and_never_pressed(tmp_path):
    room = checked_room(tmp_path)
    answer = await Checks(lambda: Config(), laptop=room.service).run("laptop")
    assert answer["ok"] is True and "The MeetUp (046d:0866) is on this Pi." in answer["text"]
    assert room.switch.moves == [] and room.devices.calls == []


@pytest.mark.parametrize("enabled, words", [
    (False, "Laptop mode is off (the Laptop switch page): the room page has no Laptop button."),
    (True, "Laptop mode is on in the settings, but the room software has not started it yet: restart the room software (Restart now, "
           "on the Laptop switch page)."),
])
async def test_laptop_without_a_running_service_says_why_and_opens_no_pin(enabled, words):
    config = Config()
    config.laptop.enabled = enabled
    answer = await Checks(lambda: config).run("laptop")
    assert answer == {"ok": False, "text": words}


def meeting_on(profile, platforms=("google_meet", "zoom")):
    config = Config()
    config.meeting.google_profile_dir = str(profile) if profile else ""
    config.meeting.platforms = list(platforms)
    return MeetingService(config)


async def test_google_reads_the_profiles_session_and_opens_no_browser(tmp_path):
    write_cookies(tmp_path, ["SID", "NID"])
    write_preferences(tmp_path, [{"email": "conference-a@crystalpm.com"}])
    answer = await Checks(lambda: Config(), meeting=meeting_on(tmp_path)).run("google")
    assert answer == {"ok": True, "text": f"Google profile: {tmp_path}\nSigned in as conference-a@crystalpm.com"}


async def test_google_not_signed_in_or_not_set_up_or_not_a_platform(tmp_path):
    write_cookies(tmp_path, ["NID"])
    answer = await Checks(lambda: Config(), meeting=meeting_on(tmp_path)).run("google")
    assert answer["ok"] is False
    assert answer["text"].splitlines()[-1] == "Not signed in: the profile holds no Google session. On the Meetings page, use Sign in to Google."
    answer = await Checks(lambda: Config(), meeting=meeting_on(None)).run("google")
    assert answer["ok"] is False and answer["text"].startswith("The room has no Google profile folder")
    write_cookies(tmp_path, ["SID"])
    answer = await Checks(lambda: Config(), meeting=meeting_on(tmp_path, platforms=["zoom"])).run("google")
    assert answer["ok"] is True and "Signed in as the room's Google account" in answer["text"]
    assert answer["text"].endswith("so the room joins no Google Meet as this account.")
    answer = await Checks(lambda: Config()).run("google")
    assert answer["ok"] is False and "meetings are not running" in answer["text"]


async def test_google_while_a_sign_in_is_open_on_the_tv(tmp_path):
    meeting = SimpleNamespace(sign_in_state=lambda: {"configured": True, "profile_dir": str(tmp_path), "active": True})
    answer = await Checks(lambda: Config(), meeting=meeting).run("google")
    assert answer["ok"] is False and "A sign-in to Google is open on the TV now" in answer["text"]


# --- one at a time, a minute at most, and what goes wrong said in words

class Gated:
    """A check that says a line and waits to be let go."""

    def __init__(self):
        self.gate, self.started = asyncio.Event(), asyncio.Event()

    async def __call__(self, out):
        print("Looking...", file=out)
        self.started.set()
        await self.gate.wait()
        print("Done.", file=out)
        return 0


async def test_a_check_asked_for_again_while_it_runs_is_already_running_and_the_others_go_on(tmp_path):
    checks = Checks(lambda: Config())
    gated = Gated()
    checks._runs["calendar"] = gated
    first = asyncio.ensure_future(checks.run("calendar"))
    await gated.started.wait()
    assert checks.running("calendar")
    with pytest.raises(CheckBusy) as busy:
        await checks.run("calendar")
    assert str(busy.value) == ALREADY_RUNNING
    assert (await checks.run("laptop"))["ok"] is False                  # another check is not held up
    gated.gate.set()
    assert await first == {"ok": True, "text": "Looking...\nDone."}
    assert not checks.running("calendar")
    gated.gate.clear()
    again = asyncio.ensure_future(checks.run("calendar"))              # and may be run again
    await asyncio.sleep(0)
    gated.gate.set()
    assert (await again)["ok"] is True


async def test_a_check_that_takes_longer_than_its_time_says_what_it_had_found(tmp_path):
    checks = Checks(lambda: Config(), timeout=0.05)
    checks._runs["zoom"] = Gated()
    answer = await checks.run("zoom")
    assert answer == {"ok": False, "text": "Looking...\nThe check did not finish within 0.05 seconds."}
    assert not checks.running("zoom")


async def test_a_config_file_that_cannot_be_read_or_a_check_that_breaks_is_said(tmp_path):
    def unreadable():
        raise SettingError("The room's settings file is not valid YAML: fix it at the TV Pi")
    answer = await Checks(unreadable).run("zoom")
    assert answer == {"ok": False, "text": "The room's settings file is not valid YAML: fix it at the TV Pi"}

    async def broken(out):
        raise RuntimeError("no")
    checks = Checks(lambda: Config())
    checks._runs["tv"] = broken
    assert await checks.run("tv") == {"ok": False, "text": "The check stopped: RuntimeError: no"}


async def test_a_check_there_is_none_of(tmp_path):
    with pytest.raises(KeyError):
        await Checks(lambda: Config()).run("wifi")


def test_the_config_editor_loads_the_file_as_the_commands_do(tmp_path):
    from croom.settings.config_edit import ConfigEditor
    path = tmp_path / "config.yaml"
    editor = ConfigEditor(path, None)
    assert isinstance(editor.config(), Config)                             # no file: the defaults, as load_config gives them
    path.write_text("meeting:\n  zoom_credentials_path: /etc/croom/zoom.json\n")
    assert editor.config().meeting.zoom_credentials_path == "/etc/croom/zoom.json"
    path.write_text("meeting: [1, 2\n")
    with pytest.raises(SettingError):
        editor.config()
    path.write_text("room:\n  no_such_setting: 1\n")
    with pytest.raises(SettingError, match="can't be loaded"):
        editor.config()
