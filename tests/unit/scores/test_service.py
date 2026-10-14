"""
The scores service (spec 2026-10-13 door sign, section 2.5): for each school the room has on, the last game and the next, read from a
feed every ten minutes and kept between reads. The feed is a fake fetcher and the clock a fake one; nothing here reaches the network.
"""

import asyncio
import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer

from croom.core.service import Service
from croom.scores.espn import SCHOOLS, ScheduleUnavailable, parse_schedule
from croom.scores.service import ScoresService
from tests.unit.control.test_service import make_service
from tests.unit.scores.schedules import CENTRAL, Clock, Feed, game, schedule, texas_sample

TEXAS_ID, MICHIGAN_ID, TEXAS_AM_ID, MISSOURI_ID = (SCHOOLS[key][0] for key in ("texas", "michigan", "texas_am", "missouri"))
TEXAS = {"school": "texas", "name": "Texas",                                  # the spec's own example, from the saved schedule
         "last": {"home": "Texas", "home_rank": 4, "home_score": 45, "away": "Texas A&M", "away_rank": None, "away_score": 42,
                  "date": "2026-10-12", "school_home": True},
         "next": {"date": "2026-10-19", "home": "Missouri", "home_rank": 9, "away": "Texas", "away_rank": 4, "neutral": False}}
MICHIGAN = {"school": "michigan", "name": "Michigan",
            "last": {"home": "Michigan", "home_rank": 12, "home_score": 31, "away": "Ohio State", "away_rank": None, "away_score": 24,
                     "date": "2026-10-10", "school_home": True},
            "next": {"date": "2026-10-24", "home": "Purdue", "home_rank": None, "away": "Michigan", "away_rank": 12, "neutral": False}}


async def wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met in time")


@pytest.fixture
def central(device_zone):
    device_zone(CENTRAL)


def make(chosen, feed=None, clock=None, **kwargs):
    """A service over a fake feed and clock, reading the schools from `chosen`, a list the test changes as the room's page would."""
    feed = Feed() if feed is None else feed
    clock = Clock() if clock is None else clock
    return ScoresService(feed, lambda: chosen, clock=clock, **kwargs), feed, clock


def test_the_service_is_one_the_agent_manages_named_scores():
    service, _, _ = make([])
    assert isinstance(service, Service) and service.name == "scores"


async def test_nothing_is_fetched_while_every_school_is_off(central):
    service, feed, clock = make([], check_s=0.01)
    await service.refresh()
    clock.advance(3600)
    await service.refresh()
    assert feed.asked == [] and service.current() == []
    await service.start()                                                    # nor does the loop, however long it looks
    await asyncio.sleep(0.1)
    await service.stop()
    assert feed.asked == [] and service.current() == []


async def test_only_the_schools_that_are_on_are_fetched_and_listed_in_the_settings_order(central):
    chosen = ["missouri", "texas"]                                           # not the order the schools table lists them in
    service, feed, _ = make(chosen)
    assert [entry["school"] for entry in service.current()] == ["missouri", "texas"]         # listed before any read, with nothing to show
    assert all(entry["last"] is None and entry["next"] is None for entry in service.current())
    await service.refresh()
    assert feed.asked == [MISSOURI_ID, TEXAS_ID]                             # Michigan and Texas A&M were not asked for
    entries = service.current()
    assert [(e["school"], e["name"]) for e in entries] == [("missouri", "Missouri"), ("texas", "Texas")]
    assert entries[1] == TEXAS and set(entries[0]) == {"school", "name", "last", "next"}


