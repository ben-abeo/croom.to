# Meeting controls, live state and dialogs

**Date:** 2026-10-13 · **Status:** draft for Ben's review · **Builds on:** the room page in two modes (spec 2026-10-12), the TV screensaver and one page (spec 2026-10-07), the Zoom Meeting SDK join (spec 2026-09-25), and branch `meeting-state` (mute and camera read from Zoom, set both ways at join, control faults answered in words, `GET /api/meeting/diagnostics`).

## 1. What this is for

Ben's asks, in his words: "we want everything that is available" from the meeting on the touchscreen; popups like Zoom's "This meeting is being transcribed and Zoom AI is on" need a plan ("Auto acknowledge", and "if there is a way to wait for a tap without auto acknowledging, that would be great too", "like a way to control things from the touchscreen"); "Include the host controls too"; "add icons to button labels".

The outcome: a person at the table can do, from the room page, everything a participant can do in the Zoom or Meet client, and everything a host can do when the room is the host, without a keyboard or mouse on the TV; nothing Zoom draws over the TV ever waits on a mouse; the table always shows the meeting as the platform has it.

What stays out: sharing the TV's screen, whiteboards, apps, dialing phones out, polls and Q&A (none of these make sense on a room device), and the kiosk's physical keyboard question (a separate, bounded fix).

## 2. Where the controls come from

The TV runs Zoom's Meeting SDK for web in Client View (`ZoomMtg`, SDK 6.5.0) in a page the agent serves (`zoom_sdk_page/meeting.html` + `meeting.js`). The page and the agent talk through a bridge: the page calls `window.crystalMeetEvent(state, detail)`; the agent calls `window.crystalMeet.*`. Google Meet is driven through Meet's own web page by the Meet provider. The room page polls `/api/status` and posts to `/api/meeting/*`.

Zoom's reference ([ZoomMtg](https://marketplacefront.zoom.us/sdk/meeting/web/ZoomMtg.html), [in-meeting events](https://marketplacefront.zoom.us/sdk/meeting/web/types/InMeetingEvent.html), [participant events guide](https://developers.zoom.us/docs/meeting-sdk/web/client-view/participant-events/)) gives three kinds of control:

| Kind | Controls | How the agent does it |
|---|---|---|
| **SDK calls, any participant** | mute/unmute self (`mute`), leave (`leaveMeeting`), rename (`rename`), the participant list (`getAttendeeslist`), the current user (`getCurrentUser`), breakout rooms (`getBreakoutRoomList`) | the bridge calls `ZoomMtg.*`, as `mute` does today |
| **SDK calls, host or co-host only** | mute all / ask all to unmute (`muteAll`), start/stop recording (`record`), lock/unlock (`lockMeeting`), admit from or send to the waiting room (`putOnHold`), remove a participant (`expel`), mute one participant (`mute` with their id), end for everyone (`endMeeting`) | the bridge calls `ZoomMtg.*`; shown only while `getCurrentUser` says host or co-host |
| **Client buttons on the TV** (no SDK call exists) | camera on/off, raise/lower hand, reactions, captions on the TV, speaker or gallery view, hide self view (pinning a participant is not driven in this version: the model always says `pin` is unavailable, and the bridge answers "Unknown control pin") | the bridge presses the client's own button, the way the camera button is pressed today: found by what it says, confirmed from the device by the diagnostics inventory (section 7) |
| **Events** | participant joins/leaves/changes (`onUserJoin`, `onUserLeave`, `onUserUpdate`, `onActiveSpeaker`), chat (`onReceiveChatMsg`), captions (`onReceiveTranscriptionMsg`), recording (`onRecordingChange`), host claim (`onClaimStatus`), network (`onNetworkQualityChange`) | the bridge listens and reports; the agent keeps the state the table shows |

Zoom documents the event names but not every payload; the guide says to treat an event as a signal to re-read the state through `getAttendeeslist`/`getCurrentUser`. The design follows that: every event re-reads, and the table shows what was read.

Google Meet has no SDK: the Meet provider presses four of Meet's own controls with its keyboard shortcuts (mute, camera, raise hand, captions) and Leave with its button, and reads their state back from the buttons' labels. Reactions, layout, pin, self view and chat are reported as unavailable in this version, and so are the host controls.

