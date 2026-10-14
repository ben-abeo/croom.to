"""The TV service and the meeting service together, as the agent wires them: a join waits for the TV to wake, for no longer
than the cap, and then goes on."""
import asyncio

from croom.core.config import Config
from croom.meeting.providers.base import MeetingInfo, MeetingState
from croom.meeting.service import MeetingService
from tests.unit.tv.helpers import running, service_for, until, within

LINK = "https://zoom.us/j/98765432100"


class WatchingProvider:
    """A provider that notes whether the TV was on at the moment the meeting service asked it to join."""

    def __init__(self, tv):
        self.state = MeetingState.IDLE
        self.tv, self.tv_on_at_join = tv, []

    def add_state_callback(self, callback):
        pass

    async def join_meeting(self, meeting_url, display_name="Conference Room", camera_on=True, mic_on=True, passcode=None):
        self.tv_on_at_join.append(self.tv.status()["on"])
        return MeetingInfo(platform="zoom", meeting_id="98765432100", meeting_url=meeting_url)


def meeting_for(tv):
    meeting = MeetingService(Config())
    provider = meeting._providers["zoom"] = WatchingProvider(tv)
    meeting.add_before_join(tv.before_join)
    return meeting, provider


async def test_a_join_waits_for_the_tv_to_come_up_and_then_joins(tmp_path):
    tv = service_for(tmp_path, key="key-9")
    tv.wake_works, tv.WAKE_POLL_S, tv.REWAKE_AFTER_S = False, 0.02, 60     # the TV comes up when the test says so
    meeting, provider = meeting_for(tv)
    async with running(tv) as client:
        client.refuse = True
        await client.drop()                                                # it went off while the room was idle: nothing answers
        joining = asyncio.ensure_future(meeting.join_meeting(LINK))
        await until(lambda: tv.woken)
        await asyncio.sleep(0.1)
        assert not joining.done() and meeting.state == MeetingState.JOINING and provider.tv_on_at_join == []
        client.tv_state.power_state, client.refuse = {"state": "Active"}, False       # and it has come up
        await within(joining)
        assert provider.tv_on_at_join == [True] and tv.woken == ["AA:BB:CC:DD:EE:FF"]


async def test_a_join_goes_on_without_a_tv_that_never_wakes_after_the_cap(tmp_path):
    tv = service_for(tmp_path, key="key-9")
    tv.wake_works, tv.WAKE_TIMEOUT_S, tv.WAKE_POLL_S, tv.REWAKE_AFTER_S = False, 0.3, 0.05, 60
    meeting, provider = meeting_for(tv)
    async with running(tv) as client:
        await client.update(power_state={"state": "Off"})
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        info = await meeting.join_meeting(LINK)
        assert loop.time() - t0 < 1.5                     # waited for the cap, not for ever
        assert provider.tv_on_at_join == [False] and info.meeting_id == "98765432100"
        assert tv.woken == ["AA:BB:CC:DD:EE:FF"] and tv.status()["on"] is False


async def test_a_join_of_a_room_whose_tv_is_on_does_not_wait(tmp_path):
    tv = service_for(tmp_path, key="key-9")
    meeting, provider = meeting_for(tv)
    async with running(tv):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await meeting.join_meeting(LINK)
        assert loop.time() - t0 < 0.5 and provider.tv_on_at_join == [True] and tv.woken == []