async def test_each_entry_carries_its_own_schools_last_game_and_next_game(central):
    service, feed, _ = make(["texas", "michigan", "texas_am", "missouri"])
    await service.refresh()
    assert feed.asked == [TEXAS_ID, MICHIGAN_ID, TEXAS_AM_ID, MISSOURI_ID]
    by_school = {entry["school"]: entry for entry in service.current()}
    assert by_school["texas"] == TEXAS and by_school["michigan"] == MICHIGAN
    assert by_school["texas_am"]["last"]["away"] == "Texas A&M" and by_school["texas_am"]["last"]["school_home"] is False      # lost at LSU, 24-21
    assert (by_school["missouri"]["last"]["home_score"], by_school["missouri"]["next"]["away"]) == (35, "Texas")
    assert by_school["texas"]["last"] == parse_schedule(texas_sample())["last"]        # what the parser makes of the feed, unchanged


async def test_a_school_is_read_again_every_ten_minutes_and_not_before(central):
    service, feed, clock = make(["texas"])
    await service.refresh()
    assert feed.asked == [TEXAS_ID]
    clock.advance(599)
    await service.refresh()
    assert feed.asked == [TEXAS_ID]                                          # 599 seconds on: not due
    clock.advance(1)
    await service.refresh()
    assert feed.asked == [TEXAS_ID, TEXAS_ID]                                # 600: due
    await service.refresh()
    clock.advance(599)
    await service.refresh()
    assert len(feed.asked) == 2                                              # and the ten minutes run from the last read
    clock.advance(1)
    await service.refresh()
    assert len(feed.asked) == 3


async def test_the_interval_is_the_one_asked_for(central):
    service, feed, clock = make(["texas"], interval_s=30)
    await service.refresh()
    clock.advance(29)
    await service.refresh()
    clock.advance(1)
    await service.refresh()
    assert len(feed.asked) == 2


async def test_the_answer_read_changes_what_is_listed_at_the_next_read(central):
    service, feed, clock = make(["texas"])
    await service.refresh()
    assert service.current()[0]["last"]["home_score"] == 45
    feed.answers[TEXAS_ID]["events"][3]["competitions"][0]["competitors"][0]["score"] = {"value": 52.0, "displayValue": "52"}
    assert service.current()[0]["last"]["home_score"] == 45                  # until it is read again
    clock.advance(600)
    await service.refresh()
    assert service.current()[0]["last"]["home_score"] == 52


async def test_a_failed_fetch_keeps_the_last_answer_and_the_status_still_answers(central, caplog):
    """The feed goes down after a good read: the door sign still has the last score (a final score does not go stale in ten minutes),
    the failure is logged once in words, and the room's status route, which the table and the sign both depend on, answers as ever."""
    chosen = ["texas"]
    service, feed, clock = make(chosen)
    control = make_service(scores=service)
    async with TestClient(TestServer(control.create_app())) as client:
        await service.refresh()
        before = (await (await client.get("/api/status")).json())["scores"]
        assert before == [TEXAS]
        feed.down = ScheduleUnavailable("ESPN answered 503")
        caplog.set_level(logging.DEBUG, logger="croom.scores.service")
        for _ in range(3):
            clock.advance(600)
            await service.refresh()                                          # three reads, all refused
            response = await client.get("/api/status")
            assert response.status == 200                                    # the status answers while the feed is down
            assert (await response.json())["scores"] == before               # with the last good answer
        assert service.current() == [TEXAS]
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1                                            # said once, in words: who, from where, why, and what is kept
        assert "Texas" in warnings[0] and "ESPN" in warnings[0] and "ESPN answered 503" in warnings[0] and "keeping the last" in warnings[0]
        assert len([r for r in caplog.records if r.levelno == logging.DEBUG and "ESPN answered 503" in r.getMessage()]) == 2   # the repeats, quietly
        feed.down = None                                                     # the feed is back
        clock.advance(600)
        await service.refresh()
        assert service.current() == [TEXAS]
        assert [r.getMessage() for r in caplog.records if r.levelno == logging.INFO and "again" in r.getMessage()] == [
            "Football scores for Texas are being read from ESPN again"]


