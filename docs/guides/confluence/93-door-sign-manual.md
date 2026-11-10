# Door sign technical manual

*Part of [Conference room technical manuals]. The printable version is attached as crystal-meet-door-sign-manual.pdf.*

> **WHICH SETUP PROCEDURE IS CURRENT**
>
> The older guide "Set up a Crystal Meet door sign panel" predates remote management. This manual and `android/door-sign/README.md` are the current procedure.

## Overview

*The panel shows the TV Pi's page and lights the colour it asks for*

The door sign tells people in the hall whether the room is free, who has it and until when, and lets them claim it on the spot. The panel itself knows almost nothing: it shows a web page that the room's TV Pi serves, and it lights its LED bar in the colour the page asks for.

- **SMT1019 panel** (by the door): App **to.croom.doorsign** shows **/sign** full screen and drives the light bar.
- **TV Pi · croom** (behind the TV): Serves the page, keeps bookings, answers check-ins, hands out jobs.
- **Google and GitHub** (outside): The room calendar; door sign releases for updates.
- Link: HTTP :8080 page, status, bookings, check-in
- Link: HTTPS events, releases

- **5 s**: Status refresh and check-in interval
- **10 min**: "Starts soon" window, the bar turns yellow
- **15 min**: Booking steps, up to 4 hours
- **10 s**: Retry when the room can't be reached

### What the sign can and cannot do

**CAN**

- **Show the room's state**: Free, starting soon, Booked or In use, in colour and on the light bar.
- **Book** any open quarter hour in the next 7 days, during booking hours.
- **Use now**: claim the free room from this minute.
- **Cancel** a booking made at the room's screens that has not started.
- **Show today and this week**.

**CANNOT**

- **End a meeting early.** End now is only on the table's touch screen; the server refuses it from the sign.
- **Cancel bookings made in Google Calendar.** Those are changed in Google.
- **Work without the TV Pi.** It shows "Sign not connected" with the bar off.
- **Start or join a call.** Calls are run from the table.

## Hardware and the light bar

*A PoE wall panel whose light bar mirrors the room's state*

| Item | Detail |
| --- | --- |
| Panel | SMT1019 10.1-inch wall panel by Apolosign / Hopestar. Rockchip RK3576, Android 14. |
| Power | PoE from a switch or injector, or the panel's own power supply. |
| Network | Ethernet (`eth0`) or Wi-Fi. It must reach the TV Pi on TCP **8080**, plain HTTP. The panel itself needs no internet access. |
| Ports | USB-C, used as the data cable for adb during setup. |
| Light bar | RGB LEDs round the panel's edge, controlled through the kernel device `/dev/ledjni`. |
| Other tablets | The app runs on any Android 8.0+ tablet, without the light bar. Remote updates need Android 12+, Wi-Fi changes Android 11+. |
| Mounting | Not specified by Crystal Meet: follow the panel maker's bracket instructions. Mount it where the hall can see it, at a comfortable touch height. |

### Light bar colours

The page decides the room's state and tells the app; the app sets the bar. The colours live only in the app.

| State | Meaning | Bar | Sign colour |
| --- | --- | --- | --- |
| free | Nothing on now, nothing within 10 minutes | Green (R0 G15 B0) | Green #1FA971 |
| soon | A booking starts within 10 minutes | Yellow (R15 G15 B0) | Amber #E8A013 |
| occupied | A meeting or booking is in progress | Red (R15 G0 B0) | Red #E5484D |
| offline | The room can't be reached, the page is loading, or any unknown state | Off | Slate grey #647087 |

### How the bar is driven

- **Native library.** `libledbar` (from `app/src/main/cpp/ledbar.c`) opens `/dev/ledjni` and sends one `ioctl(fd, channel, level)` per colour: channel **161** red, **162** green, **163** blue, level **0 to 15**. This copies the panel's own LED test app.
- **One change at a time.** Each change opens the device, sets red, green and blue, and closes it, on a single background thread.
- **The bar goes off** when the page can't load, when the WebView crashes, and on Exit to Android. Returning to the sign restores the last colour.

> **QUICK CHECK**
>
> Book the room a few minutes ahead from the sign. The bar should turn yellow, then red when the booking starts, then green when it ends, and never disagree with the sign's own colour.

## Using the sign

*Anyone in the hall can see the room's state, book it or claim it now*

