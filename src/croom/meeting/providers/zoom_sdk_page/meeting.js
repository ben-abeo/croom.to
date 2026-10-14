// The bridge between the room device and Zoom's Meeting SDK (Client View).
// Reads one-time join parameters, joins, and reports every status change to the
// device through window.crystalMeetEvent (exposed by the provider).
(function () {
  "use strict";

  const STATUS = { 1: "joining", 2: "connected", 3: "left", 4: "reconnecting" };
  const bridge = { state: "loading", detail: "", userId: null, muted: false };
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
  function currentUser() {
    return new Promise((resolve) => {
      ZoomMtg.getCurrentUser({
        success: (result) => resolve((result && result.result && result.result.currentUser) || null),
        error: () => resolve(null),
      });
    });
  }
  // Zoom's web client names the video button "start my video" / "stop my video"; other builds say "Start Video".
  // Any button whose name or text speaks of video and of starting or stopping is the one.
  function videoButton() {
    for (const b of document.querySelectorAll("button")) {
      const name = ((b.getAttribute("aria-label") || "") + " " + (b.textContent || "")).toLowerCase();
      if (name.includes("video") && (name.includes("start") || name.includes("stop"))) return b;
    }
    return null;
  }
  bridge.video = function () {
    const b = videoButton();
    if (!b) return { on: null, label: null };
    const label = (b.getAttribute("aria-label") || b.textContent || "").trim();
    return { on: label.toLowerCase().includes("stop"), label: label };
  };
  // Press the button only when Zoom's state differs from the wanted one; settle on what the button says afterwards.
  bridge.setVideo = function (on) {
    return new Promise((resolve, reject) => {
      const before = bridge.video();
      if (before.on === null) { reject(new Error("Zoom's camera button is not on the screen yet; try again in a moment")); return; }
      if (before.on === !!on) { resolve(before.on); return; }
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
  bridge.readState = async function () {
    const user = await currentUser();
    const video = bridge.video();
    const muted = pick(user, ["muted", "bMuted", "isMuted"]);
    const videoOn = video.on !== null ? video.on : pick(user, ["bVideoOn", "videoOn", "isVideoOn"]);
    if (muted !== undefined) bridge.muted = !!muted;
    return {
      muted: muted === undefined ? bridge.muted : !!muted,
      videoOn: videoOn === undefined ? null : !!videoOn,
      isHost: !!pick(user, ["isHost", "bHost"]),
      isCoHost: !!pick(user, ["isCoHost", "isCohost", "bCoHost"]),
      raisedHand: !!pick(user, ["bRaiseHand", "raiseHand", "isHandRaised", "bHandRaised"]),
      keys: user ? Object.keys(user) : [],
    };
  };
  // What the page sees, for pinning Zoom's real field names and button wording from a device.
  bridge.diagnostics = async function () {
    const state = await bridge.readState();
    const buttons = Array.from(document.querySelectorAll("button")).map((b) => ({
      label: (b.getAttribute("aria-label") || "").trim(), text: (b.textContent || "").trim().slice(0, 40),
    })).filter((b) => b.label || b.text);
    return { user_keys: state.keys, state: state, buttons: buttons, page: document.title };
  };
  // A change Zoom makes (the host muting the room, video stopped by the host) reaches the device as a "state" event:
  // read on every participant update and every five seconds, reported only when something about this room changed.
  let lastSeen = null;
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

  // Connected is reported once the current user is known, so mute works right away.
  function connected(params) {
    if (bridge.state === "connected") return;
    const finish = () => {
      report("connected", "");
      if (params.micOn === false && bridge.userId !== null) bridge.mute(true).catch(() => {});
      syncState().then(() => setInterval(syncState, 5000));
    };
    ZoomMtg.getCurrentUser({
      success: (result) => {
        const user = result && result.result && result.result.currentUser;
        if (user) { bridge.userId = user.userId; bridge.muted = !!user.muted; }
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
    ZoomMtg.inMeetingServiceListener("onUserUpdate", () => { syncState(); });
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
