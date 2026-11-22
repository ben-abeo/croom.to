# The door sign camera's people detection

**Date:** 2026-11-22. **Status:** built, not yet tried on a panel or against the office's Protect (section 7). Ben to check the calls
in section 8.
**Builds on:** `2026-11-06-door-sign-camera-design.md`, whose non-goals "Motion events" and "smart detections" this replaces, and
`2026-11-10-door-sign-camera-sound-design.md`, whose switch this one is modelled on.

## 1. Intent

Ben asked: "Can we build in UniFi people detection and other features for UniFi Protect into the door sign security camera?"

Today each door sign is a camera Protect records, with sound if the room chose it, and nothing else: no detections, so Protect can
only record it Always, cannot mark when someone was there, and cannot alert on it.

## 2. What Protect lets a third-party camera do (looked up 2026-11-22, not tested on the office's console)

Ubiquiti's own pages (help.ui.com, blog.ui.com) could not be opened from the build session, so what they say is given here as
search results reported it. The two GitHub projects were read directly.

- **Protect's own smart detections cannot be switched on from the camera.** Person, vehicle, face and the rest are worked out on
  Ubiquiti's cameras, or on a UniFi AI Port put in front of a third-party camera. Nothing was found that a third-party camera can
  send to make Protect label an event "Person". A review of Protect 7.3 with ONVIF cameras is reported to have found that Protect
  does not tell a camera's smart events from its plain motion.
- **What Protect does take from a third-party camera is motion.** Ubiquiti's help article on third-party cameras is reported to
  list motion detections, PTZ control and audio as supported and other advanced features as not, and to say that motion has to
  be set up on the camera and sent to Protect. From Protect 7.1 it subscribes to the camera's ONVIF events itself (the README of
  `danielwoz/ubiquiti-protect-onvif-event-listener`).
- **Working prior art.** Kiosk Satellite, an Android kiosk app that is also an ONVIF camera, added exactly this in October 2026
  (`jxlarrea/kiosk-satellite` issue 1098): motion over PullPoint subscriptions on the topics
  `tns1:RuleEngine/CellMotionDetector/Motion` and `tns1:VideoSource/MotionAlarm`, with a person sensor as one of the sources. Its
  author reports it tested with Protect: motion turned on and off within 2 to 3 seconds, and the camera had to be removed and
  added again, because Protect reads a camera's ONVIF services only when it adds it.
- **An AI Port** gives a third-party camera Protect's real Person (and vehicle, face, plate) detections. Ubiquiti's limit is
  reported as up to 3 ONVIF cameras at HD per port. It works with the door signs as they are today and needs no code. It is the
  only way found to the "Person" label, and it can sit beside what is built here.
- A community project (the listener above) writes Person events into Protect's own database by patching the console. Not
  considered: it changes the UDM Pro's software.

So the answer to "UniFi people detection" has two halves. The label is Ubiquiti's to give, and costs an AI Port. The behaviour
(Protect knows when a person is at a door sign, and records, marks and alerts on it) can be built: **the door sign finds the people
itself and tells Protect, as motion.** On a door sign's camera, "motion" in Protect then means a person.

## 3. Goals, non-goals

- **A per-room switch:** Settings › Devices, **Door sign camera people detection** (`control.door_sign_camera_people`), off by
  default, live. With it off the app runs the camera exactly as version 4 does.
- **The panel finds people in its own picture** and keeps one yes or no: someone is there.
- **Protect is told as ONVIF motion,** over a PullPoint subscription, on the two topics above.
- **A failure never makes the camera tell less.** A security camera whose detection broke must not leave Protect recording nothing.
  Without the model, any movement counts as someone. Without pictures to look at, someone is always there. The card says which.
- **A failure never costs the picture,** and the sign stays smooth: the watching runs on its own low-priority thread, and the model
  only while something moves or someone is there.
- Non-goals: Protect's "Person" label (an AI Port); counting people; saying where in the picture they are; faces; a per-panel
  switch; zones; panels that are not the device owner.

## 4. What changes

**The room.** `ControlConfig.door_sign_camera_people: bool = False`; the schema's switch on the devices page, live; the check-in
answer's camera block gains `"people": true|false`; `"people"` joins `CANNOT_EXTRA`, so the card shows "People detection off: …".

**The app (version 5).**

- `CameraLogin.people`, from `"people": true` only, kept in the app's storage. A different value is a different login, so the camera
  starts again with or without the watching.
- `CameraStream` takes an optional `Second`: a second, small picture the camera draws beside the encoder's (an `ImageReader`,
  YUV 4:2:0, the smallest size the camera offers whose middle 16:9 is at least 640 × 360). If the camera will not draw two pictures,
  the stream starts again with the one, as before, and the `Second` is told.
- `SecondPicture`: a camera may take both pictures and then fail, and the service starts a failed camera again every 10 seconds.
  One that fails twice running while it draws the second picture is not asked for it again until the room sends another login or
  the panel restarts, so the stream comes back and records. A camera that ran 10 minutes with both has shown it can.
- `PeopleWatch` owns that picture and a thread (`door-sign-people`, background priority). Five times a second it copies a picture
  out and gives the buffer back, then:
  - `Frame.grid`: the brightness of a 32 × 18 grid over the middle 16:9, which is what the stream shows.
  - `MotionGate`: a cell more than 10 (of 255) off what it has looked like lately has moved, after taking off the shift all cells
    share (the lights, the exposure); two moved cells is movement.
  - `Presence`: look for people while something moved in the last 2 seconds or someone is there, at most every 250 ms. Someone is
    there from the first sight of them until 10 seconds after the last, unless nothing at all has moved for 60 seconds (a poster).
  - `Frame.square` and `PeopleModel`: one of two squares, the left or the right of the picture, each as tall as it, made 320 × 320
    for the model; the square that moved, or each in turn. A person at 0.5 or surer is someone.
