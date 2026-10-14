"""Keeping the TV on, and turning it off from the table: the wake at start, before a join and after an unexpected off,
the hold that a manual off puts on all three, and the wait for a TV that does not answer. The brief's own tests of
this are in test_service.py. Here a wake that works brings the TV on (see helpers.service_for); `wake_works = False`
makes one that does nothing."""
import asyncio
import logging

import pytest
from croom.tv.errors import TvError
from tests.unit.tv.fake_client import FakeTvClient
from tests.unit.tv.helpers import (DoingTv, StandbyTv, WebOsTvCommandError, build_service, forgetful_tv, in_standby, quick_start_tv,
                                   running, service_for, silent_tv, until, within)

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
        began = service._rewake                           # (the look at the TV as the service started: it was on, nothing to wake)
        await service._drop_client()                      # as pairing again does: the client's goodbye is a cleared state
        await asyncio.sleep(0.2)
        assert service._rewake is began and service.woken == []


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
        began = service._rewake                           # (the look at the TV as the service started: it was on, nothing to wake)
        await service.control("power", "off")
        await asyncio.sleep(0.2)
        assert service.woken == [] and service._rewake is began


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


async def test_a_manual_on_of_a_tv_that_answers_but_stays_dark_says_so_in_words(tmp_path):
    """A TV in standby that is told to turn on and does nothing: the person at the table is told so, and is not shown a TV
    that is still off as if all were well."""
    service = service_for(tmp_path, in_standby, key="key-9", keep_on=False)
    service.wake_works, service.MANUAL_TURN_ON_GRACE_S = False, 0.1
    async with running(service) as client:
        with pytest.raises(TvError) as failure:
            await service.control("power", "on")
        assert "answered but did not turn on" in str(failure.value) and "10.0.0.41" in str(failure.value)
        assert service.woken == [MAC] and ("power_on",) in client.calls and service.status()["held_off"] is False
        await client.update(power_state={"state": "Active"})                  # and if it comes on after all, the table follows
        assert service.status()["on"] is True


async def test_a_manual_on_of_a_standby_tv_that_refuses_with_a_packet_that_does_nothing_says_so_in_words(tmp_path):
    """The case the review had answering `on: false` without a word: the TV answers on its socket, the command to turn on is
    refused, and the magic packet reaches nothing (Mobile TV On is off)."""
    service = service_for(tmp_path, quick_start_tv(refuses=True), key="key-9", keep_on=False)
    service.wake_works, service.MANUAL_TURN_ON_GRACE_S = False, 2.0
    async with running(service) as client:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with pytest.raises(TvError) as failure:
            await service.control("power", "on")
        # The hint is the power button alone: a TV that answers on its socket while it is dark has Quick Start+ already, so
        # "turn on Quick Start+" would send the person to a setting that is on
        assert str(failure.value) == "The TV at 10.0.0.41 answered but did not turn on: press its power button"
        assert loop.time() - t0 < 0.5                            # a refusal is no moment to wait out
        assert service.woken == [MAC] and ("power_on",) in client.calls
        assert service.status()["held_off"] is False             # the person did want it on


async def test_a_manual_on_that_the_tv_took_and_did_nothing_about_says_so_after_a_few_seconds(tmp_path):
    service = service_for(tmp_path, quick_start_tv(delay=None), key="key-9", keep_on=False)       # takes the command, stays dark
    service.wake_works, service.MANUAL_TURN_ON_GRACE_S = False, 0.3
    async with running(service):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with pytest.raises(TvError) as failure:
            await service.control("power", "on")
        assert "answered but did not turn on" in str(failure.value) and 0.25 <= loop.time() - t0 < 1.5


async def test_a_manual_on_is_given_a_few_seconds_for_the_tv_to_say_that_it_is_on(tmp_path):
    service = service_for(tmp_path, quick_start_tv(delay=0.3), key="key-9", keep_on=False)
    service.wake_works, service.MANUAL_TURN_ON_GRACE_S, service.TURN_ON_GRACE_S = False, 2.0, 0.05   # (an automatic wake would not wait)
    async with running(service):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        shown = await service.control("power", "on")
        assert shown["on"] is True and 0.25 <= loop.time() - t0 < 1.5


