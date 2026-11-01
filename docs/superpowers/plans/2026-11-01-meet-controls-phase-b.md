# Google Meet controls, Phase B: the touch Pi's screens for Meet, Zoom pin, the Meet API list — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Tasks give requirements, interfaces and tests in
> prose; implementers write the code test-first in the style of the modules they touch.

**Goal:** a Google Meet call on the touch Pi has what a Zoom call has (chat read and send, reactions, participants with Admit / Deny /
Pin, the view, hide self view, and the host controls when the room is a host or co-host); Zoom gains Pin; and, for the organization's own
meetings, the participants list comes from Google's Meet REST API.

**Architecture:** every word and shortcut the room drives Meet with stays in `croom.meeting.meet_page` (one table, `MEET`), so the
capture Ben runs later corrects them in one place. `GoogleMeetProvider` opens Meet's chat, people and host panels on the TV only while the
matching screen is open on the touch Pi, reads them into the same model Zoom fills (`info.controls`, `info.people`, `info.chat`), and
carries out the controls by pressing Meet's own buttons. The room page gains Send, Deny, Pin, a choice of views and the host switches,
each shown only when the model offers it. Zoom's bridge gains Pin. `croom.meeting.meet_api` reads the list from the Meet REST API.

**Tech Stack:** Python 3.11 asyncio, aiohttp, Playwright (Chromium), plain JS room page, google-auth.

**Spec:** `docs/superpowers/specs/2026-10-30-meet-controls-design.md` (sections 3 "Phase B on the touch Pi", 4, 5).

**Built before the capture (Ben, 2026-11-01: "Can we just start the next phases?").** Meet's labels are the best known English
(listed in Task 1) until Ben's capture confirms them; a control whose button Meet does not show is refused in words ("Meet's chat
button isn't on the screen"), never silent. A follow-up task after the capture corrects `MEET` and commits the redacted capture as
fixtures.

## Global Constraints

- Branch `meet-controls-b` off `main` (9b89716); no attribution lines in commits; tests `.venv/bin/python -m pytest <paths> -q`;
  headless Chromium from the venv's Playwright for page-script tests (skip when missing, as test_page.py does).
