# Door sign camera in UniFi Protect Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every door sign panel becomes an ONVIF camera that UniFi Protect 7.3 adopts and records, protected by a password set once per room in Settings.

**Architecture:** The TV Pi gets a new secret room setting and hands the camera login to joined door signs in the check-in answer. The
door sign app gets a foreground camera service: Camera2 → hardware H.264 (MediaCodec) → a small RTSP server (TCP 8554) plus a minimal
ONVIF SOAP server (TCP 8000) and WS-Discovery (UDP 3702). Protect talks to the panel directly. All protocol logic lives in plain
Java classes with JVM unit tests; only the camera/encoder/socket glue is Android-only.

**Tech Stack:** Python 3 (aiohttp, pytest) on the TV Pi; Java 17, Android SDK 34 (Camera2, MediaCodec, foreground service), JUnit 4 on the panel. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-06-door-sign-camera-design.md`

## Global Constraints

- Android: minSdk 26, targetSdk/compileSdk 34; the camera runs only when the app is the device owner.
- No new runtime dependencies in `android/door-sign` (tests may keep using JUnit and org.json).
- Username fixed to `camera`. Password setting: `control.door_sign_camera_password`, page `devices`, kind `secret`, label "Door sign camera password".
- Ports: ONVIF HTTP **8000**, RTSP **8554**, WS-Discovery UDP **3702** (multicast 239.255.255.250).
- Picture: H.264, **1280 × 720**, **15 fps**, **1.5 Mbit/s**, keyframe every **2 s**, no audio.
- No login, no camera: nothing listens on 8000, 8554 or 3702 until the panel holds a login from the room.
- The stream is never open: its RTSP path carries a token derived from the password, handed out only by an authenticated `GetStreamUri`.
- The sign, check-in, jobs, kiosk, PIN, update watch and light bar behave exactly as before. Only Android's green camera dot is new on screen.
- `versionCode 3`, `versionName "3.0"`.
- Card words (exact): `Camera off: set the door sign camera password in Settings › Devices`, `Camera off: this panel is not the device owner`, `Camera off: the camera did not start: <reason>`.
- Setup screen line (exact shape): `Camera on: <ip>, ONVIF port 8000, user camera`.
- Commands: Python tests `.venv/bin/python -m pytest …`; Android tests `cd android/door-sign && JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 ./gradlew testDebugUnitTest` (baseline passes).
- Work on branch `door-sign-camera` (create it from `main` before Task 1).

## Review Focus

1. **A slow or vanished Protect connection** must never block the encoder or the sign. A client whose queue fills is dropped (Task 4 test `aSlowClientIsDroppedNotWaitedFor`).
2. **The password changes while Protect is connected.** The old stream path stops working at once and old sessions are closed (Task 4 test `aNewPasswordRetiresTheOldStreamPath`, Task 5 restarts the servers).
3. **A password with XML-special characters** (`&`, `<`, `"`) must authenticate and must not break any SOAP reply (Task 3 test `passwordsWithXmlCharactersWork`).
4. **Clock skew between Protect and the panel.** A WS-UsernameToken `Created` up to 15 minutes off is accepted; further off is refused (Task 3 test `createdFarFromNowIsRefused`).
5. **Mixed versions.** A new TV Pi with an old panel, or an old TV Pi with a new panel, keeps working: the extra key is ignored, and a missing key keeps the stored login (Task 1 test `test_old_panels_ignore_the_camera_block`, Task 2 test `anAnswerWithoutCameraKeepsTheStoredLogin`).

---

### Task 1: The room's setting and the check-in answer (Python)

**Files:**
- Modify: `src/croom/core/config.py` (`ControlConfig` ~line 269; `to_dict` "control" block ~line 431)
- Modify: `src/croom/settings/schema.py` (Devices block of `SCHEMA`, after `control.camera_auto_framing`)
- Modify: `src/croom/settings/checkin.py` (`_cannot` ~line 167; add `SIGN_CAMERA_USER`, `CANNOT_EXTRA`)
- Modify: `src/croom/control/service.py` (`__init__` ~line 312 and ~374, `from_config` ~line 408, `_handle_checkin` ~line 2069)
- Test: `tests/unit/settings/test_schema.py`, `tests/unit/settings/test_checkin.py`, `tests/unit/control/test_devices_api.py`