async def test_the_power_key_that_turns_a_standby_tv_on_says_so_in_words_too(tmp_path):
    service = service_for(tmp_path, quick_start_tv(refuses=True), key="key-9", keep_on=False)
    service.wake_works = False
    async with running(service):
        with pytest.raises(TvError) as failure:
            await service.control("key", "POWER")
        assert "answered but did not turn on" in str(failure.value)


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


async def test_a_join_wakes_a_tv_that_is_off_and_finds_it_on(tmp_path):
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
async def test_a_tv_this_table_cannot_talk_to_has_nothing_to_wait_for_and_the_join_is_told_so_quietly(tmp_path, case, caplog):
    caplog.set_level(logging.DEBUG, logger="croom.tv.service")
    if case == "not set up":
        service = service_for(tmp_path, enabled=False, host="", key="key-9")
    elif case == "not paired":
        service = service_for(tmp_path)
    elif case == "refused":
        service = service_for(tmp_path, forgetful_tv, key="key-9")
    else:
        service = service_for(tmp_path, silent_tv, key="key-9", keep_on=False)       # (nothing wakes it as the service starts)
    await service.start()
    try:
        if case == "stopped":
            await service.stop()
            service._config.keep_on = True                    # the join wants the TV; the service is not there to wake it
        caplog.clear()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await service.before_join() is True                # nothing to wait for: not a TV that did not come on
        assert loop.time() - t0 < 0.5 and service.woken == []
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert [r for r in caplog.records if r.levelno == logging.DEBUG and "before the join" in r.getMessage()]
    finally:
        await service.stop()


# The wait for the TV to answer

async def test_the_wait_ends_when_the_tv_answers_and_a_tv_that_answers_in_standby_is_told_to_turn_on(tmp_path):
    """The wake waits for the TV to answer: for a TV that was off that is the TV coming up. One that keeps its socket up in
    standby (Quick Start+) answers at once and is not on: it is told to turn on, as the remote would, and given a moment to
    say that it has."""
    service = service_for(tmp_path, in_standby, key="key-9", keep_on=False)       # (this one ignores the command)
    service.wake_works, service.WAKE_POLL_S, service.TURN_ON_GRACE_S = False, 0.02, 0.1
    async with running(service) as client:
        assert service.status()["on"] is False and service.status()["reachable"] is True
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await service.ensure_on(timeout=30) is True        # it answers: that ends the wait
        assert 0.1 <= loop.time() - t0 < 0.6 and service.woken == [MAC]
        assert ("power_on",) in client.calls                      # and it was told to turn on
        await client.update(power_state={"state": "Active"})      # and then it says that it has
        assert service.status()["on"] is True


# A TV in standby with its socket up (Quick Start+) is told to turn on: the magic packet is not all there is

async def test_a_tv_in_standby_is_told_to_turn_on_and_comes_on(tmp_path):
    service = service_for(tmp_path, quick_start_tv(), key="key-9", keep_on=False)
    service.wake_works = False                                    # (Mobile TV On is off: only its own socket can turn it on)
    async with running(service) as client:
        assert service.status()["on"] is False and service.status()["reachable"] is True
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await service.ensure_on(timeout=5) is True
        assert ("power_on",) in client.calls and service.status()["on"] is True
        assert loop.time() - t0 < 0.4                             # it did not wait out the moment it would give it


async def test_a_tv_that_says_it_is_on_a_moment_after_it_was_told_is_given_that_moment(tmp_path):
    service = service_for(tmp_path, quick_start_tv(delay=0.1), key="key-9", keep_on=False)
    service.wake_works, service.TURN_ON_GRACE_S = False, 0.5
    async with running(service):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await service.ensure_on(timeout=5) is True
        assert service.status()["on"] is True and 0.08 <= loop.time() - t0 < 0.45


