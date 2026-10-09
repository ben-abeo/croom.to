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
import math
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from croom.devices.errors import DeviceUnavailable, Interrupted, NotReady
from croom.devices.v4l2 import V4L2_CID_PAN_SPEED, V4L2_CID_TILT_SPEED, V4L2_CID_ZOOM_ABSOLUTE, V4l2Controls

logger = logging.getLogger(__name__)

NO_CAMERA = "no controllable camera found"
# What a setting of the wrong kind falls back to: the config's own defaults (VideoConfig.ptz_travel_seconds and
# ControlConfig.camera_presets)
DEFAULT_TRAVEL_S = 8.0
DEFAULT_PRESET_NAMES = ("Wide", "Table", "Whiteboard")


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _whole_number(value: Any, what: str) -> int:
    """A caller's number as an int. None, a list, text that is not a number, nan and infinity are a ValueError."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as e:
        raise ValueError(f"{what} must be a finite number") from e


def _finite(value: Any) -> bool:
    """A saved number: an int or a float that is not nan or infinite. Text, None and booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:          # an integer too large for a float
        return False


def _span(control: Optional[tuple]) -> str:
    """A queried control's range for the log, or "none" when the node does not have the control."""
    return "none" if control is None else f"{control[0]}..{control[1]}"


def _saved_position(entry: Any, *numbers: str) -> bool:
    """A home or preset read back from the store: a dict whose named values are all finite numbers."""
    return isinstance(entry, dict) and all(_finite(entry.get(name)) for name in numbers)


