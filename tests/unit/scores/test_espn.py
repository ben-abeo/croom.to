"""
The ESPN feed reader (spec 2026-10-13 door sign, section 2.5): the parser against a saved schedule in ESPN's own shape, and the
fetcher against a local aiohttp app standing in for ESPN. Nothing here reaches the network.
"""

import asyncio
import random

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from croom.control.service import SIGN_SCHOOLS
from croom.scores import espn
from croom.scores.espn import SCHOOLS, ScheduleUnavailable, fetch, fetch_schedule, parse_schedule
from tests.unit.scores.schedules import AUCKLAND, CENTRAL, LOS_ANGELES, UTC, game, schedule, texas_sample

# The spec's own example (section 2.5), which the saved sample is written to give: Texas (#4) 45 - Texas A&M 42 at home on Monday
# the 12th, and Texas (#4) at Missouri (#9) on the 19th.
LAST = {"home": "Texas", "home_rank": 4, "home_score": 45, "away": "Texas A&M", "away_rank": None, "away_score": 42,
        "date": "2026-10-12", "school_home": True}
NEXT = {"date": "2026-10-19", "home": "Missouri", "home_rank": 9, "away": "Texas", "away_rank": 4}
ROAD_GAME = {"home": "Tennessee", "home_rank": 14, "home_score": 17, "away": "Texas", "away_rank": 1, "away_score": 20,
             "date": "2026-09-26", "school_home": False}                    # the game before the last one, which Texas played away
THEN_FLORIDA = {"date": "2026-10-26", "home": "Texas", "home_rank": 4, "away": "Florida", "away_rank": None}   # the game after the next one

# The sample's events by id: four played (the last of them Texas A&M's), three to come (Florida's has no kickoff time yet).
TEXAS_STATE, OHIO_STATE, TENNESSEE, TEXAS_AM, MISSOURI, FLORIDA, ARKANSAS = (f"40100{n}" for n in range(1, 8))


def event_of(data, event_id):
    return next(e for e in data["events"] if e["id"] == event_id)


def without(data, *event_ids):
    return {**data, "events": [e for e in data["events"] if e["id"] not in event_ids]}


def with_status(data, event_id, **status):
    """The sample with one event's status changed (state, completed, name)."""
    event_of(data, event_id)["competitions"][0]["status"]["type"].update(status)
    return data


def set_start(event, text):
    event["date"] = event["competitions"][0]["date"] = text


def test_the_schools_are_the_four_a_room_can_turn_on_with_espns_ids_and_the_feeds_address():
    assert SCHOOLS == {"texas": (251, "Texas"), "michigan": (130, "Michigan"), "texas_am": (245, "Texas A&M"), "missouri": (142, "Missouri")}
    assert tuple(SCHOOLS) == SIGN_SCHOOLS                                  # the room setting and the scoreboard name the same schools, in the same order
    assert espn.SCHEDULE_URL.format(team_id=251) == "https://site.api.espn.com/apis/site/v2/sports/football/college-football/teams/251/schedule"
    assert espn.REQUEST_TIMEOUT.total == 10                                # a request is given ten seconds, connecting and reading


def test_the_last_game_and_the_next_come_from_the_saved_schedule(device_zone):
    device_zone(CENTRAL)
    assert parse_schedule(texas_sample()) == {"last": LAST, "next": NEXT}


def test_the_order_the_feed_lists_its_events_in_does_not_matter(device_zone):
    device_zone(CENTRAL)
    data = texas_sample()
    random.Random(7).shuffle(data["events"])
    assert parse_schedule(data) == {"last": LAST, "next": NEXT}


@pytest.mark.parametrize("zone, day", [(CENTRAL, "2026-10-12"), (LOS_ANGELES, "2026-10-12"), (UTC, "2026-10-13"), (AUCKLAND, "2026-10-13")])
def test_a_game_with_a_kickoff_time_is_on_the_devices_own_day(device_zone, zone, day):
    """The kickoff, 7:30 PM on Monday the 12th in Central time, is 00:30 on the 13th in UTC: the sign prints the device's day."""
    device_zone(zone)
    assert parse_schedule(texas_sample())["last"]["date"] == day


