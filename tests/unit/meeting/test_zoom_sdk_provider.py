"""
The Meeting SDK provider end to end in the venv's headless Chromium against the
stub SDK (spec 2026-09-25 Zoom, section 4.4): joining, the waiting room, Zoom's
errors, the ZAK path, mute, camera, leave, and joining again.
"""

import asyncio
import json
import logging
import time

import pytest

playwright = pytest.importorskip("playwright.async_api")

from croom.core.config import DEFAULT_ACKNOWLEDGE, DEFAULT_REACTIONS, Config  # noqa: E402
from croom.core.logs import DIALOGS_LOGGER  # noqa: E402
from croom.meeting.providers.base import (  # noqa: E402
    DialogPolicy,
    MeetingControlError,
    MeetingInfo,
    MeetingState,
    controls_from,
)
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


async def mute_calls(provider):
    """What the room asked Zoom's mute for, in order (True: mute)."""
    return [c[1]["mute"] for c in await provider._page.evaluate("window.__zoomCalls") if c[0] == "mute"]


def client(**flags):
    """The stub with Zoom's web-client labels ("start my video" / "stop my video", text "Video") and the given flags."""
    return (STUB_JS + "window.ZoomMtg._behaviour.labelStyle = 'client';"
            + "".join(f"window.ZoomMtg._behaviour.{name} = {json.dumps(value)};" for name, value in flags.items()))


# Zoom's record before its attendee list has the room: a few fields, no user, no mute, no video.
NO_RECORD = ("window.ZoomMtg.getCurrentUser = function (opts) { opts.success && opts.success({ result: { currentUser: "
             "{ isOriginhost: false, isAlternativeHost: false, raiseHand: false, isHold: false } } }); };")


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
            await provider._page.evaluate("window.ZoomMtg._later(1000)")         # Zoom drops a mute made again within a second
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


