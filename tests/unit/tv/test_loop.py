"""The connection loop: finding the TV, backing off, noticing a lost link, ending when it is told to, and surviving itself."""
import asyncio
import logging

from tests.unit.tv.fake_client import FakeTvClient
from tests.unit.tv.helpers import build_service, connects, until, yielding_sleep


async def test_a_tv_that_is_off_at_start_is_found_when_it_answers(tmp_path):
    gate = asyncio.Event()

    async def sleep(seconds):                             # the back-off lasts until the test says it is over
        await gate.wait()
        await asyncio.sleep(0)
    service = build_service(tmp_path, loop_sleep=sleep)
    FakeTvClient.refuse_next = True                       # off when the agent starts, answering on the next try
    await service.start()
    try:
        assert service.status()["reachable"] is False and service.status()["on"] is False
        gate.set()
        await until(lambda: service.status()["reachable"])
        assert service.status()["on"] is True and service.status()["name"] == "Boardroom TV"
        assert len(FakeTvClient.instances) == 1           # the one client tries again: its callbacks stay registered
    finally:
        await service.stop()


class Refusing(FakeTvClient):
    """A TV that is off: every connect is refused."""

    def __init__(self, host, key):
        super().__init__(host, key)
        self.refuse = True


async def test_the_reconnect_backs_off_up_to_a_cap(tmp_path):
    delays = []
    service = build_service(tmp_path, Refusing, loop_sleep=yielding_sleep(delays))
    service.RECONNECT_FIRST_S, service.RECONNECT_MAX_S = 1.0, 5.0
    await service.start()
    try:
        await until(lambda: len(delays) >= 5)
    finally:
        await service.stop()
    assert delays[:5] == [1.0, 2.0, 4.0, 5.0, 5.0]


async def test_the_back_off_starts_over_once_the_tv_has_answered(tmp_path):
    delays = []
    service = build_service(tmp_path, Refusing, loop_sleep=yielding_sleep(delays))
    service.RECONNECT_FIRST_S, service.RECONNECT_MAX_S, service.WATCH_S = 1.0, 8.0, 0.5
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        await until(lambda: 4.0 in delays)                # the TV has not answered for a while: 1, 2, 4 ...
        client.refuse = False
        await until(lambda: service.status()["reachable"])    # ... and it answers
        client.refuse = True
        await client.drop()                               # then it is gone again
        mark = len(delays)
        await until(lambda: any(d != 0.5 for d in delays[mark:]))
        assert [d for d in delays[mark:] if d != 0.5][0] == 1.0       # the wait starts over from the first
    finally:
        await service.stop()


async def test_a_connect_timeout_goes_straight_to_the_capped_back_off(tmp_path):
    """The library's connect raises a timeout both when the TV does not answer and when the TV shows its pairing prompt
    and nobody accepts it, and the two cannot be told apart. A TV that is off does not mind being tried often; one that
    prompts every time is a nuisance on the wall of a meeting room: after a timeout the wait is the longest there is."""
    delays = []

    def timing_out(host, key):
        client = FakeTvClient(host, key)
        client.prompt_timeout = True
        return client
    service = build_service(tmp_path, timing_out, loop_sleep=yielding_sleep(delays))
    service.RECONNECT_FIRST_S, service.RECONNECT_MAX_S = 1.0, 8.0
    await service.start()
    try:
        await until(lambda: len(delays) >= 3)
    finally:
        await service.stop()
    assert delays[:3] == [8.0, 8.0, 8.0]


async def test_the_watch_tick_is_the_injected_sleep(tmp_path):
    delays = []
    service = build_service(tmp_path, loop_sleep=yielding_sleep(delays))
    service.WATCH_S = 7.0
    await service.start()
    try:
        await until(lambda: len(delays) >= 3)
    finally:
        await service.stop()
    assert delays[:3] == [7.0, 7.0, 7.0]


async def test_stop_does_not_wait_out_a_reconnect_back_off(tmp_path):
    service = build_service(tmp_path)                     # the default back-off: the next try is two seconds away
    FakeTvClient.refuse_next = True
    await service.start()
    assert service.status()["reachable"] is False
    await asyncio.wait_for(service.stop(), 0.5)
    assert ("disconnect",) in FakeTvClient.instances[-1].calls


async def test_stop_ends_the_loop_for_good(tmp_path):
    service = build_service(tmp_path, loop_sleep=yielding_sleep())
    await service.start()
    client = FakeTvClient.instances[-1]
    task = service._task
    await service.stop()
    assert task.done() and service._task is None          # finished, not merely asked to
    tries, built = connects(client), len(FakeTvClient.instances)
    await asyncio.sleep(0.05)                             # a loop left running would have tried again within microseconds
    assert connects(client) == tries and len(FakeTvClient.instances) == built
    assert service.status()["reachable"] is False