async def test_a_tv_that_never_says_it_is_on_is_not_waited_for_beyond_that_moment(tmp_path):
    service = service_for(tmp_path, quick_start_tv(delay=None), key="key-9", keep_on=False)
    service.wake_works, service.TURN_ON_GRACE_S = False, 0.2
    async with running(service):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await service.ensure_on(timeout=5) is True         # it answered: the wait is over
        assert service.status()["on"] is False and 0.15 <= loop.time() - t0 < 0.8


async def test_a_tv_that_refuses_to_turn_on_is_logged_and_nothing_more_is_waited_for(tmp_path, caplog):
    service = service_for(tmp_path, quick_start_tv(refuses=True), key="key-9", keep_on=False)
    service.wake_works, service.TURN_ON_GRACE_S = False, 2.0
    async with running(service) as client:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await service.ensure_on(timeout=5) is True
        assert loop.time() - t0 < 0.5                             # a refusal is no moment to wait out
        assert ("power_on",) in client.calls and "did not accept power on" in caplog.text and "not now" in caplog.text


async def test_a_tv_that_does_not_answer_the_command_to_turn_on_is_not_waited_for_beyond_a_few_seconds(tmp_path, caplog):
    class Deaf(StandbyTv):
        async def power_on(self):
            await asyncio.sleep(30)
    service = service_for(tmp_path, Deaf, key="key-9", keep_on=False)
    service.wake_works, service.ASK_TIMEOUT_S = False, 0.2
    async with running(service):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await within(service.ensure_on(timeout=5), 5.0) is True
        assert 0.15 <= loop.time() - t0 < 1.5 and "did not accept power on" in caplog.text


async def test_a_join_goes_on_with_a_standby_tv_that_refuses_to_turn_on(tmp_path, caplog):
    """The table is told when its own Turn on finds the TV dark; a join is not stopped by it, and is not made to wait."""
    service = service_for(tmp_path, quick_start_tv(refuses=True), key="key-9", keep_on=False)
    service.wake_works, service.MANUAL_TURN_ON_GRACE_S = False, 2.0
    async with running(service):
        service._config.keep_on = True
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await service.before_join() is True                    # the TV answered: the join goes on
        assert loop.time() - t0 < 0.5 and "did not accept power on" in caplog.text


async def test_a_manual_on_turns_a_tv_in_standby_on(tmp_path):
    service = service_for(tmp_path, quick_start_tv(), key="key-9", keep_on=False)
    service.wake_works = False
    async with running(service) as client:
        shown = await service.control("power", "on")
        assert ("power_on",) in client.calls and shown["on"] is True and shown["held_off"] is False


async def test_a_join_turns_a_tv_in_standby_on(tmp_path):
    service = service_for(tmp_path, quick_start_tv(), key="key-9", keep_on=False)    # (nothing turns it on by itself)
    service.wake_works = False
    async with running(service) as client:
        service._config.keep_on = True                            # kept on, for the join to want it
        assert service.status()["on"] is False
        assert await service.before_join() is True
        assert ("power_on",) in client.calls and service.status()["on"] is True


async def test_a_tv_in_standby_as_the_agent_starts_is_turned_on_whether_or_not_the_magic_packet_works(tmp_path):
    service = service_for(tmp_path, quick_start_tv(), key="key-9")
    service.wake_works = False
    async with running(service):
        await until(lambda: service.status()["on"])


async def test_a_tv_that_goes_to_standby_by_itself_is_turned_on_again(tmp_path):
    service = service_for(tmp_path, quick_start_tv(), key="key-9")
    service.wake_works, service.REWAKE_AFTER_S = False, 0.02
    async with running(service) as client:
        await until(lambda: service.status()["on"])
        await client.update(power_state={"state": "Active Standby"})      # switched off with its own remote
        await until(lambda: client.calls.count(("power_on",)) == 2 and service.status()["on"])


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
        assert service.woken == [MAC] and ("power_on",) not in client.calls       # (a TV that is on is not told to turn on)


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


