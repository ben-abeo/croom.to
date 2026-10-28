# The room's camera on a laptop

**Date:** 2026-10-27 · **Status:** agreed with Ben in conversation, written for his review · **Branch:** `laptop-switch` off `main`
(4302d38) · **Builds on:** room booking (spec 2026-10-25), the TV remote (spec 2026-10-13), sound and camera (spec 2026-10-08).

## 1. What this is for

The Logitech MeetUp (camera, microphones and speaker in one USB device) is plugged into the TV Pi. Today a person who joins a
meeting the room system does not run, from their own laptop, unplugs the MeetUp from the TV Pi and plugs it into the laptop. Ben
wants the MeetUp to move between the TV Pi and a laptop from the touch Pi, with no cable moved, and the TV to follow: the laptop's
HDMI input while the laptop has the MeetUp, the TV Pi's input otherwise.

The hardware is an ATEN US234 USB 3.2 Gen 1 sharing switch (ordered 2026-10-27): the MeetUp on one of its device ports, the TV Pi
on its computer port 1, a cable to the table on computer port 2. The US234 moves its devices to the other computer each time its
wired remote button is pressed. The TV Pi presses that button through a relay on a GPIO pin. The laptop also has its own HDMI cable
to the TV, on an input of its own.

Success: at the table a person taps **Laptop**, and within about ten seconds their laptop has the MeetUp as camera, microphone and
speaker and the TV shows the laptop; when their booking ends, or they tap **Back to room**, the MeetUp and the TV are the room's
again and Join now works as before. The room page always says truthfully which computer has the MeetUp.

## 2. Decisions

- **The hardware.** ATEN US234; an Adafruit STEMMA Non-Latching Mini Relay (product 4409) on the TV Pi's GPIO17, its COM and NO
  across the US234 remote button's contacts, beside the button (the hand button still works). A relay was chosen over a bare PC817
  optocoupler (Ben, 2026-10-27: easiest to work with): its contacts are a plain switch, so neither the polarity nor the voltage of the
  remote's line matters, and its cable plugs onto the Pi's pins. The software only holds a pin on for a moment, so an optocoupler or
  a relay that triggers low (`active_low`) works with the same code.
- **The Pi's GPIO.** The existing `RaspberryPiGPIO` uses `RPi.GPIO`, which does not run on a Pi 5. The switch drives its pin through
  the kernel's GPIO character device with the `gpiod` Python bindings (libgpiod 2), finding the header's chip by its label
  (`pinctrl-rp1` on a Pi 5, `pinctrl-bcm2711` on a Pi 4), so chip renumbering between kernels does not matter. `gpiod` is optional
  like the other hardware libraries: missing, the switch is unavailable and says why.
- **Where the MeetUp is** is read, never remembered: a USB device with the MeetUp's id (`046d:0866`, to be confirmed with `lsusb` on
  the TV Pi) present in `/sys/bus/usb/devices` means the room has it; absent means the laptop has it. Read every 2 s. A power cut, a
  press of the US234's own button, or the US234 handing the MeetUp back by itself are all seen the same way.
- **Switching.** One press, then up to `switch_wait_s` (10 s) for the MeetUp to appear or leave. Not moved: look once more, and only
  then press a second time; the remote button toggles, so a second press before the first is known to have failed could send the
  MeetUp back. Still not moved: "The switch did not answer: press the button on the switch box".
- **Who wins (Ben, option C).** The laptop keeps the MeetUp until it is given back: Join now and a pasted link answer "The camera is
  on the laptop: press Back to room first" while it does. It is given back by **Back to room**, or by the room on its own when the
  booking the laptop is using ends.
- **No auto-join.** The room joins a meeting only when a person presses Join now or pastes a link (`CalendarService.on_meeting_starting`
  has no listener), so there is no automatic join to hold off or restart. A meeting that starts as the laptop's booking ends is
  joined by Join now, as today.
- **The laptop's booking (Ben, option A).** **Laptop** while a booking is in progress (from Google or the room's screens) uses it: the
  room takes the MeetUp back at its end, following the end if it moves in Google, and keeping the last end it knew if it is deleted.
  **Laptop** while nothing is booked asks how long, with the choices **Use now** offers, and books the room "Laptop" from this minute
  through `RoomBookings.start_now`, so the door sign and Google show the room taken. A booking the laptop made is ended by **Back
  to room** (End now); a booking someone else made is never touched.
