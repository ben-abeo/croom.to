# On-Screen Keyboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The room page's own keyboard types like an iPad's: every tap lands, two thumbs typing fast work, keys answer the finger at once with a preview pop and an optional click, and the keyboard never closes on its own mid-word.

**Architecture:** `keyboard.js` is rewritten around pointer events (one insertion per `pointerdown`, per pointer id, with capture; the browser's clicks ignored), rows built once per layer with labels changed in place, a hide path that runs only for a tap outside the tray and fields, Hide, Join or a view change. Three iPadOS layers. A key preview and a pressed state in CSS. A Web Audio tick and the Vibration API for the click, behind a room setting (`key_click`) stored through the control service's settings file beside the screensaver choice, with an Options › Keyboard card.

**Tech Stack:** vanilla JS/CSS, aiohttp (one settings route), Playwright tests through CDP touch events, pytest.

**Spec:** `docs/superpowers/specs/2026-10-13-on-screen-keyboard-design.md`

**Base:** branch `keyboard` from `main`.

## Global Constraints

- Internal names stay `croom`; Crystal Meet in copy; no attribution lines in commits.
- Tests: `.venv/bin/pytest -q -p no:cacheprovider -W error <path>`; the page tests drive real Chromium through Playwright; never the whole suite from a task.
- The kiosk page (`?keyboard=1`, 1280 by 800) keeps `fits()` with the keyboard closed, and with it open the link box stays above the keys as today (`body.keyboard-open .screen { padding-bottom }`).
- Every existing keyboard test in `tests/unit/control/test_page.py` keeps passing or is changed deliberately with its reason in the commit message (the helpers `open_kiosk_page` and `tap` stay; `tap` presses keys the way a finger does, through `pointerdown`/`pointerup` dispatch or Playwright's `tap`).
- No new fonts, no images: the keys are CSS; the glyphs are text.
- The tray's markup stays one `#keyboard` element so the layout rules keep working.

## Review Focus

1. Two fingers landing on two keys within 40 ms, the second before the first lifts, must produce both characters in order and leave the keyboard open (Task 1 test `test_two_thumbs_typing_fast_lose_nothing_and_the_keyboard_stays`).
2. A finger that lands on Shift, 123 or #+= while another finger is still down on a letter must not break that letter's key (no element under a finger is replaced) (Task 1 test `test_switching_layers_under_a_finger_replaces_no_key`).
3. A `focusout` of the field with no related target while a key is down must not hide the keyboard (Task 1 test `test_a_lost_focus_while_a_key_is_down_does_not_close_the_keys`).
4. The key click must never throw where audio is unavailable (an AudioContext that fails to construct, or autoplay refused): typing goes on silently (Task 3 test `test_the_click_fails_quietly_without_audio`).
5. Backspace held for a second removes about twelve characters and stops when lifted; a held letter inserts once (Task 1 test `test_backspace_repeats_while_held_and_letters_do_not`).

---

### Task 1: Pointer-driven input and the fast-typing fix

**Files:**
- Modify: `src/croom/control/static/keyboard.js` (rewrite)
- Test: `tests/unit/control/test_page.py`

**Interfaces:**
- Consumes: `window.croomPage` (`el`, `view`, `kiosk`, `submitForm`, `onViewChange`), `#keyboard`, `#link-input`, `#passcode-input`, `#keyboard-toggle`, `#link-form`.
- Produces: `window.croomKeyboard = {open(), hide(), isOpen(), layer(), press(key)}` for tests and devices; keys carry `data-key` (`a`…`z`, `0`…`9`, symbols, `shift`, `backspace`, `space`, `join`, `hide`, `layer:letters`, `layer:symbols`, `layer:more`); the tray carries `data-layer` and `data-shift` (`off`, `once`, `lock`).

- [ ] **Step 1: Touch helpers and failing tests**

Add to `tests/unit/control/test_page.py` near `tap`:

```python
def touch(page, selector, pointer_id=1, lift=True, move_to=None):
    """A finger on an element: pointerdown (and pointerup unless lift is False), the way a touchscreen fires them."""
    box = page.locator(selector).bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.evaluate("""([x, y, id, lift]) => {
        const target = document.elementFromPoint(x, y);
        const init = { pointerId: id, pointerType: 'touch', isPrimary: id === 1, bubbles: true, cancelable: true, clientX: x, clientY: y, buttons: 1 };
        target.dispatchEvent(new PointerEvent('pointerdown', init));
        if (lift) target.dispatchEvent(new PointerEvent('pointerup', Object.assign({}, init, { buttons: 0 })));
    }""", [x, y, pointer_id, lift])


def lift(page, selector, pointer_id=1):
    box = page.locator(selector).bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.evaluate("""([x, y, id]) => { document.elementFromPoint(x, y).dispatchEvent(new PointerEvent('pointerup',
        { pointerId: id, pointerType: 'touch', isPrimary: id === 1, bubbles: true, cancelable: true, clientX: x, clientY: y, buttons: 0 })); }""", [x, y, pointer_id])


def test_two_thumbs_typing_fast_lose_nothing_and_the_keyboard_stays(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        text = "zoomusjmeeting"
        for i, ch in enumerate(text):                              # the next finger lands before the last one lifts
            touch(page, f"#keyboard .key[data-key='{ch}']", pointer_id=1 + (i % 2), lift=False)
            if i:
                lift(page, f"#keyboard .key[data-key='{text[i - 1]}']", pointer_id=1 + ((i - 1) % 2))
        lift(page, f"#keyboard .key[data-key='{text[-1]}']", pointer_id=1 + ((len(text) - 1) % 2))
        assert page.input_value("#link-input") == text
        assert not page.evaluate("document.getElementById('keyboard').hidden")
        page.close()


def test_switching_layers_under_a_finger_replaces_no_key(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        same = page.evaluate("""() => { const k = document.querySelector("#keyboard .key[data-key='q']"); window.__q = k; return true; }""")
        touch(page, "#keyboard .key[data-key='q']", pointer_id=1, lift=False)
        touch(page, "#keyboard .key[data-key='layer:symbols']", pointer_id=2)
        assert page.evaluate("document.getElementById('keyboard').dataset.layer") == "symbols"
        lift(page, "#keyboard .key[data-key='1']", pointer_id=1)     # the finger lifts where q was: the element is still the same node
        assert page.evaluate("window.__q.isConnected")                # the key under the finger was never replaced
        assert page.input_value("#link-input") == "q"
        page.close()


def test_a_lost_focus_while_a_key_is_down_does_not_close_the_keys(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        touch(page, "#keyboard .key[data-key='a']", pointer_id=1, lift=False)
        page.evaluate("document.getElementById('link-input').dispatchEvent(new FocusEvent('focusout', { relatedTarget: null }))")
        assert not page.evaluate("document.getElementById('keyboard').hidden")
        lift(page, "#keyboard .key[data-key='a']", pointer_id=1)
        assert page.evaluate("document.activeElement.id") == "link-input"
        page.close()


def test_backspace_repeats_while_held_and_letters_do_not(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.clock.install()
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        page.fill("#link-input", "abcdefghijklmnopqrstuvwxyz")
        touch(page, "#keyboard .key[data-key='backspace']", pointer_id=1, lift=False)
        page.clock.run_for(400 + 1000)
        lift(page, "#keyboard .key[data-key='backspace']", pointer_id=1)
        removed = 26 - len(page.input_value("#link-input"))
        assert 11 <= removed <= 14, removed                          # one at the press, then 12 a second
        page.clock.run_for(1000)
        assert 26 - len(page.input_value("#link-input")) == removed  # stopped when lifted
        touch(page, "#keyboard .key[data-key='x']", pointer_id=1, lift=False)
        page.clock.run_for(1500)
        lift(page, "#keyboard .key[data-key='x']", pointer_id=1)
        assert page.input_value("#link-input").endswith("x") and not page.input_value("#link-input").endswith("xx")
        page.close()


def test_shift_once_double_locks_and_a_tap_releases(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        touch(page, "#keyboard .key[data-key='shift']")
        touch(page, "#keyboard .key[data-key='a']")
        touch(page, "#keyboard .key[data-key='b']")
        assert page.input_value("#link-input") == "Ab"
        touch(page, "#keyboard .key[data-key='shift']")
        touch(page, "#keyboard .key[data-key='shift']")               # a double tap locks
        assert page.evaluate("document.getElementById('keyboard').dataset.shift") == "lock"
        touch(page, "#keyboard .key[data-key='c']")
        touch(page, "#keyboard .key[data-key='d']")
        touch(page, "#keyboard .key[data-key='shift']")
        touch(page, "#keyboard .key[data-key='e']")
        assert page.input_value("#link-input") == "AbCDe"
        page.close()
```

Keep `tap(page, *keys)` working by reimplementing it with `touch()` (a press and lift per key).

- [ ] **Step 2: Run them to see them fail** — `.venv/bin/pytest -q -p no:cacheprovider -W error tests/unit/control/test_page.py -k "two_thumbs or under_a_finger or lost_focus or backspace_repeats or shift_once"` — Expected: FAIL (the current keys act on click; a layer switch rebuilds; focusout hides).

- [ ] **Step 3: Rewrite `keyboard.js`**

```js
// The page's own keyboard, for screens without one (spec 2026-10-13 on-screen keyboard). Keys act the moment a
// finger lands, per finger, so two thumbs typing fast lose nothing; rows are built once per layer and their labels
// change in place, so nothing under a finger is ever replaced; the keys never take the focus, and hide only for a tap
// elsewhere, Hide, Join or a view change. The kiosk (?keyboard=1) opens it when a field is tapped; the button beside
// the link field toggles it on any device.
(function () {
  "use strict";
  const P = window.croomPage;
  const el = P.el;
  const LAYERS = {
    letters: [["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"], ["a", "s", "d", "f", "g", "h", "j", "k", "l"],
              ["shift", "z", "x", "c", "v", "b", "n", "m", "backspace"], ["layer:symbols", ".", "-", "/", "space", "join", "hide"]],
    symbols: [["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"], ["-", "/", ":", ";", "(", ")", "$", "&", "@", "\""],
              ["layer:more", ".", ",", "?", "!", "'", "backspace"], ["layer:letters", "space", "join", "hide"]],
    more: [["[", "]", "{", "}", "#", "%", "^", "*", "+", "="], ["_", "\\", "|", "~", "<", ">", "€", "£", "¥", "•"],
           ["layer:symbols", ".", ",", "?", "!", "'", "backspace"], ["layer:letters", "space", "join", "hide"]],
  };
  const LABELS = { shift: "⇧", backspace: "⌫", space: "space", join: "Join", hide: "⌄", "layer:symbols": "123", "layer:more": "#+=", "layer:letters": "ABC" };
  const WIDE = new Set(["shift", "backspace", "layer:symbols", "layer:more", "layer:letters", "hide"]);
  const REPEAT_AFTER_MS = 400, REPEAT_EVERY_MS = 83, DOUBLE_TAP_MS = 350;
  const kiosk = P.kiosk;
  const box = el("keyboard");
  const link = el("link-input");
  const passcode = el("passcode-input");
  const toggle = el("keyboard-toggle");
  let input = link;
  let open = false;
  let shift = "off";                 // off | once | lock
  let lastShiftAt = 0;
  const layers = new Map([[link, "letters"], [passcode, "symbols"]]);
  const built = new Map();           // layer name -> its rows, built once
  const down = new Map();            // pointerId -> { key, timer, interval }
  let listeners = [];                // what tests may hook (press)

  const field = () => (input.hidden ? link : input);
  const layer = () => layers.get(field());
  const label = (key) => (LABELS[key] || (shift !== "off" && layer() === "letters" ? key.toUpperCase() : key));

  function keyButton(key) {
    const b = document.createElement("button");
    b.type = "button";
    b.tabIndex = -1;
    b.className = "key" + (WIDE.has(key) ? " wide" : "") + (key === "space" ? " space" : "") + (key === "join" ? " join" : "");
    b.dataset.key = key;
    const cap = document.createElement("span");
    cap.className = "cap";
    cap.textContent = label(key);
    b.append(cap);
    b.addEventListener("pointerdown", onDown);
    b.addEventListener("pointerup", onUp);
    b.addEventListener("pointercancel", onUp);
    b.addEventListener("click", (e) => e.preventDefault());   // the browser's own click: the pointerdown already acted
    return b;
  }
  function build(name) {
    const rows = LAYERS[name].map((keys) => {
      const r = document.createElement("div");
      r.className = "row";
      r.append(...keys.map(keyButton));
      return r;
    });
    built.set(name, rows);
    return rows;
  }
  function relabel() {
    for (const b of box.querySelectorAll(".key")) b.querySelector(".cap").textContent = label(b.dataset.key);
    box.dataset.shift = shift;
    box.dataset.layer = layer();
  }
  function show() {
    if (P.view() !== "idle" || open) return;
    open = true;
    box.replaceChildren(...(built.get(layer()) || build(layer())));
    relabel();
    box.hidden = false;
    document.body.classList.add("keyboard-open");
    toggle.setAttribute("aria-pressed", "true");
    field().scrollIntoView({ block: "center" });
  }
  function hide() {
    if (!open) return;
    open = false;
    for (const d of down.values()) stopRepeat(d);
    down.clear();
    box.hidden = true;
    document.body.classList.remove("keyboard-open");
    toggle.setAttribute("aria-pressed", "false");
  }
  function switchLayer(name) {
    layers.set(field(), name);
    shift = "off";
    if (!open) return;
    const rows = built.get(name) || build(name);
    for (const r of rows) if (!r.isConnected) box.append(r);
    for (const [other, rowsOf] of built) if (other !== name) for (const r of rowsOf) r.remove();   // the pressed key's node stays alive in `built`
    relabel();
  }
  function edit(text, backspace) {
    const target = field();
    const start = target.selectionStart === null ? target.value.length : target.selectionStart;
    const end = target.selectionEnd === null ? start : target.selectionEnd;
    if (backspace) target.setRangeText("", start === end ? Math.max(0, start - 1) : start, end, "end");
    else target.setRangeText(text, start, end, "end");
    target.dispatchEvent(new Event("input", { bubbles: true }));
    target.focus({ preventScroll: true });
  }
  function act(key) {
    for (const fn of listeners) fn(key);
    if (key === "shift") {
      const now = Date.now();
      if (shift === "once" && now - lastShiftAt < DOUBLE_TAP_MS) shift = "lock";
      else shift = shift === "off" ? "once" : "off";
      lastShiftAt = now;
      relabel();
      return;
    }
    if (key.startsWith("layer:")) { switchLayer(key.slice(6)); return; }
    if (key === "hide") { hide(); return; }
    if (key === "join") { hide(); P.submitForm(el("link-form")); return; }
    if (key === "backspace") { edit("", true); return; }
    if (key === "space") { edit(" "); return; }
    edit(shift !== "off" && layer() === "letters" ? key.toUpperCase() : key);
    if (shift === "once") { shift = "off"; relabel(); }
  }
  function stopRepeat(d) { clearTimeout(d.timer); clearInterval(d.interval); }
  function onDown(e) {
    e.preventDefault();   // the field keeps the focus
    const b = e.currentTarget;
    try { b.setPointerCapture(e.pointerId); } catch (err) { /* synthetic events have no capture */ }
    b.classList.add("down");
    act(b.dataset.key);
    const d = { key: b.dataset.key, timer: null, interval: null };
    if (b.dataset.key === "backspace") d.timer = setTimeout(() => { d.interval = setInterval(() => act("backspace"), REPEAT_EVERY_MS); }, REPEAT_AFTER_MS);
    down.set(e.pointerId, d);
  }
  function onUp(e) {
    const b = e.currentTarget;
    b.classList.remove("down");
    const d = down.get(e.pointerId);
    if (d) { stopRepeat(d); down.delete(e.pointerId); }
  }
  function aim(target) {
    if (target !== input) shift = "off";
    input = target;
    if (open) switchLayer(layer());
  }
  for (const target of [link, passcode]) {
    const other = target === link ? passcode : link;
    target.addEventListener("focus", () => { aim(target); if (kiosk) show(); });
    target.addEventListener("focusout", (e) => {
      if (down.size) return;   // a key is down: the person is typing
      if (e.relatedTarget && (box.contains(e.relatedTarget) || e.relatedTarget === toggle || e.relatedTarget === other)) return;
      hide();
    });
    for (const type of ["pointerdown", "click"]) target.addEventListener(type, () => { if (kiosk && !open) { aim(target); show(); } });
  }
  box.addEventListener("pointerdown", (e) => e.preventDefault());   // the tray's gaps: no focus change either
  toggle.addEventListener("pointerdown", (e) => e.preventDefault());
  toggle.addEventListener("click", () => { if (open) { hide(); return; } show(); field().focus(); });
  P.onViewChange((name) => { if (name !== "idle") hide(); });
  window.croomKeyboard = { open: show, hide: hide, isOpen: () => open, layer: layer, press: act, onPress: (fn) => listeners.push(fn) };
})();
```

(`touch-action: none` goes on `.keyboard` in CSS in this task too, replacing `manipulation` on the keys.)

- [ ] **Step 4: Run the keyboard tests** — `.venv/bin/pytest -q -p no:cacheprovider -W error tests/unit/control/test_page.py -k "keyboard or kiosk or thumbs or under_a_finger or lost_focus or backspace or shift"` — Expected: PASS. Then the whole file.

- [ ] **Step 5: Commit**

```bash
git add src/croom/control/static/keyboard.js src/croom/control/static/style.css tests/unit/control/test_page.py
git commit -m "feat(control): the on-screen keyboard acts the moment a finger lands, per finger, and never replaces a key under one"
```

---

### Task 2: Layout and visuals: three layers, key caps, the preview pop

> **Changed in review.** The CSS and the tests below are the plan as first written. What shipped differs as follows; the spec's 3.2 and 3.3, the keyboard block of `style.css` and `keyboard.js` say it as it is, and the ledger (`.superpowers/sdd/2026-10-13-on-screen-keyboard/progress.md`, kept out of git) has the review behind each change.
>
> - **Rows.** Every row is as wide as the first, ten keys at their widest: the cap is on the row (`max-width: calc(10 * 118px + 9 * 6px)`, centred), not on each key. The 118 px key cap is the letters layer's alone (`.keyboard[data-layer="letters"] .key`), so the middle row's nine letters sit half a key in from each end. The wide keys (`.wide`: shift, delete, the layer keys, hide) have no cap of their own, not 160 px, and flex to take what the rest of their row leaves, which is how the rows end together; the symbols layers hold no key to 118 px. Space and Join are as written below.
> - **Glyphs.** Shift, delete and hide are inline SVG (24 px, in the key's own colour) with accessible names ("Shift", "Delete", "Hide keyboard"), not text: for these three keys that overrides the Global Constraints' "the glyphs are text".
> - **The pop** is at least 80 px wide, not 56, and sits 2 px above its key; it also goes with its layer (a layer switched under a held letter takes it away).
> - **The shift lock's dot** is 0.9 rem and 1 px from the key's foot, not 0.6 rem at 6 px.
> - **The tests' helper** takes `release=False`, not `lift=False` (Task 1's tests and this task's below say `lift=False`).
> - **`window.croomKeyboard`** is `{open, hide, press, onPress, setClick}`, the tests' hold on the keyboard and nothing else's: Task 1's `isOpen()` and `layer()` went because nothing used them.

**Files:**
- Modify: `src/croom/control/static/style.css`, `src/croom/control/static/keyboard.js` (the preview)
- Test: `tests/unit/control/test_page.py`

- [ ] **Step 1: Failing tests**

```python
def test_the_three_layers_hold_the_ipad_keys_and_fit_the_kiosk(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        rows = page.evaluate("Array.from(document.querySelectorAll('#keyboard .row')).map(r => Array.from(r.querySelectorAll('.key')).map(k => k.dataset.key).join(' '))")
        assert rows[0] == "q w e r t y u i o p" and rows[3].startswith("layer:symbols . - / space join hide")
        touch(page, "#keyboard .key[data-key='layer:symbols']")
        rows = page.evaluate("Array.from(document.querySelectorAll('#keyboard .row')).map(r => Array.from(r.querySelectorAll('.key')).map(k => k.dataset.key).join(' '))")
        assert rows[0] == "1 2 3 4 5 6 7 8 9 0" and rows[1] == '- / : ; ( ) $ & @ "'
        touch(page, "#keyboard .key[data-key='layer:more']")
        rows = page.evaluate("Array.from(document.querySelectorAll('#keyboard .row')).map(r => Array.from(r.querySelectorAll('.key')).map(k => k.dataset.key).join(' '))")
        assert rows[0] == "[ ] { } # % ^ * + ="
        heights = page.evaluate("Array.from(document.querySelectorAll('#keyboard .key')).map(k => k.getBoundingClientRect().height)")
        assert min(heights) >= 50 and max(heights) <= 56
        assert page.evaluate("document.querySelector('#link-input').getBoundingClientRect().bottom <= document.querySelector('#keyboard').getBoundingClientRect().top")
        page.close()


def test_a_pressed_letter_shows_its_preview_and_a_wide_key_does_not(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        touch(page, "#keyboard .key[data-key='g']", pointer_id=1, lift=False)
        assert page.locator("#key-preview").is_visible() and page.locator("#key-preview").inner_text() == "g"
        lift(page, "#keyboard .key[data-key='g']", pointer_id=1)
        page.wait_for_function("document.getElementById('key-preview').hidden", timeout=2000)
        touch(page, "#keyboard .key[data-key='shift']", pointer_id=1, lift=False)
        assert page.evaluate("document.getElementById('key-preview').hidden")
        lift(page, "#keyboard .key[data-key='shift']", pointer_id=1)
        page.close()
```

- [ ] **Step 2: Run to see them fail** — Expected: FAIL.

- [ ] **Step 3: Implement**

CSS (replace the keyboard block):

```css
.keyboard {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 50;
  padding: 8px 6px calc(8px + env(safe-area-inset-bottom, 0px));
  background: #0B1220; display: grid; gap: 6px; touch-action: none; user-select: none;
}
.keyboard[hidden] { display: none; }
.keyboard .row { display: flex; gap: 6px; justify-content: center; }
.keyboard .key {
  flex: 1 1 0; min-width: 0; max-width: 118px; height: 52px; padding: 0;
  border: 0; border-radius: 8px; background: var(--surface-800); color: #fff;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08), 0 1px 0 rgba(0, 0, 0, 0.5);
  font: inherit; font-size: 1.15rem; font-weight: 500; cursor: pointer; position: relative;
}
.keyboard .key .cap { pointer-events: none; }
.keyboard .key.wide { flex: 1.5 1 0; max-width: 160px; font-size: 1rem; background: #172033; }
.keyboard .key.space { flex: 5 1 0; max-width: none; font-size: 0.95rem; }
.keyboard .key.join { flex: 1.8 1 0; max-width: 180px; background: var(--blue-600); font-weight: 600; }
.keyboard .key.down { background: var(--navy-800); box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.6); transform: translateY(1px); }
.keyboard[data-shift="once"] .key[data-key="shift"], .keyboard[data-shift="lock"] .key[data-key="shift"] { background: #fff; color: var(--navy-900); }
.keyboard[data-shift="lock"] .key[data-key="shift"] .cap::after { content: "\2022"; position: absolute; bottom: 6px; left: 50%; transform: translateX(-50%); font-size: 0.6rem; }
#key-preview {
  position: fixed; z-index: 60; min-width: 56px; height: 64px; padding: 0 12px; border-radius: 10px;
  background: var(--navy-800); color: #fff; font-size: 1.6rem; font-weight: 600; display: flex; align-items: center; justify-content: center;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.45); pointer-events: none;
}
#key-preview[hidden] { display: none; }
body.keyboard-open .screen { padding-bottom: 290px; }
body.keyboard-open { height: auto; min-height: 100vh; }
body.keyboard-open .schedule { display: none; }
@media (max-width: 480px) { .keyboard .key { height: 44px; font-size: 1rem; } }
```

`keyboard.js`: a `#key-preview` div created once (appended to `body`, `hidden`); in `onDown` for keys that are a single character (not in `WIDE`, not `space`/`join`), place the preview centred above the key (`getBoundingClientRect`), text = the label, `hidden = false`; in `onUp`, hide it after 60 ms. Keep it hidden for wide keys.

- [ ] **Step 4: Run** the keyboard tests and `-k "fits or kiosk"` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/croom/control/static/style.css src/croom/control/static/keyboard.js tests/unit/control/test_page.py
git commit -m "feat(control): the keyboard's three layers, key caps and the preview pop above a pressed letter"
```

---

### Task 3: The key click and its setting

**Files:**
- Modify: `src/croom/control/service.py` (`GET/POST /api/settings`), `src/croom/control/static/keyboard.js` (the click), `app.js` (the Options card's wiring), `index.html` (Options › Keyboard card)
- Test: `tests/unit/control/test_service.py`, `tests/unit/control/test_page.py`

**Interfaces:**
- Produces: `GET /api/settings` → `{"key_click": true}`; `POST /api/settings {"key_click": false}` → the same shape, stored in the control service's settings file beside the screensaver style (find how `_load_style`/the screensaver POST write that file and reuse the helper); `/api/status` carries `settings: {"key_click": …}` so the page needs no extra poll; `window.croomKeyboard.setClick(bool)`.

- [ ] **Step 1: Failing tests**

```python
    async def test_key_click_setting_round_trips_and_shows_in_the_status(self, client_factory):   # test_service.py
        client = await client_factory(make_service())
        assert await (await client.get("/api/settings")).json() == {"key_click": True}
        assert (await client.post("/api/settings", json={"key_click": False})).status == 200
        assert (await (await client.get("/api/status")).json())["settings"] == {"key_click": False}
        assert (await client.post("/api/settings", json={"key_click": "no"})).status == 400
```

```python
def test_the_key_click_plays_once_per_key_and_the_options_card_turns_it_off(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.add_init_script("""window.__ticks = 0; window.AudioContext = class { constructor() { this.currentTime = 0; this.destination = {}; }
            createOscillator() { return { connect() {}, start() { window.__ticks += 1; }, stop() {}, frequency: { value: 0 } }; }
            createGain() { return { connect() {}, gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} } }; } resume() { return Promise.resolve(); } };""")
        page.reload(wait_until="networkidle")
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        tap(page, "a", "b", "c")
        assert page.evaluate("window.__ticks") == 3
        page.click("#keyboard-toggle")                              # hide, then Options › Keyboard
        page.click("#open-options")
        page.locator("#key-click-off").click()
        page.wait_for_function("window.croomPage.model.status.settings.key_click === false", timeout=5000)
        page.click("#back-from-options")
        page.click("#link-input")
        tap(page, "d")
        assert page.evaluate("window.__ticks") == 3
        page.close()


def test_the_click_fails_quietly_without_audio(browser):
    with PageServer() as server:
        page = open_kiosk_page(browser, server)
        page.add_init_script("window.AudioContext = class { constructor() { throw new Error('no audio'); } }; window.webkitAudioContext = undefined;")
        page.reload(wait_until="networkidle")
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.click("#link-input")
        page.wait_for_function("!document.getElementById('keyboard').hidden", timeout=5000)
        tap(page, "a", "b")
        assert page.input_value("#link-input") == "ab" and errors == []
        page.close()
```

- [ ] **Step 2: Run to see them fail** — Expected: FAIL.

- [ ] **Step 3: Implement**

Service: a `_settings()` dict read from the settings file (`{"key_click": True}` default), `_handle_settings_get`, `_handle_settings_post` (validates `key_click` is a bool, writes the file the way the screensaver style is written), and `"settings": self._settings()` in `_status()`. Routes `app.router.add_get("/api/settings", …)`, `add_post`.

Keyboard click:

```js
  let audio = null, clickOn = true;
  function tick() {
    if (!clickOn) return;
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      audio = audio || new Ctx();
      if (audio.state === "suspended" && audio.resume) audio.resume().catch(() => {});
      const osc = audio.createOscillator(), gain = audio.createGain();
      osc.frequency.value = 1200;
      gain.gain.setValueAtTime(0.08, audio.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + 0.012);
      osc.connect(gain); gain.connect(audio.destination);
      osc.start(); osc.stop(audio.currentTime + 0.012);
      if (navigator.vibrate) navigator.vibrate(8);
    } catch (e) { /* no audio here: typing goes on */ }
  }
```

called at the top of `onDown`; `window.croomKeyboard.setClick = (on) => { clickOn = Boolean(on); }`; `P.onRender((s) => { if (s.settings) clickOn = s.settings.key_click !== false; })`.

Options card in `index.html` (inside the options view, after the Screensaver menu item):

```html
    <section class="screen-section">
      <p class="kicker">Keyboard</p>
      <div class="picker" role="group" aria-label="Key click">
        <button type="button" id="key-click-on" class="button quiet" aria-pressed="true">Key click on</button>
        <button type="button" id="key-click-off" class="button quiet" aria-pressed="false">Key click off</button>
      </div>
    </section>
```

`app.js`: the two buttons post `/api/settings {key_click}` through `act`, and a render hook sets `aria-pressed` from `s.settings.key_click`.

- [ ] **Step 4: Run** `tests/unit/control/test_service.py tests/unit/control/test_page.py -k "key_click or fails_quietly or settings or options"` and then the whole page file — Expected: PASS, `fits()` on the options screen.

- [ ] **Step 5: Commit**

```bash
git add src/croom/control/service.py src/croom/control/static/keyboard.js src/croom/control/static/app.js src/croom/control/static/index.html tests/unit/control/test_service.py tests/unit/control/test_page.py
git commit -m "feat(control): a key click on the on-screen keyboard, off under Options > Keyboard, kept in the room's settings"
```

---

### Task 4: Docs

**Files:**
- Modify: `README.md` (the room page row mentions the keyboard; the features table gets the spec and plan row), `docs/guides/crystal-meet-room-setup/index.html` (+ rebuilt PDF: the keyboard paragraph: layers, Shift lock, the key click and its switch)
- Test: `tests/unit/docs`

- [ ] **Step 1:** add needles to `tests/unit/docs/test_readme.py`: `"docs/superpowers/specs/2026-10-13-on-screen-keyboard-design.md"`, `"Key click"`; run: FAIL. **Step 2:** write the docs, rebuild the PDF (`.venv/bin/python docs/guides/crystal-meet-room-setup/build.py`), run `tests/unit/docs`: PASS. **Step 3:** commit `docs: the on-screen keyboard's layers, Shift lock and key click in the room guide and README`.

---

## Self-review

- Spec coverage: 3.1 → Task 1; 3.2 and 3.4 → Task 2; 3.3 → Task 3; section 4 files → Tasks 1–4; section 5 tests → the named tests. The Vibration API call is in Task 3's `tick()`.
- Placeholders: none. Type consistency: `window.croomKeyboard` names are used consistently; `touch()`/`lift()` helpers defined once in Task 1.
- Review Focus: each line names its test.
