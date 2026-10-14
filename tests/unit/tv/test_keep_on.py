"""Keeping the TV on, and turning it off from the table: the wake at start, before a join and after an unexpected off,
the hold that a manual off puts on all three, and the wait for a TV that does not answer. The brief's own tests of
this are in test_service.py. Here a wake that works brings the TV on (see helpers.service_for); `wake_works = False`
makes one that does nothing."""
import asyncio

import pytest
from croom.tv.errors import TvError
from tests.unit.tv.fake_client import FakeTvClient
from tests.unit.tv.helpers import build_service, forgetful_tv, in_standby, running, service_for, silent_tv, until, within

MAC = "AA:BB:CC:DD:EE:FF"


# Waking the TV as the agent starts

async def test_a_tv_that_is_off_as_the_agent_starts_is_woken(tmp_path):
    service = service_for(tmp_path, in_standby, key="key-9")
    async with running(service):
        await until(lambda: service.woken)
        assert service.woken == [MAC]
        await until(lambda: service.status()["on"])       # it came on, and the table knows


async def test_a_tv_that_does_not_answer_as_the_agent_starts_is_woken_too(tmp_path):
    FakeTvClient.refuse_next = True                       # nothing answers: the TV is off, with no socket
    service = service_for(tmp_path, key="key-9")
    async with running(service):
        await until(lambda: service.woken)
        assert service.woken == [MAC]
        await until(lambda: service.status()["on"])


async def test_a_tv_that_is_on_as_the_agent_starts_is_left_alone(tmp_path):
    service = service_for(tmp_path, key="key-9")
    async with running(service):
        await asyncio.sleep(0.1)
        assert service.woken == []


async def test_a_tv_that_is_not_to_be_kept_on_is_not_woken_at_start(tmp_path):
    service = service_for(tmp_path, in_standby, key="key-9", keep_on=False)
    async with running(service):
        await asyncio.sleep(0.15)
        assert service.woken == []


# Waking it after it went off by itself, or by its own remote

async def test_a_tv_that_is_not_to_be_kept_on_is_not_woken_after_an_unexpected_off(tmp_path):
    service = service_for(tmp_path, key="key-9", keep_on=False)
    service.REWAKE_AFTER_S = 0.02
    async with running(service) as client:
        await client.update(power_state={"state": "Off"})
        await asyncio.sleep(0.2)
        assert service.woken == []


async def test_a_tv_that_is_back_before_the_wake_is_due_is_not_woken(tmp_path):
    service = service_for(tmp_path, key="key-9")
    service.REWAKE_AFTER_S = 0.15
    async with running(service) as client:
        await client.update(power_state={"state": "Off"})
        await client.update(power_state={"state": "Active"})      # someone switched it back on with the remote
        await asyncio.sleep(0.4)
        assert service.woken == []


async def test_one_wake_waits_at_a_time(tmp_path):
    service = service_for(tmp_path, key="key-9")
    service.REWAKE_AFTER_S = 60
    async with running(service) as client:
        await client.update(power_state={"state": "Off"})
        first = service._rewake
        assert first is not None and not first.done()
        await client.update(power_state={"state": "Active"})
        await client.update(power_state={"state": "Off"})
        assert service._rewake is first                   # the one that is waiting still is


async def test_each_unexpected_off_is_woken_once_the_last_wake_is_over(tmp_path):
    service = service_for(tmp_path, key="key-9")
    service.REWAKE_AFTER_S = 0.02
    async with running(service) as client:
        await client.update(power_state={"state": "Off"})
        await until(lambda: len(service.woken) == 1 and service.status()["on"] and service._rewake.done())
        await client.update(power_state={"state": "Off"})
        await until(lambda: len(service.woken) == 2)


