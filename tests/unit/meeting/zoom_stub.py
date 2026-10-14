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
  names it the way other builds do: "Start Video" or "Stop Video". aria-disabled is always set.
- After it come the footer's Reactions slot and the captions button, and, outside the footer, the View widget, drawn as the
  6.5.0 bundle draws them (react-bootstrap and Zoom's own popovers; no React here). The Reactions button (id stub-reactions,
  aria-label "Reactions") toggles the picker div#reactionPicker.reaction-picker[role="presentation"] inside its slot: six
  buttons named by aria-label and data-tooltip (Clap, Thumbs Up, Joy, Open Mouth, Heart, Tada, in that order: an icon and
  no text, and no emoji anywhere in the client), a More button, and two rows of visible text, "Raise Hand" / "Lower Hand"
  and "Be Right Back". The View button (id stub-view, aria-label "View") toggles div.dropdown-menu.show.dark-dropdown.
  full-screen-widget__pop-menu inside div.full-screen-viev-dropdown on the body: a.dropdown-item[role="button"] items
  "Speaker View" and "Gallery View" (the selected one has the class full-screen-widget__pop-menu--checked) and "Hide Self
  View" / "Show Self View"; the button's own label never changes. Neither popover has role="menu", both close when their
  opener is pressed again, and a pick closes them. Captions is a footer button whose label says its state.
- A host's View menu has a section more, "Attendee View", after those (before them with attendeeViewFirst): a header and two
  items, "Speaker View" (text "Speaker View", aria-label "<Attendee View>.Speaker View", checked while the attendees' view is
  speaker) and "Follow Host's View". They set the attendees' view (window.ZoomMtg._attendeeView), never the host's own, and
  are logged in window.__presses as stub-attendee-speaker and stub-attendee-follow.
- A popover is drawn, and taken out, in a microtask after the press that opened or closed it, as React 18 renders what a click
  did: what looks at the page in the click's own task sees it as it was. A pick takes the popover with it as React unmounts
  it, the picked item in it: the item is not drawn again with new words first. Only a popover that stays open, or closes a
  while after the pick (popoverStays, popoverClosesAfterMs), shows what the pick did.
