# Conference room technical manuals

For the people who install, change and troubleshoot the conference room equipment. Employees who only book and use the rooms should start at [Conference rooms: getting started].

## How the pieces fit

| Device | What it is | What it runs |
| --- | --- | --- |
| **TV Pi** | The Raspberry Pi behind the TV | The `croom` service: calls (Google Meet, Zoom), the room's calendar and bookings, the pages the other devices show, and room Settings. Everything else depends on it. |
| **Touch Pi** | The touch screen on the table | A full-screen browser showing the TV Pi's room page, plus a small agent that checks in with the TV Pi for updates, reboots and Wi-Fi changes. |
| **Door sign** | The SMT1019 Android panel by the door | The Crystal Meet Door Sign app, showing the TV Pi's `/sign` page and driving the LED light bar. |

All three talk over the office network to the TV Pi on port **8080**. Room Settings (behind the room PIN) are opened on the touch Pi under **Options › Settings**.

## The manuals

| Page | Covers |
| --- | --- |
| [Touch Pi technical manual] | The table screen: every screen and control, installation, Settings, updates, logs and troubleshooting |
| [TV Pi technical manual] | The room device: the croom service, Google Meet and Zoom, calendar, TV control, camera, Laptop mode, updates, logs and troubleshooting |
| [Door sign technical manual] | The door panel: hardware and light bar, setup as device owner, check-in, updates and Wi-Fi, building releases, troubleshooting |

Each manual is also attached to its page as a PDF.

## Related setup guides (in the repository, docs/guides)

- Set up a Crystal Meet room
- Connect Crystal Meet rooms to Google Calendar
- Let Crystal Meet rooms join Google Meet
- Connect Crystal Meet rooms to Zoom
- Set up the Crystal Meet dashboard
