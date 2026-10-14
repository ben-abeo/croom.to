"""
A stand-in for Zoom's Meeting SDK (window.ZoomMtg) so the page and the provider
can be driven in the venv's Chromium without reaching Zoom. Where the bridge
depends on Zoom's behaviour it mirrors Zoom 6.5.0 as its own bundle has it:

- getCurrentUser answers with the SDK's public record of the room: {userId, self, userName, muted, audio, isHost,
  isCoHost (often ""), isGuest, isHold, video, raiseHand, ...}. The store's own names (bVideoOn, bRaiseHand) are not in it.
- Zoom fires onUserUpdate from inside its Redux reducer. While that listener runs, getCurrentUser and getAttendeeslist
  throw "Minified Redux error #3", as Redux's getState does while a reducer runs.
- The Client View's footer is <div id="wc-footer"><div id="foot-bar">, and its video button is
  button.send-video-container__btn (id stub-video here). labelStyle "client" names it as Zoom's web client does: the
  aria-label is "start my video" or "stop my video" and the visible text is always "Video". labelStyle "sdk" (the default,
  for the older tests) puts "Start Video" or "Stop Video" in the aria-label and no text. aria-disabled is always set.

Behaviour flags, window.ZoomMtg._behaviour = {...}:
- waiting, errorFor, errorCode, errorMessage, neverConnect, delayMs, endAfterMs, pageSays, silentConnect, statusField: the
  join's course. Join fails with {errorCode, errorMessage} for the meeting number errorFor, and pageSays is text join puts
  on the page, as Zoom's own dialogs do.
- mutedOnEntry and videoOffOnEntry: the room's state when the meeting starts.
- noToolbar leaves the footer out. videoStuckOff makes the video button ignore clicks.
- videoStartsAfterMs keeps the video button aria-disabled="true" for that long after connect and after each press it
  takes (the camera switches at the end), and the button ignores clicks meanwhile, as Zoom's onClick does while its video
  encoder starts or an action is in progress.
- videoUnusable is Zoom without a usable camera (none found, permission denied, video disabled by the host): the
  button's aria-label and text are just "Video", the record's video is false, and a click does nothing.

window.ZoomMtg._setMuted(bool) is the host muting or unmuting the room: it changes the record and fires onUserUpdate.
mute() and a camera switch fire onUserUpdate too, as Zoom's roster update does. window.__videoClicks counts every click on
the video button, the ignored ones too.
"""