async def test_a_feed_that_is_down_from_the_start_lists_the_school_with_nothing_to_show_and_the_status_answers(central, caplog):
    service, feed, _ = make(["texas", "michigan"])
    feed.down = ScheduleUnavailable("ESPN could not be reached: Cannot connect to host site.api.espn.com:443")
    async with TestClient(TestServer(make_service(scores=service).create_app())) as client:
        await service.refresh()
        response = await client.get("/api/status")
        assert response.status == 200
        scores = (await response.json())["scores"]
        assert scores == [{"school": "texas", "name": "Texas", "last": None, "next": None},
                          {"school": "michigan", "name": "Michigan", "last": None, "next": None}]     # nothing for the sign to draw: it shows no strip
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2 and all("shows no scores for it until it can" in w for w in warnings)      # one for each school, with nothing kept to say


async def test_a_failed_read_is_tried_again_after_a_minute_not_after_ten(central):
    """A room that boots before its network is up must not wait ten minutes for its scores."""
    service, feed, clock = make(["texas"])
    feed.down = ScheduleUnavailable("ESPN could not be reached")
    await service.refresh()
    assert len(feed.asked) == 1
    clock.advance(59)
    await service.refresh()
    assert len(feed.asked) == 1                                              # 59 seconds on: not yet
    clock.advance(1)
    feed.down = None
    await service.refresh()
    assert len(feed.asked) == 2 and service.current() == [TEXAS]            # a minute: asked again, and the answer is there
    clock.advance(599)
    await service.refresh()
    assert len(feed.asked) == 2                                              # after a good read it is ten minutes again


async def test_a_failed_read_is_never_retried_later_than_the_interval(central):
    service, feed, clock = make(["texas"], interval_s=30)                    # a minute's retry would be later than a read that had worked
    feed.down = ScheduleUnavailable("ESPN could not be reached")
    await service.refresh()
    clock.advance(29)
    await service.refresh()
    assert len(feed.asked) == 1
    clock.advance(1)
    await service.refresh()
    assert len(feed.asked) == 2


async def test_one_schools_failure_does_not_stop_the_others_being_read(central):
    service, feed, _ = make(["texas", "michigan", "missouri"])
    feed.down_for = {MICHIGAN_ID: ScheduleUnavailable("ESPN answered 500")}
    await service.refresh()
    assert feed.asked == [TEXAS_ID, MICHIGAN_ID, MISSOURI_ID]
    by_school = {entry["school"]: entry for entry in service.current()}
    assert by_school["texas"] == TEXAS and by_school["michigan"]["last"] is None and by_school["missouri"]["last"]["home"] == "Missouri"


async def a_step(service, feed, clock, wait):
    """One look `wait` seconds after the last: a second short it asks for nothing, on the dot it asks once."""
    before = len(feed.asked)
    clock.advance(wait - 1)
    await service.refresh()
    assert len(feed.asked) == before, f"asked {wait - 1} seconds on"
    clock.advance(1)
    await service.refresh()
    assert len(feed.asked) == before + 1, f"not asked {wait} seconds on"


async def test_failed_reads_back_off_a_minute_two_five_and_ten_and_a_success_starts_over(central):
    """A feed that stays down is asked 6 times an hour by the end, not 60: a minute after the first failure, two after the second, five, then ten."""
    service, feed, clock = make(["texas"])
    feed.down = ScheduleUnavailable("ESPN answered 503")
    await service.refresh()                                                  # the first failure
    for wait in (60, 120, 300, 600, 600, 600):                                # and each further one waits longer, up to ten minutes
        await a_step(service, feed, clock, wait)
    feed.down = None
    await a_step(service, feed, clock, 600)                                   # a success
    assert service.current() == [TEXAS]
    feed.down = ScheduleUnavailable("ESPN answered 503")
    await a_step(service, feed, clock, 600)                                   # after a good read the next is ten minutes on, as ever; this one fails ...
    await a_step(service, feed, clock, 60)                                    # ... and the wait starts again at a minute, not at ten
    await a_step(service, feed, clock, 120)


