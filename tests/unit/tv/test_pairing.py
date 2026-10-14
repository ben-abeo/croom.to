"""Pairing, forgetting, and what becomes of the connection while they happen; the key the TV hands out, and a TV that
stops accepting the one that was stored."""
import asyncio
import logging

import pytest
from croom.tv.errors import TvError
from croom.tv.keystore import KeyStore
from tests.unit.tv.fake_client import DeafPromptingTvClient, FakeTvClient, PromptingTvClient
from tests.unit.tv.helpers import build_service, connects, prompt_is_up, silent_tv, until, within, yielding_sleep

KEY = "tv-client-key"


async def test_pairing_a_running_service_starts_the_connection(tmp_path):
    service = build_service(tmp_path, key=None, loop_sleep=yielding_sleep())       # started before the TV was paired
    await service.start()
    try:
        assert FakeTvClient.instances == [] and service.status()["paired"] is False
        with pytest.raises(TvError) as failure:
            await service.control("volume_up")
        assert "Pair the TV" in str(failure.value)
        await service.pair()
        client = FakeTvClient.instances[-1]
        await client.drop()                               # a drop: the loop that pairing started brings it back
        await until(lambda: service.status()["reachable"])
        assert ("connect", "key-1") in client.calls       # with the key the pairing stored
    finally:
        await service.stop()


async def test_pairing_again_a_running_service_makes_one_connection_and_one_prompt(tmp_path):
    def waits_for_the_person_when_pairing(host, key):
        return FakeTvClient(host, key) if key else PromptingTvClient(host, key)
    service = build_service(tmp_path, waits_for_the_person_when_pairing, loop_sleep=yielding_sleep())
    await service.start()
    try:
        old = FakeTvClient.instances[-1]
        pairing = asyncio.ensure_future(service.pair())
        await until(prompt_is_up)
        await asyncio.sleep(0.05)                         # a loop that had not stood aside would be trying the TV meanwhile
        new = FakeTvClient.instances[-1]
        new.accept()
        await within(pairing)
        await asyncio.sleep(0.05)
        assert new is not old and len(FakeTvClient.instances) == 2
        assert connects(new) == 1 and old.connected is False and service.status()["reachable"] is True
        assert (tmp_path / KEY).read_text() == "key-1"
    finally:
        await service.stop()


async def test_pairing_a_service_that_is_not_running_starts_no_loop(tmp_path):
    service = build_service(tmp_path, key=None)           # never started
    await service.pair()
    assert service._task is None and service.status()["reachable"] is True
    await service.stop()
    await service.pair()                                  # and not any more once it has been stopped
    assert service._task is None
    await service.stop()


async def test_forget_while_connected_lets_go_of_the_tv_and_stops_reconnecting(tmp_path):
    service = build_service(tmp_path, loop_sleep=yielding_sleep())
    await service.start()
    try:
        client = FakeTvClient.instances[-1]
        await service.forget()
        assert client.connected is False and not (tmp_path / KEY).exists()
        s = service.status()
        assert (s["paired"], s["reachable"], s["on"], s["name"]) == (False, False, False, "")
        tries = connects(client)
        await asyncio.sleep(0.05)
        assert connects(client) == tries and len(FakeTvClient.instances) == 1
    finally:
        await service.stop()


async def test_a_pairing_the_tv_does_not_complete_is_explained_and_stores_nothing(tmp_path):
    service = build_service(tmp_path, key=None)
    FakeTvClient.refuse_next = True                       # the TV is off
    with pytest.raises(TvError) as failure:
        await service.pair()
    assert "10.0.0.41" in str(failure.value) and "prompt" in str(failure.value)
    assert not (tmp_path / KEY).exists() and service.status()["paired"] is False
    assert ("disconnect",) in FakeTvClient.instances[-1].calls


async def test_a_pairing_the_person_declines_is_explained_and_stores_nothing(tmp_path, caplog):
    def declining(host, key):
        client = FakeTvClient(host, key)
        client.decline = True
        return client
    service = build_service(tmp_path, declining, key=None)
    with pytest.raises(TvError) as failure:
        await service.pair()
    assert "10.0.0.41" in str(failure.value) and "prompt" in str(failure.value)
    assert "403 User denied access" in caplog.text        # the reason is for the log; the table gets words
    assert "403" not in str(failure.value) and not (tmp_path / KEY).exists()


async def test_pairing_needs_a_configured_tv(tmp_path):
    service = build_service(tmp_path, key=None, enabled=False, host="")
    with pytest.raises(TvError) as failure:
        await service.pair()
    assert "not set up" in str(failure.value) and FakeTvClient.instances == []