**Interfaces:**
- Produces: check-in answer key `"camera": {"user": "camera", "password": str, "name": f"{room_name} door sign"}`, present only when `state == "allowed"`, the device's `kind == "sign"`, and the password is set. The registry keeps a `cannot["camera"]` string from a door sign.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/settings/test_schema.py
def test_door_sign_camera_password_is_a_live_secret_on_devices():
    s = BY_PATH["control.door_sign_camera_password"]
    assert (s.kind, s.page, s.live, s.label) == ("secret", "devices", True, "Door sign camera password")
    assert validate("control.door_sign_camera_password", "p&ss<w>rd1") == "p&ss<w>rd1"
    for bad in ("short7!", "has space here", "x" * 65):
        with pytest.raises(SettingError):
            validate("control.door_sign_camera_password", bad)

# tests/unit/settings/test_checkin.py
def test_a_door_sign_may_say_why_its_camera_is_off(registry):
    sign_joined(registry, cannot={"camera": "Camera off: the camera did not start: busy", "selfie": "x"})
    assert registry.get(SIGN)["cannot"] == {"camera": "Camera off: the camera did not start: busy"}

# tests/unit/control/test_devices_api.py
async def test_a_joined_door_sign_gets_the_camera_login(...):      # service built with sign_camera_password="secret-pw1", room_name="Conference Room B"
    # pending sign: no "camera" key; after Allow: answer["camera"] == {"user": "camera", "password": "secret-pw1", "name": "Conference Room B door sign"}
async def test_a_touch_pi_never_gets_the_camera_login(...):        # allowed kind "touch": no "camera" key
async def test_no_password_no_camera_block(...):                   # allowed sign, password "": no "camera" key
async def test_old_panels_ignore_the_camera_block(...):            # an allowed sign whose body has no "cannot" still gets 200 and its job as before
async def test_the_password_applies_at_once(...):                  # editor saves a new password → the next check-in carries it, no restart
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/unit/settings/test_schema.py tests/unit/settings/test_checkin.py tests/unit/control/test_devices_api.py -q`
Expected: the new tests FAIL (KeyError on the setting path, missing `camera` key).

- [ ] **Step 3: Implement**

- `ControlConfig.door_sign_camera_password: str = ""`, written by `to_dict` under `"control"`.
- `Setting("control.door_sign_camera_password", "Door sign camera password", "devices", "secret", live=True, max_length=64, pattern=r"[!-~]{8,64}", pattern_words="8 to 64 letters, digits or symbols, with no spaces", help="Protect adopts each door sign's camera with the user camera and this password")`.
- `checkin.py`: `SIGN_CAMERA_USER = "camera"`, `CANNOT_EXTRA = ("camera",)`; `_cannot` keeps keys in `JOBS` or `CANNOT_EXTRA`.
- `ControlService`: `from_config` passes `"sign_camera_password": config.control.door_sign_camera_password`; `__init__` keeps `self._sign_camera_password`; register `self._editor.on_change(("control.door_sign_camera_password",), self._apply_sign_camera_password)`; `_handle_checkin` adds the `camera` block per the Interfaces rule.

- [ ] **Step 4: Run the tests and the whole Python suite**

Run: `.venv/bin/python -m pytest tests -q -x`
Expected: all pass.

- [ ] **Step 5: Commit**

`git commit -am "feat(settings): a door sign camera password, handed to joined door signs"` (with the session's attribution lines).

---

### Task 2: The panel keeps the login and says why its camera is off

**Files:**
- Create: `android/door-sign/app/src/main/java/to/croom/doorsign/CameraLogin.java`
- Modify: `CheckIn.java` (`Answer` gains `camera`), `Agent.java` (constructor gains the login consumer), `Panel.java` (`cannot()` adds `camera`), `DoorSignApp.java` (wiring)
- Test: `app/src/test/java/to/croom/doorsign/CameraLoginTest.java`, `CheckInTest.java`, `AgentTest.java`

**Interfaces:**
- Produces: `final class CameraLogin { final String user, password, name; static CameraLogin from(Object json) /* null unless user and password are non-blank strings */; void save(Store s); static CameraLogin load(Store s) /* null when none */; boolean equals(Object) }` with store keys `camera_user`, `camera_password`, `camera_name`.
- Produces: `CheckIn.Answer.camera` (`CameraLogin`, null when absent or not allowed).
- Produces: `Agent(Supplier<String> server, Identity identity, Http http, Device device, JobRunner runner, Log log, Consumer<CameraLogin> camera)`. It calls `camera.accept(login)` only when an allowed answer carries a login different from the last one it passed on.
- Produces: `static String Panel.cameraCannot(boolean owner, CameraLogin login, String trouble)`, which returns the card words from Global Constraints, or null when the camera runs. `trouble` is null when it runs.

- [ ] **Step 1: Write the failing tests**

```java
@Test public void anAllowedAnswerCarriesTheLogin()      // parse(...allowed..., "camera": {"user":"camera","password":"p&ss<w>rd1","name":"Conference Room B door sign"}) → fields equal
@Test public void aPendingAnswerNeverCarriesALogin()     // pending + camera block → answer.camera == null
@Test public void anAnswerWithoutCameraKeepsTheStoredLogin() // Agent: login stored, next allowed answer without "camera" → consumer not called, store unchanged
@Test public void aNewLoginIsPassedOnOnce()             // two identical answers → consumer called once; a changed password → called again
@Test public void theLoginSurvivesARestart()            // save to MapStore, load → equal
@Test public void cameraCannotWords()                    // (false, login, null) → "Camera off: this panel is not the device owner"; (true, null, null) → "...password in Settings › Devices"; (true, login, "busy") → "Camera off: the camera did not start: busy"; (true, login, null) → null
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd android/door-sign && JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 ./gradlew testDebugUnitTest`
Expected: compile errors (`CameraLogin` missing).

- [ ] **Step 3: Implement** the Interfaces above. In `DoorSignApp`, the consumer saves the login to the `check_in` store (Task 5 adds the restart call). `Panel`'s constructor gains `Store store` and `Supplier<String> cameraTrouble`, wired to `() -> null` here and to `CameraService::trouble` in Task 5. `Panel.cannot()` adds `"camera"` from `cameraCannot(isOwner(), CameraLogin.load(store), cameraTrouble.get())` when that is non-null.

- [ ] **Step 4: Run the tests**: same command, all pass.

- [ ] **Step 5: Commit**: `feat(door-sign): keep the room's camera login and say why the camera is off`.

