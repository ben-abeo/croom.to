# Google Meet controls on the touch Pi

**Date:** 2026-10-30 · **Status:** agreed with Ben in conversation, written for his review · **Branch:** `meet-controls` off `main`
(0b7b177) · **Builds on:** Google Meet as a signed-in room (2026-10-07), meeting controls and dialogs (2026-10-13), room settings
(2026-10-29).

## 1. What this is for

Ben, 2026-10-30, with a photo of the TV: "When joining a google meet meeting, this pops up [Others may see your video differently … Got
it]. We need to make it to where it will click ok automatically. Also, can we make Google meet have controls like zoom where you can view
the chat, react to things, see participants, change views, etc?"

Today a Meet call on the touch Pi offers Mute, Camera, Raise hand and Captions and a people count; the table's chat, reactions,
participants, view and host screens show nothing for Meet, and Meet's notices stay on the TV until someone reaches the room Pi's mouse.

Success: Meet's harmless notices are answered for the room the moment they appear; a Meet call on the touch Pi has what a Zoom call has —
chat (read and send), reactions, the participants list with people asking to join (Admit, Deny), the view, hide self view, pin, and, when
the room is a host or co-host, the host controls; and when Meet changes its page, a new capture shows what broke.

## 2. Decisions (Ben, 2026-10-30)

- **Scope: everything a participant can do, letting people in, and the host controls** when the room is a host or co-host (mute everyone,
  remove someone, end for everyone, Meet's host-control switches and Quick access), **pin** a person — **for Zoom too** — and the
  **participants list from Google's Meet REST API** for the organization's own meetings.
- **No Google API drives a participant's Meet window** (the Meet REST API manages spaces and reads records; it has no in-call chat,
  reactions, view, admit or notices). So the room drives Meet's own page in the TV browser, by Meet's accessibility labels, with Meet's
  keyboard shortcuts as a second way in.
- **Meet's real page is learned from a capture**: a tool in Settings records Meet's panels in a test call; the build and its tests work
  from that capture (redacted), and a new capture shows what changed when Meet changes.
- **Two phases.** A: the capture tool, and Meet's notices answered by the same policy as Zoom's. B (after Ben's capture): the controls,
  pin for Zoom, the REST API participants.

## 3. What people see

**Meet's notices.** A box Meet draws over the call is read (its words and buttons) and judged by the room's dialog policy, the one Zoom's
dialogs use: a notice with one button whose words are on `meeting.dialogs.acknowledge` is pressed at once and noted on the table's
notice line; a button that leaves or ends the call is never pressed; anything else (a choice, an unknown box) waits on the touch Pi with
its buttons, for a tap. "others may see your video differently" joins the default list (Settings › Meetings edits it).

**The capture** (Settings › Check, **Capture Google Meet**, only while the room is in a Meet call): the TV opens Meet's chat, people
panel, reactions picker, more-options menu, Change layout, the room's own tile menu and, when shown, the host controls and a "wants to
join" box, one after another (about 30 seconds), recording each one's elements (role, accessibility label, text, a few attributes,
position) and a screenshot, then closes them. Names, email addresses and chat text are replaced by placeholders in the recorded
elements; the screenshots show the TV as it was (a switch, on by default, leaves them out). The file (a zip) downloads from the same
page. For a useful capture Ben runs a short test meeting: someone on a laptop knocks to join, is admitted, sends a chat message and a
reaction, and the room is made a co-host before Capture is pressed.

**Phase B on the touch Pi**, the screens Zoom has:
- **Chat:** the unread count from Meet's chat button; the messages from the chat panel, which the room opens on the TV only while Chat is
  open on the touch Pi; **Send** types into Meet's message box with the touch Pi's keyboard.
- **Reactions:** the emoji Meet's picker holds.
- **Participants:** the list, with Pin / Unpin; people asking to join, each with **Admit** and **Deny**; for a host or co-host, Remove and
  Mute everyone. For a meeting of the room's own organization, with the admin's grant (section 5), the list comes from the Meet REST API;
  otherwise from Meet's people panel, opened only while Participants is open on the touch Pi.
- **View:** Auto, Tiled, Spotlight, Sidebar (the room remembers the last it chose; Meet shows the current one only inside its dialog).
- **Hide self view.**
- **Host** (host or co-host only): Mute everyone, End call for everyone (both confirmed on the touch Pi), and Meet's host-control
  switches: screen share, chat, reactions, microphones, video, Quick access.
- **Zoom:** Pin / Unpin in its participants list.

A control whose button Meet does not show says so in words ("Meet's chat button isn't on the screen") instead of doing nothing.

## 4. Pieces

- `croom.meeting.meet_page`: the page scripts Meet is read and driven with (Phase A: dialogs; Phase B: chat, people, reactions, layout,
  self view, pin, host), each a function of the page that returns plain data, in one module so a Meet change is fixed in one place.
- `GoogleMeetProvider`: reads dialogs with its regular reading (every 2 s while connected, and right after the join), applies
  `DialogPolicy`, answers through `control("dialog", …)` as Zoom's provider does, and (Phase B) the controls.
- `croom.meeting.meet_capture`: the capture, run against the provider's page; redaction; the zip in the data directory.
- Control service: `POST /api/settings/meet-capture` (PIN; refused unless a Meet call is connected), `GET /api/settings/meet-capture`
  (the last capture's file); the Check page's button.
- Phase B adds: `croom.meeting.meet_api` (REST API participants with domain-wide delegation of the calendar service account, scope
  `meetings.space.readonly`, impersonating the meeting's organizer), Zoom pin, and the touch Pi screens wired for Meet.

## 5. The admin's grant (Phase B, optional)

For the participants list from the API: in the Google Admin console, Security › Access and data control › API controls › Domain-wide
delegation, add the room's service account client ID with the scope `https://www.googleapis.com/auth/meetings.space.readonly`. Without it,
or for another organization's meeting, the list comes from Meet's page.

## 6. Testing

- Phase A: the dialog reader against pages built from the photo's notice and from two-button boxes (headless Chromium, `set_content`);
  the policy decisions; the capture's steps, redaction and zip against a fake Meet page; the route and the Check page.
- Phase B: the redacted capture committed as test fixtures; every reader and control checked against them; the touch Pi screens with a
  stand-in Meet call; a hands-on checklist for a real Meet call in a room.

## 7. Out of scope

Meet's whiteboard, breakout rooms, polls, Q&A, recording (Meet records only from the organizer's side), and Teams and Webex controls.