- `PeopleModel`: TensorFlow's EfficientDet-Lite0 (COCO, 8-bit, 4.6 MB, Apache 2.0) as the app's asset `people.tflite`, run by
  TensorFlow Lite 2.16.1 on two threads. This is the app's first library.
- `Events` and `OnvifEvents`: the event service at `/onvif/event_service` and each subscription at `/onvif/subscription/<id>`,
  told apart by path because Protect sends no WS-Addressing headers. `GetServices` and `GetCapabilities` name the service;
  `GetEventProperties`, `GetServiceCapabilities`, `CreatePullPointSubscription`, `PullMessages` (which waits, up to 60 seconds, for
  news), `Renew`, `Unsubscribe` and `SetSynchronizationPoint` are answered. All need the login. At most 8 subscriptions; one that
  is neither asked nor renewed ends; at most 4 askings wait at once, so the other ONVIF requests always get a connection.
- Words: the setup line ends ", with people detection". The card says "People detection off: <why>. Protect is told of any movement
  instead" or "… Protect is told there is movement all the time, so it keeps recording".

## 5. Why two squares, and why this model

Measured here with the model on a 1280 × 720 scene and a person pasted in at different sizes (score at left, middle, right):

| Person's height | Whole picture, letterboxed to 320 | Two 720 squares, each made 320 |
| --- | --- | --- |
| 300 px | 0.74 0.72 0.74 | 0.84 0.79 0.72 |
| 220 px | 0.59 0.63 0.54 | 0.57 0.72 0.79 |
| 160 px | 0.35 0.29 0.39 | 0.63 0.71 0.69 |
| 120 px | 0.15 0.00 0.10 | 0.59 0.57 0.50 |
| 90 px | 0.09 0.00 0.00 | 0.43 0.41 0.39 |

The whole picture squeezed into the model's square loses people below about 220 px; two squares keep them to about 120 px, which
is a person roughly twice as far away. One square is looked at per look, so the cost is the same.

Against the alternatives: SSD MobileNet v1 called a dog a person on the test photos (0.52) where EfficientDet-Lite0 gave 0.15.
Android's own face detection finds faces, not people walking past side-on or away. Finding people on the room's Pi (its AI HAT)
would tie the camera's events to the Pi being up, and the HAT is busy with the room's own camera.

## 6. Other Protect features looked at

- **Motion events:** this.
- **Snapshots** (`GetSnapshotUri`) and **encoder options** (`GetVideoEncoderConfigurationOptions`): Protect asks for both when it
  adopts the camera (seen 2026-11-10) and adopts it without them. Nothing found says Protect does more with them for a third-party
  camera. Left unanswered.
- **A second, small stream; talkback; PTZ; zones; privacy masks:** not offered for third-party cameras, or nothing to move.
- **A "person" topic beside motion:** Protect is not known to read one. Left out; it is one more topic in `Events` if that changes.

## 7. Checked, and not checked

**Checked in the build session (no panel, no console):**

- 186 JVM tests, 63 of them new: the subscriptions (`EventsTest`), their SOAP (`OnvifEventsTest`), the picture (`FrameTest`), the
  movement gate, the presence logic, the model's result reading, when the second picture stops being asked for, the login's new
  flag and the card's words.
- The app's own `Frame`, given pictures laid out as Android's camera buffers are (padded rows, colour woven together), feeding the
  real model: a person near at the left 0.77, far at the right 0.59, at the seam 0.65, far at the left 0.67, an empty scene 0.00.
  The same scenes through a reference pipeline gave 0.74, 0.59, 0.67, 0.61, 0.04.
- The model's inputs and outputs are what `PeopleModel` expects: 320 × 320 × 3 bytes in; boxes, classes, scores and a count of 25
  out, in that order, class 0 a person.
- The Python tests for the setting, the check-in answer and the card's words.
- The whole app compiles against Android 14's framework and TensorFlow Lite's API. **Gradle itself was not run:** the session could
  not reach the Android SDK's download host.

**Not checked. Each needs a panel, and the last three the console:**

- That the WF2489T's camera draws two pictures at once, and that the stream's frame rate holds with the second.
- That TensorFlow Lite loads on the panel, how long a look takes on the rk3576, and that the sign stays smooth while it runs.
- The thresholds in a real hallway: `MotionGate.DELTA`, `PeopleModel.SURE`, how far away a person is still found.
- That Protect 7.3.70 subscribes after adopting the camera again, which of the two topics it reads, and how it shows the events.
- Which recording modes Protect then offers for the camera besides Always.
- Whether Protect makes a new subscription by itself after the camera restarts (a new password, a camera failure, a reboot).

## 8. My calls (Ben to check)

- **A switch, off by default,** though the camera itself has none: turning it on adds work to the panel and needs the camera
  adopted again in Protect, so each room should choose when.
- **People, not plain motion,** as asked. Plain motion is what the panel falls back to if the model cannot run.
- **A library and a 4.6 MB model in the repository.** The 2026-11-06 spec preferred no libraries; there is no way to find people
  without a model, and TensorFlow Lite is the smallest runtime for one. The APK grows by roughly 8 MB.
- **Someone at the first sight, gone 10 seconds after the last.** A miss is worse than a false alarm for a security camera, and
  the model only looks when something moved, which in an office hall is nearly always a person.
- **Fail towards recording.** A door sign that cannot tell people says "movement" too often rather than never.
- **No AI Port bought.** It is the way to Protect's own Person label, face and plate search; it needs no code and can come later.

## 9. Turning it off again

With the switch off the camera has no event service. A Protect that was set to record a door sign on motion only would then record
nothing. The manual says to set the camera's recording back to Always before turning the switch off.
