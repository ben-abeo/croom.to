# The camera's live picture on the touch Pi, Frame people, and the camera's own presets

**Date:** 2026-11-01 · **Status:** agreed with Ben in conversation, written for his review · **Branch:** `camera-view` off `main`
(b147c20) · **Builds on:** sound and camera controls (2026-10-08), the room page's screens (2026-10-12), Laptop mode, the research
`docs/superpowers/research/2026-11-01-meetup-framing.md` (read it for the evidence behind every choice here).

## 1. What this is for

Ben asked for camera framing on the touch Pi with a live picture, drag to pan and pinch to zoom, and, after the research, for the two
improvements it found: a "Frame people" button and the MeetUp's own presets.

Today the touch Pi's Camera framing screen has an arrow pad, Home, Zoom in / out and saved positions, but no picture: the picture is on
the TV, only while no meeting runs. RightSight cannot run on the TV Pi (it is Logitech's Windows and Mac software).

Success: someone at the table sees the room as the far end sees it, during a call and when the room is idle, and frames it by dragging
and pinching the picture; one tap frames the people in the room; and, if the MeetUp keeps presets of its own, a saved position is exact.

## 2. Decisions (Ben, 2026-11-01)

- **Used during calls and before them, equally.** The picture works in a call (camera on) and when the room is idle.
- **The picture goes to the touch Pi only.** A laptop or phone that opens the room page keeps the camera buttons and gets no picture.
- **A drag moves the picture** (as maps and photos do on a phone): drag right and the picture slides right, so the camera turns left.
- **All three parts, built in this order:** (1) the live picture, drag and pinch; (2) Frame people; (3) the camera's own presets, built
  only if tests T1 and T3 on the real MeetUp show the camera has them.
- **Frame people runs only when someone taps it.** Never on its own, never at call start.
- **The picture comes from a tap inside the TV's Chromium** (research option i-a), not from a screenshot of the self-view (too slow and
  fragile) and not from a separate program owning the camera (it would put every call at risk).

## 3. The camera screen on the touch Pi

- **Layout at 1280×800:** the live picture on the left, as large as fits at 16:9 (about 760×430); the controls in a narrower column on
  the right: the arrow pad with Home, Zoom out / Zoom in, the saved positions with Save, **Frame people**, and Set up. Every other device
  (no picture) sees today's screen, plus Frame people.
