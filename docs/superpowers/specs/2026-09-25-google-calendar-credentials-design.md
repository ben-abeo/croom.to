# Google Calendar credentials for Crystal Meet rooms

Date: 2026-09-25. Status: approved by Ben in conversation; written for the implementation plan.

## 1. Goal

Each Crystal Meet room device reads its own room's Google Workspace calendar without any
browser sign-in, so the room page and the door sign show real bookings and the existing
"Join now" button joins the booking's Zoom or Google Meet link. Nothing joins or leaves by
itself: Ben chose "only when someone presses Join".

Success looks like: a booking made in Google Calendar with the room added appears on the
room page and the sign within a minute, "Join now" opens ten minutes before it starts (the
page's existing window), and pressing it joins the booking's link on the TV.

## 2. Where things stand

- `croom.calendar.CalendarService` already polls a provider every `sync_interval_seconds`
  and exposes `events`, `next_meeting`, `get_current_meeting`, `get_event_by_id`,
  `connected`. The control page and the sign consume those. This has never run against a
  real calendar.
- `GoogleCalendarProvider` supports a service account key (`service_account_file`) and
  parses events, but: it labels every conference entry as Google Meet (Zoom add-on
  bookings come through `conferenceData` too), it runs the blocking Google client on the
  event loop, and the Google libraries are optional imports that are not installed.
- `CalendarService.from_config` passes only the key path. With no calendar id the service
  falls back to the service account's own primary calendar, which is empty, so the room
  would show nothing and log nothing useful.
- Bookings the room declined (double bookings) are not filtered; cancelled ones are.
- The three room configs ship with `calendar.providers: []`.
- The installer has `--config FILE` but no way to place a credentials file.

## 3. Decisions taken with Ben

- Rooms are not in Google Calendar yet: the guide starts with creating Workspace room
  resources.
- Credentials: one Google Cloud service account with a JSON key; each room's resource
  calendar is shared with the service account's email ("See all event details"). No
  domain-wide delegation, no OAuth sign-in on devices.
- No automatic joining or leaving.

## 4. Design

### 4.1 Google side (documented, done once)

1. Admin console, Buildings and resources: add a building and the three rooms. Each room
   gets a resource calendar whose address looks like
   `c_1885…@resource.calendar.google.com`. Staff book a room by adding it to the event.
2. Google Cloud console: one project, enable the Google Calendar API, create a service
   account, create a JSON key. The service account needs no roles.
3. Google Calendar (as an admin who can manage the resource calendars): share each room
   calendar with the service account's email, permission "See all event details".
4. Meeting links: Google Meet comes from the event's conference data; Zoom comes from the
   Zoom for Google Workspace add-on (also conference data) or from a link typed into the
   location or description.

### 4.2 Configuration and the credentials file on each device

- Key file: `/etc/croom/google-service-account.json`, owned by the service user, mode 600.
- `CalendarConfig` gains `google_calendar_id: str = ""`. The room configs become:

  ```yaml
  calendar:
    providers: [google]
    google_credentials_path: /etc/croom/google-service-account.json
    google_calendar_id: "REPLACE_WITH_ROOM_CALENDAR_ID"
    sync_interval_seconds: 60
  ```

- "Not configured" rule. When the provider is `google` and any of these holds, the calendar
  service does not start polling, `connected` stays false, and exactly one WARNING line
  says why ("Google Calendar not configured: <reason>"): no key path; key file missing or
  unreadable; empty calendar id; calendar id still a `REPLACE_` placeholder. The room page
  and the sign then behave as they do today without a calendar. This replaces the silent
  primary-calendar fallback: with a calendar id configured, only that calendar is read.
- `deploy/rooms/README.md` lists the fourth value to replace and the credentials file.

### 4.3 Agent changes

- `CalendarService.from_config`: pass `calendar_ids=[google_calendar_id]`; compute the
  not-configured reason (section 4.2) and hand it to the service, which logs it once in
  `initialize()` and returns False.
- `CalendarService._fetch_events`: skip events with `status == "cancelled"` or
  `response_status == "declined"` (the room resource declined the booking).
- `GoogleCalendarProvider`:
  - `authenticate`, `get_calendars` and `get_events` run the Google client calls through
    `asyncio.to_thread`, so a slow poll never stalls the room page or the sign.
  - Platform detection: for each video entry point in `conferenceData`, the platform comes
    from `detect_meeting_platform(uri)`, not a fixed Google Meet label. A link typed into
    the location or description wins over an automatically added Meet conference: if the
    conference is Meet and the location or description holds a Zoom link, the Zoom link is
    the event's link. A Zoom conference (add-on) is used as is.
  - Anything else unchanged.
- Dependencies in `pyproject.toml` `[project].dependencies`: `google-api-python-client`,
  `google-auth`, `google-auth-httplib2`, so `pip install` from the fork ships them.

### 4.4 The check command

`croom --check-calendar [-c CONFIG]` is a diagnostic for the person installing a room. It
loads the config, applies the not-configured rule, authenticates, reads the configured
calendar and prints plain words, then exits 0 on success or 1 on any failure:

```
Google Calendar: connected as crystal-meet@my-project.iam.gserviceaccount.com
Calendar: Room 1 (c_1885…@resource.calendar.google.com)
Bookings in the next 7 days:
  Thu Sep 25  9:00 AM to 9:30 AM   Design review          Zoom link
  Thu Sep 25  1:00 PM to 2:00 PM   Board lunch            no video link
```

or `No bookings in the next 7 days.` Failures print one line each with what to do:

- not configured (each reason from 4.2, for example "calendar id is still the placeholder;
  put the room's calendar address in /etc/croom/config.yaml");
- key rejected ("the key file is not a service account key, or the project's Calendar API
  is not enabled");
- calendar not found or 403 ("share the room calendar with <service account email>"; the
  email is read from the key file's `client_email`).

Implementation: `croom.calendar.check` with `async def check_calendar(config: Config,
out=sys.stdout, provider_factory=None) -> int`; `croom.core.agent.main` adds the flag and
runs it instead of the agent. The service account email is read from the key file.

### 4.5 Installer

- `--credentials FILE`: validated at parse time like `--config` (missing file refused
  before anything is installed). `install_credentials()` copies it to
  `$CONFIG_DIR/google-service-account.json`, owner `$CROOM_USER`, mode 600.
- Completion message: when credentials were installed, add the line
  `Check the calendar: /opt/croom/venv/bin/croom --check-calendar -c /etc/croom/config.yaml`.
- `--help` documents the option.

### 4.6 Guide

- New how-to in the Crystal PM brand: `docs/guides/crystal-meet-google-calendar/index.html`
  rendered to `docs/guides/crystal-meet-google-calendar.pdf`. Sections: at a glance; before
  you begin; step 1 create the rooms (Admin console); step 2 create the service account and
  key (Cloud console); step 3 share each room calendar with it; step 4 put the key and the
  calendar address on the device (installer `--credentials`, the config value, restart, the
  check command); step 5 book a test meeting and see it on the room page and the sign;
  troubleshooting (calendar not shared, wrong address, key from another project, API not
  enabled, bookings without the room added, Zoom links without a passcode).
- Both guides render through one shared renderer, `docs/guides/render_guide.py`
  (`render(folder, out, footer_title)`); each guide's `build.py` calls it. The calendar
  guide carries its own copies of the logo, the two Lexend files and OFL.txt so it stays
  self-contained.
- The setup guide's step 4 gains one sentence pointing at the calendar guide, and its PDF is
  rebuilt.

## 5. Testing

- Google provider: `_parse_event` and `get_events` against recorded `events.list` item
  shapes kept in the test module: a Meet booking (conference data), a Zoom add-on booking
  (conference data with a zoom.us entry point), a Meet conference plus a Zoom link in the
  description (Zoom wins), a pasted Zoom link with no conference data, an all-day event, a
  booking the room declined. `get_events` runs with a fake Google service object; the
  to_thread path is exercised.
- Calendar service: `from_config` passes the calendar id; each not-configured reason
  produces one warning and `connected == False`; declined and cancelled events are
  dropped; everything else flows to `events` as before.
- Check command: with an injected fake provider, the success output lines; each failure
  reason and its exit code; the placeholder case without any network.
- Config: `google_calendar_id` parses from YAML and round-trips through `to_dict`.
- Installer: `--credentials` with a missing file refused before install; `install_credentials`
  copies with owner and mode 600 (sourced with `CONFIG_DIR` in a temp dir); `--help` names it.
- Deploy: `test_room_configs_load` now expects `providers == ["google"]`, the key path and
  the placeholder id.
- Docs: both guides build to multi-page PDFs and are self-contained; the setup guide
  mentions the calendar guide.
- Suite gate as before (87 pre-existing upstream failures, none new).
- Acceptance, needs Ben: with the real key and a shared room calendar, run the check
  command on this PC, then run the agent and see the booking on the room page and the sign,
  then press Join now.

## 6. Files

| File | Responsibility |
|---|---|
| `src/croom/core/config.py` | `CalendarConfig.google_calendar_id`. |
| `src/croom/calendar/service.py` | Calendar id wiring, not-configured rule, declined filter. |
| `src/croom/calendar/providers/google.py` | Thread offload, platform from the link, typed link wins over auto Meet. |
| `src/croom/calendar/check.py` | The check command. |
| `src/croom/core/agent.py` | `--check-calendar` flag. |
| `pyproject.toml` | Google client dependencies. |
| `installer/install.sh` | `--credentials`, `install_credentials`, completion line. |
| `deploy/rooms/room-{1,2,3}.yaml`, `deploy/rooms/README.md` | Calendar section with placeholders. |
| `docs/guides/render_guide.py`, `docs/guides/crystal-meet-google-calendar/*`, `docs/guides/crystal-meet-google-calendar.pdf` | The calendar guide and the shared renderer. |
| `docs/guides/crystal-meet-room-setup/{build.py,index.html}`, `docs/guides/crystal-meet-room-setup.pdf` | Uses the shared renderer; pointer to the calendar guide. |
| `tests/unit/calendar/…`, `tests/unit/installer/test_install_script.py`, `tests/unit/deploy/test_room_configs.py`, `tests/unit/docs/…` | Tests above. |

## 7. Out of scope

Automatic joining or leaving; domain-wide delegation; Microsoft 365; credentials managed
from the dashboard; occupancy detection; hiding booking titles on the door sign; changing
the ten-minute join window.