# The magic packet is sent again while the TV does not answer: one can be lost, or sent before the TV could hear it

async def test_the_magic_packet_is_sent_again_while_the_tv_does_not_answer(tmp_path):
    service = service_for(tmp_path, key="key-9", keep_on=False)
    service.wake_works, service.WAKE_RESEND_S, service.WAKE_POLL_S = False, 0.1, 0.02
    async with running(service) as client:
        client.refuse = True
        await client.drop()
        assert await service.ensure_on(timeout=0.55) is False
        assert 3 <= len(service.woken) <= 7 and set(service.woken) == {MAC}     # about one every 0.1 s, and the first at once


async def test_one_packet_serves_a_wait_that_is_shorter_than_the_resend_interval(tmp_path):
    service = service_for(tmp_path, key="key-9", keep_on=False)
    service.wake_works, service.WAKE_POLL_S = False, 0.02
    assert service.WAKE_RESEND_S == 3.0
    async with running(service) as client:
        client.refuse = True
        await client.drop()
        assert await service.ensure_on(timeout=0.4) is False
        assert service.woken == [MAC]


async def test_no_packet_is_sent_once_the_tv_answers(tmp_path):
    service = service_for(tmp_path, key="key-9", keep_on=False)
    service.wake_works, service.WAKE_RESEND_S, service.WAKE_POLL_S = False, 0.1, 0.02
    async with running(service) as client:
        client.refuse = True
        await client.drop()

        async def comes_up():
            await asyncio.sleep(0.25)
            client.tv_state.power_state, client.refuse = {"state": "Active"}, False
        coming = asyncio.ensure_future(comes_up())
        assert await service.ensure_on(timeout=3) is True
        sent = len(service.woken)
        assert 2 <= sent <= 4
        await asyncio.sleep(0.3)
        assert len(service.woken) == sent                         # nothing more once it was found
        await coming


async def test_a_packet_that_cannot_be_sent_again_does_not_end_the_wait(tmp_path, caplog):
    calls = []

    async def flaky(mac):
        calls.append(mac)
        if len(calls) > 1:
            raise TvError("The wake-up call could not be sent to AA:BB:CC:DD:EE:FF: Network is unreachable")
    service = build_service(tmp_path, waker=flaky)
    service.WAKE_RESEND_S, service.WAKE_POLL_S = 0.05, 0.02
    async with running(service) as client:
        client.refuse = True
        await client.drop()

        async def comes_up():
            await asyncio.sleep(0.3)
            client.tv_state.power_state, client.refuse = {"state": "Active"}, False
        coming = asyncio.ensure_future(comes_up())
        assert await service.ensure_on(timeout=3, manual=True) is True    # (a person's wake: the first packet went, the rest are a bonus)
        assert len(calls) >= 3 and "cannot be woken again" in caplog.text
        await coming


# A meeting is never joined onto a TV that shows another input

def on_input(input_id):
    """A factory for a TV that is on and shows this input."""
    def build(host, key):
        client = DoingTv(host, key)
        client.tv_state.current_input, client.tv_state.current_app_id = input_id, DoingTv.APPS.get(input_id)
        return client
    return build


async def test_a_join_switches_a_tv_that_shows_another_input_to_the_room_pis(tmp_path):
    service = service_for(tmp_path, on_input("HDMI_2"), key="key-9")                  # pi_input is HDMI_1
    async with running(service) as client:
        assert service.status()["input"] == "HDMI_2"
        assert await service.before_join() is True
        assert ("set_input", "HDMI_1") in client.calls and service.status()["input"] == "HDMI_1"


async def test_a_join_does_not_switch_the_input_of_a_tv_that_did_not_answer(tmp_path):
    service = service_for(tmp_path, on_input("HDMI_2"), key="key-9")
    service.wake_works, service.WAKE_TIMEOUT_S, service.WAKE_POLL_S = False, 0.2, 0.02
    async with running(service) as client:
        client.refuse = True
        await client.drop()                               # off, and nothing answers: what it showed is what is known
        assert await service.before_join() is False
        assert not [call for call in client.calls if call[0] == "set_input"]


