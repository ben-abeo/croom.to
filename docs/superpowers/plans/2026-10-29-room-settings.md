# Room settings on the touch Pi: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Built at Ben's word ("Just build it all"): the
> tasks give requirements, interfaces and tests in prose rather than full code; implementers write the code test-first in the style of
> the modules they touch.

**Goal:** Every setting, the Google sign-in, health checks, logs, restart/reboot, software updates and Wi-Fi of the TV Pi, the touch
Pi and the door sign, from the touch Pi behind a PIN; plus a Mic level slider on the Sound panel.

**Architecture:** Settings pages in the room page (`src/croom/control/static/`), served by the control service, behind a PIN
(`croom.settings`). Privileged jobs go to a root helper on the TV Pi (`croom.admin`, a Unix-socket server with a fixed job list). The
touch Pi (a standard-library agent) and the door sign app check in with the TV Pi for jobs.

**Spec:** `docs/superpowers/specs/2026-10-29-room-settings-design.md` (binding).

## Global Constraints

- Branch `room-settings`; one commit (or a few) per task; no attribution lines in commit messages.
- Tests: `.venv/bin/python -m pytest <paths> -q`; asyncio_mode is auto. Nothing in a test may need root, touch the machine's network,
  services, `/etc`, `/media`, uinput or GitHub: every such thing is injected (runner functions, paths, clocks) and faked in tests.
