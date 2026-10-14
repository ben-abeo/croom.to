# The TV remote and the touchpad

**Date:** 2026-10-13 · **Status:** draft for Ben's review · **Builds on:** the room page in two modes (spec 2026-10-12), the TV screensaver and one page (spec 2026-10-07), meeting controls and dialogs (spec 2026-10-13).

## 1. What this is for

The conference rooms have LG C6 OLED TVs behind the room Pi (PiMeet-3), and the table has the Crystal Meet touchscreen (PiMeet-2). Ben's asks: "all controller functionality, plus a trackpad for controlling the cursor on the TV. I should be able to control power, channels, volume, inputs, number inputs, menu options, exit/back buttons, a d-pad like button system"; "accessed not only outside meetings, but also during meetings"; "a button which could turn the whole touchscreen into a mouse touch pad that could control the meeting room tv Pi (PiMeet-3)… moving a finger around would control the mouse and tapping the screen would select something"; "make it look professional and clean", in the Crystal PM house style. Earlier decisions: the TVs are on the same network and pairing is fine; the TV is on by default and can be turned on and off from the table.

The outcome: the table replaces the TV's remote and a mouse on the room Pi, so nobody needs either in the room. The remote is one screen away from both the idle and the meeting screens; the touchpad modes let a person point at anything on the TV, whether it is the TV's own menu or something the room Pi shows, such as a Zoom dialog the agent could not answer.

