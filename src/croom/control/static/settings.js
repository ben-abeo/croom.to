// Settings (spec 2026-10-29, section 3): Options > Settings asks for the PIN on a pad of its own (the first time, to choose one and
// enter it again), then shows the menu of Settings pages, with Lock and the minutes left in its top bar.
//
// The PIN gives a token, kept in this tab's sessionStorage and sent with every Settings request. The agent keeps it for 5 minutes
// after the last request that used it; the page counts the same 5 minutes from its own last request and asks for the PIN again when
// they run out, as it does when the agent refuses the token (401). Lock ends it, and so does leaving Settings for the idle or the
// meeting screen: the room's screen is shared, and the next person to walk up must not find Settings open.
//
// The pages are listed in PAGES, the one place they are named. A page that is built has a draw(host) that fills the page's box and may
// give back a function that tidies up when the page is left; the others say "Coming soon". Later pages may live in files of their own
// and hand their draw over with croomSettings.define(key, draw). Their requests go through croomSettings.api and .post, which carry
// the token and go back to the PIN pad when it is refused.
(function () {
  "use strict";
  const P = window.croomPage;
  const el = P.el;

  // The agent's unlock header, written in lower case: header names are not case sensitive, and the page's files never spell the
  // agent's own name with its capital (the service's test of the page's files looks for it).
  const UNLOCK_HEADER = "x-croom-unlock";
  const TOKEN_KEY = "settings-unlock";
  const UNLOCK_MS = 5 * 60 * 1000;
  const MAX_DIGITS = 8, MIN_DIGITS = 4;
  const AREA = ["settings-pin", "settings", "tv", "options"];   // the views Settings stays unlocked in (its TV page is Options > TV)
  const LOCKED_AGAIN = "Settings locked: enter the PIN again";
  const NO_ROOM = "Can't reach the room";
  const lockedWords = (minutes) => "Too many wrong PINs: try again in " + minutes + " minute" + (minutes === 1 ? "" : "s");

  // The pages of spec section 3, in the menu's order.
  const PAGES = [
    { key: "room", label: "Room", icon: "home" },
    { key: "calendar", label: "Calendar", icon: "calendar" },
    { key: "meetings", label: "Meetings", icon: "video" },
    { key: "devices", label: "Devices", icon: "mic" },
    { key: "tv", label: "TV", icon: "tv", view: "tv" },          // Options > TV, as it is: its Back comes back here
    { key: "laptop", label: "Laptop switch", icon: "laptop" },
    { key: "ai", label: "AI & dashboard", icon: "layout" },
    { key: "network", label: "Network", icon: "wifi" },
    { key: "check", label: "Check", icon: "check" },
    { key: "logs", label: "Logs", icon: "list" },
    { key: "software", label: "Software", icon: "refresh-cw" },
    { key: "security", label: "Security", icon: "lock", draw: drawSecurity },
  ];

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // --- The token. sessionStorage keeps it across a reload of this tab and no further; a browser that refuses storage keeps it in
  // memory instead. `deadline` is when the agent will have let it go, counted from this page's last request that used it.
  let memoryToken = null;
  let deadline = 0;
  function heldToken() {
    try { return window.sessionStorage.getItem(TOKEN_KEY) || memoryToken; } catch (e) { return memoryToken; }
  }
  function keepToken(token) {
    memoryToken = token;
    try { window.sessionStorage.setItem(TOKEN_KEY, token); } catch (e) { /* kept in memory */ }
    used();
  }
  function forgetToken() {
    memoryToken = null;
    try { window.sessionStorage.removeItem(TOKEN_KEY); } catch (e) { /* nothing kept there */ }
    el("settings-time").textContent = "";
  }
  function used() {
    deadline = Date.now() + UNLOCK_MS;
    showTimeLeft();
  }
  // End the token at the agent as well as here: a token the page has let go of must not stay usable for the rest of its 5 minutes.
  function endToken() {
    const token = heldToken();
    forgetToken();
    if (token) sendLock(token);
  }
  function sendLock(token) {
    const headers = { "Content-Type": "application/json" };
    headers[UNLOCK_HEADER] = token;
    fetch("/api/settings/lock", { method: "POST", headers: headers, body: "{}" }).catch(() => {});   // gone already, or unreachable: nothing to do
  }

  // A request with the token, answered with its status and body whatever the status (the PIN's own routes read their refusals).
  async function call(path, options) {
    const headers = { "Content-Type": "application/json" };
    const token = heldToken();
    if (token) headers[UNLOCK_HEADER] = token;
    const response = await fetch(path, Object.assign({}, options || {}, { headers: headers }));
    let data = {};
    try { data = await response.json(); } catch (e) { data = {}; }
    return { status: response.status, ok: response.ok, data: data };
  }

  // What the Settings pages ask the agent through: the body of the answer, or an Error in the agent's words, with the answer's `status`
  // and `data`. A token that is refused (401, or 423 while unlocking is locked out) takes the page back to the PIN pad, and the Error
  // says so with `locked`, for the page to say nothing more.
  async function api(path, options) {
    const answer = await call(path, options);
    if (answer.status === 401 || answer.status === 423) {
      relock(answer.status === 423 ? answer.data.error || lockedWords(answer.data.minutes || 10) : LOCKED_AGAIN, answer.status === 423 ? answer.data.minutes : 0);
      const error = new Error(answer.data.error || LOCKED_AGAIN);
      error.locked = true;
      throw error;
    }
    if (!answer.ok) {
      used();   // the token was good, and that request used it
      const error = new Error(answer.data.error || "Request failed (" + answer.status + ")");
      error.status = answer.status;
      error.data = answer.data;   // a form's refusals by setting are in data.errors
      throw error;
    }
    used();
    return answer.data;
  }
  const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

  // Back to the pad, saying why, when the person is still in Settings; the token goes either way.
  function relock(words, minutes) {
    endToken();
    if (P.view() === "settings" || P.view() === "settings-pin") {
      showPad("enter", words, minutes);
      P.setView("settings-pin");
    }
  }

  // --- The PIN pad: the digits, Delete and OK, and over them what is asked, a hint, a dot for each digit typed and a line for the
  // answer. The Settings view has one, and Security makes its own. A computer's own keys type on whichever pad is on the screen.
  const pads = [];
  function pinPad(host, options) {
    const ids = options.ids;
    const root = make("div", "pin");
    const words = make("div", "pin-words");
    const title = make("p", "pin-title");
    const hint = make("p", "note pin-hint");
    const dots = make("p", "pin-dots");
    const message = make("p", "message pin-message");
    message.setAttribute("role", "status");
    words.append(title, hint, dots, message);
    const keys = make("div", "pin-pad");
    keys.setAttribute("role", "group");
    keys.setAttribute("aria-label", "PIN pad");
    const state = { digits: "", busy: false, locked: false };
    const key = (label, attribute, value, className) => {
      const b = make("button", "button " + className, label);
      b.type = "button";
      b.setAttribute(attribute, value);
      P.keepFocus(b);
      return b;
    };
    const digitKeys = "1234567890".split("").map((d) => key(d, "data-digit", d, "quiet pin-key"));
    const del = key("Delete", "data-key", "delete", "quiet pin-key pin-word");
    const ok = key("OK", "data-key", "ok", "primary pin-key pin-word");
    keys.append(...digitKeys.slice(0, 9), del, digitKeys[9], ok);
    if (ids) {
      title.id = ids + "-title"; hint.id = ids + "-hint"; dots.id = ids + "-dots"; message.id = ids + "-message"; keys.id = ids + "-pad";
    }
    root.append(words, keys);
    host.append(root);

    function draw() {
      const dotsNow = dots.querySelectorAll(".dot.on").length;
      if (dotsNow !== state.digits.length) dots.replaceChildren(...Array.from(state.digits, () => make("span", "dot on")));
      dots.setAttribute("aria-label", state.digits.length + " of " + MAX_DIGITS + " digits");
      const off = state.busy || state.locked;
      for (const b of digitKeys) b.disabled = off;
      del.disabled = off || !state.digits;
      ok.disabled = off || state.digits.length < MIN_DIGITS;
    }
    function press(digit) {
      if (state.busy || state.locked || state.digits.length >= MAX_DIGITS) return;
      if (!state.digits) message.textContent = "";   // a new PIN begins: the last answer was about the one before
      state.digits += digit;
      draw();
    }
    function remove() {
      if (state.busy || state.locked) return;
      state.digits = state.digits.slice(0, -1);
      draw();
    }
    function submit() {
      if (state.busy || state.locked || state.digits.length < MIN_DIGITS) return;
      const digits = state.digits;
      state.digits = "";   // the dots go at once: a PIN is not left on the screen while its answer comes
      draw();
      options.onOk(digits);
    }
    for (const b of digitKeys) b.addEventListener("click", () => press(b.dataset.digit));
    del.addEventListener("click", remove);
    ok.addEventListener("click", submit);

    const pad = {
      root: root, press: press, remove: remove, submit: submit,
      reset: (titleText, hintText, words) => {
        state.digits = "";
        title.textContent = titleText;
        hint.textContent = hintText || "";
        message.textContent = words || "";
        draw();
      },
      say: (words) => { message.textContent = words; },
      busy: (on) => { state.busy = Boolean(on); draw(); },
      lock: (on) => { state.locked = Boolean(on); draw(); },
    };
    for (let i = pads.length - 1; i >= 0; i--) if (!pads[i].root.isConnected) pads.splice(i, 1);   // a Security page that has been left
    pads.push(pad);
    draw();
    return pad;
  }
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target && e.target.closest && e.target.closest("input, textarea, select, [contenteditable]")) return;
    const pad = pads.find((p) => p.root.isConnected && p.root.offsetParent !== null);   // the one on the screen, if any
    if (!pad) return;
    if (/^[0-9]$/.test(e.key)) pad.press(e.key);
    else if (e.key === "Backspace") pad.remove();
    else if (e.key === "Enter") pad.submit();
    else return;
    e.preventDefault();
  });

  // --- The Settings view's pad: Enter the PIN, or the first time Choose a PIN and enter it again.
  const TITLES = { enter: "Enter the PIN", choose: "Choose a PIN", confirm: "Enter the same PIN again" };
  const HINTS = { enter: "", choose: "4 to 8 digits. Settings asks for it each time it is opened.", confirm: "" };
  const pad = pinPad(el("pin-host"), { ids: "pin", onOk: pinEntered });
  let padMode = "enter";
  let firstChoice = "";
  let openings = 0;        // counts Options > Settings: the answer to an earlier one is not acted on
  let unlockTimer = null;  // a lock-out's end, when the pad asks the agent again

  function showPad(mode, words, minutes) {
    padMode = mode;
    if (mode !== "confirm") firstChoice = "";
    pad.reset(TITLES[mode], HINTS[mode], words || "");
    pad.busy(false);
    pad.lock(Boolean(minutes));
    clearTimeout(unlockTimer);
    if (minutes) unlockTimer = setTimeout(() => { if (P.view() === "settings-pin") openSettings(); }, minutes * 60 * 1000);
  }

  async function openSettings() {
    const asked = ++openings;
    P.setView("settings-pin");
    pad.reset("", "", "");
    pad.busy(true);
    let answer = null;
    try { answer = await call("/api/settings/pin"); } catch (e) { answer = null; }
    if (asked !== openings || P.view() !== "settings-pin") return;
    if (!answer || !answer.ok) { showPad("enter", NO_ROOM); return; }   // its OK will tell whether a PIN is to be chosen
    const s = answer.data;
    if (s.unlocked) { used(); enterSettings(); return; }                // this tab is unlocked still, and that request used it
    forgetToken();
    if (s.locked_minutes) showPad("enter", lockedWords(s.locked_minutes), s.locked_minutes);
    else showPad(s.set ? "enter" : "choose");
  }

  async function pinEntered(digits) {
    if (padMode === "choose") {
      showPad("confirm");
      firstChoice = digits;
      return;
    }
    if (padMode === "confirm" && digits !== firstChoice) {
      showPad("choose", "The two PINs were not the same: choose a PIN again");
      return;
    }
    const choosing = padMode === "confirm";
    const asked = openings;
    pad.busy(true);
    let answer;
    try {
      answer = await call(choosing ? "/api/settings/pin/set" : "/api/settings/unlock", { method: "POST", body: JSON.stringify({ pin: digits }) });
    } catch (e) {
      pad.busy(false);
      pad.say(NO_ROOM);
      return;
    }
    if (asked !== openings || P.view() !== "settings-pin") {   // the person has gone: a token they did not stay for is ended
      if (answer.ok && answer.data.token) sendLock(answer.data.token);
      return;
    }
    pad.busy(false);
    if (answer.ok) {
      keepToken(answer.data.token);
      enterSettings();
      return;
    }
    const words = answer.data.error || "Request failed (" + answer.status + ")";
    if (answer.status === 423) showPad("enter", words, answer.data.minutes);
    else if (answer.status === 409) showPad(choosing ? "enter" : "choose", words);   // chosen meanwhile at another screen; or reset at the TV Pi
    else if (choosing) showPad("choose", words);
    else pad.say(words);                                                              // a wrong PIN: the dots went at OK
  }

  // --- The menu and its pages.
  const menu = el("settings-menu");
  const pageBox = el("settings-page");
  let openPage = null;     // the page on the screen, or null for the menu
  let leavePage = null;    // what that page asked to run when it is left
  for (const page of PAGES) {
    const b = make("button", "button quiet menu-item");
    b.type = "button";
    b.dataset.page = page.key;
    P.labelWithIcon(b, page.label, page.icon);
    b.addEventListener("click", () => showPage(page));
    menu.append(b);
  }

  function enterSettings() {
    showMenu();
    P.setView("settings");
  }

  function showMenu() {
    if (typeof leavePage === "function") {
      try { leavePage(); } catch (e) { console.warn("A Settings page did not tidy up:", e); }
    }
    leavePage = null;
    openPage = null;
    el("settings-title").textContent = "Settings";
    menu.hidden = false;
    pageBox.hidden = true;
    pageBox.replaceChildren();
    delete pageBox.dataset.page;
  }

  function showPage(page) {
    if (page.view) { P.setView(page.view); return; }
    showMenu();
    openPage = page;
    el("settings-title").textContent = page.label;
    menu.hidden = true;
    pageBox.dataset.page = page.key;
    pageBox.hidden = false;
    if (page.draw) leavePage = page.draw(pageBox) || null;
    else pageBox.append(make("p", "settings-soon", "Coming soon"));
  }

  // Security: change the PIN on a pad of the page's own, in three steps: the PIN it is now, the new one, and the new one again.
  function drawSecurity(host) {
    let step = "old", old = "", fresh = "";
    const securityPad = pinPad(host, { onOk: entered });
    function begin(words) {
      step = "old"; old = ""; fresh = "";
      securityPad.reset("Enter the current PIN", "", words);
    }
    function chooseNew(words) {
      step = "new"; fresh = "";
      securityPad.reset("Choose a new PIN", "4 to 8 digits", words);
    }
    async function entered(digits) {
      if (step === "old") { old = digits; chooseNew(); return; }
      if (step === "new") { fresh = digits; step = "again"; securityPad.reset("Enter the new PIN again", ""); return; }
      if (digits !== fresh) { chooseNew("The two PINs were not the same: choose a new PIN again"); return; }
      securityPad.busy(true);
      try {
        await post("/api/settings/pin/change", { old: old, new: fresh });
        begin("Saved");
      } catch (e) {
        if (!e.locked) begin(e.message);
      }
      securityPad.busy(false);
    }
    begin();
  }

  // --- The minutes left, in the top bar; at none the page locks itself, as the agent has.
  function showTimeLeft() {
    if (!heldToken()) return;
    const left = deadline - Date.now();
    if (left <= 0) {
      endToken();
      if (P.view() === "settings") { showPad("enter", LOCKED_AGAIN); P.setView("settings-pin"); }
      return;
    }
    el("settings-time").textContent = "Locks in " + Math.ceil(left / 60000) + " min";
  }
  setInterval(showTimeLeft, 1000);

  P.onViewChange((name) => {
    if (name !== "settings" && openPage) showMenu();     // a page is begun afresh each time it is opened
    if (!AREA.includes(name)) {                          // the idle screen, a meeting, Camera framing...: Settings is left, and locked
      if (heldToken()) endToken();
      return;
    }
    if (name === "settings" && !heldToken()) { showPad("enter", LOCKED_AGAIN); P.setView("settings-pin"); }   // back from its TV page, after the minutes ran out
  });

  el("open-settings").addEventListener("click", openSettings);
  el("back-from-settings-pin").addEventListener("click", () => P.setView("options"));
  el("back-from-settings").addEventListener("click", () => { if (openPage) showMenu(); else P.setView("options"); });
  el("settings-lock").addEventListener("click", () => {
    endToken();
    showPad("enter");
    P.setView("settings-pin");
  });

  window.croomSettings = {
    PAGES: PAGES, api: api, post: post, pinPad: pinPad, open: openSettings,
    // A page built in a file of its own hands its draw over here; its place in the menu is PAGES'.
    define: (key, draw) => {
      const page = PAGES.find((p) => p.key === key);
      if (!page) throw new Error("No Settings page " + key);
      page.draw = draw;
    },
  };
})();
