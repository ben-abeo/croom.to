# The camera's live picture on the touch Pi, Frame people, and the camera's own presets — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Tasks give requirements, interfaces and tests in
> prose; implementers write the code test-first in the style of the modules they touch.

**Goal:** the touch Pi's camera screen shows the room's live picture (in a call with the camera on, and idle), moved by dragging the
picture and zoomed by pinching it; a Frame people button frames the people in the room with one tap; and, if the MeetUp keeps presets
of its own, the room's saved positions use them.

**Architecture:** a script in the TV's Chromium context (the "tap") shares the camera Chromium already has open and hands small JPEGs to
Python through a Playwright binding; `CameraView` starts and stops it and keeps the newest frame; a WebSocket on the control service
sends frames to an allowed touch Pi only; the room page draws them and turns drags and pinches into the existing camera routes. Frame
people takes one larger frame from the tap and finds faces with OpenCV's YuNet.

**Tech Stack:** Python 3.11 asyncio, aiohttp (WebSocket), Playwright (Chromium), OpenCV 5 (`cv2.FaceDetectorYN`), plain JS room page,
V4L2 / UVC ioctls (`src/croom/devices/v4l2.py`).

**Spec:** `docs/superpowers/specs/2026-11-01-camera-view-design.md`. Evidence: `docs/superpowers/research/2026-11-01-meetup-framing.md`.

## Global Constraints

- Branch `camera-view` off `main` (b147c20); no attribution lines in commits; commit style `feat(camera): …` / `fix(camera): …`; tests
  `.venv/bin/python -m pytest <paths> -q`; headless Chromium from the venv's Playwright for page tests (skip when missing, as
  `tests/unit/control/test_page.py` does).
- **Tests only where they guard real risk** (Ben: "Don't write tests for everything you do though."): the Review Focus items and the
  spec's section 8. No tests that restate the code.
- **Nothing opens the camera device outside Chromium** to read frames (the "camera in use" failure fixed in 2d99537 must not come back);
  the tap only ever *shares* Chromium's camera through `getUserMedia`. `RoomCamera` keeps writing controls on its own descriptor.
- **The picture goes to an allowed touch Pi only**: a WebSocket whose socket peer address (never a header) equals the address an allowed
  device of kind `touch` checked in from within the last 30 s (`croom.settings.checkin` registry). Everything else: 403.
- **Nothing streams unattended:** the tap runs only while (a viewer is connected, or Frame people needs a frame) and the room is not in
  Laptop mode and, in a call, the room's camera is on.
- Frame size and rate: 640×360 JPEG quality 0.7 at 10 frames/s for the view; one 1280×720 frame for Frame people. Zoom 100–500.
- The picture is never mirrored. A drag moves the picture (drag right → camera pans left); dead zone 30 px.
- Frame people runs only on a tap. Words exactly: "Framing…", "Framed N people" ("Framed 1 person"), "No one found: the camera stayed
  where it was", "Already framing", "Set up the camera to let it turn too".
- Picture-area words exactly: "Starting the picture…", "Your camera is off in the call", "The camera is on the laptop", "No picture from
  the camera" (with a Try again button, after 5 s without a frame).
- Keep everything that works today working: calls (Meet, Zoom) with their camera, the TV's idle preview, the arrow pad, zoom steps,
  presets, Home, Find the stops, Laptop mode.

## Review Focus

1. A touch Pi that loses power or network while watching: its socket is closed within about 20 s and the tap stops (the camera's light
   goes off with the call's own track when the call's camera is off).
2. The room's camera turned off in the call (from the touch Pi or at the TV) while the picture is open: the tap stops at once and the
   screen says "Your camera is off in the call"; turned on again, the picture comes back without reopening the screen.
3. The TV changing page while the picture is open (joining or leaving a call): the picture pauses and resumes from the new page; no
   tap is left running on a page that is gone, and no second tap runs.