class RoomCamera:
    WATCHDOG_S = 1.5       # motion stops this long after the last move request
    ZOOM_STEP = 25
    ZOOM_CACHE_S = 3.0
    PREVIEW_S = 180.0
    SLOTS = 3

    def __init__(self, device: str = "auto", travel_s: float = 8.0, preset_names: Optional[List[str]] = None,
                 store=None, v4l2=None, clock=time.monotonic, sleep=asyncio.sleep):
        self._device_pref = device or "auto"
        # The room config is hand-written YAML: a setting of the wrong kind is warned about and replaced by the default,
        # rather than stopping the service ("fast") or homing for no time at all (0, nan)
        if _finite(travel_s) and travel_s > 0:
            self._travel_s = float(travel_s)
        else:
            logger.warning(f"video.ptz_travel_seconds must be a number of seconds above 0, not {travel_s!r}: "
                           f"using {DEFAULT_TRAVEL_S:g}")
            self._travel_s = DEFAULT_TRAVEL_S
        if preset_names is not None and not (isinstance(preset_names, (list, tuple))
                                             and all(isinstance(name, str) for name in preset_names)):
            logger.warning(f"control.camera_presets must be a list of names, not {preset_names!r}: "
                           f"using {', '.join(DEFAULT_PRESET_NAMES)}")
            preset_names = DEFAULT_PRESET_NAMES
        names = list(preset_names or [])[:self.SLOTS]
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
        # The store keeps what it holds by reference, so the camera works on copies of what it loads (and gives
        # the store copies in _persist): an edit here must never be written by a later save of another key.
        # Entries without finite numbers are dropped, so that home and recall can convert them without failing.
        home = saved.get("home")
        self._home: Optional[Dict[str, Any]] = None
        if _saved_position(home, "pan_s", "tilt_s"):
            self._home = dict(home)
        elif home is not None:
            logger.debug(f"Ignoring the saved camera home: {home!r}")
        self._presets: Dict[str, Dict[str, Any]] = {}
        for slot, preset in presets.items():
            if _saved_position(preset, "pan_s", "tilt_s", "zoom"):
                self._presets[str(slot)] = dict(preset)
            else:
                logger.debug(f"Ignoring the saved camera preset {slot}: {preset!r}")

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
        explicit = self._device_pref != "auto"
        candidates = [self._device_pref] if explicit else self._v4l2.nodes()
        for path in candidates:
            try:
                fd = self._v4l2.open(path)
            except OSError as e:
                if explicit:
                    # The config names this node: say why it cannot be used (permission denied, busy, gone), warned
                    # about once per reason like any other
                    self._fail(f"could not open {path}: {e}")
                    return False
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
            logger.info(f"Camera: {path} (zoom {_span(zoom)}, pan {_span(pan)}, tilt {_span(tilt)})")
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
        await self._cancel_long_move()
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
        # Not while a long move runs: it times itself and ends with its own stop, and a watchdog going off inside
        # it would cut it short and leave a position that is wrong marked known. That covers one armed before the
        # move began (_long leaves it alone) and one armed by a move() that raced it (both waited for the same
        # long move, and the move woke first).
        if self._fd is not None and (self._pan or self._tilt) and not self._busy:
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
        """Interrupt a running find_stops/home/recall and wait for it to stop.

        A loop, not a single wait: several requests can be waiting for the same move, and the first of them
        to wake may start the next long move. The others then cut that one short in their turn, so the last
        request wins and two long moves never run side by side.
        """
        while self._busy:
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

    # ------------------------------------------------------------------
    # Position: long moves (find_stops, home, recall) and what presets are stored against
    # ------------------------------------------------------------------

    async def _wait(self, seconds: float) -> None:
        """Sleep, but wake at once when another command interrupts the long move."""
        if seconds <= 0:
            return
        waiter = asyncio.ensure_future(self._interrupt.wait())
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        try:
            await asyncio.wait({waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (waiter, sleeper):
                if not task.done():
                    task.cancel()
        if self._interrupt.is_set():
            raise Interrupted("the camera move was interrupted")

    async def _stop_quietly(self) -> None:
        """The best-effort stop at the end of a long move. A write that fails has already made the camera unavailable
        and warned about it, so its error ends here: shielded and left running by a second cancel, this task still
        ends without an exception that nobody retrieves."""
        try:
            await self._apply_speeds(0, 0)
        except DeviceUnavailable:
            pass

    async def _long(self, work: Callable[[], Awaitable[None]]) -> None:
        """Run a long move: busy while it runs, stopped and unknown if it is cut short.

        A watchdog armed before the move is left alone on purpose. _watch stands down while a long move runs,
        and cancelling a watchdog that is already writing would release the control lock before its stop has
        landed: the move's first write could then land ahead of it, and the camera would stop under a move that
        believes it is running. Left alone, that stop finishes first, because the lock is taken in order.
        """
        await self._require()
        await self._cancel_long_move()
        self._busy = True
        self._interrupt.clear()
        self._long_done.clear()
        try:
            await work()
        except (Interrupted, DeviceUnavailable) as e:
            self._position_known = False
            if isinstance(e, Interrupted):
                logger.info("Camera move cut short: the position is unknown until the camera is homed again")
            if self._fd is not None:
                await self._stop_quietly()
            raise
        except asyncio.CancelledError:
            # The task running the move was cancelled (a shutdown, a request that went away): the same, but the
            # stop is shielded, so that a second cancel while it is on its way cannot abandon it half done, with
            # the control lock released and the speeds and the estimate left as the move had them.
            self._position_known = False
            logger.info("Camera move cancelled: the position is unknown until the camera is homed again")
            if self._fd is not None:
                await asyncio.shield(self._stop_quietly())
            raise
        except Exception as e:
            # Anything else (a refusal from the work itself, or a bug in it or under it): the motors are still stopped,
            # because a speed control keeps its value until told otherwise and nothing else would stop them
            self._position_known = False
            logger.info(f"Camera move failed ({type(e).__name__}: {e}): the position is unknown until the camera is "
                        "homed again")
            if self._fd is not None:
                await self._stop_quietly()
            raise
        finally:
            self._busy = False
            self._long_done.set()

    async def _find_stops_work(self) -> None:
        await self._apply_speeds(-1, -1)
        if self._speed_controls(-1, -1):        # a zoom-only node has nothing to drive into a stop: no wait
            await self._wait(self._travel_s)
        await self._apply_speeds(0, 0)
        self._pan_s, self._tilt_s = 0.0, 0.0
        self._position_known = True

    async def _move_to(self, pan_s: float, tilt_s: float) -> None:
        """Move both axes at once to a position; the shorter leg stops first.

        Each leg is the difference from where the camera is now, which includes a move still under way: a recall
        can start while an arrow press is running, and legs worked out from the estimate as of the last speed
        change would be short by what that press has travelled since. The targets are clamped to the travel,
        which bounds every leg: a saved number beyond it (typed by hand, or saved before ptz_travel_seconds was
        lowered) would otherwise run an axis against its stop for as long as the number says.
        """
        if not self._speed_controls(1, 1):      # a zoom-only node cannot pan or tilt: nothing to wait for
            return
        self._account_now()
        pan_s = min(self._travel_s, max(0.0, pan_s))
        tilt_s = min(self._travel_s, max(0.0, tilt_s))
        legs = sorted([(abs(pan_s - self._pan_s), "pan"), (abs(tilt_s - self._tilt_s), "tilt")])
        pan_dir, tilt_dir = _sign(pan_s - self._pan_s), _sign(tilt_s - self._tilt_s)
        await self._apply_speeds(pan_dir, tilt_dir)
        applied = (pan_dir, tilt_dir)
        elapsed = 0.0
        for duration, axis in legs:
            await self._wait(duration - elapsed)
            elapsed = max(elapsed, duration)
            if axis == "pan":
                pan_dir = 0
            else:
                tilt_dir = 0
            if (pan_dir, tilt_dir) != applied:      # a leg of zero length changes nothing: no extra control call
                await self._apply_speeds(pan_dir, tilt_dir)
                applied = (pan_dir, tilt_dir)

    async def _home_work(self) -> None:
        await self._find_stops_work()
        await self._move_to(float(self._home["pan_s"]), float(self._home["tilt_s"]))
        await self.zoom(self._zoom_range[0])

    async def find_stops(self) -> Dict[str, Any]:
        """Drive both axes into their end stops and call that (0, 0): the one place the position is known for sure."""
        await self._long(self._find_stops_work)
        return self.state()

    async def home(self) -> Dict[str, Any]:
        """Find the stops, then move to the saved home and the widest zoom."""
        if self._home is None:
            raise NotReady("save a home first")
        await self._long(self._home_work)
        return self.state()

    def _account_now(self) -> None:
        """Bring the estimate up to this moment: add the motion segment that is still open, and start the next
        one from here. Closing the segment and stopping there would drop the rest of a move that is still under
        way (an arrow held while another tablet saves, or a recall starts), so such a move keeps counting."""
        now = self._clock()
        self._account(now)
        if self._pan or self._tilt:
            self._segment_started = now

    def _estimate_now(self) -> Dict[str, float]:
        """The estimate as of this moment, rounded for storing."""
        self._account_now()
        return {"pan_s": round(self._pan_s, 3), "tilt_s": round(self._tilt_s, 3)}

    async def save_home(self) -> Dict[str, Any]:
        """Remember where the camera is now as its home, the place homing goes back to."""
        if not self._position_known:
            raise NotReady("home the camera first")
        home = self._estimate_now()
        self._persist(home, self._presets)      # written first: a write that fails leaves the camera as it was
        self._home = home
        logger.info(f"Camera home saved at {self._home}")
        return self.state()

    def _slot(self, slot: Any) -> str:
        """The settings key of a preset slot. True is 1 and 2.0 is 2 to Python, but neither is a slot number."""
        if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= self.SLOTS:
            raise ValueError(f"preset slots are 1 to {self.SLOTS}")
        return str(slot)

    async def save(self, slot: int) -> Dict[str, Any]:
        """Store the camera's position and zoom in a preset slot."""
        key = self._slot(slot)
        if not self._position_known:
            raise NotReady("home the camera first")
        zoom = await self.read_zoom()          # first: it may wait for the camera, and the position is taken right after
        if not self._position_known:           # a long move was cut short, or a call failed, while the zoom was read
            raise NotReady("home the camera first")
        presets = {**self._presets, key: {**self._estimate_now(), "zoom": zoom if zoom is not None else self._zoom_range[0]}}
        self._persist(self._home, presets)      # written first: a write that fails leaves the camera as it was
        self._presets = presets
        logger.info(f"Camera preset {key} ({self._names[slot - 1]}) saved: {self._presets[key]}")
        return self.state()

    async def recall(self, slot: int) -> Dict[str, Any]:
        """Move to a saved preset by the difference from where the camera is, homing first when it does not know."""
        key = self._slot(slot)
        preset = self._presets.get(key)
        if preset is None:
            raise NotReady("nothing saved in this slot")
        if not self._position_known and self._home is None:
            raise NotReady("home the camera first")

        async def work():
            if not self._position_known:       # also true when this recall has just cut another long move short
                if self._home is None:
                    raise NotReady("home the camera first")
                await self._home_work()
            await self._move_to(float(preset["pan_s"]), float(preset["tilt_s"]))
            await self.zoom(int(preset.get("zoom", self._zoom_range[0])))

        await self._long(work)
        return self.state()

    def _persist(self, home: Optional[Dict[str, Any]], presets: Dict[str, Dict[str, Any]]) -> None:
        """Write a home and presets through the store, as copies: the store keeps what it is given by
        reference, so a later save of another key would otherwise write a preset that is half edited.
        The callers write the new values first and keep them only once this has returned: a save that
        raises (a full disk) must not leave the camera reporting a home or preset that is not on disk."""
        if self._store is not None:
            self._store.save("camera", {"home": dict(home) if home is not None else None,
                                        "presets": {k: dict(v) for k, v in presets.items()}})

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    def set_preview(self, on: bool) -> bool:
        """Keep the camera preview open for PREVIEW_S from now (asking again renews it), or close it."""
        self._preview_until = self._clock() + self.PREVIEW_S if on else 0.0
        return self.preview
