"""
The Meeting SDK provider end to end in the venv's headless Chromium against the
stub SDK (spec 2026-09-25 Zoom, section 4.4): joining, the waiting room, Zoom's
errors, the ZAK path, mute, camera, leave, and joining again.
"""

import asyncio
import json

import pytest

playwright = pytest.importorskip("playwright.async_api")

from croom.core.config import Config  # noqa: E402
from croom.meeting.providers.base import MeetingControlError, MeetingState  # noqa: E402
from croom.meeting.providers.zoom_sdk import PASSCODE_ASKED, ZoomSdkProvider  # noqa: E402
from croom.meeting.zoom_auth import ZoomAuthError, ZoomCredentials  # noqa: E402
from tests.unit.meeting.zoom_stub import STUB_JS  # noqa: E402

LINK = "https://zoom.us/j/99612060433?pwd=abc123"
FULL = ZoomCredentials("sdkClient123", "sdkSecret456", "acct789", "s2sClient", "s2sSecret", "room1@crystalpm.com")
SDK_ONLY = ZoomCredentials("sdkClient123", "sdkSecret456")


class FakeApi:
    def __init__(self, zak="ZAK-1", error=None):
        self.zak, self.error, self.calls = zak, error, []

    async def user_zak(self, user, ttl_seconds=7200):
        self.calls.append(user)
        if self.error:
            raise ZoomAuthError(self.error)
        return self.zak


async def provider_for(credentials=FULL, api=None, stub=STUB_JS):
    from croom.meeting.display import TvDisplay
    display = TvDisplay("about:blank", headless=True)
    await display.start()
    provider = ZoomSdkProvider(credentials, room_name="Room 1", api=api, display=display,
                               extra_init_script=stub, block_sdk_cdn=True)
    await provider.initialize()
    provider._test_display = display
    return provider


async def shutdown(provider):
    """Providers no longer own the browser: stop the display too."""
    await provider.shutdown()
    await provider._test_display.stop()


async def join_calls(provider):
    return [c[1] for c in await provider._page.evaluate("window.__zoomCalls") if c[0] == "join"]