A navy page over a photo background, with a coloured rail down the left edge. The header carries the Crystal PM logo, the company name, the time and the date. The main area shows the room's name, a status chip, the **Use now** and **Book** buttons and the **Today** list.

### The status chip, rule by rule

The page checks these in order and stops at the first that applies. "In use" counts the meeting states joining, in lobby, connected and leaving.

| When | Chip | Line under it | Bar |
| --- | --- | --- | --- |
| Before the first status | Connecting | none | unchanged |
| TV Pi not answering | Sign not connected | Check that Crystal Meet is running on the room's device. | Off |
| A call is in progress | In use (until 2:00 PM) | Meeting title, booking title, or the platform | Red |
| A booking is current | Booked until 2:00 PM | Booking title | Red |
| Next booking within 10 min | Weekly sync starts in 8 minutes / is starting | 1:00 PM to 2:00 PM | Yellow |
| A booking later today | Free until 1:00 PM | Next: title | Green |
| Nothing more today | Free for the rest of the day | Nothing else is booked in here today. | Green |
| No calendar set up | Free | none | Green |

On a short screen (560 px high or less) the soon chip shortens to "Starts in 8 minutes" or "Starting now".

### Today and This week

- **Today** shows up to three bookings that have not ended: time, title, and the platform, "Double-booked with …" or "Not in Google Calendar yet". The booking in progress is ringed in the status colour.
- **Notes under the list:** "Nothing else scheduled today." or "Calendar offline since 10:42 AM. Bookings made here will sync."
- **This week** opens seven days, today first, with "Nothing booked" or "+N more" where needed. **Back**, or 30 seconds without a tap, returns to Today.

### Use now

Shown only when the room has a booking calendar, nothing is booked or running now, and there is time free.

1. Tap **Use now**. The panel shows "Use the room now" and "Free until 2:00 PM" or "From now".
2. Pick a length; each shows its end, for example `30 min · until 12:30 PM`.
3. Type your name or the meeting's name (up to 60 characters) and tap **Start**.
4. The sign says "The room is yours until 12:30 PM." and turns red.

> **GOOD TO KNOW**
>
> Use now starts this minute and ends on the quarter hour nearest to now plus the length (halfway rounds up): at 12:03, 30 min runs to 12:30. It stops at the next booking and is **not** limited to booking hours.

### Book

1. Tap **Book**. "Book the room" offers **Today**, **Tomorrow** and the next five days.
2. Pick an open start time. Times are every 15 minutes inside booking hours (08:00 to 18:00 unless changed).
3. Fill in **Your name or the meeting's name** and choose **How long**: 15 minutes to 4 hours, limited by the next booking and the end of booking hours. The default is 30 minutes.
4. Tap **Book**. The sign confirms "Booked: title, 1:00 PM to 1:30 PM." **Change time** goes back a step.

- **Google Calendar.** The booking is written to the room's Google calendar with the description "Booked on the <room> door sign" (or "Started on…" for Use now). If Google can't be reached, the sign says so and the room sends it later.
- **Timeouts.** The confirmation closes after 4 seconds (8 with a warning). Steps left alone for 60 seconds close by themselves.
- **Cancel.** A booking made at the room's screens that has not started shows **Cancel** in Today. Tap it, then tap again within 4 seconds ("Tap again to cancel").

### Background photo and football scores

Chosen on the touch Pi under **Options › Door sign** (no PIN needed) and stored in `/var/lib/croom/control-settings.json`.

- **Backgrounds:** Austin skyline (default), Capitol, UT Tower, Pennybacker Bridge, Hamilton Pool, Texas star, Hill Country, DKR stadium, Plain. A photo that hasn't loaded in 20 seconds falls back to plain navy.
- **Scores:** Texas, Michigan, Texas A&M, Missouri, all off by default. The TV Pi fetches them from ESPN every 10 minutes; with several on, the strip rotates every 8 seconds.

## Setting up a new panel

*A factory reset, five adb commands and one Allow on the touch Pi*

About 30 minutes per panel. The app becomes the panel's **device owner**, which locks the panel to the sign and lets the room reboot it, update it and change its Wi-Fi. Android only allows a device owner on a freshly reset panel.

> **BEFORE YOU BEGIN**
>
> **A computer with adb.** Windows: `winget install Google.PlatformTools`. Linux: `sudo apt install adb`. WSL can't see USB devices, so use Windows adb or a Linux machine.
>
> **The release APK** `app-release.apk`, signed with the room key (Section 07). A debug build can never be updated to a release later.
>
> **The TV Pi's address**, for example `10.0.0.21`, and a **PIN** for the panel (4 to 8 digits).
>
> **Replacing an existing panel?** The reset erases it: note its Wi-Fi password first.

