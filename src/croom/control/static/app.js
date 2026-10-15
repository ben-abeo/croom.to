(function () {
  "use strict";

  const JOIN_WINDOW_MS = 10 * 60 * 1000;
  const STATUS_EVERY_MS = 2000;
  const EVENTS_EVERY_MS = 30000;
  const CONFIRM_MS = 5000;
  const IN_MEETING = ["joining", "in_lobby", "connected", "leaving"];   // the meeting states in which the room is taken
  // Screens (spec 2026-10-12): body[data-view] names the one shown. Each view belongs to a mode; the camera and TV views
  // belong to whichever mode showed them, so their Back returns there.
  const VIEW_MODES = { idle: "idle", options: "idle", screensaver: "idle", "door-sign": "idle", meeting: "meeting", upcoming: "meeting", participants: "meeting", chat: "meeting", host: "meeting" };
  const MAIN_SCREEN = { idle: "idle", meeting: "meeting" };
  const SUB_SCREENS = ["participants", "chat", "host", "tv"];   // the screens that go back to the meeting screen after two idle minutes (armIdleReturn): its own, each with its Back, and the TV view when opened from it

  const el = (id) => document.getElementById(id);
  // lastLink: the link of the join the page last had accepted, until that join resolves (see render) or the person has the box
  // for something else. Zoom's refusal over the passcode comes later, in the status, and the card gives that link back.
  // lastEvent: the booking, when that join was a booking's, for the same time: Join again on its card joins the booking again.
  // reactionsUntil: when the reaction row closes (a time, 0 for closed). ignoreUntil, dialogIgnoreUntil: until when the secondary row's buttons and the
  // dialog card's ignore a tap. pending: the requests of the meeting screen that are out, by name (a control's, or "dialog"). confirmEnd, confirmRemove:
  // End meeting for everyone, and Remove on the row of the person with that key, have been asked about and wait for their second tap. errorOn: the
  // view the refusal in `error` was met on (a sub-screen says the refusals of its own controls, and not the meeting screen's).
  const model = { status: null, events: [], offline: false, busy: false, error: "", errorOn: "", confirmLeave: false, confirmEnd: false, confirmRemove: null,
                  screensaver: null, settings: null, lastLink: "", lastEvent: "", reactionsUntil: 0, ignoreUntil: 0, dialogIgnoreUntil: 0, pending: new Set() };
  const renderHooks = [];   // devices.js and remote.js register their rendering here; render() calls each with the status
  const viewHooks = [];     // devices.js, keyboard.js and remote.js register here; called after every view change
  let cameraMode = "idle";  // the mode that opened the camera view
  let tvMode = "idle";      // the mode that opened the TV view
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
  const viewMode = (name) => (name === "camera" ? cameraMode : name === "tv" ? tvMode : VIEW_MODES[name]);

  function applyView(name, automatic) {
    if (name === "camera") cameraMode = mode();
    if (name === "tv") tvMode = mode();
    const was = view();
    document.body.dataset.view = name;
    if (was !== name) leftView(was);
    if (name === "chat") chatOpened = true;
    // Every view change, an automatic one too, starts or clears the wait. Before the hooks: a hook may start a timer of
    // its own (the preview's 30 s renewal), and when the two fall due together they run in the order they were set, so
    // the return goes first and the hook that ends the preview clears the renewal before it is sent.
    armIdleReturn();
    for (const fn of viewHooks) fn(name, Boolean(automatic));
  }
  function setView(name, automatic) {
    if (view() === name) return;
    // A finger aimed at the screen that was lands on the one that is (the second tap of a double tap on Host lands on Mute all; on Back, at the
    // kiosk's 1280 by 800, on Mute, which begins under Back's lower edge, from Camera and Upcoming as much as from the three sub-screens): every
    // view change in a meeting settles the page, as the meeting screen's own moves do. Idle mode has nothing under Back's edge to be hit.
    if (mode() === "meeting") settle();
    applyView(name, automatic);
    render();
  }

  // Idle mode returns to the idle screen on its own: the kiosk never reloads and nobody presses Back, so a room left on
  // Camera framing would keep the TV on the camera for good. Any tap restarts the wait; a meeting screen never returns, except that the
  // sub-screens opened from it (Participants, Chat, Host: glances, and the meeting screen is the one a room needs) and the TV view (the
  // remote is a quick look in a call) go back to it after two minutes. Upcoming and Camera, opened from the same screen, are as they
  // were: they stay. The full-screen touchpad (touchpad.js) holds the wait while it is up: a person moving the TV's cursor touches the
  // surface, not the page, and the surface has a wait of its own; the page's begins again when it goes away.
  const IDLE_RETURN_MS = 10 * 60 * 1000;
  const MEETING_RETURN_MS = 2 * 60 * 1000;
  let idleReturnTimer = null;
  let idleReturnHeld = false;
  function armIdleReturn() {
    clearTimeout(idleReturnTimer);
    idleReturnTimer = null;
    if (idleReturnHeld) return;
    if (mode() === "idle" && view() !== "idle") idleReturnTimer = setTimeout(() => setView("idle", true), IDLE_RETURN_MS);
    else if (mode() === "meeting" && SUB_SCREENS.includes(view())) idleReturnTimer = setTimeout(() => setView("meeting", true), MEETING_RETURN_MS);
  }
  document.addEventListener("pointerdown", armIdleReturn, true);

  let confirmTimer = null;
  // A second tap within half a second of the ask is not the one that was asked for: the bounce of a finger, or the second of a double tap, on a
  // button whose words have just changed under it. Leave, End meeting for everyone and Remove are the buttons that ask; each has its own time.
  const ARMED_BOUNCE_MS = 500;
  const askedAt = { leave: 0, end: 0, remove: 0 };
  const bounced = (which) => Date.now() < askedAt[which] + ARMED_BOUNCE_MS;
  let lastEventsKey = null;
  let lastPickerKey = null;
  let lastDoorSignKey = null;
  let lastUpcomingKey = null;
  const deviceEpoch = { audio: 0, camera: 0, meeting: 0, tv: 0 };   // counts each device's answers, the meeting's and the TV's, so a poll already on its way cannot undo one

  // The TV's idle styles, in the order the service lists them (spec 2026-10-07 TV, section 4.2).
  const STYLE_LABELS = { info: "Information", quiet: "Quiet", brand: "Brand", bounce: "Bounce" };

  // The door sign's room settings (spec 2026-10-13): its backgrounds in the order the room page offers them, and the schools
  // whose football scores it may show. The values are the service's keys; the labels are what the screen says.
  const SIGN_BACKGROUNDS = [["skyline", "Austin skyline"], ["capitol", "Capitol"], ["tower", "UT Tower"], ["bridge", "Pennybacker Bridge"],
    ["pool", "Hamilton Pool"], ["star", "Texas star"], ["hills", "Hill Country"], ["stadium", "DKR stadium"], ["plain", "Plain"]];
  const SIGN_SCHOOLS = [["texas", "Texas"], ["michigan", "Michigan"], ["texas_am", "Texas A&M"], ["missouri", "Missouri"]];

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
      if (model.status.settings) model.settings = model.status.settings;
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
    const on = view();
    model.busy = true;
    model.error = "";
    render();
    try {
      await request();
    } catch (e) {
      model.error = e.message;
      model.errorOn = on;
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
  // The keyboard's key click is the room's setting (spec 2026-10-13, 3.3): the service keeps it, and every status carries it.
  const setKeyClick = (on) => act(() => post("/api/settings", { key_click: on }));
  const setScreensaver = (style) => act(() => post("/api/screensaver", { style: style }).then((data) => { model.screensaver = data.style; }));
  const setSettings = (changes) => act(() => post("/api/settings", changes).then((data) => { model.settings = data; }));   // the answer is every setting

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

  // A button kept where it stands: its words and symbol in place (labelWithIcon), its class, and whether it can be pressed.
  function patchButton(b, s) {
    labelWithIcon(b, s.label, s.icon);
    b.className = "button " + s.className;
    b.disabled = Boolean(s.disabled) || model.busy;
  }

  function setActions(specs, containerId, keyHolder) {   // containerId: the id of the row's element, or the element itself (a participant's row is not in the page by id)
    // Draw the buttons again only when their labels, icons, availability or target change, so a click in progress is never
    // lost to a re-render (the timer ticks every second), and a Join never acts on a booking it no longer shows (two
    // bookings back to back both say "Join now"). A row whose specs carry an id (which button each is) and are the same
    // buttons in the same order as the row on the screen is patched where it stands: another button's words changed (a
    // chat message came in), and a finger that is down on this one must still find it at the lift, or the browser drops the
    // tap. A row whose set or order of buttons changes is built again. A tap goes to the spec the row is drawn from now
    // (keyHolder.specs), not to the one its button was drawn with, so a button that is kept runs what it says now, unless its
    // words changed after the finger went down (below).
    const key = JSON.stringify(specs.map((s) => [s.label, s.className, Boolean(s.disabled) || model.busy, s.target || null, s.icon || null]));
    keyHolder.specs = specs;
    if (key === keyHolder.key) return;
    keyHolder.key = key;
    const container = typeof containerId === "string" ? el(containerId) : containerId;
    const ids = specs.every((s) => s.id) ? specs.map((s) => s.id) : null;
    if (ids && JSON.stringify(ids) === JSON.stringify(keyHolder.ids) && container.children.length === specs.length) {
      specs.forEach((s, i) => patchButton(container.children[i], s));
      return;
    }
    keyHolder.ids = ids;
    container.replaceChildren(...specs.map((s, i) => {
      // The lift runs what the button says now, but only if it says what it did when the finger went down: a host muting the room as a
      // thumb comes down on Mute turns the button into Unmute, and the lift must not unmute the room. (A click with no press before it, a
      // key on a keyboard, has no words to compare.)
      let pressed;
      const b = button(s.label, s.className, (e) => {
        const now = keyHolder.specs[i], was = pressed;
        pressed = undefined;
        if (now && (was === undefined || was === now.label)) now.onClick(e);
      }, s.disabled, s.icon);
      b.addEventListener("pointerdown", () => { const now = keyHolder.specs[i]; pressed = now ? now.label : undefined; });
      b.addEventListener("pointercancel", () => { pressed = undefined; });
      return b;
    }));
  }
  const idleActionsKey = { key: null };
  const meetingActionsKey = { key: null };

  function leaveSpec() {
    if (model.confirmLeave) {
      return { id: "leave", label: "Tap again to leave", className: "danger", icon: "phone-off", onClick: () => {
        if (bounced("leave")) return;
        clearTimeout(confirmTimer);
        model.confirmLeave = false;
        leave();
      } };
    }
    return { id: "leave", label: "Leave", className: "danger", icon: "phone-off", onClick: () => {
      askedAt.leave = Date.now();
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
  // as on Mute and Camera. Every row is drawn with setActions and a key (and its specs' ids), so a button is not rebuilt, or
  // replaced, under a finger.

  const REACTIONS_OPEN_MS = 10 * 1000;     // how long the reaction row stays in the secondary row's place
  const UNCOVERED_TAP_MS = 500;            // buttons that have just come up under a finger ignore a tap this long: the secondary row's, uncovered by a reaction row that closed, the dialog card's, and the whole page's while it settles (settle); the second tap of a double tap
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

  // A request of the meeting screen: a control from the table, or the answer to a dialog. It can take up to 30 s inside the agent, so it does
  // not hold the whole page in model.busy as the quick actions do: the button that was pressed is inert until its answer (pending, by
  // key), every other button works, and the request gives up at 35 s. A refusal comes in words, for #meeting-message (or the dialog
  // card's own line while a card shows).
  async function request(key, path, body) {
    if (model.pending.has(key)) return;     // one request per tap: the button is inert already, and this is the guard behind it
    const on = view();                      // the screen the tap was made on: a refusal that comes later belongs to it
    model.pending.add(key);
    model.error = "";
    render();
    const cutoff = new AbortController();
    const timer = setTimeout(() => cutoff.abort(), CONTROL_TIMEOUT_MS);
    try {
      takeMeeting(await post(path, body, { signal: cutoff.signal }));
    } catch (e) {
      model.error = e.name === "AbortError" ? NO_ANSWER : e.message;
      model.errorOn = on;
    } finally {
      clearTimeout(timer);
      model.pending.delete(key);
    }
    render();
  }
  const control = (name, value) => request(name, "/api/meeting/control", { name: name, value: value });
  // Mute and Camera keep their own routes, and send the state the button names: a tap on Mute asks for muted, on Turn camera off for off, whatever the
  // room is by the time the request arrives (the host may have muted it, or the camera gone off at the TV, since the poll that drew the button), so a
  // tap made a moment late ends as the person asked and cannot undo what someone else just did. The agent sets that state. They are requests of the
  // meeting screen like the rest: only the button pressed waits for its answer, and a camera that takes twenty seconds to switch holds nothing else.
  const setMute = (muted) => request("mute", "/api/meeting/mute", { muted: muted });
  const setCamera = (on) => request("camera", "/api/meeting/camera", { on: on });
  // One control's button: inert while that control's request is out.
  const controlSpec = (name, label, icon, className, value) =>
    ({ id: name, label: label, icon: icon, className: className, disabled: model.pending.has(name), onClick: () => control(name, value) });

  // The primary row: Mute, Camera, Raise hand, Reactions, Leave. Mute and Camera keep their own routes and their state in the block;
  // the rest are the model's, and are drawn when available.
  function meetingPrimary(m) {
    const c = m.controls || {};
    const specs = [
      { id: "mute", label: m.muted ? "Unmute" : "Mute", className: "", icon: m.muted ? "mic-off" : "mic", disabled: model.pending.has("mute"), onClick: () => setMute(!m.muted) },
      { id: "camera", label: m.camera_on ? "Turn camera off" : "Turn camera on", className: "", icon: m.camera_on ? "video" : "video-off", disabled: model.pending.has("camera"), onClick: () => setCamera(!m.camera_on) },
    ];
    if (c.raise_hand && c.raise_hand.available) specs.push(controlSpec("raise_hand", c.raise_hand.on ? "Lower hand" : "Raise hand", "hand", ""));
    if (reactionChoices(c).length) specs.push({ id: "reactions", label: "Reactions", icon: "smile", className: "", disabled: model.pending.has("reactions"), onClick: openReactions });
    specs.push(leaveSpec());
    return specs;
  }

  // The secondary row: Captions, View, Self view, Participants (with the count), Chat (with the unread count), Host (for a host or
  // co-host), and the TV's remote, the table's own button and the last of them. Without a model there is nothing to draw it from, so none
  // of it is drawn.
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
    // A platform that reports a count and no roster (Meet) has no Participants screen to open, and one whose chat the agent does not read says so
    // (controls.chat.available is false): a button that opens nothing is not drawn. Zoom's model has no chat key, and keeps its button.
    const people = m.participants && Array.isArray(m.participants.list) ? m.participants.list : [];
    if (people.length) specs.push({ id: "participants", label: "Participants (" + (m.participants.count || people.length) + ")", icon: "users", className: "quiet", onClick: () => setView("participants") });
    if (m.chat && !(c.chat && c.chat.available === false)) specs.push({ id: "chat", label: "Chat" + (m.chat.unread ? " (" + m.chat.unread + ")" : ""), icon: "message", className: "quiet", onClick: () => setView("chat") });
    if (c.host && c.host.role && c.host.role !== "participant") specs.push({ id: "host", label: "Host", icon: "lock", className: "quiet", onClick: () => setView("host") });
    // The TV is the room's, not the platform's: it is drawn with the row, after Host, whatever the TV's state (a room with no TV set up, or a TV not
    // yet paired, finds its cards in the view that this opens).
    specs.push({ id: "tv", label: "TV", icon: "tv", className: "quiet", onClick: () => setView("tv") });
    // A reaction row that closes uncovers these buttons under the finger that picked: the second tap of a double tap (or a tap at the
    // moment the row lapses) is not meant for them.
    return specs.map((spec) => Object.assign({}, spec, { onClick: () => { if (Date.now() >= model.ignoreUntil) spec.onClick(); } }));
  }

  // The reaction row: a div in the secondary row's place while it is open (the row is not in the page when it is closed), one button per
  // choice, and Cancel.
  const secondaryKey = { key: null };
  let shownReactionRow = false;
  const reactionKey = { key: null };
  function renderReactionRow(choices) {
    let row = el("reaction-row");
    if (!choices) {
      if (row) row.remove();
      reactionKey.key = reactionKey.ids = null;
      return;
    }
    if (!row) {
      row = document.createElement("div");
      row.id = "reaction-row";
      row.className = "actions meeting-secondary";
      row.setAttribute("role", "group");
      row.setAttribute("aria-label", "Reactions");
      el("meeting-secondary").before(row);
      reactionKey.key = reactionKey.ids = null;           // a row that is new has no buttons to patch
    }
    const specs = choices.map((name) => {
      const look = reactionLook(name);
      return { id: "reaction:" + name, label: look.label, icon: look.icon, className: "quiet", target: name, onClick: () => pickReaction(name) };
    });
    specs.push({ id: "cancel", label: "Cancel", icon: "x-circle", className: "quiet", onClick: closeReactions });
    setActions(specs, "reaction-row", reactionKey);
  }

  // The dialog card (spec 2026-10-13, section 5). A dialog Zoom drew that the agent did not answer by its policy waits for a tap on the
  // table: the card shows its title, its text and one button for each of the client's, in the client's order. Zoom 6.5.0 lists the cancel
  // first and the affirmative last ("Leave Meeting", "Got it"), so the last button is the primary one (the spec said the first, for an
  // order it had guessed). The card's buttons carry no icon, the one exception to an icon on every button: the words are the client's own,
  // and the sprite has no symbol for a label that is not known. Each button's id holds the dialog's number and its label, so that a card
  // whose text changes under a finger keeps its buttons, and a dialog that takes another's place does not give a finger that is down on the
  // old one a button of the new.
  const dialogKey = { key: null };
  let shownDialog = "";   // the dialog the card showed at the last render, by its number and its labels ("" for none)
  const answerDialog = (id, label) => request("dialog", "/api/meeting/dialog", { id: id, button: label });
  const dialogTitle = (d) => (String(d.title || "").trim() ? String(d.title) : "Zoom asks");   // only a title that is empty gets the fallback

  function renderDialog(d) {
    const labels = d && Array.isArray(d.buttons) ? d.buttons : [];
    const shown = d ? JSON.stringify([d.id, labels]) : "";
    if (shown !== shownDialog) {
      // A card that comes up, or whose buttons change, is under a finger that was meant for what it covers (its buttons land where the
      // primary row was, and Zoom's consent dialog has "Leave Meeting" first) or for the dialog before it: its buttons ignore a tap for half a
      // second, as the ones a closing reaction row uncovers do. A card that goes, or gives way to another dialog, takes with it a refusal it
      // was showing: that was said about the dialog it had.
      if (shown) {
        model.dialogIgnoreUntil = Date.now() + UNCOVERED_TAP_MS;
        // The card is on the meeting screen, and a person on one of the screens opened from it cannot see it while the agent waits for a tap: back to the
        // meeting screen. On this edge only (a dialog that comes up, or whose number or buttons change), never at every poll while the same one goes on
        // waiting: an unwanted dialog must not trap the person there. One that goes by itself moves nobody.
        if (view() !== "meeting") { applyView("meeting", true); settle(); }
      }
      if (shownDialog) model.error = "";
      shownDialog = shown;
    }
    el("meeting-dialog").hidden = !d;
    el("dialog-title").textContent = d ? dialogTitle(d) : "";
    const text = d ? String(d.text || "") : "";
    el("dialog-text").textContent = text;
    el("dialog-text").title = text;   // the whole of it, for when the card cuts it short (read by nobody on a kiosk, and harmless)
    // While a card shows, a refusal is said in the card (almost always the dialog's own) and not under the title as well: the two would stack.
    el("dialog-message").textContent = d ? model.error : "";
    el("meeting-message").textContent = d ? "" : model.error;
    setActions(labels.map((label, i) => ({
      id: d.id + ":" + label, label: label, className: i === labels.length - 1 ? "primary" : "quiet", disabled: model.pending.has("dialog"),
      target: String(d.id),   // in the key too: the buttons of another dialog are other buttons, though their words are the same
      onClick: () => { if (Date.now() >= model.dialogIgnoreUntil) answerDialog(d.id, label); },
    })), "dialog-actions", dialogKey);
  }

  // Everything of the meeting screen but the primary row, from the same status: the notice line under the title, the dialog's card, the
  // secondary row, and the reaction row in its place while it is open. Only a connected meeting has any of it; m is null offline.
  function renderMeetingScreen(m) {
    const live = Boolean(m) && m.state === "connected";
    const notice = live ? m.notice || "" : "";
    el("meeting-notice").textContent = notice;
    el("meeting-notice").title = notice;   // the whole of it, for when the line cuts it short at two lines (read by nobody on a kiosk, and harmless)
    renderDialog(live ? m.dialog : null);
    const choices = live ? reactionChoices(m.controls || {}) : [];
    const picking = choices.length > 0 && reactionsOpen();
    if (shownReactionRow && !picking) model.ignoreUntil = Date.now() + UNCOVERED_TAP_MS;
    shownReactionRow = picking;
    setActions(live ? meetingSecondary(m) : [], "meeting-secondary", secondaryKey);
    el("meeting-secondary").hidden = picking;
    renderReactionRow(picking ? choices : null);
  }
  // The screen's controls are under a finger that was aimed at where they were: when the screen's layout shifts (a dialog's card comes up
  // or goes, a notice or a refusal appears, the secondary row wraps, the Sound strip comes or goes, a person above the one a finger is aimed at
  // leaves the Participants list) everything below the shift is somewhere else, and a tap that was on its way lands on the wrong control (Captions
  // on Mute, a button on the volume slider, one person's Mute on another's). The page ignores taps for half a second after it moves (body.settling,
  // in style.css; each move starts the half second again), and setView starts it for a change into or out of a sub-screen. The primary row and the
  // menu at its foot are where a shift above or between them shows on the meeting screen, and the rows of the people on the Participants screen,
  // each by the person. A drag already under way is not cut short (the browser keeps it), and a scroll is not a move: the positions are taken in
  // the page, whatever its scroll and the list's. Called at the end of render(), when every renderer has had its say.
  let lastPlaces = null, placesOf = "", settleTimer = null;
  function settle() {
    document.body.classList.add("settling");
    clearTimeout(settleTimer);
    settleTimer = setTimeout(() => document.body.classList.remove("settling"), UNCOVERED_TAP_MS);
  }
  function guardLayoutShift() {
    const here = view();
    if (here !== "meeting" && here !== "participants") { lastPlaces = null; return; }
    const main = document.querySelector("main");
    const at = (node, scrolled) => node.getBoundingClientRect().top + window.scrollY + main.scrollTop + (scrolled ? scrolled.scrollTop : 0);   // where it is in the page, whatever the scroll
    const places = new Map();
    if (here === "meeting") {
      places.set("primary", at(el("meeting-actions")));
      places.set("menu", at(document.querySelector('.view[data-view="meeting"] .menu')));
    } else {
      const list = document.querySelector(".view[data-view=participants] .list-scroll");
      for (const [key, row] of rosterRows.people) places.set("in:" + key, at(row.li, list));
      for (const [key, row] of rosterRows.waiting) places.set("waiting:" + key, at(row.li, list));
    }
    // Only rows that were there before and are there now can have moved: one that came in or went is not a move of its own, but of those below it.
    if (lastPlaces && placesOf === here && [...places].some(([key, top]) => lastPlaces.has(key) && Math.abs(top - lastPlaces.get(key)) > 6)) settle();
    lastPlaces = places;
    placesOf = here;
  }
  // ===== end of the meeting screen =====

  // ===== The Participants, Chat and Host screens (spec 2026-10-13, section 6.1) =====
  // Drawn from the same model, and kept where they stand like the meeting screen's rows: a person's row is found by the person (the id) and patched,
  // and only a row whose person came or went is added or removed; the chat list grows at its foot, loses messages at its head, and never draws the
  // ones it has again; the host's buttons are a row of setActions, with ids. A finger that is down on a button when a poll arrives finds it at the
  // lift, and a list does not jump under the reader. What the meeting says (names, chat lines) goes in as text and never as markup.

  const can = (host, name) => Boolean(host[name] && host[name].available);   // the model says that the client, or the role, offers it
  let removeTimer = null, endTimer = null;

  // Keys for things that may repeat (a name that two people have, the same line said twice in a second): the second of two is told apart by its turn.
  function uniqueKeys(items, keyOf) {
    const seen = new Map();
    return items.map((item) => {
      const key = keyOf(item), turn = (seen.get(key) || 0) + 1;
      seen.set(key, turn);
      return turn === 1 ? key : key + "#" + turn;
    });
  }

  // The second tap. Remove (on a person's row) and End meeting for everyone each ask for a second tap within CONFIRM_MS, so that one stray tap cannot throw
  // someone out or end the meeting for all; asking about one person's Remove moves the ask from another's. Each has a timer of its own, so that the one
  // never strands the other's lapse. What a screen was asking is not carried to the next visit (leftView).
  function forgetEnd() { clearTimeout(endTimer); model.confirmEnd = false; }
  function forgetRemove() { clearTimeout(removeTimer); model.confirmRemove = null; }
  function askToEnd() {
    forgetEnd();
    askedAt.end = Date.now();
    model.confirmEnd = true;
    endTimer = setTimeout(() => { model.confirmEnd = false; render(); }, CONFIRM_MS);
    render();
  }
  function askToRemove(key) {
    forgetRemove();
    askedAt.remove = Date.now();
    model.confirmRemove = key;
    removeTimer = setTimeout(() => { model.confirmRemove = null; render(); }, CONFIRM_MS);
    render();
  }
  // What a screen was asking, or saying, belongs to it: not the second tap on End or Remove, nor a refusal, goes with the person to the next screen.
  function leftView(was) {
    if (was === "host") forgetEnd();
    if (was === "participants") forgetRemove();
    if ((was === "host" || was === "participants") && model.errorOn === was) model.error = "";
  }

  // --- Participants: the room's people, and for a host or co-host the buttons that act on them, and the waiting room.
  const rosterRows = { people: new Map(), waiting: new Map() };   // by the person's key: { li, name, marks, actions, holder, marksKey }
  const personKey = (p) => (p.id === undefined || p.id === null ? "name:" + p.name : "id:" + p.id);
  const PERSON_MARKS = [["host", "Host", "mark-host"], ["hand", "Hand raised", "mark-hand"], ["muted", "Muted", "mark-muted"]];

  function personRow() {
    const li = document.createElement("li");
    li.className = "participant";
    const name = document.createElement("span");
    name.className = "name";
    const marks = document.createElement("span");
    marks.className = "marks";
    const actions = document.createElement("div");
    actions.className = "row-actions";
    li.append(name, marks, actions);
    return { li: li, name: name, marks: marks, actions: actions, holder: { key: null }, marksKey: "" };
  }

  function marksOf(p) {
    const marks = PERSON_MARKS.filter(([key]) => p[key]).map(([, text, cls]) => [text, cls]);
    if (p.video === false) marks.push(["Video off", "mark-video-off"]);
    return marks;
  }

  // What a host can do to this person, as the model offers it: Mute (not to one who is muted already) and Remove, on every row but the room's own,
  // and Admit on a person in the waiting room. A participant's model offers none of it.
  function personSpecs(p, key, host, waiting) {
    if (waiting) {
      return can(host, "waiting_room") ? [{ id: "admit", label: "Admit", icon: "door", className: "primary", disabled: model.pending.has("host.admit"), onClick: () => control("host.admit", p.id) }] : [];
    }
    if (p.me) return [];
    const specs = [];
    if (can(host, "mute_one") && !p.muted) {
      specs.push({ id: "mute", label: "Mute", icon: "mic-off", className: "quiet", disabled: model.pending.has("host.mute_one"), onClick: () => control("host.mute_one", p.id) });
    }
    if (can(host, "remove")) {
      const asking = model.confirmRemove === key;
      specs.push({ id: "remove", label: asking ? "Tap again to remove" : "Remove", icon: "x-circle", className: asking ? "danger" : "quiet", disabled: model.pending.has("host.remove"),
        onClick: () => { if (model.confirmRemove !== key) askToRemove(key); else if (!bounced("remove")) { forgetRemove(); control("host.remove", p.id); } } });
    }
    return specs;
  }

  function patchPerson(row, p, marks, specs) {
    const name = String(p.name || "") + (p.me ? " (this room)" : "");
    if (row.name.textContent !== name) row.name.textContent = name;
    const marksKey = JSON.stringify(marks);
    if (marksKey !== row.marksKey) {
      row.marksKey = marksKey;
      row.marks.replaceChildren(...marks.map(([text, cls]) => {
        const mark = document.createElement("span");
        mark.className = "mark " + cls;
        mark.textContent = text;
        return mark;
      }));
    }
    setActions(specs, row.actions, row.holder);
  }

  // The list shows these people in this order: a row that is there is kept and patched, one that is not is drawn, one that has gone is removed.
  // A row is moved only when it is not where it should be, and a row that comes in at the foot or goes from it moves none of the others.
  function syncPeople(list, rows, people, keys, host, waiting) {
    const wanted = new Set(keys);
    for (const [key, row] of rows) {
      if (!wanted.has(key)) { row.li.remove(); rows.delete(key); }
    }
    people.forEach((p, i) => {
      let row = rows.get(keys[i]);
      if (!row) { row = personRow(); rows.set(keys[i], row); }
      patchPerson(row, p, waiting ? [] : marksOf(p), personSpecs(p, keys[i], host, waiting));
      if (list.children[i] !== row.li) list.insertBefore(row.li, list.children[i] || null);
    });
  }

  function renderParticipants(m) {
    const host = (m && m.controls && m.controls.host) || {};
    // Zoom's attendee list holds the people in the waiting room too (waiting: true): they are in the waiting list, with Admit, and are not in the meeting's.
    const people = m && m.participants && Array.isArray(m.participants.list) ? m.participants.list.filter((p) => !p.waiting) : [];
    const waiting = m && can(host, "waiting_room") && Array.isArray(host.waiting_room.waiting) ? host.waiting_room.waiting : [];
    const keys = uniqueKeys(people, personKey), waitingKeys = uniqueKeys(waiting, personKey);
    if (model.confirmRemove !== null && !keys.includes(model.confirmRemove)) forgetRemove();   // the person it was about has gone
    syncPeople(el("participant-list"), rosterRows.people, people, keys, host, false);
    syncPeople(el("waiting-list"), rosterRows.waiting, waiting, waitingKeys, host, true);
    el("waiting-kicker").hidden = waiting.length === 0;
  }

  // --- Chat: the last messages, newest at the foot.
  const chatShown = [];      // what the list shows, oldest first: { key, li }
  let chatOpened = false;    // the screen has just been opened: its list goes to the newest message
  let chatAsked = 0;         // the unread count the screen has asked the agent to clear, while it is up

  function chatRow(msg) {
    const li = document.createElement("li");
    li.className = "chat-message";
    const from = document.createElement("p");
    from.className = "chat-from";
    const sent = msg.at ? new Date(msg.at) : null;
    from.textContent = String(msg.from || "") + (sent && !Number.isNaN(sent.getTime()) ? ", " + fmtTime(sent) : "");
    const text = document.createElement("p");
    text.className = "chat-text";
    text.textContent = String(msg.text || "");
    li.append(from, text);
    return li;
  }

  function renderChat(m) {
    const list = el("chat-list");
    const messages = m && m.chat && Array.isArray(m.chat.messages) ? m.chat.messages : [];
    const keys = uniqueKeys(messages, (msg) => JSON.stringify([msg.from, msg.at, msg.text]));
    // The agent keeps the last twenty: the messages the list shows are the head of what it should show now, less the ones that have scrolled off its
    // head. Those stay (as the elements they are), the ones before them go, and the ones after them are drawn. Nothing in common: the list is drawn again.
    const carried = (drop) => chatShown.length - drop <= keys.length && chatShown.slice(drop).every((row, i) => row.key === keys[i]);
    let drop = 0;
    while (drop < chatShown.length && !carried(drop)) drop++;
    const atFoot = !list.clientHeight || list.scrollHeight - list.scrollTop - list.clientHeight < 40;   // before the rows change
    for (const row of chatShown.splice(0, drop)) row.li.remove();
    for (let i = chatShown.length; i < messages.length; i++) {
      const row = { key: keys[i], li: chatRow(messages[i]) };
      chatShown.push(row);
      list.append(row.li);
    }
    el("chat-empty").hidden = messages.length > 0;
    // Opened, it is at the newest message; a reader who has scrolled up is not pulled down by a new one, and one at the foot follows it.
    if (view() === "chat" && chatOpened) { chatOpened = false; list.scrollTop = list.scrollHeight; }
    else if (atFoot) list.scrollTop = list.scrollHeight;
  }

  // The unread count is the agent's: opening the screen marks what is there as seen, and so does every poll that finds more while the screen is up (the
  // count would otherwise climb on the button under the reader's nose). A count that does not fall is not asked about again until it rises.
  function keepChatSeen(m) {
    if (!m || view() !== "chat") { chatAsked = 0; return; }
    const unread = m.chat && m.chat.unread > 0 ? m.chat.unread : 0;
    if (unread < chatAsked) chatAsked = unread;                    // the agent has cleared it: a message after that is a new one
    if (unread > chatAsked && !model.pending.has("chat.seen")) {
      chatAsked = unread;
      Promise.resolve().then(() => request("chat.seen", "/api/meeting/chat/seen"));   // not from inside the render that found it: request() draws the page again
    }
  }

  // --- Host: the actions on the whole meeting, as the model offers them (a co-host has no End). The icon shows the state and the label the action.
  const hostKey = { key: null };
  function hostSpecs(m) {
    const h = (m.controls && m.controls.host) || {};
    const specs = [];
    if (can(h, "mute_all")) specs.push(controlSpec("host.mute_all", "Mute all", "mic-off", ""));
    if (can(h, "ask_unmute")) specs.push(controlSpec("host.ask_unmute", "Ask all to unmute", "mic", ""));
    if (can(h, "record")) specs.push(controlSpec("host.record", h.record.on ? "Stop recording" : "Start recording", "record", ""));
    if (can(h, "lock")) specs.push(controlSpec("host.lock", h.lock.on ? "Unlock meeting" : "Lock meeting", h.lock.on ? "lock" : "unlock", ""));
    if (can(h, "end")) {
      const end = { id: "host.end", icon: "x-circle", className: "danger", disabled: model.pending.has("host.end") };
      specs.push(model.confirmEnd
        ? Object.assign(end, { label: "Tap again to end", onClick: () => { if (bounced("end")) return; forgetEnd(); control("host.end"); } })
        : Object.assign(end, { label: "End meeting for everyone", onClick: askToEnd }));
    }
    return specs;
  }

  // Called from render() with the connected meeting, or with none (the lists are emptied: nothing is known of who is in it).
  function renderSubScreens(m) {
    renderParticipants(m);
    renderChat(m);
    setActions(m ? hostSpecs(m) : [], "host-actions", hostKey);
    keepChatSeen(m);
    // A refusal is said on the screen it was met on (the meeting screen's own line is not in sight from here), and a screen says its own and not another's:
    // not the one the meeting screen's Raise hand drew before the person came, nor the one it draws after they have.
    el("participants-message").textContent = view() === "participants" && model.errorOn === "participants" ? model.error : "";
    el("host-message").textContent = view() === "host" && model.errorOn === "host" ? model.error : "";
  }
  // ===== end of the Participants, Chat and Host screens =====

  // A meeting began or ended: land on the new mode's main screen. What the last action said (a refusal on one mode's
  // screen is not news on the other's) and a Leave still waiting for its second tap belong to the mode that is over.
  function landOnMode() {
    model.error = "";
    model.confirmLeave = false;
    model.reactionsUntil = 0;
    clearTimeout(confirmTimer);
    forgetEnd();
    forgetRemove();
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
    el("message").textContent = model.error;   // the idle card's line; the meeting screen's own is written with its dialog card (renderDialog)

    if (model.offline || !model.status) {
      body.dataset.state = "offline";
      el("offline-note").hidden = !model.offline;   // in the top bar, so every screen says it (not while still loading)
      el("kicker").textContent = "Not connected";
      el("headline").textContent = "Can't reach the room";
      el("detail").textContent = "Check that Crystal Meet is running on the room's device, then this page will reconnect on its own.";
      setActions([], "actions", idleActionsKey);
      setActions([], "meeting-actions", meetingActionsKey);
      renderMeetingScreen(null);
      renderSubScreens(null);   // the lists and their buttons go as the meeting screen's rows do: nothing is known of who is in the meeting, and a Remove could only fail
      document.querySelector(".screen-section").hidden = true;
      document.querySelector(".door-section").hidden = true;
      document.querySelector(".sound-section").hidden = true;
      document.querySelector(".camera-section").hidden = true;
      document.querySelector(".keyboard-section").hidden = true;
      return;
    }
    document.querySelector(".screen-section").hidden = false;
    document.querySelector(".door-section").hidden = false;
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
    renderSubScreens(mode() === "meeting" && m.state === "connected" ? m : null);

    renderEvents(cal);
    renderKeyClick(s);
    renderPicker();
    renderDoorSign();
    for (const fn of renderHooks) fn(s);
    guardLayoutShift();   // last: the Sound strip (devices.js) and the rest have moved the screen by now
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

  // The door sign's two groups: one button per background (the one in use is pressed) and one switch per school (pressed when its
  // score is on). A school's switch sends the whole list, in the service's order, with that school turned on or off.
  function renderDoorSign() {
    // Rebuilt only when a choice or availability changes, as the screensaver's picker is, so a tap is never lost to a re-render.
    const key = JSON.stringify([model.settings, model.busy]);
    if (key === lastDoorSignKey) return;
    lastDoorSignKey = key;
    const settings = model.settings || {};
    const schools = settings.sign_scores || [];
    const pressed = (b, on) => { b.setAttribute("aria-pressed", on ? "true" : "false"); return b; };
    el("sign-picker").replaceChildren(...SIGN_BACKGROUNDS.map(([value, label]) => {
      const current = settings.sign_background === value;
      return pressed(button(label, current ? "primary" : "quiet", () => setSettings({ sign_background: value }), false), current);
    }));
    el("scores-picker").replaceChildren(...SIGN_SCHOOLS.map(([value, label]) => {
      const on = schools.includes(value);
      const list = SIGN_SCHOOLS.map(([school]) => school).filter((school) => (school === value ? !on : schools.includes(school)));
      return pressed(button(label, on ? "primary" : "quiet", () => setSettings({ sign_scores: list }), false), on);
    }));
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
  el("open-door-sign").addEventListener("click", () => setView("door-sign"));
  el("back-from-door-sign").addEventListener("click", () => setView("options"));
  el("back-from-camera").addEventListener("click", () => setView(cameraMode === "meeting" ? "meeting" : "options"));
  el("open-camera-live").addEventListener("click", () => setView("camera"));
  el("open-upcoming").addEventListener("click", () => setView("upcoming"));
  el("back-from-upcoming").addEventListener("click", () => setView("meeting"));
  for (const name of ["participants", "chat", "host"]) el("back-from-" + name).addEventListener("click", () => setView("meeting"));
  el("open-tv-options").addEventListener("click", () => setView("tv"));
  el("back-from-tv").addEventListener("click", () => setView(tvMode === "meeting" ? "meeting" : "options"));
  el("key-click-on").addEventListener("click", () => setKeyClick(true));
  el("key-click-off").addEventListener("click", () => setKeyClick(false));

  window.croomPage = {
    el: el, model: model, api: api, post: post, act: act, button: button, render: render,
    iconsReady: iconsReady, icon: icon, labelWithIcon: labelWithIcon, keepFocus: keepFocus, CONFIRM_MS: CONFIRM_MS,
    refreshStatus: refreshStatus, deviceEpoch: deviceEpoch, IN_MEETING: IN_MEETING, submitForm: submitForm,
    view: view, setView: setView, mode: mode, cameraMode: () => cameraMode, kiosk: kiosk,
    onViewChange: (fn) => viewHooks.push(fn),
    holdIdleReturn: (held) => { idleReturnHeld = Boolean(held); armIdleReturn(); },
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
