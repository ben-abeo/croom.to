# Crystal Meet room devices

One device per conference room, one config per device, one shared dashboard,
one Google service account shared with all three room calendars.

Each `room-N.yaml` is a complete agent config with four things to replace
before installing it on that room's device:

- `room.name` and `room.location`: what people call the room.
- `dashboard.url`: the dashboard backend's address on the office network, for
  example `http://192.168.1.20:3001`. If the dashboard runs under WSL2 on a
  Windows PC, enable mirrored networking or forward ports 3000 and 3001 first.
- `dashboard.enrollment_token`: create it on the dashboard's Provisioning page
  for that room and paste it in. A token works once; if you reinstall, create a
  new one.
- `calendar.google_calendar_id`: the room's Google Calendar address, which looks
  like `c_1885...@resource.calendar.google.com` (the resource email in the
  Admin console). The guide "Connect Crystal Meet rooms to Google Calendar"
  covers creating the rooms, the service account key and sharing.

Install on the device with the room config and the service account key:

    sudo bash installer/install.sh --config /path/to/room-N.yaml --credentials /path/to/google-service-account.json

The key is copied to `/etc/croom/google-service-account.json`, readable only by
the service user. Without `--credentials` the room works with pasted links only
and logs one line saying the calendar is not configured.

The room page is then at `http://<device>:8080/` for anyone on the network,
and the door sign at `http://<device>:8080/sign`. Check the calendar with
`/opt/croom/venv/bin/croom --check-calendar -c /etc/croom/config.yaml`.
Never commit a real token or key to this folder.