### 1 · Factory reset and enable USB debugging

1. On the panel: **Settings › System › Reset options › Erase all data**.
2. Go through Android's first-start screens **without adding a Google account**. Turn on Wi-Fi or plug in Ethernet.
3. **Settings › About tablet**: tap **Build number** seven times. Then **Settings › System › Developer options › USB debugging** on.
4. Connect the USB-C cable. On the panel accept **Allow USB debugging?** and tick **Always allow from this computer**.

```
adb devices
# the panel's line must end in "device"; "unauthorized" means the prompt is still waiting on the panel
```

### 2 · Install the app and make it device owner

```
adb install -r app-release.apk
adb shell dpm set-device-owner to.croom.doorsign/.AdminReceiver
# expect: Success: Device owner set to package to.croom.doorsign
```

If Android says the device already has accounts or users, the reset was incomplete or a Google account was added: go back to step 1.

### 3 · Make it the home and start-up app

```
adb shell cmd package set-home-activity to.croom.doorsign/.MainActivity
adb shell setprop persist.sys.bootapp to.croom.doorsign/to.croom.doorsign.MainActivity
adb shell am start -n to.croom.doorsign/.MainActivity
```

`persist.sys.bootapp` is the SMT1019's own start-up app setting; some panels arrive with it set to Chrome.

### 4 · First setup on the panel

1. The **Door sign setup** screen opens by itself. In **Crystal Meet server** type the TV Pi's address and port, for example `10.0.0.21:8080`. Use the IP address, not a `.local` name.
2. In **Choose a PIN for this screen** enter 4 to 8 digits. Record it in the office password manager.
3. Press **Green**, **Yellow**, **Red** and **Off** under **Test the light bar** to check the LEDs.
4. Check that **Room settings** says "This app is the panel's device owner…".
5. Press **Save**. The sign loads full screen.

### 5 · Allow it on the touch Pi

1. On the table: **Options › Settings**, enter the room PIN, open **Software**.
2. Under **Devices waiting to join**, tap **Allow** beside the door sign. Allow only a device you know.

*You should now see a **Door sign** card with "Checked in just now", its version, the newest release, **Update** and **Reboot**; its Wi-Fi appears under **Settings › Network**. Unplug the USB cable.*

## On-panel setup and the PIN

*A 5-second corner hold opens setup, behind the panel's PIN*

### Opening setup on a running sign

1. Hold the **top-left corner** (over the Crystal logo, a 96 × 96 dp area) for **5 seconds**.
2. Enter the PIN on **Enter the PIN** and tap **Unlock**. A wrong PIN says "Wrong PIN"; there is no lockout.
3. Change the server, set a **New PIN** (leave blank to keep the current one), test the light bar, and **Save**. Back returns to the sign without saving.

| Setup screen part | What it does |
| --- | --- |
| Crystal Meet server | `10.0.0.5:8080` becomes `http://10.0.0.5:8080/sign`. A full http(s) address is kept as typed. Anything else: "Enter the server's address, for example 10.0.0.5:8080". |
| PIN | 4 to 8 digits, else "The PIN must be 4 to 8 digits". Stored only as a salted SHA-256 hash. |
| Test the light bar | Green, Yellow, Red, Off drive the bar directly. |
| Room settings | Whether the app is device owner, the check-in state line, the camera line, and "Version 3 (3.0)". |
| Leave the sign | **Exit to Android**: bar off, kiosk lock released, status bar shown, the panel's own launcher opens. **Home** brings the sign back. |

### Check-in state line

| Text | Meaning |
| --- | --- |
| Joined the room at http://…:8080 | All good. |
| Waiting to join: tap Allow under Devices waiting to join on the touch Pi… | Allow it in Settings › Software. |
| Can't reach the TV Pi at http://…:8080: <reason> | Network or address problem, or croom is down. |
| The TV Pi answered 403: This device was refused | It was refused; see Section 06. |
| Not checked in: the sign has no Crystal Meet server yet | Enter the server and Save. |

### Forgotten PIN

- **Panel set up the old way (not device owner):** run `adb shell pm clear to.croom.doorsign`. This wipes the address, the PIN and the panel's check-in identity; first setup opens again and the panel must be Allowed again on the touch Pi.
- **Device-owner panel:** Android normally refuses to clear a device owner's data. Factory reset the panel and repeat Section 04. Keep the PIN in the password manager to avoid this.