## 3. The control model

The agent describes what the table may do right now, and the table draws from that description, so one page serves Zoom and Meet, participant and host, without knowing either client.

`/api/status` gains, inside `meeting` while a meeting is joined:

```json
"controls": {
  "mute":         {"available": true,  "on": false},
  "camera":       {"available": true,  "on": true},
  "raise_hand":   {"available": true,  "on": false},
  "reactions":    {"available": true,  "choices": ["Thumbs Up", "Clap", "Heart", "Joy", "Open Mouth", "Tada"]},
  "captions":     {"available": true,  "on": false},
  "layout":       {"available": true,  "value": "speaker", "choices": ["speaker", "gallery"]},
  "hide_self":    {"available": true,  "on": false},
  "pin":          {"available": false},
  "leave":        {"available": true},
  "host": {
    "role":        "participant",
    "mute_all":    {"available": false},
    "ask_unmute":  {"available": false},
    "record":      {"available": false, "on": false},
    "lock":        {"available": false, "on": false},
    "waiting_room":{"available": false, "waiting": []},
    "mute_one":    {"available": false},
    "remove":      {"available": false},
    "end":         {"available": false}
  }
},
"participants": {"count": 4, "list": [{"id": 16777216, "name": "Room 1", "muted": false, "video": true, "hand": false, "host": false, "waiting": false, "me": true}, ...]},
"chat":   {"unread": 2, "messages": [{"from": "Ann", "text": "Can you hear us?", "at": "2026-10-13T20:01:00Z"}]},
"notice": "This meeting is being recorded and transcribed.",
"dialog": {"id": 7, "title": "Recording consent", "text": "This meeting is being recorded…", "buttons": ["Leave Meeting", "Got it"]}
```

- `available` is false when the client has no such button on screen or the role does not allow it; the table hides what is unavailable and never guesses.
- `on`/`value` are what the platform has, re-read after every action and on every event, never the last thing the table pressed (the view and self view on Zoom are the exception: the client's buttons do not say them, so they are the last seen in its open menu or the last picked).
- `reactions.choices` are Zoom's names (`Thumbs Up`, `Clap`, `Heart`, `Joy`, `Open Mouth`, `Tada`): Zoom's client shows no emoji, only icons named so, and the table words each in plain terms with a Lucide symbol and sends the name back as it came (the bridge still reads an emoji as the name it stands for).
- `host.role` is `participant`, `co-host` or `host`; the host block's items are available only for a role that allows them (Zoom refuses otherwise, and the refusal would be the table's error line).
- `participants.list` is capped at 50 entries on the wire (the count is always exact); the table shows the list on its own sub-screen.
- `chat.messages` holds the last 20 incoming messages of this meeting; `unread` counts those since the table last opened the chat sub-screen (the table tells the agent with `POST /api/meeting/chat/seen`).
- `notice` is the disclosure line from acknowledged notices and banners (recording, transcription, AI Companion): each disclosure is a sentence of its own, in the order they came, a repeat dropped, and a notice that only says a state changed (the room was muted, a recording or a transcription stopped) discloses nothing, the state being read again; empty when none.
- `dialog` is the one dialog waiting for an answer (section 5), or absent.

The provider interface (`providers/base.py`) grows one generic method beside the existing `toggle_mute`/`toggle_camera`/`leave_meeting`:

```python
async def control(self, name: str, value: Any = None) -> Dict[str, Any]
```

`name` is a key of the model above (`raise_hand`, `reactions`, `captions`, `layout`, `hide_self`, `host.mute_all`, `host.ask_unmute`, `host.record`, `host.lock`, `host.admit`, `host.hold`, `host.remove`, `host.mute_one`, `host.end`, `dialog`, `chat.seen`); `value` is the choice (a reaction, a layout, a participant id, a dialog button). It returns the control's new state, or raises `MeetingControlError` with words for the table. A provider that lacks a control raises `MeetingControlError("Not available in this meeting")`; the table never shows such a button in the first place because `available` was false. `pin` is in the model and is always unavailable: no control of that name is pressable in this version.

