"""
The camera's live picture for the touch Pi (spec 2026-11-01 camera view, section 4): a tap in the TV's Chromium and CameraView, which
runs it.

The tap is a script on every page the TV's browser shows (the idle /tv page, Meet, Zoom's page). Told to start, it asks getUserMedia for
the room camera, which Chromium shares with whatever has the camera open already (the TV's preview, Meet's or Zoom's capture): nothing
outside Chromium ever opens the device to read frames. About ten times a second it draws the picture into a 640x360 canvas, never mirrored,
makes a JPEG of it and hands it to Python through a binding on the browser context: no network, so Chrome's Local Network Access rules never
apply. Told to stop, it stops its own track only. On request it makes one larger JPEG (Frame people).

CameraView runs the tap only while somebody watches (or Frame people needs a frame) and the room is not in Laptop mode and, in a call, the
room's camera is on; it stops it as soon as any of these ends. When the TV's page loads a new document (the page lent to a call, or handed
back to the screensaver) the tap there is gone with the old document, and CameraView starts it again on the new one. It keeps only the
newest frame.
"""

import asyncio
import base64
import binascii
import logging
from typing import AsyncIterator, Optional, Tuple

from croom.meeting.providers.base import MeetingState

logger = logging.getLogger(__name__)

BINDING = "crystalMeetCameraFrame"
VIEW = {"width": 640, "height": 360, "interval": 100, "quality": 0.7}
# What the TV's idle preview asks for (tv.js PREVIEW_SIZE; keep the two the same). Chromium opens the camera once, at the size its first
# caller asks for, and every later caller shares it: a tap that opens the camera before the preview asks the preview's size, so whoever
# opens first, the preview gets what it asks for.
PREVIEW_SIZE = {"width": {"ideal": 3840}, "height": {"ideal": 2160}, "frameRate": {"ideal": 30}}
STILL_QUALITY = 0.9