- A box with role="menu" (Zoom's Leave options; the client has other role="menu" nodes too) belongs to the stub for the
  bridge to leave alone. Like the popovers it is a node its owner removes with removeChild on its own parent, as React's
  unmount does, which throws if anything else took it out first: window.__reactErrors collects each such throw, and
  _unmountBystander() is the owner's unmount of the box (window.__unmounted says "ok" or the error).
- A dialog, window.ZoomMtg._showDialog(title, text, buttons), is Zoom's modal: div#stub-dialog-N.zm-modal[role="dialog"] on the
  body with an h2 for the title, a paragraph and a <button> for each label (given none, it is a banner). It is the client's node,
  as the popovers are: its owner takes it out, with removeChild on the body, in a microtask after a button is pressed or when Zoom
  closes it (_closeDialog(id)), and anything else that took it out first makes that throw (window.__reactErrors). A press is
  logged as ["dialog", title, label] in window.__zoomCalls and as [id, label] in window.__presses. _chat(from, text) fires
  onReceiveChatMsg as the SDK does.
- window.__presses logs every press of a button or an item as [id, label]; window.__maxOpen is the most popovers drawn at
  once; window.__zoomCalls gets ["reaction", "thumbs_up"] (the name, lower case) for a reaction.

Behaviour flags, window.ZoomMtg._behaviour = {...}:
- waiting, errorFor, errorCode, errorMessage, neverConnect, delayMs, endAfterMs, pageSays, silentConnect, statusField: the
  join's course. Join fails with {errorCode, errorMessage} for the meeting number errorFor, and pageSays is text join puts
  on the page, as Zoom's own dialogs do.
- mutedOnEntry and videoOffOnEntry: the room's state when the meeting starts.
- noToolbar leaves the footer out, and with it the View widget and the Leave box: the client's buttons are all drawn with
  it. videoStuckOff makes the video button ignore clicks. An opener with aria-disabled="true" or disabled ignores a press
  (the press is still logged); every other button only carries the attribute.
- videoStartsAfterMs keeps the video button aria-disabled="true" for that long after connect and after each press it
  takes (the camera switches at the end), and the button ignores clicks meanwhile, as Zoom's onClick does while its video
  encoder starts or an action is in progress.
- videoUnusable is Zoom without a usable camera (none found, permission denied, video disabled by the host): the
  button's aria-label and text are just "Video", the record's video is false, and a click does nothing.
- handInFooter is the uncommon build that draws the hand as a footer button of its own ("Raise Hand" / "Lower Hand") in place
  of the Reactions slot. handIgnored (the picker's hand row closes the picker and does nothing), selfViewIgnored (the self
  view item takes the press, does nothing and leaves the dropdown open) and openerIgnored (the openers do nothing) are
  presses the client does not act on. popoverOpensAfterMs draws a popover that long after its opener's press, popoverStays
  leaves it open after a pick, popoverClosesAfterMs has Zoom close it that long after a pick (window.__closedByZoom says it
  did), paddedLabels puts whitespace around and inside the items' labels and text, and openersRedrawn has the client draw
  an opener again (a new node) every time it is pressed. strayDropdown draws another dropdown of the client's, open, on the
  body before the View widget's (div.dropdown-menu.show, as its audio options may be) with items named like the View's: it is
  not the View menu, and a press of one of its items is logged as stub-stray. attendeeViewFirst puts a host's Attendee View
  section before the host's own items.
- role (participant, co-host, host), attendees (the other participants, in the stub's own flags bVideoOn, bRaiseHand and
  waiting), recording and refuseHostCalls: the rest of the meeting. refuseHostCalls false lets a participant's calls through.
- dialogStays is a dialog that takes a button's press (it is logged) and stays up; dialogClosesAfterMs has Zoom close it that
  long after the press, in place of the microtask.
- The SDK's calls as its own wrapper treats them (muteAll, record, lockMeeting, putOnHold, expel, mute, endMeeting, rename). A
  call it drops is answered false, and nothing is called back; a call it takes is answered through its callbacks. It drops a
  call with a key its method does not take (muteAll {muteAll}, record {record}, lockMeeting {lockMeeting}, putOnHold {userId,
  hold}, expel {userId}, mute {userId, mute}, endMeeting {}, rename {userId, oldName, newName}): its gate looks only at the keys
  given, so a key left out is no drop, and the call goes ahead with it undefined (lockMeeting({}) unlocks). It drops, too, a call
  made again inside the window it keeps for its method (mute 1 s; muteAll, rename, expel, record and lockMeeting 5 s; putOnHold
  0.1 s), and putOnHold's drop alone is silent: with callbacks given the API answers undefined, as it does to a call it takes, so
  nothing says it was dropped. The room's own mute shares the mute window with another participant's. The windows run on a clock
  of the stub's own, which only window.ZoomMtg._later(ms) moves: time goes by without the test waiting for it, and without the
  machine's speed deciding (a test of the page's own waiting puts _now on the real clock). Every call is recorded in
  window.__zoomCalls as [name, args], the keys it was given with the callbacks left out; window.__dropped holds the ones
  dropped. A call that is taken is answered after it has returned (a microtask later), as the SDK answers, so what a call
  returns is not what its callbacks have said; rename, record and endMeeting are answered inside the call.
- The host's calls (muteAll, record, lockMeeting, putOnHold, expel, endMeeting, and mute for anyone but the room) are refused
  for a participant as the SDK refuses: error({errorCode: 1, errorMessage: "Current user is neither host nor cohost"}).
  endMeeting is the host's alone: a co-host is refused too, with {errorCode: 3003, errorMessage: null}. A call it carries out
  does its work, then is answered (window.ZoomMtg._locked is what lockMeeting last set). muteAll with muteAll false only asks
  everyone to unmute: nobody's mute changes. record answers at once, and the recording changes, with onRecordingChange, when
  Zoom's server confirms: recordConfirmsAfterMs later (50), or never with recordNeverConfirms.
