// The bridge between the room device and Zoom's Meeting SDK (Client View).
// Reads one-time join parameters, joins, and reports every status change to the
// device through window.crystalMeetEvent (exposed by the provider).
(function () {
  "use strict";

  const STATUS = { 1: "joining", 2: "connected", 3: "left", 4: "reconnecting" };
  // muted is null until Zoom has said (the room's record, or Zoom's answer to a mute): never a guess.
  const bridge = { state: "loading", detail: "", userId: null, userName: null, muted: null, mutedAtJoin: false, _pendingMute: false };
  window.crystalMeet = bridge;

  function report(state, detail) {
    bridge.state = state;
    bridge.detail = detail || "";
    const label = document.getElementById("crystal-meet-status");
    if (label) label.textContent = state === "connected" ? "" : (bridge.detail || state);
    if (typeof window.crystalMeetEvent === "function") {
      try { window.crystalMeetEvent(state, bridge.detail); } catch (e) { /* the device is not listening */ }
    }
  }

  // An event whose detail is JSON (the "state", "dialog", "dialog-closed" and "chat" events), sent to the device if it is listening.
  function reportJson(state, data) {
    if (typeof window.crystalMeetEvent === "function") {
      try { window.crystalMeetEvent(state, JSON.stringify(data)); } catch (e) { /* the device is not listening */ }
    }
  }

  function words(error) {
    if (!error) return "Zoom gave no reason";
    const text = error.errorMessage || error.reason || error.message || "";
    const code = error.errorCode !== undefined && error.errorCode !== null ? " (code " + error.errorCode + ")" : "";
    return (text || JSON.stringify(error)) + code;
  }

  // Zoom's typings name the field meetingStatus; its docs also show status.
  function statusCode(data) {
    if (!data) return undefined;
    return data.meetingStatus !== undefined ? data.meetingStatus : data.status;
  }

  const UNLISTED = "Zoom has not listed the room in the meeting yet; try again in a moment";
  // Zoom's API answers false, and calls nothing back, to a call it drops: one made again inside the window it keeps for its method
  // (a second for mute), or with a key its method does not take. That is said at once, not waited for. (putOnHold's drop is
  // silent: see sdk().)
  const ignored = (method) => new Error("Zoom ignored " + method + " (it takes one a few seconds after the last); try again in a moment");
  bridge.mute = function (muted) {
    return new Promise((resolve, reject) => {
      if (!known(bridge.userId)) {
        // Connected, but Zoom has not listed the room yet (its record of the current user is built from its roster).
        reject(new Error(bridge.state === "connected" ? UNLISTED : "not in a meeting"));
        return;
      }
      const taken = ZoomMtg.mute({
        userId: bridge.userId, mute: !!muted,
        success: () => { bridge.muted = !!muted; resolve(bridge.muted); },
        error: (e) => reject(new Error(words(e))),
      });
      if (taken === false) reject(ignored("mute"));      // two taps inside the second: the second is the one Zoom drops
    });
  };

  bridge.leave = function () {
    return new Promise((resolve) => {
      ZoomMtg.leaveMeeting({
        confirm: false,
        success: () => { report("left", ""); resolve(true); },
        error: () => { report("left", ""); resolve(false); },
      });
    });
  };

  // ---- State as Zoom has it: the current user's record and the client's own buttons ----
  const pick = (obj, names) => { for (const n of names) { if (obj && obj[n] !== undefined && obj[n] !== null) return obj[n]; } return undefined; };
  const known = (value) => value !== undefined && value !== null && value !== "";
  const knownOrNull = (value) => (known(value) ? !!value : null);   // what Zoom does not say is unknown, not off
  const handUp = (user) => !!pick(user, ["raiseHand", "bRaiseHand", "isHandRaised", "bHandRaised"]);
  // The room's id and name from Zoom's record, once Zoom lists the room (before that the record has neither).
  function takeUser(user) {
    if (!user) return;
    if (known(user.userId)) bridge.userId = user.userId;
    const name = pick(user, ["userName", "displayName"]);
    if (known(name)) bridge.userName = String(name);
  }
  // Zoom's getAttendeeslist answers only when its roster is not empty and calls neither callback otherwise, and the roster
  // fills a moment after "connected". A reading must never hang on that: nothing here waits longer than ANSWER_MS for Zoom,
  // and a silence is read as the last answer was (the roster at first empty, the current user at first unknown). A silent
  // call registers nothing, so a call may be made again: with `again`, the question is asked that often until Zoom answers.
  const ANSWER_MS = 2000;
  const ASK_AGAIN_MS = 250;
  let lastRoster = [];
  let lastUser = null;
  function ask(method, silence, take, again) {
    return new Promise((resolve, reject) => {
      let over = false, deadline = null, redo = null;
      const stop = () => { over = true; clearTimeout(deadline); clearTimeout(redo); };
      deadline = setTimeout(() => { if (!over) { stop(); resolve(silence()); } }, ANSWER_MS);
      const call = () => {
        if (over) return;
        try {
          ZoomMtg[method]({
            success: (result) => { const value = take(result); if (!over) { stop(); resolve(value); } },
            error: () => { const value = take(null); if (!over) { stop(); resolve(value); } },
          });
        } catch (e) { stop(); reject(e); return; }
        if (!over && again) redo = setTimeout(call, again);
      };
      call();
    });
  }
  function currentUser() {
    return ask("getCurrentUser", () => lastUser, (result) => {
      const user = (result && result.result && result.result.currentUser) || null;
      if (user) lastUser = user;
      return user;
    });
  }
  // The footer's own video button, where Zoom's own code finds it (minimized: the widget's footer). Failing that, only a
  // button in the footer named exactly "start my video" / "stop my video" (or "Start Video" / "Stop Video") will do:
  // other buttons of Zoom's speak of video too ("Stop Incoming Video" in the More menu blanks every remote video).
  const VIDEO_NAME = /^(start|stop)( my)? video$/i;
  function videoButton() {
    const own = document.querySelector("#foot-bar .send-video-container__btn")
      || document.querySelector(".minimize-widget__footer .send-video-container__btn");
    if (own) return own;
    const footer = document.getElementById("wc-footer");
    if (!footer) return null;
    for (const b of footer.querySelectorAll("button")) {
      if (VIDEO_NAME.test((b.getAttribute("aria-label") || "").trim())) return b;
    }
    return null;
  }
  // Zoom names the button "start my video" / "stop my video" only while it can use a camera; with none found, the camera
  // blocked, or video disabled by the host it is just "Video" (usable: false, on unknown). aria-disabled is "true" while
  // the video encoder starts and while a switch is in progress, and a click then does nothing.
  bridge.video = function () {
    const b = videoButton();
    if (!b) return { on: null, usable: false, label: null, disabled: false };
    const label = (b.getAttribute("aria-label") || b.textContent || "").trim();
    const disabled = b.getAttribute("aria-disabled") === "true";
    const named = VIDEO_NAME.exec(label);
    if (!named) return { on: null, usable: false, label: label, disabled: disabled };
    return { on: named[1].toLowerCase() === "stop", usable: true, label: label, disabled: disabled };
  };
  const NO_BUTTON = "Zoom's camera button is not on the screen yet; try again in a moment";
  // Press the button only when Zoom's state differs: wait for the button to be there and to name a usable camera
  // (presentMs: the footer and Zoom's camera list arrive a moment after connected), wait for Zoom to enable it (readyMs),
  // press once, and wait for Zoom to switch (switchMs, disabled meanwhile). Whatever does not happen is said in words:
  // an unchanged state is never a success.
  bridge.setVideo = function (on, waits) {
    const want = !!on;
    const readyMs = (waits && waits.readyMs) || 8000;
    const switchMs = (waits && waits.switchMs) || 10000;
    const presentMs = (waits && waits.presentMs) || 1500;
    return new Promise((resolve, reject) => {
      if (bridge.video().on === want) { resolve(want); return; }
      let pressed = false;
      const start = Date.now();
      let deadline = start + readyMs;
      const step = () => {
        const v = bridge.video();
        if (v.on === want) { resolve(want); return; }
        if (v.label === null || !v.usable) {   // not there yet, or no camera Zoom can use yet: give it presentMs
          if (Date.now() - start >= presentMs) { reject(new Error(v.label === null ? NO_BUTTON : "Zoom cannot use the camera right now")); return; }
          setTimeout(step, 100);
          return;
        }
        if (!pressed && !v.disabled) {
          videoButton().click();
          pressed = true;
          deadline = Date.now() + switchMs;
        } else if (Date.now() >= deadline) {
          reject(new Error(pressed ? "Zoom did not switch the camera" : "Zoom's camera button is still starting; try again in a moment"));
          return;
        }
        setTimeout(step, 100);
      };
      step();
    });
  };
  // An error, or an answer with no list in it, is read as the last list was: a moment's trouble is not everyone leaving.
  function attendees() {
    return ask("getAttendeeslist", () => lastRoster, (result) => {
      const list = result && result.result && result.result.attendeesList;
      if (Array.isArray(list)) lastRoster = list;
      return lastRoster;
    }, ASK_AGAIN_MS);
  }
  // The client's buttons the table may press, each found by what it says. The inventory run on the device
  // (spec section 7) confirms these words; a button not found is simply not offered. In Zoom 6.5.0 the hand is a button of
  // its own only in an uncommon build, and then only in the footer (other panels of the client have hand buttons too); it is
  // otherwise a row of the picker the Reactions button opens, and self view is an item of the menu of the View widget's button
  // (layout), so neither has a button to find here.
  const BUTTONS = {
    raise_hand: { words: [["raise hand"], ["lower hand"]], footer: true },
    reactions: { words: [["reaction"]] },
    captions: { words: [["caption", "show"], ["caption", "hide"], ["closed caption"]], on: (label) => label.includes("hide") },
    layout: { words: [["view"]], not: ["self view"] },
  };
  function labelOf(b) { return ((b.getAttribute("aria-label") || b.textContent || "")).trim(); }
  // What the client draws when a button is pressed: its reactions picker, the View widget's dropdown, and menus (role="menu").
  const POPOVERS = '.reaction-picker, .dropdown-menu, [role="menu"]';
  // What it draws over the meeting that can be answered (the Dialogs section below), whose buttons are never the client's own: the
  // content of react-modal, which has the role (Zoom's legacy modal, div.zm-modal.zm-modal-legacy, is inside it), and the consent
  // notice at the top of the meeting, div.consent-banner[role="banner"], which is never a dialog by its role.
  const DIALOG_SELECTOR = '[role="dialog"], [role="alertdialog"], .zm-modal, .zm-modal-legacy, .consent-banner';
  // Zoom's own footer first, then the rest of the page (the View widget is outside the footer); never a button that is an item
  // of a popover or one of a dialog.
  function findButton(name) {
    const spec = BUTTONS[name];
    const footer = document.getElementById("wc-footer");
    for (const scope of [footer, spec.footer ? null : document].filter(Boolean)) {
      for (const b of scope.querySelectorAll("button")) {
        if (b.closest(POPOVERS) || b.closest(DIALOG_SELECTOR)) continue;
        const label = labelOf(b).toLowerCase();
        if (spec.not && spec.not.some((w) => label.includes(w))) continue;
        if (spec.words.some((all) => all.every((w) => label.includes(w)))) return b;
      }
    }
    return null;
  }
  // `videoOn` is what the user's record says of the camera, for a button that cannot say it itself. The camera is usable
  // while Zoom's button names a camera it can use and takes a press (not disabled: video starting, a switch in progress).
  // `hand` is what the record says of the hand: neither the Reactions button nor a footer's hand button is read for it. The
  // View button offers both the view and self view, which only its menu shows and which are kept as last seen or picked.
  function buttonsSeen(videoOn, hand) {
    const out = {};
    const video = bridge.video();
    if (video.label !== null) {
      out.camera = video.usable && !video.disabled ? { on: video.on } : { on: video.on !== null ? video.on : videoOn, usable: false };
    }
    const reactions = findButton("reactions"), captions = findButton("captions"), view = findButton("layout");
    if (reactions || findButton("raise_hand")) out.raise_hand = { on: !!hand };
    if (reactions) out.reactions = {};
    if (captions) out.captions = { on: BUTTONS.captions.on(labelOf(captions).toLowerCase()) };
    if (view) {
      out.layout = { value: bridge._layout || "speaker" };
      out.hide_self = { on: !!bridge._selfHidden };
    }
    return out;
  }
  // The whole meeting as Zoom has it. Zoom's records say userId, userName, muted, audio, video, raiseHand, isHost, isCoHost
  // (sometimes ""), isGuest, isHold (in the waiting room) and self; the older names stay as fallbacks. What a record leaves
  // out is not read as off: the room's mute is the last Zoom said (null while it has said nothing), video comes from the
  // footer's button, else from the record, and a camera nobody speaks of is unknown (null).
  bridge.readMeeting = async function () {
    const list = await attendees();       // may wait for Zoom: the user's record is read after it, so it is as new as the read's end
    const user = await currentUser();
    const heardMuted = pick(user, ["muted", "bMuted", "isMuted"]);
    const recorded = pick(user, ["video", "bVideoOn", "videoOn", "isVideoOn"]);
    const video = bridge.video();
    if (known(heardMuted)) bridge.muted = !!heardMuted;
    takeUser(user);
    applyPendingMute();                   // the join's mic-off default, if it waited for the id this record gives
    const me = {
      userId: bridge.userId,
      muted: known(heardMuted) ? !!heardMuted : bridge.muted,
      videoOn: video.on !== null ? video.on : (known(recorded) ? !!recorded : null),
      isHost: !!pick(user, ["isHost", "bHost"]),
      isCoHost: !!pick(user, ["isCoHost", "isCohost", "bCoHost"]),
      raisedHand: handUp(user),
    };
    const participants = list.map((a) => ({
      id: pick(a, ["userId"]), name: String(pick(a, ["userName", "displayName", "name"]) || ""),
      muted: !!pick(a, ["muted", "bMuted"]), video: knownOrNull(pick(a, ["video", "bVideoOn", "videoOn"])),
      hand: !!pick(a, ["raiseHand", "bRaiseHand", "bHandRaised"]), host: !!pick(a, ["isHost", "bHost"]),
      waiting: !!pick(a, ["isHold", "waiting", "bHold", "isInWaitingRoom"]), me: pick(a, ["userId"]) === me.userId,
    }));
    return {
      me: me, role: me.isHost ? "host" : (me.isCoHost ? "co-host" : "participant"),
      buttons: buttonsSeen(me.videoOn, me.raisedHand), participants: participants, count: participants.length,
      recording: !!bridge._recording, locked: !!bridge._locked, keys: user ? Object.keys(user) : [],
    };
  };
  bridge._recording = false;
  bridge._locked = false;      // Zoom's calls do not say whether the meeting is locked: it is kept as the lock call last went
  bridge._layout = null;
  bridge._selfHidden = false;
  // The current user's own part of it, as the first version of the bridge answered: mute and video null when Zoom has not
  // said, never a guess.
  bridge.readState = async function () {
    const m = await bridge.readMeeting();
    return Object.assign({}, m.me, { keys: m.keys });
  };
  // What the page sees, for pinning Zoom's real field names and button wording from a device: the footer's buttons, the View
  // widget's (outside the footer), and under `popovers` what the reactions picker and the View dropdown hold.
  const wordsOf = (b) => {
    const label = (b.getAttribute("aria-label") || "").trim();
    const text = (b.textContent || "").replace(/\s+/g, " ").trim().slice(0, 40);
    return text && text !== label ? { label: label, text: text } : { label: label };
  };
  // The View widget's buttons, which are outside the footer: where Zoom draws the widget (the icons at the top of the screen),
  // and the one the bridge itself takes for the View button.
  function viewButtons() {
    const found = new Set(document.querySelectorAll(".full-screen-icon button"));
    const view = findButton("layout");
    if (view) found.add(view);
    return Array.from(found).filter((b) => !b.closest("#wc-footer") && !b.closest(POPOVERS) && !b.closest(DIALOG_SELECTOR));
  }
  bridge.diagnostics = async function () {
    const inventory = popoverInventory();      // among the controls, in the order asked: queued now, not after the read
    const state = await bridge.readState();
    const buttons = [];
    const add = (b, where) => {      // an item of a popover that is open is listed under popovers, not as a button of the footer's
      const words = wordsOf(b);
      if ((words.label || words.text) && buttons.length < 40 && !b.closest(POPOVERS)) buttons.push(where ? Object.assign(words, { where: where }) : words);
    };
    const footer = document.getElementById("wc-footer");
    for (const b of footer ? footer.querySelectorAll("button") : []) add(b);
    for (const b of viewButtons()) add(b, "view widget");
    return { user_keys: state.keys, state: state, video: bridge.video(), buttons: buttons, popovers: await inventory, page: document.title };
  };
  // A change Zoom makes (the host muting the room, someone raising a hand, a person joining, recording starting) reaches
  // the device as a "state" event: the whole meeting is read on every such change and every five seconds, and reported
  // only when something about it changed. The detail is the model with the current user's fields at the top level too,
  // for a provider that still reads muted and videoOn from there.
  let lastSeen = null;
  async function readAndReport() {
    if (bridge.state !== "connected") return;
    const now = await bridge.readMeeting();
    if (bridge.state !== "connected") return;      // left, or reconnecting, while it read: no "state" event after that
    const copy = Object.assign({}, now); delete copy.keys;
    const key = JSON.stringify(copy);
    if (key === lastSeen) return;
    const first = lastSeen === null;
    lastSeen = key;
    if (!first) reportJson("state", Object.assign({}, now.me, now));
  }
  // One read at a time: a request that comes while a read is in progress waits for it and is answered by one more read after
  // it, so a late answer can never overtake a newer one and a change made during a read is not lost, not even when that
  // read fails: the failure is kept, the request is still read, and only a failure of the last read is passed on.
  let syncing = null;
  let syncAgain = false;
  function syncState() {
    if (syncing) { syncAgain = true; return syncing; }
    syncing = (async () => {
      let failure = null;
      try {
        do {
          syncAgain = false;
          try { await readAndReport(); failure = null; } catch (e) { failure = e; }
        } while (syncAgain);
      } finally { syncing = null; syncAgain = false; }
      if (failure) throw failure;
    })();
    return syncing;
  }
  bridge._sync = syncState;      // read now and report what changed: resolves when done, rejects when the last read failed
  // Zoom calls its listeners from inside its own state reducer, where getCurrentUser throws ("Minified Redux error #3"): a
  // listener only queues the read, and a burst of changes is read once, 50 ms after the first of them.
  let syncTimer = null;
  function queueSync() {
    if (syncTimer !== null) return;
    syncTimer = setTimeout(() => { syncTimer = null; syncState().catch(() => {}); }, 50);
  }
  // The five-second read is one timer for the whole page, however often Zoom reconnects, and it never waits on the first
  // read: a first read that fails still leaves the five-second one running.
  let pollTimer = null;
  function startPolling() {
    if (pollTimer === null) pollTimer = setInterval(() => { syncState().catch(() => {}); }, 5000);
  }

  // ---- Controls: the client's own buttons and popovers, pressed for the table ----
  // The hand, reactions, captions, the view and self view have no SDK call: the bridge presses the client's own buttons, found
  // by what they say (BUTTONS), gives the client a bounded moment to show the change, and answers with the meeting as Zoom then
  // has it. Where there is something to see (captions, the hand, self view) a press that changes nothing is a failure in
  // words, as it is for the camera, which goes through setVideo; a button that is disabled is not pressed. In Zoom 6.5.0 the
  // reactions and the hand are in a picker the Reactions button opens, and the View widget's button opens a dropdown with the
  // two views and self view. Both toggle on their opener and are the client's own nodes: a popover is opened, used and closed
  // within one call, by its opener, and nothing of the client's is ever taken out of the page.
  const PRESS_MS = 2000, OPEN_MS = 2000, CLOSE_MS = 1000;
  // Says whether done() came true within ms: looked at as soon as the client has rendered, then every 100 ms. A look that fails
  // is no answer, only not yet.
  async function settle(done, ms) {
    const end = Date.now() + ms;
    await null;      // the client renders what a press did in a microtask: that is waited for, nothing longer
    for (;;) {
      try { if (await done()) return true; } catch (e) { /* not yet */ }
      if (Date.now() >= end) return false;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
  const said = (name) => name === "hide_self" ? "self view" : name.replace("_", " ");
  const noButton = (name) => new Error("Zoom's " + said(name) + " button is not on the screen yet; try again in a moment");
  const notReady = (name) => new Error("Zoom's " + said(name) + " button is not ready yet; try again in a moment");
  // Zoom's own button (ZoomButton) has no disabled attribute: it is the class zm-btn--disabled, and its onClick ignores a press. The
  // client's other buttons say aria-disabled.
  const unready = (b) => b.disabled === true || b.getAttribute("aria-disabled") === "true" || b.classList.contains("zm-btn--disabled");
  async function handIsUp() { return handUp(await currentUser()); }
  // Names are compared in lower case with everything but letters and digits left out, so "Thumbs Up", "thumbs_up" and
  // " THUMBSUP " are one name. An item has its aria-label, its tooltip and its text to be named by: the picker's reactions
  // are an icon named in attributes, its hand row and the dropdown's items are text.
  const norm = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
  const namesOf = (e) => [e.getAttribute("aria-label"), e.getAttribute("data-tooltip"), e.textContent].map(norm).filter(Boolean);
  const named = (e, wanted) => namesOf(e).some((n) => wanted.includes(n));
  // The client shows no emoji, only named icons: the table's own words (the spec's emoji) are mapped to Zoom's names.
  const REACTIONS = { "\u{1F44D}": "thumbsup", "\u{1F44F}": "clap", "\u{1F602}": "joy", "\u{1F62E}": "openmouth", "\u{2764}": "heart", "\u{1F389}": "tada" };
  const VIEWS = { speaker: "speakerview", gallery: "galleryview" };      // the table says speaker or gallery, Zoom says "... View"
  const NOT_REACTIONS = ["raisehand", "lowerhand", "berightback", "more"];      // the rest of the picker: the hand, a status, More
  const viewOf = (e) => { const n = norm(labelOf(e)); return n.startsWith("gallery") ? "gallery" : (n.startsWith("speaker") ? "speaker" : null); };
  function need(value, what) {
    const text = value === undefined || value === null ? "" : String(value).trim();
    if (!text) throw new Error("Say which " + what);
    return text;
  }
  const CLICKABLE = 'button, a, [role="button"], [role="menuitem"], [role="option"]';
  // What the client has on the screen while each popover is open.
  const OPEN = { reactions: ".reaction-picker", layout: ".full-screen-viev-dropdown .dropdown-menu" };
  // Press the opener of a popover, hand use() the clickable things that appeared because of it (what was on the page before
  // is not looked at: no search of the page, no role to trust), and see the popover closed when use() is done. Zoom closes it
  // after a pick; when it has not, or nothing was picked (use() says so by answering false), the press a person would make
  // closes it: the opener again, which toggles it. A popover that is open already (a person's, or one a call could not close)
  // has its items on the screen: it is used as it is, and the opener, which would close it, is left alone.
  async function withPopover(opener, control, use) {
    const button = findButton(opener);
    if (!button) throw noButton(control);
    const showing = document.querySelector(OPEN[opener]);
    let items = showing ? Array.from(showing.querySelectorAll(CLICKABLE)) : [];
    if (!showing) {
      if (unready(button)) throw notReady(control);
      const before = new Set(document.querySelectorAll(CLICKABLE));
      button.click();
      const opened = await settle(() => {
        const now = findButton(opener);      // the client may draw its opener again when it is pressed: that is not an item, nor is a dialog it draws
        items = Array.from(document.querySelectorAll(CLICKABLE)).filter((e) => !before.has(e) && e !== now && !e.closest(DIALOG_SELECTOR));
        return items.length > 0;
      }, OPEN_MS);
      if (!opened) throw new Error("Zoom's " + (opener === "layout" ? "view" : "reactions") + " menu did not open; try again in a moment");
    }
    const gone = () => items.every((e) => !e.isConnected);
    try {
      if (await use(items, !showing) !== false) await settle(gone, CLOSE_MS);
    } finally {
      if (!gone()) {
        const again = findButton(opener);
        if (again) again.click();
        await settle(gone, CLOSE_MS);
      }
    }
  }
  // The View menu says what is so now: the selected view is the item marked checked, and the self view item says what it
  // would do.
  function readView(items) {
    for (const i of items) {
      if (viewOf(i) && i.classList.contains("full-screen-widget__pop-menu--checked")) bridge._layout = viewOf(i);
      if (named(i, ["hideselfview"])) bridge._selfHidden = false;
      else if (named(i, ["showselfview"])) bridge._selfHidden = true;
    }
  }
  async function raiseHand() {
    const own = findButton("raise_hand");      // a hand button of the footer's own: Zoom draws one only in an uncommon build
    if (!own && !findButton("reactions")) throw noButton("raise_hand");
    const was = await handIsUp();
    if (own) {
      if (unready(own)) throw notReady("raise_hand");
      own.click();
    } else {
      await withPopover("reactions", "raise_hand", async (items) => {
        const row = items.find((i) => named(i, ["raisehand", "lowerhand"]));
        if (!row) throw new Error("Zoom offers no raise hand here");
        if (unready(row)) throw notReady("raise_hand");
        row.click();
      });
    }
    // The room's own record says whether the hand is up: neither the Reactions button nor the picker's row does.
    if (!await settle(async () => (await handIsUp()) !== was, PRESS_MS)) throw new Error("Zoom did not " + (was ? "lower" : "raise") + " the hand");
  }
  async function hideSelf() {
    await withPopover("layout", "hide_self", async (items) => {
      readView(items);
      const item = items.find((i) => named(i, ["hideselfview", "showselfview"]));
      if (!item) throw new Error("Zoom offers no self view here");
      if (unready(item)) throw notReady("hide_self");
      const hides = named(item, ["hideselfview"]);      // what the press does
      item.click();
      // Zoom taking the press shows as the dropdown closing with the item in it, or the item saying the opposite now.
      if (!await settle(() => !item.isConnected || !named(item, [hides ? "hideselfview" : "showselfview"]), PRESS_MS)) {
        throw new Error("Zoom did not " + (hides ? "hide" : "show") + " self view");
      }
      bridge._selfHidden = hides;
    });
  }
  async function pressButton(name) {
    if (name === "raise_hand") return raiseHand();
    if (name === "hide_self") return hideSelf();
    const b = findButton(name);                // captions: the label says its state, and changes when it is pressed
    if (!b) throw noButton(name);
    if (unready(b)) throw notReady(name);
    const before = labelOf(b);
    b.click();
    if (!await settle(() => { const now = findButton(name); return !!now && labelOf(now) !== before; }, PRESS_MS)) {
      throw new Error("Zoom did not switch " + said(name));
    }
  }
  // Reactions and views: open the menu, pick the item the value names, close the menu. Only a view pick sets the view, and
  // only a reaction is sent as one: the hand, self view and Zoom's other items are not picked here.
  async function pickFromMenu(name, value) {
    const text = need(value, name === "reactions" ? "reaction" : "view");
    const wanted = [(name === "reactions" ? REACTIONS[text.replace(/\u{FE0F}/gu, "")] : VIEWS[norm(text)]) || norm(text)];
    await withPopover(name, name, async (items) => {
      if (name === "layout") readView(items);
      const item = items.find((i) => named(i, wanted) && (name === "reactions" ? !named(i, NOT_REACTIONS) : viewOf(i)));
      if (!item) throw new Error("Zoom offers no " + text + " here");
      if (unready(item)) throw notReady(name);
      item.click();
      if (name === "layout") bridge._layout = viewOf(item);
    });
  }
  // The View menu opened, read as a pick reads it, and closed again by its opener with nothing picked: the view and self view are only
  // to be read there, and until it has been opened the reading says the defaults. The device asks for it once after the room is in the
  // meeting, so the table starts from what the client has.
  async function readViewMenu() {
    await withPopover("layout", "layout", async (items) => { readView(items); return false; });
  }
  // The inventory's look into the two popovers (the device run of spec section 7): each opener is pressed, what appeared is listed
  // by what the client says (text, label, tooltip, class, disabled), and the opener closes it again. A button that is missing or
  // disabled is reported, not pressed, and nothing is pressed before the room is in the meeting.
  const itemOf = (e) => ({
    text: (e.textContent || "").replace(/\s+/g, " ").trim().slice(0, 40), label: (e.getAttribute("aria-label") || "").trim(),
    tooltip: (e.getAttribute("data-tooltip") || "").trim(), class: (e.getAttribute("class") || "").trim().slice(0, 200), disabled: unready(e),
  });
  async function look(opener) {
    const button = findButton(opener);
    const entry = { opener: button ? wordsOf(button) : null, found: !!button, pressed: false, items: [] };
    if (!button) entry.note = "the button is not on the screen";
    else if (bridge.state !== "connected") entry.note = "not in a meeting";
    else if (unready(button)) entry.note = "the button is disabled";
    else {
      entry.pressed = true;
      try {
        await withPopover(opener, opener, async (items, pressed) => { entry.pressed = pressed; entry.items = items.map(itemOf); return false; });
      } catch (e) { entry.note = e.message; }
    }
    return entry;
  }
  const popoverInventory = () => alone(async () => ({ reactions: await look("reactions"), view: await look("layout") }));

  // ---- The host's calls and rename: Zoom's own calls, not its buttons ----
  // They go through the same dispatch as the presses, so the guard, the chain that runs controls one at a time and the double
  // tap are theirs too. Zoom answers each through its success or error callback, the error in its own words (a refusal with none
  // is named by the call). The chain has no end of its own: a call Zoom never answered would hold every control after it, and
  // the inventory, until the page was reloaded, so a call ends after SDK_MS, in words, and the next control goes on. SDK_MS is
  // longer than the ten seconds the SDK itself waits for its store, whose own error says why.
  const SDK_MS = 12000;
  // The SDK's own wait for its store ends, after ten seconds, with an error whose message is the JSON of {type: "OPERATION_TIMEOUT",
  // errorCode: 1} (it may come as an Error, as an errorMessage, or as the object itself): not Zoom refusing, Zoom not saying.
  const timedOut = (e) => !!e && [e.type, e.errorMessage, e.reason, e.message, e].some((t) => typeof t === "string" && t.includes("OPERATION_TIMEOUT"));
  // Zoom drops a putOnHold made again inside 100 ms and says nothing of it: with callbacks given its API answers undefined, as it
  // does to a call it takes, and calls nothing back, so the drop can not be told from a call nobody answers. A tap on Hold and then
  // on Admit is such a pair. The page remembers when it last asked, and waits out the rest of the window (and a few ms over, for a
  // timer that fires early) before it asks again.
  const SPACED_MS = { putOnHold: 100 }, SPACED_OVER_MS = 10;
  const askedAt = Object.create(null);
  const sdk = (method, args) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Zoom did not answer " + method + " in time")), SDK_MS);
    const over = (end, value) => { clearTimeout(timer); end(value); };
    // Zoom's words; a refusal with none (endMeeting's, for a co-host, is {errorCode: 3003, errorMessage: null}) is named by the call.
    const refusal = (e) => {
      if (timedOut(e)) return "Zoom did not confirm " + method + " in time";
      if (e && (e.errorMessage || e.reason || e.message)) return words(e);
      return "Zoom refused " + method + (e && known(e.errorCode) ? " (code " + e.errorCode + ")" : "");
    };
    const ask = () => {
      askedAt[method] = performance.now();
      try {
        const taken = ZoomMtg[method](Object.assign({}, args, {
          success: () => over(resolve, true),
          // Zoom 6.5.0 calls error, then success, for a participant renaming themselves (its host check answers before the rename
          // goes on, with their own id): a refusal is passed on a moment later, and an answer in the same call is the answer.
          error: (e) => { Promise.resolve().then(() => over(reject, new Error(refusal(e)))); },
        }));
        // Its API answers false, and calls nothing back, to a call it drops: one with a key its method does not take (its gate looks
        // only at the keys given: a key left out goes ahead undefined), or one made again inside the window it keeps for its method
        // (mute 1 s; muteAll, rename, expel, record and lockMeeting 5 s). A pair of ordinary taps does that, so it is said at once
        // and not left to the bound. (putOnHold's 0.1 s is waited out above, not answered.)
        if (taken === false) over(reject, ignored(method));
      } catch (e) { over(reject, e); }
    };
    const wait = SPACED_MS[method] && askedAt[method] !== undefined ? askedAt[method] + SPACED_MS[method] + SPACED_OVER_MS - performance.now() : 0;
    if (wait > 0) setTimeout(ask, wait); else ask();
  });
  // The participant a host call acts on: the id Zoom lists them by, a whole number, which may come as digits in text. An id that is
  // missing, or is not one, is refused before anything is asked of Zoom.
  const person = (id) => {
    const n = typeof id === "string" ? (/^\d+$/.test(id) ? Number(id) : NaN) : id;
    if (!Number.isSafeInteger(n) || n <= 0) throw new Error("Say which participant");
    return n;
  };
  const ofPerson = (call) => Object.assign((id) => call(person(id)), { ofPerson: true });
  // Renaming the room: Zoom is told who (the room's id, once Zoom has listed it) and what the room was called (its record's name).
  function rename(name) {
    const next = need(name, "name to use");
    if (!known(bridge.userId)) throw new Error(UNLISTED);
    return sdk("rename", { userId: bridge.userId, oldName: bridge.userName || "", newName: next });
  }
  // The SDK answers a record at once and tells of the recording (onRecordingChange) when its server has confirmed it: the answer is
  // the meeting once Zoom has said so, as the hand's is, and a recording nobody confirms is a failure in words.
  const RECORD_MS = 3000;
  async function record() {
    const want = !bridge._recording;
    await sdk("record", { record: want });
    if (!await settle(() => bridge._recording === want, RECORD_MS)) throw new Error("Zoom did not " + (want ? "start" : "stop") + " recording");
  }
  // By the control's name, in a table with no prototype: no name an object has (constructor, toString) is a control. Recording
  // is asked for as the opposite of what Zoom last said of it (its event), locking as the opposite of how it last went.
  const HOST = Object.assign(Object.create(null), {
    "host.mute_all": () => sdk("muteAll", { muteAll: true }),
    "host.ask_unmute": () => sdk("muteAll", { muteAll: false }),
    "host.record": record,
    "host.lock": async () => { const lock = !bridge._locked; await sdk("lockMeeting", { lockMeeting: lock }); bridge._locked = lock; },
    "host.admit": ofPerson((id) => sdk("putOnHold", { userId: id, hold: false })),
    "host.hold": ofPerson((id) => sdk("putOnHold", { userId: id, hold: true })),
    "host.remove": ofPerson((id) => sdk("expel", { userId: id })),
    "host.mute_one": ofPerson((id) => sdk("mute", { userId: id, mute: true })),
    "host.end": () => sdk("endMeeting", {}),
    "rename": rename,
  });
  bridge.control = async function (name, value) {
    if (bridge.state !== "connected") throw new Error("not in a meeting");
    switch (name) {
      case "camera": await bridge.setVideo(!(bridge.video().on)); break;
      case "raise_hand": case "captions": case "hide_self": await pressButton(name); break;
      case "reactions": await pickFromMenu("reactions", value); break;
      case "layout": await pickFromMenu("layout", value); break;
      case "layout.read": await readViewMenu(); break;
      default:
        if (HOST[name]) { await HOST[name](value); break; }      // Zoom's own calls
        throw new Error("Unknown control " + name);
    }
    await syncState().catch(() => {});      // a read that fails does not undo a press that worked: the answer is read again
    return bridge.readMeeting();
  };
  // One at a time, in the order they came, the controls (the presses and Zoom's own calls) and the inventory's presses: presses
  // that overlapped would find each other's popovers. A control that is already waiting or running (the same name and value) is
  // that same press or call: a double tap is one.
  const controlNow = bridge.control;
  let controlChain = Promise.resolve();
  function alone(fn) {
    const run = controlChain.then(fn);
    controlChain = run.then(() => {}, () => {});
    return run;
  }
  const controlsInFlight = new Map();
  // What makes a control the one already running: its name and its value, and for a control on a participant the id as the number
  // it is (16777218 and "16777218" are one participant: Zoom drops the second call of a method made within seconds of the first).
  function keyOf(name, value) {
    let what = value === undefined ? null : value;
    if (HOST[name] && HOST[name].ofPerson) { try { what = person(value); } catch (e) { /* refused when it runs, in words */ } }
    return name + "\u0000" + JSON.stringify(what);
  }
  bridge.control = function (name, value) {
    const key = keyOf(name, value);
    if (controlsInFlight.has(key)) return controlsInFlight.get(key);
    const tracked = alone(() => controlNow(name, value)).finally(() => controlsInFlight.delete(key));
    controlsInFlight.set(key, tracked);
    return tracked;
  };

  // ---- Dialogs: what the client draws over the meeting that can be answered is reported; nothing is pressed here on its own ----
  // Zoom 6.5.0 draws its modals with React, so they are the client's nodes: a dialog is answered only by pressing one of its own
  // buttons, found by what it says as the popovers' items are, and the client closes it; no node of its is ever taken out of the page.
  // The page is watched with a MutationObserver, not a timer, and a burst of changes is looked at once, DIALOG_LOOK_MS after the first
  // of them. A dialog is reported once, with an id of its own that is not used again, and reported closed when its element goes. That
  // starts when the room is in the meeting: the device follows nothing before it, so what is open when the room comes in is said
  // then, and what came and went before is not said at all. A dialog is a modal with a button to press (the consent notice at the top
  // of the meeting is one, DIALOG_SELECTOR names it): a banner with none has nothing to answer from the table and is not one, and a
  // dialog that has lost its buttons is closed.
  const DIALOG_LOOK_MS = 50, DIALOG_MS = 2000;
  const dialogs = new Map();      // element -> id, for the dialogs that are open and have been reported
  let dialogIds = 0;
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const dialogButtons = (el) => Array.from(el.querySelectorAll("button")).filter((b) => clean(labelOf(b)));
  const isOpen = (el) => el.isConnected && el.matches(DIALOG_SELECTOR) && dialogButtons(el).length > 0;
  // Its title, its words and its buttons, as the client has them now. The title is the words of the title node without a button's: the
  // consent banner holds its OK button inside its title node. The words are the other text nodes, not a button's, a script's or a
  // style's, nor the footer's (a checkbox, a link); the banner has none, its description is only in its aria-label ("<title>
  // <description>"), which, with the title taken off its start, is the words when the text nodes give none. A label that does not
  // begin with the title is no description: a modal with no body has a developer's name for its label ("Mute Video Modal").
  function dialogInfo(el, id) {
    const heading = Array.from(el.querySelectorAll("h1, h2, h3, [class*='title']")).find((h) => !h.closest("button"));
    const wordsUnder = (root) => {
      const found = [], walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const node = walker.currentNode, owner = node.parentElement, button = owner && owner.closest("button");
        if (owner && !/^(script|style)$/i.test(owner.tagName) && !(button && el.contains(button))) found.push(node);
      }
      return found;
    };
    const footerOf = (node) => { const footer = node.parentElement.closest(".zm-modal-footer"); return !!footer && el.contains(footer); };
    const title = heading ? clean(wordsUnder(heading).map((n) => n.nodeValue).join("")) : "";
    let text = wordsUnder(el).filter((n) => !(heading && heading.contains(n)) && !footerOf(n)).map((n) => clean(n.nodeValue)).filter(Boolean).join(" ");
    if (!text) {
      const label = clean(el.getAttribute("aria-label"));
      text = title && label.startsWith(title) ? clean(label.slice(title.length)) : "";
    }
    return { id: id, title: title, text: text.slice(0, 400), buttons: dialogButtons(el).map((b) => clean(labelOf(b))) };
  }
  function scanDialogs() {
    if (bridge.state !== "connected") return;
    const open = new Set();
    for (const el of document.querySelectorAll(DIALOG_SELECTOR)) {
      if (el.parentElement && el.parentElement.closest(DIALOG_SELECTOR)) continue;      // a part of another modal, which is the dialog
      if (!isOpen(el)) continue;
      if (!dialogs.has(el)) {
        const id = ++dialogIds;
        dialogs.set(el, id);
        reportJson("dialog", dialogInfo(el, id));
      }
      open.add(el);
    }
    for (const [el, id] of Array.from(dialogs)) {
      if (!open.has(el)) { dialogs.delete(el); reportJson("dialog-closed", { id: id }); }
    }
  }
  let dialogTimer = null;
  new MutationObserver(() => {
    if (dialogTimer !== null || bridge.state !== "connected") return;
    dialogTimer = setTimeout(() => { dialogTimer = null; scanDialogs(); }, DIALOG_LOOK_MS);
  }).observe(document.body, { childList: true, subtree: true });
  bridge.dialogs = () => Array.from(dialogs, ([el, id]) => dialogInfo(el, id));
  // Press the button the label names and see the dialog go: the client closes it when it takes the press, and a press it does not
  // take is a failure in words, not an answer. It is one of the page's presses, so it waits its turn among the controls. The look that
  // sees the dialog gone is made here too, so the device has been told it is gone before it is answered.
  async function answerNow(id, label) {
    const wanted = need(label, "button");
    const entry = Array.from(dialogs).find(([, i]) => i === Number(id));
    if (!entry || !isOpen(entry[0])) throw new Error("That dialog is no longer on the screen");
    const el = entry[0];
    const button = dialogButtons(el).find((b) => named(b, [norm(wanted)]));
    if (!button) throw new Error("The dialog has no button called " + wanted);
    if (unready(button)) throw new Error("The dialog's " + wanted + " button is not ready yet; try again in a moment");
    button.click();
    if (!await settle(() => !isOpen(el), DIALOG_MS)) throw new Error("Zoom's dialog did not close when " + wanted + " was pressed; try again in a moment");
    scanDialogs();
    return true;
  }
  bridge.answerDialog = (id, label) => alone(() => answerNow(id, label));

  // ---- Chat: every message the client receives is reported; the chat panel is not opened or drawn here ----
  // The moment a message came, from a stamp: milliseconds as a number or as text, seconds as ten digits (or fewer) of text, or a date as
  // ISO text. A stamp that is no date this century is no moment ("10.32" read as a number is 10 ms of 1970, and Zoom's own `timestamp`
  // is only a time of day), and neither is text that is not an ISO date, whatever a lenient parser makes of it (V8 reads "Nov 5" as
  // November 5, 2001), nor a moment more than a day after now (ten digits of seconds are this century, eleven are the year 5138).
  const momentOf = (stamp) => {
    const n = Number(stamp);
    const ms = Number.isFinite(n) ? (n < 1e11 ? n * 1000 : n) : (typeof stamp === "string" && /^\d{4}-\d\d-\d\d/.test(stamp) ? Date.parse(stamp) : NaN);
    return ms >= Date.UTC(2000, 0, 1) && ms <= Date.now() + 864e5 ? new Date(ms).toISOString() : null;
  };
  function chatListener(data) {
    const sender = pick(data, ["sender"]);
    const from = pick(data, ["senderName", "from", "userName"]) || (typeof sender === "string" ? sender : sender && (sender.name || sender.userName || sender.displayName)) || "";
    // Zoom 6.5.0 sends {sender: "Ann", timestamp: "10:32 AM", type: "text", content: {text, t: epoch ms}}, and for a file type "file" and
    // content: {file: {fileName}, t}: the words are content.text (a file's are its name) and the moment is content.t, not `timestamp`.
    // Other builds send the words themselves, as content, message, text or msg.
    const body = pick(data, ["content", "message", "text", "msg"]);
    const record = body !== null && typeof body === "object" ? body : null;
    const text = record ? (record.text || (record.file && record.file.fileName) || "") : (body || "");
    const stamps = [record && record.t, pick(data, ["timestamp", "timeStamp", "time"])];
    reportJson("chat", { from: String(from), text: String(text), at: stamps.map(momentOf).find(Boolean) || new Date().toISOString() });
  }

  // The join's mic-off default goes out as soon as the room's id is known: at once, or when a later read first finds it (Zoom
  // builds the current user's record from its roster, so the first record can have no id), and only once. It is an action,
  // so the meeting is read again once Zoom has answered it: a read that found the id has the record from before the mute.
  function applyPendingMute() {
    if (bridge._pendingMute && known(bridge.userId)) {
      bridge._pendingMute = false;
      bridge.mute(true).catch(() => {}).then(() => queueSync());
    }
  }
  // Connected is reported once the current user's record has been asked for.
  function connected(params) {
    if (bridge.state === "connected") return;
    const finish = () => {
      report("connected", "");
      // The room's mic-off default is armed once per join: a reconnect must not undo an unmute the table made since. Armed,
      // it waits for the room's id, across a reconnect too.
      if (!bridge.mutedAtJoin) { bridge.mutedAtJoin = true; bridge._pendingMute = params.micOn === false; }
      applyPendingMute();
      syncState().catch(() => {});
      startPolling();
      scanDialogs();      // what the client has drawn over the meeting so far, after what the join needs of this has been done
    };
    ZoomMtg.getCurrentUser({
      success: (result) => {
        const user = result && result.result && result.result.currentUser;
        takeUser(user);   // a few fields and no user this early: readMeeting fills in what is missing
        if (user && known(user.muted)) bridge.muted = !!user.muted;
        finish();
      },
      error: finish,
    });
  }

  async function main() {
    if (typeof window.ZoomMtg === "undefined") {
      const failed = (window.__zoomLoadErrors || []).join(", ");
      report("error", "Zoom's SDK did not load from source.zoom.us" + (failed ? " (" + failed + ")" : "")
        + "; check the device's internet access and that this SDK version is still published");
      return;
    }
    const token = location.hash.replace(/^#/, "");
    let params;
    try {
      const response = await fetch("/join/" + encodeURIComponent(token));
      if (!response.ok) throw new Error("join parameters expired (" + response.status + ")");
      params = await response.json();
    } catch (e) {
      report("error", "Could not read the join parameters: " + e.message);
      return;
    }
    const meta = document.querySelector('meta[name="sdk-version"]');
    const version = params.sdkVersion || (meta && meta.content) || "6.5.0";
    ZoomMtg.setZoomJSLib("https://source.zoom.us/" + version + "/lib", "/av");
    ZoomMtg.preLoadWasm();
    ZoomMtg.prepareWebSDK();
    ZoomMtg.inMeetingServiceListener("onMeetingStatus", (data) => {
      const state = STATUS[statusCode(data)] || "joining";
      if (state === "connected") { connected(params); return; }
      report(state, "");
    });
    // Zoom also sends this to a host for other people entering or leaving the waiting room (self: false), and with
    // isInWaitingRoom: false when this room is let in: only this room's own wait is a state of the page. It can also come up
    // to a second and a half late: a room that is already in the meeting is not waiting.
    ZoomMtg.inMeetingServiceListener("onUserIsInWaitingRoom", (data) => {
      if (bridge.state !== "connected" && data && (data.self === undefined || data.self) && data.isInWaitingRoom) {
        report("waiting", "Waiting for the host to let the room in");
      }
    });
    ZoomMtg.inMeetingServiceListener("onUserUpdate", () => queueSync());
    ZoomMtg.inMeetingServiceListener("onUserJoin", () => queueSync());
    ZoomMtg.inMeetingServiceListener("onUserLeave", () => queueSync());
    // Zoom sends {recording: "stop" | "recording" | "pause", hasLocalRecord}: once as its meeting UI mounts (with "stop"),
    // then on every change. A boolean is taken as it is, for a build that sends one.
    ZoomMtg.inMeetingServiceListener("onRecordingChange", (data) => {
      const state = data && data.recording;
      bridge._recording = typeof state === "boolean" ? state : state === "recording";
      queueSync();
    });
    ZoomMtg.inMeetingServiceListener("onReceiveChatMsg", chatListener);
    report("joining", "");
    ZoomMtg.init({
      leaveUrl: "/left",
      disablePreview: true,
      disableInvite: true,
      disableRecord: false,
      showMeetingHeader: false,
      isSupportChat: true,
      isSupportCC: true,
      leaveOnPageUnload: true,
      patchJsMedia: true,
      disableCORP: !window.crossOriginIsolated,
      success: () => {
        const join = {
          signature: params.signature,
          meetingNumber: String(params.meetingNumber),
          passWord: params.passWord || "",
          userName: params.userName,
          userEmail: "",
          // Zoom normally reports connected through onMeetingStatus; if it never does, join's success is enough.
          success: () => { setTimeout(() => { if (bridge.state === "joining") connected(params); }, params.connectGraceMs || 5000); },
          error: (e) => report("error", words(e)),
        };
        if (params.zak) join.zak = params.zak;
        ZoomMtg.join(join);
      },
      error: (e) => report("error", "Zoom's SDK could not start: " + words(e)),
    });
  }

  main();
})();
