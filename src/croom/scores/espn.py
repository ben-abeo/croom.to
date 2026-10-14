"""
ESPN's public college football schedule feed (spec 2026-10-13 door sign, section 2.5): one function that asks for a team's schedule
and one that reduces it to the last game played and the next one to play. The feed needs no key or account, and nothing else in
Crystal Meet talks to ESPN: a different source is a different file with the same two functions.
"""

import asyncio
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

# The schools the door sign can show: the keys of the room setting sign_scores (the order the touchscreen lists them in), each with
# ESPN's id for its team and the name the sign prints, which is ESPN's own for the school ("Texas A&M", not "Texas A&M Aggies").
SCHOOLS: Dict[str, Tuple[int, str]] = {
    "texas": (251, "Texas"),
    "michigan": (130, "Michigan"),
    "texas_am": (245, "Texas A&M"),
    "missouri": (142, "Missouri"),
}
SCHEDULE_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/teams/{team_id}/schedule"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)   # the whole ask, connecting and reading: a feed that stalls is given up on
TOP_RANK = 25                                       # ESPN's curatedRank is 99 for a team outside the Top 25


class ScheduleUnavailable(Exception):
    """ESPN could not be asked, or did not answer with a schedule: the message says why, in words for the journal."""


async def fetch_schedule(session: aiohttp.ClientSession, team_id: int, url: str = SCHEDULE_URL) -> Dict[str, Any]:
    """One team's schedule as ESPN sends it (a JSON object).

    Raises:
        ScheduleUnavailable: when ESPN is not reached in ten seconds, answers with anything but 200, or does not send a JSON object.
    """
    try:
        async with session.get(url.format(team_id=team_id), timeout=REQUEST_TIMEOUT) as response:
            if response.status != 200:
                raise ScheduleUnavailable(f"ESPN answered {response.status}")
            data = await response.json(content_type=None)   # whatever it calls the answer: it is read as JSON or refused below
    except asyncio.TimeoutError as e:
        raise ScheduleUnavailable(f"ESPN did not answer within {REQUEST_TIMEOUT.total:g} seconds") from e
    except (aiohttp.ClientError, OSError) as e:
        raise ScheduleUnavailable(f"ESPN could not be reached: {e}") from e
    except ValueError as e:
        raise ScheduleUnavailable("ESPN's answer was not JSON") from e
    if not isinstance(data, dict):
        raise ScheduleUnavailable("ESPN's answer was not a schedule")
    return data


async def fetch(team_id: int) -> Dict[str, Any]:
    """fetch_schedule over a session of its own: what the agent hands the scores service as its fetcher. A session lives for one
    ask, as the Zoom and Microsoft calls' do, so there is nothing to close or to leak between the reads ten minutes apart."""
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        return await fetch_schedule(session, team_id, SCHEDULE_URL)