- rename runs the SDK's host check first and goes on: for a participant it calls error ("Current user is neither host nor
  cohost", code 1) and then, with the caller's own id, renames the room and calls success. The room's name follows the rename
  (Zoom's records of the room say the new one afterwards).
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
window.__dropped = [];
window.__videoClicks = 0;
window.__presses = [];
window.__reactErrors = [];
window.__maxOpen = 0;
window.ZoomMtg = {
  _listeners: {},
  _behaviour: { waiting: false, errorFor: "999", errorCode: 3712, errorMessage: "This meeting ID is not valid",
                neverConnect: false, delayMs: 50, endAfterMs: 0, pageSays: "",
                mutedOnEntry: false, videoOffOnEntry: false, silentConnect: false, statusField: "meetingStatus",
                labelStyle: "client", noToolbar: false, videoStuckOff: false, videoStartsAfterMs: 0, videoUnusable: false,
                handInFooter: false, handIgnored: false, selfViewIgnored: false, openerIgnored: false, popoverOpensAfterMs: 0,
                popoverStays: false, popoverClosesAfterMs: 0, paddedLabels: false, openersRedrawn: false,
                strayDropdown: false, attendeeViewFirst: false, recordConfirmsAfterMs: 50, recordNeverConfirms: false,
                role: "participant", recording: false, refuseHostCalls: true, silentAttendees: false, rosterAfterMs: 0,
                dialogStays: false, dialogClosesAfterMs: 0,
                attendees: [{ userId: 16777217, userName: "Ann Lee", muted: false, bVideoOn: true, bRaiseHand: false, isHost: true },
                            { userId: 16777218, userName: "Raj Patel", muted: true, bVideoOn: false, bRaiseHand: true, isHost: false }] },
  _muted: false, _videoOn: true, _hand: false, _captions: false, _layout: "speaker", _selfHidden: false,
  _attendeeView: "speaker", _name: "Room 1", _locked: false, _skew: 0, _lastCall: {},
  _takes: { mute: ["userId", "mute"], muteAll: ["muteAll"], record: ["record"], lockMeeting: ["lockMeeting"], putOnHold: ["userId", "hold"],
            expel: ["userId"], endMeeting: [], rename: ["userId", "oldName", "newName"] },
  _windowMs: { mute: 1000, muteAll: 5000, rename: 5000, expel: 5000, record: 5000, lockMeeting: 5000, putOnHold: 100 },
  _attendees: [], _dispatching: false, _joinedAt: 0, _videoBusy: false, _videoButton: null,
  _open: { picker: false, view: false }, _shown: { picker: null, view: null }, _where: { picker: null, view: null },
  _pop: {}, _viewItems: null, _bystander: null, _bystanderWhere: null, _dialogs: 0, _dialogNodes: {},
  _status(code) { const data = {}; data[this._behaviour.statusField] = code; this._fire("onMeetingStatus", data); },
  _setMuted(muted) { this._muted = !!muted; this._fire("onUserUpdate", { userId: 16777216, muted: this._muted }); },
  _setAttendee(id, patch) { const a = this._attendees.find((x) => x.userId === id); if (a) Object.assign(a, patch); this._fire("onUserUpdate", { userId: id }); },
  _setRecording(on) { this._behaviour.recording = !!on; this._fire("onRecordingChange", { recording: on ? "recording" : "stop", hasLocalRecord: false }); },
  // The SDK's public record of the room (its getCurrentUser maps the store's attendee to these names).
  _me() {
    const b = this._behaviour;
    return { userId: 16777216, self: true, userName: this._name, muted: this._muted, audio: "computer", isHost: b.role === "host",
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
  _press(id, label) { window.__presses.push([id, label]); },
  _button(bar, id, label, onClick) {
    const b = document.createElement("button"); b.id = id; b.setAttribute("aria-label", label); b.textContent = label;
    b.addEventListener("click", () => { this._press(id, b.getAttribute("aria-label")); onClick(b); });
    bar.appendChild(b); return b;
  },
  // Zoom fires onUserUpdate for a hand, from inside its reducer, as it does for every change of a user.
  _toggleHand() { this._hand = !this._hand; this._fire("onUserUpdate", { userId: 16777216, raiseHand: this._hand }); },
  _svg() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", "16"); svg.setAttribute("height", "16"); svg.setAttribute("viewBox", "0 0 24 24");
    return svg;
  },
  // A label as the client may draw it: with paddedLabels, whitespace around it and inside it.
  _said(text) { return this._behaviour.paddedLabels ? "\n  " + text.replace(" ", "   ") + "\t \n" : text; },
  // A popover is drawn when its opener is pressed and is gone when the opener is pressed again (the client's own toggle) or an
  // item is picked. It is the client's node: its owner takes it out with removeChild on the parent it put it in, as React's
  // unmount does, so anything else that took it out first makes that throw (window.__reactErrors).
  _render(key) {
    const pop = this._pop[key];
    if (this._open[key] && !this._shown[key]) {
      this._where[key] = pop.parent(); this._shown[key] = pop.draw(); this._where[key].appendChild(this._shown[key]);
      if (key === "view") this._paintView();
    } else if (!this._open[key] && this._shown[key]) {
      const node = this._shown[key]; this._shown[key] = null;
      try { this._where[key].removeChild(node); } catch (e) { window.__reactErrors.push(String(e.message || e)); }
    }
    if (key === "view") pop.opener.setAttribute("aria-expanded", String(!!this._shown[key]));
    window.__maxOpen = Math.max(window.__maxOpen, Object.keys(this._shown).filter((k) => this._shown[k]).length);
  },
  _toggle(key) {
    const b = this._behaviour, opener = this._pop[key].opener;
    if (b.openerIgnored || opener.getAttribute("aria-disabled") === "true" || opener.disabled) return;
    this._open[key] = !this._open[key];
    if (this._open[key] && b.popoverOpensAfterMs) setTimeout(() => this._render(key), b.popoverOpensAfterMs);
    else queueMicrotask(() => this._render(key));      // React 18 renders what a click did in a microtask after it, not in the click
  },
  // An item was picked and has acted: the popover goes with it, at once or when the behaviour says, or stays. At once, it goes
  // as React unmounts it, the picked item in it: nothing is drawn again first. One that stays on the screen says what the
  // pick did.
  _picked(key) {
    const b = this._behaviour;
    const close = () => { this._open[key] = false; this._render(key); };
    if (b.popoverStays) { if (key === "view") this._paintView(); return; }
    if (b.popoverClosesAfterMs) {
      if (key === "view") this._paintView();
      setTimeout(() => { if (this._open[key]) { window.__closedByZoom = true; close(); } }, b.popoverClosesAfterMs);
    } else queueMicrotask(close);
  },
  // A client that draws its opener again every time it is pressed: the same button, a new node. The old node is dead, as a
  // node React has dropped is: a press of it reaches nothing.
  _redraw(key, handler) {
    const pop = this._pop[key];
    if (!this._behaviour.openersRedrawn) return;
    const fresh = pop.opener.cloneNode(true); fresh.addEventListener("click", handler);
    pop.opener.removeEventListener("click", handler); pop.opener.replaceWith(fresh); pop.opener = fresh;
  },
  // The footer's reaction slot: the Reactions button and, while it is open, the picker beside it inside the slot.
  _reactionSlot() {
    const slot = document.createElement("div"); slot.className = "footer__button-wrap"; slot.setAttribute("feature-type", "reaction");
    const wrap = document.createElement("div"); slot.appendChild(wrap);
    const opener = document.createElement("button"); opener.id = "stub-reactions"; opener.type = "button";
    opener.className = "footer-button-base__button ax-outline footer-button__button footer-button__button--no-opacity";
    opener.setAttribute("aria-label", "Reactions");
    const label = document.createElement("div"); label.className = "footer-button-base__button-label"; label.textContent = "Reactions";
    opener.appendChild(label); wrap.appendChild(opener);
    const press = () => { this._press("stub-reactions", "Reactions"); this._toggle("picker"); this._redraw("picker", press); };
    opener.addEventListener("click", press);
    this._pop.picker = { opener, parent: () => slot, draw: () => this._drawPicker() };
    return slot;
  },
  _drawPicker() {
    const b = this._behaviour;
    const outer = document.createElement("div"); outer.style.cssText = "position:absolute;left:0;top:0;width:100%";
    const picker = document.createElement("div"); picker.id = "reactionPicker"; picker.className = "reaction-picker"; picker.setAttribute("role", "presentation");
    const inner = document.createElement("div");
    const container = document.createElement("div"); container.className = "reaction-simple-picker__container"; container.tabIndex = 0;
    const row = (last) => {
      const r = document.createElement("div"); r.className = "reaction-simple-picker__row" + (last ? " reaction-simple-picker__row--last" : "");
      container.appendChild(r); return r;
    };
    // One block of the picker: an icon and, in the rows that have it, visible text; its name, if it has one, is in attributes.
    const block = (into, kind, attrs, text, onClick) => {
      const btn = document.createElement("button"); btn.type = "button"; btn.tabIndex = 0;
      btn.className = "reaction-simple-picker__block reaction-simple-picker__block--" + kind + " ax-outline";
      for (const name of Object.keys(attrs)) btn.setAttribute(name, attrs[name]);
      const tip = document.createElement("div"); tip.className = "reaction-simple-picker__tooltip-wrapper";
      if ("data-tooltip" in attrs) tip.setAttribute("data-tooltip", attrs["data-tooltip"]);
      const content = document.createElement("div"); content.className = "reaction-simple-picker__content-wrapper";
      content.appendChild(this._svg()); content.appendChild(document.createTextNode(text));
      tip.appendChild(content); btn.appendChild(tip); btn.addEventListener("click", onClick); into.appendChild(btn);
    };
    const first = row();
    for (const name of ["Clap", "Thumbs Up", "Joy", "Open Mouth", "Heart", "Tada"]) {        // the bundle's order
      block(first, "reaction", { "aria-label": this._said(name), "data-tooltip": this._said(name) }, "", () => {
        this._press("stub-reaction", name);
        window.__zoomCalls.push(["reaction", name.toLowerCase().replace(" ", "_")]);
        this._picked("picker");
      });
    }
    block(first, "reaction", { "data-tooltip": "More" }, "", () => this._press("stub-more", "More"));
    const hand = this._hand ? "Lower Hand" : "Raise Hand";
    block(row(), "raise-hand", {}, this._said(hand), () => {
      this._press("stub-hand", hand);
      if (!b.handIgnored) this._toggleHand();
      this._picked("picker");
    });
    block(row(true), "be-right-back", {}, "Be Right Back", () => { this._press("stub-brb", "Be Right Back"); this._picked("picker"); });
    inner.appendChild(container); picker.appendChild(inner); outer.appendChild(picker);
    return outer;
  },
  // The View widget, outside the footer: a button and, portalled into a div on the body, its dropdown.
  _viewWidget() {
    const widget = document.createElement("div"); widget.className = "full-screen-icon full-screen-icon-widget";
    const holder = document.createElement("div"); holder.className = "dropdown"; widget.appendChild(holder);
    const opener = document.createElement("button"); opener.id = "stub-view"; opener.type = "button"; opener.className = "full-screen-widget__button";
    opener.setAttribute("aria-label", "View"); opener.setAttribute("aria-haspopup", "true"); opener.setAttribute("aria-expanded", "false");
    opener.appendChild(this._svg()); holder.appendChild(opener);
    const portal = document.createElement("div"); portal.className = "full-screen-viev-dropdown"; document.body.appendChild(portal);
    const press = () => { this._press("stub-view", "View"); this._toggle("view"); this._redraw("view", press); };
    opener.addEventListener("click", press);
    this._pop.view = { opener, parent: () => portal, draw: () => this._drawView() };
    return widget;
  },
  _drawView() {
    const b = this._behaviour;
    const menu = document.createElement("div"); menu.className = "dropdown-menu show dark-dropdown full-screen-widget__pop-menu";
    const item = (id, onPick) => {
      const a = document.createElement("a"); a.setAttribute("role", "button"); a.setAttribute("href", "#"); a.tabIndex = 0; a.className = "dropdown-item";
      a.setAttribute("data-rr-ui-dropdown-item", ""); a.appendChild(document.createTextNode("")); a.appendChild(this._svg());
      a.addEventListener("click", (e) => { e.preventDefault(); this._press(id, a.getAttribute("aria-label")); onPick(); });
      menu.appendChild(a); return a;
    };
    const divider = () => { const hr = document.createElement("hr"); hr.className = "dropdown-divider"; hr.setAttribute("role", "separator"); menu.appendChild(hr); };
    this._viewItems = {};
    const own = () => {
      this._viewItems.speaker = item("stub-view-item", () => { this._layout = "speaker"; this._picked("view"); });
      this._viewItems.gallery = item("stub-view-item", () => { this._layout = "gallery"; this._picked("view"); });
      divider();
      this._viewItems.self = item("stub-self", () => { if (b.selfViewIgnored) return; this._selfHidden = !this._selfHidden; this._picked("view"); });
    };
    // A host's menu has a section more, for what the attendees see: its items name the views too, and are not the host's own.
    const attendees = () => {
      const head = document.createElement("div"); head.className = "dropdown-header"; head.setAttribute("role", "heading"); head.textContent = "Attendee View"; menu.appendChild(head);
      this._viewItems.attendeeSpeaker = item("stub-attendee-speaker", () => { this._attendeeView = "speaker"; this._picked("view"); });
      this._viewItems.attendeeFollow = item("stub-attendee-follow", () => { this._attendeeView = "follow"; this._picked("view"); });
    };
    if (b.role !== "host") own();
    else if (b.attendeeViewFirst) { attendees(); divider(); own(); }
    else { own(); divider(); attendees(); }
    return menu;
  },
  // The items say what is so now: the selected view is checked, and self view says what it would do.
  _paintView() {
    const items = this._viewItems;
    const say = (a, text, label) => { a.setAttribute("aria-label", this._said(label || text)); a.firstChild.data = this._said(text); };
    say(items.speaker, "Speaker View"); say(items.gallery, "Gallery View");
    say(items.self, this._selfHidden ? "Show Self View" : "Hide Self View");
    items.speaker.classList.toggle("full-screen-widget__pop-menu--checked", this._layout === "speaker");
    items.gallery.classList.toggle("full-screen-widget__pop-menu--checked", this._layout === "gallery");
    if (items.attendeeSpeaker) {
      say(items.attendeeSpeaker, "Speaker View", "<Attendee View>.Speaker View"); say(items.attendeeFollow, "Follow Host's View");
      items.attendeeSpeaker.classList.toggle("full-screen-widget__pop-menu--checked", this._attendeeView === "speaker");
      items.attendeeFollow.classList.toggle("full-screen-widget__pop-menu--checked", this._attendeeView === "follow");
    }
  },
  // Another dropdown of the client's, open on the page (as its audio options may be): a div.dropdown-menu.show of the same kind as
  // the View menu, in a portal of its own, with items named like the View's. It is not the View widget's, and not the bridge's
  // to use, press or close; a press of one of its items is logged as stub-stray.
  _drawStray() {
    const portal = document.createElement("div"); portal.className = "audio-options-dropdown";
    const menu = document.createElement("div"); menu.className = "dropdown-menu show dark-dropdown";
    for (const name of ["Speaker View", "Gallery View", "Hide Self View"]) {
      const a = document.createElement("a"); a.setAttribute("role", "button"); a.setAttribute("href", "#"); a.className = "dropdown-item";
      a.setAttribute("aria-label", name); a.textContent = name;
      a.addEventListener("click", (e) => { e.preventDefault(); this._press("stub-stray", name); });
      menu.appendChild(a);
    }
    portal.appendChild(menu); document.body.appendChild(portal);
  },
  // Another part of the client that carries role="menu" (its Leave options): not the bridge's to find, press or close.
  _drawBystander() {
    const box = document.createElement("div"); box.className = "leave-meeting-options"; box.setAttribute("role", "menu");
    const inner = document.createElement("div"); inner.className = "leave-meeting-options__inner";
    const leave = document.createElement("button"); leave.type = "button"; leave.textContent = "Leave Meeting";
    leave.addEventListener("click", () => this._press("stub-leave", "Leave Meeting"));
    inner.appendChild(leave); box.appendChild(inner); document.body.appendChild(box);
    this._bystander = box; this._bystanderWhere = document.body;
  },
  // Its owner's unmount: removeChild on the parent it put it in. It throws, as React's does, when something else took it out.
  _unmountBystander() {
    try { this._bystanderWhere.removeChild(this._bystander); window.__unmounted = "ok"; }
    catch (e) { window.__unmounted = String(e.message || e); window.__reactErrors.push(window.__unmounted); }
  },
  // A modal of the client's, drawn over the meeting on the body. A press of its button is logged, and Zoom closes it as React unmounts
  // a node: in a microtask after the press (or dialogClosesAfterMs later, or never with dialogStays), with removeChild on its parent.
  _showDialog(title, text, buttons) {
    const id = "stub-dialog-" + (++this._dialogs), b = this._behaviour;
    const d = document.createElement("div"); d.id = id; d.setAttribute("role", "dialog"); d.className = "zm-modal";
    const h = document.createElement("h2"); h.textContent = title; d.appendChild(h);
    const p = document.createElement("p"); p.textContent = text; d.appendChild(p);
    const footer = document.createElement("div");
    for (const label of buttons) {
      const btn = document.createElement("button"); btn.textContent = label;
      btn.addEventListener("click", () => {
        this._press(id, label);
        window.__zoomCalls.push(["dialog", title, label]);
        if (b.dialogStays) return;
        if (b.dialogClosesAfterMs) setTimeout(() => this._closeDialog(id), b.dialogClosesAfterMs); else queueMicrotask(() => this._closeDialog(id));
      });
      footer.appendChild(btn);
    }
    d.appendChild(footer); document.body.appendChild(d); this._dialogNodes[id] = d;
    return id;
  },
  // Its owner's unmount: removeChild on the parent it put it in. It throws, as React's does, when something else took it out.
  _closeDialog(id) {
    const d = this._dialogNodes[id]; if (!d) return;
    delete this._dialogNodes[id];
    try { document.body.removeChild(d); } catch (e) { window.__reactErrors.push(String(e.message || e)); }
  },
  _chat(from, text) { this._fire("onReceiveChatMsg", { sender: { name: from }, content: text, timestamp: Date.now() }); },
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
    if (b.handInFooter) {     // the uncommon build: the hand is a footer button of its own, in place of the Reactions slot
      this._button(bar, "stub-hand", "Raise Hand", (btn) => {
        if (!b.handIgnored) this._toggleHand();
        btn.setAttribute("aria-label", this._hand ? "Lower Hand" : "Raise Hand"); btn.textContent = btn.getAttribute("aria-label");
      });
    } else bar.appendChild(this._reactionSlot());
    this._button(bar, "stub-captions", "Show Captions", (btn) => { this._captions = !this._captions; btn.setAttribute("aria-label", this._captions ? "Hide Captions" : "Show Captions"); btn.textContent = btn.getAttribute("aria-label"); });
    document.body.appendChild(footer);
    if (b.strayDropdown) this._drawStray();
    document.body.appendChild(this._viewWidget());
    this._drawBystander();
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
  // The windows run on the stub's clock, which only _later moves.
  _now() { return this._skew; },
  _later(ms) { this._skew += ms; },
  // A call of the SDK is recorded as asked, and is dropped, with nothing called back, when it has a key its method does not take
  // (a key left out is no drop: the gate looks only at the keys given, and the call goes ahead with it undefined) or is made again
  // inside the window kept for its method. A call that is taken starts the window; one that is dropped does not. Answers null for
  // a call that is taken; for one that is dropped, what the API hands back: false, but undefined for putOnHold's window, which is
  // silent (with callbacks given the API answers undefined, as it does to a call it takes).
  _gate(name, opts) {
    const args = {};
    for (const key of Object.keys(opts)) if (key !== "success" && key !== "error") args[key] = opts[key];
    window.__zoomCalls.push([name, args]);
    let drop = Object.keys(args).some((key) => !this._takes[name].includes(key)) ? { answer: false } : null;
    if (!drop) {
      const now = this._now(), last = this._lastCall[name];
      if (last !== undefined && now - last < this._windowMs[name]) drop = { answer: name === "putOnHold" ? undefined : false };
      else this._lastCall[name] = now;
    }
    if (drop) window.__dropped.push([name, args]);
    return drop;
  },
  // A call the SDK takes is answered after the call has returned, not inside it; rename, record and endMeeting are answered inside.
  _answer(name, fn) { if (["rename", "record", "endMeeting"].includes(name)) fn(); else queueMicrotask(fn); },
  // A call only a host or a co-host may make. The SDK refuses a participant in its words with code 1; endMeeting is the host's
  // alone, and refuses a co-host too, with a code and no words. A call it carries out does its work, then is answered.
  _hostCall(name, opts, work) {
    const drop = this._gate(name, opts);
    if (drop) return drop.answer;
    const b = this._behaviour;
    if (b.refuseHostCalls && (name === "endMeeting" ? b.role !== "host" : b.role === "participant")) {
      this._answer(name, () => opts.error && opts.error(name === "endMeeting" ? { errorCode: 3003, errorMessage: null } : { errorCode: 1, errorMessage: "Current user is neither host nor cohost" }));
      return;
    }
    work();
    this._answer(name, () => opts.success && opts.success());
  },
  // The room's own mute is any participant's to make, another's is a host's.
  mute(opts) {
    const work = () => {
      if (opts.userId === 16777216) this._muted = !!opts.mute;
      else { const a = this._attendees.find((x) => x.userId === opts.userId); if (a) a.muted = !!opts.mute; }
      this._fire("onUserUpdate", { userId: opts.userId, muted: !!opts.mute });
    };
    if (opts.userId !== 16777216) return this._hostCall("mute", opts, work);
    const drop = this._gate("mute", opts);
    if (drop) return drop.answer;
    work();
    this._answer("mute", () => opts.success && opts.success());
  },
  // muteAll false asks everyone to unmute: they stay muted until they choose to unmute.
  muteAll(opts) { return this._hostCall("muteAll", opts, () => { if (opts.muteAll) this._attendees.forEach((a) => { a.muted = true; }); }); },
  // record answers at once: the recording changes, and onRecordingChange says so, when Zoom's server confirms.
  record(opts) {
    const b = this._behaviour;
    return this._hostCall("record", opts, () => { if (!b.recordNeverConfirms) setTimeout(() => this._setRecording(opts.record), b.recordConfirmsAfterMs); });
  },
  lockMeeting(opts) { return this._hostCall("lockMeeting", opts, () => { this._locked = !!opts.lockMeeting; }); },
  putOnHold(opts) { return this._hostCall("putOnHold", opts, () => { const a = this._attendees.find((x) => x.userId === opts.userId); if (a) a.waiting = !!opts.hold; }); },
  expel(opts) { return this._hostCall("expel", opts, () => { this._attendees = this._attendees.filter((a) => a.userId !== opts.userId); }); },
  endMeeting(opts) { return this._hostCall("endMeeting", opts, () => setTimeout(() => this._status(3), 10)); },
  // rename runs the SDK's host check first and goes on: a participant is told so by error and then, with their own id, is renamed
  // and told by success. Any participant may rename themselves: the room's name follows, and Zoom says so as it does of every
  // change of a user.
  rename(opts) {
    const drop = this._gate("rename", opts);
    if (drop) return drop.answer;
    const participant = this._behaviour.role === "participant";
    if (participant || opts.userId === 16777216) { this._name = opts.newName; this._fire("onUserUpdate", { userId: 16777216, userName: this._name }); }
    this._answer("rename", () => {
      if (participant) opts.error && opts.error({ errorCode: 1, errorMessage: "Current user is neither host nor cohost" });
      opts.success && opts.success();
    });
  },
  leaveMeeting(opts) { window.__zoomCalls.push(["leaveMeeting", { confirm: opts.confirm }]); opts.success && opts.success(); },
};
"""


async def block_sdk_cdn(context):
    """Abort every request to Zoom's CDN so the page runs on the stub only."""

    async def abort(route):
        await route.abort()

    await context.route("https://source.zoom.us/**", abort)
