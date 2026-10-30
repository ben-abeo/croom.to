# Crystal Meet Door Sign (Android)

The door sign app for the SMT1019 wall panel (Apolosign / Hopestar, RK3576, Android 14). It shows the room's
`/sign` page full screen and colours the light bar round the panel from the room's state:

| Sign shows | Light bar |
|---|---|
| Free | green |
| Soon (a booking within 10 minutes) | yellow |
| In use / booked | red |
| Not connected, loading | off |

The page tells the app its state through `window.CrystalPanel.setLight(state)` (`src/croom/control/static/sign.js`,
`setLight`); the colours live only in the app (`LightBar.levels`). In a browser there is no `CrystalPanel` and the
sign is unchanged.

## How the light bar is driven

The panel's light controller is `/dev/ledjni`. The panel's own LED test (`com.elc.smt_test`, `libjnielc.so`) opens
it read-only and sends one `ioctl` per channel, 161 red, 162 green and 163 blue, with a level from 0 to 15 as the
argument. `app/src/main/cpp/ledbar.c` does the same. On a tablet without `/dev/ledjni` the app shows the sign with
no lights.

## Build

Needs JDK 17 and the Android SDK (platform 34, build-tools 34.0.0, NDK 26.3.11579264, CMake 3.22.1).

```bash
cd android/door-sign
echo "sdk.dir=$ANDROID_HOME" > local.properties
./gradlew testDebugUnitTest assembleDebug
```

The APK is `app/build/outputs/apk/debug/app-debug.apk`, signed with the debug key, for development. A panel that updates itself
from Settings runs a release build signed with the room key (see "Releases and the signing key").

## Install on a panel

With USB debugging on and the panel connected:

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell cmd package set-home-activity to.croom.doorsign/.MainActivity
adb shell setprop persist.sys.bootapp to.croom.doorsign/to.croom.doorsign.MainActivity
```

The app is then the panel's home app: it starts with the panel and Home comes back to it.

On first start the setup screen asks for the Crystal Meet server (for example `10.0.0.5:8080`; the app adds
`http://` and `/sign`) and a PIN of 4 to 8 digits.

## Setup and leaving the sign

Hold the top-left corner of the sign for 5 seconds and enter the PIN. Setup has the server address, a new PIN, buttons
to test the light bar and **Exit to Android**, which opens the panel's own home screen (Launcher3) with its status bar;
Home brings the sign back.

The panel's SystemUI keeps its status bar up after a restart; the app hides it with the panel maker's `elc.view.hide`
broadcast and shows it again with `elc.view.show` on Exit to Android.

## Room settings: the app as the panel's device owner

Settings on the touch Pi (Options › Settings, behind the room's PIN) reboots the panel, changes its Wi-Fi and updates this app,
without anyone at the panel, when the app is the panel's **device owner**. Android allows a device owner only on a panel with no
accounts on it, so it is set once per panel, after a factory reset. Without it the sign and its light bar work as before, and the
app still checks in with the room, telling Settings which jobs it can't do ("The door sign is not the panel's device owner: see its
setup guide").

### Set up a panel (once)

1. **Factory reset** the panel (Android Settings › System › Reset options › Erase all data). Go through Android's first-start
   screens without adding a Google account, turn on Wi-Fi (or plug in Ethernet), then turn on USB debugging (Settings › About
   tablet, tap Build number seven times; Settings › System › Developer options › USB debugging).
2. **Install the release APK** (signed with the room key: see "Releases and the signing key"; a panel set up with a debug
   build cannot update itself to a release):

   ```bash
   adb install -r app-release.apk
   ```

3. **Make the app the device owner:**

   ```bash
   adb shell dpm set-device-owner to.croom.doorsign/.AdminReceiver
   ```

   Android answers `Success: Device owner set to package to.croom.doorsign`. If it says the device already has accounts or
   users, the reset was not complete: do step 1 again.
4. **Make it the home app** and start it, as in "Install on a panel" above:

   ```bash
   adb shell cmd package set-home-activity to.croom.doorsign/.MainActivity
   adb shell setprop persist.sys.bootapp to.croom.doorsign/to.croom.doorsign.MainActivity
   adb shell am start -n to.croom.doorsign/.MainActivity
   ```

5. In the app's setup, enter the room's address and a PIN. The app checks in with the room every 5 seconds.
6. On the touch Pi, open **Options › Settings › Software** and tap **Allow** beside the door sign under **Devices waiting to
   join**. Its card then shows its version, the newest release, **Update** and **Reboot**; Settings › Network shows its Wi-Fi.

The panel then runs the sign in Android's lock-task kiosk: no status bar, Home or Recents to leave it by. Setup (hold the top-left
corner, then the PIN) still has **Exit to Android**, which ends the kiosk; Home brings the sign, and the kiosk, back. Setup also says
whether the app is the device owner and where its check-in is ("Waiting to join: tap Allow…", "Joined the room at …").

To take the device owner away again (to give the panel to something else): factory reset it.

### What the room can do with it

