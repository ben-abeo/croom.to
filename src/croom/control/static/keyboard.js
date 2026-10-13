// The page's own keyboard, for screens without one (added with the table Pi's kiosk installer, 2026-10-08): the table
// Pi's kiosk browser opens the page with ?keyboard=1 and the keyboard appears when the link field or the passcode field is
// tapped; the button beside the link field toggles it on any device. Keys never take the focus from the field, and edit
// whichever of the two had it last; each field keeps its own layer, the passcode's opening on the digits. The fields are
// on the idle screen only, so the keyboard opens there only and hides when the view changes (spec 2026-10-12, section 4.1).
(function () {
  "use strict";
  const P = window.croomPage;
  const el = P.el;

  const LETTERS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"];
  const SYMBOLS = ["1234567890", "-/:.?=&_%#", "@~+,;!*()'"];
  const kiosk = P.kiosk;
  const box = el("keyboard");
  const link = el("link-input");
  const passcode = el("passcode-input");
  const toggle = el("keyboard-toggle");
  let input = link;   // the field the keys edit: whichever of the two had the focus last
  let shift = false, open = false;
  // Each field's layer, kept while the person moves between them: a passcode is mostly digits, a link mostly letters.
  const layers = new Map([[link, "letters"], [passcode, "symbols"]]);

  // The passcode field hides with the link it was for, and may be the one that had the focus last: the keys go to the link box then.
  const field = () => (input.hidden ? link : input);
  const layer = () => layers.get(field());

  function keyButton(key, label, className) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "key" + (className ? " " + className : "");
    b.dataset.key = key;
    b.textContent = label;
    b.addEventListener("pointerdown", (e) => e.preventDefault());
    b.addEventListener("click", () => press(key));
    return b;
  }

  function row(...keys) {
    const r = document.createElement("div");
    r.className = "row";
    r.append(...keys);
    return r;
  }

  function chars(text) {
    return [...text].map((c) => keyButton(c, layer() === "letters" && shift ? c.toUpperCase() : c));
  }

  function render() {
    const rows = [];
    if (layer() === "letters") {
      rows.push(row(...chars(LETTERS[0])));
      rows.push(row(...chars(LETTERS[1])));
      rows.push(row(keyButton("shift", "Shift", "wide" + (shift ? " active" : "")), ...chars(LETTERS[2]), keyButton("backspace", "\u232B", "wide")));
      rows.push(row(keyButton("symbols", "123", "wide"), ...chars(".-/"), keyButton("space", "Space", "space"),
                    keyButton("join", "Join", "join"), keyButton("hide", "Hide", "wide")));
    } else {
      rows.push(row(...chars(SYMBOLS[0])));
      rows.push(row(...chars(SYMBOLS[1])));
      rows.push(row(...chars(SYMBOLS[2]), keyButton("backspace", "\u232B", "wide")));
      rows.push(row(keyButton("letters", "abc", "wide"), keyButton("space", "Space", "space"),
                    keyButton("join", "Join", "join"), keyButton("hide", "Hide", "wide")));
    }
    box.replaceChildren(...rows);
  }

  function edit(text, backspace) {
    const target = field();
    const start = target.selectionStart === null ? target.value.length : target.selectionStart;
    const end = target.selectionEnd === null ? start : target.selectionEnd;
    if (backspace) {
      target.setRangeText("", start === end ? Math.max(0, start - 1) : start, end, "end");
    } else {
      target.setRangeText(text, start, end, "end");
    }
    target.dispatchEvent(new Event("input", { bubbles: true }));
    target.focus();
  }

  function press(key) {
    if (key === "shift") { shift = !shift; render(); return; }
    if (key === "symbols") { layers.set(field(), "symbols"); shift = false; render(); return; }
    if (key === "letters") { layers.set(field(), "letters"); render(); return; }
    if (key === "hide") { hide(); return; }
    if (key === "join") { hide(); el("link-form").requestSubmit(); return; }
    if (key === "backspace") { edit("", true); return; }
    if (key === "space") { edit(" "); return; }
    edit(shift ? key.toUpperCase() : key);
    if (shift) { shift = false; render(); }
  }

  function show() {
    if (P.view() !== "idle") return;   // nothing to type into on any other screen
    if (open) return;
    open = true;
    render();
    box.hidden = false;
    document.body.classList.add("keyboard-open");
    toggle.setAttribute("aria-pressed", "true");
    field().scrollIntoView({ block: "center" });
  }

  function hide() {
    if (!open) return;
    open = false;
    box.hidden = true;
    document.body.classList.remove("keyboard-open");
    toggle.setAttribute("aria-pressed", "false");
  }

  // The keys edit this field from now on, on its own layer, even when the keyboard is already up for the other one.
  function aim(target) {
    if (target !== input) shift = false;
    input = target;
    if (open) render();
  }

  for (const target of [link, passcode]) {
    const other = target === link ? passcode : link;
    target.addEventListener("focus", () => { aim(target); if (kiosk) show(); });
    target.addEventListener("focusout", (e) => {
      // To the keys, their button or the other field, the person is still typing: the keys stay up.
      if (e.relatedTarget && (box.contains(e.relatedTarget) || e.relatedTarget === toggle || e.relatedTarget === other)) return;
      hide();
    });
    // A tap on a field that still has the focus fires no focus event (after a Join the agent refused, or Hide): on the kiosk,
    // which has no keys of its own, it brings the page's back.
    for (const type of ["pointerdown", "click"]) {
      target.addEventListener(type, () => { if (kiosk && !open) { aim(target); show(); } });
    }
  }
  toggle.addEventListener("pointerdown", (e) => e.preventDefault());
  toggle.addEventListener("click", () => {
    if (open) { hide(); return; }
    show();
    field().focus();
  });
  P.onViewChange((name) => { if (name !== "idle") hide(); });
})();