async def test_a_tv_that_does_not_answer_the_switch_is_not_waited_for_beyond_a_few_seconds(tmp_path, caplog):
    def hanging(host, key):
        client = on_input("HDMI_2")(host, key)

        async def set_input(*args):
            await asyncio.sleep(30)
        client.set_input = set_input
        return client
    service = service_for(tmp_path, hanging, key="key-9")
    service.ASK_TIMEOUT_S = 0.2
    async with running(service):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await within(service.before_join(), 5.0) is True
        assert 0.15 <= loop.time() - t0 < 1.5 and "did not switch to the room Pi's input HDMI_1" in caplog.text


async def test_a_join_leaves_a_tv_that_already_shows_the_room_pis_input_alone(tmp_path):
    service = service_for(tmp_path, on_input("HDMI_1"), key="key-9")
    async with running(service) as client:
        assert await service.before_join() is True
        assert not [call for call in client.calls if call[0] == "set_input"]


@pytest.mark.parametrize("app", ["com.webos.app.livetv", "netflix"])
async def test_a_join_switches_a_tv_on_live_tv_or_an_app_to_the_room_pis_input(tmp_path, app):
    """Live TV and the apps are not the room Pi: a meeting is not joined onto them."""
    def showing_an_app(host, key):
        client = DoingTv(host, key)
        client.tv_state.current_input, client.tv_state.current_app_id = None, app
        return client
    service = service_for(tmp_path, showing_an_app, key="key-9")
    async with running(service) as client:
        assert service.status()["input"] is None and service.status()["app"] == app
        assert await service.before_join() is True
        assert ("set_input", "HDMI_1") in client.calls and service.status()["input"] == "HDMI_1"


async def test_a_join_leaves_the_input_alone_while_the_state_is_unknown(tmp_path):
    def saying_nothing(host, key):
        client = DoingTv(host, key)
        client.tv_state.current_input, client.tv_state.current_app_id = None, None
        return client
    service = service_for(tmp_path, saying_nothing, key="key-9")
    async with running(service) as client:
        assert service.status()["input"] is None and service.status()["app"] is None
        assert await service.before_join() is True
        assert not [call for call in client.calls if call[0] == "set_input"]


async def test_a_join_leaves_the_input_alone_when_the_room_pis_is_not_named(tmp_path):
    service = service_for(tmp_path, on_input("HDMI_2"), key="key-9", pi_input="")
    async with running(service) as client:
        assert await service.before_join() is True
        assert not [call for call in client.calls if call[0] == "set_input"]


async def test_a_join_does_not_touch_the_input_of_a_tv_that_is_not_to_be_kept_on(tmp_path):
    service = service_for(tmp_path, on_input("HDMI_2"), key="key-9", keep_on=False)
    async with running(service) as client:
        assert await service.before_join() is True
        assert not [call for call in client.calls if call[0] == "set_input"]


async def test_a_tv_that_refuses_the_switch_is_logged_and_the_join_goes_on(tmp_path, caplog):
    def refusing(host, key):
        client = on_input("HDMI_2")(host, key)

        async def set_input(*args):
            raise WebOsTvCommandError("Request failed with response {'payload': {'errorText': 'no such input'}}")
        client.set_input = set_input
        return client
    service = service_for(tmp_path, refusing, key="key-9")
    async with running(service):
        assert await service.before_join() is True
        assert "did not switch to the room Pi's input HDMI_1" in caplog.text and service.status()["input"] == "HDMI_2"


def dark_on_input(input_id):
    """A factory for a TV in standby (its socket up) that takes the command to turn on and stays dark, and last showed this input."""
    def build(host, key):
        client = StandbyTv(host, key)
        client.delay = None
        client.tv_state.current_input, client.tv_state.current_app_id = input_id, DoingTv.APPS.get(input_id)
        return client
    return build


