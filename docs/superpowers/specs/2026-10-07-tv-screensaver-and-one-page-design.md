# The TV: a screensaver when idle, one page the agent navigates

Date: 2026-10-07. Status: approved by Ben in conversation; written for the implementation plan.
Builds on the `meet-account` branch (the signed-in Meet profile); merges after it.

## 1. Goal

The Pi behind the TV is headless and shows exactly one thing at a time: a screensaver while
no meeting is running, the meeting from the moment someone presses Join on the room
controller, and the screensaver again when the meeting ends or Leave is pressed. The
screensaver's style is chosen on the room controller, per room, from four options, and the
choice survives restarts. Nothing is ever plugged into the TV Pi and nothing on it is
operated directly.

Success looks like: after the service starts, the TV shows the chosen screensaver within a
few seconds; changing the style on the room page changes the TV within a couple of seconds;
Join puts Zoom or Meet on the TV with no other window ever visible; Leave or the host ending
the meeting brings the screensaver back; a restart brings the same style back; the door sign
and the dashboard behave exactly as before.

## 2. Where things stand

- Each meeting provider launches its own Playwright browser at start-up (`zoom_sdk.py`,
  `google_meet.py`, `zoom.py`, and the untested `teams.py`/`webex.py`), so a room with Zoom
  and Meet configured has two full-size windows on the TV and whichever was created last is
  on top; between meetings a provider's page sits on `about:blank`, a dark browser window.
  Providers navigate their page to the meeting on join and to `about:blank` on leave.
- Experiments on 2026-10-07: in kiosk mode Chromium opens every additional page as a new
  window, whether through Playwright's `new_page()` or `window.open`, so "tabs the agent
  switches" is not available; separate windows raised in turn would depend on the Pi's
  window manager honouring activation requests from a background app. One page navigated by
  the agent needs neither.
- The control service (`croom/control`) serves the room page at `/` and the door sign at
  `/sign` from `static/`, with `/api/status` and `/api/calendar/events` polled by both pages;
  `sign.js` shows how a status page is written. Settings have no home yet; the dashboard
  client keeps its state under `config.resolve_data_dir()` (`/var/lib/croom` on a device).
- `MeetingService.start()` builds providers through `build_provider(platform, config)` and
  initialises each; `stop()` shuts them down. `ControlService.from_config(config, meeting,
  calendar)` receives the services it drives.
- `meeting.google_profile_dir` (branch `meet-account`) names the signed-in Meet profile; the
  Meet provider launches a persistent context on it. `croom --sign-in-meet` refuses to run
  while a Chromium holds that profile.

## 3. Decisions taken with Ben

- The screensaver styles are Information (name, status in the door-sign colours, next
  booking and time, a clock, "Press Join on the controller"), Quiet (name and status),
  Brand (the Crystal PM mark on the brand background), and Bounce (the Crystal PM logo
  drifting DVD-style and changing to another brand colour at each edge). The style is picked
  on the room controller per room; Information is the default.
- One browser window, one page, navigated by the agent: screensaver when idle, the meeting
  when joined, screensaver after. Chosen over separate windows raised in turn.
- Providers borrow that page from a display owner instead of launching browsers. Teams and
  Webex providers are left untouched: untested upstream code outside the rooms' platforms.
- The shared browser keeps GPU acceleration on; the upstream Meet provider's `--disable-gpu`
  goes. Whether Meet is happy with that on a Pi 5 is a hardware check in the acceptance.
- The door sign and the dashboard are untouched.

## 4. Design

### 4.1 The TV page (`croom/control/static/tv.html`, `tv.css`, `tv.js`)

Served at `GET /tv` by the control service, full screen, dark brand background, Lexend, no
cursor. It polls `/api/status` every 2 seconds and `/api/calendar/events` every 60 seconds,
exactly as `sign.js` does, with the same offline handling, and reads the current style from
the status payload (`screensaver`). It renders one of:

- `info`: the room's name, the status word and colour the sign uses (free green, soon amber,
  in use red, booked), the next booking's title and time or "Free for the rest of the day",
  a clock updated every 10 seconds, and the line "Press Join on the controller".