---

### Task 3: ONVIF answers and WS-Discovery (pure Java)

**Files:**
- Create: `android/door-sign/app/src/main/java/to/croom/doorsign/Onvif.java`, `Discovery.java`, `MiniHttp.java`
- Test: `OnvifTest.java`, `DiscoveryTest.java`, `MiniHttpTest.java`

**Interfaces:**
- Produces: `final class Onvif`
  - `static final class Info { Info(CameraLogin login, String host, String mac, String model, String firmware, String serial) }`
  - `static final class Reply { final int status; final String xml; }`
  - `static Reply handle(String soapBody, Info info, Instant now)`
  - `static String streamPath(String password)`: `"/" +` the first 32 hex chars of HMAC-SHA256(key = password UTF-8, message = `"door-sign-stream"`).
  - `static boolean authorized(String soapBody, CameraLogin login, Instant now)`
- Produces: `static String Discovery.probeMatch(String probeXml, String endpointUrn, String xaddr, String name, String model)`, which returns null when the message is not a Probe or asks for types other than `NetworkVideoTransmitter`/`Device`.
- Produces: `MiniHttp.Request read(InputStream in)` (method, path, headers, body; at most 64 KiB body, else IOException) and `void write(OutputStream out, int status, String contentType, String body)`.

Rules the tests pin:
- Actions are matched by the local name of the first element inside `Body`.
- Without auth: `GetSystemDateAndTime` only. Every other action needs a WS-UsernameToken for user `camera`. Accept PasswordDigest = Base64(SHA-1(nonce bytes + Created + password)) and PasswordText. Refuse when `Created` is more than 15 minutes from `now`. A refusal answers HTTP 401 with a SOAP Fault `ter:NotAuthorized`.
- Answered actions: `GetSystemDateAndTime`, `GetDeviceInformation` (Manufacturer `Crystal Meet`, Model `<login.name>`, FirmwareVersion, SerialNumber, HardwareId `<model>`), `GetCapabilities`, `GetServices`, `GetNetworkInterfaces` (the MAC), `GetScopes`, `GetHostname`, `GetProfiles`/`GetProfile` (one profile `main`, with `VideoSourceConfiguration` token `vsc`/source `vs`/bounds 1280×720 and `VideoEncoderConfiguration` token `vec`, H264, 1280×720, `RateControl` FrameRateLimit 15/EncodingInterval 1/BitrateLimit 1500, GovLength 30, profile Main), `GetVideoSources`, `GetVideoSourceConfigurations`, `GetVideoEncoderConfigurations`, `GetStreamUri` (`rtsp://<host>:8554<streamPath(password)>`). Anything else gets a SOAP Fault `ter:ActionNotSupported` (HTTP 400), and the action name is logged so Protect's calls can be seen during verification.
- Every value written into XML is escaped.

