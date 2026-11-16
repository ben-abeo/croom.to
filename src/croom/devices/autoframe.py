"""
Auto framing: Frame people by itself during calls, as Logitech's RightSight does (which runs only in Logitech's own apps for Windows
and Intel Macs and on its RoomMate, so not on the room Pi). Its modes are RightSight's (control.camera_auto_framing): "off";
"call_start", where a call that connects with the room's camera on is framed once people have had a moment to sit down; and
"dynamic", where after that the picture is looked at every few seconds, and framed again when someone has come into it or reached its
edge, or someone has left it, for long enough to believe. Never sooner than a rest after the last framing, so it does not hunt.

In a room with the AI HAT (people.py), "dynamic" follows people too: the same people, moved across the picture or drawn together or
apart since the last framing, and still there some looks later, are framed again. Without the HAT it does not: YuNet loses a face that
turns away, and the group seems to move each time it does.

"track" is "dynamic" and, with the AI HAT and a camera that says where it points (the Rally), the camera steered to keep the people in
the middle as they move (_steer): the camera's live picture is looked at as fast as it comes (about ten times a second), and the
camera turns by speed, as a held arrow turns it, toward the group while its middle is away from the picture's, and stops when it is
back. A turn by speed starts in a tenth of a second or two and gathers speed the longer it is held (to about 24 degrees a second), so
a step aside is a short slow turn and someone walking across the room a long fast one. People who get out of the picture are turned
after for a moment and then looked for from the widest picture, at once and not after the count has come to believe they left. A room
without the HAT or with another camera gets "dynamic".
"""

import asyncio
import logging
import time
from typing import Any, Callable, Optional, Union

from croom.devices import framing, people
from croom.devices.errors import DeviceUnavailable, Interrupted, NotReady
from croom.meeting.providers.base import MeetingState

logger = logging.getLogger(__name__)

MODES = ("off", "call_start", "dynamic", "track")


def mode_of(value: Union[str, bool, None]) -> str:
    """The setting as a mode: one of MODES; true and false (a hand-written file) are "dynamic" and "off"; anything else is "off"."""
    if value is True:
        return "dynamic"
    if value in MODES:
        return str(value)
    if value not in (False, None):
        logger.warning(f"control.camera_auto_framing must be one of {', '.join(MODES)}, not {value!r}: Auto framing is off")
    return "off"


