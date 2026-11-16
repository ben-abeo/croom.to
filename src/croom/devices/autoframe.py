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
a step aside is a short slow turn and someone walking across the room a long fast one.

It keeps to the room: what the camera's Home shows at its widest is the room (the table and the seats around it), and only people
there are followed, and the camera's middle goes no further from Home than TRACK_REACH: a reflection in a window, or someone passing
a door, does not take it to the ceiling or the far wall. People who get out of the picture: the turn goes on to where they were last
seen and no further (a picture taken mid-turn is blurred, and shows no one), then the zoom steps out where the camera is until they
are in it again, and only with no one at the widest for a while does it go Home. It never looks for them by turning. With them in
the middle again the zoom steps back in. What it does is in the journal (croom.devices.tracking). A room without the HAT or with
another camera gets "dynamic".
"""

import asyncio
import logging
import time
from typing import Any, Callable, Optional, Tuple, Union

from croom.devices import framing, people
from croom.core.logs import TRACKING_LOGGER
from croom.devices.errors import DeviceUnavailable, Interrupted, NotReady
from croom.meeting.providers.base import MeetingState

logger = logging.getLogger(__name__)
track_log = logging.getLogger(TRACKING_LOGGER)

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
    # Tracking ("track"): tune these in a room where the camera fidgets, lags, overshoots or wanders
    TRACK_START = (0.10, 0.16)  # the group's middle this far (a fraction of the picture, across and down) from the picture's: turn
    TRACK_STOP = 0.03   # ... and this near it again: stop
    TRACK_SMOOTH = 0.5  # how much of each new picture's place for the group is taken (the rest is the pictures before): 1 is none
    TRACK_LAG_S = 0.25  # from a picture taken to the camera at rest after a stop: the turn stops this much of its speed early
    TRACK_REACH = (30.0, 12.0)  # the camera's middle goes no further than this from Home (degrees of pan, of tilt)
    TRACK_LOST_S = 1.0  # people out of the picture this long: the zoom steps out to find them
    TRACK_ZOOM_S = 0.3  # between steps of the zoom
    TRACK_OUT = 0.75    # a step out to find people who are lost: the zoom times this
    TRACK_EASE = 0.92   # a step out for people who have reached the picture's edge
    TRACK_IN = 1.06     # a step in: the zoom times this ...
    TRACK_ROOMY = 0.4   # ... while the group, in the middle, has filled less than this much of the picture ...
    TRACK_FULL = 0.7    # the group filling more than this much of it: a step out. Between the two, someone beside them is in it too
    TRACK_ROOMY_S = 2.0     # ... for this long
    TRACK_HOME_S = 8.0  # no one in the widest picture for this long: Home, the room's own picture
    TRACK_SEAT_S = 4.0  # at rest this long after turning: the camera is sent to where it is counted to be (RoomCamera.reseat)
    TRACK_ZOOM = 300    # the furthest in while tracking: room around the people for them to move in

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
        self._goal = [0.0, 0.0]                 # ... how far (degrees) the people were from the middle when last seen ...
        self._gone = [0.0, 0.0]                 # ... and how far each turn had gone by then: a turn ends where they were
        self._asked = 0.0                       # when the camera was last told its turn
        self._seen = 0.0                        # when the people the last framing found were last all in the picture
        self._rested = 0.0                      # when the camera last stopped turning
        self._zoomed = 0.0                      # when tracking last stepped the zoom
        self._roomy: Optional[float] = None     # since when the group has had room to be zoomed in on
        self._middle: Optional[Tuple[float, float]] = None     # the group's middle, smoothed over the last pictures
        self._homed = False                     # sent Home for want of anyone, until someone is seen
        self._lost_said = False
        self._counted, self._counted_at = (-1, -1), 0.0     # how many people the journal was last told were in the picture
        self._said: Optional[str] = None
        self._override: Optional[bool] = None   # tracking turned on or off for this call at the camera's controls; None: the setting

    @property
    def mode(self) -> str:
        return self._mode

    def _mode_now(self) -> str:
        """The mode for the call that is on: the room's setting, or what the camera's controls chose for this call (Tracking on
        is "track"; off is no Auto framing at all, so the camera stays where it is)."""
        if self._override is None:
            return self._mode
        return "track" if self._override else "off"

    def _can_track(self) -> bool:
        return bool(self._follow and getattr(self._camera, "says_where", False))

    def set_tracking(self, on: bool) -> None:
        """The camera's controls' Tracking, during a call: on or off for the rest of it, whatever the room's setting; the next call
        has the setting again. On also gives the camera back to Auto framing, as Frame people does."""
        if not self._can_track():
            raise NotReady("This room's camera cannot follow people (it needs the AI HAT and a camera that says where it points)")
        if self._meeting is None or self._meeting.state != MeetingState.CONNECTED:
            raise NotReady("Tracking is turned on and off during a call")
        self._override = bool(on)
        if on:
            self._camera.hand_back()
        self._camera.tracking = bool(on)
        track_log.info(f"Tracking turned {'on' if on else 'off'} for this call at the camera's controls")

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
        return (self._mode_now() == "track" and self._follow and self._call is not None and self._framed is not None
                and self._camera.auto_framing == "on" and getattr(self._camera, "says_where", False))

    async def tick(self) -> None:
        """One step: frame a call that is ready for it, or look at the picture and frame it again when the people in it have changed."""
        in_call = self._meeting is not None and self._meeting.state == MeetingState.CONNECTED
        if not in_call:
            self._call = None                   # the next call is framed afresh
            self._override = None               # ... and has the room's setting again
        mode = self._mode_now()
        taken_over = self._call is not None and self._camera.taken_over_at is not None and self._camera.taken_over_at > self._call
        # What the camera's controls show for Tracking: None where it cannot be had, or outside a call
        self._camera.tracking = (mode == "track" and not taken_over) if in_call and self._can_track() else None
        view = getattr(self._meeting, "camera_view", None)
        if not (mode != "off" and in_call and view is not None and view.blocked() is None and self._camera.available):
            self._show(None)
            return
        now = self._clock()
        if self._call is None:
            self._call, self._framed, self._more, self._fewer, self._settled, self._moved = now, None, 0, 0, None, 0
        taken = self._camera.taken_over_at
        if taken is not None and taken > self._call:   # a hand move since the call was first seen; one before it does not count
            self._show("paused")                # a person has the camera in this call; their Frame people gives it back
            return
        if mode == "call_start" and self._framed is not None:
            self._show(None)                    # framed at the start of this call: nothing more until the next
            return
        self._show("on")
        state = self._camera.state()
        if state["busy"] or state["moving"] or state["framing"]:
            return
        if self._framed is None:
            if now - self._call >= (self.START_S if mode == "call_start" else self.SOON_S):
                await self._frame(view)
            return
        faces, size = await self._look(view)
        if faces is None:
            return
        widest = state["zoom"]["level"] <= state["zoom"]["min"]      # at the widest, someone at the edge cannot be helped by framing
        # Tracking keeps people off the edge with its own zoom: only someone more is a reason to frame again
        more = len(faces) > self._framed or (not widest and not self._tracks() and framing.at_edge(faces, size, self.EDGE))
        self._more = self._more + 1 if more else 0
        self._fewer = self._fewer + 1 if len(faces) < self._framed else 0
        if self._tracks():
            # Tracking turns and zooms for whoever is in the picture: someone more, or fewer, for long enough to believe is whom it
            # keeps there from now on, with no framing from the widest picture
            if self._more >= self.MORE_LOOKS or (self._fewer >= self.FEWER_LOOKS and faces):
                track_log.info(f"Now keeping {len(faces)} in the picture (was {self._framed})")
                self._framed, self._more, self._fewer = len(faces), 0, 0
            return
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
        """One live picture while tracking: the turn and the zoom that keep the people the last framing found in the middle of the
        picture, inside the room (see the top of this file)."""
        now = self._clock()
        state = self._camera.state()
        zoom, widest = state["zoom"]["level"], state["zoom"]["min"]
        place = await self._camera.place()
        home = self._camera.home_place
        if place is None:
            return
        seen = len(faces or [])
        faces = self._in_the_room(faces, size, zoom, place, home)
        if (len(faces), seen) != self._counted and now - self._counted_at >= 1.0:
            self._counted, self._counted_at = (len(faces), seen), now
            track_log.info(f"{len(faces)} in the room's picture ({seen} found in all; keeping {self._framed}), zoom {zoom}")
        turn = [self._reach(axis, self._turn[axis], place, home) for axis in (0, 1)]

        if not faces or len(faces) < self._framed or not self._framed:
            self._middle = None
            if not self._framed:                # no one was framed: someone here now is framed as a call's first are
                await self._turn_by(0, 0)
                if faces and now - self._last >= self.REST_S / 2:
                    await self._frame(view)
                return
            for axis in (0, 1):                 # to where they were last seen, and no further: never up or down unseen
                if turn[axis]:
                    held = now - self._turn_began[axis]
                    gone = framing.turn_degrees(held, zoom) - self._gone[axis]
                    coming = framing.turn_speed(held, zoom) * self.TRACK_LAG_S
                    if axis == 1 or gone + coming >= self._goal[axis]:
                        turn[axis] = 0
            await self._turn_by(turn[0], turn[1])
            lost = now - self._seen
            if lost < self.TRACK_LOST_S or tuple(turn) != (0, 0):
                return
            if not self._lost_said:
                self._lost_said = True
                track_log.info(f"Lost them ({len(faces)} of {self._framed} in the picture) at pan {place[0]:.0f}, tilt {place[1]:.0f}, "
                               f"zoom {zoom}: zooming out here")
            if zoom > widest:
                if now - self._zoomed >= self.TRACK_ZOOM_S:
                    await self._zoom(max(widest, int(zoom * self.TRACK_OUT)), now)
            elif lost >= self.TRACK_HOME_S and not self._homed and home is not None:
                self._homed = True
                track_log.info("No one in the widest picture: Home")
                try:
                    await self._camera.home(by_person=False)
                except (NotReady, DeviceUnavailable, Interrupted) as e:
                    track_log.info(f"Not sent Home: {e}")
            return

        if self._lost_said or self._homed:
            track_log.info(f"Found them again at pan {place[0]:.0f}, tilt {place[1]:.0f}, zoom {zoom}")
        self._seen, self._lost_said, self._homed = now, False, False
        left, top, right, bottom = framing.group_box(faces, size)
        middle = ((left + right) / 2, (top + bottom) / 2)
        if self._middle is not None:            # a box that flickers from picture to picture does not start and stop the camera
            middle = tuple(self.TRACK_SMOOTH * new + (1 - self.TRACK_SMOOTH) * old for new, old in zip(middle, self._middle))
        self._middle = middle
        away = framing.angles_to(middle, zoom)                  # degrees to turn: right and up are +
        view_deg = [2 * half for half in framing.half_view(zoom)]
        for axis in (0, 1):
            off = abs(away[axis])
            toward = 1 if away[axis] > 0 else -1
            held = now - self._turn_began[axis]
            self._goal[axis] = off
            self._gone[axis] = framing.turn_degrees(held, zoom) if turn[axis] else 0.0
            if turn[axis] == 0:
                if off > self.TRACK_START[axis] * view_deg[axis]:
                    turn[axis] = self._reach(axis, toward, place, home)
            else:
                coming = framing.turn_speed(held, zoom) * self.TRACK_LAG_S       # still to come once it is told to stop
                if turn[axis] != toward or off <= max(self.TRACK_STOP * view_deg[axis], coming):
                    turn[axis] = 0
        if tuple(turn) != self._turn and (turn[0] or turn[1]):
            track_log.info(f"Turning pan {turn[0]:+d}, tilt {turn[1]:+d}: {len(faces)} people {away[0]:+.0f} across, {away[1]:+.0f} up "
                           f"of the middle, at pan {place[0]:.0f}, tilt {place[1]:.0f}, zoom {zoom}")
        await self._turn_by(turn[0], turn[1])
        if self._turn != (0, 0):
            self._roomy = None
            return

        # At rest with everyone in the picture: the zoom, a step at a time, and the camera's place made known again
        if now - self._zoomed < self.TRACK_ZOOM_S:
            return
        fill = max(right - left, bottom - top)
        if (fill > self.TRACK_FULL or framing.at_edge(faces, size, self.EDGE)) and zoom > widest:
            self._roomy = None
            await self._zoom(max(widest, int(zoom * self.TRACK_EASE)), now)
        elif fill < self.TRACK_ROOMY and zoom < self.TRACK_ZOOM:
            self._roomy = now if self._roomy is None else self._roomy
            if now - self._roomy >= self.TRACK_ROOMY_S:
                await self._zoom(min(self.TRACK_ZOOM, int(zoom * self.TRACK_IN) + 1), now)
        else:
            self._roomy = None
            if now - self._rested >= self.TRACK_SEAT_S:
                self._rested = now
                try:
                    if await self._camera.reseat():
                        track_log.info(f"At rest: the camera sent to where it is counted to be (pan {place[0]:.0f}, tilt {place[1]:.0f})")
                except (NotReady, DeviceUnavailable, Interrupted) as e:
                    track_log.info(f"Not reseated: {e}")

    def _in_the_room(self, faces, size, zoom, place, home):
        """The people who are where the camera's Home shows at its widest: the room. Without a home, everyone."""
        if home is None or not faces:
            return list(faces or [])
        half = framing.half_view(framing.ZOOM_MIN)
        kept = []
        for face in faces:
            x, y, w, h = face
            away = framing.angles_to(((x + w / 2) / size[0], (y + h / 2) / size[1]), zoom)
            if abs(place[0] + away[0] - home[0]) <= half[0] and abs(place[1] + away[1] - home[1]) <= half[1]:
                kept.append(face)
        return kept

    def _reach(self, axis: int, direction: int, place, home) -> int:
        """A turn's direction, or 0 when it would take the camera's middle further from Home than tracking goes."""
        if not direction or home is None:
            return direction
        beyond = (place[axis] - home[axis]) * direction >= self.TRACK_REACH[axis]
        return 0 if beyond else direction

    async def _zoom(self, level: int, now: float) -> None:
        self._zoomed = now
        try:
            await self._camera.zoom(level, by_person=False)
            track_log.info(f"Zoom {level}")
        except (NotReady, DeviceUnavailable, Interrupted, ValueError) as e:
            track_log.info(f"Zoom not changed: {e}")

    async def _turn_by(self, pan: int, tilt: int) -> None:
        """Set the camera turning (or stop it): written when it changes, and asked again each second while it turns, which keeps the
        camera's own watchdog from stopping a turn that is still wanted. A stop is sent whenever the camera is turning, whatever
        tracking believes."""
        now = self._clock()
        stopping = (pan, tilt) == (0, 0)
        if stopping and not self._camera.state()["moving"]:
            self._turn = (0, 0)
            return
        if not stopping and (pan, tilt) == self._turn and now - self._asked < 1.0:
            return
        for axis, (new, old) in enumerate(zip((pan, tilt), self._turn)):
            if new != old:
                self._turn_began[axis] = now
        try:
            state = await self._camera.move(pan, tilt, by_person=False)
        except (NotReady, DeviceUnavailable, Interrupted, ValueError) as e:
            track_log.info(f"Not turned: {e}")
            self._turn = (0, 0)
            return
        self._asked = now
        if stopping:
            self._rested = now
        # A turn the camera would not start (at a limit, or the end of its range) is not one: the next picture asks again
        self._turn = (pan if state.get("moving") or not pan else 0, tilt if state.get("moving") or not tilt else 0)

    def _has_moved(self, faces, size) -> bool:
        """Whether the people the last framing found, all still in the picture, are no longer where it left them. Measured from
        where they were at the first look after it and not from the picture's middle: a framing that could do no better (the
        camera at a limit, or at its furthest zoom) is not tried again."""
        if not self._follow or self._tracks() or not faces or len(faces) != self._framed:
            return False                        # tracking follows them itself: it does not frame again for it
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
            top = self.TRACK_ZOOM if self._mode_now() == "track" and self._follow else None
            answer = await self._camera.frame_people(view.still, detect=self._detect, by_person=False, zoom_top=top)
        except (NotReady, DeviceUnavailable, Interrupted) as e:
            logger.info(f"Auto framing did not frame just now: {e}")
            return
        self._framed, self._last, self._more, self._fewer = int(answer.get("framed") or 0), self._clock(), 0, 0
        self._settled, self._moved, self._turn, self._seen, self._rested = None, 0, (0, 0), self._clock(), self._clock()
        self._lost_said, self._roomy, self._middle = False, None, None
        logger.info(f"Auto framing framed {self._framed} {'person' if self._framed == 1 else 'people'}")
        if self._mode_now() == "call_start":
            self._show(None)                    # done for this call

    async def _look(self, view):
        picture = await view.still()
        if not picture:
            return None, None
        return await asyncio.to_thread(self._detect, picture)
