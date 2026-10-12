(function () {
  "use strict";

  const JOIN_WINDOW_MS = 10 * 60 * 1000;
  const STATUS_EVERY_MS = 2000;
  const EVENTS_EVERY_MS = 30000;
  const CONFIRM_MS = 5000;
  const IN_MEETING = ["joining", "in_lobby", "connected", "leaving"];   // the meeting states in which the room is taken
  // Screens (spec 2026-10-12): body[data-view] names the one shown. Each view belongs to a mode; the camera view
  // belongs to whichever mode showed it, so its Back returns there.
  const VIEW_MODES = { idle: "idle", options: "idle", screensaver: "idle", meeting: "meeting", upcoming: "meeting" };
  const MAIN_SCREEN = { idle: "idle", meeting: "meeting" };

  const el = (id) => document.getElementById(id);
  const model = { status: null, events: [], offline: false, busy: false, error: "", confirmLeave: false, screensaver: null };
  const renderHooks = [];   // devices.js registers its rendering here; render() calls each with the status
  const viewHooks = [];     // devices.js and keyboard.js register here; called after every view change
  let cameraMode = "idle";  // the mode that opened the camera view

  const view = () => document.body.dataset.view;
  const mode = () => (model.status && IN_MEETING.includes(model.status.meeting.state) ? "meeting" : "idle");
  const viewMode = (name) => (name === "camera" ? cameraMode : VIEW_MODES[name]);

  function applyView(name, automatic) {
    if (name === "camera") cameraMode = mode();
    document.body.dataset.view = name;
    for (const fn of viewHooks) fn(name, Boolean(automatic));
  }
  function setView(name, automatic) {
    if (view() === name) return;
    applyView(name, automatic);
    render();
  }
  let confirmTimer = null;
  let lastEventsKey = null;
  let lastPickerKey = null;
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

  function setActions(specs, containerId, keyHolder) {
    // Replace the buttons only when their labels or availability change, so a
    // click in progress is never lost to a re-render (the timer ticks every second).
    const key = JSON.stringify(specs.map((s) => [s.label, s.className, Boolean(s.disabled) || model.busy]));
    if (key === keyHolder.key) return;
    keyHolder.key = key;
    el(containerId).replaceChildren(...specs.map((s) => button(s.label, s.className, s.onClick, s.disabled)));
  }
  const idleActionsKey = { key: null };
  const meetingActionsKey = { key: null };

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
    el("meeting-message").textContent = model.error;   // the meeting screen's own line for a refusal or an error

    if (model.offline || !model.status) {
      body.dataset.state = "offline";
      el("kicker").textContent = "Not connected";
      el("headline").textContent = "Can't reach the room";
      el("detail").textContent = "Check that Crystal Meet is running on the room's device, then this page will reconnect on its own.";
      setActions([], "actions", idleActionsKey);
      setActions([], "meeting-actions", meetingActionsKey);
      document.querySelector(".screen-section").hidden = true;
      document.querySelector(".sound-section").hidden = true;
      document.querySelector(".camera-section").hidden = true;
      return;
    }
    document.querySelector(".screen-section").hidden = false;
    document.querySelector(".sound-section").hidden = false;
    document.querySelector(".camera-section").hidden = false;

    const s = model.status;
    if (viewMode(view()) !== mode()) applyView(MAIN_SCREEN[mode()], true);   // a meeting began or ended: land on that mode's screen
    el("open-options").hidden = mode() !== "idle";
    el("room-name").textContent = s.room.name;
    el("room-location").textContent = s.room.location;
    el("room-location").hidden = !s.room.location;
    const m = s.meeting;
    const cal = s.calendar;
    const label = m.title || (m.platform ? platformName(m.platform) + " meeting" : "the meeting");

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
    if (mode() === "meeting") {
      setActions(specs, "meeting-actions", meetingActionsKey);
      setActions([], "actions", idleActionsKey);
    } else {
      setActions(specs, "actions", idleActionsKey);
      setActions([], "meeting-actions", meetingActionsKey);
    }

    renderEvents(cal);
    renderPicker();
    for (const fn of renderHooks) fn(s);
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

  el("open-options").addEventListener("click", () => setView("options"));
  el("back-from-options").addEventListener("click", () => setView("idle"));
  el("open-camera-setup").addEventListener("click", () => setView("camera"));
  el("open-screensaver").addEventListener("click", () => setView("screensaver"));
  el("back-from-screensaver").addEventListener("click", () => setView("options"));
  el("back-from-camera").addEventListener("click", () => setView(cameraMode === "meeting" ? "meeting" : "options"));
  el("open-camera-live").addEventListener("click", () => setView("camera"));
  el("open-upcoming").addEventListener("click", () => setView("upcoming"));
  el("back-from-upcoming").addEventListener("click", () => setView("meeting"));

  window.croomPage = {
    el: el, model: model, api: api, post: post, act: act, button: button, render: render,
    refreshStatus: refreshStatus, deviceEpoch: deviceEpoch, IN_MEETING: IN_MEETING,
    view: view, setView: setView, mode: mode, cameraMode: () => cameraMode,
    onViewChange: (fn) => viewHooks.push(fn),
    // A hook that registers after a poll has been answered (on a slow link the first answer can land before the other
    // scripts have run) is given that status at once. The renders behind the hooks are keyed, so the replay is idempotent.
    onRender: (fn) => { renderHooks.push(fn); if (model.status && !model.offline) fn(model.status); },
  };

  tick();
  setInterval(tick, 1000);
  refreshStatus();
  refreshEvents();
  setInterval(refreshStatus, STATUS_EVERY_MS);
  setInterval(refreshEvents, EVENTS_EVERY_MS);
})();
