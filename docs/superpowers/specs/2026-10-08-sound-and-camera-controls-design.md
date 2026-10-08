# Sound and camera from the room page

Date: 2026-10-08. Status: approved in conversation, written for review.

## 1. Purpose

The TV Pi has no keyboard or mouse; the room page on the table screen is the only
control surface. Today it joins and leaves meetings, mutes the microphone and turns the
camera on or off. This design adds the two controls people reach for most in a call:
the room's sound level and where the camera points. It also makes the browser's view
of the room's microphones and speakers visible in one command, so a "Mic not found"
is diagnosable instead of guessed at.

Decisions Ben made on 2026-10-08:

- The speaker varies by room (the Logitech MeetUp's own speaker, a separate USB
  speakerphone, or the TV over HDMI). Volume drives the Pi's default output, with a
  per-room override in the config.
- Camera framing offers named presets plus live nudges and Home.
- The MeetUp reports no pan or tilt position (only speed controls and an absolute
  zoom), so presets are timed moves from a known home, found by driving the camera to
  its end stops. No visible sweep during a meeting; accuracy of a degree or two.
- While the room is idle, the TV shows the live camera picture so presets can be set
  up without a call.
- Architecture: small device modules behind the room page. The upstream audio and
  video services, and the Teams and Webex providers, are not touched.

## 2. What people see

**Sound panel** on the room page: the speaker's name, a large slider 0 to 100 that
sends when released, Quieter and Louder buttons (5 at a time) and a Mute speaker
toggle, clearly separate from the meeting's Mute (the microphone in the call). When
PipeWire has no sink the panel is replaced by one line, "No speaker found".

**Camera panel**: an arrow pad whose buttons move the camera while held, Zoom in and
Zoom out (25 per tap, repeating while held), Home, and three preset buttons named from
the config (default Wide, Table, Whiteboard). A Save switch turns the preset buttons
into save targets for one tap, then switches back. Until a home has been saved, Home
and the presets are replaced by a Set up flow: "Find the stops" (the camera sweeps to
its corner), nudge to the view you want, "Save as Home". Set up stays reachable behind
a small Set up link afterwards. When no camera with controls is found the panel is
replaced by "No controllable camera found".

**In a meeting** both panels sit under the meeting controls; the room's own tile on
the TV shows the framing. **Idle**, opening the Camera panel asks for a preview: the TV
replaces the screensaver with the live camera picture, full screen, captioned "Camera
preview"; closing the panel, leaving the page, or three minutes without renewal brings
the screensaver back. The door sign is unaffected.

**Feel**: motion stops on its own when the page goes quiet; Home or a preset tapped
during a move cancels the move first; the arrow pad is disabled and the panel says
"Moving…" while the camera homes or recalls a preset; volume applies at once. Every
tablet or phone on the room page shows the same levels, from `/api/status` every two
seconds.

## 3. Scope

In: the volume and camera modules, a `devices` service in the agent, the control API
routes and status blocks, the room page panels, the TV preview, the settings file
entries, the config keys, the Devices lines in `croom --check-meet`, the installer's
package list, README and guide updates, tests.

Out: cameras with absolute pan and tilt controls (they would need a position-based
variant of the camera module; the interface leaves room for it), the MeetUp's own
auto-framing (not switchable from Linux), microphone level (the meeting's Mute covers
it), the upstream audio and video services, Teams and Webex.

## 4. Design

### 4.1 The devices service

`src/croom/devices/service.py`: `DevicesService(Service)`, name `devices`, holding a
`RoomVolume` and a `RoomCamera`. `from_config(config, store)`. `start()` discovers both
devices and logs what it found (the PipeWire sinks and the chosen one; the camera node
and its control ranges), then homes the camera if a home is saved. `stop()` stops any
motion and closes the camera. While a device is unavailable, discovery is retried
every 30 s in a background task, so a camera plugged in later is picked up.

The agent registers it after `video` and before `control`; the control service
depends on it. The agent also creates one `SettingsStore` (section 4.5) and hands it
to both services.

### 4.2 Volume (`src/croom/devices/volume.py`, class `RoomVolume`)

- Talks to PipeWire through two commands already on Raspberry Pi OS: `pw-dump` to
  list sinks and the default sink, `wpctl` to read and set a sink's level and mute.
  Commands run through an injectable async runner (default: `asyncio` subprocess,
  2 s timeout). Nothing is imported from the upstream audio service.
- `from_config(config)`: the preference is `config.audio.output_device`; `auto` (the
  default) means the default sink.
- `refresh()`: parses `pw-dump`'s JSON: sinks are nodes whose `media.class` is
  `Audio/Sink`, named by `node.description` (falling back to `node.nick`, then
  `node.name`); the default sink is the node whose `node.name` matches the `default`
  metadata's `default.audio.sink`. With a preference, the chosen sink is the first
  whose name contains the preference text, case-insensitively; with none matching,
  the device is unavailable with the reason "no sink matches 'HDMI'". Then
  `wpctl get-volume <id>` gives `Volume: 0.40` or `Volume: 0.40 [MUTED]`. The result
  is cached for 3 s; a change refreshes at once.
