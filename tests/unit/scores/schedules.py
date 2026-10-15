"""ESPN-shaped schedules for the tests: the saved Texas sample, and small ones built to order for the other schools."""

import copy
import json
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

FIXTURES = Path(__file__).parent / "fixtures"

# Device time zones as POSIX strings, which need no tz database: US Central (UTC-5 from the second Sunday of March to the first of
# November, UTC-6 after), Pacific, New Zealand, and UTC itself.
CENTRAL = "CST6CDT,M3.2.0,M11.1.0"
LOS_ANGELES = "PST8PDT,M3.2.0,M11.1.0"
AUCKLAND = "NZST-12NZDT,M9.5.0,M4.1.0/3"
UTC = "UTC0"


def texas_sample() -> Dict[str, Any]:
    """The saved sample of Texas's schedule, fresh to change as a test likes."""
    return json.loads((FIXTURES / "texas-schedule.json").read_text(encoding="utf-8"))


def team_id(name: str) -> str:
    return str(zlib.crc32(name.encode()) % 100000)


def competitor(name: str, side: str, score: Optional[int] = None, rank: Optional[int] = None) -> Dict[str, Any]:
    data: Dict[str, Any] = {"id": team_id(name), "homeAway": side,
                            "team": {"id": team_id(name), "location": name, "displayName": name + " Mascots", "shortDisplayName": name}}
    if score is not None:
        data["score"] = {"value": float(score), "displayValue": str(score)}
    if rank is not None:
        data["curatedRank"] = {"current": rank}
    return data


def game(home: str, away: str, date: str, *, home_score: Optional[int] = None, away_score: Optional[int] = None,
         home_rank: Optional[int] = None, away_rank: Optional[int] = None, state: str = "pre", time_valid: bool = True,
         neutral: bool = False) -> Dict[str, Any]:
    """One event: state is "pre" (to play), "in" (in progress) or "post" (over); neutral marks a game at a neutral site (the side listed
    home is then only the one ESPN lists first)."""
    done = state == "post"
    status = {"type": {"state": state, "completed": done, "name": "STATUS_FINAL" if done else "STATUS_SCHEDULED"}}
    return {"id": f"{home}-{away}-{date}", "date": date, "timeValid": time_valid,
            "competitions": [{"date": date, "timeValid": time_valid, "neutralSite": neutral, "status": status,
                              "competitors": [competitor(home, "home", home_score, home_rank), competitor(away, "away", away_score, away_rank)]}]}


def schedule(school: str, games: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A school's schedule: `games` are its events, in any order."""
    return {"team": {"id": team_id(school), "location": school}, "events": list(games)}


def michigan() -> Dict[str, Any]:
    return schedule("Michigan", [
        game("Michigan", "Ohio State", "2026-10-10T20:00Z", home_score=31, away_score=24, home_rank=12, state="post"),
        game("Purdue", "Michigan", "2026-10-24T20:00Z", home_rank=None, away_rank=12),
    ])


def texas_am() -> Dict[str, Any]:
    return schedule("Texas A&M", [
        game("LSU", "Texas A&M", "2026-10-10T23:30Z", home_score=24, away_score=21, home_rank=18, away_rank=20, state="post"),
        game("Texas A&M", "Arkansas", "2026-10-17T19:00Z", home_rank=20),
    ])


def missouri() -> Dict[str, Any]:
    return schedule("Missouri", [
        game("Missouri", "Vanderbilt", "2026-10-11T00:00Z", home_score=35, away_score=10, home_rank=9, state="post"),
        game("Missouri", "Texas", "2026-10-19T19:00Z", home_rank=9, away_rank=4),
    ])


def longest_names() -> Dict[str, Any]:
    """Texas A&M's schedule with the longest names its opponents give the strip, Mississippi State and Louisiana State, ranked: "Texas A&M
    (#12) 45 – Mississippi State (#15) 42" over "Next: 10/19 · Louisiana State (#9) @ Texas A&M (#12)"."""
    return schedule("Texas A&M", [
        game("Texas A&M", "Mississippi State", "2026-10-10T20:00Z", home_score=45, away_score=42, home_rank=12, away_rank=15, state="post"),
        game("Texas A&M", "Louisiana State", "2026-10-19T19:00Z", home_rank=12, away_rank=9),
    ])


def every_school() -> Dict[int, Dict[str, Any]]:
    """What the feed would answer for each school's team id."""
    return {251: texas_sample(), 130: michigan(), 245: texas_am(), 142: missouri()}


class Feed:
    """The fetcher the service is given: answers with a schedule for each team id, records what it was asked, and can be down."""

    def __init__(self, answers=None):
        self.answers = every_school() if answers is None else answers
        self.asked = []
        self.down = None                  # an exception raised for every team
        self.down_for = {}                # team id -> an exception raised for that team alone

    async def __call__(self, team_id):
        self.asked.append(team_id)
        failure = self.down or self.down_for.get(team_id)
        if failure is not None:
            raise failure
        return copy.deepcopy(self.answers[team_id])


class Clock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