- **The TV.** `tv.laptop_input` beside `tv.pi_input`, chosen under Options › TV, an HDMI input other than the Pi's. Going to the
  laptop wakes the TV and switches it to `laptop_input`; going back switches it to `pi_input`. A TV that does not answer, or does
  not report its inputs, does not stop the switch: the page says "Choose HDMI 2 on the TV with its Input button" (or HDMI 1 for the
  way back).
- **Nothing moves during a meeting.** **Laptop** is offered only while the room is not in a meeting; the route refuses it otherwise.
- **The MeetUp's own controls follow it.** In Laptop mode the room has no camera framing, no room volume and no camera preview
  (the Pi cannot reach the MeetUp); the laptop's own software (Logi Tune, the meeting app) has them. Back in the room the camera is
  found again at once and homed, since the laptop may have moved it.

## 3. What people see

**Idle screen of the touch Pi:** a **Laptop** button beside Use now, shown when `laptop.enabled` and the switch is available. With a
booking in progress one tap switches; with none it opens the Use now duration list titled "Use a laptop for", and the choice books
and switches. While it switches the button says "Switching…" and the other screens show the same.

**Laptop screen**, the touch Pi's whole page while the laptop has the MeetUp: "The camera, microphones and speaker are on the
laptop until 10:30" (no time when the MeetUp left without the page asking: "The camera, microphones and speaker are on the
laptop"), a line "Choose Logitech MeetUp as the camera, microphone and speaker in your meeting app", a large **Back to room**
button, and **TV** (the existing remote). A problem shows under them in words (the TV's input, the switch not answering).

**Back to room** (by hand or at the booking's end): the MeetUp returns, the TV goes to the Pi's input, the camera homes, the
screensaver returns, and the idle screen comes back.

**Options › TV:** a second input picker, **Laptop input**, under the room Pi's input; "Not set" until chosen. With it not set,
Laptop mode leaves the TV alone and the Laptop screen says to choose the laptop's input on the TV.

The door sign changes only through the booking: the room is shown taken by "Laptop" (or by the booking that was in progress).

## 4. Pieces

- **`croom.laptop.switch`**: `UsbSwitch` (the real one) and `FakeUsbSwitch` (tests, and a TV Pi without the hardware), one
  interface: `available`, `problem`, `holder()` → `"room" | "laptop"` (the MeetUp present or absent in sysfs), `async press()` (the
  pin on for `press_ms`, then off; `active_low` inverts), and `async move_to(holder)` (the press, wait, look again, press again, give
  up of section 2), serialised by a lock. Built from config: chip found by label, the line requested as an output held off, released
  on stop. Pure reads and the timing are injected so tests run without hardware or sleeps.
- **`croom.laptop.service`**: `LaptopService(Service)` named `laptop`, depending on `meeting`, `calendar` (its `RoomBookings`),
  `devices` and `tv`. State: `holder`, `until`, `booking_id`, `made_booking`, `switching`, `problem`, saved to `laptop.json` in the
  data directory (written whole, like `bookings.json`). `to_laptop(minutes, title)`, `to_room(by_hand)`, and a watch loop (2 s)
  that reads `holder()`, notices a move nobody asked for, follows the booking's end, and takes the MeetUp back when it passes. At
  start it loads `laptop.json`: an end already passed sends the MeetUp back at once.
- **`DevicesService`**: `release()` (close the camera, stop the preview, mark the speaker and camera away so the 30 s rediscovery
  does not chase them) and `reclaim()` (discover the camera and the speaker now, and home the camera when a home is saved).
- **`TvService`**: `show_input(input_id)` (wake, then switch, the steps `_show_the_pi` uses, with the input named), `laptop_input` in
  `TvConfig`, in `croom.tv.selection` (an `HDMI_n` other than `pi_input`, or empty), and in the TV view's state.
- **Control service**: `POST /api/laptop` with `{"to": "laptop", "minutes"?: n}` or `{"to": "room"}`, answered with the `laptop`
  state (200), or 409 with the words of section 2; `/api/status` gains `laptop`: `{enabled, available, holder, until, made_booking,
  switching, problem}`; `POST /api/meeting/join` refuses while the laptop has the MeetUp; `POST /api/tv/select` accepts
  `laptop_input`.
- **Room page** (`app.js`, `index.html`, `style.css`): the Laptop button and its duration list (the Use now list, reused), the Laptop
  screen, the Laptop input picker under Options › TV.
- **Config**: a `laptop:` section: `enabled: false`, `gpio_pin: 17`, `active_low: false`, `press_ms: 200`, `usb_id: "046d:0866"`,
  `switch_wait_s: 10`; `tv.laptop_input: ""`. Parsed and written like the other sections; a room with `enabled: false` behaves
  exactly as today.
- **`croom --check-laptop`**: the pin, the chip found, whether the MeetUp is on the Pi now, and the problem if any; with
  `--switch`, one `move_to` the other way, reporting where the MeetUp ended up and how long it took.
- **Installer**: `gpiod` in the venv's optional hardware packages; the croom user is already in the `gpio` group.

## 5. When things go wrong

| What happens | What the room does |
|---|---|
| The time asked for is already booked | Refused before any press: "That time is already booked" (`SlotTaken`) |
| Google is out of reach | The booking is kept on the device and sent later, as the screens' bookings are; the switch goes on |
| The switch does not move after the second press | A booking Laptop mode just made is cancelled; "The switch did not answer: press the button on the switch box"; the page shows where the MeetUp is |
| The TV does not answer or cannot switch inputs | The USB switch still happens; the page says which input to choose with the TV's Input button |
| Someone presses the US234's own button | Seen within 2 s: the MeetUp gone shows the Laptop screen with no end; the MeetUp back ends Laptop mode, leaving the booking alone |
| The US234 hands the MeetUp back by itself (say when the laptop is unplugged) | The same as a press by hand: back to the room |
| The TV Pi restarts in Laptop mode | `laptop.json` is read; an end already passed sends the MeetUp back at once |
| The booking's end moves in Google, or the booking is deleted | The end follows Google; a deleted booking keeps the last end known |
| Two screens tap at once | One switch at a time; the second gets "Switching…" and the state |
| No `gpiod`, no chip with the label, or the pin taken | The switch is unavailable, the Laptop button is not shown, and `/api/status` and `--check-laptop` say why |
| The MeetUp is neither seen nor expected (unplugged from the US234, the US234 without power) | The Laptop screen with no end; Back to room presses and, if it still is not seen, says the switch did not answer |

## 6. Testing

- **`UsbSwitch`** against a fake GPIO line and a fake sysfs tree: the pin's level and length (`press_ms`, `active_low`), a move seen
  in time, a slow move seen on the second look with no second press, the second press, the give-up, the lock, an unavailable chip.
- **`LaptopService`** with `FakeUsbSwitch`, a fake TV and a fake meeting service, and the real `RoomBookings` on a fake provider and
  clock: Laptop with and without a booking; refused in a meeting; Join refused while the laptop has the MeetUp; back at the booking's
  end, following a moved end; Back to room ending only a booking it made; a failed switch cancelling its booking; a move by hand
  both ways; a restart before and after the end; the TV failing without stopping the switch; `release` and `reclaim` called in order.
- **Control API**: `/api/laptop` both ways, its 409s, the `laptop` block of `/api/status`, the join refusal, `laptop_input` in TV
  select; the agent test registers `laptop` with its dependencies when enabled.
- **Room page**: the Laptop button's states, the duration list, the Laptop screen with and without an end, the TV input picker, in
  the page tests' existing style.
- Every test that passes on `main` today still passes.

## 7. When the US234 arrives

1. On the TV Pi, `lsusb` with the MeetUp on it: confirm `046d:0866` (else set `laptop.usb_id`).
2. Wire the US234: MeetUp on a device port, TV Pi on computer 1, the table cable on computer 2. Press its button by hand: the
   MeetUp leaves `lsusb` and comes back.
3. Open the remote button's housing and find the button's two contacts. Solder a wire to each (any order) and take them to the
   relay's **COM** and **NO** push-in terminals.
4. Relay cable to the Pi header: red to pin 1 (3.3 V; pin 2, 5 V, if the relay does not click), black to pin 9 (GND), white to
   pin 11 (GPIO17).
5. `croom --check-laptop`, then `croom --check-laptop --switch` twice: the MeetUp goes to the laptop side and comes back.
6. Set `laptop.enabled: true`, restart croom, choose the laptop's input under Options › TV.
7. The whole thing with a Windows laptop and a Mac: Laptop with no booking (the door sign shows "Laptop"), the TV switches, the
   laptop sees the MeetUp, Back to room, Join now works; and Laptop during a booking, left to end on its own.
8. Note what the US234 does when the laptop is unplugged mid-session and after a power cut, and record it in the guide.

## 8. Out of scope

Pan, tilt and zoom of the MeetUp from the touch Pi while the laptop has it (the laptop's software has them); the laptop's picture
on the TV through anything but its own HDMI input; more than one laptop port; other switches (the `UsbSwitch` interface leaves room
for one, such as an Acroname hub, later); a room auto-join.