async def test_the_backoff_is_each_schools_own_and_is_never_longer_than_the_interval(central):
    service, feed, clock = make(["texas", "michigan"])
    feed.down_for = {MICHIGAN_ID: ScheduleUnavailable("ESPN answered 500")}
    await service.refresh()
    assert feed.asked == [TEXAS_ID, MICHIGAN_ID]
    clock.advance(60)
    await service.refresh()
    assert feed.asked == [TEXAS_ID, MICHIGAN_ID, MICHIGAN_ID]                # Michigan's minute is up; Texas, read well, waits its ten
    clock.advance(120)                                                       # 180 seconds in: Michigan's two minutes are up
    await service.refresh()
    assert feed.asked.count(MICHIGAN_ID) == 3 and feed.asked.count(TEXAS_ID) == 1
    clock.advance(300)                                                       # 480: its five minutes are up too
    await service.refresh()
    assert feed.asked.count(MICHIGAN_ID) == 4 and feed.asked.count(TEXAS_ID) == 1
    clock.advance(120)                                                       # 600: Texas's ten minutes are up, and Michigan's ten (from 480) are not
    await service.refresh()
    assert feed.asked.count(TEXAS_ID) == 2 and feed.asked.count(MICHIGAN_ID) == 4
    short, short_feed, short_clock = make(["texas"], interval_s=100)         # a steady read every 100 seconds: no failure waits longer than that
    short_feed.down = ScheduleUnavailable("ESPN answered 503")
    await short.refresh()
    for wait in (60, 100, 100, 100):
        await a_step(short, short_feed, short_clock, wait)


NO_GAME_PAYLOADS = {
    "an empty object": {},
    "an error object": {"code": 500, "message": "the feed is having a bad day"},
    "no events": {"events": []},
    "events with no game in them": {"events": [{"competitions": [{}]}, None, 5]},
    "only a game in progress": schedule("Texas", [game("Texas", "Rice", "2026-10-10T19:30Z", home_score=3, away_score=0, state="in")]),
}


@pytest.mark.parametrize("payload", NO_GAME_PAYLOADS.values(), ids=NO_GAME_PAYLOADS.keys())
async def test_a_read_with_no_game_in_it_is_a_failed_read_that_keeps_the_last_answer(central, caplog, payload):
    """A feed that answers, with nothing the sign could draw (an error object in a 200, a schedule with no games), has not told the room that
    the school has no scores: the last good answer stays, the problem is said once in words, and the read is tried again in a minute."""
    service, feed, clock = make(["texas"])
    await service.refresh()
    assert service.current() == [TEXAS]
    feed.answers[TEXAS_ID] = payload
    clock.advance(600)
    await service.refresh()
    assert service.current() == [TEXAS]                                       # kept, not replaced by nothing
    said = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(said) == 1 and "Texas" in said[0] and "ESPN" in said[0] and "no game in it" in said[0] and "keeping the last" in said[0]
    assert len(feed.asked) == 2
    clock.advance(59)
    await service.refresh()
    assert len(feed.asked) == 2
    clock.advance(1)
    await service.refresh()
    assert len(feed.asked) == 3                                               # a minute on, not ten
    feed.answers[TEXAS_ID] = texas_sample()                                   # and a schedule with games in it is an answer again
    feed.answers[TEXAS_ID]["events"][3]["competitions"][0]["competitors"][0]["score"] = {"value": 52.0, "displayValue": "52"}
    clock.advance(120)
    await service.refresh()
    assert service.current()[0]["last"]["home_score"] == 52


