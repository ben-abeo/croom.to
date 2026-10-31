# Google Meet controls, Phase A: notices and the capture — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Tasks give requirements, interfaces and tests in
> prose; implementers write the code test-first in the style of the modules they touch.

**Goal:** Meet's harmless notices ("Others may see your video differently … Got it") are pressed for the room by the same policy as
Zoom's dialogs, and a Settings › Check button captures Meet's real page (redacted) for Phase B.

**Architecture:** page scripts in a new `croom.meeting.meet_page`; `GoogleMeetProvider` reads dialogs with its regular reading and applies
`DialogPolicy`; `croom.meeting.meet_capture` drives the panels and writes a zip; two PIN-protected routes and a Check page button.

**Spec:** `docs/superpowers/specs/2026-10-30-meet-controls-design.md`

## Global Constraints

- Branch `meet-controls`; no attribution lines in commits; tests `.venv/bin/python -m pytest <paths> -q` (asyncio auto); headless
  Chromium from the venv's Playwright for page-script tests (skip when missing, as test_page.py does).
- Words, verbatim: the default acknowledge list gains `"others may see your video differently"`; a never-pressed button is any whose label
  has the whole word leave, end, exit or quit (`DialogPolicy.NEVER_PRESSED`, unchanged).
- Nothing in a test reaches meet.google.com; Meet pages are built locally.
- Keep everything that works for Meet today working (join, mute, camera, hand, captions, the people count, the C shortcut rule).

## Review Focus

- A box with a real choice ("Let X in?", "Leave call?", "Remove …?") is never pressed for the room.
- A notice pressed once is not pressed again and again (each dialog keyed by its words while it stays on the screen).
- The capture never leaves Meet's panels open on the TV, even when a step fails, and never presses anything but open/close (no Admit, no
  reaction, no send).
- Redaction: no participant name, email address or chat text in the recorded elements.

---

### Task 1: Meet's notices

**Files:** create `src/croom/meeting/meet_page.py`; modify `src/croom/meeting/providers/google_meet.py`, `src/croom/core/config.py`
(`DEFAULT_ACKNOWLEDGE`), tests `tests/unit/meeting/test_meet_dialogs.py`.

**Requirements:**
- `READ_DIALOGS` (JS in meet_page): every drawn, visible `[role=dialog]` / `[role=alertdialog]` (and Meet's notice cards if they use
  another role — check the photo's structure is covered by what the fixture builds), each as `{"title", "text", "buttons": [labels of its
  drawn buttons, in order]}`; a stable key per dialog = its title + text (Meet gives no ids).
- The provider: dialogs read every 2 s while connected (a separate light loop, or the existing reading at 2 s — keep the existing 5 s
  button reading as it is if splitting), and once right after the join; `DialogPolicy(config.meeting.dialogs.acknowledge,
  ask_everything)` built in `from_config`; an acknowledged notice pressed by clicking the button with that label inside that dialog, at
  most one press per 1 s, never twice for the same key while it is on the screen, its disclosure sentence added to `info.notice` as
  Zoom's are; other dialogs kept in a queue, the oldest on `info.dialog` (`{"id", "title", "text", "buttons"}`, id a small int per key);
  `control("dialog", {"id", "button"})` presses that button in that dialog (refused in words when it has gone); a dialog no longer read
  is forgotten. A press that fails puts the dialog on the table.
- `DEFAULT_ACKNOWLEDGE` gains `"others may see your video differently"`.

**Tests:** the reader against `set_content` pages: the photo's notice ("Others may see your video differently" / "Your self view tile may
show less of your background than others see. To view your entire video, click the icon button on the bottom right of your tile." /
"Got it"), a two-button "Someone wants to join" box (Admit / Deny), a "Leave call?" box, hidden dialogs ignored. The provider with a fake
page: the notice pressed once, its line on the notice; the two-button box on `info.dialog`; answered through control; forgotten when gone;
Leave never pressed; a press that fails goes to the table; config default contains the phrase.

---

### Task 2: The capture

**Files:** create `src/croom/meeting/meet_capture.py`; modify `src/croom/meeting/meet_page.py` (the element recorder),
`src/croom/control/service.py` (routes), `src/croom/control/static/settings-tools.js` (the Check page's button and download); tests
`tests/unit/meeting/test_meet_capture.py`, control route and page tests.

**Requirements:**
- `RECORD_ELEMENTS` (JS): every drawn element with a role, an accessibility label, or button/input/textarea/li semantics: tag, role,
  aria-label, aria-pressed/expanded/checked/selected, data-* attribute names (not values), its own visible text (first 120 chars), its
  bounding box; plus the document's title and URL path (no query).
- `MeetCapture(page, redact=..., screenshots=True)`: steps — the call as it is; chat panel; people panel; reactions picker; more options
  menu; Change layout dialog; the room's own tile menu; host controls (when Meet shows them); any dialog on screen. Each step opens by
  accessibility label first and Meet's keyboard shortcut second, waits for the panel, records, screenshots (when on), then closes it
  (Escape / the close button) and checks it closed; a step that cannot open is recorded as `{"opened": false, "why": …}` and the capture
  goes on. Never clicks Admit, Deny, a reaction, Send, Remove, Mute everyone, End, Leave. Always ends with every panel closed (a
  `finally`). Limited to 60 s in all.
- Redaction: names (from the people panel's list items and the participant tiles' name labels), email addresses (regex), and the chat
  panel's message texts are replaced by `Person 1…`, `person1@example.com`, `Message 1…` in every recorded text and label, consistently.
- Output: `meet-capture-<UTC time>.zip` in the data directory (keep the newest 3): `capture.json` (steps, elements, versions: Chromium,
  Meet's page `<html>` lang and the app's version) and `NN-<step>.png`.
- Routes: `POST /api/settings/meet-capture {"screenshots": bool}` (PIN; 409 "Join a Google Meet call first" unless Meet is connected; 409
  while one runs) → `{"file", "steps": [{name, opened}]}`; `GET /api/settings/meet-capture` (PIN) → the newest zip as a download.
- The Check page: **Capture Google Meet** with the screenshots switch, the result per step, and **Download**.

**Tests:** against a local fake Meet page (buttons with Meet's labels opening fake panels): every step opens, records and closes; a step
that cannot open is recorded and the rest go on; panels closed after a failure; nothing pressed beyond open/close; redaction of names,
emails and chat text in labels and texts; the zip's contents; the routes (PIN, not in a Meet call, one at a time); the page.