### Returning a panel to a normal tablet

**Device-owner panel:** factory reset. It is the only way to remove a device owner. **Old-style panel:**

```
adb shell cmd package set-home-activity com.android.launcher3/.uioverrides.QuickstepLauncher
adb shell setprop persist.sys.bootapp ""
adb shell am broadcast -a elc.view.show
adb uninstall to.croom.doorsign
```

### After a power cut

The panel boots straight into the sign. Until the TV Pi answers it shows "Can't reach Crystal Meet at …" with the bar off and retries every 10 seconds. A Wi-Fi change or job that was cut off is finished or undone at start and reported to the room.

## Managing it from the room

*Once allowed, the room can reboot, update and re-network the panel*

Once allowed, the panel checks in with the TV Pi every 5 seconds. Each answer can carry one job. Everything here is done on the touch Pi under **Options › Settings** (room PIN) and needs the app to be device owner.

### The device card

- **Title:** "Door sign" plus the panel's model name.
- **Seen:** "Checked in just now" or "N seconds ago" while online (checked in within 20 seconds); otherwise "Not seen since 10:42 AM".
- **Version:** "Version 3 (3.0)"; **Newest:** "Newest: version N, date" or "Up to date".
- **Buttons:** **Update**, **Reboot**, and **Forget** (second tap) for a replaced panel. Network changes are on **Settings › Network**.

### Allow, Refuse, Forget

| Action | Effect |
| --- | --- |
| **Allow** | The waiting device joins and gets its jobs. At most 10 can wait; a waiting entry unseen for 10 minutes is dropped. |
| **Refuse** (tap twice) | Remembers the device's id and turns it away from then on. |
| **Forget** (tap twice) | Removes a joined device and remembers nothing. |

**Undo a Refuse:** on the TV Pi, stop the service, empty the `"refused"` list, start it again. Or clear the panel app's data so it gets a new id.

```
sudo systemctl stop croom
sudo nano /var/lib/croom/devices.json     # set "refused": []
sudo systemctl start croom
```

### Jobs

| Job | What the panel does | What the card reports |
| --- | --- | --- |
| Reboot | Asks Android to reboot | Rebooting, then Rebooted |
| Update | Downloads, checks and silently installs a newer release | Downloading, Checking, Installing, Updated to version N |
| Wi-Fi scan | Lists up to 30 networks, strongest first | Looking for networks, Found N networks |
| Wi-Fi connect | Joins with a 90-second safety net | Joined X. The door sign reaches the TV Pi |
| Wi-Fi forget | Removes a saved network | Forgot X |

- **Never during a meeting.** Reboot, update and Wi-Fi changes are refused while a call runs: "Leave the meeting first".
- **One at a time.** "The door sign is busy: wait for it to finish". A job is never run twice.

### How an update runs

1. **Update** (or **Update all**, which does touch Pis and door signs first and the TV Pi last) asks the TV Pi for release N.
2. The TV Pi downloads `app-release.apk` from the GitHub release `door-sign-N`, checks its size, SHA-256 and that it is an APK, and keeps it in `/var/lib/croom/door-sign/` (two newest kept).
3. The panel fetches it from `GET /api/devices/apk/N`, checks size and SHA-256, that it is the door sign app, newer, and signed with the same key.
4. It installs without asking anyone (up to 3 minutes), restarts, and reports "Updated to version N".

> **NO ROLLBACK**
>
> If the panel hasn't checked in 5 minutes after installing, the card says "Door sign didn't check in after updating". Go to the panel. If it comes back later its own report replaces the message.

### Changing Wi-Fi safely

1. The change is recorded first; the old network stays saved.
2. The panel joins the new network and, every 5 seconds for up to 90 seconds, checks it is on it **and** can reach the TV Pi.
3. Success: "Joined X. The door sign reaches the TV Pi". Failure: it goes back to the old network and says why, for example "Could not join X within 90 seconds" (often a wrong password).

Refused on purpose: enterprise (user-name) networks, WEP, a new password for the network it is on ("join another network first"), and a new password for another saved network ("Forget X first"). The room can't move the panel to a network that can't reach the TV Pi.

### The camera in UniFi Protect

