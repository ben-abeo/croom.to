# The door sign, redesigned

**Date:** 2026-10-13 · **Status:** approved in principle by Ben ("Plan it, build it and push it to main when done implementing"); the mockups he saw are the design · **Replaces:** the door sign's look from the brand spec (2026-09-24), where the whole background took the status colour.

## 1. What this is for

Ben: "Not a fan of the green, yellow and red backgrounds. I want it to look cleaner and more professional while incorporating the color statuses as well still." The sign must show the company name and logo, the room name, the room's status, upcoming meetings, a button for a week's calendar, the time and the date; "tie in some sort of Austin, TX skyline and/or University of Texas inspiration background", with "an option for setting the background given a list of a few"; and, as an option that is off by default and switchable from the touchscreen, a Longhorns football scoreboard at the bottom ("Texas (#4) 45 - Texas A&M 42" and "Next: 10/19 - Texas (#4) @ Missouri (#9)").

The outcome: a sign that reads from the hallway (free, soon or in use at a glance), looks like the rest of Crystal Meet and the Crystal PM brand, says who the room belongs to, and carries a little Austin.

## 2. Design

### 2.1 The status, without a coloured wall

The status colour lives in three places that together read from the hallway and still look calm up close: a solid rail down the left edge (22 px), a solid chip under the room name ("Free until 1:00 PM", "Customer call starts in 8 minutes", "In use until 2:00 PM") with navy text on green and amber and white text on red, and the scenery along the bottom tinted in the status colour at low opacity, so the whole lower third glows faintly green, amber or red. Nothing else is coloured. The three colours: free `#1FA971`, soon `#E8A013`, busy `#E5484D`, on the brand's navy `#001636`.

### 2.2 The layout

