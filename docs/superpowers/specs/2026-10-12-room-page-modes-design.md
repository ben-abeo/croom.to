# The room page in two modes: design

**Date:** 2026-10-12. **Branch:** `room-page-modes`, cut from `room-devices` (PR #3); it lands after it.
**Follows:** the sound and camera controls (`2026-10-08-sound-and-camera-controls-design.md`) and the TV screensaver (`2026-10-07-tv-screensaver-and-one-page-design.md`).
**Next project:** TV control for the LG C5s (its own spec) adds a TV screen through the view switch this design introduces.

## 1. Why

The table screen on PiMeet-2 is the only thing anyone touches. Today the room page is one long column (status, bookings, the link box, Sound, Camera, the screensaver picker), so on the 10.1-inch touch display it scrolls, and a meeting shows the same column as an empty room. The page should show exactly what the moment needs and never scroll: when the room is free, how to join and what is booked; during a meeting, the call's controls. Two smaller faults ride along: the kiosk starts Chromium at normal scale on a 1920 by 1200 panel, so everything is small; and a room without a location shows the installer's `REPLACE_WITH_LOCATION` placeholder under its name.

## 2. Scope

In: the room page's screens, modes and navigation; the kiosk launcher's scale; the location placeholder; the docs.
Out: TV control (the next project); the door sign and the TV pages; the dashboard; a phone layout beyond "still usable" (a phone may scroll, as today).

## 3. Decisions

- **One page, switching views.** The page keeps its one status poll and one model. A `data-view` attribute on `<body>` names the visible screen; every screen is a `<section class="view" data-view="...">` in `index.html`, and only the matching one is shown. Views live in the page's memory only: a reload lands on the mode's main screen.
- **Sized for 1280 by 800.** The kiosk launcher starts Chromium with `--force-device-scale-factor=1.5`, so the 1920 by 1200 Forzola panel lays the page out at 1280 by 800 with buttons about 11 mm tall. Every screen fits that viewport with no page scrolling; only the bookings list and the upcoming list scroll, inside their own boxes. The non-scrolling layout applies from 1024 by 700 up; smaller viewports (a 2 times scale, a phone) use the scrolling layout. Within it, a screen that cannot fit (a very long refusal in a short window) scrolls under the top bar instead of being cut off.
- **Mode follows the agent.** Meeting mode whenever `meeting.state` is `joining`, `in_lobby`, `connected` or `leaving` (the same four states the page and the server already use); idle mode otherwise. Entering a mode lands on its main screen; sub-screens never carry over.
- **Names people see:** Options, Camera framing, Screensaver (no longer "TV when idle"), Camera, Upcoming meetings, Back.
- **The camera preview follows the Camera framing screen.** On while that screen is shown in idle mode, off the moment it is left. The Camera screen in a meeting never asks for a preview.
- **Idle screens return on their own.** In idle mode, any screen other than the idle screen returns to it after ten minutes without a tap, which also ends the preview. In meeting mode a sub-screen stays until Back or the meeting ends.
- **Sound lives on the meeting screen.** Volume is reached for during a call; between meetings nobody needs it. The row drives the room speaker as configured (the MeetUp, or the TV over HDMI); the TV's own volume arrives with the TV project.
- **The location placeholder counts as none.** Decided once where the config is loaded, so the page, the door sign and the dashboard agree.

## 4. Design

### 4.1 Screens

Each screen is a `<section class="view" data-view="NAME">`; `body[data-view="NAME"]` shows it and hides the others (`.view { display: none } body[data-view="idle"] .view[data-view="idle"] { display: grid }`, one rule per screen). Existing element ids are kept so the current tests and the TV preview keep working; new ids are listed here.

**Top bar** (shared by every screen, outside the views): logo, `#room-name`, `#room-location` (hidden when empty), `#clock`, and `#offline-note` ("Can't reach the room") whenever the page cannot reach the room, so every screen says so. In idle mode it also shows `#open-options` ("Options"); in meeting mode `#meeting-state` (Joining, In the lobby, In a meeting, Leaving), `#meeting-elapsed` ("12 min") and `#next-warning` (hidden unless the next booking starts within ten minutes: "Next: 2:00 PM Design review"). Ids appear once in the page.

**`idle`** (main screen of idle mode)
- Status card as today (`#kicker`, `#headline`, `#detail`, `#message`, `#actions`); in idle mode `#actions` holds Join when a booking is current, or Dismiss after a failed join.
- Join row as today: `#link-form` with `#link-input`, `#keyboard-toggle`, the Join button.
- Today: `#events` in a box (`.schedule`) that scrolls on its own; `#calendar-note`.

**`options`**
- `#back-from-options` ("Back", to `idle`).
- Two large buttons: `#open-camera-setup` ("Camera framing") and `#open-screensaver` ("Screensaver"). The TV project adds "TV remote" here.

**`camera`** (used by both modes)
- `#back-from-camera` ("Back", to `options` in idle mode, to `meeting` in meeting mode).
- `#camera-note` and `#camera-status`; the controls in two columns: left `#arrow-pad` with `#camera-home` at its centre, and the zoom buttons; right `#preset-row`, `#preset-save-mode`, `#setup-link`, `#camera-setup` with `#find-stops` and `#save-home` (all unchanged).
- In meeting mode `#preset-save-mode`, `#setup-link` and `#camera-setup` are hidden: recall and nudge only. The `<details id="camera-panel">` disappears; `#camera-panel` becomes the plain container of these controls.
- "Camera unavailable" shows `#camera-note` and hides the controls, as today.

**`screensaver`**
- `#back-from-screensaver` ("Back", to `options`).
- `#screen-picker` as today, under the kicker "Screensaver".

**`meeting`** (main screen of meeting mode)
- `#meeting-message` for a refusal or error (the idle status card keeps `#message`).
- Controls row (`#meeting-actions`): Mute / Unmute, Turn camera off / on, Leave (with the existing confirm step: the button becomes "Leave?" for a few seconds).
- Sound row: `#sound-note`, `#sound-panel` with `#sound-device`, `#volume-slider`, `#quieter`, `#volume-level`, `#louder`, `#speaker-mute` (unchanged ids).
- Sub-screen row: `#open-camera-live` ("Camera") and `#open-upcoming` ("Upcoming meetings"). The TV project adds "TV".

**`upcoming`**
- `#back-from-upcoming` ("Back", to `meeting`).
- `#upcoming-list`: today's bookings that have not started yet, each with its time and title, or `#upcoming-empty` ("No more meetings today").

**Keyboard** (`#keyboard`): unchanged, and only ever opened on the `idle` screen. While `body.keyboard-open`, the `.schedule` box is hidden so the link box stays above the keys.

### 4.2 Mode and navigation

- `mode()` is `"meeting"` when the status's `meeting.state` is one of the four in-progress states, else `"idle"`. `render()` compares it with the mode of the current view: on a change it sets the view to the new mode's main screen (`idle` or `meeting`) and clears any sub-screen timer. Loading and offline keep the current view and show the existing banner states (`body[data-state]`), with the controls disabled as today.
- Navigation buttons set the view; Back buttons return to the screen named in the table above. `camera` belongs to whichever mode showed it, so its Back goes to `options` or `meeting` accordingly.
- Idle return: in idle mode a timer starts whenever the view is not `idle`; any `pointerdown` on the page restarts it; after ten minutes it sets the view to `idle`. This replaces the Camera panel's own ten-minute close; the preview's background renewal continues to count as nobody's action.
- The preview: `previewWanted()` is true when the view is `camera`, the mode is idle, the camera is available and no meeting is in progress; leaving the view, a mode change or the idle return all send `preview off` once (through the existing background path when the page, not a person, leaves the screen). The 30 s renewal is unchanged.
- `#next-warning` and the upcoming list use the events the page already polls every 30 s; "within ten minutes" is measured against the page's clock.

### 4.3 Location

`Config` normalises `room.location` when it is built: the value is stripped, and a value that is empty or starts with `REPLACE_WITH_` becomes `""`. The page and the door sign hide `#room-location` when it is empty (the dashboard already sends what the config holds). The three room templates in `deploy/rooms/` get a comment: set `location` to what people call the place, or delete the line.

### 4.4 Kiosk

`installer/install-kiosk.sh` adds `--force-device-scale-factor=1.5` to the Chromium command it writes, with a comment naming the 1920 by 1200 panel and the 1280 by 800 layout it produces.

### 4.5 Files

- `src/croom/control/static/index.html`: the six `view` sections; three script tags in order: `app.js`, `devices.js`, `keyboard.js`.
- `src/croom/control/static/app.js`: the model, the polls, `mode()`, the view switch, the idle return timer, the idle and meeting screens, bookings and the upcoming list, the screensaver picker. It publishes `window.croomPage = { model, el, api, post, render, onRender, view, setView, mode, IN_MEETING }` for the other two files, where `onRender(fn)` registers a function `render()` calls after its own work.
- `src/croom/control/static/devices.js`: today's Sound and Camera panel code and the preview, moved out of `app.js` with as few changes as the view switch needs (`previewWanted`, the Back targets, the hidden Set up in meeting mode). It registers its rendering through `onRender`.
- `src/croom/control/static/keyboard.js`: today's keyboard IIFE, moved; it refuses to open unless the view is `idle`.
- `src/croom/control/static/style.css`: the view rules, the per-screen grids for 1280 by 800, the two-column camera layout, the large navigation buttons (at least 64 px tall), `.keyboard-open .schedule { display: none }`.
- `src/croom/core/config.py`: the location normalisation. `installer/install-kiosk.sh`: the scale flag. `deploy/rooms/room-{1,2,3}.yaml`: the comment.
- README (pieces row, decisions) and `docs/guides/crystal-meet-room-setup/` (the two modes, Options, Screensaver, the location line; rebuilt PDF).

## 5. Testing

Page tests in real Chromium at 1280 by 800 through `PageServer` and the existing stubs (`tests/unit/control/test_page.py`, with the Sound/Camera tests moved to a `test_page_devices.py` if the file grows past what one reader holds):
- Every screen fits: for each view, `document.documentElement.scrollHeight <= clientHeight` at 1280 by 800, including the camera screen with the Set up flow open and the idle screen with eight bookings (the box scrolls, the page does not).
- Navigation: Options, Camera framing, Back, Screensaver, Back; the camera's Back goes to `options` in idle and to `meeting` in a meeting.
- Mode switch: the stub meeting joins, the page lands on `meeting` from wherever it was; it leaves, the page lands on `idle`; a sub-screen open at the switch is dropped.
- Preview: on when the camera screen is shown in idle, off on Back, off on the idle return, never requested in a meeting; the stub records the sequence.
- Idle return: on a stopped clock, ten minutes on Options returns to idle; a tap at nine minutes restarts the wait.
- Meeting screen: Mute, Turn camera off, Leave with its confirm; the volume row drives the stub; the Camera sub-screen hides Save and Set up; Upcoming lists only bookings that have not started and says when there are none; the ten-minute warning appears in the top bar.
- Keyboard: opens on idle only, hides the bookings while open; the existing typing, layer and Join tests are kept.
- Location: `tests/unit/core/test_config.py` pins the normalisation (placeholder, blanks, a real value); a page test shows the top bar without the location line; the door sign test the same.
- Kiosk: `tests/unit/installer/test_install_kiosk_script.py` pins the scale flag.
- The existing hold, zoom, preset, refusal and auto-close tests are kept and re-pointed at the screens; the TV page tests are untouched.

## 6. Acceptance on PiMeet-2 and PiMeet-3

- The kiosk shows the idle screen with buttons about a finger tall and nothing to scroll; Options, Camera framing (the TV shows the camera), Back, Screensaver, Back.
- Join a meeting: the meeting screen appears on its own; Mute and Turn camera off work; Camera recalls Table during the call; Upcoming shows the next booking; Leave asks once and returns to idle.
- Leave the room on Camera framing: ten minutes later the idle screen is back and the TV is on the screensaver.
- A room whose config has no location shows its name alone.