From version 3, every door sign's front camera can be recorded by UniFi Protect as a third-party (ONVIF) camera. The panel serves the camera itself, so recording carries on while the TV Pi restarts or updates. It needs the app to be device owner and the room's **Door sign camera password**. Until the room sends that password, nothing listens on the panel. From version 4 it can send the panel's microphone too, room by room: see **Sound** below.

1. On each room's touch screen: **Options › Settings › Devices**, set **Door sign camera password** (8 to 64 characters, no spaces). It applies at once. Using the same password in every room is simplest. Keep it in the office password manager.
2. **Update** the door sign on **Settings › Software** to version 3 or later.
3. In Protect: **Settings › System › Discover Third-Party Cameras** on.
4. On the UniFi Devices page, adopt the door sign with user `camera` and the password. If it isn't listed, use **Advanced Adoption** with the panel's IP address and port **8000**.
5. Set its recording to **Always**. The panel sends no motion events.

| | |
| --- | --- |
| Picture | H.264, 1280 × 720, 15 frames a second, about 1.5 Mbit/s (about 16 GB a day per panel when recorded Always) |
| Sound | Only while the room's **Door sign camera sound** is on: AAC, 16 kHz mono, 32 kbit/s (about 350 MB a day per panel) |
| Ports on the panel | ONVIF TCP 8000, RTSP TCP 8554, WS-Discovery UDP 3702 |
| Login | User `camera`, the room's password. The stream's address is handed out only to a client that logged in, and changes with the password |
| On the sign | Android's small green dot in a top corner, always: the camera, and the microphone too while the sound is on. It is normal |
| Setup screen | "Camera on: 192.168.0.194, ONVIF port 8000, user camera" (ending ", with sound" while it sends the microphone), or why it is off |

| The card says | Meaning | Fix |
| --- | --- | --- |
| Camera off: set the door sign camera password in Settings › Devices | The room has no password, or the panel hasn't checked in since it was set. | Set it; the panel picks it up within 5 seconds. |
| Camera off: this panel is not the device owner | Set up the old way. | Factory reset and follow Section 04. |
| Camera off: the camera did not start: … | The camera, encoder or a port failed. It tries again every 10 seconds. | Reboot the panel from the card. If it stays, read `adb logcat -s DoorSign`. |

Changing the password locks out the old login: Protect stops recording until the camera is adopted again with the new one.

#### Sound

Off in every room until someone turns it on. With it on, each door sign sends its microphone with its picture, and Protect records the hallway's sound.

> **CHECK BEFORE YOU TURN IT ON**
>
> A door sign hears the conversations in the hallway. In many US states, recording a conversation needs the consent of everyone in it, which recording video does not. Check the law where the room is, and put up a notice by the door that sound is recorded.

1. On the room's touch screen: **Options › Settings › Devices**, turn on **Door sign camera sound**. It applies at once: each door sign turns its microphone on at its next check-in (within 5 seconds), and its camera starts again, so Protect reconnects.
2. **Update** the door sign on **Settings › Software** to version 4 or later.
3. In Protect, **remove the door sign's camera and adopt it again** (user `camera`, the password): Protect reads whether a camera has sound only when it adopts it.
4. Play back a recording to check the sound. In Protect's default stream mode the camera has no Enable Audio switch, and Protect records the sound as it comes. If a recording has none, set the camera's stream compatibility mode to **Improved** and turn on **Enable Audio**: Protect then converts the sound on the console, which uses some of its processing. Going back to the default mode means adopting the camera again.

Turning it off stops the microphone at the next check-in, and the app takes its own microphone permission away again.

| The card says | Meaning | Fix |
| --- | --- | --- |
| Sound off: Android has not granted the app the microphone | The panel hasn't taken the permission yet. | Wait for the next check-in; if it stays, reboot the panel from the card. |
| Sound off: Android would not let the app use the microphone: … | Android refused the camera service the microphone. The picture carries on. | Reboot the panel from the card. |
| Sound off: the microphone did not start: … | The microphone or its encoder failed. The picture carries on; the sound is tried again when the camera next starts. | Reboot the panel from the card. If it stays, read `adb logcat -s DoorSign`. |

## Building and releasing the app

*One signing key for every panel, and a GitHub release per version*

| Need | Version |
| --- | --- |
| Java | JDK 17 |
| Android SDK | Platform 34, build-tools 34.0.0, NDK 26.3.11579264, CMake 3.22.1 |
| Gradle | Wrapper 8.7, Android Gradle Plugin 8.5.2 (use `./gradlew`) |
| Targets | compileSdk/targetSdk 34, minSdk 26, ABIs arm64-v8a and armeabi-v7a |
| Source | `android/door-sign`, package `to.croom.doorsign` |

