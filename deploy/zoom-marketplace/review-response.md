# Reply to Zoom's review of Crystal Meet Rooms

Paste each answer under the matching note in the reply to Zoom's reviewers. Fill in every [TO CONFIRM] first, and work through the checklist below before resubmitting.

## Replies

### 1. Images

We have added four screenshots of the integration in use to the App Gallery, after the existing overview image:

2. The room page on the table's touch screen: today's bookings, with **Join now** for a Zoom meeting about to start.
3. The room page during a Zoom meeting, with **Mute**, **Turn camera off** and **Leave**.
4. The room's TV between meetings. When someone presses Join, Zoom's meeting view appears here.
5. The door sign while the room is in a meeting.

During a meeting the TV shows Zoom's own Meeting SDK view, as the demo video in note 6 shows.

### 2. Account Credentials

Crystal Meet Rooms has no end-user login, no OAuth user flow and no user roles, so we have no account to share, with or without dummy data. It is an admin-managed General App used only by conference-room devices. The device signs each Meeting SDK join with the app's Client ID and Client Secret, and it gets a ZAK for the room's own Zoom user through our account's Server-to-Server OAuth app. Nobody signs in on the device.

The device uses the app's **Production** Client ID. No authorization step chooses a Client ID at run time. The device's check command, shown in the demo video, prints the Client ID in use, so you can compare it with the app's Production credentials.

You can test the full flow in any of these ways:

- (A) Watch the recorded video of our device joining your meeting 5590000559: [TO CONFIRM: link].
- (B) Join a live session at a time that suits you. Our device joins a meeting you host and runs every step at your direction. [TO CONFIRM: contact and proposed times]
- (C) [TO CONFIRM: offer, or delete this option] Use a password-protected link to one room's page to make the room join a meeting you host. [TO CONFIRM: URL, user name, password]

Section 9 of the attached test plan describes each option.

### 3. Test Plan Requirements

The complete test plan, in English, is attached: [TO CONFIRM: file name or link]. It covers:

- what the integration is and why it has no user login;
- the environment and prerequisites;
- setup and authorization exactly as a customer does it, with the Production Client ID;
- scope-by-scope tests for the three read-only scope grants of the two apps (there are no optional scopes);
- the end-to-end flow with the expected outcome of each step: join, waiting room, mute, camera, leave and errors;
- multi-user flows: not applicable, as there is one role;
- plan tier and 2FA: no paid feature is needed, the room user is a Basic user, and the device never signs in with a password, so 2FA does not apply;
- the steps for your meeting 5590000559, how you can test it yourselves, and how to remove the integration.

### 4. Documentation URL

The end-user documentation is now on our own domain: https://www.crystalpm.com/crystal-meet/zoom/ [TO CONFIRM once hosted]. It has the three sections you asked for:

- **Adding the app**: setup and authorization, step by step.
- **Using the app**: prerequisites, joining, the in-meeting controls, what the TV and door sign show, and troubleshooting.
- **Removing the app**: taking the credentials off the device, deactivating or deleting the apps, removing the room user, and what changes for users.

It carries a pre-release notice until the app is approved.

### 5. Privacy and Terms

Both now point to pages on our own domain, written for this integration:

- Privacy policy: https://www.crystalpm.com/crystal-meet/zoom/privacy.html [TO CONFIRM once hosted]
- Terms of use: https://www.crystalpm.com/crystal-meet/zoom/terms.html [TO CONFIRM once hosted]

### 6. Test Plan (demo)

