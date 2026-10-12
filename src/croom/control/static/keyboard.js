// The page's own keyboard, for screens without one (added with the table Pi's kiosk installer, 2026-10-08): the table
// Pi's kiosk browser opens the page with ?keyboard=1 and the keyboard appears when the link field is tapped; the button
// beside the field toggles it on any device. Keys never take the focus from the field. The link field is on the idle
// screen only, so the keyboard opens there only and hides when the view changes (spec 2026-10-12, section 4.1).
(function () {
  "use strict";
  const P = window.croomPage;
  const el = P.el;

  const LETTERS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"];
  const SYMBOLS = ["1234567890", "-/:.?=&_%#", "@~+,;!*()'"];
  const kiosk = new URLSearchParams(window.location.search).get("keyboard") === "1";
  const box = el("keyboard");
  const input = el("link-input");
  const toggle = el("keyboard-toggle");
  let layer = "letters", shift = false, open = false;

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
    return [...text].map((c) => keyButton(c, layer === "letters" && shift ? c.toUpperCase() : c));
  }

  function render() {
    const rows = [];
    if (layer === "letters") {
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
    const start = input.selectionStart === null ? input.value.length : input.selectionStart;
    const end = input.selectionEnd === null ? start : input.selectionEnd;
    if (backspace) {
      input.setRangeText("", start === end ? Math.max(0, start - 1) : start, end, "end");
    } else {
      input.setRangeText(text, start, end, "end");
    }
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
  }

  function press(key) {
    if (key === "shift") { shift = !shift; render(); return; }
    if (key === "symbols") { layer = "symbols"; shift = false; render(); return; }
    if (key === "letters") { layer = "letters"; render(); return; }
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
    input.scrollIntoView({ block: "center" });
  }

  function hide() {
    if (!open) return;
    open = false;
    box.hidden = true;
    document.body.classList.remove("keyboard-open");
    toggle.setAttribute("aria-pressed", "false");
  }

  input.addEventListener("focus", () => { if (kiosk) show(); });
  input.addEventListener("focusout", (e) => {
    if (e.relatedTarget && (box.contains(e.relatedTarget) || e.relatedTarget === toggle)) return;
    hide();
  });
  toggle.addEventListener("pointerdown", (e) => e.preventDefault());
  toggle.addEventListener("click", () => {
    if (open) { hide(); return; }
    show();
    input.focus();
  });
  P.onViewChange((name) => { if (name !== "idle") hide(); });
})();
