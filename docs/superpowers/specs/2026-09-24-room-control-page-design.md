# Room control page and control API: design

Date: 2026-09-24. Branch: `room-control` off `main` (8f294dd) on the ben-abeo/croom.to fork. Status: approved in discussion, spec under review.

## 1. Goal

Give a Croom device a room screen: a web page, served by the agent itself, that anyone on the room's network can open to see today's meetings, join one (or join from a pasted link), and control the meeting while it runs. The same page later runs full screen on a touchscreen attached to the Pi. Target users: Google Workspace calendars and Zoom meetings, with the other platforms the agent already supports left enabled.

Success criteria:

1. With the agent running, `http://<device>:8080/` shows the room name, the clock, and a "join with a link" field. Pasting a Zoom link makes the room's browser join the meeting, and the page moves through joining, in meeting (with Mute, Camera and Leave), and back to idle after Leave.
2. When the calendar service is connected, today's meetings appear with a Join button on each one that carries a link for a configured platform; without a calendar the page says so and still works from a link.
3. The API is covered by tests against stub meeting and calendar services; the agent test proves the `control` service is registered with the real services attached.
4. Tests that pass on `main` today still pass, and the page ships with the package so a Pi installed from git gets it.

## 2. Background

- `MeetingService` (`src/croom/meeting/service.py`) already provides everything a control surface needs: `join_meeting(url, display_name, camera_on, mic_on)`, `leave_meeting()`, `toggle_mute()`, `toggle_camera()`, `state`, `current_meeting`, `get_available_platforms()` and `add_state_callback()`. Nothing in the agent calls them.
- `CalendarService` provides `events` (today's events, sorted), `next_meeting`, `get_current_meeting()`, `get_event_by_id()` and `on_events_updated()`. It runs idle until credentials exist (a later step).
- `src/croom/ui/web_interface.py` is an unused admin server whose meeting and calendar calls do not match those services, whose page assets do not exist, and which exposes reboot, shutdown and Wi-Fi endpoints. It is not reused and not touched.
- `src/croom-ui` (Qt) is a shell whose Join button is a stub. This page supersedes it for the touchscreen.

## 3. Scope

In scope: sections 4 and 5.

Out of scope: calendar credentials and auto-join, any PIN or settings pages, Zoom passcode entry for links without an embedded `pwd`, the kiosk launcher for the future touchscreen, the Qt touch UI, provider selector fixes on real hardware, and the pre-existing upstream test failures.

## 4. Design

### 4.1 Components and configuration

- New package `src/croom/control/`: `service.py` with `ControlService(Service)` named `control`, and `static/` with `index.html`, `style.css` and `app.js`. No build step and no external assets. `pyproject.toml` adds `control/static/*` to the package data so the files ship in the wheel.
- New config section `control` on `Config`: `enabled: bool = True`, `host: str = "0.0.0.0"`, `port: int = 8080`, parsed and serialised like the other sections.
- `ControlService.from_config(config, meeting, calendar)` builds the service from `Config` plus the meeting and calendar service instances (either may be `None`). Its dict constructor takes `host`, `port`, `room_name`, `room_location` and `static_dir`.
- `start()` creates the aiohttp application (also exposed as `create_app()` for tests), an `AppRunner` and a `TCPSite`; `stop()` cancels any running join task and shuts the site down. A port already in use is logged as an error and the service runs without a listener rather than aborting the agent, consistent with the failure policy of the other services.

### 4.2 Agent wiring

In `CroomAgent._initialize_services()`, after the dashboard client: when `control.enabled`, build the service with `self.service_manager.get_service("meeting")` and `get_service("calendar")` and register it with dependencies `["meeting", "calendar"]`, so it starts after both. Import errors are logged like the other optional services.

### 4.3 API contract

All responses are JSON. No authentication. Paths:

- `GET /` serves the page with `Cache-Control: no-cache`; `GET /static/{file}` serves the assets.
- `GET /api/status` returns:
  - `room`: `{name, location}`
  - `server_time`: ISO 8601 with offset
  - `platforms`: the meeting service's available platforms, `[]` when there is no meeting service
  - `meeting`: `{state, platform, meeting_id, title, url, joined_at, muted, camera_on, error}` where `state` is one of `idle`, `joining`, `in_lobby`, `connected`, `leaving`, `error` (`idle` without a meeting service); `platform`, `meeting_id`, `url`, `joined_at` and `error` are `null` when absent; `title` is the calendar event title for an event join and `""` otherwise.
  - `calendar`: `{connected, provider, current, next}` where `current` and `next` are event objects or `null`.
- `GET /api/calendar/events` returns `{"events": [...]}`: today's events by the device's local date, in start order (the calendar service caches a week), each limited to `id`, `title`, `start_time`, `end_time`, `meeting_platform` and `joinable` (true when the event has a `meeting_url` for an available platform). Descriptions, organizers, locations and links stay off the network. `[]` without a calendar service. `calendar.next` in the status is the first of today's events that has not started.
- `POST /api/meeting/join` with `{"url": "..."}` or `{"event_id": "..."}`:
  - `url` is trimmed; a value of 9 to 11 digits is treated as a Zoom meeting id and becomes `https://zoom.us/j/<id>`.
  - `event_id` is resolved through the calendar service; an unknown id or an event without a link is `400`.
  - A link without a scheme gets `https://`. Its hostname must be one of an available platform's domains (`zoom.us`, `zoomgov.com`, `meet.google.com`, `g.co`, `teams.microsoft.com`, `teams.live.com`, `webex.com`) or a subdomain of one; otherwise `400 {"error": "Unsupported meeting link"}`. Substring matches are not enough: `https://evil.example/?zoom.us` is refused.
  - When the meeting state is anything other than `idle` or `error`: `409 {"error": "A meeting is already in progress"}`.
  - No meeting service: `503`.
  - Otherwise the join starts in a background task and the response is `202 {"state": "joining", "url": "<normalized url>"}` immediately, because a join takes ten to thirty seconds and the page polls.
- `POST /api/meeting/leave`: `409` when `idle` with nothing to clear; `200 {"state": "idle"}` when it only dismisses a stored join error; otherwise the leave runs in a background task and the response is `202 {"state": "leaving"}`. Leave also clears an `error` state.
- `POST /api/meeting/mute` and `POST /api/meeting/camera`: `409` unless `connected`; otherwise call the toggle and return `200 {"muted": bool}` or `200 {"camera_on": bool}`.
- Every POST must carry `Content-Type: application/json`; anything else is `415`, which keeps a cross-site form on another web page from driving the room. Request bodies that are not JSON objects: `400`. Bodies are limited to 4 KiB.

### 4.4 Join flow and state

- The service keeps one background task for the current join or leave. A join calls `meeting.join_meeting(url, display_name=room name)`; an exception is logged and stored as the last error. The error field in the status is the stored message, or `current_meeting.error_message` when the meeting state is `error`; a new join or a leave clears it. When the provider refuses a link before it starts joining (the meeting state stays `idle`), the status reports `error` anyway so the page can show the message.
- `joined_at` is recorded from the meeting service's state callback when the state becomes `connected` and cleared when it returns to `idle`.
- `muted` and `camera_on` come from `current_meeting.is_muted` and `is_camera_on`; the providers update those on toggles.
- `title` and the event reference are kept by the control service for the current join only.

### 4.5 Page

One page, three states, polling `GET /api/status` every 2 seconds and `GET /api/calendar/events` every 30 seconds, with buttons disabled while a request is in flight and errors shown inline. Buttons and the schedule list are rebuilt only when their labels or availability change, so a click in progress is never lost while the timer ticks.

- Idle: room name and location, a clock, a headline card for the current meeting if one is running on the calendar, otherwise the next one, with a countdown and a Join button that is enabled from ten minutes before the start until the end; today's list below with a Join button per joinable event; a "join with a link" field that accepts a link or a Zoom meeting id. Without a calendar, a single quiet line says the calendar is not connected.
- Joining: the meeting title or platform, a progress message that follows `joining` and `in_lobby`, and a Cancel button that calls leave.
- In meeting: title or platform and id, elapsed time since `joined_at`, Mute, Camera and Leave. Leave asks for a second tap to confirm. An `error` state shows the message with a Dismiss (leave) button.
- Presentation: dark theme, large touch targets (at least 48 px), readable from a phone width up to a 1080p kiosk, system fonts, all styling and script inline in the package's files. The visual work follows the frontend-design skill during implementation.

### 4.6 Security

Open on the room network by design, like a wall-mounted touchscreen. No reboot, shutdown, Wi-Fi, settings or diagnostics endpoints. The room's browser can only be sent to links the meeting service's platforms recognise. The listener binds to `control.host`; set it to `127.0.0.1` to keep the page local.

### 4.7 Calendar service change

`CalendarService` gains a read-only `connected` property returning its initialised flag, so the page can say whether the calendar is live without touching private state.

## 5. Testing

Unit tests, offline, using `aiohttp.test_utils.TestClient` on `create_app()` with small stub services (plain classes, not mocks) that record calls:

- `tests/unit/control/test_service.py`: status shape in idle, joining, connected and error states; platforms and calendar fields with and without services; events with the `joinable` flag; join validation (missing body, unsupported link, unknown event, event without link), Zoom id normalisation, `202` then the stub's `join_meeting` called with the room name, `409` while in progress, `503` without a meeting service, error surfacing when the stub raises; leave `409` when idle and `202` otherwise; mute and camera `409` unless connected and the toggled values when connected; the page served with `200`, HTML content type and `no-cache`; `from_config` mapping; the service is a `Service` named `control`.
- `tests/unit/core/test_config.py`: `control` section defaults and round trip.
- `tests/unit/core/test_agent.py`: `control` is registered when enabled with the meeting and calendar services attached, and absent when disabled.
- `tests/unit/calendar/test_service.py`: `connected` reflects initialisation.

Acceptance on this machine: run the agent, open `http://localhost:8080/` in the Windows browser, paste a Zoom link for a meeting you host elsewhere, watch the room's Chromium window join and the page follow it through joining and in meeting, use Mute, then Leave. Phones on the LAN cannot reach WSL2 without Windows port forwarding; that limitation does not exist on the Pi.

## 6. Files

- `src/croom/control/__init__.py`, `src/croom/control/service.py`, `src/croom/control/static/index.html`, `src/croom/control/static/style.css`, `src/croom/control/static/app.js`
- `src/croom/core/config.py`: `ControlConfig`
- `src/croom/core/agent.py`: register `control`
- `src/croom/calendar/service.py`: `connected`
- `pyproject.toml`: package data
- tests listed in section 5
- this document

## 7. Notes and risks

- Zoom links with an embedded `pwd` join directly; a meeting that requires typing a passcode stops at the web client's prompt until provider support exists.
- Google Meet joins as a guest and waits to be admitted; a signed-in room account is not possible in the automated browser.
- The providers' in-meeting button selectors have never run against the live web clients; Mute and Camera may need selector fixes on real hardware.
- The old `web_interface.py` stays in the tree unused; removing it is a later cleanup.
