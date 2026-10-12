# The room page in two modes: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The table screen shows an idle screen (join, bookings, an Options button) or a meeting screen (the call's controls) depending on the room's state, with sub-screens behind Back instead of one scrolling column, sized for the 10.1-inch kiosk display.

**Architecture:** One page, one status poll, one model. Every screen is a `<section class="view" data-view="NAME">` in `index.html`; `data-view` on `<body>` names the visible one. `app.js` owns the model, the polls, the mode (`idle` or `meeting`, from the agent's meeting state), the view switch and the idle and meeting screens; `devices.js` (moved out of `app.js`) owns the Sound and Camera controls and the TV preview; `keyboard.js` (moved out) owns the on-screen keyboard. The three scripts share `window.croomPage`.

**Tech Stack:** Python 3.12 aiohttp control service (unchanged), plain HTML/CSS/JS pages, pytest with Playwright (sync API, real Chromium) for the page tests.

**Spec:** `docs/superpowers/specs/2026-10-12-room-page-modes-design.md`

## Global Constraints

- Internal names stay `croom`; what people see says Crystal Meet.
- Branch `room-page-modes`, cut from `main` after PR #3 merged (`git log` shows `d324529 Merge pull request #3`).
- Every screen fits a 1280 by 800 viewport with no page scrolling: `document.documentElement.scrollHeight <= document.documentElement.clientHeight`. Only `.events` (the bookings box) and `#upcoming-list` scroll, inside their own boxes. Below 1024 px wide the page may scroll.
- Meeting mode is exactly the four states `joining`, `in_lobby`, `connected`, `leaving` (`IN_MEETING` in `app.js`, `IN_PROGRESS_STATES` in the service); idle mode is every other state.
- Names people see: Options, Camera framing, Screensaver, Camera, Upcoming meetings, Back. Nothing says "TV when idle" any more.
- Existing element ids are kept: `#room-name`, `#room-location`, `#clock`, `#kicker`, `#headline`, `#detail`, `#message`, `#actions`, `#events`, `#calendar-note`, `#link-form`, `#link-input`, `#keyboard-toggle`, `#keyboard`, `#screen-picker`, `#sound-note`, `#sound-panel`, `#sound-device`, `#volume-slider`, `#quieter`, `#volume-level`, `#louder`, `#speaker-mute`, `#camera-note`, `#camera-panel`, `#camera-status`, `#arrow-pad`, `#camera-home`, `#zoom-out`, `#zoom-in`, `#preset-row`, `#preset-save-mode`, `#setup-link`, `#camera-setup`, `#find-stops`, `#save-home`. New ids: `#open-options`, `#meeting-state`, `#meeting-elapsed`, `#next-warning`, `#back-from-options`, `#open-camera-setup`, `#open-screensaver`, `#back-from-camera`, `#back-from-screensaver`, `#meeting-message`, `#meeting-actions`, `#open-camera-live`, `#open-upcoming`, `#back-from-upcoming`, `#upcoming-list`, `#upcoming-empty`.
- The kiosk launcher starts Chromium with `--force-device-scale-factor=1.5`.
- Ten minutes: `IDLE_RETURN_MS = 10 * 60 * 1000` (idle mode returns to the idle screen); 30 s preview renewal; the next-meeting warning shows when the next booking starts within `10 * 60 * 1000` ms.
- Tests: `.venv/bin/pytest -q -p no:cacheprovider <path>`; page tests take a few seconds each. Before the final commit: `bash /tmp/claude-1000/-home-cpm-ssh/f78b4b20-e6f0-4204-894b-193a8d8a2549/scratchpad/suite-gate.sh room-page-modes | grep "GATE:"` (81 upstream failures are the baseline; the gate script lives in the session scratchpad).
- Commit messages are plain, no attribution lines.

## Review Focus

1. The meeting ends while a finger holds an arrow on the meeting's Camera screen: the page switches to idle and the pad disappears under the finger; the camera must still get its stop. (Pinned in Task 4.)
2. A calendar join fails while someone is on Options: the "Couldn't join" card lives on the idle screen, so the page must land on idle when the state turns to `error`, or the error hides behind Options for ten minutes. (Pinned in Task 4.)
3. The agent goes offline while the Camera framing screen is shown: Back must still work and the preview must be released, so the TV is not left on the camera by a page that cannot reach the agent. (Pinned in Task 5.)
4. A day with eight bookings: the bookings box scrolls, the idle screen does not; the Join row and the status card stay in place. (Pinned in Task 3.)
5. The keyboard is open when a meeting starts (a join from the link box): the page switches to the meeting screen and the keyboard must close with it, or it covers the meeting controls. (Pinned in Task 5.)

---

### Task 1: A placeholder location counts as none

**Files:**
- Modify: `src/croom/core/config.py` (the `RoomConfig` dataclass, lines 37-42)
- Modify: `src/croom/control/static/app.js` (line 168, `el("room-location").textContent = s.room.location;`)
- Modify: `src/croom/control/static/sign.js` (line 65, the same line)
- Modify: `src/croom/control/static/style.css` (after line 78, `.location`)
- Modify: `deploy/rooms/room-1.yaml`, `deploy/rooms/room-2.yaml`, `deploy/rooms/room-3.yaml` (line 8, `location: "REPLACE_WITH_LOCATION"`)
- Modify: `tests/unit/core/test_config.py`, `tests/unit/control/test_page.py` (`PageServer` and two tests)

**Interfaces:**
- Produces: `croom.core.config.without_placeholder(value) -> str`; `RoomConfig.__post_init__` applies it to `location`; `PageServer(room_location="2nd floor")` keyword in the page tests.

- [ ] **Step 1: Write the failing config tests**

Append to `tests/unit/core/test_config.py` (it already imports `Config` and `RoomConfig` from `croom.core.config`; add `without_placeholder` to that import):

```python


class TestRoomLocationPlaceholder:
    """The room templates ship location: "REPLACE_WITH_LOCATION"; a room that never set it shows its name alone."""

    def test_the_installer_placeholder_counts_as_no_location(self):
        assert RoomConfig(location="REPLACE_WITH_LOCATION").location == ""

    def test_blanks_count_as_no_location(self):
        assert RoomConfig(location="   ").location == ""

    def test_a_real_location_is_kept_and_trimmed(self):
        assert RoomConfig(location=" 2nd floor ").location == "2nd floor"

    def test_from_dict_and_to_dict_carry_the_blank(self):
        config = Config.from_dict({"room": {"name": "Room 1", "location": "REPLACE_WITH_LOCATION"}})
        assert config.room.location == ""
        assert config.to_dict()["room"]["location"] == ""

    def test_without_placeholder_handles_none_and_numbers(self):
        assert without_placeholder(None) == ""
        assert without_placeholder(3) == "3"
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/core/test_config.py -k Placeholder`
Expected: FAIL (`ImportError: cannot import name 'without_placeholder'`).

- [ ] **Step 3: Normalise the location in the config**

In `src/croom/core/config.py`, replace the `RoomConfig` dataclass (lines 37-42) with:

```python
PLACEHOLDER_PREFIX = "REPLACE_WITH_"


def without_placeholder(value: Any) -> str:
    """A config text, with blanks and the installer's REPLACE_WITH_... placeholder treated as unset."""
    text = str(value if value is not None else "").strip()
    return "" if text.startswith(PLACEHOLDER_PREFIX) else text


@dataclass
class RoomConfig:
    """Room-specific configuration."""
    name: str = "Conference Room"
    location: str = ""   # what people call the place; "" shows the room's name alone
    timezone: str = "UTC"

    def __post_init__(self):
        self.location = without_placeholder(self.location)
```

`Any` is already imported in that module (`from typing import Any, Dict, ...`); check the import line and add `Any` if it is missing.

- [ ] **Step 4: Run the config tests**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/core/test_config.py`
Expected: PASS.

- [ ] **Step 5: Write the failing page tests**

In `tests/unit/control/test_page.py`, give `PageServer` a `room_location` keyword: change its `__init__` signature to

```python
    def __init__(self, calendar_events=(), room_name="Lab", room_location="2nd floor", calendar_connected=True, port=0, devices=None):
```

(keep whatever other keywords it already has, in their current order, and add `room_location` after `room_name`), store it with `self.room_location = room_location`, and in `_main` replace the literal `"room_location": "2nd floor"` with `"room_location": self.room_location`. Then add, after `test_door_sign_hides_the_schedule_without_a_calendar`:

```python
def test_the_location_line_disappears_when_the_room_has_none(browser):
    with PageServer(room_location="") as server:
        page = open_page(browser, server)
        assert page.locator("#room-location").is_hidden()
        assert page.locator("#room-name").inner_text() == "Lab"
        page.close()
    with PageServer() as server:
        page = open_page(browser, server)
        assert page.locator("#room-location").is_visible() and page.locator("#room-location").inner_text() == "2nd floor"
        page.close()


def test_the_door_sign_hides_the_location_line_when_the_room_has_none(browser):
    with PageServer(room_location="") as server:
        page = browser.new_page(viewport={"width": 1024, "height": 600})
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.locator("#room-location").is_hidden()
        page.close()
```

- [ ] **Step 6: Run them to see them fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_page.py -k "location_line"`
Expected: FAIL (`is_hidden()` is False: the empty paragraph is still rendered).

- [ ] **Step 7: Hide the empty line on both pages**

In `src/croom/control/static/app.js` replace line 168 with:

```js
    el("room-location").textContent = s.room.location;
    el("room-location").hidden = !s.room.location;
```

In `src/croom/control/static/sign.js` replace line 65 with the same two lines. In `src/croom/control/static/style.css` add after the `.location` rule (line 78):

```css
.location[hidden] { display: none; }
```

In each of `deploy/rooms/room-1.yaml`, `room-2.yaml`, `room-3.yaml`, put this comment on the line above `location: "REPLACE_WITH_LOCATION"`:

```yaml
  # What people call the place, e.g. "2nd floor, by the kitchen". Leave the placeholder or delete the line to show the room's name alone.
```

- [ ] **Step 8: Run the page tests and the config tests**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_page.py -k "location_line or door_sign" tests/unit/core/test_config.py`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/croom/core/config.py src/croom/control/static/app.js src/croom/control/static/sign.js src/croom/control/static/style.css deploy/rooms tests/unit/core/test_config.py tests/unit/control/test_page.py
git commit -m "fix(config): a placeholder location counts as none, and the pages hide the empty line"
```

---

### Task 2: Split the page script into app, devices and keyboard

**Files:**
- Modify: `src/croom/control/static/app.js` (the keyboard IIFE, lines 322-422; the Sound/Camera/preview code, lines 424-634; the start-up lines at the end)
- Create: `src/croom/control/static/devices.js`, `src/croom/control/static/keyboard.js`
- Modify: `src/croom/control/static/index.html` (the script tag, line 106)
- Test: the existing `tests/unit/control/test_page.py` (unchanged: this task changes no behaviour)

**Interfaces:**
- Produces: `window.croomPage = { el, model, api, post, act, button, render, refreshStatus, deviceEpoch, IN_MEETING, onRender }` published by `app.js` before the other two scripts load. `onRender(fn)` registers a function that `render()` calls, after its own work, with the status object (`fn(status)`), only when the page is not offline. `devices.js` and `keyboard.js` read the namespace as `const P = window.croomPage;`.
- Consumes: nothing new.

- [ ] **Step 1: Run the page tests once to record the baseline**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_page.py`
Expected: PASS (all of them; note the count).

- [ ] **Step 2: Create `keyboard.js`**

Create `src/croom/control/static/keyboard.js` with this skeleton, and move the body of the keyboard IIFE from `app.js` (everything between `const keyboard = (function () {` and its closing `})();`, lines 325-422: from `const LETTERS = ...` through `return { show: show, hide: hide };`) into it verbatim where the comment says:

```js
// The page's own keyboard, for screens without one (spec 2026-10-08, the kiosk package): the table Pi's kiosk browser
// opens the page with ?keyboard=1 and the keyboard appears when the link field is tapped; the button beside the field
// toggles it on any device. Keys never take the focus from the field.
(function () {
  "use strict";
  const P = window.croomPage;
  const el = P.el;

  // --- moved verbatim from app.js: from `const LETTERS = ...` to `return { show: show, hide: hide };` ---
})();
```

Delete the moved block and the comment above it from `app.js` (lines 322-422, including the `const keyboard = (function () {` line and its `})();`). Nothing in `app.js` used the `keyboard` variable.

- [ ] **Step 3: Create `devices.js`**

Create `src/croom/control/static/devices.js` with this skeleton, moving the Sound, Camera, hold-to-move and preview code from `app.js` (lines 424-634: from the `// --- Sound and Camera: the commands behind both panels` comment through the `window.addEventListener("pagehide", ...)` block) verbatim where the comment says:

```js
// Sound and Camera: the controls behind both panels and the TV preview (spec 2026-10-08, section 4.6).
(function () {
  "use strict";
  const P = window.croomPage;
  const el = P.el;
  const model = P.model;
  const post = P.post;
  const render = P.render;
  const button = P.button;
  const refreshStatus = P.refreshStatus;
  const deviceEpoch = P.deviceEpoch;
  const IN_MEETING = P.IN_MEETING;

  const SLIDE_OFF_PX = 16;   // how far outside a pressed pad button a finger may drift before that counts as letting go
  const PANEL_IDLE_MS = 10 * 60 * 1000;   // the Camera panel closes itself after this long without a camera action
  let lastSoundKey = null;
  let lastCameraKey = null;
  let presetSaveMode = false;
  let setupOpen = false;

  // --- moved verbatim from app.js: from the "Sound and Camera: the commands behind both panels" comment through
  //     the window.addEventListener("pagehide", ...) block ---

  P.onRender((s) => {
    renderSound(s.audio);
    renderCamera(s.camera);
  });
})();
```

Then in `app.js`:
- delete the moved block (lines 424-634);
- delete the now-unused declarations `SLIDE_OFF_PX`, `PANEL_IDLE_MS`, `lastSoundKey`, `lastCameraKey`, `presetSaveMode`, `setupOpen` (lines 8-9 and 19-22);
- in `render()`, delete the two lines `renderSound(s.audio);` and `renderCamera(s.camera);` and put in their place:

```js
    for (const fn of renderHooks) fn(s);
```

- right after `const model = { ... };` (line 13) add:

```js
  const renderHooks = [];   // devices.js registers its rendering here; render() calls each with the status
```

- replace the start-up lines at the end of the file (`tick(); setInterval(tick, 1000); refreshStatus(); refreshEvents(); setInterval(refreshStatus, ...); setInterval(refreshEvents, ...);`) with:

```js
  window.croomPage = {
    el: el, model: model, api: api, post: post, act: act, button: button, render: render,
    refreshStatus: refreshStatus, deviceEpoch: deviceEpoch, IN_MEETING: IN_MEETING,
    onRender: (fn) => renderHooks.push(fn),
  };

  tick();
  setInterval(tick, 1000);
  refreshStatus();
  refreshEvents();
  setInterval(refreshStatus, STATUS_EVERY_MS);
  setInterval(refreshEvents, EVENTS_EVERY_MS);
```

The namespace is published before the first poll, and the other two scripts run (and register) before any poll answers, because a script tag runs to completion before a fetch resolves.

- [ ] **Step 4: Load the three scripts**

In `src/croom/control/static/index.html` replace line 106 (`<script src="/static/app.js"></script>`) with:

```html
<script src="/static/app.js"></script>
<script src="/static/devices.js"></script>
<script src="/static/keyboard.js"></script>
```

- [ ] **Step 5: Check the scripts parse and the page tests still pass**

Run: `node --check src/croom/control/static/app.js && node --check src/croom/control/static/devices.js && node --check src/croom/control/static/keyboard.js`
Expected: no output.

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_page.py tests/unit/control/test_service.py`
Expected: PASS with the same count as Step 1. (`test_static_files_are_package_data` in `test_service.py` checks `control/static/*` is package data, so the new files ship with the wheel.)

- [ ] **Step 6: Commit**

```bash
git add src/croom/control/static/app.js src/croom/control/static/devices.js src/croom/control/static/keyboard.js src/croom/control/static/index.html
git commit -m "refactor(control): the room page's devices and keyboard code in their own files"
```

---

### Task 3: The screens, the view switch and the kiosk scale

**Files:**
- Modify: `src/croom/control/static/index.html` (rewrite the body)
- Modify: `src/croom/control/static/style.css` (replace lines 48-73, the `.screen` grid, and lines 240-242, the `<details>` rules; add the screen rules)
- Modify: `src/croom/control/static/app.js` (the view switch, navigation, meeting actions)
- Modify: `src/croom/control/static/devices.js` (`previewWanted` and the preview's open/close, because `#camera-panel` is no longer a `<details>`)
- Modify: `installer/install-kiosk.sh` (the `exec` line, ~line 76), `tests/unit/installer/test_install_kiosk_script.py`
- Modify: `tests/unit/control/test_page.py` (helpers, every `page.click("#camera-panel summary")`, the picker, sound and panel tests; new navigation and fit tests)
- Modify: `docs/superpowers/specs/2026-10-12-room-page-modes-design.md` (section 4.5 says "seven" view sections; there are six)

**Interfaces:**
- Produces, in `window.croomPage`: `view() -> string` (the current `data-view`), `setView(name, automatic=false)`, `mode() -> "idle" | "meeting"`, `cameraMode() -> "idle" | "meeting"` (which mode showed the `camera` view), `onViewChange(fn)` where `fn(name, automatic)` runs after the view changes. Views: `idle`, `options`, `camera`, `screensaver`, `meeting`, `upcoming`.
- Produces: in `app.js`, meeting-state actions render into `#meeting-actions` (Cancel while joining, Mute / Turn camera off / Leave while connected) and `#meeting-message` shows `model.error` in meeting mode. The idle status card keeps its own `#actions` and `#message`.
- Produces, in the tests: `open_camera_screen(page)` (Options then Camera framing, in idle mode), `open_screensaver_screen(page)`, `PANEL_OPEN = "document.body.dataset.view === 'camera'"`, `fits(page) -> bool`.
- Consumes: Task 2's namespace and hooks.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/control/test_page.py`:

(a) Add these helpers right after `join_by_id`:

```python
def open_camera_screen(page):
    """Idle mode: Options, then Camera framing. Waits until the camera view is up."""
    page.click("#open-options")
    page.click("#open-camera-setup")
    page.wait_for_function("document.body.dataset.view === 'camera'", timeout=5000)


def open_screensaver_screen(page):
    page.click("#open-options")
    page.click("#open-screensaver")
    page.wait_for_function("document.body.dataset.view === 'screensaver'", timeout=5000)


def fits(page):
    """True when nothing on the page needs the page itself to scroll (the bookings box may scroll inside)."""
    return page.evaluate("document.documentElement.scrollHeight <= document.documentElement.clientHeight")
```

(b) Replace every `page.click("#camera-panel summary")` with `open_camera_screen(page)`:

```bash
sed -i 's/page.click("#camera-panel summary")/open_camera_screen(page)/' tests/unit/control/test_page.py
```

and change `PANEL_OPEN` (line ~889) to:

```python
PANEL_OPEN = "document.body.dataset.view === 'camera'"
```

(c) The screensaver picker test (`test_the_screen_picker_posts_the_style_and_marks_the_current_one`): after `page = open_page(browser, server)` add `open_screensaver_screen(page)`.

(d) Sound lives on the meeting screen now. In `test_sound_panel_shows_the_speaker_and_changes_the_level` and `test_a_refused_volume_change_shows_its_reason_and_the_slider_goes_back`, add `join_by_id(page)` right after the page is opened (before the first sound assertion). In `test_panels_hide_with_a_note_when_the_devices_are_missing` (and the second test that asserts both notes, around line 750), open the camera screen before the camera assertion and join before the sound assertion:

```python
        open_camera_screen(page)
        assert page.locator("#camera-panel").is_hidden() and page.locator("#camera-note").inner_text() == "No controllable camera found"
        page.click("#back-from-camera")
        page.click("#back-from-options")
        join_by_id(page)
        assert page.locator("#sound-panel").is_hidden() and page.locator("#sound-note").inner_text() == "No speaker found"
```

(e) The join flow (`test_join_by_id_mute_and_leave_flow`): the meeting's buttons are in `#meeting-actions` now; replace `#actions button:has-text('Mute')`, `('Unmute')`, `('Leave')`, `('Tap again to leave')` with `#meeting-actions button:has-text(...)`. Keep `assert page.locator(".link").is_hidden()` (the idle view is hidden in a meeting).

(f) `test_a_refusal_comes_into_view_beside_the_controls_that_caused_it` asserted the old panel scrolled; replace its body's last assertions with: the note is inside the viewport on the camera screen:

```python
        box = page.locator("#camera-note").bounding_box()
        assert box["y"] >= 0 and box["y"] + box["height"] <= 800
```

(remove the `bounding_box()["y"] < 0` assertion on the summary).

(g) Add the new tests after `test_the_panels_fit_a_phone_width`:

```python
def test_options_leads_to_camera_framing_and_screensaver_and_back(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        assert page.evaluate("document.body.dataset.view") == "idle"
        assert page.locator("#open-options").is_visible()
        page.click("#open-options")
        assert page.evaluate("document.body.dataset.view") == "options"
        assert page.locator("#link-form").is_hidden()                       # the idle screen is gone
        page.click("#open-camera-setup")
        assert page.evaluate("document.body.dataset.view") == "camera"
        assert page.locator("#arrow-pad").is_visible()
        page.click("#back-from-camera")
        assert page.evaluate("document.body.dataset.view") == "options"
        page.click("#open-screensaver")
        assert page.evaluate("document.body.dataset.view") == "screensaver"
        assert page.locator("#screen-picker button").count() == 4
        page.click("#back-from-screensaver")
        page.click("#back-from-options")
        assert page.evaluate("document.body.dataset.view") == "idle" and page.locator("#link-form").is_visible()
        page.close()


def test_the_page_switches_to_the_meeting_screen_and_back_on_its_own(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.click("#open-options")                                          # on a sub-screen when the meeting starts
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/98765432100"}',
                          headers={"Content-Type": "application/json"})      # a join from another screen, or the calendar
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=8000)
        assert page.locator("#meeting-actions button:has-text('Mute')").is_visible()
        assert page.locator("#open-options").is_hidden()
        page.click("#open-camera-live")
        assert page.evaluate("document.body.dataset.view") == "camera"
        page.click("#back-from-camera")
        assert page.evaluate("document.body.dataset.view") == "meeting"      # Back from the camera returns to the mode's screen
        page.click("#meeting-actions button:has-text('Leave')")
        page.click("#meeting-actions button:has-text('Tap again to leave')")
        page.wait_for_function("document.body.dataset.view === 'idle'", timeout=5000)
        assert page.locator("#link-form").is_visible() and page.locator("#open-options").is_visible()
        page.close()


def test_every_idle_screen_fits_the_kiosk_without_scrolling(browser):
    bookings = [event(f"e{i}", f"Booking {i}", 20 + 30 * i) for i in range(8)]
    with PageServer(calendar_events=bookings) as server:
        page = open_page(browser, server)
        page.wait_for_function("document.querySelectorAll('#events .event').length === 8", timeout=5000)
        assert fits(page), "the idle screen scrolls with eight bookings"
        assert page.evaluate("const b = document.querySelector('#events'); b.scrollHeight > b.clientHeight")   # the box scrolls instead
        assert page.locator("#link-form").is_visible()
        page.click("#open-options")
        assert fits(page), "the options screen scrolls"
        page.click("#open-camera-setup")
        page.click("#setup-link") if page.locator("#setup-link").is_visible() else None
        assert fits(page), "the camera screen scrolls"
        page.click("#back-from-camera")
        page.click("#open-screensaver")
        assert fits(page), "the screensaver screen scrolls"
        page.close()
```

(h) The kiosk flag, in `tests/unit/installer/test_install_kiosk_script.py`, add to `test_kiosk_command_opens_the_room_page_full_screen_with_the_keyboard_flag` after the `--kiosk` assertion:

```python
    assert "--force-device-scale-factor=1.5" in text   # the 1920x1200 touch panel lays the page out at 1280x800
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_page.py -k "options_leads or switches_to_the_meeting or fits_the_kiosk" tests/unit/installer/test_install_kiosk_script.py`
Expected: FAIL (no `#open-options`, no scale flag).

- [ ] **Step 3: Rewrite the page's body**

Replace the whole `<body>` of `src/croom/control/static/index.html` (keep the `<head>`) with:

```html
<body data-state="loading" data-view="idle">
<header class="top">
  <div class="brand">
    <img class="logo" src="/static/crystalpm-logo-white.svg" alt="Crystal PM">
    <div>
      <h1 id="room-name">Room</h1>
      <p id="room-location" class="location" hidden></p>
    </div>
  </div>
  <div class="top-right">
    <p id="next-warning" class="next-warning" hidden></p>
    <p id="meeting-state" class="meeting-state" hidden></p>
    <p id="meeting-elapsed" class="meeting-elapsed" hidden></p>
    <p id="clock" class="clock"></p>
    <button type="button" id="open-options" class="button quiet nav">Options</button>
  </div>
</header>
<main class="screen">
  <section class="view" data-view="idle">
    <section class="now status-card" aria-live="polite">
      <p id="kicker" class="kicker">Connecting</p>
      <p id="headline" class="headline">Connecting to the room</p>
      <p id="detail" class="detail"></p>
      <p id="message" class="message" role="status"></p>
      <div id="actions" class="actions"></div>
    </section>
    <section class="link">
      <p class="kicker">Join with a link</p>
      <form id="link-form" class="link-form">
        <label class="visually-hidden" for="link-input">Meeting link or Zoom meeting ID</label>
        <input id="link-input" type="text" inputmode="url" autocomplete="off" spellcheck="false"
               placeholder="Paste a meeting link or a Zoom meeting ID">
        <button type="button" id="keyboard-toggle" class="button quiet keyboard-toggle" aria-label="Show keyboard" aria-pressed="false">&#9000;</button>
        <button type="submit" class="button primary">Join</button>
      </form>
    </section>
    <section class="schedule">
      <p class="kicker">Today</p>
      <ul id="events" class="events"></ul>
      <p id="calendar-note" class="note"></p>
    </section>
  </section>

  <section class="view" data-view="options">
    <button type="button" id="back-from-options" class="button quiet back">Back</button>
    <div class="menu">
      <button type="button" id="open-camera-setup" class="button quiet menu-item">Camera framing</button>
      <button type="button" id="open-screensaver" class="button quiet menu-item">Screensaver</button>
    </div>
  </section>

  <section class="view" data-view="camera">
    <button type="button" id="back-from-camera" class="button quiet back">Back</button>
    <section class="camera-section">
      <p class="kicker">Camera</p>
      <p id="camera-note" class="note"></p>
      <div id="camera-panel" class="camera" hidden>
        <p id="camera-status" class="note"></p>
        <div class="camera-grid">
          <div class="camera-left">
            <div id="arrow-pad" class="arrow-pad" role="group" aria-label="Pan and tilt">
              <span></span>
              <button type="button" class="button quiet pad" data-pan="0" data-tilt="1" aria-label="Tilt up">&#9650;</button>
              <span></span>
              <button type="button" class="button quiet pad" data-pan="-1" data-tilt="0" aria-label="Pan left">&#9664;</button>
              <button type="button" id="camera-home" class="button quiet pad" aria-label="Home">&#8962;</button>
              <button type="button" class="button quiet pad" data-pan="1" data-tilt="0" aria-label="Pan right">&#9654;</button>
              <span></span>
              <button type="button" class="button quiet pad" data-pan="0" data-tilt="-1" aria-label="Tilt down">&#9660;</button>
              <span></span>
            </div>
            <div class="zoom-buttons">
              <button type="button" id="zoom-out" class="button quiet" data-zoom="-25">Zoom out</button>
              <button type="button" id="zoom-in" class="button quiet" data-zoom="25">Zoom in</button>
            </div>
          </div>
          <div class="camera-right">
            <div id="preset-row" class="presets" hidden></div>
            <button type="button" id="preset-save-mode" class="button quiet" aria-pressed="false" hidden>Save</button>
            <button type="button" id="setup-link" class="link-button" hidden>Set up</button>
            <div id="camera-setup" class="setup" hidden>
              <p class="note">Set up once: find the camera's end stops, nudge it to the view you want, then save that as Home.</p>
              <div class="setup-buttons">
                <button type="button" id="find-stops" class="button quiet">Find the stops</button>
                <button type="button" id="save-home" class="button primary" disabled>Save as Home</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  </section>

  <section class="view" data-view="screensaver">
    <button type="button" id="back-from-screensaver" class="button quiet back">Back</button>
    <section class="screen-section">
      <p class="kicker">Screensaver</p>
      <div id="screen-picker" class="picker" role="group" aria-label="Screensaver style"></div>
    </section>
  </section>

  <section class="view" data-view="meeting">
    <p id="meeting-message" class="message" role="status"></p>
    <div id="meeting-actions" class="actions meeting-actions"></div>
    <section class="sound-section">
      <p class="kicker">Sound</p>
      <p id="sound-note" class="note"></p>
      <div id="sound-panel" class="sound" hidden>
        <p id="sound-device" class="device"></p>
        <label class="visually-hidden" for="volume-slider">Volume</label>
        <input id="volume-slider" type="range" min="0" max="100" step="1" value="0">
        <div class="sound-buttons">
          <button type="button" id="quieter" class="button quiet">Quieter</button>
          <span id="volume-level" class="level" aria-live="polite"></span>
          <button type="button" id="louder" class="button quiet">Louder</button>
          <button type="button" id="speaker-mute" class="button quiet" aria-pressed="false">Mute speaker</button>
        </div>
      </div>
    </section>
    <div class="menu">
      <button type="button" id="open-camera-live" class="button quiet menu-item">Camera</button>
      <button type="button" id="open-upcoming" class="button quiet menu-item">Upcoming meetings</button>
    </div>
  </section>

  <section class="view" data-view="upcoming">
    <button type="button" id="back-from-upcoming" class="button quiet back">Back</button>
    <section class="schedule">
      <p class="kicker">Upcoming meetings</p>
      <ul id="upcoming-list" class="events"></ul>
      <p id="upcoming-empty" class="note" hidden>No more meetings today</p>
    </section>
  </section>
</main>
<div id="keyboard" class="keyboard" role="group" aria-label="On-screen keyboard" hidden></div>
<script src="/static/app.js"></script>
<script src="/static/devices.js"></script>
<script src="/static/keyboard.js"></script>
</body>
```

- [ ] **Step 4: The screen styles**

In `src/croom/control/static/style.css`:

(a) Replace lines 48-73 (from `.screen {` through `.screen-section { grid-area: screen; }`) with:

```css
/* Screens (spec 2026-10-12): one view at a time, laid out for 1280 by 800 with no page scrolling. The top bar
   is shared; main holds every screen and shows the one named by body[data-view]. */
body { display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
.top { flex: 0 0 auto; padding: var(--gutter) var(--gutter) 0; display: flex; justify-content: space-between; align-items: flex-start; gap: 24px; }
.top-right { display: flex; align-items: center; gap: 20px; }
.screen { flex: 1 1 auto; min-height: 0; padding: clamp(16px, 2.5vh, 24px) var(--gutter) var(--gutter); }
.view { display: none; height: 100%; min-height: 0; gap: clamp(16px, 2.5vh, 24px); align-content: start; }
body[data-view="idle"] .view[data-view="idle"],
body[data-view="options"] .view[data-view="options"],
body[data-view="camera"] .view[data-view="camera"],
body[data-view="screensaver"] .view[data-view="screensaver"],
body[data-view="meeting"] .view[data-view="meeting"],
body[data-view="upcoming"] .view[data-view="upcoming"] { display: grid; }
.view[data-view="idle"] {
  grid-template-columns: 3fr 2fr;
  grid-template-rows: auto auto 1fr;
  grid-template-areas: "now schedule" "link schedule" ". schedule";
  column-gap: clamp(32px, 4vw, 72px);
}
.now { grid-area: now; }
.link { grid-area: link; }
.schedule { grid-area: schedule; min-height: 0; display: flex; flex-direction: column; }
.schedule .events { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding-right: 4px; }
.view[data-view="options"], .view[data-view="camera"], .view[data-view="screensaver"], .view[data-view="upcoming"] { grid-template-rows: auto 1fr; justify-items: start; }
.view[data-view="upcoming"] .schedule { width: 100%; }
.view[data-view="meeting"] { grid-template-rows: auto auto auto 1fr; }
.back { justify-self: start; min-height: 56px; padding: 0 22px; }
.nav { min-height: 56px; }
.menu { display: grid; grid-template-columns: repeat(2, minmax(260px, 420px)); gap: 24px; width: 100%; align-content: start; }
.menu-item { min-height: 160px; font-size: 1.4rem; }
.meeting-actions .button { min-height: 72px; font-size: 1.2rem; padding: 0 32px; }
.meeting-state { margin: 0; font-weight: 600; }
.meeting-elapsed { margin: 0; color: var(--periwinkle-300); font-variant-numeric: tabular-nums; }
.next-warning { margin: 0; padding: 8px 14px; border-radius: var(--radius-sm); background: var(--warn-fill); color: var(--ink-900); font-weight: 600; }
.meeting-state[hidden], .meeting-elapsed[hidden], .next-warning[hidden], .nav[hidden] { display: none; }
@media (max-width: 1023px) {
  /* A phone: one column, and the page may scroll. */
  body { height: auto; min-height: 100vh; overflow: auto; }
  .view { height: auto; }
  .view[data-view="idle"] { grid-template-columns: 1fr; grid-template-rows: auto; grid-template-areas: "now" "link" "schedule"; }
  .schedule .events { overflow: visible; }
  .menu { grid-template-columns: 1fr; }
}
```

(b) Make the top bar smaller so the screens get the height: change `--gutter` (line 32) to `clamp(16px, 2.5vw, 32px)`, `.logo` width (line 76) to `clamp(96px, 9vw, 120px)`, `.clock` font-size (line 82) to `clamp(1.8rem, 3.5vw, 3rem)`, `.headline` font-size (line 110) to `clamp(1.5rem, 3.4vw, 2.6rem)`, and `.status-card` padding (line 102) to `clamp(18px, 2.5vw, 28px)`.

(c) Replace the three `<details>` rules (lines 240-242: `.camera summary ...`, `.camera summary::-webkit-details-marker ...`, `.camera[open] summary ...`) with the two-column camera layout:

```css
.camera-grid { display: grid; grid-template-columns: auto 1fr; gap: 40px; align-items: start; margin-top: 14px; }
.camera-left { display: flex; gap: 24px; align-items: center; }
.camera-right { display: flex; flex-direction: column; align-items: flex-start; }
.camera-right .presets { margin-top: 0; }
```

and delete the old `.camera-grid { display: flex; ... }` rule that follows (line 243). Remove `.sound-section { grid-area: sound; }` and `.camera-section { grid-area: camera; }` (lines 233-234): the sections sit inside their views now.

- [ ] **Step 5: The view switch in `app.js`**

(a) After `const IN_MEETING = [...]` (line 10) add:

```js
  // Screens (spec 2026-10-12): body[data-view] names the one shown. Each view belongs to a mode; the camera view
  // belongs to whichever mode showed it, so its Back returns there.
  const VIEW_MODES = { idle: "idle", options: "idle", screensaver: "idle", meeting: "meeting", upcoming: "meeting" };
  const MAIN_SCREEN = { idle: "idle", meeting: "meeting" };
```

(b) After `const renderHooks = [];` add:

```js
  const viewHooks = [];     // devices.js and keyboard.js register here; called after every view change
  let cameraMode = "idle";  // the mode that opened the camera view

  const view = () => document.body.dataset.view;
  const mode = () => (model.status && IN_MEETING.includes(model.status.meeting.state) ? "meeting" : "idle");
  const viewMode = (name) => (name === "camera" ? cameraMode : VIEW_MODES[name]);

  function applyView(name, automatic) {
    if (name === "camera") cameraMode = mode();
    document.body.dataset.view = name;
    for (const fn of viewHooks) fn(name, Boolean(automatic));
  }
  function setView(name, automatic) {
    if (view() === name) return;
    applyView(name, automatic);
    render();
  }
```

(c) Generalise the keyed action rebuild. Replace `setActions(specs)` (lines 108-117) with:

```js
  function setActions(specs, containerId, keyHolder) {
    // Replace the buttons only when their labels or availability change, so a
    // click in progress is never lost to a re-render (the timer ticks every second).
    const key = JSON.stringify(specs.map((s) => [s.label, s.className, Boolean(s.disabled) || model.busy]));
    if (key === keyHolder.key) return;
    keyHolder.key = key;
    el(containerId).replaceChildren(...specs.map((s) => button(s.label, s.className, s.onClick, s.disabled)));
  }
  const idleActionsKey = { key: null };
  const meetingActionsKey = { key: null };
```

and delete `let lastActionsKey = null;` (line 15).

(d) In `render()`:
- the offline branch keeps hiding `.screen-section`, `.sound-section`, `.camera-section` and calls `setActions([], "actions", idleActionsKey);` (replace the bare `setActions([])`); add `setActions([], "meeting-actions", meetingActionsKey);` next to it;
- after `const s = model.status;` add the mode landing:

```js
    if (viewMode(view()) !== mode()) applyView(MAIN_SCREEN[mode()], true);   // a meeting began or ended: land on that mode's screen
    el("open-options").hidden = mode() !== "idle";
```

- replace `document.querySelector(".link").hidden = IN_MEETING.includes(m.state);` with `el("meeting-message").textContent = model.error;` (the idle view, and its link form, is simply not shown in meeting mode);
- the meeting branches keep setting `body.dataset.state`, `#kicker`, `#headline`, `#detail` (harmless, and `tick()` reads them) but their `specs` now go to the meeting screen: change the final `setActions(specs);` to

```js
    if (mode() === "meeting") {
      setActions(specs, "meeting-actions", meetingActionsKey);
      setActions([], "actions", idleActionsKey);
    } else {
      setActions(specs, "actions", idleActionsKey);
      setActions([], "meeting-actions", meetingActionsKey);
    }
```

(e) Navigation, after the `el("link-form").addEventListener("submit", ...)` block:

```js
  el("open-options").addEventListener("click", () => setView("options"));
  el("back-from-options").addEventListener("click", () => setView("idle"));
  el("open-camera-setup").addEventListener("click", () => setView("camera"));
  el("open-screensaver").addEventListener("click", () => setView("screensaver"));
  el("back-from-screensaver").addEventListener("click", () => setView("options"));
  el("back-from-camera").addEventListener("click", () => setView(cameraMode === "meeting" ? "meeting" : "options"));
  el("open-camera-live").addEventListener("click", () => setView("camera"));
  el("open-upcoming").addEventListener("click", () => setView("upcoming"));
  el("back-from-upcoming").addEventListener("click", () => setView("meeting"));
```

(f) Publish the new members: add to `window.croomPage`:

```js
    view: view, setView: setView, mode: mode, cameraMode: () => cameraMode,
    onViewChange: (fn) => viewHooks.push(fn),
```

- [ ] **Step 6: The camera panel is no longer a `<details>`**

In `src/croom/control/static/devices.js`:
- `renderCamera` keeps `panel.hidden = !camera.available` (the `#camera-panel` div) and everything else as it is;
- `previewWanted()` becomes

```js
  function previewWanted() {
    const s = model.status;
    return Boolean(P.view() === "camera" && P.cameraMode() === "idle" && s && s.camera && s.camera.available && !IN_MEETING.includes(s.meeting.state));
  }
```

- replace the whole `el("camera-panel").addEventListener("toggle", ...)` block and the `pagehide` block with a view hook (Task 5 adds the idle return; this keeps today's behaviour minus the panel's own timer):

```js
  let previewing = false;
  P.onViewChange((name, automatic) => {
    if (previewing) {
      clearInterval(previewTimer);
      previewTimer = null;
      previewing = false;
      setPreview(false, automatic ? { background: true } : undefined);   // the page left the screen on its own: nobody's action
    }
    if (name === "camera" && P.cameraMode() === "idle") {
      previewing = true;
      lastCameraAction = Date.now();
      if (previewWanted()) setPreview(true);
      previewTimer = setInterval(() => { if (previewWanted()) setPreview(true, { background: true }); }, 30000);
    }
  });
  window.addEventListener("pagehide", () => {
    if (!previewing) return;
    fetch("/api/camera/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ on: false }), keepalive: true }).catch(() => {});
  });
```

- delete `closingOnIdle` and `PANEL_IDLE_MS` (the idle return in Task 5 replaces them); keep `lastCameraAction` for Task 5.

- [ ] **Step 7: The kiosk scale**

In `installer/install-kiosk.sh`, in the `exec` command written to `crystal-meet-kiosk` (~line 76), add the flag after `--kiosk`:

```bash
exec "\$BROWSER" --kiosk --force-device-scale-factor=1.5 --noerrdialogs --disable-infobars --disable-session-crashed-bubble \\
```

and add above the `exec` line the comment `# 1.5x scale: the 1920x1200 touch panel lays the page out at 1280x800, buttons about 11 mm tall.`

In the spec, section 4.5, change "the seven `view` sections" to "the six `view` sections".

- [ ] **Step 8: Run the page, service and installer tests**

Run: `node --check src/croom/control/static/app.js && node --check src/croom/control/static/devices.js`
Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_page.py tests/unit/control/test_service.py tests/unit/installer/test_install_kiosk_script.py`
Expected: PASS. If a hold test fails because the pad is off-screen, the `press` helper scrolls it into view already; check the camera view is shown first. If `test_the_camera_panel_closes_itself_after_ten_minutes_without_a_camera_action` or `test_a_camera_action_keeps_the_camera_panel_open_another_ten_minutes` fail: they test the panel's own timer, which Task 5 replaces; mark them `@pytest.mark.skip(reason="replaced by the idle return in Task 5")` for this commit only.

- [ ] **Step 9: Commit**

```bash
git add src/croom/control/static installer/install-kiosk.sh tests/unit/control/test_page.py tests/unit/installer/test_install_kiosk_script.py docs/superpowers/specs/2026-10-12-room-page-modes-design.md
git commit -m "feat(control): the room page as screens: idle with Options, and a meeting screen"
```

---

### Task 4: The meeting screen's content

**Files:**
- Modify: `src/croom/control/static/app.js` (`render()`, `tick()`, new `renderMeetingBar`, `renderUpcoming`)
- Modify: `src/croom/control/static/devices.js` (`renderCamera` hides Save and Set up in meeting mode)
- Test: `tests/unit/control/test_page.py`

**Interfaces:**
- Consumes: `mode()`, `cameraMode()`, `view()`, `applyView` (inside `app.js`), `model.events`.
- Produces: `#meeting-state` texts `Joining`, `In the lobby`, `In a meeting`, `Leaving`; `#meeting-elapsed` as `m:ss` or `h:mm:ss` (the existing `elapsedSince`); `#next-warning` text `Next: <time> <title>`; `#upcoming-list` items (`li.event` with `time` and `.title`, like `#events`); the `error` state lands the page on `idle`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/control/test_page.py`:

```python
def test_the_meeting_screen_shows_the_state_the_time_and_what_is_next(browser):
    bookings = [event("e1", "Design review", 8), event("e2", "Sales call", 90)]   # the first starts in 8 minutes
    with PageServer(calendar_events=bookings) as server:
        page = open_page(browser, server)
        join_by_id(page)
        assert page.locator("#meeting-state").inner_text() == "In a meeting"
        page.wait_for_function("/^\\d+:\\d\\d$/.test(document.getElementById('meeting-elapsed').innerText)", timeout=5000)
        warning = page.locator("#next-warning")
        assert warning.is_visible() and warning.inner_text().startswith("Next: ") and "Design review" in warning.inner_text()
        assert page.locator("#open-options").is_hidden()
        page.click("#open-upcoming")
        assert page.evaluate("document.body.dataset.view") == "upcoming"
        titles = page.locator("#upcoming-list .title").all_inner_texts()
        assert titles == ["Design review", "Sales call"]
        assert page.locator("#upcoming-empty").is_hidden()
        page.click("#back-from-upcoming")
        assert page.evaluate("document.body.dataset.view") == "meeting"
        page.close()


def test_upcoming_says_so_when_nothing_else_is_booked(browser):
    with PageServer(calendar_events=[event("e0", "Earlier", -60)]) as server:   # already started: not upcoming
        page = open_page(browser, server)
        join_by_id(page)
        assert page.locator("#next-warning").is_hidden()
        page.click("#open-upcoming")
        assert page.locator("#upcoming-list .event").count() == 0 and page.locator("#upcoming-empty").is_visible()
        page.close()


def test_the_camera_during_a_meeting_recalls_and_nudges_but_never_sets_up(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        open_camera_screen(page)
        page.click("#find-stops")
        page.wait_for_function("!document.getElementById('save-home').disabled", timeout=5000)
        page.click("#save-home")
        page.wait_for_function("document.querySelectorAll('#preset-row button').length === 3", timeout=5000)
        page.click("#back-from-camera")
        page.click("#back-from-options")
        join_by_id(page)
        asked = preview_requests(page)
        page.click("#open-camera-live")
        assert page.locator("#preset-row button").count() == 3
        assert page.locator("#preset-save-mode").is_hidden() and page.locator("#setup-link").is_hidden() and page.locator("#camera-setup").is_hidden()
        page.click("#preset-row button[data-slot='1']")
        page.wait_for_function("document.body.dataset.view === 'camera'", timeout=5000)
        page.wait_for_timeout(300)
        assert asked == []                                     # no preview during a meeting
        hold(page, "#arrow-pad button[data-pan='1']")
        assert server.devices.camera.moves[-2:] == [(1, 0), (0, 0)]
        page.close()


def test_a_meeting_that_ends_under_a_held_arrow_still_stops_the_camera(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.click("#open-camera-live")
        press(page, "#arrow-pad button[data-pan='1']")
        page.wait_for_timeout(200)
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/leave", data="{}", headers={"Content-Type": "application/json"})
        page.wait_for_function("document.body.dataset.view === 'idle'", timeout=8000)
        page.wait_for_timeout(300)
        page.mouse.up()
        page.wait_for_timeout(300)
        assert server.devices.camera.moves[-1] == (0, 0)       # the pad vanished under the finger; the stop still went out
        page.close()


def test_a_failed_join_brings_the_page_back_to_the_idle_screen(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.click("#open-options")
        server.meeting.fail_next_join = True
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join", data='{"url": "https://zoom.us/j/1"}', headers={"Content-Type": "application/json"})
        page.wait_for_function("document.body.dataset.state === 'error'", timeout=8000)
        assert page.evaluate("document.body.dataset.view") == "idle"
        assert page.locator("#kicker").inner_text() == "Couldn't join"
        page.close()
```

`fail_next_join` on `StubMeeting`: if the stub has no such switch, add one in `tests/unit/control/test_service.py`: in `__init__` set `self.fail_next_join = False`, and at the top of `join_meeting` add

```python
        if self.fail_next_join:
            self.fail_next_join = False
            self.error = "Meet refused the room"
            self.set_state(MeetingState.ERROR)
            return False
```

(follow how the stub already reports an error state; `MeetingState.ERROR` exists in `croom.meeting.service`). The `event()` helper accepts a negative offset for a booking that already started.

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_page.py -k "meeting_screen_shows or upcoming_says or during_a_meeting_recalls or under_a_held_arrow or failed_join_brings"`
Expected: FAIL (`#meeting-state` empty, the upcoming list empty, Save visible in a meeting).

- [ ] **Step 3: The meeting bar, the upcoming list and the error landing**

In `src/croom/control/static/app.js`:

(a) Add after `joinWindow(ev)`:

```js
  const WARN_BEFORE_MS = 10 * 60 * 1000;
  const STATE_LABELS = { joining: "Joining", in_lobby: "In the lobby", connected: "In a meeting", leaving: "Leaving" };

  function upcoming() {
    const now = Date.now();
    return model.events.filter((ev) => new Date(ev.start_time).getTime() > now);
  }

  function renderMeetingBar(m) {
    const inMeeting = mode() === "meeting";
    el("meeting-state").hidden = !inMeeting;
    el("meeting-elapsed").hidden = !(inMeeting && m.state === "connected" && m.joined_at);
    el("meeting-state").textContent = STATE_LABELS[m.state] || "";
    el("meeting-elapsed").textContent = m.joined_at ? elapsedSince(m.joined_at) : "";
    const next = upcoming()[0];
    const soon = inMeeting && next && new Date(next.start_time).getTime() - Date.now() <= WARN_BEFORE_MS;
    el("next-warning").hidden = !soon;
    el("next-warning").textContent = soon ? "Next: " + fmtTime(new Date(next.start_time)) + " " + next.title : "";
  }

  let lastUpcomingKey = null;
  function renderUpcoming() {
    const items = upcoming();
    const key = JSON.stringify(items.map((ev) => [ev.id, ev.title, ev.start_time]));
    if (key === lastUpcomingKey) return;
    lastUpcomingKey = key;
    el("upcoming-empty").hidden = items.length > 0;
    el("upcoming-list").replaceChildren(...items.map((ev) => {
      const li = document.createElement("li");
      li.className = "event";
      const time = document.createElement("time");
      time.dateTime = ev.start_time;
      time.textContent = fmtTime(new Date(ev.start_time));
      const text = document.createElement("div");
      const title = document.createElement("p");
      title.className = "title";
      title.textContent = ev.title;
      text.append(title);
      if (ev.joinable) {
        const platform = document.createElement("p");
        platform.className = "platform";
        platform.textContent = platformName(ev.meeting_platform);
        text.append(platform);
      }
      li.append(time, text);
      return li;
    }));
  }
```

(b) In `render()`, right after the mode landing line from Task 3 (`if (viewMode(view()) !== mode()) ...`), add the error landing and the bar:

```js
    if (m.state !== lastMeetingState) {
      // A join that failed while someone was on Options: the "Couldn't join" card is on the idle screen.
      if (m.state === "error") applyView("idle", true);
      lastMeetingState = m.state;
    }
    renderMeetingBar(m);
    renderUpcoming();
```

Note `const m = s.meeting;` must come before these lines: move the `const m = s.meeting; const cal = s.calendar;` pair above the mode landing. Declare `let lastMeetingState = null;` next to `let cameraMode = "idle";`.

(c) In `tick()`, add `if (model.status && model.status.meeting.state === "connected") el("meeting-elapsed").textContent = elapsedSince(model.status.meeting.joined_at);` (keep the existing `#detail` line).

(d) In `src/croom/control/static/devices.js`, in `renderCamera`, replace the three lines

```js
    el("preset-save-mode").hidden = !ready;
    el("setup-link").hidden = !ready;
    el("camera-setup").hidden = ready && !setupOpen;
```

with

```js
    const setupAllowed = P.cameraMode() === "idle";   // during a meeting: recall and nudge only
    el("preset-save-mode").hidden = !ready || !setupAllowed;
    el("setup-link").hidden = !ready || !setupAllowed;
    el("camera-setup").hidden = !setupAllowed || (ready && !setupOpen);
```

and add `P.cameraMode()` to the `key` array at the top of `renderCamera`, so a mode change redraws the panel.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_page.py`
Expected: PASS (the two tests skipped in Task 3 stay skipped until Task 5).

- [ ] **Step 5: Commit**

```bash
git add src/croom/control/static/app.js src/croom/control/static/devices.js tests/unit/control/test_page.py tests/unit/control/test_service.py
git commit -m "feat(control): the meeting screen shows the state, the clock and what is next; the camera recalls only"
```

---

### Task 5: The idle return, the keyboard and the fit of every screen

**Files:**
- Modify: `src/croom/control/static/app.js` (the idle return timer)
- Modify: `src/croom/control/static/devices.js` (remove `lastCameraAction`)
- Modify: `src/croom/control/static/keyboard.js` (idle only; hides on a view change)
- Modify: `src/croom/control/static/style.css` (the keyboard rule)
- Test: `tests/unit/control/test_page.py`

**Interfaces:**
- Produces: `IDLE_RETURN_MS = 10 * 60 * 1000` in `app.js`; in idle mode any view other than `idle` returns to `idle` (`setView("idle", true)`) after that long without a `pointerdown` on the page; the preview follows (Task 3's hook, `automatic` true means the background off call).
- Consumes: `onViewChange`, `view()`, `setView`.

- [ ] **Step 1: Write the failing tests**

Delete the two Task 3 skips and replace those two tests (`test_the_camera_panel_closes_itself_after_ten_minutes_without_a_camera_action` and `test_a_camera_action_keeps_the_camera_panel_open_another_ten_minutes`) with:

```python
def test_an_idle_screen_returns_to_the_idle_screen_after_ten_minutes_and_ends_the_preview(browser):
    """The kiosk never reloads and nobody presses Back: without this, a room left on Camera framing keeps the TV on
    the camera for good. Each timer that falls due during a fast-forward fires once, at the end."""
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        asked = preview_requests(page)
        page.click("#open-options")
        page.click("#open-camera-setup")
        page.wait_for_timeout(300)
        assert asked == [True]
        page.clock.run_for(60_000)
        page.clock.fast_forward(8 * 60_000)                                   # nine minutes: still there, still renewing
        page.wait_for_timeout(300)
        assert page.evaluate("document.body.dataset.view") == "camera" and asked == [True] * 4
        page.clock.fast_forward(90_000)                                       # past ten minutes without a tap
        page.wait_for_timeout(300)
        assert page.evaluate("document.body.dataset.view") == "idle"
        assert asked == [True] * 4 + [False] and server.devices.camera.preview_on is False
        page.clock.run_for(60_000)
        page.wait_for_timeout(300)
        assert asked == [True] * 4 + [False]                                  # no renewal after the return
        page.close()


def test_a_tap_keeps_an_idle_screen_another_ten_minutes(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        page.click("#open-options")
        page.clock.fast_forward(9 * 60_000)
        page.wait_for_timeout(200)
        page.click("#open-screensaver")                                       # a tap at nine minutes
        page.clock.fast_forward(9 * 60_000)
        page.wait_for_timeout(200)
        assert page.evaluate("document.body.dataset.view") == "screensaver"   # eighteen minutes in, still here
        page.clock.fast_forward(90_000)
        page.wait_for_timeout(200)
        assert page.evaluate("document.body.dataset.view") == "idle"
        page.close()


def test_a_meeting_screen_never_returns_on_its_own(browser):
    with PageServer() as server:
        page = page_on_a_stopped_clock(browser, server)
        join_by_id(page)
        page.click("#open-upcoming")
        page.clock.fast_forward(30 * 60_000)
        page.wait_for_timeout(200)
        assert page.evaluate("document.body.dataset.view") == "upcoming"
        page.close()


def test_the_keyboard_opens_on_the_idle_screen_only_and_hides_when_a_meeting_starts(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_selector("#keyboard:not([hidden])", timeout=5000)
        assert page.locator(".schedule").is_hidden()                           # the bookings make room for the keys
        assert page.locator("#link-input").is_visible()
        tap(page, "9", "8", "7", "6", "5", "4", "3", "2", "1", "0", "0")
        tap(page, "join")
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        assert page.locator("#keyboard").is_hidden() and not page.evaluate("document.body.classList.contains('keyboard-open')")
        page.click("#meeting-actions button:has-text('Leave')")
        page.click("#meeting-actions button:has-text('Tap again to leave')")
        page.wait_for_function("document.body.dataset.view === 'idle'", timeout=5000)
        assert page.locator(".schedule").is_visible()
        page.click("#open-options")
        page.click("#keyboard-toggle", force=True) if page.locator("#keyboard-toggle").is_visible() else None
        assert page.locator("#keyboard").is_hidden()                            # nothing to type into on Options
        page.close()


def test_going_offline_on_the_camera_screen_keeps_back_working_and_releases_the_preview(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        asked = preview_requests(page)
        open_camera_screen(page)
        page.wait_for_timeout(300)
        assert asked == [True]
        page.route("**/api/status", lambda route: route.abort())
        page.wait_for_function("document.body.dataset.state === 'offline'", timeout=8000)
        assert page.locator("#back-from-camera").is_enabled()
        page.click("#back-from-camera")
        assert page.evaluate("document.body.dataset.view") == "options"
        page.wait_for_timeout(300)
        assert asked == [True, False]                                          # the off call went out, even if it could not be answered
        page.click("#back-from-options")
        assert page.evaluate("document.body.dataset.view") == "idle"
        page.close()


def test_every_meeting_screen_fits_the_kiosk_without_scrolling(browser):
    bookings = [event(f"e{i}", f"Booking {i}", 20 + 30 * i) for i in range(8)]
    with PageServer(calendar_events=bookings) as server:
        page = open_page(browser, server)
        join_by_id(page)
        assert fits(page), "the meeting screen scrolls"
        page.click("#open-camera-live")
        assert fits(page), "the meeting's camera screen scrolls"
        page.click("#back-from-camera")
        page.click("#open-upcoming")
        page.wait_for_function("document.querySelectorAll('#upcoming-list .event').length === 8", timeout=5000)
        assert fits(page), "the upcoming screen scrolls"
        page.close()
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/control/test_page.py -k "idle_screen_returns or tap_keeps or never_returns or keyboard_opens_on_the_idle or going_offline or meeting_screen_fits"`
Expected: FAIL (the view never returns on its own; the keyboard stays open in the meeting; the schedule box stays visible).

- [ ] **Step 3: The idle return in `app.js`**

After `function setView(...)` add:

```js
  // Idle mode returns to the idle screen on its own: the kiosk never reloads and nobody presses Back, so a room left on
  // Camera framing would keep the TV on the camera for good. Any tap restarts the wait; a meeting screen never returns.
  const IDLE_RETURN_MS = 10 * 60 * 1000;
  let idleReturnTimer = null;
  function armIdleReturn() {
    clearTimeout(idleReturnTimer);
    idleReturnTimer = null;
    if (mode() === "idle" && view() !== "idle") idleReturnTimer = setTimeout(() => setView("idle", true), IDLE_RETURN_MS);
  }
  document.addEventListener("pointerdown", armIdleReturn, true);
```

and call `armIdleReturn();` at the end of `applyView()` (after the hooks), so every view change, including an automatic one, starts or clears the wait. Publish `IDLE_RETURN_MS` is not needed.

- [ ] **Step 4: The keyboard and the devices**

In `src/croom/control/static/keyboard.js`:
- in `show()`, as the first line: `if (P.view() !== "idle") return;   // nothing to type into on any other screen`
- after `return { show: show, hide: hide };` is not reachable from outside now, so change the IIFE's end: replace `return { show: show, hide: hide };` with

```js
    P.onViewChange((name) => { if (name !== "idle") hide(); });
```

In `src/croom/control/static/style.css` replace `body.keyboard-open .screen { padding-bottom: 290px; }` (line 219) with:

```css
body.keyboard-open .schedule { display: none; }   /* the bookings make room; the link box stays above the keys */
```

In `src/croom/control/static/devices.js` delete `lastCameraAction` and its two assignments (in `deviceCall` and the view hook): the idle return counts taps itself.

- [ ] **Step 5: Run the page tests**

Run: `.venv/bin/pytest -q -p no:cacheprovider -W error tests/unit/control/test_page.py tests/unit/control/test_tv_page.py tests/unit/control/test_service.py`
Expected: PASS, no skips left in `test_page.py`.

- [ ] **Step 6: Commit**

```bash
git add src/croom/control/static tests/unit/control/test_page.py
git commit -m "feat(control): idle screens return on their own, the keyboard keeps to the idle screen, every screen fits the kiosk"
```

---

### Task 6: Docs, the gate and the acceptance

**Files:**
- Modify: `README.md` (the room page row, line 16; the decisions list), `docs/guides/crystal-meet-room-setup/index.html` (steps at lines ~131 and ~136; rebuilt `docs/guides/crystal-meet-room-setup.pdf`), `tests/unit/docs/test_readme.py`, `tests/unit/docs/test_room_setup_guide.py`

- [ ] **Step 1: Write the failing docs tests**

Append to `tests/unit/docs/test_readme.py`:

```python
def test_readme_describes_the_room_page_in_two_modes():
    text = README.read_text(encoding="utf-8")
    for needle in ("Options", "Screensaver", "Upcoming meetings", "meeting screen", "1280 by 800", "REPLACE_WITH_LOCATION"):
        assert needle in text, needle
    assert "TV when idle" not in text
```

Append to `tests/unit/docs/test_room_setup_guide.py` (it reads the guide's HTML into a variable; follow its existing helper):

```python
def test_guide_describes_the_screens_and_the_options_button():
    html = GUIDE.read_text(encoding="utf-8")
    for needle in ("Options", "Camera framing", "Screensaver", "Upcoming meetings", "meeting screen", "ten minutes", "Back"):
        assert needle in html, needle
    assert "TV when idle" not in html
```

(`GUIDE` is whatever name the file already uses for the guide's `index.html` path; reuse it.)

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/docs -k "two_modes or screens_and_the_options"`
Expected: FAIL.

- [ ] **Step 3: Write the docs**

README line 16 (the room page row): replace the row's description with: "An idle screen (today's bookings, Join now, a box to paste a Zoom or Meet link with its own on-screen keyboard, and an **Options** button for **Camera framing** and the **Screensaver**) and a meeting screen that appears on its own during a call (Mute, Turn camera off, Leave, the room's volume, **Camera** for presets and nudges, **Upcoming meetings**). Every screen fits the table Pi's 10.1-inch display without scrolling (the kiosk runs Chromium at 1.5 times scale, 1280 by 800); an options screen left alone returns to the idle screen after ten minutes."

Add to the README's decisions list: "- The room page is two modes, not one column: the idle screen shows how to join and what is booked, the meeting screen shows the call's controls; everything else is a screen behind Options or a button, with Back, so the table screen never scrolls. A room config that still has `location: \"REPLACE_WITH_LOCATION\"` shows the room's name alone."

Room setup guide: in the "Pick the TV's idle look" step replace "Under <span class=\"ui\">TV when idle</span> on the room page" with "Tap <span class=\"ui\">Options</span>, then <span class=\"ui\">Screensaver</span> on the room page"; in the "Set up the camera and sound" step replace "Open <span class=\"ui\">Camera framing</span>" with "Tap <span class=\"ui\">Options</span>, then <span class=\"ui\">Camera framing</span>", and the sentence about the panel closing itself with "a screen left alone returns to the idle screen after ten minutes, and <span class=\"ui\">Back</span> returns at once". Add one sentence to that step: "During a call the page shows the meeting screen on its own: Mute, Turn camera off, Leave, the volume, <span class=\"ui\">Camera</span> for a preset or a nudge, and <span class=\"ui\">Upcoming meetings</span> to see what is booked next." Then rebuild: `.venv/bin/python docs/guides/crystal-meet-room-setup/build.py`.

- [ ] **Step 4: Run the docs tests and the full gate**

Run: `.venv/bin/pytest -q -p no:cacheprovider -W error tests/unit/docs tests/unit/control tests/unit/core/test_config.py tests/unit/installer/test_install_kiosk_script.py`
Expected: PASS.

Run: `bash /tmp/claude-1000/-home-cpm-ssh/f78b4b20-e6f0-4204-894b-193a8d8a2549/scratchpad/suite-gate.sh room-page-modes | grep "GATE:"`
Expected: `GATE: PASSED`.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/guides/crystal-meet-room-setup/index.html docs/guides/crystal-meet-room-setup.pdf tests/unit/docs
git commit -m "docs: the room page in two modes, Options and Screensaver, the location line"
```

- [ ] **Step 6: Acceptance on the Pis (after merge, Ben's step)**

On PiMeet-2 re-run `sudo bash installer/install-kiosk.sh --url http://pimeet-3.local:8080/` (the scale flag) and log out and in; on PiMeet-3 update the service. Then: the idle screen shows buttons about a finger tall and nothing to scroll; Options, Camera framing (the TV shows the camera), Back, Screensaver, Back; join a meeting: the meeting screen appears on its own, Mute and Turn camera off work, Camera recalls Table during the call, Upcoming shows the next booking, Leave asks once and returns to idle; leave the room on Camera framing: ten minutes later the idle screen is back and the TV is on the screensaver; a room whose config has no location shows its name alone.