- `quiet`: the room's name and the status word and colour.
- `brand`: the Crystal PM logo, large, centred, on the brand gradient; nothing live.
- `bounce`: the logo (inlined SVG with `fill="currentColor"`) about 320 px wide moving at a
  constant speed on a `requestAnimationFrame` loop, reflecting off each edge and taking the
  next colour from the brand palette (`#1B52E5`, `#BDCEFF`, `#FFFFFF`, `#6C92F5`, `#1FA971`,
  `#E8A013`) at every bounce. The page's name and status are not shown in this style.

Switching style re-renders in place; the page never reloads. A window resize re-fits the
bounce area. When the room is in a meeting the TV page is not on screen, so it shows nothing
special for that state beyond the sign's "in use" word.

### 4.2 Choosing the style

- `GET /api/screensaver` answers `{"style": "info", "styles": ["info", "quiet", "brand",
  "bounce"]}`; `POST /api/screensaver` with `{"style": "..."}` (JSON only, like the other
  POSTs) stores it and answers the same shape, or 400 with a one-line error for an unknown
  style. `/api/status` carries `screensaver` so the TV page needs no second poll.
- The choice is kept in `control-settings.json` under the data directory
  (`config.resolve_data_dir()`), as `{"screensaver": "bounce"}`, written atomically (temp
  file and rename) with mode 600. A missing or unreadable file means the configured default,
  `control.screensaver` (default `info`). `ControlService.from_config` passes the path as
  `settings_file`; tests pass a temp path.
- The room page (`index.html`, `app.js`, `style.css`) gets a section "TV when idle" below
  "Join with a link": four buttons, Information, Quiet, Brand, Bounce, the current one
  highlighted, which POST the choice and re-render from the response. The section is hidden
  while the room page shows it is offline.

### 4.3 The display owner (`croom/meeting/display.py`)

`TvDisplay(idle_url: str, profile_dir: Optional[str] = None, headless: bool = False,
kiosk: bool = True)` with `from_config(config)`: `idle_url` is
`http://127.0.0.1:<control.port>/tv` when the control service is enabled, else
`about:blank`; `profile_dir` is `meeting.google_profile_dir`; `kiosk` is `meeting.kiosk`.

- `BROWSER_ARGS`: `--use-fake-ui-for-media-stream`, `--autoplay-policy=no-user-gesture-required`,
  `--disable-infobars`, `--no-sandbox`, `--disable-setuid-sandbox`, `--disable-dev-shm-usage`,
  `--window-size=1920,1080` (the window when kiosk is off), plus `--kiosk` when kiosk is on. Context
  options: camera and microphone permissions, no fixed viewport (the page fills the window, so the
  TV is driven at whatever resolution the Pi outputs, 1080p or 4K alike), no user-agent override.
- `start()`: starts Playwright; with a profile dir, creates it (mode 700, a clear error
  naming it when that fails) and launches a persistent context on it; otherwise launches a
  browser and a context. Takes the context's first page. Then `show_idle()`.
- `page()` (async): the shared page; if it was closed or crashed, opens a new one and parks
  it on the idle URL, logging a warning. `context` (property) for tests and for the Zoom SDK
  provider's test hooks.
- `show_idle()`: navigates the page to the idle URL with `wait_until="commit"`; a failure
  (the control service not up yet) is retried every 2 seconds for up to 30 seconds in a
  background task, then logged once; never raises.
- `stop()`: closes the context, the browser when there is one, and Playwright, each guarded
  by a 5-second timeout like the providers do today.

### 4.4 Providers borrow the page

- `build_provider(platform, config, display)` passes the display to the Zoom SDK provider,
  the Zoom web-client provider and the Meet provider (`from_config(config, display)`).
  Each keeps `self._display` and sets `self._page = await self._display.page()` in
  `initialize()` and again at the start of `join_meeting()`.
- The providers' own Playwright start-up, browser launch, context creation, `BROWSER_ARGS`,
  `context_options()`, `headless` and `profile_dir` parameters are removed. The Meet
  provider becomes `GoogleMeetProvider(display, room_name="Conference Room")`; its profile
  handling moves to the display, which exposes `profile_dir` so the provider can still tell
  a signed-in room (no name field expected) from a guest. `shutdown()` no longer closes a
  browser; the Zoom SDK provider still stops its loopback site.
