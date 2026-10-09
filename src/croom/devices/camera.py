"""
The room camera's framing through V4L2 (spec 2026-10-08 sound and camera, section
4.3). The Logitech MeetUp reports no pan or tilt position: it has speed controls
(move until told to stop) and an absolute zoom. So the camera keeps an estimate of
where it is in seconds of travel from the end stops, found by homing, and presets
are stored against that estimate. Control calls go through a second file
descriptor next to Chromium's stream; nothing here streams video.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from croom.devices.errors import DeviceUnavailable, Interrupted, NotReady  # noqa: F401 - raised by find_stops, home and recall
from croom.devices.v4l2 import V4L2_CID_PAN_SPEED, V4L2_CID_TILT_SPEED, V4L2_CID_ZOOM_ABSOLUTE, V4l2Controls

logger = logging.getLogger(__name__)

NO_CAMERA = "no controllable camera found"


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _whole_number(value: Any, what: str) -> int:
    """A caller's number as an int. None, a list, text that is not a number, nan and infinity are a ValueError."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as e:
        raise ValueError(f"{what} must be a finite number") from e


class RoomCamera:
    WATCHDOG_S = 1.5       # motion stops this long after the last move request
    ZOOM_STEP = 25
    ZOOM_CACHE_S = 3.0
    PREVIEW_S = 180.0
    SLOTS = 3

    def __init__(self, device: str = "auto", travel_s: float = 8.0, preset_names: Optional[List[str]] = None,
                 store=None, v4l2=None, clock=time.monotonic, sleep=asyncio.sleep):
        self._device_pref = device or "auto"
        self._travel_s = float(travel_s)
        names = [str(n) for n in (preset_names or [])][:self.SLOTS]
        self._names = names + [f"Preset {i}" for i in range(len(names) + 1, self.SLOTS + 1)]
        self._store = store
        self._v4l2 = v4l2 or V4l2Controls()
        self._clock = clock
        self._sleep = sleep
        self._fd: Optional[int] = None
        self._path: Optional[str] = None
        self._reason: Optional[str] = "not checked yet"
        self._warned: Optional[str] = None
        self._zoom_range = (100, 500)
        self._has_pan = False
        self._has_tilt = False
        self._zoom: Optional[int] = None
        self._zoom_read_at: Optional[float] = None
        self._pan = 0
        self._tilt = 0
        self._segment_started: Optional[float] = None
        self._pan_s = 0.0
        self._tilt_s = 0.0
        self._position_known = False
        self._watchdog: Optional[asyncio.Task] = None
        self._busy = False
        self._interrupt = asyncio.Event()
        self._long_done = asyncio.Event()
        self._long_done.set()
        # One control sequence at a time: a pan write then a tilt write, a zoom write, a zoom read.
        # Overlapping requests (a repeat move and a release, two tablets, the watchdog) would otherwise
        # mix their writes and leave the camera, the estimate and the cached zoom disagreeing. Held only
        # around the short control calls and their bookkeeping, never across a wait, so a long move can
        # still be interrupted by move() and stop().
        self._io_lock = asyncio.Lock()
        self._preview_until = 0.0
        saved = (store.get("camera") if store is not None else None) or {}
        if not isinstance(saved, dict):   # a hand-edited or damaged file is ignored, not fatal
            saved = {}
        presets = saved.get("presets")
        if not isinstance(presets, dict):
            presets = {}
        self._home = saved.get("home") if isinstance(saved.get("home"), dict) else None
        self._presets = {str(k): v for k, v in presets.items() if isinstance(v, dict)}

    @classmethod
    def from_config(cls, config, store) -> "RoomCamera":
        return cls(device=config.video.device, travel_s=config.video.ptz_travel_seconds,
                   preset_names=config.control.camera_presets, store=store)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._fd is not None

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def position_known(self) -> bool:
        return self._position_known

    @property
    def home_saved(self) -> bool:
        return self._home is not None

    @property
    def preview(self) -> bool:
        return self._clock() < self._preview_until

    def state(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "device": self._path,
            "reason": None if self.available else self._reason,
            "moving": bool(self._pan or self._tilt),
            "busy": self._busy,
            "zoom": {"level": self._zoom if self._zoom is not None else self._zoom_range[0],
                     "min": self._zoom_range[0], "max": self._zoom_range[1]},
            "position_known": self._position_known,
            "home_saved": self.home_saved,
            "presets": [{"slot": slot, "name": self._names[slot - 1], "saved": str(slot) in self._presets}
                        for slot in range(1, self.SLOTS + 1)],
            "preview": self.preview,
        }

    # ------------------------------------------------------------------
    # The device
    # ------------------------------------------------------------------

    async def discover(self) -> bool:
        """Open the configured node, or the first one with a zoom or pan control."""
        if self._fd is not None:
            return True
        candidates = [self._device_pref] if self._device_pref != "auto" else self._v4l2.nodes()
        for path in candidates:
            try:
                fd = self._v4l2.open(path)
            except OSError as e:
                logger.debug(f"Could not open {path}: {e}")
                continue
            zoom = self._v4l2.query(fd, V4L2_CID_ZOOM_ABSOLUTE)
            pan = self._v4l2.query(fd, V4L2_CID_PAN_SPEED)
            tilt = self._v4l2.query(fd, V4L2_CID_TILT_SPEED)
            if zoom is None and pan is None:
                self._v4l2.close(fd)
                continue
            self._fd, self._path = fd, path
            self._has_pan, self._has_tilt = pan is not None, tilt is not None
            if zoom is not None:
                self._zoom_range = (zoom[0], zoom[1])
                self._zoom = zoom[3]
            self._reason = None
            self._warned = None
            logger.info(f"Camera: {path} (zoom {self._zoom_range[0]}..{self._zoom_range[1]}, "
                        f"pan {'yes' if self._has_pan else 'no'}, tilt {'yes' if self._has_tilt else 'no'})")
            return True
        self._fail(NO_CAMERA)
        return False

    def _fail(self, reason: str) -> None:
        if self._warned != reason:
            logger.warning(f"Camera unavailable: {reason}")
            self._warned = reason
        if self._fd is not None:
            self._stop_motors()
            try:
                self._v4l2.close(self._fd)
            except OSError:
                pass
        self._fd, self._path = None, None
        self._reason = reason
        self._pan = self._tilt = 0
        self._segment_started = None
        self._position_known = False

    def _speed_controls(self, pan: int, tilt: int) -> Dict[int, int]:
        """The speed controls this node has, with the values to write; pan and tilt go out in one call."""
        speeds: Dict[int, int] = {}
        if self._has_pan:
            speeds[V4L2_CID_PAN_SPEED] = pan
        if self._has_tilt:
            speeds[V4L2_CID_TILT_SPEED] = tilt
        return speeds

    def _stop_motors(self) -> None:
        """One best-effort zero write of both speed controls on the node that is about to be closed.

        A call that failed while the camera was moving must not leave the motors running with nothing able
        to stop them: the speed controls keep their value until told otherwise. On an unplugged node the
        write fails at once; any error is ignored, because there is nothing more to do for a node that is gone.
        """
        speeds = self._speed_controls(0, 0)
        if not speeds:
            return
        try:
            self._v4l2.set_many(self._fd, speeds)
        except OSError as e:
            logger.debug(f"Could not stop the camera before closing it: {e}")

    async def _require(self) -> None:
        if not await self.discover():
            raise DeviceUnavailable(self._reason or NO_CAMERA)

    def _check_open(self) -> None:
        """A call that waited for the lock may find the camera gone: refuse cleanly rather than write to no node."""
        if self._fd is None:
            raise DeviceUnavailable(self._reason or NO_CAMERA)

    async def _set(self, cid: int, value: int) -> None:
        self._check_open()
        try:
            await asyncio.to_thread(self._v4l2.set, self._fd, cid, value)
        except OSError as e:
            self._fail(f"camera call failed: {e}")
            raise DeviceUnavailable(self._reason) from e

    async def _set_many(self, values: Dict[int, int]) -> None:
        self._check_open()
        try:
            await asyncio.to_thread(self._v4l2.set_many, self._fd, values)
        except OSError as e:
            self._fail(f"camera call failed: {e}")
            raise DeviceUnavailable(self._reason) from e

    async def _get(self, cid: int) -> int:
        self._check_open()
        try:
            return await asyncio.to_thread(self._v4l2.get, self._fd, cid)
        except OSError as e:
            self._fail(f"camera call failed: {e}")
            raise DeviceUnavailable(self._reason) from e

    async def close(self) -> None:
        self._cancel_watchdog()
        if self._fd is not None:
            try:
                await self._apply_speeds(0, 0)
            except DeviceUnavailable:
                pass
        if self._fd is not None:
            try:
                self._v4l2.close(self._fd)
            except OSError:
                pass
        self._fd, self._path = None, None
        self._reason = "closed"

    # ------------------------------------------------------------------
    # Moving
    # ------------------------------------------------------------------

    async def move(self, pan: int, tilt: int) -> Dict[str, Any]:
        """Start, change or stop motion; a second request within WATCHDOG_S keeps it going."""
        if (isinstance(pan, bool) or isinstance(tilt, bool)   # True and False equal 1 and 0 but are not directions
                or pan not in (-1, 0, 1) or tilt not in (-1, 0, 1)):
            raise ValueError("pan and tilt must each be -1, 0 or 1")
        await self._require()
        await self._cancel_long_move()
        await self._apply_speeds(int(pan), int(tilt))
        self._cancel_watchdog()
        if pan or tilt:
            self._watchdog = asyncio.create_task(self._watch())
        return self.state()

    async def stop(self) -> Dict[str, Any]:
        """Stop all motion. A node lost while the camera was moving is looked for again first, so the stop
        can still reach it; when no camera is found this simply reports the state."""
        await self._cancel_long_move()
        self._cancel_watchdog()
        if self._fd is None:
            await self.discover()
        if self._fd is not None:
            await self._apply_speeds(0, 0)
        return self.state()

    async def _watch(self) -> None:
        await asyncio.sleep(self.WATCHDOG_S)
        if self._fd is not None and (self._pan or self._tilt):
            logger.info("Camera motion stopped: the page went quiet")
            try:
                await self._apply_speeds(0, 0)
            except DeviceUnavailable:
                pass

    def _cancel_watchdog(self) -> None:
        if self._watchdog is not None and not self._watchdog.done() and self._watchdog is not asyncio.current_task():
            self._watchdog.cancel()
        self._watchdog = None

    async def _apply_speeds(self, pan: int, tilt: int) -> None:
        """Set the speed controls and close the motion segment that ends here (the position estimate).

        The whole sequence runs under the control lock, so no other request's write lands in the middle of
        it. Pan and tilt go out in ONE call: on a UVC camera they are the two halves of one relative control,
        and two separate writes could cancel each other. The clock is read after the write has landed: the
        old speeds applied up to that reading and the new ones apply from it, so the time the write itself
        takes is not dropped from the estimate.
        """
        async with self._io_lock:
            speeds = self._speed_controls(pan, tilt)
            if speeds:
                await self._set_many(speeds)
            now = self._clock()
            self._account(now)
            self._pan, self._tilt = (pan if self._has_pan else 0), (tilt if self._has_tilt else 0)
            self._segment_started = now if (self._pan or self._tilt) else None

    def _account(self, until: Optional[float] = None) -> None:
        """Add the motion segment that ends at `until` (default: now) to the estimate, and close it."""
        if self._segment_started is None:
            return
        elapsed = max(0.0, (self._clock() if until is None else until) - self._segment_started)
        self._pan_s = min(self._travel_s, max(0.0, self._pan_s + self._pan * elapsed))
        self._tilt_s = min(self._travel_s, max(0.0, self._tilt_s + self._tilt * elapsed))
        self._segment_started = None

    async def _cancel_long_move(self) -> None:
        """Interrupt a running find_stops/home/recall (Task 5) and wait for it to stop."""
        if self._busy:
            self._interrupt.set()
            await self._long_done.wait()

    # ------------------------------------------------------------------
    # Zoom
    # ------------------------------------------------------------------

    async def read_zoom(self) -> Optional[int]:
        await self._require()
        async with self._io_lock:
            # the cache is checked inside the lock: a caller that waited here can use the read it waited for
            now = self._clock()
            if self._zoom is None or self._zoom_read_at is None or now - self._zoom_read_at >= self.ZOOM_CACHE_S:
                self._zoom = await self._get(V4L2_CID_ZOOM_ABSOLUTE)
                self._zoom_read_at = now
            return self._zoom

    async def zoom(self, level: int) -> int:
        level = _whole_number(level, "the zoom level")
        await self._require()
        level = max(self._zoom_range[0], min(self._zoom_range[1], level))
        async with self._io_lock:
            await self._set(V4L2_CID_ZOOM_ABSOLUTE, level)
            self._zoom = level
            self._zoom_read_at = self._clock()
        return level

    async def zoom_step(self, delta: int) -> int:
        delta = _whole_number(delta, "the zoom step")
        current = await self.read_zoom()
        return await self.zoom((current if current is not None else self._zoom_range[0]) + delta)