Here is the demo video: [TO CONFIRM: link]. It shows our room device (the touch screen on the table and the room's TV) joining your meeting 5590000559 with passcode 940104, using the app's Production credentials. In order, it shows:

- the device's check command printing the Production Client ID;
- the meeting's link typed on the room page;
- Zoom's meeting on the room's TV;
- the room, named "Room 1", in the participant list of meeting 559 000 0559, seen from a second participant's Zoom window with the meeting ID displayed;
- then Mute, Turn camera off and Leave.

The room joined at [TO CONFIRM: date, time and time zone].

## Checklist for Crystal PM

- [ ] **Legal review** of `docs/privacy.html` and `docs/terms.html`. Fill in their [TO CONFIRM] items: legal entity, postal address, effective dates, liability cap, governing law and courts. Then remove the draft comment at the top of each. Section 1 of the terms says the integration is used only in rooms Crystal PM sets up and manages, matching the listing's EU non-trader answer. If customers' rooms are meant, change both together.
- [ ] **Host the three pages**:

  | File | Address |
  |---|---|
  | `docs/index.html` | https://www.crystalpm.com/crystal-meet/zoom/ |
  | `docs/privacy.html` | https://www.crystalpm.com/crystal-meet/zoom/privacy.html |
  | `docs/terms.html` | https://www.crystalpm.com/crystal-meet/zoom/terms.html |

  Check that each page opens without a login. If you pick other addresses, update the links inside the three pages, in `listing.md`, in `test-plan.md` and in this reply.
- [ ] **Verify crystalpm.com** on the Publish page, by DNS record, file or meta tag.
- [ ] **Links & Support** (Production › App Listing): enter the three addresses above and the Support URL https://www.crystalpm.com/support/. The old /contact address redirects there.
- [ ] **App gallery** (App Information): upload `gallery-2-room-page.png`, `gallery-3-in-meeting.png`, `gallery-4-tv.png` and `gallery-5-door-sign.png` after `gallery.png`, with the captions in `listing.md`.
- [ ] **Room devices get the no-store fix**: once this branch is merged into main, update each room device (`sudo /opt/croom/venv/bin/pip install -q --upgrade git+https://github.com/ben-abeo/croom.to.git && sudo systemctl restart croom`). The device's join page then sends the signature, passcode and ZAK with `Cache-Control: no-store`, which keeps Security answer 3 true.
- [ ] **Recording room**: put the General App's Production Client ID and Client Secret in `/etc/croom/zoom-credentials.json`, run `sudo systemctl restart croom`, and confirm that the check ends with "Ready" and shows the Production Client ID.
- [ ] **Record the video** as in `test-plan.md` section 8:
  - Join by a pasted link at a time when no booking with a video link is in progress in that room. The room page's Today list has a known layout bug while a linked booking is in progress; it is being fixed on another branch.
  - Show the meeting ID in the typed link and on a second participant's screen, since the TV hides Zoom's meeting header.

  Upload the video where Zoom can open it without signing in, and fill in the link and join time in test-plan.md sections 8 and 9 and in replies 2 and 6.
- [ ] **Reviewer access**: decide which of options A, B and C to offer, then fill in or delete their [TO CONFIRM] items in test-plan.md section 9 and reply 2. For C, publish one room's page behind a password for the review only, and withdraw it afterwards.
- [ ] **Test plan**: fill in the review contact, plan date and room, export it to PDF, and attach it to the reply and the submission.
- [ ] **Technical Design › Security**: update answer 3 with the wording in `listing.md`. The credentials file belongs to the service user with mode 600; it is not "root-only". The architecture diagram (`architecture.png` and `.pdf`) still says "root-only": change that word in `render_diagram.py` and re-render it before uploading it again.
- [ ] **Publish page**: under test credentials, replace "none" with "No end-user login; see the test plan, section 9 (video, live session)".
- [ ] **Reply and resubmit**: reply to the reviewers' email with the six answers, then resubmit from the Publish page.

## Notes for the operator

- **The browser cache is fixed on this branch.** The TV's browser keeps a persistent profile, and the device's one-time join page (`/join/{token}` in `src/croom/meeting/providers/zoom_sdk_site.py`) carries the signature, passcode and ZAK. That page used to go out with no caching headers, so Chromium wrote it to the profile's disk cache. It now sends `Cache-Control: no-store` and `Pragma: no-cache`. A unit test checks the headers, and a Chromium check with a persistent profile found no copy left. The privacy draft and the test plan say so.
- **If Zoom refuses the join to 5590000559** even with the Production pair (for example "not authorized"), do not fall back to the Development pair, since Zoom asked for the Production credentials. Send the reviewer the exact words on the room page and the time.