Three routes carry it: `POST /api/meeting/control {"name": "...", "value": ...}` → 200 with the new state, 409 with words on a `MeetingControlError` or any other failure (as the mute and camera routes answer today; an unknown name is one too, in the bridge's own words, "Unknown control wave": the control service keeps no list of names), 400 for a bad body; and `POST /api/meeting/dialog {"id": 7, "button": "Got it"}` (section 5), and `POST /api/meeting/chat/seen` (no body: the table has read the chat). `toggle_mute`/`toggle_camera` and their routes stay, and the routes take the state asked for (amended after the whole-branch review): `POST /api/meeting/mute {"muted": true}` and `POST /api/meeting/camera {"on": false}` set that state, through the providers' `set_mute(muted)`/`set_camera(on)` (Zoom's call is told the state, so it is idempotent; Meet reads the button's label under its press lock and presses only when it differs), and a state that is already as asked answers 200 with it and presses nothing; a body that names no state (none at all, or `{}`) toggles as ever, for other callers; a state that is not true or false is a 400. The table's Mute and Camera buttons send the state they name, so that a tap made a moment after the host muted the room (or after the camera went off at the TV) ends as the person asked instead of flipping it back, and both are requests of the meeting screen like the rest (only the button pressed waits, a camera that takes twenty seconds to switch holds nothing else). Both routes answer `{"muted": bool, "meeting": ...}` and `{"camera_on": bool, "meeting": ...}`, the block the other routes answer with.

## 4. Zoom: the bridge and the provider

### 4.1 State

The bridge's `readState()` (branch `meeting-state`) grows into `readMeeting()`: the current user (muted, video, host, co-host, raised hand), the attendee list (`getAttendeeslist`, mapped to the participants model), the client's buttons the table may press (found by section 4.3 and reported with their current state: captions on, layout, self view hidden), recording state (from `onRecordingChange` and the client's recording indicator), the waiting-room list for a host (attendees `getAttendeeslist` marks as waiting, when the SDK reports them; otherwise from `onUserIsInWaitingRoom`). The page reads it on every participant, recording, claim and dialog event and every 5 seconds, and sends a `"state"` event only when the model changed, as today. The provider's `_take_state` keeps the whole model and the status route serializes it.

### 4.2 Calls

`window.crystalMeet.control(name, value)` dispatches: SDK calls for `mute`, `host.*`, `rename`; button presses for `camera`, `raise_hand`, `reactions`, `captions`, `layout`, `hide_self`; dialog answers for `dialog`. Each returns a promise that resolves with the new state (re-read after the action settles, at most 2 seconds of polling as `setVideo` does today) or rejects with words. The provider's `control()` is a thin call into it that maps rejections to `MeetingControlError`.

For the SDK init, `isSupportChat` becomes true (chat events arrive only then; the chat panel stays closed unless the bridge opens it to send a message, which this version does not), `isSupportCC` becomes true (captions), `disableRecord` becomes false (the host's recording control), and `disableInvite` stays true.

### 4.3 Finding the client's buttons

The client names its buttons for accessibility. The bridge finds each by what it says, in a small table of patterns, as the video button is found today:

| Control | Pattern on `aria-label` or text (case-insensitive) | State from the label |
|---|---|---|
| camera | "video" with "start" or "stop" | "stop" → on |
| raise_hand | the row "Raise Hand" / "Lower Hand" (text) of the picker the Reactions button opens; a footer button of that name instead, in the uncommon build that draws one | the room's own record (`raiseHand`): no button says it |
| reactions | "reactions", the footer's button; it toggles the picker (`div#reactionPicker`), whose items are icon buttons named by `aria-label` and `data-tooltip` (Clap, Thumbs Up, Joy, Open Mouth, Heart, Tada; names, not emoji) | — |
| captions | "captions" or "closed caption" with "show" / "hide" | "hide" → on |
| layout | "view", the View widget's button (outside the footer); it toggles a dropdown (`div.dropdown-menu` in `div.full-screen-viev-dropdown`) whose items are `a[role=button]` "Speaker View" / "Gallery View" | the item with the class `full-screen-widget__pop-menu--checked`, read while the menu is open for a pick; else the last pick (default speaker) |
| hide_self | the item "Hide Self View" / "Show Self View" of that same dropdown | "show" → hidden, read while the menu is open for a pick; else the last pick (default shown) |
| pin | not driven in this version: the client's per-participant menu is not opened, so `pin` is always unavailable and the bridge answers "Unknown control pin" | — |

The patterns are confirmed before they are pinned: the diagnostics inventory (section 7) lists every button on the device during a real meeting, and the final table and its tests use the labels the device reported. A button the finder cannot see makes its control `available: false`, never an error; a press that finds no button answers "not on the screen yet; try again in a moment", as the camera does today, a disabled one answers "is not ready yet", and a press that changes nothing (the button's label, or for the hand the room's record) answers "Zoom did not …", as the camera does. The reaction picker and the View dropdown are the client's own and toggle on their opener: the bridge snapshots what is clickable, presses the opener, uses only the elements that appeared (never a search of the page, and never `role="menu"`, which in 6.5.0 belongs to other parts of the client), picks, and closes the popover by pressing its opener again if Zoom has not, within one call, so the TV never sits with a popover open; it never takes a node of the client out of the page. Names are compared in lower case with only letters and digits kept; a missing value is refused before anything is pressed. Controls run one at a time, and an identical control while one is in flight is that same press.

### 4.4 Host actions

`host.mute_all` → `muteAll({muteAll: true})`; `host.ask_unmute` → `muteAll({muteAll: false})` (Zoom asks everyone to unmute); `host.record` → `record({record: !on})`; `host.lock` → `lockMeeting({lockMeeting: !on})`; `host.admit` → `putOnHold({userId, hold: false})`; `host.hold` → `putOnHold({userId, hold: true})`; `host.remove` → `expel({userId})`; `host.mute_one` → `mute({userId, mute: true})`; `host.end` → `endMeeting({})`. Zoom's refusal (not host, feature off for the account) comes back in Zoom's words on the table. `host.admit` and `host.hold` are available when `waiting_room.available` is; `host.mute_one` and `host.remove` act on a participant id from the Participants sub-screen. `host.end` and `host.remove` take a second tap on the table, the way Leave does today.

### 4.5 Google Meet

The Meet provider implements `control()` for `mute`, `camera`, `raise_hand` and `captions` by pressing Meet's own keyboard shortcuts (Ctrl+D, Ctrl+E, Ctrl+Alt+H and C) and reading the change back from the button's label, which names what a press does (Meet's labels are the plan's English until a Meet diagnostics run confirms them), and for `leave` as the table's own Leave does; it reads the people count from the people button's label or text (no roster), and reports `reactions`, `layout`, `pin`, `hide_self`, chat and `host.*` as unavailable in this version. Everything in section 3 that Meet cannot report is `available: false`, and the table shows the smaller set.

## 5. Dialogs

The client draws modal dialogs over the meeting: transcription and AI Companion notices, recording consent ("Got it" / "Leave meeting"), "the host would like you to unmute", "you have been muted by the host", breakout room invitations, host-ended meetings. The TV has no mouse, so nothing on it may wait for a click.

**The page reports every dialog.** A `MutationObserver` on the client's dialog container (confirmed from the inventory; Zoom's Client View uses `[role="dialog"]`-style modals) sends `crystalMeetEvent("dialog", JSON)` with an id, the title, the body text (first 400 characters) and the button labels when a dialog appears, and `("dialog-closed", id)` when it goes. `window.crystalMeet.answerDialog(id, label)` presses the named button.

**The agent answers by policy**, in this order:

1. **Acknowledge.** A dialog whose text matches a phrase in the acknowledge list and that has exactly one button, which does not leave or end the meeting (a label holding the whole word Leave, End, Exit or Quit, in any case, is `NEVER_PRESSED`: pressing it would walk the room out of its own meeting by policy, so such a dialog waits for a tap, as in 2): the agent presses it at once and adds the disclosure to `notice` (recording, transcription, AI Companion keep their words; "you have been muted by the host" refreshes the state instead). The default list: `is being recorded`, `is being transcribed`, `AI Companion`, `Zoom AI is on`, `has been muted`, `has been unmuted`, `recording has started`, `recording has stopped`. The list is configuration: `meeting.dialogs.acknowledge: [phrases]` in `config.yaml`; the stock config writes the defaults out so an admin can edit them.
2. **Wait for a tap.** Every other dialog (any dialog with more than one button; any wording not on the list; the host asking to unmute) goes to the table as `dialog` in the status, with its own text and buttons, and nothing is pressed until a person taps one. `POST /api/meeting/dialog {"id", "button"}` presses it on the TV. `meeting.dialogs.ask_everything: true` sends acknowledgeable dialogs this way too, for a room that wants every popup to wait for a tap.
3. **Log and learn.** Every dialog, answered or waiting, is logged at INFO with its title, text and buttons, so a new one can be judged and added to the list from the journal.

A dialog that disappears on its own (the host withdrew it, the meeting ended) clears from the status with `dialog-closed`. A banner with no buttons is never a dialog; recording and transcription banners feed `notice`. After any dialog closes, the state is re-read (the host's mute shows at once).

On the table, a waiting dialog is a card above the controls: the title, the text, and one button per label, in the client's order, the last one primary (Zoom 6.5.0 draws its dialogs cancel-first, `["Leave Meeting", "Got it"]`, so the affirmative button is the last). The notice line sits under the meeting title, in the detail style, for the rest of the call.

## 6. The room page

### 6.1 The meeting screen

The screen keeps its fixed, non-scrolling layout at 1280 by 800 (spec 2026-10-12). From top to bottom: the meeting title and detail; the notice line (two lines at most, cut short with an ellipsis, the whole text in its tooltip); the dialog card when one waits; the primary row; the secondary row; the Sound strip; the Camera framing and Upcoming meetings buttons as today.

- **Primary row** (always, when available): Mute / Unmute, Camera on / off, Raise hand / Lower hand, Reactions (opens a row of the six choices in place of the secondary row until one is tapped or 10 seconds pass), Leave (two taps, as today).
- **Secondary row**: Captions on / off, View (Speaker / Gallery), Hide self view / Show self view, Participants (with the count), Chat (with the unread count), Host (only for a host or co-host), and TV, the way into the TV remote (spec 2026-10-13, the TV remote and the touchpad), always the last, on every call: a platform that reports no controls at all (the Zoom web-client fallback) has the row hold it alone.
- **Sub-screens**, each with Back, each returning to the meeting screen on its own after 2 idle minutes (not 10: the meeting screen is the one people need): **Participants** (the list with mute, video and hand marks; for a host, Mute and Remove per row, and an Admit row per person waiting), **Chat** (the last 20 incoming messages, newest at the bottom; no reply box in this version), **Host** (Mute all, Ask all to unmute, Start / Stop recording, Lock / Unlock, End meeting for everyone with a second tap).
- A control that is not available does not show. The page re-renders from every status poll, so a host muting the room flips the Mute button within the poll interval, and a button never shows a state the platform does not have.
- The error line under the title (`#meeting-message`) shows a control's words (a 409) until the next action, as the camera fault does on `meeting-state`.

### 6.2 Icons

Every button on the meeting screen, and the existing Join, Options, Camera framing, Screensaver and Upcoming buttons, get an icon before the label, except the buttons of a dialog's card: their labels are the client's own words, and the sprite has no symbol for a label that is not known. The icons are inline SVG symbols in one sprite shipped with the page (`static/icons.svg`, loaded once and referenced with `<use href="#icon-mic">`), 24 px, stroke style, drawn from Lucide (ISC licence, noted in `static/LICENSES.md`): mic, mic-off, video, video-off, hand, smile, captions, layout-grid, user, users, message-square, phone-off, circle (record), lock, lock-open, log-in (admit), x-circle (end), volume, move (framing), monitor (screensaver), settings (options), calendar (upcoming). Icons are decorative (`aria-hidden`), the label stays the accessible name, and a button's icon changes with its state (mic ↔ mic-off). The page works without the sprite (a missing file leaves the labels alone).

## 7. Diagnostics and the inventory step

`GET /api/meeting/diagnostics` (branch `meeting-state`) grows to return: the current user's raw field names, the attendee list's field names, every button's label and text (the footer's, and the View widget's, which is outside it), what the reaction picker and the View dropdown hold under `popovers` (the route presses each opener, lists the items by text, label, tooltip, class and whether they are disabled, and presses the opener again to close it; a button that is missing or disabled is reported, not pressed), every dialog seen in this meeting (title, text, buttons), and the SDK version. Before the pattern tables in sections 4.3 and 5 are pinned, Ben runs one inventory meeting on PiMeet-3: start recording, turn on AI Companion, ask the room to unmute, raise a hand from another participant, open a breakout room, and read `http://pimeet-3.local:8080/api/meeting/diagnostics` and the journal. The labels and dialog texts from that run become the tests' fixtures. The same route is the support tool afterwards.

## 8. Configuration

```yaml
meeting:
  dialogs:
    acknowledge: ["is being recorded", "is being transcribed", "AI Companion", "Zoom AI is on", "has been muted", "has been unmuted", "recording has started", "recording has stopped"]
    ask_everything: false
  controls:
    reactions: ["Thumbs Up", "Clap", "Heart", "Joy", "Open Mouth", "Tada"]
```

Nothing else is configurable: host controls appear by role, and every control appears by availability.

## 9. Testing

- **Stub SDK** (`tests/unit/meeting/zoom_stub.py`) learns: an attendee list with mute, video, hand, host, waiting flags; `_behaviour.role` (participant, co-host, host); the client's buttons as Zoom 6.5.0 draws them (the Reactions slot with its toggling picker and the hand row, captions, the View widget with its toggling dropdown of the views and self view), with Zoom's label style; `muteAll`, `record`, `lockMeeting`, `putOnHold`, `expel`, `endMeeting`, `rename` recorded in `__zoomCalls` and refused when the role does not allow; `_showDialog(title, text, buttons)` that draws a modal and records which button was pressed; `_chat(from, text)` firing `onReceiveChatMsg`; `_recording(on)` firing `onRecordingChange`.
- **Page tests**: `readMeeting()` reports every field; each `control()` presses the right button or makes the right call and settles on the new state; menus open and close within a call; a dialog is reported with its buttons and answered by label; no dialog is pressed by the page on its own.
- **Provider tests**: the model in the status; every `control()` through the provider; the acknowledge policy (list, one-button rule, `ask_everything`), the waiting dialog in the status, `dialog-closed`, the notice line; host items available only by role; a Zoom refusal in words; chat and unread counting; state re-read after each action and each event.
- **Control service tests**: the routes, their 400/409 answers, the status shape, chat seen.
- **Room page tests** (Playwright): every button drawn from `controls`, hidden when unavailable, icon present; the dialog card and its tap; the notice line; the Participants, Chat and Host sub-screens and their 2-minute return; the second tap on End; the screen still fits 1280 by 800 with everything shown (`fits()`), and on a phone it scrolls.
- **Meet provider tests**: `control()` against the Meet page fixtures, `available` false for host items.
- **Pi acceptance** (Ben, a real meeting): the inventory run (section 7); each control from the table; a recording notice acknowledged with the disclosure on the table; the consent dialog waiting for a tap; `ask_everything` making the transcription notice wait; a host meeting with Mute all, admit from the waiting room and End for everyone; captions on the TV; the host muting the room shows within seconds.

## 10. Decisions made here

- Notices with one button are acknowledged by default and disclosed on the table (Ben: "Auto acknowledge"); `ask_everything` turns that off (Ben: "a way to wait for a tap").
- Every dialog not acknowledged waits on the table with its own buttons, so unknown popups never strand the TV (Ben: "control things from the touchscreen").
- Host controls are in (Ben: "Include the host controls too").
- Chat is read-only on the table in this version; sending needs the kiosk keyboard on a sub-screen and comes later.
- Controls are described by the agent and drawn by the table, so Meet and Zoom share one page.
- Labels and dialog texts come from the device, not from memory: the inventory step precedes the pinned tables.