- [ ] **Step 1: Write the failing tests**

```java
@Test public void dateAndTimeNeedsNoLogin()
@Test public void everythingElseNeedsTheLogin()            // GetProfiles without Security header → 401, Fault NotAuthorized
@Test public void digestLoginIsAccepted()                   // build PasswordDigest in the test from a fixed nonce/created
@Test public void wrongPasswordIsRefused()
@Test public void createdFarFromNowIsRefused()              // Created = now - 16 min → refused; now - 14 min → accepted
@Test public void passwordsWithXmlCharactersWork()          // password "p&ss<w>rd1": digest accepted; GetStreamUri reply parses as XML
@Test public void theStreamUriCarriesThePasswordsPath()     // contains "rtsp://192.168.0.194:8554" + streamPath("secret-pw1")
@Test public void aNewPasswordGivesANewPath()               // streamPath("a…") != streamPath("b…"), both 33 chars
@Test public void profilesHaveRateControlAndVideoSourceConfiguration()
@Test public void networkInterfacesGiveTheMac()             // "f8:7a:39:7c:f6:25"
@Test public void unknownActionsAreNotSupported()           // GetAudioOutputs → 400, ActionNotSupported
@Test public void probeIsAnsweredWithTheRelatesTo()          // Discovery: MessageID echoed in RelatesTo; XAddrs "http://192.168.0.194:8000/onvif/device_service"
@Test public void otherTypesAreIgnored()                     // Probe for dn:SomethingElse → null; a ProbeMatch (not a Probe) → null
@Test public void httpRequestAndTooLargeBody()               // MiniHttp: POST with Content-Length parsed; 70 KiB body → IOException
```

- [ ] **Step 2: Run them and see them fail** (Android test command; compile errors).
- [ ] **Step 3: Implement** with the JDK's `javax.xml.parsers`/`java.util.Base64`/`MessageDigest`/`Mac`. Write replies as string templates with escaped values. The SOAP 1.2 envelope uses namespaces `tds`, `trt`, `tt`, `ter`.
- [ ] **Step 4: Run the tests**: all pass.
- [ ] **Step 5: Commit**: `feat(door-sign): ONVIF answers and discovery for Protect`.

---

### Task 4: RTSP sessions and H.264 over RTP (pure Java)

**Files:**
- Create: `android/door-sign/app/src/main/java/to/croom/doorsign/Rtsp.java`, `H264Rtp.java`, `StreamClients.java`
- Test: `RtspTest.java`, `H264RtpTest.java`, `StreamClientsTest.java`

**Interfaces:**
- Produces: `final class Rtsp`
  - `static final class Request { String method, uri; Map<String,String> headers /* lower-case keys */; }`, `static Request read(InputStream in)`. Interleaved `$` frames from the client are read and skipped.
  - `static final class Reply { int status; Map<String,String> headers; String body; boolean play; boolean close; }`
  - `static Reply answer(Request r, String streamPath, String sdp, String sessionId)`: OPTIONS → `Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER`. Any other method on a URI whose path is not `streamPath` or below it → 404. DESCRIBE → `application/sdp`. SETUP with `RTP/AVP/TCP` → `Transport: RTP/AVP/TCP;unicast;interleaved=0-1`, and `Session: <id>;timeout=60`. SETUP with UDP → 461. PLAY → 200 with `play = true`. TEARDOWN → 200 with `close = true`. GET_PARAMETER/SET_PARAMETER → 200. `CSeq` is always echoed.
  - `static String sdp(String host, byte[] sps, byte[] pps)`: `m=video 0 RTP/AVP 96`, `a=rtpmap:96 H264/90000`, `a=fmtp:96 packetization-mode=1;profile-level-id=<hex sps[1..3]>;sprop-parameter-sets=<b64 sps>,<b64 pps>`, `a=control:trackID=0`.
