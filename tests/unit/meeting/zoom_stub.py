"""
A stand-in for Zoom's Meeting SDK (window.ZoomMtg) so the page and the provider
can be driven in the venv's Chromium without reaching Zoom. Behaviour flags:
window.ZoomMtg._behaviour = {waiting, errorFor, errorCode, errorMessage, neverConnect, delayMs, pageSays, ...}; join
fails with {errorCode, errorMessage} for the meeting number errorFor, and pageSays is text join puts on the page, as
Zoom's own dialogs do. labelStyle "client" names the video button the way Zoom's web client does (aria-label "start my
video"; its visible text is always "Video"); noToolbar leaves the buttons out; videoStuckOff makes the video button
ignore clicks. window.ZoomMtg._setMuted(bool) is the host muting or unmuting the room: it changes what getCurrentUser
says and fires onUserUpdate, as Zoom does. window.__videoClicks counts clicks on the video button.

The rest of the meeting: role (participant, co-host, host), attendees, recording, refuseHostCalls; _setAttendee(id,
patch), _setRecording(on); the client's hand, reactions, captions, view and self-view buttons under .footer (the real
client's #wc-footer > #foot-bar), with window.__presses logging each press; host calls are recorded and refused for a
participant. getCurrentUser and getAttendeeslist answer in Zoom's own field names (userId, userName, muted, audio,
video, raiseHand, isHost, isCoHost - "" when not -, isGuest, isHold, self) while the stub keeps its own flags (bVideoOn,
bRaiseHand, waiting). onUserUpdate, onUserJoin, onUserLeave and onRecordingChange are fired from inside what Zoom calls
its reducer, where both calls throw, so a listener that reads the meeting at once fails here as it does there.
"""

STUB_JS = r"""
window.__zoomCalls = [];
window.__videoClicks = 0;
window.__presses = [];
window.ZoomMtg = {
  _listeners: {},
  _behaviour: { waiting: false, errorFor: "999", errorCode: 3712, errorMessage: "This meeting ID is not valid",
                neverConnect: false, delayMs: 50, endAfterMs: 0, pageSays: "",
                mutedOnEntry: false, videoOffOnEntry: false, silentConnect: false, statusField: "meetingStatus",
                labelStyle: "sdk", noToolbar: false, videoStuckOff: false,
                role: "participant", recording: false, refuseHostCalls: true,
                attendees: [{ userId: 16777217, userName: "Ann Lee", muted: false, bVideoOn: true, bRaiseHand: false, isHost: true },
                            { userId: 16777218, userName: "Raj Patel", muted: true, bVideoOn: false, bRaiseHand: true, isHost: false }] },
  _muted: false, _videoOn: true, _hand: false, _captions: false, _layout: "speaker", _selfHidden: false,
  _attendees: [], _dispatching: false,
  _status(code) { const data = {}; data[this._behaviour.statusField] = code; this._fire("onMeetingStatus", data); },
  _setMuted(muted) { this._muted = !!muted; this._fire("onUserUpdate", { userId: 16777216 }); },
  _setAttendee(id, patch) { const a = this._attendees.find((x) => x.userId === id); if (a) Object.assign(a, patch); this._fire("onUserUpdate", { userId: id }); },
  _setRecording(on) { this._behaviour.recording = !!on; this._fire("onRecordingChange", { bRecording: !!on }); },
  _me() { const b = this._behaviour; return { userId: 16777216, userName: "Room 1", muted: this._muted, audio: "computer", video: this._videoOn,
           isHost: b.role === "host", isCoHost: b.role === "co-host" || "", isGuest: false, raiseHand: this._hand, isHold: false, self: true }; },
  _record(a) { return { userId: a.userId, userName: a.userName, muted: a.muted, audio: "computer", video: !!a.bVideoOn,
           isHost: !!a.isHost, isCoHost: "", isGuest: false, raiseHand: !!a.bRaiseHand, isHold: !!a.waiting, self: false }; },
  _labelVideo(button) {
    const on = this._videoOn;
    button.textContent = "Video";
    if (this._behaviour.labelStyle === "client") {
      button.setAttribute("aria-label", on ? "stop my video" : "start my video");
    } else {
      button.setAttribute("aria-label", on ? "Stop Video" : "Start Video");
    }
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
    const video = document.createElement("button"); video.id = "stub-video"; video.className = "send-video-container__btn"; this._labelVideo(video);
    video.addEventListener("click", () => { window.__videoClicks += 1; if (b.videoStuckOff) return; this._videoOn = !this._videoOn; this._labelVideo(video); });
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
  },
  setZoomJSLib(path, av) { window.__zoomCalls.push(["setZoomJSLib", path, av]); },
  preLoadWasm() { window.__zoomCalls.push(["preLoadWasm"]); },
  prepareWebSDK() { window.__zoomCalls.push(["prepareWebSDK"]); },
  inMeetingServiceListener(name, cb) { this._listeners[name] = cb; },
  _fire(name, data) {
    const listener = this._listeners[name];
    if (!listener) return;
    this._dispatching = ["onUserUpdate", "onUserJoin", "onUserLeave", "onRecordingChange"].includes(name);
    try { listener(data); } finally { this._dispatching = false; }
  },
  _guard() { if (this._dispatching) throw new Error("Minified Redux error #3"); },
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
    this._videoOn = !b.videoOffOnEntry;
    this._attendees = b.attendees.map((a) => Object.assign({}, a));
    window.__videoClicks = 0;
    setTimeout(() => this._status(1), 10);
    if (b.waiting) setTimeout(() => this._fire("onUserIsInWaitingRoom", {}), 20);
    setTimeout(() => {
      if (!b.noToolbar) this._drawFooter();
      if (!b.silentConnect) this._status(2);
      opts.success && opts.success();
      if (b.endAfterMs > 0) setTimeout(() => this._status(3), b.endAfterMs);
    }, b.waiting ? 300 : b.delayMs);
  },
  getCurrentUser(opts) { this._guard(); opts.success && opts.success({ result: { currentUser: this._me() } }); },
  getAttendeeslist(opts) { this._guard(); opts.success && opts.success({ result: { attendeesList: [this._me()].concat(this._attendees.map((a) => this._record(a))) } }); },
  _hostCall(name, args, opts) {
    window.__zoomCalls.push([name, args]);
    if (this._behaviour.role === "participant" && this._behaviour.refuseHostCalls) { opts.error && opts.error({ errorCode: 1, errorMessage: "Not the host" }); return false; }
    opts.success && opts.success(); return true;
  },
  mute(opts) { window.__zoomCalls.push(["mute", { userId: opts.userId, mute: opts.mute }]); if (opts.userId === 16777216) this._muted = !!opts.mute; else { const a = this._attendees.find((x) => x.userId === opts.userId); if (a) a.muted = !!opts.mute; } opts.success && opts.success(); },
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
