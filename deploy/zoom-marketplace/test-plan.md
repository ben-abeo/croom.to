# Crystal Meet Rooms: test plan for Zoom's app review

- App: **Crystal Meet Rooms** (General App with the Meeting SDK, admin-managed; unlisted after approval)
- Developer: Crystal PM, support@crystalpm.com
- Documentation: https://www.crystalpm.com/crystal-meet/zoom/ [TO CONFIRM once hosted]
- Review contact: [TO CONFIRM: name, email, phone, time zone]
- Plan date: [TO CONFIRM]

## 1. What the integration is

- Crystal Meet is Crystal PM's conference-room system. Each room has a Raspberry Pi behind the TV (the "room device"), a touch screen on the table that shows the room page (today's bookings, Join, a box for a meeting link, Mute, Turn camera off, Leave), and a sign by the door.
- When a person presses **Join** on the room page, the room device joins the Zoom meeting as an ordinary participant through Zoom's **Meeting SDK for Web** (Client View, version 6.5.0) in the device's own Chromium, and shows it on the TV with the room's camera and speakerphone. Nothing joins by itself.
- The device joins as the room's own Zoom user (for example `room1@crystalpm.com`, display name "Room 1"). Each join carries:
  - a Meeting SDK signature (HS256 JWT) minted on the device from the General App's Client ID and Client Secret, and
  - a ZAK for the room's Zoom user, fetched through the account's Server-to-Server OAuth app. Zoom requires this since March 2, 2026, for SDK joins of meetings hosted outside the app's own account.
- **There is no end-user login, no OAuth authorization (consent) flow, no user roles and no screen inside the Zoom client.** The app is admin-managed: an account admin creates the apps and puts their credentials on the device. The only "user" of the app is the room device; the people in the room never sign in to anything.
- No webhooks or event subscriptions. Nothing is recorded. The device keeps a credentials file (`/etc/croom/zoom-credentials.json`, mode 600). Every call to Zoom uses HTTPS with TLS 1.2 or higher.

## 2. Environment and prerequisites

| Item | Used for this review |
|---|---|
| Zoom account | Crystal PM's paid Zoom account, which owns both apps and the room users. |
| Marketplace app | **Crystal Meet Rooms**: General App, Admin-managed, Meeting SDK on (Features › Embed), scope `user:read:zak:admin`. The device holds its **Production** Client ID and Client Secret. |
| Server-to-Server OAuth app | **Crystal Meet room tokens**: same account, activated, scopes `user:read:zak:admin` and `user:read:token:admin`. |
| Room user | One Basic (unlicensed) Zoom user per room, display name = room name. For this review: [TO CONFIRM: room, e.g. Room 1, room1@crystalpm.com]. |
| Room device | Raspberry Pi 5, Raspberry Pi OS (64-bit), behind the room's TV; USB camera and speakerphone; the Crystal Meet agent (Python 3.12) and Chromium. A touch screen on the table shows the room page. |
| Network | Outbound internet access to `zoom.us`, `api.zoom.us`, `source.zoom.us` and Zoom's meeting servers. No inbound access. |
| Reviewer | A Zoom meeting the reviewer hosts or attends, such as 5590000559. The reviewer needs no account, device or software from us. |

The room device is physical hardware in Crystal PM's office, so reviewers cannot install it. Section 9 lists the ways reviewers can see and drive the flow.

## 3. Setup and authorization, as a customer does it

There is no sign-in or consent screen. The account admin authorizes the integration by creating and activating the two apps and placing their credentials on the device.

1. **General App with the Meeting SDK**
   1. marketplace.zoom.us › **Develop** › **Build App** › **General App** › **Create**.
   2. **Basic Information**: name "Crystal Meet Rooms", **Admin-managed**. **App Credentials** shows the Development and Production pairs.
   3. **Features** › **Embed**: **Meeting SDK** on. **programmatic join use case**, **Request Anonymous Join Exception** and **Use Device OAuth** stay off.
   4. **Scopes** › **Add Scopes**: `user:read:zak:admin`, nothing else.
   5. After Zoom's approval, the **Production** Client ID and Client Secret go on the device.