- Produces: `final class H264Rtp { H264Rtp(int ssrc); List<byte[]> packets(byte[] nal, long presentationUs, boolean lastOfFrame); static List<byte[]> nals(byte[] annexB); static byte[] interleaved(byte[] rtp) /* '$', 0, len16, rtp */ }`. MTU 1400: a single NAL when it fits, FU-A when it doesn't. Timestamp = presentationUs × 90 / 1000. Marker on the last packet of `lastOfFrame`. Sequence numbers wrap at 65535.
- Produces: `final class StreamClients { StreamClients(int maxQueued); Client add(OutputStream out); void send(byte[] frame); void closeAll(); int size(); }`. Each client has its own bounded queue and writer thread. A client whose queue is full, or whose write fails, is closed and removed. `send` never blocks. `maxQueued` = 600 packets (about 2 s at 1.5 Mbit/s).

- [ ] **Step 1: Write the failing tests**

```java
@Test public void optionsDescribeSetupPlayTeardown()     // happy path with CSeq echo, Session header, interleaved=0-1, play/close flags
@Test public void theWrongPathIsNotFound()               // DESCRIBE rtsp://h:8554/guess → 404
@Test public void aNewPasswordRetiresTheOldStreamPath()  // answer(r with old path, Onvif.streamPath(new), …) → 404
@Test public void udpIsRefusedSoClientsUseTcp()          // 461
@Test public void sdpHasTheParameterSets()               // profile-level-id from sps bytes, base64 sets
@Test public void smallNalIsOnePacket()                  // header: V=2, PT 96, marker when lastOfFrame, timestamp 90 kHz
@Test public void largeNalIsFragmented()                 // 5000-byte NAL → FU-A start/end bits, reassembles to the original
@Test public void sequenceWraps()
@Test public void annexBSplitsOnThreeAndFourByteStartCodes()
@Test public void aSlowClientIsDroppedNotWaitedFor()     // a client whose OutputStream blocks: after maxQueued+1 sends it is removed; send() returned promptly each time (<50 ms)
@Test public void aFailingClientIsRemoved()              // OutputStream that throws → size() drops to 0
```

- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests**: all pass.
- [ ] **Step 5: Commit**: `feat(door-sign): RTSP and H.264 over RTP for the camera stream`.

---

### Task 5: The camera service on the panel (Android glue), version 3

**Files:**
- Create: `android/door-sign/app/src/main/java/to/croom/doorsign/CameraStream.java` (Camera2 + MediaCodec), `CameraService.java` (foreground service; owns the stream, the three servers and `trouble()`), `CameraControl.java` (start, restart or leave the service)
- Modify: `AndroidManifest.xml`, `DoorSignApp.java`, `MainActivity.java` (`onResume` → `CameraControl.ensure`), `Panel.java` (grant `CAMERA` as device owner, next to `grantWifiPermissions`), `SetupActivity.java` (the camera line under Room settings), `app/build.gradle` (`versionCode 3`, `versionName "3.0"`)

