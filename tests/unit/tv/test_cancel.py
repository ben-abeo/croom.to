"""A try at the TV that is cancelled half way (a join given up on, a wake that a manual on takes over, a request that was
dropped) must leave the service able to try again: aiowebostv's client, cancelled inside connect(), keeps a cancelled future
until its connect task has ended, and every connect() on it raises CancelledError until then (LibraryLike, in helpers).
Also here: a try is bounded by the time that is left, a client the service has let go of says nothing that counts, and
starting the service does not hold the agent up."""
import asyncio

import pytest
from croom.tv.errors import TvError
from tests.unit.tv.fake_client import FakeTvClient
from tests.unit.tv.helpers import LibraryLike, connects, service_for, until, within

MAC = "AA:BB:CC:DD:EE:FF"


async def test_a_manual_on_while_a_wake_is_mid_attempt_is_answered_in_words_and_not_by_a_stray_cancellation(tmp_path, gate):
    service = service_for(tmp_path, LibraryLike, key="key-9")
    service.wake_works, service.WAKE_POLL_S, service.WAKE_TIMEOUT_S = False, 0.02, 0.3
    service._started = True
    service._wake_after(0)                                    # a wake of the service's own, whose attempt at the TV hangs
    await until(lambda: FakeTvClient.instances and connects(FakeTvClient.instances[-1]) >= 1)
    asyncio.get_running_loop().call_later(0.1, gate.set)      # the attempt ends a moment later
    try:
        with pytest.raises(TvError):                          # in words, and not a CancelledError nobody asked for
            await service.control("power", "on")
    finally:
        service._started = False
        await service._drop_client()


async def test_the_connection_loop_outlives_an_attempt_that_was_cancelled(tmp_path, gate):
    service = service_for(tmp_path, LibraryLike, key="key-9")
    service._started = True
    attempt = asyncio.ensure_future(service._connect_once())  # a join given up on, a manual on: an attempt cancelled mid-connect
    await until(lambda: FakeTvClient.instances and connects(FakeTvClient.instances[-1]) >= 1)
    attempt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attempt
    service._run_loop()                                       # the loop's own next attempt
    await asyncio.sleep(0.2)
    try:
        assert not service._task.done(), "the connection loop died on the client the cancelled attempt left behind"
    finally:
        service._started = False
        await service._halt_loop()
        await service._drop_client()


async def test_an_attempt_that_is_cancelled_hangs_up_on_its_client_and_lets_the_next_one_start_afresh(tmp_path, gate):
    service = service_for(tmp_path, LibraryLike, key="key-9")
    attempt = asyncio.ensure_future(service._connect_once())
    await until(lambda: FakeTvClient.instances and connects(FakeTvClient.instances[-1]) >= 1)
    first = FakeTvClient.instances[-1]
    attempt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attempt
    assert ("disconnect",) in first.calls and service._client is None    # hung up on, and not kept
    assert not service._attempt.locked()                                  # and nobody is left holding the way to the TV
    gate.set()
    assert await service._connect_once() is False                         # the next try: a new client, which answers as it does
    assert len(FakeTvClient.instances) == 2 and FakeTvClient.instances[-1] is not first


async def test_a_second_cancellation_does_not_leave_the_hang_up_half_done(tmp_path, gate):
    """The one that cancels may be impatient and cancel again while the service is hanging up on the client: the hang-up
    is not the canceller's to cut short."""
    class SlowToHangUp(LibraryLike):
        closed = False

        async def disconnect(self):
            await asyncio.sleep(0.2)
            await super().disconnect()
            SlowToHangUp.closed = True
    service = service_for(tmp_path, SlowToHangUp, key="key-9")
    attempt = asyncio.ensure_future(service._connect_once())
    await until(lambda: FakeTvClient.instances and connects(FakeTvClient.instances[-1]) >= 1)
    attempt.cancel()
    await asyncio.sleep(0.05)                                 # the service is hanging up ...
    attempt.cancel()                                          # ... and is cancelled again
    with pytest.raises(asyncio.CancelledError):
        await attempt
    await until(lambda: SlowToHangUp.closed)                  # the hang-up was finished all the same
    assert service._client is None and not service._attempt.locked()


async def test_a_join_given_up_on_does_not_stop_the_one_that_follows(tmp_path, gate):
    """A Leave during the wait for the TV, and a Join tapped at once: the second must not die of what the first left."""
    service = service_for(tmp_path, LibraryLike, key="key-9")
    service.wake_works, service.WAKE_POLL_S, service.WAKE_TIMEOUT_S = False, 0.02, 0.3
    service._started = True
    first = asyncio.ensure_future(service.before_join())
    await until(lambda: FakeTvClient.instances and connects(FakeTvClient.instances[-1]) >= 1)
    first.cancel()                                            # given up on, mid-attempt
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    try:
        assert await within(service.before_join(), 3.0) is False   # no TV: but an answer, and not a stray cancellation
        assert len(service.woken) >= 2
    finally:
        service._started = False
        await service._drop_client()


