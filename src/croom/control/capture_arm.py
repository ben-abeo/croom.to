"""
Settings › Check's "Capture the next Google Meet call" (spec 2026-10-30 Meet controls, section 3). Settings closes when a meeting starts
(the screen is shared), so the capture cannot be pressed during the call: it is armed beforehand instead. Armed, it waits for the room to
have been connected to a Google Meet call for the chosen delay (1, 3 or 5 minutes, time to knock, admit, chat, react and make the room a
co-host), runs the capture once and disarms. A call that ends before the delay leaves it armed for the next one. The arm lasts two hours
and is kept in memory only: a restart of the room software forgets it.
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from croom.meeting.providers.base import MeetingState

logger = logging.getLogger(__name__)


class CaptureArm:
    ARMED_FOR_S = 2 * 3600
    MINUTE_S = 60
    DELAYS = (1, 3, 5)

    def __init__(self, meeting, run: Callable[[bool], Awaitable[Any]], clock: Callable[[], float] = time.time):
        self._meeting = meeting
        self._run = run                    # runs the capture now, with or without screenshots; it says what became of it itself
        self._clock = clock
        self.until: Optional[float] = None
        self.after_minutes = 3
        self.screenshots = True
        self.fires_at: Optional[float] = None
        self._timer: Optional[asyncio.Task] = None
        self._listening = False

    @property
    def armed(self) -> bool:
        return self.until is not None and self._clock() < self.until

    def arm(self, after_minutes: int, screenshots: bool) -> None:
        """Arm (again: the last arm is replaced) for ARMED_FOR_S. In a Meet call already, the delay counts from now."""
        if after_minutes not in self.DELAYS:
            raise ValueError(f"after_minutes is one of {', '.join(map(str, self.DELAYS))}")
        self._stop_timer()
        self.until = self._clock() + self.ARMED_FOR_S
        self.after_minutes, self.screenshots = after_minutes, bool(screenshots)
        if not self._listening:
            self._meeting.add_state_callback(self._on_state)
            self._listening = True
        if self._in_meet_call():
            self._start_timer()
        logger.info(f"The capture of Google Meet is armed: {after_minutes} min into the next Meet call, for two hours")

    def cancel(self) -> None:
        self.until = None
        self._stop_timer()

    def close(self) -> None:
        """The agent stops: the wait ends (a capture that is running is not cut short)."""
        self._stop_timer()

    def _in_meet_call(self) -> bool:
        meeting = getattr(self._meeting, "current_meeting", None)
        return self._meeting.state == MeetingState.CONNECTED and getattr(meeting, "platform", None) == "google_meet"

    def _on_state(self, state: MeetingState) -> None:
        """The meeting's state changed: a Meet call connected starts the wait, anything else ends it (the arm stays for the next call)."""
        try:
            if state == MeetingState.CONNECTED:
                if self.armed and self._timer is None and self._in_meet_call():
                    self._start_timer()
            else:
                self._stop_timer()
        except Exception:  # noqa: BLE001 - a state callback must not raise into the provider
            logger.exception("The armed capture of Google Meet could not follow the meeting")

    def _start_timer(self) -> None:
        self.fires_at = self._clock() + self.after_minutes * self.MINUTE_S
        self._timer = asyncio.get_running_loop().create_task(self._fire())

    def _stop_timer(self) -> None:
        timer, self._timer, self.fires_at = self._timer, None, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()

    async def _fire(self) -> None:
        try:
            await asyncio.sleep(self.after_minutes * self.MINUTE_S)
        except asyncio.CancelledError:
            return
        self._timer, self.fires_at = None, None   # from here on a call that ends does not cancel the capture: it stops by itself
        if not self.armed or not self._in_meet_call():
            return
        self.until = None                          # once
        logger.info("The armed capture of Google Meet runs")
        await self._run(self.screenshots)

    def state(self) -> Dict[str, Any]:
        return {"armed": self.armed, "until": self.until if self.armed else None, "after_minutes": self.after_minutes,
                "screenshots": self.screenshots, "runs_at": self.fires_at if self._timer is not None else None}