async def test_a_school_whose_first_read_has_no_game_in_it_has_nothing_to_show_and_says_why_once(central, caplog):
    service, feed, clock = make(["texas", "michigan"])
    feed.answers[TEXAS_ID] = {"events": []}
    await service.refresh()
    assert service.current()[0] == {"school": "texas", "name": "Texas", "last": None, "next": None} and service.current()[1] == MICHIGAN
    said = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(said) == 1 and "Texas" in said[0] and "no game in it" in said[0] and "shows no scores for it until it can" in said[0]


async def test_a_school_that_starts_failing_is_warned_about_once_a_new_reason_is_told_at_info_and_the_recovery_at_info(central, caplog):
    caplog.set_level(logging.DEBUG, logger="croom.scores.service")
    service, feed, clock = make(["texas"])
    await service.refresh()                                                   # a good read: nothing to say
    for failure in (ScheduleUnavailable("ESPN answered 503"), ScheduleUnavailable("ESPN answered 503"), ScheduleUnavailable("ESPN did not answer within 10 seconds"),
                    RuntimeError("boom"), RuntimeError("boom"), ScheduleUnavailable("ESPN answered 503")):
        feed.down = failure
        clock.advance(600)
        await service.refresh()
    sorted_by_level = lambda level, word="Could not read": [r.getMessage() for r in caplog.records if r.levelno == level and word in r.getMessage()]
    warned, told, quiet = sorted_by_level(logging.WARNING), sorted_by_level(logging.INFO), sorted_by_level(logging.DEBUG)
    assert len(warned) == 1 and "503" in warned[0]                           # once, when the school starts failing
    assert len(told) == 3 and "10 seconds" in told[0] and "RuntimeError: boom" in told[1] and "503" in told[2]      # each change of reason, at INFO
    assert len(quiet) == 2                                                    # a reason said again, quietly
    feed.down = None
    clock.advance(600)
    await service.refresh()
    again = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO and "again" in r.getMessage()]
    assert again == ["Football scores for Texas are being read from ESPN again"]       # the recovery, once
    feed.down = ScheduleUnavailable("ESPN answered 503")                      # and a school that fails once more is a new transition: warned about again
    clock.advance(600)
    await service.refresh()
    assert len(sorted_by_level(logging.WARNING)) == 2


async def test_a_school_turned_on_is_read_at_the_loops_next_look_and_not_ten_minutes_later(central):
    chosen = []
    service, feed, clock = make(chosen, check_s=0.01)
    await service.start()
    try:
        await asyncio.sleep(0.1)                                             # a good many looks at an empty list
        assert feed.asked == []
        chosen.append("texas")
        await wait_until(lambda: feed.asked == [TEXAS_ID])                   # read at once, by a loop that has been looking all along
        await wait_until(lambda: service.current() == [TEXAS])
        chosen.append("michigan")
        await wait_until(lambda: feed.asked == [TEXAS_ID, MICHIGAN_ID])
        await asyncio.sleep(0.1)
        assert feed.asked == [TEXAS_ID, MICHIGAN_ID]                         # the clock has not moved: Texas is not read again
        clock.advance(600)
        await wait_until(lambda: sorted(feed.asked) == sorted([TEXAS_ID, MICHIGAN_ID] * 2))
    finally:
        await service.stop()


async def test_the_loops_first_read_is_at_start_and_stop_ends_it(central):
    chosen = ["texas"]
    service, feed, clock = make(chosen, check_s=3600)                        # a look an hour apart: the first one is not waited for
    await service.start()
    await service.start()                                                    # twice is no second loop
    await wait_until(lambda: feed.asked == [TEXAS_ID])
    await service.stop()
    await service.stop()                                                     # and stopping twice is no trouble
    chosen.append("michigan")
    clock.advance(3600)
    await asyncio.sleep(0.05)
    assert feed.asked == [TEXAS_ID]                                          # nothing more is read once it is stopped
    await service.start()                                                    # a restart reads what is due
    await wait_until(lambda: feed.asked == [TEXAS_ID, TEXAS_ID, MICHIGAN_ID])           # Texas is due again, Michigan was never read
    await service.stop()