- Room-page code is plain JS/HTML/CSS in `src/croom/control/static/`, no build step; page tests use Playwright (`tests/unit/control/
  test_page.py`'s `PageServer` and `browser` fixture). The touch Pi is 1280 by 800; pages must fit it (`fits(page)`), and work in a
  laptop browser.
- Every POST is JSON (415 otherwise) except the credential upload (multipart); refusals are words for the person, `{"error": …}`.
- The PIN: 4 to 8 digits; stored hashed (`hashlib.scrypt` or PBKDF2-HMAC-SHA256 with a random salt) in `settings-pin.json` in the data
  directory, mode 600; unlock tokens live in memory, 5 minutes after the last request that used them; **Lock** ends one; 5 wrong tries
  lock unlocking for 10 minutes. Protected routes answer 401 `{"error": "Enter the PIN", "locked": false}` without a valid token
  (sent as header `X-Croom-Unlock`), and 423 with the minutes left while locked out.
- Words, verbatim: "Leave the meeting first"; "Saved"; "Saved: applies after a restart"; "Restart now"; "Not available on this device";
  "Can't check for updates right now"; "Your last change was undone: the room did not start with it"; "Signed in as <address>";
  "Not seen since <time>".
- Helper socket `/run/croom-admin.sock` (group `croom`, mode 660); one JSON request per connection, one JSON answer
  `{"ok": true, …}` or `{"ok": false, "error": words}`; no shell anywhere (`subprocess` with argument lists).
- Config writes keep `config.yaml.1` … `config.yaml.5`; the restart health watch waits 2 minutes for `GET /api/status`.
- Touch Pi agent: standard-library Python 3.11 only (it runs from the repo checkout with the system Python, no venv).
- A room without the helper (developer machine, tests) shows privileged pages as "Not available on this device" and every other page
  works.
- Keep everything that works today working: Options' existing items, the door sign, Laptop mode, the TV.

## Review Focus

- A config the room does not start with must be rolled back by the helper, never leave the room down.
- A Wi-Fi change that loses the TV Pi must come back by itself (every device).
- An update cut off half way (power) must leave the running version intact.
- The PIN must not be bypassable: every privileged and settings route checks the token; the helper refuses anything outside its list.
- The sign-in must always give the TV its room browser back (Done, Cancel, timeout, agent stop).

---

### Task 1: The PIN and the Settings shell

**Files:** create `src/croom/settings/__init__.py`, `src/croom/settings/pin.py`; modify `src/croom/control/service.py`, `static/index.html`,
`static/app.js` (or a new `static/settings.js` loaded by index.html), `static/style.css`; tests `tests/unit/settings/test_pin.py`,
`tests/unit/control/test_settings_api.py`, `tests/unit/control/test_settings_page.py`.

**Produces:**
- `PinStore(path, clock=time.monotonic)`: `is_set() -> bool`, `set_pin(pin)` (only when not set, or via `change(old, new)`),
  `unlock(pin) -> str token` (raises `PinError` with words; `PinLocked(minutes)` after 5 wrong tries for 10 minutes), `check(token) ->
  bool` (sliding 5 minutes), `lock(token)`, `change(old, new)`.
- Control service: `ControlService(..., pin_store=None)` (default from the data dir); routes `GET /api/settings/pin` → `{"set": bool,
  "locked_minutes": int|0}`, `POST /api/settings/pin/set {pin}` (only when unset; answers a token), `POST /api/settings/unlock {pin}` →
  `{"token", "expires_in"}`, `POST /api/settings/lock`, `POST /api/settings/pin/change {old, new}` (protected); a helper
  `self._require_unlock(request) -> Optional[web.Response]` every later settings route calls first.
- Page: Options gains **Settings** (icon `settings`) → a PIN pad view (`data-view="settings-pin"`: digits, delete, OK; "Choose a PIN"
  with confirm the first time) → the Settings menu view (`data-view="settings"`) listing the pages of spec §3 as menu items (pages not
  built yet show "Coming soon" when opened), a top bar with **Lock** and the minutes left, and **Back** to Options. The token is kept in
  `sessionStorage` and sent as `X-Croom-Unlock`; a 401 returns to the PIN pad. The Settings views go back to idle after the usual idle
  time like Options. The **Security** page (change the PIN: old, new, confirm) is built here.

**Tests:** PinStore (hashing — the file holds no digits of the PIN; length rules; wrong tries and the 10-minute lock with a fake clock;
sliding expiry; lock; change). API (set once only; unlock/lock; 401 without token; 423 while locked; change). Page (first-time choose
and confirm; unlock; wrong PIN words; Lock returns to the pad; fits at 1280×800).

---

### Task 2: The root helper

**Files:** create `src/croom/admin/__init__.py`, `src/croom/admin/server.py` (the socket server and job table), `src/croom/admin/jobs.py`
(the jobs), `src/croom/admin/client.py`, `src/croom/admin/__main__.py` (`python -m croom.admin` runs the server); modify
`installer/install.sh` (a `croom-admin.service` running as root, `ExecStart=$INSTALL_DIR/venv/bin/python -m croom.admin`, socket group
`croom`); tests `tests/unit/admin/test_server.py`, `tests/unit/admin/test_jobs.py`.

**Produces:**
- `AdminServer(socket_path, jobs, group=None)`; `serve()`; one JSON request `{"job": name, ...args}` per connection, answer as in the
  Global Constraints; unknown job → `{"ok": false, "error": "Unknown job"}`; a job's own `JobError(words)` → its words; anything else →
  "The helper failed: <class>" and logged.
- `AdminClient(socket_path="/run/croom-admin.sock", timeout)`: `available() -> bool` (socket exists), `async call(job, **args) -> dict`
  raising `AdminUnavailable` / `AdminError(words)`.
- Jobs in this task (each a function taking injected `paths`/`run` so tests fake them): `write_config {yaml}` (parse with
  `yaml.safe_load`, `Config.from_dict`, the schema check from Task 3 when present — until then `Config.from_dict` only; rotate `.1`…`.5`;
  write a temp file in `/etc/croom` and `os.replace`; chown `croom`, mode 640), `save_credential {kind: "google"|"zoom", content}` (the
  checks of spec §4; write `/etc/croom/google-service-account.json` or `zoom-credentials.json`, mode 600, owner croom; answer
  `{"path", "summary"}`), `usb_files` (`*.json` directly under `/media/*/*/`, size ≤ 64 KB), `read_usb_file {path}` (only a path
  `usb_files` lists), `restart` (`systemctl restart croom` then the health watch in a background thread: poll
  `http://127.0.0.1:<control port>/api/status` every 5 s for 2 minutes; on failure restore `config.yaml.1` if the last write was within
  the last 10 minutes and restart again; record `{"undone": true, "at": …, "why": …}` in `/var/lib/croom/admin-state.json`),
  `restart_status` (that record), `reboot` (`systemctl reboot`), `version` (reads `/opt/croom/VERSION` if present, else "unknown").

**Tests:** server round trip on a temp socket; unknown job; a job's words; write_config rotation and refusal of a bad config (nothing
written); save_credential checks (a Zoom file as google refused naming what it is; mode 600); usb_files only lists json under the fake
media root and read_usb_file refuses other paths; restart's watch rolls back with a fake status probe and fake runner.

