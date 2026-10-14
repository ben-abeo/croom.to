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
    more: [["[", "]", "{", "}", "#", "%", "^", "*", "+", "="], ["_", "\\", "|", "~", "<", ">", "\u20AC", "\u00A3", "\u00A5", "\u2022"],
           ["layer:symbols", ".", ",", "?", "!", "'", "backspace"], ["layer:letters", "space", "join", "hide"]],
  };
  // The glyphs (shift, backspace and hide here; euro, pound, yen and bullet above) are escapes: the page's scripts are plain ASCII.
  const LABELS = { shift: "\u21E7", backspace: "\u232B", space: "space", join: "Join", hide: "\u2304", "layer:symbols": "123", "layer:more": "#+=", "layer:letters": "ABC" };
  const WIDE = new Set(["shift", "backspace", "layer:symbols", "layer:more", "layer:letters", "hide"]);
  const REPEAT_AFTER_MS = 400, REPEAT_EVERY_MS = 83, DOUBLE_TAP_MS = 350;
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
    for (const b of box.querySelectorAll(".key")) b.querySelector(".cap").textContent = label(b.dataset.key);
    box.dataset.shift = shift;
    box.dataset.layer = layer();
  }
  function show() {
    if (P.view() !== "idle" || open) return;
    open = true;
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
    for (const d of down.values()) { stopRepeat(d); d.button.classList.remove("down"); }
    down.clear();
    box.hidden = true;
    document.body.classList.remove("keyboard-open");
    toggle.setAttribute("aria-pressed", "false");
  }
  function switchLayer(name) {
    layers.set(field(), name);
    shift = "off";
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
    const d = { button: b, timer: null, interval: null };
    if (b.dataset.key === "backspace") d.timer = setTimeout(() => { d.interval = setInterval(() => act("backspace"), REPEAT_EVERY_MS); }, REPEAT_AFTER_MS);
    down.set(e.pointerId, d);
  }
  // A finger lifting ends its press wherever it lifts, on the key it pressed or not (a layer may have changed under it, or
  // it slid off), so the listener is the document's: the pressed key is let go and a held key stops repeating.
  function onUp(e) {
    const d = down.get(e.pointerId);
    if (!d) return;
    d.button.classList.remove("down");
    stopRepeat(d);
    down.delete(e.pointerId);
  }
  document.addEventListener("pointerup", onUp, true);
  document.addEventListener("pointercancel", onUp, true);
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
  // A touchscreen follows a tap with a click at the finger's spot once it lifts, and by then Hide or Join has taken the tray
  // away: the click would land on whatever the page has beneath. Cancelling the touch leaves no click to land (the pointer
  // events, which do the typing, are not touch events and still come).
  box.addEventListener("touchstart", (e) => e.preventDefault(), { passive: false });
  toggle.addEventListener("pointerdown", (e) => e.preventDefault());
  toggle.addEventListener("click", () => { if (open) { hide(); return; } show(); field().focus(); });
  P.onViewChange((name) => { if (name !== "idle") hide(); });
  window.croomKeyboard = { open: show, hide: hide, isOpen: () => open, layer: layer, press: act, onPress: (fn) => listeners.push(fn) };
})();