class TestSetToTheStateAsked:
    """Mute and the camera set to the state the table asked for (the whole-branch review's race): the table's button can be a poll behind Zoom, and a
    tap on it must end as the person asked, not flip what Zoom has by the time the tap arrives."""

    @pytest.mark.parametrize("before, wanted", [
        (True, True),       # the host muted the room; the table's Mute is a poll behind: the room stays muted (the toggle sent unmuted it, live)
        (False, False),     # the host unmuted the room; a stale Unmute leaves it live
        (False, True),      # the ordinary Mute
        (True, False),      # the ordinary Unmute
    ])
    async def test_a_mute_asked_for_ends_as_asked_whatever_zoom_had_and_the_provider_last_knew(self, before, wanted):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("(muted) => window.ZoomMtg._setMuted(muted)", before)       # the host changes the room's microphone ...
            assert await eventually(lambda: info.is_muted is before)
            await provider._page.evaluate("window.ZoomMtg._later(1000)")                               # (Zoom takes one mute a second)
            info.is_muted = provider._muted = not before                                              # ... and the provider has not heard of it: its cache is behind
            assert await provider.set_mute(wanted) is wanted
            assert info.is_muted is wanted and provider.get_status()["meeting"]["controls"]["mute"]["on"] is (not wanted)
            assert await provider._page.evaluate("window.ZoomMtg._muted") is wanted
            assert await mute_calls(provider) == [wanted]                    # Zoom was told the state, never to flip: nothing the opposite went out
        finally:
            await shutdown(provider)

    async def test_the_toggle_still_flips_what_zoom_has_now(self):
        """The bodiless route, the IR remote and the gestures keep the toggle: it reads Zoom first and sends the opposite."""
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("window.ZoomMtg._setMuted(true)")
            assert await eventually(lambda: info.is_muted is True)
            info.is_muted = provider._muted = False                                                   # the provider's cache is behind, as above
            assert await provider.toggle_mute() is False and await mute_calls(provider) == [False]    # Zoom had it muted: the toggle unmutes
        finally:
            await shutdown(provider)

    async def test_a_second_identical_mute_inside_the_second_zoom_keeps_is_no_refusal_when_the_room_is_as_asked(self):
        """Zoom drops a mute made again within a second and calls nothing back; the room is muted all the same, and the table asked for muted:
        that is the state, not a refusal. The opposite ask, in the same second, is one: the room is not as asked."""
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            assert await provider.set_mute(True) is True
            assert await asyncio.wait_for(provider.set_mute(True), timeout=5) is True                  # dropped by Zoom, answered at once by the page
            assert len(await provider._page.evaluate("window.__dropped")) == 1 and info.is_muted is True
            with pytest.raises(MeetingControlError) as failure:
                await asyncio.wait_for(provider.set_mute(False), timeout=5)
            assert str(failure.value) == "Zoom ignored mute (it takes one a few seconds after the last); try again in a moment"
            assert info.is_muted is True and provider.state == MeetingState.CONNECTED
        finally:
            await shutdown(provider)

    async def test_a_mute_zoom_refuses_is_refused_in_its_words_and_the_state_stays(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.mutedOnEntry = true;")
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("() => { window.ZoomMtg.mute = (o) => o.error({ errorCode: 3008, errorMessage: 'Host does not allow participants to unmute' }); }")
            with pytest.raises(MeetingControlError) as failure:
                await provider.set_mute(False)
            assert str(failure.value) == "Host does not allow participants to unmute (code 3008)"
            assert info.is_muted is True and provider.state == MeetingState.CONNECTED
        finally:
            await shutdown(provider)

    async def test_a_set_takes_what_zoom_has_afterwards_not_what_was_asked(self):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("() => { window.ZoomMtg.mute = (o) => o.success(); }")        # Zoom answers, and nothing changes
            assert await provider.set_mute(True) is False and info.is_muted is False
        finally:
            await shutdown(provider)

    @pytest.mark.parametrize("before, wanted, clicks", [
        (False, False, 0),     # it went off at the TV and the table still says Turn camera off: already as asked, so nothing is pressed (the toggle turned it on)
        (True, True, 0),       # and the other way about
        (True, False, 1),
        (False, True, 1),
    ])
    async def test_a_camera_asked_for_ends_as_asked_and_a_camera_that_is_so_already_is_not_pressed(self, before, wanted, clicks):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            if not before:
                await provider._page.evaluate("document.getElementById('stub-video').click()")        # someone at the TV turned the camera off
            pressed = await provider._page.evaluate("window.__videoClicks")
            info.is_camera_on = provider._camera_on = not before                                       # the provider has not looked since: its cache is behind
            assert await provider.set_camera(wanted) is wanted
            assert info.is_camera_on is wanted and provider.get_status()["meeting"]["controls"]["camera"]["on"] is wanted
            assert await provider._page.evaluate("window.__videoClicks") - pressed == clicks
            assert await provider._page.get_attribute("#stub-video", "aria-label") == ("stop my video" if wanted else "start my video")
        finally:
            await shutdown(provider)

    async def test_a_camera_set_zoom_cannot_carry_out_says_why_and_presses_nothing(self):
        provider = await provider_for(api=FakeApi(), stub=client(videoUnusable=True))
        try:
            info = await provider.join_meeting(LINK, camera_on=True)
            with pytest.raises(MeetingControlError) as failure:
                await provider.set_camera(True)
            assert str(failure.value) == "Zoom cannot use the camera right now"
            assert info.is_camera_on is False and await provider._page.evaluate("window.__videoClicks") == 0
        finally:
            await shutdown(provider)

    async def test_setting_needs_a_meeting_as_toggling_does(self):
        provider = await provider_for(api=FakeApi())
        try:
            with pytest.raises(RuntimeError):
                await provider.set_mute(True)
            with pytest.raises(RuntimeError):
                await provider.set_camera(False)
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

    async def test_at_join_the_camera_follows_the_room_and_a_host_mute_on_entry_stays(self):
        """A meeting that starts the room muted with video off (the host's settings): the camera comes on, as the room
        wants it, and the room stays muted. Unmuting it hung on whether the host's mute landed before or after the join
        looked; now the room never unmutes itself, and the table offers Unmute, one tap away."""
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.mutedOnEntry = true; window.ZoomMtg._behaviour.videoOffOnEntry = true;")
        try:
            info = await provider.join_meeting(LINK, camera_on=True, mic_on=True)
            assert info.is_muted is True and info.is_camera_on is True
            assert await provider._page.get_attribute("#stub-video", "aria-label") == "stop my video"
            assert await mute_calls(provider) == []                      # the room did not unmute itself
            assert await provider.toggle_mute() is False                 # Unmute, one tap away
        finally:
            await shutdown(provider)

    async def test_at_join_a_room_that_should_start_muted_is_muted(self):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK, mic_on=False)
            assert info.is_muted is True and await provider._page.evaluate("window.ZoomMtg._muted") is True
            assert set(await mute_calls(provider)) == {True}
        finally:
            await shutdown(provider)

    async def test_at_join_the_room_is_asked_to_mute_once_and_zoom_takes_it(self):
        """Zoom drops a mute made again within a second. The join's own look finds the room already muted by the page's mic-off
        default (the first mute there is), so it asks for no second: nothing of the join is the call Zoom drops."""
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK, mic_on=False)
            assert info.is_muted is True and await mute_calls(provider) == [True]
            assert await provider._page.evaluate("window.__dropped") == []
        finally:
            await shutdown(provider)

    async def test_the_camera_wanted_on_at_join_waits_for_zooms_button_to_take_the_press(self):
        """Zoom's video button is disabled while its video encoder starts, two or three seconds after connecting, and a
        press then does nothing: the room joined dark. The join now waits for the button, and the camera comes on."""
        provider = await provider_for(api=FakeApi(), stub=client(videoOffOnEntry=True, videoStartsAfterMs=1000))
        try:
            info = await provider.join_meeting(LINK, camera_on=True)
            assert info.is_camera_on is True
            assert await provider._page.get_attribute("#stub-video", "aria-label") == "stop my video"
            assert await provider._page.evaluate("window.__videoClicks") == 1
        finally:
            await shutdown(provider)

    async def test_without_a_usable_camera_the_status_says_off_and_a_tap_says_why(self):
        """No camera, the camera blocked, or video disabled by the host: Zoom's button says just "Video". The table shows
        the camera off, as Zoom has it, and a tap answers in words without pressing anything."""
        provider = await provider_for(api=FakeApi(), stub=client(videoUnusable=True))
        try:
            info = await provider.join_meeting(LINK, camera_on=True)
            assert provider.state == MeetingState.CONNECTED and info.is_camera_on is False
            with pytest.raises(MeetingControlError) as failure:
                await provider.toggle_camera()
            assert str(failure.value) == "Zoom cannot use the camera right now"
            assert info.is_camera_on is False and await provider._page.evaluate("window.__videoClicks") == 0
        finally:
            await shutdown(provider)

    async def test_a_refused_unmute_reaches_the_table_in_zooms_words(self):
        """Seen in the test meeting: "Could not change the microphone: Page.evaluate: Error: Host does not allow
        participants to unmute (code 3008)". The table gets Zoom's words alone."""
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.mutedOnEntry = true;")
        try:
            await provider.join_meeting(LINK)
            await provider._page.evaluate("() => { window.ZoomMtg.mute = (o) => o.error({ errorCode: 3008, errorMessage: 'Host does not allow participants to unmute' }); }")
            with pytest.raises(MeetingControlError) as failure:
                await provider.toggle_mute()
            assert str(failure.value) == "Host does not allow participants to unmute (code 3008)"
            assert provider.current_meeting.is_muted is True and provider.state == MeetingState.CONNECTED
        finally:
            await shutdown(provider)

    async def test_a_mute_takes_what_zoom_has_afterwards_not_what_was_asked(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(LINK)
            await provider._page.evaluate("() => { window.ZoomMtg.mute = (o) => o.success(); }")   # Zoom answers, and nothing changes
            assert await provider.toggle_mute() is False
            assert provider.current_meeting.is_muted is False
        finally:
            await shutdown(provider)

    async def test_a_second_mute_tap_inside_the_second_zoom_keeps_for_it_is_refused_at_once_in_zooms_words(self):
        """Zoom drops a mute made again within a second and calls nothing back: the page says so at once, where the table waited out
        the whole page call. What the first tap did stands."""
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(LINK)
            assert await provider.toggle_mute() is True
            with pytest.raises(MeetingControlError) as failure:
                await asyncio.wait_for(provider.toggle_mute(), timeout=5)        # not the ten seconds a page call that never answers takes
            assert str(failure.value) == "Zoom ignored mute (it takes one a few seconds after the last); try again in a moment"
            assert provider.current_meeting.is_muted is True and provider.state == MeetingState.CONNECTED
            await provider._page.evaluate("window.ZoomMtg._later(1000)")         # a second later the tap is taken
            assert await provider.toggle_mute() is False and provider.current_meeting.is_muted is False
        finally:
            await shutdown(provider)

    async def test_a_control_the_page_never_answers_is_answered_in_words(self):
        provider = await provider_for(api=FakeApi())
        provider.PAGE_CALL_TIMEOUT_S = 0.5
        try:
            await provider.join_meeting(LINK)
            await provider._page.evaluate("() => { window.ZoomMtg.mute = () => {}; }")   # Zoom never calls back
            with pytest.raises(MeetingControlError) as failure:
                await asyncio.wait_for(provider.toggle_mute(), timeout=5)
            assert str(failure.value) == "Could not change the microphone: the meeting page did not answer within 0.5 seconds"
            assert provider.state == MeetingState.CONNECTED
        finally:
            await shutdown(provider)

    async def test_a_join_is_not_held_by_a_page_call_that_never_answers(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg.mute = () => {};")   # Zoom never answers a mute
        provider.PAGE_CALL_TIMEOUT_S = 0.5
        try:
            info = await asyncio.wait_for(provider.join_meeting(LINK, mic_on=False), timeout=10)
            assert provider.state == MeetingState.CONNECTED and info.is_muted is False   # what Zoom has: the mic is live
        finally:
            await shutdown(provider)

    async def test_a_join_starts_from_its_own_wanted_state_never_the_last_meetings(self):
        """Where Zoom has not said (no button yet, no record yet), the status is what this join wanted, not what the
        last meeting ended with."""
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;" + NO_RECORD)
        provider._muted, provider._camera_on = True, False                   # how the last meeting ended
        try:
            info = await provider.join_meeting(LINK, camera_on=True, mic_on=True)
            assert info.is_muted is False and info.is_camera_on is True
        finally:
            await shutdown(provider)

    async def test_entry_state_is_read_from_zoom_not_assumed(self):
        """When Zoom will not turn the video on, the status says off: what Zoom has, never what the room wanted."""
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.videoOffOnEntry = true; window.ZoomMtg._behaviour.videoStuckOff = true;")
        provider.VIDEO_SWITCH_MS = 300                                       # the wait for a switch that never comes, shortened
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
            await asyncio.sleep(0.3)           # the page's own reads at join are over: none in progress to take the change for theirs
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

    async def test_the_mic_off_default_survives_a_roster_that_comes_late(self):
        """Zoom's roster, and with it the current user's id, can be a moment late: the join still ends muted when the room
        wanted the mic off, and the status says what Zoom has. At 2.5 s the roster comes after the join's first read has
        given up waiting for it (2 s), so the join's own mute finds no id and is refused: only the page's mic-off default,
        sent once a later read finds the id, mutes the room."""
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.rosterAfterMs = 2500;")
        try:
            info = await provider.join_meeting(LINK, mic_on=False)
            await asyncio.sleep(0.5)           # whatever the page reported during the join has reached the status by now
            assert await provider._page.evaluate("window.ZoomMtg._muted") is True
            assert info.is_muted is True
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
            assert {"userId", "userName", "raiseHand", "isHold"} <= set(data["attendee_keys"])        # the attendees' field names, beside the current user's
            assert data["sdk_version"] == provider.SDK_VERSION                                          # the version the provider handed the page
            assert data["dialogs"] == [] and data["dialogs_seen"] == []                                 # no dialog yet in this meeting
        finally:
            await shutdown(provider)

    async def test_diagnostics_list_what_the_reactions_picker_and_the_view_dropdown_hold_and_leave_them_closed(self):
        """The inventory run on the device: the View widget's button beside the footer's, and what each popover holds, by what
        the client says (the stub draws them as Zoom 6.5.0 does)."""
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.labelStyle = 'client';")
        try:
            await provider.join_meeting(LINK)
            data = await provider.diagnostics()
            assert data["buttons"][-1] == {"label": "View", "where": "view widget"}
            reactions, view = data["popovers"]["reactions"], data["popovers"]["view"]
            assert reactions["pressed"] is True and view["pressed"] is True
            assert {"text": "", "label": "Clap", "tooltip": "Clap", "disabled": False}.items() <= reactions["items"][0].items()
            assert [i["label"] for i in view["items"]] == ["Speaker View", "Gallery View", "Hide Self View"]
            assert await provider._page.evaluate("window.ZoomMtg._open") == {"picker": False, "view": False}
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
    """A page that says the same words to _page_words and has no toolbar, or one whose video button has the label given
    ("Stop Video"; just "Video" when Zoom has no usable camera)."""

    def __init__(self, words, toolbar=None):
        self.words, self.toolbar = words, toolbar

    async def evaluate(self, script, *args):
        if "crystalMeet.video" in script:                               # the bridge's reading of the client's video button
            if self.toolbar is None:
                return {"on": None, "usable": False, "label": None, "disabled": False}
            named = self.toolbar.lower() in ("start video", "stop video")
            return {"on": self.toolbar.lower() == "stop video" if named else None, "usable": named, "label": self.toolbar, "disabled": False}
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
        provider._page = WordsPage("Join parameters: password are invalid!", toolbar="Stop Video")
        provider.PASSCODE_CHECK_S = 5                                   # as shipped: the deadline comes before the first reading
        await provider._wait_for_connection()

    async def test_a_video_button_that_says_only_video_is_the_toolbar_too(self):
        """Zoom names the button just "Video" when it has no usable camera: the meeting's controls are on the screen all the same."""
        provider = self.waiting("In the meeting", connect_timeout_s=0.3)
        provider._page = WordsPage("In the meeting", toolbar="Video")
        provider.PASSCODE_CHECK_S = 5
        await provider._wait_for_connection()

    async def test_the_waiting_room_is_not_read_for_a_complaint(self):
        """Zoom took the passcode to put the room there, and the meeting's title may say "password": the join goes on."""
        provider = self.waiting("Waiting for the host to let the room in. Password review")
        provider._events.put_nowait(("waiting", "Waiting for the host to let the room in"))
        asyncio.get_running_loop().call_later(0.3, provider._events.put_nowait, ("connected", ""))
        await provider._wait_for_connection()                           # about six readings' worth of waiting, none made
        assert provider.state == MeetingState.IN_LOBBY


class BridgePage:
    """A page whose bridge answers with Zoom's state as given and records what it was asked to do (no browser)."""

    def __init__(self, muted, video_on, mute_failure=None):
        self.state = {"muted": muted, "videoOn": video_on}
        self.mute_failure = mute_failure   # what the page says when its mute is refused
        self.calls = []

    async def evaluate(self, script, arg=None):
        if "readState" in script:
            return dict(self.state)
        if ".mute(" in script:
            self.calls.append(("mute", arg))
            if self.mute_failure:
                raise RuntimeError(f"Page.evaluate: Error: {self.mute_failure}\n    at window.crystalMeet.mute (http://127.0.0.1/static/meeting.js:50:15)")
            self.state["muted"] = bool(arg)
            return bool(arg)
        if "setVideo" in script:
            self.calls.append(("setVideo", arg[0]))
            self.state["videoOn"] = arg[0]
            return arg[0]
        raise AssertionError(f"unexpected page call: {script}")


class TestTheRoomsDefaultsAtJoin:
    """The controller's ruling: at join the camera follows the room's default both ways, the microphone only towards
    muted. A room the host muted on entry stays muted whenever the host's mute landed, and Unmute is one tap away."""

    async def applied(self, zoom_muted, zoom_video, mic_on, camera_on):
        provider = ZoomSdkProvider(SDK_ONLY)
        provider._page = BridgePage(zoom_muted, zoom_video)
        zoom = await provider._read_state_from_zoom()
        await provider._apply_wanted_state(zoom, camera_on=camera_on, mic_on=mic_on)
        return provider._page.calls

    @pytest.mark.parametrize("zoom_muted, mic_on, calls", [
        (True, True, []),                    # muted by the host on entry, the mic wanted on: it stays muted
        (False, True, []),
        (True, False, []),
        (False, False, [("mute", True)]),    # live, the mic wanted off: muted
        (None, False, [("mute", True)]),     # Zoom has not said: muted, the safe way
        (None, True, []),
    ])
    async def test_the_microphone_is_set_towards_muted_only(self, zoom_muted, mic_on, calls):
        assert await self.applied(zoom_muted, True, mic_on, True) == calls

    @pytest.mark.parametrize("failure, level", [
        ("Zoom ignored mute (it takes one a few seconds after the last); try again in a moment", logging.DEBUG),
        ("Zoom has not listed the room in the meeting yet; try again in a moment", logging.WARNING),
    ])
    async def test_a_second_mute_zoom_ignores_at_join_is_not_a_warning_and_any_other_failure_still_is(self, failure, level, caplog):
        """The page's own mic-off default mutes the room first. When Zoom's record still lags that mute, the join's own look finds the
        room live and asks again inside the second Zoom keeps for a mute, and Zoom drops that one: the room is muted all the same, so
        that is a line for the debug level. The provider still asks (the page's default may not have run), and a mute that failed for
        another reason is still a warning."""
        caplog.set_level(logging.DEBUG, logger="croom.meeting.providers.zoom_sdk")
        provider = ZoomSdkProvider(SDK_ONLY)
        provider._page = BridgePage(False, True, mute_failure=failure)
        zoom = await provider._read_state_from_zoom()
        await provider._apply_wanted_state(zoom, camera_on=True, mic_on=False)
        assert provider._page.calls == [("mute", True)]                       # the provider's own mute is still asked for
        said = [r for r in caplog.records if "mute" in r.getMessage().lower()]
        assert [r.levelno for r in said] == [level], [r.getMessage() for r in caplog.records]
        assert failure in said[0].getMessage()                                # either way the line carries Zoom's words

    @pytest.mark.parametrize("zoom_video, camera_on, calls", [
        (False, True, [("setVideo", True)]),
        (True, False, [("setVideo", False)]),
        (True, True, []),
        (False, False, []),
        (None, True, []),                    # no button and no record: nothing to press
    ])
    async def test_the_camera_is_set_both_ways(self, zoom_video, camera_on, calls):
        assert await self.applied(False, zoom_video, True, camera_on) == calls


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


# ---- The meeting as a model for the table: the controls, and the policy for the dialogs Zoom draws (spec 2026-10-13, sections 3 and 5) ----
# Pure: no browser.


def test_controls_from_a_participant_reading_hides_host_items_and_absent_buttons():
    reading = {"me": {"muted": True}, "role": "participant", "buttons": {"camera": {"on": False}, "captions": {"on": True}},
               "participants": [], "recording": False}
    c = controls_from(reading, ["Thumbs Up"])
    assert c["mute"] == {"available": True, "on": False} and c["camera"] == {"available": True, "on": False}
    assert c["raise_hand"] == {"available": False} and c["captions"] == {"available": True, "on": True}
    assert c["reactions"] == {"available": False, "choices": ["Thumbs Up"]}
    assert c["layout"] == {"available": False, "choices": ["speaker", "gallery"]} and c["hide_self"] == {"available": False}
    assert c["pin"] == {"available": False} and c["leave"] == {"available": True}
    assert c["host"]["role"] == "participant" and c["host"]["record"] == {"available": False, "on": False} and c["host"]["end"]["available"] is False
    assert not [name for name, item in c["host"].items() if name != "role" and item["available"]]


def test_controls_from_a_host_reading_offers_the_host_items_and_the_waiting_list():
    reading = {"me": {"muted": False}, "role": "host", "buttons": {}, "recording": True, "locked": False,
               "participants": [{"id": 5, "name": "Guest", "waiting": True}, {"id": 6, "name": "Ann", "waiting": False}]}
    c = controls_from(reading, [])
    assert c["host"]["record"] == {"available": True, "on": True} and c["host"]["end"]["available"] is True
    assert c["host"]["lock"] == {"available": True, "on": False}
    assert c["host"]["waiting_room"] == {"available": True, "waiting": [{"id": 5, "name": "Guest"}]}
    assert controls_from(dict(reading, role="co-host"), [])["host"]["end"]["available"] is False


def test_a_co_host_has_the_host_items_but_not_the_end_for_everyone():
    c = controls_from({"role": "co-host", "locked": True, "participants": []}, [])
    assert c["host"]["role"] == "co-host" and c["host"]["lock"] == {"available": True, "on": True}
    assert [name for name, item in c["host"].items() if name != "role" and item["available"]] == [
        "mute_all", "ask_unmute", "record", "lock", "waiting_room", "mute_one", "remove"]


def test_controls_from_passes_on_what_the_page_says_of_the_view_and_of_a_camera_it_cannot_use():
    reading = {"me": {"muted": False}, "buttons": {"camera": {"on": None, "usable": False}, "layout": {"value": "gallery"}, "hide_self": {"on": True}}}
    c = controls_from(reading, [])
    assert c["layout"] == {"available": True, "value": "gallery", "choices": ["speaker", "gallery"]}
    assert c["hide_self"] == {"available": True, "on": True}
    assert c["camera"] == {"available": True, "on": None, "usable": False}


def test_controls_from_an_empty_reading_offers_only_what_needs_no_button():
    """The mute control reads the reading's mute only: a reading that does not say is read as live here, and the provider, which knows
    what Zoom last said, puts that in the reading before it asks (test_the_mute_control_agrees_with_the_status_when_zoom_has_not_said)."""
    c = controls_from({}, DEFAULT_REACTIONS)
    assert c["mute"] == {"available": True, "on": True} and c["leave"] == {"available": True}
    assert [name for name in ("camera", "raise_hand", "reactions", "captions", "layout", "hide_self", "pin") if c[name]["available"]] == []
    assert c["host"]["role"] == "participant" and c["host"]["waiting_room"] == {"available": False, "waiting": []}


def test_the_reactions_offered_are_the_configured_names_and_the_openers_presence_is_the_availability():
    names = ["Thumbs Up", "Clap", "Heart"]
    assert controls_from({"buttons": {"reactions": {}}}, names)["reactions"] == {"available": True, "choices": names}
    # When the page says what the picker holds, the configured names it has are offered, in the configured order, however Zoom spells them.
    held = {"buttons": {"reactions": {"choices": ["clap", "Thumbs_Up", "Tada"]}}}
    assert controls_from(held, names)["reactions"] == {"available": True, "choices": ["Thumbs Up", "Clap"]}
    # Nothing to offer is nothing to show: the room's config may list none, or none of them be in the picker.
    assert controls_from({"buttons": {"reactions": {}}}, [])["reactions"] == {"available": False, "choices": []}
    assert controls_from({"buttons": {"reactions": {"choices": ["Tada"]}}}, ["Clap"])["reactions"] == {"available": False, "choices": []}


def test_the_mute_control_says_whether_the_microphone_is_live():
    assert controls_from({"me": {"muted": False}}, [])["mute"] == {"available": True, "on": True}
    assert controls_from({"me": {"muted": True}}, [])["mute"] == {"available": True, "on": False}


def test_the_policy_acknowledges_only_one_button_notices_on_the_list():
    policy = DialogPolicy(["is being transcribed", "AI Companion"])
    assert policy.decide({"title": "", "text": "This meeting is being transcribed and Zoom AI is on.", "buttons": ["OK"]}) == "acknowledge"
    assert policy.decide({"title": "Recording consent", "text": "This meeting is being recorded.", "buttons": ["Leave Meeting", "Got it"]}) == "ask"
    assert policy.decide({"title": "Breakout rooms", "text": "Join Room 1?", "buttons": ["Join"]}) == "ask"
    assert DialogPolicy(["is being transcribed"], ask_everything=True).decide({"text": "This meeting is being transcribed.", "buttons": ["OK"]}) == "ask"
    assert policy.disclosure({"text": "This meeting is being transcribed and Zoom AI is on. Anyone with access may share it.", "buttons": ["OK"]}) == "This meeting is being transcribed and Zoom AI is on."
    assert policy.disclosure({"text": "You have been muted by the host", "buttons": ["OK"]}) == ""


def test_the_consent_dialog_waits_for_a_tap_though_the_default_list_names_it():
    """The same words with a choice to make are not a notice: with two buttons, whatever the list says, the table is asked."""
    policy = DialogPolicy(DEFAULT_ACKNOWLEDGE)
    consent = {"title": "Recording consent", "text": "This meeting is being recorded.", "buttons": ["Leave Meeting", "Got it"]}
    assert policy.decide(consent) == "ask"
    assert policy.decide(dict(consent, buttons=["Got it"])) == "acknowledge"
    assert policy.decide(dict(consent, buttons=[])) == "ask"


def test_the_list_is_matched_in_the_title_or_the_text_whatever_the_case():
    policy = DialogPolicy(["Is Being Recorded", "ai companion"])
    assert policy.decide({"title": "THIS MEETING IS BEING RECORDED", "text": "", "buttons": ["OK"]}) == "acknowledge"
    assert policy.decide({"title": "", "text": "AI Companion is on.", "buttons": ["OK"]}) == "acknowledge"
    assert policy.decide({"text": "Welcome to the meeting.", "buttons": ["OK"]}) == "ask"
    assert policy.decide({"buttons": ["OK"]}) == "ask"


def test_a_blank_phrase_or_a_bare_word_never_makes_every_dialog_a_notice():
    """A list the admin left a blank line in matches nothing for it (a blank phrase is in every text), and a bare word is one phrase, not
    its letters (each of which is in nearly every text)."""
    for acknowledge in (["", "   ", None], "is being recorded", [], None):
        policy = DialogPolicy(acknowledge)
        assert policy.decide({"text": "Join Room 1?", "buttons": ["Join"]}) == "ask", acknowledge
    assert DialogPolicy("is being recorded").decide({"text": "This meeting is being recorded.", "buttons": ["OK"]}) == "acknowledge"


def test_the_policy_presses_the_last_button_which_zoom_draws_as_the_affirmative_one():
    policy = DialogPolicy(DEFAULT_ACKNOWLEDGE)
    assert policy.answer({"buttons": ["OK"]}) == "OK"
    assert policy.answer({"buttons": ["Leave Meeting", "Got it"]}) == "Got it"
    assert policy.answer({"buttons": []}) == ""


def test_the_disclosure_is_the_first_sentence_that_holds_a_phrase_of_the_list_else_the_first_sentence():
    policy = DialogPolicy(DEFAULT_ACKNOWLEDGE)
    assert policy.disclosure({"title": "Recording in progress", "text": "This meeting is being recorded. Your host may share it.", "buttons": ["OK"]}) == "This meeting is being recorded."
    # What the notice is about is not always its first sentence.
    assert policy.disclosure({"title": "Notice", "text": "Your audio and video are off until you agree. This meeting is being recorded.", "buttons": ["OK"]}) == "This meeting is being recorded."
    assert policy.disclosure({"title": "Notice", "text": "Anyone with access may share it. AI Companion is on. Zoom does not use it to train models.", "buttons": ["OK"]}) == "AI Companion is on."
    # An admin's own phrase picks the sentence too, whatever the case of either.
    assert DialogPolicy(["Quality Review"]).disclosure({"text": "Welcome. This call is part of a quality review.", "buttons": ["OK"]}) == "This call is part of a quality review."
    # No sentence of the words holds one (the phrase is in the title): the first sentence.
    assert policy.disclosure({"title": "AI Companion", "text": "Anyone with access to the summary may view or share it. Zoom does not use it to train models.", "buttons": ["OK"]}) == "Anyone with access to the summary may view or share it."
    assert policy.disclosure({"title": "This meeting is being recorded", "text": "", "buttons": ["OK"]}) == "This meeting is being recorded."
    assert policy.disclosure({"title": "", "text": "  ", "buttons": ["OK"]}) == ""
    assert policy.disclosure({}) == ""


@pytest.mark.parametrize("words", [
    "Your microphone has been muted by the host.", "You have been muted by the host", "The host unmuted you: you have been unmuted",
    "Recording has stopped.", "Transcription has stopped. Anyone with access may still view the transcript.",
    "Click OK to continue. Your microphone has been muted by the host.", "Anyone may view it. Recording has stopped.",   # whichever sentence comes first
])
def test_a_notice_that_only_says_a_state_changed_has_no_disclosure_line(words):
    """The room's mute shows on the Mute button, and a recording or a transcript that stopped is read from Zoom again: the line under the
    title is for what the meeting has disclosed, and stays as it was."""
    assert DialogPolicy(DEFAULT_ACKNOWLEDGE).disclosure({"text": words, "buttons": ["OK"]}) == ""


def test_a_stop_is_judged_by_its_own_sentence_and_does_not_take_the_disclosure_beside_it_away():
    """One notice can say that a recording stopped and that the meeting is being recorded again: the first sentence is a state that changed,
    the second is what the meeting discloses. The stop phrases were looked for in the whole title and text, so the stop silenced the line."""
    policy = DialogPolicy(DEFAULT_ACKNOWLEDGE)
    assert policy.disclosure({"text": "The previous recording has stopped. This meeting is being recorded again.", "buttons": ["OK"]}) == "This meeting is being recorded again."
    assert policy.disclosure({"title": "Recording", "text": "Transcription has stopped. AI Companion is on.", "buttons": ["OK"]}) == "AI Companion is on."
    assert policy.disclosure({"text": "You have been muted by the host. This meeting is being recorded.", "buttons": ["OK"]}) == "This meeting is being recorded."
    # The stop is still no line of its own, and a notice whose only sentence naming the list is the stop has none either.
    assert policy.disclosure({"text": "This meeting is being recorded again. The previous recording has stopped.", "buttons": ["OK"]}) == "This meeting is being recorded again."
    assert policy.disclosure({"text": "The recording has stopped. Anyone with access may still view it.", "buttons": ["OK"]}) == ""


# ---- The provider's model, its controls and its dialogs, against the stub page (spec 2026-10-13, sections 3 to 5) ----


async def eventually(check, seconds=3.0):
    """The provider follows the page by events: wait, briefly, for `check` to come true, and say whether it did."""
    end = time.monotonic() + seconds
    while True:
        if check():
            return True
        if time.monotonic() >= end:
            return False
        await asyncio.sleep(0.05)


async def dialog_presses(provider, at_least=0, seconds=4.0):
    """What the dialogs' buttons were pressed for, as the stub logs a press its button took: [["dialog", title, label], ...], once there
    are `at_least` of them (the provider answers a notice a moment after it hears of it)."""
    end = time.monotonic() + seconds
    while True:
        found = [c for c in await provider._page.evaluate("window.__zoomCalls") if c[0] == "dialog"]
        if len(found) >= at_least or time.monotonic() >= end:
            return found
        await asyncio.sleep(0.05)


async def settle_background(provider):
    """Wait for what the provider has started on its own (answering a dialog, reading the meeting again) to be done."""
    while provider._background:
        await asyncio.gather(*list(provider._background), return_exceptions=True)


# The meeting as the page reads it (meeting.js readMeeting), for the tests that have no browser behind the provider.
READING = {"me": {"muted": False, "videoOn": True}, "role": "participant", "participants": [], "count": 0, "recording": False, "locked": False,
           "buttons": {"layout": {"value": "gallery"}, "hide_self": {"on": True}}}


class FakePage:
    """A page with no browser behind it: it answers the provider's reads and controls with `reading`, presses dialogs' buttons as it is told
    to (or refuses to), and logs what it was asked and when."""

    def __init__(self, reading=None, press_failure=None, control_reading=None):
        self.reading = dict(READING) if reading is None else reading
        self.control_reading = self.reading if control_reading is None else control_reading     # what a control comes back with
        self.press_failure = press_failure
        self.asked, self.pressed = [], []

    async def evaluate(self, script, arg=None):
        if "answerDialog" in script:
            self.pressed.append((time.monotonic(), arg[0], arg[1]))
            if self.press_failure:
                raise RuntimeError(f"Page.evaluate: Error: {self.press_failure}\n    at answerNow (http://127.0.0.1/static/meeting.js:1:1)")
            return True
        if "crystalMeet.control" in script:
            self.asked.append(arg)
            return self.control_reading
        if "readMeeting" in script:
            return self.reading
        raise AssertionError(f"unexpected page call: {script}")


class SilentPage(FakePage):
    """A page that reads as FakePage does but never answers a dialog press or a control: what is pinned with it is the bound the provider
    puts on the call."""

    async def evaluate(self, script, arg=None):
        if "answerDialog" in script or "crystalMeet.control" in script:
            await asyncio.sleep(3600)
        return await super().evaluate(script, arg)


def in_a_meeting(page):
    """A provider that is in a meeting with nothing behind it but the page it is given: its model, its events and its policy, no browser."""
    provider = ZoomSdkProvider(SDK_ONLY)
    provider._page = page
    provider._events = asyncio.Queue()
    provider._current_meeting = MeetingInfo(platform="zoom", meeting_id="1", meeting_url=LINK)
    provider._state = MeetingState.CONNECTED
    return provider


class NoDisplay:
    async def show_idle(self):
        pass


def queued(events):
    """What is waiting in the provider's queue of the page's events, in order."""
    found = []
    while not events.empty():
        found.append(events.get_nowait())
    return found


class TestControlsAndDialogs:
    async def test_the_status_model_fills_after_join_and_follows_events(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.role = 'host';")
        try:
            info = await provider.join_meeting(LINK)
            assert info.controls["host"]["role"] == "host" and info.controls["captions"] == {"available": True, "on": False}
            assert info.people["count"] == 3 and info.people["list"][2]["name"] == "Raj Patel"
            assert info.recording is False and info.locked is False
            await provider._page.evaluate("window.ZoomMtg._setAttendee(16777218, {bRaiseHand: false})")
            assert await eventually(lambda: info.people["list"][2]["hand"] is False)
            await provider._page.evaluate("window.ZoomMtg._setRecording(true)")
            assert await eventually(lambda: info.recording and info.controls["host"]["record"] == {"available": True, "on": True})
        finally:
            await shutdown(provider)

    async def test_events_before_connected_are_ignored_and_the_model_fills_after(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.delayMs = 400;")
        try:
            info = await provider.join_meeting(LINK)                       # dialog scans and syncs may fire during the wait
            assert provider.state == MeetingState.CONNECTED and info.controls["mute"]["available"] is True
        finally:
            await shutdown(provider)

    async def test_the_status_carries_the_model_under_its_names(self):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            sent = provider.get_status()["meeting"]
            assert sent["controls"]["raise_hand"] == {"available": True, "on": False} and sent["people"]["count"] == 3
            assert sent["chat"] == {"unread": 0, "messages": []} and sent["notice"] == "" and sent["dialog"] is None
            assert sent["recording"] is False and sent["locked"] is False and sent["is_muted"] is False
            json.dumps(sent)                                                                  # it goes out as JSON
            assert sent["controls"] is info.controls
        finally:
            await shutdown(provider)

    async def test_control_presses_and_reports_and_a_missing_button_says_so(self):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            state = await provider.control("raise_hand")
            assert state["raise_hand"]["on"] is True and info.controls["raise_hand"]["on"] is True
            state = await provider.control("layout", "gallery")
            assert state["layout"]["value"] == "gallery"
            await provider.control("reactions", "Clap")
            assert ["reaction", "clap"] in await provider._page.evaluate("window.__zoomCalls")
            with pytest.raises(MeetingControlError) as failure:
                await provider.control("reactions", "Unicorn")
            assert str(failure.value) == "Zoom offers no Unicorn here" and provider.state == MeetingState.CONNECTED
            state = await provider.control("captions")
            assert state["captions"]["on"] is True and info.controls["captions"]["on"] is True
            state = await provider.control("hide_self")
            assert state["hide_self"]["on"] is True
        finally:
            await shutdown(provider)

    async def test_a_control_whose_button_is_not_on_the_screen_says_so_in_the_pages_words(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;")
        try:
            info = await provider.join_meeting(LINK)
            assert info.controls["captions"] == {"available": False} and info.controls["reactions"]["available"] is False
            with pytest.raises(MeetingControlError) as failure:
                await provider.control("captions")
            assert str(failure.value) == "Zoom's captions button is not on the screen yet; try again in a moment"
            assert provider.state == MeetingState.CONNECTED
        finally:
            await shutdown(provider)

    async def test_a_host_call_refused_by_zoom_answers_in_words(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(LINK)
            with pytest.raises(MeetingControlError) as failure:
                await provider.control("host.mute_all")
            assert str(failure.value) == "Current user is neither host nor cohost (code 1)" and provider.state == MeetingState.CONNECTED
            with pytest.raises(MeetingControlError) as failure:
                await provider.control("no_such_control")
            assert str(failure.value) == "Unknown control no_such_control"
        finally:
            await shutdown(provider)

    async def test_a_host_carries_out_the_host_controls_and_the_model_follows(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.role = 'host';")
        try:
            info = await provider.join_meeting(LINK)
            assert info.controls["host"]["lock"] == {"available": True, "on": False}
            state = await provider.control("host.lock")
            assert state["host"]["lock"]["on"] is True and info.locked is True
            await provider.control("host.record")
            assert info.recording is True and info.controls["host"]["record"]["on"] is True
            await provider.control("host.mute_one", "16777217")                  # Ann Lee, by the id Zoom lists her by
            assert info.people["list"][1]["name"] == "Ann Lee" and info.people["list"][1]["muted"] is True
        finally:
            await shutdown(provider)

    async def test_a_control_takes_longer_than_a_read_may_and_says_so_when_the_page_never_answers(self):
        assert ZoomSdkProvider.CONTROL_TIMEOUT_S >= 30 > ZoomSdkProvider.PAGE_CALL_TIMEOUT_S      # Zoom's calls take 12 s each, one at a time
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.role = 'host';")
        provider.CONTROL_TIMEOUT_S = 0.5
        try:
            await provider.join_meeting(LINK)
            await provider._page.evaluate("() => { window.ZoomMtg.muteAll = () => {}; }")        # Zoom never calls back
            with pytest.raises(MeetingControlError) as failure:
                await asyncio.wait_for(provider.control("host.mute_all"), timeout=5)
            assert str(failure.value) == "Could not carry out host mute all: the meeting page did not answer within 0.5 seconds"
            assert provider.state == MeetingState.CONNECTED
        finally:
            await shutdown(provider)

    async def test_a_page_that_is_gone_names_the_control_in_words_not_by_its_key(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(LINK)

            async def gone(*_args, **_kwargs):
                raise RuntimeError("Page.evaluate: Target page, context or browser has been closed\n    at the stack")
            real = provider._page.evaluate
            provider._page.evaluate = gone
            try:
                for name, said in (("raise_hand", "raise hand"), ("host.mute_all", "host mute all"), ("...", "the control")):
                    with pytest.raises(MeetingControlError) as failure:
                        await provider.control(name)
                    assert str(failure.value) == f"Could not carry out {said}: Target page, context or browser has been closed", name
            finally:
                provider._page.evaluate = real
        finally:
            await shutdown(provider)

    async def test_mute_and_camera_keep_their_own_paths_and_the_model_follows(self):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            state = await provider.control("mute")
            assert info.is_muted is True and state["mute"] == {"available": True, "on": False} and await mute_calls(provider) == [True]
            state = await provider.control("camera")
            assert info.is_camera_on is False and state["camera"] == {"available": True, "on": False}
            assert await provider._page.evaluate("window.__videoClicks") == 1
            assert info.controls["camera"]["on"] is False
        finally:
            await shutdown(provider)

    async def test_the_mute_and_camera_controls_are_the_toggles_the_join_and_the_reconnect_rules_are_made_for(self):
        provider = in_a_meeting(object())               # a page that is never asked: the toggles below stand for the page's calls
        asked = []

        async def toggle_mute():
            asked.append("mute")
            return True

        async def toggle_camera():
            asked.append("camera")
            return True

        async def read():
            asked.append("read")
            return {}

        provider.toggle_mute, provider.toggle_camera, provider._read_state_from_zoom = toggle_mute, toggle_camera, read
        await provider.control("mute")
        await provider.control("camera")
        assert asked == ["mute", "camera", "read"]       # the camera's switch is read back (the mute's toggle reads for itself)

    async def test_leaving_is_a_control_too(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(LINK)
            assert await provider.control("leave") == {}
            assert provider.state == MeetingState.IDLE and provider.current_meeting is None
        finally:
            await shutdown(provider)

    async def test_controls_need_a_meeting(self):
        provider = await provider_for(api=FakeApi())
        try:
            with pytest.raises(RuntimeError, match="Not in a meeting"):
                await provider.control("raise_hand")
        finally:
            await shutdown(provider)

    async def test_an_acknowledged_dialog_is_pressed_once_and_disclosed(self):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("window.ZoomMtg._showDialog('', 'This meeting is being transcribed and Zoom AI is on. Anyone with access to the transcript may view or share it with others.', ['OK'])")
            assert await eventually(lambda: info.notice)
            assert info.notice == "This meeting is being transcribed and Zoom AI is on."
            assert await dialog_presses(provider, at_least=1) == [["dialog", "", "OK"]]
            assert info.dialog is None
            await provider._page.evaluate("window.ZoomMtg._showDialog('', 'This meeting is being recorded.', ['OK'])")
            assert await eventually(lambda: "recorded" in info.notice)
            assert info.notice == "This meeting is being transcribed and Zoom AI is on. This meeting is being recorded."
            assert [c[1:] for c in await dialog_presses(provider, at_least=2)] == [["", "OK"], ["", "OK"]]      # a second after the first: one answer a second
            await provider._page.evaluate("window.ZoomMtg._showDialog('', 'This meeting is being recorded.', ['OK'])")      # the same words again: pressed, said once
            assert len(await dialog_presses(provider, at_least=3)) == 3 and info.notice.count("This meeting is being recorded.") == 1
            await asyncio.sleep(0.3)
            assert len(await dialog_presses(provider)) == 3 and info.dialog is None
        finally:
            await shutdown(provider)

    async def test_a_dialog_with_a_choice_waits_on_the_table_and_a_tap_answers_it(self):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("window.ZoomMtg._showDialog('Recording consent', 'This meeting is being recorded.', ['Leave Meeting', 'Got it'])")
            assert await eventually(lambda: info.dialog)
            assert info.dialog["buttons"] == ["Leave Meeting", "Got it"] and info.dialog["title"] == "Recording consent"      # in the client's order: the affirmative last
            assert info.dialog["text"] == "This meeting is being recorded." and info.notice == ""
            await asyncio.sleep(0.3)
            assert await dialog_presses(provider) == []                                  # nothing is pressed until a person taps
            assert await provider.control("dialog", {"id": info.dialog["id"], "button": "Got it"}) == {"dialog": None}
            assert info.dialog is None
            assert ["dialog", "Recording consent", "Got it"] in await provider._page.evaluate("window.__zoomCalls")
            assert await eventually(lambda: not provider._dialogs and not provider._on_screen)      # the page's own word that it closed changes nothing more
            assert info.dialog is None
        finally:
            await shutdown(provider)

    async def test_a_second_dialog_waits_behind_the_first_and_shows_when_the_first_is_answered(self):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("window.ZoomMtg._showDialog('Unmute?', 'The host would like you to unmute.', ['Stay muted', 'Unmute'])")
            assert await eventually(lambda: info.dialog)
            first = dict(info.dialog)
            await provider._page.evaluate("window.ZoomMtg._showDialog('Breakout rooms', 'Join Room 1?', ['Not now', 'Join'])")
            assert await eventually(lambda: len(provider._dialogs) == 2)
            assert info.dialog == first                                                  # the oldest is the one on show: the second does not hide it
            assert await provider.control("dialog", {"id": first["id"], "button": "Stay muted"}) == {"dialog": info.dialog}
            assert info.dialog["title"] == "Breakout rooms" and info.dialog["id"] != first["id"]
            await provider.control("dialog", {"id": info.dialog["id"], "button": "Not now"})
            assert info.dialog is None
        finally:
            await shutdown(provider)

    async def test_a_dialog_that_goes_away_clears_and_the_state_is_read_again(self):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            reads = []
            read = provider._read_state_from_zoom

            async def counting():
                reads.append(1)
                return await read()

            provider._read_state_from_zoom = counting
            sid = await provider._page.evaluate("window.ZoomMtg._showDialog('Unmute?', 'The host would like you to unmute.', ['Stay muted', 'Unmute'])")
            assert await eventually(lambda: info.dialog)
            reads.clear()
            await provider._page.evaluate(f"window.ZoomMtg._closeDialog('{sid}')")
            assert await eventually(lambda: info.dialog is None)
            assert await eventually(lambda: reads)                                      # the host may have muted the room with that dialog
        finally:
            await shutdown(provider)

    async def test_ask_everything_makes_a_notice_wait(self):
        provider = await provider_for(api=FakeApi())
        provider.dialog_policy = DialogPolicy(DEFAULT_ACKNOWLEDGE, ask_everything=True)
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("window.ZoomMtg._showDialog('', 'This meeting is being transcribed.', ['OK'])")
            assert await eventually(lambda: info.dialog)
            assert info.dialog["buttons"] == ["OK"] and info.notice == ""
            await asyncio.sleep(0.3)
            assert await dialog_presses(provider) == []
        finally:
            await shutdown(provider)

    async def test_a_tap_zoom_does_not_take_says_why_and_the_dialog_stays_for_another_try(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.dialogStays = true;")
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("window.ZoomMtg._showDialog('Unmute?', 'The host would like you to unmute.', ['Stay muted', 'Unmute'])")
            assert await eventually(lambda: info.dialog)
            with pytest.raises(MeetingControlError) as failure:
                await provider.control("dialog", {"id": info.dialog["id"], "button": "Unmute"})
            assert str(failure.value) == "Zoom's dialog did not close when Unmute was pressed; try again in a moment"
            assert info.dialog is not None and info.dialog["title"] == "Unmute?" and provider.state == MeetingState.CONNECTED
        finally:
            await shutdown(provider)

    async def test_a_notice_zoom_takes_the_press_of_and_keeps_waits_on_the_table_with_its_words_on_the_line(self, caplog):
        caplog.set_level(logging.INFO, logger="croom.meeting.providers.zoom_sdk")
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.dialogStays = true;")
        provider.ANSWER_SPACING_S = 0.2
        try:
            info = await provider.join_meeting(LINK)
            sid = await provider._page.evaluate("window.ZoomMtg._showDialog('', 'This meeting is being transcribed.', ['OK'])")
            assert await eventually(lambda: info.dialog, seconds=6)                      # the page gives up on it after two seconds
            assert info.dialog["buttons"] == ["OK"] and info.dialog["text"] == "This meeting is being transcribed." and info.notice == "This meeting is being transcribed."
            warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.name == "croom.meeting.providers.zoom_sdk"]
            assert len(warned) == 1 and "This meeting is being transcribed." in warned[0] and "did not close" in warned[0]
            await asyncio.sleep(0.6)
            assert len(await dialog_presses(provider)) == 1                              # it was pressed once and is not pressed again
            with pytest.raises(MeetingControlError) as failure:
                await provider.control("dialog", {"id": info.dialog["id"], "button": "OK"})      # a tap gets the page's words
            assert "did not close" in str(failure.value) and info.dialog is not None
            await provider._page.evaluate(f"window.ZoomMtg._closeDialog('{sid}')")       # Zoom closes it at last
            assert await eventually(lambda: info.dialog is None)
            assert await provider.control("dialog", {"id": 1, "button": "OK"}) == {"dialog": None}      # a tap that comes after it is gone is done
        finally:
            await shutdown(provider)

    async def test_a_recording_that_stopped_is_pressed_read_again_and_not_disclosed(self):
        provider = await provider_for(api=FakeApi())
        provider.ANSWER_SPACING_S = 0.1
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("window.ZoomMtg._showDialog('', 'Recording has started. Everyone in the meeting is notified.', ['OK'])")
            assert await eventually(lambda: info.notice)
            reads = []
            read = provider._read_state_from_zoom

            async def counting():
                reads.append(1)
                return await read()

            provider._read_state_from_zoom = counting
            await provider._page.evaluate("window.ZoomMtg._showDialog('', 'Recording has stopped.', ['OK'])")
            assert len(await dialog_presses(provider, at_least=2)) == 2
            assert await eventually(lambda: reads)                                       # the state is read again once it has closed
            assert info.notice == "Recording has started." and info.dialog is None
        finally:
            await shutdown(provider)

    async def test_a_button_that_is_not_ready_says_so_and_a_button_the_dialog_lacks_does_too(self):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("window.ZoomMtg._showDialog('Breakout rooms', 'Join Room 1?', ['Not now', {label: 'Join', disabled: true}])")
            assert await eventually(lambda: info.dialog)
            with pytest.raises(MeetingControlError) as failure:
                await provider.control("dialog", {"id": info.dialog["id"], "button": "Join"})
            assert str(failure.value) == "The dialog's Join button is not ready yet; try again in a moment"
            with pytest.raises(MeetingControlError) as failure:
                await provider.control("dialog", {"id": info.dialog["id"], "button": "Maybe"})
            assert str(failure.value) == "The dialog has no button called Maybe"
            assert info.dialog is not None and await dialog_presses(provider) == []
        finally:
            await shutdown(provider)

    async def test_a_second_tap_on_a_dialog_just_answered_is_done_not_an_error(self):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("window.ZoomMtg._showDialog('Recording consent', 'This meeting is being recorded.', ['Leave Meeting', 'Got it'])")
            assert await eventually(lambda: info.dialog)
            tap = {"id": info.dialog["id"], "button": "Got it"}
            both = await asyncio.gather(provider.control("dialog", tap), provider.control("dialog", tap), return_exceptions=True)
            assert both == [{"dialog": None}, {"dialog": None}]                          # the page finds the second one's dialog gone
            assert len(await dialog_presses(provider)) == 1
        finally:
            await shutdown(provider)

    async def test_a_tap_on_a_dialog_nobody_answered_that_is_gone_says_so(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(LINK)
            with pytest.raises(MeetingControlError) as failure:
                await provider.control("dialog", {"id": 99, "button": "OK"})
            assert str(failure.value) == "That dialog is no longer on the screen"
            for body in (None, {"id": 1}, {"button": "OK"}, {"id": "one", "button": "OK"}, "OK"):
                with pytest.raises(MeetingControlError) as failure:
                    await provider.control("dialog", body)
                assert str(failure.value) == "Say which dialog and which button", body
        finally:
            await shutdown(provider)

    async def test_chat_is_kept_with_an_unread_count_the_table_can_clear(self):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("window.ZoomMtg._chat('Ann Lee', 'Can you hear us?')")
            assert await eventually(lambda: info.chat["messages"])
            [message] = info.chat["messages"]
            assert (message["from"], message["text"]) == ("Ann Lee", "Can you hear us?") and message["at"].endswith("Z")
            assert info.chat["unread"] == 1
            assert await provider.control("chat.seen") == {"chat": info.chat}
            assert info.chat["unread"] == 0 and len(info.chat["messages"]) == 1
            for i in range(25):
                await provider._page.evaluate(f"window.ZoomMtg._chat('Raj', 'm{i}')")
            assert await eventually(lambda: len(info.chat["messages"]) == 20 and info.chat["messages"][-1]["text"] == "m24", seconds=5)
            assert len(info.chat["messages"]) == 20 and info.chat["unread"] == 25
            assert info.chat["messages"][0]["text"] == "m5"                               # the last twenty, oldest first
            await provider._page.evaluate("window.ZoomMtg._chatFile('Ann Lee', 'agenda.pdf')")
            assert await eventually(lambda: info.chat["messages"][-1]["text"] == "agenda.pdf")      # a file is its name
        finally:
            await shutdown(provider)

    async def test_a_new_meeting_starts_clean_whatever_the_last_one_left(self):
        provider = await provider_for(api=FakeApi())
        provider.ANSWER_SPACING_S = 0.2
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("window.ZoomMtg._showDialog('', 'This meeting is being recorded.', ['OK'])")      # the page's first dialog: its id is 1
            await provider._page.evaluate("window.ZoomMtg._showDialog('Unmute?', 'The host would like you to unmute.', ['Stay muted', 'Unmute'])")
            await provider._page.evaluate("window.ZoomMtg._chat('Ann Lee', 'hello')")
            assert await eventually(lambda: info.dialog and info.notice and info.chat["messages"])
            assert await eventually(lambda: len(provider._background) == 0 and len(provider._dialogs) == 1)
            await provider.leave_meeting()
            info = await provider.join_meeting(LINK)
            assert info.dialog is None and info.notice == "" and info.chat == {"unread": 0, "messages": []}
            await provider._page.evaluate("window.ZoomMtg._showDialog('', 'This meeting is being recorded.', ['OK'])")      # the new page numbers its dialogs from 1 again
            await provider._page.evaluate("window.ZoomMtg._showDialog('Breakout rooms', 'Join Room 1?', ['Not now', 'Join'])")   # so this is 2, as the last meeting's waiting one was
            assert await eventually(lambda: info.notice and info.dialog)
            assert info.dialog["title"] == "Breakout rooms"
            assert await dialog_presses(provider, at_least=1) == [["dialog", "", "OK"]]
        finally:
            await shutdown(provider)

    async def test_the_view_and_self_view_start_from_what_the_client_has(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._layout = 'gallery'; window.ZoomMtg._selfHidden = true;")
        try:
            info = await provider.join_meeting(LINK)
            assert await eventually(lambda: info.controls["layout"]["value"] == "gallery" and info.controls["hide_self"]["on"] is True)
            assert [p[0] for p in await provider._page.evaluate("window.__presses")] == ["stub-view", "stub-view"]       # looked at once, nothing picked
            assert await provider._page.evaluate("[window.ZoomMtg._layout, window.ZoomMtg._selfHidden]") == ["gallery", True]
            await provider._page.evaluate("window.ZoomMtg._setAttendee(16777218, {bRaiseHand: false})")
            assert await eventually(lambda: info.people["list"][2]["hand"] is False)
            await asyncio.sleep(0.3)
            assert len(await provider._page.evaluate("window.__presses")) == 2                                            # and not again
        finally:
            await shutdown(provider)

    async def test_the_view_waits_for_a_dialog_that_was_open_when_the_room_came_in(self):
        """The page says a dialog as soon as it is drawn, and the provider's watcher takes what it said after the join's reads: the View
        menu is not opened over a dialog the client already has on the screen, but when it is gone."""
        stub = (STUB_JS + "window.ZoomMtg._behaviour.videoOffOnEntry = true; window.ZoomMtg._behaviour.videoStartsAfterMs = 1000;"
                "const join = window.ZoomMtg.join; window.ZoomMtg.join = function (o) { join.call(this, o); setTimeout(() => "
                "window.ZoomMtg._showDialog('Unmute?', 'The host would like you to unmute.', ['Stay muted', 'Unmute']), 100); };")
        provider = await provider_for(api=FakeApi(), stub=stub)
        try:
            info = await provider.join_meeting(LINK, camera_on=True)                     # the camera's switch is waited for: a second, the dialog comes within it
            assert info.is_camera_on is True and await eventually(lambda: info.dialog)
            await asyncio.sleep(0.5)

            async def view_presses():
                return [p for p in await provider._page.evaluate("window.__presses") if p[0] == "stub-view"]

            assert await view_presses() == []                                            # nothing has opened the menu over the dialog
            await provider.control("dialog", {"id": info.dialog["id"], "button": "Stay muted"})
            for _ in range(60):
                if len(await view_presses()) == 2:
                    break
                await asyncio.sleep(0.05)
            assert len(await view_presses()) == 2                                        # and now it has, once: opened and closed
        finally:
            await shutdown(provider)

    async def test_ending_the_meeting_for_everyone_is_done_when_the_meeting_is_over(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.role = 'host';")
        try:
            info = await provider.join_meeting(LINK)
            assert info.controls["host"]["end"]["available"] is True
            assert await provider.control("host.end") == {}
            assert provider.state == MeetingState.IDLE and provider.current_meeting is None
            assert provider._page.url == "about:blank"
        finally:
            await shutdown(provider)

    async def test_a_refused_end_for_everyone_is_in_words_and_the_meeting_goes_on(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.role = 'co-host';")
        try:
            info = await provider.join_meeting(LINK)
            assert info.controls["host"]["end"]["available"] is False                    # the host's alone: and Zoom refuses a co-host
            with pytest.raises(MeetingControlError) as failure:
                await provider.control("host.end")
            assert str(failure.value) == "Zoom refused endMeeting (code 3003)" and provider.state == MeetingState.CONNECTED
        finally:
            await shutdown(provider)

    async def test_from_config_takes_the_dialog_policy_and_the_reactions(self, tmp_path):
        path = tmp_path / "zoom-credentials.json"
        path.write_text(json.dumps({"sdk_client_id": "a", "sdk_client_secret": "b"}))
        config = Config()
        config.meeting.zoom_credentials_path = str(path)
        provider = ZoomSdkProvider.from_config(config)
        assert provider.dialog_policy.acknowledge == [p.lower() for p in DEFAULT_ACKNOWLEDGE] and provider.dialog_policy.ask_everything is False
        assert provider.reactions == DEFAULT_REACTIONS
        config.meeting.dialogs.acknowledge, config.meeting.dialogs.ask_everything = ["Is Being Recorded"], True
        config.meeting.controls.reactions = ["Clap"]
        provider = ZoomSdkProvider.from_config(config)
        assert provider.dialog_policy.acknowledge == ["is being recorded"] and provider.dialog_policy.ask_everything is True
        assert provider.reactions == ["Clap"]


def test_a_provider_is_built_from_a_config_whose_lists_were_left_blank(tmp_path):
    """`reactions:` or `acknowledge:` with its lines deleted loads as nothing: the defaults stand, and the Zoom provider is still built."""
    path = tmp_path / "zoom-credentials.json"
    path.write_text(json.dumps({"sdk_client_id": "a", "sdk_client_secret": "b"}))
    config = Config.from_dict({"meeting": {"zoom_credentials_path": str(path), "dialogs": {"acknowledge": None, "ask_everything": True}, "controls": {"reactions": None}}})
    provider = ZoomSdkProvider.from_config(config)
    assert provider.dialog_policy.acknowledge == [p.lower() for p in DEFAULT_ACKNOWLEDGE] and provider.dialog_policy.ask_everything is True
    assert provider.reactions == DEFAULT_REACTIONS
    assert Config.from_dict(config.to_dict()).meeting.controls.reactions == DEFAULT_REACTIONS


def test_the_base_provider_offers_no_controls():
    from croom.meeting.providers.zoom import ZoomProvider

    async def ask():
        await ZoomProvider().control("raise_hand")

    with pytest.raises(MeetingControlError) as failure:
        asyncio.run(ask())
    assert str(failure.value) == "Not available in this meeting"


class HostEndPage:
    """The page of a room whose host ends the meeting for everyone, as it goes in the real client: the page says the meeting is over, and the
    call it was asked for is answered, or cut off by the navigation that follows, or fails after the meeting is gone; or Zoom refuses it."""

    def __init__(self, provider, how):
        self.provider, self.how = provider, how

    async def evaluate(self, script, arg=None):
        assert "crystalMeet.control" in script and arg == ["host.end", None], (script, arg)
        if self.how == "refused":
            raise playwright.Error("Page.evaluate: Error: Zoom refused endMeeting (code 3003)\n    at sdk (http://127.0.0.1/static/meeting.js:1:1)")
        if self.how != "answered, no event":
            self.provider._on_page_event("left", "")
        if self.how in ("answered", "answered, no event"):
            return {}
        if self.how == "cut off":
            raise playwright.Error("Page.evaluate: Execution context was destroyed, most likely because of a navigation")
        for _ in range(200):                                      # "gone first": it fails once the room has gone back to idle
            if self.provider.state == MeetingState.IDLE:
                break
            await asyncio.sleep(0.01)
        raise playwright.Error("Page.evaluate: Target page, context or browser has been closed")


class TestTheModelAndTheDialogsWithoutABrowser:
    def test_the_mute_control_agrees_with_the_status_when_zoom_has_not_said(self):
        provider = in_a_meeting(None)
        info = provider.current_meeting
        provider._muted = True                       # this join's own start: the room should be muted, and Zoom has not listed it yet
        provider._take_state(dict(READING, me={"muted": None, "videoOn": None}, buttons={}))
        assert info.is_muted is True and info.controls["mute"] == {"available": True, "on": False}
        provider._take_state(dict(READING, me={"muted": False, "videoOn": True}, buttons={}))
        assert info.is_muted is False and info.controls["mute"] == {"available": True, "on": True}

    async def test_a_controls_answer_is_taken_into_the_model_it_returns(self):
        """The page answers a control with the meeting as it is afterwards: that is what the table is told, without waiting for the page's own
        event to say the same."""
        before = dict(READING, buttons={"raise_hand": {"on": False}})
        after = dict(READING, buttons={"raise_hand": {"on": True}}, me=dict(READING["me"], raisedHand=True))
        page = FakePage(reading=before, control_reading=after)
        provider = in_a_meeting(page)
        provider._take_state(before)
        assert provider.current_meeting.controls["raise_hand"]["on"] is False
        state = await provider.control("raise_hand")
        assert page.asked == [["raise_hand", None]] and state["raise_hand"] == {"available": True, "on": True}
        assert provider.current_meeting.controls["raise_hand"]["on"] is True and state is provider.current_meeting.controls

    def test_the_roster_is_listed_at_most_fifty_long_and_counted_whole(self):
        provider = in_a_meeting(None)
        crowd = [{"id": 100 + i, "name": f"Person {i}", "me": False} for i in range(60)]
        provider._take_state(dict(READING, participants=crowd, count=60, buttons={}))
        people = provider.current_meeting.people
        assert people["count"] == 60 and len(people["list"]) == 50 and people["list"][0]["name"] == "Person 0"

    def test_people_in_the_waiting_room_are_not_counted_or_listed_among_those_in_the_meeting(self):
        """Zoom's attendee list holds the people in the waiting room too (waiting: true); the table lists them apart (controls.host.waiting_room), so the
        count and the list are of those who are in the meeting."""
        provider = in_a_meeting(None)
        people = [{"id": 1, "name": "Room", "me": True, "waiting": False}, {"id": 2, "name": "Ann", "waiting": False}, {"id": 3, "name": "Guest", "waiting": True}]
        provider._take_state(dict(READING, participants=people, count=3, buttons={}))
        shown = provider.current_meeting.people
        assert shown["count"] == 2 and [p["name"] for p in shown["list"]] == ["Room", "Ann"]

    def test_the_cap_is_on_those_in_the_meeting_and_the_count_is_whole(self):
        provider = in_a_meeting(None)
        crowd = [{"id": 100 + i, "name": f"Person {i}", "me": False, "waiting": i < 5} for i in range(60)]     # five of the sixty are waiting
        provider._take_state(dict(READING, participants=crowd, count=60, buttons={}))
        people = provider.current_meeting.people
        assert people["count"] == 55 and len(people["list"]) == 50 and people["list"][0]["name"] == "Person 5"
        provider._take_state(dict(READING, participants=crowd[3:8], count=120, buttons={}))     # a count of the reading's own (Zoom may list fewer than it counts): less the waiting
        people = provider.current_meeting.people
        assert people["count"] == 118 and [p["name"] for p in people["list"]] == ["Person 5", "Person 6", "Person 7"]

    def test_a_reading_of_the_current_user_alone_changes_only_what_it_says(self):
        provider = in_a_meeting(None)
        info = provider.current_meeting
        provider._take_state(dict(READING, participants=[{"id": 1, "name": "Ann", "me": False}], count=1, buttons={}, recording=True, locked=True))
        provider._take_state({"muted": True, "videoOn": False})                      # the page's first form of a reading: the user's own fields only
        assert info.is_muted is True and info.is_camera_on is False and info.controls["mute"]["on"] is False
        assert info.people["count"] == 1 and info.recording is True and info.locked is True
        provider._take_state(None)
        provider._take_state("not a reading")
        assert info.is_muted is True and info.people["count"] == 1

    async def test_acknowledgments_are_one_at_a_time_and_a_second_apart(self):
        assert ZoomSdkProvider.ANSWER_SPACING_S == 1
        page = FakePage()
        provider = in_a_meeting(page)
        provider.ANSWER_SPACING_S = 0.3                # shortened, as the other waits are here
        for i in (1, 2, 3):
            provider._on_dialog({"id": i, "title": "", "text": f"This meeting is being transcribed ({i}).", "buttons": ["OK"]})
        assert page.pressed == []
        await settle_background(provider)
        assert [(i, label) for _, i, label in page.pressed] == [(1, "OK"), (2, "OK"), (3, "OK")]
        gaps = [b[0] - a[0] for a, b in zip(page.pressed, page.pressed[1:], strict=False)]
        assert all(gap >= 0.28 for gap in gaps), gaps

    async def test_leaving_or_shutting_down_cancels_what_the_provider_started_on_its_own(self):
        provider = in_a_meeting(FakePage())
        waiting = provider._spawn(asyncio.sleep(60))                          # an answer to a dialog that waits for its turn, say
        await asyncio.sleep(0)
        await provider._cancel_tasks()
        assert waiting.cancelled() and provider._background == set()

    async def test_a_step_the_provider_took_on_its_own_that_fails_is_a_warning_not_a_lost_exception(self, caplog):
        async def fails():
            raise ValueError("a bug")

        provider = in_a_meeting(FakePage())
        provider._spawn(fails())
        await settle_background(provider)
        await asyncio.sleep(0)
        assert [r.levelno for r in caplog.records if "a bug" in r.getMessage()] == [logging.WARNING]
        assert provider._background == set()

    async def test_a_dialog_the_page_says_twice_is_answered_once(self):
        page = FakePage()
        provider = in_a_meeting(page)
        info = provider.current_meeting
        notice = {"id": 4, "title": "", "text": "This meeting is being transcribed.", "buttons": ["OK"]}
        provider._on_dialog(notice)
        provider._on_dialog(dict(notice))
        await settle_background(provider)
        assert [(i, label) for _, i, label in page.pressed] == [(4, "OK")] and info.notice == "This meeting is being transcribed."
        waiting = {"id": 5, "title": "Unmute?", "text": "The host would like you to unmute.", "buttons": ["Stay muted", "Unmute"]}
        provider._on_dialog(waiting)
        provider._on_dialog(dict(waiting))
        assert info.dialog["id"] == 5 and len(provider._dialogs) == 1

    async def test_a_dialog_is_never_answered_twice_even_when_the_first_press_failed(self):
        page = FakePage(press_failure="The dialog's OK button is not ready yet; try again in a moment")
        provider = in_a_meeting(page)
        notice = {"id": 4, "title": "", "text": "This meeting is being transcribed.", "buttons": ["OK"]}
        provider._on_dialog(notice)
        await settle_background(provider)
        provider._on_dialog_closed({"id": 4})
        await settle_background(provider)
        provider._on_dialog(dict(notice))                                               # the page says it again, once it has closed
        await settle_background(provider)
        assert [(i, label) for _, i, label in page.pressed] == [(4, "OK")]

    async def test_the_reactions_the_table_offers_are_the_ones_in_the_config(self):
        provider = in_a_meeting(None)
        provider.reactions = ["Clap", "Tada"]
        provider._take_state(dict(READING, buttons={"reactions": {}}))
        assert provider.current_meeting.controls["reactions"] == {"available": True, "choices": ["Clap", "Tada"]}

    async def test_a_notice_about_the_rooms_own_mute_is_pressed_and_has_no_line(self):
        page = FakePage()
        provider = in_a_meeting(page)
        provider._on_dialog({"id": 6, "title": "", "text": "Your microphone has been muted by the host.", "buttons": ["OK"]})
        await settle_background(provider)
        assert [(i, label) for _, i, label in page.pressed] == [(6, "OK")]
        assert provider.current_meeting.notice == "" and provider.current_meeting.dialog is None

    async def test_a_recording_that_stopped_is_pressed_and_adds_nothing_to_the_notice_line(self):
        page = FakePage()
        provider = in_a_meeting(page)
        provider.ANSWER_SPACING_S = 0.05
        info = provider.current_meeting
        provider._on_dialog({"id": 1, "title": "", "text": "Recording has started. Everyone in the meeting is notified.", "buttons": ["OK"]})
        provider._on_dialog({"id": 2, "title": "", "text": "Recording has stopped.", "buttons": ["OK"]})
        await settle_background(provider)
        assert info.notice == "Recording has started." and info.dialog is None
        assert [(i, label) for _, i, label in page.pressed] == [(1, "OK"), (2, "OK")]

    async def test_a_notice_said_again_is_pressed_again_and_said_once_and_different_ones_follow_in_the_order_they_came(self):
        page = FakePage()
        provider = in_a_meeting(page)
        provider.ANSWER_SPACING_S = 0.05
        info = provider.current_meeting
        for i, words in enumerate(["This meeting is being recorded.", "This meeting is being recorded.", "This meeting is being transcribed and Zoom AI is on."], 1):
            provider._on_dialog({"id": i, "title": "", "text": words, "buttons": ["OK"]})
        await settle_background(provider)
        assert info.notice == "This meeting is being recorded. This meeting is being transcribed and Zoom AI is on."
        assert [i for _, i, _ in page.pressed] == [1, 2, 3]

    async def test_an_acknowledgment_the_page_refuses_is_a_warning_and_the_dialog_waits_on_the_table_unpressed_again(self, caplog):
        caplog.set_level(logging.DEBUG, logger="croom.meeting.providers.zoom_sdk")
        page = FakePage(press_failure="The dialog's OK button is not ready yet; try again in a moment")
        provider = in_a_meeting(page)
        info = provider.current_meeting
        provider._on_dialog({"id": 7, "title": "Recording", "text": "This meeting is being recorded.", "buttons": ["OK"]})
        await settle_background(provider)
        assert info.notice == "This meeting is being recorded."                       # its words are on the notice line, and it is still on the TV:
        assert info.dialog == {"id": 7, "title": "Recording", "text": "This meeting is being recorded.", "buttons": ["OK"]}      # so it waits for a tap
        warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.name == "croom.meeting.providers.zoom_sdk"]
        assert len(warned) == 1 and "Recording" in warned[0] and "This meeting is being recorded." in warned[0] and "not ready yet" in warned[0]
        await asyncio.sleep(0.2)
        provider._on_dialog({"id": 7, "title": "Recording", "text": "This meeting is being recorded.", "buttons": ["OK"]})      # said again: not taken again
        await settle_background(provider)
        assert [(i, label) for _, i, label in page.pressed] == [(7, "OK")] and len(provider._dialogs) == 1       # never the same dialog twice

    async def test_a_notice_whose_press_fails_waits_behind_the_dialog_that_was_waiting_first(self):
        page = FakePage(press_failure="The dialog's OK button is not ready yet; try again in a moment")
        provider = in_a_meeting(page)
        info = provider.current_meeting
        provider._on_dialog({"id": 1, "title": "Unmute?", "text": "", "buttons": ["Stay muted", "Unmute"]})       # waits for a tap
        provider._on_dialog({"id": 2, "title": "", "text": "This meeting is being recorded.", "buttons": ["OK"]})  # a notice whose press fails
        await settle_background(provider)
        assert [d["id"] for d in provider._dialogs] == [1, 2] and info.dialog["id"] == 1     # the oldest is the one on show

    async def test_a_tap_on_a_notice_the_provider_could_not_press_gets_the_pages_words_and_is_done_once_the_dialog_is_gone(self):
        page = FakePage(press_failure="The dialog's OK button is not ready yet; try again in a moment")
        provider = in_a_meeting(page)
        info = provider.current_meeting
        provider._on_dialog({"id": 7, "title": "", "text": "This meeting is being recorded.", "buttons": ["OK"]})
        await settle_background(provider)
        with pytest.raises(MeetingControlError) as failure:
            await provider.control("dialog", {"id": 7, "button": "OK"})                    # the page still will not
        assert str(failure.value) == "The dialog's OK button is not ready yet; try again in a moment" and info.dialog["id"] == 7
        page.press_failure = "That dialog is no longer on the screen"                         # it went away on its own: the notice was this provider's to answer
        assert await provider.control("dialog", {"id": 7, "button": "OK"}) == {"dialog": None}
        assert info.dialog is None
        page.press_failure = None
        provider._on_dialog({"id": 8, "title": "", "text": "This meeting is being transcribed.", "buttons": ["OK"]})
        await settle_background(provider)
        assert info.dialog is None                                                           # and one that is pressed never reaches the table

    async def test_a_notice_that_closes_before_its_press_fails_does_not_reach_the_table(self, caplog):
        caplog.set_level(logging.DEBUG, logger="croom.meeting.providers.zoom_sdk")
        provider = in_a_meeting(FakePage(press_failure="Zoom's dialog did not close when OK was pressed; try again in a moment"))
        provider._on_dialog({"id": 7, "title": "", "text": "This meeting is being recorded.", "buttons": ["OK"]})
        provider._on_dialog_closed({"id": 7})                                                # Zoom closed it on its own before the page gave up
        await settle_background(provider)
        assert provider.current_meeting.dialog is None and provider._dialogs == []
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], [r.getMessage() for r in caplog.records]

    async def test_every_dialog_is_a_line_in_the_journal_with_its_words_and_buttons(self, caplog):
        caplog.set_level(logging.INFO, logger=DIALOGS_LOGGER)
        provider = in_a_meeting(FakePage())
        provider._on_dialog({"id": 8, "title": "Recording consent", "text": "This meeting is being recorded.", "buttons": ["Leave Meeting", "Got it"]})
        provider._on_dialog({"id": 9, "title": "", "text": "This meeting is being transcribed.", "buttons": ["OK"]})
        await settle_background(provider)
        lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO and "Zoom dialog" in r.getMessage()]
        assert len(lines) == 2 and "Recording consent" in lines[0] and "Leave Meeting" in lines[0] and "Got it" in lines[0]
        assert "This meeting is being transcribed." in lines[1] and "'OK'" in lines[1]

    async def test_each_dialogs_three_lines_come_from_the_dialogs_logger_in_the_words_the_guide_quotes(self, caplog):
        """What Zoom drew, and what was done with it: pressed for the room ("Acknowledged ...") or left for the table ("The dialog waits on the table"). They are
        the one thing a stock install's journal must hold of a meeting, so they come from a logger of their own that the agent keeps at INFO."""
        caplog.set_level(logging.INFO)
        provider = in_a_meeting(FakePage())
        provider._on_dialog({"id": 8, "title": "Recording consent", "text": "This meeting is being recorded.", "buttons": ["Leave Meeting", "Got it"]})
        provider._on_dialog({"id": 9, "title": "", "text": "This meeting is being transcribed.", "buttons": ["OK"]})
        await settle_background(provider)
        ours = [(r.name, r.levelno, r.getMessage()) for r in caplog.records if r.name == DIALOGS_LOGGER]
        assert ours == [
            (DIALOGS_LOGGER, logging.INFO, "Zoom dialog: 'Recording consent' 'This meeting is being recorded.' buttons ['Leave Meeting', 'Got it']"),
            (DIALOGS_LOGGER, logging.INFO, "The dialog waits on the table"),
            (DIALOGS_LOGGER, logging.INFO, "Zoom dialog: '' 'This meeting is being transcribed.' buttons ['OK']"),
            (DIALOGS_LOGGER, logging.INFO, "Acknowledged Zoom's dialog with 'OK'"),
        ]
        assert not [r for r in caplog.records if r.name == "croom.meeting.providers.zoom_sdk" and r.levelno == logging.INFO and "dialog" in r.getMessage()]

    async def test_on_a_stock_install_a_dialog_reaches_the_journal_though_the_agent_logs_warnings_only(self, agent_logging):
        """The unit starts the agent with no -v. The provider's own INFO lines are not written then; the dialogs' are."""
        reached = agent_logging()
        provider = in_a_meeting(FakePage())
        provider._on_dialog({"id": 8, "title": "Recording consent", "text": "This meeting is being recorded.", "buttons": ["Leave Meeting", "Got it"]})
        provider._on_dialog({"id": 9, "title": "", "text": "This meeting is being transcribed.", "buttons": ["OK"]})
        await settle_background(provider)
        logging.getLogger("croom.meeting.providers.zoom_sdk").info("Left the Zoom meeting")        # an INFO line of the provider's own
        said = [r.getMessage() for r in reached]
        assert said == [
            "Zoom dialog: 'Recording consent' 'This meeting is being recorded.' buttons ['Leave Meeting', 'Got it']",
            "The dialog waits on the table",
            "Zoom dialog: '' 'This meeting is being transcribed.' buttons ['OK']",
            "Acknowledged Zoom's dialog with 'OK'",
        ]

    async def test_an_event_the_provider_cannot_read_is_dropped_and_the_watcher_goes_on(self):
        provider = in_a_meeting(FakePage())
        provider._display = NoDisplay()
        info = provider.current_meeting
        for event in [("state", "not json"), ("dialog", "{}"), ("dialog", "[1]"), ("dialog-closed", "null"), ("chat", "[]"), ("chat", "7"),
                      ("chat", json.dumps({"from": "Ann", "text": "hi", "at": "x"})),
                      ("dialog", json.dumps({"id": 3, "title": "Unmute?", "text": "", "buttons": ["Stay muted", "Unmute"]})),
                      ("left", "")]:
            provider._events.put_nowait(event)
        await asyncio.wait_for(provider._watch_meeting(), timeout=5)
        assert [m["text"] for m in info.chat["messages"]] == ["hi"]
        assert provider.state == MeetingState.IDLE and provider.current_meeting is None
        assert provider._dialogs == [] and not provider._on_screen and info.dialog is None   # nothing is said when the meeting ends: the list is cleared

    async def test_a_handler_that_fails_is_a_warning_and_the_watcher_goes_on(self, caplog):
        """Whatever is wrong with one event, the watcher must live: it is what sees the meeting end and returns the room to idle."""
        provider = in_a_meeting(FakePage())

        def fails(message):
            raise RuntimeError("a bug in the handler")

        provider._on_chat = fails
        for event in [("chat", json.dumps({"from": "Ann", "text": "hi", "at": "x"})), ("left", "")]:
            provider._events.put_nowait(event)
        await asyncio.wait_for(provider._watch_meeting(), timeout=5)
        assert provider.state == MeetingState.IDLE
        assert [r.levelno for r in caplog.records if "Could not take an event" in r.getMessage()] == [logging.WARNING]

    def test_what_the_page_sends_of_a_dialog_or_a_message_is_read_with_care(self):
        provider = in_a_meeting(None)
        assert provider._dialog_id({"id": "7"}) == 7 and provider._dialog_id({"id": 7.0}) == 7
        for odd in ({}, {"id": "seven"}, {"id": None}, [1], None, "x", 7):
            assert provider._dialog_id(odd) is None, odd
        for odd in ([], 7, None, "hi"):
            provider._on_chat(odd)
        assert provider.current_meeting.chat == {"unread": 0, "messages": []}

    async def test_a_tap_on_a_dialog_is_given_the_controls_long_bound_not_the_reads_short_one(self):
        provider = in_a_meeting(SilentPage())
        provider.CONTROL_TIMEOUT_S, provider.PAGE_CALL_TIMEOUT_S = 0.4, 30
        provider._on_dialog({"id": 1, "title": "Unmute?", "text": "", "buttons": ["Stay muted", "Unmute"]})
        with pytest.raises(MeetingControlError) as failure:
            await asyncio.wait_for(provider.control("dialog", {"id": 1, "button": "Unmute"}), timeout=5)
        assert str(failure.value) == "Could not answer the dialog: the meeting page did not answer within 0.4 seconds"
        assert provider.current_meeting.dialog["id"] == 1                                    # it stays for another try

    async def test_the_press_of_a_notice_is_given_the_same_bound_and_a_press_that_never_answers_puts_the_notice_on_the_table(self):
        provider = in_a_meeting(SilentPage())
        provider.CONTROL_TIMEOUT_S, provider.PAGE_CALL_TIMEOUT_S = 0.4, 30
        provider._on_dialog({"id": 1, "title": "", "text": "This meeting is being recorded.", "buttons": ["OK"]})
        await asyncio.wait_for(settle_background(provider), timeout=5)
        assert provider.current_meeting.dialog["id"] == 1 and provider.current_meeting.notice == "This meeting is being recorded."

    async def test_ending_the_meeting_is_given_the_controls_long_bound_too(self):
        provider = in_a_meeting(SilentPage())
        provider.CONTROL_TIMEOUT_S, provider.PAGE_CALL_TIMEOUT_S = 0.4, 30
        with pytest.raises(MeetingControlError) as failure:
            await asyncio.wait_for(provider.control("host.end"), timeout=5)
        assert str(failure.value) == "Could not end the meeting: the meeting page did not answer within 0.4 seconds"
        assert provider.state == MeetingState.CONNECTED                                       # a call that never answered is no end

    async def test_the_view_read_is_given_the_controls_long_bound_too(self, caplog):
        caplog.set_level(logging.DEBUG, logger="croom.meeting.providers.zoom_sdk")
        provider = in_a_meeting(SilentPage())
        provider.CONTROL_TIMEOUT_S, provider.PAGE_CALL_TIMEOUT_S = 0.4, 30
        provider._take_state(READING)
        await asyncio.wait_for(settle_background(provider), timeout=5)
        assert [r.getMessage() for r in caplog.records if "The View menu was not read" in r.getMessage()] == [
            "The View menu was not read: Could not read the View menu: the meeting page did not answer within 0.4 seconds"]

    @pytest.mark.parametrize("event", [("left", ""), ("error", "boom")])
    async def test_what_the_provider_started_on_its_own_is_cancelled_when_the_meeting_ends_or_fails(self, event):
        provider = in_a_meeting(FakePage())
        provider._display = NoDisplay()
        waiting = provider._spawn(asyncio.sleep(60))                                          # a press waiting for its turn, say
        provider._events.put_nowait(event)
        await asyncio.wait_for(provider._watch_meeting(), timeout=5)
        await asyncio.gather(waiting, return_exceptions=True)
        await asyncio.sleep(0)
        assert waiting.cancelled() and provider._background == set()

    async def test_an_error_takes_the_waiting_dialog_off_the_status(self):
        provider = in_a_meeting(FakePage())
        info = provider.current_meeting
        for event in [("dialog", json.dumps({"id": 3, "title": "Unmute?", "text": "", "buttons": ["Stay muted", "Unmute"]})), ("error", "boom")]:
            provider._events.put_nowait(event)
        await asyncio.wait_for(provider._watch_meeting(), timeout=5)
        assert provider.state == MeetingState.ERROR and info.error_message == "boom"
        assert info.dialog is None and provider._dialogs == []

    async def test_a_reconnect_keeps_the_dialogs_the_page_has_already_said(self):
        """The page keeps its dialogs' ids across a reconnect and says a gone one closed: the list is not cleared on reconnecting."""
        provider = in_a_meeting(FakePage())
        provider._display = NoDisplay()
        info = provider.current_meeting
        watcher = asyncio.ensure_future(provider._watch_meeting())
        for event in [("dialog", json.dumps({"id": 3, "title": "Unmute?", "text": "", "buttons": ["Stay muted", "Unmute"]})), ("reconnecting", ""), ("connected", "")]:
            provider._events.put_nowait(event)
        await asyncio.sleep(0.1)
        assert info.dialog is not None and info.dialog["id"] == 3                        # the reconnect did not clear it
        provider._events.put_nowait(("dialog-closed", json.dumps({"id": 3})))            # a dialog that went meanwhile is said to be closed
        await asyncio.sleep(0.1)
        assert info.dialog is None and provider.state == MeetingState.CONNECTED
        provider._events.put_nowait(("left", ""))
        await asyncio.wait_for(watcher, timeout=5)

    async def test_state_events_older_than_the_last_read_at_join_are_dropped_and_the_others_kept(self):
        """The watcher starts after the join's reads, so an event the page sent during the join is taken after them: one that is older than
        the last read would put the status back to what it was. A read covers the state events; a dialog, a chat message or the meeting
        ending is not something a read says, and stays for the watcher."""
        page = BridgePage(True, True)                                  # Zoom says: muted
        provider = ZoomSdkProvider(SDK_ONLY)
        provider._page, provider._events = page, asyncio.Queue()
        provider._current_meeting = MeetingInfo(platform="zoom", meeting_id="1", meeting_url=LINK)
        stale = json.dumps({"muted": False, "videoOn": True})
        dialog = json.dumps({"id": 1, "title": "", "text": "This meeting is being recorded.", "buttons": ["OK"]})
        chat = json.dumps({"from": "Ann", "text": "hi", "at": "x"})
        for event in [("state", stale), ("dialog", dialog), ("state", stale), ("chat", chat), ("left", "")]:
            provider._events.put_nowait(event)
        zoom = await provider._read_state_from_zoom()
        await provider._apply_wanted_state(zoom, camera_on=True, mic_on=False)
        assert queued(provider._events) == [("dialog", dialog), ("chat", chat), ("left", "")]
        assert provider.current_meeting.is_muted is True

    async def test_the_view_is_read_once_when_the_view_button_is_there_and_no_dialog_is_open(self):
        defaults = dict(READING, buttons={"layout": {"value": "speaker"}, "hide_self": {"on": False}})      # what the page says until the menu has been read
        page = FakePage(reading=defaults, control_reading=READING)                                          # and what it says once it has: gallery, self view hidden
        provider = in_a_meeting(page)
        info = provider.current_meeting
        provider._take_state(dict(READING, buttons={}))                                  # no View button on the screen yet
        await settle_background(provider)
        assert page.asked == [] and info.controls["layout"] == {"available": False, "choices": ["speaker", "gallery"]}
        provider._on_dialog({"id": 1, "title": "Unmute?", "text": "", "buttons": ["Stay muted", "Unmute"]})
        provider._take_state(defaults)                                                  # the button is there, a dialog too
        await settle_background(provider)
        assert page.asked == [] and info.controls["layout"]["value"] == "speaker"       # a dialog drawn in the menu's window would add its buttons to it
        provider._on_dialog_closed({"id": 1})                                           # it closes: the state is read again, and now the view is looked at
        await settle_background(provider)
        assert page.asked == [["layout.read", None]]
        assert info.controls["layout"]["value"] == "gallery" and info.controls["hide_self"]["on"] is True      # what the client has, from the menu's own answer
        provider._take_state(defaults)
        await settle_background(provider)
        assert page.asked == [["layout.read", None]]                                    # once

    async def test_the_view_is_not_read_while_the_room_is_still_joining(self):
        page = FakePage()
        provider = in_a_meeting(page)
        provider._state = MeetingState.JOINING
        provider._take_state(READING)
        await settle_background(provider)
        assert page.asked == []
        provider._state = MeetingState.CONNECTED
        provider._take_state(READING)
        await settle_background(provider)
        assert page.asked == [["layout.read", None]]

    async def test_a_view_the_page_will_not_read_is_a_debug_line_and_is_not_asked_again(self, caplog):
        caplog.set_level(logging.DEBUG, logger="croom.meeting.providers.zoom_sdk")

        class Refusing(FakePage):
            async def evaluate(self, script, arg=None):
                if "crystalMeet.control" in script:
                    self.asked.append(arg)
                    raise RuntimeError("Page.evaluate: Error: Zoom's layout button is not ready yet; try again in a moment")
                return await super().evaluate(script, arg)

        page = Refusing()
        provider = in_a_meeting(page)
        provider._take_state(READING)
        await settle_background(provider)
        provider._take_state(READING)
        await settle_background(provider)
        assert page.asked == [["layout.read", None]]
        assert [r.levelno for r in caplog.records if "not ready yet" in r.getMessage()] == [logging.DEBUG]
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    @pytest.mark.parametrize("how", ["answered", "cut off", "gone first"])
    async def test_ending_the_meeting_is_done_however_the_end_reaches_the_call(self, how):
        """It is done when the call is answered, when the navigation that follows the end cuts the call off ("Execution context was
        destroyed"), or when the meeting is already over by the time the call fails: in each the control waits for the room to be idle, and
        none of them is an error for the table."""
        provider = in_a_meeting(None)
        provider._page = HostEndPage(provider, how)
        provider._display = NoDisplay()
        provider._watcher = asyncio.ensure_future(provider._watch_meeting())
        assert await provider.control("host.end") == {}
        assert provider.state == MeetingState.IDLE and provider.current_meeting is None and provider._watcher is None

    async def test_an_end_zoom_refuses_is_in_words_and_the_meeting_goes_on(self):
        provider = in_a_meeting(None)
        provider._page = HostEndPage(provider, "refused")
        provider._watcher = asyncio.ensure_future(provider._watch_meeting())
        try:
            with pytest.raises(MeetingControlError) as failure:
                await provider.control("host.end")
            assert str(failure.value) == "Zoom refused endMeeting (code 3003)" and provider.state == MeetingState.CONNECTED
        finally:
            provider._watcher.cancel()
            await asyncio.gather(provider._watcher, return_exceptions=True)

    async def test_an_end_zoom_took_is_done_even_when_the_left_event_is_slow(self):
        provider = in_a_meeting(None)
        provider._page = HostEndPage(provider, "answered, no event")
        provider._watcher = asyncio.ensure_future(provider._watch_meeting())
        provider.LEFT_WAIT_S = 0.2
        try:
            assert await provider.control("host.end") == {}                                # Zoom said yes: the wait for the room to be idle is bounded
            assert provider.state == MeetingState.CONNECTED                                # and the left event, when it comes, ends it
        finally:
            provider._watcher.cancel()
            await asyncio.gather(provider._watcher, return_exceptions=True)