---

### Task 3: The settings schema and the Room page

**Files:** create `src/croom/settings/schema.py`, `src/croom/settings/config_edit.py`; modify `src/croom/control/service.py`,
`src/croom/admin/jobs.py` (write_config uses the schema check), static files; tests `tests/unit/settings/test_schema.py`,
`tests/unit/control/test_settings_config_api.py`, page test.

**Produces:**
- `Setting(path: "room.name", label, page, kind, choices|range, live: bool, help)`; `SCHEMA: list[Setting]` covering every key the
  pages of spec §3 edit (all of `room`, `meeting` (platforms, camera_default_on, mic_default_on, kiosk, dialogs.acknowledge,
  dialogs.ask_everything, controls.reactions), `calendar` (google_calendar_id, booking_start, booking_end, google_credentials_path as a
  credential), `audio` (input_device, output_device), `video` (device, ptz_travel_seconds), `control.camera_presets`, `laptop.*`, `ai.*`,
  `dashboard` (enabled, url, enrollment_token)), `validate(path, value) -> value` raising `SettingError(words)`, `check_config(dict)`.
- `ConfigEditor(config_path, admin_client)`: `values(page) -> dict` (read from the current config file), `async save(page, changes) ->
  {"saved": [...], "restart": bool}` (validate each, build the whole new YAML from the current file's dict with the changes applied, send
  `write_config`; apply live settings at once through hooks the control service registers: booking hours → `RoomBookings` hours;
  camera preset names → the camera).