@pytest.mark.parametrize("zone, kickoff_day", [(CENTRAL, "2026-10-25"), (LOS_ANGELES, "2026-10-25"), (UTC, "2026-10-26"), (AUCKLAND, "2026-10-26")])
def test_a_game_with_no_kickoff_time_yet_is_on_the_day_espn_means_in_every_zone(device_zone, zone, kickoff_day):
    """ESPN keeps such a game at midnight on the East coast, 04:00 UTC in October and 05:00 once the clocks have gone back: on a clock to
    the west that instant is the evening before, and the sign must not print the 25th for the game on the 26th, or the 6th for the 7th."""
    device_zone(zone)
    data = without(texas_sample(), MISSOURI)                               # so that Florida, whose kickoff time is not set, is the next game
    florida = event_of(data, FLORIDA)
    assert florida["timeValid"] is False and parse_schedule(data)["next"] == THEN_FLORIDA                 # 04:00 UTC on the 26th: the 26th
    set_start(florida, "2026-11-07T05:00Z")                                                               # after the clocks went back
    assert parse_schedule(data)["next"]["date"] == "2026-11-07"
    florida["timeValid"] = florida["competitions"][0]["timeValid"] = True                                 # the same instant as a kickoff: the device's own day
    set_start(florida, "2026-10-26T04:00Z")
    assert parse_schedule(data)["next"]["date"] == kickoff_day


def test_a_start_with_no_zone_is_taken_as_utc(device_zone):
    device_zone(CENTRAL)
    data = texas_sample()
    set_start(event_of(data, TEXAS_AM), "2026-10-13T00:30")                # not "...00:30Z": ESPN always says Z, and a zone-less time is not the device's
    assert parse_schedule(data)["last"]["date"] == "2026-10-12"


def test_only_the_first_competition_of_an_event_is_read_and_its_time_flag_may_sit_on_the_event(device_zone):
    device_zone(CENTRAL)
    data = without(texas_sample(), MISSOURI, FLORIDA)                      # Arkansas is the next game
    arkansas = event_of(data, ARKANSAS)
    arkansas["competitions"].append({"date": "2020-01-01T00:00Z", "competitors": []})                   # a second competition is not read
    set_start(arkansas, "2026-11-14T05:00Z")
    del arkansas["competitions"][0]["timeValid"]
    arkansas["timeValid"] = False                                          # the flag is on the event, where the competition has none
    assert parse_schedule(data)["next"]["date"] == "2026-11-14"
    arkansas["timeValid"] = True                                           # a kickoff at 05:00 UTC: 11 PM on the 13th in Central time
    assert parse_schedule(data)["next"]["date"] == "2026-11-13"


def test_ranks_present_and_absent(device_zone):
    device_zone(CENTRAL)
    assert (LAST["home_rank"], LAST["away_rank"]) == (4, None)             # present for Texas; Texas A&M's competitor has no rank object at all
    data = texas_sample()
    data["events"] = [event_of(data, TEXAS_STATE)]
    assert data["events"][0]["competitions"][0]["competitors"][1]["curatedRank"] == {"current": 99}      # an unranked Texas State: ESPN's 99
    last = parse_schedule(data)["last"]
    assert (last["home"], last["home_rank"], last["away"], last["away_rank"]) == ("Texas", 5, "Texas State", None)


@pytest.mark.parametrize("rank, shown", [({"current": 1}, 1), ({"current": 25}, 25), ({"current": 4.0}, 4), ({"current": "7"}, 7),
                                         ({"current": 26}, None), ({"current": 99}, None), ({"current": 0}, None), ({"current": -3}, None),
                                         ({"current": None}, None), ({"current": "x"}, None), ({"current": True}, None), ({"current": [4]}, None),
                                         ({}, None), (None, None), (4, None), ("4", None), ([4], None)])
def test_a_rank_is_a_place_in_the_top_25_and_anything_else_is_none(device_zone, rank, shown):
    device_zone(CENTRAL)
    data = texas_sample()
    event_of(data, TEXAS_AM)["competitions"][0]["competitors"][1]["curatedRank"] = rank       # the away side, Texas A&M
    assert parse_schedule(data)["last"]["away_rank"] == shown


def test_a_game_in_progress_is_neither_the_last_nor_the_next(device_zone):
    device_zone(CENTRAL)
    live = {"state": "in", "completed": False, "name": "STATUS_IN_PROGRESS"}
    assert parse_schedule(with_status(texas_sample(), TEXAS_AM, **live)) == {"last": ROAD_GAME, "next": NEXT}   # Texas A&M's game has begun: the line before it stays up
    result = parse_schedule(with_status(texas_sample(), MISSOURI, **live))                                    # the next game has begun: the one after it is the next
    assert result == {"last": LAST, "next": THEN_FLORIDA}


@pytest.mark.parametrize("name", ["STATUS_POSTPONED", "STATUS_CANCELED"])
def test_a_postponed_or_cancelled_game_is_neither(device_zone, name):
    device_zone(CENTRAL)
    called_off = {"state": "post", "completed": False, "name": name}
    assert parse_schedule(with_status(texas_sample(), MISSOURI, **called_off))["next"] == THEN_FLORIDA
    assert parse_schedule(with_status(texas_sample(), TEXAS_AM, **called_off))["last"] == ROAD_GAME


