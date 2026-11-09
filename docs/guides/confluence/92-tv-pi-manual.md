# TV Pi technical manual

*Part of [Conference room technical manuals]. The printable version is attached as crystal-meet-tv-pi-manual.pdf.*

## Overview

*The TV Pi runs the room; every other device is a window onto it*

One TV Pi per room. It owns the single browser page on the TV, reads the room's Google Calendar, joins Google Meet and Zoom when someone presses Join, controls the TV over the network, drives the camera and sound, and serves the pages the touch Pi and door sign show. If `croom` stops, both screens say they can't reach the room.

- **Touch Pi** (on the table): Shows the room page **/**: Join, bookings, Options, Settings, touchpad.
- **TV Pi · croom** (behind the TV): Meetings, calendar, TV control, camera, sound, Settings, updates.
- **TV and MeetUp** (in the room): LG or Samsung TV on HDMI; Logitech MeetUp camera and speakerphone on USB.
- Link: HTTP :8080 page, API, touchpad
- Link: HDMI, USB network to the TV

- **Door sign** (by the door): Shows **/sign**; checks in for jobs and updates.
- **TV Pi · croom** (behind the TV): The touch Pi and door sign never talk to each other, only to it.
- **Google, Zoom, GitHub** (outside): Calendar and Meet; Zoom's SDK and tokens; new versions of the software.
- Link: HTTP :8080 page, check-in
- Link: HTTPS out only

- **8080**: Port for every page and API, plain HTTP on the LAN
- **60 s**: Calendar poll interval
- **2 min**: Watch after a restart before a saved change is undone
- **3 min**: Watch after an update before it is undone

### What runs on it

```
systemd
├── croom.service        (the desktop user)  python -m croom.core.agent -c /etc/croom/config.yaml
│     └── Playwright → Chromium (bundled build in /opt/croom/browsers), one kiosk page on the TV,
│                              profile /var/lib/croom/meet-profile
├── croom-admin.service  (root)  python -m croom.admin --owner <user> --group <group>, socket /run/croom-admin.sock
└── croom-ui.service     (written, normally not enabled: upstream Qt touch UI, untested in this fork)
```

| Service inside croom | What it does in a Crystal Meet room |
| --- | --- |
| tv | Pairs with and controls the TV over the network; wakes it and switches its input before every join. Always registered. |
| meeting | Owns the TV's Chromium (screensaver page `/tv` between calls) and the Google Meet and Zoom providers. |
| calendar | Polls the room's Google calendar and keeps room bookings. |
| devices | Camera pan, tilt, zoom and presets; speaker volume; microphone level. |
| laptop | Laptop mode, only when `laptop.enabled` is true. |
| control | The web server on 8080: room page, door sign, TV page, API, Settings, device check-in. |
| dashboard | Enrolment and heartbeat to the Crystal Meet dashboard, when enabled. |
| scores | Door sign football scores from ESPN, idle until a school is switched on. |
| video, audio, display, ai | Upstream services. Video only lists cameras and leaves the camera free for Meet and Zoom; display probes HDMI-CEC once and then does nothing; audio does nothing useful; ai is off in room configs. |

### What the TV Pi can and cannot do

**CAN**

- **Join a call when someone presses Join**: Google Meet as the room's signed-in Google user, Zoom through Zoom's Meeting SDK.
- **Turn the TV on and pick its input** before every join, over the network.
- **Book the room** in Google Calendar from the touch Pi and door sign, and keep bookings while Google is unreachable.
- **Move the camera** to saved presets and set speaker and mic levels.
- **Lend the MeetUp to a laptop** where a USB switch is fitted.
- **Update itself** from GitHub and undo an update the room does not start with.

**CANNOT**

- **Join or leave by itself.** No auto-join, no auto-leave.
- **Frame people automatically.** The MeetUp's RightSight is Logitech software for Windows and macOS.
- **Control the TV over HDMI-CEC.** TV control is over the network.
- **Lock its pages.** Everything on 8080 is open to the LAN; only Settings has a PIN.
- **Refresh its installer pieces on update.** Units, udev rules, sudoers and apt packages need the installer.

## Hardware and connections

*One HDMI cable, one USB cable, and the network does the rest*

| Item | Detail |
| --- | --- |
| Pi | Raspberry Pi 4 (4 GB) or Raspberry Pi 5, official power supply, 32 GB microSD. The code supports both. |
| Operating system | Raspberry Pi OS (64-bit) **with desktop**, Bookworm or Trixie. The installer needs `aarch64` or `x86_64`; it also accepts jammy and noble and warns on other codenames. |
| Desktop | Auto-login desktop user, normally `pi`. labwc on current Raspberry Pi OS, with an X display at `:0`; the service waits for `/tmp/.X11-unix/X0`. |
| Network | Wi-Fi or Ethernet, with a fixed address (DHCP reservation). The TV must be on the same network segment as the Pi's default-route interface: Wake-on-LAN is a broadcast and TV discovery is multicast. |
| TV | One HDMI cable from the Pi to the TV input named in `tv.pi_input` (default `HDMI_1`). Rooms use LG C6 OLEDs (webOS); the Samsung S90HD is also supported. |
| Camera and speakerphone | Logitech MeetUp on USB (id `046d:0866`): camera, speakerphone and motorised pan, tilt and zoom in one unit. |
| Laptop switch (optional) | ATEN US234 USB sharing switch and an Adafruit relay (product 4409) on the Pi's header. See Section 09. |

> **NOT RECORDED**
>
> The repo does not say which Pi model each room has: Pi 4 (4 GB) or Pi 5. Note it on the Pi when you install or service it. Laptop mode looks for GPIO chip `pinctrl-bcm2711` (Pi 4) or `pinctrl-rp1` (Pi 5).

> **THE CAMERA IS THE MEETUP**
>
> The code is built for the Logitech MeetUp. The room setup guide's example (Brio 505 and Jabra Speak2 55) predates it. Camera framing needs a camera with V4L2 zoom or pan and tilt speed controls.

### Settings needed on the TV

