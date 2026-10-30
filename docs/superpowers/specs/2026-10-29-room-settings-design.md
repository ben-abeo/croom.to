# Room settings on the touch Pi

**Date:** 2026-10-29 · **Status:** agreed with Ben in conversation; built at his word ("Just build it all") without a separate review
stop · **Branch:** `room-settings` off `main` (18baa20) · **Builds on:** the room page and Options (specs 2026-09-24, 2026-10-12), the
TV remote and touchpad (2026-10-13), Google Meet as a signed-in room (2026-10-07), the door sign app (2026-10-13), Laptop mode
(2026-10-27).

## 1. What this is for

Ben: "have all settings built directly into the touch Pi so that you don't have to set and change settings through SSH", the Google
sign-in included, "done on the touch Pi and prompted to the TV". Today a room is set up and changed over SSH: `config.yaml`, the
credential files, `croom --sign-in-meet` with a keyboard on the Pi and the service stopped, the `--check-…` commands, `pip` updates,
`systemctl restart`, Wi-Fi, `journalctl`. The touch Pi and the door sign panel are updated by hand too.

Success: a person with the PIN, at the room's touch screen (or a laptop on the office network), can change every setting, upload the
room's keys, sign the room in to Google on the TV, run the checks, read the log, restart or reboot, update the TV Pi, the touch Pi and
the door sign, and change any of their Wi-Fi, without a terminal, and none of it can leave a room unreachable without its putting
itself back.

## 2. Decisions (Ben, 2026-10-29)

- **Scope: everything.** Every section of `config.yaml`, the Google sign-in, health checks, logs, restart and reboot, software updates
  and Wi-Fi, for the **TV Pi, the touch Pi and the door sign**.
- **One design, built in phases** (section 9), each usable on its own.
- **A PIN, from any device on the network.** Options › Settings asks for it; the first time, it asks to choose one (4 to 8 digits).
  Unlocked for 5 minutes after the last request from that screen; **Lock** ends it; 5 wrong tries lock it for 10 minutes. The
  everyday controls (camera framing, screensaver, door sign look, TV remote, key click) stay outside the PIN, as today.
- **The door sign app becomes the panel's device owner** (one `adb` command per panel, once, after a factory reset): silent self-
  updates, reboot and Wi-Fi from Settings, and Android's lock-task kiosk.
- **Updates on demand**, from `main` on GitHub (`ben-abeo/croom.to`): each device shows its version, the newest, and the commit
  titles between; **Update** per device and **Update all**; each device keeps its previous version and rolls back by itself if it
  does not come back healthy (the door sign excepted: Android cannot install an older version over a newer one).
- **Approach:** Settings is part of the room page served by the TV Pi; privileged work goes to a root helper on the TV Pi with a fixed
  list of jobs; the touch Pi and the door sign **check in** with the TV Pi (no inbound ports on them).

## 3. What people see

**Options › Settings** (a new menu item, icon `settings`) opens the PIN pad, then the Settings menu. A **Lock** button and the time
left sit in its top bar. The same pages work in a laptop browser at the room's address (no on-screen keyboard there).

