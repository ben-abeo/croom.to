# MeetUp framing: auto framing (RightSight) and a live view on the touch Pi

Research spike, 2026-11-01. This page gives answers and evidence. It is not a design.
Items marked *estimate* are reasoned and not measured. Items marked *test* can only be
settled on the real MeetUp. The checklist at the end lists them.

## Recommendations

**(4) Auto framing.** Recommendation (c): RightSight cannot be turned on for the TV Pi.
On the MeetUp, RightSight is host software. It runs as a process on the Windows or Mac
computer the camera is plugged into, so it does not run on the camera, and enabling it once
from a laptop does not carry over to the Pi. Confidence: high (Logitech says so directly,
sources below). The one cheap check is test T5 below. If the room needs auto framing, the
option that remains is our own "Frame people" button (section 4.5).

**(3) Live view and PTZ.** One frame tap inside the TV's Chromium, for calls and for idle
alike. Chromium shares one open camera between every `getUserMedia` in the browser, so an
injected script takes a second track from the camera that Meet or Zoom already holds, or
from the TV's own idle preview. The script draws small JPEGs (about 640x360 at 10 to 15 fps)
and passes them to Python through a Playwright binding. The aiohttp service sends them to the
room page over a WebSocket. PTZ stays as it is today: control ioctls on a second file
descriptor, which uvcvideo allows while another process streams. Drag-to-pan drives the
speed controls like a joystick (the MeetUp has no absolute pan or tilt), and pinch drives
`zoom_absolute`.

## 1. What the repo has today

- `src/croom/devices/camera.py` (`RoomCamera`) opens the camera node for **control only**,
  "next to Chromium's stream". On PiMeet-3 the MeetUp is `/dev/video0` with
  `zoom_absolute` 100..500, `pan_speed` -1..1 and `tilt_speed` -1..1. It has no
  `pan_absolute` or `tilt_absolute`. Presets are therefore timed estimates: seconds of
  travel from the end stops, found by homing ("find the stops"). Pan and tilt speeds are
  written together with `VIDIOC_S_EXT_CTRLS` (`src/croom/devices/v4l2.py`).
- Spec `docs/superpowers/specs/2026-10-08-sound-and-camera-controls-design.md` says in
  section 4.3: "The MeetUp's RightSight auto-framing is not touched: it is not reachable
  through V4L2." Its acceptance list includes "A preset recall during a Meet call lands
  where it was saved", which relies on control writes working while Chromium streams.
- The "preview" (`POST /api/camera/preview`, `RoomCamera.set_preview`) is a flag with a
  three-minute deadline. While the flag is on and no meeting runs, the **TV** page
  (`src/croom/control/static/tv.js`) shows a full-screen `getUserMedia` video and asks for
  3840x2160 at 30 fps as ideals (commit 497cc77). The preview is refused during a meeting.
  In a meeting, the only "view" is Meet's own self-view tile on the TV. The touch Pi shows
  no video at all.
- Commit 2d99537 ("leave the room camera free for Zoom and Meet") shows the cost of a
  second process holding the camera: the video service read frames forever and Zoom
  reported "camera in use". A design that opens the device outside Chromium must never
  bring that back.
- `TvDisplay` (`src/croom/meeting/display.py`) runs one persistent Chromium context and
  one page. The page is lent to Meet or Zoom and handed back to `/tv` when idle. The
  context already gets `add_init_script` (the hidden pointer). `zoom_sdk.py` already uses
  `page.expose_function("crystalMeetEvent", ...)`, so a page-to-Python binding has a
  precedent.
- Laptop mode (`DevicesService.release()` and `reclaim()`) closes the camera and ends the
  preview while the ATEN switch gives the MeetUp to a laptop.
- `croom.ai` has person detection backends (ONNX CPU, Hailo, Coral, OpenVINO) and an
  `AutoFramingEngine`. Since 2d99537 nothing feeds them camera frames.

## 2. Part (4): RightSight auto framing

### 2.1 Evidence

