"""
Occupancy: how many people are in the room, counted from the camera once a minute (people.py: the AI HAT's bodies and faces, or
YuNet's faces without it). Off unless the room's setting is on (control.occupancy): it looks at the room outside calls too, and the
camera's light comes on for each look.

Each look is one still from the TV's browser (CameraView.still, as Frame people's), so nothing else opens the camera, and there is
none in Laptop mode or while the call's camera is off: the count is then not known (None), never 0. Only the number is kept; no
picture is. The room's status carries it, and the dashboard is sent it (its Average and Peak Occupancy) while anyone is there
and once when the room empties.

Releasing a booking nobody came to (control.release_empty_minutes, 0: never): a booking in progress whose every look so far has found
the room empty, for that many looks (minutes) in a row, is ended at the room and in Google as End now ends it (RoomBookings.end), so
the room shows free and can be booked. Anyone seen once, or a call joined, keeps the booking for good; a look with no picture says
nothing either way. The camera sees what it points at: a room whose home leaves seats out of the picture should not release.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional

from croom.devices import people
from croom.meeting.providers.base import MeetingState

logger = logging.getLogger(__name__)


class Occupancy:
    LOOK_S = 60.0       # between looks

    def __init__(self, camera, meeting, on: bool = False, count: Callable[[bytes], int] = people.count,
                 report: Optional[Callable[[int], Awaitable[Any]]] = None, sleep=asyncio.sleep,
                 clock: Callable[[], datetime] = lambda: datetime.now().astimezone(), release_minutes: Any = 0):
        self._camera, self._meeting, self._on, self._count = camera, meeting, bool(on), count
        self.report = report            # where each count goes (the dashboard), set by the agent; None: nowhere
        self.bookings: Callable[[], Any] = lambda: None     # the room's bookings (RoomBookings), set by the agent; None: no calendar
        self._release = 0               # release a booking nobody came to after this many empty minutes; 0: never
        self.set_release(release_minutes)
        self._booking: Optional[str] = None     # the booking in progress as last looked at: its key
        self._came = False                      # someone has been seen during it, or a call joined: it is kept
        self._empty = 0                         # looks in a row, all of it so far, that found the room empty
        self._sleep, self._clock = sleep, clock
        self._task: Optional[asyncio.Task] = None
        self._people: Optional[int] = None
        self._at: Optional[datetime] = None
        self._said: Optional[str] = None

    def set_on(self, on: Any) -> None:
        """The room's setting, at start and when Settings changes it (at once). Off forgets the count."""
        self._on = bool(on)
        if not self._on:
            self._people = self._at = None

    def set_release(self, minutes: Any) -> None:
        """The room's setting (control.release_empty_minutes), at start and when Settings changes it (at once)."""
        self._release = minutes if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes > 0 else 0

    def state(self) -> Dict[str, Any]:
        return {"enabled": self._on, "count": self._people, "at": self._at.isoformat(timespec="seconds") if self._at else None}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.wait({task})

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - a fault here must not end the counting for good; said once per message
                if str(e) != self._said:
                    self._said = str(e)
                    logger.warning(f"Occupancy: {type(e).__name__}: {e}", exc_info=True)
            await self._sleep(self.LOOK_S)

    async def tick(self) -> None:
        """One look: the count, or None when the room gives no picture just now."""
        if not self._on:
            return
        view = getattr(self._meeting, "camera_view", None)
        picture = None
        if view is not None and view.blocked() is None and self._camera.available:
            picture = await view.still()
        if not picture:
            self._people = self._at = None
            await self._no_show()
            return
        before = self._people
        self._people, self._at = await asyncio.to_thread(self._count, picture), self._clock()
        await self._no_show()
        if self.report is not None and (self._people or before):     # while anyone is there, and once when the last has gone
            await self.report(self._people)

    async def _no_show(self) -> None:
        """After a look: end the booking in progress when nobody has come to it for the room's minutes."""
        bookings = self.bookings() if self._release else None
        booking = bookings.in_progress() if bookings is not None else None
        key = (booking.booking_id or booking.id) if booking is not None else None
        if key != self._booking:
            self._booking, self._came, self._empty = key, False, 0
        if key is None or self._came:
            return
        if self._people or getattr(self._meeting, "state", MeetingState.IDLE) != MeetingState.IDLE:
            self._came = True
        elif self._people == 0:
            self._empty += 1
            if self._empty >= self._release:
                self._came = True       # whatever comes of it, not tried again for this booking
                logger.info(f"No one came to {booking.title!r} in {self._empty} minutes: the room is released")
                try:
                    await bookings.end(key)
                except ValueError as e:     # a BookingError: ended meanwhile, or not the room's to end
                    logger.info(f"The booking was left as it is: {e}")