Landscape (the Pi's 1280 by 800 and anything near it):

```
 ▌ [logo] Crystal Practice Management                 12:50 PM
 ▌                                            Monday, October 13, 2026
 ▌ Boardroom                                  Today            [This week]
 ▌ 2nd floor, by the kitchen                  1:00 PM  Customer call …  Zoom
 ▌ ( ● Free until 1:00 PM )                   2:30 PM  Design review    Google Meet
 ▌ Next: Customer call with Lone Star Eye Care 4:00 PM  Sales pipeline  Zoom
 ▌
 ▌      ~~~~~ Austin skyline silhouette, tinted in the status colour ~~~~~
 ▌ Texas (#4) 45 – Texas A&M 42            Next: 10/19 · Texas (#4) @ Missouri (#9)   (optional)
```

- The brand row: the white Crystal PM logo and "Crystal Practice Management" (the company name is `room.company` in the config, default "Crystal Practice Management", so another practice could use the software).
- The room name at 5.4 rem bold, the location under it, then the chip and one line of detail (the next booking's title, or the current one's).
- Today: the next three bookings as rows (time, title, platform), the current one ringed in the status colour; **This week** opens the week view.
- The clock and the date top right, as the room page shows them (12-hour, "Monday, October 13, 2026").
- Portrait (a tablet by the door, 800 by 1280): the same, stacked; the scenery shorter.

### 2.3 The week view

A tap on **This week** replaces the Today list with seven columns, today first, each a day name and date with its bookings (time and title, platform in small type), empty days saying "Nothing booked"; **Back** or 30 idle seconds return to the Today list. The data is `GET /api/calendar/week`: the calendar service already fetches a week ahead, so the control service groups its events by local day for the next seven days.

### 2.4 Backgrounds

Photographs, not drawings (amended 2026-10-14 00:40 after Ben's review of the drawn set: "try a more realistic approach"). The sign fills its whole surface with a photograph of Austin and lays a navy overlay over it, heavier on the left where the words are (from 94 % navy at the left edge to 55 % at the right, and darker again along the bottom), so the room name, the chip and the bookings read as before while the place shows through. The status rail, the solid chip and the ringed current booking carry the status colour; the photograph itself is not tinted.

The set, chosen from the touchscreen under Options › Door sign: **Austin skyline** (the default, the towers from Lady Bird Lake at dusk), **Capitol**, **UT Tower**, **Pennybacker Bridge**, **Hamilton Pool**, **Texas star** (the Bullock museum), and when photographs are found for them **Hill Country** and **DKR stadium**; **Plain** keeps the navy alone. The choice is a room setting (`sign_background`) kept by the control service beside the screensaver style and read by the sign from `/api/status`, so it survives reloads and applies to every sign of the room.

The photographs ship with the page as `src/croom/control/static/backgrounds/<key>.jpg` (1920 px wide, progressive JPEG, about 200–400 KB each), with `CREDITS.md` beside them naming each photograph's author and licence. The first set comes from Wikimedia Commons under CC BY and CC BY-SA licences, which allow this use with attribution; the room guide repeats the credits. Any photograph may be swapped for one Ben licenses or takes, or one generated for the purpose, by replacing the file and its credit line.

### 2.5 The college football scoreboard

Off by default. **Options › Door sign › Football scores** on the touchscreen lists the schools and each is a switch: **Texas**, **Michigan**, **Texas A&M**, **Missouri** (`sign_scores`, a list of the schools turned on; empty means off). When at least one is on, the agent's `croom.scores` module fetches each chosen school's football schedule from ESPN's public site API every ten minutes (`site.api.espn.com/apis/site/v2/sports/football/college-football/teams/<id>/schedule`, an unauthenticated JSON feed; team ids: Texas 251, Michigan 130, Texas A&M 245, Missouri 142; the fetcher is one function behind an interface, so a different source is a one-file change) and keeps, per school, the last completed game and the next one: `{"school": "texas", "name": "Texas", "last": {"home": "Texas", "home_rank": 4, "home_score": 45, "away": "Texas A&M", "away_rank": null, "away_score": 42, "date": "2026-10-12"}, "next": {"date": "2026-10-19", "home": "Missouri", "home_rank": 9, "away": "Texas", "away_rank": 4}}`.

The sign draws the strip along the bottom: the school's last score on the left ("Texas (#4) 45 – Texas A&M 42") and the next game on the right ("Next: 10/19 · Texas (#4) @ Missouri (#9)"), ranks in burnt orange `#FFB366`. With more than one school on, the strip shows one school at a time and moves to the next every 8 seconds, the school's name leading the line ("Michigan: Michigan (#12) 31 – Ohio State 24"). No data, a fetch failure or every school off hide the strip; nothing else on the sign moves. `/api/status` carries `scores` (a list, one entry per school on) when any is on.

### 2.6 Design direction

Tokens from the Crystal brand: navy `#001636` base, panels at 6 % white, blue `#1B52E5` for nothing but focus, periwinkle `#BDCEFF` for secondary text, white for the room name and titles; Lexend throughout (the two weights the page ships); 12 px radii; no gradients, no shadows beyond the scenery's own layering, no labels in capitals. The one memorable element is the tinted skyline that takes the room's status colour; everything above it is quiet and aligned to the left rail.

## 3. Files

`sign.html`, `sign.css`, `sign.js` (restyled; the week view and the strip), `src/croom/control/service.py` (`/api/calendar/week`, `/api/settings` with `sign_background` and `sign_scores`, `scores` in the status), `src/croom/scores/` (new: the fetcher, the parser, a cached `ScoresService`), `src/croom/core/config.py` (`room.company`), `index.html`/`app.js` (Options › Door sign card), the installer's stock config, the room setup guide and README.

## 4. Testing

- Sign page (Playwright): each state's rail, chip and tint (computed colours), the chip's wording, the brand row with the company name, the clock and date, the Today rows and the ringed current one, This week and the week view's seven columns from `/api/calendar/week` with its 30 s return on the page's stopped clock, each background's SVG present and the plain glow, the strip hidden by default and shown with the setting and data, `fits()` at 1280 by 800 and 800 by 1280, every existing sign test kept.
- Control service: `/api/calendar/week` grouping across a month boundary and an empty week; `/api/settings` for the two keys with bad values refused; `scores` in the status only when on.
- Scores: the parser against a saved ESPN schedule JSON (a fixture file) for the last and next game, ranks present and absent, a bye week, a game in progress (counts as neither), the season over (no next game); the service's ten-minute cache, a fetch failure keeping the last good answer, and the setting off fetching nothing.
- Options card on the touchscreen: the background and scoreboard choices round-trip.
- Pi acceptance (Ben): the sign on the hallway screen in each state; the week view; each background; the strip after a game weekend.

## 5. Decisions made here

- The status colour moves from the whole background to a rail, a solid chip and the tinted scenery (Ben: cleaner, professional, still colour-coded).
- Photographs under a navy overlay, settable from the touchscreen, Austin skyline by default (Ben: skyline, UT, the signature landscapes, a list to choose from; the drawn silhouettes were dropped after his review).
- The scoreboard is built now but off by default, with Texas, Michigan, Texas A&M and Missouri each switchable from the touchscreen (Ben: eventually, optional, not on by default; the four schools, configurable).
- The company name is configurable with the Crystal PM default.
- The design uses the Crystal brand tokens; the plugin link Ben gave (plugin_01Vkx3KVjWNESJpXEVjrxKpy) is not among the plugins visible to this session, so the house style came from the crystal-brand-docs skill on this machine.
