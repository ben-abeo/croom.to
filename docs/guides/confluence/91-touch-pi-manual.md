# Touch Pi technical manual

*Part of [Conference room technical manuals]. The printable version is attached as crystal-meet-touch-pi-manual.pdf.*

## Overview

*The touch Pi is a browser and a small agent; the TV Pi does the rest*

The table screen is where people join calls, book the room, run the meeting and change room Settings. The touch Pi itself holds almost nothing: Chromium shows the room page that the TV Pi serves, and a small root agent checks in with the TV Pi so the room can reboot it, update it and change its Wi-Fi.

- **Touch Pi** (on the table): Chromium kiosk shows **/?keyboard=1**; **croom-kiosk-agent** checks in and runs jobs.
- **TV Pi · croom** (behind the TV): Serves the page, runs the call, calendar, camera, sound and TV, queues jobs.
- **Google and GitHub** (outside): The room calendar; versions of **main**. The touch Pi fetches GitHub itself only during an update.
- Link: HTTP :8080 page, APIs, touchpad, check-in
- Link: HTTPS calendar, versions

- **2 s**: The page asks the TV Pi for the room's status
- **5 s**: The agent checks in with the TV Pi
- **1280×800**: Layout size: the 1920×1200 panel at 1.5× scale
- **10 min**: Options screens return to the idle screen

### What the table screen can and cannot do

**CAN**

- **Join and run calls**: Zoom and Google Meet, by booking or by link, with mute, camera, hand, reactions, captions, layout, participants, chat and host controls.
- **Book the room**, use it now, end a booking made at the room, or cancel one that has not started.
- **Drive the camera, sound and TV**: presets, mic level, volume, a TV remote and a touchpad.
- **Change room Settings** behind a PIN, including the door sign and the touch Pi itself.

**CANNOT**

- **Work without the TV Pi.** The page, the call and every control live there. The top bar says "Can't reach the room".
- **Show its own logs.** Settings › Logs shows only the TV Pi's journal; the touch Pi's are read over SSH.
- **Appear on the fleet dashboard.** Only the TV Pi reports there.
- **Show the camera's picture yet.** A live picture on the camera screen is coming in a later release.

## Hardware and network

*Any desktop Pi with the touch panel, on a network that reaches the TV Pi*

| Item | Detail |
| --- | --- |
| Computer | A Raspberry Pi. **The model is not specified** in Crystal Meet's code or guides. As a baseline, use what the room setup guide lists for the TV Pi: a Pi 4 (4 GB) or Pi 5, the official power supply and a 32 GB microSD card. |
| Operating system | Raspberry Pi OS (64-bit) with desktop, on the **labwc** (Wayland) desktop of current releases (Bookworm, Trixie). **Auto-login to the desktop must be on.** |
| Touch display | A 10.1-inch, 1920×1200 Forzola touch panel, with no vibration motor. **Its part number and connection (HDMI with USB touch, or DSI) are not specified.** |
| Scale | Chromium runs at 1.5×, so the page lays out at 1280×800; buttons are about 11 mm tall. Every screen fits without page scrolling. |
| Power | **Not specified.** Whatever powers it, after a power cut the Pi must boot to the desktop by itself so the kiosk restarts. |
| Audio | Only the on-screen keyboard's key click plays on the touch Pi's own audio output. Call sound comes from the TV Pi's speaker. |
| Network | Ethernet or Wi-Fi, managed by NetworkManager (`nmcli` is needed for Wi-Fi changes from Settings). It must reach the TV Pi on TCP **8080**, and github.com on HTTPS for updates. |
| Cabling | Not cabled to the TV Pi. It needs only power and the network. |

### What the touch Pi talks to

| Peer | How | When |
| --- | --- | --- |
| TV Pi | HTTP and WebSocket to `http://<TV Pi>:8080` | Always: the page, every API, the touchpad link `/ws/pointer`, and the agent's check-in every 5 s. |
| GitHub | HTTPS 443, `git fetch` of `ben-abeo/croom.to` | Only during an update. |
| Apt mirrors | HTTP/HTTPS | Only during install, when Chromium, curl or git are missing. |
| Google, TV, door sign, dashboard | None directly | The TV Pi talks to all of them; the touch Pi asks the TV Pi. |

> **GOOD TO KNOW**
>
> **Nothing listens on the touch Pi.** Crystal Meet opens no port on it; it only calls the TV Pi. SSH on port 22 is there only if you turned it on in Raspberry Pi Imager.

## Using the table screen

*One screen at a time, every one with Back, and idle screens go home by themselves*

A top bar runs across every screen: the room's name and location, the time and date, the meeting's state and time in call, "Next: 2:00 PM Weekly sync" from 10 minutes before a booking, and "Can't reach the room" when the TV Pi does not answer. Below it, one view at a time. Every button has an icon before its label.

- **Back everywhere.** Every screen below the idle screen has **Back**.
- **Going home.** Options screens return to the idle screen after 10 minutes untouched. Meeting screens (Participants, Chat, Host, TV, Mic) return to the meeting after 2 minutes.
- **Second taps.** Anything hard to undo asks "Tap again to …" and waits 5 seconds for the second tap.
- **Mis-tap guard.** Taps are ignored for half a second after the layout shifts or a dialog appears.

### The idle screen

A status card with a kicker, a headline and a line under it, for example **Room free** · "Free until 1:00 PM", **Starting soon** · "Weekly sync starts in 8 minutes", **Happening now**, or **In use** · "Weekly sync until 2:00 PM". Other lines: "Double-booked with …", "Booked here; not in Google Calendar yet.", "Free for the rest of the day", and "No calendar is connected. Join with a link below."

