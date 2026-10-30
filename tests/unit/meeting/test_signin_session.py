"""
Sign in to Google on the TV (spec 2026-10-29, sections 3 and 8): the TV's browser closes, Google's sign-in opens on the room's profile,
and whatever ends it (Done, Cancel, 15 minutes without a touch, the browser closing by itself, the agent stopping) gives the TV its
browser back. The display, the browser's process, the clock and the sleep are fakes: nothing here opens a browser.
"""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from croom.core.config import Config
from croom.meeting.meet_signin import SIGN_IN_URL
from croom.meeting.providers.base import MeetingState
from croom.meeting.service import MeetingService
from croom.meeting.signin_session import (
    LEAVE_FIRST, NO_SESSION, NOT_SIGNING_IN, PROFILE_HELD, ROOM_ACCOUNT, SET_UP_FIRST, SIGN_IN_FIRST, SignInError, SignInSession,
    google_account, signed_in_address,
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


async def no_wait(_seconds):
    """The session's sleep: no time passes but the clock's, which the test moves."""
    await asyncio.sleep(0)


class FakeDisplay:
    """The TV's browser: started or not, and what was asked of it, in order (shared with the browser's process, to see which came first)."""

    def __init__(self, profile_dir, events, started=True):
        self.profile_dir = profile_dir
        self.started = started
        self.events = events
        self.fail_start = None

    async def stop(self):
        self.events.append("display stopped")
        self.started = False

    async def start(self):
        self.events.append("display started")
        if self.fail_start is not None:
            raise self.fail_start
        self.started = True

    def pointer_in_use(self):
        pass


class FakeProcess:
    """The sign-in browser. terminate() ends it (after Google's cookies are written, as Chromium writes them as it closes), unless it is
    `stubborn`, which only kill() ends."""

    def __init__(self, events, on_close=None, stubborn=False):
        self.pid = 4242
        self.returncode = None
        self.events = events
        self.on_close = on_close
        self.stubborn = stubborn
        self._ended = asyncio.Event()
        self.terminated = self.killed = 0

    async def wait(self):
        await self._ended.wait()
        return self.returncode

    def _end(self, code):
        if self.returncode is None:
            if self.on_close is not None:
                self.on_close()
            self.returncode = code
            self.events.append("browser closed")
            self._ended.set()

    def terminate(self):
        self.terminated += 1
        if not self.stubborn:
            self._end(0)

    def kill(self):
        self.killed += 1
        self._end(-9)

    def dies(self):
        """It closes by itself (it crashed)."""
        self._end(1)


def write_cookies(profile: Path, names):
    (profile / "Default").mkdir(parents=True, exist_ok=True)
    path = profile / "Default" / "Cookies"
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
    db.executemany("INSERT INTO cookies VALUES (?, ?, '')", [(".google.com", n) for n in names])
    db.commit()
    db.close()


def write_preferences(profile: Path, accounts):
    (profile / "Default").mkdir(parents=True, exist_ok=True)
    (profile / "Default" / "Preferences").write_text(json.dumps({"account_info": accounts, "profile": {"name": "Person 1"}}))


class Rig:
    """A session on fakes, and what happened, in order."""

    def __init__(self, tmp_path, started=True, google_signs_in=None, stubborn=False, holder=None):
        self.profile = tmp_path / "meet-profile"
        self.events = []
        self.clock = Clock()
        self.display = FakeDisplay(self.profile, self.events, started=started)
        self.commands = []
        self.processes = []
        self.google_signs_in = google_signs_in    # what Google writes to the profile while the sign-in browser is open
        self.stubborn = stubborn
        self.session = SignInSession(self.display, self.profile, spawn=self.spawn, find_executable=self.find, clock=self.clock,
                                     sleep=no_wait, holder=holder or (lambda profile: None))

    async def find(self):
        return "/opt/croom/browsers/chromium/chrome"

    async def spawn(self, command):
        self.events.append("browser opened")
        self.commands.append(command)
        process = FakeProcess(self.events, on_close=self.google_signs_in, stubborn=self.stubborn)
        self.processes.append(process)
        return process

    @property
    def process(self):
        return self.processes[-1]


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


async def until(condition, rounds=500):
    for _ in range(rounds):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("never happened")


# --- the beginning


async def test_the_tvs_browser_closes_and_googles_sign_in_opens_plainly_on_the_room_profile_filling_the_tv(tmp_path):
    rig = Rig(tmp_path)
    await rig.session.begin()
    assert rig.events == ["display stopped", "browser opened"]
    [command] = rig.commands
    assert command[0] == "/opt/croom/browsers/chromium/chrome" and command[-1] == SIGN_IN_URL == "https://accounts.google.com/"
    assert f"--user-data-dir={rig.profile}" in command and "--kiosk" in command and "--password-store=basic" in command
    assert not any(arg.startswith("--remote-debugging") or arg == "--enable-automation" for arg in command)   # no automation
    assert rig.session.active and rig.session.state()["idle_left_s"] == 15 * 60
    assert oct(rig.profile.stat().st_mode & 0o777) == "0o700"
    await rig.session.end("cancelled")


async def test_a_second_begin_while_one_is_on_leaves_it_as_it_is(tmp_path):
    rig = Rig(tmp_path)
    await rig.session.begin()
    await rig.session.begin()
    assert rig.events.count("browser opened") == 1
    await rig.session.end("cancelled")


async def test_the_sign_in_waits_for_the_tvs_browser_to_let_go_of_the_profile(tmp_path):
    holds = [1234, 1234, None]
    rig = Rig(tmp_path, holder=lambda profile: holds.pop(0) if holds else None)
    await rig.session.begin()
    assert holds == [] and rig.events == ["display stopped", "browser opened"]
    await rig.session.end("cancelled")


async def test_a_profile_that_stays_held_is_refused_in_words_and_the_tv_gets_its_browser_back(tmp_path):
    rig = Rig(tmp_path, holder=lambda profile: 1234)

    async def sleep(seconds):
        rig.clock.advance(seconds)
        await asyncio.sleep(0)
    rig.session._sleep = sleep
    with pytest.raises(SignInError) as failure:
        await rig.session.begin()
    assert str(failure.value) == PROFILE_HELD == "The TV's browser did not let go of the room's Google profile: try again in a moment"
    assert rig.events == ["display stopped", "display started"] and not rig.session.active


async def test_a_browser_that_cannot_be_started_is_said_and_the_tv_gets_its_browser_back(tmp_path):
    rig = Rig(tmp_path)

    async def spawn(command):
        raise FileNotFoundError(2, "No such file or directory", command[0])
    rig.session._spawn = spawn
    with pytest.raises(SignInError) as failure:
        await rig.session.begin()
    assert str(failure.value).startswith("Google's sign-in could not be opened on the TV: ") and failure.value.status == 500
    assert rig.events == ["display stopped", "display started"] and not rig.session.active


async def test_a_display_that_was_not_running_is_not_started_by_the_sign_in(tmp_path):
    rig = Rig(tmp_path, started=False)
    await rig.session.begin()
    await rig.session.end("cancelled")
    assert "display started" not in rig.events


# --- Done


async def test_done_closes_the_browser_reads_the_address_and_brings_the_tvs_browser_back(tmp_path):
    rig = Rig(tmp_path)
    rig.google_signs_in = lambda: (write_cookies(rig.profile, ["NID", "SID", "__Secure-1PSID"]),
                                   write_preferences(rig.profile, [{"email": "conference-a@crystalpm.com", "gaia": "1"}]))
    await rig.session.begin()
    address = await rig.session.end("done")
    assert address == "conference-a@crystalpm.com"
    assert rig.events == ["display stopped", "browser opened", "browser closed", "display started"]   # closed first, then the TV's back
    assert rig.process.terminated == 1 and rig.process.killed == 0
    assert not rig.session.active
    assert rig.session.last["outcome"] == "done" and rig.session.last["words"] == "Signed in as conference-a@crystalpm.com"
    assert google_account(rig.profile) == {"signed_in": True, "address": "conference-a@crystalpm.com"}   # kept
    assert not (rig.profile / SignInSession.SNAPSHOT).exists()


async def test_done_with_a_session_but_no_address_in_the_profile_is_the_rooms_google_account(tmp_path):
    rig = Rig(tmp_path)
    rig.google_signs_in = lambda: write_cookies(rig.profile, ["SID"])
    await rig.session.begin()
    assert await rig.session.end("done") == ROOM_ACCOUNT == "the room's Google account"
    assert rig.session.last["words"] == "Signed in as the room's Google account"


async def test_done_when_google_kept_no_session_says_so_and_the_tv_still_comes_back(tmp_path):
    rig = Rig(tmp_path)
    rig.google_signs_in = lambda: write_cookies(rig.profile, ["NID", "AEC"])   # the sign-in page's own, before anyone signs in
    await rig.session.begin()
    assert await rig.session.end("done") is None
    assert rig.session.last["words"] == NO_SESSION
    assert rig.events[-1] == "display started"


async def test_a_browser_that_does_not_close_is_killed(tmp_path):
    rig = Rig(tmp_path, stubborn=True)
    rig.session.TERMINATE_WAIT_S = 0.01
    await rig.session.begin()
    await rig.session.end("cancelled")
    assert rig.process.terminated == 1 and rig.process.killed == 1
    assert rig.events[-2:] == ["browser closed", "display started"]


async def test_a_display_that_fails_to_start_again_does_not_fail_the_end(tmp_path):
    rig = Rig(tmp_path)
    rig.display.fail_start = RuntimeError("Chromium did not start")
    await rig.session.begin()
    await rig.session.end("cancelled")
    assert not rig.session.active and rig.events[-1] == "display started"


async def test_ending_when_no_sign_in_is_on_is_refused_in_words(tmp_path):
    rig = Rig(tmp_path)
    with pytest.raises(SignInError) as failure:
        await rig.session.end("done")
    assert str(failure.value) == NOT_SIGNING_IN and failure.value.status == 409


# --- Cancel, and the ends that come by themselves: nothing changes, and the TV comes back


async def test_cancel_puts_the_cookies_back_as_they_were(tmp_path):
    rig = Rig(tmp_path)
    write_cookies(rig.profile, ["SID", "OLD"])
    rig.google_signs_in = lambda: write_cookies(rig.profile, ["NID"])   # signed out, or another account: not kept on Cancel
    await rig.session.begin()
    assert await rig.session.end("cancelled") is None
    db = sqlite3.connect(rig.profile / "Default" / "Cookies")
    assert sorted(name for (name,) in db.execute("SELECT name FROM cookies")) == ["OLD", "SID"]
    db.close()
    assert rig.events[-2:] == ["browser closed", "display started"]
    assert rig.session.last["words"] == "Sign-in cancelled: nothing changed"
    assert not (rig.profile / SignInSession.SNAPSHOT).exists()


async def test_cancel_on_a_profile_that_had_no_cookies_leaves_none(tmp_path):
    rig = Rig(tmp_path)
    rig.google_signs_in = lambda: write_cookies(rig.profile, ["SID"])
    await rig.session.begin()
    await rig.session.end("cancelled")
    assert not (rig.profile / "Default" / "Cookies").exists()
    assert google_account(rig.profile) == {"signed_in": False, "address": None}


async def test_fifteen_minutes_without_a_touch_close_the_sign_in(tmp_path):
    rig = Rig(tmp_path)
    write_cookies(rig.profile, ["SID"])
    rig.google_signs_in = lambda: write_cookies(rig.profile, ["NID"])
    await rig.session.begin()
    rig.clock.advance(14 * 60)
    await settle()
    assert rig.session.active and rig.session.state()["idle_left_s"] == 60
    rig.session.touch()                                    # a pointer or a key event: the 15 minutes start again
    rig.clock.advance(14 * 60)
    await settle()
    assert rig.session.active
    rig.clock.advance(60)
    await until(lambda: not rig.session.active)
    assert rig.events[-2:] == ["browser closed", "display started"]
    assert rig.session.last["outcome"] == "idle"
    assert rig.session.last["words"] == "The sign-in closed after 15 minutes without a touch: nothing changed"
    assert google_account(rig.profile)["signed_in"] is True   # put back as it was


async def test_a_browser_that_closes_by_itself_ends_the_sign_in_and_the_tv_comes_back(tmp_path):
    rig = Rig(tmp_path)
    await rig.session.begin()
    rig.process.dies()
    await until(lambda: not rig.session.active)
    assert rig.events == ["display stopped", "browser opened", "browser closed", "display started"]
    assert rig.session.last["outcome"] == "closed" and rig.session.last["words"] == "The sign-in browser closed by itself: nothing changed"


async def test_the_agent_stopping_closes_the_browser_and_leaves_the_tvs_browser_to_stop_with_it(tmp_path):
    rig = Rig(tmp_path)
    await rig.session.begin()
    await rig.session.close()
    assert rig.events == ["display stopped", "browser opened", "browser closed"]
    assert not rig.session.active and rig.session.last["outcome"] == "stopped"
    await rig.session.close()                              # (nothing to do a second time)


async def test_the_timer_and_the_browsers_end_after_done_do_nothing_more(tmp_path):
    rig = Rig(tmp_path)
    await rig.session.begin()
    await rig.session.end("cancelled")
    rig.clock.advance(60 * 60)
    await settle()
    assert rig.events.count("display started") == 1 and rig.session.last["outcome"] == "cancelled"


# --- what the profile says


def test_the_address_is_the_first_account_in_the_profiles_preferences(tmp_path):
    assert signed_in_address(tmp_path) is None
    write_preferences(tmp_path, [])
    assert signed_in_address(tmp_path) is None
    write_preferences(tmp_path, [{"gaia": "1"}, {"email": "conference-a@crystalpm.com"}, {"email": "b@crystalpm.com"}])
    assert signed_in_address(tmp_path) == "conference-a@crystalpm.com"
    (tmp_path / "Default" / "Preferences").write_text("{not json")
    assert signed_in_address(tmp_path) is None
    (tmp_path / "Default" / "Preferences").write_text(json.dumps({"account_info": "nope"}))
    assert signed_in_address(tmp_path) is None


# --- the meeting service: one sign-in, never in a meeting, and no meeting while it is on


def meeting_service(tmp_path, profile="set", started=True):
    config = Config()
    config.meeting.google_profile_dir = str(tmp_path / "meet-profile") if profile == "set" else ""
    rig = Rig(tmp_path, started=started)
    rig.display.profile_dir = rig.profile if profile == "set" else None
    service = MeetingService(config, display=rig.display, sign_in=rig.session)
    return service, rig


class Provider:
    def __init__(self, state):
        self.state = state
        self.current_meeting = None


@pytest.mark.parametrize("state", [MeetingState.JOINING, MeetingState.IN_LOBBY, MeetingState.CONNECTED, MeetingState.LEAVING])
async def test_a_sign_in_is_refused_in_a_meeting(tmp_path, state):
    service, rig = meeting_service(tmp_path)
    service._active_provider = Provider(state)
    with pytest.raises(SignInError) as failure:
        await service.begin_sign_in()
    assert str(failure.value) == LEAVE_FIRST == "Leave the meeting first"
    assert rig.events == [] and not service.signing_in


async def test_a_sign_in_after_a_meeting_that_failed_is_let_through(tmp_path):
    service, rig = meeting_service(tmp_path)
    service._active_provider = Provider(MeetingState.ERROR)
    state = await service.begin_sign_in()
    assert state["active"] and state["configured"]
    await service.end_sign_in(False)


async def test_a_room_with_no_google_profile_is_told_to_set_up_google_meet_first(tmp_path):
    service, rig = meeting_service(tmp_path, profile=None)
    with pytest.raises(SignInError) as failure:
        await service.begin_sign_in()
    assert str(failure.value) == SET_UP_FIRST and str(failure.value).startswith("Set up Google Meet first")
    assert service.sign_in_state()["configured"] is False


async def test_a_join_is_refused_while_signing_in(tmp_path):
    service, rig = meeting_service(tmp_path)
    await service.begin_sign_in()
    assert service.signing_in
    with pytest.raises(RuntimeError) as failure:
        await service.join_meeting("https://meet.google.com/abc-defg-hij")
    assert str(failure.value) == SIGN_IN_FIRST == "Signing in to Google: finish or cancel it first"
    await service.end_sign_in(False)
    assert not service.signing_in


async def test_a_join_asked_for_while_the_sign_in_is_starting_is_refused(tmp_path):
    service, rig = meeting_service(tmp_path)
    gate = asyncio.Event()

    async def slow_find():
        await gate.wait()
        return "/opt/chrome"
    rig.session._find_executable = slow_find
    begun = asyncio.ensure_future(service.begin_sign_in())
    await settle()
    with pytest.raises(RuntimeError, match="Signing in to Google"):
        await service.join_meeting("https://meet.google.com/abc-defg-hij")
    gate.set()
    await begun
    await service.end_sign_in(False)


async def test_the_pointer_and_the_keys_keep_the_sign_in_open(tmp_path):
    service, rig = meeting_service(tmp_path)
    await service.begin_sign_in()
    rig.clock.advance(10 * 60)
    service.pointer_in_use()
    assert service.sign_in_state()["idle_left_s"] == 15 * 60
    await service.end_sign_in(False)


async def test_done_through_the_service_answers_the_address_and_the_state_says_signed_in(tmp_path):
    service, rig = meeting_service(tmp_path)
    rig.google_signs_in = lambda: (write_cookies(rig.profile, ["SID"]), write_preferences(rig.profile, [{"email": "room@crystalpm.com"}]))
    await service.begin_sign_in()
    assert service.sign_in_state()["active"] is True
    assert await service.end_sign_in(True) == "room@crystalpm.com"
    state = service.sign_in_state()
    assert state["active"] is False and state["signed_in"] is True and state["address"] == "room@crystalpm.com"
    assert state["last"]["words"] == "Signed in as room@crystalpm.com" and state["profile_dir"] == str(rig.profile)


async def test_ending_through_the_service_with_no_sign_in_is_refused(tmp_path):
    service, rig = meeting_service(tmp_path)
    with pytest.raises(SignInError):
        await service.end_sign_in(True)


async def test_the_meeting_service_stopping_ends_the_sign_in(tmp_path):
    service, rig = meeting_service(tmp_path)
    await service.begin_sign_in()
    await service.stop()
    assert rig.events == ["display stopped", "browser opened", "browser closed", "display stopped"]
    assert not service.signing_in


async def test_the_sign_in_wakes_the_tv_as_a_join_does_without_waiting_for_it(tmp_path):
    service, rig = meeting_service(tmp_path)
    woken = []
    gate = asyncio.Event()

    async def wake():
        woken.append("asked")
        await gate.wait()
        return True
    service.add_before_join(wake)
    await service.begin_sign_in()                          # not held up by the TV
    await settle()
    assert woken == ["asked"] and service.state == MeetingState.IDLE   # (the room is not "joining")
    gate.set()
    await service.end_sign_in(False)