Out of scope: typing into the room Pi from the table (a later version carries the page's keyboard through the same channel), controlling TVs of other makes, and the TV's own apps beyond launching them.

## 2. How the TV is reached

LG webOS TVs take commands over the network through their SSAP WebSocket (port 3001 with TLS, 3000 without) after a one-time pairing that yields a client key. The Python library `aiowebostv` (Home Assistant's, Python 3.11 or later) wraps it: pairing (`WebOsClient(host, client_key)`, `connect()`, `client_key`), state subscriptions (power, volume, mute, current app, input, channel), and the controls: `power_off()`, `power_on()`, `volume_up()/volume_down()/set_volume(n)`, `set_mute(bool)`, `channel_up()/channel_down()/set_channel(id)`, `get_inputs()/set_input(id)`, `get_apps()/launch_app(id)`, `send_message(text)` (a toast on the TV), `button(name)` for the remote's keys over the pointer input socket, and `move(dx, dy)`, `click()`, `scroll(dx, dy)` for the Magic Remote cursor. The remote key names the library accepts: `LEFT RIGHT UP DOWN ENTER BACK EXIT HOME MENU QMENU INFO GUIDE LIST RECENT MYAPPS INPUT_HUB MUTE VOLUMEUP VOLUMEDOWN CHANNELUP CHANNELDOWN PLAY PAUSE STOP REWIND FASTFORWARD RECORD RED GREEN YELLOW BLUE 0–9 DASH ASTERISK CC AD SAP ASPECT_RATIO SCREEN_REMOTE TELETEXT TEXTOPTION 3D_MODE LIVE_ZOOM MAGNIFIER_ZOOM PROGRAM AMAZON NETFLIX POWER`.

Power on from standby is the one thing the socket cannot do: a TV that is off has no socket. The standard way is Wake-on-LAN to the TV's MAC address, which LG enables through the TV's "Mobile TV On" / "Turn on via Wi-Fi" setting (Quick Start+ keeps the network alive in standby). The agent sends the magic packet (`wakeonlan`) and then waits for the socket. The room Pi is also on the TV's HDMI, so HDMI-CEC from the Pi (`cec-ctl`) could power the TV on and switch its input without the network; this spec keeps CEC as the fallback to add if Wake-on-LAN proves unreliable on the C6, and does not build it now.

## 3. The TV service on the room Pi

A new service in the agent, `croom.tv` (`src/croom/tv/`), beside `devices`:

- **Configuration** (`tv:` in `config.yaml`): `enabled`, `host` (the TV's IP or hostname), `mac` (for Wake-on-LAN), `pi_input` (the TV input the room Pi is on, such as `HDMI_1`), `keep_on` (default true). The client key lives in the agent's data dir (`/var/lib/croom/tv-client-key`, mode 600), written by the pairing, never in the config.
- **Discovery and pairing** from the table: the Options screen gets **TV**; its first run shows **Find TVs** (SSDP search for LG webOS TVs on the LAN, listing name and address) and **Pair** (connects without a key; the TV shows its pairing prompt; the person accepts it on the TV with the TV's own remote, once; the key is stored). A paired TV shows its name, its state (on, standby, unreachable) and **Forget TV**. `croom --check-tv` prints the same from a shell.
- **Connection**: one `WebOsClient` kept connected while the TV is on, reconnecting with back-off; subscriptions keep a state model (`on`, `volume`, `muted`, `input`, `app`, `channel`, `inputs` list, `apps` list) that `/api/status` carries under `tv`.
- **Keeping the TV on**: when `keep_on` is true the service wakes the TV at agent start, before every meeting join (the meeting waits up to 15 s for the TV, then joins anyway), and whenever its state goes off while nobody turned it off from the table. A manual **Off** from the table holds until a manual **On** or the next meeting join. The screensaver page is what the TV shows when on and idle, as today, on `pi_input`.
- **Controls**, through `POST /api/tv/control {"name", "value"?}` answered like the meeting's controls (200 with the new state, 409 with words): `power` (on: Wake-on-LAN then wait; off: `power_off`), `volume_up`, `volume_down`, `volume` (value 0–100), `mute`, `channel_up`, `channel_down`, `channel` (a number typed), `input` (an id from the inputs list; `pi` switches to `pi_input`), `app` (an id), `key` (one of the remote key names above), `digit` (0–9 typed into the TV as the remote's numbers), `toast` (a message on the TV). Every control re-reads the state and the table shows what the TV has.
- **The cursor**: a WebSocket at `/ws/pointer` carries the touchpad's events from the table to the room Pi (section 5). For the TV, the service forwards `move` as `move(dx, dy)` on the pointer socket, `tap` as `click()`, `scroll` as `scroll(dx, dy)`, `down`/`up` as `move(..., down=1)` for drags.

Failures answer in words: an unpaired TV ("Pair the TV under Options › TV"), an unreachable one ("The TV is not answering at 10.0.0.41"), a key the TV refused.

## 4. The room Pi as a mouse

The second touchpad target is the room Pi itself. The agent creates a virtual mouse with `evdev.UInput` (relative X/Y, left and right buttons, wheel), which the Pi's desktop (labwc or X11) takes like any USB mouse; the cursor appears on the TV and clicks land on whatever the Pi shows, the kiosk Chromium included. The installer adds a udev rule for `/dev/uinput` and puts the croom user in the `input` group. The same `/ws/pointer` messages drive it with `target: "pi"`: `move` → relative motion, `tap` → left click, `two-finger tap` → right click, `scroll` → wheel, `down`/`up` → press and release for drags. A `keys` message type carries key presses for the later version (typing into the Pi); this version defines the message and refuses it.

Pointer gain, acceleration and the tap threshold live in the page (section 5); the service applies integer deltas as they arrive.

## 5. The table

### 5.1 Where it lives

- **Idle mode:** Options › **TV** (a menu item beside Camera framing and Screensaver).
- **Meeting mode:** **TV** in the meeting screen's secondary row (the one that holds Captions, View, Participants, Chat, Host).
- Both open the same `tv` view, which belongs to whichever mode opened it (like the camera view) and returns there with Back, or on its own after 10 idle minutes in idle mode and 2 in meeting mode.

### 5.2 The remote

One screen, no scrolling at 1280 by 800, laid out like a remote a person already knows but with the table's room:

```
+---------------------------------------------------------------+
| Back        LG C6 · Boardroom TV · On · HDMI 1 (Crystal Meet)  |
|                                                                |
|  [Power]  [Input ▾]  [Home]  [Menu]      Vol  [-] [ 24 ] [+]   |
|                                           [Mute]   Ch [▲] [▼]  |
|                                                                |
|          ╭─────────────╮         Numbers   [1][2][3]           |
|          │      ▲      │                   [4][5][6]           |
|          │  ◀  OK   ▶  │                   [7][8][9]           |
|          │      ▼      │                   [-][0][Go]          |
|          ╰─────────────╯                                       |
|   [Back] [Exit] [Info] [Guide]      [⏮][⏯][⏭]  [●][●][●][●]   |
|                                                                |
|  [ Pointer ]  [ Room Pi mouse ]  [ Full-screen touchpad ]      |
+---------------------------------------------------------------+
```

- The top line names the TV and its state; a TV that is off shows **Turn on** in place of the state; an unpaired room shows the pairing card instead of the remote.
- **Power** asks for a second tap when turning off (the way Leave does). **Input** opens a row of the TV's inputs with the room Pi's one marked (**Crystal Meet**). **Numbers** type into the TV as the remote's digits; **Go** sends ENTER.
- The d-pad is one round control: four directions and OK, with a press that lights the pressed sector. Back, Exit, Info and Guide sit under it, where a thumb finds them.
- The playback row and the four colour keys are there because the remote has them; they take little room.
- **Pointer** turns the d-pad's disc into a trackpad for the TV's cursor (the disc grows to the whole middle of the screen while the pointer is on; a tap on it clicks; two fingers scroll; a second tap on **Pointer** brings the d-pad back). **Room Pi mouse** does the same with the room Pi as the target. **Full-screen touchpad** (section 5.3) is the mode Ben described.
- Icons on every button from the page's sprite (spec 2026-10-13 meeting controls, section 6.2); labels in sentence case; the remote keys the TV may refuse (colour keys on a menu-less input) still send, and the TV's own answer shows in the status line.

### 5.3 The full-screen touchpad

A tap on **Full-screen touchpad** (or **Room Pi mouse** then **Full screen**) hands the whole touchscreen to the pointer: a dark surface with a thin top strip naming the target ("Moving the mouse on the room's screen" / "Moving the TV's cursor"), a switch between the two targets, and **Done**. On the surface: one finger moves (relative, with a light acceleration curve so a flick crosses the screen and a slow drag is precise), a tap clicks, a two-finger tap right-clicks (room Pi only), two fingers scroll, a press-and-hold then move drags. Nothing else is on the surface, so nothing is tapped by mistake. The table's own idle return is paused while the touchpad is up; **Done** or 10 idle minutes return to the screen that opened it. The page sends events over `/ws/pointer` as they happen (coalesced to one `move` per animation frame, about 60 a second, with integer deltas and the remainder carried over) and shows a connection mark in the strip when the socket drops.

### 5.4 Design direction

Brief and audience: a touch remote on a 10-inch table screen in a Crystal PM conference room, used by whoever is in the room, often under time pressure in a meeting. Its job is to be found and used without thought, and to look like it belongs to the room page beside it.

- **Tokens** (the room page's own, from the Crystal brand): surface `#111827` and `#0B1220` for the tray, navy `#001636`/`#16244F` for pressed and raised states, blue `#1B52E5` for the one accent, periwinkle `#BDCEFF` for secondary text, white for labels; the warn amber `#E8A013` only for the second-tap Off. Lexend throughout; the type scale: 1.4 rem for the d-pad's OK, 1.05 rem for keys, 0.95 rem for the status line, numbers in tabular figures.
- **The one memorable element** is the d-pad disc: a single round control with a soft inner ring, whose sectors light when pressed and which becomes the trackpad in place, so the screen never rearranges when the person switches between keys and pointer. Everything else is quiet, flat and aligned on a 12-column grid with 12 px gaps; buttons are the room page's buttons (rounded 12 px, outlined on the dark surface, filled blue only for Power when the TV is off and for the active mode).
- **Not** a skeuomorphic remote, not a card grid, no gradients, no shadows, no labels in capitals, no decoration that is not a control. Pressed state answers the finger within a frame (a darker fill and a 1 px inset), and the key the TV refused flashes amber once with its reason in the status line.
- Fits 1280 by 800 without scrolling; on a phone the rows stack and scroll.

## 6. Configuration, status and API

```yaml
tv:
  enabled: true
  host: "10.0.0.41"      # or the TV's hostname; left empty until Find TVs fills it
  mac: "AA:BB:CC:DD:EE:FF"  # for turning the TV on (Mobile TV On must be enabled on the TV)
  pi_input: "HDMI_1"       # the input the room Pi is on
  keep_on: true
```

`/api/status` gains `tv: {configured, paired, on, reachable, name, volume, muted, input, inputs: [{id, label}], app, channel, keep_on, held_off}`. Routes: `POST /api/tv/control`, `POST /api/tv/pair`, `POST /api/tv/forget`, `GET /api/tv/discover`, `WS /ws/pointer`. The pointer socket's messages are JSON lines: `{"target": "tv"|"pi", "type": "move"|"tap"|"right"|"scroll"|"down"|"up", "dx", "dy"}`.

## 7. Testing

- **TV service** against a fake `WebOsClient` (recording calls, scripted state): pairing stores the key with mode 600 and forgets it; controls map to the right calls and re-read state; `keep_on` wakes at start, before a join, and after an unexpected off, but not after a manual off; Wake-on-LAN sends the magic packet to the configured MAC and waits for the socket with a 15 s cap; failures answer in words.
- **Pointer**: the `/ws/pointer` handler forwards TV moves, taps and scrolls to the fake client and Pi moves, clicks and wheel to a fake `UInput`; a `keys` message is refused with words; a dropped socket stops nothing.
- **Control service**: the routes, the status shape, 409s with words, `/api/tv/discover` with a fake SSDP answer.
- **Room page** (Playwright): the TV view from both modes with Back to each; the remote's buttons post the right controls; the pairing card when unpaired; Power's second tap; the Numbers pad; the pointer modes and the full-screen touchpad send the right events for a finger path (synthetic pointer events: one move per frame, a tap, a two-finger scroll) and return on Done; `fits()` at 1280 by 800; icons on every button.
- **Installer**: the udev rule and the group; `croom --check-tv`.
- **Pi acceptance** (Ben): pair the Boardroom TV from the table; every key; power off and on from the table; the TV coming back on by itself after being switched off with its remote; input switching to and from the Pi; the TV cursor from the trackpad; the Pi mouse clicking a Zoom dialog on the TV during a meeting; the full-screen touchpad from inside a meeting.

## 8. Decisions made here

- Network control through webOS with Wake-on-LAN for power on, HDMI-CEC held as the fallback (Ben: same network, pairing fine).
- The TV stays on by default; a manual Off holds until a manual On or the next meeting join (Ben: always on by default, manual on/off from the table).
- The remote is reachable from both modes (Ben: outside and during meetings).
- The room Pi is the second pointer target through a virtual mouse, with typing to follow later (Ben: the whole touchscreen as a touchpad for PiMeet-3).
- The design keeps the room page's language and spends its one bold element on the d-pad disc that becomes the trackpad (Ben: professional and clean, the Crystal style).