2. **Server-to-Server OAuth app**
   1. **Develop** › **Build App** › **Server-to-Server OAuth** › **Create**, named "Crystal Meet room tokens".
   2. **App Credentials**: Account ID, Client ID, Client Secret. **Information**: description, company, developer contact. **Feature**: no event subscriptions.
   3. **Scopes** › **Add Scopes**: `user:read:zak:admin` and `user:read:token:admin`.
   4. **Activation**: activate.
3. **Room user**: admin.zoom.us › **User Management** › **Users** › **Add Users**: user type **Basic**, activated from the invitation email, display name "Room 1".
4. **Credentials file on the device**: `/etc/croom/zoom-credentials.json`, owned by the service account, mode 600. Then `sudo systemctl restart croom`.

   ```json
   {
     "sdk_client_id": "<General App: Production Client ID>",
     "sdk_client_secret": "<General App: Production Client Secret>",
     "account_id": "<Server-to-Server app: Account ID>",
     "s2s_client_id": "<Server-to-Server app: Client ID>",
     "s2s_client_secret": "<Server-to-Server app: Client Secret>",
     "room_user": "room1@crystalpm.com"
   }
   ```

   **Production Client ID.** `sdk_client_id` and `sdk_client_secret` hold the General App's Production pair, the one Zoom reviews. The device never chooses a Client ID at run time: it signs every join with this one.
5. **Check**: `/opt/croom/venv/bin/croom --check-zoom -c /etc/croom/config.yaml`. Expected output, exit code 0:

   ```
   Zoom Meeting SDK: app <Production Client ID>, signature minted (Zoom checks it only when a meeting is joined)
   Zoom account: server-to-server token obtained for account <account ID>
   Zoom room user: room1@crystalpm.com, ZAK obtained (valid 2 hours)
   Ready: this room can join meetings on your own Zoom account now; meetings hosted by other accounts also need Zoom's approval of the SDK app, and the first outside-hosted meeting proves it.
   ```

   The client ID on the first line can be compared with **App Credentials › Production** on the app's page. The check never prints a secret.

## 4. Scope-by-scope tests

The two apps hold three scope grants in all, every one of them read-only. **There are no optional scopes**, and neither app requests any other scope.

| App | Scope or grant | What it is for | How it is exercised | Expected outcome |
|---|---|---|---|---|
| Crystal Meet Rooms (General App) | Meeting SDK (a feature, not a scope) | Joining a meeting from the device with an SDK signature | Every join: the device mints the signature from the Production Client ID and Secret and calls `ZoomMtg.join` | The room appears in the meeting's participant list |
| Crystal Meet Rooms (General App) | `user:read:zak:admin` | The app's one data need: the ZAK of a conference-room user, so that the room can join meetings hosted by other accounts as that user. Zoom's build flow requires at least one scope, and this is the scope that names that need. | The General App is never OAuth-authorized by a user and makes no API call itself. The ZAK it needs is fetched with the Server-to-Server app below, which holds the same scope, and is passed to `ZoomMtg.join`. Seen in section 5: the room joins under its user's name, "Room 1", including in a meeting another account hosts, such as the reviewer's. | Join succeeds as "Room 1" |
| Crystal Meet room tokens (Server-to-Server) | `account_credentials` grant (no scope) | An access token for the account | `POST https://zoom.us/oauth/token?grant_type=account_credentials&account_id=<account ID>` with HTTP Basic client credentials. Seen on line 2 of the check. | "Zoom account: server-to-server token obtained for account …" |
| Crystal Meet room tokens (Server-to-Server) | `user:read:zak:admin` and `user:read:token:admin` | Reading the room user's ZAK. Zoom's documentation names the first for ZAKs and the second for this endpoint; the app holds both. | `GET https://api.zoom.us/v2/users/<room user>/token?type=zak&ttl=7200` with the bearer token, before every join. Seen on line 3 of the check. | HTTP 200 with a token; "Zoom room user: room1@crystalpm.com, ZAK obtained (valid 2 hours)" |

Negative tests, available in a live session (section 9, option B):