async def test_a_pairing_that_yields_no_key_is_explained(tmp_path):
    class Keyless(FakeTvClient):
        async def connect(self):                          # connects, but the TV never hands a key over
            self.calls.append(("connect", self.client_key))
            self.connected = True
            return True
    service = build_service(tmp_path, Keyless, key=None)
    with pytest.raises(TvError) as failure:
        await service.pair()
    assert "did not hand out a key" in str(failure.value) and "10.0.0.41" in str(failure.value)
    assert service.status()["paired"] is False and service.status()["reachable"] is False
    assert ("disconnect",) in FakeTvClient.instances[-1].calls


async def test_a_key_that_cannot_be_kept_is_explained_and_the_connection_let_go(tmp_path):
    (tmp_path / "blocker").write_text("a file where the data dir should be")
    service = build_service(tmp_path, key=None, key_path=tmp_path / "blocker" / KEY)
    with pytest.raises(TvError) as failure:
        await service.pair()
    assert "key could not be kept" in str(failure.value) and KEY in str(failure.value)
    assert service.status()["paired"] is False and service.status()["reachable"] is False
    assert ("disconnect",) in FakeTvClient.instances[-1].calls


async def test_the_library_missing_is_said_in_words_when_pairing(tmp_path):
    def without_the_library(host, key):
        raise TvError("The TV library is not installed on this device: pip install aiowebostv")
    service = build_service(tmp_path, without_the_library, key=None)
    with pytest.raises(TvError) as failure:
        await service.pair()
    assert "aiowebostv" in str(failure.value)


# A pairing waits for the person at the TV, for as long as PAIR_WAIT_S allows: the rest of the service goes on around it.

@pytest.mark.parametrize("factory", [PromptingTvClient, DeafPromptingTvClient])
async def test_stop_during_a_pairing_leaves_nothing_connected(tmp_path, factory):
    service = build_service(tmp_path, factory, key=None)
    await service.start()
    pairing = asyncio.ensure_future(service.pair())
    await until(prompt_is_up)
    await service.stop()
    client = FakeTvClient.instances[-1]
    client.accept()                                       # the person accepts, too late
    with pytest.raises(TvError) as failure:
        await within(pairing)
    assert "was stopped" in str(failure.value) and "check that the TV is on" not in str(failure.value)
    assert client.connected is False and not (tmp_path / KEY).exists()
    assert service._client is None and service._task is None and service.status()["reachable"] is False


async def test_forget_during_a_pairing_ends_it_and_keeps_no_key(tmp_path):
    service = build_service(tmp_path, PromptingTvClient, key=None)
    pairing = asyncio.ensure_future(service.pair())
    await until(prompt_is_up)
    await service.forget()
    client = FakeTvClient.instances[-1]
    client.accept()
    with pytest.raises(TvError) as failure:
        await within(pairing)
    assert "was stopped" in str(failure.value)
    assert client.connected is False and not (tmp_path / KEY).exists() and service._client is None


async def test_a_pairing_cancelled_while_it_waits_leaves_no_client(tmp_path):
    service = build_service(tmp_path, PromptingTvClient, key=None)
    pairing = asyncio.ensure_future(service.pair())
    await until(prompt_is_up)
    pairing.cancel()                                      # the caller went away: the page closed, the request was dropped
    with pytest.raises(asyncio.CancelledError):
        await within(pairing)
    first = FakeTvClient.instances[-1]
    assert ("disconnect",) in first.calls and first.connected is False
    assert service._client is None and not (tmp_path / KEY).exists()
    again = asyncio.ensure_future(service.pair())         # and pairing can be tried again at once
    await until(lambda: len(FakeTvClient.instances) == 2 and prompt_is_up())
    FakeTvClient.instances[-1].accept()
    await within(again)
    assert service.status()["paired"] is True and service.status()["reachable"] is True


def test_the_words_for_a_pairing_nobody_answers_say_how_long_it_waited():
    from croom.tv.service import NOBODY_ACCEPTED, TvService
    assert NOBODY_ACCEPTED.format(seconds=TvService.PAIR_WAIT_S) == (
        "Nobody accepted the prompt on the TV within 60 seconds: tap Pair to try again")


