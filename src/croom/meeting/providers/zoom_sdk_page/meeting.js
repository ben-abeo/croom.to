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

  bridge.mute = function (muted) {
    return new Promise((resolve, reject) => {
      if (!known(bridge.userId)) {
        // Connected, but Zoom has not listed the room yet (its record of the current user is built from its roster).
        reject(new Error(bridge.state === "connected" ? "Zoom has not listed the room in the meeting yet; try again in a moment" : "not in a meeting"));
        return;
      }
      ZoomMtg.mute({
        userId: bridge.userId, mute: !!muted,
        success: () => { bridge.muted = !!muted; resolve(bridge.muted); },
        error: (e) => reject(new Error(words(e))),
      });
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
  const pick = (obj, names) => { for (const n of names) { if (obj && obj[n] !== undefined) return obj[n]; } return undefined; };
  const known = (value) => value !== undefined && value !== null && value !== "";
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
  // (spec section 7) confirms these words; a button not found is simply not offered.
  const BUTTONS = {
    raise_hand: { words: [["raise hand"], ["lower hand"]], on: (label) => label.includes("lower") },
    reactions: { words: [["reaction"]], on: null },
    captions: { words: [["caption", "show"], ["caption", "hide"], ["closed caption"]], on: (label) => label.includes("hide") },
    layout: { words: [["view"]], not: ["self view"], on: null },
    hide_self: { words: [["self view"]], on: (label) => label.includes("show") },
  };
  function labelOf(b) { return ((b.getAttribute("aria-label") || b.textContent || "")).trim(); }
  // Zoom's own footer first, then the rest of the page; never a button that is an item of a menu.
  function findButton(name) {
    const spec = BUTTONS[name];
    const footer = document.getElementById("wc-footer");
    for (const scope of footer ? [footer, document] : [document]) {
      for (const b of scope.querySelectorAll("button")) {
        if (b.closest('[role="menu"]')) continue;
        const label = labelOf(b).toLowerCase();
        if (spec.not && spec.not.some((w) => label.includes(w))) continue;
        if (spec.words.some((all) => all.every((w) => label.includes(w)))) return b;
      }
    }
    return null;
  }
  // `videoOn` is what the user's record says of the camera, for a button that cannot say it itself. The camera is usable
  // while Zoom's button names a camera it can use and takes a press (not disabled: video starting, a switch in progress).
  function buttonsSeen(videoOn) {
    const out = {};
    const video = bridge.video();
    if (video.label !== null) {
      out.camera = video.usable && !video.disabled ? { on: video.on } : { on: video.on !== null ? video.on : videoOn, usable: false };
    }
    for (const name of Object.keys(BUTTONS)) {
      const b = findButton(name);
      if (!b) continue;
      const spec = BUTTONS[name];
      if (name === "layout") out.layout = { value: bridge._layout || "speaker" };
      else if (spec.on) out[name] = { on: spec.on(labelOf(b).toLowerCase()) };
      else out[name] = {};
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
      raisedHand: !!pick(user, ["raiseHand", "bRaiseHand", "isHandRaised", "bHandRaised"]),
    };
    const participants = list.map((a) => ({
      id: pick(a, ["userId"]), name: String(pick(a, ["userName", "displayName", "name"]) || ""),
      muted: !!pick(a, ["muted", "bMuted"]), video: !!pick(a, ["video", "bVideoOn", "videoOn"]),
      hand: !!pick(a, ["raiseHand", "bRaiseHand", "bHandRaised"]), host: !!pick(a, ["isHost", "bHost"]),
      waiting: !!pick(a, ["isHold", "waiting", "bHold", "isInWaitingRoom"]), me: pick(a, ["userId"]) === me.userId,
    }));
    return {
      me: me, role: me.isHost ? "host" : (me.isCoHost ? "co-host" : "participant"),
      buttons: buttonsSeen(me.videoOn), participants: participants, count: participants.length,
      recording: !!bridge._recording, keys: user ? Object.keys(user) : [],
    };
  };
  bridge._recording = false;
  bridge._layout = null;
  // The current user's own part of it, as the first version of the bridge answered: mute and video null when Zoom has not
  // said, never a guess.
  bridge.readState = async function () {
    const m = await bridge.readMeeting();
    return Object.assign({}, m.me, { keys: m.keys });
  };
  // What the page sees, for pinning Zoom's real field names and button wording from a device: the footer's buttons only.
  bridge.diagnostics = async function () {
    const state = await bridge.readState();
    const buttons = [];
    const footer = document.getElementById("wc-footer");
    for (const b of footer ? footer.querySelectorAll("button") : []) {
      const label = (b.getAttribute("aria-label") || "").trim();
      const text = (b.textContent || "").replace(/\s+/g, " ").trim().slice(0, 40);
      if (!label && !text) continue;
      buttons.push(text && text !== label ? { label: label, text: text } : { label: label });
      if (buttons.length >= 40) break;
    }
    return { user_keys: state.keys, state: state, video: bridge.video(), buttons: buttons, page: document.title };
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
    if (!first && typeof window.crystalMeetEvent === "function") {
      try { window.crystalMeetEvent("state", JSON.stringify(Object.assign({}, now.me, now))); } catch (e) { /* the device is not listening */ }
    }
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

  // ---- Controls: the client's own buttons, pressed for the table ----
  // The hand, reactions, captions, the view and self view have no SDK call: the bridge presses the client's own button, found
  // by what it says (BUTTONS), gives the client a bounded moment to show the change, and answers with the meeting as Zoom then
  // has it, never with the state the press was meant to reach. The camera goes through setVideo, which knows its button.
  const settle = (done, ms) => new Promise((resolve) => {
    let tries = 0;
    const tick = () => { if (done() || tries++ >= Math.ceil(ms / 100)) resolve(); else setTimeout(tick, 100); };
    setTimeout(tick, 100);
  });
  const noButton = (name) => new Error("Zoom's " + name.replace("_", " ") + " button is not on the screen yet; try again in a moment");
  // Never leave a menu open on the TV.
  function closeMenus() { for (const m of document.querySelectorAll('[role="menu"]')) m.remove(); }
  async function pressButton(name) {
    const b = findButton(name);
    if (!b) throw noButton(name);
    const before = labelOf(b);
    b.click();
    await settle(() => labelOf(b) !== before, 2000);
  }
  // Open the menu, pick the item the value names (by its text, a data attribute or its label), and close the menu if the
  // client has not. The client's buttons do not say which view is showing: the bridge keeps the last one it picked.
  async function pickFromMenu(name, value) {
    const b = findButton(name);
    if (!b) throw noButton(name);
    b.click();
    await settle(() => document.querySelector('[role="menu"], [role="menuitem"]'), 2000);
    const wanted = String(value).toLowerCase();
    const items = Array.from(document.querySelectorAll('[role="menuitem"], [role="menu"] button'));
    const item = items.find((i) => {
      const text = (i.textContent || "").trim().toLowerCase();
      const data = Object.values(i.dataset || {}).map((v) => String(v).toLowerCase());
      return text === wanted || data.includes(wanted) || (i.getAttribute("aria-label") || "").toLowerCase() === wanted;
    });
    if (!item) {
      closeMenus();
      throw new Error("Zoom offers no " + value + " here");
    }
    item.click();
    await settle(() => !document.querySelector('[role="menu"]'), 1000);
    closeMenus();
    if (name === "layout") bridge._layout = String(value);
  }
  bridge.control = async function (name, value) {
    switch (name) {
      case "camera": await bridge.setVideo(!(bridge.video().on)); break;
      case "raise_hand": case "captions": case "hide_self": await pressButton(name); break;
      case "reactions": await pickFromMenu("reactions", value); break;
      case "layout": await pickFromMenu("layout", value); break;
      default: throw new Error("Unknown control " + name);
    }
    await syncState();
    return bridge.readMeeting();
  };

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
    report("joining", "");
    ZoomMtg.init({
      leaveUrl: "/left",
      disablePreview: true,
      disableInvite: true,
      disableRecord: true,
      showMeetingHeader: false,
      isSupportChat: false,
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