async def test_stopping_does_not_wait_for_a_read_that_never_answers(central):
    answering = asyncio.Event()

    async def hung(team_id):
        await answering.wait()

    service = ScoresService(hung, lambda: ["texas"], check_s=0.01)
    await service.start()
    await asyncio.sleep(0.05)
    await asyncio.wait_for(service.stop(), 2)                                # the loop is cancelled in the middle of its read


async def test_a_school_turned_off_is_no_longer_listed_or_read_and_comes_back_with_what_it_had(central):
    chosen = ["texas", "michigan"]
    service, feed, clock = make(chosen)
    await service.refresh()
    chosen.remove("texas")
    assert [entry["school"] for entry in service.current()] == ["michigan"]
    clock.advance(600)
    await service.refresh()
    assert feed.asked == [TEXAS_ID, MICHIGAN_ID, MICHIGAN_ID]               # Texas is not asked for while it is off
    chosen.insert(0, "texas")
    assert service.current()[0] == TEXAS                                     # back at once with its last answer, until it is read again
    await service.refresh()
    assert feed.asked[-1] == TEXAS_ID                                        # which is due again: its last read was ten minutes ago
    chosen.clear()
    assert service.current() == []


async def test_schools_the_scoreboard_does_not_know_and_repeats_are_ignored(central):
    service, feed, _ = make(["texas", "ohio_state", "texas", "", None, 7, "michigan"])
    await service.refresh()
    assert feed.asked == [TEXAS_ID, MICHIGAN_ID]
    assert [entry["school"] for entry in service.current()] == ["texas", "michigan"]


async def test_what_current_hands_out_is_a_copy(central):
    service, _, _ = make(["texas"])
    await service.refresh()
    service.current()[0]["last"]["home_score"] = 0
    service.current()[0]["next"].clear()
    service.current().clear()
    assert service.current() == [TEXAS]


async def test_a_schools_list_that_raises_lists_nothing_says_so_once_and_the_loop_goes_on(central, caplog):
    broken = {"now": True}

    def schools():
        if broken["now"]:
            raise RuntimeError("the settings file is gone")
        return ["texas"]

    feed, clock = Feed(), Clock()
    service = ScoresService(feed, schools, clock=clock, check_s=0.01)
    assert service.current() == []                                           # the status route is not told
    await service.start()
    try:
        await asyncio.sleep(0.1)
        assert feed.asked == [] and service.current() == []
        said = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(said) == 1 and "the settings file is gone" in said[0] and "which schools" in said[0]
        broken["now"] = False
        await wait_until(lambda: service.current() == [TEXAS])               # the loop was still there
    finally:
        await service.stop()


async def test_an_error_in_one_look_is_said_once_and_the_loop_goes_on(central, caplog):
    calls = []

    def clock():
        calls.append(1)
        if len(calls) <= 3:
            raise RuntimeError("the clock broke")                            # a bug that is not a feed's failure, three looks running
        return 5000.0

    feed = Feed()
    service = ScoresService(feed, lambda: ["texas"], clock=clock, check_s=0.01)
    await service.start()
    try:
        await wait_until(lambda: service.current() == [TEXAS])               # the fourth look reads the school: the loop was not ended
    finally:
        await service.stop()
    said = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(said) == 1 and "the clock broke" in said[0] and "goes on" in said[0]


async def test_a_fetcher_that_misbehaves_costs_that_read_and_not_the_loop(central):
    feed = Feed()
    service = ScoresService(feed, lambda: ["texas"], check_s=0.01, retry_s=0.01)            # the real clock: the retry has to come due
    feed.down = ValueError("not what a fetcher raises")
    await service.start()
    try:
        await wait_until(lambda: len(feed.asked) >= 2)                       # read again after the retry, not given up on
        feed.down = None
        await wait_until(lambda: service.current() == [TEXAS])
    finally:
        await service.stop()
