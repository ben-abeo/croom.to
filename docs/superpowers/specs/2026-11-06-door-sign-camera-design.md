# The door sign's camera in UniFi Protect

**Date:** 2026-11-06 · **Status:** draft for Ben's review · **Touches:** the door sign app (`android/door-sign`), room settings
(`src/croom/settings`), the check-in (`settings/checkin.py`, `Agent.java`, `CheckIn.java`), the door sign manual.

## 1. What this is for

The door signs face the office's main areas, and those areas have gaps in UniFi Protect's coverage. Every door sign panel has a
front camera. Ben wants each panel's camera to appear in Protect as a camera and record with the office's existing system, so the gaps
are covered without mounting more cameras. Picture quality is not a concern: "I just want it seen through unifi protect and able to be
recorded with our system."

Success: each door sign shows up in Protect, is adopted once like any third-party camera, and records from then on with the rest, while
the sign goes on exactly as it is.

## 2. Decisions (Ben, 2026-11-06)

- **Every door sign**, not only Conference Room B's.
- **Always on.** No switch to turn a panel's camera off.
- **The camera's password is set once per room in Settings** on the touch Pi, not on each panel.
- **Android's green camera dot is fine.** Android 12 and later draws a small green dot in a top corner whenever the camera is in use,
  even over a kiosk. With the camera always on, every door sign shows it permanently. Ben: "yes the dot is fine". Nothing is done to
  hide it, and it also tells people in the hall that the camera is on.
- The rest of section 3 was played back to Ben and accepted ("looks good"): video only, a modest picture, Protect talking straight to
  the panel, the sign unchanged, delivery through the normal door sign update.

## 3. Goals, non-goals, anti-goals

**Goals**

- Each door sign panel is an ONVIF camera that UniFi Protect 7.3 (the office runs 7.3.70 on the UDM Pro `crystal_gateway`,
  192.168.0.1) discovers and adopts as a third-party camera, and records.
- The stream needs a login. Nobody on the network watches it without the password set in the room's Settings.
- It reaches every panel through the existing update (Settings › Software › **Update**), and a panel set up from now on has it with no
  extra setup step.

**Non-goals**

- **Sound.** Video only (my call, agreed in playback): recording hallway conversations raises consent questions that video does not.
  *Superseded 2026-11-10:* sound behind a per-room switch, off by default (`2026-11-10-door-sign-camera-sound-design.md`).
- **Motion events.** Protect only gets motion from a third-party camera that sends ONVIF events. The panel doesn't, so its camera
  is recorded with Protect's "Always" recording mode (my call; see section 9).
- **PTZ, a second stream, snapshots, smart detections.** Protect does not offer these for third-party cameras, or they aren't needed.
- **An on/off switch,** per Ben.
- **Panels that aren't the device owner** (set up the old way). They get no camera and say why (section 6).

**Anti-goals: it fails even if Protect records**

- The sign gets slow, stutters, freezes, or the page or check-in stops while the camera runs.
- The room can no longer reboot, update or re-network the panel.
- The stream can be watched without the password, or the password can be read back from Settings or the panel.
- Something changes on the sign's screen, apart from Android's own green camera dot (section 2).

## 4. What is known (checked 2026-11-06)

**The panel** (Conference Room B's, 192.168.0.194; the other rooms' panels are assumed to be the same model, *unverified*):

- Model WF2489T, Rockchip rk3576, Android 14, wired Ethernet (`eth0`, MAC `f8:7a:39:7c:f6:25`), not Wi-Fi.
- One front-facing camera, 2592 × 1944 sensor; Camera2 outputs include 1280 × 720 and 1920 × 1080.
- Hardware H.264 encoder `c2.rk.avc.encoder` (also HEVC, which Protect handles badly from third-party cameras: use H.264).
- The app is the panel's device owner, so it can grant itself the camera permission without asking anyone
  (`DevicePolicyManager.setPermissionGrantState`).