- `set_level(0..100)` → `wpctl set-volume <id> 0.55` (the level divided by 100, capped
  at 1.0); `step(±n)` → the clamped new level; `set_muted(bool)` →
  `wpctl set-mute <id> 1|0`.
- `state()` → `{"available", "device", "level", "muted", "reason"}`.
- Any command failure or timeout makes the device unavailable with the reason, logged
  once until it recovers.

### 4.3 Camera (`src/croom/devices/camera.py`, class `RoomCamera`)

- Opens the camera's video node for control calls only (no streaming), next to
  Chromium's stream, through a small injectable V4L2 layer: `open(path)`,
  `query(fd, cid)` → range or None, `get`, `set`, `close`, and `nodes()` → the
  `/dev/video*` paths. The real layer uses `fcntl.ioctl` with the structures and
  constants from `src/croom/video/v4l2_ioctl.py`, adding `V4L2_CID_PAN_SPEED`
  (0x009a0920) and `V4L2_CID_TILT_SPEED` (0x009a0921).
- Discovery: `config.video.device` names the node; `auto` (the default) takes the
  first node, in numeric order, that has `zoom_absolute` or `pan_speed`. The MeetUp on
  PiMeet-3 is `/dev/video0` with `zoom_absolute` 100..500, `pan_speed` −1..1 and
  `tilt_speed` −1..1.
- `move(pan, tilt)` with each of −1, 0 or 1 sets the speed controls. A watchdog stops
  all motion 1.5 s after the last `move` call; the room page re-sends every 750 ms
  while a button is held. `stop()` sets both speeds to 0.
- `zoom(level)` and `zoom_step(delta)` set `zoom_absolute`, clamped to the queried
  range. The current zoom is read on status refresh, cached for 3 s.
- Position: two floats, `pan_s` and `tilt_s`, seconds of travel at speed 1 from the
  negative end stop, and a `position_known` flag, false until a homing completes.
  Every motion segment (from a speed change to the next) adds its direction times its
  duration, clamped to 0..`travel_s`. Time comes from an injectable monotonic clock
  and sleep, so tests run instantly.
- `find_stops()`: drives pan and tilt to −1 for `travel_s` (config
  `video.ptz_travel_seconds`, default 8) at the same time, stops, and sets the
  position to (0, 0), known. `home()`: `find_stops()` then timed moves of the saved
  home offset on both axes at once, then zoom 100. Both are "long moves": they set
  `busy`, and a new `move`, `stop`, `home`, or recall cancels the running one first.
  A cancelled or failed long move leaves the position unknown rather than wrong.
- Presets: `save(slot)` needs a known position and stores `pan_s`, `tilt_s` and the
  current zoom; `recall(slot)` homes first when the position is unknown (and answers
  "home the camera first" when no home is saved), then moves each axis by the
  difference, both axes at once, and sets the zoom. Names come from
  `config.control.camera_presets`; a slot without a saved preset is shown but refused
  with "nothing saved in this slot".
- Preview: `set_preview(on)` keeps a deadline three minutes ahead; `preview` is true
  until then. Renewal is the same call.
- `state()` → `{"available", "device", "reason", "moving", "busy", "zoom": {"level",
  "min", "max"}, "position_known", "home_saved", "presets": [{"slot", "name",
  "saved"}], "preview"}`.
- The MeetUp's RightSight auto-framing is not touched: it is not reachable through
  V4L2.

### 4.4 Control API and status

All POSTs are JSON-only (415 otherwise) and answer with the block they changed.

- `GET /api/audio/volume` → the volume state. `POST /api/audio/volume` takes one of
  `{"level": 55}`, `{"step": 5}` (or `-5`), `{"muted": true}`; 400 when none is given;
  409 with the reason when the speaker is unavailable.
- `POST /api/camera/move` `{"pan": -1|0|1, "tilt": -1|0|1}` (400 for other values).
  `POST /api/camera/zoom` `{"level": 250}` or `{"step": 25}`. `POST /api/camera/home`.
  `POST /api/camera/presets/{slot}` (1..3) `{"action": "recall"|"save"}`.
  `POST /api/camera/setup` `{"action": "find_stops"|"save_home"}`.
  `POST /api/camera/preview` `{"on": true|false}`, refused with 409 while a meeting is
  joining, in the lobby, connected or leaving. Every camera POST answers 409 with the
  reason when the camera is unavailable or the action needs a known position or a
  saved home.
- `/api/status` gains `"audio"` and `"camera"`, the two `state()` dicts. The TV page
  uses `camera.preview`; the sign ignores both.

### 4.5 Settings and config