### The release signing key (once, for every panel)

```
keytool -genkeypair -v -keystore door-sign-release.jks -alias door-sign \
    -keyalg RSA -keysize 4096 -validity 36500
```

> **GUARD THIS KEY**
>
> Keep door-sign-release.jks and its passwords out of git (it is ignored) and in the office password manager, with a second copy offline. Android only installs an update signed with the same key. If the key is lost, every panel must be factory reset and set up again.

### Build

Give Gradle the key in `android/door-sign/keystore.properties` (never committed), or in the environment variables `DOOR_SIGN_KEYSTORE`, `DOOR_SIGN_KEYSTORE_PASSWORD`, `DOOR_SIGN_KEY_ALIAS`, `DOOR_SIGN_KEY_PASSWORD`. The file wins.

```
storeFile=/path/to/door-sign-release.jks
storePassword=…
keyAlias=door-sign
keyPassword=…
```

```
cd android/door-sign
echo "sdk.dir=$ANDROID_HOME" > local.properties     # once
./gradlew testDebugUnitTest assembleRelease
# → app/build/outputs/apk/release/app-release.apk
```

- **No key, no install.** Without a key Gradle leaves `app-release-unsigned.apk`, which no panel installs.
- **Debug for development only.** `./gradlew testDebugUnitTest assembleDebug` gives `app-debug.apk`. Debug and release builds can't update each other (`INSTALL_FAILED_UPDATE_INCOMPATIBLE`).

### Publish a release the room can install

1. Raise `versionCode` (and `versionName`) in `app/build.gradle` and commit.
2. Build the release as above, then run `sha256sum app/build/outputs/apk/release/app-release.apk`.
3. On GitHub `ben-abeo/croom.to`, create a release tagged `door-sign-<versionCode>` (for example `door-sign-3`), titled with what changed.
4. Attach the APK named exactly `app-release.apk`, and put `SHA-256: <hex>` in the notes.
5. Publish it. Drafts and pre-releases are ignored. The TV Pi lists the newest 50 releases and needs HTTPS to github.com.

*You should now see "Newest: version N" on the door sign's card on the touch Pi, and Update installs it.*

## How it works

*A locked WebView kiosk polling the TV Pi every 5 seconds*

### The app

