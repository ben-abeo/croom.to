"""
The root helper, croom-admin (spec docs/superpowers/specs/2026-10-29-room-settings-design.md, section 5).

A systemd service running as root on the TV Pi, listening on /run/croom-admin.sock (group the room's, mode 660): one JSON request per
connection, one JSON answer. It does a fixed list of jobs (`croom.admin.jobs`) and never runs a shell. The room page's Settings reach it
through `croom.admin.client`, behind the PIN; a machine without it (a developer's) shows those pages as "Not available on this device".
"""
