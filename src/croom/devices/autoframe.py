"""
Auto framing: Frame people by itself during calls, as Logitech's RightSight does (which runs only in Logitech's own apps for Windows
and Intel Macs and on its RoomMate, so not on the room Pi). Its modes are RightSight's (control.camera_auto_framing): "off";
"call_start", where a call that connects with the room's camera on is framed once people have had a moment to sit down; and
"dynamic", where after that the picture is looked at every few seconds, and framed again when someone has come into it or reached its
edge, or someone has left it, for long enough to believe. Never sooner than a rest after the last framing, so it does not hunt.

A person who takes the camera by hand during the call (an arrow or a drag, the zoom, Home, a preset) pauses it for the rest of that
call; their tap on Frame people gives the camera back to it. The camera screen says which (RoomCamera.auto_framing). Each look is one
still from the call's own picture (CameraView.still) and the face detector in a worker thread; nothing else opens the camera.
"""

import asyncio
import logging
import time
from typing import Any, Callable, Optional, Union

from croom.devices import framing
from croom.devices.errors import DeviceUnavailable, Interrupted, NotReady
from croom.meeting.providers.base import MeetingState

logger = logging.getLogger(__name__)

MODES = ("off", "call_start", "dynamic")


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

    def __init__(self, camera, meeting, mode: Union[str, bool, None] = "off", detect: Optional[Callable[[bytes], Any]] = None,
                 clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep):
        self._camera = camera
        self._meeting = meeting
        self._mode = mode_of(mode)
        self._detect = detect or framing.detect_faces
        self._clock, self._sleep = clock, sleep
        self._task: Optional[asyncio.Task] = None
        self._call: Optional[float] = None      # when the call being framed was first seen ready
        self._framed: Optional[int] = None      # how many people its last framing found; None: not framed yet in this call
        self._last = 0.0                        # when it last framed
        self._more = self._fewer = 0            # looks in a row that saw more people (or one at the edge), and fewer
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

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.wait({task})
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
            self._call, self._framed, self._more, self._fewer = now, None, 0, 0
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
            if now - self._call >= self.START_S:
                await self._frame(view)
            return
        faces, size = await self._look(view)
        if faces is None:
            return
        widest = state["zoom"]["level"] <= state["zoom"]["min"]      # at the widest, someone at the edge cannot be helped by framing
        more = len(faces) > self._framed or (not widest and framing.at_edge(faces, size, self.EDGE))
        self._more = self._more + 1 if more else 0
        self._fewer = self._fewer + 1 if len(faces) < self._framed else 0
        if (self._more >= self.MORE_LOOKS or self._fewer >= self.FEWER_LOOKS) and now - self._last >= self.REST_S:
            await self._frame(view)

    async def _frame(self, view) -> None:
        try:
            answer = await self._camera.frame_people(view.still, detect=self._detect, by_person=False)
        except (NotReady, DeviceUnavailable, Interrupted) as e:
            logger.info(f"Auto framing did not frame just now: {e}")
            return
        self._framed, self._last, self._more, self._fewer = int(answer.get("framed") or 0), self._clock(), 0, 0
        logger.info(f"Auto framing framed {self._framed} {'person' if self._framed == 1 else 'people'}")
        if self._mode == "call_start":
            self._show(None)                    # done for this call

    async def _look(self, view):
        picture = await view.still()
        if not picture:
            return None, None
        return await asyncio.to_thread(self._detect, picture)