def _object(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _whole_number(value: Any) -> Optional[int]:
    """A score or a rank as a whole number; None for what ESPN leaves out (a game not played has no score) or sends as nothing like one."""
    if isinstance(value, dict):                                  # a score is {"value": 45.0, "displayValue": "45"}
        value = value.get("value", value.get("displayValue"))
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    try:
        if isinstance(value, (int, float)) and math.isfinite(value):
            return int(value)
    except OverflowError:                                          # an integer too big for a float is no score and no rank
        pass
    return None


def _start(text: Any) -> Optional[datetime]:
    """When ESPN says a game begins ("2026-10-19T19:00Z"), as a moment in UTC; None when it says nothing a moment can be read from."""
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _day(start: datetime, timed: bool) -> str:
    """The date a game is on, as the sign prints it. A game with a kickoff time is on the device's own day (a 7:30 PM Central kickoff
    is the next day in UTC). A game without one has only the placeholder ESPN keeps for it, midnight on the East coast (04:00 or 05:00
    UTC), which a clock to the west calls the evening before: its day is the date as ESPN wrote it."""
    return (start.astimezone() if timed else start).date().isoformat()


def _name(team: Dict[str, Any]) -> Optional[str]:
    """The school as ESPN names it ("Texas A&M"; displayName is "Texas A&M Aggies" and shortDisplayName can abbreviate)."""
    for key in ("location", "shortDisplayName", "displayName", "abbreviation"):
        value = team.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _rank(competitor: Dict[str, Any]) -> Optional[int]:
    """The Top 25 rank, or None for a team that is not in it (ESPN says 99, or has no rank at all)."""
    rank = _whole_number(_object(competitor.get("curatedRank")).get("current"))
    return rank if rank is not None and 1 <= rank <= TOP_RANK else None


def _game(event: Any, school_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """One event of the schedule as a game, or None when it is not one the sign could draw (no two teams, no date)."""
    event = _object(event)
    competitions = event.get("competitions")
    competition = _object(competitions[0]) if isinstance(competitions, list) and competitions else {}
    sides: Dict[str, Dict[str, Any]] = {}
    for competitor in _list(competition.get("competitors")):
        competitor = _object(competitor)
        side = competitor.get("homeAway")
        if side in ("home", "away"):
            sides[side] = competitor
    if len(sides) != 2:
        return None
    start = _start(competition.get("date") or event.get("date"))
    names = {side: _name(_object(competitor.get("team"))) for side, competitor in sides.items()}
    if start is None or not all(names.values()):
        return None
    timed = competition.get("timeValid", event.get("timeValid", True)) is not False
    try:
        day = _day(start, timed)
    except (OverflowError, OSError, ValueError):                   # a date the device's zone cannot be applied to (the year 1, the year 9999)
        return None
    kind = _object(_object(competition.get("status")).get("type"))
    if kind.get("completed") is True:
        phase = "played"
    elif kind.get("state") == "pre":
        phase = "coming"
    else:
        phase = None      # in progress, postponed, cancelled: neither the last game nor the next
    ids = {side: str(competitor.get("id") or _object(competitor.get("team")).get("id") or "") for side, competitor in sides.items()}
    return {
        "phase": phase,
        "start": start,
        "day": day,
        "home": names["home"], "home_rank": _rank(sides["home"]), "home_score": _whole_number(sides["home"].get("score")),
        "away": names["away"], "away_rank": _rank(sides["away"]), "away_score": _whole_number(sides["away"].get("score")),
        "school_home": None if school_id is None or school_id not in ids.values() else ids["home"] == school_id,
    }


def parse_schedule(data: Any) -> Dict[str, Optional[Dict[str, Any]]]:
    """The last game played and the next one to play, from a team's schedule as ESPN sends it.

    {"last": {"home", "home_rank", "home_score", "away", "away_rank", "away_score", "date", "school_home"} or None,
     "next": {"date", "home", "home_rank", "away", "away_rank"} or None}

    The last game is the latest one that is over and has both scores; the next is the earliest one still to play. A game in progress is
    neither (the line before it stays up until it ends), nor is one postponed or cancelled. A rank is None for a team outside the Top 25;
    date is the day it was or will be played, as the device keeps days ("2026-10-19"); school_home says whether the team the schedule
    belongs to was the home side (None when the schedule does not say whose it is). Anything the feed leaves out is survived: a schedule
    with nothing usable in it is {"last": None, "next": None}.
    """
    data = _object(data)
    school_id = _object(data.get("team")).get("id")
    games = [game for game in (_game(event, None if school_id is None else str(school_id)) for event in _list(data.get("events"))) if game]
    played = [g for g in games if g["phase"] == "played" and g["home_score"] is not None and g["away_score"] is not None]
    coming = [g for g in games if g["phase"] == "coming"]
    last = max(played, key=lambda g: g["start"], default=None)
    upcoming = min(coming, key=lambda g: g["start"], default=None)
    return {
        "last": None if last is None else {
            "home": last["home"], "home_rank": last["home_rank"], "home_score": last["home_score"],
            "away": last["away"], "away_rank": last["away_rank"], "away_score": last["away_score"],
            "date": last["day"], "school_home": last["school_home"],
        },
        "next": None if upcoming is None else {
            "date": upcoming["day"],
            "home": upcoming["home"], "home_rank": upcoming["home_rank"],
            "away": upcoming["away"], "away_rank": upcoming["away_rank"],
        },
    }