**Interfaces:**
- Consumes: `CameraLogin` (Task 2), `Onvif`, `Discovery`, `MiniHttp` (Task 3), `Rtsp`, `H264Rtp`, `StreamClients` (Task 4).
- Produces: `static String CameraService.trouble()` (null while running; the reason otherwise), `static String CameraService.line(Context)` (the setup screen's line), `static void CameraControl.ensure(Context)` (starts the service when the app is owner and a login is stored; otherwise stops it), `static void CameraControl.changed(Context)` (stops, then `ensure`s, so servers restart with the new login and old sessions close).

Behaviour (no new JVM tests: the logic it calls is tested in Tasks 2–4; verified on the panel in Task 7):
- Manifest: `CAMERA`, `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_CAMERA`, `CHANGE_WIFI_MULTICAST_STATE` permissions; `<uses-feature android:name="android.hardware.camera.front" android:required="false"/>`; `<service android:name=".CameraService" android:exported="false" android:foregroundServiceType="camera"/>`.
- `CameraService.onStartCommand`: `startForeground` with a low-importance notification (channel `camera`, text "Door sign camera"), type `FOREGROUND_SERVICE_TYPE_CAMERA`; `START_STICKY`. Start `ServerSocket(8000)` (MiniHttp → `Onvif.handle`, path `/onvif/*`), `ServerSocket(8554)` (one thread per connection, `Rtsp.answer`; on `play` the socket's stream joins `StreamClients`), and a `MulticastSocket(3702)` joined to 239.255.255.250 (with a Wi-Fi `MulticastLock`), answering `Discovery.probeMatch` by unicast to the sender. The endpoint URN is `urn:uuid:` + `UUID.nameUUIDFromBytes(identity.id)`. Host and MAC come from the active network's link properties.
- `CameraStream`: front camera; MediaCodec `video/avc` (Android picks the hardware `c2.rk.avc.encoder` first), `COLOR_FormatSurface`, 1280×720, bitrate 1_500_000, frame rate 15, I-frame interval 2, `KEY_PROFILE` Main (falling back to the encoder's default if refused); AE target FPS range 15–15, or the nearest that includes 15. It works on a `HandlerThread`, never the main thread. SPS/PPS come from the `BUFFER_FLAG_CODEC_CONFIG` output and are re-sent before every IDR. When a client starts playing, it calls `setParameters(PARAMETER_KEY_REQUEST_SYNC_FRAME)`.
- Failure (camera error or disconnect, encoder exception, port in use): close everything, set `trouble` to the reason, and retry after 10 s. The sign's activity is never touched.
- `DoorSignApp`: the Task 2 login consumer now also calls `CameraControl.changed(context)`, and `Panel`'s `cameraTrouble` becomes `CameraService::trouble`.
- `DoorSignApp.start` and `MainActivity.onResume` call `CameraControl.ensure`. The Android 14 foreground-service rules for camera are met because the app is the device owner and the sign is the foreground activity. If `startForegroundService` throws, `trouble` says so.

- [ ] **Step 1:** Implement the files above.
- [ ] **Step 2: Build and run all tests**: `cd android/door-sign && JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 ./gradlew testDebugUnitTest assembleRelease`. Expected: tests pass, and `app/build/outputs/apk/release/app-release.apk` is release-signed (`~/android-sdk/build-tools/34.0.0/apksigner verify --print-certs` shows `CN=Ben Walton`).
- [ ] **Step 3: Commit**: `feat(door-sign): the camera service — Protect records the door sign's camera`.

---

### Task 6: Docs

**Files:**
- Modify: `docs/guides/confluence/93-door-sign-manual.md` (new section "The camera in UniFi Protect", after "Managing it from the room"), `docs/guides/crystal-meet-door-sign-manual/index.html` (the same section), `docs/guides/confluence/91-touch-pi-manual.md` (Devices row: "the door sign camera password"), `android/door-sign/README.md` (the camera, its ports, the green dot)
- Modify: the spec, adding an "As built" section with what Task 7 found
- Test: `tests/unit/docs/` (existing docs tests)

Content: the spec's section 7 adoption steps verbatim, the ports, user `camera`, recording "Always" and ~16 GB per panel per day, the green dot being normal, the three card words, and the setup screen line.

- [ ] **Step 1:** Write the sections.
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/unit/docs -q`. Expected: pass.
- [ ] **Step 3: Commit**: `docs(door-sign): the camera in UniFi Protect`.

---

### Task 7: Verify on Conference Room B's panel (needs Ben)

- [ ] **Step 1:** Install on the Room B panel: `adb connect 192.168.0.194:5555 && adb -s 192.168.0.194:5555 install -r android/door-sign/app/build/outputs/apk/release/app-release.apk`. Check that the sign comes back and the card still checks in.
- [ ] **Step 2:** Room side: **ask Ben** before pushing the branch or merging to `main`. Then update Room B's TV Pi with Settings › Software › **Update** and set **Door sign camera password** under Settings › Devices.
- [ ] **Step 3:** Within 10 s the panel's setup line says `Camera on: 192.168.0.194, ONVIF port 8000, user camera`. The ports answer: an unauthenticated `GetSystemDateAndTime` POST gives 200, and `GetProfiles` without a login gives 401. A static `ffprobe` (downloaded to the scratchpad) reads `rtsp://192.168.0.194:8554<path>` as H.264 1280×720, and a guessed path gives 404.
- [ ] **Step 4:** Ben adopts it in Protect (spec section 7) and records for an hour. Check `adb logcat -s DoorSign` for unsupported ONVIF actions Protect asked for, and add any it needs (back to Task 3's tests). Check that the picture is upright (the sensor reports orientation 0), and that the sign stays smooth and keeps checking in every 5 s.
- [ ] **Step 5:** Check that the other rooms' panels are the same model (`adb shell getprop ro.product.model` if they have adb over the network; otherwise ask Ben). A different model must still run the camera, or show its reason on the card.
- [ ] **Step 6:** Record the findings in the spec's "As built" section and commit. Publishing release `door-sign-3` on GitHub (so the other rooms' **Update** offers it) happens only with Ben's go-ahead.
