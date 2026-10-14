"""
A stand-in for Zoom's Meeting SDK (window.ZoomMtg) so the page and the provider
can be driven in the venv's Chromium without reaching Zoom. Behaviour flags:
window.ZoomMtg._behaviour = {waiting, errorFor, errorCode, errorMessage, neverConnect, delayMs, pageSays, ...}; join
fails with {errorCode, errorMessage} for the meeting number errorFor, and pageSays is text join puts on the page, as
Zoom's own dialogs do. labelStyle "client" names the video button the way Zoom's web client does (aria-label "start my
video", text "Start Video"); noToolbar leaves the button out; videoStuckOff makes the button ignore clicks.
window.ZoomMtg._setMuted(bool) is the host muting or unmuting the room: it changes what getCurrentUser says and fires
onUserUpdate, as Zoom does. window.__videoClicks counts clicks on the video button.
"""

STUB_JS = r"""
window.__zoomCalls = [];
window.ZoomMtg = {
  _listeners: {},
  _behaviour: { waiting: false, errorFor: "999", errorCode: 3712, errorMessage: "This meeting ID is not valid",
                neverConnect: false, delayMs: 50, endAfterMs: 0, pageSays: "",
                mutedOnEntry: false, videoOffOnEntry: false, silentConnect: false, statusField: "meetingStatus",
                labelStyle: "sdk", noToolbar: false, videoStuckOff: false },
  _muted: false, _videoOn: true,
  _setMuted(muted) { this._muted = !!muted; this._fire("onUserUpdate", { userId: 16777216 }); },
  _labelVideo(button) {
    const on = this._videoOn;
    if (this._behaviour.labelStyle === "client") {
      button.setAttribute("aria-label", on ? "stop my video" : "start my video");
      button.textContent = on ? "Stop Video" : "Start Video";
    } else {
      button.setAttribute("aria-label", on ? "Stop Video" : "Start Video");
    }
  },
  _status(code) { const data = {}; data[this._behaviour.statusField] = code; this._fire("onMeetingStatus", data); },
  setZoomJSLib(path, av) { window.__zoomCalls.push(["setZoomJSLib", path, av]); },
  preLoadWasm() { window.__zoomCalls.push(["preLoadWasm"]); },
  prepareWebSDK() { window.__zoomCalls.push(["prepareWebSDK"]); },
  inMeetingServiceListener(name, cb) { this._listeners[name] = cb; },
  _fire(name, data) { if (this._listeners[name]) this._listeners[name](data); },
  init(opts) {
    const copy = Object.assign({}, opts); delete copy.success; delete copy.error;
    window.__zoomCalls.push(["init", copy]);
    setTimeout(() => opts.success && opts.success(), 10);
  },
  join(opts) {
    const copy = Object.assign({}, opts); delete copy.success; delete copy.error;
    window.__zoomCalls.push(["join", copy]);
    const b = this._behaviour;
    if (b.pageSays) {
      const said = document.createElement("div");
      said.textContent = b.pageSays;
      document.body.appendChild(said);
    }
    if (String(opts.meetingNumber) === b.errorFor) {
      setTimeout(() => opts.error && opts.error({ errorCode: b.errorCode, errorMessage: b.errorMessage }), 10);
      return;
    }
    if (b.neverConnect) return;
    setTimeout(() => this._status(1), 10);
    if (b.waiting) setTimeout(() => this._fire("onUserIsInWaitingRoom", {}), 20);
    this._muted = b.mutedOnEntry;
    this._videoOn = !b.videoOffOnEntry;
    window.__videoClicks = 0;
    setTimeout(() => {
      if (!b.noToolbar) {
        const footer = document.createElement("div");
        footer.className = "footer";
        const button = document.createElement("button");
        button.id = "stub-video";
        this._labelVideo(button);
        button.addEventListener("click", () => {
          window.__videoClicks += 1;
          if (b.videoStuckOff) return;
          this._videoOn = !this._videoOn;
          this._labelVideo(button);
        });
        footer.appendChild(button);
        document.body.appendChild(footer);
      }
      if (!b.silentConnect) this._status(2);
      opts.success && opts.success();
      if (b.endAfterMs > 0) setTimeout(() => this._status(3), b.endAfterMs);
    }, b.waiting ? 300 : b.delayMs);
  },
  getCurrentUser(opts) { opts.success && opts.success({ result: { currentUser: { userId: 16777216, userName: "Room 1",
    muted: this._muted, bVideoOn: this._videoOn, isHost: false, isCoHost: false, bRaiseHand: false } } }); },
  mute(opts) { window.__zoomCalls.push(["mute", { userId: opts.userId, mute: opts.mute }]); this._muted = !!opts.mute; opts.success && opts.success(); },
  leaveMeeting(opts) { window.__zoomCalls.push(["leaveMeeting", { confirm: opts.confirm }]); opts.success && opts.success(); },
};
"""


async def block_sdk_cdn(context):
    """Abort every request to Zoom's CDN so the page runs on the stub only."""

    async def abort(route):
        await route.abort()

    await context.route("https://source.zoom.us/**", abort)