- Routes (protected): `GET /api/settings/config/{page}` → `{"values", "fields": [schema entries]}`; `POST /api/settings/config/{page}`
  → the save answer or 400 with `{"errors": {path: words}}`; `POST /api/settings/restart` (refused in a meeting; 503 "Not available on
  this device" without the helper); `GET /api/settings/restart` → restart_status.
- Page: a generic form renderer from `fields` (text with the on-screen keyboard, number, switch, choice, list editor, time), Save,
  per-field errors, "Saved" / "Saved: applies after a restart" with **Restart now**; the undo banner from restart_status. The **Room**
  page uses it (time zone choices from `zoneinfo.available_timezones()`).

**Tests:** schema validation per kind; check_config; ConfigEditor with a fake admin client (the YAML sent; live hooks called; restart
flag); API errors; page (edit the room name, Saved: applies after a restart, Restart now refused in a meeting).

---

### Task 4: Mic level

**Files:** `src/croom/devices/volume.py` (or a new `src/croom/devices/mic.py` with the same shape as `RoomVolume`, reusing its pw-dump
parsing), `src/croom/devices/service.py`, control service (routes `GET/POST /api/audio/mic`), `static/devices.js` and `index.html`
(the Sound panel), tests.

**Produces:** `RoomMic` (`Audio/Source` nodes from pw-dump; `audio.input_device` preference or the default source; `level` 0–150;
`set_level`, `step`); the devices service keeps the chosen level in the settings store (`mic_level`) and re-applies it when a meeting
joins and every 5 s while connected (the meeting service's state callback); status gains `mic` like `audio`. The Sound panel shows Mic
level under the speaker with Quieter/Louder; "No microphone found" when there is none.

**Tests:** parsing sources; preference; set/step clamping to 0–150 (wpctl takes 1.5); re-apply loop with a fake clock/runner; route;
page.

---

### Task 5: The other config pages and the credential files

**Files:** static files (pages Calendar, Meetings, Devices, Laptop switch, AI & dashboard; the TV page opens Options › TV), control
service routes for credentials, `src/croom/settings/credentials.py`; tests.

**Produces:**
- Routes (protected): `POST /api/settings/credentials/{kind}` (multipart field `file`, ≤ 64 KB) and `POST
  /api/settings/credentials/{kind}/usb {path}` → helper `save_credential` (then the config's path set through ConfigEditor, restart
  flag); `GET /api/settings/credentials` → `{"google": {"summary", "saved_at"}|null, "zoom": …}` read from the files' metadata (never
  their secrets); `GET /api/settings/usb-files` → helper `usb_files`; `POST /api/settings/credentials/zoom/typed {key, secret}`.
- Devices page choices from what the room sees: speakers and microphones from pw-dump, cameras from `/dev/video*` names
  (`V4l2Controls`), each with Automatic.
- Laptop switch page **Test the switch** runs `check_laptop(move=True)` against the running Laptop service's switch when Laptop mode
  is on (refused in a meeting), else says to turn it on and restart.

**Tests:** credential routes (multipart; wrong kind refused; summary shows client_email only); USB path flow with a fake helper;
device choices; page tests for each page saving one field.

---

### Task 6: Sign in to Google on the TV

**Files:** `src/croom/tv/pointer.py` (enable `keys` for target `pi`), a `PiKeyboard` (evdev UInput, next to `PiMouse`), `src/croom/
meeting/signin_session.py`, `src/croom/meeting/service.py` (`async begin_sign_in()`, `async end_sign_in(done: bool) ->
Optional[str] address`, `sign_in_state()`), control routes (protected: `POST /api/settings/google/sign-in`, `.../done`,
`.../cancel`, `GET .../state`), `static/keyboard.js` (a "Type on the TV" target), static Meetings page; tests.

**Produces:**
- `keys` messages `{"target": "pi", "type": "keys", "text": "abc"}` and `{"target": "pi", "type": "keys", "key": "Enter"|"Backspace"|
  "Tab"|"ArrowLeft"|…}` typed through `PiKeyboard` (US layout: letters, digits, the printable ASCII symbols with shift as needed);
  refused for target `tv`.
- Sign-in session: refused in a meeting; `TvDisplay.stop()`; launch the bundled Chromium (as `meet_signin` does: same profile dir,
  `--kiosk`, `https://accounts.google.com/`, no automation) as a subprocess; Done → terminate it, check the session cookies
  (`meet_signin`'s check), read the signed-in address from the profile (the `Preferences` file's `account_info` email, else "the room's
  Google account"), `TvDisplay.start()`; Cancel or 15 minutes idle (no pointer or key events) → terminate, start the display; agent stop
  → the same. A room with no `meeting.google_profile_dir` → "Set up Google Meet first" (the Meetings page offers to set the default
  profile dir `/var/lib/croom/meet-profile`).
- Page: Meetings › **Sign in to Google** → the full-screen touchpad on Room Pi with a keyboard toggle that types on the TV, Done and
  Cancel; the Meetings page shows "Signed in as …" or "Not signed in".

**Tests:** keys mapping (characters → key codes and shift); refusal for tv target; session lifecycle with fake display/process/
clock (Done, Cancel, idle timeout, refusal in a meeting); routes; page (keys reach the socket).

---

### Task 7: Check and Logs

**Files:** `src/croom/settings/checks.py`, control routes (protected `POST /api/settings/checks/{name}`, `GET /api/settings/logs`),
`src/croom/admin/jobs.py` (`journal`), static pages; tests.

**Produces:** checks `calendar` (`check_calendar(config, out)`), `tv` (`check_tv`), `laptop` (the running service's state and holder;
no press), `google` (the profile's session check), `zoom` (`check_zoom`): each run in-process with a `StringIO`, answering `{"ok",
"text"}` (time-limited 60 s); logs via the helper's `journal {lines: 200, level: "all"|"warning", before: cursor}` → `[{"time",
"level", "text"}]`, newest first, "Load more".

**Tests:** each check with its existing fakes; journal parsing of `journalctl -o json` lines; routes; page.

---

### Task 8: TV Pi software: updates and reboot

**Files:** `src/croom/admin/update.py` (jobs `update`, `update_status`, `rollback`, and the startup sweep), `src/croom/settings/
github.py` (newest commit and commit titles, cached 10 minutes, injected HTTP), control routes (protected `GET /api/settings/software`,
`POST /api/settings/software/update {device, commit}`, `POST /api/settings/reboot {device}`), `installer/install.sh` (release layout
and `/opt/croom/VERSION`), static Software page; tests.

**Produces:** releases in `/opt/croom/releases/<commit>/venv`, `/opt/croom/venv` a symlink, `VERSION` per release; `update` builds
(`python -m venv`, `pip install git+https://github.com/ben-abeo/croom.to.git@<commit>`, progress lines kept in admin-state), switches
the link atomically (`os.replace` of a temp symlink), restarts, watches 3 minutes, rolls back on failure; two releases kept; a startup
sweep removes unfinished release folders. Software page: the TV Pi card (version, newest, changes, Update, progress, rolled-back notice),
**Restart room software**, **Reboot**; refused in a meeting.

**Tests:** the whole update/rollback flow with fake runner, fake filesystem root and fake health probe; github client with a fake
transport; routes; page.

---

### Task 9: Check-in, the touch Pi agent

**Files:** `src/croom/settings/devices.py` (registry: pending/allowed devices, hashed tokens, job queue, last seen, persisted in the
data dir), control routes (`POST /api/devices/checkin` — not PIN-protected, token-checked; protected `GET /api/settings/devices`,
`POST /api/settings/devices/{id}/allow|refuse`, jobs routes), `tools/kiosk_agent/croom_kiosk_agent.py` (standard library only),
`installer/install-kiosk.sh` (install the agent as a root systemd service with the room URL; pairing happens on first check-in), static
Software page cards for devices and "Devices waiting to join"; tests `tests/unit/settings/test_devices.py`, `tests/unit/kiosk_agent/`.

**Produces:** check-in protocol of spec §7 (id = a random id the device keeps; kind; version; network; last job result); jobs
`reboot`, `update {commit}` (git fetch/checkout into a second checkout, re-run the kiosk setup steps, record previous, reboot; after
reboot, if no successful check-in with the new version within 5 minutes, switch back and reboot), `wifi_scan`, `wifi_connect`,
`wifi_forget` (Task 10 wires the page); the touch Pi card on the Software page (version, Update, Reboot, last seen).

**Tests:** registry (pending → allowed → token required; refuse; job queue one at a time; last seen); checkin route; the agent's job
runner with fakes (update rollback logic with a fake clock/runner/filesystem).

---

### Task 10: Wi-Fi for the TV Pi and the touch Pi; the Network page

**Files:** `src/croom/admin/wifi.py` (jobs `wifi_status`, `wifi_scan`, `wifi_connect`, `wifi_forget` with the safety net),
`tools/kiosk_agent/` (the same nmcli logic, standard library), control routes (protected `GET /api/settings/network`, `POST
/api/settings/network/{device}/scan|connect|forget`), static Network page; tests.

**Produces:** nmcli wrappers (`nmcli -t -f …` parsing); connect = keep the current connection's name, `nmcli device wifi connect`,
then within 90 s the device's check (TV Pi: gateway ping and a touch Pi check-in seen by the control service — the helper asks
`/api/settings/devices/seen-since`; touch Pi: `GET <room url>/api/status`), else `nmcli connection up <old>` and report why. Network
page cards per device with scan list, password field (on-screen keyboard), Join, Forget, and the result.

**Tests:** nmcli output parsing; connect success; fallback on failure; touch Pi agent fallback; page.

---

### Task 11: The door sign as device owner

**Files:** `android/door-sign/` (a `DeviceAdminReceiver`, manifest `device_admin` and `REQUEST_INSTALL_PACKAGES`, `CheckIn` client,
`Jobs` (reboot, Wi-Fi connect/forget/scan, self-update via `PackageInstaller` session from the TV Pi's URL), lock-task kiosk when
device owner, versionCode/versionName), TV Pi side (`GET /api/devices/apk/{versionCode}` serving an APK the TV Pi downloaded from the
GitHub release `door-sign-<versionCode>`; the door sign card on the Software page and Network page), the setup guide section (factory
reset, `adb install`, `adb shell dpm set-device-owner to.croom.doorsign/.AdminReceiver`); tests (Java unit tests for the check-in and job
parsing; Python tests for the APK route and release lookup with fakes).

---

### Task 12: Documentation and the whole suite

README (the Settings area, the PIN and its reset, the helper, updates, check-in, the door sign device owner, Mic level), the room setup
guide's steps that no longer need SSH, the spec's as-built note; the full suite compared to main.