- `src/croom/control/settings.py`: `SettingsStore(path)` with `load()` (missing or
  corrupt file → empty), `get(key, default)` and `save(key, value)` (atomic temp file
  and rename, mode 600). The control service keeps its screensaver choice there (the
  file and format are unchanged, key `screensaver`); the camera keeps
  `camera: {"home": {"pan_s", "tilt_s"}, "presets": {"1": {"pan_s", "tilt_s",
  "zoom"}}}`.
- Config: `audio.output_device` (exists, default `auto`) is the speaker match text;
  `video.device` (exists, default `auto`) names the camera node;
  `video.ptz_travel_seconds` (new, default 8); `control.camera_presets` (new, default
  `["Wide", "Table", "Whiteboard"]`, exactly three names). Existing room configs keep
  working unchanged; `to_dict` round-trips the new keys.

### 4.6 Room page

- `index.html`: a Sound section and a Camera section after the link form, before the
  screensaver picker; `app.js` renders both from `/api/status` with the same keyed
  rebuild rule the other controls use, so a held button is never replaced mid-press.
- Hold-to-move: pointer down sends `move` with the direction and starts a 750 ms
  repeat; pointer up, pointer cancel or leaving the button sends `move` with zeros and
  stops the repeat. Zoom buttons repeat `step` every 400 ms while held.
- The slider sends `level` on change (release), the buttons send `step`; the panel
  shows the level and the device name.
- Preview: opening the Camera panel while idle sends `preview on` and renews it every
  30 s; closing it, or the page unloading, sends `preview off`. The panel is a
  collapsible section; it opens closed.
- `style.css`: the arrow pad is a 3 by 3 grid of 64 px buttons; everything wraps at
  phone width with no horizontal scroll (the existing phone test keeps guarding it).

### 4.7 TV page

`tv.js` reads `camera.preview` from the status it already polls. While true and no
meeting is in progress, the page shows a full-screen `<video>` fed by
`getUserMedia({video: true})` (the kiosk context already grants camera access), fit to
the screen on black, with the caption; when it turns false the tracks are stopped and
the screensaver returns. A failing `getUserMedia` shows "Camera preview unavailable"
for as long as the preview is requested.

### 4.8 Diagnostics

`croom --check-meet` prints a Devices section after opening the page: the
microphones, speakers and cameras the browser sees (labels from `enumerateDevices`
after a short `getUserMedia`). The devices service's start-up log names the PipeWire
sinks, the chosen one, and the camera node with its ranges. Together they say whether
PipeWire or the browser lost a device.

### 4.9 Installer and docs

`libpulse0` joins the installer's package list (PipeWire's Pulse bridge and
`v4l-utils` are already there). README: the two panels in the pieces table, the config
keys, a decision line on timed presets, the implementation-notes row. Room setup
guide: a "Set up the camera and sound" step (find the stops, save Home, save the
presets, pick the speaker) and the Devices lines in troubleshooting. Meet guide: the
check's Devices lines.

## 5. Tests

- `tests/unit/devices/test_volume.py`: default sink, name match, no match, no sinks,
  level and mute parsing, clamping, step, command failure and timeout → unavailable
  with reason, cache and refresh after a change. All against a fake runner with
  recorded `pw-dump` output in the Pi's shape.
- `tests/unit/devices/test_camera.py`: discovery (auto picks the first node with
  controls, explicit path, none), move and the watchdog stop, zoom clamping, the
  position estimate from timed segments, `find_stops`, `home` with an offset, preset
  save and recall (including recall from an unknown position homing first, and the
  refusals), cancellation of a long move, preview deadline. Against a fake V4L2 layer
  with a controllable clock.
- `tests/unit/devices/test_service.py`: start discovers and homes when a home is
  saved, does not home otherwise, stop stops motion; rediscovery after a failure.
- `tests/unit/control/test_service.py`: the routes, the 400/409/415 answers, the status
  blocks, the preview refusal during a meeting, with stub devices.
- `tests/unit/control/test_page.py`: the Sound panel changes the level; hold-to-move
  sends start and stop; a preset save and recall; the Set up flow; both panels hidden
  with the notes when unavailable; the phone-width test still passes.
- `tests/unit/control/test_tv_page.py`: with Chromium's fake camera device, the
  preview appears when the status says so and the screensaver returns after.
- `tests/unit/control/test_settings.py`: load, save, corrupt file, mode 600.
- `tests/unit/core/test_config.py`: the new keys and their round trip.
- `tests/unit/meeting/test_meet_check.py`: the Devices section.
- Docs tests for README and the guides; the installer test for `libpulse0`.

## 6. Acceptance on PiMeet-3

Volume moves the MeetUp; with `audio.output_device: HDMI` it moves the TV instead.
The camera homes at service start once a home is saved. Set up: find the stops, nudge,
save Home, save Table and Whiteboard. A preset recall during a Meet call lands where
it was saved. Opening the Camera panel while idle shows the live picture on the TV;
closing it brings the screensaver back. `croom --check-meet` lists the MeetUp
microphone and speaker.
