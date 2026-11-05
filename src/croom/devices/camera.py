"""
The room camera's framing through V4L2 (spec 2026-10-08 sound and camera, section
4.3). The Logitech MeetUp reports no pan or tilt position: it has speed controls
(move until told to stop) and an absolute zoom. So the camera keeps an estimate of
where it is in seconds of travel from the end stops, found by homing, and presets
are stored against that estimate. A camera that says where it is (the Rally:
pan_absolute and tilt_absolute) keeps its own position in its home and presets
instead, and is sent back there. It is never turned by speed: the Rally goes on
saying the last place it was sent whatever a turn by speed does, so an arrow
steps its position instead. It also keeps to limits set in Set up: the furthest
its picture's edges may go down, up, left and right (past the bottom one, its
own base comes into the picture). Control calls go through a second file
descriptor next to Chromium's stream; nothing here streams video.
"""

import asyncio
import errno
import logging
import math
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from croom.devices import framing
from croom.devices.errors import DeviceUnavailable, Interrupted, NotReady
from croom.devices.v4l2 import (V4L2_CID_AUTO_WHITE_BALANCE, V4L2_CID_BACKLIGHT_COMPENSATION, V4L2_CID_BRIGHTNESS,
                                V4L2_CID_CONTRAST, V4L2_CID_PAN_ABSOLUTE, V4L2_CID_PAN_SPEED, V4L2_CID_SATURATION, V4L2_CID_SHARPNESS,
                                V4L2_CID_TILT_ABSOLUTE, V4L2_CID_TILT_SPEED, V4L2_CID_WHITE_BALANCE_TEMPERATURE,
                                V4L2_CID_ZOOM_ABSOLUTE, V4l2Controls)

logger = logging.getLogger(__name__)

NO_CAMERA = "no controllable camera found"
# What a setting of the wrong kind falls back to: the config's own defaults (VideoConfig.ptz_travel_seconds and
# ControlConfig.camera_presets)
DEFAULT_TRAVEL_S = 8.0
DEFAULT_PRESET_NAMES = ("Wide", "Table", "Whiteboard")
# Frame people's refusals, in the camera screen's words (spec 2026-11-01 camera view, section 5)
ALREADY_FRAMING = "Already framing"
MOVING = "Moving\u2026"                 # what the camera screen says while the camera homes or recalls a preset (devices.js)
NO_PICTURE = "No picture from the camera"
NO_LIMITS = "This camera does not say where it points, so it cannot keep to limits"
ARCSEC = 3600                           # a camera's pan and tilt are counted in 1/3600 deg (UVC)
# The picture settings a room may change, by the names the page and the store use, in the order they are put back (the automatic white
# balance before the temperature, which the camera takes only while the automatic one is off). A camera has those it reports.
PICTURE = (("brightness", V4L2_CID_BRIGHTNESS), ("contrast", V4L2_CID_CONTRAST), ("saturation", V4L2_CID_SATURATION),
           ("sharpness", V4L2_CID_SHARPNESS), ("backlight_compensation", V4L2_CID_BACKLIGHT_COMPENSATION),
           ("white_balance_automatic", V4L2_CID_AUTO_WHITE_BALANCE), ("white_balance_temperature", V4L2_CID_WHITE_BALANCE_TEMPERATURE))
