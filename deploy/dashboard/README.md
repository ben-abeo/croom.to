# Crystal Meet dashboard under Docker Compose

What runs on the dashboard host (a fourth Raspberry Pi, or any 64-bit Docker
host): `docker-compose.yml` starts Postgres 16 and the dashboard image built
from this repo by `Dockerfile`. The dashboard serves the web app, the API and
the devices' WebSocket on port 3001; port 80 is published to the same port so
people can open it without a port number.

`installer/install-dashboard.sh` does the install and every later update:

    sudo bash installer/install-dashboard.sh --admin-email you@crystalpm.com

It writes `.env` next to the compose file on the first run (mode 600; the keys
are listed in `.env.example`), never changes it afterwards, builds and starts
the stack, and installs a nightly backup timer. The first admin comes from
`ADMIN_EMAIL` and `ADMIN_PASSWORD`; the account is created only when it does
not exist, so a password changed on the Settings page sticks.

Day to day, from `/opt/croom-dashboard/deploy/dashboard`. The settings file is
readable by root only, so every compose command below starts with `sudo`:

    sudo docker compose ps                    # both services should be Up (healthy)
    sudo docker compose logs -f dashboard     # the backend's log
    sudo bash ../../installer/install-dashboard.sh   # update: pull, rebuild, restart

Backups: `croom-dashboard-backup.timer` runs `backup.sh` nightly at 02:30 and
keeps 14 days of `croom-dashboard-YYYY-MM-DD.sql.gz` in
`/var/backups/croom-dashboard`, readable by root only. Take one right now with
`sudo systemctl start croom-dashboard-backup.service`. To copy a file off the
Pi, first make a copy you own, then `scp` that from your computer:

    sudo cp /var/backups/croom-dashboard/croom-dashboard-2026-10-05.sql.gz ~ && sudo chown "$USER" ~/croom-dashboard-2026-10-05.sql.gz

Restore one onto a running stack (it drops and recreates the tables and stops
at the first error):

    sudo docker compose stop dashboard
    sudo gunzip -c /var/backups/croom-dashboard/croom-dashboard-2026-10-05.sql.gz | sudo docker compose exec -T db psql -v ON_ERROR_STOP=1 -U croom -d croom
    sudo docker compose start dashboard

Forgotten admin password: delete the user, and the next start recreates it from
`.env`:

    sudo docker compose exec db psql -U croom -d croom -c "DELETE FROM users WHERE email = 'you@crystalpm.com';"
    sudo docker compose restart dashboard

There are no database migrations: the backend creates missing tables at start
and never alters existing ones. Nothing here holds a real secret; `.env` is
gitignored.