async def test_a_pairing_nobody_answers_ends_in_words_and_leaves_the_service_ready_for_the_next(tmp_path):
    """The library's own wait for the prompt does not end while the connection's heartbeat keeps it alive (aiohttp restarts its
    receive timeout on every ping and pong, and aiowebostv pings every five seconds): a pairing nobody answers would wait for
    ever, and every tap on Pair after it would be told that one is already waiting. The service gives the person a wait of its
    own."""
    service = build_service(tmp_path, PromptingTvClient, key=None)
    service.PAIR_WAIT_S = 0.2
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(TvError) as failure:
        await within(service.pair(), 3.0)
    assert 0.15 <= loop.time() - t0 < 2.0                 # (the wait is the one it was given, and it is not the library's)
    assert str(failure.value) == "Nobody accepted the prompt on the TV within 0.2 seconds: tap Pair to try again"
    first = FakeTvClient.instances[-1]
    assert ("disconnect",) in first.calls and first.connected is False
    assert service._client is None and service._pairing is False and not (tmp_path / KEY).exists()
    assert service.status()["paired"] is False and service.status()["reachable"] is False
    again = asyncio.ensure_future(service.pair())         # tapping Pair again starts a pairing: it is not "already waiting"
    await until(lambda: len(FakeTvClient.instances) == 2 and prompt_is_up())
    FakeTvClient.instances[-1].accept()
    await within(again)
    assert service.status()["paired"] is True and service.status()["reachable"] is True


class AcceptedInTheLastMoments(FakeTvClient):
    """A person who accepts the prompt as the wait runs out. The TV registers the client and hands it the key at once; the library
    goes on to open its input socket and read the TV's state, and connect() returns `finish` seconds later (None: never does)."""
    finish = 0.5

    async def connect(self):
        self.calls.append(("connect", self.client_key))
        self.client_key = "key-1"
        self.calls.append(("paired",))
        if self.finish is None:
            await asyncio.Event().wait()
        await asyncio.sleep(self.finish)
        self.connected = True
        return True


def test_a_tv_that_accepted_is_given_five_more_seconds_to_finish():
    from croom.tv.service import TvService
    assert TvService.PAIR_FINISH_S == 5.0


async def test_a_person_who_accepts_in_the_last_moments_of_the_wait_is_paired_and_not_told_that_nobody_did(tmp_path):
    """The wait is for the person at the TV. An accept in its last second is the TV's to finish: cut off then, the TV has
    registered this client and no key was stored (an entry on the TV that belongs to nobody), and the person is told that nobody accepted."""
    service = build_service(tmp_path, AcceptedInTheLastMoments, key=None)
    service.PAIR_WAIT_S, service.PAIR_FINISH_S = 0.2, 2.0         # the connect takes 0.5 s: it runs past the wait, not past the extra time
    await within(service.pair(), 3.0)
    client = FakeTvClient.instances[-1]
    assert (tmp_path / KEY).read_text() == "key-1" and service.status()["paired"] is True
    assert client.connected is True and ("disconnect",) not in client.calls
    assert service.status()["reachable"] is True and service._pairing is False


async def test_a_tv_that_accepted_and_does_not_finish_is_given_up_on_after_the_extra_time(tmp_path):
    class NeverFinishes(AcceptedInTheLastMoments):
        finish = None
    service = build_service(tmp_path, NeverFinishes, key=None)
    service.PAIR_WAIT_S, service.PAIR_FINISH_S = 0.1, 0.2
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(TvError) as failure:
        await within(service.pair(), 3.0)
    assert 0.25 <= loop.time() - t0 < 2.0                 # the wait, and the extra time, and no more
    assert "did not finish" in str(failure.value) and "Nobody accepted" not in str(failure.value)   # somebody did accept
    assert ("disconnect",) in FakeTvClient.instances[-1].calls
    assert service._client is None and service._pairing is False and not (tmp_path / KEY).exists()
    assert service.status()["paired"] is False and service.status()["reachable"] is False


async def test_the_wait_for_the_person_leaves_nothing_behind_when_the_pairing_ends_sooner(tmp_path, caplog):
    """The timer that ends the person's wait is cancelled with the pairing: left to run it would act on a pairing that is over."""
    service = build_service(tmp_path, key=None)
    service.PAIR_WAIT_S = 0.1
    FakeTvClient.refuse_next = True                       # the TV is off: the pairing ends at once, in words
    with pytest.raises(TvError):
        await service.pair()
    await asyncio.sleep(0.25)                             # past the end of the wait
    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []


