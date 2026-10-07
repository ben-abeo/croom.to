# Zoom Marketplace listing text for "Crystal Meet Rooms"

Copy these into the Production pages of the app. Replace the crystalpm.com
addresses with real pages before submitting; every address must be on a domain
verified for the account on the Publish page.

## App Listing, App Information

- App name: Crystal Meet Rooms
- Short description: Crystal PM's conference-room devices join Zoom meetings
  from the room's TV when someone presses Join on the room's screen.
- Long description: Crystal Meet is Crystal PM's internal conference-room
  system. A small computer behind each room's TV joins Zoom meetings through
  Zoom's Meeting SDK when a person in the room presses Join on the table
  screen, and shows the meeting on the TV with the room's camera and
  speakerphone. The device joins as the room's own Zoom user and appears in the
  participant list under the room's name. It has no screens of its own inside
  Zoom, collects no data beyond the room's own Zoom Access Key, and is used only
  by Crystal PM, a practice-management software company in the United States.
- App icon: icon.png (160 by 160), light and dark mode.
- App gallery: gallery.png (1200 by 780).
- Cover image: cover.png (1824 by 176; the left side sits under the icon, so it carries no text).
- Adding Your App: From Marketplace.
- Company name: Crystal PM
- Marketplace Category: Productivity (or Meetings and Rooms, if offered); Industry Category: Healthcare (Crystal PM's customers) or Technology
- Developer contact: a mailbox someone reads, for example it@crystalpm.com

## App Listing, Links and Support

- Support URL: https://www.crystalpm.com/contact (or the support page you have)
- Privacy policy URL: https://www.crystalpm.com/privacy
- Terms of use URL: https://www.crystalpm.com/terms
- Documentation URL: https://github.com/ben-abeo/croom.to (the README)
- Privacy Policy acknowledgment: tick it.
- Support contact email: the same mailbox as above

## Basic Information (Production)

- OAuth Redirect URL: https://www.crystalpm.com/ (never used by the rooms; it
  only has to be a page on a verified domain).
- Contact Information: the developer's name and the same monitored mailbox.

## App Listing, EU and Discoverability

- Discoverability: Set my app as "Unlisted".
- The nine EU fields (business name, address, email, telephone, last 4 digits
  of a bank account, trade register number or DUNS, bank name, identification
  document, compliance declaration) are the EU Digital Services Act "trader"
  details Zoom collects for apps offered to EU users. Crystal Meet Rooms is
  not offered to anyone, so take one of these two ways out, in this order:
  1. Email integration.testers@zoom.us: "Crystal Meet Rooms (General App,
     account level) is an internal, device-specific app used only by Crystal
     PM's own conference rooms. It is not offered or sold to anyone, so we are
     not a trader under the DSA. Please remove the EU trader fields from our
     build flow so we can submit." Zoom's docs offer exactly this.
  2. Or turn off "List my app in the EU" in that section (the opt-out). The
     app is then unavailable to EU Zoom accounts, which costs nothing for an
     app nobody installs; whether that also affects joining a meeting hosted
     by an EU account is not documented, so prefer the email.
- If Zoom insists on the fields anyway: business name Crystal PM, the office
  address, the monitored mailbox and the office telephone number; the bank,
  identification and compliance items are described by Zoom as applying to
  traders that charge for the app, which this app does not.

## Technical Design

### Overview tab

- Technology Stack:

  The room device is a Raspberry Pi 5 running Raspberry Pi OS (64-bit). The
  Crystal Meet agent is a Python 3.12 service (the open-source croom.to
  project, forked by Crystal PM) using aiohttp for its local HTTP server and
  outbound HTTPS, Playwright with its bundled Chromium to display the meeting
  on the TV, PyYAML for configuration, and google-api-python-client to read the
  room's Google Calendar through a service account. Zoom pieces: the Zoom Web
  Meeting SDK 6.5.0 (Client View) loaded from source.zoom.us into a one-page
  site the agent serves on 127.0.0.1 only; a Meeting SDK signature (HS256 JWT,
  Python standard library hmac/hashlib) minted from the SDK app's client id and
  secret; the Server-to-Server OAuth token endpoint (https://zoom.us/oauth/token,
  account_credentials grant); and GET https://api.zoom.us/v2/users/{user}/token
  ?type=zak for the room's own Zoom user, scope user:read:zak:admin, used only
  for meetings hosted by other accounts. The companion dashboard (Node.js,
  Express, Postgres under Docker on a separate Raspberry Pi) tracks device
  status on the office network and never contacts Zoom. No cloud services,
  databases or third-party applications are involved on the Zoom path.

- Architecture Diagram: upload architecture.png (or architecture.pdf) from this folder.
- Application Development, answer honestly; "Yes" obliges you to upload evidence:
  1. Secure software development process (SSDLC): Yes only if Crystal PM has a
     written process it can attach; otherwise No. (Code is reviewed before merge
     and every change ships with automated tests, which you can say in the notes.)
  2. SAST and/or DAST: No, unless Crystal PM runs such scans on this code.
  3. Periodic third-party penetration testing: No.
  4. Additional documents: optional; attach Crystal PM's security or privacy
     policy if one exists. Nothing is required.

### Security tab (three questions; wording may differ)

- Data stored: none of Zoom's data is stored. The device keeps its own
  credentials (SDK client id and secret, Server-to-Server credentials, the
  room user's address) in a root-only file; the ZAK is held in memory for one
  join and discarded. No meeting content, recordings, chat or participant data
  is captured or kept.
- Data in transit: all calls to Zoom are HTTPS; meeting media is Zoom's own
  encrypted transport inside Zoom's web SDK. The room page and the dashboard
  are reachable only on the office network.
- Access and retention: nothing retained; access to the device is by SSH with
  the office's accounts; secrets can be revoked at any time by regenerating
  them in the Marketplace or deactivating the Server-to-Server app.

## Publish page

- Audience: External Zoom users (that is the choice that goes through the
  review; the app stays unlisted).
- Verify domain: crystalpm.com (DNS record or file, done by whoever manages
  the site).
- Release notes for the reviewer: "Crystal Meet Rooms is a device-specific
  internal app: a Raspberry Pi behind a conference-room TV joins a meeting as
  an ordinary participant when a person presses Join on the room's screen. It
  has no sign-in flow, no user interface inside Zoom and no users outside
  Crystal PM. We can provide a screen recording of a join, or a live session
  with the review team; the device cannot be installed by reviewers."
- Test credentials: none (device-specific; see release notes).
- Activation: activate my app immediately after it is approved.
