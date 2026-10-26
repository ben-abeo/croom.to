"""
Room bookings made at the door sign and the room page (spec 2026-10-25).

A booking is kept on the device first (bookings.json in the data directory) and sent to the room's Google calendar from
there, so the room books, starts, ends and cancels its own meetings while Google is out of reach. The last calendar read
from Google is kept too (calendar-cache.json), so a device that restarts while Google is out of reach still has its day.

What the room shows is Google's bookings with the room's own laid over them: a booking of the room's is the room's
version (it may hold a change Google has not had yet), and Google's word goes for one with no change waiting.

Each booking carries the room's tag (booking_id) on its Google event, which is how End now and Cancel know a booking
as the room's own, and how a create whose answer was lost is found instead of being made twice. A booking written while
Google was out of reach is written whatever Google has meanwhile: the room's word goes for its own, and a booking it
overlaps is shown on it as a clash for a person to sort out. Booked while Google answers, a time Google has taken is
refused instead, with the person still at the screen.
"""

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from croom.calendar.providers.base import CalendarEvent, CalendarWriteRefused, MeetingPlatform

logger = logging.getLogger(__name__)

STEP_MINUTES = 15
MAX_MINUTES = 240        # a booking, or Use now, is four hours at most
BOOK_DAYS = 7            # today and the six days after it: the week the room reads from Google
TITLE_MAX = 60
SOURCES = ("door sign", "room page")
KEEP_ENDED = timedelta(days=1)       # a booking that ended this long ago, with nothing to send, is let go
INTERACTIVE_WAIT_S = 10.0            # how long a booking at the screen waits for Google before it is kept to send later
REFUSED = ("The room's calendar does not take bookings from Crystal Meet: share it with the service account "
           "with Make changes to events. Bookings made here are kept and sent when it does.")

Span = Tuple[datetime, datetime, str]


class BookingError(ValueError):
    """A booking the room cannot make as asked: the words are for the person at the screen."""
    status = 400


class SlotTaken(BookingError):
    status = 409


class UnknownBooking(BookingError):
    status = 404


# ----------------------------------------------------------------------------------------------------------------------
# The open start times and the durations


def local(day: date, at: time) -> datetime:
    """A time of day on the device's own clock, with that day's offset (a day the clocks change keeps its hours)."""
    return datetime.combine(day, at).astimezone()


def parse_hour(text: str) -> time:
    try:
        parsed = time.fromisoformat(str(text).strip())
    except ValueError:
        raise ValueError(f"booking hours are HH:MM, not {text!r}") from None
    if parsed.minute % STEP_MINUTES or parsed.second:
        raise ValueError(f"booking hours are on the quarter hour, not {text!r}")
    return parsed


def next_step(moment: datetime) -> datetime:
    """The quarter hour at or after a moment."""
    floor = moment.replace(minute=moment.minute - moment.minute % STEP_MINUTES, second=0, microsecond=0)
    return floor if floor == moment else floor + timedelta(minutes=STEP_MINUTES)


