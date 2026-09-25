# Crystal Meet: brand, naming and three-room deployment: design

Date: 2026-09-24. Branch: `crystal-meet` off `main` (be03948) on the ben-abeo/croom.to fork. Status: approved in discussion, spec under review.

## 1. Goal

Present the product as Crystal Meet everywhere a person sees it, styled to the Crystal PM brand standards, and package the setup for three conference rooms, each with its own device on the office network. Each room has three screens: the TV behind which the room device runs the meeting, a table screen for joining and controlling it, and a door sign showing whether the room is free. The table screen and the door sign are separate network devices (Wi-Fi or Ethernet, PoE or USB power) that only display pages served by the room device; nothing is cabled to it except the TV, the camera and the audio device. Internal names (the `croom` package, the `croom` command, systemd units, paths, the repository) stay as they are so upstream changes can still be merged.

Success criteria:

1. The room page and the dashboard say Crystal Meet, use the Crystal blue theme and Lexend, and carry the real Crystal PM logo. No "Croom" remains in a browser title, page, or dashboard screen.
2. The room page still passes its API and browser tests and still works on a room network with no internet access.
3. `deploy/rooms/` holds one config per room with no secrets, and the installer can install from this fork onto a Pi with a prepared room config and a service that can reach the Pi's screen and audio.
4. A branded how-to guide, "Crystal Meet: set up a room", exists as a PDF in the repo and reads correctly page by page, including how to point the table screen and the door sign at the room device.
6. The room device serves a door sign page at `/sign` that shows, from down the hall, whether the room is free, starting soon, in use or booked, and what is next.
5. Tests that pass on `main` today still pass.

## 2. Background

- The brand standards live in the `crystal-brand-docs` skill: blue theme tokens (navy-900 `#001636`, navy-800 `#16244F`, blue-600 `#1B52E5`, blue-700 `#003EBC`, periwinkle-300 `#BDCEFF`, tint-100 `#EDF2FE`, tint-50 `#F7F9FF`, slate-500 `#647087`, ink-900 `#1C2024`), Lexend for everything, letter-spaced caps kicker labels above headlines, sentence-case headlines that state the takeaway, rounded cards (8 to 12 px), stat treatment, callout bars, the logo files `CrystalPM_Logo.svg` and `CrystalPM_LogoWhite.svg`, and a how-to guide layout. The skill targets documents; this design translates its rules to screens.
- The room page (`src/croom/control/static/`) is a dark, state-tinted page with system fonts. The dashboard (`src/croom-dashboard/frontend`) is a Tailwind app on a generic gray dark theme named "Croom" in its shell, login and titles.
- `installer/install.sh` installs the package from upstream's GitHub, runs the agent as a `croom` system user with `DISPLAY=:0`, which cannot open the desktop user's screen or audio session, and writes a config template that predates the `control` section.

## 3. Scope

In scope: sections 4 and 5. Out of scope: renaming the package, command, units or repository; calendar credentials and auto-join; the Pi kiosk launcher; any change to the control API; the pre-existing upstream test failures.

## 4. Design

### 4.1 Room page