async def test_a_state_pushed_while_the_tv_is_still_off_is_no_new_off(tmp_path):
    service = service_for(tmp_path, key="key-9")
    service.wake_works, service.REWAKE_AFTER_S, service.WAKE_TIMEOUT_S = False, 0.02, 0.05
    async with running(service) as client:
        await client.update(power_state={"state": "Off"})
        await until(lambda: service.woken and service._rewake.done())       # woken once, and it did not come on
        await client.update(volume=30)                    # the TV says something else while it is off
        await client.update(power_state={"state": "Off"})
        await asyncio.sleep(0.2)
        assert service.woken == [MAC]                     # it did not go off again: it never came on


async def test_a_manual_off_while_a_wake_waits_ends_the_wait(tmp_path):
    service = service_for(tmp_path, key="key-9")
    service.REWAKE_AFTER_S = 0.2
    async with running(service) as client:
        await client.update(power_state={"state": "Off"})         # off by itself: a wake is due in a moment
        waiting = service._rewake
        await service.control("power", "off")                      # and now someone at the table says off
        await asyncio.sleep(0)
        assert waiting.cancelled()                        # the wait is over, not merely harmless when it ends
        await asyncio.sleep(0.4)
        assert service.woken == []


async def test_a_manual_on_takes_over_from_a_wake_that_waits(tmp_path):
    service = service_for(tmp_path, key="key-9")
    service.wake_works, service.REWAKE_AFTER_S, service.WAKE_TIMEOUT_S, service.WAKE_POLL_S = False, 0.1, 0.4, 0.02
    async with running(service) as client:
        client.refuse = True
        await client.drop()                                       # off, and nothing answers: a wake is due in 0.1 s ...
        with pytest.raises(TvError):
            await service.control("power", "on")                   # ... and the person's own, which waits 0.4 s, is the only one
        assert service.woken == [MAC]


@pytest.mark.parametrize("how", ["stop", "forget"])
async def test_stop_and_forget_end_a_wake_that_is_waiting(tmp_path, how):
    service = service_for(tmp_path, key="key-9")
    service.REWAKE_AFTER_S = 0.1
    await service.start()
    try:
        await FakeTvClient.instances[-1].update(power_state={"state": "Off"})
        task = service._rewake
        assert task is not None and not task.done()
        await getattr(service, how)()
        assert task.done() and service._rewake is None        # ended, not merely asked to
        await asyncio.sleep(0.25)
        assert service.woken == []
    finally:
        await service.stop()


async def test_the_goodbye_the_service_says_does_not_wake_the_tv(tmp_path):
    service = service_for(tmp_path, key="key-9")
    service.REWAKE_AFTER_S = 0.02
    async with running(service):
        await service._drop_client()                      # as pairing again does: the client's goodbye is a cleared state
        await asyncio.sleep(0.2)
        assert service._rewake is None and service.woken == []


# Turning the TV off and on from the table

async def test_power_off_answers_with_the_tv_off_and_held(tmp_path):
    service = service_for(tmp_path, key="key-9")
    async with running(service):
        shown = await service.control("power", "off")
        assert shown["on"] is False and shown["held_off"] is True and shown == service.status()


async def test_power_off_of_a_tv_that_is_already_in_standby_still_holds_it(tmp_path):
    service = service_for(tmp_path, in_standby, key="key-9", keep_on=False)
    async with running(service) as client:
        assert (await service.control("power", "off"))["held_off"] is True
        assert ("power_off",) in client.calls             # (the library's power_off does nothing for a TV that is not on)


async def test_a_power_off_the_tv_did_not_get_leaves_no_hold(tmp_path):
    class Deaf(FakeTvClient):
        async def power_off(self):
            raise ConnectionError("Response failed {'payload': {'returnValue': False, 'errorText': 'the TV is busy'}}")
    service = service_for(tmp_path, Deaf, key="key-9")
    async with running(service):
        with pytest.raises(TvError) as failure:
            await service.control("power", "off")
        assert str(failure.value) == "The TV did not accept that: the TV is busy"
        shown = service.status()
        assert shown["held_off"] is False and shown["on"] is True


