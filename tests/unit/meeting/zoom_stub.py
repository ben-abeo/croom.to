"""
A stand-in for Zoom's Meeting SDK (window.ZoomMtg) so the page and the provider
can be driven in the venv's Chromium without reaching Zoom. Behaviour flags:
window.ZoomMtg._behaviour = {waiting, errorFor, neverConnect, delayMs}.
"""

STUB_JS = r"""
window.__zoomCalls = [];
window.ZoomMtg = {
  _listeners: {},
  _behaviour: { waiting: false, errorFor: "999", neverConnect: false, delayMs: 50 },
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
    if (String(opts.meetingNumber) === b.errorFor) {
      setTimeout(() => opts.error && opts.error({ errorCode: 3712, errorMessage: "This meeting ID is not valid" }), 10);
      return;
    }
    if (b.neverConnect) return;
    setTimeout(() => this._fire("onMeetingStatus", { meetingStatus: 1 }), 10);
    if (b.waiting) setTimeout(() => this._fire("onUserIsInWaitingRoom", {}), 20);
    setTimeout(() => {
      const button = document.createElement("button");
      button.id = "stub-video";
      button.setAttribute("aria-label", "Stop Video");
      button.addEventListener("click", () => button.setAttribute("aria-label",
        button.getAttribute("aria-label") === "Stop Video" ? "Start Video" : "Stop Video"));
      document.body.appendChild(button);
      this._fire("onMeetingStatus", { meetingStatus: 2 });
      opts.success && opts.success();
    }, b.waiting ? 300 : b.delayMs);
  },
  getCurrentUser(opts) { opts.success && opts.success({ result: { currentUser: { userId: 16777216, userName: "Room 1", muted: false } } }); },
  mute(opts) { window.__zoomCalls.push(["mute", { userId: opts.userId, mute: opts.mute }]); opts.success && opts.success(); },
  leaveMeeting(opts) { window.__zoomCalls.push(["leaveMeeting", { confirm: opts.confirm }]); opts.success && opts.success(); },
};
"""


async def block_sdk_cdn(context):
    """Abort every request to Zoom's CDN so the page runs on the stub only."""

    async def abort(route):
        await route.abort()

    await context.route("https://source.zoom.us/**", abort)
