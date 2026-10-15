(function () {
  "use strict";

  const JOIN_WINDOW_MS = 10 * 60 * 1000;
  const STATUS_EVERY_MS = 2000;
  const EVENTS_EVERY_MS = 30000;
  const CONFIRM_MS = 5000;
  const IN_MEETING = ["joining", "in_lobby", "connected", "leaving"];   // the meeting states in which the room is taken
  // Screens (spec 2026-10-12): body[data-view] names the one shown. Each view belongs to a mode; the camera view
  // belongs to whichever mode showed it, so its Back returns there.
  const VIEW_MODES = { idle: "idle", options: "idle", screensaver: "idle", meeting: "meeting", upcoming: "meeting", participants: "meeting", chat: "meeting", host: "meeting" };
  const MAIN_SCREEN = { idle: "idle", meeting: "meeting" };

  const el = (id) => document.getElementById(id);
  // lastLink: the link of the join the page last had accepted, until that join resolves (see render) or the person has the box
  // for something else. Zoom's refusal over the passcode comes later, in the status, and the card gives that link back.
  // lastEvent: the booking, when that join was a booking's, for the same time: Join again on its card joins the booking again.
  const model = { status: null, events: [], offline: false, busy: false, error: "", confirmLeave: false, screensaver: null, lastLink: "", lastEvent: "",
                  reactionsUntil: 0, pending: new Set() };   // when the reaction row closes (a time, 0 for closed); the controls, by name, whose requests are out
  const renderHooks = [];   // devices.js registers its rendering here; render() calls each with the status
  const viewHooks = [];     // devices.js and keyboard.js register here; called after every view change
  let cameraMode = "idle";  // the mode that opened the camera view
  let lastMeetingState = null;   // the meeting state the last render saw: a landing on the error screen happens once, on the change
  let passcodeCard = false;      // the last render showed the card for a join Zoom refused over the passcode: its landing happens once too
  let restoredLink = "";         // what that card put back in the link box, until the person edits the box: it goes with the card
  const kiosk = new URLSearchParams(window.location.search).get("keyboard") === "1";   // the table Pi's kiosk browser has no keyboard of its own (keyboard.js)

  // Icons: one sprite of Lucide symbols (icons.svg), fetched once and kept in the document, so <use href="#icon-..."> resolves
  // without a request per button. A page without the sprite keeps its labels (the fetch may fail offline).
  const iconsReady = fetch("/static/icons.svg").then((r) => {
    if (!r.ok) throw new Error("the server answered " + r.status);
    return r.text();
  }).then((svg) => {
    if (!svg) throw new Error("the file is empty");
    const holder = document.createElement("div");
    holder.hidden = true;
    holder.innerHTML = svg;
    document.body.prepend(holder);
    return true;
  }).catch((e) => {
    console.warn("The icon sprite did not load, so the buttons show their words alone:", e);   // once: the page keeps working
    return false;
  }).then((ok) => {
    document.body.classList.toggle("no-icons", !ok);   // style.css: no empty icon boxes, the words in the middle again, a glyph on the keyboard button
    return ok;
  });
  // The browser's own HTML parser makes the picture, so that no namespace address is written in this file (the service's
  // test of the page's files looks for any web address in them); the name goes in as an attribute, never as markup.
  function icon(name) {
    const holder = document.createElement("template");
    holder.innerHTML = '<svg class="icon" aria-hidden="true"><use></use></svg>';
    const svg = holder.content.firstChild;
    svg.firstChild.setAttribute("href", "#icon-" + name);
    return svg;
  }
  // A button's content: its icon, if it has one, then its label. The icon is hidden from a screen reader: the label is the name
  // (a button with an icon and no label, the keyboard toggle, is named by its aria-label). What does not change is not replaced: a
  // finger that went down on the icon or on the words must find them still there at the lift, or the browser drops the tap, and the
  // speaker button is drawn again at every volume change, Save at every camera change. A new label or icon is changed in place.
  function labelWithIcon(node, label, name) {
    let picture = null, words = null;
    for (const child of Array.from(node.childNodes)) {
      if (!picture && child.nodeType === 1 && child.matches("svg.icon")) picture = child;
      else if (!words && child.nodeType === 1 && child.tagName === "SPAN") words = child;
      else child.remove();   // the plain text the markup gave the button, or anything else that is not ours
    }
    if (name) {
      if (!picture) { picture = icon(name); node.prepend(picture); }
      else if (picture.querySelector("use").getAttribute("href") !== "#icon-" + name) picture.querySelector("use").setAttribute("href", "#icon-" + name);
    } else if (picture) picture.remove();
    if (label) {
      if (!words) { words = document.createElement("span"); node.append(words); }
      if (words.textContent !== label) words.textContent = label;
    } else if (words) words.remove();
  }

  const view = () => document.body.dataset.view;
  const mode = () => (model.status && IN_MEETING.includes(model.status.meeting.state) ? "meeting" : "idle");
  const viewMode = (name) => (name === "camera" ? cameraMode : VIEW_MODES[name]);

  function applyView(name, automatic) {
    if (name === "camera") cameraMode = mode();
    document.body.dataset.view = name;
    // Every view change, an automatic one too, starts or clears the wait. Before the hooks: a hook may start a timer of
    // its own (the preview's 30 s renewal), and when the two fall due together they run in the order they were set, so
    // the return goes first and the hook that ends the preview clears the renewal before it is sent.
    armIdleReturn();
    for (const fn of viewHooks) fn(name, Boolean(automatic));
  }
  function setView(name, automatic) {
    if (view() === name) return;
    applyView(name, automatic);
    render();
  }

  // Idle mode returns to the idle screen on its own: the kiosk never reloads and nobody presses Back, so a room left on
  // Camera framing would keep the TV on the camera for good. Any tap restarts the wait; a meeting screen never returns.
  const IDLE_RETURN_MS = 10 * 60 * 1000;
  let idleReturnTimer = null;
  function armIdleReturn() {
    clearTimeout(idleReturnTimer);
    idleReturnTimer = null;
    if (mode() === "idle" && view() !== "idle") idleReturnTimer = setTimeout(() => setView("idle", true), IDLE_RETURN_MS);
  }
  document.addEventListener("pointerdown", armIdleReturn, true);

  let confirmTimer = null;
  let lastEventsKey = null;
  let lastPickerKey = null;
  let lastUpcomingKey = null;
  const deviceEpoch = { audio: 0, camera: 0, meeting: 0 };   // counts each device's answers, and the meeting's, so a poll already on its way cannot undo one

  // The TV's idle styles, in the order the service lists them (spec 2026-10-07 TV, section 4.2).
  const STYLE_LABELS = { info: "Information", quiet: "Quiet", brand: "Brand", bounce: "Bounce" };

  const platformNames = { zoom: "Zoom", google_meet: "Google Meet", teams: "Teams", webex: "Webex" };
  const platformName = (key) => platformNames[key] || key || "";
  // Times read the American way whatever the browser's locale: 12-hour with AM or PM, and the date as "October 13, 2026"
  // (the Pi's Chromium would otherwise follow the system locale and show 24-hour times).
  // The space before AM or PM is a no-break one: a time never splits across lines (the sign's and the TV's headlines
  // broke "10:30 AM" as "10:30" / "AM"; Chromium writes a plain or a narrow space there depending on its version).
  const fmtTime = (d) => d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true }).replace(/\s+(?=[AP]M$)/, "\u00A0");
  const fmtDate = (d) => d.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });
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

  // The passcode field is for a Zoom meeting whose link carries none of its own. The page only decides whether to show the
  // field: the agent reads the link itself, and a passcode it refuses comes back in its own words, in the card's message.
  const linkInput = el("link-input");
  const passcodeInput = el("passcode-input");
  const ZOOM_DOMAINS = ["zoom.us", "zoomgov.com"];
  const PASSCODE_ASKED = "Zoom needs the meeting passcode";   // how the agent's reason starts when Zoom refuses a join over the passcode

  // A Zoom meeting ID: 9 to 11 digits, written in groups as an invitation prints it ("559 000 0559") or not. The digits, or "".
  function meetingId(text) {
    const digits = text.replace(/\s+/g, "");
    return /^\d{9,11}$/.test(digits) ? digits : "";
  }

  // A bare meeting ID, or a Zoom link whose query has no pwd=. A link typed without its scheme is read on the page's own:
  // only its host and its query count.
  function looksLikeZoomWithoutPasscode(text) {
    const value = text.trim();
    if (meetingId(value)) return true;
    let url;
    try { url = value.includes("://") ? new URL(value) : new URL("//" + value, location.href); } catch (e) { return false; }
    const host = url.hostname.toLowerCase();
    return ZOOM_DOMAINS.some((domain) => host === domain || host.endsWith("." + domain)) && !url.searchParams.get("pwd");
  }

  function showPasscodeField(show) {
    passcodeInput.hidden = !show;
    if (!show) passcodeInput.value = "";   // a code typed for one meeting is not for the next
  }
  const syncPasscodeField = () => showPasscodeField(looksLikeZoomWithoutPasscode(linkInput.value));
  const forgetJoin = () => { model.lastLink = ""; model.lastEvent = ""; };
  linkInput.addEventListener("input", () => {
    forgetJoin();   // the box is the person's own now: the card no longer gives the failed link back, nor joins its booking again
    restoredLink = "";
    syncPasscodeField();
  });

  // The agent took the join, so both boxes are done with; what it was stays in the model for a refusal that is still to come.
  function joinAccepted(url, eventId) {
    model.lastLink = url;
    model.lastEvent = eventId || "";
    linkInput.value = "";
    showPasscodeField(false);
  }

  // The card for a join Zoom refused over the passcode. While it is up the link that failed is in the box (the accepted join
  // emptied it, and a booking's join never filled it: the status names that link). The first time it shows, the field is there
  // whatever that link looks like (it may carry a pwd= that has gone stale), unless the person has begun something else in the
  // box since: a Meet link has no passcode to ask for.
  function askForPasscode(m, landing) {
    if (landing && !model.lastLink && !linkInput.value.trim()) model.lastLink = m.url || "";
    if (model.lastLink && !linkInput.value.trim()) {
      linkInput.value = restoredLink = model.lastLink;
      syncPasscodeField();
    }
    const value = linkInput.value;
    if (landing && (!value.trim() || value === restoredLink || looksLikeZoomWithoutPasscode(value))) showPasscodeField(true);
  }

  // The card has gone (Dismiss, a join that began, or another page): the link it put back goes with it, unless the person has
  // made the box their own, and the field follows the box again.
  function passcodeCardGone() {
    if (restoredLink && linkInput.value === restoredLink) linkInput.value = "";
    restoredLink = "";
    syncPasscodeField();
  }

  // The kiosk's keys come up on the passcode field. Called once the card is drawn, at the end of render(): the keyboard
  // scrolls the field into view, and the card, taller than the one it replaced, has pushed the field down by then.
  function focusPasscode() {
    if (document.activeElement === passcodeInput) passcodeInput.blur();   // the keys hear of a focus, not of one the field already has
    passcodeInput.focus();
  }

  // Form.requestSubmit is missing from old iOS Safari: there the page sends the submit event itself (its handler cancels it,
  // so nothing navigates). Used by the card's Join again here and by the keyboard's Join key.
  function submitForm(form) {
    if (typeof form.requestSubmit === "function") form.requestSubmit();
    else form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  }

  // Join again on the card. Zoom asked for a passcode: with the field there and empty there is nothing new to send, and a
  // join without one is refused again. The field takes the focus instead (the kiosk's keys come up with it). A field that is
  // not shown (the link in the box carries its own passcode) has nothing to wait for.
  function joinAgain() {
    if (!passcodeInput.hidden && !passcodeInput.value.trim()) { focusPasscode(); return; }
    submitForm(el("link-form"));
  }

  const post = (path, body, options) => api(path, Object.assign({ method: "POST", body: JSON.stringify(body || {}) }, options));
  const withPasscode = (body, passcode) => (passcode ? Object.assign(body, { passcode: passcode }) : body);
  const joinLink = (url, passcode) => act(() => post("/api/meeting/join", withPasscode({ url: url }, passcode)).then(() => joinAccepted(url)));
  const joinEvent = (id) => { forgetJoin(); return act(() => post("/api/meeting/join", { event_id: id }).then(() => { model.lastEvent = id; })); };
  // Join again on a booking's card: the booking once more, with the typed code (the agent takes it over the booking's own), so
  // the call keeps the booking's title. The link the card put back stays in the model for the old card a poll may still show.
  const joinEventAgain = (id, passcode) => act(() => post("/api/meeting/join", withPasscode({ event_id: id }, passcode)).then(() => joinAccepted(model.lastLink, id)));
  const leave = () => { forgetJoin(); return act(() => post("/api/meeting/leave")); };   // Cancel, Dismiss and Leave give the join up
  const toggle = (kind) => act(() => post("/api/meeting/" + kind));
  // The keyboard's key click is the room's setting (spec 2026-10-13, 3.3): the service keeps it, and every status carries it.
  const setKeyClick = (on) => act(() => post("/api/settings", { key_click: on }));
  const setScreensaver = (style) => act(() => post("/api/screensaver", { style: style }).then((data) => { model.screensaver = data.style; }));

  // A press on a button leaves the focus where it is. On the kiosk the keys go when the field loses it: the page shrinks, the
  // button moves from under the finger before it lifts, and the tap is lost.
  const keepFocus = (b) => b.addEventListener("pointerdown", (e) => e.preventDefault());

  function button(label, className, onClick, disabled, iconName) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "button " + className;
    labelWithIcon(b, label, iconName);
    b.disabled = Boolean(disabled) || model.busy;
    keepFocus(b);
    b.addEventListener("click", onClick);
    return b;
  }

  function setActions(specs, containerId, keyHolder) {
    // Replace the buttons only when their labels, icons, availability or target change, so a click in progress is never
    // lost to a re-render (the timer ticks every second), and a Join never acts on a booking it no longer shows (two
    // bookings back to back both say "Join now").
    const key = JSON.stringify(specs.map((s) => [s.label, s.className, Boolean(s.disabled) || model.busy, s.target || null, s.icon || null]));
    if (key === keyHolder.key) return;
    keyHolder.key = key;
    el(containerId).replaceChildren(...specs.map((s) => button(s.label, s.className, s.onClick, s.disabled, s.icon)));
  }
  const idleActionsKey = { key: null };
  const meetingActionsKey = { key: null };

  function leaveSpec() {
    if (model.confirmLeave) {
      return { label: "Tap again to leave", className: "danger", icon: "phone-off", onClick: () => {
        clearTimeout(confirmTimer);
        model.confirmLeave = false;
        leave();
      } };
    }
    return { label: "Leave", className: "danger", icon: "phone-off", onClick: () => {
      model.confirmLeave = true;
      clearTimeout(confirmTimer);
      confirmTimer = setTimeout(() => { model.confirmLeave = false; render(); }, CONFIRM_MS);
      render();
    } };
  }

  function meetingDetail(m) {
    return (m.title ? m.title + ", " : "") + platformName(m.platform) + (m.joined_at ? ", " + elapsedSince(m.joined_at) : "");
  }

  // The meeting screen's first line: which meeting the room is in, and why it may be waiting. The idle status card says
  // the same things (#headline, #detail) but is hidden in meeting mode. The elapsed time is the top bar's, so it is not here.
  function meetingLine(m, label) {
    if (m.state === "connected") return m.title && m.platform ? m.title + ", " + platformName(m.platform) : label;
    if (m.state === "joining") return "Joining " + label + "\nThe room's screen is connecting.";
    if (m.state === "in_lobby") return "Waiting for the host to let the room in.";
    if (m.state === "leaving") return "Leaving";
    return "";
  }

  // ===== The meeting screen, drawn from the control model (spec 2026-10-13, sections 3 and 6.1) =====
  // The agent describes what the table may do now (status.meeting.controls) and this draws exactly that: a control that is not
  // available is not drawn, and a button says what the platform has, never the last thing that was pressed. A status with no model
  // (a platform that has none yet) keeps the three buttons the screen began with. The icon shows the state and the label the action,
  // as on Mute and Camera. Every row is drawn with setActions and a key, so a button is not rebuilt under a finger.

  const REACTIONS_OPEN_MS = 10 * 1000;     // how long the reaction row stays in the secondary row's place
  const CONTROL_TIMEOUT_MS = 35 * 1000;    // the agent gives a control up to 30 s (Zoom's calls take seconds each, one at a time); the page waits a little longer
  const NO_ANSWER = "The room's screen did not answer in time; try again in a moment";

  // Zoom 6.5.0's reaction picker has no emoji (and the kiosk may have no colour-emoji font): the choices are Zoom's own names
  // (controls.reactions.choices), sent back as they came, and the table shows each in friendly words with a Lucide symbol. A name
  // that is not here is shown as itself, with the smile.
  const REACTIONS = {
    "Thumbs Up": { label: "Thumbs up", icon: "thumbs-up" },
    "Clap": { label: "Clap", icon: "hand" },
    "Heart": { label: "Heart", icon: "heart" },
    "Joy": { label: "Laugh", icon: "laugh" },
    "Open Mouth": { label: "Wow", icon: "smile" },
    "Tada": { label: "Celebrate", icon: "party-popper" },
  };
  const plainName = (name) => String(name).toLowerCase().replace(/[^a-z0-9]+/g, "");   // "Thumbs Up", "thumbs_up" and "THUMBSUP" are one name, as the agent compares them
  function reactionLook(name) {
    const zoom = Object.keys(REACTIONS).find((known) => plainName(known) === plainName(name));
    return zoom ? REACTIONS[zoom] : { label: String(name), icon: "smile" };
  }
  const reactionChoices = (c) => (c.reactions && c.reactions.available && Array.isArray(c.reactions.choices) ? c.reactions.choices : []);
  const reactionsOpen = () => Date.now() < model.reactionsUntil;
  function openReactions() { model.reactionsUntil = Date.now() + REACTIONS_OPEN_MS; render(); }
  function closeReactions() { model.reactionsUntil = 0; render(); }
  function pickReaction(name) { closeReactions(); control("reactions", name); }

  // What the three routes (control, dialog, chat/seen) answer with: the meeting block as it is after the request, the one the poll carries.
  // It goes into the model at once, with an epoch, so that a poll already on its way cannot put an older block back over it.
  function takeMeeting(answer) {
    if (answer && answer.meeting && model.status) { model.status.meeting = answer.meeting; deviceEpoch.meeting += 1; }
  }

  // A control from the table. It can take up to 30 s inside the agent, so it does not hold the whole page in model.busy as the quick
  // actions do: the button that was pressed is inert until its answer, every other button works, and the request gives up at 35 s.
  // A refusal comes in words, for #meeting-message.
  async function control(name, value) {
    if (model.pending.has(name)) return;     // one request per tap: the button is inert already, and this is the guard behind it
    model.pending.add(name);
    model.error = "";
    render();
    const cutoff = new AbortController();
    const timer = setTimeout(() => cutoff.abort(), CONTROL_TIMEOUT_MS);
    try {
      takeMeeting(await post("/api/meeting/control", { name: name, value: value }, { signal: cutoff.signal }));
    } catch (e) {
      model.error = e.name === "AbortError" ? NO_ANSWER : e.message;
    } finally {
      clearTimeout(timer);
      model.pending.delete(name);
    }
    render();
  }
  // One control's button: inert while that control's request is out.
  const controlSpec = (name, label, icon, className, value) =>
    ({ label: label, icon: icon, className: className, disabled: model.pending.has(name), onClick: () => control(name, value) });

  // The primary row: Mute, Camera, Raise hand, Reactions, Leave. Mute and Camera keep their own routes and their state in the block;
  // the rest are the model's, and are drawn when available.
  function meetingPrimary(m) {
    const c = m.controls || {};
    const specs = [
      { label: m.muted ? "Unmute" : "Mute", className: "", icon: m.muted ? "mic-off" : "mic", onClick: () => toggle("mute") },
      { label: m.camera_on ? "Turn camera off" : "Turn camera on", className: "", icon: m.camera_on ? "video" : "video-off", onClick: () => toggle("camera") },
    ];
    if (c.raise_hand && c.raise_hand.available) specs.push(controlSpec("raise_hand", c.raise_hand.on ? "Lower hand" : "Raise hand", "hand", ""));
    if (reactionChoices(c).length) specs.push({ label: "Reactions", icon: "smile", className: "", disabled: model.pending.has("reactions"), onClick: openReactions });
    specs.push(leaveSpec());
    return specs;
  }

  // The secondary row: Captions, View, Self view, Participants (with the count), Chat (with the unread count), Host (for a host or
  // co-host). Without a model there is nothing to draw it from, so none of it is drawn.
  function meetingSecondary(m) {
    const c = m.controls || {};
    if (!Object.keys(c).length) return [];
    const has = (name) => Boolean(c[name] && c[name].available);
    const specs = [];
    if (has("captions")) specs.push(controlSpec("captions", c.captions.on ? "Captions off" : "Captions on", "captions", "quiet"));
    if (has("layout")) {
      const gallery = c.layout.value === "gallery";
      specs.push(controlSpec("layout", gallery ? "Speaker view" : "Gallery view", "layout", "quiet", gallery ? "speaker" : "gallery"));
    }
    if (has("hide_self")) specs.push(controlSpec("hide_self", c.hide_self.on ? "Show self view" : "Hide self view", "user", "quiet"));
    if (m.participants && m.participants.count) specs.push({ label: "Participants (" + m.participants.count + ")", icon: "users", className: "quiet", onClick: () => setView("participants") });
    if (m.chat) specs.push({ label: "Chat" + (m.chat.unread ? " (" + m.chat.unread + ")" : ""), icon: "message", className: "quiet", onClick: () => setView("chat") });
    if (c.host && c.host.role && c.host.role !== "participant") specs.push({ label: "Host", icon: "lock", className: "quiet", onClick: () => setView("host") });
    return specs;
  }

  // The reaction row: a div in the secondary row's place while it is open (the row is not in the page when it is closed), one button per
  // choice, and Cancel.
  const secondaryKey = { key: null };
  const reactionKey = { key: null };
  function renderReactionRow(choices) {
    let row = el("reaction-row");
    if (!choices) {
      if (row) row.remove();
      reactionKey.key = null;
      return;
    }
    if (!row) {
      row = document.createElement("div");
      row.id = "reaction-row";
      row.className = "actions meeting-secondary";
      row.setAttribute("role", "group");
      row.setAttribute("aria-label", "Reactions");
      el("meeting-secondary").before(row);
      reactionKey.key = null;
    }
    const specs = choices.map((name) => {
      const look = reactionLook(name);
      return { label: look.label, icon: look.icon, className: "quiet", target: name, onClick: () => pickReaction(name) };
    });
    specs.push({ label: "Cancel", icon: "x-circle", className: "quiet", onClick: closeReactions });
    setActions(specs, "reaction-row", reactionKey);
  }

  // Everything of the meeting screen but the primary row, from the same status: the notice line under the title, the dialog's card, the
  // secondary row, and the reaction row in its place while it is open. Only a connected meeting has any of it; m is null offline.
  function renderMeetingScreen(m) {
    const live = Boolean(m) && m.state === "connected";
    el("meeting-notice").textContent = live ? m.notice || "" : "";
    el("meeting-dialog").hidden = !(live && m.dialog);
    const choices = live ? reactionChoices(m.controls || {}) : [];
    const picking = choices.length > 0 && reactionsOpen();
    setActions(live ? meetingSecondary(m) : [], "meeting-secondary", secondaryKey);
    el("meeting-secondary").hidden = picking;
    renderReactionRow(picking ? choices : null);
  }
  // ===== end of the meeting screen =====

  // A meeting began or ended: land on the new mode's main screen. What the last action said (a refusal on one mode's
  // screen is not news on the other's) and a Leave still waiting for its second tap belong to the mode that is over.
  function landOnMode() {
    model.error = "";
    model.confirmLeave = false;
    model.reactionsUntil = 0;
    clearTimeout(confirmTimer);
    if (mode() === "meeting") { linkInput.value = ""; showPasscodeField(false); }   // the idle screen's boxes were for the call that has begun
    applyView(MAIN_SCREEN[mode()], true);
  }

  function joinWindow(ev) {
    const start = new Date(ev.start_time).getTime();
    const end = new Date(ev.end_time).getTime();
    const now = Date.now();
    return { open: now >= start - JOIN_WINDOW_MS && now < end, opensAt: new Date(start - JOIN_WINDOW_MS), past: now >= end };
  }

  const WARN_BEFORE_MS = 10 * 60 * 1000;
  const STATE_LABELS = { joining: "Joining", in_lobby: "In the lobby", connected: "In a meeting", leaving: "Leaving" };

  // What is still to come today. In meeting mode the booking the room joined from its window has not started yet, and
  // must not show as "next" to the call that is in it: the status carries that meeting's title, so a booking with the
  // same title is left out. A meeting joined by link has no title and leaves nothing out.
  function upcoming() {
    const now = Date.now();
    const title = mode() === "meeting" ? model.status.meeting.title : "";
    return model.events.filter((ev) => new Date(ev.start_time).getTime() > now && !(title && ev.title === title));
  }

  const rowFields = (ev) => [ev.id, ev.title, ev.start_time, ev.joinable, ev.meeting_platform];   // what eventItem reads: the key of either list covers it

  // The booking the passcode card is for: the one this page joined, else (a join from another page) the one the status names
  // by its title. Its row's Join would send the booking alone, without the code typed on the card; Join again is the retry.
  function cardIsFor(ev) {
    if (!passcodeCard) return false;
    const title = model.status.meeting.title;
    return model.lastEvent ? ev.id === model.lastEvent : Boolean(title) && ev.title === title;
  }

  // The top bar in meeting mode: what the call is doing, for how long, and a warning when the next booking is close.
  function renderMeetingBar(m) {
    const inMeeting = mode() === "meeting";
    el("meeting-state").hidden = !inMeeting;
    el("meeting-elapsed").hidden = !(inMeeting && m.state === "connected" && m.joined_at);
    el("meeting-state").textContent = STATE_LABELS[m.state] || "";
    el("meeting-elapsed").textContent = m.joined_at ? elapsedSince(m.joined_at) : "";
    const next = upcoming()[0];
    const soon = inMeeting && next && new Date(next.start_time).getTime() - Date.now() <= WARN_BEFORE_MS;
    el("next-warning").hidden = !soon;
    el("next-warning").textContent = soon ? "Next: " + fmtTime(new Date(next.start_time)) + " " + next.title : "";
  }

  // One booking's row, for the Today list and the Upcoming list alike, so the two cannot drift: its time, its title and,
  // when it has a link, the platform. The Today list passes its Join button as withJoin; the Upcoming list passes none.
  function eventItem(ev, withJoin) {
    const li = document.createElement("li");
    li.className = "event";
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
    if (withJoin) li.append(withJoin);
    return li;
  }

  function renderUpcoming() {
    // Rebuilt only when what a row shows changes, so the list keeps its scroll position while the page polls.
    const items = upcoming();
    const key = JSON.stringify(items.map(rowFields));
    if (key === lastUpcomingKey) return;
    lastUpcomingKey = key;
    el("upcoming-empty").hidden = items.length > 0;
    el("upcoming-list").replaceChildren(...items.map((ev) => eventItem(ev)));
  }

  function render() {
    const body = document.body;
    if (!model.offline && model.status && viewMode(view()) !== mode()) landOnMode();   // before the error lines below are written
    el("message").textContent = model.error;
    el("meeting-message").textContent = model.error;   // the meeting screen's own line for a refusal or an error

    if (model.offline || !model.status) {
      body.dataset.state = "offline";
      el("offline-note").hidden = !model.offline;   // in the top bar, so every screen says it (not while still loading)
      el("kicker").textContent = "Not connected";
      el("headline").textContent = "Can't reach the room";
      el("detail").textContent = "Check that Crystal Meet is running on the room's device, then this page will reconnect on its own.";
      setActions([], "actions", idleActionsKey);
      setActions([], "meeting-actions", meetingActionsKey);
      renderMeetingScreen(null);
      document.querySelector(".screen-section").hidden = true;
      document.querySelector(".sound-section").hidden = true;
      document.querySelector(".camera-section").hidden = true;
      document.querySelector(".keyboard-section").hidden = true;
      return;
    }
    document.querySelector(".screen-section").hidden = false;
    document.querySelector(".sound-section").hidden = false;
    document.querySelector(".camera-section").hidden = false;
    document.querySelector(".keyboard-section").hidden = false;

    el("offline-note").hidden = true;
    const s = model.status;
    const m = s.meeting;
    const cal = s.calendar;
    const asking = m.state === "error" && (m.error || "").startsWith(PASSCODE_ASKED);
    if (m.state !== lastMeetingState) {
      // A join that failed while someone was on Options: the "Couldn't join" card is on the idle screen.
      if (m.state === "error" && view() !== "idle") applyView("idle", true);
      // The join resolved: it took, is being left, failed some other way, or was given up on another page (joining, or the
      // card for a refusal, then idle), and its link is spent. Not while it is going on, nor on "idle" after anything else (an
      // accepted join that has not begun yet), nor when Zoom asks for the passcode (the card gives the link back). Only a change
      // counts: the poll just after a retry may still show the old card, and that is no news.
      const cancelled = m.state === "idle" && (lastMeetingState === "joining" || lastMeetingState === "error");
      if (m.state === "in_lobby" || m.state === "connected" || m.state === "leaving" || (m.state === "error" && !asking) || cancelled) forgetJoin();
      lastMeetingState = m.state;
    }
    const landing = asking && !passcodeCard;
    if (asking) askForPasscode(m, landing);
    else if (passcodeCard) passcodeCardGone();
    passcodeCard = asking;
    renderMeetingBar(m);
    renderUpcoming();
    el("open-options").hidden = mode() !== "idle";
    el("room-name").textContent = s.room.name;
    el("room-location").textContent = s.room.location;
    el("room-location").hidden = !s.room.location;
    const label = m.title || (m.platform ? platformName(m.platform) + " meeting" : "the meeting");
    el("meeting-detail").textContent = meetingLine(m, label);

    let specs = [];
    if (m.state === "joining" || m.state === "in_lobby" || m.state === "leaving") {
      body.dataset.state = "joining";
      el("kicker").textContent = m.state === "leaving" ? "Leaving" : "Joining";
      el("headline").textContent = m.state === "leaving" ? "Leaving" : "Joining " + label;
      el("detail").textContent = m.state === "in_lobby" ? "Waiting for the host to let the room in." : "The room's screen is connecting.";
      if (m.state !== "leaving") specs = [{ label: "Cancel", className: "quiet", icon: "x-circle", onClick: leave }];
    } else if (m.state === "connected") {
      body.dataset.state = "meeting";
      el("kicker").textContent = "In a meeting";
      el("headline").textContent = "In a meeting";
      el("detail").textContent = meetingDetail(m);
      specs = meetingPrimary(m);
    } else if (m.state === "error") {
      body.dataset.state = "error";
      el("kicker").textContent = "Couldn't join";
      el("headline").textContent = "Couldn't join " + label;
      el("detail").textContent = m.error || "The room's screen could not join. Try again, or join from a different link.";
      specs = [{ label: "Dismiss", className: "quiet", icon: "x-circle", onClick: leave }];
      if (asking) specs.push({ label: "Join again", className: "primary", icon: "video", onClick: joinAgain });
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
    renderMeetingScreen(mode() === "meeting" ? m : null);

    renderEvents(cal);
    renderKeyClick(s);
    renderPicker();
    for (const fn of renderHooks) fn(s);
    if (landing && kiosk && view() === "idle") focusPasscode();
  }

  // Options > Keyboard: the two buttons show the room's setting, on until a status says otherwise (keyboard.js reads the same one).
  function renderKeyClick(s) {
    const on = !s.settings || s.settings.key_click !== false;
    el("key-click-on").setAttribute("aria-pressed", on ? "true" : "false");
    el("key-click-off").setAttribute("aria-pressed", on ? "false" : "true");
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
      return current.joinable ? [{ label: "Join now", className: "primary", icon: "video", onClick: () => joinEvent(current.id), target: current.id }] : [];
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
      return [{ label: window.open ? "Join now" : "Join opens at " + fmtTime(window.opensAt), className: "primary", icon: "video", onClick: () => joinEvent(next.id), disabled: !window.open, target: next.id }];
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
      model.events.map((ev) => [...rowFields(ev), joinWindow(ev).open, joinWindow(ev).past, cardIsFor(ev)])]);
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
      const canJoin = ev.joinable && window.open && (!model.status || model.status.meeting.state === "idle" || model.status.meeting.state === "error");
      const li = eventItem(ev, canJoin ? button("Join", "primary", () => joinEvent(ev.id), cardIsFor(ev), "video") : null);
      li.classList.toggle("past", window.past);
      li.classList.toggle("now", Boolean(cal.current && cal.current.id === ev.id));
      list.append(li);
    }
  }

  function tick() {
    el("clock").textContent = fmtTime(new Date());
    el("date").textContent = fmtDate(new Date());
    if (model.status && model.status.meeting.state === "connected") {
      el("detail").textContent = meetingDetail(model.status.meeting);
      if (model.status.meeting.joined_at) el("meeting-elapsed").textContent = elapsedSince(model.status.meeting.joined_at);
    }
    if (model.reactionsUntil && !reactionsOpen()) closeReactions();   // the row's ten seconds are over
  }

  el("link-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const passcode = passcodeInput.hidden ? "" : passcodeInput.value.trim();
    if (passcodeCard && model.lastEvent) { joinEventAgain(model.lastEvent, passcode); return; }   // a booking's card: the booking again
    const typed = linkInput.value.trim();
    if (!typed) return;
    const url = meetingId(typed) || typed;   // the agent takes an ID as digits only
    joinLink(url, passcode);   // the boxes clear in joinAccepted, once the agent has taken the join
  });

  keepFocus(el("link-form").querySelector("button[type=submit]"));   // Join, beside the link box

  // The buttons in index.html name their icon in data-icon (every Back is the arrow); each gets it once, here, with the label the
  // markup gives it.
  for (const b of document.querySelectorAll("button[data-icon], button.back")) labelWithIcon(b, b.textContent.trim(), b.dataset.icon || "back");

  el("open-options").addEventListener("click", () => setView("options"));
  el("back-from-options").addEventListener("click", () => setView("idle"));
  el("open-camera-setup").addEventListener("click", () => setView("camera"));
  el("open-screensaver").addEventListener("click", () => setView("screensaver"));
  el("back-from-screensaver").addEventListener("click", () => setView("options"));
  el("back-from-camera").addEventListener("click", () => setView(cameraMode === "meeting" ? "meeting" : "options"));
  el("open-camera-live").addEventListener("click", () => setView("camera"));
  el("open-upcoming").addEventListener("click", () => setView("upcoming"));
  el("back-from-upcoming").addEventListener("click", () => setView("meeting"));
  for (const name of ["participants", "chat", "host"]) el("back-from-" + name).addEventListener("click", () => setView("meeting"));
  el("key-click-on").addEventListener("click", () => setKeyClick(true));
  el("key-click-off").addEventListener("click", () => setKeyClick(false));

  window.croomPage = {
    el: el, model: model, api: api, post: post, act: act, button: button, render: render,
    iconsReady: iconsReady, icon: icon, labelWithIcon: labelWithIcon,
    refreshStatus: refreshStatus, deviceEpoch: deviceEpoch, IN_MEETING: IN_MEETING, submitForm: submitForm,
    view: view, setView: setView, mode: mode, cameraMode: () => cameraMode, kiosk: kiosk,
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
