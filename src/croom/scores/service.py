"""
The football scoreboard's data for the door sign (spec 2026-10-13, section 2.5): for each school the room has turned on, the last
game played and the next one to play, read from a schedule feed every ten minutes and kept between reads.

The service reads nothing while no school is on, keeps each school's last good answer through a feed that fails or answers with no game
the sign could draw, and never lets a failure reach the room's status route: current() is the one call the route makes, it is cheap and
it does not wait on the network.
"""

import asyncio
import copy
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from croom.core.service import Service
from croom.scores.espn import SCHOOLS, ScheduleUnavailable, parse_schedule

logger = logging.getLogger(__name__)

INTERVAL_S = 600   # a school's scores are read this often (a final score does not change, and a feed asked less often is a kinder one)
RETRY_S = 60       # after a read that failed, one minute: a room that boots before its network is up must not wait ten for its scores ...
BACKOFF = (1, 2, 5, 10)   # ... and then two, five and ten (times RETRY_S) for each further failure running, never more than INTERVAL_S: a feed
                          # that stays down is asked 6 times an hour by the end, not 60
CHECK_S = 2        # how often the loop looks again at which schools are on and which are due, so that a school the table has just
                   # turned on is read within seconds rather than at the next ten-minute mark
NO_GAME = "ESPN's answer had no game in it to show"

SCHOOLS_LIST, LOOP = "(the list of schools)", "(the loop)"   # keys of the problems that are not one school's

Fetcher = Callable[[int], Awaitable[Dict[str, Any]]]


class ScoresService(Service):
    """
    Args:
        fetcher: an async function that is given a team's id and returns that team's schedule as the feed sends it (espn.fetch), or
            raises. The service parses what it returns (espn.parse_schedule).
        schools: a function that returns the keys of the schools that are on, in the order they are to be shown (the room's setting
            sign_scores). It is called every time the list is needed, so a change made on the room's page is picked up by the next look;
            keys the scoreboard does not know and repeats are ignored.
        interval_s: seconds between reads of a school.
        retry_s: seconds before a read that failed is tried again; each failure running doubles it, then five times, then ten times it
            (60, 120, 300, 600 s for the default), and none waits longer than interval_s. A success starts it over.
        check_s: seconds between the loop's looks at which schools are due.
        clock: seconds from a clock that does not go back (time.monotonic); tests give it a fake.
    """

    def __init__(self, fetcher: Fetcher, schools: Callable[[], List[str]], interval_s: float = INTERVAL_S, retry_s: float = RETRY_S,
                 check_s: float = CHECK_S, clock: Callable[[], float] = time.monotonic):
        super().__init__("scores")
        self._fetcher = fetcher
        self._schools = schools
        self._interval_s = interval_s
        self._retry_s = retry_s
        self._check_s = check_s
        self._clock = clock
        self._answers: Dict[str, Dict[str, Any]] = {}   # school -> the last good answer, {"last": ..., "next": ...}
        self._due: Dict[str, float] = {}                # school -> when it is to be read next; one that is not here is read at once
        self._failures: Dict[str, int] = {}             # school -> how many reads have failed running (not here: its last read worked)
        self._reasons: Dict[str, str] = {}              # school -> why its last read failed, as said in the journal
        self._problems: Dict[str, str] = {}             # SCHOOLS_LIST, LOOP -> the problem last logged for it (a problem that is not one school's)
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # What the door sign asks for
    # ------------------------------------------------------------------

    def current(self) -> List[Dict[str, Any]]:
        """One entry per school that is on, in the order the room's setting lists them: {"school", "name", "last", "next"}. A school
        that has not been read yet, or whose feed has failed since it was turned on, has None for both. The entries are copies."""
        entries = []
        for school in self._wanted():
            answer = self._answers.get(school) or {}
            entries.append({"school": school, "name": SCHOOLS[school][1], "last": answer.get("last"), "next": answer.get("next")})
        return copy.deepcopy(entries)

    def _wanted(self) -> List[str]:
        """The schools that are on, as the setting lists them, without what the scoreboard does not know and without repeats. A list
        that cannot be had is no schools, said once: the status route and the loop both ask for it."""
        try:
            wanted = [school for school in dict.fromkeys(self._schools()) if isinstance(school, str) and school in SCHOOLS]
        except Exception as e:  # noqa: BLE001 - the status must always answer
            self._report(SCHOOLS_LIST, f"Could not tell which schools' football scores to show: {type(e).__name__}: {e}")
            return []
        self._clear(SCHOOLS_LIST)
        return wanted

    # ------------------------------------------------------------------
    # Reading the feed
    # ------------------------------------------------------------------

    async def refresh(self) -> None:
        """One look: read each school that is on and due, one after another. A read that fails, or that comes back with no game the
        sign could draw, costs that school its update and nothing else: it keeps its last good answer and is tried again soon."""
        for school in self._wanted():
            if self._due.get(school, float("-inf")) > self._clock():
                continue
            team_id, name = SCHOOLS[school]
            try:
                answer = parse_schedule(await self._fetcher(team_id))
                failure = None if answer["last"] or answer["next"] else NO_GAME   # an error object in a 200 is not "no scores"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - whatever a fetcher raises is a failed read, and the loop goes on
                failure = str(e) if isinstance(e, ScheduleUnavailable) else f"{type(e).__name__}: {e}"
            if failure is not None:
                self._failed(school, name, failure)
                continue
            self._answers[school] = answer
            self._due[school] = self._clock() + self._interval_s
            if school in self._failures:
                logger.info(f"Football scores for {name} are being read from ESPN again")
                del self._failures[school]
                self._reasons.pop(school, None)

    def _failed(self, school: str, name: str, reason: str) -> None:
        """A read that did not work: the school is asked again after a wait that grows with each failure running, and the journal is told
        once when the school starts failing (WARNING), again at INFO when the reason changes while it goes on, and otherwise only at DEBUG."""
        failures = self._failures[school] = self._failures.get(school, 0) + 1
        wait = self._retry_s * BACKOFF[min(failures, len(BACKOFF)) - 1]
        self._due[school] = self._clock() + min(wait, self._interval_s)
        kept = "keeping the last scores it read" if school in self._answers else "the door sign shows no scores for it until it can"
        message = f"Could not read the football schedule for {name} from ESPN ({reason}); {kept}"
        if failures == 1:
            logger.warning(message)
        elif reason != self._reasons.get(school):
            logger.info(message)
        else:
            logger.debug(message)
        self._reasons[school] = reason

    async def start(self) -> None:
        """Start the loop, which looks at once and then every few seconds; idle while no school is on."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())
        logger.info("Football scores started")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("Football scores stopped")

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - a bug in one look must not end the loop
                self._report(LOOP, f"The football scores loop met an error and goes on: {type(e).__name__}: {e}")
            await asyncio.sleep(self._check_s)

    # ------------------------------------------------------------------
    # The journal
    # ------------------------------------------------------------------

    def _report(self, key: str, message: str) -> None:
        """A problem is logged once at WARNING; the same problem again goes to DEBUG so that the journal stays readable."""
        if self._problems.get(key) != message:
            logger.warning(message)
            self._problems[key] = message
        else:
            logger.debug(message)

    def _clear(self, key: str) -> None:
        self._problems.pop(key, None)