- **Reboot** (Settings › Software): Android's `DevicePolicyManager.reboot`. The card says "Rebooted" when the panel is back.
- **Wi-Fi** (Settings › Network): look for networks, join one (with its password, or a hidden one by its name), forget one. The
  safety net is the TV Pi's and the touch Pi's: the panel keeps the network it is on, joins the new one, and must reach the TV Pi
  (`GET /api/status`) on it within 90 seconds; otherwise it goes back and says why ("Joined New Net, but the door sign could not
  reach the TV Pi on it within 90 seconds. The door sign is back on Office"). A panel that restarts during those 90 seconds goes
  back when the app starts. Android does not tell an app other than its own Settings that a password was wrong, so a wrong password
  shows as "Could not join … within 90 seconds".
  Android saves a password typed for a network it knows over the one it has, and an app can't read the old one back to put it
  back, so the panel refuses those: a password for the network it is on ("This door sign is on Office now: join another network
  first"), and for any other network it has saved ("Forget Guest first, then join it again"; joining a saved network with the
  password field left empty uses its saved password and is allowed).
- **Update** (Settings › Software, and Update all): see "Releases" below. The TV Pi downloads the release's APK from GitHub,
  checks it and keeps it in its data directory (`door-sign/`); the panel downloads it from the TV Pi
  (`GET /api/devices/apk/<versionCode>`, with its id and token), checks its size and SHA-256 against the job, that it is this app
  (`to.croom.doorsign`), the version the job names and newer than the one installed, and signed with the same key, and only then
  installs it through Android's `PackageInstaller` without asking. Updates from Settings need Android 12 or newer on the panel (the
  SMT1019 runs 14): before it, Android does not give an APK's signers for that check. Android restarts the app with the new version, which reports
  "Updated to version N". There is no going back (Android does not install an older version over a newer one): a panel that has not
  checked in within 5 minutes of installing shows "Door sign didn't check in after updating", and someone looks at the panel.

The check-in is the touch Pi's protocol (`POST /api/devices/checkin`, kind `sign`, the version being the app's `versionCode`); the
id and token are made by the app at its first start and kept in its private storage. Clearing the app's data makes a new id, which
asks to join again (a refused id stays refused).

## Releases and the signing key

Every release APK must be signed with the **same key**: Android installs an update only over an app signed with the same key, and
the app checks it before it installs. Debug builds (`assembleDebug`) keep using the debug key for development; they cannot update a
panel that runs a release, nor be updated to one.

### Make the key (once, for every room)

```bash
keytool -genkeypair -v -keystore door-sign-release.jks -alias door-sign \
    -keyalg RSA -keysize 4096 -validity 36500
```

Keep `door-sign-release.jks` and its passwords **out of the repository** (`.gitignore` refuses `*.jks`, `*.keystore` and
`keystore.properties`): in the office password manager, with a second copy offline. A lost key means every panel has to be reset
and set up again to take a release signed with a new one.

### Build a release

Give Gradle the key in `android/door-sign/keystore.properties` (never committed):

```properties
storeFile=/path/to/door-sign-release.jks
storePassword=…
keyAlias=door-sign
keyPassword=…
```

or in the environment (`DOOR_SIGN_KEYSTORE`, `DOOR_SIGN_KEYSTORE_PASSWORD`, `DOOR_SIGN_KEY_ALIAS`, `DOOR_SIGN_KEY_PASSWORD`). Then:

```bash
./gradlew testDebugUnitTest assembleRelease
```

The APK is `app/build/outputs/apk/release/app-release.apk` (without a key Gradle leaves `app-release-unsigned.apk`, which no panel
installs).

### Publish it

1. Raise `versionCode` (and `versionName`) in `app/build.gradle` for every release, and commit it.
2. Build the release as above.
3. On GitHub (`ben-abeo/croom.to`), make a release tagged **`door-sign-<versionCode>`** (for example `door-sign-3`), titled with
   what changed, and attach the APK as **`app-release.apk`**. GitHub states each asset's SHA-256, which the TV Pi checks; a line
   `SHA-256: <hex>` in the release's notes is checked too (`sha256sum app-release.apk`). Drafts and pre-releases are not offered.

The Software page lists the newest release and the ones between the panel's version and it; **Update** installs the newest.

### Hands-on checklist (a panel)

- `adb shell dpm set-device-owner …` succeeds after a reset; the sign starts in the kiosk (no status bar, Home and Recents do
  nothing); Exit to Android leaves it, Home brings it back.
- The panel appears under Devices waiting to join; Allow; its card says "Checked in just now" and its version.
- Reboot: the panel restarts and the card says "Rebooted".
- Network: Change Wi-Fi lists the networks; join one the TV Pi is reached on: kept. Join one it is not (a guest network): back on
  the old one within about 2 minutes, in words. Pull the panel's power during the 90 seconds: back on the old one after it starts.
- Retype the password of the network the panel is on, wrongly: the panel refuses ("This door sign is on … now: join another
  network first") and stays connected, its saved password unchanged.
- Update to a newer release: the sign comes back by itself on the new version, the card says "Updated to version N". An APK signed
  with another key, or an older version, is refused in words and nothing is installed.
- A panel without device owner (a debug build installed the old way): the sign works, the card says it is not the device owner, and
  Reboot, Update and Change Wi-Fi are not offered.

## Testing over USB

`adb reverse tcp:8080 tcp:8080` lets the panel reach a Crystal Meet server on the computer at `127.0.0.1:8080`
(set that as the server in setup). `adb logcat -s DoorSign` shows each light change (`Light bar: free`).