- Navy-900 background at all times (the brand's dark-hero mode); state is carried by the status card, not by washing the page.
- Lexend is bundled as package data (`static/fonts/lexend-400.woff2`, `lexend-600.woff2`, SIL Open Font License, with the license file beside them) and declared with `@font-face`; the fallback stack is `'Segoe UI', Arial, sans-serif`. No external requests.
- Header: the white Crystal PM logo (`static/crystalpm-logo-white.svg`, about 140 px wide), the room name and location, and the clock in tabular figures.
- Status card, 12 px radius, with a kicker and a headline:
  - free: navy-800 card, periwinkle kicker `ROOM FREE`, white headline ("Free until 4:00 PM"), slate detail.
  - starting soon or happening now: tint-100 card, blue-600 kicker `STARTING SOON` or `HAPPENING NOW`, navy-800 headline and ink text.
  - joining or leaving: navy-800 card, periwinkle kicker `JOINING` or `LEAVING`.
  - in a meeting: blue-600 card, white kicker `IN A MEETING`, white headline, elapsed time.
  - error: card with the brand's warning callout treatment (`#FFF8E1` fill, `#E8A013` left bar, ink text), kicker `COULDN'T JOIN`.
  - offline: navy-800 card, kicker `NOT CONNECTED`.
- Buttons: 12 px radius, 56 px tall, Lexend semibold. Primary is blue-600 with white text; on the blue card the primary is white with navy-800 text; quiet buttons are outlined in periwinkle; Leave is white text on navy-900 with a periwinkle outline inside the blue card, so the one strong accent stays the card.
- Today's schedule: kicker `TODAY`, then one navy-800 card per event with the time in periwinkle tabular figures, the title in white, the platform in slate; past events at reduced opacity; Join buttons as today.
- Join with a link: kicker `JOIN WITH A LINK`, the field in navy-800 with a periwinkle border, the Join button in blue-600.
- Copy: "Crystal Meet" replaces "Croom" ("Check that Crystal Meet is running on the room's device"); the browser title is "Crystal Meet". Kickers are the only caps text; headlines stay sentence case.
- Layout, polling, states and behaviour are unchanged from the room control spec.

### 4.2 Dashboard

- Name: "Crystal Meet" in the sidebar, the login card, the browser title and the description. The white logo sits above the name in the sidebar and the login card; a `public/crystalpm-logo-white.svg` copy ships with the app.
- Lexend loaded from bundled files in `public/fonts/` with `@font-face` in `index.css`; `body` uses the brand font stack.
- Palette remap in `tailwind.config.js` so every page inherits the brand without editing each one: the `gray` scale is replaced by navy-derived shades (900 `#001636`, 800 `#16244F`, 700 `#22346B`, 600 `#33477F`, 500 `#647087`, 400 `#8E9AB1`, 300 `#BDCEFF`, 200 `#DCE5FF`, 100 `#EDF2FE`, 50 `#F7F9FF`) and the `blue` scale by Crystal blue (600 `#1B52E5`, 700 `#003EBC`, 500 `#3B6DF0`, 400 `#6C92F5`, 300 `#BDCEFF`, 200 `#DCE5FF`, 100 `#EDF2FE`, 50 `#F7F9FF`, 800 `#00308F`, 900 `#001F5C`).
- Page headers get a kicker line above the title on the five pages that have one (Dashboard, Devices, Analytics, Provisioning, Settings), for example `FLEET OVERVIEW` above "Dashboard"; subtitles say "your Crystal Meet rooms" where they said "Croom fleet".

### 4.3 Three rooms

- `deploy/rooms/README.md` explains the layout: one device per room, one config per device, the dashboard shared.
- `deploy/rooms/room-1.yaml`, `room-2.yaml`, `room-3.yaml`: complete agent configs with placeholders for the room name, location, the dashboard address and the enrollment token (`REPLACE_WITH_TOKEN_FROM_DASHBOARD`), `meeting.platforms: [zoom, google_meet]`, `ai.enabled: false`, `control` on port 8080, `dashboard.enabled: true`. No real tokens are committed.
- The installer (`installer/install.sh`) changes:
  - `CROOM_REPO` variable, default `git+https://github.com/ben-abeo/croom.to.git`, used for the source install (the PyPI attempt is removed: the name is not on PyPI and the attempt only slows the install).
  - `--config <file>` option: the given file is copied to `/etc/croom/config.yaml` instead of the built-in template.
  - The service runs as the desktop user that invoked `sudo` (`$SUDO_USER`, typically `pi`) so Chromium can use the display and audio; that user is added to the `video`, `audio` and `input` groups; the `croom` system user is no longer created; `/opt/croom`, `/var/lib/croom` and `/var/log/croom` are owned by that user. The unit's `Environment=XDG_RUNTIME_DIR` uses that user's id.
  - The built-in template gains the `control` section, `meeting.platforms: [zoom, google_meet]` and `ai.enabled: false` (no models ship).
  - The script is checked with `bash -n` and, when available, `shellcheck`; the apt and pip steps cannot run on this machine.
- The dashboard must be reachable from the Pis: the guide's prerequisites say to run it on a machine with an address on the office network, and that WSL2 on this PC needs mirrored networking or a Windows port proxy for ports 3000 and 3001.

### 4.4 The setup guide

- Source: `docs/guides/crystal-meet-room-setup/index.html` with the brand stylesheet inlined, the logo and Lexend files beside it; output `docs/guides/crystal-meet-room-setup.pdf`, letter portrait, rendered with the venv's Chromium.
- Structure per the brand's how-to pattern: header banner (`CRYSTAL PM`, `HOW-TO GUIDE`, title "Set up a Crystal Meet room", one-line promise), intro paragraph with the time it takes and reassurance, steps-at-a-glance cards (Prepare the device, Create the room, Install, First run and screens), numbered sections with bold-lead bullets and UI values in blue-600 Lexend semibold, a step that points the table screen at `/` and the door sign at `/sign` on the room device with a fixed address, callouts (Before you begin: hardware for the device and the two screens, the dashboard address; Good to know: what enrollment does; Tip: testing with a Zoom link), a troubleshooting section (page not loading, device Offline in the dashboard, browser opens but never joins, the screens cannot open the page, Zoom asks for a passcode, Google Meet asks to be admitted), footers "How-To Guide · Set up a Crystal Meet room" and page numbers.
- Voice: second person, one action per bullet, tell the reader what they should now see after each task.

### 4.5 Door sign page

- Served by the room device at `GET /sign` from `static/sign.html`, `sign.css` and `sign.js`, with the same `Cache-Control: no-cache`, logo and bundled Lexend; no buttons; polls `GET /api/status` every 5 seconds and `GET /api/calendar/events` every 60 seconds.
- Layout: the logo, room name and location at the top left, the clock at the top right, one headline that fills the screen, a detail line, then up to three upcoming events with their times under a `TODAY` kicker. Works in landscape and portrait; the headline scales with the viewport.
- States and colors, because color is the information on a door sign: free is green `#1FA971`, starting soon is amber `#E8A013` with navy-800 text, in use and booked are deep red `#B42318`; text is white otherwise. Not connected is navy-900.
  - in use: the room device's meeting state is joining, in lobby, connected or leaving; kicker `IN USE`, headline "In use until 4:30 PM" when the calendar has a current meeting, otherwise "In use"; detail is the meeting title or platform.
  - booked: no meeting on the device but the calendar has a meeting now; kicker `BOOKED`, headline "Booked until 4:30 PM", detail the title.
  - starting soon: the next meeting starts within ten minutes; kicker `STARTING SOON`, headline "Design review starts in 8 minutes".
  - free: kicker `AVAILABLE`, headline "Free until 4:00 PM" with "Next: title" as the detail, or "Free for the rest of the day"; with no calendar, "Free".
  - not connected: kicker `NOT CONNECTED`, headline "Sign not connected".
- The sign shows meeting titles from the room's calendar; a room whose calendar is private can leave the calendar disconnected and the sign still shows free and in use from the device's own state.

## 5. Testing

- Room page: `tests/unit/control/test_service.py::TestPage` updated for the "Crystal Meet" title and the font and logo assets being served; the browser tests in `tests/unit/control/test_page.py` still pass; a screenshot review of free, in-meeting and phone widths.
- Door sign: `/sign` served with its assets; a browser test that joins through the API and watches the sign go to in use and back to free; a screenshot review in landscape and portrait.
- Dashboard: `npm run build` in `frontend` is not available (the app ships no tsconfig), so the check is the Vite dev server plus a headless screenshot review of the login and dashboard pages after signing in; the backend is untouched.
- Installer: `bash -n installer/install.sh` and `shellcheck` if installed; a unit test is out of reach, so the diff is reviewed line by line.
- Guide: the PDF is rendered, each page converted to an image and checked for kickers, aligned cards, footers and sentence-case headlines, as the brand skill asks.
- Full suite: at most the 87 pre-existing failures.

## 6. Files

- `src/croom/control/static/index.html`, `style.css`, `app.js`, `sign.html`, `sign.css`, `sign.js`, `crystalpm-logo-white.svg`, `fonts/lexend-400.woff2`, `fonts/lexend-600.woff2`, `fonts/OFL.txt`; `src/croom/control/service.py` gains the `/sign` route; `pyproject.toml` package data for `control/static/fonts/*`
- `src/croom-dashboard/frontend/index.html`, `index.css`, `tailwind.config.js`, `public/crystalpm-logo-white.svg`, `public/fonts/`, `src/components/Layout.tsx`, `src/pages/Login.tsx`, page headers in `Dashboard.tsx`, `Devices.tsx`, `Analytics.tsx`, `Provisioning.tsx`, `Settings.tsx`
- `deploy/rooms/README.md`, `room-1.yaml`, `room-2.yaml`, `room-3.yaml`
- `installer/install.sh`
- `docs/guides/crystal-meet-room-setup/` and `docs/guides/crystal-meet-room-setup.pdf`
- tests listed in section 5
- this document

## 7. Notes and risks

- Lexend is redistributed under the SIL Open Font License; the license file ships with the fonts.
- Running the service as the desktop user is the pragmatic fix for the display and audio session; a locked-down service account would need a display manager configuration that is out of scope.
- The installer's apt package list is untouched; it was verified against the Raspberry Pi OS Bookworm and Trixie indexes earlier.
- The dashboard's data model and API keep the word "Croom" only in internal identifiers such as the backend's service name in logs.