# The tap. In the top frame only (Meet's iframes need none; Meet and Zoom's client open the camera in the top document), defined once per
# document. It never chooses the camera's size:
# - It notes the page's own camera tracks (the TV preview's, Meet's, Zoom's) by wrapping getUserMedia, and MediaStreamTrack's stop and
#   clone, and reports through the binding when the page's camera goes on and off: (null, "page_camera_on") / (null, "page_camera_off").
# - When the page has its camera open, the tap opens the same device at the same size (a share that changes nothing). Otherwise, with
#   opts.follow (a call's page) it waits for the call's own camera and answers "waiting"; on the TV's own page it opens the camera with
#   opts.size, the preview's own size.
# - With opts.follow, the call's own camera stopping (the camera turned off at the TV) stops the tap at once.
# start() answers "on", "waiting" or "error: <name>". A track of the tap's that ends by itself (the camera unplugged, taken away by the USB
# switch) is reported as (null, "ended"), and a start that fails after waiting as (null, "error"). `opened` counts the tracks this
# document's tap opened and `lastTrack` is the last of them, for the tests.
TAP_SCRIPT = """(() => {
  if (window !== window.top || window.__croomTap) return;
  const BINDING = "%s";
  const tap = { opened: 0, lastTrack: null };
  let stream = null, video = null, timer = null, pending = null, generation = 0, encoding = false, options = null, waiting = false;

  const send = (data, event) => {
    try { const binding = window[BINDING]; if (binding) Promise.resolve(binding(data, event || null)).catch(() => {}); } catch (e) {}
  };

  // The page's own camera
  const devices = window.MediaDevices && MediaDevices.prototype;
  const getUserMedia = devices && devices.getUserMedia;
  const pageTracks = new Set();
  let pageLive = false;
  const pageTrack = () => { for (const t of pageTracks) { if (t.readyState === "live") return t; } return null; };
  const pageChanged = () => {
    const live = Boolean(pageTrack());
    if (live === pageLive) return;
    pageLive = live;
    send(null, live ? "page_camera_on" : "page_camera_off");
    if (live && waiting) {
      open(pageTrack()).then((answer) => { if (answer.startsWith("error")) send(null, "error"); });
    } else if (!live && options && options.follow && (stream || pending)) {
      tap.stop();   // the call turned its camera off: the tap's own track must not keep the camera on
    }
  };
  const adopt = (track) => {
    if (track.kind !== "video" || pageTracks.has(track)) return;
    pageTracks.add(track);
    track.addEventListener("ended", () => { pageTracks.delete(track); pageChanged(); });
    pageChanged();
  };
  if (getUserMedia) {
    devices.getUserMedia = function (constraints) {
      return getUserMedia.call(this, constraints).then((s) => { s.getVideoTracks().forEach(adopt); return s; });
    };
  }
  const trackStop = MediaStreamTrack.prototype.stop;
  MediaStreamTrack.prototype.stop = function () { trackStop.call(this); if (pageTracks.delete(this)) pageChanged(); };
  const trackClone = MediaStreamTrack.prototype.clone;
  MediaStreamTrack.prototype.clone = function () { const c = trackClone.call(this); if (pageTracks.has(this)) adopt(c); return c; };

  // The tap's own track
  const toBase64 = (blob) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => { const url = String(reader.result); resolve(url.slice(url.indexOf(",") + 1)); };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
  const ready = () => Boolean(video && video.readyState >= 2 && video.videoWidth > 0);
  const jpeg = (width, height, quality) => {   // the picture fitted into width x height, as the far end sees it (never mirrored)
    const canvas = new OffscreenCanvas(width, height);
    const g = canvas.getContext("2d");
    g.fillStyle = "#000";
    g.fillRect(0, 0, width, height);
    const scale = Math.min(width / video.videoWidth, height / video.videoHeight);
    const w = video.videoWidth * scale, h = video.videoHeight * scale;
    g.drawImage(video, (width - w) / 2, (height - h) / 2, w, h);
    return canvas.convertToBlob({ type: "image/jpeg", quality }).then(toBase64);
  };
  const release = () => {
    if (timer) clearInterval(timer);
    timer = null;
    if (stream) stream.getTracks().forEach((t) => { t.onended = null; trackStop.call(t); });
    stream = null;
    if (video) video.srcObject = null;
    video = null;
  };
  const tick = async () => {
    if (encoding || !ready()) return;
    encoding = true;
    const mine = generation;
    try {
      const data = await jpeg(options.width, options.height, options.quality);
      if (mine === generation && stream) send(data);
    } catch (e) {
    } finally {
      encoding = false;
    }
  };
  const choose = async (label) => {   // the room camera by the name the room knows it by, else the first camera
    const cameras = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
    const wanted = String(label || "").trim().toLowerCase();
    const named = wanted ? cameras.find((d) => d.label.toLowerCase().includes(wanted)) : null;
    return (named || cameras[0] || {}).deviceId || null;
  };
  const open = (shared) => {
    waiting = false;
    const mine = ++generation;
    pending = (async () => {
      try {
        let constraints;
        if (shared) {   // the page's camera is open: the same device at the same size
          const s = shared.getSettings();
          constraints = { deviceId: { exact: s.deviceId } };
          if (s.width && s.height) { constraints.width = { ideal: s.width }; constraints.height = { ideal: s.height }; }
        } else {        // the TV's own page with no preview: the preview's size, so that the preview opened later gets it
          const deviceId = await choose(options.label);
          constraints = Object.assign({}, options.size || {});
          if (deviceId) constraints.deviceId = { exact: deviceId };
        }
        const opened = await getUserMedia.call(navigator.mediaDevices, { video: constraints, audio: false });
        const track = opened.getVideoTracks()[0];
        if (mine !== generation || (options.follow && !pageTrack())) {
          opened.getTracks().forEach((t) => trackStop.call(t));
          return "stopped";
        }
        tap.opened += 1;
        tap.lastTrack = track;
        stream = opened;
        track.onended = () => { if (stream === opened) { release(); send(null, "ended"); } };
        video = document.createElement("video");
        video.muted = true;
        video.playsInline = true;
        video.srcObject = opened;
        video.play().catch(() => {});
        timer = setInterval(tick, options.interval);
        return "on";
      } catch (e) {
        return "error: " + ((e && e.name) || String(e));
      } finally {
        if (mine === generation) pending = null;
      }
    })();
    return pending;
  };

  tap.start = (opts) => {
    if (stream) return Promise.resolve("on");
    if (pending) return pending;
    options = opts;
    const shared = pageTrack();
    if (!shared && opts.follow) { waiting = true; return Promise.resolve("waiting"); }
    if (!getUserMedia) return Promise.resolve("error: no camera on this page");
    return open(shared);
  };
  tap.stop = () => { generation += 1; pending = null; waiting = false; release(); };
  tap.running = () => Boolean(stream);
  tap.still = async (width, height) => (ready() ? jpeg(width, height, %s) : null);
  Object.defineProperty(window, "__croomTap", { value: tap });
})()""" % (BINDING, STILL_QUALITY)