| Page | Contents |
|---|---|
| **Room** | Name, location, company, time zone (a list of the system's zones) |
| **Calendar** | Room calendar address; Google service-account key (Upload, From USB stick; shows "Key for <client_email>, saved <date>"); booking hours (start, end) |
| **Meetings** | Platforms (Zoom, Google Meet, Teams, Webex); camera on at join; microphone on at join; kiosk; dialogs answered by the room (list); reactions (list); Zoom SDK keys (Upload, From USB stick, or typed: key and secret); **Sign in to Google** and the account the room is signed in as |
| **Devices** | Speaker (the system's speakers, or Automatic); microphone (likewise); camera (the system's cameras, or Automatic); camera travel time; camera preset names |
| **TV** | Opens Options › TV (unchanged) |
| **Laptop switch** | On or off; GPIO pin; switches when the pin goes low; press length; USB id of the camera; wait for a move; **Test the switch** |
| **AI & dashboard** | AI features (the `ai` section's switches and backend); dashboard on or off, address, enrollment token |
| **Network** | One card per device (TV Pi, touch Pi, door sign): connection (Wi-Fi or Ethernet), network name, signal, address; **Change Wi-Fi** (a scan list, a password field, Join) and **Forget** |
| **Check** | Calendar, TV, Laptop switch, Google sign-in, Zoom keys: one button each; the result in the words the `--check-…` commands print |
| **Logs** | The room software's log, newest first; All / Warnings and errors; Load more |
| **Software** | One card per device: its version, the newest on GitHub, the changes between; **Update**; **Update all**; **Restart room software**; **Reboot** per device; **Devices waiting to join** (Allow / Refuse) |
| **Security** | Change the PIN |

**Mic level (Ben, 2026-10-29: "increase microphone volume for when people say they can't hear you"):** the Sound panel, in a
meeting and outside one, gets a **Mic level** slider (0 to 150, where 100 is the device's normal level) under the speaker's, with Quieter
/ Louder (5 at a time). It is an everyday control, outside the PIN. It drives the room microphone's PipeWire source (`audio.input_device`,
or the default source) with `wpctl`, as the speaker slider drives its sink. The chosen level is kept in the control settings and put
back at each join and every 5 seconds during a meeting, because the meeting apps' automatic gain control in the browser moves the
source's level by itself. "No microphone found" when there is no source.

**Saving a page:** every field is checked; a refused value says why beside it, and nothing is saved. A setting the room applies at once
says **Saved**; one that needs the room software to restart says **Saved: applies after a restart**, and the page shows **Restart
now**. Restart, reboot, update and sign-in are refused during a meeting: "Leave the meeting first".

**Sign in to Google** (Meetings page): the TV's meeting browser closes and Google's sign-in page fills the TV; the touch Pi shows the
touchpad (Room Pi mouse) with a **Type on the TV** keyboard and **Done** / **Cancel**. Done checks that Google kept the session
("Signed in as conference-a@crystalpm.com"), closes the sign-in browser and brings the room back; Cancel, or 15 minutes without a
touch, closes it and changes nothing.

## 4. Storage and applying

- **`/etc/croom/config.yaml` stays the one source of truth.** Settings sends the whole new config to the root helper, which checks it
  (it loads with `Config.from_dict`, and every field the settings schema knows is within its range), writes it beside the old one and
  renames it into place (owner `croom`, mode 640), after moving the last versions along: `config.yaml.1` … `config.yaml.5`. Comments
  in a hand-written file are lost on the first save (the README holds the explanations).
- **A settings schema** (`croom.settings.schema`) lists every setting the pages edit: its config path, label, page, kind (text, number,
  switch, choice, list, time, secret), range or choices, and whether it is **live** or **restart**. Live today: booking hours, camera
  preset names (and what Options already saves in its own files: the TV selection, screensaver, door sign look, key click, camera home
  and presets, which keep their files). Everything else is restart.
- **After a restart the helper watches the room come back**: if `GET /api/status` does not answer within 2 minutes, it puts back
  `config.yaml.1`, restarts again, and records why; Settings shows "Your last change was undone: the room did not start with it".
- **Credential files:** Upload (multipart, from a laptop) or From USB stick (the helper lists `*.json` on mounted USB sticks under
  `/media` and reads the one chosen). Checked before saving: a Google key is `"type": "service_account"` with `client_email` and
  `private_key`; a Zoom file has the keys the Zoom provider reads. Saved under `/etc/croom/` (`google-service-account.json`,
  `zoom-credentials.json`), owner `croom`, mode 600; the config's path is set to it. The page never shows a key again, only what it is
  and when it was saved.

## 5. The root helper (`croom-admin`)

A systemd service running as root, listening on a Unix socket `/run/croom-admin.sock` (owner root, group `croom`, mode 660): one JSON
request per connection, one JSON answer. It never runs a shell; every job checks its arguments.

| Job | Does |
|---|---|
| `write_config {yaml}` | the check, the rotation, the write (section 4) |
| `save_credential {kind, content}` | the check, the write, mode 600 |
| `usb_files` / `read_usb_file {path}` | `*.json` under `/media/*/*` only |
| `restart` | `systemctl restart croom`, then the health watch (section 4) |
| `reboot` | `systemctl reboot` |
| `journal {lines, level, before}` | `journalctl -u croom -o json` |
| `wifi_status` / `wifi_scan` / `wifi_connect {ssid, password}` / `wifi_forget {ssid}` | NetworkManager (`nmcli`), with the fallback of section 7 |
| `update {commit}` / `update_status` / `rollback` | section 6 |
| `version` | the commit this TV Pi runs |

The control service is its only client (`croom.admin.client`), and only behind the PIN. A room without the helper (a developer's
machine) shows the privileged pages as "Not available on this device".

## 6. Updates

- **Versions** are commits of `main` on GitHub. The TV Pi asks GitHub's API for the newest commit and the commit titles between the
  running one and it (cached 10 minutes; "Can't check for updates right now" when GitHub does not answer).
- **TV Pi:** releases live in `/opt/croom/releases/<commit>/` (a venv each); `/opt/croom/venv` is a link to the running one (the
  installer moves an existing venv into a release on first run). `update` builds the new release beside the running one (`pip install
  git+…@<commit>`; progress shown), switches the link, restarts, and watches: not answering normally within 3 minutes → the link goes
  back and the room restarts on the old release. Two releases are kept. A build that fails or is cut off never touched the running one;
  its folder is removed at the next start.
- **Touch Pi:** its agent fetches the commit into a second checkout, runs the kiosk setup from it, records the previous one, reboots;
  if it has not checked in with the new version within 5 minutes after the reboot, it switches back and reboots.
- **Door sign:** each door sign release is an APK attached to a GitHub release (`door-sign-<versionCode>`). The TV Pi downloads it, the
  panel downloads it from the TV Pi and installs it silently (device owner, `PackageInstaller`); no rollback, and a panel that has not
  checked in with the new version within 5 minutes shows "Door sign didn't check in after updating".

## 7. The touch Pi and the door sign: check-in

- `POST /api/devices/checkin` from each device every 5 seconds: its id, kind (`touch`, `sign`), version, network status, the result of
  its last job. The answer is its next job, if any. Unknown devices get a pending entry, shown on the Software page; **Allow** (behind
  the PIN) gives the device a token (stored hashed on the TV Pi); from then on its check-ins must carry it. **Refuse** removes it.
- **Jobs:** `reboot`, `update {commit | apk url}`, `wifi_scan`, `wifi_connect {ssid, password}`, `wifi_forget {ssid}`. A device does
  one at a time and reports done or failed with words.
- **The touch Pi agent** (`croom_kiosk_agent`, standard-library Python, run as root by systemd) is installed and paired by
  `install-kiosk.sh` (which already knows the room's address).
- **The door sign app** checks in the same way, as device owner: reboot (`DevicePolicyManager.reboot`), Wi-Fi (`WifiManager` as
  device owner), self-update (`PackageInstaller`), lock-task kiosk. Its setup guide gives the factory reset and the `adb shell dpm
  set-device-owner` command.
- **Wi-Fi safety net (every device):** the device keeps its current connection's profile, joins the new network, and within 90 seconds
  checks it can do its job — the touch Pi and the door sign reach the TV Pi; the TV Pi reaches its gateway and sees the touch Pi check
  in — and otherwise goes back to the old network and reports why. The TV Pi should have a fixed address on the router (the setup
  guide says so already), so the others keep finding it.

## 8. When things go wrong

| What happens | What the room does |
|---|---|
| A refused value | Nothing saved; the field says why |
| A config that passes the check but the room does not start with | The helper puts back `config.yaml.1` after 2 minutes and says so |
| Restart, reboot, update or sign-in in a meeting | "Leave the meeting first" |
| A forgotten PIN | Reset only at the TV Pi: delete `settings-pin.json` in the data directory (the guide says so) |
| A file that is not what it is uploaded as | Refused, naming what it is |
| Sign-in abandoned | Cancel or 15 minutes: the sign-in browser closes, the room's browser comes back, nothing changes |
| GitHub unreachable | Software says so; everything else works |
| An update that fails or is cut off | The running version untouched; the half-built one removed |
| A device that stops checking in | "Not seen since 10:42"; its jobs wait |
| No helper (not a room device) | The privileged pages say "Not available on this device" |

Accepted risk: the room page is plain HTTP on the office network, so the PIN and an uploaded key cross the LAN unencrypted, as the
rest of the room page does today. HTTPS on the Pis is a separate project.

## 9. Phases

1. **Settings shell:** the PIN, the helper (config, restart), the schema, the **Room** page.
2. **Every config page** and the credential files; the **Mic level** slider.
3. **Sign in to Google on the TV**, with the TV keyboard.
4. **Check and Logs.**
5. **TV Pi software:** update with rollback, reboot.
6. **Check-in and the touch Pi agent:** reboot, update, Wi-Fi.
7. **TV Pi Wi-Fi.**
8. **The door sign as device owner:** check-in, reboot, Wi-Fi, updates; its setup guide.

## 10. Testing

Unit tests replace everything privileged with stand-ins (the socket server runs on a temporary path; `systemctl`, `journalctl`,
`nmcli`, `pip`, `git`, GitHub, `/media`, uinput are injected), so nothing needs root or touches the machine running the tests. The
browser tests cover the PIN, every page, uploads and the sign-in screen. The door sign app gets Java unit tests for check-in and its
jobs. Each phase ends with a hands-on checklist for the rooms (in the README's section for it).

## 11. As built (2026-10-30)

Built on `room-settings` in twelve tasks; the plan's deviations, and the rulings that change what people see:

- **The PIN.** Leaving the Settings area (the idle return, a meeting, any Options screen but TV, a reload) locks it, stricter than the 5
  minutes alone, for a shared screen. The wrong-try count and the lock-out live in memory: a restart of the room software clears them.
  Changing the PIN, or deleting `settings-pin.json`, ends every other screen's unlock. **Restart now**'s wait renews the unlock while it
  watches (up to about 10 minutes), so its answer is not lost to the lock.
- **Options** has six items, three and three: Mic level and Settings joined it.
- **Mic level is a screen of its own** (from Options, and from a **Mic** button on the meeting dock's volume line), not a slider under
  the speaker's: a third dock row overflowed the 1280 by 800 meeting screen. Its level is put back at join and every 5 seconds as §3 says,
  but not while the laptop has the MeetUp.
- **Saving.** A hand-written value the schema refuses blocks every save until it is put right on its own page; the message names it.
  `control.enabled`, `control.host` and `control.port` are refused (the room page would be lost). The device fields offer what the TV Pi
  finds plus the value in the file, and accept any one line (the helper cannot see the devices). `ai.backend` is auto, hailo, coral, intel,
  nvidia or cpu.
- **The helper** answers restart and reboot first and acts after (a restart waited on its own answer and stalled 60 s); its record
  (restarts, the last write, updates, the release kept before, Wi-Fi) is in `/var/lib/croom-admin`, root's alone, not the data directory.
  Releases are root's, and the helper runs or switches to a release only when it is root's and writable by root alone.
- **Credentials.** Typing the Zoom SDK key and secret is offered beside upload and USB, keeping the room's server-to-server values.
- **Sign in to Google.** Cancel, the timeout, the browser closing by itself and the agent stopping put the profile's cookies back as they
  were. Typing on the room Pi is accepted only while a sign-in is open. The installer sets the room Pi's keyboard layout to US (Pi OS
  starts with GB, where the US key codes cannot type `@`); a reboot applies it.
- **Check** is not refused in a meeting (no check disturbs one); the TV check reads the room's own connection, the calendar and Zoom checks
  read the config file on disk. The agent now writes each journal line with its priority, so **Warnings and errors** works.
- **Updates.** The helper installs only a commit on `main` (it asks GitHub's compare API) and proves the new release's helper starts before
  the switch; it restarts itself after a successful update; after a reboot its watch waits for croom to be active first. An update is
  refused while a saved config waits for a restart ("Restart now first, then update"). **Update all** queues the devices first and the TV
  Pi last. **Test the switch** moves the MeetUp over and back.
- **Check-in.** The device makes its own token and sends it from its first check-in; **Allow** approves that id and token (no token crosses
  the LAN from the TV Pi). **Refuse** is remembered (403 from then on) and **Forget** was added, removing a device without remembering it;
  undoing a Refuse means a new identity on the device (or emptying `refused` in `devices.json`). The registry module is
  `croom.settings.checkin` (`croom.settings.devices` was the Devices page's).
- **Wi-Fi.** The Pis write NetworkManager's keyfile and `connection load` it rather than `nmcli device wifi connect`, so the password is
  never on a command line; the profile on trial has autoconnect off (a power cut comes back on the old network) and the trial counts only
  while the device stays on it; a proven network gets autoconnect on. The network left stays saved. nmcli runs with `LC_ALL=C`. The TV
  Pi's check is its gateway and a table Pi check-in after the switch (the gateway alone on Ethernet or with no table Pi joined), asked of
  the room page through `GET /api/devices/seen-since`, answered only to the TV Pi itself. Wi-Fi changes are refused in a meeting (looking
  is not). Neither limit is lifted: a device cannot be moved to a separate LAN through the page, and the TV Pi's Wi-Fi cannot change
  while its table Pi is offline.
- **The door sign** refuses a new password for the network it is on or another it has saved (Android saves it over the old one, which an
  app cannot read back); a wrong password shows as "Could not join … within 90 seconds". Updates from Settings need Android 12 or newer.
  Jobs a panel cannot do (not the device owner) are reported by the panel and not offered.
- **Moving a room from the release before** is by hand, once: the installer again on the TV Pi, `install-kiosk.sh` again on the table Pi,
  and a factory reset with the device owner command on each door sign panel (README, "Upgrade a room to room settings").
