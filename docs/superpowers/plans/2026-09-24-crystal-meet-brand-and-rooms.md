# Crystal Meet Brand and Three-Room Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Present the product as Crystal Meet in the Crystal PM brand on the room page and the dashboard, add a door sign page, and package the installation for three conference-room devices with a branded setup guide that covers the table screen and the door sign as network devices.

**Architecture:** The room page keeps its markup, API and behaviour; its stylesheet is rewritten on the brand tokens with Lexend and the logo bundled as package data, and the script only gains kicker labels and new copy. The dashboard is rebranded by remapping Tailwind's gray and blue scales to brand shades so every page inherits the palette, plus the name, logo, font and kicker labels in the shell and page headers. The installer gains a fork repository default, a `--config` option, and runs the agent as the signed-in desktop user. Three room configs live in `deploy/rooms/`, and the guide is a static HTML page rendered to PDF with the venv's Chromium.

**Tech Stack:** Python 3.12 venv at `.venv` (pytest 9, pytest-asyncio 1.4 auto mode, Playwright with Chromium), aiohttp, plain HTML/CSS/JS for the room page, React 18 + Vite 5 + Tailwind 3 for the dashboard (dev server on `localhost:3000`, backend on `:3001`), bash for the installer.

**Spec:** `docs/superpowers/specs/2026-09-24-crystal-meet-brand-and-rooms-design.md`

## Global Constraints

- Internal names stay: the `croom` package and command, systemd unit names, `/etc/croom`, `/var/lib/croom`, the repository. Only what a person sees changes.
- Brand tokens exactly: navy-900 `#001636`, navy-800 `#16244F`, blue-600 `#1B52E5`, blue-700 `#003EBC`, periwinkle-300 `#BDCEFF`, tint-100 `#EDF2FE`, tint-50 `#F7F9FF`, slate-500 `#647087`, ink-900 `#1C2024`. Lexend everywhere with fallback `'Segoe UI', Arial, sans-serif`. Kicker labels are the only uppercase text; headlines are sentence case.
- The room page makes no external requests: fonts and logo are served from `/static/`.
- The control API, its payloads and the page's `data-state` values (`loading`, `offline`, `free`, `soon`, `joining`, `meeting`, `error`) are unchanged.
- No secrets in `deploy/rooms/`; tokens are placeholders.
- Work on branch `crystal-meet` (created from `main` at be03948, pushed). Commit after each task with a `type(scope): summary` message and no attribution trailers.
- Run tests with `.venv/bin/pytest` from the repo root. After every task run `.venv/bin/pytest -q --tb=no -p no:cacheprovider --deselect tests/unit/video/test_v4l2_camera.py::TestV4L2Camera::test_start` and confirm at most 87 failed (the pre-existing upstream failures) and every test this plan adds passing.
- The Lexend files and license are already downloaded at `/tmp/claude-1000/-home-cpm-ssh/f78b4b20-e6f0-4204-894b-193a8d8a2549/scratchpad/lexend/` (`lexend-400.woff2`, `lexend-600.woff2`, `OFL.txt`); the logo files are in the brand skill at `/home/cpm_ssh/.claude/skills/synced/24908a5b-b637-4f7d-9d0e-16594be0816b_fecba94c-b935-4173-aae2-4dfd7ad8c4a6/crystal-brand-docs/assets/`.

## Review Focus

1. The room page on a network with no internet: every font and image loads from the device. Pinned by Task 1 `test_page_has_no_external_references` and `test_brand_assets_are_served`.
2. A long room name or meeting title on a phone: no horizontal scrolling, text wraps inside the card. Pinned by Task 1 `test_long_names_do_not_overflow_on_phone`.
3. The installer run with `--config` pointing at a missing file: a clear error before anything is installed. Pinned by Task 3 `test_missing_config_file_is_refused_before_install`.
4. The installer run as root without `sudo` from a desktop account: refused with an explanation, since the service must own a desktop session. Pinned by Task 3 `test_requires_a_desktop_user`.
5. A room config that does not parse would only fail on the Pi at first start: every shipped config loads through `Config.from_dict`. Pinned by Task 3 `test_room_configs_load`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/croom/control/static/style.css` | Brand stylesheet: tokens, Lexend `@font-face`, status card tones, buttons, schedule, link form. |
| `src/croom/control/static/index.html` | Logo header, status card with kicker, kickers on sections, title "Crystal Meet". |
| `src/croom/control/static/app.js` | Kicker text per state and Crystal Meet copy; behaviour unchanged. |
| `src/croom/control/static/crystalpm-logo-white.svg`, `fonts/lexend-400.woff2`, `fonts/lexend-600.woff2`, `fonts/OFL.txt` | Bundled brand assets. |
| `pyproject.toml` | Package data for `control/static/fonts/*`. |
| `src/croom-dashboard/frontend/tailwind.config.js`, `src/index.css`, `index.html`, `public/…` | Palette remap, font, kicker class, name, logo, favicon. |
| `src/croom-dashboard/frontend/src/components/Layout.tsx`, `src/pages/Login.tsx`, `Dashboard.tsx`, `Devices.tsx`, `Analytics.tsx`, `Provisioning.tsx`, `Settings.tsx` | Name, logo, kickers, copy. |
| `deploy/rooms/README.md`, `room-1.yaml`, `room-2.yaml`, `room-3.yaml` | Per-room configs. |
| `installer/install.sh` | Fork repository default, `--config`, desktop-user service, updated template, source guard. |
| `src/croom/control/static/sign.html`, `sign.css`, `sign.js`; `src/croom/control/service.py` | The door sign page and its route. |
| `docs/guides/crystal-meet-room-setup/index.html`, `build.py`, assets; `docs/guides/crystal-meet-room-setup.pdf` | The guide and its renderer. |
| `tests/unit/control/test_service.py`, `tests/unit/control/test_page.py`, `tests/unit/installer/test_install_script.py`, `tests/unit/deploy/test_room_configs.py`, `tests/unit/docs/test_room_setup_guide.py` | Tests. |

---

### Task 1: Room page in the Crystal Meet brand

**Files:**
- Create: `src/croom/control/static/crystalpm-logo-white.svg`, `src/croom/control/static/fonts/lexend-400.woff2`, `src/croom/control/static/fonts/lexend-600.woff2`, `src/croom/control/static/fonts/OFL.txt`
- Rewrite: `src/croom/control/static/style.css`, `src/croom/control/static/index.html`
- Modify: `src/croom/control/static/app.js`, `pyproject.toml`
- Test: `tests/unit/control/test_service.py`, `tests/unit/control/test_page.py`

**Interfaces:**
- Consumes: the control API and the existing `data-state` contract.
- Produces: the same page under the new brand; `#kicker` element; `/static/fonts/*` and `/static/crystalpm-logo-white.svg` served. Task 4's guide points people at this page.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/control/test_service.py`, inside `class TestPage`, change the title assertion in `test_index_is_served_uncached` from `assert "<title>Croom room</title>" in body` to `assert "<title>Crystal Meet</title>" in body`, and append these tests to the class:

```python
    async def test_brand_assets_are_served(self, client_factory):
        client = await client_factory(make_service())
        for path, content_type in (
            ("/static/crystalpm-logo-white.svg", "image/svg+xml"),
            ("/static/fonts/lexend-400.woff2", "font/woff2"),
            ("/static/fonts/lexend-600.woff2", "font/woff2"),
            ("/static/fonts/OFL.txt", "text/plain"),
        ):
            resp = await client.get(path)
            assert resp.status == 200, path
            assert resp.headers["Content-Type"].startswith(content_type), (path, resp.headers["Content-Type"])

    def test_page_has_no_external_references(self):
        from croom.control.service import STATIC_DIR
        for name in ("index.html", "style.css", "app.js"):
            text = (STATIC_DIR / name).read_text(encoding="utf-8")
            assert "http://" not in text and "https://" not in text and "//fonts." not in text, name
        assert "Croom" not in (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        assert "Croom" not in (STATIC_DIR / "app.js").read_text(encoding="utf-8")
```

In `tests/unit/control/test_page.py`, give `PageServer` a room name: change its `__init__` signature to `def __init__(self, calendar_events=(), room_name="Lab"):`, store `self.room_name = room_name`, and use `"room_name": self.room_name` in the `ControlService` config inside `_main`. Then append:

```python
def test_long_names_do_not_overflow_on_phone(browser):
    long_name = "The Extraordinarily Long Conference Room Name Nobody Abbreviates"
    with PageServer(calendar_events=[event("e1", "Quarterly planning session with the entire leadership team", 25)],
                    room_name=long_name) as server:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"http://127.0.0.1:{server.port}/", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        assert page.locator("#room-name").inner_text() == long_name
        assert page.locator("#kicker").inner_text().upper() == "ROOM FREE"
        page.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/control/test_service.py -q -p no:cacheprovider -k "TestPage"` and `.venv/bin/pytest tests/unit/control/test_page.py -q -p no:cacheprovider -k long_names`
Expected: FAIL: the title assertion (`Crystal Meet` missing), the brand assets with `404`, the external-reference test on `Croom` in the files, and the phone test on the missing `#kicker` element.

- [ ] **Step 3: Add the assets**

```bash
S=/tmp/claude-1000/-home-cpm-ssh/f78b4b20-e6f0-4204-894b-193a8d8a2549/scratchpad/lexend
B=/home/cpm_ssh/.claude/skills/synced/24908a5b-b637-4f7d-9d0e-16594be0816b_fecba94c-b935-4173-aae2-4dfd7ad8c4a6/crystal-brand-docs/assets
mkdir -p src/croom/control/static/fonts
cp "$S/lexend-400.woff2" "$S/lexend-600.woff2" "$S/OFL.txt" src/croom/control/static/fonts/
cp "$B/CrystalPM_LogoWhite.svg" src/croom/control/static/crystalpm-logo-white.svg
```

In `pyproject.toml` change `croom = ["py.typed", "control/static/*"]` to `croom = ["py.typed", "control/static/*", "control/static/fonts/*"]`.

- [ ] **Step 4: Rewrite the stylesheet**

Write `src/croom/control/static/style.css`:

```css
/* Crystal Meet room page: Crystal PM blue theme, dark-hero mode. */
@font-face {
  font-family: "Lexend";
  src: url("/static/fonts/lexend-400.woff2") format("woff2");
  font-weight: 400;
  font-display: swap;
}
@font-face {
  font-family: "Lexend";
  src: url("/static/fonts/lexend-600.woff2") format("woff2");
  font-weight: 600;
  font-display: swap;
}

