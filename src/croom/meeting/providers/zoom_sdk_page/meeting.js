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

  // Connected is reported once the current user is known, so mute works right away.
  function connected(params) {
    if (bridge.state === "connected") return;
    const finish = () => {
      report("connected", "");
      if (params.micOn === false && bridge.userId !== null) bridge.mute(true).catch(() => {});
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