async def test_a_pairing_nobody_has_accepted_is_not_given_the_extra_time(tmp_path):
    service = build_service(tmp_path, PromptingTvClient, key=None)
    service.PAIR_WAIT_S, service.PAIR_FINISH_S = 0.1, 5.0
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(TvError) as failure:
        await within(service.pair(), 3.0)
    assert loop.time() - t0 < 1.0 and "Nobody accepted" in str(failure.value)     # (no key was handed out: the extra time is for the TV's finishing)


async def test_a_pairing_the_library_gives_up_on_says_what_it_said_before_the_services_own_wait_is_up(tmp_path):
    service = build_service(tmp_path, key=None)
    service.PAIR_WAIT_S = 30.0
    FakeTvClient.refuse_next = False
    def timing_out(host, key):
        client = FakeTvClient(host, key)
        client.prompt_timeout = True                      # the library's own timeout: the prompt was not answered in its time
        return client
    service._factory = timing_out
    with pytest.raises(TvError) as failure:
        await service.pair()
    assert "did not finish" in str(failure.value) and "Nobody accepted" not in str(failure.value)
    assert ("disconnect",) in FakeTvClient.instances[-1].calls


async def test_a_second_pair_while_one_waits_is_refused(tmp_path):
    service = build_service(tmp_path, PromptingTvClient, key=None)
    first = asyncio.ensure_future(service.pair())
    await until(prompt_is_up)
    with pytest.raises(TvError) as failure:               # a double tap on Pair
        await within(service.pair())
    assert "already waiting" in str(failure.value)
    client = FakeTvClient.instances[-1]
    assert len(FakeTvClient.instances) == 1 and connects(client) == 1       # one prompt on the TV, not two
    client.accept()
    await within(first)
    assert service.status()["paired"] is True and client.connected is True and len(FakeTvClient.instances) == 1


# A key the TV hands out again, and a TV that will not take the stored one.

class Reissuing(FakeTvClient):
    async def connect(self):
        await super().connect()
        self.client_key = "key-2"             # the TV did not know the key, showed its prompt, was accepted, and issued a new one
        return True


class Counting(KeyStore):
    def __init__(self, path):
        super().__init__(path)
        self.written = []

    def write(self, key):
        self.written.append(key)
        super().write(key)


async def test_a_key_the_tv_issues_anew_is_kept_once(tmp_path):
    service = build_service(tmp_path, Reissuing, keystore=Counting)
    await service.start()
    try:
        assert service.status()["reachable"] is True
        assert (tmp_path / KEY).read_text() == "key-2"    # or the next start would make the TV show its prompt again
        assert await service._connect_once() is True and await service._connect_once() is True
        assert service._keys.written == ["key-2"]         # a key that has not changed is not written again
    finally:
        await service.stop()


async def test_a_key_file_edited_by_hand_is_left_alone_across_a_reconnect(tmp_path):
    """The client keeps the key it was built with. A reconnect that finds the file different is no reason to write that
    key over the edit: only a key the TV changed during the connect is the TV's to give."""
    service = build_service(tmp_path, keystore=Counting)
    assert await service._connect_once() is True          # the client is built with the stored key-9
    (tmp_path / KEY).write_text("key-by-hand")
    FakeTvClient.instances[-1].connected = False          # the link drops
    assert await service._connect_once() is True          # and is made again, by the same client
    assert (tmp_path / KEY).read_text() == "key-by-hand" and service._keys.written == []


async def test_a_key_the_tv_changes_in_a_reconnect_is_kept_even_over_a_hand_edit(tmp_path):
    class Rotating(FakeTvClient):
        """A TV that issues a new key at every connect."""
        issued = 1

        async def connect(self):
            await super().connect()
            self.issued += 1
            self.client_key = f"key-{self.issued}"
            return True
    service = build_service(tmp_path, Rotating, keystore=Counting)
    assert await service._connect_once() is True          # key-2 is issued, and kept
    (tmp_path / KEY).write_text("key-by-hand")
    FakeTvClient.instances[-1].connected = False
    assert await service._connect_once() is True          # key-3 is the TV's own word, and the TV is the one that knows
    assert (tmp_path / KEY).read_text() == "key-3" and service._keys.written == ["key-2", "key-3"]


async def test_a_new_key_that_cannot_be_kept_costs_nothing_but_a_log_line(tmp_path, caplog):
    class Failing(KeyStore):
        def write(self, key):
            raise PermissionError("read-only data dir")
    service = build_service(tmp_path, Reissuing, keystore=Failing)
    await service.start()
    try:
        assert service.status()["reachable"] is True and (tmp_path / KEY).read_text() == "key-9"
        assert "could not be saved" in caplog.text
    finally:
        await service.stop()