def test_no_next_game_at_the_end_of_the_season_and_no_last_game_before_it_begins(device_zone):
    device_zone(CENTRAL)
    data = texas_sample()
    assert parse_schedule(without(data, MISSOURI, FLORIDA, ARKANSAS)) == {"last": LAST, "next": None}                     # the season is over
    assert parse_schedule(without(data, TEXAS_STATE, OHIO_STATE, TENNESSEE, TEXAS_AM)) == {"last": None, "next": NEXT}   # it has not begun
    assert parse_schedule({**data, "events": []}) == {"last": None, "next": None}


@pytest.mark.parametrize("data", [None, [], "schedule", 7, {}, {"events": None}, {"events": "none"}, {"events": {}}, {"events": [None, 5, "x", [], {}]},
                                  {"events": [{"competitions": None}, {"competitions": []}, {"competitions": [None]}, {"competitions": [{}]},
                                              {"competitions": [{"competitors": None}]}, {"competitions": [{"competitors": []}]}]}])
def test_a_schedule_with_nothing_usable_in_it_is_survived(data):
    assert parse_schedule(data) == {"last": None, "next": None}


def no_date(event):
    del event["date"], event["competitions"][0]["date"]


def a_date_that_is_not_one(event):
    set_start(event, "soon")


def no_home_side(event):
    del event["competitions"][0]["competitors"][0]["homeAway"]


def one_team_only(event):
    event["competitions"][0]["competitors"].pop()


def no_team(event):
    del event["competitions"][0]["competitors"][1]["team"]


def a_team_with_no_name(event):
    event["competitions"][0]["competitors"][1]["team"] = {"id": "245"}


def no_scores(event):
    for competitor in event["competitions"][0]["competitors"]:
        del competitor["score"]


def one_score_only(event):
    del event["competitions"][0]["competitors"][0]["score"]


def no_status(event):
    del event["competitions"][0]["status"]


EVENT_BREAKAGES = [no_date, a_date_that_is_not_one, no_home_side, one_team_only, no_team, a_team_with_no_name, no_status]


@pytest.mark.parametrize("spoil", EVENT_BREAKAGES + [no_scores, one_score_only], ids=lambda f: f.__name__)
def test_what_espn_leaves_out_of_one_event_costs_that_event_and_nothing_else(device_zone, spoil):
    device_zone(CENTRAL)
    data = texas_sample()
    spoil(event_of(data, TEXAS_AM))
    assert parse_schedule(data) == {"last": ROAD_GAME, "next": NEXT}       # the last game is the one before the spoiled one; the games to come are as they were


@pytest.mark.parametrize("spoil", EVENT_BREAKAGES, ids=lambda f: f.__name__)
def test_a_game_to_come_that_cannot_be_drawn_is_skipped_too(device_zone, spoil):
    device_zone(CENTRAL)
    data = texas_sample()
    spoil(event_of(data, MISSOURI))
    assert parse_schedule(data) == {"last": LAST, "next": THEN_FLORIDA}


@pytest.mark.parametrize("score, shown", [({"value": 45.0, "displayValue": "45"}, 45), ({"value": 45}, 45), ({"displayValue": "45"}, 45), (45, 45), (45.0, 45), ("45", 45),
                                          ({"value": "45"}, 45), (0, 0), ({"value": 0.0}, 0)])
def test_a_score_is_a_whole_number_however_espn_writes_it(device_zone, score, shown):
    device_zone(CENTRAL)
    data = texas_sample()
    event_of(data, TEXAS_AM)["competitions"][0]["competitors"][0]["score"] = score
    assert parse_schedule(data)["last"]["home_score"] == shown


@pytest.mark.parametrize("score", [None, "", "none", True, float("nan"), float("inf"), [45], {}, {"value": None}])
def test_a_score_that_is_no_number_makes_the_game_unusable_as_the_last_one(device_zone, score):
    device_zone(CENTRAL)
    data = texas_sample()
    event_of(data, TEXAS_AM)["competitions"][0]["competitors"][0]["score"] = score
    assert parse_schedule(data)["last"] == ROAD_GAME


@pytest.mark.parametrize("team, name", [
    ({"location": "Texas A&M", "shortDisplayName": "Aggies", "displayName": "Texas A&M Aggies"}, "Texas A&M"),   # the school, not the mascot
    ({"shortDisplayName": "Texas St", "displayName": "Texas State Bobcats"}, "Texas St"),
    ({"displayName": "Missouri Tigers", "abbreviation": "MIZ"}, "Missouri Tigers"),
    ({"abbreviation": "MIZ"}, "MIZ"),
    ({"location": "  ", "shortDisplayName": "", "displayName": "Ohio State Buckeyes"}, "Ohio State Buckeyes"),    # blank is nothing
    ({"location": 7, "displayName": "Ohio State Buckeyes"}, "Ohio State Buckeyes"),
])
def test_a_team_is_named_by_its_location_then_by_the_shorter_names_it_has(device_zone, team, name):
    device_zone(CENTRAL)
    data = schedule("Texas", [game("Texas", "Rival", "2026-10-10T20:00Z", home_score=1, away_score=0, state="post")])
    data["events"][0]["competitions"][0]["competitors"][1]["team"] = team
    assert parse_schedule(data)["last"]["away"] == name