STUB_JS = r"""
window.__zoomCalls = [];
window.ZoomMtg = {
  _listeners: {},
  _behaviour: { waiting: false, errorFor: "999", errorCode: 3712, errorMessage: "This meeting ID is not valid",
                neverConnect: false, delayMs: 50, endAfterMs: 0, pageSays: "",
                mutedOnEntry: false, videoOffOnEntry: false, silentConnect: false, statusField: "meetingStatus",
                labelStyle: "sdk", noToolbar: false, videoStuckOff: false, videoStartsAfterMs: 0, videoUnusable: false },
  _muted: false, _videoOn: true, _dispatching: false, _videoBusy: false, _videoButton: null,
  _setMuted(muted) { this._muted = !!muted; this._fire("onUserUpdate", { userId: 16777216, muted: this._muted }); },
  // The SDK's public record (its getCurrentUser maps the store's attendee to these names).
  _record() {
    return { userId: 16777216, self: true, userName: "Room 1", muted: this._muted, audio: "computer", isHost: false,
             isCoHost: "", isGuest: false, isHold: false, persistentID: "", participantUUID: "", customerKey: "",
             video: this._videoOn, raiseHand: false, sharingStatus: 0, isBotUser: false, botBrandName: "",
             botAuthorizerName: "", isOriginhost: false, isAlternativeHost: false };
  },
  _paintVideo() {
    const button = this._videoButton, b = this._behaviour, on = this._videoOn;
    if (!button) return;
    let label;
    if (b.videoUnusable) label = "Video";
    else if (b.labelStyle === "client") label = on ? "stop my video" : "start my video";
    else label = on ? "Stop Video" : "Start Video";
    button.setAttribute("aria-label", label);
    button.textContent = (b.labelStyle === "client" || b.videoUnusable) ? "Video" : "";
    button.setAttribute("aria-disabled", String(this._videoBusy));
  },
  // Disabled for ms (the encoder starting, a press in progress), then whatever comes next.
  _busyVideo(ms, then) {
    if (ms <= 0) { if (then) then(); this._paintVideo(); return; }
    this._videoBusy = true;
    this._paintVideo();
    setTimeout(() => { this._videoBusy = false; if (then) then(); this._paintVideo(); }, ms);
  },
  _status(code) { const data = {}; data[this._behaviour.statusField] = code; this._fire("onMeetingStatus", data); },
  setZoomJSLib(path, av) { window.__zoomCalls.push(["setZoomJSLib", path, av]); },
  preLoadWasm() { window.__zoomCalls.push(["preLoadWasm"]); },
  prepareWebSDK() { window.__zoomCalls.push(["prepareWebSDK"]); },
  inMeetingServiceListener(name, cb) { this._listeners[name] = cb; },
  // onUserUpdate comes from inside Zoom's reducer: the store cannot be read while its listener runs.
  _fire(name, data) {
    const listener = this._listeners[name];
    if (!listener) return;
    if (name !== "onUserUpdate") { listener(data); return; }
    this._dispatching = true;
    try { listener(data); } finally { this._dispatching = false; }
  },
  _readStore() { if (this._dispatching) throw new Error("Minified Redux error #3"); },
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
    this._videoOn = !b.videoOffOnEntry && !b.videoUnusable;
    window.__videoClicks = 0;
    setTimeout(() => {
      if (!b.noToolbar) {
        const footer = document.createElement("div");
        footer.id = "wc-footer";
        const bar = document.createElement("div");
        bar.id = "foot-bar";
        const button = document.createElement("button");
        button.id = "stub-video";
        button.className = "footer-button-base__button send-video-container__btn";
        this._videoButton = button;
        button.addEventListener("click", () => {
          window.__videoClicks += 1;
          if (b.videoUnusable || b.videoStuckOff || this._videoBusy) return;   // Zoom's onClick returns at once
          this._busyVideo(b.videoStartsAfterMs, () => {
            this._videoOn = !this._videoOn;
            this._fire("onUserUpdate", { userId: 16777216, video: this._videoOn });
          });
        });
        bar.appendChild(button);
        footer.appendChild(bar);
        document.body.appendChild(footer);
        this._busyVideo(b.videoStartsAfterMs);   // the encoder starts after connect
      }
      if (!b.silentConnect) this._status(2);
      opts.success && opts.success();
      if (b.endAfterMs > 0) setTimeout(() => this._status(3), b.endAfterMs);
    }, b.waiting ? 300 : b.delayMs);
  },
  getCurrentUser(opts) { this._readStore(); opts.success && opts.success({ result: { currentUser: this._record() } }); },
  getAttendeeslist(opts) { this._readStore(); opts.success && opts.success({ result: { attendeesList: [this._record()] } }); },
  mute(opts) {
    window.__zoomCalls.push(["mute", { userId: opts.userId, mute: opts.mute }]);
    this._muted = !!opts.mute;
    this._fire("onUserUpdate", { userId: opts.userId, muted: this._muted });
    opts.success && opts.success();
  },
  leaveMeeting(opts) { window.__zoomCalls.push(["leaveMeeting", { confirm: opts.confirm }]); opts.success && opts.success(); },
};
"""


async def block_sdk_cdn(context):
    """Abort every request to Zoom's CDN so the page runs on the stub only."""

    async def abort(route):
        await route.abort()

    await context.route("https://source.zoom.us/**", abort)