| Button | When it shows | What it does |
| --- | --- | --- |
| Join now | A booking with a video link | Joins the call. It opens 10 minutes before the start; until then it says "Join opens at 12:50 PM". |
| Book room | A calendar is set up | Books a later slot (below). |
| Use now | The room is free now | Claims the room from this minute (below). |
| End now | A meeting is in progress (booked at the room's screens or in Google Calendar) | Ends it on the second tap: shortened in Google, or declined by the room when people invited it. Only the table can end a meeting; the door sign cannot. |
| Laptop | Laptop mode is turned on in Settings | Hands the camera, microphones and speaker to a laptop (below). |
| TV Remote | Top bar, when a TV is set up and paired | Opens the TV remote. |
| Options | Top bar, always (hidden while a laptop has the camera) | Opens Options. |

### Join with a link

1. Tap the field **Zoom meeting ID, Meet code or link**. The page's own keyboard opens.
2. Type a Zoom meeting ID, a Meet code (`abc-defg-hij`, with or without its dashes) or a link. For a bare Zoom ID, or a Zoom link without `pwd=`, a **Passcode** field appears: fill it in.
3. Tap **Join** (on the screen or on the keyboard). The top bar shows Joining, then In a meeting, and the meeting screen opens.

If Zoom refuses over the passcode, a "Couldn't join" card says "Zoom needs the meeting passcode" with **Join again**.

### The Today list

Today's bookings, scrolling inside their own box. The room's own bookings that have not started carry **Cancel** (second tap). Notes such as "Not in Google Calendar yet" and "Calendar offline since 10:42 AM. Bookings made here will sync." appear when Google can't be reached.

### Book room and Use now

1. Tap **Book room**. Choose the day: today and the next six.
2. Choose an open start time. Times run every 15 minutes inside booking hours (08:00 to 18:00 unless changed).
3. Type a name in **Your name or the meeting's name**; the page's keyboard opens by itself.
4. Choose a length, 15 minutes to 4 hours, as far as the next booking allows, and tap **Book** (or **Done** on the keyboard). "Booked" shows for 4 seconds.

**Use now** asks for a name and a length shown with its end, for example `30 min · until 12:30 PM`, then **Start**. It ends on the nearest quarter hour. Booking steps left alone for a minute close by themselves; **Back** goes back one step. Bookings are written to the room's Google calendar, or kept on the TV Pi and synced when Google is back.

### Options

Six items, no PIN needed except for Settings: **Camera framing**, **Mic level**, **Screensaver**, **Door sign**, **TV**, **Settings**. Below them, **Keyboard**: **Key click on** / **Key click off**.

| Item | What it offers |
| --- | --- |
| Camera framing | Arrow pad (hold to move; the middle key is Home), **Zoom out** / **Zoom in**, three preset buttons (Wide, Table, Whiteboard by default), **Save** ("Tap a preset to save it here"), and **Set up**: **Find the stops**, then nudge to the view you want and **Save as Home**. The TV shows the camera's picture while this screen is open. "No controllable camera found" if the TV Pi has no camera it can turn. Coming in a later release: the camera's live picture here, with drag to turn and pinch to zoom. |
| Mic level | A slider from 0 to 150 (100 is normal) with **Quieter** / **Louder** in steps of 5. Kept, and applied at every join. "No microphone found" if there is none. |
| Screensaver | What the TV shows when idle: Information, Quiet, Brand or Bounce. The TV changes at once. |
| Door sign | **Door sign background**: Austin skyline (default), Capitol, UT Tower, Pennybacker Bridge, Hamilton Pool, Texas star, Hill Country, DKR stadium, Plain. **Football scores**: Texas, Michigan, Texas A&M, Missouri, all off by default. |
| TV | The TV remote and touchpad (below). |
| Settings | Room Settings, behind the PIN (Section 05). |
| Keyboard | Turns the on-screen keyboard's click on or off for every screen of the room. |

### The meeting screen

Opens by itself during a call. It is laid out as a control deck.

| Area | What is there |
| --- | --- |
| Meeting card (left) | State (Joining, In the lobby, In a meeting, Leaving), time in call, "N people in the call", "N unread chat messages", "The room is host/co-host", and what the meeting disclosed (recorded, transcribed, AI Companion). **Leave** asks "Tap again to leave" within 5 seconds; a double tap on the glass does not count. |
| Main tiles | **Mute** / **Unmute**, **Turn camera off** / **Turn camera on**, **Raise hand** / **Lower hand** (when the call offers it), **Reactions** (a row of choices for 10 seconds, with Cancel). |
| Second row | **Captions on** / **off**; on Zoom **Gallery view** / **Speaker view**, on Meet **Layout** (Auto, Tiled, Spotlight, Sidebar); **Hide self view** / **Show self view**; **Participants (N)**; **Chat (N)**; **Host** when the room is host or co-host. |
| Dock | **Sound**: volume slider, **Quieter** / **Louder**, **Mute speaker**, and **Mic** for the mic level. **Camera**: presets and nudges. **Upcoming meetings**. |
| Top bar | **Touchpad**: a full-screen touchpad on the TV Pi's mouse. **TV**: the remote. |
| Dialog card | A Zoom or Meet question that waits for a tap ("Zoom asks" / "Meet asks" when untitled), with the platform's own buttons. Questions the room is set to answer are answered by themselves. |

- **Participants.** Each person with Host, Hand raised, Muted or Video off, and "(this room)". **Pin** / **Unpin** for anyone. As host: **Mute** one person and **Remove** (second tap). On Meet, a **Waiting room** with **Admit** and **Deny**. On Meet the list is read from Meet's people panel while this screen is open ("Reading the list from Meet").
- **Chat.** The last 20 messages. In a **Meet** call a message box and **Send** appear when Meet's chat is available; **Zoom chat is read-only**.
- **Host controls on Zoom.** **Mute all**, **Ask all to unmute**, **Start recording** / **Stop recording**, **Lock meeting** / **Unlock meeting**, **End meeting for everyone** (second tap).
- **Host controls on Meet.** **Mute everyone** (second tap), **End call for everyone**, and **Meeting settings** switches read from Meet's host panel (screen share, chat, reactions, microphones, video, Quick access).
- **When a control can't be done**, the screen says why in words, for example "Meet's chat button isn't on the screen".

### Laptop mode

Only when Laptop mode is turned on in Settings. During a booking, **Laptop** switches in one tap; in a free room it asks "Use a laptop for" a length, then **Switch to laptop** (this books the room when there is a calendar). The button says "Switching…". The laptop screen says "The camera, microphones and speaker are on the laptop until 2:00 PM", with **Back to room**. Meanwhile joins are refused and Options is hidden.

### TV remote and touchpad

- **Status line.** For example "<TV name> · On · HDMI 1 (Crystal Meet)", with **Turn on** when the TV is off, **Change TV** and **Forget TV** (second tap).
- **Keys.** **Power** (second tap to turn off), **Input** (the TV Pi's input is marked Crystal Meet), Home, Menu, Volume, Mute, Channel, a round pad with **OK**, Back, Exit, Info, Guide, digits with **Go**, playback and colour keys.
- **Modes.** **Pointer** (the TV's own cursor, LG only), **Room Pi mouse** (a mouse on the TV Pi), and **Full-screen touchpad** with **TV** / **Room Pi** and **Done**.
- **Gestures.** One finger moves; a quick tap clicks; two fingers scroll; a two-finger tap is a right-click on the Pi; press and hold, then move, drags (a blue border shows the hold is armed). A dropped link shows "Reconnecting…" and retries by itself.
- **No TV yet.** "No TV is set up for this room": **Find TVs**, **Connect**, then accept on the TV within a minute. **Change TV** sets **TV MAC address**, **Room Pi input** and **Laptop input**, then **Save connection**.
- **Samsung TVs.** Input opens the TV's source menu and there is no pointer mode; the Room Pi mouse and touchpad still work.

### The on-screen keyboard

The kiosk opens the page with `?keyboard=1`, so the page draws its own keyboard: the Pi's system keyboard would be hidden under the kiosk window.

- **Layers.** Letters, **123**, **#+=**, and **ABC** back. Shift once for one capital; double-tap Shift for caps lock (a dot under the arrow).
- **Feel.** Keys act on touch-down and take several fingers at once; holding delete repeats; a preview pops above the key; the click is a short tick on the touch Pi's speaker.
- **Closing it.** The hide key, or a tap elsewhere. **Join** on the keyboard joins the call. The MAC address field and the chat have their own layouts.

### Sign in to Google

From **Settings › Meetings**. The TV shows Google's sign-in, and the table shows the full-screen touchpad on the TV Pi's mouse with **Type on the TV**, **Done** ("Signed in as …") and **Cancel**. Fifteen minutes without a touch cancels it. Refused during a call or in Laptop mode.

## Installing a touch Pi

*One installer sets up the kiosk and the agent; then Allow it in Settings*

The installer `installer/install-kiosk.sh` turns a Raspberry Pi OS desktop into the table screen: it installs Chromium if needed, opens the room page full screen at every login, turns off screen blanking, and installs the agent that lets the room manage it.

> **BEFORE YOU BEGIN**
>
> **The TV Pi installed and running**, with a **fixed address** (a DHCP reservation). The kiosk and the agent both hold this address. Use the IP address rather than a `.local` name if name lookup is unreliable on the network.
>
> **A TV Pi with room settings.** If it was set up before room settings existed, upgrade the TV Pi first.
>
> **Raspberry Pi OS (64-bit) with desktop** on the touch Pi, written with Raspberry Pi Imager, with a desktop user (for example `pi`), Wi-Fi or Ethernet, and SSH if you want it.
>
> **Outside meeting hours** when working on a room in use.

> **NEVER RUN INSTALL.SH ON THE TOUCH PI**
>
> installer/install.sh installs the full room software for the TV Pi. On a Pi with a touch screen it would also turn on an untested upstream touch interface. The touch Pi only ever gets install-kiosk.sh.

### 1 · Turn on auto-login

1. On the touch Pi open **Raspberry Pi Configuration › System** and turn **Auto Login** on for the desktop user.
2. Reboot. The Pi should come up on the desktop without asking for a password.

### 2 · Run the installer

Log in as the desktop user (at the Pi or over SSH) and run:

```
sudo apt update && sudo apt install -y git
git clone https://github.com/ben-abeo/croom.to.git
cd croom.to
sudo bash installer/install-kiosk.sh --url http://<TV Pi address>:8080/
# ends with "Crystal Meet table screen set up." and how to Allow this Pi
sudo reboot
```

- **Run it with sudo from the desktop user.** Run as root directly, it stops with "Run with sudo from the desktop user's login, so the kiosk starts in that desktop".
- **Run it from a git clone.** Otherwise: "Run this from a clone of the repository".
- **`--url` is required.** Without it: "Say which room page to show". `--help` prints the usage.

*After the reboot you should see the room page full screen. It can take a few minutes if the TV Pi is still starting: the kiosk waits for it.*

> **GOOD TO KNOW**
>
> The installer prints a command to open the kiosk without rebooting. It works from a terminal on the Pi's own desktop. **Over SSH it has not been tested** and probably fails for lack of the desktop's display settings: reboot instead, or tap the **Room controls** icon on the desktop.

### What the installer does

| Step | Result |
| --- | --- |
| **Packages** | Installs `chromium`, `curl` and `git` if missing. |
| **Kiosk launcher** | Writes `/usr/local/bin/crystal-meet-kiosk` holding the URL with `?keyboard=1`. |
| **Autostart** | Adds `/usr/local/bin/crystal-meet-kiosk &` once to `~/.config/labwc/autostart`. |
| **Desktop icon** | **Room controls** on the desktop, to reopen the kiosk. |
| **Blanking off** | `raspi-config nonint do_blanking 1`. |
| **Agent** | A bare clone in `/opt/croom-kiosk/repo.git`, a sealed release of the clone's current commit in `/opt/croom-kiosk/releases/`, the links `current` and `good`, the state folder `/var/lib/croom-kiosk` with `config.json`, and the service `croom-kiosk-agent`, enabled and started. |

### 3 · Allow it in Settings

1. On the table screen tap **Options › Settings**. The first time, choose a PIN of 4 to 8 digits and enter it again. There is no default PIN.
2. Open **Software**. Under **Devices waiting to join** you should see this Pi, named by its hostname.
3. Tap **Allow** beside it. Allow only a device you know.

*You should now see a **Touch Pi** card with "Checked in just now", its version, the newest version, **Update** and **Reboot**; its connection appears under **Settings › Network**.*

### 4 · First-run check

1. Tap the link field and type a test meeting link on the on-screen keyboard. The key click should sound if it is on.
2. Tap **Join**. The TV should join the call and the table should show the meeting screen.
3. Tap **Leave**, then **Tap again to leave**. The table should return to the idle screen.

### Re-running the installer

Run it again to change the TV Pi's address, to repair the kiosk, or to bring an older table Pi (set up before room settings, with no agent) up to date. It is safe to repeat and keeps the Pi's identity, so the Pi stays joined.

```
cd ~/croom.to && git checkout main && git pull
sudo bash installer/install-kiosk.sh --url http://<TV Pi address>:8080/
sudo reboot
```

> **PULL FIRST, EVERY TIME**
>
> The installer installs the agent from the clone's **current commit**. Run git pull before it, or you may move the touch Pi back to an older version than the one the room updated it to.

A Pi that had no agent then appears under **Devices waiting to join**: **Allow** it. For a whole room, do the TV Pi first, then the touch Pi, then the door sign.

### A tablet instead of a touch Pi

An Android tablet in Fully Kiosk Browser, or an iPad in Guided Access with Safari, can show `http://<TV Pi>:8080/` full screen and kept awake. It uses its own keyboard and has no agent, so it can't be updated, rebooted or moved to new Wi-Fi from Settings.

## Configuration and Settings

*The touch Pi holds only an address; everything else is kept on the TV Pi*

There is no `config.yaml` on the touch Pi. What it shows and how the room behaves are set on the TV Pi, mostly from **Options › Settings** on the table. The same Settings pages also work in a laptop's browser at `http://<TV Pi>:8080/`.

### On the touch Pi

| What | Where | Content |
| --- | --- | --- |
| Kiosk URL | `/usr/local/bin/crystal-meet-kiosk` | The room page with `?keyboard=1` |
| Agent config | `/var/lib/croom-kiosk/config.json` (root, 600) | `{"url": "http://<TV Pi>:8080/", "user": "pi"}` |
| Agent identity | `/var/lib/croom-kiosk/identity.json` (root, 600) | Its id and secret token |
| Autostart | `~/.config/labwc/autostart` | The launcher line |
| Wi-Fi profiles | `/etc/NetworkManager/system-connections/` | Those made from Settings are `croom-<uuid>.nmconnection` (root, 600) |

To point the touch Pi at a new TV Pi address, re-run the installer with the new `--url` (Section 04). It rewrites both the launcher and the agent's config. Then reboot.

### On the TV Pi

- **`/etc/croom/config.yaml`**, written by Settings through the TV Pi's root helper. The last five versions are kept as `config.yaml.1` to `.5`, and the last one that started well as `config.yaml.good`.
- **`/var/lib/croom/control-settings.json`**, the choices made on the table without a PIN: screensaver, door sign background and scores, key click, mic level, camera Home and presets.
- **`/var/lib/croom/settings-pin.json`**, the Settings PIN as a salted hash (mode 600).
- **`/var/lib/croom/devices.json`**, the joined devices, with each token stored only as a hash (mode 600).

### The Settings PIN

- **First use.** Choose 4 to 8 digits on the PIN pad, tap **OK**, then enter the same PIN again. Mismatched: "The two PINs were not the same: choose a PIN again".
- **Unlocked for 5 minutes** after the last Settings action. The header shows "Locks in N min"; **Lock** ends it at once.
- **Leaving Settings locks it**: going idle, a call starting, opening any Options screen other than TV, or reloading the page.
- **Wrong PINs.** "Wrong PIN: N tries left". After 5 wrong PINs every screen is locked out for 10 minutes ("Too many wrong PINs: try again in 10 minutes"). Restarting `croom` on the TV Pi clears the count.
- **Change it** under **Security**: current PIN, new PIN, new PIN again. Every other screen's unlock ends.
- **Forgotten PIN.** On the TV Pi, `sudo rm /var/lib/croom/settings-pin.json`. No restart is needed; the next person chooses a new PIN.

### Settings pages

| Page | What it holds |
| --- | --- |
| Room | Name (required, up to 80 characters), location, company (shown on the door sign; blank shows the logo alone), time zone. |
| Calendar | Google calendar id; the service-account key, by **Upload** or **From USB stick** (a `.json` at the top of a stick in the **TV Pi**), then shown only as "Key for …, saved …"; booking hours. |
| Meetings | Platforms, camera and mic on at join, kiosk mode, the dialogs the room answers, ask about every dialog, reactions, Zoom SDK keys (upload, USB or **Type the keys**), Meet participants domain, **Sign in to Google**. |
| Devices | Speaker, microphone and camera on the TV Pi (or Automatic), camera travel time (1 to 60 s), the three camera preset names. |
| TV | Opens **Options › TV**; its Back returns to Settings. |
| Laptop switch | On or off, GPIO pin (BCM 0 to 27), press length (50 to 2000 ms), the camera's USB id, wait for a move (1 to 60 s), **Test the switch**. |
| AI & dashboard | AI switches and backend; the fleet dashboard's on/off, URL and enrolment token. |
| Network | A card per device (TV Pi, **Touch Pi**, door sign): Wi-Fi or Ethernet, network, signal, address, **Change Wi-Fi**, **Forget** (Section 06). |
| Check | **Calendar**, **TV**, **Laptop switch**, **Google sign-in**, **Zoom keys**; and **Capture the next Google Meet call** for support. None checks the touch Pi. |
| Logs | The TV Pi's `croom` journal, newest first, 200 lines and **Load more**, with **All** / **Warnings and errors**. **Not the touch Pi's logs.** |
| Software | A card for the TV Pi, one per joined device, **Update all**, and **Devices waiting to join** (Section 06). |
| Security | Change the PIN. |

### Saving rules

- **Checked before saving.** A refused value says why next to the field, and nothing is saved: "Not saved: see the settings marked".
- **Live or after a restart.** Live settings (booking hours, camera preset names) say **Saved**. Others say **Saved: applies after a restart** with **Restart now**.
- **Automatic undo.** After Restart now, the TV Pi watches the room for 2 minutes. If it does not start, the last good config comes back and the page says "Your last change was undone: the room did not start with it". The refused one is kept as `config.yaml.1`.
- **Not in a call.** Restart, reboot, update, Wi-Fi changes, Google sign-in and touch Pi jobs are refused during a call: "Leave the meeting first".
- **"Not available on this device".** Saving, keys, restart, Logs and the TV Pi's Software and Wi-Fi need the TV Pi's root helper `croom-admin`. If it is not running, those pages say this; the rest still work.

## Managing it from the room

*Once allowed, the room can reboot, update and move the touch Pi without SSH*

The agent checks in with the TV Pi every 5 seconds, and each answer can carry one job. Everything here is done under **Options › Settings** with the room PIN.

> **TESTED AGAINST STAND-INS**
>
> The agent, updates and the Wi-Fi safety net are covered by automated tests that use stand-ins for Git, systemd and NetworkManager. **They have not yet been proven on real Pis.** Try the first update and the first Wi-Fi change with someone at the table, outside meeting hours.

### The Touch Pi card

- **Seen.** "Checked in just now" or "N seconds ago" while online (checked in within 20 seconds); otherwise "Not seen since 10:42 AM", and its jobs wait.
- **Version.** The commit it runs, or "Version unknown". **Newest:** "Newest: 1a2b3c4, date" with the list of changes, or "Up to date".
- **Job.** What it is doing now, in the agent's own words, and the last result.
- **Buttons.** **Update**, **Reboot** (second tap) and **Forget** (second tap).

### Allow, Refuse, Forget

| Action | Effect |
| --- | --- |
| **Allow** | The waiting device joins and gets its jobs. At most 10 can wait; a waiting entry unseen for 10 minutes is dropped. |
| **Refuse** (tap twice) | Remembers the device's id and turns it away from then on: "This device was refused". |
| **Forget** (tap twice) | Removes a joined device and remembers nothing. For a replaced Pi; if the old one is still running it asks to join again. |

**Undo a Refuse** by giving the touch Pi a new identity. It then appears under Devices waiting to join.

```
sudo rm /var/lib/croom-kiosk/identity.json
sudo systemctl restart croom-kiosk-agent
```

### Jobs

| Job | What the touch Pi does | What the card reports |
| --- | --- | --- |
| Reboot | `sync`, then `systemctl reboot` | Rebooting, then Rebooted at the first check-in after |
| Update | Fetches the chosen commit of main, sets it up, reboots onto it on trial | Fetching main from GitHub, Getting…, Setting up…, Restarting on…, Updated to … |
| Wi-Fi scan | Lists up to 30 networks | Looking for networks |
| Wi-Fi connect | Joins with a 90-second safety net | Joining a network, then Joined … |
| Wi-Fi forget | Removes a saved network | Forgot … |

- **Never during a call.** Reboot, update and Wi-Fi changes are refused: "Leave the meeting first".
- **One at a time.** "The Touch Pi is busy: wait for it to finish".
- **Jobs live in the TV Pi's memory.** Restarting `croom` forgets a job not yet handed out.

### How an update runs

Versions are commits of `main` on GitHub `ben-abeo/croom.to`. The TV Pi checks the newest every 10 minutes; if it can't, the card says "Can't check for updates right now". Updating the touch Pi changes only its launcher, agent and Wi-Fi code. **The screens it shows come from the TV Pi**, so updating the TV Pi is what changes them.

1. Tap **Update** on the Touch Pi card, or **Update all** (touch Pis and door signs first, the TV Pi last). The card shows "Fetching main from GitHub".
2. The agent fetches main itself and checks the commit is on it. It does not trust the TV Pi for this.
3. It builds the new release beside the old one ("Getting …"), runs the new release's installer ("Setting up …"), then reboots ("Restarting on …").
4. After the reboot the old agent starts the new one on trial and waits up to **5 minutes** for it to check in.
5. It checks in: the card says "Updated to …" and the new release becomes the good one.

> **IT ROLLS ITSELF BACK**
>
> If the new version's setup fails, the Pi stays where it was: "The update to X did not happen: its kiosk setup did not finish". If the new version does not check in within 5 minutes (or crashes 3 times), the Pi goes back and reboots: "The update to X was undone: the touch Pi did not check in with it within 5 minutes. It is back on Y". A power cut at any step is recovered at the next start.

### Changing Wi-Fi safely

1. In **Settings › Network**, on the Touch Pi card, tap **Change Wi-Fi**. A list of networks appears; **Look again** rescans and **Hidden network** lets you type a name.
2. Choose a network, type the password and tap **Join**. The card shows "Joined X: checking that the touch Pi can do its job on it".
3. For up to 90 seconds the touch Pi checks every 5 seconds that it can reach the room page on the new network.
4. Proven: the card says "Joined X" and older profiles for that network are removed. Not proven: it goes back to the old network and says why.

- **Refused on purpose:** networks that ask for a user name (enterprise), WEP, a password on an open network, and forgetting the network it is on ("join another network first, or it would be cut off").
- **Hidden networks** with a password are joined as WPA2; a WPA3-only hidden network fails and goes back.
- **Power cut during the 90 seconds:** the touch Pi comes back on the old network.
- **The TV Pi waits for the touch Pi.** A Wi-Fi change on the TV Pi is proven by the touch Pi checking in, so it is undone if the touch Pi is offline.

### Updating by hand

When Settings can't reach the touch Pi, update it over SSH. Pull first (Section 04).

```
cd ~/croom.to && git checkout main && git pull
sudo bash installer/install-kiosk.sh --url http://<TV Pi address>:8080/
sudo reboot
```

## How it works

*A launcher in the desktop session and a root service, both pointed at port 8080*

### What runs on the touch Pi

| Process | Started by | Runs as |
| --- | --- | --- |
| Chromium kiosk | The labwc autostart line, or the **Room controls** icon. Not a systemd unit. | The desktop user |
| croom-kiosk-agent | systemd, at boot, after the network; restarts 5 s after a crash | root, system Python 3, standard library only |
| Trial agent | The good agent, for up to 5 minutes after an update reboot | root |

The touch Pi runs no `croom`, no `croom-admin` and no Python virtual environment.

### The kiosk launcher

1. If a Chromium kiosk is already running, it stops, so the icon never opens a second window.
2. It waits for the room page to answer: up to 60 tries, each a 2-second request and a 2-second pause. After that it opens Chromium anyway.
3. It starts Chromium full screen on the room page.

```
chromium --kiosk --force-device-scale-factor=1.5 --noerrdialogs --disable-infobars
  --disable-session-crashed-bubble --hide-crash-restore-bubble
  --check-for-update-interval=31536000 --password-store=basic
  --overscroll-history-navigation=0 --disable-pinch "http://<TV Pi>:8080/?keyboard=1"
```

### The page and the TV Pi

- **Polling.** `GET /api/status` every 2 seconds; `GET /api/calendar/events` every 30 seconds. A failed status shows "Can't reach the room".
- **No caching.** The page and its files are served with `Cache-Control: no-cache`.
- **No reload by itself.** As far as the code shows, the page does not reload after the TV Pi is updated. To be sure the table runs the new screens, reboot the touch Pi from Settings.
- **JSON only.** Every POST must be `Content-Type: application/json`, else 415.

### APIs the table uses

| Group | Routes on the TV Pi, port 8080 |
| --- | --- |
| Room | GET /, /api/status, /api/calendar/events, /api/calendar/week |
| Bookings | GET /api/bookings/slots; POST /api/bookings, /api/bookings/now, /api/bookings/{key}/end (only from the room page), /api/bookings/{key}/cancel |
| Meeting | POST /api/meeting/join, /leave, /mute, /camera, /control, /dialog, /chat/seen |
| Look and sound | /api/screensaver, /api/settings (key click, door sign background and scores), /api/audio/volume, /api/audio/mic |
| Camera | POST /api/camera/move, /zoom, /home, /presets/{slot}, /setup, /preview |
| TV and laptop | POST /api/tv/control, /pair, /select, /settings, /forget; GET /api/tv/discover; POST /api/laptop |
| Touchpad | WebSocket /ws/pointer, refused to pages of another origin |
| Settings (PIN) | /api/settings/… with header X-Croom-Unlock; the PIN routes /api/settings/pin, /pin/set, /unlock, /lock |
| Agent | POST /api/devices/checkin with header X-Croom-Device-Token |

The everyday routes have no login: the room page is meant for the office LAN only. Only Settings is behind the PIN.

### Check-in protocol

```
POST http://<TV Pi>:8080/api/devices/checkin        every 5 s
X-Croom-Device-Token: <token>
{"id": "<32 hex>", "kind": "touch", "version": "<commit>", "name": "<hostname>",
 "network": {…}, "job": <last job report or null>}

→ {"state": "pending"}
→ {"state": "allowed", "job": {"id", "type", "args"} | null}
```

The touch Pi makes its own id and 256-bit token on first start; no token ever comes from the TV Pi, which keeps only a SHA-256 hash of it. A job not acknowledged is handed out again after 15 seconds.

### Files on the touch Pi

| Path | What |
| --- | --- |
| ~/croom.to/ | The git clone the installer ran from |
| /usr/local/bin/crystal-meet-kiosk | The kiosk launcher (root, 755) |
| ~/.config/labwc/autostart | Starts the launcher at login |
| ~/Desktop/room-controls.desktop | The Room controls icon |
| /etc/systemd/system/croom-kiosk-agent.service | The agent's unit |
| /opt/croom-kiosk/repo.git | Bare clone, origin github.com/ben-abeo/croom.to |
| /opt/croom-kiosk/releases/<commit>/ | Sealed releases, each with a VERSION file |
| /opt/croom-kiosk/good, current | Links: the release systemd runs the agent from; the release whose kiosk setup is installed |
| /var/lib/croom-kiosk/ (root, 700) | config.json, identity.json, update.json, job.json, wifi.json, trial-ok (each 600) |

### Security in brief

- **An open room page.** On the LAN, anyone can open the room page, drive the call, the camera and the TV, and read `/api/status`, which includes the meeting link and recent chat. This is by design; keep the room on the office network.
- **Plain HTTP.** The PIN, keys and Wi-Fi passwords cross the LAN unencrypted, including the Wi-Fi password sent to the touch Pi in a check-in answer.
- **A narrow root agent.** It accepts only five jobs, checks every argument, never runs a shell, checks an update's commit on GitHub itself, and runs only root-owned releases no one else can write.

## Logs and diagnostics

*The touch Pi's own story is in its agent journal, read over SSH*

There is no separate health check for the touch Pi. It is healthy when the kiosk shows the room page and its card in **Settings › Software** says "Checked in". Settings › Logs shows only the TV Pi.

### On the touch Pi

```
systemctl status croom-kiosk-agent
sudo journalctl -u croom-kiosk-agent -n 100          # check-ins, joining, jobs, updates, Wi-Fi
sudo journalctl -u croom-kiosk-agent -f              # follow it live
sudo cat /var/lib/croom-kiosk/config.json            # the room address and desktop user
sudo cat /var/lib/croom-kiosk/update.json            # last update: state, commit, from, why
sudo cat /var/lib/croom-kiosk/job.json               # last job and its result
sudo cat /var/lib/croom-kiosk/wifi.json              # last Wi-Fi change
ls -l /opt/croom-kiosk/                              # current and good point at the running release
cat /opt/croom-kiosk/good/VERSION
pgrep -af "chromium.*--kiosk"                        # is the kiosk running?
curl -s http://<TV Pi>:8080/api/status | head -c 300 # does the TV Pi answer?
nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status
cat ~/.config/labwc/autostart /usr/local/bin/crystal-meet-kiosk
```

Chromium's own output is not saved anywhere by the installer.

### Agent journal lines

| Line | Meaning |
| --- | --- |
| Checking in with <url> as <id>, version <short> | The agent has started. |
| This touch Pi is <id>: it waits to join until Allow is tapped in Settings › Software | First start: a new identity. |
| Waiting to join: tap Allow in Settings › Software on the room page | Allow it (Section 04). |
| Can't reach the room at <url>: … | Network, address, or the TV Pi is down. |
| The room refused the check-in (<status>): <words> | Refused, or a token mismatch (Section 09). |
| Checking in with the room | Recovered after a problem. |
| Updated to <short> / The update to … was undone (…) | The end of an update. |
| No room address in /var/lib/croom-kiosk/config.json | Re-run the installer with `--url`. |
| Wi-Fi: <words> | A Wi-Fi job's progress. |

Trouble lines are written once when something changes, not every 5 seconds.

### On the TV Pi

```
sudo systemctl status croom croom-admin
sudo journalctl -u croom -n 50                 # look for the first ERROR; joins and Allows are logged here
sudo journalctl -u croom-admin -n 50
sudo cat /var/lib/croom/devices.json           # joined and refused devices (tokens hashed)
/opt/croom/venv/bin/croom --check-calendar -c /etc/croom/config.yaml
/opt/croom/venv/bin/croom --check-tv -c /etc/croom/config.yaml     # as the desktop user, never root
curl http://<TV Pi>:8080/api/meeting/diagnostics   # in a call only; opens menus on the TV briefly, never poll it
```

The TV Pi's journal records "A touch Pi asks to join: <name> at <address>", "The touch Pi <name> may join" and each job asked for from Settings.

## Troubleshooting

*Start with two checks, then find the symptom*

From the touch Pi (or a laptop on its network), run `curl http://<TV Pi>:8080/api/status`. If it answers, the problem is on the touch Pi; if not, it is the TV Pi or the network. On the touch Pi, read `sudo journalctl -u croom-kiosk-agent -n 100`.

### The screen and the kiosk

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Desktop shown, no room page | Kiosk not started: auto-login off, autostart line missing, or the browser was closed. | Tap **Room controls** on the desktop. Check auto-login and that `~/.config/labwc/autostart` has the launcher line. Re-run the installer (pull first). |
| The page takes minutes to appear after a boot | The launcher waits for the TV Pi to answer. | Normal while the TV Pi starts. If it happens every time, check the TV Pi and the network. |
| Browser error page ("can't be reached") | The TV Pi was down for the whole wait, or its address changed. | Check `sudo systemctl status croom` on the TV Pi and its fixed address. Reboot the touch Pi. If the address changed, re-run the installer with the new `--url`. |
| Top bar says "Can't reach the room" | Status requests fail: the TV Pi is down or restarting, or the network dropped. | Wait a moment after a restart. Run the curl check from the touch Pi; read `journalctl -u croom` on the TV Pi. |
| A `.local` address fails | Name lookup (mDNS) not working on the network. | Re-run the installer with the TV Pi's IP address. |
| No on-screen keyboard | The URL lacks `?keyboard=1` (a tablet, or a browser opened by hand). | Re-run the installer, which adds it. |
| Everything tiny or huge | An old launcher without the 1.5× scale. | Re-run the installer and reboot. |
| Screen goes blank when idle | Blanking was not turned off. | `sudo raspi-config nonint do_blanking 1`, then reboot. |
| No key click | Key click is off, or the touch Pi has no audio output. | **Options › Key click on**; check the touch Pi's audio output. |
| Screens did not change after the TV Pi was updated | The kiosk still has the old page loaded. | Reboot the touch Pi from **Settings › Software**. |

### Joining and check-in

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Not listed under Devices waiting to join | Agent stopped, wrong address in config.json, TV Pi unreachable, refused before, or 10 already waiting. | `systemctl status croom-kiosk-agent` and its journal. Check `config.json`. "Too many devices are waiting to join": wait 10 minutes or deal with the others. |
| "No room address in /var/lib/croom-kiosk/config.json" | Installed without the agent, or the file was deleted. | Re-run the installer with `--url`. |
| "The room refused the check-in (403): This device was refused" | Someone tapped Refuse. | Delete `identity.json` and restart the agent (Section 06), then Allow. |
| "The room refused the check-in (403): This device's token is not the one it joined with" | The identity was replaced while the old id is still allowed. | In Settings › Software, **Forget** the old card; the Pi then waits to join: **Allow**. |
| "Can't reach the room at <url>: …" | Network, TV Pi down, or wrong address. | Check the network, the TV Pi, and `config.json`. |
| Card says "Not seen since …" | Touch Pi off, agent stopped, or network down. | Check power and network; `sudo systemctl restart croom-kiosk-agent`. Jobs wait until it returns. |

### Updates and jobs

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| "Version unknown", or "This touch Pi was not set up for updates: run install-kiosk.sh on it once more" | The agent runs from a clone, or its release links are wrong. | Pull, re-run the installer, reboot. |
| "Can't reach GitHub (…)" | No HTTPS from the touch Pi to github.com. | Fix outbound HTTPS or DNS, then retry. The running version is untouched. |
| "That version is not on main on GitHub" | The commit is not on main. | Use **Check again** and choose a version on main. |
| "The update to X did not happen: its kiosk setup did not finish (…)" | The new release's installer failed (apt, disk space). | The Pi stays on the old version. Read the agent journal, fix, retry. |
| "The update to X was undone: the touch Pi did not check in with it within 5 minutes" | No network after the reboot, or a fault in the new release. | Check the network and TV Pi; retry. Report the commit if it repeats. |
| "The update to X was undone: … after 3 starts" | Power cuts or crashes during the trial. | Steady the power; retry. |
| "The touch Pi restarted before it finished" / "stopped before it finished" | The Pi restarted or stopped mid-job. | Retry the job; read the agent journal. |
| "The Touch Pi is busy: wait for it to finish" | One job at a time. | Wait for the current job. |
| "This touch Pi has not joined yet: allow it first" | A job for a waiting device. | **Allow** it first. |
| "Leave the meeting first" | Reboot, update or Wi-Fi change during a call. | Leave the call and retry. |
| Update all refused with words about the laptop | Laptop mode has the camera. | Tap **Back to room** first. |
| "Can't check for updates right now" | The TV Pi can't reach GitHub. | Check the TV Pi's internet. Everything else works. |

### Wi-Fi

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| "Joined X, but the touch Pi could not reach the room at <url> within 90 seconds. The touch Pi is back on Y" | The new network does not route to the TV Pi (another VLAN, guest network, client isolation). | Choose a network that reaches the TV Pi, or move it by hand at the Pi. |
| "Could not join X: …" | Wrong password or out of range. | Retype the password. It is back on the old network already. |
| "X asks for a user name…" / "X uses WEP…" | Enterprise or WEP network. | Not supported: use Ethernet or a WPA2/WPA3-Personal network. |
| "This touch Pi has no Wi-Fi" | No Wi-Fi radio managed by NetworkManager. | Use Ethernet; check `nmcli device status`. |
| "A Wi-Fi change is under way: wait for it to finish" | A change is still in its 90 seconds. | Wait for it. |
| The TV Pi's Wi-Fi change is undone after 90 s | The touch Pi was offline, so it could not prove the change. | Bring the touch Pi online first. |

### Settings and the PIN

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| "Settings locked: enter the PIN again" | 5 minutes passed, Settings was left, `croom` restarted, or the PIN changed elsewhere. | Enter the PIN again. |
| "Too many wrong PINs: try again in N minutes" | 5 wrong PINs. | Wait 10 minutes, or `sudo systemctl restart croom` on the TV Pi. |
| PIN forgotten | — | On the TV Pi, `sudo rm /var/lib/croom/settings-pin.json`, then choose a new one. |
| "Not available on this device" | `croom-admin` is not running on the TV Pi. | `sudo systemctl status croom-admin` and its journal; re-run the TV Pi's installer. |
| "Your last change was undone: the room did not start with it" | A saved setting stopped the room from starting. | Fix the value; the refused config is `/etc/croom/config.yaml.1`. |

### Calls, TV and devices

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| "Unsupported meeting link" / "A meeting is already in progress" | The platform is not turned on in Settings, or a call is running. | Use a supported link; leave the current call first. |
| "Couldn't join": "Zoom needs the meeting passcode" | No passcode in the link. | Type the passcode and tap **Join again**. |
| Join refused with words about the laptop | Laptop mode has the camera. | **Back to room**. |
| "End now is on the room's touch screen" | End was asked for from somewhere other than the table. | Use **End now** on the table. |
| Touchpad says "Reconnecting…" | The touchpad link dropped. | It retries by itself; check the TV Pi. |
| Room Pi mouse does nothing ("needs /dev/uinput") | The TV Pi's virtual mouse is not set up. | On the TV Pi check `ls -l /dev/uinput` and the rule `99-croom-uinput.rules`; re-run its installer; restart croom. |
| "The TV is not answering at …" / Find TVs lists nothing | TV off, on another network, or a firewall on the TV Pi. | Run `croom --check-tv` on the TV Pi; see the room setup guide. |
| "The TV no longer accepts this table" | The TV forgot the pairing. | **Forget TV**, **Pair**, and accept on the TV within a minute. |
| "Wake unavailable · Add a MAC address in Change TV" | No TV MAC address saved. | **Change TV › TV MAC address › Save connection**. |
| "No speaker found" / "No microphone found" / "No controllable camera found" | The TV Pi does not see the device. | Check the USB cables on the TV Pi, reboot it, then **Settings › Devices**. |
| "Reading the list from Meet" | Meet's people panel is still opening on the TV. | Wait a moment with the screen open. |
| "Calendar offline since …" | The TV Pi can't reach Google. | Bookings sync later. Check the TV Pi's internet and `croom --check-calendar`. |

## Reference

*Names, numbers and files on a few pages*

| What | Value |
| --- | --- |
| Room page | http://<TV Pi>:8080/?keyboard=1 |
| Installer | installer/install-kiosk.sh --url http://<TV Pi>:8080/ |
| Agent | tools/kiosk_agent/croom_kiosk_agent.py, service croom-kiosk-agent |
| Check-in | POST /api/devices/checkin every 5 s, kind "touch", header X-Croom-Device-Token |
| Versions | Commits of main on github.com/ben-abeo/croom.to |
| Settings PIN | 4 to 8 digits; 5 minutes unlocked; 5 tries, then 10 minutes locked |
| Desktop icon | Room controls |
| Hardware | Raspberry Pi (model not specified), 10.1" 1920×1200 Forzola touch panel (part number and power not specified) |

### Timers and limits

| Value | Where it applies |
| --- | --- |
| Check-in every 5 s; online if seen within 20 s; job resent after 15 s | Agent and the TV Pi's registry |
| Agent restarted 5 s after a crash | systemd unit |
| Update trial 5 min, at most 3 starts; fetch and setup time out after 15 min | Agent |
| Wi-Fi proof within 90 s, checked every 5 s; join wait 45 s; status re-read every 30 s | Agent's Wi-Fi code |
| Status every 2 s, events every 30 s; second tap within 5 s | The room page |
| Options return after 10 min; meeting screens after 2 min; reactions row 10 s; tap guard 0.5 s | The room page |
| Launcher waits at most 60 tries for the TV Pi | crystal-meet-kiosk |
| Restart watch 2 min; TV Pi update watch 3 min; GitHub answer cached 10 min | TV Pi |
| Sign in to Google cancelled after 15 min without a touch | TV Pi |

### Ports

| Port | Use |
| --- | --- |
| TCP 8080, touch Pi to TV Pi | Room page, APIs, touchpad, check-in |
| TCP 443, touch Pi to github.com | Fetching an update |
| TCP 80 and 443, touch Pi to apt mirrors | Install only |
| TCP 22, to the touch Pi | SSH, if turned on |
| None | Crystal Meet opens no listening port on the touch Pi |

### Installer environment overrides

For tests and unusual setups: `KIOSK_URL`, `KIOSK_USER`, `KIOSK_HOME`, `BIN_DIR`, `SKIP_PACKAGES`, `SKIP_RASPI_CONFIG`, `SKIP_AGENT`, `KIOSK_SOURCE`, `KIOSK_REPO_URL`, `AGENT_DIR`, `AGENT_STATE`, `AGENT_PYTHON`, `UNIT_DIR`, `RELEASE_OWNER`. The agent sets `KIOSK_FROM_AGENT` itself during an update.

### Source map

| Path | Holds |
| --- | --- |
| installer/install-kiosk.sh | The touch Pi installer |
| tools/kiosk_agent/croom_kiosk_agent.py | The agent: check-in, jobs, updates, trials |
| src/croom/admin/nmcli.py | Wi-Fi with the safety net, shared with the TV Pi |
| src/croom/settings/checkin.py | The device registry on the TV Pi |
| src/croom/control/service.py | Every route |
| src/croom/control/static/ | The room page: index.html, app.js, booking.js, devices.js, keyboard.js, remote.js, touchpad.js, settings*.js, style.css, icons.svg |
| docs/superpowers/specs/2026-10-29-room-settings-design.md | The design of room settings (sections 6 and 7 for the touch Pi) |

### Tests

Run with pytest from the repository: `tests/unit/kiosk_agent/` (the agent), `tests/unit/installer/test_install_kiosk_script.py` (the installer) and `tests/unit/control/` (the room page and its routes).

```
cd ~/croom.to
.venv/bin/python -m pytest tests/unit/kiosk_agent tests/unit/installer/test_install_kiosk_script.py
.venv/bin/python -m pytest tests/unit/control
```