1. Logitech, "How do I set up RightSight on MeetUp?": "RightSight runs as a separate
   process on the computer connected to your MeetUp." It is supported only for a
   "Windows 10 (64-bit) PC ... or a computer with macOS 10.14+". On Windows you can confirm
   it is running by finding "Logitech RightSight Service (x86)" in Task Manager. It is
   toggled from Logitech Sync, or from the on-device settings when the MeetUp is plugged
   into a RoomMate (a CollabOS appliance, which means a host that runs Logitech's software).
   <https://hub.sync.logitech.com/meetup/post/how-do-i-set-up-rightsight-on-meetup-3lzMT7Bndio5Klo>
2. Logitech RightSense datasheet (2018), RightSight column: "Note: RightSight is enabled
   by a software download with initial support for Windows 10."
   <https://futureisnow.logitech.com/content/dam/logitech/vc/en/pdf/rightsense-datasheet.pdf>
3. Logitech enterprise deployment note for MeetUp: RightSight ships as `FWUpdateMeetup.exe`
   with the install-time switches `/rs-dynamic=1`, `/rs-ocs=1` (on call start) and
   `/rs-off=1`. It is removed with `VideoCameraServiceUnInst.exe` ("will uninstall the
   Logitech camera services"). These are Windows services on the host.
   <https://support.logi.com/hc/articles/360023357953>
4. "What products support RightSight" lists only Windows 10 64-bit and macOS 10.14+ for
   MeetUp, Rally and Rally Camera. Linux, ChromeOS and appliances are not listed.
   <https://support.logi.com/hc/en-us/articles/360023204934-What-products-support-RightSight>
5. Logi Tune: "RightSight is currently only available with the Logi Tune software". That
   page is about personal webcams. The Tune supported-devices lists name personal webcams
   (Brio, MX Brio, C9xx) and not the MeetUp. For the MeetUp, the tool is Logi Sync (the old
   "Camera Settings" app has reached end of life).
   <https://support.logi.com/hc/en-gb/articles/20713215679511-What-is-RightSight>,
   <https://hub.sync.logitech.com/tune-admin-guides-01nbloql/post/logi-tune-s-supported-devices-QWxHfwibhMxSiUa>,
   <https://support.logi.com/hc/articles/4412711948311-End-of-Life-FAQ-for-the-Camera-Settings-app>
6. On-device framing in the Logitech family is RightSight 2 and 3 on CollabOS devices
   (Rally Bar, Rally Bar Mini, Rally Bar Huddle, MeetUp 2). There, the Meet, Zoom Rooms or
   Teams admin console chooses "Manufacturer framing". That is a different product
   generation from the 2017 MeetUp.
   <https://hub.sync.logitech.com/collabosguides/post/rightsight-2-for-google-meet-4imQTIIlvVXVzZr>

### 2.2 Extension units (XU): what exists, and what it is good for

No open-source project, kernel table or Chromium table found in this search has an XU
control that switches RightSight on a MeetUp. That fits evidence 1: the host runs the
detector and drives pan, tilt and zoom over the camera's ordinary controls, so the camera
has nothing to switch. The Logitech and standard XUs that do exist are about **motors and
presets**. They could make the room's presets better (see 4.4), but they do not give auto
framing:

| GUID | Who documents it | Controls |
|---|---|---|
| `{212de5ff-3080-2c4e-82d9-f587d00540bd}` "Logitech peripheral" | Chromium `webcam_private/v4l2_webcam.cc` (named "CC3000e motor control"); Linux patch "uvcvideo: Import standard controls from uvcdynctrl" (Dec 2025, `UVC_GUID_LOGITECH_PERIPHERAL`) | Selector 1: pan and tilt relative, 4 bytes, bits 0-15 pan and bits 16-31 tilt in 1/64 degree. Selector 2: pan/tilt reset. Chromium also maps **selector 2 as a "Pan/Tilt Go" menu**: Set Home 0x01, Go Home 0x02/0x03, Set Preset 1-8 0x04-0x0B, Restore Preset 1-8 0x0C-0x13. Selector 12: pan and tilt absolute, 2 x int32. |
| `{82066163-7050-ab49-b8cc-b3855e8d2256}` `UVC_GUID_LOGITECH_MOTOR_CONTROL_V1` | Linux patch above, from libwebcam/uvcdynctrl `logitech.xml` | Pan and tilt relative, reset, focus (older QuickCam Orbit/Sphere). |
| `{74D7E924-49C9-4A45-98A3-8A9F60061E83}` Google "Peripheral Control XU" | Google Meet hardware UVC XU spec | `GOOGXU_FRAME_STRATEGY` (sel 1, 8 bytes: 0 = none, 1 = CAZ speaker, 2 = CAZ room, 0x100 = vendor preferred), `GOOGXU_REFRAME` (sel 2), `GOOGXU_PRESETS` (sel 7: save 0x01, restore 0x02, reset 0x03 + index; index 0 = home). This is the standard way a Meet-certified camera exposes **on-device** framing. A 2017 MeetUp whose framing runs on the host is very unlikely to have it, but `lsusb -v` settles it in one line (T1). |

Sources: <https://chromium.googlesource.com/chromium/src/+/main/extensions/browser/api/webcam_private/v4l2_webcam.cc>,
<https://lkml.iu.edu/hypermail/linux/kernel/2512.1/00459.html>,
<https://developers.google.com/workspace/meet/hardware/uvc-xu-spec>.
The Windows tool PTZControl (xMRi) drives Rally and PTZ Pro presets through "Logitech
internal interfaces" from Logitech's SDK. This confirms that presets live in a Logitech XU
rather than in standard UVC controls: <https://github.com/xMRi/PTZControl>.

### 2.3 The three options

- **(a) Switch RightSight from Linux with an XU: no.** No such control is documented, and
  RightSight's detector is not on the camera. Writing undocumented selectors on a
  conference camera risks putting it into a bad state that needs a power cycle.
- **(b) Enable it once from a laptop and rely on it persisting: no.** The setting
  belongs to the host service (Sync and the RightSight service on Windows or Mac). When
  the MeetUp is switched back to the Pi, nothing runs the detector. Logi Tune does not
  even list the MeetUp. Confidence: high. T5 confirms it in two minutes with Laptop mode.
- **(c) Not possible on the TV Pi: yes**, for RightSight itself.

### 2.4 What we could do instead (not asked, noted for later)

A "Frame people" button that frames once, like RightSight's "on call start" mode:

1. Take one frame from the tap in section 3.
2. Run person detection (`croom.ai` ONNX CPU, a few hundred ms on a Pi 5, *estimate*).
3. Set `zoom_absolute` for the group's bounding box.
4. Pan and tilt toward its centre.

Without absolute pan, step 4 would be timed and rough. If the peripheral XU's absolute or
1/64-degree relative pan works on the MeetUp (T4), it becomes precise. Continuous tracking
is not recommended: moving motors during a call is distracting, and a Pi that already runs
Meet has little CPU to spare.

## 3. Part (3): a live view on the touch Pi with drag-to-pan and pinch-zoom

### 3.1 Facts the choice rests on

**uvcvideo allows one streamer per node.** On the Pi's 6.6 kernel
(`raspberrypi/linux` `rpi-6.6.y`, `drivers/media/usb/uvc/uvc_v4l2.c`),
`uvc_acquire_privileges()` gives one file handle per stream the "active" state.
`S_FMT`, `REQBUFS`, `CREATE_BUFS`, `STREAMON`, `S_INPUT` and `S_PARM` call it, and a
second handle gets `-EBUSY` ("Check if the device already has a privileged handle").
Mainline has since moved to vb2's queue ownership (`vb2_ioctl_reqbufs`,
`vb2_is_busy` → `-EBUSY` in `uvc_ioctl_s_fmt`), with the same effect. So while Chromium
streams, another process on the TV Pi can open the node but **cannot read frames**.

**Controls are not exclusive.** `uvc_ioctl_s_ext_ctrls` / `uvc_ioctl_s_try_ext_ctrls`
take no privilege. Any open handle may set controls, while another handle streams. The
request travels on the USB control endpoint, separate from the video endpoint. So
`pan_speed`, `tilt_speed` and `zoom_absolute` (and an XU through `UVCIOC_CTRL_QUERY`)
can be set during a call. `RoomCamera` is built on this, and its acceptance line (a preset
recall during Meet) depends on it. Ben has used it on PiMeet-3. Source:
<https://github.com/raspberrypi/linux/blob/rpi-6.6.y/drivers/media/usb/uvc/uvc_v4l2.c>.

**One Chromium shares one camera.** `content/browser/renderer_host/media/video_capture_manager.cc`,
`GetOrCreateController`: "Check if another session has already opened this device. If so,
just use that opened device." Every `getUserMedia` in the same browser, in any page or
frame, receives frames from the one open device, at the format the first opener chose.
Constraints of later callers are met by scaling and cropping in the renderer, which does
not reopen the device. A second track therefore does not disturb Meet's or Zoom's
capture. The exception (`was_crop_ever_called`) is for Region Capture of tabs and does not
apply to cameras.
<https://chromium.googlesource.com/chromium/src/+/main/content/browser/renderer_host/media/video_capture_manager.cc>

**The Pi 5 has no video encoder and no JPEG block.** Raspberry Pi's white paper "H.264
encoding performance on Raspberry Pi 5" says the Pi 5 encodes with libx264 on the CPU.
Its only hardware decode is HEVC. The Pi 4's V4L2 M2M JPEG and H.264 codecs are gone. All
MJPEG decode (Chromium does this for the MeetUp's 4K MJPEG) and all encoding is CPU work
on the A76 cores.
<https://pip-assets.raspberrypi.com/categories/685-app-notes-guides-whitepapers/documents/RP-010033-WP-1-H.264%20encoding%20performance%20on%20Raspberry%20Pi%205_series%20computers.pdf>

**Chrome's Local Network Access.** Since Chrome 141/142, a public origin such as
`https://meet.google.com` gets a permission prompt (or a block) when it fetches from the
local network or loopback. Chrome 146 extends this to WebRTC. A script injected into
Meet's page therefore must not open its own socket to `127.0.0.1:8080` or to the touch Pi.
A Playwright binding (CDP `Runtime.addBinding`) avoids the network entirely. Sources:
<https://support.jamf.com/en/articles/12894739-chrome-local-network-access-prompts-with-jamf-trust-ztna>,
<https://support.beyondidentity.com/hc/en-us/articles/34457691192215-Managing-the-Chrome-v142Local-Network-Access-Prompt>.

### 3.2 Ways to get frames, compared

Latency is glass-to-glass on the touch Pi. All figures are *estimates* for a Pi 5 and are
checked by T7.

| Option | In call | Idle | Latency | Extra CPU on the TV Pi | Risks |
|---|---|---|---|---|---|
| **(i-a) Tap in the TV's Chromium**: an injected script calls `getUserMedia` (shared capture) or clones Meet's track, draws to an `OffscreenCanvas` at 640x360, `convertToBlob('image/jpeg', 0.7)`, and sends it through a Playwright binding to Python, then over a WebSocket to the room page | yes | yes (the `/tv` page already opens the camera for the TV preview) | ~100-200 ms | JPEG encode 640x360 at 10-15 fps plus one GPU readback: ~5-10 % of one core | Meet's camera button: see 3.4. A frame per binding call is base64 over CDP, ~0.3-0.5 MB/s, which is fine. |
| (i-b) WebRTC from the TV page to the room page | yes | yes | ~100-150 ms | a **second software VP8/H.264 encode** next to Meet's own: ~10-25 % of a core at 640x360 | signalling, ICE with mDNS host candidates, LNA on WebRTC from Chrome 146, and more code for little gain over (i-a) |
| (i-c) Screenshot of the self-view tile (Playwright `locator.screenshot` or CDP `Page.startScreencast`) | yes | n/a | 200-500 ms, irregular | a full-page composite and PNG/JPEG per frame | Meet's layout and mirroring, the tile is small, a hidden self-view gives nothing. Only good as a fallback. |
| (ii) `v4l2loopback` / GStreamer tee: GStreamer owns the MeetUp, decodes MJPEG once, writes raw frames to `/dev/videoN` (loopback) for Chromium, and JPEGs a small branch for the preview | yes | yes | ~80-150 ms preview; adds ~1 frame to the call | MJPEG decode moves from Chromium to GStreamer (1080p30 ~20-25 % of a core, 4K30 about a whole core) plus copies; preview encode as in (i-a) | an out-of-tree DKMS module on the Pi kernel; Chromium needs `exclusive_caps=1` and sees a "Dummy video device"; Meet and Zoom can no longer pick a resolution; Laptop mode and hot-unplug need a placeholder source; the pipeline must start before Chromium enumerates. A second owner of the camera is the 2d99537 bug in another form. |
| (ii-b) PipeWire camera (`chrome://flags` "PipeWire camera support", xdg camera portal) with a second `pipewiresrc` client | maybe | maybe | similar | similar | the flag is experimental (Chromium 127/128+, "broken" in early builds per its author), it needs xdg-desktop-portal in a kiosk session, and format negotiation is unknown. Not ready for a room. |
| (iii) Open the node directly in the aiohttp service when idle, MJPEG passthrough: pick a 640x360 or 1280x720 MJPEG mode and forward the camera's own JPEGs untouched | **no** (EBUSY) | yes | ~60-120 ms | ~0: no decode, no encode | Must close before Meet or Zoom opens the camera. A join while the view is open is a race ("Could not start video source", 2d99537 again). It also conflicts with the TV page's own idle preview (`getUserMedia` there gets "NotReadableError"). Two code paths: idle and in call. |

### 3.3 Recommendation

**Use (i-a) for both in-call and idle, with one frame path.**

- **In a call** the camera is already open in the TV's Chromium. Add the tap with
  `page.evaluate`, or with `context.add_init_script` plus a start/stop message. It calls
  `navigator.mediaDevices.getUserMedia({video: {deviceId: <the MeetUp>}})`, which shares
  Meet's or Zoom's capture, or it clones the track Meet obtained (wrapping `getUserMedia`
  in an init script). It produces JPEGs with a `requestVideoFrameCallback` or a timer. The
  same code works for the Zoom SDK page (served from 127.0.0.1) and for Meet
  (`meet.google.com`), because it never uses the network: `context.expose_binding`
  (one binding for every page of the context, like `zoom_sdk.py`'s `expose_function`)
  hands each JPEG to Python.
- **Idle**: the `/tv` page already opens the camera for the TV preview when the room page
  shows the camera view. The same tap runs on that stream. The TV preview and the touch
  view then come from one capture, and nothing outside Chromium ever opens the device.
  For the touch view, consider asking the idle preview for 1920x1080 instead of 4K:
  Chromium decodes 4K MJPEG on the CPU (no JPEG hardware on the Pi 5), about one core at
  30 fps (*estimate*, T7). The TV preview can keep 4K if T7 shows the Pi copes.
- **Transport to the touch Pi**: the aiohttp service on :8080 keeps the latest JPEG and
  pushes it to the room page over a WebSocket. It drops frames for a slow client and never
  queues them. The room page draws each one into an `<img>` or `<canvas>`. Multipart
  MJPEG (`multipart/x-mixed-replace`) in an `<img>` also works and is simpler still, but a
  WebSocket closes cleanly when the view closes. 640x360 at JPEG q0.7 is ~25-35 KB per
  frame, so about 2.5-3.5 Mbit/s at 10 fps on the LAN (*estimate*). Start the tap when the
  camera view opens and stop it when it closes, with the deadline the preview flag already
  has (renewed every 30 s, three minutes). It must never run unattended.
- **PTZ**: no change to how commands reach the camera. `RoomCamera` writes controls on
  its own descriptor during calls, which uvcvideo allows.
  - **Drag-to-pan**: the MeetUp has only `pan_speed` and `tilt_speed` (-1/0/1), so
    position cannot follow a finger 1:1. Map a drag to a joystick: the direction of the
    drag from its start point sets the speeds, release stops them. Re-send while the
    finger is held, as the arrow pad does now (750 ms repeat, 1.5 s watchdog). With
    ~150 ms of view latency and the motor's own lag, a "dead zone" of a few dozen pixels
    keeps it from jittering. If T4 shows the peripheral XU's relative pan in 1/64° or
    absolute pan works on the MeetUp, a drag can become "move by this angle": a drag
    distance converted to degrees through the field of view (±25° pan, ±15° tilt
    mechanical, about 120° FOV) and the zoom level.
  - **Pinch-zoom**: `zoom_absolute` 100..500 (the MeetUp's zoom is digital, a crop of the
    4K sensor). Map the pinch scale to a target level and send it at most every ~100 ms.
    For instant feedback, scale the image on the touch Pi locally during the pinch and
    snap back when the camera's new frames arrive.
  - Show the picture **unmirrored**, as the far end sees it. Meet's self-view on the TV is
    mirrored, and drag directions only feel right on the true image.
- **Trade-offs accepted**: the live view exists only while Chromium has the camera open.
  That is always true in a call with the camera on, and always true idle while the view is
  open. There is no view in Laptop mode, which is already the rule for the camera. The JPEG
  path costs a few percent of a core and 2-4 Mbit/s, against a second video encoder for
  WebRTC or a kernel module for loopback.

### 3.4 Points that need care

- **Meet's camera button.** When the room turns its camera off in Meet, Meet stops its
  track, the MeetUp stops streaming, and the LED goes off. A tap holding its own
  `getUserMedia` track would keep the camera streaming. The LED would stay on, and the
  next press of "camera on" might find odd state. Rule: in a call, the tap only runs while
  `camera_on` is true. When it is false, the room page says "Your camera is off in the
  call", and PTZ still works blind. Cloning Meet's own track instead of opening a second
  session ends the tap with Meet's track automatically (T8).
- **Resolution.** In a call the shared capture runs at what Meet or Zoom asked for
  (typically 720p, sometimes 1080p). The tap only scales down. Idle runs at what the TV
  preview asks.
- **Meet's DOM and CSP do not matter.** The tap uses `getUserMedia`, `OffscreenCanvas`
  and a CDP binding, and none of these depend on Meet's markup or Content Security Policy.
  Meet could notice an extra track, which is unlikely to matter. The tap must not touch
  Meet's own tracks other than cloning them.
- **CPU headroom.** Meet on a Pi 5 is already the heaviest load, with software VP8/VP9 or
  AV1 encode and MJPEG decode. Keep the tap at 640x360 and ≤15 fps, and measure (T7)
  before raising either.

## 4. Smaller findings worth keeping

- **Hardware presets.** The MeetUp's remote stores three camera presets in the camera
  (datasheet: "RF remote control with three camera presets"). Chromium drives Logitech
  presets through the peripheral XU (`212de5ff-...`, selector 2, the "Pan/Tilt Go" menu:
  Set Home, Go Home, Set/Restore Preset 1-8). If the MeetUp has this XU (T1, T3), the
  room's timed presets and the "find the stops" homing could be replaced by the camera's
  own exact presets. That would be a bigger improvement to "Camera framing" than anything
  in this spike.
- The kernel patch "Import standard controls from uvcdynctrl" (Dec 2025) maps the
  Logitech peripheral XU's relative pan and tilt to V4L2 controls in new kernels. The Pi's
  6.6 or 6.12 kernel does not have it. On those, `uvcdynctrl` with `logitech.xml` (Debian
  package `uvcdynctrl`) or a raw `UVCIOC_CTRL_QUERY` ioctl reaches the same selectors.

## 5. Open questions only the real MeetUp can answer

Run these on the TV Pi. `/dev/video0` is the MeetUp on PiMeet-3; check with T2 first.

- **T1, the XUs the MeetUp exposes:**
  `lsusb -d 046d:` (expect `046d:0866`), then
  `sudo lsusb -v -d 046d:0866 | grep -E "bUnitID|guidExtensionCode|bNumControls|bmControls"`.
  Look for `{212de5ff-3080-2c4e-82d9-f587d00540bd}`, `{82066163-7050-ab49-b8cc-b3855e8d2256}`
  and `{74d7e924-49c9-4a45-98a3-8a9f60061e83}`, and note each one's `bUnitID`.
- **T2, controls and formats:** `v4l2-ctl --list-devices`;
  `v4l2-ctl -d /dev/video0 --list-ctrls-menus`;
  `v4l2-ctl -d /dev/video0 --list-formats-ext`. Which MJPEG and YUYV sizes and rates exist?
  Is there a small MJPEG mode (640x360) for the option (iii) fallback?
- **T3, presets through the peripheral XU** (only if T1 lists it):
  `sudo apt install uvcdynctrl`;
  `uvcdynctrl -d /dev/video0 -i /usr/share/uvcdynctrl/data/046d/logitech.xml`;
  `uvcdynctrl -d /dev/video0 -c -v`. Then try get and set on the unit's selectors 2 and 12
  (`uvcdynctrl --help` names the raw get/set options in the Debian build). Save a preset
  with the remote, then send "Restore Preset 1". Does the camera go there? Does
  "Go Home" work?
- **T4, precise pan:** does selector 1 (relative, 1/64°) or selector 12 (absolute pan and
  tilt, int32) move the MeetUp by a known amount? Can selector 12 be read back as a
  position?
- **T5, RightSight is host-only (2 minutes, with Laptop mode):** on a laptop with Logi
  Sync, enable RightSight (Dynamic) and walk around until it reframes. Switch the ATEN back
  to the Pi, start a Meet call, walk to the edge of the frame. If the camera never moves on
  its own, it is host-only (expected). Then run T1 again to make sure nothing was left on.
- **T6, exclusivity during a call:**
  `fuser -v /dev/video*` (expect Chromium);
  `v4l2-ctl -d /dev/video0 --stream-mmap --stream-count=1` (expect "Device or resource busy");
  `v4l2-ctl -d /dev/video0 --set-ctrl=zoom_absolute=300` then `=100` (expect the zoom to
  change in the call).
- **T7, the tap's cost and latency:** during a Meet call, in the TV page's DevTools
  (remote debugging) or with a Playwright `page.evaluate`, run
  `const s = await navigator.mediaDevices.getUserMedia({video:true}); s.getVideoTracks()[0].getSettings()`.
  Check that Meet's picture is undisturbed and note the shared resolution. Then run the
  canvas to JPEG loop at 640x360 and 10 fps and watch `top -H -p $(pgrep -d, -f chrom)`
  or `pidstat -t 1` with and without it. Idle, compare the TV preview's CPU at 4K and at
  1080p. For glass-to-glass latency, film a stopwatch shown on the touch Pi next to the
  real one.
- **T8, Meet's camera button:** with the tap holding a second session, turn the camera off
  in Meet. Does the MeetUp's LED stay on? Repeat with a clone of Meet's own track: does
  the clone end when Meet turns the camera off?
- **T9, motor response for drag:** the time from `pan_speed=1` to visible motion, and the
  coast after `pan_speed=0`:
  `v4l2-ctl -d /dev/video0 --set-ctrl=pan_speed=1; sleep 0.3; v4l2-ctl -d /dev/video0 --set-ctrl=pan_speed=0`.
  This sets the drag dead zone and repeat rate.
