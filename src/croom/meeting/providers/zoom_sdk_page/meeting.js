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
      if (bridge.userId === null) { reject(new Error("Zoom has not listed the room in the meeting yet; try again in a moment")); return; }
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
    if (known(user.userName)) bridge.userName = user.userName;
  }
  function currentUser() {
    return new Promise((resolve) => {
      ZoomMtg.getCurrentUser({
        success: (result) => resolve((result && result.result && result.result.currentUser) || null),
        error: () => resolve(null),
      });
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
  // Press the button only when Zoom's state differs: wait for Zoom to enable it (readyMs), press once, and wait for Zoom to
  // switch (switchMs, disabled meanwhile). Whatever does not happen is said in words: an unchanged state is never a success.
  bridge.setVideo = function (on, waits) {
    const want = !!on;
    const readyMs = (waits && waits.readyMs) || 8000;
    const switchMs = (waits && waits.switchMs) || 10000;
    return new Promise((resolve, reject) => {
      const first = bridge.video();
      if (first.label === null) { reject(new Error(NO_BUTTON)); return; }
      if (!first.usable) { reject(new Error("Zoom cannot use the camera right now")); return; }
      if (first.on === want) { resolve(want); return; }
      let pressed = false;
      let deadline = Date.now() + readyMs;
      const step = () => {
        const v = bridge.video();
        if (v.on === want) { resolve(want); return; }
        if (v.label !== null && !v.usable) { reject(new Error("Zoom cannot use the camera right now")); return; }
        if (!pressed && v.label !== null && !v.disabled) {
          videoButton().click();
          pressed = true;
          deadline = Date.now() + switchMs;
        } else if (Date.now() >= deadline) {
          if (v.label === null) reject(new Error(NO_BUTTON));
          else reject(new Error(pressed ? "Zoom did not switch the camera" : "Zoom's camera button is still starting; try again in a moment"));
          return;
        }
        setTimeout(step, 100);
      };
      step();
    });
  };
  // Mute from the room's record, video from the button (from the record while the button cannot say); null when Zoom has
  // not said, never a guess.
  bridge.readState = async function () {
    const user = await currentUser();
    takeUser(user);
    const video = bridge.video();
    const muted = pick(user, ["muted", "bMuted", "isMuted"]);
    const recorded = pick(user, ["video", "bVideoOn", "videoOn", "isVideoOn"]);
    if (known(muted)) bridge.muted = !!muted;
    return {
      muted: known(muted) ? !!muted : null,
      videoOn: video.on !== null ? video.on : (known(recorded) ? !!recorded : null),
      isHost: !!pick(user, ["isHost", "bHost"]),
      isCoHost: !!pick(user, ["isCoHost", "isCohost", "bCoHost"]),
      raisedHand: !!pick(user, ["raiseHand", "bRaiseHand", "isHandRaised", "bHandRaised"]),
      keys: user ? Object.keys(user) : [],
    };
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
  // A change Zoom makes (the host muting the room, video stopped by the host) reaches the device as a "state" event:
  // read after every participant update and every five seconds, reported only when something about this room changed.
  let lastSeen = null;
  let poll = null;
  let syncQueued = false;
  async function syncState() {
    if (bridge.state !== "connected") return;
    const now = await bridge.readState();
    const key = JSON.stringify([now.muted, now.videoOn, now.isHost, now.isCoHost, now.raisedHand]);
    if (key === lastSeen) return;
    const first = lastSeen === null;
    lastSeen = key;
    if (!first && typeof window.crystalMeetEvent === "function") {
      try { window.crystalMeetEvent("state", JSON.stringify(now)); } catch (e) { /* the device is not listening */ }
    }
  }
  // Zoom calls onUserUpdate from inside its Redux reducer, where reading the record throws ("Minified Redux error #3"):
  // the read is made a moment later, once, however many updates came meanwhile.
  function queueSync() {
    if (syncQueued) return;
    syncQueued = true;
    setTimeout(() => { syncQueued = false; syncState().catch(() => {}); }, 50);
  }

  // Connected is reported once the current user is known, so mute works right away.
  function connected(params) {
    if (bridge.state === "connected") return;
    const finish = () => {
      report("connected", "");
      if (params.micOn === false && bridge.userId !== null) bridge.mute(true).catch(() => {});
      syncState().catch(() => {});
      if (poll === null) poll = setInterval(() => { syncState().catch(() => {}); }, 5000);   // one, however often Zoom reconnects
    };
    ZoomMtg.getCurrentUser({
      success: (result) => {
        const user = result && result.result && result.result.currentUser;
        takeUser(user);
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
    ZoomMtg.inMeetingServiceListener("onUserIsInWaitingRoom", () => report("waiting", "Waiting for the host to let the room in"));
    ZoomMtg.inMeetingServiceListener("onUserUpdate", queueSync);
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