START = "(opts) => window.__croomTap ? window.__croomTap.start(opts) : 'error: no tap on this page'"
STOP = "() => { if (window.__croomTap) window.__croomTap.stop(); }"
STILL = "([w, h]) => window.__croomTap ? window.__croomTap.still(w, h) : null"


class CameraView:
    FIRST_FRAME_S = 5.0     # a tap with no frame this long after its start: "failed" until a watch() or a retry
    CHECK_S = 0.25          # while the tap is wanted, the meeting's camera and state are looked at this often (no callback says
                            # when the call's camera is turned off)
    CALL_TIMEOUT_S = 5.0    # a start, stop or still on the page answers within this
    PAUSED = (MeetingState.JOINING, MeetingState.LEAVING)   # the call's page is opening (or closing) its own camera: the tap waits
    IN_CALL = (MeetingState.IN_LOBBY, MeetingState.CONNECTED)  # the TV's page is the call's: the tap follows the call's own camera

    def __init__(self, meeting=None, devices=None):
        self._meeting = meeting      # the meeting service: its state and the call's camera
        self._devices = devices      # the devices service: Laptop mode (lent) and the camera's name (camera.label)
        self._page = None            # the TV's page
        self._document = 0           # counts the documents the page has loaded: a tap belongs to one
        self._viewers = 0
        self._stills = 0
        self._tap_on = False         # the tap runs (or is starting) on the page's current document
        self._tap_page = None
        self._first_frame_due: Optional[float] = None
        self._failed = False
        self._waiting = False        # the tap waits for the call's own camera to open
        self._page_off = False       # the call's page stopped its own camera (turned off at the TV) before the meeting says so
        self._frame: Optional[Tuple[int, bytes]] = None
        self._sequence = 0
        self._fresh = asyncio.Event()   # set (and replaced) at each new frame
        self._changed = asyncio.Event()   # set (and replaced) when state() has changed (see state_changes)
        self._announced: Optional[str] = None
        self._wake = asyncio.Event()
        self._runner: Optional[asyncio.Task] = None
        if meeting is not None and hasattr(meeting, "add_state_callback"):
            meeting.add_state_callback(self._on_meeting_state)
        if devices is not None and hasattr(devices, "add_lent_callback"):
            devices.add_lent_callback(self._on_lent)

    # ------------------------------------------------------------------
    # For the touch Pi's camera screen and Frame people
    # ------------------------------------------------------------------

    def watch(self) -> None:
        """A viewer came: the tap runs while there is one (and the room allows it). A tap that failed is tried again."""
        self._viewers += 1
        self._failed = False
        self._kick()

    def unwatch(self) -> None:
        """A viewer went: the last one stops the tap at once."""
        self._viewers = max(0, self._viewers - 1)
        self._kick()

    def retry(self) -> None:
        """Try again after "failed" (the screen's Try again)."""
        self._failed = False
        self._kick()

    def latest(self) -> Optional[Tuple[int, bytes]]:
        """The newest frame (sequence, jpeg), or None while the tap gives none."""
        return self._frame

    async def frames(self) -> AsyncIterator[bytes]:
        """For one viewer: the newest frame each time there is a new one. Never a queue: frames that came while the viewer was busy are
        skipped."""
        last = 0
        while True:
            frame = self._frame
            if frame is not None and frame[0] > last:
                last = frame[0]
                yield frame[1]
                continue
            await self._fresh.wait()

    def state(self) -> str:
        """"off" (nobody watches), "laptop", "camera_off" (in a call, the room's camera is off), "failed" (no picture), "on" or "starting"."""
        if not self._wanted():
            return "off"
        blocked = self._blocked()
        if blocked is not None:
            return blocked
        if self._failed:
            return "failed"
        return "on" if self._tap_on and self._frame is not None else "starting"

    async def state_changes(self) -> AsyncIterator[str]:
        """For one viewer (the touch Pi's socket): state() now, and again each time it changes. The runner looks at the room every CHECK_S
        while anyone watches (and at once when something wakes it), so a change is told within that; nothing polls beyond it."""
        last = None
        while True:
            state = self.state()
            if state != last:
                last = state
                yield state
                continue
            await self._changed.wait()

    def _announce(self) -> None:
        state = self.state()
        if state != self._announced:
            self._announced = state
            changed, self._changed = self._changed, asyncio.Event()
            changed.set()

    async def still(self, width: int = 1280, height: int = 720, timeout_s: float = 5.0) -> Optional[bytes]:
        """One larger JPEG (Frame people). The tap runs for as long as this takes when nobody watches. None when the camera gives no
        picture within timeout_s (Laptop mode, the call's camera off, no frame)."""
        self._stills += 1
        self._failed = False
        self._kick()
        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    if self._failed or self._blocked() in ("laptop", "camera_off"):
                        return None
                    page = self._tap_page
                    if self._tap_on and self._frame is not None and page is not None:
                        try:
                            data = await page.evaluate(STILL, [width, height])
                        except Exception as e:  # noqa: BLE001 - the page is between documents: the tap starts again there
                            logger.debug(f"The camera's still was not taken ({type(e).__name__}); trying again")
                            data = None
                        if data:
                            return base64.b64decode(data)
                    fresh = self._fresh
                    try:
                        await asyncio.wait_for(fresh.wait(), self.CHECK_S)
                    except asyncio.TimeoutError:
                        pass
        except TimeoutError:
            logger.info(f"No still from the camera in {timeout_s:g} s")
            return None
        finally:
            self._stills -= 1
            self._kick()

    async def close(self) -> None:
        """The room stops: the tap stops and nothing starts it again."""
        self._viewers = self._stills = 0
        runner, self._runner = self._runner, None
        if runner is not None and not runner.done():
            runner.cancel()
            await asyncio.wait({runner})
        if self._tap_on:
            await self._stop()
        self._announce()

    # ------------------------------------------------------------------
    # From the TV's browser (TvDisplay)
    # ------------------------------------------------------------------

    async def install(self, context) -> None:
        """Put the tap on every page of a new browser context, and the binding it hands frames to."""
        await context.add_init_script(TAP_SCRIPT)
        await context.expose_binding(BINDING, self._on_binding)

    def set_page(self, page) -> None:
        """The TV's page: a fresh browser's, or None when the browser goes. Each new document it loads gets the tap again."""
        if page is self._page:
            return
        if self._page is not None:
            try:
                self._page.remove_listener("domcontentloaded", self._on_document)
            except Exception:  # noqa: BLE001 - a page that has gone
                pass
        self._page = page
        if page is not None:
            page.on("domcontentloaded", self._on_document)
        self._new_document()

    def _on_document(self, page) -> None:
        if page is self._page:
            self._new_document()

    def _new_document(self) -> None:
        """The tap went with the document it ran on: the picture pauses, and starts again on the new one when still wanted."""
        self._document += 1
        if self._tap_on:
            logger.info("The TV's page changed: the camera's picture starts again on the new one")
        self._tap_on = False
        self._first_frame_due = None
        self._frame = None
        self._failed = False
        self._waiting = False
        self._page_off = False
        self._kick()

    def _on_binding(self, source, data=None, event=None) -> None:
        """A frame (base64 JPEG) from the tap; (None, "page_camera_on" / "page_camera_off"): the page's own camera opened or stopped;
        (None, "ended"): the tap's track ended by itself; (None, "error"): a tap that waited for the call's camera could not open."""
        page = source.get("page")
        if event in ("page_camera_on", "page_camera_off"):
            if page is self._page:
                self._on_page_camera(event == "page_camera_on")
            return
        if not self._tap_on or page is not self._tap_page:
            return
        if event in ("ended", "error"):
            logger.warning("The camera's picture ended by itself (the camera went away)" if event == "ended"
                           else "The camera's picture could not start with the call's camera")
            self._tap_on = False
            self._first_frame_due = None
            self._frame = None
            self._failed = True
            self._kick()
            return
        if not isinstance(data, str):
            return
        try:
            jpeg = base64.b64decode(data)
        except (binascii.Error, ValueError):
            return
        self._publish(jpeg)

    def _on_page_camera(self, on: bool) -> None:
        """The page's own camera (the call's) opened, or stopped: in a call, a stop is the camera turned off at the TV, and the tap (which
        has stopped itself on the page) stays off until the call's camera is on again (by the page, or by the meeting's own word)."""
        if on:
            self._page_off = False
            if self._tap_on and self._waiting:   # the tap opens on the page now: the wait for its first frame starts
                self._waiting = False
                self._first_frame_due = asyncio.get_running_loop().time() + self.FIRST_FRAME_S
        elif self._in_call():
            logger.info("The call's camera was turned off on the TV: the camera's picture stops")
            self._page_off = True
        self._kick()

    def _publish(self, jpeg: bytes) -> None:
        self._sequence += 1
        self._frame = (self._sequence, jpeg)
        self._first_frame_due = None
        self._waiting = False
        fresh, self._fresh = self._fresh, asyncio.Event()
        fresh.set()
        self._announce()   # (the first frame makes the state "on")

    # ------------------------------------------------------------------
    # What the room allows
    # ------------------------------------------------------------------

    def _on_meeting_state(self, state) -> None:
        self._kick()

    def _on_lent(self, lent: bool) -> None:
        """Laptop mode started (the tap stops before the switch moves the MeetUp) or ended (a track the move ended is no failure)."""
        if not lent:
            self._failed = False
        self._kick()

    def _wanted(self) -> bool:
        return self._viewers > 0 or self._stills > 0

    def _blocked(self) -> Optional[str]:
        """Why the room does not allow the tap just now ("laptop", "camera_off", "starting" while a call's page opens), or None."""
        if self._devices is not None and getattr(self._devices, "lent", False):
            return "laptop"
        meeting = self._meeting
        if meeting is None:
            return None
        state = meeting.state
        if state in self.PAUSED:
            return "starting"
        current = meeting.current_meeting
        if state == MeetingState.IDLE or current is None:
            self._page_off = False
            return None
        if not current.is_camera_on:
            self._page_off = False   # the meeting agrees now: its word alone says when the camera is on again
            return "camera_off"
        return "camera_off" if self._page_off else None

    def _in_call(self) -> bool:
        return self._meeting is not None and self._meeting.state in self.IN_CALL

    def _label(self) -> str:
        camera = getattr(self._devices, "camera", None)
        return getattr(camera, "label", None) or ""

    # ------------------------------------------------------------------
    # The runner: the one task that starts and stops the tap
    # ------------------------------------------------------------------

    def _kick(self) -> None:
        self._wake.set()
        if (self._wanted() or self._tap_on) and (self._runner is None or self._runner.done()):
            try:
                self._runner = asyncio.get_running_loop().create_task(self._run())
            except RuntimeError:   # no loop: nothing runs the tap outside the agent's loop
                pass

    async def _run(self) -> None:
        """Start or stop the tap as the room allows, every CHECK_S and at once when something changes; ends with the tap stopped once
        nothing wants it. One task, so one tap at a time."""
        try:
            while True:
                self._wake.clear()
                await self._reconcile()
                self._announce()
                if not self._wanted() and not self._tap_on:
                    return
                try:
                    await asyncio.wait_for(self._wake.wait(), self.CHECK_S)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a fault here must not leave the agent with a task that raised
            logger.exception("The camera's picture stopped on an error")
            if self._tap_on:
                await self._stop()

    async def _reconcile(self) -> None:
        allowed = self._wanted() and self._blocked() is None and not self._failed and self._page is not None
        if allowed and not self._tap_on:
            await self._start()
        elif not allowed and self._tap_on:
            await self._stop()
        elif self._tap_on and self._first_frame_due is not None and asyncio.get_running_loop().time() >= self._first_frame_due:
            logger.warning(f"No picture from the camera {self.FIRST_FRAME_S:g} s after the tap started")
            self._failed = True
            await self._stop()

    async def _start(self) -> None:
        page, document = self._page, self._document
        self._tap_on, self._tap_page, self._frame = True, page, None   # (frames that come before the start answers are taken)
        self._first_frame_due = asyncio.get_running_loop().time() + self.FIRST_FRAME_S
        options = dict(VIEW, label=self._label(), size=PREVIEW_SIZE, follow=self._in_call())
        try:
            answer = await asyncio.wait_for(page.evaluate(START, options), self.CALL_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 - the page between documents, or a camera that does not answer
            answer = f"error: {type(e).__name__}: {e}"
        if document != self._document or page is not self._page:
            return   # a new document came meanwhile: the tap went with the old one, and starts again on the new
        if answer == "waiting":   # the call has not opened its camera yet: the tap opens after it, and its 5 s start then
            if self._frame is None:
                self._waiting, self._first_frame_due = True, None
            return
        if answer != "on":
            logger.warning(f"The camera's picture could not start ({answer})")
            self._failed = True
            await self._stop()

    async def _stop(self) -> None:
        page = self._tap_page
        self._tap_on, self._tap_page, self._frame, self._first_frame_due, self._waiting = False, None, None, None, False
        if page is None or page.is_closed():
            return
        try:
            await asyncio.wait_for(page.evaluate(STOP), self.CALL_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 - the document went, and its track with it
            logger.debug(f"The camera's tap was not stopped on the page ({type(e).__name__}); its document has gone")