async def test_the_hold_is_in_place_before_the_tv_can_say_it_is_off(tmp_path):
    """A TV may tell that it is off while the command that switched it off is still being answered: that is the table's
    own doing, and must not be woken again."""
    class Quick(FakeTvClient):
        async def power_off(self):
            await super().power_off()
            await self.update(power_state={"state": "Power Off"})
    service = service_for(tmp_path, Quick, key="key-9")
    service.REWAKE_AFTER_S = 0.02
    async with running(service):
        await service.control("power", "off")
        await asyncio.sleep(0.2)
        assert service.woken == [] and service._rewake is None


async def test_the_power_key_is_a_power_control(tmp_path):
    """The remote's POWER key switches the TV: from the table that is the table turning it off or on, and it holds."""
    service = service_for(tmp_path, key="key-9")
    async with running(service) as client:
        await service.control("key", "POWER")             # the TV is on: it goes off
        assert ("power_off",) in client.calls and not [call for call in client.calls if call[0] == "button"]
        assert service.status()["held_off"] is True
        await client.update(power_state={"state": "Off"})
        await service.control("key", "power")             # it is off: it comes on
        assert service.woken == [MAC] and service.status()["held_off"] is False


async def test_a_wake_a_person_asked_for_goes_ahead_of_the_hold_and_leaves_it_to_the_control_to_release(tmp_path):
    service = service_for(tmp_path, key="key-9", keep_on=False)
    async with running(service) as client:
        await service.control("power", "off")             # held off
        await client.update(power_state={"state": "Off"})
        assert await service.ensure_on(timeout=0.2) is False and service.woken == []                 # not by itself ...
        assert await service.ensure_on(timeout=0.2, manual=True) is True and service.woken == [MAC]   # ... but at a person's word
        assert service.status()["held_off"] is True       # (the control's "on" is what lets go of the hold)


async def test_a_manual_on_does_not_need_keep_on(tmp_path):
    service = service_for(tmp_path, key="key-9", keep_on=False)
    async with running(service) as client:
        await service.control("power", "off")
        await client.update(power_state={"state": "Off"})
        await service.control("power", "on")
        assert service.woken == [MAC] and service.status()["held_off"] is False


async def test_a_manual_on_of_a_tv_that_is_on_wakes_nothing(tmp_path):
    service = service_for(tmp_path, key="key-9")
    async with running(service):
        assert (await service.control("power", "on"))["on"] is True
        assert service.woken == []


async def test_a_manual_on_that_never_brings_the_tv_up_says_so_in_words(tmp_path):
    service = service_for(tmp_path, key="key-9")
    service.wake_works, service.WAKE_TIMEOUT_S, service.WAKE_POLL_S = False, 0.2, 0.02
    async with running(service) as client:
        client.refuse = True
        await client.drop()                               # off, and nothing answers, however it is woken
        with pytest.raises(TvError) as failure:
            await service.control("power", "on")
        assert "did not turn on" in str(failure.value) and "10.0.0.41" in str(failure.value) and "Wake-on-LAN" in str(failure.value)
        assert service.woken == [MAC] and service.status()["held_off"] is False       # the person did want it on


async def test_a_manual_on_of_a_tv_that_answers_but_is_not_on_yet_answers_with_what_it_says(tmp_path):
    """A TV that keeps its socket up in standby (Quick Start+) answers at once. It is not an error that it has not come on
    in that moment: the table follows what the TV says next."""
    service = service_for(tmp_path, in_standby, key="key-9", keep_on=False)
    service.wake_works = False
    async with running(service) as client:
        shown = await service.control("power", "on")
        assert service.woken == [MAC] and shown["on"] is False and shown["reachable"] is True
        await client.update(power_state={"state": "Active"})
        assert service.status()["on"] is True