- **Drag with one finger:** once the finger is more than 30 px from where it touched, the camera turns at its one speed opposite to the
  drag's direction (the picture follows the finger); diagonals move pan and tilt together. The camera keeps turning while the finger is
  held, and stops when it lifts or comes back within the 30 px. The drag sends the same move requests as the arrow pad (repeated while
  held; the camera's 1.5 s watchdog stops it if a release is lost).
- **Pinch:** the picture scales under the fingers at once (on the touch Pi); the room sends `POST /api/camera/zoom {"level": 100–500}`
  (the existing route) at most every 100 ms, and the picture settles as the camera's frames catch up. The level follows the pinch's
  scale from the level at its start.
- **No other gestures** in this version (no double tap, no tap to centre).
- **The picture is never mirrored**: it is what the far end sees.
- **When there is no picture**, the picture area says: "Starting the picture…" (the first seconds, and while the TV changes page); "Your
  camera is off in the call" (drag, the arrows and zoom still move the camera); "The camera is on the laptop" (Laptop mode); "No picture
  from the camera", with **Try again**, when no frame comes for 5 s.
- **The TV** keeps showing its full-screen preview when the room is idle and the camera screen is open (as today), and the call during a
  call.

## 4. How the picture reaches the touch Pi

- **The tap (in the TV's Chromium).** A script added to the TV's browser context (`add_init_script`, so every page the TV shows has it:
  the idle `/tv` page, Meet, Zoom's page). Told to start, it calls `getUserMedia` for the room camera (chosen by the name the room's
  camera settings use; Chromium shares the camera it already has open, so Meet's and Zoom's own capture is not disturbed); draws each
  frame to a 640×360 `OffscreenCanvas` about 10 times a second; makes a JPEG (quality 0.7); and hands it to Python through a binding on
  the context (`expose_binding`: no network, so Chrome's Local Network Access rules never apply). Told to stop, it stops its track.
  On request it sends one 1280×720 frame (Frame people).
- **`CameraView` (new, in the room software).** It starts the tap when the touch Pi is watching (or Frame people needs a frame, for as
  long as it takes) **and** the room is not in Laptop mode **and**, in a call, the room's camera is on; it stops it as soon as any of
  these ends. When the TV changes page (idle → call → idle) it
  starts the tap again on the new page. It keeps only the newest frame.
- **The socket.** The touch Pi's camera screen opens `GET /api/camera/view` (a WebSocket) and draws each binary JPEG it receives. The
  socket is accepted only when its peer address (the socket's, never a header) is the address an **allowed touch device** checked in
  from in the last 30 s (`croom.settings.checkin` records it); anything else is refused (403). A slow viewer is sent the newest frame
  and never a queue. The page closes the socket when the camera screen closes (Back, the screen's return to the start screen); the TV Pi
  pings and closes a socket that does not answer within 20 s. When the last viewer goes, the tap stops.
- **Limits:** 640×360 at 10 frames/s, about 3 Mbit/s and a few percent of one core (estimates: test T7 measures them; raise only after it).
- **Moving the camera** is unchanged: `RoomCamera` writes the controls on its own descriptor, which works while Chromium streams.

## 5. Frame people

- **Where:** the camera screen's controls, on every device that shows them. It works when the camera gives a picture (idle, or in a call
  with the camera on; not in Laptop mode). It does not need anyone to be watching the picture.
- **Detector:** OpenCV's YuNet face detector (`cv2.FaceDetectorYN`, OpenCV is already a dependency); its model file (about 230 KB, MIT
  licence) is committed with the code. Not YOLOv8, which `croom.ai` names but the project does not ship, and whose AGPL licence would
  bind the product.
- **One tap:**
  1. zoom to 100 (everyone in reach in view) and wait about 1 s for the picture to settle;
  2. take one 1280×720 frame from the tap;
  3. find the faces;
  4. the group's box: each face's box widened to head and shoulders, all of them together, plus a margin, fitted to 16:9;
  5. zoom so the box fills the picture (between 100 and 500);
  6. turn towards the box's centre, timed with the travel time the camera's Set up measured (the MeetUp takes speeds only); exact when
     part 3 gives exact pan and tilt; without Set up the camera only zooms, and the screen says "Set up the camera to let it turn too";
  7. take a second frame and, if the faces sit off-centre, make one small correction: never more than one.
- **Words:** "Framing…" while it works; "Framed 4 people"; "No one found: the camera stayed where it was" (and it is put back where it
  was). A tap while framing (or while the camera is moving to a preset or Home) is refused: "Already framing" / the existing busy words.

## 6. The camera's own presets (built only if the MeetUp has them)

- **The gate:** T1 shows the Logitech peripheral extension unit `{212de5ff-3080-2c4e-82d9-f587d00540bd}` on the MeetUp, and T3 shows
  its selector 2 ("Pan/Tilt Go": Set Home, Go Home, Set / Restore Preset 1–8) restoring a position saved with the remote. If not, this
  part is dropped and the timed positions stay.
- **Found at start-up:** `RoomCamera` asks the unit (`UVCIOC_CTRL_QUERY`, GET_INFO, on its own descriptor: allowed during a call). If it
  answers, the room uses the camera's presets; if not, the timed ones. No setting.
- **Save** stores the position in the camera ("Set Preset n"); **recall** is "Restore Preset n"; **Home** is Set Home / Go Home. The
  room's positions 1–3 are the remote's three buttons, so the remote and the touch Pi agree; 4–8 are the touch Pi's only.
- **Set up** becomes just "Save as Home" ("Find the stops" is not needed).
- **Positions saved before:** timed positions cannot be converted. The first time, the camera screen says "Save your positions again:
  the camera now remembers them exactly"; their names are kept.
- **Only the selectors Chromium uses for these cameras** (presets and Home) are written; nothing untested. A write the camera refuses is
  said in words and the room falls back to the timed way.
- If T4 shows exact pan and tilt (selector 1 relative in 1/64°, or 12 absolute), Frame people turns exactly.

## 7. When things go wrong

- The tap cannot get the camera (held elsewhere, gone): "No picture from the camera" and Try again. The tap only ever shares the camera:
  it never opens the device outside Chromium (the "camera in use" failure of 2d99537 must not come back).
- The TV changes page during the view: "Starting the picture…", then the picture again from the new page.
- The touch Pi vanishes: its socket is closed within 20 s and the tap stops. Nothing streams unattended.
- Laptop mode starts during the view: the tap stops; the screen says the camera is on the laptop.
- The room's camera is turned off in the call: the tap stops at once (so the camera's light goes off with Meet's or Zoom's track).
- A drag when the picture stops: the camera stops with the finger, as the arrow pad does.

## 8. Tests (only where they guard real risk)

- A socket from any address other than a recently checked-in allowed touch device is refused.
- Nothing streams unattended: the tap stops when the last viewer goes, when a silent socket times out, in Laptop mode, and when the
  camera is turned off in a call.
- A slow viewer gets the newest frame, never a growing queue.
- Drag: right turns the camera left; within 30 px nothing moves.
- Frame people: no faces leaves the camera where it was; the zoom stays within 100–500; one correction at most; without Set up it only
  zooms.
- Presets (if built): a refused write falls back to the timed way and says so.

## 9. Checks on the real MeetUp (Ben, on the TV Pi)

The commands are in the research, section 5. Part 3 waits on **T1** (the extension units) and **T3** (presets through selector 2);
**T4** (exact pan and tilt) improves Frame people. **T7** (the tap's CPU cost and delay), **T8** (the camera light when the camera is
turned off in a call) and **T9** (motor start and coast, for the drag's feel) check parts 1 and 2 once built.

## 10. Not in this

RightSight or any continuous tracking; Frame people at call start; a picture for laptops and phones; double tap or tap to centre; a
picture in Laptop mode.