- Today the app only calls out; nothing listens on the panel (the `Agent` class says so). This design is the first time the panel
  listens, which is why the login matters.

**Protect's third-party cameras** (Ubiquiti's help article and community reports, 2024–2025, not tested against 7.3.70 yet):

- Turned on with **Settings › System › Discover Third-Party Cameras**. Cameras on the same subnet are found by ONVIF WS-Discovery and
  adopted from the UniFi Devices page with a username and password. **Advanced Adoption** adds a camera by IP address and port instead.
- Protect treats each ONVIF server as one camera, with its own IP address and MAC; it seems to identify cameras by MAC. One server per
  panel suits that, and the panel's wired MAC is fixed.
- H.264 records reliably; H.265 doesn't. No extra licence is needed.
- Reports disagree on whether Protect sends credentials on the RTSP stream itself. It does send them on every ONVIF (SOAP) request.
- go2rtc's ONVIF server (PR AlexxIT/go2rtc#1520) is working prior art for a minimal ONVIF server that Protect adopts:
  `VideoEncoderConfiguration.RateControl` must be present (its contents aren't checked), and removing `VideoSourceConfiguration` breaks
  things.

## 5. How it works

```
 Touch Pi Settings ──(room PIN)──▶ TV Pi config.yaml: door sign camera password
                                        │
                                        ▼ check-in answer, every 5 s (joined door signs only)
 Door sign panel ── camera service ── H.264 (hardware) ── RTSP server :8554 ◀── Protect records
                 └─ ONVIF server :8000 + WS-Discovery (UDP 3702) ◀───────────── Protect finds and adopts
```

- **The setting.** A new room setting on the **Devices** page, "Door sign camera password", of the existing `secret` kind (shown as
  set or not set, never read back). The username is fixed, `camera` (my call), and the setting's help says so. Applying it at once
  (live) is preferred so changing it needs no restart; the builder decides if that is easy.
- **Getting it to the panels.** The check-in answer to a door sign that has joined carries the camera login. A pending, refused or
  unknown device never gets it. This is the same channel and the same trust the Wi-Fi jobs already use for Wi-Fi passwords. The panel
  keeps the login in its own storage, so it keeps working when the TV Pi is down, and replaces it when the room sends another.
- **The camera service** runs on the panel as an Android foreground service of the camera type, started with the app and at boot,
  independent of the sign's page, the setup screen and **Exit to Android**. It opens the camera at 1280 × 720, about 15 frames a second
  (my call), encodes H.264 in hardware at about 1.5 Mbit/s (my call), and serves it. If the camera or encoder fails, it tries again,
  and never takes the sign down with it.
- **No login, no camera.** Until the panel has a password from the room, nothing listens: no stream, no ONVIF, no discovery.
- **ONVIF**, the minimum Protect needs to find and adopt one camera with one stream: WS-Discovery on UDP 3702; the device and media
  services on TCP 8000 (my call: Android apps can't use ports below 1024), answering at least `GetSystemDateAndTime`,
  `GetDeviceInformation`, `GetCapabilities`/`GetServices`, `GetProfiles`, `GetVideoSources`, `GetStreamUri`, with the fields go2rtc
  found Protect needs. Every request except `GetSystemDateAndTime` and discovery needs the WS-UsernameToken login.
  `GetDeviceInformation` names the room ("Conference Room B door sign") so Protect can show it that way. If Protect ignores that name,
  renaming the camera once in Protect is acceptable. The room name reaches the panel in the check-in answer, like the login.
- **RTSP on TCP 8554** (my call), H.264 over RTP, needing the same login (Digest). If testing shows Protect 7.3.70 won't send RTSP
  credentials, the stream instead lives at an unguessable path that is handed out only by an authenticated `GetStreamUri`, and changes
  when the password does. One of the two must hold: the stream is never open.
- **Delivery.** `versionCode` 3, released as `door-sign-3` in the usual way, and installed with **Update** on each room's Software page.

## 6. What people see

- **On the sign:** nothing new except Android's small green camera dot in a top corner, always on. The manual says so, so nobody
  mistakes it for a fault.
- **On the door sign's card (Settings › Software):** if the camera can't run, the card says why, using the existing `cannot` list the
  panel already reports. For example: "Camera off: set the door sign camera password in Settings › Devices", "Camera off: this panel
  is not the device owner", or "Camera off: the camera did not start: <reason>". When the camera runs, the card says nothing extra (my
  call).
- **On the panel's setup screen** (behind its PIN): one line under Room settings with the camera's state and its address, for example
  "Camera on: 192.168.0.194, ONVIF port 8000, user camera". It is for whoever adopts it in Protect, and never shows the password.
- **Builder note:** today the TV Pi's `_cannot` (`settings/checkin.py`) keeps only reasons keyed by a job, and drops anything else.
  It and the card need a `camera` entry for the "Camera off" words to reach Settings.
- **The password can't be cleared** once set (a blank `secret` field means "keep it"). That fits "always on". Changing it is how a
  room locks out an old login, and Protect then needs the new one.

## 7. Adopting it in Protect (goes in the door sign manual)

1. In Settings on each room's touch screen, set **Door sign camera password** (each room has its own setting; the same password
   everywhere is simplest). Keep it in the office password manager.