:root {
  --navy-900: #001636;
  --navy-800: #16244F;
  --blue-600: #1B52E5;
  --blue-700: #003EBC;
  --periwinkle-300: #BDCEFF;
  --tint-100: #EDF2FE;
  --tint-50: #F7F9FF;
  --slate-500: #647087;
  --ink-900: #1C2024;
  --warn-fill: #FFF8E1;
  --warn-bar: #E8A013;
  --radius: 12px;
  --radius-sm: 8px;
  --gutter: clamp(20px, 4vw, 56px);
  --font: "Lexend", "Segoe UI", Arial, sans-serif;
}

* { box-sizing: border-box; }
html, body { height: 100%; }

body {
  margin: 0;
  background: var(--navy-900);
  color: #fff;
  font-family: var(--font);
  font-size: clamp(16px, 1.15vw, 20px);
  line-height: 1.45;
}

.screen {
  min-height: 100%;
  padding: var(--gutter);
  display: grid;
  gap: clamp(24px, 3.5vh, 40px);
  grid-template-areas: "top" "now" "link" "schedule";
  align-content: start;
}
@media (min-width: 1024px) {
  .screen {
    grid-template-columns: 3fr 2fr;
    grid-template-areas:
      "top top"
      "now schedule"
      "link schedule";
    column-gap: clamp(40px, 5vw, 96px);
  }
}
.top { grid-area: top; display: flex; justify-content: space-between; align-items: flex-start; gap: 24px; }
.now { grid-area: now; }
.schedule { grid-area: schedule; }
.link { grid-area: link; }

