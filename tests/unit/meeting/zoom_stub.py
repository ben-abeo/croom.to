"""
A stand-in for Zoom's Meeting SDK (window.ZoomMtg) so the page and the provider
can be driven in the venv's Chromium without reaching Zoom. Where the bridge
depends on Zoom's behaviour it mirrors Zoom 6.5.0 as its own bundle has it:

- getCurrentUser answers with the SDK's public record of the room: {userId, self, userName, muted, audio, isHost,
  isCoHost ("" when not), isGuest, isHold, video, raiseHand, ...}. The store's own names (bVideoOn, bRaiseHand) are not in
  it. getAttendeeslist answers with every participant's record in the same names, the room's first.
- Zoom builds the room's record from its roster, and getAttendeeslist calls neither callback while the roster is empty:
  until the roster has the room, getCurrentUser's record is four fields and no user ({isOriginhost, isAlternativeHost,
  raiseHand, isHold}: no userId, no mute, no video).
- Zoom fires onUserUpdate, onUserJoin and onUserLeave from inside its Redux reducer. While those listeners run,
  getCurrentUser and getAttendeeslist throw "Minified Redux error #3", as Redux's getState does while a reducer runs.
  onRecordingChange is an effect, not a reducer: it sends {recording: "stop" | "recording" | "pause", hasLocalRecord} and
  fires once, with "stop", as the meeting UI mounts.
- The Client View's footer is <div id="wc-footer"><div id="foot-bar">, and its video button is
  button.send-video-container__btn (id stub-video here), whose visible text is always "Video". labelStyle "client" (the
  default) names it as Zoom's web client does: the aria-label is "start my video" or "stop my video". labelStyle "sdk"
  names it the way other builds do: "Start Video" or "Stop Video". aria-disabled is always set. After it come the
  client's hand, reactions (a menu), captions, view (a menu) and self-view buttons; window.__presses logs their presses.

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
- role (participant, co-host, host), attendees (the other participants, in the stub's own flags bVideoOn, bRaiseHand and
  waiting), recording and refuseHostCalls: the rest of the meeting. Host calls (muteAll, record, lockMeeting, putOnHold,
  expel, endMeeting) are recorded in window.__zoomCalls and refused for a participant.
- silentAttendees: getAttendeeslist never answers. rosterAfterMs: it stays silent until that long after join, and
  getCurrentUser gives the four-field record until then.

window.ZoomMtg._setMuted(bool) is the host muting or unmuting the room, _setAttendee(id, patch) a change to another
participant, _setRecording(on) recording starting or stopping: each changes what Zoom says and fires Zoom's event. mute()
and a camera switch fire onUserUpdate too, as Zoom's roster update does. window.__videoClicks counts every click on the
video button, the ignored ones too. window.__pollFns holds the page's 5000 ms setInterval callbacks (a test can call one
as the tick), window.__rejections its unhandled rejections.
"""