2. **Update** the door sign on Settings › Software.
3. In Protect: **Settings › System › Discover Third-Party Cameras** on.
4. On the UniFi Devices page, adopt the door sign with username `camera` and the password. If it isn't listed, use **Advanced
   Adoption** with the panel's IP address and port 8000.
5. Set its recording to **Always**.

## 8. Constraints

- Android 14 (minSdk stays 26, targetSdk 34). The camera feature needs the device owner, as the room's jobs do.
- The repo's door sign app has no runtime dependencies today. Writing the small RTSP and ONVIF servers in the app is preferred over
  adding a library. If the builder finds a library clearly safer for RTSP, that is their call, but it must not bloat the APK past what
  the TV Pi's APK size check allows.
- The camera must not hurt the sign: keep the encoder in hardware, and keep the camera work off the main thread and the WebView.
- The panel's existing behaviour stays: check-in, jobs, kiosk, PIN, update watch, light bar.

## 9. Calls I made (Ben to check)

- Username fixed to `camera`; only the password is a setting.
- Ports: ONVIF 8000, RTSP 8554.
- Picture: 1280 × 720, ~15 fps, ~1.5 Mbit/s, H.264.
- Recording mode "Always", because there are no motion events. At ~1.5 Mbit/s that's roughly 16 GB per panel per day on the UDM
  Pro's disk, and Protect overwrites the oldest footage when full.
- Protect connects to the panel directly, not through the room's TV Pi, so recording carries on while the room software restarts or
  updates.
- No sound.
- The setting lives on the **Devices** page.

## 10. Left to the builder

- How the RTSP server, RTP packetisation and ONVIF responses are written, and the service's structure.
- Whether the setting can be live.
- Verifying against the real Protect 7.3.70 (Ben's console): discovery, adoption, recording, whether RTSP auth is sent (which decides
  between the two protections in section 5), and whether the room name shows.
- Confirming the other rooms' panels are the same model before release; if one isn't, it must still work or say why on its card.

## 11. Testing

- JVM unit tests beside the existing ones (`app/src/test`): ONVIF request parsing and responses, WS-UsernameToken and Digest checks
  (right, wrong and missing password), "no login, no listening", the RTSP request handling, and the check-in answer carrying the login
  only to a joined door sign.
- Python tests: the new setting (validation, secret never read back) and the check-in answer (login only to an allowed `sign`).
- Docs tests (`tests/unit/docs/test_door_sign_docs.py`) updated for the new manual section.
- By hand on Conference Room B's panel: adopt it in Protect, record for an hour, and confirm the sign stays smooth (no dropped
  frames visible, the page and check-in keep their 5-second rhythm), and that a wrong password gets nothing from either port.
