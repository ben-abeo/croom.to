// The bridge between the room device and Zoom's Meeting SDK (Client View).
// Reads one-time join parameters, joins, and reports every status change to the
// device through window.crystalMeetEvent (exposed by the provider).
(function () {
  "use strict";

  const STATUS = { 1: "joining", 2: "connected", 3: "left", 4: "reconnecting" };
  const bridge = { state: "loading", detail: "", userId: null, userName: null, muted: false };
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
      if (bridge.userId === null) { reject(new Error("not in a meeting")); return; }
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
  // Zoom's getAttendeeslist answers only when its roster is not empty and calls neither callback otherwise, and the roster
  // fills a moment after "connected". A reading must never hang on that: nothing here waits longer than ANSWER_MS for Zoom,
  // and a silence is read as the last answer was (the roster at first empty, the current user at first unknown).
  const ANSWER_MS = 2000;
  let lastRoster = [];
  let lastUser = null;
  function ask(method, silence, take) {
    return new Promise((resolve) => {
      const timer = setTimeout(() => resolve(silence()), ANSWER_MS);
      try {
        ZoomMtg[method]({
          success: (result) => { clearTimeout(timer); resolve(take(result)); },
          error: () => { clearTimeout(timer); resolve(take(null)); },
        });
      } catch (e) { clearTimeout(timer); throw e; }
    });
  }
  function currentUser() {
    return ask("getCurrentUser", () => lastUser, (result) => {
      const user = (result && result.result && result.result.currentUser) || null;
      if (user) lastUser = user;
      return user;
    });
  }
  // Zoom's web client keeps its video button in the footer: #foot-bar .send-video-container__btn. Its visible text is
  // always "Video"; its accessibility name is "start my video" / "stop my video" only while the camera is usable. Other
  // builds name it "Start Video" / "Stop Video": a footer button so named is the one when the class is not there.
  const VIDEO_NAME = /^(start|stop)( my)? video$/i;
  function videoButton() {
    const real = document.querySelector("#foot-bar .send-video-container__btn");
    if (real) return real;
    const footer = document.getElementById("wc-footer");
    if (!footer) return null;
    for (const b of footer.querySelectorAll("button")) {
      if (VIDEO_NAME.test((b.getAttribute("aria-label") || "").trim()) || VIDEO_NAME.test((b.textContent || "").trim())) return b;
    }
    return null;
  }
  // {on, label} when the button says what the camera does; usable: false beside them when the button cannot be pressed now
  // (while video starts it is aria-disabled and ignores clicks); {on: null, usable: false, label} when it is there but says
  // just "Video" (the camera is not usable); {on: null, label: null} with no button on the screen.
  bridge.video = function () {
    const b = videoButton();
    if (!b) return { on: null, label: null };
    const label = (b.getAttribute("aria-label") || b.textContent || "").trim();
    const name = label.toLowerCase();
    const stop = name.includes("stop"), start = name.includes("start");
    if (stop === start) return { on: null, usable: false, label: label };
    if (b.getAttribute("aria-disabled") === "true") return { on: stop, usable: false, label: label };
    return { on: stop, label: label };
  };
  // Press the button only when Zoom's state differs from the wanted one; settle on what the button says afterwards.
  bridge.setVideo = function (on) {
    return new Promise((resolve, reject) => {
      const before = bridge.video();
      if (before.on === null) { reject(new Error("Zoom's camera button is not on the screen yet; try again in a moment")); return; }
      if (before.on === !!on) { resolve(before.on); return; }
      if (before.usable === false) { reject(new Error("Zoom's camera button is not ready yet; try again in a moment")); return; }
      videoButton().click();
      let tries = 0;
      const settle = () => {
        const now = bridge.video();
        if (now.on === !!on || tries++ >= 20) { resolve(now.on === null ? before.on : now.on); return; }
        setTimeout(settle, 100);
      };
      setTimeout(settle, 100);
    });
  };
  function attendees() {
    return ask("getAttendeeslist", () => lastRoster, (result) => {
      const list = result && result.result && result.result.attendeesList;
      if (list) lastRoster = list;
      return list || [];
    });
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
  // `videoOn` is what the user's record says of the camera, for a button that cannot say it itself.
  function buttonsSeen(videoOn) {
    const out = {};
    const video = bridge.video();
    if (video.label !== null) {
      out.camera = video.usable === false ? { on: video.on !== null ? video.on : videoOn, usable: false } : { on: video.on };
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
  // out is not read as off: the last known mute stays, and a camera nobody speaks of is unknown (null).
  bridge.readMeeting = async function () {
    const user = await currentUser();
    const list = await attendees();
    const heardMuted = pick(user, ["muted", "bMuted", "isMuted"]);
    const video = bridge.video();
    const heardVideo = video.on !== null ? video.on : pick(user, ["video", "bVideoOn", "videoOn", "isVideoOn"]);
    const id = pick(user, ["userId"]);
    const name = pick(user, ["userName", "displayName"]);
    if (heardMuted !== undefined) bridge.muted = !!heardMuted;
    if (id !== undefined && (bridge.userId === null || bridge.userId === undefined)) bridge.userId = id;
    if (name !== undefined && !bridge.userName) bridge.userName = String(name);
    const me = {
      userId: id === undefined ? bridge.userId : id,
      muted: heardMuted === undefined ? bridge.muted : !!heardMuted,
      videoOn: heardVideo === undefined ? null : !!heardVideo,
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
  // The current user's own part of it, as the first version of the bridge answered.
  bridge.readState = async function () {
    const m = await bridge.readMeeting();
    return Object.assign({}, m.me, { keys: m.keys });
  };
  // What the page sees, for pinning Zoom's real field names and button wording from a device.
  bridge.diagnostics = async function () {
    const state = await bridge.readState();
    const buttons = Array.from(document.querySelectorAll("button")).map((b) => ({
      label: (b.getAttribute("aria-label") || "").trim(), text: (b.textContent || "").trim().slice(0, 40),
    })).filter((b) => b.label || b.text);
    return { user_keys: state.keys, state: state, buttons: buttons, page: document.title };
  };
  // A change Zoom makes (the host muting the room, someone raising a hand, a person joining, recording starting) reaches
  // the device as a "state" event: the whole meeting is read on every such change and every five seconds, and reported
  // only when something about it changed. The detail is the model with the current user's fields at the top level too,
  // for a provider that still reads muted and videoOn from there.
  let lastSeen = null;
  async function readAndReport() {
    if (bridge.state !== "connected") return;
    const now = await bridge.readMeeting();
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
  // it, so a late answer can never overtake a newer one and a change made during a read is not lost.
  let syncing = null;
  let syncAgain = false;
  function syncState() {
    if (syncing) { syncAgain = true; return syncing; }
    syncing = (async () => {
      try {
        do { syncAgain = false; await readAndReport(); } while (syncAgain);
      } finally { syncing = null; syncAgain = false; }
    })();
    return syncing;
  }
  // Zoom calls its listeners from inside its own state reducer, where getCurrentUser throws: a listener only queues the
  // read, and a burst of changes is read once, 50 ms after the first of them.
  let syncTimer = null;
  function queueSync() {
    if (syncTimer !== null) return;
    syncTimer = setTimeout(() => { syncTimer = null; syncState().catch(() => {}); }, 50);
  }
  // The five-second read is one timer for the whole page: a reconnect does not start another.
  let pollTimer = null;
  function startPolling() {
    if (pollTimer === null) pollTimer = setInterval(() => { syncState().catch(() => {}); }, 5000);
  }

  // Connected is reported once the current user is known, so mute works right away.
  function connected(params) {
    if (bridge.state === "connected") return;
    const finish = () => {
      report("connected", "");
      if (params.micOn === false && bridge.userId !== null) bridge.mute(true).catch(() => {});
      syncState().catch(() => {});
      startPolling();
    };
    ZoomMtg.getCurrentUser({
      success: (result) => {
        const user = result && result.result && result.result.currentUser;
        if (user) {   // {} this early: readMeeting fills in what is missing
          if (user.userId !== undefined) bridge.userId = user.userId;
          if (user.userName !== undefined) bridge.userName = String(user.userName);
          if (user.muted !== undefined) bridge.muted = !!user.muted;
        }
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
    // isInWaitingRoom: false when this room is let in: only this room's own wait is a state of the page.
    ZoomMtg.inMeetingServiceListener("onUserIsInWaitingRoom", (data) => {
      if (data && (data.self === undefined || data.self) && data.isInWaitingRoom) report("waiting", "Waiting for the host to let the room in");
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
