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

The APK is `app/build/outputs/apk/debug/app-debug.apk`.

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

## Testing over USB

`adb reverse tcp:8080 tcp:8080` lets the panel reach a Crystal Meet server on the computer at `127.0.0.1:8080`
(set that as the server in setup). `adb logcat -s DoorSign` shows each light change (`Light bar: free`).