async def test_a_manual_on_of_a_tv_that_is_out_of_reach_wakes_it(tmp_path):
    FakeTvClient.refuse_next = True
    service = service_for(tmp_path, key="key-9", keep_on=False)       # (nothing wakes it by itself)
    async with running(service):
        assert service.status()["reachable"] is False
        shown = await service.control("power", "on")
        assert service.woken == [MAC] and shown["on"] is True and shown["reachable"] is True


async def test_a_wake_waits_no_longer_than_it_is_told_to(tmp_path):
    service = service_for(tmp_path, key="key-9")
    service.wake_works, service.WAKE_TIMEOUT_S, service.WAKE_POLL_S = False, 30.0, 5.0       # (a look is due every five seconds)
    async with running(service) as client:
        client.refuse = True
        await client.drop()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await service.ensure_on(timeout=0.2) is False
        assert 0.15 <= loop.time() - t0 < 1.0             # its own limit, not the service's


async def test_a_wake_that_breaks_is_logged_and_the_next_off_is_woken_all_the_same(tmp_path, caplog):
    calls = []

    async def breaks_once(mac):
        calls.append(mac)
        if len(calls) == 1:
            raise RuntimeError("the network stack choked")
    service = build_service(tmp_path, waker=breaks_once)
    service.REWAKE_AFTER_S, service.WAKE_TIMEOUT_S, service.WAKE_POLL_S = 0.02, 0.05, 0.01
    async with running(service) as client:
        await client.update(power_state={"state": "Off"})
        await until(lambda: calls and service._rewake.done())
        assert "waking the TV failed" in caplog.text and "the network stack choked" in caplog.text
        await client.update(power_state={"state": "Active"})
        await client.update(power_state={"state": "Off"})
        await until(lambda: len(calls) == 2)


async def test_a_wake_that_cannot_be_sent_is_words_for_the_table_and_no_wait_for_a_join(tmp_path, caplog):
    async def unable(mac):
        raise TvError("The TV's MAC address is not set (tv.mac in the room's config), so the TV cannot be turned on from the table")
    service = build_service(tmp_path, waker=unable, mac="")
    service.WAKE_TIMEOUT_S = 3.0                          # (what a join would wait if it waited at all)
    async with running(service) as client:
        client.refuse = True
        await client.drop()                               # off, and nothing answers
        with pytest.raises(TvError) as failure:
            await service.control("power", "on")
        assert "The TV's MAC address is not set" in str(failure.value)    # the waker's own words, not a general one
        assert service.status()["held_off"] is False
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await service.before_join() is False
        assert loop.time() - t0 < 0.5                     # a join does not wait for a wake that cannot be sent
        assert "cannot be woken" in caplog.text and "MAC address" in caplog.text


# Before a join

async def test_a_join_does_not_wait_for_a_tv_that_is_on(tmp_path):
    service = service_for(tmp_path, key="key-9")
    async with running(service):
        assert await service.before_join() is True and service.woken == []


async def test_a_join_wakes_a_tv_that_is_off_and_waits_until_it_is_on(tmp_path):
    service = service_for(tmp_path, key="key-9")
    async with running(service) as client:
        await client.update(power_state={"state": "Active Standby"})    # off, and it is not yet time for the wake that would follow
        assert await service.before_join() is True and service.woken == [MAC] and service.status()["on"] is True


async def test_a_join_changes_nothing_when_the_tv_is_not_to_be_kept_on(tmp_path):
    service = service_for(tmp_path, key="key-9", keep_on=False)
    async with running(service) as client:
        await service.control("power", "off")
        await client.update(power_state={"state": "Off"})
        assert await service.before_join() is True
        assert service.woken == [] and service.status()["held_off"] is True        # the hold is for a service that keeps the TV on


