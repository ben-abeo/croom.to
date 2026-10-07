# Google Meet as a signed-in room

Date: 2026-10-07. Status: approved by Ben in conversation; written for the implementation plan.

## 1. Goal

A Crystal Meet room joins Google Meet meetings reliably from the room page's "Join now" and
"Join with a link", as a Google Workspace user of its own, so Meet treats it as a member of
the organization: no guest wall, no knocking for internal meetings, and an "Ask to join" that
the host admits for meetings hosted by other organizations. The camera and microphone start
in the state the room config asks for.

Success looks like: pressing Join now on a booking made by a Crystal PM user puts the room in
the call within about fifteen seconds with the camera on and the microphone on; a meeting
hosted by a customer shows "waiting" on the room page until the host admits "Room 1"; a lapsed
Google sign-in gives the room page a message naming the command that fixes it; the one-time
sign-in per room takes a person five minutes with a keyboard on the Pi.

## 2. Where things stand

- Facts established on PiMeet-3 on 2026-10-07: Google Meet accepts the Pi's plain Chromium as
  a guest, refuses the same Chromium when Playwright drives it ("You can't join this video
  call"), because a driven browser announces automation, and accepts the driven browser once
  it is signed in through a persistent profile (the pre-join page "Ready to join?" with an
  enabled "Ask to join", the camera found). The Chrome 120 user-agent override was removed
  the same morning, and the provider now types the room's name into Meet's placeholder-only
  field, waits for the join control to enable, and on failure quotes Meet's words and saves a
  screenshot. `croom --check-meet URL [--profile DIR]` opens a link the way the room does and
  reports what Meet rendered.
- `GoogleMeetProvider` launches a fresh guest browser context at every start;
  `_handle_prejoin` only ever turns the camera or microphone off; `build_provider` constructs
  it with no arguments, while Zoom's provider has a `from_config`. The installer creates
  `/var/lib/croom` for the service user. The agent's unit sets `DISPLAY=:0` and
  `PLAYWRIGHT_BROWSERS_PATH=/opt/croom/browsers`; a person running a check by hand has to set
  both.
- Google Workspace signs web sessions out after an admin-set length unless an organizational
  unit's session control says otherwise; a room that must be re-signed-in every two weeks is
  not acceptable.

## 3. Decisions taken with Ben

- The room joins as a signed-in Workspace user. The alternative, hiding the automation flag so
  Meet sees a plain Chrome, was declined for the same reason as for Zoom: it is a disguise,
  and Google can tighten detection at any time.
- One Workspace user per room (`room1@`, `room2@`, `room3@crystalpm.com`), each named after
  its room so hosts see "Room 1" in the call. A single shared rooms account is documented as
  the cheaper variant, with its cost: every room appears under one name.
- The Google session lives in a persistent browser profile at `/var/lib/croom/meet-profile`,
  owned by the service user with mode 700. Guest mode stays available when no profile is
  configured, but Meet refuses it today, so the room configs ship with the profile set.
- The sign-in happens once per room through `croom --sign-in-meet`, which opens the room's
  own Chromium plainly, with no automation, so Google's sign-in works as it does for a person.
- Camera and microphone are read from the pre-join buttons and set to the config, not assumed.

## 4. Design

### 4.1 Google side (done once by the Workspace admin; documented in the guide)

1. Admin console › Directory › Users › Add new user, three times: first name "Room", last
   name "1" (then 2, 3), email `room1@crystalpm.com`, a strong password set by the admin and
   kept with the room's other secrets. Any Workspace edition seat works; no Gmail or Drive is
   needed.
2. Directory › Organizational units: a unit "Meeting rooms"; move the three users into it.
3. Security › Access and data control › Google session control, for that unit: the session
   length set to never expire (or the longest offered). Without this the rooms are signed out
   on Workspace's schedule and someone must sign them in again.
4. Security › Authentication › 2-Step Verification, for that unit: allowed but not enforced,
   so the one-time sign-in needs only the password.
5. Optional: Apps › Google Workspace, for that unit: Gmail and Drive off, Meet and Calendar on.
6. The Meet safety setting for guests is no longer needed for rooms; it can stay as it is.

### 4.2 Configuration and the factory

- `MeetingConfig.google_profile_dir: str = ""`, in `to_dict` and `from_dict`. Empty means
  guest mode (today's behaviour). The three room configs set
  `google_profile_dir: /var/lib/croom/meet-profile`.
- `build_provider("google_meet", config)` returns `GoogleMeetProvider.from_config(config)`,
  which is `GoogleMeetProvider(profile_dir=config.meeting.google_profile_dir or None,
  room_name=config.room.name or "Conference Room")`. Other platforms are built as before.

### 4.3 The provider (`croom/meeting/providers/google_meet.py`)

- `__init__(self, profile_dir: Optional[str] = None, room_name: str = "Conference Room",
  headless: bool = False)`; `headless` exists for tests, as in the Zoom SDK provider.
- `initialize()`: with a profile dir, create it (mode 700, best effort) and launch
  `chromium.launch_persistent_context(profile_dir, headless, args=BROWSER_ARGS,
  **context_options())`; the page is the context's first page. Without one, today's guest
  launch. `shutdown()` closes the context, then the browser when there is one.
- `join_meeting(...)`: the flow is unchanged; `display_name` defaults to the room's name and
  is typed only when Meet shows a name field (a signed-in room sees none).
- `_handle_prejoin`: after the name, `_set_toggle("camera", camera_on)` and
  `_set_toggle("microphone", mic_on)`. A toggle is any `button` or `[role="button"]` whose
  `aria-label` contains the word; a label containing "turn on" means the device is off, "turn
  off" means on. The room clicks when the state differs from the wanted one, reads the label
  again, and logs the result; a missing toggle is logged at warning and does not stop the join.
- Expired or missing sign-in: when the join control is not found and the page's address is on
  `accounts.google.com`, or its words begin with "Sign in", the failure reads "The room's
  Google sign-in has expired or was never done; stop the service and run croom --sign-in-meet
  on the device". Other failures keep this morning's quoted words and screenshot.
- `_wait_for_connection`: the lobby check also recognises "Asking to join" and "Someone will
  let you in".

### 4.4 The sign-in command (`croom/meeting/meet_signin.py`)

`croom --sign-in-meet [-c CONFIG]` reads `meeting.google_profile_dir` (refuses when empty) and
calls `sign_in_meet(profile_dir, out, display=os.environ.get("DISPLAY"), executable=None,
runner=subprocess.run) -> int`:

1. Without a display: exit 1 with "run this on the room device with its screen, for example
   with DISPLAY=:0".
2. If `profile_dir/SingletonLock` exists: exit 1 with "the room service is using this profile;
   stop it first: sudo systemctl stop croom".
3. Create the folder with mode 700. Find the bundled Chromium through Playwright
   (`chromium.executable_path`), unless `executable` is given.
4. Print what to do: sign in on the room's screen as the room's Google user, then close the
   browser window. Run `[executable, --user-data-dir=<profile>, --no-first-run,
   --no-default-browser-check, --window-size=1920,1080, https://accounts.google.com/]` with
   `runner` and wait.
5. When the browser exits: if `profile_dir/Default/Cookies` exists, print "Profile saved;
   test it with croom --check-meet <link> -c CONFIG" and exit 0; otherwise print that no
   session was saved and exit 1.

Both `meet_signin` and `meet_check` set `PLAYWRIGHT_BROWSERS_PATH=/opt/croom/browsers` when
the variable is unset and that folder exists, so hand-run commands need only `DISPLAY`.

### 4.5 The check command

`croom --check-meet URL -c CONFIG` uses the configured profile when the config has one;
`--profile DIR` overrides it. The output's first line says which it used.

### 4.6 Installer, room configs, README

- `create_directories` makes `$DATA_DIR/meet-profile` (mode 700, owned by the service user
  through the existing `chown -R`).
- `print_completion`, when the installed room config contains `google_profile_dir`, prints the
  three lines to sign the room in: stop the service, run `--sign-in-meet` as the service user
  with `DISPLAY=:0`, start the service.
- README: five guides, the Meet guide fourth (after the calendar, before Zoom); the Meet
  paragraph in "How the pieces fit" and the decisions list say that rooms join Meet as their
  own Workspace user because Meet refuses automated guests; the known-limitation line about
  guests being admitted is replaced.
- The room setup guide's sentence "Google Meet waits until someone in the meeting admits the
  room" becomes "Google Meet needs the room signed in once; see the Meet guide".

### 4.7 The guide

`docs/guides/crystal-meet-google-meet/` renders "Let Crystal Meet rooms join Google Meet" in
the house style: an intro saying why (Meet refuses automated guests; a room with its own
account is treated like a colleague); steps 1 and 2 for the users and the organizational unit
with session control and 2-step verification (section 4.1); step 3 the sign-in on the device
(`sudo systemctl stop croom`, the `--sign-in-meet` command with `DISPLAY=:0`, what appears on
the TV, `sudo systemctl start croom`); step 4 the test (an internal meeting joins with "Join
now"; a meeting hosted by a personal or outside account knocks and is admitted;
`croom --check-meet <link> -c /etc/croom/config.yaml` for a terminal view); troubleshooting
(the sign-in expired message; "You can't join this video call" means no profile is
configured or the sign-in was done as a guest; the wrong account name showing in the call;
camera or microphone off); a note on the shared-account variant.

## 5. Testing

- `tests/unit/core` or the existing config tests: `google_profile_dir` round-trips through
  `to_dict`/`from_dict` and defaults to empty; `tests/unit/deploy/test_room_configs.py`
  expects `/var/lib/croom/meet-profile`.
- `tests/unit/meeting/test_meet_selection.py` (new): `build_provider("google_meet", config)`
  yields a provider with the profile dir and the room's name; empty dir means guest mode.
- `tests/unit/meeting/test_meet_prejoin.py` (extended, replica pages): a signed-in pre-join
  page with "Ready to join?", "Join now", a microphone button labelled "Turn on microphone"
  and a camera button labelled "Turn off camera": with both wanted on, the microphone is
  clicked once and the camera not at all, then Join now is pressed; with both wanted off, the
  camera is clicked and the microphone not; a page with no toggles still joins; a replica of
  Google's sign-in page produces the expired-sign-in message.
- `tests/unit/meeting/test_meet_profile.py` (new): `initialize()` with a temporary profile
  dir and `headless=True` creates the folder with mode 700 and opens a persistent context
  that can load a local page; `shutdown()` leaves no browser behind; `from_config` passes
  the dir and the name.
- `tests/unit/meeting/test_meet_signin.py` (new): no display → exit 1 and the message; a
  `SingletonLock` → exit 1 naming `systemctl stop croom`; with a fake runner, the command
  line (executable, `--user-data-dir`, the sign-in address), the folder mode 700, the
  success message when `Default/Cookies` exists and the failure message when it does not;
  `--sign-in-meet` and its help text on the command line; `PLAYWRIGHT_BROWSERS_PATH`
  defaulting when `/opt/croom/browsers` exists (with a fake path).
- `tests/unit/meeting/test_meet_check.py` (extended): `-c CONFIG` with a profile dir in the
  config uses it; `--profile` wins over the config.
- Installer tests: the profile folder is created with mode 700; the completion text names
  `--sign-in-meet` when the room config has a profile dir and not otherwise.
- Docs tests: the new guide builds, is self-contained and quotes the commands; the README
  lists five guides and links this spec and plan; the room setup guide's sentence changed.
- The full-suite gate; acceptance on PiMeet-3 with a real room account: sign in, an internal
  meeting with "Join now", an outside-hosted meeting admitted, camera and microphone states,
  then `croom --check-meet` with the config.

## 6. Files

New: `src/croom/meeting/meet_signin.py`, `docs/guides/crystal-meet-google-meet/*` and the
PDF, `tests/unit/meeting/test_meet_selection.py`, `test_meet_profile.py`,
`test_meet_signin.py`, `tests/unit/docs/test_google_meet_guide.py`.

Changed: `src/croom/core/config.py`, `src/croom/core/agent.py`,
`src/croom/meeting/providers/__init__.py`, `src/croom/meeting/providers/google_meet.py`,
`src/croom/meeting/meet_check.py`, `deploy/rooms/room-{1,2,3}.yaml`, `deploy/rooms/README.md`,
`installer/install.sh`, `README.md`, `docs/guides/crystal-meet-room-setup/index.html` and PDF,
`tests/unit/meeting/test_meet_prejoin.py`, `test_meet_check.py`,
`tests/unit/deploy/test_room_configs.py`, `tests/unit/installer/test_install_script.py`,
`tests/unit/docs/test_readme.py`, `test_room_setup_guide.py`.

## 7. Out of scope

Hiding the automation flag; Google's Meet Media API; automating the sign-in itself;
monitoring the session from the dashboard (a later feature could surface "sign-in expired"
there); a shared rooms account beyond its mention in the guide; Zoom, which is unchanged; the
one-Pi-per-room touch layout, which is a separate design.
