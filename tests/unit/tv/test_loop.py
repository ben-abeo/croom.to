"""The connection loop: finding the TV, backing off, noticing a lost link, ending when it is told to, and surviving itself."""
import asyncio
import logging

from tests.unit.scores.schedules import Clock
from tests.unit.tv.fake_client import FakeTvClient, PromptingTvClient, WebOsTvPairError
from tests.unit.tv.helpers import build_service, connects, prompt_is_up, until, within, yielding_sleep


async def test_a_tv_that_is_off_at_start_is_found_when_it_answers(tmp_path):
    gate = asyncio.Event()

    async def sleep(seconds):                             # the back-off lasts until the test says it is over
        await gate.wait()
        await asyncio.sleep(0)
    service = build_service(tmp_path, loop_sleep=sleep, keep_on=False)       # (the wake of a TV that is to be kept on is not the loop's)
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
    service = build_service(tmp_path, keep_on=False)      # the default back-off: the next try is two seconds away
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
    service = build_service(tmp_path, loop_sleep=sleep, keep_on=False)
    real, rounds = service._connect_once, []

    async def flaky():
        rounds.append(1)
        if len(rounds) == 1:
            raise RuntimeError("a bug in the first round")
        return await real()
    service._connect_once = flaky
    await asyncio.wait_for(service.start(), 1.0)          # the first look is over when the round has failed, however it failed
    try:
        assert len(rounds) == 1                           # and the loop backs off before it goes round again
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


# A TV that has forgotten this table: the person at the table is told, not left with a TV that says only "Turn on"

def at_its_prompt(clock, error=TimeoutError):
    """A TV that shows its pairing prompt for the stored key and that nobody answers: the library waits for the person, and its connect
    ends in `error` (its own timeout) after `waits` seconds of the service's clock (the library gives the person ten seconds; a TV that
    does not answer at all is given two). The wait is time that the clock is moved by; `gate`, when there is one, is what the connect
    waits for first."""
    class AtItsPrompt(FakeTvClient):
        waits = 10.0
        gate = None

        async def connect(self):
            self.calls.append(("connect", self.client_key))
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(0)
            clock.advance(self.waits)
            raise error
    return AtItsPrompt


async def test_a_try_that_waited_at_the_tvs_prompt_is_asking_to_pair_and_one_that_got_no_answer_at_all_is_not(tmp_path):
    """The library's connect times out when the TV does not answer (it gives it two seconds) and when its prompt is not answered (ten): the
    time the try waited tells them apart, for a TV that is off is not asking anything of the table."""
    clock = Clock()
    service = build_service(tmp_path, at_its_prompt(clock), clock=clock)
    assert service.status()["prompting"] is False                             # nothing has been tried
    assert await service._connect_once() is False                             # the library gave the person ten seconds
    status = service.status()
    assert status["prompting"] is True and status["refused"] is False and status["on"] is False and status["paired"] is True
    service._client.waits = 2.0                                               # now it does not answer at all: two seconds
    assert await service._connect_once() is False
    assert service.status()["prompting"] is False
    service._client.waits = service.PROMPT_AFTER_S - 0.25                     # just short of the bound: still a TV that is not answering
    assert await service._connect_once() is False
    assert service.status()["prompting"] is False
    service._client.waits = service.PROMPT_AFTER_S                            # the bound: the try has reached the TV
    assert await service._connect_once() is False
    assert service.status()["prompting"] is True
    service._client.connect = FakeTvClient.connect.__get__(service._client)   # the TV answers
    assert await service._connect_once() is True
    assert service.status()["prompting"] is False and service.status()["reachable"] is True


async def test_the_next_try_does_not_take_the_asking_back_while_it_waits_and_ends_it_when_it_fails_at_once(tmp_path):
    """The loop tries again after its back-off, and a page that drew "asking" a moment ago must not draw "Turn on" for the first seconds
    of every try."""
    clock = Clock()
    service = build_service(tmp_path, at_its_prompt(clock), clock=clock)
    assert await service._connect_once() is False and service.status()["prompting"] is True
    service._client.gate = asyncio.Event()
    again = asyncio.ensure_future(service._connect_once())                     # the next try, at the prompt again
    try:
        await until(lambda: service._waiting_since is not None)
        assert service.status()["prompting"] is True                           # (it has waited no time at all yet)
    finally:
        service._client.waits = 2.0                                            # and then the TV is off: two seconds
        service._client.gate.set()
        assert await within(again) is False
    assert service.status()["prompting"] is False


async def test_a_tv_that_is_off_or_that_refuses_is_not_asking_to_pair(tmp_path):
    for reason in ("refuse", "prompt_timeout"):                                # (both end at once, as a TV that does not answer does)
        service = build_service(tmp_path, FakeTvClient)
        assert await service._connect_once() is True
        service._client.connected = False
        setattr(service._client, reason, True)
        assert await service._connect_once() is False
        assert service.status()["prompting"] is False and service.status()["on"] is False, reason


async def test_a_loop_that_waits_at_the_prompt_says_so_when_it_has_waited_long_enough_and_stops_when_the_prompt_is_answered(tmp_path):
    """With the stored key a TV that has forgotten the table shows its prompt, and the library goes on waiting for the person at the TV
    (a wait ended by an answer, the TV going away, stop or Forget). Once the try has waited longer than a TV that does not answer takes,
    the table's page says so."""
    clock = Clock()
    service = build_service(tmp_path, PromptingTvClient, clock=clock)
    service.START_WAIT_S = 0.01
    await service.start()
    try:
        await until(prompt_is_up)                                              # the try is waiting at the prompt
        assert service.status()["prompting"] is False                          # (it has only begun)
        clock.advance(service.PROMPT_AFTER_S - 0.25)
        assert service.status()["prompting"] is False
        clock.advance(0.25)
        status = service.status()
        assert status["prompting"] is True and status["paired"] is True and status["refused"] is False
        assert status["reachable"] is False and status["on"] is False
        FakeTvClient.instances[-1].accept()                                    # the person accepts it with the TV's own remote
        await until(lambda: service.status()["reachable"])
        assert service.status()["prompting"] is False
    finally:
        await service.stop()


async def test_forget_ends_the_asking(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, PromptingTvClient, clock=clock)
    service.START_WAIT_S = 0.01
    await service.start()
    await until(prompt_is_up)
    clock.advance(service.PROMPT_AFTER_S)
    assert service.status()["prompting"] is True
    await service.forget()                                                     # the pairing is let go of
    assert service.status()["prompting"] is False and service.status()["paired"] is False
    await service.stop()


async def test_forget_ends_the_asking_of_a_try_that_had_ended_unanswered_too(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, at_its_prompt(clock), clock=clock)
    assert await service._connect_once() is False and service.status()["prompting"] is True
    await service.forget()                                                     # (an unpaired table has nothing to be asked)
    assert service.status()["prompting"] is False and service.status()["paired"] is False


async def test_a_prompt_that_is_declined_is_the_refused_card_and_not_a_tv_that_is_asking(tmp_path):
    clock = Clock()
    service = build_service(tmp_path, at_its_prompt(clock, WebOsTvPairError("403 User denied access")), clock=clock)
    assert await service._connect_once() is False                              # (it waited at the prompt ten seconds, and the person said no)
    assert service.status()["refused"] is True and service.status()["prompting"] is False    # it has its own card