async def test_a_connection_that_drops_is_made_again_with_the_stored_key(tmp_path):
    service = build_service(tmp_path, loop_sleep=yielding_sleep())
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        client.refuse = True                              # the TV is switched off: its socket closes and it stops answering
        await client.drop()
        await until(lambda: connects(client) >= 2)        # a try the TV refuses
        assert service.status()["on"] is False and service.status()["reachable"] is False
        client.tv_state.power_state, client.refuse = {"state": "Active"}, False           # and switched on again
        await until(lambda: service.status()["reachable"])
        assert service.status()["on"] is True
        assert {c for c in client.calls if c[0] == "connect"} == {("connect", "key-9")}   # every try with the stored key
    finally:
        await service.stop()


async def test_the_loop_ends_when_the_key_is_gone(tmp_path):
    service = build_service(tmp_path, loop_sleep=yielding_sleep())
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        task = service._task
        (tmp_path / "tv-client-key").unlink()
        await client.drop()                               # the connection is lost, and there is no key to make it again with
        await until(task.done)
        assert connects(client) == 1 and service.status()["paired"] is False
    finally:
        await service.stop()


async def test_one_try_at_the_tv_at_a_time(tmp_path):
    gate = asyncio.Event()
    running = peak = 0

    class Slow(FakeTvClient):
        async def connect(self):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            try:
                await gate.wait()
                return await super().connect()
            finally:
                running -= 1
    service = build_service(tmp_path, Slow)
    first, second = asyncio.ensure_future(service._connect_once()), asyncio.ensure_future(service._connect_once())
    for _ in range(5):
        await asyncio.sleep(0)                            # both have had every chance to get going
    assert running == 1
    gate.set()
    assert await asyncio.gather(first, second) == [True, True]
    assert peak == 1 and len(FakeTvClient.instances) == 1


async def test_a_tv_that_keeps_refusing_is_warned_about_once_for_each_reason(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="croom.tv.service")
    service = build_service(tmp_path, Refusing)

    def told(level, text):
        return [r for r in caplog.records if r.levelno == level and text in r.getMessage()]
    for _ in range(3):
        assert await service._connect_once() is False
    assert len(told(logging.WARNING, "is not answering")) == 1 and len(told(logging.DEBUG, "is not answering")) == 2
    client = FakeTvClient.instances[-1]
    client.refuse, client.prompt_timeout = False, True    # another reason: it is said again
    assert await service._connect_once() is False
    assert len(told(logging.WARNING, "is not answering")) == 2 and told(logging.WARNING, "no answer in time")
    client.prompt_timeout = False                         # it answers: that is said, and the next failure counts as new
    assert await service._connect_once() is True
    assert len(told(logging.INFO, "answering again")) == 1
    client.refuse, client.connected = True, False
    assert await service._connect_once() is False
    assert len(told(logging.WARNING, "is not answering")) == 3


async def test_a_connect_that_answers_false_is_not_a_connection(tmp_path, caplog):
    class Closed(FakeTvClient):
        async def connect(self):                          # what the library answers when the connection is closed as it is made
            self.calls.append(("connect", self.client_key))
            return False
    service = build_service(tmp_path, Closed)
    await service.start()
    try:
        assert service.status()["reachable"] is False and "closed before it was made" in caplog.text
    finally:
        await service.stop()


async def test_a_bug_in_one_round_does_not_end_the_loop(tmp_path, caplog):
    gate = asyncio.Event()

    async def sleep(seconds):                             # the back-off lasts until the test says it is over
        await gate.wait()
        await asyncio.sleep(0)
    service = build_service(tmp_path, loop_sleep=sleep)
    real, rounds = service._connect_once, []

    async def flaky():
        rounds.append(1)
        if len(rounds) == 1:
            raise RuntimeError("a bug in the first round")
        return await real()
    service._connect_once = flaky
    await asyncio.wait_for(service.start(), 1.0)          # the first look is over when the round has failed, however it failed
    try:
        gate.set()
        await until(lambda: service.status()["reachable"])
        assert len(rounds) >= 2 and "a bug in the first round" in caplog.text
    finally:
        await service.stop()


async def test_stopping_the_loop_is_not_an_error(tmp_path, caplog):
    service = build_service(tmp_path, loop_sleep=yielding_sleep())
    await service.start()
    await service.stop()
    await asyncio.sleep(0)                                # the loop task's done-callback runs on the next turn
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], caplog.text


async def test_an_error_that_ends_the_loop_is_logged(tmp_path, caplog):
    class Broken(BaseException):                          # nothing in the loop catches this: it ends the loop's task
        pass

    async def sleep(seconds):
        raise Broken("the sleep broke")
    service = build_service(tmp_path, loop_sleep=sleep)
    await service.start()
    try:
        task = service._task
        await until(task.done)
        await asyncio.sleep(0)                            # the task's done-callback runs on the next turn
        assert "ended on an error" in caplog.text and "the sleep broke" in caplog.text
    finally:
        await service.stop()