@pytest.mark.parametrize("how", ["stop", "forget"])
async def test_stop_or_forget_during_a_wake_does_not_end_a_wait_queued_behind_it_with_a_stray_cancellation(tmp_path, gate, how):
    service = service_for(tmp_path, LibraryLike, key="key-9")
    service.wake_works, service.WAKE_POLL_S, service.WAKE_TIMEOUT_S, service.REWAKE_AFTER_S = False, 0.02, 0.3, 60
    service._started = True
    service._wake_after(0)                                    # a wake whose attempt hangs on the TV ...
    await until(lambda: FakeTvClient.instances and connects(FakeTvClient.instances[-1]) >= 1)
    joining = asyncio.ensure_future(service.before_join())    # ... and a join that queues behind it for the way to the TV
    await asyncio.sleep(0.05)
    await within(getattr(service, how)(), 5.0)                # ends the wake, mid-attempt
    try:
        assert await within(joining, 3.0) is False            # the join's wait ends in an answer: stopped, or the TV forgotten
        assert service._client is None and not service._attempt.locked()
    finally:
        service._started = False
        await service._drop_client()


async def test_an_attempt_is_bounded_by_the_time_that_is_left(tmp_path, gate):
    """The library's connect can take two seconds for a TV that does not answer, and ten for one that is slow to say hello:
    a wake that was told to wait 0.3 s waits 0.3 s, whatever the attempt in flight is doing."""
    service = service_for(tmp_path, LibraryLike, key="key-9")
    service.wake_works, service.WAKE_POLL_S = False, 0.02
    service._started = True
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    try:
        assert await within(service.ensure_on(timeout=0.3), 5.0) is False
        assert 0.25 <= loop.time() - t0 < 0.9
        assert service._client is None and not service._attempt.locked()    # the attempt that was cut off let go of its client
    finally:
        service._started = False


async def test_a_join_does_not_wait_longer_than_the_cap_for_a_slow_tv(tmp_path, gate):
    service = service_for(tmp_path, LibraryLike, key="key-9")
    service.wake_works, service.WAKE_POLL_S, service.WAKE_TIMEOUT_S = False, 0.02, 0.3
    service._started = True
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    try:
        assert await within(service.before_join(), 5.0) is False
        assert loop.time() - t0 < 1.5
    finally:
        service._started = False


# A client the service has let go of is no longer the TV

async def test_a_client_that_was_let_go_of_and_says_goodbye_later_is_not_the_tv_going_off(tmp_path):
    class Stubborn(FakeTvClient):
        async def disconnect(self):                           # the hang-up fails: the connection is still up behind it
            raise RuntimeError("the library choked")
    clients = [Stubborn, FakeTvClient]
    service = service_for(tmp_path, lambda host, key: clients.pop(0)(host, key), key="key-9")
    service.REWAKE_AFTER_S = 0.02
    service._started = True
    assert await service._connect_once() is True
    old = FakeTvClient.instances[-1]
    await service._drop_client()                              # let go of, as pairing again does: the old one is still talking
    assert await service._connect_once() is True              # the TV, through a new client
    assert service._shown["on"] is True
    await old.drop()                                          # and now the old one's connection ends, and it says goodbye
    await asyncio.sleep(0.2)
    assert service._shown["on"] is True and service._rewake is None and service.woken == []
    service._started = False
    await service._drop_client()


async def test_the_news_of_the_current_client_still_counts(tmp_path):
    service = service_for(tmp_path, key="key-9")
    service._started = True
    assert await service._connect_once() is True
    await FakeTvClient.instances[-1].update(volume=31, muted=True)
    assert service._shown["volume"] == 31 and service._shown["muted"] is True
    service._started = False
    await service._drop_client()


# Starting the service does not hold the agent up

async def test_start_does_not_hold_the_agent_for_a_tv_that_does_not_answer_and_the_wake_waits_for_the_first_look(tmp_path, gate):
    service = service_for(tmp_path, LibraryLike, key="key-9")
    service.wake_works, service.WAKE_POLL_S, service.WAKE_TIMEOUT_S = False, 0.02, 0.2
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await within(service.start(), 5.0)                        # the first look hangs: the agent is not made to wait for it
    try:
        assert loop.time() - t0 < 1.0 and service.status()["reachable"] is False
        await asyncio.sleep(0.1)
        assert service.woken == []                            # the wake waits for the first look, in the background
        gate.set()                                            # the first look ends
        await until(lambda: service.woken)
        assert service.woken == [MAC]
    finally:
        await service.stop()