class AutoFramer:
    START_S = 8.0       # after the call connects with the room's camera on: people sit down, and the call's picture settles
    LOOK_S = 3.0        # between framings, a look at the picture this often
    MORE_LOOKS = 3      # someone more in the picture, or at its edge, this many looks in a row (about 9 s): frame again
    FEWER_LOOKS = 5     # someone fewer, or no one, this many looks in a row (about 15 s): a face turned away is missed now and then
    REST_S = 20.0       # never framed again sooner than this after the last framing
    EDGE = 0.04         # a head and shoulders this near the picture's edge (a fraction of it): someone at the edge
    # Following (the AI HAT's rooms): tune these in a room where the camera moves too readily, or too late
    MOVED = 0.12        # the group's middle this far (a fraction of the picture) from where the last framing left it: they have moved
    SPREAD = 1.4        # ... or the group this many times wider or narrower than it left them
    MOVED_LOOKS = 3     # moved this many looks in a row (about 9 s): someone who leaned over, or walked through, is not followed
    SOON_S = 2.0        # ... but "dynamic" and "track", which frame again as people settle, frame this soon after it connects
    # Tracking ("track"): tune these in a room where the camera fidgets, lags or overshoots
    TRACK_START = 0.10  # the group's middle this far (a fraction of the picture) from the picture's: start turning toward it
    TRACK_STOP = 0.03   # ... and this near it again: stop
    TRACK_LAG_S = 0.25  # from a picture taken to the camera at rest after a stop: the turn stops this much of its speed early
    TRACK_COAST_S = 0.7     # people out of the picture: the turn after them goes on this long
    TRACK_LOST_S = 2.0      # ... and still not in it after this long, they are looked for from the widest picture
    TRACK_FIND_S = 6.0      # never looked for again sooner than this after the last framing
    TRACK_ZOOM = 300    # the furthest Frame people zooms in while tracking: room around the people for them to move in

    def __init__(self, camera, meeting, mode: Union[str, bool, None] = "off", detect: Optional[Callable[[bytes], Any]] = None,
                 clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep, follow: Optional[bool] = None):
        self._camera = camera
        self._meeting = meeting
        self._mode = mode_of(mode)
        self._detect = detect or people.detect
        self._clock, self._sleep = clock, sleep
        self._task: Optional[asyncio.Task] = None
        self._call: Optional[float] = None      # when the call being framed was first seen ready
        self._framed: Optional[int] = None      # how many people its last framing found; None: not framed yet in this call
        self._last = 0.0                        # when it last framed
        self._more = self._fewer = 0            # looks in a row that saw more people (or one at the edge), and fewer
        self._follow = people.available() if follow is None else follow    # the same people are framed again when they move
        self._settled: Optional[framing.Box] = None    # the group's box at the first look after the last framing: where it left them
        self._moved = 0                         # looks in a row that saw them moved from there
        self._steering: Optional[asyncio.Task] = None
        self._turn = (0, 0)                     # tracking: the pan and tilt the camera is turning by (-1, 0 or 1 each)
        self._turn_began = [0.0, 0.0]           # ... and when each began
        self._asked = 0.0                       # tracking: when the camera was last told its turn
        self._seen = 0.0                        # tracking: when the people the last framing found were last all in the picture
        self._said: Optional[str] = None

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: Union[str, bool, None]) -> None:
        """The room's setting, at start and when Settings changes it (at once)."""
        self._mode = mode_of(mode)
        if self._mode == "off":
            self._show(None)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        if self._steering is None or self._steering.done():
            self._steering = asyncio.create_task(self._steer())

    async def stop(self) -> None:
        tasks = [t for t in (self._task, self._steering) if t is not None]
        self._task = self._steering = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)
        self._show(None)

    def _show(self, state: Optional[str]) -> None:
        self._camera.auto_framing = state

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - a fault here must not end Auto framing for good; said once per message
                if str(e) != self._said:
                    self._said = str(e)
                    logger.warning(f"Auto framing: {type(e).__name__}: {e}", exc_info=True)
            await self._sleep(self.LOOK_S)

    def _tracks(self) -> bool:
        """Whether the camera is kept on the people just now: the mode, the HAT, a camera that says where it points, and a call it
        has framed and that nobody has taken the camera in."""
        return (self._mode == "track" and self._follow and self._call is not None and self._framed is not None
                and self._camera.auto_framing == "on" and getattr(self._camera, "says_where", False))

    async def tick(self) -> None:
        """One step: frame a call that is ready for it, or look at the picture and frame it again when the people in it have changed."""
        in_call = self._meeting is not None and self._meeting.state == MeetingState.CONNECTED
        if not in_call:
            self._call = None                   # the next call is framed afresh
        view = getattr(self._meeting, "camera_view", None)
        if not (self._mode != "off" and in_call and view is not None and view.blocked() is None and self._camera.available):
            self._show(None)
            return
        now = self._clock()
        if self._call is None:
            self._call, self._framed, self._more, self._fewer, self._settled, self._moved = now, None, 0, 0, None, 0
        taken = self._camera.taken_over_at
        if taken is not None and taken > self._call:   # a hand move since the call was first seen; one before it does not count
            self._show("paused")                # a person has the camera in this call; their Frame people gives it back
            return
        if self._mode == "call_start" and self._framed is not None:
            self._show(None)                    # framed at the start of this call: nothing more until the next
            return
        self._show("on")
        state = self._camera.state()
        if state["busy"] or state["moving"] or state["framing"]:
            return
        if self._framed is None:
            if now - self._call >= (self.START_S if self._mode == "call_start" else self.SOON_S):
                await self._frame(view)
            return
        faces, size = await self._look(view)
        if faces is None:
            return
        widest = state["zoom"]["level"] <= state["zoom"]["min"]      # at the widest, someone at the edge cannot be helped by framing
        more = len(faces) > self._framed or (not widest and framing.at_edge(faces, size, self.EDGE))
        self._more = self._more + 1 if more else 0
        self._fewer = self._fewer + 1 if len(faces) < self._framed else 0
        self._moved = self._moved + 1 if self._has_moved(faces, size) else 0
        if max(self._more - self.MORE_LOOKS, self._fewer - self.FEWER_LOOKS, self._moved - self.MOVED_LOOKS) >= 0 \
                and now - self._last >= self.REST_S:
            await self._frame(view)

    # -- tracking

    async def _steer(self) -> None:
        """Tracking's own loop: while the camera is kept on the people, each of its live pictures is looked at and the turn set by it."""
        while True:
            view = getattr(self._meeting, "camera_view", None)
            if view is None or not self._tracks():
                await self._sleep(0.5)
                continue
            view.watch()                        # the live picture runs while it is watched
            try:
                async for picture in view.frames():
                    if not self._tracks():
                        break
                    state = self._camera.state()
                    if state["busy"] or state["framing"]:       # a framing, a Home or a preset: it has the camera
                        self._turn = (0, 0)
                        continue
                    faces, size = await asyncio.to_thread(self._detect, picture)
                    await self.steer(faces, size, view)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - a fault here must not end the tracking for good; said once per message
                if str(e) != self._said:
                    self._said = str(e)
                    logger.warning(f"Auto framing's tracking: {type(e).__name__}: {e}", exc_info=True)
                await self._sleep(1.0)
            finally:
                view.unwatch()
                await self._turn_by(0, 0)

    async def steer(self, faces, size, view) -> None:
        """One live picture while tracking: the turn that keeps the people the last framing found in the middle. Fewer of them in
        the picture: the turn after them goes on for a moment, then stops, and then they are looked for from the widest picture."""
        now = self._clock()
        if not faces or len(faces) < self._framed or not self._framed:
            if self._framed and now - self._seen > self.TRACK_COAST_S:
                await self._turn_by(0, 0)
            # No one framed and someone here now, or the ones framed gone for long enough: find them, but not over and over
            found = bool(faces) and not self._framed
            lost = bool(self._framed) and now - self._seen >= self.TRACK_LOST_S
            if (found or lost) and now - self._last >= self.TRACK_FIND_S:
                await self._turn_by(0, 0)
                await self._frame(view)
            return
        self._seen = now
        zoom = self._camera.state()["zoom"]["level"]
        left, top, right, bottom = framing.group_box(faces, size)
        away = framing.angles_to(((left + right) / 2, (top + bottom) / 2), zoom)     # degrees to turn: right and up are +
        view_deg = [2 * half for half in framing.half_view(zoom)]
        turn = list(self._turn)
        for axis in (0, 1):
            off = abs(away[axis])
            toward = 1 if away[axis] > 0 else -1
            if turn[axis] == 0:
                if off > self.TRACK_START * view_deg[axis]:
                    turn[axis] = toward
            else:
                coming = framing.turn_speed(now - self._turn_began[axis], zoom) * self.TRACK_LAG_S   # still to come once stopped
                if turn[axis] != toward or off <= max(self.TRACK_STOP * view_deg[axis], coming):
                    turn[axis] = 0
        await self._turn_by(turn[0], turn[1])

    async def _turn_by(self, pan: int, tilt: int) -> None:
        """Set the camera turning (or stop it): written when it changes, and asked again each second while it turns, which keeps the
        camera's own watchdog from stopping a turn that is still wanted."""
        now = self._clock()
        if (pan, tilt) == self._turn and ((pan, tilt) == (0, 0) or now - self._asked < 1.0):
            return
        for axis, (new, old) in enumerate(zip((pan, tilt), self._turn)):
            if new != old:
                self._turn_began[axis] = now
        try:
            state = await self._camera.move(pan, tilt, by_person=False)
        except (NotReady, DeviceUnavailable, Interrupted, ValueError) as e:
            logger.info(f"Auto framing did not turn just now: {e}")
            self._turn = (0, 0)
            return
        self._asked = now
        # A turn the camera would not start (at a limit, or the end of its range) is not one: the next picture asks again
        self._turn = (pan if state.get("moving") or not pan else 0, tilt if state.get("moving") or not tilt else 0)

    def _has_moved(self, faces, size) -> bool:
        """Whether the people the last framing found, all still in the picture, are no longer where it left them. Measured from
        where they were at the first look after it and not from the picture's middle: a framing that could do no better (the
        camera at a limit, or at its furthest zoom) is not tried again."""
        if not self._follow or not faces or len(faces) != self._framed:
            return False
        left, top, right, bottom = framing.group_box(faces, size)
        if self._settled is None:
            self._settled = (left, top, right, bottom)
            return False
        was_left, was_top, was_right, was_bottom = self._settled
        across = abs((left + right) - (was_left + was_right)) / 2
        down = abs((top + bottom) - (was_top + was_bottom)) / 2
        spread = (right - left) / (was_right - was_left)
        return across > self.MOVED or down > self.MOVED or spread > self.SPREAD or spread < 1 / self.SPREAD

    async def _frame(self, view) -> None:
        try:
            top = self.TRACK_ZOOM if self._mode == "track" and self._follow else None
            answer = await self._camera.frame_people(view.still, detect=self._detect, by_person=False, zoom_top=top)
        except (NotReady, DeviceUnavailable, Interrupted) as e:
            logger.info(f"Auto framing did not frame just now: {e}")
            return
        self._framed, self._last, self._more, self._fewer = int(answer.get("framed") or 0), self._clock(), 0, 0
        self._settled, self._moved, self._turn, self._seen = None, 0, (0, 0), self._clock()
        logger.info(f"Auto framing framed {self._framed} {'person' if self._framed == 1 else 'people'}")
        if self._mode == "call_start":
            self._show(None)                    # done for this call

    async def _look(self, view):
        picture = await view.still()
        if not picture:
            return None, None
        return await asyncio.to_thread(self._detect, picture)