- Where a provider navigated to `about:blank` (leave, the meeting-end watcher, a failed
  join's clean-up) it now awaits `self._display.show_idle()`.
- The Zoom SDK provider exposes `crystalMeetEvent` on the page it is handed; because the
  display may hand it a recreated page, it tracks which page object it exposed on and
  exposes again when the page changed. Its test hooks `extra_init_script` and
  `block_sdk_cdn` apply to `display.context`.
- `MeetingService.start()`: `self._display = TvDisplay.from_config(config)`, `await
  self._display.start()`, then the providers with the display; a display that fails to start
  logs the error and leaves the service without providers, as today when every provider
  fails. `stop()`: providers' `shutdown()`, then `self._display.stop()`.
- `croom --check-meet` and `croom --sign-in-meet` keep their own browsers and their
  profile-lock checks; the running service holds the profile, so they still require it
  stopped, which the guide already says.

### 4.5 Configuration and docs

- `MeetingConfig.kiosk: bool = True` and `ControlConfig.screensaver: str = "info"`, both in
  `to_dict`. The room configs gain nothing: the defaults are right.
- README: the pieces table's room-device row mentions the screensaver and that the TV
  shows one thing at a time; the decisions list gets a line about the one page; the known
  limitation about one browser window per platform is removed.
- The room setup guide's first-run step says the TV shows the screensaver, that the style
  is chosen on the room page, and that Join replaces it with the meeting; its PDF is
  rebuilt.

## 5. Testing

- `tests/unit/control/test_tv_page.py` (Playwright, like `test_page.py`): the `/tv` route
  serves the page; with a fake status and events, `info` shows the name, the status colour,
  the next booking and the clock; `quiet` shows only name and status; `brand` shows the logo
  and no status text; `bounce` moves the logo between two frames and changes its colour after
  an edge hit (the test shrinks the viewport so an edge comes quickly); a style change in the
  status payload re-renders without a reload.
- `tests/unit/control/test_service.py` (extended): `GET /api/screensaver` default `info`;
  `POST` stores a valid style, rejects an unknown one with 400 and a non-JSON body with 415;
  `/api/status` carries `screensaver`; a second `ControlService` on the same settings file
  starts with the stored style; an unreadable file falls back to the configured default.
- `tests/unit/control/test_page.py` (extended): the room page's picker posts the style and
  highlights the chosen one.
- `tests/unit/meeting/test_display.py` (new, headless): `start()` parks on the idle URL (a
  local replica served by a tiny aiohttp site in the test); `page()` after the page is
  closed returns a new page parked on idle; `show_idle()` with an unreachable idle URL
  retries without raising; the persistent-profile launch creates the folder with mode 700;
  `stop()` leaves no browser.
- `tests/unit/meeting/test_zoom_sdk_provider.py`, `test_meet_profile.py`,
  `test_meet_prejoin.py`, `test_zoom_web_client.py`: constructed with a headless display;
  the assertions they make today stay; leaving a meeting lands on the display's idle URL.
- `tests/unit/core/test_config.py` or `test_meet_selection.py`: `kiosk` and `screensaver`
  round-trip and default.
- The full-suite gate; acceptance on PiMeet-3 (section 1's success list, including a Meet
  join with GPU acceleration on).

## 6. Files

New: `src/croom/meeting/display.py`, `src/croom/control/static/tv.html`, `tv.css`, `tv.js`,
`tests/unit/control/test_tv_page.py`, `tests/unit/meeting/test_display.py`.

Changed: `src/croom/core/config.py`, `src/croom/control/service.py`,
`src/croom/control/static/index.html`, `app.js`, `style.css`,
`src/croom/meeting/service.py`, `src/croom/meeting/providers/__init__.py`,
`providers/zoom_sdk.py`, `providers/zoom.py`, `providers/google_meet.py`, `README.md`,
`docs/guides/crystal-meet-room-setup/index.html` and PDF, the tests named in section 5.

## 7. Out of scope

The controller's new controls (admit, camera framing, volume); the room as host and
scheduling flows; turning the TV off and on with HDMI-CEC; Teams and Webex providers; a
night mode; the dashboard showing the chosen style; the one-Pi-per-room layout, which is
dropped.