- Set `room_user` in the credentials file to an address that is not a user on the account, then run the check. It reports Zoom's refusal of the ZAK for that address, for example "Zoom: Zoom has no user <address> on this account", and exits 1: the device asks for the ZAK of the configured room user and nobody else. Restore the file afterwards.
- Deactivate the Server-to-Server app, then run the check. It prints "Zoom: Zoom refused the server-to-server credentials: check the account id, client id and client secret, and that the app is activated" and exits 1. A join from the room page then fails with Zoom's refusal shown on the room page, and the TV keeps its idle screen.

## 5. End-to-end flow and expected outcomes

Preconditions: the check from section 3 ends with "Ready"; the TV shows the room's idle screen; someone hosts a Zoom meeting, or it allows join before host.

| # | Action | Expected outcome |
|---|---|---|
| 1 | On the room page, type or paste the meeting's invitation link, passcode included (for example `https://zoom.us/j/<meeting ID>?pwd=<passcode>`), into **Join with a link** and press **Join**. A booking on the room's calendar with a Zoom link offers **Join now** instead. | The room page shows "Joining Zoom meeting". Within about 15 seconds the TV shows Zoom's meeting view, full screen, and the room page shows "In a meeting" with "Zoom" and the time since joining. The host's participant list shows **Room 1**. The door sign turns red: "In use". |
| 2 | (Meeting with a waiting room) The host admits Room 1. | Until the host admits the room, the room page says "Waiting for the host to let the room in." Then it shows "In a meeting". |
| 3 | Press **Mute**. | The button changes to **Unmute**. The participant list shows Room 1's microphone muted. |
| 4 | Press **Unmute**. | Room 1's microphone is unmuted. |
| 5 | Press **Turn camera off**. | The button changes to **Turn camera on**. Room 1's video stops, and its tile shows the name. |
| 6 | Press **Turn camera on**. | Room 1's video returns. |
| 7 | Press **Leave**, then **Tap again to leave** within five seconds. | Room 1 leaves the participant list. The TV returns to its idle screen. The room page and the door sign return to the room's status, for example "Room free" and "Available". |
| 8 | (Alternative to 7) The host ends the meeting for all. | The room returns to idle by itself, as in step 7. |
| 9 | Error path: paste a link with a wrong passcode and press **Join**. | The room page shows "Couldn't join Zoom meeting" with Zoom's reason and a **Dismiss** button. Nothing joins. |

## 6. Multi-user flows

Not applicable. The integration has one role, the room device. It has no per-user authorization, no host/attendee split within the app, and no users outside the account that owns the room. People in the room use the room page without signing in.

## 7. Plan tiers, access controls and 2FA

- **Plan tier.** No paid add-on or particular plan feature is needed for any test. The room user is a **Basic** (unlicensed) user: it joins meetings and never hosts, so it needs no paid licence. Adding users needs a paid Zoom account, which Crystal PM has.
- **2FA.** Not involved. The device never signs in with a password: it fetches the room user's ZAK through the Server-to-Server app, so the room user needs no 2FA set up, and 2FA settings on the account do not affect the flow. Reviewers need no login at all (section 9).
- **Seed data.** For the join-from-booking path: a booking on the room's calendar with a Zoom link in it, which Crystal PM creates for a live session [TO CONFIRM]. The join-from-link path, used for the reviewer's meeting, needs none.

## 8. The reviewer's meeting: 5590000559, passcode 940104

Zoom's reviewer asked for a video of the hardware device joining meeting 5590000559 with the app's Production credentials. The steps below are what Crystal PM's operator does. The device's meeting view hides Zoom's meeting header, so the video shows the meeting ID in two other places: the link typed on the room page, and the meeting information in a second participant's Zoom window.

**Before recording**

1. Put the General App's **Production** Client ID and Client Secret into `/etc/croom/zoom-credentials.json` on the room device, then `sudo systemctl restart croom`.
2. Run the check from section 3. It must end with "Ready", and its first line must show the Production Client ID, which matches **App Credentials › Production**.
3. Set up the recording: a camera or phone that films the table's touch screen and the TV together, and screen recording on a laptop that will join the same meeting as a second participant.

**Recording**