async def test_a_join_leaves_the_input_alone_for_a_tv_that_answered_but_stayed_dark(tmp_path):
    """A TV that answers and does not come on is not switched: what it showed last is not on a screen. (It is the join that
    asks for the TV, so the TV is told to turn on; and it is the TV being on that the switch waits for.)"""
    service = service_for(tmp_path, dark_on_input("HDMI_2"), key="key-9", keep_on=False)
    service.wake_works, service.TURN_ON_GRACE_S = False, 0.05
    async with running(service) as client:
        service._config.keep_on = True
        assert service.status()["on"] is False and service.status()["input"] == "HDMI_2"
        assert await service.before_join() is True                        # it answered: the join goes on
        assert ("power_on",) in client.calls and service.status()["on"] is False
        assert not [call for call in client.calls if call[0] == "set_input"]


# When the TV's list of inputs cannot be matched to the app on the screen, the app's own name says whether it is the room Pi's

def unlisted(app):
    """A factory for a TV that is on with this app on its screen and a list of inputs that has no input for it."""
    def build(host, key):
        client = DoingTv(host, key)
        client.tv_state.inputs, client.tv_state.current_input, client.tv_state.current_app_id = {}, None, app
        return client
    return build


@pytest.mark.parametrize("pi_input, app", [("HDMI_1", "com.webos.app.hdmi1"), ("HDMI_2", "com.webos.app.hdmi2"),
                                           ("HDMI1", "com.webos.app.hdmi1")])
async def test_a_join_does_not_switch_a_tv_to_the_room_pi_when_the_app_it_runs_is_the_pis_input(tmp_path, pi_input, app):
    service = service_for(tmp_path, unlisted(app), key="key-9", pi_input=pi_input)
    async with running(service) as client:
        assert service.status()["input"] is None and service.status()["app"] == app       # the list names no input for the app
        assert await service.before_join() is True
        assert not [call for call in client.calls if call[0] == "set_input"]


@pytest.mark.parametrize("app", ["com.webos.app.hdmi2", "com.webos.app.livetv", "netflix", "com.webos.app.hdmi10"])
async def test_a_join_still_switches_a_tv_to_the_room_pi_when_the_app_it_runs_is_not_the_pis_input(tmp_path, app):
    service = service_for(tmp_path, unlisted(app), key="key-9")                       # pi_input is HDMI_1
    async with running(service) as client:
        assert await service.before_join() is True
        assert ("set_input", "HDMI_1") in client.calls


# A join waits no longer than the wait it is given for the TV: what comes after the TV has answered (the command to turn
# on, the moment to say that it has, the switch to the room Pi, the reads that follow it) is held to what is left of it

def answers_late(client, after, power_state):
    """A TV that does not answer, and answers `after` seconds from now with this power state."""
    async def comes_up():
        await asyncio.sleep(after)
        client.tv_state.power_state, client.refuse = power_state, False
    return asyncio.ensure_future(comes_up())


async def test_a_join_does_not_wait_for_a_slow_command_to_turn_on_past_the_time_it_has(tmp_path, caplog):
    class Slow(StandbyTv):
        async def power_on(self):
            await asyncio.sleep(30)
    service = service_for(tmp_path, Slow, key="key-9", keep_on=False)
    service.wake_works, service.WAKE_TIMEOUT_S, service.WAKE_POLL_S = False, 0.5, 0.02       # (the command may take ASK_TIMEOUT_S: 3 s)
    async with running(service) as client:
        client.refuse = True
        await client.drop()
        service._config.keep_on = True
        coming = answers_late(client, 0.3, {"state": "Active Standby"})                      # it answers with 0.2 s of the 0.5 left
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await within(service.before_join(), 5.0) is True                              # and it did answer
        assert 0.4 <= loop.time() - t0 < 0.8 and "did not accept power on" in caplog.text
        await coming


