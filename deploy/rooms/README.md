# Crystal Meet room devices

One device per conference room, one config per device, one shared dashboard,
one Google service account shared with all three room calendars.

Each `room-N.yaml` is a complete agent config with three values to replace
before installing it on that room's device (`dashboard.url`,
`dashboard.enrollment_token` and `calendar.google_calendar_id`), an optional
location, and a `tv:` block that is switched off until the room's LG TV is set up:

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
- `tv`: the room's LG TV, switched off (`enabled: false`) until it is set up, with
  its other keys commented under it. Leave it as it is for a room with no LG TV.
  For one with an LG TV, the room guide's section The TV says how to switch it on:
  set `enabled` to true and put that TV's own address and MAC address in this one
  block (a second `tv:` in the file would silently win), then run `croom --check-tv`.

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
