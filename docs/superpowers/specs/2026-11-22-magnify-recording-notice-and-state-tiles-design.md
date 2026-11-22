# Magnify, the recording notice, and tiles that show their state

**Date:** 2026-11-22. **Status:** built; checked against stand-in pages in a real browser, not yet in a real Zoom or Meet call on a room
(section 5). Ben to check the calls in section 6.
**Builds on:** `2026-10-13-meeting-controls-and-dialogs-design.md` (the control model, the dialog policy, the meeting screen), whose
rule for a dialog with two buttons section 3 changes.

## 1. Intent

Ben asked for three things on the touch Pi during a call:

1. A way to make a shared screen larger and move around it, in Zoom and in Google Meet, with quick shortcuts on the touch Pi: "either
   increase the screen size and move around the image, or a magnifying glass type box which could be moved around. Whatever you
   think would be best and most feasible."
2. A recorded Zoom meeting puts up "This meeting is being recorded" with an OK button after the meeting has begun, and it is not
   dismissed by itself: someone taps it on the touch Pi every time.
3. The Mute and Camera buttons must show, visibly, that the room is muted or its camera is off.

## 2. Magnify

**Neither platform has a call for it.** Zoom's Meeting SDK has no method that sets a viewer's zoom on a shared screen, and Google Meet
has no API for its call page at all. Each has a zoom of its own in its menus, with labels the room would have to find and press, and
those could not be read from a real call in the build session. So the room does it in its own browser, the same way for both.

**How.** The TV Pi's browser scales the whole page it shows about a point (`croom/meeting/magnify.py`): a CSS transform on the page's
root, from 1 (the whole picture) to 4. Nothing of the platform's page is pressed or rearranged, so its controls go on reading as
they did, and it works whatever the platform draws. The screen never shows past the picture's edges. At 1 the root's own styles are
put back exactly.

- A transform on the root makes it the box its fixed-position children are laid out in, which the window was before. While the page
  is magnified the root is kept the size of the window, so a page built wholly of fixed layers (as a meeting's is) keeps its layout.
- It is a magnifying glass, not a sharper picture: text 20 pixels tall becomes 40, with no detail the platform did not draw.
- The magnification goes with the page: a call that ends starts the next one whole. If the platform reloads its page during a call,
  the TV is whole again while the touch Pi still says the zoom; any tap on the Magnify screen puts the two in step.

**The model.** Both providers add `controls.magnify` = `{available, zoom, x, y, max}`: `zoom` 1 to `max` (4), and `(x, y)` the point
of the picture at the middle of the TV, as fractions of its width and height. The control is `magnify` with `{zoom, x, y}`, any of them
left out (what is left out stays). It goes through `POST /api/meeting/control` like every other, but waits behind nothing: no press
of Meet's and no call of Zoom's is in its way.

**On the touch Pi.**

- **Magnify** in the top bar, beside Touchpad and TV: the room's own tools, not the platform's. Shown in a call whose page can be
  magnified. While the TV is magnified it is filled and says by how much ("Magnify 2×"). On a phone it goes under the bar's row.
  While the warning of the next booking is also in the bar, the kiosk's one row has no room for the word: the button is its picture
  alone and the warning is 84 px narrower, so the room's name keeps its width, the bar its height and the meeting screen its place.
- Its screen (`magnify.js`): a pad at the TV's 16:9 that stands for the whole picture, with a box for the part the TV shows.
  - A tap on the whole picture magnifies to 2× about the point tapped.
  - A finger on the box takes it where it is held; a finger outside brings the box to itself. The TV follows as it moves.
  - Two fingers spreading magnify, and pinching together makes it smaller.
  - **Smaller** / **Larger** step through 1×, 1.5×, 2×, 3×, 4×, each of which also has a button. **Whole picture** puts it back.
- One request is out at a time, at most 20 a second, and what is asked meanwhile replaces what waited: the TV goes to where the
  finger is, and never plays back where it has been. While a finger is down the screen draws what was asked, not the poll's answer.
- The screen returns to the meeting screen after two idle minutes, as the other sub-screens do; the TV stays as it is.

## 3. The recording notice

Zoom's notice has two buttons, **Leave Meeting** and **OK** (or **Got it**). The dialog policy pressed a notice on its list only when
it had exactly one button, so this one went to the table as a card, every time.

Leaving is never the room's answer. A button that leaves or ends the meeting was already never pressed (`NEVER_PRESSED`); now it is
also passed over when counting: **a notice on the list with exactly one button besides those that leave is answered with that
button.** The room stays in the meeting, and the notice's sentence stays under the meeting's title, as for any acknowledged notice.

- Still waiting for a tap: a dialog with two buttons that both stay (the host asking the room to unmute), one with only buttons
  that leave, one whose words are not on the list, and every dialog when `ask_everything` is on.
- The same rule answers Google Meet's boxes, and any other notice on the list: AI Companion's and a transcript's among them.

## 4. Tiles that show their state

A tile's label says what a tap does ("Unmute"), and its icon was the only sign of the state. Now a muted microphone and a camera that
is off turn their tile red, as Zoom's and Meet's own buttons are, with **Muted** or **Camera off** in a badge at its corner. The
label is unchanged. The badge is drawn from the button's `data-badge`, not a node in it, so the tile's words are still only its label.

## 5. Checked, and not checked

**Checked in the build session:**

- The magnifier in a real browser against pages laid out as a meeting's are: the picture doubles about its middle, the point asked
  for is at the middle of the screen, the edges hold, the screen shows the part asked for (by its pixels), nothing scrolls, fixed
  layers keep their layout with and without a root of the window's height, and the page is as it was at 1.
- Both providers, through their test pages: the model, a reading that keeps it, the next call whole, a refusal in words.
- The touch Pi's screen in a real browser at 1280 by 800, on a tablet and on a phone: the button, the steps, the drag, the tap, the
  pinch, the edges, a refusal, the call ending under it. And the tiles' colour, badge and taps.
- The policy and both providers' handling of the recording notice with Leave Meeting and OK.

**Not checked. Each needs a real call on a room:**

- That Zoom's client and Meet's call page keep their layout while their root is transformed. Either may measure itself in a way a
  transform changes (a pop-up placed from an element's drawn position would be misplaced while magnified).
- How the Pi's browser keeps up while the picture moves, with a shared screen decoding.
- How readable a shared screen is at 2× and above on the room's TV, which depends on what the platform sends.
- The real words and buttons of Zoom's recording notice in the SDK version the rooms run. The journal shows them:
  `Zoom dialog: … buttons [...]` and then `Acknowledged Zoom's dialog with 'OK'`. If it still waits on the table, that line says why.

## 6. My calls (Ben to check)

- **Magnify the whole page, not the shared screen alone.** It needs nothing of either platform's page to stay as it is today, and
  it is one mechanism for both. The cost is that people's tiles and the platform's bar are magnified with it.
- **A box on a pad, with no live picture on the pad.** A picture of the TV on the touch Pi would let anyone on the office network
  fetch what a meeting is showing. The TV itself is what people watch while they move the box.
- **The button in the top bar.** The meeting screen's rows are full at 1280 by 800 for a host.
- **Up to 4×, and steps of 1, 1.5, 2, 3, 4.**
- **The recording notice is answered for every notice on the list, not recordings alone.** One rule, and `ask_everything` is still
  there for a room that wants every pop-up to wait.
- **Red for off.** It is what both platforms use, and Leave is in another column.