async def test_a_join_does_not_wait_for_the_moment_a_tv_is_given_to_say_it_is_on_past_the_time_it_has(tmp_path):
    service = service_for(tmp_path, quick_start_tv(delay=None), key="key-9", keep_on=False)      # takes the command, stays dark
    service.wake_works, service.WAKE_TIMEOUT_S, service.WAKE_POLL_S, service.TURN_ON_GRACE_S = False, 0.5, 0.02, 5.0
    async with running(service) as client:
        client.refuse = True
        await client.drop()
        service._config.keep_on = True
        coming = answers_late(client, 0.3, {"state": "Active Standby"})
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await within(service.before_join(), 8.0) is True
        assert 0.4 <= loop.time() - t0 < 0.8 and ("power_on",) in client.calls
        await coming


async def test_a_wake_that_a_person_asked_for_does_not_wait_for_the_tv_to_say_it_is_on_past_its_wait_either(tmp_path):
    service = service_for(tmp_path, quick_start_tv(delay=None), key="key-9", keep_on=False)
    service.wake_works, service.WAKE_POLL_S, service.MANUAL_TURN_ON_GRACE_S = False, 0.02, 5.0
    async with running(service) as client:
        client.refuse = True
        await client.drop()
        coming = answers_late(client, 0.3, {"state": "Active Standby"})
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await within(service.ensure_on(timeout=0.5, manual=True), 8.0) is True
        assert 0.4 <= loop.time() - t0 < 0.8
        await coming


async def test_a_join_does_not_wait_for_a_slow_switch_to_the_room_pi_past_the_time_it_has(tmp_path, caplog):
    def hanging(host, key):
        client = on_input("HDMI_2")(host, key)

        async def set_input(*args):
            await asyncio.sleep(30)
        client.set_input = set_input
        return client
    service = service_for(tmp_path, hanging, key="key-9")
    service.wake_works, service.WAKE_TIMEOUT_S, service.WAKE_POLL_S = False, 0.5, 0.02
    async with running(service) as client:
        client.refuse = True
        await client.drop()
        coming = answers_late(client, 0.3, {"state": "Active"})                              # on, still showing HDMI_2
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await within(service.before_join(), 5.0) is True
        assert 0.4 <= loop.time() - t0 < 0.8 and "did not switch to the room Pi's input HDMI_1" in caplog.text
        await coming


async def test_a_join_does_not_wait_for_the_reads_after_the_switch_past_the_time_it_has(tmp_path):
    class Slow(DoingTv):
        async def get_volume(self):
            await asyncio.sleep(30)

        async def get_muted(self):
            await asyncio.sleep(30)

        async def get_input(self):
            await asyncio.sleep(30)
    service = service_for(tmp_path, Slow, key="key-9")
    service.wake_works, service.WAKE_TIMEOUT_S, service.WAKE_POLL_S = False, 0.5, 0.02
    async with running(service) as client:
        client.tv_state.current_input, client.tv_state.current_app_id = "HDMI_2", DoingTv.APPS["HDMI_2"]
        await client.update(power_state={"state": "Active"})
        client.refuse = True
        await client.drop()
        coming = answers_late(client, 0.3, {"state": "Active"})
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await within(service.before_join(), 5.0) is True
        assert ("set_input", "HDMI_1") in client.calls                                       # the switch went out; the reads are what is cut
        assert 0.4 <= loop.time() - t0 < 0.8                                                 # (REREAD_TIMEOUT_S: 2 s)
        await coming


async def test_nothing_after_the_wait_is_asked_of_the_tv_when_no_time_is_left(tmp_path, caplog):
    """The steps that follow the TV's answer are not started at all when the wait is over, rather than started and cut (which
    is a command the TV never got, logged as one it did not accept)."""
    service = service_for(tmp_path, on_input("HDMI_2"), key="key-9")
    caplog.set_level(logging.DEBUG, logger="croom.tv.service")
    async with running(service) as client:
        caplog.clear()
        over = asyncio.get_running_loop().time() - 1.0
        await service._turn_on(False, over)
        await service._show_the_pi(over)
        await service._read_back(client, "input", over)
        assert not [call for call in client.calls if call[0] in ("power_on", "set_input", "get_volume", "get_muted", "get_input")]
        assert [record.getMessage() for record in caplog.records] == []


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
