# Book the room from its screens

**Date:** 2026-10-25 · **Status:** built at Ben's word ("just build it all"), on the design agreed in conversation; no separate plan.

## 1. What this is for

Ben: people walk up to the door sign or the room's touch screen and (1) reserve the room for a time, for an in-person meeting that
needs no Zoom or Meet link (pick a date, an open time, a name, and a length from a list in 15-minute steps), or (2) start a meeting
now for X minutes, with a name, when the room is free; (3) either blocks the time in the room's Google calendar. The rooms must keep
working when Google has a problem: what is booked is kept on the device as well as in Google.

## 2. Decisions

- **Where the bookings live.** The room device keeps every booking made at its screens in `bookings.json` (its data directory) and
  sends it to the room's Google calendar from there. The screens never hold Google credentials. The last calendar read from Google
  is kept in `calendar-cache.json`, so a device that restarts while Google is out of reach still shows its day.
- **What the room shows** is Google's bookings with the room's own laid over them. The room's version of its own booking wins while
  a change of it waits to be sent; with nothing waiting, Google's word goes (a booking moved or deleted in Google follows Google).
- **The tag.** Each booking is written with a private extended property `croom_id`, the room's id for it. It is how the room knows
  its own (End now and Cancel are only for those, never for meetings booked in Google by people), and how a create whose answer was
  lost is found instead of being made twice.
- **The sync.** Changes are sent in the order they were made, on the spot when Google answers, else on every poll (60 s) until they
  go; the first that cannot go stops the round. A calendar that refuses the write (shared with See all event details) keeps the
  booking waiting and says what to share; anything else is Google out of reach.
- **Clashes.** Booked while Google answers, a time Google has taken since the last poll is refused with the other booking's name
  ("That time was just booked in Google Calendar: …"). A booking that waited is written whatever Google has meanwhile (Ben chose
  "room wins if started": a meeting in progress is the truth; a future one is written too), and any booking of the room's that
  overlaps another shows "Double-booked with …" on both screens until someone moves or cancels one.
- **The rules.** Book: today and the six days after it (the week the room reads), starts every 15 minutes from `booking_start` to
  the last start before `booking_end` (`calendar.booking_start` / `booking_end` in the config, `"08:00"` and `"18:00"` unless set;
  hours it cannot use are warned about and the defaults kept), never in the past; 15 minutes to four hours in steps of 15, up to the
  next booking and `booking_end`. Use now: from this minute, not held to the booking hours, 15 minutes to four hours up to the next
  booking; not offered while the room is taken or the next booking is less than 15 minutes away. A name of 1 to 60 characters.
- **End now** shortens a booking in progress to this minute (one that has not started is cancelled instead). It is on the room's
  touch screen only, never on the door sign (Ben: "The end now should only be able to be done from the touch controller"); the route
  refuses a request that does not say it comes from the room page. **Cancel** deletes a booking that has not started, from either
  screen's Today list, with a second tap.
- **Anyone at a screen can book**, as anyone can Join: there is no sign-in.

## 3. Pieces

- `croom.calendar.bookings`: `open_starts` and `now_minutes` (the rules, pure), `Booking`, and `RoomBookings` (the store, the cache,
  the merge, `book`, `start_now`, `end`, `cancel`, `sync`, `state`).
- `GoogleCalendarProvider`: scope `calendar.events` added; `find_booking`, `create_booking`, `update_booking`, `delete_booking`,
  `check_booking` (a quarter hour on January 1, 2000, written and deleted, for `croom --check-calendar`); 401, and 403 that is not a
  rate limit, raise `CalendarWriteRefused`; `_parse_event` reads the tag into `CalendarEvent.booking_id`.
- `CalendarService`: owns the `RoomBookings` (when Google and a calendar address are configured), shows the cache until Google
  answers, sends what waits before each poll's read, and hands each read to the bookings.
- Control service: `GET /api/bookings/slots?date=`, `POST /api/bookings`, `POST /api/bookings/now`, `POST /api/bookings/{id}/end`
  (room page only, else 403), `POST /api/bookings/{id}/cancel`; `calendar.booking` in the status (online, offline_since, pending,
  problem, hours, what Use now offers, and a revision that moves with every change so the screens read the day again at once); events
  carry `booking_id`, `pending` and `clash`.
- `booking.js`: the steps, shared by both pages, each page giving them a place and its own styles; they close after a minute alone.
  The door sign has Use now and Book under the status and takes its whole width for the steps; the room page has Use now, Book room
  and End now in the status card and a `booking` view, and its keyboard types the name (`data-keyboard="name"`, Done books).
- `croom --check-calendar` proves the room can book, and says what waits to be sent and what is double-booked.

## 4. Setting it up

Each room calendar is shared with the service account with **Make changes to events**, which needs the Workspace setting
"Share all information, and outsiders can change calendars" for secondary calendars (the service account is an outside address).
The Google Calendar guide and the room guide's "Book the room" say so.
