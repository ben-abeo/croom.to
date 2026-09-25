# Crystal Meet room devices

One device per conference room, one config per device, one shared dashboard.

Each `room-N.yaml` is a complete agent config with three things to replace
before installing it on that room's device:

- `room.name` and `room.location`: what people call the room.
- `dashboard.url`: the dashboard backend's address on the office network, for
  example `http://192.168.1.20:3001`. If the dashboard runs under WSL2 on a
  Windows PC, enable mirrored networking or forward ports 3000 and 3001 first.
- `dashboard.enrollment_token`: create it on the dashboard's Provisioning page
  for that room and paste it in. A token works once; if you reinstall, create a
  new one.

Install on the device with:

    sudo bash installer/install.sh --config /path/to/room-N.yaml

The room page is then at `http://<device>:8080/` for anyone on the network,
and the door sign at `http://<device>:8080/sign`.
Never commit a real token to this folder.
