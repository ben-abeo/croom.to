"""
Laptop mode (spec docs/superpowers/specs/2026-10-27-laptop-switch-design.md): the room's MeetUp lent to a laptop at the table
through the USB switch and given back, the TV following it, a booking holding the time, and Join refused while it is away.

Where the MeetUp is comes from the switch (is it on this Pi's USB?), looked at every WATCH_S. What Laptop mode adds to that (until
when, the booking it follows, whether it made that booking) is kept in laptop.json, so a restart keeps the time to give it back.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from croom.calendar.bookings import BookingError
from croom.control.settings import SettingsStore
from croom.core.service import Service
from croom.laptop.errors import (CAMERA_AWAY, HOW_LONG, IN_A_MEETING, NOT_READY, SOURCE, SWITCHING, TITLE, TV_FAILED,  # noqa: F401
                                 LaptopError)
from croom.laptop.switch import LAPTOP, NO_ANSWER, ROOM, UsbSwitch
from croom.meeting.providers.base import MeetingState

logger = logging.getLogger(__name__)

BUSY_STATES = (MeetingState.JOINING, MeetingState.IN_LOBBY, MeetingState.CONNECTED, MeetingState.LEAVING)


class LaptopService(Service):
    WATCH_S = 2.0

    def __init__(self, switch, store: SettingsStore, calendar=None, meeting=None, devices=None, tv=None,
                 clock: Optional[Callable[[], datetime]] = None):
        super().__init__("laptop")
        self._switch, self._store = switch, store
        self._calendar, self._meeting, self._devices, self._tv = calendar, meeting, devices, tv
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._lock = asyncio.Lock()
        self._watch_task: Optional[asyncio.Task] = None
        self._problem: Optional[str] = None
        self._active, self._until, self._key, self._made = False, None, None, False
        self._load()

    @classmethod
    def from_config(cls, config, calendar=None, meeting=None, devices=None, tv=None, switch=None) -> "LaptopService":
        return cls(switch or UsbSwitch.from_config(config), SettingsStore(config.resolve_data_dir() / "laptop.json"),
                   calendar=calendar, meeting=meeting, devices=devices, tv=tv)

    # -- what is kept

    def _load(self) -> None:
        saved = self._store.get("laptop")
        if not isinstance(saved, dict) or not saved.get("active"):
            return
        try:
            until = datetime.fromisoformat(saved["until"]) if saved.get("until") else None
        except (TypeError, ValueError):
            until = None
        key = saved.get("booking")
        self._active, self._until = True, until
        self._key, self._made = (key if isinstance(key, str) else None), bool(saved.get("made_booking"))

    def _set(self, active: bool, until: Optional[datetime] = None, key: Optional[str] = None, made: bool = False) -> None:
        self._active, self._until, self._key, self._made = active, until, key, made
        data = {"active": active, "until": until.isoformat() if until else None, "booking": key, "made_booking": made}
        try:
            self._store.save("laptop", data)
        except (OSError, ValueError) as e:
            logger.warning(f"Could not keep Laptop mode in {self._store.path}: {e}")

    # -- what the page reads

    @property
    def bookings(self):
        return getattr(self._calendar, "bookings", None)

    @property
    def camera_away(self) -> bool:
        return self._active

    def state(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "available": self._switch.available,
            "holder": LAPTOP if self._active else ROOM,
            "until": self._until.isoformat() if self._until else None,
            "made_booking": self._made,
            "switching": self._lock.locked(),
            "problem": self._problem or (None if self._switch.available else self._switch.problem),
        }

    # -- the lifecycle

    async def start(self) -> None:
        self._switch.open()
        await self._tick()          # a restart in Laptop mode: an end that has passed gives the MeetUp back now
        self._watch_task = asyncio.create_task(self._watch())
        logger.info("Laptop mode ready" if self._switch.available else f"Laptop mode unavailable: {self._switch.problem}")

    async def stop(self) -> None:
        task, self._watch_task = self._watch_task, None
        if task is not None:
            task.cancel()
            await asyncio.wait({task})
        self._switch.close()

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self.WATCH_S)
            await self._tick()

    async def _tick(self) -> None:
        """One look: the MeetUp moved at the switch itself, the booking's end moved in Google, or the end has come."""
        if self._lock.locked():
            return
        try:
            holder = self._switch.holder()
            if holder == LAPTOP and not self._active:
                logger.info("The MeetUp left this Pi without Laptop mode asking: the laptop has it")
                self._set(True)
                await self._release()
            elif holder == ROOM and self._active:
                logger.info("The MeetUp came back to this Pi at the switch: Laptop mode ends")
                self._set(False)
                await self._reclaim()
            elif self._active:
                self._follow_booking()
                if self._until is not None and self._clock() >= self._until:
                    logger.info("The laptop's time is up: the MeetUp goes back to the room")
                    await self.to_room(by_hand=False)
        except LaptopError as e:
            self._problem = str(e)
        except Exception:  # noqa: BLE001 - the watch must go on
            logger.exception("Laptop mode's look at the switch failed")

    def _follow_booking(self) -> None:
        bookings = self.bookings
        if bookings is None or self._key is None:
            return
        event = bookings.find(self._key)
        if event is not None and event.end_time != self._until:
            logger.info(f"The laptop's booking now ends at {event.end_time:%H:%M}")
            self._set(True, event.end_time, self._key, self._made)

    # -- what the page asks

    async def to_laptop(self, minutes: Optional[int] = None) -> Dict[str, Any]:
        """Lend the MeetUp to the laptop: for the booking in progress, or for `minutes` booked now as "Laptop" (BookingError when
        that time is taken, before anything is pressed), or with no end in a room with no calendar to book in."""
        if not self._switch.available:
            raise LaptopError(self._switch.problem or NOT_READY)
        if self._meeting is not None and getattr(self._meeting, "state", None) in BUSY_STATES:
            raise LaptopError(IN_A_MEETING)
        if self._lock.locked():
            raise LaptopError(SWITCHING)
        async with self._lock:
            if self._active:
                return self.state()
            bookings = self.bookings
            ongoing = bookings.in_progress() if bookings is not None else None
            if ongoing is not None:
                until, key, made = ongoing.end_time, ongoing.booking_id or ongoing.id, False
            elif bookings is not None:
                if not isinstance(minutes, int) or isinstance(minutes, bool):
                    raise LaptopError(HOW_LONG, 400)
                booking = await bookings.start_now(minutes, TITLE, SOURCE)
                until, key, made = booking.end, booking.id, True
            else:
                until, key, made = None, None, False
            self._problem = None
            await self._release()
            if not await self._switch.move_to(LAPTOP):
                if made:
                    await self._end_booking(key)
                await self._reclaim()
                self._problem = NO_ANSWER
                raise LaptopError(NO_ANSWER)
            self._set(True, until, key, made)
            self._problem = await self._show("laptop")
            logger.info(f"The MeetUp is on the laptop{' until ' + format(until, '%H:%M') if until else ''}")
            return self.state()

    async def to_room(self, by_hand: bool = True) -> Dict[str, Any]:
        """Give the MeetUp back to the room. By hand, a booking Laptop mode made is ended now; at its end it has run its time."""
        if self._lock.locked():
            raise LaptopError(SWITCHING)
        async with self._lock:
            if not self._switch.available:
                raise LaptopError(self._switch.problem or NOT_READY)
            if not await self._switch.move_to(ROOM):
                self._problem = NO_ANSWER
                raise LaptopError(NO_ANSWER)
            key, made = self._key, self._made
            self._set(False)
            await self._reclaim()
            if by_hand and made and key:
                await self._end_booking(key)
            logger.info("The MeetUp is back on the room")
            return self.state()

    # -- the others

    async def _release(self) -> None:
        if self._devices is not None:
            await self._devices.release()

    async def _reclaim(self) -> None:
        if self._devices is not None:
            await self._devices.reclaim()
        self._problem = await self._show("pi")

    async def _show(self, which: str) -> Optional[str]:
        if self._tv is None:
            return None
        try:
            return await self._tv.show_input(which)
        except Exception as e:  # noqa: BLE001 - the TV never stops the switch
            logger.warning(f"The TV did not switch to the {which} input: {type(e).__name__}: {e}")
            return TV_FAILED

    async def _end_booking(self, key: Optional[str]) -> None:
        bookings = self.bookings
        if bookings is None or key is None:
            return
        try:
            await bookings.end(key)
        except BookingError as e:
            logger.info(f"The laptop's booking was left as it is: {e}")
