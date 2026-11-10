# The door sign camera's sound

**Date:** 2026-11-10. **Status:** approved by Ben ("build it"), after the proposal of a per-room switch, off by default.
**Builds on:** `2026-11-06-door-sign-camera-design.md`, whose non-goal "Sound" this replaces.

## 1. Intent

UniFi Protect records each door sign's picture. Ben asked whether it can record sound too. The panel has a microphone (Android lists
"Built-In Mic"; the rk3576 has a PDM microphone array), and Ubiquiti's help article "Third-Party Cameras in UniFi Protect" lists
audio as supported on third-party cameras, converted on the console. The camera spec left sound out on purpose: a door sign in a
hallway records people's conversations, and in many US states recording a conversation needs everyone's consent. So sound is a
choice each room makes, not a default.

## 2. Goals, non-goals

- **A per-room switch:** Settings › Devices, **Door sign camera sound** (`control.door_sign_camera_sound`), off by default, live.
  Its help text says to check the consent law and to put up a notice.
- **The panel sends the microphone with the picture** while the switch is on, through the existing camera: same ports, same login,
  same stream path, a second track.
- **The microphone permission follows the switch:** granted by the app as device owner while it is on, denied again when it goes
  off. With it off the app cannot record sound at all.
- **A failing microphone costs only the sound.** The picture carries on and the card says "Sound off: …".
- Non-goals: two-way audio (Protect's talkback), sound on panels that aren't the device owner, a per-panel switch, RTCP.

## 3. What changes

**The room.** `ControlConfig.door_sign_camera_sound: bool = False`; the schema's switch on the devices page, live; the check-in
answer's camera block gains `"sound": true|false`. The block still goes only to joined door signs, only with a password. `"sound"`
joins `"camera"` in `CANNOT_EXTRA`, so the card shows the panel's "Sound off: …".

**The app (version 4).**

- `CameraLogin.sound`, from `"sound": true` only (any other value is off), kept in the app's storage. A different value is a
  different login, so the camera starts again with or without the microphone.
- `Panel.grantPermissions` sets `RECORD_AUDIO` to granted or denied from the kept login. It runs on the check-in thread when a new
  login arrives, before `CameraControl.ensure`.
- `CameraService` runs as camera **and microphone** (`foregroundServiceType="camera|microphone"`, `FOREGROUND_SERVICE_MICROPHONE`)
  while it sends sound: from Android 11, Android silences a microphone used outside a foreground service of that type. If Android
  refuses the microphone type, the service runs as camera only and says why.
- `SoundStream`: `AudioRecord` (MIC, 16 kHz mono PCM16) into MediaCodec's AAC-LC encoder, 32 kbit/s, on a thread of its own.
  Timestamps come from the sample count.
- RTSP: the SDP adds `m=audio 0 RTP/AVP 97`, `MPEG4-GENERIC/16000/1`, AAC-hbr (`sizelength=13;indexlength=3;indexdeltalength=3`)
  with the encoder's AudioSpecificConfig as `config=`, and `trackID=1`. A SETUP of `trackID=1` gets interleaved channels 2-3.
  DESCRIBE waits (503) until the encoder has given its config, unless the microphone has failed.
- `AacRtp`: one AAC frame per packet behind one AU header (RFC 3640), marker set. `StreamClients.sendSound` sends only to viewers
  that set the sound track up, and only once their picture has started at a keyframe.
- ONVIF: with sound, the profile has an `AudioSourceConfiguration` and an `AudioEncoderConfiguration` (AAC, 32 kbit/s, 16 kHz).
  `GetAudioSources`, `GetAudioSourceConfigurations` and `GetAudioEncoderConfigurations` are answered. Without sound they are not.
- Words: the setup line ends ", with sound" while the microphone is sent. The card says "Sound off: Android has not granted the
  app the microphone", "Sound off: Android would not let the app use the microphone: …" or "Sound off: the microphone did not
  start: …". A microphone that failed is tried again when the camera next starts (a reboot, a new login, a camera failure).

## 4. My calls

- **AAC-LC, 16 kHz mono, 32 kbit/s:** the format IP cameras send and Protect's own cameras use. Speech needs no more, and it is
  about 350 MB a day per panel.
- **No RTCP sender reports.** A viewer lines the tracks up by when each began, and both begin at the viewer's first keyframe,
  within a frame of each other. If Protect plays them out of step, add SRs from one clock.
- **A microphone failure doesn't stop the camera** (unlike a camera failure): recording the picture matters more than the sound.

## 5. Checked on Room-1's panel (2026-11-10, the Boardroom's sign, Protect 7.3.70)

- The stream: ONVIF offers H.264 and AAC; the SDP has both tracks; in 10 seconds 1,439 picture and 157 sound packets at 32.2 kbit/s,
  the two clocks in step. Android records the microphone, not silenced, as camera and microphone foreground service.
- Protect, re-adopting: GetSystemDateAndTime, GetServices, GetProfiles, GetDeviceInformation, GetSnapshotUri (not answered),
  GetStreamUri, GetServices, GetVideoEncoderConfigurationOptions (not answered); no ONVIF audio call. On RTSP, SETUP of both
  tracks (`trackID=1` on 2-3), PLAY, GET_PARAMETER every 10 s. Ben: the sound "works now" in Protect after re-adopting.
- With sound off (the room sending no `sound`), version 4 runs as camera only and the microphone permission is denied.
- Seen once: turning sound on restarts the camera, and the new camera opened while the old one was still closing failed
  ("the encoder failed: MediaCodec error 1101"); the 10-second retry recovered. The same restart follows a new password in
  version 3. Not fixed yet: the restart could wait for the old camera to close.
- Not measured: how well the microphone hears the hallway, and the console's extra load.