4. A drag whose release is lost (the finger leaves the screen edge, the page loses focus): the camera stops (the move requests stop
   repeating; the camera's 1.5 s watchdog stops the motors).
5. Frame people while the camera is moving (a drag, Home, a preset) or twice in a row: refused in words, nothing moves twice.

---

### Task 1: The tap and CameraView

**Files:** create `src/croom/meeting/camera_view.py` (the tap's JavaScript as a constant, and `CameraView`); modify
`src/croom/meeting/display.py` (`TvDisplay` installs the tap's init script and the binding on its context, and tells `CameraView` when
the TV's page changes), the place the agent builds the TV display and devices (`src/croom/core/agent.py` or the meeting / devices
services: follow where `TvDisplay` is created) to create one `CameraView`; tests `tests/unit/meeting/test_camera_view.py`.

**Interfaces — produces (Tasks 2–4 use these):**
- `CameraView.watch() -> None` / `CameraView.unwatch() -> None`: a viewer came / went (counted).
- `CameraView.latest() -> Optional[tuple[int, bytes]]`: the newest frame `(sequence, jpeg)`, or None.
- `CameraView.frames() -> AsyncIterator[bytes]`: for one viewer, yields the newest frame each time a new one exists (never a queue:
  a slow consumer skips frames).
- `CameraView.state() -> str`: `"off"`, `"starting"`, `"on"`, `"camera_off"` (in a call, camera off), `"laptop"`, `"failed"`.
- `async CameraView.still(width=1280, height=720, timeout_s=5.0) -> Optional[bytes]`: one larger JPEG for Frame people (starts the tap
  for as long as it takes when nobody watches; None when the camera gives no picture).

**Requirements:**
- The tap script (installed with `context.add_init_script`, so every page the TV shows has it): exposes `window.__croomTap.start(opts)` /
  `.stop()` / `.still(w, h)`. `start` calls `navigator.mediaDevices.getUserMedia({video: {deviceId: <the room camera>}})` (the room
  camera found with `enumerateDevices` by the label `RoomCamera` reports, else the first video input), draws frames to an
  `OffscreenCanvas` of 640×360 about every 100 ms, `convertToBlob({type: "image/jpeg", quality: 0.7})`, and passes each (base64) to the
  binding `crystalMeetCameraFrame`. `stop` stops its own track only (never Meet's or Zoom's). A track that ends on its own reports
  "failed". Mirroring: none.
- `CameraView` starts the tap on the TV's current page when (viewers > 0 or a still is pending) and not in Laptop mode and not (in a
  call and the meeting's `is_camera_on` is false); stops it at once when any of these ends (a meeting state callback, the meeting's
  camera state, the devices service's Laptop mode — find how each is observed today). When `TvDisplay` hands the page to Meet / Zoom or
  takes it back, `CameraView` starts the tap again on the new page if still wanted; one tap at a time.
- If no frame arrives within 5 s of starting: state "failed" (a later `watch()` or a retry starts again).

**Tests (risky paths only):** in headless Chromium with `--use-fake-device-for-media-stream --use-fake-ui-for-media-stream` and a
local page: frames arrive at the binding after `watch()`; `unwatch()` of the last viewer stops the track (the page's track
`readyState` is "ended"); Laptop mode and a call's camera turned off stop it; a page change runs exactly one tap on the new page;
`frames()` for a consumer that sleeps between reads yields the newest frame, not every frame.

---

### Task 2: The WebSocket to the touch Pi

**Files:** modify `src/croom/control/service.py` (route `GET /api/camera/view`; the status gains `camera.view` for the requesting
address); tests `tests/unit/control/test_camera_view_api.py`.

**Interfaces:** consumes Task 1's `CameraView.watch/unwatch/frames/state`. Produces for Task 3: the socket sends binary messages (one
JPEG each) and text messages `{"state": <CameraView.state()>}` when the state changes; `/api/status` → `camera.view` is
`{"allowed": bool}` for the address that asked (the page decides whether to draw the picture area).

**Requirements:**
- Allowed only when the socket's peer address equals the `address` of a registry device with `kind == "touch"`, `state == "allowed"`
  and seen within 30 s (`ControlService._registry`); else 403 before the upgrade. The same check gives `camera.view.allowed`.
- `heartbeat=10` (aiohttp pings; a socket that does not answer is closed within ~20 s), `watch()` on open and `unwatch()` in a
  `finally` (every way the socket ends). Frames are sent from `frames()`; a send that is still in progress when a newer frame exists
  skips to the newest.

**Tests (risky paths only):** a socket from an address that is not an allowed, recently seen touch device is refused (403) and never
calls `watch()`; a socket whose client stops answering pings is closed and `unwatch()` is called; a client that reads slowly receives
the newest frame after its pause.

---

### Task 3: The touch Pi's camera screen with the picture, drag and pinch

**Files:** modify `src/croom/control/static/devices.js` (the camera screen: picture area, socket, drag, pinch), `index.html` (the picture
area in the camera view), `style.css` (the 1280×800 layout: picture left, controls column right); tests: add to
`tests/unit/control/test_page.py` (or a new `test_camera_view_page.py` beside it, following its fixtures) for the risky items only.

**Interfaces:** consumes Task 2's socket and `camera.view.allowed`; the existing `/api/camera/move {pan, tilt}` (repeat while held, as the
arrow pad's `holdToMove` does) and `/api/camera/zoom {level}`.

**Requirements:**
- When `camera.view.allowed` and the camera view is shown: the picture area (16:9, as large as fits on the left), the socket opened;
  closed when the view closes (Back, the screen's own return, a page hide). Not allowed: today's screen, unchanged.
- Frames drawn into an `<img>` (object URLs, the previous one revoked) or a `<canvas>`; the state messages and a 5 s no-frame timer drive
  the words (Global Constraints) and Try again (closes and reopens the socket).
- Drag (pointer events, one pointer): beyond 30 px from the touch point, pan = −sign(dx), tilt = +sign(dy) (screen y grows downward: a
  drag down moves the picture down, so the camera tilts up) — only for the axes beyond 30 px; repeated as the arrow pad repeats; back
  within 30 px or released or `pointercancel` / `lostpointercapture` / page hide → `{pan: 0, tilt: 0}` once.
- Pinch (two pointers): the picture scales by the pinch ratio at once (CSS transform); the level = clamp(start level × ratio, 100, 500)
  sent at most every 100 ms and once at the end; the transform is cleared when the pinch ends.
- The arrow pad, zoom buttons, presets, Save, Set up stay in the controls column.

**Tests (risky paths only):** a drag right of 40 px sends `pan: -1, tilt: 0` and its release sends `pan: 0, tilt: 0`; a drag of 20 px
sends nothing; a `pointercancel` mid-drag sends the stop; leaving the camera view closes the socket; a page whose status says
`allowed: false` opens no socket.

---

### Task 4: Frame people

**Files:** create `src/croom/devices/framing.py` (pure: faces → the group's box → target zoom and the pan / tilt travel), commit the
YuNet model `src/croom/devices/models/face_detection_yunet_2023mar.onnx` (from opencv/opencv_zoo, MIT; check its SHA-256 against the
one opencv_zoo publishes and note it with the licence in `src/croom/control/static/LICENSES.md` or the repo's licence list — follow
where third-party licences are listed); modify `src/croom/devices/camera.py` (`RoomCamera.frame_people(still)`, using the travel time
Set up measured), `src/croom/control/service.py` (`POST /api/camera/frame`), `devices.js` / `index.html` (the Frame people button and its
words); tests `tests/unit/devices/test_framing.py`.

**Interfaces:** consumes Task 1's `CameraView.still()`; `RoomCamera.zoom(level)`, its travel accounting (`_move_to(pan_s, tilt_s)`,
`position_known`, `busy`). Produces `POST /api/camera/frame` → `{"framed": n}` or a refusal in words.

**Requirements:** the spec's section 5 steps 1–7 exactly (zoom 100, wait ~1 s, `still()`, `cv2.FaceDetectorYN` on it, each face box
widened to head and shoulders (×2 wide, ×2.5 tall, centred on the face, extending downward), the union plus a 10 % margin fitted to 16:9,
zoom so the box fills the picture (clamped 100–500), turn towards the box centre by timed travel converted from the offset (the field of
view at zoom 100 and the camera's ±25° pan / ±15° tilt from the research), then one check and at most one correction). No faces: back to
where it was. Not `position_known`: zoom only, with the words. Busy (moving, framing): refused ("Already framing" while framing, else the
existing busy words). The geometry is in `framing.py` as pure functions so it is tested without a camera.

**Tests (risky paths only):** no faces → the camera is put back (zoom and position as before); a box at the picture's edge gives a zoom
within 100–500 and a turn toward it; a second correction is never made; without `position_known` only the zoom is set; a frame request
while busy is refused.

---

### Task 5 (gated): The camera's own presets

**Gate:** do not start this task until Ben has run T1 and T3 (research section 5) on the TV Pi and the results are in this plan. If the
MeetUp does not have the Logitech peripheral unit `{212de5ff-3080-2c4e-82d9-f587d00540bd}` with presets on selector 2, this task is
dropped.

**Files:** modify `src/croom/devices/v4l2.py` (`UVCIOC_CTRL_QUERY` for a unit / selector: GET_INFO, GET_CUR, SET_CUR), `camera.py`
(found at start-up; save / recall / Home through the unit when it answers; Set up is only Save as Home then); `devices.js` (the
"Save your positions again" note once); tests `tests/unit/devices/` with `fake_v4l2.py`.

**Requirements:** the spec's section 6 (positions 1–3 are the remote's slots, up to 8; only the selectors Chromium uses; a refused write
falls back to the timed way and says so; names kept; old timed positions shown as needing a new save).

**Tests (risky paths only):** a unit that does not answer leaves the timed presets in use; a refused SET_CUR falls back and says so; a
recall during a call writes only the unit's control (no stream ioctl).