STUB_JS = r"""
(function () {
  window.__pollFns = [];
  window.__rejections = [];
  const realSetInterval = window.setInterval;
  window.setInterval = function (fn, ms, ...rest) { if (ms === 5000) window.__pollFns.push(fn); return realSetInterval.call(window, fn, ms, ...rest); };
  window.addEventListener("unhandledrejection", (e) => { window.__rejections.push(String((e.reason && e.reason.message) || e.reason)); });
})();
window.__zoomCalls = [];
window.__videoClicks = 0;
window.__presses = [];
window.ZoomMtg = {
  _listeners: {},
  _behaviour: { waiting: false, errorFor: "999", errorCode: 3712, errorMessage: "This meeting ID is not valid",
                neverConnect: false, delayMs: 50, endAfterMs: 0, pageSays: "",
                mutedOnEntry: false, videoOffOnEntry: false, silentConnect: false, statusField: "meetingStatus",
                labelStyle: "client", noToolbar: false, videoStuckOff: false, videoStartsAfterMs: 0, videoUnusable: false,
                role: "participant", recording: false, refuseHostCalls: true, silentAttendees: false, rosterAfterMs: 0,
                attendees: [{ userId: 16777217, userName: "Ann Lee", muted: false, bVideoOn: true, bRaiseHand: false, isHost: true },
                            { userId: 16777218, userName: "Raj Patel", muted: true, bVideoOn: false, bRaiseHand: true, isHost: false }] },
  _muted: false, _videoOn: true, _hand: false, _captions: false, _layout: "speaker", _selfHidden: false,
  _attendees: [], _dispatching: false, _joinedAt: 0, _videoBusy: false, _videoButton: null,
  _status(code) { const data = {}; data[this._behaviour.statusField] = code; this._fire("onMeetingStatus", data); },
  _setMuted(muted) { this._muted = !!muted; this._fire("onUserUpdate", { userId: 16777216, muted: this._muted }); },
  _setAttendee(id, patch) { const a = this._attendees.find((x) => x.userId === id); if (a) Object.assign(a, patch); this._fire("onUserUpdate", { userId: id }); },
  _setRecording(on) { this._behaviour.recording = !!on; this._fire("onRecordingChange", { recording: on ? "recording" : "stop", hasLocalRecord: false }); },
  // The SDK's public record of the room (its getCurrentUser maps the store's attendee to these names).
  _me() {
    const b = this._behaviour;
    return { userId: 16777216, self: true, userName: "Room 1", muted: this._muted, audio: "computer", isHost: b.role === "host",
             isCoHost: b.role === "co-host" || "", isGuest: false, isHold: false, persistentID: "", participantUUID: "",
             customerKey: "", video: this._videoOn, raiseHand: this._hand, sharingStatus: 0, isBotUser: false,
             botBrandName: "", botAuthorizerName: "", isOriginhost: false, isAlternativeHost: false };
  },
  // Another participant's record, in the same names, from the stub's own flags.
  _record(a) { return { userId: a.userId, userName: a.userName, muted: a.muted, audio: "computer", video: !!a.bVideoOn,
           isHost: !!a.isHost, isCoHost: "", isGuest: false, raiseHand: !!a.bRaiseHand, isHold: !!a.waiting, self: false }; },
  _paintVideo() {
    const button = this._videoButton, b = this._behaviour, on = this._videoOn;
    if (!button) return;
    let label;
    if (b.videoUnusable) label = "Video";
    else if (b.labelStyle === "client") label = on ? "stop my video" : "start my video";
    else label = on ? "Stop Video" : "Start Video";
    button.setAttribute("aria-label", label);
    button.textContent = "Video";
    button.setAttribute("aria-disabled", String(this._videoBusy));
  },
  // Disabled for ms (the encoder starting, a press in progress), then whatever comes next.
  _busyVideo(ms, then) {
    if (ms <= 0) { if (then) then(); this._paintVideo(); return; }
    this._videoBusy = true;
    this._paintVideo();
    setTimeout(() => { this._videoBusy = false; if (then) then(); this._paintVideo(); }, ms);
  },
  _button(bar, id, label, onClick) {
    const b = document.createElement("button"); b.id = id; b.setAttribute("aria-label", label); b.textContent = label;
    b.addEventListener("click", () => { window.__presses.push([id, b.getAttribute("aria-label")]); onClick(b); });
    bar.appendChild(b); return b;
  },
  _menu(id, items, onPick) {
    const menu = document.createElement("div"); menu.id = id; menu.setAttribute("role", "menu");
    for (const [value, label] of items) {
      const item = document.createElement("button"); item.dataset[id === "stub-view-menu" ? "view" : "reaction"] = value;
      item.setAttribute("role", "menuitem"); item.textContent = label;
      item.addEventListener("click", () => { onPick(value); menu.remove(); });
      menu.appendChild(item);
    }
    document.body.appendChild(menu);
  },
  _drawFooter() {
    const b = this._behaviour;
    const footer = document.createElement("div"); footer.id = "wc-footer"; footer.className = "footer";
    const bar = document.createElement("div"); bar.id = "foot-bar"; footer.appendChild(bar);
    const video = document.createElement("button"); video.id = "stub-video"; video.className = "footer-button-base__button send-video-container__btn";
    this._videoButton = video;
    video.addEventListener("click", () => {
      window.__videoClicks += 1;
      if (b.videoUnusable || b.videoStuckOff || this._videoBusy) return;   // Zoom's onClick returns at once
      this._busyVideo(b.videoStartsAfterMs, () => {
        this._videoOn = !this._videoOn;
        this._fire("onUserUpdate", { userId: 16777216, video: this._videoOn });
      });
    });
    bar.appendChild(video);
    this._button(bar, "stub-hand", "Raise Hand", (btn) => { this._hand = !this._hand; btn.setAttribute("aria-label", this._hand ? "Lower Hand" : "Raise Hand"); btn.textContent = btn.getAttribute("aria-label"); });
    this._button(bar, "stub-reactions", "Reactions", () => this._menu("stub-reaction-menu",
      [["thumbsup", "👍"], ["clap", "👏"], ["heart", "❤️"], ["joy", "😂"], ["open_mouth", "😮"], ["tada", "🎉"]],
      (value) => window.__zoomCalls.push(["reaction", value])));
    this._button(bar, "stub-captions", "Show Captions", (btn) => { this._captions = !this._captions; btn.setAttribute("aria-label", this._captions ? "Hide Captions" : "Show Captions"); btn.textContent = btn.getAttribute("aria-label"); });
    this._button(bar, "stub-view", "View", () => this._menu("stub-view-menu", [["speaker", "Speaker"], ["gallery", "Gallery"]],
      (value) => { this._layout = value; }));
    this._button(bar, "stub-self", "Hide Self View", (btn) => { this._selfHidden = !this._selfHidden; btn.setAttribute("aria-label", this._selfHidden ? "Show Self View" : "Hide Self View"); btn.textContent = btn.getAttribute("aria-label"); });
    document.body.appendChild(footer);
    this._busyVideo(b.videoStartsAfterMs);   // the encoder starts after connect
  },
  setZoomJSLib(path, av) { window.__zoomCalls.push(["setZoomJSLib", path, av]); },
  preLoadWasm() { window.__zoomCalls.push(["preLoadWasm"]); },
  prepareWebSDK() { window.__zoomCalls.push(["prepareWebSDK"]); },
  inMeetingServiceListener(name, cb) { this._listeners[name] = cb; },
  // onUserUpdate, onUserJoin and onUserLeave come from inside Zoom's reducer: the store cannot be read while they run.
  _fire(name, data) {
    const listener = this._listeners[name];
    if (!listener) return;
    this._dispatching = ["onUserUpdate", "onUserJoin", "onUserLeave"].includes(name);
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
    this._muted = b.mutedOnEntry;
    this._videoOn = !b.videoOffOnEntry && !b.videoUnusable;
    this._attendees = b.attendees.map((a) => Object.assign({}, a));
    this._joinedAt = Date.now();
    window.__videoClicks = 0;
    setTimeout(() => this._status(1), 10);
    if (b.waiting) setTimeout(() => this._fire("onUserIsInWaitingRoom", { userId: 16777216, userName: "Room 1", self: true, isInWaitingRoom: true }), 20);
    setTimeout(() => {
      if (!b.noToolbar) this._drawFooter();
      this._fire("onRecordingChange", { recording: b.recording ? "recording" : "stop", hasLocalRecord: false });
      if (!b.silentConnect) this._status(2);
      opts.success && opts.success();
      if (b.endAfterMs > 0) setTimeout(() => this._status(3), b.endAfterMs);
    }, b.waiting ? 300 : b.delayMs);
  },
  _listed() { return Date.now() - this._joinedAt >= this._behaviour.rosterAfterMs; },
  getCurrentUser(opts) {
    this._readStore();
    const user = this._listed() ? this._me()     // before the roster has the room, the SDK's record of nobody
      : { isOriginhost: false, isAlternativeHost: false, raiseHand: false, isHold: false };
    opts.success && opts.success({ result: { currentUser: user } });
  },
  getAttendeeslist(opts) {
    this._readStore();
    if (this._behaviour.silentAttendees || !this._listed()) return;
    opts.success && opts.success({ result: { attendeesList: [this._me()].concat(this._attendees.map((a) => this._record(a))) } });
  },
  _hostCall(name, args, opts) {
    window.__zoomCalls.push([name, args]);
    if (this._behaviour.role === "participant" && this._behaviour.refuseHostCalls) { opts.error && opts.error({ errorCode: 1, errorMessage: "Not the host" }); return false; }
    opts.success && opts.success(); return true;
  },
  mute(opts) {
    window.__zoomCalls.push(["mute", { userId: opts.userId, mute: opts.mute }]);
    if (opts.userId === 16777216) this._muted = !!opts.mute;
    else { const a = this._attendees.find((x) => x.userId === opts.userId); if (a) a.muted = !!opts.mute; }
    this._fire("onUserUpdate", { userId: opts.userId, muted: !!opts.mute });
    opts.success && opts.success();
  },
  muteAll(opts) { if (this._hostCall("muteAll", { muteAll: opts.muteAll }, opts)) this._attendees.forEach((a) => { a.muted = !!opts.muteAll; }); },
  record(opts) { if (this._hostCall("record", { record: opts.record }, opts)) this._setRecording(opts.record); },
  lockMeeting(opts) { this._hostCall("lockMeeting", { lockMeeting: opts.lockMeeting }, opts); },
  putOnHold(opts) { if (this._hostCall("putOnHold", { userId: opts.userId, hold: opts.hold }, opts)) { const a = this._attendees.find((x) => x.userId === opts.userId); if (a) a.waiting = !!opts.hold; } },
  expel(opts) { if (this._hostCall("expel", { userId: opts.userId }, opts)) this._attendees = this._attendees.filter((a) => a.userId !== opts.userId); },
  endMeeting(opts) { if (this._hostCall("endMeeting", {}, opts)) setTimeout(() => this._status(3), 10); },
  rename(opts) { window.__zoomCalls.push(["rename", { userId: opts.userId, newName: opts.newName }]); opts.success && opts.success(); },
  leaveMeeting(opts) { window.__zoomCalls.push(["leaveMeeting", { confirm: opts.confirm }]); opts.success && opts.success(); },
};
"""


async def block_sdk_cdn(context):
    """Abort every request to Zoom's CDN so the page runs on the stub only."""

    async def abort(route):
        await route.abort()

    await context.route("https://source.zoom.us/**", abort)
