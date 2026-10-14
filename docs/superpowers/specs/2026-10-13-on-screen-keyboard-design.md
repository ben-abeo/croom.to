# The on-screen keyboard, done properly

**Date:** 2026-10-13 · **Status:** draft for Ben's review · **Replaces:** the keyboard added with the kiosk installer (2026-10-08) and kept in `keyboard.js`.

## 1. What this is for

Ben: "the touch keyboard, if you type fast on it, it will end up crashing and closing. This keyboard should have the design and functionality like an apple iPad keyboard does. I want it professional. I do like the look of it, but currently it feels a little janky and buggy. Not as crisp. Maybe we even add a slight click to each input (this could be turned off in a settings area if needed)."

The outcome: typing a link or a passcode on the table feels like typing on an iPad: every tap lands, fast typing with two thumbs works, keys answer the finger at once, the keyboard never closes on its own while someone is typing, and each key can click.

## 2. What is wrong today

The keys act on the browser's `click` event, which a touchscreen fires only after the finger lifts and only for the first finger: a second thumb that lands before the first lifts gets no click, so fast typing drops letters. A press of Shift or 123 rebuilds every key, so a finger already on a key when the rows are rebuilt presses a key that no longer exists. The keyboard hides on `focusout` of the field; a lost click or a rebuilt key can move the focus, and the keys vanish mid-word, which reads as a crash. There is no press feedback beyond the browser's `:active` colour, no key repeat, no sound.

## 3. Design

### 3.1 Input handling

- Keys act on **pointerdown**, per pointer id, with pointer capture: a letter is inserted the moment a finger lands, two fingers are two keys, and lifting does nothing (except to stop the repeat of a held key; Shift locks with a double tap, below, not a long press). `touch-action: none` on the tray; the browser's synthesized clicks are ignored.
- Rows are built once per layer; switching layer or Shift only changes labels and classes in place, so no element under a finger is ever replaced.
- The field keeps the focus by construction: the tray and its keys are not focusable, every pointerdown on them prevents the default, and the hide path runs only for a tap outside the tray and the fields, for Hide, for Join, and for a view change. A `focusout` with no related target is ignored while a key is down.
- Backspace repeats after 400 ms at 12 per second while held; Space does not repeat.
- Shift: a tap gives one capital, a double tap locks it (the key shows a lock mark), a tap again releases.
- The keyboard's state (layer, Shift) stays per field, as today.

### 3.2 Layout

iPad-style, three layers:

```
q w e r t y u i o p
 a s d f g h j k l
⇧  z x c v b n m   ⌫
123  .  -  /   [ space ]   Join   ⌄
```

- Letters; **123** gives numbers and the symbols a link or a passcode needs (`1 2 3 4 5 6 7 8 9 0` / `- / : ; ( ) $ & @ "` / `#+=` layer behind it with `[ ] { } # % ^ * + =` and `_ \ | ~ < > € £ ¥ •`), as iPadOS lays them.
- The last row: the layer key, three link-friendly keys, Space, **Join** (blue, submits the form the way the form's own Join does) and a hide key (⌄).
- Key caps: 52 px tall at the kiosk's 1.5x scale (a thumb's width), 6 px gaps, 8 px radius, a lighter top edge; the tray is the room page's tray colour.
- No row is wider than the first at its widest: ten keys of 118 px with 6 px gaps. On a screen of about 1250 px or more the rows are that wide and the block of them sits in the middle of the tray, and the nine letters of the middle row sit half a key in from each end, as on the iPad (a letter key is never wider than 118 px). On a narrower screen the rows run edge to edge, and that inset shrinks with the screen to nothing (below about 1120 px). The wide keys (Shift, delete, the layer keys, hide) and Space take what the rest of their row leaves. The symbols layers hold no key to 118 px: their rows share their width in flex proportions (a key 1, a wide key 1.5, Space 5, Join 1.8 up to 180 px), so a key there can be wider than the letters are.
- Shift, delete and hide are drawn as inline SVG (24 px, in the key's own colour) and named for a screen reader ("Shift", "Delete", "Hide keyboard"), not set as font glyphs: the fonts a Pi falls back to are not ours to count on. The sketch above shows them as glyphs only to show where they sit.

### 3.3 Feedback

- Pressed: the key fills navy (navy-800, `#16244F`) within the same frame and shows its glyph in a small pop-up above the key (the iPad's "key preview") for letters and symbols, not for the wide keys. The pop is at least 80 px wide and sits centred just above its key (2 px clear of it); it follows the latest finger and goes 60 ms after that finger lifts.
- **Key click**: an optional short tick through the browser's audio (Web Audio, a 12 ms sine burst at 1.2 kHz, no file, through the table Pi's audio output), on by default on the kiosk. **Options › Keyboard › Key click: On / Off** stores the choice in the room's settings file through the control service (`/api/settings`), like the screensaver style, so it holds across reloads and devices. The Vibration API is also called when present (the Forzola panel has no motor; a tablet may).

### 3.4 Design direction

- The room page's tokens: tray `#0B1220`, keys `#1F2937` (surface-800) with a 1 px top highlight at 8 % white, pressed `#16244F` (navy-800), the Join key blue `#1B52E5`, glyphs white in Lexend 1.15 rem, the preview pop-up white on navy with the glyph at 1.6 rem.
- The one memorable element is the key preview: a crisp pop that confirms every letter without the eye leaving the keys. Everything else is flat and quiet, as the iPad keyboard is.
- Fits under the link box at 1280 by 800 with the status card scrolled up as today; on a phone the keys are 44 px.

## 4. Files

`src/croom/control/static/keyboard.js` (rewritten), `style.css` (the keyboard block), `index.html` (the keyboard tray markup stays one element; Options gets the Keyboard card), `src/croom/control/service.py` (`/api/settings` gains `key_click`), the kiosk guide and README.

## 5. Testing

- A Playwright test with two overlapping touch points (CDP `Input.dispatchTouchEvent`) typing twenty characters with the second landing before the first lifts: all twenty arrive in order and the keyboard stays open. This test fails on the current keyboard (the bug Ben saw).
- Shift once, Shift double (lock), 123 and #+= layers, backspace repeat timing on the page's stopped clock, Join submits, hide hides, a tap outside hides, a view change hides, the per-field layer and target, the key click setting round-trips through `/api/settings` and the Options card, the keyboard's `fits()` at 1280 by 800.
- Pi acceptance (Ben): type a long link with two thumbs quickly on PiMeet-2; the click heard through the table Pi's output; the setting off.

## 6. Decisions made here

- Keys act on pointerdown with capture, per finger (the fix for dropped letters and the "crash").
- No DOM rebuild while a finger is down (labels change in place).
- A key click through Web Audio, on by default on the kiosk, off in Options › Keyboard.
- iPadOS layers and key order, with Join in the last row where the iPad has Return.
