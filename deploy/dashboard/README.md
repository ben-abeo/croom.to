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

Day to day, from `/opt/croom-dashboard/deploy/dashboard`:

    docker compose ps                    # both services should be Up (healthy)
    docker compose logs -f dashboard     # the backend's log
    sudo bash ../../installer/install-dashboard.sh   # update: pull, rebuild, restart

Backups: `croom-dashboard-backup.timer` runs `backup.sh` nightly at 02:30 and
keeps 14 days of `croom-dashboard-YYYY-MM-DD.sql.gz` in
`/var/backups/croom-dashboard`. Copy a file off the Pi with `scp`. Restore one
onto a running stack (it drops and recreates the tables):

    docker compose stop dashboard
    gunzip -c /var/backups/croom-dashboard/croom-dashboard-2026-10-05.sql.gz | docker compose exec -T db psql -U croom -d croom
    docker compose start dashboard

Forgotten admin password: delete the user, and the next start recreates it from
`.env`:

    docker compose exec db psql -U croom -d croom -c "DELETE FROM users WHERE email = 'you@crystalpm.com';"
    docker compose restart dashboard

There are no database migrations: the backend creates missing tables at start
and never alters existing ones. Nothing here holds a real secret; `.env` is
gitignored.
