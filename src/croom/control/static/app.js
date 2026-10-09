(function () {
  "use strict";

  const JOIN_WINDOW_MS = 10 * 60 * 1000;
  const STATUS_EVERY_MS = 2000;
  const EVENTS_EVERY_MS = 30000;
  const CONFIRM_MS = 5000;
  const SLIDE_OFF_PX = 16;   // how far outside a pressed pad button a finger may drift before that counts as letting go
  const IN_MEETING = ["joining", "in_lobby", "connected", "leaving"];   // the meeting states in which the room is taken

  const el = (id) => document.getElementById(id);
  const model = { status: null, events: [], offline: false, busy: false, error: "", confirmLeave: false, screensaver: null };
  let confirmTimer = null;
  let lastActionsKey = null;
  let lastEventsKey = null;
  let lastPickerKey = null;
  let lastSoundKey = null;
  let lastCameraKey = null;
  let presetSaveMode = false;
  let setupOpen = false;
  const deviceEpoch = { audio: 0, camera: 0 };   // counts each device's answers, so a poll already on its way cannot undo one

  // The TV's idle styles, in the order the service lists them (spec 2026-10-07 TV, section 4.2).
  const STYLE_LABELS = { info: "Information", quiet: "Quiet", brand: "Brand", bounce: "Bounce" };

  const platformNames = { zoom: "Zoom", google_meet: "Google Meet", teams: "Teams", webex: "Webex" };
  const platformName = (key) => platformNames[key] || key || "";
  const fmtTime = (d) => d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const fmtRange = (a, b) => fmtTime(a) + " to " + fmtTime(b);
  const minutesUntil = (d) => Math.max(0, Math.round((d - Date.now()) / 60000));
  const plural = (n, word) => n + " " + word + (n === 1 ? "" : "s");

  function elapsedSince(iso) {
    const seconds = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    const mm = String(m).padStart(h ? 2 : 1, "0");
    const ss = String(s).padStart(2, "0");
    return (h ? h + ":" : "") + mm + ":" + ss;
  }

  async function api(path, options) {
    const init = Object.assign({ headers: { "Content-Type": "application/json" } }, options || {});
    const response = await fetch(path, init);
    let data = {};
    try { data = await response.json(); } catch (e) { data = {}; }
    if (!response.ok) throw new Error(data.error || "Request failed (" + response.status + ")");
    return data;
  }

  async function refreshStatus() {
    const epochs = Object.assign({}, deviceEpoch);
    try {
      const status = await api("/api/status");
      for (const kind of Object.keys(epochs)) {
        // A device answered a command while this poll was on its way: its block is newer than the poll's. The other
        // device's block is still the poll's to give.
        if (epochs[kind] !== deviceEpoch[kind] && model.status) status[kind] = model.status[kind];
      }
      model.status = status;
      model.offline = false;
      if (model.status.screensaver) model.screensaver = model.status.screensaver;
    } catch (e) {
      model.offline = true;
    }
    render();
  }

  async function refreshEvents() {
    try {
      model.events = (await api("/api/calendar/events")).events || [];
    } catch (e) {
      // keep the last list
    }
    render();
  }

  async function act(request) {
    if (model.busy) return;
    model.busy = true;
    model.error = "";
    render();
    try {
      await request();
    } catch (e) {
      model.error = e.message;
    }
    model.busy = false;
    await refreshStatus();
  }

  const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });
  const joinLink = (url) => act(() => post("/api/meeting/join", { url: url }));
  const joinEvent = (id) => act(() => post("/api/meeting/join", { event_id: id }));
  const leave = () => act(() => post("/api/meeting/leave"));
  const toggle = (kind) => act(() => post("/api/meeting/" + kind));
  const setScreensaver = (style) => act(() => post("/api/screensaver", { style: style }).then((data) => { model.screensaver = data.style; }));

  function button(label, className, onClick, disabled) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "button " + className;
    b.textContent = label;
    b.disabled = Boolean(disabled) || model.busy;
    b.addEventListener("click", onClick);
    return b;
  }

  function setActions(specs) {
    // Replace the buttons only when their labels or availability change, so a
    // click in progress is never lost to a re-render (the timer ticks every second).
    const key = JSON.stringify(specs.map((s) => [s.label, s.className, Boolean(s.disabled) || model.busy]));
    if (key === lastActionsKey) return;
    lastActionsKey = key;
    el("actions").replaceChildren(...specs.map((s) => button(s.label, s.className, s.onClick, s.disabled)));
  }

  function leaveSpec() {
    if (model.confirmLeave) {
      return { label: "Tap again to leave", className: "danger", onClick: () => {
        clearTimeout(confirmTimer);
        model.confirmLeave = false;
        leave();
      } };
    }
    return { label: "Leave", className: "danger", onClick: () => {
      model.confirmLeave = true;
      clearTimeout(confirmTimer);
      confirmTimer = setTimeout(() => { model.confirmLeave = false; render(); }, CONFIRM_MS);
      render();
    } };
  }

  function meetingDetail(m) {
    return (m.title ? m.title + ", " : "") + platformName(m.platform) + (m.joined_at ? ", " + elapsedSince(m.joined_at) : "");
  }

  function joinWindow(ev) {
    const start = new Date(ev.start_time).getTime();
    const end = new Date(ev.end_time).getTime();
    const now = Date.now();
    return { open: now >= start - JOIN_WINDOW_MS && now < end, opensAt: new Date(start - JOIN_WINDOW_MS), past: now >= end };
  }

  function render() {
    const body = document.body;
    el("message").textContent = model.error;

    if (model.offline || !model.status) {
      body.dataset.state = "offline";
      el("kicker").textContent = "Not connected";
      el("headline").textContent = "Can't reach the room";
      el("detail").textContent = "Check that Crystal Meet is running on the room's device, then this page will reconnect on its own.";
      setActions([]);
      document.querySelector(".screen-section").hidden = true;
      document.querySelector(".sound-section").hidden = true;
      document.querySelector(".camera-section").hidden = true;
      return;
    }
    document.querySelector(".screen-section").hidden = false;
    document.querySelector(".sound-section").hidden = false;
    document.querySelector(".camera-section").hidden = false;

    const s = model.status;
    el("room-name").textContent = s.room.name;
    el("room-location").textContent = s.room.location;
    const m = s.meeting;
    const cal = s.calendar;
    const label = m.title || (m.platform ? platformName(m.platform) + " meeting" : "the meeting");
    // A join is refused while a meeting is in progress, so do not offer the link form then.
    document.querySelector(".link").hidden = IN_MEETING.includes(m.state);

    let specs = [];
    if (m.state === "joining" || m.state === "in_lobby" || m.state === "leaving") {
      body.dataset.state = "joining";
      el("kicker").textContent = m.state === "leaving" ? "Leaving" : "Joining";
      el("headline").textContent = m.state === "leaving" ? "Leaving" : "Joining " + label;
      el("detail").textContent = m.state === "in_lobby" ? "Waiting for the host to let the room in." : "The room's screen is connecting.";
      if (m.state !== "leaving") specs = [{ label: "Cancel", className: "quiet", onClick: leave }];
    } else if (m.state === "connected") {
      body.dataset.state = "meeting";
      el("kicker").textContent = "In a meeting";
      el("headline").textContent = "In a meeting";
      el("detail").textContent = meetingDetail(m);
      specs = [
        { label: m.muted ? "Unmute" : "Mute", className: "", onClick: () => toggle("mute") },
        { label: m.camera_on ? "Turn camera off" : "Turn camera on", className: "", onClick: () => toggle("camera") },
        leaveSpec(),
      ];
    } else if (m.state === "error") {
      body.dataset.state = "error";
      el("kicker").textContent = "Couldn't join";
      el("headline").textContent = "Couldn't join " + label;
      el("detail").textContent = m.error || "The room's screen could not join. Try again, or join from a different link.";
      specs = [{ label: "Dismiss", className: "quiet", onClick: leave }];
    } else {
      specs = renderIdle(cal);
    }
    setActions(specs);

    renderEvents(cal);
    renderPicker();
    renderSound(s.audio);
    renderCamera(s.camera);
  }

  function renderPicker() {
    // Rebuilt only when the choice or availability changes, so a tap is never lost to a re-render.
    const key = JSON.stringify([model.screensaver, model.busy]);
    if (key === lastPickerKey) return;
    lastPickerKey = key;
    const buttons = Object.keys(STYLE_LABELS).map((style) => {
      const current = model.screensaver === style;
      const b = button(STYLE_LABELS[style], current ? "primary" : "quiet", () => setScreensaver(style), false);
      b.setAttribute("aria-pressed", current ? "true" : "false");
      return b;
    });
    el("screen-picker").replaceChildren(...buttons);
  }

  function renderIdle(cal) {
    const body = document.body;
    const current = cal.current;
    const next = cal.next;

    if (current) {
      body.dataset.state = "soon";
      el("kicker").textContent = "Happening now";
      el("headline").textContent = current.title + " is happening now";
      el("detail").textContent = fmtRange(new Date(current.start_time), new Date(current.end_time)) + (current.joinable ? ", " + platformName(current.meeting_platform) : "");
      return current.joinable ? [{ label: "Join now", className: "primary", onClick: () => joinEvent(current.id) }] : [];
    }

    if (next) {
      const start = new Date(next.start_time);
      const window = joinWindow(next);
      const minutes = minutesUntil(start);
      if (window.open) {
        body.dataset.state = "soon";
        el("kicker").textContent = "Starting soon";
        el("headline").textContent = minutes === 0 ? next.title + " is starting" : next.title + " starts in " + plural(minutes, "minute");
      } else {
        body.dataset.state = "free";
        el("kicker").textContent = "Room free";
        el("headline").textContent = "Free until " + fmtTime(start);
      }
      el("detail").textContent = "Next: " + next.title + ", " + fmtRange(start, new Date(next.end_time)) + (next.joinable ? ", " + platformName(next.meeting_platform) : "");
      if (!next.joinable) return [];
      return [{ label: window.open ? "Join now" : "Join opens at " + fmtTime(window.opensAt), className: "primary", onClick: () => joinEvent(next.id), disabled: !window.open }];
    }

    body.dataset.state = "free";
    el("kicker").textContent = "Room free";
    el("headline").textContent = cal.connected ? "Free for the rest of the day" : "Free";
    el("detail").textContent = cal.connected ? "Nothing else is booked in here today." : "No calendar is connected. Join with a link below.";
    return [];
  }

  function renderEvents(cal) {
    const list = el("events");
    const note = el("calendar-note");
    const meetingState = model.status ? model.status.meeting.state : "idle";
    const key = JSON.stringify([cal.connected, cal.current && cal.current.id, meetingState, model.busy,
      model.events.map((ev) => [ev.id, ev.title, ev.start_time, ev.joinable, joinWindow(ev).open, joinWindow(ev).past])]);
    if (key === lastEventsKey) return;
    lastEventsKey = key;
    list.replaceChildren();
    if (!cal.connected) {
      note.textContent = "Calendar not connected.";
      return;
    }
    if (model.events.length === 0) {
      note.textContent = "Nothing scheduled today.";
      return;
    }
    note.textContent = "";
    for (const ev of model.events) {
      const window = joinWindow(ev);
      const li = document.createElement("li");
      li.className = "event" + (window.past ? " past" : "") + (cal.current && cal.current.id === ev.id ? " now" : "");
      const time = document.createElement("time");
      time.dateTime = ev.start_time;
      time.textContent = fmtTime(new Date(ev.start_time));
      const text = document.createElement("div");
      const title = document.createElement("p");
      title.className = "title";
      title.textContent = ev.title;
      text.append(title);
      if (ev.joinable) {
        const platform = document.createElement("p");
        platform.className = "platform";
        platform.textContent = platformName(ev.meeting_platform);
        text.append(platform);
      }
      li.append(time, text);
      if (ev.joinable && window.open && (!model.status || model.status.meeting.state === "idle" || model.status.meeting.state === "error")) {
        li.append(button("Join", "primary", () => joinEvent(ev.id)));
      }
      list.append(li);
    }
  }

  function tick() {
    el("clock").textContent = fmtTime(new Date());
    if (model.status && model.status.meeting.state === "connected") {
      el("detail").textContent = meetingDetail(model.status.meeting);
    }
  }

  el("link-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = el("link-input");
    const value = input.value.trim();
    if (!value) return;
    joinLink(value).then(() => { if (!model.error) input.value = ""; });
  });

  // The page's own keyboard, for screens without one: the table Pi's kiosk browser opens
  // the page with ?keyboard=1 and it appears when the link field is tapped; the button
  // beside the field toggles it on any device. Keys never take the focus from the field.
  const keyboard = (function () {
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
    return { show: show, hide: hide };
  })();

  // --- Sound and Camera: the commands behind both panels (spec 2026-10-08, section 4.6) ---
  // A success answers with the device's own block, which replaces the model's at once (no waiting for the next poll).
  // Any refusal (a non-2xx) puts its reason in that panel's note and leaves the model as it was. A long move that a
  // newer command cut short answers 409 "interrupted": the newer tap is the normal cause, so that says nothing at all.
  // options.long: Home, a preset recall and Find the stops run for seconds inside the request. options.background: a
  // call the page makes on its own (the preview's renewal): its answer is applied, but it neither clears the note the
  // user last got nor writes one, and a refusal of it is not shown or scrolled to.
  const INTERRUPTED = "the camera move was interrupted";
  const deviceNotes = { audio: "", camera: "" };

  async function deviceCall(kind, path, body, options) {
    const long = Boolean(options && options.long);
    const background = Boolean(options && options.background);
    if (long) {
      // Home, a preset recall and Find the stops run for seconds inside the request: ask the status for the camera's
      // "busy" now, so "Moving..." shows without waiting for the poll's turn.
      deviceNotes[kind] = "";
      render();
      setTimeout(refreshStatus, 300);
    }
    let block;
    try {
      block = await post(path, body);
    } catch (e) {
      if (!background && e.message !== INTERRUPTED) {
        deviceNotes[kind] = e.message;
        if (kind === "audio") lastSoundKey = null; else lastCameraKey = null;   // drawn again from the model: a slider the finger moved goes back
        render();
        // The note sits above its panel: if the controls have scrolled it out of sight, bring it back, and no further.
        el(kind === "audio" ? "sound-note" : "camera-note").scrollIntoView({ block: "nearest" });
      }
      return false;
    }
    if (!background) deviceNotes[kind] = "";
    if (model.status) model.status[kind] = block;
    deviceEpoch[kind] += 1;
    render();
    return true;
  }

  // --- Sound ---
  function renderSound(audio) {
    const key = JSON.stringify([audio && audio.available, audio && audio.device, audio && audio.level, audio && audio.muted, deviceNotes.audio]);
    if (key === lastSoundKey) return;
    lastSoundKey = key;
    const panel = el("sound-panel");
    if (!audio || !audio.available) {
      panel.hidden = true;
      el("sound-note").textContent = "No speaker found";
      return;
    }
    panel.hidden = false;
    el("sound-note").textContent = deviceNotes.audio;
    el("sound-device").textContent = audio.device;
    el("volume-slider").value = audio.level;
    el("volume-level").textContent = String(audio.level);
    el("speaker-mute").setAttribute("aria-pressed", audio.muted ? "true" : "false");
    el("speaker-mute").textContent = audio.muted ? "Unmute speaker" : "Mute speaker";
  }

  const setVolume = (body) => deviceCall("audio", "/api/audio/volume", body);
  el("volume-slider").addEventListener("change", (e) => setVolume({ level: Number(e.target.value) }));
  el("quieter").addEventListener("click", () => setVolume({ step: -5 }));
  el("louder").addEventListener("click", () => setVolume({ step: 5 }));
  el("speaker-mute").addEventListener("click", () => {
    const muted = el("speaker-mute").getAttribute("aria-pressed") === "true";
    setVolume({ muted: !muted });
  });

  // --- Camera ---
  let previewTimer = null;

  function renderCamera(camera) {
    // Keyed like the other controls, so a button is never replaced under a finger when nothing about it changed.
    // model.busy is in the key because button() reads it.
    const key = JSON.stringify([camera && camera.available, camera && camera.busy, camera && camera.position_known,
      camera && camera.home_saved, camera && camera.zoom, camera && camera.presets, presetSaveMode, setupOpen, model.busy, deviceNotes.camera]);
    if (key === lastCameraKey) return;
    lastCameraKey = key;
    const panel = el("camera-panel");
    if (!camera || !camera.available) {
      panel.hidden = true;
      el("camera-note").textContent = "No controllable camera found";
      return;
    }
    panel.hidden = false;
    el("camera-note").textContent = deviceNotes.camera;
    el("camera-status").textContent = camera.busy ? "Moving\u2026" : "Zoom " + camera.zoom.level + " of " + camera.zoom.min + " to " + camera.zoom.max;
    for (const b of document.querySelectorAll("#arrow-pad .pad")) b.disabled = camera.busy;
    el("camera-home").disabled = camera.busy || !camera.home_saved;
    const ready = camera.home_saved;
    el("preset-row").hidden = !ready;
    el("preset-save-mode").hidden = !ready;
    el("setup-link").hidden = !ready;
    el("camera-setup").hidden = ready && !setupOpen;
    el("save-home").disabled = !camera.position_known || camera.busy;
    el("find-stops").disabled = camera.busy;
    el("preset-save-mode").setAttribute("aria-pressed", presetSaveMode ? "true" : "false");
    el("preset-save-mode").textContent = presetSaveMode ? "Tap a preset to save it here" : "Save";
    el("preset-row").replaceChildren(...camera.presets.map((p) => {
      const b = button(p.name, p.saved ? "quiet saved" : "quiet", () => presetAction(p.slot), camera.busy);
      b.dataset.slot = String(p.slot);
      if (!p.saved && !presetSaveMode) b.title = "Nothing saved yet";
      return b;
    }));
  }

  function presetAction(slot) {
    const action = presetSaveMode ? "save" : "recall";
    presetSaveMode = false;
    lastCameraKey = null;
    deviceCall("camera", "/api/camera/presets/" + slot, { action: action }, { long: action === "recall" });
  }

  el("preset-save-mode").addEventListener("click", () => { presetSaveMode = !presetSaveMode; lastCameraKey = null; render(); });
  el("setup-link").addEventListener("click", () => { setupOpen = !setupOpen; lastCameraKey = null; render(); });
  el("find-stops").addEventListener("click", () => deviceCall("camera", "/api/camera/setup", { action: "find_stops" }, { long: true }));
  el("save-home").addEventListener("click", () => {
    deviceCall("camera", "/api/camera/setup", { action: "save_home" }).then((saved) => {
      if (saved) { setupOpen = false; render(); }   // Set up closes only once the home is really saved
    });
  });
  el("camera-home").addEventListener("click", () => deviceCall("camera", "/api/camera/home", {}, { long: true }));

  // Hold to move: press starts, release stops; re-sent every 750 ms while held so the agent's watchdog stays quiet.
  // Sliding the finger off the button is a release too (spec 2026-10-08, section 4.6): the pointer stays captured so
  // that a lift is always seen, and a move more than SLIDE_OFF_PX outside the button's box (a finger resting on the
  // edge jitters) ends the hold the same way a lift does.
  function holdToMove(buttonEl, start, repeatMs, stop) {
    let timer = null;
    const release = () => {
      if (timer === null) return;
      clearInterval(timer);
      timer = null;
      // A pad that has gone disabled (a Home or a preset from another screen made the camera busy) sends no stop: it
      // would cut that long move short.
      if (stop && !buttonEl.disabled) stop();
    };
    buttonEl.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      if (buttonEl.disabled || timer !== null) return;
      buttonEl.setPointerCapture(e.pointerId);
      start();
      timer = setInterval(() => {
        // The camera went busy under the finger (a Home or a preset from another screen): stop repeating, and do not
        // send the stop either, which would cut that move short.
        if (buttonEl.disabled) { clearInterval(timer); timer = null; return; }
        start();
      }, repeatMs);
    });
    for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) buttonEl.addEventListener(type, release);
    buttonEl.addEventListener("pointermove", (e) => {
      if (timer === null) return;   // no hold: nothing to end, and the rest of a press that already ended is ignored
      const box = buttonEl.getBoundingClientRect();
      const slack = SLIDE_OFF_PX;
      if (e.clientX >= box.left - slack && e.clientX <= box.right + slack && e.clientY >= box.top - slack && e.clientY <= box.bottom + slack) return;
      release();
      if (buttonEl.hasPointerCapture(e.pointerId)) buttonEl.releasePointerCapture(e.pointerId);
    });
    buttonEl.addEventListener("contextmenu", (e) => e.preventDefault());   // a long press on a touch screen must not open a menu
  }
  const moveCamera = (pan, tilt) => deviceCall("camera", "/api/camera/move", { pan: pan, tilt: tilt });
  for (const b of document.querySelectorAll("#arrow-pad [data-pan]")) {
    const pan = Number(b.dataset.pan), tilt = Number(b.dataset.tilt);
    holdToMove(b, () => moveCamera(pan, tilt), 750, () => moveCamera(0, 0));
  }
  for (const b of document.querySelectorAll("[data-zoom]")) {
    holdToMove(b, () => deviceCall("camera", "/api/camera/zoom", { step: Number(b.dataset.zoom) }), 400, null);
  }

  // The idle preview on the TV: asked for while the panel is open and no meeting runs, renewed every 30 s. The renewal
  // keeps running while the panel is open, so it also picks the preview up again when a meeting ends or the camera returns.
  function previewWanted() {
    const s = model.status;
    return Boolean(el("camera-panel").open && s && s.camera && s.camera.available && !IN_MEETING.includes(s.meeting.state));
  }
  const setPreview = (on, options) => deviceCall("camera", "/api/camera/preview", { on: on }, options);
  el("camera-panel").addEventListener("toggle", () => {
    clearInterval(previewTimer);
    previewTimer = null;
    if (!el("camera-panel").open) {
      setPreview(false);
      return;
    }
    if (previewWanted()) setPreview(true);
    previewTimer = setInterval(() => { if (previewWanted()) setPreview(true, { background: true }); }, 30000);
  });
  window.addEventListener("pagehide", () => {
    if (!el("camera-panel").open) return;
    fetch("/api/camera/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ on: false }), keepalive: true }).catch(() => {});
  });

  tick();
  setInterval(tick, 1000);
  refreshStatus();
  refreshEvents();
  setInterval(refreshStatus, STATUS_EVERY_MS);
  setInterval(refreshEvents, EVENTS_EVERY_MS);
})();
