// The page's own keyboard, for screens without one (spec 2026-10-13 on-screen keyboard). Keys act the moment a
// finger lands, per finger, so two thumbs typing fast lose nothing; rows are built once per layer and their labels
// change in place, so nothing under a finger is ever replaced; the keys never take the focus, and hide only for a tap
// elsewhere, Hide, Join or a view change. The kiosk (?keyboard=1) opens it when a field is tapped; the button beside
// the link field toggles it on any device. Each key a finger lands on can click (spec 2026-10-13, 3.3), as the room's
// setting says (Options > Keyboard).
(function () {
  "use strict";
  const P = window.croomPage;
  const el = P.el;
  const LAYERS = {
    letters: [["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"], ["a", "s", "d", "f", "g", "h", "j", "k", "l"],
              ["shift", "z", "x", "c", "v", "b", "n", "m", "backspace"], ["layer:symbols", ".", "-", "/", "space", "join", "hide"]],
    symbols: [["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"], ["-", "/", ":", ";", "(", ")", "$", "&", "@", "\""],
              ["layer:more", ".", ",", "?", "!", "'", "backspace"], ["layer:letters", "space", "join", "hide"]],
    more: [["[", "]", "{", "}", "#", "%", "^", "*", "+", "="], ["_", "\\", "|", "~", "<", ">", "\u20AC", "\u00A3", "\u00A5", "\u2022"],
           ["layer:symbols", ".", ",", "?", "!", "'", "backspace"], ["layer:letters", "space", "join", "hide"]],
  };
  // The euro, pound, yen and bullet above are escapes: the page's scripts are plain ASCII.
  const LABELS = { space: "space", join: "Join", "layer:symbols": "123", "layer:more": "#+=", "layer:letters": "ABC" };
  // The three keys that are a picture, not a word, are drawn (inline SVG, in the key's own colour) so that no font has to
  // supply them; their buttons carry their names, for a screen reader.
  const icon = (paths) => '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + paths + "</svg>";
  const ICONS = {
    shift: icon('<path d="M12 16V4"/><path d="M6 10l6-6 6 6"/><path d="M6 20h12"/>'),                    // an arrow up, over a flat base
    backspace: icon('<path d="M9 5h11a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-7-7z"/><path d="M13 9l6 6M19 9l-6 6"/>'),   // a key pointing left, with an x
    hide: icon('<path d="M6 9l6 6 6-6"/>'),                                                                // a chevron down
  };
  const NAMES = { shift: "Shift", backspace: "Delete", hide: "Hide keyboard" };
  const WIDE = new Set(["shift", "backspace", "layer:symbols", "layer:more", "layer:letters", "hide"]);
  const REPEAT_AFTER_MS = 400, REPEAT_EVERY_MS = 83, DOUBLE_TAP_MS = 350, PREVIEW_LINGER_MS = 60;
  // The click: a soft tick, a 1.2 kHz sine that fades out in 4 ms (heard for about 3) at a low level, made by the browser's audio
  // (no file), and a short buzz where the device has a motor.
  const CLICK_HZ = 1200, CLICK_MS = 4, CLICK_LEVEL = 0.15, BUZZ_MS = 8;
  const kiosk = P.kiosk;
  const box = el("keyboard");
  const link = el("link-input");
  const passcode = el("passcode-input");
  const toggle = el("keyboard-toggle");
  const shelf = document.createElement("div");   // where the layers not showing wait: in the page, out of the tray
  shelf.hidden = true;
  box.after(shelf);
  let input = link;
  let open = false;
  let shift = "off";                 // off | once | lock
  let lastShiftAt = 0;
  const layers = new Map([[link, "letters"], [passcode, "symbols"]]);
  const built = new Map();           // layer name -> its rows, built once
  const down = new Map();            // pointerId -> { button, timer, interval }
  let listeners = [];                // what tests may hook (press)
  const preview = document.createElement("div");   // the pop above a pressed key: made once, moved from key to key, never rebuilt
  preview.id = "key-preview";
  preview.setAttribute("aria-hidden", "true");     // it repeats the key, which is already read
  preview.hidden = true;
  document.body.append(preview);
  let previewOf = null;              // the pointer whose key the pop shows
  let previewTimer = null;
  let clickOn = true;                // the room's setting (the page hands it over with every status); on until it says otherwise
  let audio = null;                  // made by the first key, not before: a tap has led to a key, and a browser starts audio made earlier silent

  const field = () => (input.hidden ? link : input);
  const inTray = (t) => Boolean(t && t.closest && t.closest(".key, .keyboard"));   // a key may be on the shelf: its row is not under the tray then
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
    if (ICONS[key]) {
      cap.innerHTML = ICONS[key];
      b.setAttribute("aria-label", NAMES[key]);
    } else cap.textContent = label(key);
    b.append(cap);
    b.addEventListener("pointerdown", onDown);
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
  // The tray holds the rows of one layer. The others are moved to the shelf, never rebuilt or dropped from the page: a key
  // a finger is on stays the same node, still in the document, for the finger to lift from.
  function place(name) {
    const rows = built.get(name) || build(name);
    for (const [other, rowsOf] of built) if (other !== name) for (const r of rowsOf) if (r.parentNode !== shelf) shelf.append(r);
    for (const r of rows) if (r.parentNode !== box) box.append(r);
  }
  function relabel() {
    for (const b of box.querySelectorAll(".key")) if (!ICONS[b.dataset.key]) b.querySelector(".cap").textContent = label(b.dataset.key);
    box.dataset.shift = shift;
    box.dataset.layer = layer();
  }
  // A touchscreen follows a tap with a click at the finger's spot once it lifts, and by then Hide or Join has taken the tray
  // away, or a layer key has taken its own row to the shelf: the click would land on whatever is beneath the finger now.
  // Cancelling the touch leaves no click to land (the pointer events, which do the typing, are not touch events and still
  // come). It is the document that listens: a layer key's pointerdown has moved its row off the tray before the touchstart
  // that follows it, and a listener on the tray would not hear it. And only while the keys are up (show(), hide()): a
  // listener that may cancel makes the browser wait for it before it scrolls on any touch, anywhere on the page.
  const cancelTrayTouch = (e) => { if (inTray(e.target)) e.preventDefault(); };
  function trayTouches(on) {
    if (on) document.addEventListener("touchstart", cancelTrayTouch, { capture: true, passive: false });
    else document.removeEventListener("touchstart", cancelTrayTouch, true);
  }
  function show() {
    if (P.view() !== "idle" || open) return;
    open = true;
    trayTouches(true);
    place(layer());
    relabel();
    box.hidden = false;
    document.body.classList.add("keyboard-open");
    toggle.setAttribute("aria-pressed", "true");
    field().scrollIntoView({ block: "center" });
  }
  function hide() {
    if (!open) return;
    open = false;
    letGoAll();
    box.hidden = true;
    document.body.classList.remove("keyboard-open");
    toggle.setAttribute("aria-pressed", "false");
    // Not at once: the touch that took the keys away (a tap on Hide or Join) has its touchstart still to come, and the
    // listener has to cancel it, or the tap's click lands on whatever is beneath. A tick is time enough; keys that are back
    // by then keep the listener.
    setTimeout(() => { if (!open) trayTouches(false); }, 0);
  }
  function switchLayer(name) {
    layers.set(field(), name);
    shift = "off";
    // A held letter's pop would stay over keys that are not its: it goes with its layer, and the lift that comes later finds nothing to do.
    // Only when the layer changes: a press that gives the focus back to the field comes here too, with the layer it has.
    if (box.dataset.layer !== name) hidePreview();
    if (!open) return;
    place(name);
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
    perform(key);
    for (const fn of listeners) {   // after the key: a hook that fails (the click, the buzz) costs the person nothing
      try { fn(key); } catch (err) { console.error("croomKeyboard: a press hook failed", err); }
    }
  }
  function perform(key) {
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
  function letGo(d) { stopRepeat(d); d.button.classList.remove("down"); }
  function letGoAll() { for (const d of down.values()) letGo(d); down.clear(); hidePreview(); }
  // The pop is for letters and symbols, not for the wide keys, Space or Join. It shows the key as the finger found it (a
  // capital under Shift: the press spends the Shift and relabels the keys), centred above the key and kept on the screen,
  // follows the latest finger, and stays 60 ms after that finger lifts.
  const popsUp = (key) => !WIDE.has(key) && key !== "space" && key !== "join";
  function showPreview(button, pointerId) {
    clearTimeout(previewTimer);
    previewOf = pointerId;
    preview.textContent = button.querySelector(".cap").textContent;
    preview.hidden = false;
    const r = button.getBoundingClientRect();
    const left = r.left + r.width / 2 - preview.offsetWidth / 2;
    preview.style.left = Math.max(4, Math.min(left, document.documentElement.clientWidth - preview.offsetWidth - 4)) + "px";
    preview.style.top = r.top - preview.offsetHeight - 2 + "px";
  }
  function hidePreview() { clearTimeout(previewTimer); previewOf = null; preview.hidden = true; }
  function lingerPreview(pointerId) { if (previewOf === pointerId) previewTimer = setTimeout(hidePreview, PREVIEW_LINGER_MS); }
  // One tick for a finger landing on a key. It never throws: with no audio, or audio that will not play, typing goes on in silence.
  function sound() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const fresh = !audio;
    audio = audio || new Ctx();
    if (audio.state && audio.state !== "running") {   // held back by the browser until a tap has led to it, or interrupted: ask it to start
      const asked = audio.resume && audio.resume();
      if (asked && asked.catch) asked.catch(() => {});
      if (!fresh) return;   // and queue nothing behind it: every tick waiting would sound at once the moment it started (this key's own may follow it)
    }
    const at = audio.currentTime, osc = audio.createOscillator(), gain = audio.createGain();
    osc.type = "sine";
    osc.frequency.value = CLICK_HZ;
    gain.gain.setValueAtTime(CLICK_LEVEL, at);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + CLICK_MS / 1000);
    osc.connect(gain);
    gain.connect(audio.destination);
    osc.start(at);
    osc.stop(at + CLICK_MS / 1000 + 0.001);
  }
  function tick() {
    if (!clickOn) return;
    try { sound(); } catch (err) { /* no audio here */ }
    try { if (navigator.vibrate) navigator.vibrate(BUZZ_MS); } catch (err) { /* no motor */ }
  }
  function onDown(e) {
    if (e.pointerType === "mouse" && e.button !== 0) return;   // only the main button types; the tray's own handler keeps the focus for the others
    e.preventDefault();   // the field keeps the focus
    tick();               // every key a finger lands on, whatever the key does; not a held key's repeat, nor a press made from code
    onUp(e);              // a press of this pointer that never lifted is over: its repeat must not outlive it
    const b = e.currentTarget;
    try { b.setPointerCapture(e.pointerId); } catch (err) { /* synthetic events have no capture */ }
    b.classList.add("down");
    const d = { button: b, timer: null, interval: null };
    down.set(e.pointerId, d);   // before the key acts: Hide and Join let every press go, this one with them
    if (popsUp(b.dataset.key)) showPreview(b, e.pointerId);   // before it too: a press may change the labels
    if (b.dataset.key === "backspace") d.timer = setTimeout(() => { d.interval = setInterval(() => act("backspace"), REPEAT_EVERY_MS); }, REPEAT_AFTER_MS);
    act(b.dataset.key);
  }
  // A finger lifting ends its press wherever it lifts, on the key it pressed or not (a layer may have changed under it, or
  // it slid off), so the listener is the document's: the pressed key is let go and a held key stops repeating.
  function onUp(e) {
    const d = down.get(e.pointerId);
    if (!d) return;
    letGo(d);
    down.delete(e.pointerId);
    lingerPreview(e.pointerId);
  }
  document.addEventListener("pointerup", onUp, true);
  document.addEventListener("pointercancel", onUp, true);
  window.addEventListener("blur", letGoAll);   // no lift comes to a window nobody is looking at
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
  document.addEventListener("contextmenu", (e) => { if (inTray(e.target)) e.preventDefault(); }, true);   // a long press is no context menu on the keys either
  toggle.addEventListener("pointerdown", (e) => e.preventDefault());
  toggle.addEventListener("click", () => { if (open) { hide(); return; } show(); field().focus(); });
  P.onViewChange((name) => { if (name !== "idle") hide(); });
  const setClick = (on) => { clickOn = Boolean(on); };
  P.onRender((s) => { if (s.settings) setClick(s.settings.key_click !== false); });   // the room's setting, with every status
  window.croomKeyboard = { open: show, hide: hide, isOpen: () => open, layer: layer, press: act, onPress: (fn) => listeners.push(fn), setClick: setClick };
})();