PICTURE_CIDS = dict(PICTURE)
WHITE_BALANCE_IS_AUTOMATIC = "Turn off automatic white balance first"


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
    SETTLE_S = 1.0         # Frame people: the picture settles this long after a zoom or a turn before it is looked at
    # A camera that says where it is, turned by an arrow or a drag: its position moved STEP_DEG every STEP_S (10 deg/s, near the
    # MeetUp's speed). The knobs for how it feels; the Rally counts its position in 1/3600 deg and takes whole degrees.
    STEP_S = 0.1
    STEP_DEG = 1

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
        self.set_preset_names(preset_names)
        self._store = store
        self._v4l2 = v4l2 or V4l2Controls()
        self._clock = clock
        self._sleep = sleep
        self._fd: Optional[int] = None
        self._path: Optional[str] = None
        self._label: Optional[str] = None   # the camera's name (V4L2 card), as Chromium's device label starts with it
        self._reason: Optional[str] = "not checked yet"
        self._warned: Optional[str] = None
        self._zoom_range = (100, 500)
        self._has_pan = False
        self._has_tilt = False
        self._absolute = False   # the camera says where it is (pan_absolute and tilt_absolute): no estimate needed
        self._pan_range = self._tilt_range = (0, 0)   # its pan and tilt, lowest to highest, in its own units (1/3600 deg)
        self._stepper: Optional[asyncio.Task] = None
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
        self._framing = False   # Frame people runs (a long move of its own: busy as well)
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
        if _saved_position(home, "pan_s", "tilt_s") or _saved_position(home, "pan", "tilt"):
            self._home = dict(home)
        elif home is not None:
            logger.debug(f"Ignoring the saved camera home: {home!r}")
        self._presets: Dict[str, Dict[str, Any]] = {}
        for slot, preset in presets.items():
            if _saved_position(preset, "pan_s", "tilt_s", "zoom") or _saved_position(preset, "pan", "tilt", "zoom"):
                self._presets[str(slot)] = dict(preset)
            else:
                logger.debug(f"Ignoring the saved camera preset {slot}: {preset!r}")
        # The limits: the furthest the picture's edges may go, by edge (framing.EDGES), in the camera's units
        limits = saved.get("limits")
        self._limits: Dict[str, int] = {}
        if isinstance(limits, dict):
            self._limits = {edge: int(limits[edge]) for edge in framing.EDGES if _finite(limits.get(edge))}
        # The picture: the room's own settings (by PICTURE's names), put back whenever the camera is opened; the open camera's ranges
        # (min, max, step, default) for the ones it has, and what it says they are now
        picture = saved.get("picture")
        self._picture: Dict[str, int] = {}
        if isinstance(picture, dict):
            self._picture = {name: int(picture[name]) for name, _ in PICTURE if _finite(picture.get(name))}
        self._picture_ranges: Dict[str, tuple] = {}
        self._picture_now: Dict[str, int] = {}

    def set_preset_names(self, preset_names: Optional[List[str]]) -> None:
        """The three slots' names (control.camera_presets): at start, and when Settings changes them, which applies at once. Slots past
        the names given are Preset 2, Preset 3; anything but a list of names is the defaults, with a warning."""
        if preset_names is not None and not (isinstance(preset_names, (list, tuple))
                                             and all(isinstance(name, str) for name in preset_names)):
            logger.warning(f"control.camera_presets must be a list of names, not {preset_names!r}: "
                           f"using {', '.join(DEFAULT_PRESET_NAMES)}")
            preset_names = DEFAULT_PRESET_NAMES
        names = list(preset_names or [])[:self.SLOTS]
        self._names = names + [f"Preset {i}" for i in range(len(names) + 1, self.SLOTS + 1)]

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
    def label(self) -> Optional[str]:
        """The name the open camera gives (its V4L2 card name), which Chromium's label for it contains; None when unknown."""
        return self._label

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def position_known(self) -> bool:
        return self._absolute or self._position_known

    @property
    def home_saved(self) -> bool:
        return self._usable(self._home)

    def _usable(self, saved: Any) -> bool:
        """A saved home or preset this camera can go to: its own pan and tilt on a camera that says where it is, seconds of travel
        on one that does not (the MeetUp). The one kind is no home or preset for the other camera."""
        return _saved_position(saved, *(("pan", "tilt") if self._absolute else ("pan_s", "tilt_s")))

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
            "framing": self._framing,
            "zoom": {"level": self._zoom if self._zoom is not None else self._zoom_range[0],
                     "min": self._zoom_range[0], "max": self._zoom_range[1]},
            "position_known": self.position_known,
            "home_saved": self.home_saved,
            "presets": [{"slot": slot, "name": self._names[slot - 1], "saved": self._usable(self._presets.get(str(slot)))}
                        for slot in range(1, self.SLOTS + 1)],
            # which limits are set, on a camera that can keep to them (one that says where it points); None on any other
            "limits": {edge: edge in self._limits for edge in framing.EDGES} if self._absolute else None,
            # the picture settings this camera has, in PICTURE's order: their ranges, and the value each has now
            "picture": [{"name": name, "min": r[0], "max": r[1], "step": r[2], "default": r[3],
                         "value": self._picture_now.get(name, r[3])}
                        for name, r in ((name, self._picture_ranges.get(name)) for name, _ in PICTURE) if r is not None],
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
            namer = getattr(self._v4l2, "camera_name", None)   # QUERYCAP on a node of its own: no frames are read
            self._label = namer(path) if namer is not None else None
            self._has_pan, self._has_tilt = pan is not None, tilt is not None
            pan_at, tilt_at = self._v4l2.query(fd, V4L2_CID_PAN_ABSOLUTE), self._v4l2.query(fd, V4L2_CID_TILT_ABSOLUTE)
            self._absolute = pan_at is not None and tilt_at is not None
            if self._absolute:
                self._pan_range, self._tilt_range = (pan_at[0], pan_at[1]), (tilt_at[0], tilt_at[1])
            if zoom is not None:
                self._zoom_range = (zoom[0], zoom[1])
                self._zoom = zoom[3]
            self._picture_ranges = {name: r for name, r in ((name, self._v4l2.query(fd, cid)) for name, cid in PICTURE)
                                    if r is not None}
            self._picture_now = {}
            self._reason = None
            self._warned = None
            logger.info(f"Camera: {path} (zoom {_span(zoom)}, pan {_span(pan)}, tilt {_span(tilt)}"
                        f"{', says where it is' if self._absolute else ''})")
            await self._put_picture_back()
            return self._fd is not None
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
        self._absolute = False
        self._picture_ranges, self._picture_now = {}, {}

    def _speed_controls(self, pan: int, tilt: int) -> Dict[int, int]:
        """The speed controls this node has, with the values to write; pan and tilt go out in one call. None on a camera that says
        where it is: a turn by speed is not counted in the place it says (the Rally), so it is turned by its position (_step)."""
        speeds: Dict[int, int] = {}
        if self._absolute:
            return speeds
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

    def forget_position(self) -> None:
        """Stop trusting the dead-reckoned position: something else (a laptop) may have moved the camera."""
        self._position_known = False

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
        self._absolute = False
        self._picture_ranges, self._picture_now = {}, {}
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
        if self._absolute and (self._pan or self._tilt) and (self._stepper is None or self._stepper.done()):
            self._stepper = asyncio.create_task(self._step())
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

    async def _step(self) -> None:
        """A camera that says where it is, turned by an arrow or a drag: its pan and tilt moved a step every STEP_S, within its range,
        for as long as the direction move() set stays (a let-go, the watchdog, a Home or a preset set it to nothing). Each step starts
        from the place the camera says, so the place it says stays where it is."""
        step = self.STEP_DEG * ARCSEC
        try:
            while self._fd is not None and (self._pan or self._tilt):
                async with self._io_lock:
                    if not (self._pan or self._tilt):     # stopped while this step waited for the camera
                        break
                    pan = await self._get(V4L2_CID_PAN_ABSOLUTE) + self._pan * step
                    tilt = await self._get(V4L2_CID_TILT_ABSOLUTE) + self._tilt * step
                    await self._set_many(self._inside(pan, tilt, self._zoom if self._zoom is not None else self._zoom_range[0]))
                await self._sleep(self.STEP_S)
        except DeviceUnavailable:
            pass        # the camera has gone, and _fail has said so

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
    # The picture: brightness, contrast, colour, sharpness, a bright window behind people, white balance
    # ------------------------------------------------------------------

    async def _put_picture_back(self) -> None:
        """The room's picture settings, written to the camera that has just been opened (a camera unplugged or restarted forgets them),
        then what it says they are. A setting it refuses is left as the camera has it, and said in the log."""
        try:
            async with self._io_lock:
                for name, cid in PICTURE:
                    if name in self._picture and name in self._picture_ranges and not self._temperature_is_automatic(name):
                        await self._write_picture(name, self._picture[name])
                await self._read_picture()
        except (DeviceUnavailable, ValueError) as e:
            logger.info(f"The camera's picture settings were not all put back: {e}")

    def _temperature_is_automatic(self, name: str) -> bool:
        automatic = self._picture.get("white_balance_automatic", self._picture_now.get("white_balance_automatic", 1))
        return name == "white_balance_temperature" and automatic != 0

    async def _write_picture(self, name: str, value: int) -> None:
        """One picture setting, under the control lock. A camera that is gone fails as any control call does; one that refuses the value
        (a setting the camera has made inactive) is a ValueError, and the camera stays available."""
        self._check_open()
        try:
            await asyncio.to_thread(self._v4l2.set, self._fd, PICTURE_CIDS[name], value)
        except OSError as e:
            if e.errno in (errno.ENODEV, errno.ENOENT, errno.EIO):
                self._fail(f"camera call failed: {e}")
                raise DeviceUnavailable(self._reason) from e
            raise ValueError(f"The camera refused {name.replace('_', ' ')} {value}: {e.strerror or e}") from e

    async def _read_picture(self) -> None:
        self._picture_now = {name: await self._get(PICTURE_CIDS[name]) for name in self._picture_ranges}

    async def set_picture(self, name: Any, value: Any) -> Dict[str, Any]:
        """Change one picture setting, keep it for the room (put back whenever the camera is opened), and answer the state with what the
        camera says every setting is now (automatic white balance changes the temperature it reports)."""
        if name not in PICTURE_CIDS:
            raise ValueError(f"name must be one of {', '.join(n for n, _ in PICTURE)}")
        value = _whole_number(value, name.replace("_", " "))
        await self._require()
        span = self._picture_ranges.get(name)
        if span is None:
            raise NotReady(f"This camera has no {name.replace('_', ' ')} setting")
        low, high, step = span[0], span[1], max(1, span[2])
        value = max(low, min(high, low + round((value - low) / step) * step))
        if name == "white_balance_temperature" and self._picture_now.get("white_balance_automatic", 0) != 0:
            raise NotReady(WHITE_BALANCE_IS_AUTOMATIC)
        async with self._io_lock:
            await self._write_picture(name, value)
            await self._read_picture()
        picture = {**self._picture, name: value}
        if name == "white_balance_automatic" and value == 0 and "white_balance_temperature" in self._picture_now:
            picture["white_balance_temperature"] = self._picture_now["white_balance_temperature"]   # where the automatic one left it
        self._persist(self._home, self._presets, picture=picture)
        self._picture = picture
        logger.info(f"Camera picture: {name} {value}")
        return self.state()

    async def reset_picture(self) -> Dict[str, Any]:
        """Every picture setting back to the camera's own default, and none kept for the room."""
        await self._require()
        async with self._io_lock:
            for name, _ in PICTURE:
                span = self._picture_ranges.get(name)
                if span is not None and name != "white_balance_temperature":   # the automatic white balance (its default) sets it
                    await self._write_picture(name, span[3])
            await self._read_picture()
        self._persist(self._home, self._presets, picture={})
        self._picture = {}
        logger.info("Camera picture back to the camera's defaults")
        return self.state()

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
            if self._absolute and self._limits:
                await self._keep_inside(level)      # first: a wider picture is moved in before it can show what is past a limit
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

    async def _long(self, work: Callable[[], Awaitable[None]], keeps_position: bool = False) -> None:
        """Run a long move: busy while it runs, stopped and unknown if it is cut short.

        keeps_position: the work also spends time with the motors at rest (Frame people zooms, waits and looks before it
        turns), and the position stays known when it is cut short there. Cut short while a motor runs, it is unknown as
        for any other long move.

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
            after = self._lose_position(keeps_position)
            if isinstance(e, Interrupted):
                logger.info(f"Camera move cut short: {after}")
            if self._fd is not None:
                await self._stop_quietly()
            raise
        except asyncio.CancelledError:
            # The task running the move was cancelled (a shutdown, a request that went away): the same, but the
            # stop is shielded, so that a second cancel while it is on its way cannot abandon it half done, with
            # the control lock released and the speeds and the estimate left as the move had them.
            after = self._lose_position(keeps_position)
            logger.info(f"Camera move cancelled: {after}")
            if self._fd is not None:
                await asyncio.shield(self._stop_quietly())
            raise
        except Exception as e:
            # Anything else (a refusal from the work itself, or a bug in it or under it): the motors are still stopped,
            # because a speed control keeps its value until told otherwise and nothing else would stop them
            after = self._lose_position(keeps_position)
            logger.info(f"Camera move failed ({type(e).__name__}: {e}): {after}")
            if self._fd is not None:
                await self._stop_quietly()
            raise
        finally:
            self._busy = False
            self._long_done.set()

    def _lose_position(self, keeps_position: bool) -> str:
        """A long move that failed: the position is unknown, unless the work keeps it and no motor was running (a camera that
        failed has already lost it in _fail). Says which, for the log."""
        if keeps_position and not (self._pan or self._tilt):
            return "nothing was turning, so the position is as it was"
        self._position_known = False
        return "the position is unknown until the camera is homed again"

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

    async def _go_to(self, saved: Dict[str, Any], zoom: int) -> None:
        """A camera that says where it is: stop any turn, then send it to the saved pan and tilt in one call (on a UVC camera they are
        the two halves of one control), kept inside the limits at the zoom it is going to, and to the zoom. The camera drives itself
        there."""
        zoom = max(self._zoom_range[0], min(self._zoom_range[1], int(zoom)))
        await self._apply_speeds(0, 0)
        async with self._io_lock:
            await self._set_many(self._inside(float(saved["pan"]), float(saved["tilt"]), zoom))
        await self.zoom(zoom)

    def _limit_degrees(self) -> Dict[str, float]:
        return {edge: value / ARCSEC for edge, value in self._limits.items()}

    def _inside(self, pan: float, tilt: float, zoom: int) -> Dict[int, int]:
        """The pan and tilt (camera units) to write for the ones asked for: moved as little as keeps the picture at `zoom` inside the
        limits and the camera inside its range, in whole degrees (the Rally's step). A degree that rounding puts past a limit is
        moved back inside it; within 0.01 deg of a limit is at it (a limit is stored to the 1/3600 deg, and the sums carry float dust)."""
        limits = self._limit_degrees()
        kept = framing.within(pan / ARCSEC, tilt / ARCSEC, zoom, limits)
        near = (round(kept[0]), round(kept[1]))
        held = framing.within(near[0], near[1], zoom, limits)

        def whole(n: int, h: float) -> int:
            if abs(h - n) < 0.01:
                return n * ARCSEC
            return (math.ceil(h - 0.01) if h > n else math.floor(h + 0.01)) * ARCSEC

        return {V4L2_CID_PAN_ABSOLUTE: max(self._pan_range[0], min(self._pan_range[1], whole(near[0], held[0]))),
                V4L2_CID_TILT_ABSOLUTE: max(self._tilt_range[0], min(self._tilt_range[1], whole(near[1], held[1])))}

    async def _keep_inside(self, zoom: int) -> None:
        """Under the control lock: move the camera back inside the limits for a picture at `zoom`, when it is not."""
        now = {V4L2_CID_PAN_ABSOLUTE: await self._get(V4L2_CID_PAN_ABSOLUTE),
               V4L2_CID_TILT_ABSOLUTE: await self._get(V4L2_CID_TILT_ABSOLUTE)}
        inside = self._inside(now[V4L2_CID_PAN_ABSOLUTE], now[V4L2_CID_TILT_ABSOLUTE], zoom)
        if inside != now:
            await self._set_many(inside)

    async def _where(self) -> Dict[str, Any]:
        """Where the camera is, as a home or preset keeps it: the camera's own pan and tilt when it says, else the estimate."""
        if not self._absolute:
            return self._estimate_now()
        async with self._io_lock:
            return {"pan": await self._get(V4L2_CID_PAN_ABSOLUTE), "tilt": await self._get(V4L2_CID_TILT_ABSOLUTE)}

    async def find_stops(self) -> Dict[str, Any]:
        """Drive both axes into their end stops and call that (0, 0): the one place the position is known for sure."""
        await self._long(self._find_stops_work)
        return self.state()

    async def home(self) -> Dict[str, Any]:
        """Find the stops, then move to the saved home and the widest zoom (a camera that says where it is goes straight there)."""
        if not self.home_saved:
            raise NotReady("save a home first")
        if self._absolute:
            await self._long(lambda: self._go_to(self._home, self._zoom_range[0]))
        else:
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
        if not self.position_known:
            raise NotReady("home the camera first")
        home = await self._where()
        self._persist(home, self._presets)      # written first: a write that fails leaves the camera as it was
        self._home = home
        logger.info(f"Camera home saved at {self._home}")
        return self.state()

    async def set_limit(self, edge: Any, on: bool) -> Dict[str, Any]:
        """Set Up: keep the picture's edge `edge` (down, up, left or right) from going past where it is now, at the zoom it is at now (a
        wider picture is moved in to keep to it), or clear that limit. Only on a camera that says where it points."""
        if edge not in framing.EDGES:
            raise ValueError("edge must be down, up, left or right")
        await self._require()
        if not self._absolute:
            raise NotReady(NO_LIMITS)
        limits = dict(self._limits)
        if on:
            zoom = await self.read_zoom()
            at = await self._where()
            half_w, half_h = framing.half_view(zoom if zoom is not None else self._zoom_range[0])
            pan, tilt = at["pan"] / ARCSEC, at["tilt"] / ARCSEC
            reach = {"down": tilt - half_h, "up": tilt + half_h, "left": pan - half_w, "right": pan + half_w}[edge]
            limits[edge] = round(reach * ARCSEC)
        else:
            limits.pop(edge, None)
        self._persist(self._home, self._presets, limits)    # written first: a write that fails leaves the camera as it was
        self._limits = limits
        logger.info(f"Camera limit {edge} {'set at ' + format(limits[edge] / ARCSEC, '.1f') + ' deg' if on else 'cleared'}")
        return self.state()

    def _slot(self, slot: Any) -> str:
        """The settings key of a preset slot. True is 1 and 2.0 is 2 to Python, but neither is a slot number."""
        if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= self.SLOTS:
            raise ValueError(f"preset slots are 1 to {self.SLOTS}")
        return str(slot)

    async def save(self, slot: int) -> Dict[str, Any]:
        """Store the camera's position and zoom in a preset slot."""
        key = self._slot(slot)
        if not self.position_known:
            raise NotReady("home the camera first")
        zoom = await self.read_zoom()          # first: it may wait for the camera, and the position is taken right after
        if not self.position_known:            # a long move was cut short, or a call failed, while the zoom was read
            raise NotReady("home the camera first")
        presets = {**self._presets, key: {**(await self._where()), "zoom": zoom if zoom is not None else self._zoom_range[0]}}
        self._persist(self._home, presets)      # written first: a write that fails leaves the camera as it was
        self._presets = presets
        logger.info(f"Camera preset {key} ({self._names[slot - 1]}) saved: {self._presets[key]}")
        return self.state()

    async def recall(self, slot: int) -> Dict[str, Any]:
        """Move to a saved preset by the difference from where the camera is, homing first when it does not know."""
        key = self._slot(slot)
        preset = self._presets.get(key)
        if not self._usable(preset):
            raise NotReady("nothing saved in this slot")
        if not self.position_known and not self.home_saved:
            raise NotReady("home the camera first")

        async def work():
            if self._absolute:
                return await self._go_to(preset, int(preset["zoom"]))
            if not self._position_known:       # also true when this recall has just cut another long move short
                if not self.home_saved:
                    raise NotReady("home the camera first")
                await self._home_work()
            await self._move_to(float(preset["pan_s"]), float(preset["tilt_s"]))
            await self.zoom(int(preset.get("zoom", self._zoom_range[0])))

        await self._long(work)
        return self.state()

    # ------------------------------------------------------------------
    # Frame people (spec 2026-11-01 camera view, section 5)
    # ------------------------------------------------------------------

    async def frame_people(self, still: Callable[[], Awaitable[Optional[bytes]]],
                           detect: Optional[Callable[[bytes], Any]] = None) -> Dict[str, Any]:
        """Frame the people in the room once: zoom to the widest and let the picture settle, take one picture (`still`: the tap's
        CameraView.still, a JPEG or None), find the faces (`detect`: framing.detect_faces by default, run in a worker thread), zoom
        so the group fills the picture and turn toward it by timed travel, then look once more and correct once at most.

        It is a long move of its own, so the camera is busy throughout, and a drag, an arrow, Home or a preset cuts it short as
        they cut each other. It refuses, before touching anything, while it already runs ("Already framing") and while the
        camera moves ("Moving..."). No one found: the zoom goes back to where it was, and nothing has turned. Without Set up
        (the position not known) it only zooms. A camera that says where it points is turned straight to the group, inside its
        limits (_aim_work). Answers the camera's state with "framed" (people found) and "turned" (whether
        it could turn toward them)."""
        if self._framing:
            raise NotReady(ALREADY_FRAMING)
        if self._busy or self._pan or self._tilt:
            raise NotReady(MOVING)
        outcome: Dict[str, Any] = {}
        self._framing = True      # before anything awaits: a second tap meanwhile is "Already framing"
        try:
            await self._long(lambda: self._frame_work(still, detect or framing.detect_faces, outcome), keeps_position=True)
        finally:
            self._framing = False
        return dict(self.state(), **outcome)

    async def _frame_work(self, still, detect, outcome: Dict[str, Any]) -> None:
        before = await self.read_zoom()
        await self.zoom(self._zoom_range[0])                         # 1. everyone in reach in view
        await self._wait(self.SETTLE_S)
        faces, size = await self._look(still, detect)               # 2. and 3.
        if faces is None or not faces:
            await self.zoom(before if before is not None else self._zoom_range[0])
            if faces is None:
                raise NotReady(NO_PICTURE)
            outcome.update(framed=0, turned=False)
            return
        if self._absolute:
            return await self._aim_work(still, detect, faces, size, outcome)
        position = None
        if self._position_known and self._speed_controls(1, 1):
            self._account_now()
            position = (self._pan_s, self._tilt_s)
        target = framing.plan(faces, size, self._travel_s, position)  # 4.
        zoom = await self.zoom(target.zoom)                           # 5.
        outcome.update(framed=target.people, turned=target.pan_s is not None)
        if target.pan_s is None:                                      # without Set up: the zoom only
            return
        await self._move_to(target.pan_s, target.tilt_s)              # 6.
        await self._wait(self.SETTLE_S)
        faces, size = await self._look(still, detect)                # 7. one check, and one correction at most
        fix = framing.correction(faces or [], size, zoom, self._travel_s, target.people)
        if fix is not None:
            self._account_now()
            await self._move_to(self._pan_s + fix[0], self._tilt_s + fix[1])

    async def _aim_work(self, still, detect, faces, size, outcome: Dict[str, Any]) -> None:
        """Steps 4 to 7 on a camera that says where it is: from where it points, straight to the pan, tilt and zoom that frame the group
        (framing.aim, inside the limits), then one look and one nudge at most, never toward fewer people than were found."""
        at = await self._where()
        zoom, pan, tilt = framing.aim(faces, size, (at["pan"] / ARCSEC, at["tilt"] / ARCSEC), self._limit_degrees(),
                                      self._zoom_range[1])
        outcome.update(framed=len(faces), turned=True)
        await self._go_to({"pan": pan * ARCSEC, "tilt": tilt * ARCSEC}, zoom)
        await self._wait(self.SETTLE_S)
        seen, size = await self._look(still, detect)
        fix = framing.nudge(seen or [], size, zoom, len(faces))
        if fix is not None:
            at = await self._where()
            await self._go_to({"pan": at["pan"] + fix[0] * ARCSEC, "tilt": at["tilt"] + fix[1] * ARCSEC}, zoom)

    async def _look(self, still, detect):
        """One picture and the faces in it: (faces, (width, height)), or (None, None) when the camera gave no picture or the
        faces could not be looked for (logged). A command that cuts the framing short ends the wait for either at once."""
        picture = await self._unless_interrupted(still())
        if not picture:
            return None, None
        try:
            return await self._unless_interrupted(asyncio.to_thread(detect, picture))
        except Interrupted:
            raise
        except Exception:  # noqa: BLE001 - a detector that fails (a damaged model) answers as no picture, and says why in the log
            logger.warning("Frame people could not look for faces", exc_info=True)
            return None, None

    async def _unless_interrupted(self, awaitable: Awaitable[Any]) -> Any:
        """Await something a long move waits for that is not a sleep (a picture, the detector), but give it up and raise
        Interrupted the moment another command cuts the move short, as _wait does."""
        task = asyncio.ensure_future(awaitable)
        waiter = asyncio.ensure_future(self._interrupt.wait())
        try:
            await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            if not task.done():
                task.cancel()
        if self._interrupt.is_set():
            if task.done() and not task.cancelled():
                task.exception()          # retrieved, so a failure nobody wants is not reported as never retrieved
            raise Interrupted("the camera move was interrupted")
        return task.result()

    def _persist(self, home: Optional[Dict[str, Any]], presets: Dict[str, Dict[str, Any]],
                 limits: Optional[Dict[str, int]] = None, picture: Optional[Dict[str, int]] = None) -> None:
        """Write a home, presets and limits (the ones kept now when not given) through the store, as copies: the store keeps what it is
        given by reference, so a later save of another key would otherwise write a preset that is half edited. The callers write the
        new values first and keep them only once this has returned: a save that raises (a full disk) must not leave the camera
        reporting a home, preset or limit that is not on disk."""
        if self._store is not None:
            self._store.save("camera", {"home": dict(home) if home is not None else None,
                                        "presets": {k: dict(v) for k, v in presets.items()},
                                        "limits": dict(self._limits if limits is None else limits),
                                        "picture": dict(self._picture if picture is None else picture)})

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    def set_preview(self, on: bool) -> bool:
        """Keep the camera preview open for PREVIEW_S from now (asking again renews it), or close it."""
        self._preview_until = self._clock() + self.PREVIEW_S if on else 0.0
        return self.preview
