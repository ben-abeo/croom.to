# The Crystal Meet dashboard on a Raspberry Pi

Date: 2026-10-05. Status: approved by Ben in conversation; written for the implementation plan.

## 1. Goal

One always-on Crystal Meet dashboard for the three rooms, hosted on a fourth Raspberry Pi
(Pi 4 or Pi 5 with 4 GB or more, Raspberry Pi OS Lite 64-bit) on the office network. It is
installed with one command, reachable at a fixed address the room configs point at, comes
back on its own after a reboot or power cut, is backed up nightly, and is updated by running
the same command again. A fourth guide lets someone else repeat the install.

Success looks like: a fresh Pi becomes a working dashboard in about fifteen minutes; the
admin signs in at `http://crystal-meet.local` (or the Pi's address) and creates one token per
room; the room devices show Online with their heartbeats; pulling the Pi's power and plugging
it back brings the dashboard up without anyone touching it; a dated backup file appears every
night; re-running the installer after a code update rebuilds the dashboard with every device,
user and token still there.

## 2. Where things stand

- The dashboard is a Node/Express backend (REST plus a WebSocket on one port, 3001), a
  React/Vite web app, and Postgres through Sequelize. It only runs in development: two
  `npm run dev` processes and a Postgres container on Ben's WSL PC, which WSL's NAT keeps off
  the office network. The upstream README mentions `docker-compose up -d`, but no compose
  file or Dockerfile exists.
- Production gaps in the backend: `initDatabase` creates or alters tables only when
  `NODE_ENV` is not `production` and expects migrations in production, but there are none;
  no admin account is ever created (the development admin was made by calling
  `POST /api/auth/register`, which is open to anyone and accepts any role); the backend
  does not serve the built web app (Vite's dev server proxies `/api` and `/ws` for it);
  `helmet()`'s default content security policy includes `upgrade-insecure-requests`, which
  makes browsers fetch a plain-HTTP site's assets over HTTPS once the backend serves HTML;
  production logging writes `error.log` and `combined.log` into the working directory;
  `BASE_URL` and `WS_URL` only feed the links the Provisioning page shows, because the device
  derives its WebSocket address from its own `dashboard.url`.
- The web app calls `/api` with relative paths and opens no WebSocket, so it can be served
  from the backend's own origin unchanged. Its Settings page has no change-password form,
  although the backend has `POST /api/auth/change-password`.
- The room installer (`installer/install.sh`) is bash with one function per step and a
  `run_installer "$@"` guard so tests can source it; the guides are rendered by
  `docs/guides/render_guide.py`; the room configs carry `dashboard.url` as
  `http://REPLACE_WITH_DASHBOARD_ADDRESS:3001`.
- The room agent reconnects to the dashboard with a backoff capped at a minute and joins
  meetings without it, so the dashboard is outside the rooms' critical path except at first
  enrollment.

## 3. Decisions taken with Ben

- The dashboard runs on the fourth Pi, not on a room Pi: a room Pi is the busiest machine
  during a call, the most likely to be unplugged or reinstalled, and reinstalling it would
  wipe the dashboard's data. An office VM can take over later with the same compose file.
- Packaging is Docker Compose with images built on the Pi from the repo. No registry, no CI
  workflow: three rooms do not justify them.
- Everything is served on one port, 3001, which the room configs already use; port 80 is
  published as well so people can type `http://crystal-meet.local` without a port.
- Secrets are generated on the Pi by the installer and never live in the repo; the repo
  carries an example file with placeholders, like the room configs.
- Backups are a nightly `pg_dump` kept for 14 days on the Pi. Copying them elsewhere is a
  documented manual step, not automated.
- The Pi gets the hostname `crystal-meet` (so `crystal-meet.local` works on the same
  network through mDNS) and a DHCP reservation in UniFi; room configs use the reserved
  address.
- Plain HTTP on the LAN. No TLS in this round.
- Docker Compose was chosen over a native systemd install (fewest layers, but tied to the Pi)
  and over prebuilt images from GitHub (fastest install, but a public registry and a
  workflow to maintain).

## 4. Design

### 4.1 Files

- `deploy/dashboard/docker-compose.yml`, `Dockerfile`, `.env.example`, `README.md`,
  `backup.sh`, `croom-dashboard-backup.service`, `croom-dashboard-backup.timer`.
- `.dockerignore` at the repo root (the build context is the repo root), keeping the context
  to the two dashboard packages without their `node_modules` and `dist`.
- `installer/install-dashboard.sh`.
- Backend: `src/croom-dashboard/backend/src/index.ts`, `models/index.ts`, `routes/auth.ts`,
  `services/logger.ts`, new `bootstrap.ts` (first admin), new `webapp.ts` (static site),
  new `security.ts` (helmet options), `jest.config.js`, tests under `src/__tests__/`.
- Web app: a change-password form on the Settings page, calling the existing route.
- `docs/guides/crystal-meet-dashboard/` (`index.html`, `build.py`, logo, fonts) and
  `docs/guides/crystal-meet-dashboard.pdf`.
- Edits: `README.md`, `deploy/rooms/README.md`, `docs/guides/crystal-meet-room-setup/index.html`
  and its PDF, `.gitignore`.
- Tests: `tests/unit/installer/test_install_dashboard_script.py`,
  `tests/unit/deploy/test_dashboard_compose.py`, `tests/unit/docs/test_dashboard_guide.py`,
  updates to `tests/unit/docs/test_readme.py` and `test_room_setup_guide.py`.

### 4.2 Compose file and image

Two services and one named volume, project name `croom-dashboard`:

- `db`: `postgres:16-alpine`, `restart: unless-stopped`, data in the named volume `pgdata`,
  no published ports, `POSTGRES_DB`/`POSTGRES_USER`/`POSTGRES_PASSWORD` from the env file,
  a `pg_isready` health check every ten seconds.
- `dashboard`: built from the repo root with `deploy/dashboard/Dockerfile`, tagged
  `croom-dashboard:local`, `restart: unless-stopped`, `env_file: .env`, fixed environment
  `NODE_ENV=production`, `HOST=0.0.0.0`, `PORT=3001`, `DB_HOST=db`, `DB_PORT=5432`,
  `STATIC_DIR=/app/public`; ports `${DASHBOARD_PORT:-3001}:3001` and
  `${DASHBOARD_HTTP_PORT:-80}:3001` (the variables exist so the stack can run on spare ports
  on a development machine); `depends_on: db` with `condition: service_healthy`; Docker's
  json-file logging capped at three files of 10 MB; a health check that fetches
  `http://127.0.0.1:3001/health` with Node itself, since the slim image has no curl.

The Dockerfile has three stages on `node:20-bookworm-slim`:

1. `frontend-build`: copy the frontend package files, `npm ci`, copy the sources,
   `npm run build` (tsc and vite) into `dist`.
2. `backend-build`: install `python3`, `make` and `g++` so `bcrypt` can compile when no
   prebuilt arm64 binary is available, copy the backend package files, `npm ci`, copy the
   sources, `npm run build` (tsc), then `npm prune --omit=dev`.
3. runtime: `/app` with the backend's `dist`, pruned `node_modules` and `package.json`, the
   web app at `/app/public`, `USER node`, `EXPOSE 3001`, `CMD ["node", "dist/index.js"]`.

### 4.3 The env file

The installer writes `deploy/dashboard/.env` on the Pi (owner root, mode 600) on the first
run and never touches it again:

```
DB_NAME=croom
DB_USER=croom
DB_PASSWORD=<32 random hex characters>
JWT_SECRET=<64 random hex characters>
ADMIN_EMAIL=<from --admin-email>
ADMIN_PASSWORD=<16 random characters>
BASE_URL=http://<address>:3001
WS_URL=ws://<address>:3001
LOG_LEVEL=info
```

`.env.example` carries the same keys with `REPLACE_WITH_...` placeholders, and `.gitignore`
lists `deploy/dashboard/.env`. `CORS_ORIGIN` stays unset: the web app is served from the
API's own origin and the devices are not browsers.

### 4.4 Backend changes

- `initDatabase` runs `sequelize.sync()` in production, which creates missing tables and
  never alters existing ones, and keeps `sync({ alter: true })` in development. The README
  states that there are no migrations yet.
- `bootstrap.ts` exports `ensureAdmin(users, env)`: when `ADMIN_EMAIL` and `ADMIN_PASSWORD`
  are both set and no user with that email exists, create it with role `admin`, name
  "Administrator", bcrypt hash at 12 rounds, and log "Created admin user <email>". When the
  user exists, do nothing, so a password changed in the app survives restarts. When either
  variable is unset and the users table is empty, log a warning that nobody can sign in.
  `main()` calls it after `initDatabase`.
- `POST /api/auth/register` requires a signed-in admin (`authMiddleware`, `requireRole('admin')`).
- `webapp.ts` exports `serveWebApp(app, dir)`: `express.static(dir)` plus a GET fallback that
  sends `index.html` for requests that accept HTML and do not start with `/api`, `/ws` or
  `/health`; other requests fall through to the JSON 404. `main()` calls it when `STATIC_DIR`
  is set and the directory exists, after the API routes and before the error handler, and
  logs the directory.
- `security.ts` exports `helmetOptions()`: helmet's default directives without
  `upgrade-insecure-requests`. `main()` passes them to `helmet()`.
- `logger.ts` adds the two file transports only when `LOG_DIR` is set, writing under it;
  otherwise everything goes to standard output, which Docker collects.
- The Settings page gets a change-password form (current password, new password twice)
  calling `POST /api/auth/change-password`, with the result shown inline.

Nothing changes in the WebSocket protocol, enrollment, or the device-facing API.

### 4.5 The installer

`sudo bash installer/install-dashboard.sh --admin-email you@crystalpm.com [--address HOST]`.
Options: `--admin-email` (required on the first run, when no env file exists; ignored with a
note afterwards), `--address` (the address written into `BASE_URL` and `WS_URL` and printed
at the end; default: the Pi's first IPv4 address from `hostname -I`), `--help`. Environment
overrides: `CROOM_REPO` (default `https://github.com/ben-abeo/croom.to.git`),
`CROOM_BRANCH` (`main`), `INSTALL_DIR` (`/opt/croom-dashboard`), `BACKUP_DIR`
(`/var/backups/croom-dashboard`), `SYSTEMD_DIR` (for tests), `MEMORY_KB` (for tests;
default read from `/proc/meminfo`).

One function per step, run by `main` only when executed, not when sourced:

1. `check_root`: refuse without root.
2. `check_platform`: `uname -m` must be `aarch64` or `x86_64` (32-bit Pi OS is refused with
   the reason); `/etc/os-release` must be Debian or a derivative (apt); below 2 GB of memory
   refuse, below 4 GB warn that the image build takes longer.
3. `install_docker`: skip when `docker compose version` already works; otherwise install
   `ca-certificates`, `curl` and `git`, add Docker's apt key and repository for the OS
   codename (Raspberry Pi OS reports Debian's codename), install `docker-ce`,
   `docker-ce-cli`, `containerd.io` and `docker-compose-plugin`, enable the service, and add
   the invoking user to the `docker` group.
4. `fetch_source`: clone `CROOM_REPO` at `CROOM_BRANCH` into `INSTALL_DIR`, or in an
   existing clone `git fetch`, check out the branch and `git pull --ff-only`.
5. `write_env`: on the first run refuse without `--admin-email`, generate the secrets with
   `openssl rand`, write the file with mode 600 owned by root; on later runs keep it and say
   so.
6. `start_stack`: `docker compose up -d --build` in `deploy/dashboard`, then poll
   `http://127.0.0.1:3001/health` for up to three minutes and fail with the last lines of
   `docker compose logs dashboard` if it never answers.
7. `install_backup`: copy `backup.sh` to `/usr/local/bin/croom-dashboard-backup`, write the
   service and timer units into `SYSTEMD_DIR` (daily at 02:30, `Persistent=true`), reload
   systemd, enable and start the timer.
8. `print_completion`: the addresses (`http://<address>`, `http://<address>:3001`,
   `http://<hostname>.local`), the admin email, the generated password on the first run only,
   the next steps (reserve the address in UniFi, sign in, change the password, create one
   token per room on Provisioning, put `http://<address>:3001` in each room config), where
   the backups are, how to see logs (`docker compose logs -f` in the deploy folder), and
   that running the same command again updates the dashboard.

`backup.sh` reads `DB_USER` and `DB_NAME` from the env file, runs
`docker compose exec -T db pg_dump --clean --if-exists` into
`BACKUP_DIR/croom-dashboard-YYYY-MM-DD.sql.gz` (mode 600) and deletes files older than 14
days. Restore, documented in the guide and the deploy README: stop the `dashboard`
container, pipe the file through `gunzip` into `docker compose exec -T db psql`, start the
container.

Updating is the same command: `fetch_source` pulls, `write_env` keeps the file,
`start_stack` rebuilds and restarts, the volume keeps the data.

### 4.6 The guide

`docs/guides/crystal-meet-dashboard/` renders "Set up the Crystal Meet dashboard" in the
same style as the other guides:

1. Prepare the Pi: Raspberry Pi Imager, Raspberry Pi OS Lite 64-bit, hostname
   `crystal-meet`, a username and password, SSH on, wired Ethernet; an SSD is nicer than an
   SD card for a database but not required.
2. Reserve its address: find it with `hostname -I`, give it a fixed address in UniFi (Client
   Devices, the Pi, Fixed IP) or in whatever router the office uses.
3. Install: SSH in, `sudo apt install -y git`, clone the fork, run the installer with the
   admin email; what it prints and how long it takes; what the password line means.
4. Sign in at `http://crystal-meet.local` or the address, change the password on Settings.
5. Create one token per room on Provisioning.
6. Point the rooms at it: `dashboard.url` in each `room-N.yaml` before installing a room; for
   an installed room, edit `/etc/croom/config.yaml` and `sudo systemctl restart croom`.
7. Backups and updates: where the nightly files are, how to copy one off the Pi, the restore
   command, the update command, logs.
8. Troubleshooting: the installer stops at Docker (no internet or apt broken); the build runs
   out of memory (a 2 GB Pi; add swap or use a 4 GB one); the page is blank (check
   `docker compose logs dashboard`); rooms show Offline (curl the health URL from a room; the
   address in the room config); forgotten admin password (the documented `psql` one-liner that
   deletes the admin user, after which the next restart recreates it from the env file).

### 4.7 README and existing docs

- README's Crystal Meet section: "Follow the four guides in this order", the dashboard guide
  first because rooms enroll into it; the dashboard paragraph describes production on the Pi
  (the install command, the deploy folder, the volume, the backups) and keeps the development
  commands; the implementation-notes table gets this spec and plan; known limitations add
  plain HTTP on the LAN and the absence of migrations.
- `deploy/rooms/README.md`: `dashboard.url` is the dashboard Pi's reserved address, with the
  dashboard guide named; the WSL note moves to the README's development paragraph.
- The room setup guide's dashboard-address callout points at the dashboard guide instead of
  the WSL workaround; its PDF is rebuilt.
- `deploy/dashboard/README.md` is the short operator's reference: files, env keys, the
  commands for logs, update, backup and restore.

## 5. Testing

- `tests/unit/installer/test_install_dashboard_script.py` sources the script like the room
  installer tests: it parses; `--help` exits 0 and names both options; a first run without
  `--admin-email` is refused before anything is installed; `write_env` creates a mode-600
  file whose secrets differ from each other and contain the email and address; a second
  `write_env` leaves the file byte-identical; `check_platform` refuses `armv7l` and refuses
  `MEMORY_KB` below 2 GB (overriding `uname` with a shell function); `install_backup` writes
  a timer with a daily `OnCalendar` and `Persistent=true` into `SYSTEMD_DIR`;
  `print_completion` names the address, the update command and the backup folder;
  `backup.sh` parses, uses `--clean --if-exists` and prunes at 14 days.
- `tests/unit/deploy/test_dashboard_compose.py`: the compose file parses as YAML with the two
  services and the volume; `db` publishes no ports; both services restart unless stopped;
  `dashboard` depends on a healthy `db`, sets `STATIC_DIR` and the two port variables with
  defaults 3001 and 80; `.env.example` has every key with a `REPLACE_WITH_` value and no real
  secret; the Dockerfile has the three stages, runs as `node` and starts `dist/index.js`;
  `.dockerignore` excludes `node_modules`, `dist` and `.git`.
- Backend Jest tests with ts-jest (already in devDependencies): `ensureAdmin` creates, skips
  and warns with a fake users model; `serveWebApp` on an Express app over a temporary
  directory serves `index.html` for `/` and `/devices`, serves an asset file, leaves `/api/x`
  to the JSON 404 and lets non-HTML requests fall through; `/api/auth/register` without a
  token answers 401; `helmetOptions()` contains no `upgrade-insecure-requests`; the logger
  has only the console transport without `LOG_DIR`.
- `tests/unit/docs/test_dashboard_guide.py` like the other guide tests (builds to a
  multi-page PDF, self-contained, quotes the install command, the hostname, the restore and
  update commands); `test_readme.py` expects four guides, the dashboard PDF link and the
  installer name; the room setup guide test stops expecting the WSL text if it did.
- Acceptance on this WSL machine: a throwaway env file, `DASHBOARD_PORT=3101` and
  `DASHBOARD_HTTP_PORT=8081`, `docker compose up -d --build`; `/health` answers; Playwright
  opens the sign-in page from the backend, signs in as the generated admin, creates a token,
  and the local agent pointed at port 3101 shows Online; `docker compose down -v` afterwards.
  Then the full-suite gate. The real acceptance is Ben's fourth Pi with the guide.

## 6. Files

New: `deploy/dashboard/docker-compose.yml`, `deploy/dashboard/Dockerfile`,
`deploy/dashboard/.env.example`, `deploy/dashboard/README.md`, `deploy/dashboard/backup.sh`,
`deploy/dashboard/croom-dashboard-backup.service`, `deploy/dashboard/croom-dashboard-backup.timer`,
`.dockerignore`, `installer/install-dashboard.sh`,
`src/croom-dashboard/backend/src/bootstrap.ts`, `webapp.ts`, `security.ts`, `jest.config.js`,
`src/croom-dashboard/backend/src/__tests__/*.test.ts`,
`docs/guides/crystal-meet-dashboard/*`, `docs/guides/crystal-meet-dashboard.pdf`,
`tests/unit/installer/test_install_dashboard_script.py`,
`tests/unit/deploy/test_dashboard_compose.py`, `tests/unit/docs/test_dashboard_guide.py`.

Changed: `src/croom-dashboard/backend/src/index.ts`, `models/index.ts`, `routes/auth.ts`,
`services/logger.ts`, `src/croom-dashboard/frontend/src/pages/Settings.tsx` and
`services/api.ts`, `README.md`, `deploy/rooms/README.md`,
`docs/guides/crystal-meet-room-setup/index.html` and PDF, `.gitignore`,
`tests/unit/docs/test_readme.py`, `tests/unit/docs/test_room_setup_guide.py`.

## 7. Out of scope

TLS and HTTPS; migration tooling; prebuilt images or a CI workflow; copying backups off the
Pi automatically; moving the development database from WSL (the Pi starts empty and the
rooms enroll again with new tokens); user management beyond the first admin and the
change-password form; mDNS discovery of the dashboard by the rooms; running the dashboard on
a room Pi; monitoring or alerting on the dashboard itself. Where the table touchscreen's
controls run is a separate brainstorm.