| TV | On the TV | How the Pi talks to it |
| --- | --- | --- |
| LG webOS (C6) | **LG Connect Apps** on. **Mobile TV On** ("Turn on via Wi-Fi") on, unless **Quick Start+** is on. | aiowebostv over ws/wss, ports 3000/3001 (the library's defaults). One pairing prompt, accepted with the TV's remote. Power, inputs, volume and mute read back, apps, channels, every remote key, the TV's own pointer. |
| Samsung Tizen (S90HD) | **Power On with Mobile** on. If the prompt keeps coming back, set **Device Connection Manager › Access Notification** to **First Time Only**. | Local secure WebSocket on 8002, REST on https 8002, http 8001 for the MAC. No SmartThings account. No input, volume or mute readback; Input opens the source menu; no TV pointer; no automatic HDMI switch before a join. |
| Both | A fixed address for the TV (reserve it on the router) and its MAC address. | Wake-on-LAN is always sent first, so the MAC is needed for every Turn on, even with Quick Start+. |

> **NOT HDMI-CEC**
>
> HDMI-CEC does not turn the TV on or off or switch its input. Upstream's CEC service still starts and probes cec-client and /dev/cec0 once, but nothing calls it. All TV control is over the network. The old tv-lifecycle.service and tv-on.sh scripts in the repo are not installed and should not be used.

### Network

- **In:** TCP 8080 from the touch Pi, door sign and laptops on the LAN; UDP replies from TVs during Find TVs. A firewall that blocks inbound UDP hides every TV.
- **Out:** the TV (LG 3000/3001, Samsung 8002), Google, Zoom, GitHub, the dashboard on 3001, and ESPN for door sign scores. The full list is in Section 13.
- **Never to the other screens.** The touch Pi and door sign poll the TV Pi and check in every 5 seconds; the TV Pi never connects to them.

## Installing a TV Pi

*One installer run takes a blank card to a working room*

Prepare the card, get a dashboard token, run `installer/install.sh` with the room's config and keys, then finish the room from the touch Pi. The installer itself takes 10 to 20 minutes.

> **BEFORE YOU BEGIN**
>
> **The Pi, cabled:** TV on HDMI, MeetUp on USB, power last.
>
> **Raspberry Pi Imager** on a computer, and a 32 GB microSD card.
>
> **The room calendar's address**, `c_…@resource.calendar.google.com`, and the dashboard's address.
>
> **The keys:** `google-service-account.json` and `zoom-credentials.json` (template `deploy/rooms/zoom-credentials.example.json`).
>
> **The room's Google user** (for example `room1@crystalpm.com`) and its password, for the sign-in afterwards.

### 1 · Prepare the Pi

1. In Raspberry Pi Imager choose **Raspberry Pi OS (64-bit)** with desktop. Set the hostname (for example `crystal-meet-room-1`), user `pi`, Wi-Fi, time zone, and turn SSH on. Write the card.
2. Put the card in the Pi and power it. The desktop appears on the TV.
3. Open **Preferences › Raspberry Pi Configuration › System** and turn **Auto Login** on. The room then comes back by itself after a power cut.
4. Reserve the Pi's IP address on the router.

### 2 · Get the room's dashboard token

1. On the dashboard open **Provisioning**, type the room's name and press **Generate Token**.
2. Copy the token. It works once: a reinstall needs a new one.

### 3 · Run the installer

As the desktop user, over SSH or in a terminal on the Pi:

```
sudo apt update && sudo apt install -y git
git clone https://github.com/ben-abeo/croom.to.git && cd croom.to
cp deploy/rooms/room-1.yaml ~/room.yaml       # room-2.yaml or room-3.yaml for the other rooms
nano ~/room.yaml
# set room.name, room.location (optional), dashboard.url (http://<dashboard>:3001),
# dashboard.enrollment_token and calendar.google_calendar_id
sudo bash installer/install.sh --config ~/room.yaml \
     --credentials ~/google-service-account.json \
     --zoom-credentials ~/zoom-credentials.json       # 10 to 20 minutes
sudo systemctl start croom
sudo reboot                                    # once, so the US keyboard layout takes effect
rm ~/google-service-account.json ~/zoom-credentials.json
# the installed copies are in /etc/croom
```

| Installer option | Effect |
| --- | --- |
| **--config FILE** | Copied to `/etc/croom/config.yaml` (room user, mode 640). Without it an existing config is kept, or a stock one written. |
| **--credentials FILE** | Google key to `/etc/croom/google-service-account.json`, mode 600. |
| **--zoom-credentials FILE** | Zoom keys to `/etc/croom/zoom-credentials.json`, mode 600. |
| **--enable-ui** | Enables the upstream Qt touch UI. Not used in Crystal Meet rooms. |
| **--no-service** | Parsed but **not honoured**. |
| **CROOM_REPO=…** | Environment variable overriding the pip source (default `git+https://github.com/ben-abeo/croom.to.git`, branch main). Builds a release named `local-<epoch>`. |

> **GOOD TO KNOW**
>
> The installer must run under `sudo` from the desktop user: `$SUDO_USER` becomes the service user. It refuses to run as root directly. The finishing text prints the exact `--check-calendar`, `--check-zoom` and `--sign-in-meet` commands for this Pi.

### What the installer does, in order

| Step | What changes on the Pi |
| --- | --- |
| **Checks** | Run under sudo, a real desktop user, a supported architecture and release. |
| **User groups** | Adds the user to `video, audio, input, dialout, gpio`. |
| **Virtual mouse** | `/etc/udev/rules.d/99-croom-uinput.rules` and `/etc/modules-load.d/croom-uinput.conf`; loads `uinput`. |
| **US keyboard** | `/etc/default/keyboard` and `~/.config/labwc/environment`. Sign in to Google types US key codes; the default GB layout turns `@` into `"`. Applies after a reboot. |
| **Folders** | `/opt/croom`, `/etc/croom`, `/var/lib/croom`, `/var/lib/croom/meet-profile` (700), `/opt/croom/browsers`, `/var/lib/croom-admin` (root, 700). |
| **Packages** | python3, venv, python3-dev, gcc, libc6-dev (to build evdev), chromium, pulseaudio, libpulse0, v4l-utils, libcamera-apps, cec-utils, git, curl, wget, and Qt packages (may warn). |
| **The software** | Builds `/opt/croom/releases/<commit>/venv` from main, points the `/opt/croom/venv` link at it, keeps two releases, then installs Playwright's Chromium into `/opt/croom/browsers`. |
| **Config and keys** | Copies the config, saves `config.yaml.good`, installs the keys (mode 600). |
| **Services** | Writes `croom.service`, `croom-admin.service`, `croom-ui.service`; enables croom; enables and starts croom-admin. |
| **Desktop icon and sudoers** | **Start Crystal Meet** on the desktop, and `/etc/sudoers.d/croom` letting the user run `systemctl start|restart croom` without a password. |

### The unit files it writes

```
# /etc/systemd/system/croom.service
[Unit]
Description=Crystal Meet room agent (croom)
After=display-manager.service network-online.target pulseaudio.service
Wants=network-online.target
[Service]
Type=simple
User=<user>
Group=<user's primary group>
WorkingDirectory=/opt/croom
ExecStartPre=/usr/bin/timeout 120 /bin/sh -c 'until [ -S /tmp/.X11-unix/X0 ]; do sleep 2; done'
ExecStart=/opt/croom/venv/bin/python -m croom.core.agent -c /etc/croom/config.yaml
Restart=always
RestartSec=10
Environment=DISPLAY=:0
Environment=XDG_RUNTIME_DIR=/run/user/<uid>
Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/croom/browsers
[Install]
WantedBy=multi-user.target

# /etc/systemd/system/croom-admin.service
[Service]
ExecStart=/opt/croom/venv/bin/python -m croom.admin --owner <user> --group <group>
Restart=always
RestartSec=5
```

### 4 · Finish the room from the touch Pi

1. Open `http://<TV Pi>:8080/` on the touch Pi or a laptop. Use the IP address: many tablets do not resolve `.local` names. The room page appears.
2. Open **Options › Settings** and choose a PIN of 4 to 8 digits. Record it in the office password manager.
3. Set up the TV under **Options › TV › Find TVs** (Section 08). The TV shows the screensaver after pairing.
4. Sign the room in to Google with **Settings › Meetings › Sign in to Google** (Section 06). The table shows "Signed in as …".
5. Set up the camera under **Options › Camera framing**: **Find the stops**, nudge, **Save as Home**, save presets.
6. On the table Pi run `sudo bash installer/install-kiosk.sh --url http://<TV Pi>:8080/`, then tap **Allow** under **Settings › Software › Devices waiting to join**.
7. Run each test under **Settings › Check**: Calendar, TV, Google sign-in, Zoom keys.

*You should now see every check pass, the TV Pi's card under **Settings › Software** with its version, and the room online on the dashboard's Devices page.*

### Upgrading a room from before room settings (once, by hand)

```
sudo systemctl stop croom
cd ~/croom.to && git checkout main && git pull
sudo bash installer/install.sh          # no --config: the config and keys are kept
sudo reboot
```

If the installer stops with an error, `sudo systemctl start croom` brings the room back on the old version. Do not **Switch back** to the version from before room settings afterwards: it has no helper, and Settings does not offer it.

## Configuration

*One file holds the room's configuration, and Settings edits it safely*

`/etc/croom/config.yaml` is the single source of truth (room user, mode 640). Settings on the table writes it through the root helper, keeps the five previous versions as `config.yaml.1` to `.5`, and keeps `config.yaml.good`, the config the room last started healthy with. Comments in the file are lost at the first Settings save.

### The keys a Crystal Meet room uses

"Live" keys apply at once; every other change applies after **Restart now**. Full defaults are in `src/croom/core/config.py`.

| Key | Default | Settings page | Notes |
| --- | --- | --- | --- |
| room.name | Conference Room | Room | Shown on every screen and the dashboard; the Meet and Zoom name falls back to it. |
| room.location | "" | Room | A `REPLACE_…` placeholder counts as none. |
| room.timezone | UTC (room files America/Chicago) | Room | Also set the OS time zone (Localisation): it drives event times. |
| room.company | Crystal Practice Management | Room | Door sign; blank shows the logo alone. |
| meeting.platforms | room files [zoom, google_meet] | Meetings | Joins are limited to these platforms' hosts. |
| meeting.camera_default_on, mic_default_on | true | Meetings | Camera and mic state at join. |
| meeting.kiosk | true | Meetings | Full-screen TV browser with no tab strip. |
| meeting.zoom_credentials_path | room files /etc/croom/zoom-credentials.json | Meetings | Without it Zoom falls back to its web client, which Zoom blocks. |
| meeting.google_profile_dir | room files /var/lib/croom/meet-profile | Meetings | Without it the TV browser is a guest and Meet refuses it. |
| meeting.meet_api_domain | "" | Meetings | For example `crystalpm.com`; blank means off. |
| meeting.dialogs.acknowledge | 9 phrases | Meetings | One-button notices the room presses at once. |
| meeting.dialogs.ask_everything | false | Meetings | Every dialog waits for a tap on the table. |
| meeting.controls.reactions | Thumbs Up, Clap, Heart, Joy, Open Mouth, Tada | Meetings | `[]` hides Reactions. |
| calendar.providers | room files [google] | — | Only the first is used. |
| calendar.google_credentials_path | "" | Calendar | `/etc/croom/google-service-account.json` |
| calendar.google_calendar_id | "" | Calendar | `c_…@resource.calendar.google.com` |
| calendar.sync_interval_seconds | 60 | — | Poll interval. |
| calendar.booking_start, booking_end | 08:00, 18:00 | Calendar (live) | On the quarter hour; end after start. |
| control.enabled, host, port | true, 0.0.0.0, 8080 | Refused | Settings refuses to change them. |
| control.screensaver | info | — | First style only; the table's choice is kept in `control-settings.json`. |
| control.camera_presets | Wide, Table, Whiteboard | Devices (live) | Names of preset slots 1 to 3. |
| audio.output_device, input_device | auto | Devices | Part of a PipeWire device's name. |
| video.device | auto | Devices | Or `/dev/videoN`. |
| video.ptz_travel_seconds | 8 | Devices | 1 to 60 in Settings. |
| tv.enabled, brand, host, mac | false, lg, "", "" | Options › TV | Find TVs saves a selection in `tv-settings.json` that overrides these. Put a MAC in quotes. |
| tv.pi_input, laptop_input | HDMI_1, "" | TV connection settings | The laptop input must differ from the Pi's. |
| tv.keep_on | true | — | Wake at start, before every join, and after an unexpected off. |
| laptop.* | see Section 09 | Laptop switch | enabled, gpio_pin, active_low, press_ms, usb_id, switch_wait_s. |
| ai.* | false in room files | AI & dashboard | Upstream; not used. |
| dashboard.enabled, url, enrollment_token, heartbeat_interval_seconds | true, "", "", 30 | AI & dashboard | URL `http://<dashboard>:3001`; the token works once; heartbeat 5 to 55 s. |
| data_dir | "" | — | Overrides `/var/lib/croom`. |

> **AN UNKNOWN KEY THROWS THE WHOLE FILE AWAY**
>
> An unknown key **inside a section** (other than under meeting.dialogs or meeting.controls) makes the agent discard the whole config and run on built-in defaults. The one warning line is printed to stdout and reaches the journal only when the service stops. An unknown top-level key is ignored. After a hand edit, compare with config.yaml.good.

### Credentials

| What | Holds | Put there by | Checked by |
| --- | --- | --- | --- |
| /etc/croom/google-service-account.json | The Google Cloud service-account key (calendar; also the Meet API). | Installer `--credentials`, or **Settings › Calendar**: Upload or From USB stick. | `croom --check-calendar`; **Check › Calendar** |
| /etc/croom/zoom-credentials.json | Zoom SDK pair, plus the server-to-server values and room user. | Installer `--zoom-credentials`, or **Settings › Meetings**: upload, USB or Type the keys. | `croom --check-zoom`; **Check › Zoom keys** |
| Google session | Cookies in `/var/lib/croom/meet-profile`. | **Sign in to Google**, or `croom --sign-in-meet`. | **Check › Google sign-in** |
| TV keys | `tv-client-key` or `tv-<hash>-key` in the data folder. | Pairing from the table. | `croom --check-tv` |
| Settings PIN | `settings-pin.json` (scrypt hash). | First Settings open. | — |
| Dashboard | Token in config, then `dashboard-state.json`. | Config, or **Settings › AI & dashboard**. | Dashboard › Devices |

Keys saved from Settings are written to `/etc/croom` (mode 600), up to 64 KB, and apply after a restart. A key on a USB stick must be a `.json` file at the top of the stick. To revoke: delete the service-account key in the Cloud console, suspend the room's Workspace user, or deactivate the Zoom apps.

### Settings on the touch Pi

- **Where:** **Options › Settings** on the touch Pi, or the same page in a laptop browser at `http://<TV Pi>:8080/`.
- **PIN:** 4 to 8 digits. Unlocked for 5 minutes after the last use; **Lock** ends it; a service restart locks every screen.
- **Lockout:** 5 wrong PINs in a row lock every screen for 10 minutes. A restart clears it.
- **Forgotten PIN:** `sudo rm /var/lib/croom/settings-pin.json` on the TV Pi. No restart needed; choose a new PIN on the next open.
- **Saving:** shows **Saved**, or **Saved: applies after a restart** with **Restart now**. Live fields: booking hours and camera preset names.

| Page | What it changes on the TV Pi |
| --- | --- |
| Room | Name, location, company, time zone. |
| Calendar | Room calendar address, Google key (Upload, From USB stick), booking hours (live). |
| Meetings | Platforms, camera and mic at join, kiosk, dialogs the room answers, reactions, Zoom SDK keys, Meet participants domain, Google sign-in folder and **Sign in to Google**. |
| Devices | Speaker, microphone and camera (from what the Pi finds now, or Automatic), camera travel time, preset names (live). |
| TV | Opens **Options › TV**: remote, Find TVs, Pair, Forget TV, connection settings (MAC, the Pi's HDMI input, Laptop input). |
| Laptop switch | On or off, GPIO pin, active low, press length, USB id, wait; **Test the switch**. |
| AI & dashboard | Upstream AI switches; dashboard on or off, address, enrolment token. |
| Network | Each device's Wi-Fi or Ethernet, network, signal, address; **Change Wi-Fi** with a 90-second safety net; **Forget**. |
| Check | Calendar, TV, Laptop switch, Google sign-in, Zoom keys (each up to 60 s); **Capture the next Google Meet call**. |
| Logs | The croom journal, newest first, 200 lines a page, All or Warnings and errors. |
| Software | Each device's version and the newest; **Update**, **Reboot**, **Restart room software**, **Switch back to**, **Update all**; Devices waiting to join. |
| Security | Change the PIN. |

- **Refused in a meeting:** restart, reboot, update, Wi-Fi changes and Google sign-in answer "Leave the meeting first".
- **Refused in Laptop mode:** restart, reboot, update and sign-in answer "The camera is on the laptop: press Back to room first".
- **No helper:** pages that need the root helper say "Not available on this device" (Section 10).

> **NOT YET VERIFIED**
>
> Settings' room-management jobs (NetworkManager, systemd, journald, pip, git, GitHub) are verified against stand-ins, not yet on a room's own Pis. The README's hands-on checklist is still to be run.

### The table's own choices: control-settings.json

Kept in `/var/lib/croom/control-settings.json` (mode 600), not in config.yaml: `screensaver` (info, quiet, brand, bounce), `key_click`, `sign_background`, `sign_scores`, `camera` (home and presets) and `mic_level`. If the file can't be written the choice is held in memory until the next restart, with the log line "Could not save the room settings to …".

## Google Calendar and bookings

*The room reads one Google calendar every minute and keeps booking when Google is away*

A Google service account reads the room's resource calendar and writes bookings made at the touch Pi and door sign. The room keeps its last read and its own bookings on disk, so the screens work through an outage and sync when Google returns.

### Setting it up

1. In the Google Cloud project "Crystal Meet", enable the Calendar API and create a service account (for example `crystal-meet-rooms@….iam.gserviceaccount.com`) with no roles. Download a JSON key.
2. In Google Calendar, share the room's resource calendar with the service account's address with **Make changes to events**. The Workspace setting for secondary calendars must be "Share all information, and outsiders can change calendars".
3. On the touch Pi, **Settings › Calendar**: enter the **Room calendar address** and upload the key. Tap **Restart now**.
4. Run **Settings › Check › Calendar**, or the command below. The last line reads "Bookings: the door sign and the room page can book this calendar."

```
sudo -u pi /opt/croom/venv/bin/croom --check-calendar -c /etc/croom/config.yaml
# Google Calendar: connected as crystal-meet-rooms@….iam.gserviceaccount.com
# Calendar: Room 1 (c_…@resource.calendar.google.com)
# Bookings in the next 7 days:   (the first 10)
# Bookings: the door sign and the room page can book this calendar.
# exit 0 when everything works, 1 otherwise; run it as the service user, who can read the key
```

The check also writes and deletes a test event, "Crystal Meet booking check", dated 2000-01-01, to prove it can write. It reports bookings still waiting to be sent and any that are double-booked.

### How the room reads the calendar

- **Every 60 seconds** it reads from one hour ago to seven days ahead, up to 100 events, from the one calendar in `google_calendar_id`.
- **Dropped:** cancelled events and events the room resource declined (double bookings).
- **Which link wins:** the event's conference link, except that a typed Zoom, Teams or Webex link beats an automatically added Meet link.
- **Zoom passcode:** from `pwd=` in the link, else from "Passcode:" or "Password:" in the location or description.
- **Join now** appears on the room page from 10 minutes before a booking until it ends. Nothing joins automatically.
- **Not configured** (no key, unreadable key, empty or `REPLACE_` address): one warning in the log; the room still joins pasted links.

### Bookings made at the room's screens

| Action | Rule |
| --- | --- |
| Book | Today and the next 6 days, every quarter hour inside booking hours, 15 minutes to 4 hours, stopped by the next booking and the end of booking hours. |
| Use now | Starts this minute, ends on the quarter hour nearest to now plus the length. Not limited to booking hours. |
| End now | Touch Pi only. Shortens a booking in progress; cancels one that has not started. A meeting people booked in Google is declined by the room (or shortened, in the room's own calendar), kept in `ended-early.json`. |
| Cancel | Only a booking made at the room's screens that has not started. |

- **Saved first, then sent.** A booking goes into `bookings.json`, then to Google with a 10-second wait at the screen. If Google doesn't answer it is queued.
- **Tagged.** Each event carries the private property `croom_id` and the description "Booked on the <room> door sign" or "Started on the <room> room page".
- **Queue order.** Queued changes go oldest first at the start of each poll, and the round stops at the first failure.
- **Clashes.** While Google answers, a taken time is refused: "That time was just booked in Google Calendar: <title>". A booking queued offline is written anyway and shows "Double-booked with <title>".
- **Google wins** for settled bookings: moving or deleting an event in Google changes the room's booking.
- **Offline.** The last read is in `calendar-cache.json` (it includes links and passcodes, mode 600). Screens say "Calendar offline since 10:42 AM. Bookings made here will sync."

> **GOOD TO KNOW**
>
> With the placeholder calendar address or a missing key, the screens still accept bookings, but they stay "Not in Google Calendar yet" forever. A calendar shared read-only does the same, with "The room's calendar does not take bookings from Crystal Meet: share it with the service account with Make changes to events."

## Google Meet

*Meet joins as the room's own Google user, signed in once on the TV*

Meet refuses a browser driven by automation when it joins as a guest ("You can't join this video call"), but accepts it once signed in. So each room has its own Google Workspace user, signed in once into the TV browser's profile at `/var/lib/croom/meet-profile`.

### The room's Google account

- **One Workspace user per room:** `room1@crystalpm.com`, `room2@`, `room3@`. Its name is the name people see in calls.
- **An organisational unit "Meeting rooms"** with Google session control set to **never expire**. Without it, rooms are signed out on Workspace's schedule.
- **2-Step Verification** allowed but not enforced for that unit.

### Sign in from the table (preferred)

1. Open **Settings › Meetings**. If **Google sign-in** has no folder, tap **Use /var/lib/croom/meet-profile**, then **Restart now**.
2. Tap **Sign in to Google**. The TV's browser closes and Google's sign-in page fills the TV. The table shows a full-screen touchpad with **Type on the TV**, **Done** and **Cancel**.
3. Use the touchpad to click Google's email field, then **Type on the TV** to enter the room's address.
4. Enter the password and accept Google's prompts the same way.
5. When Google shows the account, tap **Done**. The table says "Signed in as <address>" or "Signed in as the room's Google account" (both mean success), and the TV gets its screensaver back.

- **Nothing changes on a failed attempt.** **Cancel**, 15 minutes without a touch, the browser closing by itself, or the service stopping put the old cookies back: "Sign-in cancelled: nothing changed".
- **Refused** in a meeting, in Laptop mode, and with no sign-in folder ("Set up Google Meet first: …"). A join waits until the sign-in ends.
- **A leftover backup.** The sign-in copies the cookies to `<profile>/.croom-sign-in-before/`; a folder left behind means a sign-in did not finish cleanly.

### Sign in from a terminal

Use this over VNC when the table can't be used. The service must be stopped, and the command must run as the service user, never as root.

```
sudo raspi-config nonint do_vnc 0          # once, to see the TV's screen from a laptop
sudo systemctl stop croom
sudo -u pi DISPLAY=:0 /opt/croom/venv/bin/croom --sign-in-meet -c /etc/croom/config.yaml
# sign in as room1@crystalpm.com in the VNC window, close the browser → "Profile saved. Test it with: …"
sudo -u pi DISPLAY=:0 /opt/croom/venv/bin/croom --check-meet https://meet.google.com/abc-defg-hij -c /etc/croom/config.yaml
sudo systemctl start croom
sudo raspi-config nonint do_vnc 1          # optional: VNC off again
```

The terminal sign-in takes no backup of the cookies. "No session was saved: the browser closed before the sign-in finished" means run it again.

> **VERIFY ON A PI**
>
> Both sign-in checks read only `<profile>/Default/Cookies`, looking for Google's `SID`, `__Secure-1PSID` or `__Secure-3PSID`. If the Pi's Chromium keeps cookies in `Default/Network/Cookies`, a sign-in that worked can still show as not saved. Check with `ls /var/lib/croom/meet-profile/Default/`.

Every Chromium on the room's profile runs with `--profile-directory=Default --allow-browser-signin=false`. Before this fix (commit 0b7b177), Chromium moved the Workspace account into a new "Profile 1" where neither the TV nor the check looked. A stray `Profile 1` folder is ignored.

### How a Meet join runs

| Stage | What happens |
| --- | --- |
| **TV** | The TV is woken and switched to the Pi's input (up to 15 seconds; never blocks the join). |
| **Page** | The TV browser opens `https://meet.google.com/<code>`, fills the name if asked, and sets camera and mic to the defaults. |
| **Join** | Presses **Join now** or **Ask to join**, then waits up to 30 seconds for the Leave button, or up to 5 minutes in the lobby. |
| **In the call** | Mute (Ctrl+D), camera (Ctrl+E), raise hand (Ctrl+Alt+H), captions (C), plus chat, people, pin, host switches, layout, reactions, admit and deny, mute everyone and end. |
| **Failure** | The table quotes what Meet showed; a screenshot goes to `/tmp/croom-meet-failure.png`; the TV goes back to the screensaver. |

> **NOT YET VERIFIED**
>
> Meet's English button labels the room looks for are "not yet confirmed on a room Pi". On the first real calls, save `GET /api/meeting/diagnostics` (Section 11). A control whose button isn't found is hidden rather than pressed wrongly.

### Participants from the Meet API (optional)

1. In the Admin console, grant domain-wide delegation of `https://www.googleapis.com/auth/meetings.space.readonly` to the calendar service account's client ID.
2. Enable the Google Meet API in the service account's Cloud project.
3. Set **Meet participants domain** (`meeting.meet_api_domain`) to `crystalpm.com` and restart.

The list is read every 15 seconds for meetings organised in that domain. If Google refuses, the room waits 5 minutes before asking again and reads the list from Meet's page meanwhile.

### Capture the next Google Meet call

**Settings › Check › Capture the next Google Meet call** records Meet's panels 1, 3 or 5 minutes into the next Meet call (armed for two hours, forgotten on restart). Names, emails and chat are redacted. It never presses admit, remove, mute all, end or leave. The newest three zips are kept as `/var/lib/croom/meet-capture-*.zip`; download the newest from the same page.

## Zoom

*Zoom runs in Zoom's own web SDK, signed for each join as the room's Zoom user*

The TV Pi serves a page on the Pi's own loopback address that loads Zoom's Meeting SDK for Web 6.5.0, signs each join with the SDK app's keys, and adds the room user's ZAK so meetings hosted by other accounts can be joined. Without the credentials file Zoom falls back to its public web client, which Zoom blocks for automated browsers.

### What Zoom needs

| Piece | Setup | Used for |
| --- | --- | --- |
| Meeting SDK app | Zoom Marketplace "General App", admin-managed, Meeting SDK on. The development pair joins Crystal PM's own meetings; other companies' meetings need Zoom's review (submitted "External Zoom users", then set Unlisted) and the production pair. | `sdk_client_id`, `sdk_client_secret`: an HS256 signature per join, valid 2 hours. |
| Server-to-Server OAuth app | Scopes `user:read:zak:admin` and `user:read:token:admin`; activated. | `account_id`, `s2s_client_id`, `s2s_client_secret`: an account token, then the room's ZAK at every join. |
| Room's Zoom user | One per room, Basic (free), display name = the room's name. | `room_user`: whose ZAK the room joins with. |

```
# /etc/croom/zoom-credentials.json  (room user, mode 600)
{
  "sdk_client_id": "…",          # required
  "sdk_client_secret": "…",      # required
  "account_id": "…",             # these four go together, or not at all
  "s2s_client_id": "…",
  "s2s_client_secret": "…",
  "room_user": "room1@crystalpm.com"
}
```

1. Put the file in place: **Settings › Meetings › Zoom SDK keys** (upload, USB stick, or **Type the keys**, which keeps the server-to-server values), or `sudo install -o pi -g pi -m 600 ~/zoom-credentials.json /etc/croom/zoom-credentials.json`.
2. Restart the room software. The log shows "Zoom Meeting SDK provider ready (page on http://127.0.0.1:<port>/meeting)".
3. Run **Settings › Check › Zoom keys**, or the command below. It ends with "Ready: …".

```
sudo -u pi /opt/croom/venv/bin/croom --check-zoom -c /etc/croom/config.yaml
# Zoom Meeting SDK: app …, signature minted (Zoom checks it only when a meeting is joined)
# Zoom account: server-to-server token obtained for account …
# Zoom room user: room1@crystalpm.com, ZAK obtained (valid 2 hours)
# Ready: this room can join meetings on your own Zoom account now; …
# with no room_user it says so and still exits 0: own-account meetings only
```

### How a Zoom join runs

- **Tokens first.** An account token (cached until 60 s before it expires), then a fresh ZAK, each with a 15-second timeout. A failure here opens nothing on the TV.
- **The page.** The TV browser opens `http://127.0.0.1:<port>/meeting#<one-time token>`, which loads the SDK from `source.zoom.us/6.5.0/`.
- **Waiting.** Up to 90 seconds to connect, 300 seconds in a waiting room.
- **Camera and mic.** The camera is set both ways at join; the mic only towards muted, so a host's mute-on-entry stays.
- **Passcode.** From `?pwd=`, the invitation text, or the room page's Passcode field (letters, digits, `@ - _ *`, up to 32). "Zoom needs the meeting passcode (…)" makes the page ask.
- **Dialogs.** One-button notices matching `meeting.dialogs.acknowledge` (recording, transcription, AI Companion, mute notices) are pressed at once; every other dialog waits for a tap on the table. Each is logged.

> **NOT YET VERIFIED**
>
> Joining meetings hosted by other companies has not been tested against a live meeting. It needs Zoom's approval of the SDK app, the production pair and the ZAK. If Zoom still refuses, the fallback is the SDK app's own OAuth, which is not built.

> **GOOD TO KNOW**
>
> The SDK version is set once, `SDK_VERSION = "6.5.0"` in `src/croom/meeting/providers/zoom_sdk_site.py`. When Zoom retires it from its CDN, bump it there and release. Zoom's button labels were read from the 6.5.0 files, not a live meeting.

## TV control, camera and audio

*The TV, the camera and the sound are all set up from the table*

### Pairing the TV

1. Turn the TV on and have its remote ready. Check the settings in Section 02.
2. On the touch Pi open **Options › TV** and tap **Find TVs**. LG and Samsung TVs on the network are listed after a few seconds.
3. Select the room's TV (within 5 minutes of the search). Pairing starts and the TV shows a "Crystal Meet" prompt.
4. Accept the prompt with the TV's remote within 60 seconds. The table shows the TV as paired; no restart is needed.
5. Open the TV's connection settings and check the **MAC address**, the Pi's HDMI input and, if fitted, the **Laptop input**.

The choice is saved in `/var/lib/croom/tv-settings.json`, which overrides the `tv:` section of config.yaml. The pairing key is `tv-<hash>-key` in the same folder. Never delete key files by hand: use **Forget TV**, then **Pair**.

### Keeping the TV on

- **At start and before every join** (with `tv.keep_on: true`): Wake-on-LAN, resent every 3 seconds for up to 15 seconds, then a switch to `tv.pi_input` if the TV shows another input or an app. The join goes ahead after 15 seconds either way.
- **After an unexpected off**, such as the TV's own remote, it is woken again 5 seconds later.
- **Off from the table** (second tap) holds the TV off until a manual On or the next join. A restart forgets the hold.
- **Turn on from the table** can take up to about 23 seconds.
- **Reconnects** back off from 2 to 30 seconds. A TV that refused pairing is not retried until **Forget TV** and **Pair**.

### The touchpad

The table's touchpad uses the WebSocket `/ws/pointer`. On LG it drives the TV's own cursor; otherwise it drives the Pi's virtual mouse "Crystal Meet touchpad" on `/dev/uinput`, which needs the udev rule, the `input` group and the `uinput` module set up by the installer. The pointer is hidden on the TV and shown for 5 seconds after it moves. The same mouse parks the pointer in the screen's bottom-right corner as the desktop starts (`python -m croom.tv.park_pointer` in `~/.config/labwc/autostart`), as the TV's browser starts and each time the screensaver loads, so no pointer shows before the browser is up or after a call.

### Checking the TV from a terminal

```
sudo -u pi /opt/croom/venv/bin/croom --check-tv -c /etc/croom/config.yaml
# as the service user, never root: the key is mode 600, and a check as root
# could leave a renewed key the service can't read
# never wakes, pairs or switches; exit 0 when the TV answered
# with no TV configured it lists the TVs it finds
# ends "Ready: the TV answers." or "The TV answers, with the warnings above."
```

> **NOT YET VERIFIED**
>
> TV control of the LG C6 and the Samsung S90HD (remote, pairing, touchpad) has only been tested against a fake TV and a fake virtual mouse, not yet on a real TV behind a room Pi. The Samsung needs a physical acceptance test.

### Camera framing

The MeetUp reports no pan or tilt position, so the room estimates it in seconds of travel from the end stops. Set it up once under **Options › Camera framing**:

1. Tap **Find the stops**. The camera drives to its corner for `video.ptz_travel_seconds` (8 by default) and the position becomes known.
2. Nudge the camera to the room's normal view and tap **Save as Home**.
3. Nudge to each other view and save presets 1 to 3 (named by `control.camera_presets`: Wide, Table, Whiteboard).

- **Homes at every start** once a home is saved. Presets are in `control-settings.json`, key `camera`.
- **Drift.** If presets land wrong, find the stops again, save home, re-save presets, or adjust the travel time.
- **Preview.** While someone uses Camera framing between meetings, the TV shows the camera for up to 180 seconds. Not during a call.
- **The camera stays free for calls.** The room opens the camera only for control calls; Chromium owns the picture.

> **GOOD TO KNOW**
>
> Automatic framing is not available: the MeetUp's RightSight runs only in Logitech's Windows and macOS software, and upstream's AI features are off in every room. Coming in a later release: the live camera picture on the touch Pi, with drag to turn and pinch to zoom.

### Sound

- **PipeWire** of the desktop session, through `pw-dump` and `wpctl`. Chromium reaches it through the Pulse layer, which needs `libpulse0`.
- **Speaker:** `audio.output_device`, `auto` or part of a device name (`MeetUp`, `HDMI` for the TV's speakers). A named device is also made the default, and WirePlumber remembers it: to change back, name the other device rather than `auto`.
- **Microphone:** `audio.input_device`, same rules. **Mic level** runs 0 to 150 (100 normal) and is re-applied at join and every 5 seconds in a call, because the browser's gain control moves it.

## Laptop mode

*Laptop mode lends the MeetUp to a laptop through a USB switch*

For a call on someone's laptop: a relay on the TV Pi presses the ATEN US234's button, the MeetUp moves to the laptop's port, and the TV switches to the laptop's HDMI input. At the end of the booking it all moves back.

> **NOT YET VERIFIED**
>
> Laptop mode has never run on the real ATEN US234 and relay: every test uses a fake switch. The MeetUp's USB id `046d:0866` is still to be confirmed with `lsusb` on the TV Pi. Run **Test the switch** before relying on it.

### Wiring

| Connection | Where |
| --- | --- |
| MeetUp | A device port of the US234 |
| TV Pi | US234 computer port 1 |
| Laptop cable on the table | US234 computer port 2; the laptop has its own HDMI cable to the TV (`tv.laptop_input`) |
| Relay red | Pi header pin 1 (3.3 V); pin 2 (5 V) if the relay doesn't click |
| Relay black | Pin 9 (GND) |
| Relay white | Pin 11 (GPIO17) |
| Relay COM and NO | Soldered across the two contacts of the US234's remote button, either order. The hand button keeps working. |

### Settings › Laptop switch

| Key | Default | Range |
| --- | --- | --- |
| **laptop.enabled** | false | Applies after a restart |
| **laptop.gpio_pin** | 17 (BCM, header pin 11) | 0 to 27 |
| **laptop.active_low** | false | True for relays that trigger on low |
| **laptop.press_ms** | 200 | 50 to 2000 |
| **laptop.usb_id** | 046d:0866 | vendor:product, four hex digits each |
| **laptop.switch_wait_s** | 10 | 1 to 60 |

### How it switches

- **To the laptop.** Uses the end of the booking in progress; otherwise books "Laptop" with Use now for the minutes chosen; with no calendar there is no end. Releases the camera, presses the relay, and waits up to 10 seconds for the MeetUp to leave (read from `/sys/bus/usb/devices`), pressing once more if needed. Then switches the TV to the laptop's input.
- **Back to room.** By hand, or automatically at the booking's end (followed every 2 seconds, so moving the booking in Google moves the end). Presses back, finds the camera again within 15 seconds, homes it, switches the TV to the Pi. Ends only a booking Laptop mode made.
- **If the switch doesn't move:** two presses fail, the booking it made is withdrawn, and the table says "The switch did not answer: press the button on the switch box".
- **The hand button is followed.** Two consecutive looks that agree are enough. State survives restarts in `laptop.json`.
- **While the laptop has the MeetUp:** joins, restart, reboot, update and Google sign-in are refused with "The camera is on the laptop: press Back to room first".

### Testing the switch

1. With the service running, open **Settings › Laptop switch** and tap **Test the switch**. It presses, then brings the MeetUp back, and reports "Pressed again: the MeetUp is back on the Pi (X.X s)."
2. Or, from a terminal, stop the service first (it holds the pin):

```
sudo systemctl stop croom                     # croom holds the pin
/opt/croom/venv/bin/croom --check-laptop -c /etc/croom/config.yaml
# looks only; add --switch to press once and report where the MeetUp went
lsusb -d 046d:                                # confirm the MeetUp's USB id
sudo systemctl start croom
```

## Updates, rollback and reboot

*Updates build beside the running version and undo themselves if the room doesn't start*

The root helper, `croom-admin`, does everything that needs root: config writes, restarts, reboots, updates, rollback, Wi-Fi and the journal. It never runs a shell; it answers a fixed list of jobs on `/run/croom-admin.sock` and keeps its record in `/var/lib/croom-admin/admin-state.json`.

### Restart with undo

1. Tap **Restart now** or **Restart room software**. The helper restarts croom and asks `/api/status` every 5 seconds for 2 minutes.
2. If the room answers, Settings shows it running again and `config.yaml.good` is refreshed.
3. If it doesn't answer and a saved change was waiting, the helper puts back `config.yaml.good`, keeps the failed config as `config.yaml.1`, restarts again, and the page says "Your last change was undone: the room did not start with it".

With no change waiting it leaves the room as it is ("down"). After a reboot or power cut it waits up to 5 minutes for systemd to start croom and watches the same way.

### Updating the TV Pi

1. Open **Settings › Software**. The TV Pi's card shows its version, the newest commit of main on GitHub, and the changes between them.
2. Tap **Update**. The card shows each step as it runs.
3. Wait. If a meeting is on, the switch waits for it to end, up to 30 minutes, then goes ahead.

*You should now see the new version on the card. If the room did not answer within 3 minutes, the card says "The update to … was undone: the room did not start with it" and the room is on the old version.*

| Step | What the helper does |
| --- | --- |
| **Build** | New venv in `/opt/croom/releases/<commit>`, `pip install git+https://github.com/ben-abeo/croom.to.git@<commit>` (40-minute limit), Playwright's Chromium, an import check, `python -m croom.admin --check`. |
| **Switch** | Moves the `/opt/croom/venv` link in one step, restarts croom, restarts the helper on the new release. |
| **Watch** | 3 minutes. No answer: link back, restart on the old release. |
| **Keep** | Two releases: the running one and the previous one. |

- **Refused** for a commit not on main, during a meeting, a restart or a Wi-Fi change, and while a saved config waits ("Restart now first, then update"). Joins are refused while it runs: "The room is updating: it restarts in a few minutes".
- **A cut-off build never touches the running release.** Unfinished folders are swept at the helper's next start.
- **Switch back to** offers the previous release, only if its helper starts.
- **Update all** queues the touch Pi and door sign first and the TV Pi last. The pages on the touch Pi, the TV and the door sign reload themselves when the TV Pi comes back with other page files (`page` in `/api/status`).
- **GitHub** is asked without an account (60 requests an hour per address), cached 10 minutes; "Can't check for updates right now" when it doesn't answer.

> **UPDATES DON'T RE-RUN THE INSTALLER**
>
> An update from Settings › Software replaces only the Python release. Unit files, udev rules, sudoers, apt packages and the keyboard layout are refreshed only by an installer run. When a release changes any of those, or the card says "This TV Pi can't update itself until its installer has been run once more", run the installer by hand:
>
> ```
> cd ~/croom.to && git pull && sudo bash installer/install.sh && sudo reboot   # keeps config and keys
> ```

### Reboot

**Settings › Software › Reboot** on the TV Pi's card (second tap), or `sudo reboot`. Refused in a meeting or Laptop mode. The service comes back by itself: it waits up to 120 seconds for the desktop each try and restarts every 10 seconds.

## Logs, the croom command and checks

*The journal and five checks tell you what the room is doing*

### Logs and quick checks

```
sudo systemctl status croom croom-admin
sudo journalctl -u croom -n 50                      # last 50 lines: look for the first ERROR
sudo journalctl -u croom -f                         # follow
sudo journalctl -u croom -p warning --since today   # warnings and errors only
sudo journalctl -u croom --since "1 hour ago" | grep -E "Zoom dialog|Acknowledged|waits on the table"
sudo journalctl -u croom-admin -n 50                # updates, restarts, Wi-Fi
curl -s http://localhost:8080/api/status | python3 -m json.tool   # everything the screens see
curl http://<dashboard>:3001/health                # dashboard reachable?
ls -l /opt/croom/venv; cat /opt/croom/VERSION       # running release
wpctl status                                        # sound devices (as the desktop user)
v4l2-ctl --list-devices; arecord -l; lsusb -d 046d: # camera, mic, MeetUp on USB
ls -l /dev/uinput; groups pi; lsmod | grep uinput   # virtual mouse prerequisites
df -h /var/lib/croom; ls -ld /var/lib/croom         # data folder writable?
```

- **Level:** WARNING by default. Every Zoom and Meet dialog (title, words, buttons, what was done) is logged at INFO regardless.
- **Priorities:** each line carries its syslog priority, so `-p warning` and Settings › Logs can filter.
- **Only the journal.** `/var/log/croom` is created but unused.

### Verbose logging for a while (between meetings)

```
sudo nano /etc/systemd/system/croom.service
# add -v: ExecStart=/opt/croom/venv/bin/python -m croom.core.agent -v -c /etc/croom/config.yaml
sudo systemctl daemon-reload && sudo systemctl restart croom
# undo the same way afterwards; an installer run also rewrites the unit
```

### Settings › Check

| Check | What it does |
| --- | --- |
| Calendar | The same as `croom --check-calendar`, on config.yaml as it is now. |
| TV | Looks through the room's own TV connection; opens no second one. |
| Laptop switch | Looks at the switch, never presses it, and says who has the MeetUp. |
| Google sign-in | Reads the profile's cookies: "Signed in as …" or "Not signed in: the profile holds no Google session". |
| Zoom keys | The same as `croom --check-zoom`. |

Each check stops after 60 seconds. Checks run side by side but not twice at once ("Already running"). None is refused during a meeting.

### Every croom command option

Run `/opt/croom/venv/bin/croom` as the service user (`sudo -u pi …`), never as root. Without `-c` the config is searched at `/etc/croom/config.yaml`, `~/.config/croom/config.yaml`, `./config.yaml`.

| Command | What it does | Needs | Exit |
| --- | --- | --- | --- |
| croom -c CONFIG | Runs the agent, as the unit does. | The desktop session | 1 on failure |
| -v, --debug | INFO or DEBUG logging. | — | — |
| --check-calendar | Signs in, opens the calendar, lists up to 10 bookings in 7 days, tests writing, reports queued and double-booked room bookings. | Network; readable key | 0 ok, 1 not |
| --check-zoom | Validates the credentials, mints a signature, gets a token and the room's ZAK. | Network | 0 ok (also with no room user), 1 not |
| --check-tv | Looks at the configured TV through a new connection (never wakes, pairs or switches), or lists TVs found. | Service user (key is 600) | 0 answered, 1 not |
| --check-laptop | Shows the pin, the GPIO chip and where the MeetUp is. | Service stopped | 0 pin usable, 1 not |
| --check-laptop --switch | As above, presses once and says where the MeetUp went. | Service stopped | 0 moved, 1 not |
| --check-meet URL [--profile DIR] | Opens a Meet link the way the room does, lists the devices the browser sees, the name field and join control, quotes Meet, saves `/tmp/croom-meet-check.png`. Does not join. | `DISPLAY=:0`; service stopped if it uses the room profile | 0 join control found, 1 not |
| --sign-in-meet | Opens a plain Chromium on the room profile at accounts.google.com; "Profile saved" if a Google session is found after the window closes. | `DISPLAY=:0`; service stopped; `google_profile_dir` set | 0 saved, 1 not |
| --help | Lists the options above. | — | 0 |
| python -m croom.admin --check | Builds the helper's job table without listening; prints "The helper starts". | — | 0 |
| python -m croom.admin --owner U --group G [--socket PATH] | The root helper itself, as `croom-admin.service` runs it. | root | — |

### Meeting diagnostics

```
curl -s http://<TV Pi>:8080/api/meeting/diagnostics > diagnostics-1.json
# only during a call; opens and closes Zoom's menus on the TV, so take one snapshot: never poll it
```

For Meet it returns every labelled element on the page, what the room read and where the keyboard focus is. For Zoom it returns Zoom's own field names, buttons and the SDK version. Use it on first calls to confirm the labels the room relies on.

## Troubleshooting

*Most problems show up in three places: the screens, the journal and Settings › Check*

Start with three checks. From a laptop on the room's network open `http://<TV Pi>:8080/api/status`. On the TV Pi run `sudo systemctl status croom croom-admin` and `sudo journalctl -u croom -n 50`, and look for the first ERROR. Then run the matching test under Settings › Check.

### Service and screens

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Room page doesn't load at :8080 | croom not running, or port 8080 taken (log "Room control page cannot listen on 0.0.0.0:8080: …"; the agent runs on without a web server). | Check status and the journal; free the port. Over VNC, the desktop icon **Start Crystal Meet** restarts it. |
| A tablet or the door sign can't open the page | `.local` name not resolved, or another network. | Use the IP address. Open `/api/status` from a laptop on the same network. |
| No browser on the TV after boot | The service waits for the desktop (up to 120 s each try); Auto Login off; Playwright's Chromium missing. | Wait a minute. Look for "Failed to initialize" in the journal. Turn Auto Login on. If `/opt/croom/browsers` is missing, re-run the installer. |
| The TV shows a tab strip and address bar | `meeting.kiosk: false`, or an old version. | Set kiosk on in Settings › Meetings; update. |
| TV stays navy or shows a Chromium error | The screensaver page can't be reached (log "The TV could not load the screensaver at … for 30 s; still trying"). | Check the control service started; `curl http://127.0.0.1:8080/tv`. |
| "The TV page crashed; a fresh browser will replace it", repeating | Renderer crash loop. | Check memory with `free -h`; reboot; read the journal around the crash. |
| "Room control page assets are missing." (or Door sign, TV page) | `index.html`, `sign.html` or `tv.html` not installed. | Update, or re-run the installer. |
| Config changes ignored; the room runs on defaults | An unknown key inside a section discarded the whole file. | Remove unknown keys; compare with `config.yaml.good`; restart. |
| A table choice (screensaver, background, scores) lost after restart | Data folder not writable (SD full or read-only, wrong owner); log "Could not save the room settings". | `df -h /var/lib/croom`; `ls -ld /var/lib/croom` must show the desktop user. |
| Room offline on the dashboard | Dashboard unreachable; token used or invalid; log "Enrollment rejected by dashboard (…)" or "Dashboard does not know this device; clearing enrollment state". | `curl http://<dashboard>:3001/health`; check `dashboard.url`; new token from Provisioning in Settings › AI & dashboard; restart. |

### Settings, PIN and the helper

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| "Not available on this device" | croom-admin not running, or its socket is missing. | `sudo systemctl status croom-admin`; `sudo journalctl -u croom-admin -n 50`; re-run the installer. |
| "The helper did not answer" / "could not be reached" | Helper hung, crashed, or wrong socket permissions. | `sudo systemctl restart croom-admin`; check the journal. |
| "Your last change was undone: the room did not start with it" | A saved change stopped the room starting; the helper restored `config.yaml.good`. | Read `/etc/croom/config.yaml.1` (the rejected one) and the journal. |
| Restart state "down": "The room did not answer within 2 minutes of a restart" | The room is unhealthy and there was nothing to undo. | Read the journal for the first ERROR. |
| "The room's settings file is not valid YAML: fix it at the TV Pi" | A hand edit broke the file. | Fix it by hand; earlier versions are `config.yaml.1` to `.5` and `.good`. |
| Forgotten PIN, or "The saved PIN can't be read…" | — / corrupt `settings-pin.json`. | `sudo rm /var/lib/croom/settings-pin.json`; no restart needed. |
| "Too many wrong PINs: try again in N minutes" | 5 wrong PINs. | Wait 10 minutes, or `sudo systemctl restart croom`. |
| "Enter the PIN" again and again | Unlock lasts 5 minutes after the last use; a restart locks every screen. | Enter the PIN again. |
| "Leave the meeting first" | Restart, reboot, update, Wi-Fi or sign-in tried during a call. | Leave the call first. |
| "An update is under way: wait for it to finish" / "The room is restarting: try again in a moment" | The helper is busy. | Wait; Settings › Software shows progress. |

### Updates

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| "The update to … was undone: the room did not start with it" | The new release didn't answer within 3 minutes. | The room is on the old release. Read `journalctl -u croom` for the failed start. |
| Update failed during a build step | pip, GitHub or the network. | Read the card's lines and `journalctl -u croom-admin`; try again. |
| "The update was cut off before it was finished" | Power cut or helper restart mid-build. | Nothing changed; update again. |
| "This TV Pi can't update itself until its installer has been run once more" | The release needs installer changes. | Run the installer by hand (Section 10). |
| "Can't check for updates right now" | api.github.com unreachable, or 60 requests an hour used. | Check the network to GitHub; try later. |
| "Restart now first, then update" | A saved change waits for a restart. | Tap Restart now, then Update. |
| Join refused: "The room is updating: it restarts in a few minutes" | An update is running. | Wait. |

### Google Meet

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| "The room's Google sign-in has expired or was never done; …" | Meet showed Google's sign-in page. | Settings › Meetings › Sign in to Google. If it recurs every few weeks, the unit's session control isn't "never expire". |
| Table quotes Meet: "You can't join this video call" | The browser is a guest: no `google_profile_dir`, not in use yet, or the sign-in was abandoned. | Set `/var/lib/croom/meet-profile`, restart, sign in again. |
| "No session was saved" / "Not signed in: Google did not keep a sign-in…" although the sign-in looked fine | Done pressed too early; an old version put the session in "Profile 1"; or cookies are in `Default/Network/Cookies`. | Update; sign in again and press Done only once the account shows; `ls /var/lib/croom/meet-profile/Default/`. |
| "The room service is using /var/lib/croom/meet-profile (process N); stop it first" | A CLI command run while croom holds the profile. | `sudo systemctl stop croom`, run it, start again. |
| "The TV's browser did not let go of the room's Google profile: try again in a moment" | The profile lock was still held after 5 s. | Try again; if stuck, restart croom. |
| Google asks for a phone or second factor | 2-Step Verification enforced for room users. | Allow but don't enforce it for the Meeting rooms unit. |
| Typed `@` comes out as `"` | Keyboard still GB. | The installer sets US; reboot. |
| "Could not find Meet's join button; Meet shows: …" / "join button stayed disabled" / "Failed to join meeting" | Meet's page changed or showed something unexpected; name missing. | Read the quote and `/tmp/croom-meet-failure.png`; run `--check-meet` with the service stopped. |
| "The host did not admit the room in time" | More than 5 minutes in the lobby. | The host must admit the room. |
| "Meet's chat box has the keyboard; close it on the TV first" | Shortcuts go to the chat box. | Close the chat on the TV with the touchpad. |
| Wrong name in the call | The Workspace user's name. | Rename the user in the Admin console. |
| Log "Google's Meet API refused the participants list (…)" | No delegation, Meet API off, organiser outside the domain, or a typed link. | Grant `meetings.space.readonly`; enable the API; set `meet_api_domain`; restart. The list falls back to Meet's page. |

### Zoom

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Zoom says "Automated bots aren't allowed…"; log "Zoom Meeting SDK not configured: …" | No credentials path, or the file is incomplete or still REPLACE. | Install `zoom-credentials.json`; `--check-zoom`; restart. |
| "Zoom refused the server-to-server credentials…" | Wrong account id, client id or secret, or the app isn't activated. | Re-copy from the app's credentials page; activate. |
| "Zoom has no user … on this account" / "the server-to-server app lacks a scope…" | `room_user` not an active user, or a scope missing. | Fix the user; add the scope and activate the app again. |
| Zoom's "not authorized" on another company's meeting | SDK app not approved, development pair still on the Pi, or no room user. | Approval, production pair and server-to-server values; restart; `--check-zoom`. |
| "Zoom needs the meeting passcode (…)" | No `pwd=` and no "Passcode:" in the invitation. | Type the passcode and tap **Join again**. |
| "Zoom's SDK did not load from source.zoom.us (…)"; TV dark | The CDN is blocked, or SDK 6.5.0 retired. | Check internet access to source.zoom.us; bump `SDK_VERSION` and release. |
| "Zoom cannot use the camera right now" | Another process holds the camera; Laptop mode has the MeetUp; the MeetUp is missing. | Update; check `lsusb` and Laptop mode; `sudo fuser -v /dev/video*`; reboot. |
| "Zoom did not connect; the page says: …" | No connection in 90 s (300 s in a waiting room). | Read Zoom's words; check the network. |
| "Could not read the join parameters: join parameters expired" | The join page was reloaded (the token works once). | Join again. |
| "Zoom ignored … (it takes one a few seconds after the last)" | Zoom's rate limits. | Wait and try again. |
| Room joins muted | Host's mute on entry. | Tap Unmute. |
| A Zoom dialog sits on the TV | Its words aren't on the acknowledge list. | Answer it from the table; add the phrase under Settings › Meetings. |

### TV

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Find TVs lists nothing / "The TV is not answering at …" | Inbound UDP blocked on the Pi; TV on another network or off; LG Connect Apps off; wrong address. | Allow inbound UDP; same subnet as the Pi's default route; turn on LG Connect Apps; reserve the TV's IP; `--check-tv`. |
| "The TV at … did not turn on: check that Wake-on-LAN is enabled…"; joins go ahead with the TV dark | Missing, malformed or wrong MAC (Wi-Fi vs Ethernet); Mobile TV On and Quick Start+ both off. | Set the active interface's MAC in the TV's connection settings (in quotes in YAML); turn on Mobile TV On or Quick Start+. |
| "The TV's MAC address is not set…" / "'…' is not a MAC address…" | As above. | As above. |
| "The TV no longer accepts this table: Forget TV, then Pair" / TV keeps prompting | TV reset or its paired devices cleared. | Forget TV, then Pair; accept within 60 s. |
| "Nobody accepted the prompt on the TV within 60 seconds" | Prompt not accepted. | Pair again with the remote ready. |
| `--check-tv`: "The TV is not paired with this device … found no TV key it can read…" | Run as the wrong user, or not paired. | `sudo -u pi /opt/croom/venv/bin/croom --check-tv -c /etc/croom/config.yaml` |
| TV stays on the wrong input at join | `keep_on: false`; `pi_input` wrong; Samsung (no direct input); TV state unknown. | Set the right HDMI input; `--check-tv` warns. |
| TV turns itself back on after its own remote turned it off | `keep_on` wakes it after 5 s. | Turn it off from the table, or set `keep_on: false`. |
| Touchpad does nothing: "…is not allowed to use /dev/uinput…" / "…needs /dev/uinput, and it is not there…" | udev rule, input group or uinput module missing; evdev not built. | `sudo bash -c 'source installer/install.sh && install_virtual_mouse'`, then restart croom (a group change may need a reboot). |
| "Enter the Settings PIN again to type on the TV" | The PIN unlock ran out during a sign-in. | Unlock Settings again. |

### Sound, camera and devices

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| No sound or picture in the call; `wpctl status` lists the MeetUp but the browser doesn't | The browser can't reach PipeWire. | Check `libpulse0`; the unit's `XDG_RUNTIME_DIR`; plug devices before boot; reboot. |
| "No speaker found" / "No microphone found" | No device, or the configured name matches nothing. | `wpctl status` as the desktop user; set `auto` or a matching name. |
| Sound on the wrong device | WirePlumber remembers the default. | Name the wanted device (not auto); `HDMI` for the TV. |
| "They can't hear us" | Mic level. | Options › Mic level (100 normal, up to 150). |
| "No controllable camera found" | No camera with zoom or pan/tilt controls; MeetUp lent to the laptop. | `v4l2-ctl --list-devices`; check Laptop mode; replug. |
| Presets land in the wrong place | Drift; wrong travel time. | Find the stops, Save as Home, re-save presets; adjust the travel time. |
| "the TV is in a meeting" on the camera preview | The preview is for between meetings. | Expected. |
| "could not save the camera settings: …" | `control-settings.json` not writable. | Check disk space and ownership. |

### Calendar

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| "Google Calendar not configured: …" | No key path, unreadable key, empty or placeholder address. | Fix config and key; restart. Run checks as the service user. |
| "Could not sign in with …: the key file is not a service account key, or the project's Calendar API is not enabled." | Wrong JSON (an OAuth client file), API off, key deleted, **or no network**. | New key; enable the API; check the network. |
| "Calendar … not found or not shared…" | Wrong address, or not shared with the service account. | Share with **Make changes to events**; check the address. |
| "…for reading only…" / bookings stuck "Not in Google Calendar yet" | Shared as See all event details. | Change to Make changes to events; the queue sends on the next poll. |
| "Calendar offline since …" | Google unreachable. | Check the network; bookings are kept and sent later. |
| "Double-booked with …" | Booked here while offline; a clash in Google. | Move or cancel one in Google Calendar. |
| A booking is missing | Room not added, room declined, more than 7 days ahead, or cancelled. | Fix the event in Google. |
| Bookings at the wrong time | The OS time zone. | Set Localisation; restart croom. |
| Join now missing | No joinable video link. | Add a Meet or Zoom link to the event. |
| Google: "Key creation is not allowed" | Organisation policy. | Turn off "Disable service account key creation" for the project. |

### Laptop mode

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| "This room has no laptop switch" | `laptop.enabled` off, or not restarted. | Turn it on in Settings › Laptop switch; Restart now. |
| `--check-laptop`: "The switch cannot be pressed: …" | croom holds the pin; gpiod missing; no Pi 4 or Pi 5 GPIO chip. | Stop croom first; check the Pi model. |
| "The switch did not answer: …" | Relay not clicking (3.3 V vs 5 V), wrong contacts, `active_low` wrong. | Check the wiring; try pin 2; toggle active low; press the US234's button by hand. |
| "The camera is on the laptop: press Back to room first" | Laptop mode is on. | Back to room on the table. |
| TV not on the laptop: "Choose the laptop's input under Options › TV…" / "Choose HDMI 2 on the TV with its Input button" | No laptop input set; TV off or unreachable; Samsung. | Set the Laptop input; or switch with the TV's remote. |
| The MeetUp's USB id differs | The default is unconfirmed. | `lsusb`, then set `laptop.usb_id`. |

## Reference

*Every file, route and timing in one place*

### Files and folders on the TV Pi

| Path | Owner, mode | What |
| --- | --- | --- |
| /etc/croom/config.yaml | room user, 640 | Room config; `.1` to `.5` previous versions; `.good` last healthy start |
| /etc/croom/google-service-account.json | room user, 600 | Google service-account key |
| /etc/croom/zoom-credentials.json | room user, 600 | Zoom keys |
| /opt/croom/venv | link | Points at `releases/<commit>/venv`, the running release; two kept |
| /opt/croom/VERSION | root, 644 | Running release's commit |
| /opt/croom/browsers/ | room user | Playwright's Chromium |
| /var/lib/croom/ | room user | Data folder (falls back to `~/.local/share/croom` if not writable) |
| …/meet-profile/ | 700 | TV browser profile and Google session |
| …/control-settings.json | 600 | Screensaver, door sign look, key click, camera home and presets, mic level |
| …/settings-pin.json | 600 | Settings PIN hash; delete to reset |
| …/devices.json | 600 | Joined and refused touch Pis and door signs |
| …/tv-settings.json, tv-<hash>-key, tv-client-key | 600 | Chosen TV; pairing keys |
| …/calendar-cache.json, bookings.json | 600 | Last calendar read; room bookings and their sync state |
| …/laptop.json | 600 | Laptop mode state |
| …/dashboard-state.json | 600 | Dashboard enrolment; delete, with a new token, to re-enrol |
| …/door-sign/, meet-capture-*.zip |  | Door sign APKs (two newest); Meet captures (three newest) |
| /var/lib/croom-admin/admin-state.json | root, 600 | Helper's record: restarts, updates, previous release, Wi-Fi changes |
| /run/croom-admin.sock | root:group, 660 | Helper socket |
| /etc/systemd/system/croom*.service | root | Units |
| /etc/sudoers.d/croom | root, 440 | `systemctl start|restart croom` for the desktop user |
| /etc/udev/rules.d/99-croom-uinput.rules, /etc/modules-load.d/croom-uinput.conf | root | Virtual mouse and keyboard |
| /etc/NetworkManager/system-connections/croom-*.nmconnection | root, 600 | Wi-Fi networks added from Settings |
| /tmp/croom-meet-failure.png, croom-meet-check.png |  | Screenshots of a failed Meet join and of `--check-meet` |

### Network and ports

| Direction | Port or address | Purpose |
| --- | --- | --- |
| In, LAN | TCP 8080 | Room page `/`, door sign `/sign`, TV page `/tv`, `/api/…`, WebSocket `/ws/pointer` |
| In, loopback | 127.0.0.1, any free port | The page Zoom's SDK runs in; refuses anyone but the Pi itself |
| In, local socket | /run/croom-admin.sock | The root helper (mode 660, the room user's group) |
| In, LAN UDP | any port | Replies from TVs during Find TVs: a firewall that blocks inbound UDP hides every TV |
| Out, multicast | UDP 239.255.255.250:1900 | TV discovery (SSDP) |
| Out, broadcast | UDP | Wake-on-LAN to the TV's MAC |
| Out, TV | LG 3000/3001; Samsung 8002 (and 8001) | TV control |
| Out, HTTPS | www.googleapis.com, oauth2.googleapis.com | Calendar API |
| Out, HTTPS | meet.google.com, accounts.google.com, meet.googleapis.com | Google Meet; Meet participants API (only with `meeting.meet_api_domain`) |
| Out, HTTPS | zoom.us, api.zoom.us, source.zoom.us | Zoom tokens, the room's ZAK, the SDK 6.5.0 scripts |
| Out, HTTPS | api.github.com, github.com | Update check and builds; door sign releases |
| Out, HTTP | <dashboard>:3001 | Dashboard enrolment and WebSocket heartbeat |
| Out, HTTPS | site.api.espn.com | Door sign scores, only while a school is on |

### Important routes on port 8080

| Route | Access | Use |
| --- | --- | --- |
| GET / · /sign · /tv | Open | Room page, door sign, TV screensaver |
| GET /api/status | Open | Everything the screens show; never errors |
| GET /api/calendar/events · /week | Open | Today; seven days |
| /api/bookings, /now, /{key}/end, /{key}/cancel, /slots | Open | Booking; End now only from the room page |
| POST /api/meeting/join · leave · mute · camera · control · dialog | Open | Calls from the table |
| GET /api/meeting/diagnostics | Open | What the meeting page shows, during a call |
| /api/tv/discover · select · pair · forget · control · settings | Open | TV setup and remote |
| /api/camera/*, /api/audio/volume, /api/audio/mic | Open | Camera, speaker, microphone |
| POST /api/laptop | Open | `{"to":"laptop"}` or `{"to":"room"}` |
| WS /ws/pointer | Same origin | Touchpad and typing on the TV |
| POST /api/settings/unlock · /api/settings/… | PIN, then token | Unlock returns the `X-Croom-Unlock` token every Settings route needs |
| POST /api/devices/checkin · GET /api/devices/apk/{code} | Device token | Touch Pi and door sign check-in; door sign APKs |
| GET /api/devices/busy · seen-since | Loopback only | Used by the root helper |

Every POST must be `Content-Type: application/json` (else 415), except key uploads (multipart). Errors are `{"error": "…"}`.

### Timings

| What | Value |
| --- | --- |
| **Screens** | Status every 2 s (room page, TV page); device check-in every 5 s; online if seen within 20 s |
| **Calendar** | Poll every 60 s; 10 s wait at the screen for a booking; Join now from 10 min before |
| **Settings** | Unlock 5 min after last use; 5 wrong PINs lock 10 min; each check up to 60 s |
| **Restart and update** | Restart watched 2 min (every 5 s); update watched 3 min; update waits up to 30 min for a meeting; build limit 40 min; GitHub cached 10 min |
| **TV** | Wake up to 15 s, packet resent every 3 s; Turn on up to about 23 s; re-wake 5 s after an unexpected off; pairing 60 s (+5 s); reconnect 2 to 30 s; Find TVs results valid 5 min |
| **TV browser** | Screensaver retry 2 to 10 s, warning after 30 s; crash relaunch back-off 10 s; pointer shown 5 s |
| **Google Meet** | Join control 30 s; lobby 5 min; Meet API every 15 s; Google sign-in closes after 15 min untouched |
| **Zoom** | Connect 90 s; waiting room 300 s; signature and ZAK valid 2 h; 15 s per token request |
| **Devices** | Camera stops 1.5 s after the last move; preview 180 s; missing devices looked for every 30 s; mic re-applied every 5 s in a call |
| **Laptop mode** | Relay press 200 ms; wait 10 s per press, two presses; watch every 2 s; camera found again within 15 s |

### Repo material not to follow for the TV Pi

- **scripts/ and services/** (`startup.sh`, `tv-on.sh`, `tv-standby.sh`, `tv-lifecycle.service`, `firstboot.service`…): from the earlier "minimeet" setup; not installed.
- **Upstream guides:** `docs/guides/administrator-guide.md`, `deployment-guide.md`, `user-guide.md` are generic croom.to, not Crystal Meet. `packaging/debian` is unused.
- **Unused code:** `croom.ai`, `croom.display` controls, `meeting/controls.py`, `quality.py`, the Teams and Webex providers, the Microsoft 365 calendar, `croom-ui`.
- **Older text:** the setup guide's Brio and Jabra example; "Meet offers four controls"; `--check-tv` help saying "LG".

### Source map

| Path in ben-abeo/croom.to | Holds |
| --- | --- |
| installer/install.sh, deploy/rooms/ | The installer and unit files; room configs and the Zoom template |
| src/croom/core/agent.py, config.py | Start-up, CLI options, every config key |
| src/croom/control/, settings/, admin/ | Web server and pages; Settings; the root helper |
| src/croom/meeting/, calendar/, tv/, laptop/, devices/ | Meetings, calendar, TV, Laptop mode, camera and sound |