- **Tests only where they guard real risk** (Ben, 2026-11-01: "Don't write tests for everything you do though. It is not
  necessary."): the Review Focus items and the safety rules below. No exhaustive coverage, no tests that restate the code.
- Nothing in a test reaches meet.google.com, zoom.us or googleapis.com; Meet pages are built locally (extend the capture tests' fake
  Meet page, `tests/unit/meeting/test_meet_capture.py`).
- Never pressed for the room without a tap that names it: Leave, End call for everyone, Remove, Mute everyone, Admit, Deny. End and
  Mute everyone are confirmed on the touch Pi (a second tap, as Zoom's End is today); Remove as Zoom's is (Tap again to remove).
- Lock order in the provider stays `_dialog_turn` then `_pressing`. The capture (Phase A) holds `_dialog_turn` for its run; nothing in
  this plan opens or closes a panel while it runs (a control meanwhile is refused at once: "The room is capturing Meet's page; try again in a minute").
- Keep everything that works for Meet and Zoom today working (join, mute, camera, hand, captions, the people count, the C shortcut
  rule, Meet's notices, the capture, every Zoom control).

## Review Focus

1. A panel the room opened is closed when its screen on the touch Pi closes, when the touch Pi stops saying it is open (the page went
   away: 3 minutes with no renewal), when the call ends, and before the capture starts; it is never left on the TV.
2. A panel the room opened is never taken for one of Meet's boxes (the box loop must not queue the chat or people panel as a dialog,
   nor press anything in it).
3. Chat Send types only into Meet's chat message box, and only after checking that box has the keyboard; otherwise it refuses in
   words and nothing is typed (keys typed elsewhere in Meet are shortcuts: C, Ctrl+D…).
4. An action on a person (Pin, Remove, Admit, Deny) acts on that person or on nobody: the person is found again by the id or name
   the touch Pi sent at the moment of the press; two people with that name, or none, is a refusal in words.
5. End call for everyone and Mute everyone happen only after the touch Pi's second tap, and Meet's own confirmation box for them is
   answered by the action itself (the box loop does not queue or press it meanwhile).

---

### Task 1: Meet's page, read and driven (meet_page)

**Files:** modify `src/croom/meeting/meet_page.py`; tests `tests/unit/meeting/test_meet_page_b.py` (fixtures beside the capture
tests' fake Meet page; reuse it).

**Requirements:**
- `MEET`: one table (a Python dict passed to the page scripts as their argument) of every label, text and shortcut used below, each with
  its alternatives, matched case-insensitively on the start of an accessibility label or a button's text. Starting values (Meet's English, unconfirmed):
  chat `["Chat with everyone", "Chat"]`, shortcut `Control+Alt+c`; people `["Show everyone", "People"]`, `Control+Alt+p`; host controls
  `["Host controls"]`; reactions `["Send a reaction", "Reactions"]`; more options `["More options"]`; change layout menu item
  `["Change layout"]`; layouts `{"auto": ["Auto", "Auto (dynamic)"], "tiled": ["Tiled"], "spotlight": ["Spotlight"], "sidebar":
  ["Sidebar"]}`; hide self view `["Hide self view", "Hide yourself"]` / show `["Show self view", "Show yourself"]`; pin `["Pin"]` /
  unpin `["Unpin"]` (also as the start of a label like "Pin Pat to your main screen"); the chat message box `["Send a message",
  "Send a message to everyone"]` and its send button `["Send a message", "Send message", "Send"]`; admit `["Admit"]`, deny `["Deny",
  "Deny entry"]`, admit all `["Admit all"]`; remove `["Remove from the call", "Remove"]`; mute everyone `["Mute all", "Mute everyone"]`;
  end for everyone `["End the call for everyone", "End call for everyone"]`; the waiting section `["Waiting to join", "Waiting to be
  admitted"]`; host switches `{"share_screen": ["Share their screen"], "chat": ["Send chat messages"], "reactions": ["Send reactions"],
  "microphone": ["Turn on their microphone"], "video": ["Turn on their video"], "quick_access": ["Quick access"]}`.
- Readers (each a function of the page returning plain data, never throwing for a missing element):
  - `READ_CALL` (existing) also returns `chat_unread` (the number on the chat button's badge or label, 0 without one), `hosting`
    (the host controls button drawn), and `panels` (which of chat / people / host is open now).
  - `READ_PEOPLE`: with the people panel open, `{"people": [{"id", "name", "me", "pinned", "muted"}], "waiting": [{"id", "name"}]}`;
    `id` is Meet's own participant id attribute when the row has one (`data-participant-id` or similar), else `"name:" + name`.
  - `READ_CHAT`: with the chat panel open, the last 20 messages `[{"from", "text", "at"}]` in order (`at` the time text Meet shows, or "").
  - `READ_REACTIONS`: with the picker open, the emoji it holds, in order.
  - `READ_HOST_SWITCHES`: with the host panel open, `[{"key", "label", "on"}]` for each switch of `MEET.host_switches` drawn.
- Drivers: `PRESS_BY_LABEL` (a drawn, enabled element whose label starts with one of the given words, inside an optional container:
  the people row of a given id, the open panel, the open menu; returns the label pressed or null) and `FOCUS_CHAT_BOX` (focus Meet's
  message box and return whether it now has the keyboard).
- **Panels are not boxes (Review Focus 2):** `READ_DIALOGS` leaves out any dialog that is, or is inside, an open side panel (chat,
  people, host, activities) — decided by structure (the panel's container, `aside` / `[role=complementary]` / the panel's heading
  matching a panel name), not only by words.

**Tests (risky paths only):** against the fake Meet page with the chat and people panels open and drawn as `role=dialog`:
`READ_DIALOGS` returns none of them while a real "Someone wants to join" box beside them is still returned; `READ_PEOPLE` keeps two
people with the same name apart (ids) and marks the room's own row `me`; `PRESS_BY_LABEL` scoped to one person's row never presses
another row's button; `FOCUS_CHAT_BOX` answers false when the box is missing.

---

### Task 2: The provider follows the touch Pi's screens and reads Meet into the model

**Files:** modify `src/croom/meeting/providers/google_meet.py`, `src/croom/meeting/providers/base.py` (only if the model needs a new
field: keep Zoom's shape), `src/croom/control/service.py` (nothing new if `control("screen", …)` goes through the existing control
route); tests `tests/unit/meeting/test_meet_panels.py`.

**Interfaces:** consumes Task 1's `MEET`, `READ_CALL`, `READ_PEOPLE`, `READ_CHAT`, `READ_REACTIONS`, `READ_HOST_SWITCHES`,
`PRESS_BY_LABEL`. Produces the control `control("screen", "chat" | "participants" | "host" | "")` and the model below (Task 4 draws it).

**Requirements:**
- `control("screen", name)`: the touch Pi says which Meet screen is open ("" for none). The provider opens Meet's matching panel on
  the TV (chat → chat panel; participants → people panel; host → host controls panel; Meet shows one side panel at a time) and closes
  the one it opened when the screen changes to another or to "", when no renewal comes for 180 s (the page renews every 60 s, Task 4),
  when the call ends, and before the capture starts (the capture asks the provider to close it; the panel is opened again after the
  capture if the screen is still open). It never closes a panel it did not open (a person at the TV's mouse may have one open).
- While a panel the room opened is open, its reader runs every 2 s (in the existing 2 s box loop's turn, under `_dialog_turn`; one page
  read at a time): people → `info.people` (`count` from the people button as today, `list` from the panel, at most 50) and
  `controls.host.waiting_room.waiting`; chat → `info.chat.messages` (the last 20; `unread` from `READ_CALL.chat_unread`, 0 while the chat
  screen is open, as `chat.seen` does for Zoom); host → `controls.host.switches`.
- The model (`meet_controls`), same shape as Zoom's plus: `chat_send: {"available"}` (the chat button drawn); `reactions.choices` (from
  the picker the last time it was opened; until then the configured reactions, as Zoom's table does); `layout: {"available", "choices":
  ["auto", "tiled", "spotlight", "sidebar"], "value"}` (value: the last the room chose this call, else null: Meet shows the current one
  only inside its dialog); `hide_self: {"available", "on"}` (the room's own choice this call); `pin: {"available"}` and each person's
  `pinned`; `host.role` "host" when `READ_CALL.hosting` (Meet does not tell host from co-host on the call page: both have the host
  controls) else "participant"; `host.mute_all`, `host.remove`, `host.end`, `host.waiting_room` (with `deny: true`, which the page reads
  to offer Deny) available when hosting; `host.switches` the list read, `[]` until the host screen has been open; `host.record`,
  `host.lock`, `host.ask_unmute`, `host.mute_one` not available for Meet.
- A control while the capture runs is refused at once (the words in Global Constraints).

**Tests (risky paths only):** with a fake page: the panel opened for "participants" is closed when the screen becomes "", when 180 s
pass with no renewal (a fake clock), when the call ends, and before the capture; a panel the room did not open is never closed; the
room's open chat panel does not appear as `info.dialog`.

---

### Task 3: The provider carries out Meet's controls

**Files:** modify `src/croom/meeting/providers/google_meet.py`; tests `tests/unit/meeting/test_meet_actions.py`.

**Interfaces:** consumes Tasks 1–2. Produces these control names (the page sends them; Task 4): `chat.send` (text), `reactions`
(an emoji the picker holds), `layout` ("auto" | "tiled" | "spotlight" | "sidebar"), `hide_self`, `pin` (person id), `unpin` (person
id), `host.admit` (id), `host.deny` (id), `host.mute_all`, `host.remove` (id), `host.end`, `host.switch` (`{"key", "on"}`).

**Requirements:**
- Each control: under `_pressing`, opens what it needs (a panel, a menu, the layout dialog), presses by `PRESS_BY_LABEL`, waits for the
  change to show (at most 3 s), closes what it opened that was not already open (a panel the touch Pi has open stays open), and answers
  the model. A button Meet does not show: a refusal naming it ("Meet's chat button isn't on the screen", "Meet has no Pin for that
  person"). A failure closes what it opened (a `finally`).
- `chat.send`: text 1–500 characters after trimming; opens the chat panel if needed; `FOCUS_CHAT_BOX` must answer true, else the refusal
  "Meet's message box isn't ready; nothing was sent" and nothing is typed (Review Focus 3); types with `keyboard.insert_text` (not key
  presses: no shortcut can fire), then presses the send button inside the chat panel (Enter only if Meet shows no send button).
- Person actions (Review Focus 4): the person is found in the people panel by the id the touch Pi sent; an id of the form `name:<n>`
  matches only if exactly one row has that name, else the refusal "Two people in the call are called <n>" / "<n> isn't in the call any
  more". The button is pressed inside that person's row (or that row's own menu) only.
- `host.mute_all` and `host.end` (Review Focus 5): the touch Pi has asked twice (Task 4); the provider holds `_dialog_turn` from the
  first press to the end, so the box loop does not take Meet's confirmation box ("Mute all participants?" / "End the call for
  everyone?"), presses that box's own matching button (`Mute all` / `End call`), and fails in words if the box does not come. `host.end`:
  Meet's leave menu's "End the call for everyone"; afterwards the room leaves as `leave` does. `host.remove`: the row's Remove and then
  Meet's confirmation's Remove, the same way.
- `layout`: More options → Change layout → the choice → close the dialog; remembered as `layout.value`. `hide_self`: the room's own tile
  menu's Hide/Show self view, remembered as `hide_self.on`.
- `reactions`: opens the picker, takes its choices into the model, presses the one asked for (an emoji not in the picker is refused).

**Tests (risky paths only):** with the fake Meet page: `chat.send` with the message box missing types nothing anywhere (the page's
keydown listener sees no key) and refuses; a `name:` id with two matching rows is refused and nothing is pressed; `host.end` presses
Meet's confirmation's End only after its own first press and the box loop queues nothing meanwhile; a person action presses only in
that person's row.

---

### Task 4: The touch Pi's screens for Meet

**Files:** modify `src/croom/control/static/app.js`, `src/croom/control/static/index.html`, the page's CSS; tests: add to
`tests/unit/control/test_page.py` only for the risky items below.

**Interfaces:** consumes the model of Task 2 and the controls of Task 3.

**Requirements:**
- The page tells the agent which meeting screen is open: on entering Participants, Chat or Host in a Meet call it sends
  `control("screen", "participants" | "chat" | "host")`, renews it every 60 s while that screen stays, and sends `control("screen", "")`
  on leaving it (Back, the two-minute return, a meeting that ends). Zoom calls send nothing new.
- Chat: under the list, a message field (the page's keyboard, `data-keyboard`) and **Send** when `controls.chat_send.available`; Send
  is inert while its request is out; the field clears when the send is answered without a refusal; a refusal is said on the chat screen.
- Participants: **Pin** / **Unpin** on each row when `controls.pin.available` (by `p.pinned`); **Deny** beside Admit for a waiting
  person when `host.waiting_room.deny`.
- The view: when `controls.layout.choices` has more than two, the meeting screen's Layout button opens a row of the choices (Auto, Tiled,
  Spotlight, Sidebar; the current one marked when `value` is known), in the reaction row's place and way; two choices keep today's toggle
  (Zoom).
- Host: **Mute everyone** (a second tap, as End), **End call for everyone** (as today), and a list of the switches in
  `host.switches`, each a switch control sending `host.switch {key, on}`.
- A titleless Meet box still says "Meet asks" (Phase A).

**Tests (risky paths only):** the screen signal: entering Chat in a Meet call sends `screen: "chat"`, the two-minute return sends
`screen: ""`, a Zoom call sends none; Mute everyone needs the second tap.

---

### Task 5: Zoom pin

**Files:** modify `src/croom/meeting/providers/zoom_sdk_page/meeting.js`, `src/croom/meeting/providers/zoom_sdk.py`,
`src/croom/meeting/providers/base.py` (`controls_from`: `pin.available`); tests: the bridge's existing test file for the one risky item.

**Requirements:**
- Find Pin in the Zoom Meeting SDK for Web (client view) of the version the page loads (`version` in meeting.js; its typings / docs on
  source.zoom.us or marketplace docs): a pin call for a user id if the SDK has one (names such as `pinVideo`, `operatePin`); otherwise the
  client's own participant menu item "Pin" pressed by the bridge's existing menu presser (`pickFromMenu` style). Record which in a comment
  with the source.
- Controls `pin` (id) and `unpin` (id) through `HOST`-style `ofPerson` (the id checks as today); `pin.available` true when the bridge
  can pin; each participant's `pinned` from the SDK's attendee record if it says, else the bridge's own record of what it pinned this call.
- If neither way exists in this SDK version, `pin.available` stays false and the report says why (no fake button).

**Tests (risky paths only):** a pin with a missing or non-numeric id is refused before anything is asked of Zoom.

---

### Task 6: The participants list from Google's Meet REST API

**Files:** create `src/croom/meeting/meet_api.py`; modify `src/croom/core/config.py` (`meeting.meet_api_domain`, default ""),
`src/croom/meeting/providers/google_meet.py` (use it), the Settings › Meetings page (the domain field), README (the admin's grant,
spec section 5); tests `tests/unit/meeting/test_meet_api.py`.

**Requirements:**
- Off unless `meeting.meet_api_domain` is set (e.g. `crystalpm.com`) and the call's organizer (`MeetingInfo.organizer`, from the
  calendar) has an address in that domain and the meeting code is known from the URL (`abc-mnop-xyz`).
- Credentials: the calendar's service account file (`calendar.google_credentials_path`), scope
  `https://www.googleapis.com/auth/meetings.space.readonly`, subject = the organizer (domain-wide delegation; google-auth's
  `with_subject`), token refreshed off the event loop.
- Calls (v2): `GET https://meet.googleapis.com/v2/spaces/{meeting_code}` → the space name; `GET /v2/conferenceRecords?filter=space.name="<space>" AND end_time IS NULL`
  → the live conference; `GET /v2/{conference}/participants?filter=latest_end_time IS NULL` (pages followed) → names from
  `signedinUser.displayName` / `anonymousUser.displayName` / `phoneUser.displayName`.
- Read every 15 s while connected; when it answers, `info.people.list` (and count) come from it while the people panel is not open (the
  panel's list, with ids, wins when open: Pin and Remove need it); people from the API have ids `name:<name>` (Task 3 finds them by name).
- A refusal (403: no grant; 404) is logged once per call and the list falls back to the panel; never retried more than every 5 minutes
  after a refusal; nothing on the touch Pi breaks without it.
- README: the grant (Admin console › Security › Access and data control › API controls › Domain-wide delegation: the service account's
  client ID with the scope above) and the setting.

**Tests (risky paths only):** a 403 falls back silently after one log line and is not retried within 5 minutes; an organizer outside
the domain makes no request; the token's subject is the organizer.