.brand { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.logo { width: clamp(120px, 12vw, 160px); height: auto; display: block; }
h1 { margin: 0; font-size: clamp(1.3rem, 2.2vw, 2rem); font-weight: 600; line-height: 1.15; overflow-wrap: anywhere; }
.location { margin: 2px 0 0; color: var(--periwinkle-300); }

.clock {
  margin: 0;
  font-size: clamp(2.2rem, 6vw, 5.2rem);
  font-weight: 400;
  line-height: 1;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.kicker {
  margin: 0 0 10px;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  color: var(--periwinkle-300);
}

/* Status card: the one element that changes with the room's state. */
.status-card {
  border-radius: var(--radius);
  padding: clamp(20px, 3vw, 36px);
  background: var(--navy-800);
  color: #fff;
  transition: background-color 400ms ease, color 400ms ease;
}
@media (prefers-reduced-motion: reduce) { .status-card { transition: none; } }
.headline {
  margin: 0;
  font-size: clamp(1.7rem, 4.4vw, 3.8rem);
  font-weight: 600;
  line-height: 1.1;
  letter-spacing: -0.015em;
  max-width: 18ch;
  overflow-wrap: anywhere;
}
.detail { margin: 12px 0 0; color: var(--periwinkle-300); max-width: 48ch; overflow-wrap: anywhere; }
.message { margin: 12px 0 0; min-height: 1.45em; color: #FFD7D5; max-width: 48ch; }
.message:empty { display: none; }
.actions { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 22px; }

body[data-state="soon"] .status-card { background: var(--tint-100); color: var(--navy-800); }
body[data-state="soon"] .kicker { color: var(--blue-600); }
body[data-state="soon"] .detail { color: var(--slate-500); }
body[data-state="meeting"] .status-card { background: var(--blue-600); }
body[data-state="meeting"] .kicker, body[data-state="meeting"] .detail { color: #fff; }
body[data-state="error"] .status-card {
  background: var(--warn-fill);
  color: var(--ink-900);
  border-left: 6px solid var(--warn-bar);
  border-radius: var(--radius-sm);
}
body[data-state="error"] .kicker { color: var(--blue-700); }
body[data-state="error"] .detail { color: var(--ink-900); }
body[data-state="error"] .message { color: #A3260F; }

/* Buttons */
.button {
  min-height: 56px;
  padding: 0 26px;
  border-radius: var(--radius);
  border: 2px solid transparent;
  font: inherit;
  font-size: 1.05rem;
  font-weight: 600;
  color: #fff;
  background: var(--blue-600);
  cursor: pointer;
  touch-action: manipulation;
}
.button:hover { background: var(--blue-700); }
.button.quiet { background: transparent; border-color: var(--periwinkle-300); color: inherit; }
.button.quiet:hover { background: rgba(255, 255, 255, 0.08); }
.button.danger { background: var(--navy-900); border-color: var(--periwinkle-300); }
.button:disabled { opacity: 0.45; cursor: default; }
.button:focus-visible, input:focus-visible { outline: 3px solid var(--periwinkle-300); outline-offset: 2px; }
body[data-state="soon"] .button.primary { background: var(--blue-600); color: #fff; }
body[data-state="meeting"] .button { background: transparent; border-color: rgba(255, 255, 255, 0.7); color: #fff; }
body[data-state="meeting"] .button:hover { background: rgba(255, 255, 255, 0.12); }
body[data-state="meeting"] .button.danger { background: var(--navy-900); border-color: var(--periwinkle-300); }
body[data-state="error"] .button.quiet { border-color: var(--blue-700); color: var(--blue-700); }

/* Today's schedule */
.events { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }
.event {
  display: grid;
  grid-template-columns: 6.5ch 1fr auto;
  gap: 14px;
  align-items: center;
  padding: 14px 18px;
  border-radius: var(--radius);
  background: var(--navy-800);
}
.event.past { opacity: 0.5; }
.event.now { box-shadow: inset 0 0 0 2px var(--blue-600); }
.event time { font-variant-numeric: tabular-nums; color: var(--periwinkle-300); }
.event .title { margin: 0; overflow-wrap: anywhere; }
.event .platform { margin: 2px 0 0; color: var(--periwinkle-300); font-size: 0.9em; }
.event .button { min-height: 48px; padding: 0 20px; }
.note { margin: 12px 0 0; color: var(--periwinkle-300); }
.note:empty { display: none; }

/* Join with a link */
.link-form { display: flex; flex-wrap: wrap; gap: 12px; }
.link-form input {
  flex: 1 1 260px;
  min-height: 56px;
  padding: 0 18px;
  border-radius: var(--radius);
  border: 2px solid var(--periwinkle-300);
  background: var(--navy-800);
  color: #fff;
  font: inherit;
}
.link-form input::placeholder { color: rgba(189, 206, 255, 0.7); }

.visually-hidden {
  position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
}
```

- [ ] **Step 5: Rewrite the page markup**

Write `src/croom/control/static/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crystal Meet</title>
<link rel="icon" href="/static/crystalpm-logo-white.svg" type="image/svg+xml">
<link rel="stylesheet" href="/static/style.css">
</head>
<body data-state="loading">
<main class="screen">
  <header class="top">
    <div class="brand">
      <img class="logo" src="/static/crystalpm-logo-white.svg" alt="Crystal PM">
      <div>
        <h1 id="room-name">Room</h1>
        <p id="room-location" class="location"></p>
      </div>
    </div>
    <p id="clock" class="clock"></p>
  </header>

  <section class="now status-card" aria-live="polite">
    <p id="kicker" class="kicker">Connecting</p>
    <p id="headline" class="headline">Connecting to the room</p>
    <p id="detail" class="detail"></p>
    <p id="message" class="message" role="status"></p>
    <div id="actions" class="actions"></div>
  </section>

  <section class="schedule">
    <p class="kicker">Today</p>
    <ul id="events" class="events"></ul>
    <p id="calendar-note" class="note"></p>
  </section>

  <section class="link">
    <p class="kicker">Join with a link</p>
    <form id="link-form" class="link-form">
      <label class="visually-hidden" for="link-input">Meeting link or Zoom meeting ID</label>
      <input id="link-input" type="text" inputmode="url" autocomplete="off" spellcheck="false"
             placeholder="Paste a meeting link or a Zoom meeting ID">
      <button type="submit" class="button primary">Join</button>
    </form>
  </section>
</main>
<script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 6: Update the script's copy and kickers**

In `src/croom/control/static/app.js` make these replacements.

Replace

```javascript
      el("headline").textContent = "Can't reach the room";
      el("detail").textContent = "Check that the Croom agent is running, then this page will reconnect on its own.";
```

with

```javascript
      el("kicker").textContent = "Not connected";
      el("headline").textContent = "Can't reach the room";
      el("detail").textContent = "Check that Crystal Meet is running on the room's device, then this page will reconnect on its own.";
```

Replace

```javascript
      body.dataset.state = "joining";
      el("headline").textContent = m.state === "leaving" ? "Leaving" : "Joining " + label;
```

with

```javascript
      body.dataset.state = "joining";
      el("kicker").textContent = m.state === "leaving" ? "Leaving" : "Joining";
      el("headline").textContent = m.state === "leaving" ? "Leaving" : "Joining " + label;
```

Replace

```javascript
      body.dataset.state = "meeting";
      el("headline").textContent = "In a meeting";
```

with

```javascript
      body.dataset.state = "meeting";
      el("kicker").textContent = "In a meeting";
      el("headline").textContent = "In a meeting";
```

Replace

```javascript
      body.dataset.state = "error";
      el("headline").textContent = "Couldn't join " + label;
```

with

```javascript
      body.dataset.state = "error";
      el("kicker").textContent = "Couldn't join";
      el("headline").textContent = "Couldn't join " + label;
```

Replace

```javascript
    if (current) {
      body.dataset.state = "soon";
      el("headline").textContent = current.title + " is happening now";
```

with

```javascript
    if (current) {
      body.dataset.state = "soon";
      el("kicker").textContent = "Happening now";
      el("headline").textContent = current.title + " is happening now";
```

Replace

```javascript
      if (window.open) {
        body.dataset.state = "soon";
        el("headline").textContent = minutes === 0 ? next.title + " is starting" : next.title + " starts in " + plural(minutes, "minute");
      } else {
        body.dataset.state = "free";
        el("headline").textContent = "Free until " + fmtTime(start);
      }
```

with

```javascript
      if (window.open) {
        body.dataset.state = "soon";
        el("kicker").textContent = "Starting soon";
        el("headline").textContent = minutes === 0 ? next.title + " is starting" : next.title + " starts in " + plural(minutes, "minute");
      } else {
        body.dataset.state = "free";
        el("kicker").textContent = "Room free";
        el("headline").textContent = "Free until " + fmtTime(start);
      }
```

Replace

```javascript
    body.dataset.state = "free";
    el("headline").textContent = cal.connected ? "Free for the rest of the day" : "Free";
```

with

```javascript
    body.dataset.state = "free";
    el("kicker").textContent = "Room free";
    el("headline").textContent = cal.connected ? "Free for the rest of the day" : "Free";
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/control/test_service.py tests/unit/control/test_page.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 8: Look at the page**

Serve the page with stub services and capture the free, in-meeting and phone views with the venv's Chromium (the recipe from the room-control plan's Task 4 Step 5 still applies). Check: the logo shows, Lexend renders (letterforms differ from the system sans), kickers are letter-spaced caps, the status card changes tone per state, buttons are 56 px tall, nothing overflows at 390 px. Fix anything off before committing.

- [ ] **Step 9: Run the whole suite** (Global Constraints). Expected: at most 87 failed.

- [ ] **Step 10: Commit**

```bash
git add src/croom/control/static pyproject.toml tests/unit/control/test_service.py tests/unit/control/test_page.py
git commit -m "feat(control): Crystal Meet room page in the Crystal PM brand"
```

---

### Task 2: Dashboard in the Crystal Meet brand

**Files:**
- Create: `src/croom-dashboard/frontend/public/crystalpm-logo-white.svg`, `public/favicon.svg`, `public/fonts/lexend-400.woff2`, `public/fonts/lexend-600.woff2`, `public/fonts/OFL.txt`
- Modify: `src/croom-dashboard/frontend/tailwind.config.js`, `src/index.css`, `index.html`, `src/components/Layout.tsx`, `src/pages/Login.tsx`, `src/pages/Dashboard.tsx`, `src/pages/Devices.tsx`, `src/pages/Analytics.tsx`, `src/pages/Provisioning.tsx`, `src/pages/Settings.tsx`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: the rebranded dashboard. No code interfaces. Verified by screenshots, since the frontend has no test runner.

- [ ] **Step 1: Record the before state**

With the dev servers running (backend `:3001`, frontend `:3000`), capture `http://localhost:3000/login` at 1280×800 with the venv's Chromium to `scratchpad/dashboard-before.png`, and confirm the shell says "Croom". This is the visual "red" for a task with no test runner.

- [ ] **Step 2: Add the assets**

```bash
S=/tmp/claude-1000/-home-cpm-ssh/f78b4b20-e6f0-4204-894b-193a8d8a2549/scratchpad/lexend
B=/home/cpm_ssh/.claude/skills/synced/24908a5b-b637-4f7d-9d0e-16594be0816b_fecba94c-b935-4173-aae2-4dfd7ad8c4a6/crystal-brand-docs/assets
F=src/croom-dashboard/frontend
mkdir -p "$F/public/fonts"
cp "$S/lexend-400.woff2" "$S/lexend-600.woff2" "$S/OFL.txt" "$F/public/fonts/"
cp "$B/CrystalPM_LogoWhite.svg" "$F/public/crystalpm-logo-white.svg"
cp "$B/CrystalPM_Logo.svg" "$F/public/favicon.svg"   # blue mark: visible on light browser tabs
```

- [ ] **Step 3: Remap the palette and font**

Write `src/croom-dashboard/frontend/tailwind.config.js`:

```javascript
/** @type {import('tailwindcss').Config} */
// Crystal PM blue theme. The gray and blue scales are remapped to brand shades so
// every page inherits the palette without editing each class.
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Lexend', 'Segoe UI', 'Arial', 'sans-serif'],
      },
      colors: {
        gray: {
          50: '#F7F9FF',
          100: '#EDF2FE',
          200: '#DCE5FF',
          300: '#BDCEFF',
          400: '#8E9AB1',
          500: '#647087',
          600: '#33477F',
          700: '#22346B',
          800: '#16244F',
          900: '#001636',
        },
        blue: {
          50: '#F7F9FF',
          100: '#EDF2FE',
          200: '#DCE5FF',
          300: '#BDCEFF',
          400: '#6C92F5',
          500: '#3B6DF0',
          600: '#1B52E5',
          700: '#003EBC',
          800: '#00308F',
          900: '#001F5C',
        },
      },
    },
  },
  plugins: [],
}
```

Write `src/croom-dashboard/frontend/src/index.css`:

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

@font-face {
  font-family: 'Lexend';
  src: url('/fonts/lexend-400.woff2') format('woff2');
  font-weight: 400;
  font-display: swap;
}
@font-face {
  font-family: 'Lexend';
  src: url('/fonts/lexend-600.woff2') format('woff2');
  font-weight: 600;
  font-display: swap;
}

body {
  margin: 0;
  font-family: 'Lexend', 'Segoe UI', Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

@layer components {
  /* Letter-spaced caps line above a page headline. */
  .kicker {
    @apply text-xs font-bold uppercase tracking-[0.15em] text-blue-300 mb-1;
  }
}

/* Custom scrollbar */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: #16244F; }
::-webkit-scrollbar-thumb { background: #33477F; border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: #647087; }

/* Focus styles */
input:focus,
button:focus {
  outline: none;
  box-shadow: 0 0 0 2px rgba(189, 206, 255, 0.7);
}
```

In `src/croom-dashboard/frontend/index.html` change the description to `Crystal Meet rooms dashboard` and the title to `<title>Crystal Meet</title>` (the favicon line already points at `/favicon.svg`).

- [ ] **Step 4: Rename and add the logo in the shell**

In `src/components/Layout.tsx` replace

```tsx
          <div className="p-6 border-b border-gray-700">
            <h1 className="text-2xl font-bold text-white">Croom</h1>
            <p className="text-sm text-gray-400">Enterprise Dashboard</p>
          </div>
```

with

```tsx
          <div className="p-6 border-b border-gray-700">
            <img src="/crystalpm-logo-white.svg" alt="Crystal PM" className="h-8 w-auto mb-3" />
            <h1 className="text-2xl font-bold text-white">Crystal Meet</h1>
            <p className="text-sm text-gray-400">Rooms dashboard</p>
          </div>
```

In `src/pages/Login.tsx` replace

```tsx
          <div className="text-center mb-8">
            <h1 className="text-3xl font-bold text-white">Croom</h1>
            <p className="text-gray-400 mt-2">Enterprise Management Dashboard</p>
          </div>
```

with

```tsx
          <div className="text-center mb-8">
            <img src="/crystalpm-logo-white.svg" alt="Crystal PM" className="h-10 w-auto mx-auto mb-4" />
            <h1 className="text-3xl font-bold text-white">Crystal Meet</h1>
            <p className="text-gray-400 mt-2">Sign in to manage your rooms</p>
          </div>
```

- [ ] **Step 5: Kickers and copy on the page headers**

Make these replacements, one per file.

`src/pages/Dashboard.tsx`:

```tsx
        <h1 className="text-3xl font-bold">Dashboard</h1>
        <p className="text-gray-400 mt-1">Overview of your Croom fleet</p>
```
becomes
```tsx
        <p className="kicker">Fleet overview</p>
        <h1 className="text-3xl font-bold">Dashboard</h1>
        <p className="text-gray-400 mt-1">Every Crystal Meet room at a glance</p>
```

`src/pages/Devices.tsx`:

```tsx
          <h1 className="text-3xl font-bold">Devices</h1>
```
becomes
```tsx
          <p className="kicker">Room devices</p>
          <h1 className="text-3xl font-bold">Devices</h1>
```

`src/pages/Analytics.tsx`:

```tsx
        <h1 className="text-3xl font-bold">Analytics</h1>
```
becomes
```tsx
        <p className="kicker">Usage</p>
        <h1 className="text-3xl font-bold">Analytics</h1>
```

`src/pages/Provisioning.tsx`:

```tsx
        <h1 className="text-3xl font-bold">Device Provisioning</h1>
        <p className="text-gray-400 mt-1">Add new devices to your fleet</p>
```
becomes
```tsx
        <p className="kicker">Add a room</p>
        <h1 className="text-3xl font-bold">Device provisioning</h1>
        <p className="text-gray-400 mt-1">Create an enrollment token for a new room device</p>
```

`src/pages/Settings.tsx`:

```tsx
        <h1 className="text-3xl font-bold">Settings</h1>
```
becomes
```tsx
        <p className="kicker">Dashboard settings</p>
        <h1 className="text-3xl font-bold">Settings</h1>
```

Then confirm nothing user-facing still says Croom: `grep -rn "Croom" src/croom-dashboard/frontend/src src/croom-dashboard/frontend/index.html` should print nothing.

- [ ] **Step 6: Look at the result**

The Vite dev server reloads on save. Capture `http://localhost:3000/login` and, after signing in as `admin@croom.local` with the password in `~/.config/croom/dashboard-admin-password.txt`, `http://localhost:3000/` and `/provisioning` at 1280×800 with the venv's Chromium. Check: the logo renders white on navy, the name is Crystal Meet, Lexend is in use, the sidebar's active item is Crystal blue, kickers sit above the headlines, cards are navy-800 on navy-900, and the login button is Crystal blue. Fix anything off before committing.

- [ ] **Step 7: Commit**

```bash
git add src/croom-dashboard/frontend
git commit -m "feat(dashboard): Crystal Meet name, logo and Crystal PM palette"
```

---

### Task 3: Room configs and the installer for three Pis

**Files:**
- Create: `deploy/rooms/README.md`, `deploy/rooms/room-1.yaml`, `deploy/rooms/room-2.yaml`, `deploy/rooms/room-3.yaml`, `tests/unit/deploy/__init__.py`, `tests/unit/deploy/test_room_configs.py`, `tests/unit/installer/test_install_script.py`
- Modify: `installer/install.sh`

**Interfaces:**
- Consumes: `Config.from_dict` (existing).
- Produces: `installer/install.sh --config <file>`, env `CROOM_REPO`, functions `check_desktop_user`, `create_config` callable when the script is sourced; the three configs. Task 4's guide documents exactly these.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/deploy/__init__.py` (empty) and `tests/unit/deploy/test_room_configs.py`:

```python
"""
Every shipped room config must load through the agent's Config so a typo
surfaces here and not at first boot on a Pi.
"""

from pathlib import Path

import pytest
import yaml

from croom.core.config import Config

ROOMS = sorted((Path(__file__).resolve().parents[3] / "deploy" / "rooms").glob("room-*.yaml"))


@pytest.mark.parametrize("path", ROOMS, ids=lambda p: p.name)
def test_room_configs_load(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = Config.from_dict(data)
    assert config.control.enabled is True
    assert config.control.port == 8080
    assert config.dashboard.enabled is True
    assert config.dashboard.enrollment_token == "REPLACE_WITH_TOKEN_FROM_DASHBOARD"
    assert "REPLACE_WITH_DASHBOARD_ADDRESS" in config.dashboard.url
    assert config.meeting.platforms == ["zoom", "google_meet"]
    assert config.ai.enabled is False
    assert config.calendar.providers == []


def test_there_are_three_rooms():
    assert [p.name for p in ROOMS] == ["room-1.yaml", "room-2.yaml", "room-3.yaml"]
```

Create `tests/unit/installer/test_install_script.py`:

```python
"""
Tests for installer/install.sh that do not need root or apt: argument
handling, the desktop-user check, and config creation with the script sourced.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "installer" / "install.sh"


def run_bash(snippet, env=None):
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, env=merged, cwd=REPO)


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_help_exits_zero_and_mentions_config():
    result = run_bash(f"bash {SCRIPT} --help")
    assert result.returncode == 0
    assert "--config" in result.stdout


def test_missing_config_file_is_refused_before_install(tmp_path):
    result = run_bash(f"bash {SCRIPT} --config {tmp_path / 'nope.yaml'}")
    assert result.returncode == 1
    assert "not found" in (result.stdout + result.stderr).lower()


def test_requires_a_desktop_user():
    result = run_bash(f"source {SCRIPT}; SUDO_USER= check_desktop_user")
    assert result.returncode == 1
    assert "sudo" in (result.stdout + result.stderr).lower()
    result = run_bash(f"source {SCRIPT}; SUDO_USER=root check_desktop_user")
    assert result.returncode == 1


def test_default_config_has_the_control_section(tmp_path):
    result = run_bash(
        f"source {SCRIPT}; CONFIG_DIR={tmp_path}; CROOM_USER=$(id -un); create_config",
    )
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "config.yaml").read_text()
    assert "control:" in text and "port: 8080" in text
    assert "platforms:" in text and "- zoom" in text and "- google_meet" in text
    assert "enabled: false" in text.split("ai:")[1].split("\n\n")[0]


def test_prepared_config_is_installed_verbatim(tmp_path):
    room = tmp_path / "room.yaml"
    room.write_text("version: 2\nroom:\n  name: Room 9\n")
    result = run_bash(
        f"source {SCRIPT}; CONFIG_DIR={tmp_path / 'etc'}; CROOM_USER=$(id -un); ROOM_CONFIG={room}; create_config",
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "etc" / "config.yaml").read_text() == room.read_text()


def test_installs_from_the_fork_by_default():
    text = SCRIPT.read_text()
    assert 'CROOM_REPO="${CROOM_REPO:-git+https://github.com/ben-abeo/croom.to.git}"' in text
    assert "pip\" install croom" not in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/deploy tests/unit/installer/test_install_script.py -q -p no:cacheprovider`
Expected: FAIL: no room files (`test_there_are_three_rooms`, and the parametrized test collects nothing), `--help` output lacks `--config`, the missing-config run exits with the "must be run as root" error, `check_desktop_user` is not a function, `create_config` runs the whole script (`main` executes on source) or writes the old template, and the fork assertion fails.

- [ ] **Step 3: Write the room configs**

Create `deploy/rooms/README.md`:

```markdown
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

The room page is then at `http://<device>:8080/` for anyone on the network.
Never commit a real token to this folder.
```

Create `deploy/rooms/room-1.yaml`:

```yaml
# Crystal Meet room device: Room 1
# Replace the three REPLACE values, then install with:
#   sudo bash installer/install.sh --config room-1.yaml
version: 2

room:
  name: "Room 1"
  location: "REPLACE_WITH_LOCATION"
  timezone: "America/Chicago"

meeting:
  platforms: [zoom, google_meet]
  default_platform: auto
  join_early_minutes: 1
  auto_leave: true
  camera_default_on: true
  mic_default_on: true

calendar:
  providers: []

ai:
  enabled: false

control:
  enabled: true
  host: "0.0.0.0"
  port: 8080

dashboard:
  enabled: true
  url: "http://REPLACE_WITH_DASHBOARD_ADDRESS:3001"
  enrollment_token: "REPLACE_WITH_TOKEN_FROM_DASHBOARD"
  heartbeat_interval_seconds: 30
  metrics_interval_seconds: 60
```

Create `room-2.yaml` and `room-3.yaml` as copies with `Room 2` / `Room 3` in the comment and `room.name`.

- [ ] **Step 4: Change the installer**

In `installer/install.sh` make these edits.

Replace

```bash
CROOM_USER="croom"
```

with

```bash
# The agent runs as the desktop user that invoked sudo, so Chromium can use the
# screen and audio of the signed-in session. Override the source with CROOM_REPO.
CROOM_USER="${SUDO_USER:-}"
CROOM_REPO="${CROOM_REPO:-git+https://github.com/ben-abeo/croom.to.git}"
ROOM_CONFIG=""
```

Directly after the `check_root()` function add:

```bash
# The service needs a desktop session: refuse a bare root shell.
check_desktop_user() {
    if [[ -z "$CROOM_USER" || "$CROOM_USER" == "root" ]]; then
        error "Run this installer with sudo from the desktop user account, for example: sudo bash installer/install.sh"
    fi
    if ! id "$CROOM_USER" &>/dev/null; then
        error "User '$CROOM_USER' does not exist"
    fi
    log "Crystal Meet will run as user $CROOM_USER"
}
```

Replace the whole `create_user()` function with:

```bash
# Give the desktop user access to cameras, audio and input devices
create_user() {
    log "Preparing user $CROOM_USER..."
    usermod -a -G video,audio,input,dialout,gpio "$CROOM_USER" 2>/dev/null || true
}
```

In `install_croom()` replace

```bash
    # Install package
    "$INSTALL_DIR/venv/bin/pip" install --upgrade pip
    "$INSTALL_DIR/venv/bin/pip" install croom || {
        # If package not on PyPI, install from source
        log "Installing from source..."
        "$INSTALL_DIR/venv/bin/pip" install /usr/local/src/croom 2>/dev/null || \
        "$INSTALL_DIR/venv/bin/pip" install git+https://github.com/amirhmoradi/croom.to.git
    }
```

with

```bash
    # Install package from the configured repository
    "$INSTALL_DIR/venv/bin/pip" install --upgrade pip
    log "Installing from $CROOM_REPO"
    "$INSTALL_DIR/venv/bin/pip" install "$CROOM_REPO"
```

In `create_config()` replace the opening

```bash
    log "Creating configuration..."

    if [[ -f "$CONFIG_DIR/config.yaml" ]]; then
        log "Configuration already exists, skipping"
        return
    fi
```

with

```bash
    log "Creating configuration..."
    mkdir -p "$CONFIG_DIR"

    if [[ -n "$ROOM_CONFIG" ]]; then
        cp "$ROOM_CONFIG" "$CONFIG_DIR/config.yaml"
        chown "$CROOM_USER:$CROOM_USER" "$CONFIG_DIR/config.yaml"
        chmod 640 "$CONFIG_DIR/config.yaml"
        log "Installed room configuration from $ROOM_CONFIG"
        return
    fi

    if [[ -f "$CONFIG_DIR/config.yaml" ]]; then
        log "Configuration already exists, skipping"
        return
    fi
```

In the template inside `create_config()` replace

```yaml
meeting:
  platforms:
    - google_meet
    - teams
    - zoom
```

with

```yaml
meeting:
  platforms:
    - zoom
    - google_meet
```

replace

```yaml
ai:
  enabled: true
```

with

```yaml
ai:
  enabled: false
```

and directly before the line `dashboard:` in the template insert

```yaml
control:
  enabled: true
  host: "0.0.0.0"
  port: 8080

```

In `create_service()`, in both unit files, replace `Description=Croom Conference Room Agent` with `Description=Crystal Meet room agent (croom)`, `Description=Croom Touch UI` with `Description=Crystal Meet touch UI (croom-ui)`, and both occurrences of `Environment=XDG_RUNTIME_DIR=/run/user/1000` with `Environment=XDG_RUNTIME_DIR=/run/user/$(id -u "$CROOM_USER")`.

In `print_completion()` replace `echo -e "${GREEN}  Croom Installation Complete!${NC}"` with `echo -e "${GREEN}  Crystal Meet installation complete${NC}"`, and after the `echo "Configuration: $CONFIG_DIR/config.yaml"` line add `echo "Room page: http://$(hostname).local:8080/  (or use this device's IP address)"`.

In `main()`, insert `check_desktop_user` directly after `check_root`.

Replace the argument parser and the final `main` call (everything from `# Parse arguments` to the end of the file) with:

```bash
# Parse arguments and run only when executed, not when sourced by tests
run_installer() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --config)
                ROOM_CONFIG="$2"
                if [[ -z "$ROOM_CONFIG" || ! -f "$ROOM_CONFIG" ]]; then
                    error "Config file not found: ${ROOM_CONFIG:-<missing>}"
                fi
                shift 2
                ;;
            --enable-ui)
                ENABLE_UI="yes"
                shift
                ;;
            --no-service)
                NO_SERVICE="yes"
                shift
                ;;
            --help)
                echo "Usage: $0 [options]"
                echo ""
                echo "Options:"
                echo "  --config FILE   Install a prepared room config as /etc/croom/config.yaml"
                echo "  --enable-ui     Enable Touch UI service"
                echo "  --no-service    Don't create systemd services"
                echo "  --help          Show this help"
                echo ""
                echo "Environment:"
                echo "  CROOM_REPO      pip source to install (default: this fork on GitHub)"
                exit 0
                ;;
            *)
                error "Unknown option: $1"
                ;;
        esac
    done
    main
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    run_installer "$@"
fi
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `bash -n installer/install.sh && .venv/bin/pytest tests/unit/deploy tests/unit/installer/test_install_script.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Run the whole suite** (Global Constraints). Expected: at most 87 failed.

- [ ] **Step 7: Commit**

```bash
git add deploy installer/install.sh tests/unit/deploy tests/unit/installer/test_install_script.py
git commit -m "feat(deploy): room configs and installer support for three Crystal Meet devices"
```

---

### Task 4: The setup guide

**Files:**
- Create: `docs/guides/crystal-meet-room-setup/index.html`, `docs/guides/crystal-meet-room-setup/build.py`, `docs/guides/crystal-meet-room-setup/crystalpm-logo-white.svg`, `docs/guides/crystal-meet-room-setup/fonts/lexend-400.woff2`, `docs/guides/crystal-meet-room-setup/fonts/lexend-600.woff2`, `docs/guides/crystal-meet-room-setup.pdf`, `tests/unit/docs/__init__.py`, `tests/unit/docs/test_room_setup_guide.py`

**Interfaces:**
- Consumes: the installer's `--config` option and the room configs (Task 3), the room page (Task 1), the dashboard's Provisioning page (Task 2).
- Produces: `build.py` that renders the PDF; the PDF itself.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/docs/__init__.py` (empty) and `tests/unit/docs/test_room_setup_guide.py`:

```python
"""
The setup guide builds to a multi-page PDF with the venv's Chromium.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GUIDE = REPO / "docs" / "guides" / "crystal-meet-room-setup"

pytest.importorskip("playwright.sync_api")


def test_guide_builds_to_a_multi_page_pdf(tmp_path):
    out = tmp_path / "guide.pdf"
    result = subprocess.run([sys.executable, str(GUIDE / "build.py"), str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = out.read_bytes()
    assert data.startswith(b"%PDF")
    counts = [int(m) for m in re.findall(rb"/Count (\d+)", data)]
    assert counts and max(counts) >= 3, counts


def test_guide_source_is_self_contained():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert "https://" not in html and "http://fonts" not in html
    assert "Crystal Meet" in html and "Croom " not in html
    for asset in ("crystalpm-logo-white.svg", "fonts/lexend-400.woff2", "fonts/lexend-600.woff2"):
        assert (GUIDE / asset).is_file(), asset
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/unit/docs -q -p no:cacheprovider`
Expected: FAIL: `build.py` and `index.html` do not exist.

- [ ] **Step 3: Add the assets and the renderer**

```bash
S=/tmp/claude-1000/-home-cpm-ssh/f78b4b20-e6f0-4204-894b-193a8d8a2549/scratchpad/lexend
B=/home/cpm_ssh/.claude/skills/synced/24908a5b-b637-4f7d-9d0e-16594be0816b_fecba94c-b935-4173-aae2-4dfd7ad8c4a6/crystal-brand-docs/assets
G=docs/guides/crystal-meet-room-setup
mkdir -p "$G/fonts"
cp "$S/lexend-400.woff2" "$S/lexend-600.woff2" "$G/fonts/"
cp "$B/CrystalPM_LogoWhite.svg" "$G/crystalpm-logo-white.svg"
```

Create `docs/guides/crystal-meet-room-setup/build.py`:

```python
"""
Render the Crystal Meet room setup guide to PDF with the venv's Chromium.

Usage: python build.py [output.pdf]   (default: ../crystal-meet-room-setup.pdf)
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE.parent / "crystal-meet-room-setup.pdf"

FOOTER = """
<div style="width:100%;font-family:Lexend,'Segoe UI',Arial,sans-serif;font-size:7.5pt;color:#647087;
            padding:0 0.6in;display:flex;justify-content:space-between;">
  <span>How-To Guide · Set up a Crystal Meet room</span>
  <span>Page <span class="pageNumber"></span></span>
</div>
"""


def build(out: Path) -> None:
    for asset in ("index.html", "crystalpm-logo-white.svg", "fonts/lexend-400.woff2", "fonts/lexend-600.woff2"):
        if not (HERE / asset).is_file():
            raise SystemExit(f"missing asset: {asset}")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto((HERE / "index.html").as_uri(), wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        page.pdf(
            path=str(out),
            format="Letter",
            print_background=True,
            display_header_footer=True,
            header_template="<span></span>",
            footer_template=FOOTER,
            margin={"top": "0.6in", "bottom": "0.7in", "left": "0.6in", "right": "0.6in"},
        )
        browser.close()
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT)
```

- [ ] **Step 4: Write the guide**

Create `docs/guides/crystal-meet-room-setup/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Set up a Crystal Meet room</title>
<style>
@font-face { font-family: "Lexend"; src: url("fonts/lexend-400.woff2") format("woff2"); font-weight: 400; }
@font-face { font-family: "Lexend"; src: url("fonts/lexend-600.woff2") format("woff2"); font-weight: 600; }
:root {
  --navy-900: #001636; --navy-800: #16244F; --blue-600: #1B52E5; --blue-700: #003EBC;
  --periwinkle-300: #BDCEFF; --tint-100: #EDF2FE; --tint-50: #F7F9FF; --slate-500: #647087; --ink-900: #1C2024;
  --radius: 12px; --radius-sm: 8px;
}
@page { size: Letter; }
* { box-sizing: border-box; }
body { margin: 0; font-family: "Lexend", "Segoe UI", Arial, sans-serif; color: var(--ink-900); font-size: 10.5pt; line-height: 1.5; }
h1, h2, h3 { font-weight: 600; line-height: 1.15; margin: 0.2em 0; }
h2 { font-size: 16pt; color: var(--navy-800); }
p { margin: 0.4em 0; }
.kicker { font-size: 9pt; font-weight: 600; letter-spacing: 0.15em; text-transform: uppercase; color: var(--blue-600); margin: 0 0 6px; }
.banner { background: var(--navy-800); color: #fff; border-radius: var(--radius); padding: 28px 32px; margin-bottom: 20px; }
.banner .kicker { color: var(--periwinkle-300); }
.banner .kicker.top { color: #fff; margin-bottom: 2px; }
.banner h1 { font-size: 26pt; margin: 6px 0 8px; }
.banner p { color: var(--periwinkle-300); font-size: 11pt; margin: 0; max-width: 60ch; }
.banner img { height: 26px; display: block; margin-bottom: 18px; }
.intro { font-size: 11pt; margin: 0 0 18px; max-width: 78ch; }
.glance { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 12px; margin: 0 0 22px; }
.glance .card { border-top: 4px solid var(--blue-600); }
.glance .card:nth-child(2) { border-top-color: #3B6DF0; }
.glance .card:nth-child(3) { border-top-color: #6C92F5; }
.glance .card:nth-child(4) { border-top-color: var(--periwinkle-300); }
.card { border-radius: var(--radius); padding: 14px 16px; background: #fff; border: 1px solid var(--tint-100); }
.card .kicker { font-size: 8pt; }
.card h3 { font-size: 11.5pt; margin: 2px 0 4px; }
.card p { margin: 0; color: var(--slate-500); font-size: 9.5pt; }
.step { display: flex; align-items: center; gap: 12px; margin: 22px 0 8px; break-after: avoid; }
.badge { width: 30px; height: 30px; border-radius: 8px; background: var(--blue-600); color: #fff; font-weight: 600; display: inline-flex; align-items: center; justify-content: center; font-size: 12pt; }
ul { margin: 6px 0 10px; padding-left: 0; list-style: none; }
li { position: relative; padding-left: 18px; margin: 5px 0; }
li::before { content: ""; position: absolute; left: 2px; top: 0.6em; width: 7px; height: 7px; border-radius: 50%; background: var(--blue-600); }
.ui { font-weight: 600; color: var(--blue-600); }
.chip { font-weight: 600; color: var(--blue-600); background: var(--tint-100); border-radius: 6px; padding: 1px 8px; white-space: nowrap; }
.see { color: var(--slate-500); font-size: 9.5pt; margin: 4px 0 0 18px; }
.callout { border-radius: var(--radius-sm); padding: 12px 18px; margin: 12px 0; border-left: 4px solid var(--blue-600); background: var(--tint-100); break-inside: avoid; }
.callout.warn { background: #FFF8E1; border-left-color: #E8A013; }
.callout.tip { background: #EDFBF3; border-left-color: #1FA971; }
.callout .kicker { margin-bottom: 4px; }
.callout p { margin: 2px 0; }
.trouble { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.trouble .card h3 { font-size: 10.5pt; }
.page-break { break-before: page; }
</style>
</head>
<body>

<section class="banner">
  <img src="crystalpm-logo-white.svg" alt="Crystal PM">
  <p class="kicker top">Crystal PM</p>
  <p class="kicker">How-to guide</p>
  <h1>Set up a Crystal Meet room</h1>
  <p>Put a Crystal Meet device in a conference room and join Zoom and Google Meet calls from the room's screen, step by step.</p>
</section>

<p class="intro">This guide takes one conference room from an empty Raspberry Pi to a room that shows up in the Crystal Meet dashboard and joins calls from its own web page. Plan about 45 minutes per room, most of it waiting for downloads. No programming experience is needed: if you can follow a recipe and type the commands printed here, you can do this. Repeat it for each room.</p>

<div class="glance">
  <div class="card"><p class="kicker">Step 1</p><h3>Prepare the device</h3><p>Install Raspberry Pi OS, sign in, connect the TV and camera.</p></div>
  <div class="card"><p class="kicker">Step 2</p><h3>Create the room</h3><p>Add the room in the dashboard and copy its token. <span class="ui">Provisioning</span></p></div>
  <div class="card"><p class="kicker">Step 3</p><h3>Install Crystal Meet</h3><p>One command with the room's config file.</p></div>
  <div class="card"><p class="kicker">Step 4</p><h3>First run and screens</h3><p>Join a test call, then point the table screen and the door sign at the device.</p></div>
</div>

<div class="callout warn">
  <p class="kicker">Before you begin</p>
  <p><strong>Hardware per room.</strong> Raspberry Pi 4 (4 GB) or Pi 5, the official power supply, a 32 GB microSD card, an HDMI cable to the room's TV, and a USB camera or speakerphone (a Logitech C920 or a Jabra Speak works well).</p>
  <p><strong>Two screens on the network.</strong> A touch tablet or PoE touch panel on the table to join and control meetings, and a small screen by the door to show whether the room is free. Neither connects to the Pi: each only needs a browser, power, and the office network. Give the room device a fixed address on your router (a DHCP reservation) so the screens can always find it.</p>
  <p><strong>The dashboard address.</strong> The Crystal Meet dashboard must be reachable from the room's network, for example <span class="chip">http://192.168.1.20:3000</span> for the dashboard and port <span class="chip">3001</span> for devices. If the dashboard runs on a Windows PC under WSL2, turn on mirrored networking or forward ports 3000 and 3001 first, or the devices will show Offline.</p>
  <p><strong>Accounts.</strong> A dashboard sign-in with the admin role, and the Zoom or Google Meet links you want to test with.</p>
</div>

<div class="step"><span class="badge">1</span><h2>Prepare the device</h2></div>
<ul>
  <li><strong>Flash the card.</strong> On your computer, open <span class="ui">Raspberry Pi Imager</span>, choose your Pi model, then <span class="ui">Raspberry Pi OS (64-bit)</span> with desktop, and your microSD card.</li>
  <li><strong>Pre-fill the settings.</strong> When Imager offers customisation, set the hostname to something like <span class="chip">crystal-meet-room-1</span>, the username to <span class="chip">pi</span> with a password you keep, your Wi-Fi network, and turn on SSH. Write the card.</li>
  <li><strong>Connect and boot.</strong> Put the card in the Pi, plug in the TV over HDMI, the camera or speakerphone over USB, then power. Wait for the desktop to appear on the TV.</li>
  <li><strong>Turn on automatic sign-in.</strong> Open <span class="ui">Preferences › Raspberry Pi Configuration › System</span> and set <span class="ui">Auto Login</span> to on, so the room comes back by itself after a power cut.</li>
</ul>
<p class="see">You should now see the Raspberry Pi desktop on the room's TV, and the Pi should be on the office network.</p>

<div class="step"><span class="badge">2</span><h2>Create the room in the dashboard</h2></div>
<ul>
  <li><strong>Open the dashboard</strong> at its address on port 3000 and sign in.</li>
  <li><strong>Go to</strong> <span class="ui">Provisioning</span> and fill in the <span class="ui">Room name</span> exactly as people call the room (for example <span class="chip">Room 1</span>) and its <span class="ui">Location</span>.</li>
  <li><strong>Create the token</strong> and copy it. It is a long string of letters and numbers; you will paste it into the device's config in the next step.</li>
</ul>
<p class="see">You should now see the token on the page and the room listed as pending until its device connects.</p>
<div class="callout">
  <p class="kicker">Good to know</p>
  <p>The token links one device to this room and works once. If a device is ever reinstalled, create a new token for it. Nothing else about the room needs to change.</p>
</div>

<div class="step"><span class="badge">3</span><h2>Install Crystal Meet on the device</h2></div>
<ul>
  <li><strong>Open a terminal</strong> on the Pi (the black icon in the top bar), or connect over SSH from your computer.</li>
  <li><strong>Get the software.</strong> Type <span class="chip">sudo apt update && sudo apt install -y git</span>, then <span class="chip">git clone https://github.com/ben-abeo/croom.to.git</span>, then <span class="chip">cd croom.to</span>.</li>
  <li><strong>Make the room's config.</strong> Type <span class="chip">cp deploy/rooms/room-1.yaml ~/room.yaml</span> (use <span class="chip">room-2.yaml</span> or <span class="chip">room-3.yaml</span> for the other rooms), then <span class="chip">nano ~/room.yaml</span>.</li>
  <li><strong>Fill in three values.</strong> The room's <span class="ui">name</span> and <span class="ui">location</span>; the dashboard <span class="ui">url</span> as <span class="chip">http://&lt;dashboard address&gt;:3001</span>; and the <span class="ui">enrollment_token</span> you copied. Save with <span class="ui">Ctrl+O</span>, Enter, then <span class="ui">Ctrl+X</span>.</li>
  <li><strong>Run the installer.</strong> Type <span class="chip">sudo bash installer/install.sh --config ~/room.yaml</span>. It installs the browser and the room software; expect 10 to 20 minutes.</li>
  <li><strong>Start it.</strong> When the installer prints <span class="ui">Crystal Meet installation complete</span>, type <span class="chip">sudo systemctl start croom</span>. The service also starts on every boot from now on.</li>
</ul>
<p class="see">You should now see a browser window open on the TV. That window is the room's meeting screen; leave it open.</p>

<div class="step"><span class="badge">4</span><h2>First run and the screens</h2></div>
<ul>
  <li><strong>Open the room page.</strong> On your laptop or phone on the office network, go to <span class="chip">http://crystal-meet-room-1.local:8080</span>, or use the device's IP address with <span class="chip">:8080</span>.</li>
  <li><strong>Check the dashboard.</strong> On <span class="ui">Devices</span>, the room should show <span class="ui">Online</span> within a minute.</li>
  <li><strong>Join a test call.</strong> Paste a Zoom link, or a Zoom meeting ID, into <span class="ui">Join with a link</span> and press <span class="ui">Join</span>. The TV shows the meeting joining; the page turns blue and offers <span class="ui">Mute</span>, <span class="ui">Turn camera off</span> and <span class="ui">Leave</span>.</li>
  <li><strong>Leave.</strong> Press <span class="ui">Leave</span>, then <span class="ui">Tap again to leave</span>.</li>
  <li><strong>Set up the table screen.</strong> On the tablet or panel, open its kiosk browser (Fully Kiosk Browser on Android, Guided Access with Safari on an iPad) and set the start page to <span class="chip">http://&lt;device address&gt;:8080/</span>. Turn on full screen and keep the screen awake.</li>
  <li><strong>Set up the door sign.</strong> On the door screen, set the start page to <span class="chip">http://&lt;device address&gt;:8080/sign</span>. It shows green when the room is free, amber when a meeting is about to start, and red while the room is in use or booked.</li>
</ul>
<p class="see">You should now see the room page say the room is free again, the door sign green, and the dashboard still showing the device Online.</p>
<div class="callout tip">
  <p class="kicker">Tip</p>
  <p>Zoom links that include their passcode (the part after <span class="chip">?pwd=</span>) join straight through. Google Meet joins as a guest, so someone in the meeting admits the room when it asks.</p>
</div>

<div class="page-break"></div>
<p class="kicker">If something is off</p>
<h2>Troubleshooting</h2>
<div class="trouble">
  <div class="card"><h3>The room page does not load</h3><p>On the Pi, type <span class="chip">sudo systemctl status croom</span>. If it is not running, type <span class="chip">sudo journalctl -u croom -n 50</span> and look for the first line marked ERROR. Make sure port 8080 is not used by something else on the device.</p></div>
  <div class="card"><h3>The device shows Offline</h3><p>From the Pi, type <span class="chip">curl http://&lt;dashboard address&gt;:3001/health</span>. If nothing comes back, the dashboard is not reachable from the room's network: check the address in <span class="chip">~/room.yaml</span> and the dashboard PC's networking. If the token was already used, create a new one and run the installer again with the updated config.</p></div>
  <div class="card"><h3>The browser opens but never joins</h3><p>Zoom needs the passcode inside the link; paste the full invitation link. Google Meet waits until someone in the meeting admits "the room". If the page says it could not join, press <span class="ui">Dismiss</span> and try the link again.</p></div>
  <div class="card"><h3>The screens cannot open the page</h3><p>Use the device's IP address rather than its <span class="chip">.local</span> name; many tablets do not resolve those. Check that the screen and the device are on the same network, then open <span class="chip">http://&lt;device address&gt;:8080/api/status</span> from a laptop to confirm the device answers.</p></div>
  <div class="card"><h3>No sound or picture in the call</h3><p>Check the USB camera or speakerphone is connected before the device starts, then reboot the Pi. The service runs as the signed-in desktop user so it can use the room's screen and audio.</p></div>
</div>
<div class="callout">
  <p class="kicker">Good to know</p>
  <p>Under the hood the device software is still called <span class="chip">croom</span>: that is the name you will see in service and log commands. Everything people see says Crystal Meet.</p>
</div>

</body>
</html>
```

- [ ] **Step 5: Build the PDF and run the tests**

Run: `.venv/bin/python docs/guides/crystal-meet-room-setup/build.py` then `.venv/bin/pytest tests/unit/docs -q -p no:cacheprovider`
Expected: `wrote docs/guides/crystal-meet-room-setup.pdf (...)` and the tests pass.

- [ ] **Step 6: Look at the guide**

Render each page to an image for review: open `index.html` in the venv's Chromium at 816×1056 with `page.emulate_media(media="print")` and take a full-page screenshot, or open the PDF in a viewer on Windows. Check the brand list: kicker labels present, cards aligned, footer on every page with a page number, one theme only, headlines in sentence case, whitespace not cramped. Fix and rebuild before committing.

- [ ] **Step 7: Run the whole suite** (Global Constraints). Expected: at most 87 failed.

- [ ] **Step 8: Commit**

```bash
git add docs/guides/crystal-meet-room-setup docs/guides/crystal-meet-room-setup.pdf tests/unit/docs
git commit -m "docs: Crystal Meet room setup guide"
```

---

### Task 5: The door sign page

**Files:**
- Create: `src/croom/control/static/sign.html`, `src/croom/control/static/sign.css`, `src/croom/control/static/sign.js`
- Modify: `src/croom/control/service.py` (route)
- Test: `tests/unit/control/test_service.py`, `tests/unit/control/test_page.py`

**Interfaces:**
- Consumes: `GET /api/status` and `GET /api/calendar/events` (unchanged); the assets from Task 1.
- Produces: `GET /sign`. The guide (Task 4) points the door screen at it.

- [ ] **Step 1: Write the failing tests**

Append to `class TestPage` in `tests/unit/control/test_service.py`:

```python
    async def test_sign_is_served_uncached(self, client_factory):
        client = await client_factory(make_service())
        resp = await client.get("/sign")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/html")
        assert resp.headers["Cache-Control"] == "no-cache"
        body = await resp.text()
        assert "<title>Crystal Meet</title>" in body
        assert 'src="/static/sign.js"' in body and 'href="/static/sign.css"' in body
        assert (await client.get("/static/sign.js")).status == 200
        assert (await client.get("/static/sign.css")).status == 200
```

Append to `tests/unit/control/test_page.py`:

```python
def test_door_sign_follows_the_room_state(browser):
    with PageServer(calendar_events=[event("e1", "Design review", 25)], room_name="Room 1") as server:
        page = browser.new_page(viewport={"width": 1024, "height": 600})
        page.goto(f"http://127.0.0.1:{server.port}/sign", wait_until="networkidle")
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=5000)
        assert page.locator("#headline").inner_text().startswith("Free until")
        assert page.locator("#kicker").inner_text().upper() == "AVAILABLE"
        assert page.locator("#actions").count() == 0
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/join",
                          data='{"url": "https://zoom.us/j/98765432100"}',
                          headers={"Content-Type": "application/json"})
        page.wait_for_function("document.body.dataset.state === 'occupied'", timeout=8000)
        assert page.locator("#headline").inner_text() == "In use"
        assert page.locator("#kicker").inner_text().upper() == "IN USE"
        page.request.post(f"http://127.0.0.1:{server.port}/api/meeting/leave",
                          data="{}", headers={"Content-Type": "application/json"})
        page.wait_for_function("document.body.dataset.state === 'free'", timeout=8000)
        page.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/control/test_service.py -q -p no:cacheprovider -k test_sign` and `.venv/bin/pytest tests/unit/control/test_page.py -q -p no:cacheprovider -k door_sign`
Expected: FAIL with `404` for `/sign` and the browser test timing out on the missing page.

- [ ] **Step 3: Add the route**

In `src/croom/control/service.py`, in `create_app`, directly after `app.router.add_get("/", self._handle_index)` add:

```python
        app.router.add_get("/sign", self._handle_sign)
```

and after `_handle_index` add:

```python
    async def _handle_sign(self, request: web.Request) -> web.StreamResponse:
        sign = self._static_dir / "sign.html"
        if not sign.is_file():
            return web.Response(text="Door sign assets are missing.", status=500)
        return web.FileResponse(sign, headers={"Cache-Control": "no-cache"})
```

- [ ] **Step 4: Create the sign page**

Create `src/croom/control/static/sign.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crystal Meet</title>
<link rel="icon" href="/static/crystalpm-logo-white.svg" type="image/svg+xml">
<link rel="stylesheet" href="/static/sign.css">
</head>
<body data-state="loading">
<main class="sign">
  <header class="top">
    <div class="brand">
      <img class="logo" src="/static/crystalpm-logo-white.svg" alt="Crystal PM">
      <div>
        <h1 id="room-name">Room</h1>
        <p id="room-location" class="location"></p>
      </div>
    </div>
    <p id="clock" class="clock"></p>
  </header>

  <section class="status" aria-live="polite">
    <p id="kicker" class="kicker">Connecting</p>
    <p id="headline" class="headline">Connecting</p>
    <p id="detail" class="detail"></p>
  </section>

  <section class="upcoming">
    <p class="kicker">Today</p>
    <ul id="events" class="events"></ul>
    <p id="calendar-note" class="note"></p>
  </section>
</main>
<script src="/static/sign.js"></script>
</body>
</html>
```

Create `src/croom/control/static/sign.css`:

```css
/* Crystal Meet door sign: the background is the room's status. */
@font-face { font-family: "Lexend"; src: url("/static/fonts/lexend-400.woff2") format("woff2"); font-weight: 400; font-display: swap; }
@font-face { font-family: "Lexend"; src: url("/static/fonts/lexend-600.woff2") format("woff2"); font-weight: 600; font-display: swap; }

:root {
  --navy-900: #001636;
  --navy-800: #16244F;
  --periwinkle-300: #BDCEFF;
  --free: #1FA971;
  --soon: #E8A013;
  --busy: #B42318;
  --gutter: clamp(20px, 4vw, 56px);
  --font: "Lexend", "Segoe UI", Arial, sans-serif;
}

* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  background: var(--navy-900);
  color: #fff;
  font-family: var(--font);
  font-size: clamp(16px, 1.6vw, 24px);
  line-height: 1.4;
  transition: background-color 500ms ease, color 500ms ease;
}
@media (prefers-reduced-motion: reduce) { body { transition: none; } }

body[data-state="free"] { background: var(--free); }
body[data-state="soon"] { background: var(--soon); color: var(--navy-800); }
body[data-state="occupied"] { background: var(--busy); }
body[data-state="offline"], body[data-state="loading"] { background: var(--navy-900); }

.sign {
  min-height: 100%;
  padding: var(--gutter);
  display: grid;
  grid-template-rows: auto 1fr auto;
  gap: clamp(20px, 3vh, 40px);
}
.top { display: flex; justify-content: space-between; align-items: flex-start; gap: 24px; }
.brand { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.logo { width: clamp(110px, 12vw, 170px); height: auto; display: block; }
body[data-state="soon"] .logo { filter: brightness(0) saturate(100%) invert(11%) sepia(38%) saturate(2400%) hue-rotate(205deg) brightness(90%); }
h1 { margin: 0; font-size: clamp(1.3rem, 2.6vw, 2.4rem); font-weight: 600; line-height: 1.15; overflow-wrap: anywhere; }
.location { margin: 2px 0 0; opacity: 0.85; }
.clock {
  margin: 0;
  font-size: clamp(2.2rem, 7vw, 6rem);
  font-weight: 400;
  line-height: 1;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.kicker {
  margin: 0 0 10px;
  font-size: clamp(0.75rem, 1.4vw, 1.1rem);
  font-weight: 700;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  opacity: 0.9;
}
.status { align-self: center; }
.headline {
  margin: 0;
  font-size: clamp(2.4rem, 9vw, 8rem);
  font-weight: 600;
  line-height: 1.02;
  letter-spacing: -0.02em;
  max-width: 14ch;
  overflow-wrap: anywhere;
}
.detail { margin: 14px 0 0; font-size: clamp(1.1rem, 2.4vw, 2rem); max-width: 40ch; overflow-wrap: anywhere; opacity: 0.92; }
.detail:empty { display: none; }

.events { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
.event { display: grid; grid-template-columns: 7ch 1fr; gap: 14px; align-items: baseline; }
.event time { font-variant-numeric: tabular-nums; opacity: 0.85; }
.event .title { margin: 0; overflow-wrap: anywhere; }
.note { margin: 6px 0 0; opacity: 0.85; }
.note:empty { display: none; }
```

Create `src/croom/control/static/sign.js`:

```javascript
(function () {
  "use strict";

  const SOON_MS = 10 * 60 * 1000;
  const STATUS_EVERY_MS = 5000;
  const EVENTS_EVERY_MS = 60000;
  const IN_PROGRESS = ["joining", "in_lobby", "connected", "leaving"];

  const el = (id) => document.getElementById(id);
  const model = { status: null, events: [], offline: false };

  const platformNames = { zoom: "Zoom", google_meet: "Google Meet", teams: "Teams", webex: "Webex" };
  const platformName = (key) => platformNames[key] || key || "";
  const fmtTime = (d) => d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const plural = (n, word) => n + " " + word + (n === 1 ? "" : "s");

  async function getJson(path) {
    const response = await fetch(path);
    if (!response.ok) throw new Error("Request failed (" + response.status + ")");
    return response.json();
  }

  async function refreshStatus() {
    try {
      model.status = await getJson("/api/status");
      model.offline = false;
    } catch (e) {
      model.offline = true;
    }
    render();
  }

  async function refreshEvents() {
    try {
      model.events = (await getJson("/api/calendar/events")).events || [];
    } catch (e) {
      // keep the last list
    }
    render();
  }

  function setStatus(state, kicker, headline, detail) {
    document.body.dataset.state = state;
    el("kicker").textContent = kicker;
    el("headline").textContent = headline;
    el("detail").textContent = detail || "";
  }

  function render() {
    if (model.offline || !model.status) {
      setStatus("offline", "Not connected", "Sign not connected", "Check that Crystal Meet is running on the room's device.");
      return;
    }
    const s = model.status;
    el("room-name").textContent = s.room.name;
    el("room-location").textContent = s.room.location;
    const m = s.meeting;
    const cal = s.calendar;
    const current = cal.current;
    const next = cal.next;
    const now = Date.now();

    if (IN_PROGRESS.includes(m.state)) {
      const until = current ? " until " + fmtTime(new Date(current.end_time)) : "";
      setStatus("occupied", "In use", "In use" + until, m.title || (current ? current.title : platformName(m.platform)));
    } else if (current) {
      setStatus("occupied", "Booked", "Booked until " + fmtTime(new Date(current.end_time)), current.title);
    } else if (next && new Date(next.start_time).getTime() - now <= SOON_MS) {
      const minutes = Math.max(0, Math.round((new Date(next.start_time).getTime() - now) / 60000));
      setStatus("soon", "Starting soon", minutes === 0 ? next.title + " is starting" : next.title + " starts in " + plural(minutes, "minute"), fmtTime(new Date(next.start_time)) + " to " + fmtTime(new Date(next.end_time)));
    } else if (next) {
      setStatus("free", "Available", "Free until " + fmtTime(new Date(next.start_time)), "Next: " + next.title);
    } else {
      setStatus("free", "Available", cal.connected ? "Free for the rest of the day" : "Free", cal.connected ? "Nothing else is booked in here today." : "");
    }

    renderUpcoming(cal, now);
  }

  function renderUpcoming(cal, now) {
    const list = el("events");
    const note = el("calendar-note");
    list.replaceChildren();
    if (!cal.connected) {
      note.textContent = "";
      return;
    }
    const upcoming = model.events.filter((ev) => new Date(ev.end_time).getTime() > now).slice(0, 3);
    note.textContent = upcoming.length ? "" : "Nothing else scheduled today.";
    for (const ev of upcoming) {
      const li = document.createElement("li");
      li.className = "event";
      const time = document.createElement("time");
      time.dateTime = ev.start_time;
      time.textContent = fmtTime(new Date(ev.start_time));
      const title = document.createElement("p");
      title.className = "title";
      title.textContent = ev.title;
      li.append(time, title);
      list.append(li);
    }
  }

  function tick() {
    el("clock").textContent = fmtTime(new Date());
  }

  tick();
  setInterval(tick, 1000);
  refreshStatus();
  refreshEvents();
  setInterval(refreshStatus, STATUS_EVERY_MS);
  setInterval(refreshEvents, EVENTS_EVERY_MS);
})();
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/control/test_service.py tests/unit/control/test_page.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Look at the sign**

Serve the page with stub services and capture `/sign` at 1024×600 (landscape) and 600×1024 (portrait) in the free state, then after an API join in the in-use state. Check: green and red backgrounds, the logo, the headline readable at arm's length when the capture is viewed small, no overflow in portrait. Fix before committing.

- [ ] **Step 7: Run the whole suite** (Global Constraints). Expected: at most 87 failed.

- [ ] **Step 8: Commit**

```bash
git add src/croom/control/static/sign.html src/croom/control/static/sign.css src/croom/control/static/sign.js src/croom/control/service.py tests/unit/control/test_service.py tests/unit/control/test_page.py
git commit -m "feat(control): door sign page"
```

---

### Task 6: Push and final check

**Files:** none new.

- [ ] **Step 1: Confirm nothing user-facing says Croom**

Run: `grep -rn "Croom" src/croom/control/static src/croom-dashboard/frontend/src src/croom-dashboard/frontend/index.html deploy docs/guides/crystal-meet-room-setup/index.html`
(the sign and room pages included)
Expected: no output.

- [ ] **Step 2: Run the whole suite** (Global Constraints). Expected: at most 87 failed.

- [ ] **Step 3: Push**

```bash
git push origin crystal-meet
```
