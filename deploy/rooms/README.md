# Crystal Meet room devices

One device per conference room, one config per device, one shared dashboard,
one Google service account shared with all three room calendars.

Each `room-N.yaml` is a complete agent config with three values to replace
before installing it on that room's device (`dashboard.url`,
`dashboard.enrollment_token` and `calendar.google_calendar_id`), an optional
location, and optional `tv:` defaults. Select an LG or Samsung TV on the table to set it up:

- `room.name` and `room.location`: what people call the room and where it is. The
  location is optional: leave the placeholder or delete the line and the pages
  show the room's name alone.
- `dashboard.url`: the dashboard's address on port 3001, which is the dashboard
  Pi's reserved address, for example `http://10.0.0.50:3001`. The guide "Set up
  the Crystal Meet dashboard" installs it and reserves the address.
- `dashboard.enrollment_token`: create it on the dashboard's Provisioning page
  for that room and paste it in. A token works once; if you reinstall, create a
  new one.
- `calendar.google_calendar_id`: the room's Google Calendar address, which looks
  like `c_1885...@resource.calendar.google.com` (the resource email in the
  Admin console). The guide "Connect Crystal Meet rooms to Google Calendar"
  covers creating the rooms, the service account key and sharing.
- `meeting.google_profile_dir`: where the room keeps its signed-in Google Meet
  session, `/var/lib/croom/meet-profile`. Leave it; the guide "Let Crystal Meet
  rooms join Google Meet" covers the room's Workspace user and the one-time
  `croom --sign-in-meet` on the device.
- `tv`: optional defaults for the room's LG or Samsung TV. To set one up, open
  **Options > TV > Find TVs** on the table, select it, and accept the request on
  the TV. The Pi saves the brand, address and discovered MAC in `tv-settings.json`
  under its data directory and restores them on restart; no YAML edit is needed.
  If discovery cannot obtain the MAC, enter it in the table's connection settings
  to enable waking the TV. Set the Pi's HDMI socket there too if it is not HDMI 1.
  Existing manual configurations work as before; `brand` defaults to `lg`, or
  can be `samsung`. A table selection overrides the YAML connection defaults.
  `croom --check-tv` uses the saved selection too.

Zoom needs one more file per room, `zoom-credentials.json`, made from
`zoom-credentials.example.json`: the Meeting SDK app's client id and secret
(the same for every room), and, to join meetings hosted by other Zoom accounts,
the Server-to-Server app's account id, client id and secret plus that room's own
Zoom user (`room_user`, for example `room1@crystalpm.com`). The guide "Connect
Crystal Meet rooms to Zoom" covers creating all of it. Joining meetings hosted by
other Zoom accounts is not yet verified against a live meeting; see the guide's step 5.

Install on the device with the room config and both credential files:

    sudo bash installer/install.sh --config /path/to/room-N.yaml --credentials /path/to/google-service-account.json --zoom-credentials /path/to/zoom-credentials.json

The files are copied to `/etc/croom/google-service-account.json` and
`/etc/croom/zoom-credentials.json`, readable only by the service user. Without
`--credentials` the room works with pasted links only and logs one line saying
the calendar is not configured; without `--zoom-credentials` Zoom joins fall
back to the public web client, which Zoom blocks for automated guests.

The room page is then at `http://<device>:8080/` for anyone on the network,
and the door sign at `http://<device>:8080/sign` (on an SMT1019 wall panel, the app in
`android/door-sign` shows it and colours the panel's light bar from it). Check the calendar with
`/opt/croom/venv/bin/croom --check-calendar -c /etc/croom/config.yaml`, Zoom
with `/opt/croom/venv/bin/croom --check-zoom -c /etc/croom/config.yaml` and the TV
(once it is set up) with
`/opt/croom/venv/bin/croom --check-tv -c /etc/croom/config.yaml`, each run as the
desktop user the service runs as (the TV's key is private to that user).
Never commit a real token, key or secret to this folder.