- **A WebView kiosk.** `MainActivity` is both the launcher and the HOME app, and holds one full-screen WebView loading the sign URL. JavaScript and DOM storage on, screen kept on, system bars hidden (plus the panel's `elc.view.hide` broadcast at resume, 2 s later, on focus and on each page load). Back does nothing.
- **Locked to its server.** Navigation off the sign's server (scheme, host, port) is blocked, and the page's `window.CrystalPanel.setLight(state)` bridge is obeyed only from that server.
- **Lock-task kiosk.** As device owner the app calls `setLockTaskPackages` and `startLockTask()` on every resume: no status bar, Home or Recents.
- **Load errors.** A failed load shows "Can't reach Crystal Meet at <url> … Trying again every 10 seconds." with the bar off. A crashed renderer is recreated.
- **Background threads.** `door-sign-check-in` posts every 5 s whatever the screen shows; jobs run on `door-sign-job`.
- **Logs.** Tag `DoorSign`: `adb logcat -s DoorSign`. Each light change logs "Light bar: free".

### Stored on the panel

| Preferences file | Keys |
| --- | --- |
| **door_sign** | `url`, `pin_salt`, `pin_hash` (SHA-256 of salt + ":" + PIN; the PIN is never stored) |
| **check_in** | `checkin_id` (32 hex), `checkin_token` (256-bit), last `job` and `wifi` change, never with passwords |

### The page and its calls to the TV Pi

`GET http://<TV Pi>:8080/sign` is served by croom's control service with no login: it is meant for the office LAN only. Any browser can open it to check what the sign shows.

| Call | When |
| --- | --- |
| GET /api/status | Every 5 s, 4 s timeout. Any failure shows "Sign not connected". |
| GET /api/calendar/events | Every 60 s, and at once when bookings change. On failure the last list stays. |
| GET /api/calendar/week | When This week is opened |
| GET /api/bookings/slots | During Book (`?date=YYYY-MM-DD`) |
| POST /api/bookings | Book: start, minutes, title, source "door sign" |
| POST /api/bookings/now | Use now: minutes, title, source "door sign" |
| POST /api/bookings/{id}/cancel | Cancel, after the second tap |

POSTs must be JSON (else 415). End is refused from the sign: 403 "End now is on the room's touch screen".

### Check-in protocol

```
POST http://<TV Pi>:8080/api/devices/checkin        every 5 s (connect 5 s, read 10 s)
X-Croom-Device-Token: <token>
{"id": "<32 hex>", "kind": "sign", "version": "2", "version_name": "2.0",
 "name": "<model>", "network": {…}, "job": <last report or null>, "cannot": {…}}

→ {"state": "pending", "interval": 5}
→ {"state": "allowed", "job": {"id", "type", "args"} | null, "interval": 5}
```

The interval is clamped to 2 to 60 seconds. On the TV Pi, devices live in `/var/lib/croom/devices.json` (mode 600, token stored hashed). An unreadable file is moved aside to `devices.json.bad-<time>` and devices must be Allowed again.

### Booking rules on the TV Pi

- **Limits.** 15-minute steps, 4 hours maximum, 7 days ahead, names up to 60 characters.
- **Booking hours.** `calendar.booking_start` and `calendar.booking_end` in `/etc/croom/config.yaml`, default 08:00 to 18:00, on the quarter hour. Changed through Settings they apply at once; after editing the file by hand, restart croom.
- **Offline.** Bookings are queued in `bookings.json` and the last calendar is kept in `calendar-cache.json`, both in `/var/lib/croom`. They sync when Google is back.

## Troubleshooting

*Two checks from a computer tell you whether it's the panel or the room*

Start with two checks from a computer on the panel's network: open `http://<TV Pi>:8080/sign` and `http://<TV Pi>:8080/api/status`. If both load, the problem is on the panel; if not, it is the TV Pi or the network. On the TV Pi: `sudo systemctl status croom` and `sudo journalctl -u croom -n 50`.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| "Can't reach Crystal Meet at http://…/sign", bar off | TV Pi off, croom stopped, wrong address or port, or the panel is on a network that can't reach the TV Pi (guest Wi-Fi, another VLAN). | Run the two checks above. Fix the address in setup (corner hold, PIN) using the IP address. Start croom. |
| "The server answered 500 … Door sign assets are missing." | `sign.html` missing on the TV Pi. | Reinstall or update Crystal Meet on the TV Pi. |
| Grey "Sign not connected" | The page loaded but /api/status fails or takes over 4 s (croom restarting, network blip). | Recovers within 5 s of the TV Pi answering. If it persists, check croom. |
| Sign shows colours, bar stays off | Not an SMT1019, or `/dev/ledjni` or the native library unavailable. | Use the setup test buttons. `adb logcat -s DoorSign`: look for "No light bar: /dev/ledjni does not open on this tablet" or "The light bar refused channel N". |
| Bar doesn't follow the sign | The page came from a different scheme, host or port than the configured address (a redirect). | Set the address to exactly the server the page is served from. |
| No network over the PoE cable | No Ethernet link: cable, injector or switch port. | `adb shell ip link show eth0`: NO-CARRIER means no link. Check the injector's data-in port, the switch port and PoE, try another 8-wire cable. Wi-Fi works meanwhile. |
| Android status bar shows at the top | The panel's SystemUI kept it after a restart. | Press Home, or `adb shell am broadcast -a elc.view.hide`. |
| Panel never appears under Devices waiting to join | No server set, TV Pi unreachable, refused, or 10 already waiting. | Read the check-in line in setup. Undo a Refuse (Section 06). |
| Card says "not the panel's device owner"; no Update, Reboot or Wi-Fi | Set up the old way, without `dpm set-device-owner`. | Factory reset and follow Section 04. |
| "Door sign didn't check in after updating" | No check-in within 5 minutes of installing. | Go to the panel; check it shows the sign and is on the network. No automatic rollback. |
| Update refused: "signed with another key" | APK signed with another keystore, or the panel runs a debug build. | Sign with the room key. A debug-installed panel needs a factory reset and a release install. |
| "The door sign has version N already" | versionCode not raised. | Raise versionCode and publish a new `door-sign-N` release. |
| "There is no door sign release N on GitHub" / "has no app-release.apk" / "Can't check for updates right now" | Release missing, misnamed, draft or pre-release; or the TV Pi has no internet. | Check the tag, the asset name and that it is published. Check the TV Pi reaches github.com. |
| "Could not join X within 90 seconds" | Wrong Wi-Fi password, or out of range. | Retype the password; Android doesn't tell apps a password is wrong. |
| "Joined X, but the door sign could not reach the TV Pi on it…" | That network doesn't route to the TV Pi. | Use a network that reaches the TV Pi. |
| `INSTALL_FAILED_UPDATE_INCOMPATIBLE` from adb | Installed app signed with a different key (other computer's debug key, or debug vs release). | Old-style panel: `adb uninstall to.croom.doorsign`, install, redo home app and setup. Device owner: factory reset. |
| `adb devices` shows unauthorized | The USB debugging prompt hasn't been accepted. | Accept it on the panel and tick Always allow. |
| `dpm set-device-owner` fails: accounts or users exist | Not freshly reset, or a Google account was added. | Factory reset; skip adding an account. |
| Book and Use now missing | No booking calendar set up, the TV Pi unreachable, or (Use now) the room isn't free. | Share the room calendar with the service account with **Make changes to events**; check with `croom --check-calendar -c /etc/croom/config.yaml`. |
| "Calendar offline since…", "Not in Google Calendar yet", "Double-booked with…" | Google unreachable; bookings are queued. | They sync by themselves. Resolve double bookings in Google Calendar. |
| Plain navy background | Plain chosen, or the photo failed to load within 20 s. | Pick another under Options › Door sign. |
| Forgotten PIN | — | See Section 05. |

## Reference

*Names, numbers, files and tests on two pages*

| What | Value |
| --- | --- |
| Package | to.croom.doorsign |
| Activities | .MainActivity (launcher and home), .SetupActivity |
| Device admin | to.croom.doorsign/.AdminReceiver |
| Sign URL | http://<TV Pi>:8080/sign |
| Check-in | POST /api/devices/checkin every 5 s, header X-Croom-Device-Token |
| APK download | GET /api/devices/apk/<versionCode>, headers X-Croom-Device-Id and X-Croom-Device-Token |
| Setup gesture | Hold top-left corner 5 s |
| PIN | 4 to 8 digits, no lockout |
| Retry after a load error | 10 s |
| LED device | /dev/ledjni, ioctl 161/162/163 (R/G/B), levels 0 to 15 |
| Panel broadcasts | elc.view.hide, elc.view.show |
| Start-up property | persist.sys.bootapp |
| Panel's own launcher | com.android.launcher3/.uioverrides.QuickstepLauncher |
| Releases | Tag door-sign-<versionCode>, asset app-release.apk, repo ben-abeo/croom.to |
| On the TV Pi | /var/lib/croom/devices.json, /var/lib/croom/door-sign/, /var/lib/croom/control-settings.json, /etc/croom/config.yaml |
| Timings | Online if seen within 20 s · job resent after 15 s · update watch 5 min · Wi-Fi safety net 90 s |
| Booking limits | 15-minute steps, 4 h maximum, 7 days ahead, 08:00 to 18:00 by default, 60-character names |

### Tests

63 plain JVM unit tests in `android/door-sign/app/src/test/java/to/croom/doorsign/`: Wi-Fi change (21), job runner (14), light bar (5), sign address (5), agent (4), PIN (4), identity (3), job (3), APK check (2), check-in (2). Every build command runs them first.

```
cd android/door-sign
./gradlew testDebugUnitTest
./gradlew testDebugUnitTest --tests to.croom.doorsign.LightBarTest   # one class
# report: app/build/reports/tests/testDebugUnitTest/index.html
```

On the server side, run the Python tests with pytest: `tests/unit/control/test_door_sign_api.py`, `test_settings_door_sign_page.py`, `test_booking_pages.py`, `test_bookings_api.py`, `tests/unit/calendar/test_bookings.py`, `tests/unit/settings/test_checkin.py`, `test_apk.py`.

### Source map

| Path | Holds |
| --- | --- |
| android/door-sign/app/src/main/java/to/croom/doorsign/ | MainActivity, SetupActivity, Kiosk, LightBar, Agent, CheckIn, JobRunner, Panel, WifiChange, ApkCheck |
| android/door-sign/app/src/main/cpp/ledbar.c | The light bar driver |
| src/croom/control/static/sign.html, sign.js, sign.css, booking.js | The /sign page |
| src/croom/control/service.py | The routes |
| src/croom/calendar/bookings.py | Booking rules |
| src/croom/settings/checkin.py, apk.py | Check-in registry, door sign releases |