def test_the_schools_own_side_is_found_by_its_id_and_is_none_when_the_schedule_does_not_say_whose_it_is(device_zone):
    device_zone(CENTRAL)
    assert parse_schedule(texas_sample())["last"]["school_home"] is True
    assert parse_schedule(with_status(texas_sample(), TEXAS_AM, state="in", completed=False))["last"]["school_home"] is False      # at Tennessee
    for team in ({}, {"id": "9999"}, None):
        data = texas_sample()
        data["team"] = team
        assert parse_schedule(data)["last"]["school_home"] is None         # no team, or one that played in neither game


# --- the fetcher, against a local app in ESPN's place ---

class StandIn:
    """A local aiohttp app answering where ESPN would: `respond(request)` is set by a test, `asked` records the team ids it was asked for."""

    def __init__(self):
        self.asked = []
        self.release = asyncio.Event()                                     # a held request goes when the test is over
        self.server = TestServer(self._app())
        self.url = None

    async def respond(self, request):
        return web.json_response(texas_sample())

    def _app(self):
        async def handler(request):
            self.asked.append(request.match_info["team_id"])
            return await self.respond(request)

        app = web.Application()
        app.router.add_get("/teams/{team_id}/schedule", handler)
        return app

    async def start(self):
        await self.server.start_server()
        self.url = f"http://127.0.0.1:{self.server.port}/teams/{{team_id}}/schedule"

    async def stop(self):
        self.release.set()
        await self.server.close()


@pytest.fixture
async def stand_in():
    stand_in = StandIn()
    await stand_in.start()
    yield stand_in
    await stand_in.stop()


async def test_fetch_schedule_asks_for_the_teams_schedule_and_returns_what_espn_sent(stand_in):
    async with aiohttp.ClientSession() as session:
        data = await fetch_schedule(session, 251, stand_in.url)
        await fetch_schedule(session, 142, stand_in.url)
    assert data == texas_sample() and stand_in.asked == ["251", "142"]      # one request for each team, at its own address


async def test_fetch_opens_a_session_of_its_own_and_asks_the_address_the_module_names(stand_in, monkeypatch):
    monkeypatch.setattr(espn, "SCHEDULE_URL", stand_in.url)
    assert await fetch(251) == texas_sample() and await fetch(245) == texas_sample()
    assert stand_in.asked == ["251", "245"]


async def test_the_answer_is_read_as_json_whatever_espn_calls_it(stand_in):
    async def respond(request):
        return web.Response(text='{"events": []}', content_type="text/plain")

    stand_in.respond = respond
    async with aiohttp.ClientSession() as session:
        assert await fetch_schedule(session, 251, stand_in.url) == {"events": []}


@pytest.mark.parametrize("status", [404, 500, 503])
async def test_a_refusal_is_a_failure_that_says_what_espn_answered(stand_in, status):
    async def respond(request):
        return web.Response(status=status, text="no")

    stand_in.respond = respond
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ScheduleUnavailable, match=f"ESPN answered {status}"):
            await fetch_schedule(session, 251, stand_in.url)


@pytest.mark.parametrize("body, words", [("<html>maintenance</html>", "was not JSON"), ("", "was not a schedule"), ("[1, 2]", "was not a schedule"),
                                          ('"schedule"', "was not a schedule"), ("null", "was not a schedule")])
async def test_an_answer_that_is_not_a_json_object_is_a_failure(stand_in, body, words):
    async def respond(request):
        return web.Response(text=body, content_type="application/json")

    stand_in.respond = respond
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ScheduleUnavailable, match=words):
            await fetch_schedule(session, 251, stand_in.url)


async def test_a_feed_that_cannot_be_reached_is_a_failure(stand_in):
    await stand_in.stop()                                                   # nothing listens where it was
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ScheduleUnavailable, match="ESPN could not be reached"):
            await fetch_schedule(session, 251, stand_in.url)


async def test_a_feed_that_stalls_is_given_up_on_at_the_timeout_and_says_how_long(stand_in, monkeypatch):
    async def respond(request):
        await stand_in.release.wait()                                       # an answer that never comes

    stand_in.respond = respond
    monkeypatch.setattr(espn, "REQUEST_TIMEOUT", aiohttp.ClientTimeout(total=0.3))
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ScheduleUnavailable, match="ESPN did not answer within 0.3 seconds"):
            await asyncio.wait_for(fetch_schedule(session, 251, stand_in.url), 5)     # the bound is the fetcher's own: the guard only fails the test sooner
    assert stand_in.asked == ["251"]