def _durations(start: datetime, limit: datetime) -> List[int]:
    longest = min(int((limit - start).total_seconds() // 60), MAX_MINUTES)
    return list(range(STEP_MINUTES, longest - longest % STEP_MINUTES + 1, STEP_MINUTES)) if longest >= STEP_MINUTES else []


def open_starts(day: date, now: datetime, busy: List[Span], start_hour: time, end_hour: time) -> List[Tuple[datetime, List[int]]]:
    """The quarter hours of a day's booking hours that are free, each with the durations it allows: 15 minutes to four hours in
    steps of 15, up to the next booking and the end of the hours. Today's start at the next quarter hour."""
    first, last = local(day, start_hour), local(day, end_hour)
    starts = []
    moment = max(first, next_step(now))
    while moment < last:
        step_end = moment + timedelta(minutes=STEP_MINUTES)
        if not any(s < step_end and e > moment for s, e, _ in busy):
            following = min((s for s, _, _ in busy if s >= step_end), default=last)
            starts.append((moment, _durations(moment, min(following, last))))
        moment = step_end
    return starts


def nearest_step(moment: datetime) -> datetime:
    """The quarter hour nearest a moment; halfway goes on to the later one."""
    floor = moment.replace(minute=moment.minute - moment.minute % STEP_MINUTES, second=0, microsecond=0)
    return floor + timedelta(minutes=STEP_MINUTES) if moment - floor >= timedelta(minutes=STEP_MINUTES) / 2 else floor


def now_ends(now: datetime, busy: List[Span]) -> List[Tuple[int, datetime]]:
    """What Use now offers at this moment: each length (15 minutes to four hours, in steps of 15) with the quarter hour it ends on, the one
    nearest the length asked (Ben, 2026-10-26: at 12:03 half an hour is until 12:30, not 12:33), up to the next booking. Nothing while the room
    is taken, or when the first of them would run into the next booking."""
    if any(s <= now < e for s, e, _ in busy):
        return []
    following = min((s for s, _, _ in busy if s > now), default=None)
    ends = []
    for minutes in range(STEP_MINUTES, MAX_MINUTES + 1, STEP_MINUTES):
        end = nearest_step(now + timedelta(minutes=minutes))
        if following is not None and end > following:
            break
        ends.append((minutes, end))
    return ends


def now_minutes(now: datetime, busy: List[Span]) -> Tuple[List[int], Optional[datetime]]:
    """The lengths Use now offers at this moment (now_ends), and the start of the booking that cuts them short."""
    following = None if any(s <= now < e for s, e, _ in busy) else min((s for s, _, _ in busy if s > now), default=None)
    return [minutes for minutes, _ in now_ends(now, busy)], following


# ----------------------------------------------------------------------------------------------------------------------
# The bookings kept on the device


@dataclass
class Booking:
    id: str
    title: str
    start: datetime
    end: datetime
    source: str
    created: datetime
    google_id: Optional[str] = None
    cancelled: bool = False
    pending: bool = True     # a change of it is still to reach Google

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "title": self.title, "start": self.start.isoformat(), "end": self.end.isoformat(),
                "source": self.source, "created": self.created.isoformat(), "google_id": self.google_id,
                "cancelled": self.cancelled, "pending": self.pending}

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Booking":
        return cls(id=str(data["id"]), title=str(data["title"]), start=datetime.fromisoformat(data["start"]),
                   end=datetime.fromisoformat(data["end"]), source=str(data.get("source", "")),
                   created=datetime.fromisoformat(data["created"]), google_id=data.get("google_id"),
                   cancelled=bool(data.get("cancelled")), pending=bool(data.get("pending", True)))

    def event(self, calendar_id: str = "") -> CalendarEvent:
        return CalendarEvent(id=self.google_id or "room-" + self.id, title=self.title, start_time=self.start, end_time=self.end,
                             calendar_id=calendar_id, booking_id=self.id, pending=self.pending)


def _write_json(path: Path, data: Any) -> None:
    """Whole or not at all: a power cut mid-write leaves the last file, not half of a new one. Readable by the agent's user only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(temporary, path)


def _read_json(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning(f"Ignoring {path}: {e}")
        return None


def event_to_cache(e: CalendarEvent) -> Dict[str, Any]:
    return {"id": e.id, "title": e.title, "start": e.start_time.isoformat(), "end": e.end_time.isoformat(),
            "meeting_url": e.meeting_url, "meeting_platform": e.meeting_platform.value, "meeting_passcode": e.meeting_passcode,
            "is_all_day": e.is_all_day, "status": e.status, "response_status": e.response_status, "booking_id": e.booking_id,
            "calendar_id": e.calendar_id}


def event_from_cache(d: Dict[str, Any]) -> CalendarEvent:
    try:
        platform = MeetingPlatform(d.get("meeting_platform") or "unknown")
    except ValueError:
        platform = MeetingPlatform.UNKNOWN
    return CalendarEvent(id=str(d["id"]), title=str(d.get("title", "")), start_time=datetime.fromisoformat(d["start"]),
                         end_time=datetime.fromisoformat(d["end"]), meeting_url=d.get("meeting_url"), meeting_platform=platform,
                         meeting_passcode=d.get("meeting_passcode"), is_all_day=bool(d.get("is_all_day")),
                         status=str(d.get("status", "confirmed")), response_status=str(d.get("response_status", "accepted")),
                         booking_id=d.get("booking_id"), calendar_id=str(d.get("calendar_id", "")))


def busy_spans(events: List[CalendarEvent], leave_out: Optional[str] = None) -> List[Span]:
    """What takes the room: every booking but a cancelled or declined one (and, when one is being checked, itself)."""
    return [(e.start_time, e.end_time, e.title) for e in events
            if e.status != "cancelled" and e.response_status != "declined" and (leave_out is None or e.booking_id != leave_out)]


class RoomBookings:
    """The room's own bookings, the last calendar read from Google, and the sync between them. The calendar service hands it what
    Google has after every poll (remember), says whether Google answers (set_online), and asks it to send what waits (sync)."""

    def __init__(self, store_path, cache_path, room_name: str, booking_start: str = "08:00", booking_end: str = "18:00",
                 clock: Optional[Callable[[], datetime]] = None):
        self.store_path = Path(store_path)
        self.cache_path = Path(cache_path)
        self.room_name = room_name
        self.start_hour = parse_hour(booking_start)
        self.end_hour = parse_hour(booking_end)
        if self.end_hour <= self.start_hour:
            raise ValueError(f"booking hours end ({booking_end}) before they start ({booking_start})")
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._provider = None
        self._calendar_id = ""
        self._online = False
        self._problem: Optional[str] = None
        self._lock = asyncio.Lock()
        self._revision = 0       # counts the changes to what the room shows: a screen reads the day again when it moves
        self._records: List[Booking] = self._load()
        self._google, self._saved_at = self.cached()

    # -- files

    def _load(self) -> List[Booking]:
        data = _read_json(self.store_path) or []
        records = []
        for item in data if isinstance(data, list) else []:
            try:
                records.append(Booking.from_json(item))
            except (KeyError, TypeError, ValueError) as e:
                logger.warning(f"Ignoring a booking in {self.store_path}: {e}")
        return records

    def _save(self) -> None:
        self._revision += 1
        cutoff = self.clock() - KEEP_ENDED
        self._records = [b for b in self._records if b.pending or b.end > cutoff]
        _write_json(self.store_path, [b.to_json() for b in self._records])

    def cached(self) -> Tuple[List[CalendarEvent], Optional[datetime]]:
        """The calendar as Google last gave it, and when: what a device that restarts without Google shows."""
        data = _read_json(self.cache_path)
        if not isinstance(data, dict):
            return [], None
        events = []
        for item in data.get("events", []):
            try:
                events.append(event_from_cache(item))
            except (KeyError, TypeError, ValueError):
                continue
        try:
            saved_at = datetime.fromisoformat(data["saved_at"])
        except (KeyError, TypeError, ValueError):
            saved_at = None
        return events, saved_at

    # -- Google, as the calendar service has it

    def attach(self, provider, calendar_id: str) -> None:
        self._provider, self._calendar_id = provider, calendar_id
        self._online = True

    def set_online(self, online: bool) -> None:
        self._online = online

    def remember(self, events: List[CalendarEvent]) -> None:
        """A poll's events: kept for a restart, and Google's word on the room's bookings that have no change waiting (moved in Google,
        or deleted there)."""
        now = self.clock()
        window_start = now - timedelta(hours=1)
        if [event_to_cache(e) for e in events] != [event_to_cache(e) for e in self._google]:
            self._revision += 1
        self._google = list(events)
        self._saved_at = now
        self._online = True
        by_tag = {e.booking_id: e for e in events if e.booking_id}
        by_id = {e.id: e for e in events}
        changed = False
        for b in list(self._records):
            if b.pending:
                continue
            found = by_tag.get(b.id) or (by_id.get(b.google_id) if b.google_id else None)
            if found is None:
                if b.end > window_start:          # inside what was read, and not there: deleted in Google
                    self._records.remove(b)
                    changed = True
            elif (found.title, found.start_time, found.end_time, found.id) != (b.title, b.start, b.end, b.google_id):
                b.title, b.start, b.end, b.google_id = found.title, found.start_time, found.end_time, found.id
                changed = True
        if changed:
            self._save()
        try:
            _write_json(self.cache_path, {"saved_at": now.isoformat(), "events": [event_to_cache(e) for e in events]})
        except OSError as e:
            logger.warning(f"Could not keep the calendar in {self.cache_path}: {e}")

    # -- what the room shows

    def merge(self, google_events: Optional[List[CalendarEvent]] = None) -> List[CalendarEvent]:
        """Google's bookings with the room's own laid over them, in start order, each of the room's flagged with a booking it overlaps."""
        google_events = self._google if google_events is None else google_events
        tags = {b.id for b in self._records}
        google_ids = {b.google_id for b in self._records if b.google_id}
        merged = [e for e in google_events if e.booking_id not in tags and e.id not in google_ids]
        merged += [b.event(self._calendar_id) for b in self._records if not b.cancelled]
        others = [e for e in merged if not e.booking_id and e.status != "cancelled" and e.response_status != "declined"]
        for e in merged:
            if e.booking_id:
                e.clash = next((o.title for o in others if o.start_time < e.end_time and o.end_time > e.start_time), None)
        return sorted(merged, key=lambda e: e.start_time)

    def busy(self, leave_out: Optional[str] = None) -> List[Span]:
        return busy_spans(self.merge(), leave_out)

    def slots(self, day: date) -> Dict[str, Any]:
        starts = open_starts(day, self.clock(), self.busy(), self.start_hour, self.end_hour) if self._bookable_day(day) else []
        return {"date": day.isoformat(), "starts": [{"start": s.isoformat(), "durations": d} for s, d in starts]}

    def days(self) -> List[date]:
        today = self.clock().date()
        return [today + timedelta(days=n) for n in range(BOOK_DAYS)]

    def _bookable_day(self, day: date) -> bool:
        return day in self.days()

    def state(self) -> Dict[str, Any]:
        now = self.clock()
        ends = now_ends(now, self.busy())
        _, until = now_minutes(now, self.busy())
        return {
            "online": self._online,
            "offline_since": self._saved_at.isoformat() if not self._online and self._saved_at else None,
            "pending": sum(1 for b in self._records if b.pending),
            "problem": self._problem,
            "revision": self._revision,
            "hours": {"start": self.start_hour.strftime("%H:%M"), "end": self.end_hour.strftime("%H:%M")},
            "now": {"minutes": [m for m, _ in ends], "ends": [e.isoformat() for _, e in ends], "until": until.isoformat() if until else None},
        }

    # -- what the screens do

    @staticmethod
    def _title(title: Any) -> str:
        text = " ".join(str(title or "").split())
        if not text:
            raise BookingError("Type your name or the meeting's name")
        if len(text) > TITLE_MAX:
            raise BookingError(f"The name is {TITLE_MAX} characters at most")
        return text

    @staticmethod
    def _source(source: Any) -> str:
        if source not in SOURCES:
            raise BookingError("Unknown screen")
        return str(source)

    async def book(self, start: datetime, minutes: int, title: str, source: str) -> Booking:
        title, source = self._title(title), self._source(source)
        day = start.astimezone().date()
        if not self._within_the_rules(start, minutes):
            raise BookingError("That time is not open for booking")
        offered = dict(open_starts(day, self.clock(), self.busy(), self.start_hour, self.end_hour))
        if minutes not in offered.get(start, []):
            raise SlotTaken("That time is already booked")   # within the rules, so another booking has it
        return await self._add(title, start, start + timedelta(minutes=minutes), source)

    def _within_the_rules(self, start: datetime, minutes: int) -> bool:
        """A start and length the room would offer on a free day: this week, on the quarter hour, inside the booking hours, not past."""
        day = start.astimezone().date()
        return (self._bookable_day(day) and start == next_step(start) and start >= self.clock()
                and minutes in range(STEP_MINUTES, MAX_MINUTES + 1, STEP_MINUTES)
                and local(day, self.start_hour) <= start and start + timedelta(minutes=minutes) <= local(day, self.end_hour))

    async def start_now(self, minutes: int, title: str, source: str) -> Booking:
        title, source = self._title(title), self._source(source)
        now = self.clock().replace(second=0, microsecond=0)
        allowed = dict(now_ends(self.clock(), self.busy()))
        if minutes not in allowed:
            if not allowed:
                raise SlotTaken("The room is booked now or very soon")
            raise BookingError("Choose a duration the time allows")
        return await self._add(title, now, allowed[minutes], source)   # from this minute to the quarter hour nearest the length

    async def _add(self, title: str, start: datetime, end: datetime, source: str) -> Booking:
        booking = Booking(id=uuid.uuid4().hex[:12], title=title, start=start, end=end, source=source, created=self.clock())
        self._records.append(booking)
        self._save()
        if self._provider is not None:
            try:
                async with self._lock:
                    await asyncio.wait_for(self._send(booking, at_the_screen=True), INTERACTIVE_WAIT_S)
            except SlotTaken:
                self._records.remove(booking)
                self._save()
                raise
            except asyncio.TimeoutError:
                self._online = False
                logger.warning(f"Google did not answer in {INTERACTIVE_WAIT_S:.0f} s: keeping {title!r} to send later")
        return booking

    def _find(self, key: str) -> Booking:
        for b in self._records:
            if not b.cancelled and key in (b.id, b.google_id):
                return b
        for e in self._google:   # the room's own, known only from Google (a new card lost the file): take it back on
            if e.booking_id and key in (e.booking_id, e.id):
                b = Booking(id=e.booking_id, title=e.title, start=e.start_time, end=e.end_time, source="", created=self.clock(),
                            google_id=e.id, pending=False)
                self._records.append(b)
                return b
        raise UnknownBooking("That booking was not made at this room's screens")

    async def end(self, key: str) -> Booking:
        """End now: a booking in progress ends this minute (one that has not started is cancelled)."""
        booking = self._find(key)
        now = self.clock().replace(second=0, microsecond=0)
        if booking.start >= now:
            return await self.cancel(key)
        if booking.end <= now:
            raise BookingError("That booking has already ended")
        booking.end = max(now, booking.start + timedelta(minutes=1))
        return await self._changed(booking)

    async def cancel(self, key: str) -> Booking:
        booking = self._find(key)
        if booking.start < self.clock().replace(second=0, microsecond=0):
            raise BookingError("That booking has started: end it instead")
        booking.cancelled = True
        return await self._changed(booking)

    async def _changed(self, booking: Booking) -> Booking:
        booking.pending = True
        self._save()
        if self._provider is not None:
            try:
                async with self._lock:
                    await asyncio.wait_for(self._send(booking), INTERACTIVE_WAIT_S)
            except asyncio.TimeoutError:
                self._online = False
        return booking

    # -- the sync

    async def sync(self) -> None:
        """Send every change that waits, oldest booking first; stop at the first that cannot go (Google is out of reach)."""
        if self._provider is None:
            return
        async with self._lock:
            for booking in sorted((b for b in self._records if b.pending), key=lambda b: b.created):
                if not await self._send(booking):
                    break

    async def _send(self, booking: Booking, at_the_screen: bool = False) -> bool:
        """One booking's change to Google. True when it went (or there was nothing to send); False when Google is out of reach or
        refuses, and the change waits. At the screen, a time Google has taken since the last poll is refused (SlotTaken)."""
        provider, calendar_id = self._provider, self._calendar_id
        try:
            if booking.google_id is None:
                booking.google_id = await provider.find_booking(calendar_id, booking.id)   # a create whose answer was lost
            if booking.cancelled:
                if booking.google_id:
                    await provider.delete_booking(calendar_id, booking.google_id)
                self._records.remove(booking)
            elif booking.google_id:
                await provider.update_booking(calendar_id, booking.google_id, booking.title, booking.start, booking.end)
            else:
                if at_the_screen:
                    taken = [e for e in await provider.get_events(calendar_id, booking.start, booking.end)
                             if e.booking_id != booking.id and e.status != "cancelled" and e.response_status != "declined"
                             and e.start_time < booking.end and e.end_time > booking.start]
                    if taken:
                        raise SlotTaken(f"That time was just booked in Google Calendar: {taken[0].title}")
                verb = "Started" if booking.start == booking.created.replace(second=0, microsecond=0) else "Booked"
                booking.google_id = await provider.create_booking(
                    calendar_id, booking.id, booking.title, booking.start, booking.end, f"{verb} on the {self.room_name} {booking.source}")
            booking.pending = False
        except SlotTaken:
            raise
        except CalendarWriteRefused as e:
            self._problem = REFUSED
            logger.warning(f"Google refused {booking.title!r}: {e}")
            self._save()
            return False
        except Exception as e:
            self._online = False
            logger.warning(f"Could not send {booking.title!r} to Google, keeping it to send later: {e}")
            self._save()
            return False
        self._problem = None
        self._online = True
        self._save()
        return True
