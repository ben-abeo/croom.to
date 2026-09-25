"""
The Meeting SDK provider end to end in the venv's headless Chromium against the
stub SDK (spec 2026-09-25 Zoom, section 4.4): joining, the waiting room, Zoom's
errors, the ZAK path, mute, camera, leave, and joining again.
"""

import json

import pytest

playwright = pytest.importorskip("playwright.async_api")

from croom.core.config import Config  # noqa: E402
from croom.meeting.providers.base import MeetingState  # noqa: E402
from croom.meeting.providers.zoom_sdk import ZoomSdkProvider  # noqa: E402
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
    provider = ZoomSdkProvider(credentials, room_name="Room 1", api=api, headless=True,
                               extra_init_script=stub, block_sdk_cdn=True)
    await provider.initialize()
    return provider


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
            await provider.shutdown()

    async def test_without_a_room_user_joins_with_the_signature_only(self):
        provider = await provider_for(credentials=SDK_ONLY)
        try:
            await provider.join_meeting(LINK)
            [join] = await join_calls(provider)
            assert "zak" not in join
        finally:
            await provider.shutdown()

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
            await provider.shutdown()

    async def test_join_error_reaches_the_room_page_in_zooms_words(self):
        provider = await provider_for(api=FakeApi())
        try:
            with pytest.raises(RuntimeError) as failure:
                await provider.join_meeting("https://zoom.us/j/999?pwd=x")
            assert "This meeting ID is not valid (code 3712)" in str(failure.value)
            assert provider.state == MeetingState.ERROR
            assert provider.current_meeting.error_message == str(failure.value)
        finally:
            await provider.shutdown()

    async def test_waiting_room_then_connected(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.waiting = true;")
        try:
            states = []
            provider.add_state_callback(states.append)
            await provider.join_meeting(LINK)
            assert states == [MeetingState.JOINING, MeetingState.IN_LOBBY, MeetingState.CONNECTED]
        finally:
            await provider.shutdown()

    async def test_never_connecting_times_out_with_the_pages_words(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.neverConnect = true;")
        provider.CONNECT_TIMEOUT_S = 1
        try:
            with pytest.raises(RuntimeError) as failure:
                await provider.join_meeting(LINK)
            assert "Zoom did not connect; the page says:" in str(failure.value)
            assert provider.state == MeetingState.ERROR
        finally:
            await provider.shutdown()

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
            await provider.shutdown()


class TestControls:
    async def test_mute_camera_and_leave(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(LINK)
            assert await provider.toggle_mute() is True and provider.current_meeting.is_muted is True
            assert await provider.toggle_mute() is False and provider.current_meeting.is_muted is False
            assert await provider.toggle_camera() is False and provider.current_meeting.is_camera_on is False
            assert await provider._page.get_attribute("#stub-video", "aria-label") == "Start Video"
            assert await provider.toggle_camera() is True
            await provider.leave_meeting()
            assert provider.state == MeetingState.IDLE and provider.current_meeting is None
            assert provider._page.url == "about:blank"
        finally:
            await provider.shutdown()

    async def test_camera_off_at_join_presses_the_video_button(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(LINK, camera_on=False)
            assert await provider._page.get_attribute("#stub-video", "aria-label") == "Start Video"
            assert provider.current_meeting.is_camera_on is False
        finally:
            await provider.shutdown()

    async def test_controls_need_a_meeting(self):
        provider = await provider_for(api=FakeApi())
        try:
            with pytest.raises(RuntimeError):
                await provider.toggle_mute()
            with pytest.raises(RuntimeError):
                await provider.toggle_camera()
        finally:
            await provider.shutdown()


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
            await provider.shutdown()


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