@pytest.mark.parametrize("case", ["not set up", "not paired", "refused", "stopped"])
async def test_a_tv_this_table_cannot_talk_to_makes_no_join_wait(tmp_path, case):
    if case == "not set up":
        service = service_for(tmp_path, enabled=False, host="", key="key-9")
    elif case == "not paired":
        service = service_for(tmp_path)
    elif case == "refused":
        service = service_for(tmp_path, forgetful_tv, key="key-9")
    else:
        service = service_for(tmp_path, silent_tv, key="key-9")
    await service.start()
    try:
        if case == "stopped":
            await service.stop()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await service.before_join() is False
        assert loop.time() - t0 < 0.5 and service.woken == []
    finally:
        await service.stop()


# The wait for the TV to answer

async def test_the_wait_ends_when_the_tv_answers(tmp_path):
    """The wake waits for the TV to answer: for a TV that was off that is the TV coming up. One that keeps its socket up in
    standby (Quick Start+) answers at once, and is on a moment later: it is not waited for, the table follows what it says."""
    service = service_for(tmp_path, in_standby, key="key-9", keep_on=False)
    service.wake_works, service.WAKE_POLL_S = False, 0.02
    async with running(service) as client:
        assert service.status()["on"] is False and service.status()["reachable"] is True
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await service.ensure_on(timeout=30) is False       # on, it is not yet
        assert loop.time() - t0 < 0.5 and service.woken == [MAC]
        await client.update(power_state={"state": "Active"})      # and then it says that it is
        assert service.status()["on"] is True


async def test_a_tv_that_comes_up_during_the_wait_is_found_on(tmp_path):
    service = service_for(tmp_path, key="key-9", keep_on=False)
    service.wake_works, service.WAKE_POLL_S = False, 0.02
    async with running(service) as client:
        client.refuse = True
        await client.drop()
        assert service.status()["reachable"] is False

        async def comes_up():
            await asyncio.sleep(0.15)
            client.tv_state.power_state, client.refuse = {"state": "Active"}, False
        coming = asyncio.ensure_future(comes_up())
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await service.ensure_on(timeout=2.0) is True
        assert 0.1 <= loop.time() - t0 < 0.8              # when it answered, and not a poll or two later
        await coming


async def test_a_tv_whose_connection_went_quiet_is_not_taken_for_on(tmp_path):
    """The service says on only for a TV it can still reach: a link that is gone, and has not yet been noticed, is a TV to
    ask after, not one that is on."""
    service = service_for(tmp_path, key="key-9")
    async with running(service) as client:
        assert service.status()["on"] is True
        client.connected = False                          # the link is gone; the state the TV last pushed still says Active
        assert service.status()["on"] is False
        assert await service.ensure_on(timeout=2.0) is True       # a wake, and a look: the TV answers, and is on
        assert service.woken == [MAC]


async def test_a_tv_whose_network_dropped_is_found_again_by_the_wake(tmp_path):
    """A drop of the TV's network reaches the service as a cleared state, the same as the TV going off: the TV may be on
    all the time. The wake asks, then looks, and finding the TV answering again is success."""
    service = service_for(tmp_path, key="key-9", keep_on=False)
    service.wake_works, service.WAKE_POLL_S = False, 0.02
    async with running(service) as client:
        client.refuse = True
        await client.drop()
        assert service.status()["on"] is False and service.status()["reachable"] is False

        async def network_is_back():
            await asyncio.sleep(0.1)
            client.tv_state.power_state, client.refuse = {"state": "Active"}, False
        back = asyncio.ensure_future(network_is_back())
        assert await service.ensure_on(timeout=2.0) is True
        assert service.woken == [MAC] and service.status()["on"] is True
        await back


async def test_stop_ends_a_wait_for_the_tv_and_leaves_nothing_connected(tmp_path):
    service = service_for(tmp_path, silent_tv, key="key-9", keep_on=False)
    service.wake_works, service.WAKE_POLL_S = False, 0.02
    await service.start()
    built = len(FakeTvClient.instances)
    waiting = asyncio.ensure_future(service.ensure_on(timeout=30))
    await until(lambda: service.woken)
    await service.stop()
    assert await within(waiting, 1.0) is False
    assert service._client is None and len(FakeTvClient.instances) == built       # no new client after stop