1. Show the room: the TV with the room's idle screen, and the touch screen with the room page. Show the output of the check, run on the device (for example over SSH from the laptop): it shows the Production Client ID and no secret.
2. On the laptop, join meeting **5590000559** with passcode **940104** in the Zoom client. Open the meeting information (the icon at the top left of the meeting window) so that **Meeting ID 559 000 0559** is visible, and open **Participants**.
3. On the room page, tap **Join with a link** and type `https://zoom.us/j/5590000559?pwd=940104`. The link is needed because a bare meeting ID carries no passcode. Hold the typed link in view, then press **Join**.
4. Expected: the room page shows "Joining Zoom meeting", then "In a meeting". The TV shows Zoom's meeting view within about 15 seconds. On the laptop, **Room 1** appears in the participant list of meeting 559 000 0559, and the room's video appears in the gallery.
5. Press **Mute** and then **Unmute**, and **Turn camera off** and then **Turn camera on**, showing each change on the laptop.
6. Press **Leave**, then **Tap again to leave**. Show Room 1 leaving the laptop's participant list, and the TV returning to the room's idle screen.
7. Stop the recording and note the date, time and time zone of the join for the reviewer: [TO CONFIRM].

**What the reviewer sees in meeting 5590000559**: a participant named **Room 1** (the room user's display name) joins at the noted time, through the Web Meeting SDK with the app's Production credentials. It mutes, unmutes, stops and restarts its video, then leaves.

Video link: [TO CONFIRM]

## 9. How reviewers can test it themselves

The people in the room never sign in, so there is no account to hand over. Reviewers can see and drive the integration in one of these ways; Crystal PM confirms which it offers.

- **A. Recorded video** of the room device joining meeting 5590000559 (section 8). The meeting ID is visible in the link typed on the room page and in a second participant's meeting information, and the room is visible in that participant's list. Link: [TO CONFIRM]
- **B. Live session** at a time the reviewer chooses. The reviewer hosts any Zoom meeting, on any account, and sends the invitation link (or reuses 5590000559). Crystal PM's operator joins Room 1 and runs section 5 at the reviewer's direction, including a waiting room and the negative tests of section 4. The reviewer watches the room in their own participant list. Proposed times and contact: [TO CONFIRM]
- **C. Reviewer access to one room's page.** A password-protected link to Room 1's room page, opened for the review only. The reviewer opens it in a browser, pastes the invitation link of a meeting they host into **Join with a link**, presses **Join**, and then uses **Mute**, **Turn camera off** and **Leave**. They see Room 1 join in their own Zoom client, with the room's camera view. The room page is normally reachable only on Crystal PM's office network; this link is [TO CONFIRM: how it is published, e.g. an authenticated proxy] and is withdrawn after the review. URL: [TO CONFIRM], user name: [TO CONFIRM], password: [TO CONFIRM], available until: [TO CONFIRM]

Options A and B need no credentials from the reviewer. Option C needs only the password for that link. The app's own credentials stay on the device and are never shared.

## 10. Removing the integration

| # | Action | Expected outcome |
|---|---|---|
| 1 | On the device: `sudo rm /etc/croom/zoom-credentials.json`, then `sudo systemctl restart croom` | The check prints "Zoom Meeting SDK not configured: credentials file not found or unreadable: /etc/croom/zoom-credentials.json" and exits 1. The room can no longer join Zoom meetings. Google Meet, bookings, the TV screens and the door sign keep working. |
| 2 | Optional: remove `zoom` from `meeting.platforms` in `/etc/croom/config.yaml` and restart | The room page offers no Join for Zoom bookings and refuses Zoom links as "Unsupported meeting link". |
| 3 | Marketplace › **Manage** › **Created Apps**: deactivate or delete "Crystal Meet room tokens", and delete "Crystal Meet Rooms" (remove it under **Manage** › **Added Apps** if it was added during setup) | The Server-to-Server credentials stop working at once. Tokens already issued expire on their own: the account token within an hour, a ZAK within two hours. |
| 4 | Optional: admin.zoom.us › **User Management** › **Users**: deactivate or delete the room user | Nothing of the integration remains in the Zoom account. |
