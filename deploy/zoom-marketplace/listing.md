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
- App gallery: gallery.png (1280 by 720).
- Company name: Crystal PM
- Category: Productivity (or Meetings and Rooms, if offered)
- Developer contact: a mailbox someone reads, for example it@crystalpm.com

## App Listing, Links and Support

- Support URL: https://www.crystalpm.com/contact (or the support page you have)
- Privacy policy URL: https://www.crystalpm.com/privacy
- Terms of use URL: https://www.crystalpm.com/terms
- Documentation URL: https://github.com/ben-abeo/croom.to (the README)
- Support contact email: the same mailbox as above

## App Listing, EU and Discoverability

- Discoverability: Set my app as "Unlisted".
- EU questions: the app is used only by Crystal PM in the United States; it
  processes no data about EU users; no EU representative.

## Technical Design

- Architecture: A Raspberry Pi behind each conference-room TV runs the Crystal
  Meet agent. When a person in the room presses Join on the room's touch
  screen, the agent opens Zoom's Meeting SDK (web, Client View) in a local
  browser on the TV and joins the meeting as a participant with the room's
  camera and microphone. For meetings hosted by other Zoom accounts the agent
  first obtains a ZAK for the room's own Zoom user through a Server-to-Server
  OAuth app on the same account, so the room joins as that user. Nobody signs
  in to the app; it has no web pages, no end users outside Crystal PM, and no
  in-client surface.
- Data: the only Zoom data the app reads is the ZAK of the room's own Zoom
  user, requested per join and discarded after use. Nothing is stored or sent
  anywhere else. Credentials live in a root-only file on each device.
- Scopes: user:read:zak:admin, to read the room user's ZAK (listed on the
  Server-to-Server app the devices use; the SDK app itself carries the same
  scope so the room can fall back to it).

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