class TestJoin:
    async def test_joins_as_the_room_user_and_connects(self):
        api = FakeApi()
        provider = await provider_for(api=api)
        try:
            states = []
            provider.add_state_callback(states.append)
            info = await provider.join_meeting(LINK, display_name="Room 1")
            assert provider.state == MeetingState.CONNECTED and info.meeting_id == "99612060433"
            assert states == [MeetingState.JOINING, MeetingState.CONNECTED]
            [join] = await join_calls(provider)
            assert join["meetingNumber"] == "99612060433" and join["passWord"] == "abc123"
            assert join["userName"] == "Room 1" and join["zak"] == "ZAK-1"
            assert join["signature"].count(".") == 2
            assert api.calls == ["room1@crystalpm.com"]
        finally:
            await shutdown(provider)

    async def test_without_a_room_user_joins_with_the_signature_only(self):
        provider = await provider_for(credentials=SDK_ONLY)
        try:
            await provider.join_meeting(LINK)
            [join] = await join_calls(provider)
            assert "zak" not in join
        finally:
            await shutdown(provider)

    async def test_zak_failure_reports_and_opens_nothing(self):
        words = "the server-to-server app lacks the user token scope (user:read:token:admin); add it and re-activate the app"
        provider = await provider_for(api=FakeApi(error=words))
        try:
            with pytest.raises(ZoomAuthError):
                await provider.join_meeting(LINK)
            assert provider.state == MeetingState.ERROR
            assert provider.current_meeting.error_message == words
            assert provider._page.url == "about:blank"
        finally:
            await shutdown(provider)

    async def test_join_error_reaches_the_room_page_in_zooms_words(self):
        provider = await provider_for(api=FakeApi())
        try:
            with pytest.raises(RuntimeError) as failure:
                await provider.join_meeting("https://zoom.us/j/999?pwd=x")
            assert "This meeting ID is not valid (code 3712)" in str(failure.value)
            assert provider.state == MeetingState.ERROR
            assert provider.current_meeting.error_message == str(failure.value)
        finally:
            await shutdown(provider)

    async def test_waiting_room_then_connected(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.waiting = true;")
        try:
            states = []
            provider.add_state_callback(states.append)
            await provider.join_meeting(LINK)
            assert states == [MeetingState.JOINING, MeetingState.IN_LOBBY, MeetingState.CONNECTED]
        finally:
            await shutdown(provider)

    async def test_never_connecting_times_out_with_the_pages_words(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.neverConnect = true;")
        provider.CONNECT_TIMEOUT_S = 1
        try:
            with pytest.raises(RuntimeError) as failure:
                await provider.join_meeting(LINK)
            assert "Zoom did not connect; the page says:" in str(failure.value)
            assert provider.state == MeetingState.ERROR
        finally:
            await shutdown(provider)

    async def test_a_second_join_after_a_failed_one_reloads_the_page(self):
        provider = await provider_for(api=FakeApi())
        try:
            with pytest.raises(RuntimeError):
                await provider.join_meeting("https://zoom.us/j/999?pwd=x")
            await provider.join_meeting(LINK)
            assert provider.state == MeetingState.CONNECTED
            [join] = await join_calls(provider)
            assert join["meetingNumber"] == "99612060433"
        finally:
            await shutdown(provider)


class TestControls:
    async def test_mute_camera_and_leave(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(LINK)
            assert await provider.toggle_mute() is True and provider.current_meeting.is_muted is True
            assert await provider.toggle_mute() is False and provider.current_meeting.is_muted is False
            assert await provider.toggle_camera() is False and provider.current_meeting.is_camera_on is False
            assert await provider._page.get_attribute("#stub-video", "aria-label") == "start my video"
            assert await provider.toggle_camera() is True
            await provider.leave_meeting()
            assert provider.state == MeetingState.IDLE and provider.current_meeting is None
            assert provider._page.url == "about:blank"
        finally:
            await shutdown(provider)

    async def test_camera_off_at_join_presses_the_video_button(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(LINK, camera_on=False)
            assert await provider._page.get_attribute("#stub-video", "aria-label") == "start my video"
            assert provider.current_meeting.is_camera_on is False
        finally:
            await shutdown(provider)

    async def test_controls_need_a_meeting(self):
        provider = await provider_for(api=FakeApi())
        try:
            with pytest.raises(RuntimeError):
                await provider.toggle_mute()
            with pytest.raises(RuntimeError):
                await provider.toggle_camera()
        finally:
            await shutdown(provider)


class TestAgain:
    async def test_join_leave_join_again(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(LINK)
            await provider.leave_meeting()
            await provider.join_meeting(LINK)
            assert provider.state == MeetingState.CONNECTED
            [join] = await join_calls(provider)
            assert join["meetingNumber"] == "99612060433"
        finally:
            await shutdown(provider)


class TestUrlsAndConfig:
    def test_delegates_link_handling_to_the_zoom_patterns(self):
        assert ZoomSdkProvider.can_handle_url("https://us02web.zoom.us/j/123?pwd=x") is True
        assert ZoomSdkProvider.extract_meeting_id("https://zoom.us/wc/99612060433/join") == "99612060433"
        assert ZoomSdkProvider.can_handle_url("https://meet.google.com/abc-defg-hij") is False

    def test_from_config_reads_the_credentials_file(self, tmp_path):
        path = tmp_path / "zoom-credentials.json"
        path.write_text(json.dumps({"sdk_client_id": "a", "sdk_client_secret": "b"}))
        config = Config()
        config.meeting.zoom_credentials_path = str(path)
        config.room.name = "Room 2"
        provider = ZoomSdkProvider.from_config(config)
        assert provider.name == "zoom" and provider.display_name == "Zoom"
        assert provider._credentials.sdk_client_id == "a" and provider._room_name == "Room 2"
        assert provider._api is None

    def test_from_config_builds_the_api_for_a_room_user(self, tmp_path):
        path = tmp_path / "zoom-credentials.json"
        path.write_text(json.dumps({"sdk_client_id": "a", "sdk_client_secret": "b", "account_id": "c",
                                    "s2s_client_id": "d", "s2s_client_secret": "e", "room_user": "room2@crystalpm.com"}))
        config = Config()
        config.meeting.zoom_credentials_path = str(path)
        assert ZoomSdkProvider.from_config(config)._api is not None


class TestReviewFixes:
    async def test_host_ending_the_meeting_returns_the_room_to_idle(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.endAfterMs = 300;")
        try:
            await provider.join_meeting(LINK)
            assert provider.state == MeetingState.CONNECTED
            for _ in range(60):
                if provider.state == MeetingState.IDLE:
                    break
                await asyncio.sleep(0.05)
            assert provider.state == MeetingState.IDLE and provider.current_meeting is None
            assert provider._page.url == "about:blank"
        finally:
            await shutdown(provider)

    async def test_leave_during_a_join_cancels_it_quietly(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.neverConnect = true;")
        try:
            states = []
            provider.add_state_callback(states.append)
            task = asyncio.create_task(provider.join_meeting(LINK))
            for _ in range(100):
                if provider.state == MeetingState.JOINING and provider._page.url != "about:blank":
                    break
                await asyncio.sleep(0.05)
            await provider.leave_meeting()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert provider.state == MeetingState.IDLE and provider.current_meeting is None
            assert MeetingState.ERROR not in states
        finally:
            await shutdown(provider)

    async def test_the_wanted_mic_and_camera_are_applied_at_join_in_both_directions(self):
        """A meeting that starts the room muted with video off (the host's settings) gets the room's wanted state:
        the fault seen in the test meeting, where the room joined muted and dark and the table said otherwise."""
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.mutedOnEntry = true; window.ZoomMtg._behaviour.videoOffOnEntry = true;")
        try:
            info = await provider.join_meeting(LINK, camera_on=True, mic_on=True)
            assert info.is_muted is False and info.is_camera_on is True
            assert await provider._page.get_attribute("#stub-video", "aria-label") == "stop my video"
            assert [c["mute"] for c in await provider._page.evaluate("window.__zoomCalls.filter(c => c[0] === 'mute').map(c => c[1])")] == [False]
        finally:
            await shutdown(provider)

    async def test_entry_state_is_read_from_zoom_not_assumed(self):
        """When Zoom will not turn the video on, the status says off: what Zoom has, never what the room wanted."""
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.videoOffOnEntry = true; window.ZoomMtg._behaviour.videoStuckOff = true;")
        try:
            info = await provider.join_meeting(LINK, camera_on=True, mic_on=True)
            assert info.is_camera_on is False
            assert await provider._page.evaluate("window.__videoClicks") == 1
        finally:
            await shutdown(provider)

    async def test_zooms_own_button_labels_work_for_the_camera(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.labelStyle = 'client'; window.ZoomMtg._behaviour.videoOffOnEntry = true;")
        try:
            info = await provider.join_meeting(LINK, camera_on=True)
            assert info.is_camera_on is True
            assert await provider._page.get_attribute("#stub-video", "aria-label") == "stop my video"
            assert await provider.toggle_camera() is False
            assert await provider._page.get_attribute("#stub-video", "aria-label") == "start my video"
        finally:
            await shutdown(provider)

    async def test_the_host_muting_the_room_shows_on_the_status(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(LINK)
            assert provider.current_meeting.is_muted is False
            await provider._page.evaluate("window.ZoomMtg._setMuted(true)")
            for _ in range(40):
                if provider.current_meeting.is_muted:
                    break
                await asyncio.sleep(0.05)
            assert provider.current_meeting.is_muted is True and provider.state == MeetingState.CONNECTED
            assert await provider.toggle_mute() is False                  # the next tap unmutes: the table and Zoom agree
        finally:
            await shutdown(provider)

    async def test_a_camera_press_without_a_toolbar_says_so_and_keeps_the_meeting(self):
        """No button to press: the state is what Zoom's record says, the press fails in words, the meeting goes on."""
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true; window.ZoomMtg._behaviour.videoOffOnEntry = true;")
        try:
            info = await provider.join_meeting(LINK, camera_on=True)
            assert provider.state == MeetingState.CONNECTED and info.is_camera_on is False
            with pytest.raises(MeetingControlError) as failure:
                await provider.toggle_camera()
            assert "camera button" in str(failure.value)
            assert provider.state == MeetingState.CONNECTED and info.is_camera_on is False
        finally:
            await shutdown(provider)

    async def test_diagnostics_name_zooms_fields_and_buttons(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.labelStyle = 'client';")
        try:
            await provider.join_meeting(LINK)
            data = await provider.diagnostics()
            assert "video" in data["user_keys"] and data["state"]["muted"] is False
            assert {"label": "stop my video", "text": "Video"} in data["buttons"]
        finally:
            await shutdown(provider)

    async def test_camera_off_join_does_not_press_when_video_is_already_off(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.videoOffOnEntry = true;")
        try:
            info = await provider.join_meeting(LINK, camera_on=False)
            assert info.is_camera_on is False
            assert await provider._page.get_attribute("#stub-video", "aria-label") == "start my video"
        finally:
            await shutdown(provider)

    async def test_join_success_without_status_events_still_connects(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.silentConnect = true;")
        provider.CONNECT_GRACE_MS = 300
        try:
            await provider.join_meeting(LINK)
            assert provider.state == MeetingState.CONNECTED
        finally:
            await shutdown(provider)

    async def test_meeting_controls_on_screen_count_as_connected_at_the_deadline(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.silentConnect = true;")
        provider.CONNECT_GRACE_MS = 60000
        provider.CONNECT_TIMEOUT_S = 1
        try:
            await provider.join_meeting(LINK)
            assert provider.state == MeetingState.CONNECTED
        finally:
            await shutdown(provider)

    async def test_status_events_with_the_other_field_name_connect_too(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.statusField = 'status';")
        provider.CONNECT_GRACE_MS = 60000
        try:
            await provider.join_meeting(LINK)
            assert provider.state == MeetingState.CONNECTED
        finally:
            await shutdown(provider)


class TestDisplay:
    async def test_leaving_parks_the_page_on_the_screensaver(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(LINK)
            assert not provider._test_display.parked              # the page is claimed for the meeting
            await provider.leave_meeting()
            assert provider._page.url == "about:blank"
            assert provider.state == MeetingState.IDLE
            assert provider._test_display.parked
        finally:
            await shutdown(provider)


WRONG_PASSCODE = ("window.ZoomMtg._behaviour.errorFor = '99612060433';"
                  "window.ZoomMtg._behaviour.errorMessage = 'Wrong password';"
                  "window.ZoomMtg._behaviour.errorCode = 3004;")


class TestPasscode:
    """The passcode the join registers: the one given (typed on the room page, or the booking's), else the link's pwd, else none."""

    async def test_the_passcode_argument_is_used_when_the_link_has_no_pwd(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting("https://zoom.us/j/99612060433", passcode="940104")
            [join] = await join_calls(provider)
            assert join["meetingNumber"] == "99612060433" and join["passWord"] == "940104"
        finally:
            await shutdown(provider)

    async def test_the_passcode_argument_wins_over_the_links_pwd(self):
        """A person who types a code after Zoom refused the link's own meant that code."""
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting("https://zoom.us/j/99612060433?pwd=abc", passcode="940104")
            [join] = await join_calls(provider)
            assert join["passWord"] == "940104"
        finally:
            await shutdown(provider)

    @pytest.mark.parametrize("passcode", [None, ""])
    async def test_the_links_pwd_is_used_when_no_passcode_is_given(self, passcode):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting("https://zoom.us/j/99612060433?pwd=abc", passcode=passcode)
            [join] = await join_calls(provider)
            assert join["passWord"] == "abc"
        finally:
            await shutdown(provider)

    async def test_a_blank_pwd_in_the_link_is_no_passcode_and_the_one_given_is_used(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting("https://zoom.us/j/99612060433?pwd=", passcode="940104")
            [join] = await join_calls(provider)
            assert join["passWord"] == "940104"
        finally:
            await shutdown(provider)

    @pytest.mark.parametrize("link, passcode", [
        ("https://zoom.us/j/99612060433", None),
        ("https://zoom.us/j/99612060433", ""),
        ("https://zoom.us/j/99612060433?pwd=", None),
    ])
    async def test_with_neither_the_passcode_is_empty(self, link, passcode):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(link, passcode=passcode)
            [join] = await join_calls(provider)
            assert join["passWord"] == ""
        finally:
            await shutdown(provider)

    async def test_a_wrong_passcode_says_so_in_the_error(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + WRONG_PASSCODE)
        try:
            with pytest.raises(RuntimeError) as failure:
                await provider.join_meeting("https://zoom.us/j/99612060433", passcode="000000")
            assert str(failure.value) == "Zoom needs the meeting passcode (Wrong password (code 3004))"
            assert str(failure.value).startswith(PASSCODE_ASKED)
            assert "passcode" in provider.current_meeting.error_message
            assert provider.current_meeting.error_message == str(failure.value)
            assert provider.state == MeetingState.ERROR
        finally:
            await shutdown(provider)

    async def test_a_join_that_times_out_on_zooms_passcode_complaint_says_so_too(self):
        """Zoom's join error may never come for a bad passcode: its own modal ("Join parameters: password are invalid!")
        is then all there is when the wait runs out, and the page's words are read the way the error's are."""
        stub = (STUB_JS + "window.ZoomMtg._behaviour.neverConnect = true;"
                "window.ZoomMtg._behaviour.pageSays = 'Join parameters: password are invalid!';")
        provider = await provider_for(api=FakeApi(), stub=stub)
        provider.CONNECT_TIMEOUT_S = 1
        try:
            with pytest.raises(RuntimeError) as failure:
                await provider.join_meeting("https://zoom.us/j/99612060433", passcode="000000")
            words = await provider._page_words()
            assert "Join parameters: password are invalid!" in words
            assert str(failure.value) == f"{PASSCODE_ASKED} ({words})"
            assert provider.current_meeting.error_message == str(failure.value)
            assert provider.state == MeetingState.ERROR
        finally:
            await shutdown(provider)

    async def test_a_passcode_complaint_on_the_page_fails_the_join_while_the_wait_is_still_on(self):
        """Zoom's modal may come with no error event at all. The wait reads the page's words every few seconds, so the person
        is asked for the passcode at once, not when the 90 seconds are up."""
        assert ZoomSdkProvider.PASSCODE_CHECK_S == 5 and ZoomSdkProvider.CONNECT_TIMEOUT_S == 90
        stub = (STUB_JS + "window.ZoomMtg._behaviour.neverConnect = true;"
                "window.ZoomMtg._behaviour.pageSays = 'Join parameters: password are invalid!';")
        provider = await provider_for(api=FakeApi(), stub=stub)
        provider.PASSCODE_CHECK_S = 0.2   # shortened, as the other waits are here; the deadline stays at 90 seconds
        try:
            with pytest.raises(RuntimeError) as failure:
                await asyncio.wait_for(provider.join_meeting("https://zoom.us/j/99612060433", passcode="000000"), timeout=10)
            words = await provider._page_words()
            assert "Join parameters: password are invalid!" in words
            assert str(failure.value) == f"{PASSCODE_ASKED} ({words})"
            assert provider.current_meeting.error_message == str(failure.value)
            assert provider.state == MeetingState.ERROR
        finally:
            await shutdown(provider)

    async def test_a_passcode_refusal_reaches_the_rooms_status_through_the_control_service(self):
        """The whole chain: the room page's join request, the meeting service, this provider against the stub SDK, and
        the status the room page reads, which must start with the words the page looks for."""
        from aiohttp.test_utils import TestClient, TestServer

        from croom.control.service import ControlService
        from croom.meeting.service import MeetingService

        provider = await provider_for(api=FakeApi(), stub=STUB_JS + WRONG_PASSCODE)
        meeting = MeetingService(Config())
        meeting._providers["zoom"] = provider
        client = TestClient(TestServer(ControlService(config={"host": "127.0.0.1", "port": 0, "room_name": "Room 1"},
                                                      meeting=meeting).create_app()))
        await client.start_server()
        try:
            resp = await client.post("/api/meeting/join", json={"url": "https://zoom.us/j/99612060433", "passcode": "000000"})
            assert resp.status == 202
            for _ in range(200):
                status = await (await client.get("/api/status")).json()
                if status["meeting"]["state"] == "error":
                    break
                await asyncio.sleep(0.05)
            assert status["meeting"]["state"] == "error"
            assert status["meeting"]["error"] == f"{PASSCODE_ASKED} (Wrong password (code 3004))"
            [join] = await join_calls(provider)
            assert join["passWord"] == "000000"
        finally:
            await client.close()
            await shutdown(provider)


class WordsPage:
    """A page that says the same words to _page_words and has no toolbar, or one whose video button says Stop Video."""

    def __init__(self, words, toolbar=False):
        self.words, self.toolbar = words, toolbar

    async def evaluate(self, script, *args):
        if "crystalMeet.video" in script:                               # the bridge's reading of the client's video button
            return {"on": True, "label": "Stop Video"} if self.toolbar else {"on": None, "label": None}
        return self.words


class TestPasscodeWordsDuringTheWait:
    """The wait reads the page's words every PASSCODE_CHECK_S for Zoom's own passcode complaint (no browser: a fake page)."""

    def waiting(self, words, connect_timeout_s=30):
        provider = ZoomSdkProvider(SDK_ONLY)
        provider._events = asyncio.Queue()
        provider._page = WordsPage(words)
        provider.CONNECT_TIMEOUT_S = connect_timeout_s
        provider.PASSCODE_CHECK_S = 0.05
        return provider

    async def test_words_that_are_not_a_complaint_wait_for_the_deadline(self):
        provider = self.waiting("joining Waiting for something else", connect_timeout_s=0.5)
        with pytest.raises(RuntimeError) as failure:
            await provider._wait_for_connection()                      # several readings (every 0.05 s), none of them a complaint
        assert str(failure.value) == "Zoom did not connect; the page says: joining Waiting for something else"

    async def test_a_complaint_is_read_while_the_wait_runs_on(self):
        provider = self.waiting("joining Join parameters: password are invalid!")
        with pytest.raises(RuntimeError) as failure:
            await asyncio.wait_for(provider._wait_for_connection(), timeout=10)   # 30 s of wait: the failure is not the deadline's
        assert str(failure.value) == f"{PASSCODE_ASKED} (joining Join parameters: password are invalid!)"

    async def test_the_deadline_still_takes_the_meeting_controls_over_the_words(self):
        """A meeting whose toolbar is up is connected, whatever its page says: the deadline looks for that first, as it always did."""
        provider = self.waiting("Join parameters: password are invalid!", connect_timeout_s=0.3)
        provider._page = WordsPage("Join parameters: password are invalid!", toolbar=True)
        provider.PASSCODE_CHECK_S = 5                                   # as shipped: the deadline comes before the first reading
        await provider._wait_for_connection()

    async def test_the_waiting_room_is_not_read_for_a_complaint(self):
        """Zoom took the passcode to put the room there, and the meeting's title may say "password": the join goes on."""
        provider = self.waiting("Waiting for the host to let the room in. Password review")
        provider._events.put_nowait(("waiting", "Waiting for the host to let the room in"))
        asyncio.get_running_loop().call_later(0.3, provider._events.put_nowait, ("connected", ""))
        await provider._wait_for_connection()                           # about six readings' worth of waiting, none made
        assert provider.state == MeetingState.IN_LOBBY


class TestPasscodeErrors:
    """Which of Zoom's error texts name the passcode (the page's event is queued: no browser needed)."""

    async def refusal(self, detail):
        provider = ZoomSdkProvider(SDK_ONLY)
        provider._events = asyncio.Queue()
        provider._events.put_nowait(("error", detail))
        with pytest.raises(RuntimeError) as failure:
            await provider._wait_for_connection()
        return str(failure.value)

    @pytest.mark.parametrize("detail", [
        "Wrong password (code 3004)",
        "Meeting passcode wrong (code 3000)",
        "Passcode required",
        "The meeting passWord is incorrect",
        "Join failed (code 3004)",                  # Zoom's wrong-passcode code, whatever the words
        '{"errorCode":3004} (code 3004)',           # Zoom gave no words at all
    ])
    async def test_a_passcode_refusal_is_named(self, detail):
        assert await self.refusal(detail) == f"Zoom needs the meeting passcode ({detail})"

    @pytest.mark.parametrize("detail, shown", [
        ("This meeting ID is not valid (code 3712)", "This meeting ID is not valid (code 3712)"),
        ("Zoom's SDK could not start: Invalid signature (code 3712)", "Zoom's SDK could not start: Invalid signature (code 3712)"),
        ("Join failed (code 13004)", "Join failed (code 13004)"),
        ("", "Zoom reported an error"),
    ])
    async def test_other_errors_keep_zooms_words(self, detail, shown):
        assert await self.refusal(detail) == shown