def forgetful_tv(host, key):
    """A TV that has forgotten this table: a stored key makes it show its prompt, and the prompt is declined."""
    client = FakeTvClient(host, key)
    client.decline = key is not None
    return client


async def test_a_tv_that_no_longer_knows_this_table_is_not_prompted_again_and_control_says_so(tmp_path, caplog):
    service = build_service(tmp_path, forgetful_tv, loop_sleep=yielding_sleep())
    await asyncio.wait_for(service.start(), 1.0)          # the loop ends on the refusal: the first look is over at once
    try:
        client = FakeTvClient.instances[-1]
        await until(service._task.done)
        assert connects(client) == 1 and "no longer accepts this table" in caplog.text
        with pytest.raises(TvError) as failure:
            await service.control("volume_up")
        assert "The TV no longer accepts this table" in str(failure.value) and "Forget TV, then Pair" in str(failure.value)
        s = service.status()
        assert s["paired"] is True and s["reachable"] is False       # the key stays until the person forgets the TV
        assert await service._connect_once() is False         # a wake that polls the TV does not prompt it either
        await asyncio.sleep(0.05)
        assert connects(client) == 1 and len(FakeTvClient.instances) == 1
    finally:
        await service.stop()


async def test_forget_and_pair_put_a_refused_tv_right(tmp_path):
    service = build_service(tmp_path, forgetful_tv, loop_sleep=yielding_sleep())
    await service.start()
    try:
        await until(service._task.done)
        await service.forget()
        await service.pair()                              # the person accepts the prompt on the TV this time
        assert service.status()["reachable"] is True and service.status()["paired"] is True
        assert service._require_ready() is FakeTvClient.instances[-1]        # control no longer says the table was refused
    finally:
        await service.stop()


async def test_pairing_without_forgetting_also_puts_a_refused_tv_right(tmp_path):
    service = build_service(tmp_path, forgetful_tv, loop_sleep=yielding_sleep())
    await service.start()
    try:
        await until(service._task.done)
        await service.pair()
        assert service._require_ready() is FakeTvClient.instances[-1] and service.status()["reachable"] is True
        client = FakeTvClient.instances[-1]
        await client.drop()
        await until(lambda: service.status()["reachable"])    # and the loop is running again
    finally:
        await service.stop()


async def test_the_status_says_when_the_tv_declined_this_table_so_that_pair_again_is_not_not_answering(tmp_path):
    """The table's page tells "pair again" from "not answering": both are a TV that is not reachable, and only the first
    is the person's to put right."""
    service = build_service(tmp_path, forgetful_tv, loop_sleep=yielding_sleep())
    assert service.status()["refused"] is False                  # nothing has been refused before the first try
    await service.start()
    try:
        await until(service._task.done)
        s = service.status()
        assert s["refused"] is True and s["paired"] is True and s["reachable"] is False
        await service.pair()                                     # the person accepts the prompt this time
        assert service.status()["refused"] is False and service.status()["reachable"] is True
    finally:
        await service.stop()


async def test_a_tv_that_is_merely_off_has_not_refused_anything(tmp_path):
    service = build_service(tmp_path, silent_tv)
    await service.start()                                        # (the first look at a TV that is off is over at once)
    try:
        assert service.status()["reachable"] is False and service.status()["refused"] is False
    finally:
        await service.stop()


async def test_a_new_start_tries_the_stored_key_once_more(tmp_path):
    service = build_service(tmp_path, forgetful_tv, loop_sleep=yielding_sleep())
    await service.start()
    await until(service._task.done)
    await service.stop()
    await service.start()
    try:
        await until(service._task.done)
        assert len(FakeTvClient.instances) == 2 and connects(FakeTvClient.instances[-1]) == 1
    finally:
        await service.stop()


# Forgetting a TV whose key cannot be removed

async def test_a_key_that_cannot_be_removed_is_explained_and_the_connection_comes_back(tmp_path):
    class Stuck(KeyStore):
        def clear(self):
            raise PermissionError("read-only data dir")
    service = build_service(tmp_path, keystore=Stuck, loop_sleep=yielding_sleep())
    await service.start()
    try:
        first = FakeTvClient.instances[-1]
        with pytest.raises(TvError) as failure:
            await service.forget()
        assert "could not be forgotten" in str(failure.value) and KEY in str(failure.value)
        assert (tmp_path / KEY).exists() and service.status()["paired"] is True
        assert first.connected is False                   # it let go of the TV ...
        await until(lambda: service.status()["reachable"])      # ... and the loop, still wanted, found it again
    finally:
        await service.stop()
