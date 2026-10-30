# Crystal Meet

Crystal PM's conference rooms: a Raspberry Pi behind each TV joins Zoom and
Google Meet calls, a touchscreen on the table opens the room page to join and
control the call, a small screen by the door shows whether the room is free,
and one dashboard watches every room. It is a fork of
[croom.to](https://github.com/amirhmoradi/croom.to); the upstream README
follows this section. The code keeps upstream's `croom` names for the package,
the command, the service units and the folders under `/etc` and `/opt`.

## How the pieces fit

| Piece | Where it runs | What it does |
|---|---|---|
| Room device, the `croom` agent | A Raspberry Pi behind the TV, one per room, with no keyboard or mouse | Owns the one browser page on the TV: the screensaver between meetings, the meeting when Join is pressed. Reads the room's Google Calendar, serves the room page, the door sign and the TV page, reports to the dashboard. For an LG webOS TV it also pairs with the TV, keeps it on, and carries the table's remote and touchpad to it and to a virtual mouse on the Pi itself. A root helper beside it, `croom-admin`, does the few privileged jobs the room page's **Settings** asks for (the config, the keys, restarts, updates, Wi-Fi). |
| Room page `http://<device>:8080/` | Any browser on the office network: a tablet on the table, or a Raspberry Pi with the touch display set up by `installer/install-kiosk.sh --url http://<device>:8080/` | An idle screen (today's bookings, Join now, **Use now** and **Book room** to book the room in its calendar for an in-person meeting and **End now** for one in progress that was booked at the room's screens, a box to paste a Zoom or Meet link with its own on-screen keyboard, and an **Options** button for **Camera framing**, **Mic level**, the **Screensaver**, the **Door sign**, the **TV** remote, the keyboard's **Key click** and **Settings**, behind a PIN) and a meeting screen that appears on its own during a call. It draws only what the platform offers the room right now, as the platform has it, as a control deck: the meeting's card on the left (the time in the call in large type, how many people are in it, the chat that waits, whether the room is host, and **Leave** at its foot), and on the right Mute, Turn camera off, Raise hand and **Reactions** as tiles, a second set of **Captions**, the view (**Gallery view** or **Speaker view**), **Hide self view**, **Participants**, **Chat** and **Host** (only while the room is host or co-host), and at the foot a dock of the room's volume, **Camera** for presets and nudges and **Upcoming meetings**, with **Touchpad** and **TV** in the top bar; the dock's **Mic** button, on the volume's line, opens **Mic level**, as **Options** does outside a call. A Google Meet call offers Mute, Turn camera off, Raise hand and Captions, and no more (Leave, **TV** and the rest of the screen are as ever). A call on a platform that reports no controls at all keeps Mute, Turn camera off and Leave, and **TV**, which is the room's own and never depends on the platform. A line under the meeting's title says what the meeting has disclosed of itself (recorded, transcribed; two lines at most, cut short with an ellipsis), and a Zoom dialog that waits for a tap shows as a card above the controls with Zoom's own buttons. **Participants**, **Chat** (read only) and **Host** are screens of their own, each with **Back**, and each returns to the meeting screen after two idle minutes; Leave, Remove and End meeting for everyone ask for a second tap. Every button on the meeting screen has a [Lucide](https://lucide.dev) icon before its label (the buttons of a Zoom dialog's card excepted: their words are Zoom's), and so do Join, Options and the menu buttons. Every screen fits the table Pi's 10.1-inch display without the page scrolling (the bookings boxes scroll inside themselves; the kiosk runs Chromium at 1.5 times scale, 1280 by 800); an options screen left alone returns to the idle screen after ten minutes. The on-screen keyboard has three layers like an iPad's (letters, **123** for numbers and the symbols a link or a passcode needs, and **#+=**); Shift gives one capital and a double tap locks it; every key acts the moment a finger lands, so two thumbs typing fast lose nothing; a pressed letter shows a larger copy above its key; and each key clicks softly until **Key click off** is chosen under **Options**, then **Keyboard**. The TV remote opens from **Options**, then **TV** on the idle screen and from **TV** in the top bar on the meeting screen, beside **Touchpad**, which opens the full-screen touchpad on the room Pi's mouse in one tap; **Back** returns to the screen that opened it, and left alone it returns by itself after ten minutes (two on the meeting screen). It fits one screen: **Power** (a second tap turns the TV off), **Input** with the room Pi's input marked Crystal Meet, Home, Menu, volume and mute, channel up and down, a round d-pad with OK, Back, Exit, Info and Guide, the number keys with Go, the playback and colour keys, and **Forget TV**; in the remote's place a room with no TV set up shows **Find TVs** and how to set one up, a TV not yet paired shows **Pair** (and **Find TVs**), and a TV that no longer accepts the table shows **Forget TV** and **Pair**; under any of those cards **Full-screen touchpad** stays, so a room with no TV still has the room Pi's mouse. Under the keys, **Pointer** turns the d-pad into a trackpad for the TV's cursor, **Room Pi mouse** moves the mouse of the Pi behind the TV (a virtual mouse that the Pi's desktop takes like a USB mouse, so its pointer should appear on the TV; the first use in a room confirms it) and **Full-screen touchpad** gives the whole table screen to either, with a switch between them and **Done**, and returns to the TV screen on **Done** or after ten idle minutes. The TV hides its pointer, on the screensaver and in a meeting alike, but while the touchpad is driving the room Pi's mouse and for five seconds after. One finger moves, a tap clicks, two fingers scroll, a two-finger tap right-clicks the Pi, and press and hold, then move, drags. A blue border on the pad means a hold is armed: after a short rest the next move drags, a touch that moves sooner is a move for good, and after a rest a nudge under 8 pixels does nothing. When the link to the device drops, the pad says **Reconnecting…** and the page tries again after 1 second, then 2, then every 5 seconds. |
| Door sign `http://<device>:8080/sign` | A small screen by the door, PoE or Wi-Fi | The room's name, the practice's name beside the logo, the time and date, and today's next bookings, over a photograph of Austin under a navy overlay (nine backgrounds, chosen on the room page under **Options**, then **Door sign**). The status is a coloured rail down the left edge, a solid chip under the room's name and a ring round the booking in progress: green when free, amber ten minutes before a booking, red while in use or booked. **This week** shows the next seven days. **Use now** (while the room is free) and **Book** under the status book the room in its Google calendar for an in-person meeting: a name and a length in steps of 15 minutes, from this minute or from an open start time of today or the six days after it; a booking of the room's own that has not started has **Cancel** in Today. On an SMT1019 wall panel the Door Sign app (`android/door-sign`) shows the page and lights the panel's bar in the same colour (off while the sign is not connected). A strip of football scores for Texas, Michigan, Texas A&M and Missouri can run along the bottom (off by default; the device reads ESPN's public schedule feed for it). |
| TV page `http://<device>:8080/tv` | The room's TV, in the device's own browser | The screensaver between meetings: Information (name, status, next booking, clock), Quiet, Brand, or the bouncing Crystal PM logo. Chosen on the room page under **Options**, then **Screensaver**, and remembered; while someone frames the camera (**Options**, then **Camera framing**) it shows the camera instead. |
| Dashboard, `src/croom-dashboard` | A fourth Raspberry Pi (or any 64-bit Docker host) on the office network, from `deploy/dashboard/` | Fleet overview, device status, enrollment tokens on the Provisioning page. |

Neither screen is cabled to the Pi: each only needs a browser, power and the
network. The device joins nothing by itself; someone presses Join now.

## Set up a room

Follow the five guides in this order:

1. [Set up the Crystal Meet dashboard](docs/guides/crystal-meet-dashboard.pdf): a
   fourth Raspberry Pi on the office network runs the dashboard under Docker;
   one install command, a fixed address, nightly backups.
2. [Set up a Crystal Meet room](docs/guides/crystal-meet-room-setup.pdf): prepare
   the Pi, create the room on the dashboard, install with the room's config,
   first run, point the table screen and the door sign at the device and, for an LG TV, pair it and use its remote and touchpad.
   For a Raspberry Pi table screen, clone the repository on it and run `sudo bash installer/install-kiosk.sh --url http://<device>:8080/`: the room page opens full screen at every login, a Room controls icon on its desktop reopens it, and the table Pi's agent asks the room to let it join (tap **Allow** under **Options**, then **Settings**, then **Software**). From then on **Settings** updates the table Pi with the TV Pi: its agent runs the install-kiosk setup of the new version and reboots it, which is what puts a new kiosk command, and its 1.5 times scale, into effect (nothing relaunches the browser). A table Pi set up before room settings has no agent: once, pull the repository on the table Pi too, run the install-kiosk command again and reboot it ([Upgrade a room to room settings](#upgrade-a-room-to-room-settings)). The TV Pi gets a Start Crystal Meet icon on its desktop (over VNC) that starts the service if it ever stops.
3. [Connect Crystal Meet rooms to Google Calendar](docs/guides/crystal-meet-google-calendar.pdf):
   room resources in Google Workspace, one service account and key, share each
   room calendar with it, put the key and the calendar address on the device.
4. [Let Crystal Meet rooms join Google Meet](docs/guides/crystal-meet-google-meet.pdf):
   one Workspace user per room in a unit whose sessions never expire, and the
   one-time sign-in on each Pi, so Meet accepts the room instead of refusing an
   automated guest.
5. [Connect Crystal Meet rooms to Zoom](docs/guides/crystal-meet-zoom.pdf): a
   Meeting SDK app, its submission to Zoom's review (unlisted, needed only for
   meetings hosted by other accounts), a Server-to-Server app for tokens, one
   Zoom user per room, and the credentials file on the device.

For an SMT1019 wall panel at the door, set a new panel up as
[android/door-sign/README.md](android/door-sign/README.md#room-settings-the-app-as-the-panels-device-owner) says under
"Room settings: the app as the panel's device owner": a factory reset, the release APK over USB and the app made the panel's
device owner, so that the room can reboot, update and move it from **Settings**. The panel starts into the door sign, and
the light bar round it turns green, yellow or red with the room's status. The PDF guide
[Set up a Crystal Meet door sign panel](docs/guides/crystal-meet-door-sign-panel.pdf) predates the device owner and is out
of date until it is redone: follow the README for a new panel.

The commands the guides walk through, for reference:

```bash
git clone https://github.com/ben-abeo/croom.to.git && cd croom.to
cp deploy/rooms/room-1.yaml ~/room.yaml            # then edit the three REPLACE values, and the location if the room has one
sudo bash installer/install.sh --config ~/room.yaml --credentials ~/google-service-account.json --zoom-credentials ~/zoom-credentials.json
sudo systemctl start croom
/opt/croom/venv/bin/croom --check-calendar -c /etc/croom/config.yaml
/opt/croom/venv/bin/croom --check-zoom -c /etc/croom/config.yaml
/opt/croom/venv/bin/croom --check-tv -c /etc/croom/config.yaml     # as the desktop user: the TV's key is private to it; with no TV set up it lists the LG TVs on the network
/opt/croom/venv/bin/croom --check-laptop -c /etc/croom/config.yaml   # the laptop switch's pin and where the MeetUp is; --switch moves it once; service stopped first (croom holds the pin)
sudo -u pi DISPLAY=:0 /opt/croom/venv/bin/croom --sign-in-meet -c /etc/croom/config.yaml   # once per room, service stopped
sudo -u pi DISPLAY=:0 /opt/croom/venv/bin/croom --check-meet https://meet.google.com/abc-defg-hij -c /etc/croom/config.yaml   # service stopped first
```

Installer options: `--config FILE` installs a prepared room config as
`/etc/croom/config.yaml`; `--credentials FILE` installs a Google service
account key as `/etc/croom/google-service-account.json` and
`--zoom-credentials FILE` the Zoom credentials as
`/etc/croom/zoom-credentials.json`, both readable only by the service user;
`--enable-ui` and `--no-service` are upstream options; the
`CROOM_REPO` environment variable overrides the pip source, which defaults to
this fork's `main` branch. Run the installer with `sudo` from the desktop user
account: the service runs as that user so Chromium can use the TV and the
room's audio. It installs the agent as a release, `/opt/croom/releases/<commit>/venv`
(root's, readable by all), with `/opt/croom/venv` a link to it, Playwright's
Chromium into `/opt/croom/browsers`, a `croom.service` unit that waits for
the desktop before starting, and `croom-admin.service`, the root helper. It also
sets the room Pi's keyboard layout to US (`XKBLAYOUT="us"` in `/etc/default/keyboard`
and `XKB_DEFAULT_LAYOUT=us` in the desktop user's labwc environment), because
**Sign in to Google** types on the TV with US key codes and Raspberry Pi OS starts
with a GB layout, where Shift and 2 is `"` and not `@`; a reboot applies it.

Since room settings, everything in the commands above but the install is on the table screen, under **Options**, then **Settings**, behind the room's PIN: the keys (**Calendar** and **Meetings**, by upload or from a USB stick in the TV Pi), the Google sign-in (**Meetings**, on the TV, with the service running), the checks (**Check**) and the log (**Logs**); the commands still work from a terminal.

To update a room device, open **Options**, then **Settings**, then **Software** on the table screen: **Update** on a device's card, or **Update all**, installs the newest commit of `main` on GitHub on the TV Pi and the table Pi, each of which goes back by itself if it does not come back healthy, and the newest door sign release on the panel ([Software updates](#software-updates)). A room set up before room settings is moved to them once by hand, in the order [Upgrade a room to room settings](#upgrade-a-room-to-room-settings) gives. By hand, the installer can always be run again on the TV Pi, `cd ~/croom.to && git pull && sudo bash installer/install.sh`, then `sudo reboot`: it builds the newest `main` as a release beside the running one, as **Software** does, keeps the two newest, and leaves the room's config and keys as they are (the old `pip install --force-reinstall` steps are gone: `/opt/croom/venv` is now a link to a root-owned release, and a pip run into it would leave its recorded version wrong). The room guide's section Updating a room device says the same.

## Settings on the table screen

**Options**, then **Settings** holds every room setting that used to take SSH: the
config, the keys, the Google sign-in, the checks, the log, restarts and reboots,
software updates and Wi-Fi, for the TV Pi, the table Pi and the door sign panel.
The same pages work in a laptop browser at `http://<device>:8080/` (without the
on-screen keyboard). The everyday controls (Camera framing, Mic level, the
Screensaver, the Door sign's look, the TV remote, Key click) stay outside the PIN,
as before.

### The PIN

The first time, **Settings** asks to choose a PIN of 4 to 8 digits and to enter it
again. After that it asks for the PIN, and stays unlocked for 5 minutes after the
last thing done in it (the top bar says **Locks in 4 min**); **Lock** ends it at
once, and so does leaving the Settings area for the idle screen, a meeting or
another screen than **Options** and **TV**, or reloading the page. Five wrong PINs
lock the pad for 10 minutes ("Too many wrong PINs: try again in 10 minutes"); the
count lives in memory, so a restart of the room software clears it. **Security**
changes the PIN (the current one, then the new one twice), which ends every other
screen's unlock. The PIN is kept hashed in `settings-pin.json` in the data
directory, mode 600. **A forgotten PIN is reset only at the TV Pi**: over SSH,
`sudo rm /var/lib/croom/settings-pin.json` (with no restart: every open unlock ends
at once, and the next person to open **Settings** chooses a new PIN). The page is
plain HTTP on the office network, as the rest of the room page is, so the PIN and
an uploaded key cross the LAN unencrypted; HTTPS on the Pis is a separate project.

### The pages

| Page | What it holds |
|---|---|
| **Room** | The room's name, location, company (the name beside the logo on the door sign) and time zone, from the system's list of zones. |
| **Calendar** | The room calendar's address; the Google service-account key, by **Upload** (from a laptop) or **From USB stick** (a `.json` file at the top of a stick plugged into the TV Pi), shown afterwards only as "Key for <client_email>, saved <date>"; the booking hours. A file that is not what it is uploaded as is refused, naming what it is. |
| **Meetings** | The platforms, camera and microphone on at join, kiosk, the dialogs the room answers and the reactions it offers; the Zoom SDK keys (upload, USB stick or **Type the keys**); **Sign in to Google** and the account the room is signed in as. |
| **Devices** | The speaker, the microphone and the camera, from what the TV Pi finds now (or Automatic), the camera's travel time and the preset names. |
| **TV** | Opens **Options**, then **TV**, as it is; its **Back** comes back to Settings. |
| **Laptop switch** | Laptop mode on or off, the GPIO pin, the level it switches on, the press length, the camera's USB id and the wait for a move; **Test the switch**. |
| **AI & dashboard** | Upstream's AI switches and backend; the dashboard on or off, its address and the enrollment token. |
| **Network** | A card per device (TV Pi, each table Pi and door sign that joined): Wi-Fi or Ethernet, the network, the signal, the address; **Change Wi-Fi** (a list to pick from, **Look again**, **Hidden network**, a password field, **Join**) and **Forget** for a saved network. |
| **Check** | **Calendar**, **TV**, **Laptop switch**, **Google sign-in** and **Zoom keys**: one button each, and the answer in the words the `--check-…` commands print. The calendar and Zoom checks read the config file as it is now, so a key saved a moment ago is checked before the restart that puts it to use; the TV check looks through the room's own connection to the TV (it opens no second one), and the laptop check only looks. None is refused in a meeting, and each stops after 60 seconds. |
| **Logs** | The room software's log (`journalctl -u croom`, read by the root helper), newest first, 200 lines at a time with **Load more**; **All** or **Warnings and errors**. Under systemd the agent now gives each line its level, which the filter needs; lines written before this release stay at info in the journal, though the page still shows the level their text says. |
| **Software** | A card per device: its version, the newest on GitHub, the changes between, **Update**, **Reboot** (a second tap) and, for the TV Pi, **Restart room software** and **Switch back to** the version before; **Update all**; **Devices waiting to join**, with **Allow** and **Refuse**. |
| **Security** | Change the PIN. |

**Saving.** Every field is checked before anything is written; a refused value says
why beside it and nothing is saved. A value a hand-written config already holds and
the check refuses blocks every save, from any page, until it is put right on its own
page, which the message names ("AI & dashboard: AI backend: choose one of the list").
A setting the room applies at once (the booking hours, the camera preset names) says
**Saved**; any other says **Saved: applies after a restart**, and the page shows
**Restart now**. Restart, reboot, update, Wi-Fi changes and the Google sign-in are
refused during a meeting: "Leave the meeting first". Restart, reboot, update and
**Update all** are refused too while Laptop mode has the MeetUp, and a join is refused
while the TV Pi updates ("The room is updating: it restarts in a few minutes").

**Restarting, and the undo.** **Restart now** (and **Restart room software**) asks
the root helper to restart `croom`, and the helper then watches `GET /api/status`
for 2 minutes. The helper keeps `/etc/croom/config.yaml.good`, the config the room
last started with (root writes it, owner the room user, mode 640): the installer
writes it at every install and upgrade, and the helper each time a watch sees the room
answer. If the room does not answer and a saved change was waiting for the restart
when it was asked for (however long before: no time window), the helper puts
`config.yaml.good` back, keeps the config the room did not start with as
`config.yaml.1`, restarts again and records why, naming the change undone; the page
says "Your last change was undone: the room did not start with it". A change is undone
once only, and a room that does not come back with no change waiting is left as it is.
A save of only the settings the room applies at once (the booking hours, the camera
preset names) leaves nothing waiting. A reboot or a power cut after a save starts the
room with the change and no Restart now: the helper, starting, waits for systemd to
start `croom` (up to 5 minutes) and watches it the same way. On a real room the
restarted agent has forgotten the unlock, so the PIN pad comes up first.

### The root helper

`croom-admin.service` runs `python -m croom.admin` from the running release as root.
It listens on `/run/croom-admin.sock` (the room user's group, mode 660), takes one JSON
request per connection and answers `{"ok": true, …}` or `{"ok": false, "error": words}`.
It does a fixed list of jobs and refuses any other: `write_config` (the check, the
rotation to `config.yaml.1` … `.5`, the write, owner the room user, mode 640; and
`config.yaml.good`, the config the room last started with, which the restart watch
puts back),
`save_credential` (the key's check, then `/etc/croom/google-service-account.json` or
`zoom-credentials.json`, mode 600), `usb_files` and `read_usb_file` (`*.json` directly
under `/media/*/*/`, 64 KB at most), `restart` and `restart_status`, `reboot`,
`journal`, `version`, `update`, `update_status` and `rollback`, and `wifi_status`,
`wifi_scan`, `wifi_connect` and `wifi_forget`. It never runs a shell (every command is
an argument list) and checks every argument. Its own record (restarts, the last config
write, updates, the release kept before, Wi-Fi changes) is in `/var/lib/croom-admin`,
root's alone (mode 700), so the room's user cannot steer what root does. The room
software is its only client, and only behind the PIN; a machine without the helper (a
developer's) shows the privileged pages as "Not available on this device", and every
other page works.

### Software updates

Versions are commits of `main` on GitHub (`ben-abeo/croom.to`). The TV Pi asks
GitHub's API, without an account, for the newest commit and the commit titles between
each device's version and it, and keeps the answer 10 minutes; when GitHub does not
answer, **Software** says "Can't check for updates right now" and everything else
works.

- **The TV Pi.** **Update** builds the new version as a release beside the running one
  (`/opt/croom/releases/<commit>/venv`: a venv, pip from GitHub, the meeting browser,
  an import check, a check that its helper starts), with its progress on the card,
  then moves the `/opt/croom/venv` link to it in one step and restarts the room. If the
  room does not answer within 3 minutes the link goes back and the room restarts on the
  version before ("The update to … was undone: the room did not start with it"). A
  build that fails, or is cut off by the power, never touched the running release; its
  folder is removed at the helper's next start. Two releases are kept, and **Switch back
  to** goes to the one before, offered only when that release's own helper starts (its
  `python -m croom.admin --check`: a release from before room settings has none). The
  helper installs only a commit that is on `main`, and refuses an update during a
  meeting, a restart, a Wi-Fi change, or while a saved config waits for a restart
  ("Restart now first, then update"). Before it switches, it asks the room on the
  loopback whether a meeting is on (`GET /api/devices/busy`) and waits for it to end,
  up to 30 minutes, then goes on, saying so in its lines.
- **The table Pi.** Its agent (`croom-kiosk-agent.service`, standard-library Python,
  run as root from `/opt/croom-kiosk`) fetches `main` into its own clone, checks the
  commit is on it, unpacks it as a release beside the running one, runs that version's
  `install-kiosk.sh` and reboots. If it has not checked in with the new version within
  5 minutes of the reboot, it switches back and reboots again ("The update to … was
  undone: the touch Pi did not check in with it within 5 minutes").
- **The door sign.** Each release of the app is a GitHub release tagged
  `door-sign-<versionCode>` with the APK attached as `app-release.apk`. The TV Pi
  downloads and checks it; the panel downloads it from the TV Pi, checks its size,
  SHA-256, package, version and signing key, and installs it without asking. There is
  no going back (Android installs only a newer version), and a panel that has not
  checked in within 5 minutes of installing shows "Door sign didn't check in after
  updating".
- **Update all** queues the devices first and updates the TV Pi last, because its
  update restarts the room and the device jobs live in its memory.

### Devices that check in

The table Pi and the door sign check in with the TV Pi every 5 seconds
(`POST /api/devices/checkin`) and take their jobs from the answer: nothing listens on
them. A new device makes its own id and token and appears under **Devices waiting to
join**; **Allow** lets that id with that token in (the TV Pi keeps only the token's
hash, in `devices.json`), and every later check-in must carry it. **Refuse** removes
it and remembers the id, so it is turned away from then on; **Forget**, on a joined
device's card, removes it and remembers nothing, for a Pi or panel that was replaced
(if it is still there, it asks to join again). To take back a **Refuse**, give the
device a new identity: on a table Pi, `sudo rm /var/lib/croom-kiosk/identity.json &&
sudo systemctl restart croom-kiosk-agent`; for either, at the TV Pi,
`sudo systemctl stop croom`, empty the `"refused"` list in `/var/lib/croom/devices.json`,
and `sudo systemctl start croom`. A device that stops checking in shows "Not seen since
10:42 AM", and its jobs wait.

### Wi-Fi, with the safety net

**Change Wi-Fi** on a card moves that device to another network, and no change can
leave a device unreachable without its putting itself back. The device keeps the
network it is on, joins the new one, and within 90 seconds must do its job there: a
table Pi or a door sign must reach the TV Pi (`GET /api/status`); the TV Pi must reach
its gateway and see its table Pi check in again (the gateway alone when it is on
Ethernet or no table Pi has joined). Otherwise it goes back to the old network and
says why ("Joined New Net, but the touch Pi did not check in within 90 seconds. The TV
Pi is back on Office"). The new network is saved so that it does not connect by itself
until it is proven, so a power cut during the 90 seconds comes back on the old one;
the device finishes or undoes the change when it starts. On the Pis the password never
goes on a command line: the helper and the agent write NetworkManager's own keyfile
(`/etc/NetworkManager/system-connections/croom-<uuid>.nmconnection`, mode 600) and
load it. The network left is kept as a saved network; **Forget** removes one (not the
one a device is on, unless it also has Ethernet). The limits:

- A device cannot be moved to a separate LAN through the page: on the new network it
  must still reach the TV Pi (and the TV Pi its table Pi), or the change is undone. A
  room moving to another network is moved by hand, at each device.
- The TV Pi's Wi-Fi cannot change while its joined table Pi is offline: the check
  waits for a check-in that never comes, and the change is undone after 90 seconds.
- `nmcli` is run with `LC_ALL=C` and its words are matched in English.
- WPA-Enterprise and WEP networks are refused; a hidden network typed with a password
  is joined as WPA2 (a WPA3-only one fails and goes back).
- The door sign refuses a new password for the network it is on ("join another network
  first") and for any other network it has saved ("Forget … first, then join it
  again"), because Android saves it over the old one and an app cannot put that back;
  and Android does not tell an app that a password was wrong, so a wrong one there
  shows as "Could not join … within 90 seconds".

Wi-Fi changes are refused in a meeting; looking for networks is not.

### Sign in to Google on the TV

**Settings**, then **Meetings**, then **Sign in to Google** replaces `croom
--sign-in-meet` and its keyboard on the Pi. The TV's browser closes and Google's
sign-in page fills the TV, in the room's own Google profile; the table shows the
full-screen touchpad on the room Pi's mouse with **Type on the TV** (the on-screen
keyboard, typing on the room Pi with US key codes, which is why the installer sets the
Pi's layout to US) and **Done** and **Cancel**. **Done** checks that Google kept the
session ("Signed in as conference-a@crystalpm.com", or "the room's Google account"
when the profile does not say), closes the sign-in browser and gives the TV its
browser back; **Cancel**, 15 minutes without a touch, the browser closing by itself or
the room software stopping does the same and puts the profile's cookies back as they
were. It is refused during a meeting and while the laptop has the MeetUp, and a join
waits until it ends. A room with no `meeting.google_profile_dir` is offered **Use
/var/lib/croom/meet-profile** first (applied after a restart). Typing on the room Pi
is refused at any other time.

### Mic level

**Mic level** is a screen of its own, from **Options** and from the meeting dock's
**Mic** button (a slider under the speaker's would not fit the meeting screen at 1280
by 800): a slider from 0 to 150, where 100 is the microphone's normal level, and
**Quieter** and **Louder**, 5 at a time, for "they can't hear us". It drives the room
microphone's PipeWire source (`audio.input_device`, or the default source) with
`wpctl`, as the volume drives the speaker, and is outside the PIN. The level chosen is
kept (`mic_level` in `control-settings.json`) and put back at each join and every 5
seconds during a call, because the meeting apps' automatic gain control in the browser
moves the source's level by itself; not while the laptop has the MeetUp. "No
microphone found" when the TV Pi sees none.

### The door sign as the panel's device owner

The Door Sign app, set once per panel as its **device owner** (a factory reset, the
release APK over USB, `adb shell dpm set-device-owner to.croom.doorsign/.AdminReceiver`),
checks in like the table Pi: **Reboot**, **Change Wi-Fi** and **Update** then work from
**Settings** with nobody at the panel, and the panel runs the sign in Android's
lock-task kiosk. Without it the sign and its light bar work as before, and its card
says what it cannot do. [android/door-sign/README.md](android/door-sign/README.md)
has the setup, the release key, how a release is published and the panel's checklist.

### Hands-on checklist

What the fakes in the tests cannot prove, to try once on a real room:

- **The PIN:** choose one; five wrong ones lock the pad for 10 minutes; `sudo rm
  /var/lib/croom/settings-pin.json` at the TV Pi lets the next person choose.
- **Saving and restarting:** change the room's name, **Restart now**: the room comes
  back with the new name. Upload the Google key from a laptop and from a USB stick in
  the TV Pi; **Check**, then **Calendar** passes.
- **Sign in to Google:** type an address with `@` on the TV; **Done** says "Signed in
  as …" and the screensaver comes back; **Cancel**, and 15 minutes left alone, bring
  it back too.
- **Logs:** **Warnings and errors** lists the warnings written since this release.
- **Mic level:** in a call, set 130: the far end hears the room louder, and the level
  holds for the whole call.
- **Software:** **Update** the TV Pi to a newer commit: the progress, the restart,
  "Updated to …". Pull the TV Pi's power during the build: it comes back on the old
  version with "The update was cut off before it was finished". **Switch back**.
  **Update** the table Pi: it reboots and checks in with the new version.
- **Wi-Fi:** move the TV Pi to a network the table Pi cannot reach: back on the old one
  within about 2 minutes, in words. A wrong password: back, in words. Pull the power
  during the 90 seconds: back on the old one after it starts. The same for the table Pi.
  Watch for NetworkManager on Bookworm taking the SSID written as bytes, `connection
  load` and WPA3 (`sae`).
- **Laptop switch:** **Test the switch** moves the MeetUp over and back.
- **The door sign:** the checklist in `android/door-sign/README.md`.

## Upgrade a room to room settings

A room set up before room settings is moved to them once, by hand, device by device:
the TV Pi first (the others check in with it), then the table Pi, then the door sign
panel. Do it outside meeting hours. Before the first room: `room-settings` is merged
into `main` and pushed, because the installer and the table Pi's agent install `main`
from GitHub; and, for the door sign, the release key exists and the release
`door-sign-2` (the app's `versionCode`) is published with its `app-release.apk`, as
`android/door-sign/README.md` says under "Releases and the signing key".

**1. The TV Pi**, over SSH as the desktop user (about 20 minutes, the room down
throughout):

```bash
sudo systemctl stop croom
cd ~/croom.to && git checkout main && git pull
sudo bash installer/install.sh          # no --config: the room's config and keys are kept
sudo reboot
```

If the installer stops with an error, `sudo systemctl start croom` brings the room back
on its old version (the link still names it); fix what it said and run it again.

The installer moves the old `/opt/croom/venv` into `/opt/croom/releases/<its commit>`
(root's) and makes `/opt/croom/venv` a link to it, builds the newest `main` beside it
(10 to 20 minutes), switches the link and keeps the old release as the one to switch
back to; installs and starts `croom-admin.service` and makes `/var/lib/croom-admin`;
rewrites the units; keeps the room's config as `/etc/croom/config.yaml.good` (the one the
restart watch puts back); and sets the keyboard layout to US, which the reboot applies
(with croom on the new release). Croom is stopped first so that it does not run half on the
old code and half on the new while the link moves. Afterwards, on the table screen:
**Options**, then **Settings**: choose the room's PIN. **Software** shows the TV Pi on
the newest version, and no **Switch back** to the version before this one: it has no
Settings and no helper that starts, so it is not offered (only the installer brings
them back).

**2. The table Pi**, over SSH or at its desktop (about 5 minutes):

```bash
cd ~/croom.to && git checkout main && git pull
sudo bash installer/install-kiosk.sh --url http://<TV Pi address>:8080/
sudo reboot
```

This installs the agent (`/opt/croom-kiosk`, `croom-kiosk-agent.service`). After the
reboot, on the table screen: **Options**, then **Settings**, then **Software**, and
**Allow** beside the Touch Pi under **Devices waiting to join**. From then on
**Settings** updates and reboots it.

**3. The door sign panel**, at the panel with a laptop that has `adb` and the release
APK, per panel (about 30 minutes). The APK is `app-release.apk` from the `door-sign-2`
release on GitHub (the repository's Releases), or one built with the room's release key
as "Build a release" in `android/door-sign/README.md` says
([its section](android/door-sign/README.md#build-a-release); the plain Build section makes
a debug APK, which a panel that updates itself from Settings does not take). The one-time cost: the factory reset erases
everything on the panel (its Wi-Fi, the app's room address and PIN, any other app), so
note the room address and the Wi-Fi password first, and the door shows nothing until it
is done. Then:

1. Android Settings, then System, then Reset options, then **Erase all data**. Go
   through the first-start screens without a Google account, join the Wi-Fi (or plug in
   Ethernet), and turn on USB debugging (About tablet, tap Build number seven times;
   then System, Developer options, USB debugging).
2. Over USB:

   ```bash
   adb install -r app-release.apk
   adb shell dpm set-device-owner to.croom.doorsign/.AdminReceiver
   adb shell cmd package set-home-activity to.croom.doorsign/.MainActivity
   adb shell setprop persist.sys.bootapp to.croom.doorsign/to.croom.doorsign.MainActivity
   adb shell am start -n to.croom.doorsign/.MainActivity
   ```

   `dpm` answers `Success: Device owner set to package to.croom.doorsign`; if it says
   the panel has accounts or users, the reset was not complete.
3. In the app's setup, the room's address (`<TV Pi address>:8080`) and a PIN of its own.
4. On the table screen, **Settings**, then **Software**: **Allow** beside the door sign.

A panel left on its old app keeps working as a sign; it just does not check in, so
**Settings** cannot reboot, update or move it.

## Configure a room

`deploy/rooms/` holds one config per room with placeholders, and its README
lists the values to replace. Once the room runs, **Settings** on the table screen
edits the same file, `/etc/croom/config.yaml`, through the root helper: every
value is checked first, the five versions before are kept as `config.yaml.1` to
`config.yaml.5`, and a comment in a hand-written file is lost at the first save
(this table holds the explanations). The sections that matter:

| Section | Keys | Notes |
|---|---|---|
| `room` | `name`, `location`, `timezone`, `company` | The name is shown on the page, the sign and the dashboard. `company` is the practice's name beside the logo on the door sign: "Crystal Practice Management" unless you change it, and `company: ""` shows the logo alone. |
| Door sign settings (not in the config file) | `sign_background` | Chosen on the room page under **Options**, then **Door sign**: `skyline` (the default), `capitol`, `tower`, `bridge`, `pool`, `star`, `hills`, `stadium` or `plain` (the navy alone). Kept in `control-settings.json` under the data directory, with the screensaver's choice, and read by the sign from `/api/status`, so every door sign of the room follows it. The photographs are in `src/croom/control/static/backgrounds/`, with their authors and licences in `CREDITS.md`. |
| Door sign settings (not in the config file) | `sign_scores` | The schools whose football scores run along the bottom of the sign, any of `texas`, `michigan`, `texas_am` and `missouri`; empty, the default, is off. Chosen under **Options**, **Door sign**, **Football scores** and kept in `control-settings.json`. While any is on, the agent reads each school's schedule from ESPN's public feed (`site.api.espn.com`, no account or key) every ten minutes, so the device needs outbound HTTPS to it. |
| `meeting` | `platforms: [zoom, google_meet]`, `zoom_credentials_path`, `google_profile_dir`, `kiosk` | Which links the room can join; joins are limited to those platforms' hostnames. `platforms` is a list: a bare word (`platforms: zoom`) is that one platform, and a value that is not a list (`platforms: no`, which YAML reads as false) is replaced by the default list, `google_meet`, `teams` and `zoom`, with a warning in the journal. Zoom joins go through Zoom's Meeting SDK with the credentials file (`deploy/rooms/zoom-credentials.example.json`); without it the public web client is used, and Zoom blocks automated guests there. **Settings**, then **Meetings** saves that file from an upload, a USB stick or two typed keys and points `zoom_credentials_path` at `/etc/croom/zoom-credentials.json`; a room with no `google_profile_dir` is offered **Use /var/lib/croom/meet-profile** there, before **Sign in to Google**. |
| `meeting.dialogs` | `acknowledge`, `ask_everything` | What the agent does with a dialog Zoom draws over the TV (the stock config and `deploy/rooms/` write the defaults out). A dialog with one button whose title or words contain a phrase of `acknowledge` (any case) is pressed at once and its sentence is kept under the meeting's title, except that a notice which only says a state changed (`has been muted`, `has been unmuted`, `recording has stopped`) refreshes the state and discloses nothing, and that a dialog whose one button leaves or ends the meeting (the whole word `Leave`, `End`, `Exit` or `Quit` in its label, in any case) is never pressed for the room, whatever its words say; every other dialog waits for a tap on the room page, and `ask_everything: true` (default `false`) makes every dialog wait for a tap. Every dialog Zoom draws is written to the journal (`journalctl -u croom`) with its title, words and buttons and what was done with it, whether or not the agent runs with `-v`, so a new one can be judged and added to the list. The default `acknowledge` list is `is being recorded`, `is being transcribed`, `AI Companion`, `Zoom AI is on`, `has been muted`, `has been unmuted`, `recording has started` and `recording has stopped`. A mistake under `dialogs:` or `controls:` costs only its own setting, with a warning in the journal: a value that is not a list (`acknowledge: no`, which YAML reads as false) is replaced by its default; an `ask_everything` that is not true or false is read as false (the words yes, no, on and off, in quotes, are read as they say); an unknown key is ignored; a section that is not a set of settings gives the defaults. Without a warning: a bare word is a one-item list, a setting with nothing after it takes its default, and `acknowledge: []` acknowledges nothing, so every dialog waits. A section with nothing under it (`meeting:` with its lines deleted) is that section's defaults, in every section of the file, with no warning and the rest of the file in force. Inside any other section an unknown key makes the agent set the whole file aside: none of the file's settings take effect and the agent runs on its built-in defaults (the one line it prints about it is block-buffered and reaches the journal only when the service stops); an unknown key at the top of the file, outside every section, is ignored without a word. |
| `meeting.controls` | `reactions` | The reactions the **Reactions** button offers, by the names Zoom's picker gives them: `Thumbs Up`, `Clap`, `Heart`, `Joy`, `Open Mouth` and `Tada` by default (shown on the table as Thumbs up, Clap, Heart, Laugh, Wow and Celebrate); `reactions: []` offers none, and the **Reactions** button is not drawn. |
| `calendar` | `providers: [google]`, `google_credentials_path`, `google_calendar_id`, `sync_interval_seconds`, `booking_start`, `booking_end` | The room's calendar address looks like `c_1885...@resource.calendar.google.com`. A placeholder or a missing key logs one line and the room works with pasted links only. `booking_start` and `booking_end` (`"08:00"` and `"18:00"` unless set, on the quarter hour) are the hours the door sign and the room page offer start times in; the room books its calendar from them only when the calendar is shared with the service account with **Make changes to events**. What is booked there is kept in `bookings.json` under the data directory and sent to Google, and the last calendar read in `calendar-cache.json`, so the room books and shows its day while Google is out of reach. **Settings**, then **Calendar** saves the key (to `/etc/croom/google-service-account.json`, and `google_credentials_path` with it) and the address; the booking hours saved there apply at once, with no restart. |
| `control` | `enabled`, `host`, `port`, `screensaver`, `camera_presets` | The room page, sign and TV page on port 8080, open on the LAN by design. `screensaver` is the TV's style until someone picks another on the room page (`info`, `quiet`, `brand` or `bounce`); the choice is kept in `control-settings.json` under the data directory. `camera_presets` is the three preset names the Camera screens show (default `Wide`, `Table`, `Whiteboard`); the saved Home and presets are kept in the same file, and so is the keyboard's click, `key_click` (on until someone turns it off under **Options**, then **Keyboard**): the page sets it through `/api/settings` and finds it in every `/api/status`. The preset names saved under **Settings**, then **Devices** apply at once; `enabled`, `host` and `port` are refused there, since the room page, Settings included, would no longer be found. |
| `audio` | `output_device`, `input_device` | `input_device` is the microphone **Mic level** drives, chosen the same way (`auto`, or the first source whose name contains the text, which then becomes PipeWire's default source); the level chosen is `mic_level` in `control-settings.json`. `output_device` is the speaker the meeting screen's Sound row drives: `auto` means PipeWire's default output; otherwise the first output whose name contains this text (`HDMI`, `MeetUp`), so `audio.output_device: HDMI` sends the room's sound to the TV. With no match the room page says No speaker found. With a name set, the chosen output also becomes PipeWire's default sink, so the call plays there too (WirePlumber remembers it: to go back, name the other speaker rather than `auto`), and the upstream audio service, which reads the same key as a PyAudio device id, matches no device to a name like `HDMI` and falls through harmlessly (a room device has no PyAudio). |
| `video` | `device`, `ptz_travel_seconds` | The camera the Camera screens drive (`auto`: the first video node with zoom or pan controls; with none, the room page says No controllable camera found) and how long it is driven to reach an end stop when homing (8 s). |
| `tv` | `enabled`, `brand`, `host`, `mac`, `pi_input`, `laptop_input`, `keep_on` | Optional manual defaults; selecting a TV on the table overrides these connection settings. `brand` is `lg` (default) or `samsung`; Samsung differences are described below. For an LG webOS TV configured manually, control starts when `enabled` is `true` (the room files in `deploy/rooms/` carry the block with `enabled: false`). The TV needs **LG Connect Apps** switched on in its settings, and **Mobile TV On** too unless Quick Start+ is on (the room guide's The TV section says where). `host` is the TV's address, which **Options**, then **TV**, then **Find TVs** lists on the table; `mac` is the TV's MAC address, which every Turn on needs (the wake-up message goes out first, so a missing or malformed one stops it even with Quick Start+), and a `mac` of digits only, such as `001122334455` or `10:20:30:40:50:59`, is a number to YAML and must be quoted. `pi_input` is the TV input the room Pi is on (the default, `HDMI_1`, is a guess). `laptop_input` is the TV input a laptop at the table is on, chosen under **Options**, then **TV**, as **Laptop input**; empty (the default) leaves the TV alone in Laptop mode. `keep_on` (default `true`) wakes the TV when the agent starts, before every join and after an unexpected off, and before every join it also switches the TV to `pi_input`, live TV and apps included, and leaves a TV whose state is not known alone; a manual **Off** from the table holds until a manual **On** or the next join (`held_off` lives in memory only, so a restart forgets it). For legacy LG configurations, the key the TV hands out at pairing is kept in `tv-client-key` under the data directory, mode 600, never in the config. Selected TVs and Samsung configurations use a separate key per brand and address. `croom --check-tv` checks the TV from a terminal. |
| `laptop` | `enabled`, `gpio_pin`, `active_low`, `press_ms`, `usb_id`, `switch_wait_s` | Laptop mode: the room's Logitech MeetUp lent to a laptop at the table through an ATEN US234 USB switch whose remote button a relay on the pin presses (`enabled` is `false` by default; `gpio_pin` is 17, which is header pin 11; `active_low` is `false`, set it for a relay that closes on a low level; `press_ms` is how long the button is held, 200; `switch_wait_s` is how long a press is given to move the MeetUp, 10 seconds). Where the MeetUp is is read from the Pi's USB: `usb_id` is its vendor and product id, `046d:0866` by default (`lsusb` shows it). `croom --check-laptop -c /etc/croom/config.yaml` shows the pin, the GPIO chip and where the MeetUp is now, and with `--switch` moves it once; stop croom first, which holds the pin. From the table, **Settings**, then **Laptop switch** has **Test the switch**, which presses through the running service, moves the MeetUp over and back and says what it saw (refused in a meeting, and while the switch is off in the settings). The wiring is in `docs/superpowers/specs/2026-10-27-laptop-switch-design.md`. |
| `ai` | `enabled`, `backend` and a switch per feature | Upstream's AI features, on **Settings**, then **AI & dashboard**: `backend` is `auto`, `hailo`, `coral`, `intel`, `nvidia` or `cpu`. |
| `dashboard` | `url`, `enrollment_token`, `heartbeat_interval_seconds` | The token comes from the dashboard's Provisioning page and works once. |
| Room settings files (not in the config file) | `settings-pin.json`, `devices.json`, `door-sign/` | Under the data directory: the Settings PIN, hashed (scrypt with a random salt), mode 600; the table Pis and door signs that joined the room or were refused, with their tokens hashed, mode 600; and the door sign releases the TV Pi downloaded, the two newest. The root helper keeps its own record (restarts, updates, Wi-Fi changes, the release kept before) in `/var/lib/croom-admin`, root's alone. |

Never commit a real token or key.

## Select the room TV from the table

Open **Options > TV > Find TVs**, select the LG or Samsung TV, and accept
**Crystal Meet** on the TV with its own remote. The room Pi saves its brand,
IP address and discovered MAC and connects immediately. No YAML edit or service
restart is needed. If the MAC could not be found, the table can still connect;
enter the active Ethernet or Wi-Fi connection's MAC in the connection settings
to enable waking the TV. Choose the Pi's HDMI socket there if it is not HDMI 1.
Keep the TV and Pi on the same local network and reserve the TV's IP on the router.

The selection lives in `tv-settings.json` under the agent's data directory and
overrides the YAML connection defaults. Pairing keys are kept per brand and
address, mode 600. Existing LG configurations and their `tv-client-key` continue
to work; `tv.brand` defaults to `lg` and also accepts `samsung`. Both the agent
and `croom --check-tv` restore the saved selection. **Find TVs** can change the
room's TV later; connection settings update its MAC and HDMI socket without
pairing again.

Samsung S90HD/Tizen control uses a local secure WebSocket, with no SmartThings
account. Power/wake, volume keys, mute, navigation, Home, Back, the source menu,
channel and playback keys are supported. Samsung does not expose reliable
input/volume/mute feedback through this connection: **Input** opens its source
menu, **Mute** toggles the sound, and automatic HDMI selection before joining
is unavailable. TV-pointer mode is hidden; **Room Pi mouse** and
**Full-screen touchpad** remain available. Enable **Power On with Mobile** for
wake and use **First Time Only** in Device Connection Manager if approval
prompts repeat. See [Samsung wake settings](https://www.samsung.com/us/support/troubleshoot/TSG10002365/)
and the [Samsung protocol reference](https://github.com/xchwarze/samsung-tv-ws-api).
The S90HD still needs a physical-TV acceptance test after deployment.

The picker uses `POST /api/tv/select` with a host from the server's recent
discovery results, then saves and pairs that TV. `POST /api/tv/settings` changes
the selected TV's MAC and HDMI socket. Both return the current `tv` status,
which also includes `brand`, `mac` and `capabilities`.

## Run the dashboard

In production the dashboard runs on its own Raspberry Pi, or any 64-bit Docker
host, from `deploy/dashboard/`: Postgres and the dashboard image built from
this repo, under Docker Compose. One command installs it and later updates it:

```bash
sudo bash installer/install-dashboard.sh --admin-email you@crystalpm.com
```

It installs Docker, clones this fork into `/opt/croom-dashboard`, writes the
secrets once to `deploy/dashboard/.env` there (mode 600, never committed),
builds and starts the stack on ports 3001 and 80, and adds a nightly `pg_dump`
to `/var/backups/croom-dashboard`. The first admin comes from `ADMIN_EMAIL` and
`ADMIN_PASSWORD` in that file and is created only when missing. There are no
database migrations: production creates missing tables at start-up and never
alters existing ones. The dashboard guide covers the Pi; `deploy/dashboard/README.md`
has the operator's commands.

For development on this machine:

```bash
docker run -d --name croom_postgres -e POSTGRES_USER=croom -e POSTGRES_PASSWORD=croom -e POSTGRES_DB=croom \
  -p 127.0.0.1:5432:5432 -v croom_pgdata:/var/lib/postgresql/data --restart unless-stopped postgres:16-alpine
cd src/croom-dashboard/backend && npm install && npm run dev     # API and WebSocket on :3001; settings in backend/.env (not committed)
cd src/croom-dashboard/frontend && npm install && npm run dev    # web app on :3000, proxies /api and /ws to :3001
```

Open `http://localhost:3000` and sign in; on an empty database the backend
creates the admin from `ADMIN_EMAIL` and `ADMIN_PASSWORD` in `backend/.env`.
Use Provisioning to create one token per room.
Devices enrol with `POST /api/provisioning/enroll` and then keep a WebSocket
open for heartbeats, status and meeting events. Under WSL2, turn on mirrored
networking or forward ports 3000 and 3001 so the Pis can reach the dashboard.
Restart the Vite dev server after changing `tailwind.config.js`.

## Develop and test

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/playwright install chromium
.venv/bin/pytest -q -p no:cacheprovider --deselect tests/unit/video/test_v4l2_camera.py::TestV4L2Camera::test_start
.venv/bin/croom -v -c ~/.config/croom/config.yaml                 # run the agent on this machine (WSL2 with WSLg works)
.venv/bin/python docs/guides/crystal-meet-room-setup/build.py     # rebuild a guide's PDF after editing its index.html
```

The upstream suite carries 87 tests that fail because they were written for
code that never ran, and the deselected test stalls; leave them. Everything
this fork added lives under `tests/unit/control`, `tests/unit/calendar`,
`tests/unit/deploy`, `tests/unit/installer`, `tests/unit/settings`,
`tests/unit/admin`, `tests/unit/kiosk_agent` and `tests/unit/docs` and must
pass (the door sign app has its own, `./gradlew testDebugUnitTest` in
`android/door-sign`). The browser tests drive the room page and the sign in the venv's
Chromium; the docs tests render the PDFs. The Zoom web SDK version is pinned once, as
`SDK_VERSION` in `src/croom/meeting/providers/zoom_sdk_site.py`; bump it there when Zoom
retires that version from its CDN.

## Implementation notes

Each piece was designed in a spec, built from a plan test-first, and reviewed
by a fresh reviewer before merging. The spec holds the why and the contracts,
the plan holds the how, step by step with the code.

| Work | Spec | Plan |
|---|---|---|
| Agent startup fix and dashboard enrollment: the services join the `Service` framework; REST enroll plus a WebSocket for heartbeats | [spec](docs/superpowers/specs/2026-09-24-agent-startup-and-dashboard-enrollment-design.md) | [plan](docs/superpowers/plans/2026-09-24-agent-startup-and-dashboard-enrollment.md) |
| Room control page and API: `croom.control` with `/api/status`, `/api/calendar/events` and `/api/meeting/{join,leave,mute,camera}` | [spec](docs/superpowers/specs/2026-09-24-room-control-page-design.md) | [plan](docs/superpowers/plans/2026-09-24-room-control-page.md) |
| Crystal Meet brand, three room configs and the installer, the door sign, the setup guide | [spec](docs/superpowers/specs/2026-09-24-crystal-meet-brand-and-rooms-design.md) | [plan](docs/superpowers/plans/2026-09-24-crystal-meet-brand-and-rooms.md) |
| Google Calendar credentials, the check command, the calendar guide | [spec](docs/superpowers/specs/2026-09-25-google-calendar-credentials-design.md) | [plan](docs/superpowers/plans/2026-09-25-google-calendar-credentials.md) |
| Zoom joins through the Meeting SDK: signature, per-room Zoom user's ZAK for outside hosts, loopback page, `croom --check-zoom`, the Zoom guide | [spec](docs/superpowers/specs/2026-09-25-zoom-meeting-sdk-design.md) | [plan](docs/superpowers/plans/2026-09-25-zoom-meeting-sdk.md) |
| The dashboard on a Raspberry Pi: Docker Compose packaging, production mode in the backend, the dashboard installer and guide | [spec](docs/superpowers/specs/2026-10-05-dashboard-on-a-pi-design.md) | [plan](docs/superpowers/plans/2026-10-05-dashboard-on-a-pi.md) |
| Google Meet as a signed-in room: the persistent profile, `croom --sign-in-meet`, pre-join camera and microphone, the Meet guide | [spec](docs/superpowers/specs/2026-10-07-google-meet-room-account-design.md) | [plan](docs/superpowers/plans/2026-10-07-google-meet-room-account.md) |
| The TV screensaver and the one page on the TV: `TvDisplay` owns the browser, providers borrow its page, the `/tv` page with four styles, `/api/screensaver`, the picker on the room page | [spec](docs/superpowers/specs/2026-10-07-tv-screensaver-and-one-page-design.md) | [plan](docs/superpowers/plans/2026-10-07-tv-screensaver-and-one-page.md) |
| Sound and camera from the room page: volume through PipeWire, MeetUp framing with timed presets, the idle camera preview on the TV | [spec](docs/superpowers/specs/2026-10-08-sound-and-camera-controls-design.md) | [plan](docs/superpowers/plans/2026-10-08-sound-and-camera-controls.md) |
| The room page in two modes: an idle screen and a meeting screen behind one view switch, Options with Camera framing and Screensaver, Back on every sub-screen, the ten-minute return to the idle screen, the kiosk at 1.5 times scale, a placeholder location counted as none | [spec](docs/superpowers/specs/2026-10-12-room-page-modes-design.md) | [plan](docs/superpowers/plans/2026-10-12-room-page-modes.md) |
| Meeting controls, live state and dialogs: what a participant or host can do from the table on Zoom (hand, reactions, captions, view, self view, participants, chat, host controls) and the four controls Google Meet's shortcuts allow, the state as the platform has it, Zoom's dialogs acknowledged by policy or answered from the table (`ask_everything`), a Lucide icon before the label of every button but the dialog card's (whose labels are Zoom's own words), `/api/meeting/control`, `/api/meeting/dialog`, `/api/meeting/chat/seen` and `/api/meeting/diagnostics` | [spec](docs/superpowers/specs/2026-10-13-meeting-controls-and-dialogs-design.md) | [plan](docs/superpowers/plans/2026-10-13-meeting-controls-and-dialogs.md) |
| The TV remote and the touchpad: the LG C6 over webOS with pairing from the table, Wake-on-LAN and keep-on, every remote key from the table, the d-pad disc as a trackpad for the TV's cursor, a virtual mouse on the room Pi, a full-screen touchpad and `croom --check-tv` | [spec](docs/superpowers/specs/2026-10-13-tv-remote-and-touchpad-design.md) | [plan](docs/superpowers/plans/2026-10-13-tv-remote-and-touchpad.md) |
| The door sign redesigned: the status as a rail, a solid chip and a ring on the current booking, photographs of Austin under a navy overlay (nine backgrounds, eight photographs and plain, chosen on the room page), the company name, a week view, optional football scores for four schools (off by default) | [spec](docs/superpowers/specs/2026-10-13-door-sign-design.md) | [plan](docs/superpowers/plans/2026-10-13-door-sign.md) |
| Book the room from its screens: Book and Use now on the door sign and the room page, End now on the room page only, Cancel, the bookings kept on the device and sent to Google with the room's tag, the calendar kept for a restart without Google, double bookings flagged, `croom --check-calendar` proving the room can book | [spec](docs/superpowers/specs/2026-10-25-room-booking-design.md) | built without a separate plan |
| The on-screen keyboard done properly: keys act the moment a finger lands, per finger, iPad layers and key caps, the preview pop, a key click that can be switched off under **Options**, then **Keyboard** | [spec](docs/superpowers/specs/2026-10-13-on-screen-keyboard-design.md) | [plan](docs/superpowers/plans/2026-10-13-on-screen-keyboard.md) |
| Laptop mode: the room's MeetUp lent to a laptop at the table through an ATEN US234 USB switch whose remote button a relay on GPIO17 presses, the TV switched to the laptop's HDMI input, a booking holding the time, Join refused meanwhile, `croom --check-laptop` | [spec](docs/superpowers/specs/2026-10-27-laptop-switch-design.md) | [plan](docs/superpowers/plans/2026-10-27-laptop-switch.md) |
| Room settings on the table screen: **Settings** behind a PIN, every config page, the keys by upload or USB stick, Sign in to Google on the TV, Check and Logs, restart with the undo, software updates with rollback for the TV Pi and the table Pi and silent updates for the door sign, Wi-Fi with the safety net on every device, the root helper `croom-admin`, check-in for the table Pi and the door sign as device owner, Mic level | [spec](docs/superpowers/specs/2026-10-29-room-settings-design.md) | [plan](docs/superpowers/plans/2026-10-29-room-settings.md) |

Decisions worth knowing before changing things:

- Internal names stay `croom`; only what a person sees says Crystal Meet.
- The room page and sign are open on the LAN by design; joins are restricted to the configured platforms' hostnames and every POST must be JSON. The meeting screen's Sound row and the Camera screens call `/api/audio/volume` and `/api/camera/{move,zoom,home,presets/<slot>,setup,preview}`, and **Mic level** `/api/audio/mic` (`{level}` or `{step}`, open like the volume), and `/api/status` carries their `audio`, `mic` and `camera` blocks. The door sign reads `/api/status` (which carries `room.company`, `settings` with `sign_background` and `sign_scores` (and the room page's `key_click`) and, only while a school is on, `scores`: a list with one entry per school, its `last` and `next` null until the first read), `/api/calendar/events` and, when **This week** is tapped, `/api/calendar/week` (the next seven days, today first, each `{date, label, events}` by the device's local day); `POST /api/settings` changes `sign_background`, `sign_scores` and `key_click`, and refuses a bad value with words.
- **Settings** is the one part of the room page behind a PIN. Open: `GET /api/settings/pin` (`{set, locked_minutes, unlocked}`), `POST /api/settings/pin/set` (only while no PIN is set), `POST /api/settings/unlock` (`{pin}`, answering `{token, expires_in}`) and `POST /api/settings/lock`. Every other `/api/settings/...` route but `/api/settings` itself (the door sign's look and the key click, open as before) needs the token in an `X-Croom-Unlock` header and answers 401 `{"error": "Enter the PIN", "locked": false}` without a good one, or 423 with the minutes left during a lock-out: `pin/change`; `config/{page}` (GET the values and the fields, POST `{"values": {path: value}}`, a 400 with `errors` by path for a refused one); `restart` (GET the undo record, POST Restart now); `credentials` (GET what each key is), `credentials/{google|zoom}` (multipart, field `file`, 64 KB at most), `credentials/{kind}/usb` (`{path}`), `credentials/zoom/typed` and `usb-files`; `laptop/test`; `google/sign-in` and its `/done`, `/cancel` and `/state`; `checks/{name}`; `logs?level=&before=`; `software`, `software/update` (`{device, commit}`, or a door sign's `version_code`), `software/update-all`, `software/rollback` and `reboot` (`{device}`); `devices`, `devices/{id}/allow|refuse|forget` and `devices/{id}/jobs`; `network` and `network/{device}/scan|connect|forget`. Every POST is JSON (the key upload excepted) and a refusal is words, `{"error": …}`; restart, reboot, update, Wi-Fi changes and the sign-in are a 409 "Leave the meeting first" in a meeting, and a privileged route is a 503 "Not available on this device" without the helper. The devices' own routes take no PIN: `POST /api/devices/checkin` (a device's id and kind, and its token in `X-Croom-Device-Token`), `GET /api/devices/apk/{versionCode}` (a joined door sign's id and token) and `GET /api/devices/seen-since` (answered only to the TV Pi itself, for the helper's Wi-Fi check).
- Mute and Camera keep their own routes and send the state their button names, `POST /api/meeting/mute` with `{"muted": true}` and `POST /api/meeting/camera` with `{"on": false}`: the agent sets that state (a microphone or camera that is as asked is left alone), so a tap made a moment after the host muted the room, or after the camera went off at the TV, ends as the person asked and does not flip it back; a body that names no state still toggles, as the routes always did, and both answer with the meeting block as well as the state. The meeting screen's other controls are one route: `POST /api/meeting/control` with `{"name": ..., "value": ...}`; a Zoom dialog that waits for a tap is answered with `POST /api/meeting/dialog` (`{"id": ..., "button": ...}`) and the chat the table has read is reported with `POST /api/meeting/chat/seen`. All three answer `{"meeting": ...}`, the block `/api/status` carries, so the table draws from one shape; a control the platform cannot carry out is a 409 with its words, an unknown name included. `GET /api/meeting/diagnostics` is the support route for pinning a platform's real wording from a device: during a call it lists what the platform's page shows (on a Zoom call the footer's buttons, the Reactions picker's and View menu's items, every dialog met and the SDK version; on a Meet call the labels the page draws), and on a Zoom call it briefly opens the Reactions and View menus on the TV, so it is never polled. `/api/status` is unauthenticated on the LAN, as the whole page is, and now carries the participants' names, the last twenty chat messages and the words of a waiting dialog, as it already carried the meeting link (its passcode too, when the link has one); `/api/meeting/diagnostics` is open the same way and lists more of the meeting (the whole roster with its flags, the words of every dialog met).
- The dark theme is Tailwind's stock charcoal (`#111827` page, `#1F2937` cards) with the Crystal PM blue accent and Lexend; navy was rejected for dark mode.
- Brand assets (logo, Lexend, its OFL licence) are bundled under `src/croom/control/static` and in each guide folder, so nothing loads from the internet.
- Icons are [Lucide](https://lucide.dev) (ISC licence, lucide-react 0.303.0), in one sprite, `src/croom/control/static/icons.svg`, with the licence text beside it in `LICENSES.md`; the room guide draws the same symbols inline.
- Nothing joins or leaves by itself; Join now opens ten minutes before a booking.
- The TV remote and the touchpad call `/api/tv/control`, `/api/tv/pair`, `/api/tv/forget` and `/api/tv/discover` (every POST must be JSON, as elsewhere), and `/api/status` carries the `tv` block they draw from (`configured`, `paired`, `refused`, `prompting`, `on`, `reachable`, `name`, `volume`, `muted`, `input`, `inputs`, `app`, `channel`, `keep_on`, `held_off`, `host` and `pi_input`); `prompting` is true while the TV shows its pairing prompt for the key the table keeps (the agent has waited at it longer than a TV that does not answer takes: the TV has forgotten the table), and the page's status line then says to **Forget TV**, then **Pair**. The touchpad's events travel over one WebSocket, `/ws/pointer`, as JSON messages like `{"target": "tv", "type": "move", "dx": 3, "dy": -2}` (targets `tv` and `pi`; types `move`, `tap`, `right`, `scroll`, `down` and `up`); a `keys` message (`{"target": "pi", "type": "keys", "text": "abc"}`, or `"key": "Enter"` and the like) types on the room Pi through a virtual keyboard beside the virtual mouse, with US key codes, and only while **Sign in to Google** is open; the TV's cursor takes none. The cursor is not a control: it takes no turn behind the controls, which go one at a time (a Turn on waits up to 23 seconds for the TV). Like the rest of the page, the pointer takes no sign-in, so anyone on the network who can open the page can drive the TV and the Pi's mouse; a page from another site is refused the pointer. The TV is reached over LG's webOS socket (`aiowebostv`) after a one-time pairing. To turn a TV on the agent sends a Wake-on-LAN message first, whatever the TV keeps (`wakeonlan`, a limited broadcast from the default-route interface: the Pi and the TV must share that network), so a missing or malformed `mac` stops a Turn on, a join's wake and the re-wake alike; a TV that is fully off is woken by that message alone, which needs Mobile TV On, and a TV that keeps its socket in standby (Quick Start+) is then told to turn on over it, so it needs no Mobile TV On. The room Pi's mouse is a virtual device on `/dev/uinput` (`evdev`), made in a thread when the first pointer socket opens, so that the desktop has it before the first touch, and kept until the agent stops (the Pi's desktop takes it like a USB mouse, so its own pointer should appear on the TV from then on; the first use in a room confirms it); the installer's udev rule gives `/dev/uinput` to the `input` group, loads the `uinput` module at boot and puts the desktop user in that group, and `evdev` has no wheel, so `python3-dev`, `gcc` and `libc6-dev` must be on the device before the agent is installed or updated.
- Laptop mode calls `POST /api/laptop`, `{"to": "laptop", "minutes"?: n}` to lend the MeetUp to the laptop at the table (the TV switches to `tv.laptop_input`, and a booking holds the time unless the room has no Google calendar, when there is no end and no booking) or `{"to": "room"}` to take it back, and answers with the `laptop` status block, or a 409 in words when it cannot be done (every POST must be JSON, as elsewhere). `/api/status` carries that block as `laptop` (`enabled`, `available`, `holder`, `until`, `made_booking`, `switching` and `problem`); `holder` is `room` or `laptop` only. Join (`POST /api/meeting/join`) is refused with a 409 while the laptop has the MeetUp.
- The table Pi's kiosk browser hides the Pi's own on-screen keyboard, so the room page carries its own, opened with `?keyboard=1`; the TV Pi's desktop launcher gets sudo for exactly `systemctl start|restart croom` and nothing else (`/etc/sudoers.d/croom`).
- The room page's keyboard acts on each touch as it lands, one pointer event per finger and never on the click a browser adds after a lift (it adds one for the first finger only), so two thumbs typing fast lose nothing. It builds the rows of a layer once and changes their labels in place, so no key under a finger is ever replaced, and it hides only for a tap elsewhere, its own hide key, Join or a change of screen. Its click is a 1.2 kHz tick that fades out in 4 ms, made by the browser's audio (no sound file, so it plays through the table Pi's audio output), with a short buzz where the device has a motor; the choice is the room's `key_click` setting, kept with the screensaver's in `control-settings.json` and served at `/api/settings`, on until someone turns it off.
- The TV shows one page, owned by the agent: the screensaver when idle, the meeting when joined. The providers borrow that page and hand it back; nothing else ever opens a window on the TV, and the TV needs no keyboard or mouse. The browser runs in kiosk mode (`meeting.kiosk`) with no fixed viewport, so the page fills the TV at whatever resolution the Pi drives it. It always runs on a profile folder: the Meet sign-in profile when one is configured, otherwise a throwaway folder in the temp folder that a clean stop removes and the next start sweeps if the agent was killed, because Chromium keeps kiosk mode for the window it opens at startup only and only a profile launch opens one (a launch without a profile put a tab strip and an address bar on the TV). If the TV page dies, the agent starts the browser again the next time the page is needed, and after a crash on the screensaver without waiting for that (paused for ten seconds when the browser had only just started), rather than opening a replacement page, which would come up in a plain window too.
- With a calendar address configured, only that calendar is read; the room resource's declined (double-booked) and cancelled bookings are dropped; a link typed into an event wins over an automatically added Meet.
- Google Meet refuses guests that arrive through an automated browser, so each room joins Meet signed in as its own Workspace user from a persistent browser profile (`meeting.google_profile_dir`); the browser is never disguised. `croom --sign-in-meet` does the one-time sign-in on the room's screen, and **Sign in to Google** under **Settings**, then **Meetings** does it from the table, on the TV, with the service running.
- Zoom is joined through Zoom's Meeting SDK, never by driving the public web client, which blocks automated guests. Meetings on your own account need only the SDK app's signature; meetings hosted elsewhere need Zoom's review of the SDK app (it may stay unlisted; its production credentials are the approved ones) plus the room's Zoom user and its ZAK, fetched with the Server-to-Server credential.
- The MeetUp reports no pan or tilt position, so presets are timed moves from a home found against the end stops: Set up on the room page is **Find the stops**, nudge to the view, **Save as Home**, then save the presets. The camera homes at service start once a home is saved, on Home, and before a recall that finds its position unknown; the position is tracked in seconds of travel and recalls move only the difference, so Home and a recall take seconds and the page says Moving… meanwhile. Volume goes through PipeWire's own tools (`pw-dump`, `wpctl`), never through the upstream audio service.
- The room page is two modes, not one column: the idle screen shows how to join and what is booked, the meeting screen shows the call's controls; everything else is a screen behind Options or a button, with Back, so every screen fits without the page scrolling (the bookings boxes scroll inside themselves). A room config that still has `location: "REPLACE_WITH_LOCATION"` shows the room's name alone.

Known limitations, mostly inherited from upstream:

- Stopping the meeting service can hang while closing the headed Chromium; the service unit's restart covers it.
- A Zoom meeting's passcode is read from the link, from the booking's invitation text, or typed in the **Passcode** field that appears on the room page for a bare meeting ID or a link without one; a join that fails because Zoom wanted one keeps its link on the "Couldn't join" card, with the field and a **Join again** button. A passcode may be letters, digits and `@ - _ *`, up to 32 characters. A failed Meet join logs what Meet showed and saves a screenshot under `/tmp`; `croom --check-meet URL -c CONFIG` reports the same from a terminal.
- The buttons, menus and notices of Zoom's client that the meeting screen relies on were read from Zoom's 6.5.0 web client files, not yet from a live meeting, and the English labels it looks for on Google Meet's call page are the controls plan's wording; a control whose button the page does not find is hidden rather than pressed wrongly. The first real meeting confirms them: the Zoom guide's **In the meeting** section lists the inventory to run with `GET /api/meeting/diagnostics`, and the Meet guide says what to check on a Meet call.
- The TV remote, the pairing and the touchpad are verified against a fake TV and a fake virtual mouse, not yet on an LG C6 behind a room Pi; the gesture numbers (gain, tap and hold times, the scroll notch) are constants at the top of `touchpad.js`, tuned on the table's own screen.
- Room settings are verified against stand-ins for NetworkManager, systemd, journald, pip, git, GitHub and Android, not yet on a room's Pis or a panel: the hands-on checklist under [Settings on the table screen](#settings-on-the-table-screen) and the door sign's own are still to be run.
- `--no-service` is parsed but not honoured; the touch UI (`croom-ui`) and Microsoft 365 are untested in this fork.
- The browser path and boot-ordering fixes in the installer are verified by tests of the generated unit files, not yet on a Pi.
- The football scores come from a public ESPN feed that ESPN does not document for this use: what it lists, and whether it keeps working, is ESPN's to change (the fetcher is one file, `src/croom/scores/espn.py`). While it cannot be reached the strip keeps the last answer it read, which may be days old. The parser was checked against live answers from a development machine; a Pi's network, the postseason and the off-season have not been seen yet. A room name goes down to 1.3 rem and no further. Room names over about 150 characters (five lines of the smallest type on a 7-inch 800 by 480 sign) run off the sign; use a shorter name.
- The dashboard speaks plain HTTP on the office network, with no TLS, and has no database migrations; a model change that needs an altered table is a manual `psql` step in production.
- Joining Zoom meetings hosted by other accounts needs the SDK app approved by Zoom's review, which takes weeks, and relies on a ZAK fetched through the Server-to-Server app; this is not yet verified against a live outside-hosted meeting. If Zoom refuses after the approval, the fallback is the SDK app's own OAuth authorization.

---

*The upstream croom.to README follows.*


<div align="center">

# 🎥 Croom

### Turn Any Raspberry Pi Into a Professional Video Conferencing System

**The open-source alternative to Cisco Webex Room Kit — for 1/50th the price**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Raspberry Pi](https://img.shields.io/badge/Raspberry%20Pi-5%20|%204-red.svg)](https://www.raspberrypi.com/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![GitHub Stars](https://img.shields.io/github/stars/amirhmoradi/croom.to?style=social)](https://github.com/amirhmoradi/croom.to)
[![Made in France](https://img.shields.io/badge/Made%20in-France-blue.svg)](https://en.wikipedia.org/wiki/French_Tech)
[![Digital Sovereignty](https://img.shields.io/badge/Digital-Sovereignty-purple.svg)](#-digital-sovereignty--data-privacy)

[Features](#-features) • [Quick Start](#-quick-start) • [Sovereignty](#-digital-sovereignty--data-privacy) • [Documentation](#-documentation) • [Contributing](#-contributing)

---

<img src="docs/assets/hero-banner.png" alt="Croom Dashboard" width="800"/>

*Transform conference rooms with enterprise-grade video conferencing at a fraction of the cost*

</div>

---

## 🇫🇷 Digital Sovereignty & Data Privacy

> **Croom is a French initiative** committed to digital resilience, data privacy, and technological independence.

In a world where video conferencing has become critical infrastructure, organizations deserve **control over their communication systems**. Croom was created to break free from:

- **Vendor Lock-in**: No dependency on single cloud providers
- **Data Exploitation**: Your meetings, your data — processed locally on your hardware
- **Unpredictable Pricing**: No per-seat licenses that scale against you
- **Opaque Systems**: Fully open source, audit everything

### Our Principles

| Principle | How Croom Delivers |
|-----------|---------------------|
| **Data Sovereignty** | All processing happens on YOUR hardware. No cloud required. |
| **Privacy by Design** | AI runs locally via Hailo/Coral. No data leaves your network. |
| **Open Source** | MIT licensed. Inspect, modify, and audit every line of code. |
| **Vendor Independence** | Works with Meet, Teams, Zoom — switch platforms freely. |
| **European Values** | GDPR-ready architecture. Built with privacy regulations in mind. |

### Self-Hosted & Air-Gapped Ready

Croom can operate **completely offline** in air-gapped environments:
- No internet required for core functionality
- Local AI processing with Hailo-8L or Coral TPU
- On-premise management dashboard
- Full functionality without external dependencies

## 💰 Why Croom?

| | Cisco Room Kit | Poly Studio | **Croom** |
|---|:---:|:---:|:---:|
| **Hardware Cost** | $3,000 - $15,000 | $2,000 - $8,000 | **< $250** |
| **Monthly License** | $15/device | $12/device | **Free forever** |
| **10 Rooms (Year 1)** | $31,800+ | $21,440+ | **$2,500** |
| **Multi-platform** | Limited | Limited | **✅ Meet, Teams, Zoom** |
| **AI Features** | ✅ | ✅ | **✅** |
| **Open Source** | ❌ | ❌ | **✅** |

> **Save $29,000+ per year** on a 10-room deployment while getting the same enterprise features.

## ✨ Features

<table>
<tr>
<td width="50%">

### 🖥️ Multi-Platform Support
- **Google Meet** - Full support with calendar integration
- **Microsoft Teams** - Join any Teams meeting
- **Zoom** - Works with Zoom web client
- Auto-detect platform from meeting URL

</td>
<td width="50%">

### 🤖 Edge AI Processing
- **Auto-framing** - Keeps participants in frame
- **Noise reduction** - Crystal clear audio
- **Occupancy counting** - Room analytics
- Works with Hailo-8L, Coral, or CPU fallback

</td>
</tr>
<tr>
<td width="50%">

### 📱 Touch Screen UI
- Beautiful room control interface
- One-tap meeting join
- Calendar view for scheduled meetings
- Camera/mic controls

</td>
<td width="50%">

### 🏢 Fleet Management
- Centralized dashboard for all devices
- Real-time device status monitoring
- Remote configuration & updates
- Usage analytics & reporting

</td>
</tr>
<tr>
<td width="50%">

### 🔧 Zero-Touch Provisioning
- QR code based device enrollment
- Automatic configuration sync
- No manual setup per device
- Scale to hundreds of rooms

</td>
<td width="50%">

### 🔒 Enterprise Security
- End-to-end encryption
- Role-based access control
- Audit logging
- On-premise deployment option

</td>
</tr>
</table>

## 🎙️ Meeting Intelligence with Vexa Integration

Croom integrates with [**Vexa**](https://github.com/Vexa-ai/vexa) — the open-source, self-hosted meeting transcription platform — for advanced meeting intelligence features while keeping **all data on your infrastructure**.

### What Vexa Adds to Croom

| Feature | Description |
|---------|-------------|
| **Real-time Transcription** | Live transcripts during meetings (100+ languages) |
| **Meeting Summaries** | AI-generated summaries and action items |
| **Searchable Archives** | Find any discussion across all your meetings |
| **Translation** | Real-time translation between 100 languages |
| **Self-Hosted** | Run on your infrastructure — no cloud dependency |

### Why Vexa + Croom?

Both projects share the same values:
- **Open Source** (Vexa: Apache 2.0, Croom: MIT)
- **Self-Hosted First** — Your data never leaves your network
- **Privacy by Design** — No third-party data processing
- **Enterprise Ready** — Built for organizations that take security seriously

```bash
# Deploy Vexa alongside Croom
git clone https://github.com/Vexa-ai/vexa
cd vexa && make all  # CPU mode (add GPU=1 for GPU acceleration)
```

### Integration Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Your Infrastructure                          │
│                                                                 │
│  ┌─────────────┐          ┌─────────────┐                      │
│  │   Croom    │◄────────►│    Vexa     │                      │
│  │   Device    │ WebSocket│  Instance   │                      │
│  │             │          │             │                      │
│  │ • Camera    │          │ • Whisper   │                      │
│  │ • Audio     │──────────│ • Transcr.  │                      │
│  │ • Display   │  Audio   │ • Summarize │                      │
│  └─────────────┘  Stream  └─────────────┘                      │
│                                                                 │
│  No data leaves your network. Everything runs locally.         │
└─────────────────────────────────────────────────────────────────┘
```

## 🚀 Quick Start

### One-Line Installation

```bash
curl -sSL https://croom.to/install.sh | sudo bash
```

### Manual Installation

```bash
# Clone the repository
git clone https://github.com/amirhmoradi/croom.to.git
cd croom

# Install Croom
pip install -e .

# Start the agent
croom --config /etc/croom/config.yaml
```

### Docker (Dashboard)

```bash
docker-compose up -d
```

Open `http://localhost:3000` to access the management dashboard.

## 🛠️ Hardware Requirements

### Recommended Setup (~$200)

| Component | Model | Price |
|-----------|-------|-------|
| Computer | Raspberry Pi 5 (4GB) | $60 |
| Case | Argon ONE V3 | $25 |
| Camera | Logitech C920 | $60 |
| AI Accelerator | Hailo-8L AI Kit | $70 |
| Storage | 32GB microSD | $10 |
| **Total** | | **~$225** |

### Minimum Setup (~$100)

| Component | Model | Price |
|-----------|-------|-------|
| Computer | Raspberry Pi 4 (4GB) | $55 |
| Camera | Generic USB Webcam | $20 |
| Storage | 32GB microSD | $10 |
| Power + Cables | | $15 |
| **Total** | | **~$100** |

## 📐 Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Croom Device                            │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │  Touch UI   │  │   Agent     │  │  AI Engine  │             │
│  │   (Qt6)     │  │  (Python)   │  │  (Hailo/    │             │
│  │             │  │             │  │   Coral)    │             │
│  └─────────────┘  └─────────────┘  └─────────────┘             │
└────────────────────────────┬────────────────────────────────────┘
                             │ WebSocket
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Management Dashboard                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │   React     │  │   Node.js   │  │ PostgreSQL  │             │
│  │  Frontend   │◄─┤   Backend   │◄─┤  Database   │             │
│  └─────────────┘  └─────────────┘  └─────────────┘             │
└─────────────────────────────────────────────────────────────────┘
```

## 📖 Documentation

| Guide | Description |
|-------|-------------|
| [📘 User Guide](docs/guides/user-guide.md) | End-user instructions |
| [📗 Admin Guide](docs/guides/administrator-guide.md) | IT administrator setup |
| [📙 Deployment Guide](docs/guides/deployment-guide.md) | Large-scale rollout |
| [📕 API Reference](docs/api/README.md) | REST & WebSocket APIs |
| [🗺️ Roadmap](docs/roadmap/enterprise-roadmap.md) | Future development plans |

### Product Requirements
- [PRD-001: Management Dashboard](docs/prd/001-management-dashboard.md)
- [PRD-005: Touch Screen UI](docs/prd/005-touch-screen-room-ui.md)
- [PRD-006: Edge AI Features](docs/prd/006-edge-ai-features.md)
- [PRD-008: Cross-Platform Architecture](docs/prd/008-cross-platform-architecture.md)

## 🤝 Contributing

We love contributions! Croom is built by the community, for the community.

### Ways to Contribute

- 🐛 **Report Bugs** - Found an issue? [Open a bug report](https://github.com/amirhmoradi/croom.to/issues/new?template=bug_report.md)
- 💡 **Request Features** - Have an idea? [Submit a feature request](https://github.com/amirhmoradi/croom.to/issues/new?template=feature_request.md)
- 📝 **Improve Docs** - Help us make documentation better
- 💻 **Submit PRs** - Code contributions are welcome!
- ⭐ **Star the Repo** - Show your support!

See our [Contributing Guide](CONTRIBUTING.md) for detailed instructions.

### Development Setup

```bash
# Clone and setup
git clone https://github.com/amirhmoradi/croom.to.git
cd croom

# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Start development servers
make dev
```

## 🌟 Community

- 💬 [GitHub Discussions](https://github.com/amirhmoradi/croom.to/discussions) - Ask questions, share ideas
- 🐛 [Issue Tracker](https://github.com/amirhmoradi/croom.to/issues) - Report bugs, request features
- 📧 [Mailing List](mailto:croom-help@googlegroups.com) - Stay updated

### Contributors

<a href="https://github.com/amirhmoradi/croom.to/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=amirhmoradi/croom.to" />
</a>

## 🏢 Enterprise Offerings (Coming Soon)

While Croom is and will always be **100% open source**, we're planning optional enterprise services for organizations that need additional support:

### Open Source (Free Forever)
- Full Croom functionality
- Community support via GitHub
- Self-hosted deployment
- All core features included

### Enterprise Support (Planned)
| Service | Description |
|---------|-------------|
| **Priority Support** | SLA-backed response times, dedicated support channel |
| **Professional Services** | Deployment assistance, custom integrations |
| **Training** | Administrator and end-user training programs |
| **Managed Updates** | Tested update packages, security patches |

### Enterprise Features (Planned)
| Feature | Description |
|---------|-------------|
| **SSO Integration** | SAML/OIDC with your identity provider |
| **Advanced Analytics** | Meeting quality scoring, usage reports, ROI dashboards |
| **Compliance Packages** | Pre-configured for GDPR, HIPAA, SOC 2 |
| **Multi-Tenant Management** | MSP/reseller dashboard for managing multiple orgs |
| **Hardware Bundles** | Pre-configured, tested hardware kits |

### Vexa Enterprise Integration (Planned)
| Feature | Description |
|---------|-------------|
| **Managed Vexa Cluster** | Hosted transcription infrastructure |
| **Meeting Intelligence Suite** | Advanced analytics, sentiment analysis, coaching |
| **Compliance Recording** | Automated retention policies, legal hold |
| **API Access** | Programmatic access to transcripts and insights |

> **Interested in enterprise offerings?** [Contact us](mailto:enterprise@croom.to) or [open a discussion](https://github.com/amirhmoradi/croom.to/discussions) to share your requirements.

## 📜 License

Croom is [MIT licensed](LICENSE). Use it freely in personal and commercial projects.

## 🙏 Acknowledgments

- Built on the foundation of the original [PiMeet](https://github.com/pmansour/pimeet) project
- Inspired by enterprise solutions like Cisco Webex Room Kit and Poly Studio
- Meeting intelligence powered by [Vexa](https://github.com/Vexa-ai/vexa) — open-source transcription
- Thanks to all [contributors](https://github.com/amirhmoradi/croom.to/graphs/contributors) who make this possible

---

## 📜 Upstream Inspiration

Croom is built upon and extends the excellent work of the original **[PiMeet](https://github.com/pmansour/pimeet)** project created by [Peter Mansour](https://github.com/pmansour).

### About the Original PiMeet

The original PiMeet project pioneered the concept of turning a Raspberry Pi into a dedicated video conferencing appliance. It demonstrated that affordable, single-board computers could effectively replace expensive commercial room systems for basic video conferencing needs.

**Original PiMeet Features:**
- Raspberry Pi-based video conferencing
- Google Meet support via Chromium
- Basic HDMI-CEC display control
- Simple bash-based automation

### What Croom Adds

Croom takes the original vision and expands it into a full enterprise-grade platform:

| Capability | Original PiMeet | Croom |
|------------|-----------------|-------|
| Meeting Platforms | Google Meet | Meet, Teams, Zoom, Webex |
| Hardware Support | Raspberry Pi 4 | Pi 4/5, x86_64 PCs, NUCs |
| AI Features | None | Auto-framing, speaker tracking, noise reduction |
| Management | Manual SSH | Fleet dashboard, zero-touch provisioning |
| Security | Basic | RBAC, audit logs, SSO, encryption |
| UI | None | Touch screen room control interface |
| Transcription | None | Vexa integration for meeting intelligence |

### Contributing Back

We believe in giving back to the open-source community. Improvements that benefit the original project scope will be contributed upstream where appropriate. See our [upstream contributions guide](docs/roadmap/upstream-contributions.md) for details.

**Thank you, Peter, for creating the foundation that made Croom possible!**

---

## 🔍 SEO & Search Keywords

<details>
<summary><b>Click to expand: What people search for when looking for solutions like Croom</b></summary>

### Conference Room Solutions
- **Open source video conferencing room system** - Croom is a fully open-source alternative to proprietary room systems
- **Raspberry Pi conference room** - Transform any Pi into a professional meeting room device
- **DIY video conferencing appliance** - Build your own enterprise-grade conferencing system
- **Self-hosted meeting room device** - Keep all your data on-premise with Croom
- **Linux conference room system** - Native Linux support for Pi and x86_64

### Alternatives & Comparisons
- **Cisco Webex Room Kit alternative** - Save 90%+ with equivalent features
- **Poly Studio alternative open source** - Full-featured replacement at fraction of cost
- **Zoom Rooms alternative self-hosted** - Multi-platform support without vendor lock-in
- **Microsoft Teams Room alternative** - Works with Teams, Meet, Zoom, and Webex
- **Neat Bar alternative** - Edge AI features without the price tag
- **Logitech Rally alternative** - Professional conferencing without expensive hardware

### Technical Features
- **Edge AI video conferencing** - Local AI processing with Hailo, Coral, NVIDIA
- **Auto-framing camera software** - Keep participants in frame automatically
- **Speaker tracking open source** - AI-powered active speaker detection
- **Meeting room noise reduction** - Crystal clear audio processing
- **HDMI-CEC meeting room** - Automatic display power management
- **WebRTC conference room** - Browser-based meeting platform support

### Enterprise & IT
- **Enterprise video conferencing fleet management** - Centralized dashboard for all devices
- **Zero-touch provisioning video conferencing** - QR code enrollment, automatic setup
- **GDPR compliant video conferencing** - European data sovereignty by design
- **SOC 2 video conferencing solution** - Enterprise security compliance ready
- **On-premise video conferencing** - Air-gapped deployment support
- **Meeting room analytics dashboard** - Usage reports and device monitoring

### Hardware & Setup
- **Raspberry Pi 5 video conferencing** - Optimized for latest Pi hardware
- **Raspberry Pi Google Meet** - Native Meet support with calendar integration
- **Raspberry Pi Microsoft Teams** - Full Teams meeting support
- **Raspberry Pi Zoom room** - Zoom web client integration
- **Intel NUC conference room** - x86_64 support for more powerful setups
- **USB webcam conference room software** - Works with any V4L2-compatible camera

### Use Cases
- **Small business video conferencing** - Affordable solution for SMBs
- **Huddle room video system** - Perfect for small meeting spaces
- **Education video conferencing** - Schools and universities deployment
- **Healthcare video conferencing HIPAA** - Compliance-ready for medical use
- **Government video conferencing** - Digital sovereignty for public sector
- **Remote office video conferencing** - Branch office deployment at scale

### Integration & Ecosystem
- **Meeting transcription self-hosted** - Vexa integration for AI transcription
- **Google Calendar meeting room** - Automatic calendar sync
- **Microsoft 365 meeting room** - Azure AD and calendar integration
- **Open source meeting room booking** - Calendar provider agnostic
- **Video conferencing API** - REST and WebSocket APIs for integration

</details>

---

<div align="center">

**⭐ Star us on GitHub — it motivates us a lot!**

[Report Bug](https://github.com/amirhmoradi/croom.to/issues) · [Request Feature](https://github.com/amirhmoradi/croom.to/issues) · [Join Discussion](https://github.com/amirhmoradi/croom.to/discussions)

---

🇫🇷 **A French Initiative for Digital Sovereignty**

*Breaking vendor lock-in. Protecting data privacy. Empowering organizations.*

Made with ❤️ by the Croom Community

</div>
