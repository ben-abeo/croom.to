# Meeting Controls, Live State and Dialogs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything a participant or host can do in a Zoom or Meet call, done from the room page; every dialog Zoom draws on the TV answered by policy or from the table; the table always showing the meeting as the platform has it; icons on every button.

**Architecture:** The TV page's bridge (`meeting.js`) reads the whole meeting from Zoom (current user, attendees, the client's own buttons, dialogs, chat, recording) and reports a `"state"` event on change; it carries out controls as SDK calls or button presses. The Zoom provider keeps the reading on `MeetingInfo` as a platform-neutral model (`controls`, `people`, `chat`, `notice`, `dialog`) and answers `control(name, value)`; the dialog policy lives there, from `config.meeting.dialogs`. The control service serializes the model into `/api/status` and routes `POST /api/meeting/control`, `/dialog`, `/chat/seen`. The room page draws its meeting screen from the model: a primary and a secondary row, a dialog card, a notice line, and Participants, Chat and Host sub-screens, with inline SVG icons. The Meet provider answers `control()` for what Meet's page allows.

**Tech Stack:** Python 3.12, aiohttp, Playwright (headless Chromium in tests), the stub `window.ZoomMtg` in `tests/unit/meeting/zoom_stub.py`, vanilla JS/CSS for the room page, pytest.

**Spec:** `docs/superpowers/specs/2026-10-13-meeting-controls-and-dialogs-design.md`

**Base:** branch `meeting-controls` from `main` after `meeting-state` has merged (the bridge's `readState()`, `video()`, `setVideo()`, `diagnostics()`, the `"state"` event, `MeetingControlError`, and the 409-with-words routes are in that branch).

## Global Constraints

- Internal names stay `croom`; the product name in copy is Crystal Meet; no secrets in the repo (`REPLACE_WITH_...` placeholders).
- No attribution lines in commit messages; messages explain the why in plain sentences.
- Tests: `.venv/bin/pytest -q -p no:cacheprovider -W error <path>`; page tests drive real Chromium through Playwright; never run the whole suite from a task (the controller runs the gate).
- The room page keeps its fixed, non-scrolling layout at 1280 by 800 (`fits()` in `tests/unit/control/test_page.py`), and scrolls on a phone.
- A control the platform cannot carry out raises `MeetingControlError` with words for the table; a route never answers 500 for one.
- The table shows what the platform has (re-read after every action and on every event), never the last thing it pressed.
- Nothing on the TV ever waits for a mouse: a dialog is acknowledged by policy or answered from the table.
- `ZoomMtg` SDK 6.5.0 Client View; `source.zoom.us` is blocked in tests and the stub stands in.
- Labels and dialog texts pinned in tests are the stub's; the inventory run (spec section 7) on the Pi may change them, and the pattern tables are written to take such a change in one place.
- Lucide icons (ISC) with the licence noted in `src/croom/control/static/LICENSES.md`.

## Review Focus

1. A `"state"` or `"dialog"` event arriving while a join is still in `_wait_for_connection` must not be mistaken for a status event or lost: the wait ignores unknown states and the baseline sync runs only when connected (Task 5 test `test_events_before_connected_are_ignored_and_the_model_fills_after`).
2. A dialog the policy acknowledges must be pressed exactly once even when the observer reports it twice (a re-render of the same modal), and a dialog that vanishes on its own must clear from the status (Task 5 tests `test_an_acknowledged_dialog_is_pressed_once` and `test_a_dialog_that_goes_away_clears`).
3. A host control pressed by a participant, or any Zoom refusal, must answer in words and leave the meeting running (Task 5 test `test_a_host_call_refused_by_zoom_answers_in_words`).
4. The room page with everything shown (host role, a waiting dialog, a notice, unread chat) must still fit 1280 by 800 without scrolling, and must show nothing for a control that is not available (Task 8 test `test_the_meeting_screen_with_everything_still_fits_and_hides_the_unavailable`).
5. A status answer with no `controls` at all (the Meet provider before Task 11, Teams, Webex) must leave the meeting screen as it is today: Mute, camera, Leave (Task 8 test `test_a_status_without_controls_keeps_the_three_buttons`).

---

### Task 1: The stub SDK learns the whole meeting, and the bridge reads it

**Files:**
- Modify: `tests/unit/meeting/zoom_stub.py`
- Modify: `src/croom/meeting/providers/zoom_sdk_page/meeting.js`
- Test: `tests/unit/meeting/test_zoom_sdk_page.py`

**Interfaces:**
- Consumes: `bridge.readState()`, `bridge.video()`, `bridge.setVideo(on)`, `syncState()` from branch `meeting-state`.
- Produces: `window.crystalMeet.readMeeting()` → `{me: {userId, muted, videoOn, isHost, isCoHost, raisedHand}, role: "participant"|"co-host"|"host", buttons: {camera: {on}, raise_hand: {on}, reactions: {}, captions: {on}, layout: {value}, hide_self: {on}}, participants: [{id, name, muted, video, hand, host, waiting, me}], count, recording: bool, keys: [...]}` (only the buttons the client shows appear under `buttons`); the `"state"` event's detail becomes this whole object. Stub behaviours `role`, `attendees`, `recording`, `window.ZoomMtg._attendees`, `_setAttendee(id, patch)`, `_setRecording(on)`, the client buttons `#stub-hand`, `#stub-reactions` (+ menu `#stub-reaction-menu` with `button[data-reaction]`), `#stub-captions`, `#stub-view` (+ menu `#stub-view-menu` with `button[data-view]`), `#stub-self`, `window.__presses` (a log of `[buttonId, label]`).

- [ ] **Step 1: Extend the stub with attendees, roles, recording and the client's buttons**

In `tests/unit/meeting/zoom_stub.py`, replace the `_behaviour` line and add the attendee model and buttons. The `join()` body's `setTimeout(() => { ... })` grows the footer; keep everything already there.

```python
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
  _attendees: [],
  _status(code) { const data = {}; data[this._behaviour.statusField] = code; this._fire("onMeetingStatus", data); },
  _setMuted(muted) { this._muted = !!muted; this._fire("onUserUpdate", { userId: 16777216 }); },
  _setAttendee(id, patch) { const a = this._attendees.find((x) => x.userId === id); if (a) Object.assign(a, patch); this._fire("onUserUpdate", { userId: id }); },
  _setRecording(on) { this._behaviour.recording = !!on; this._fire("onRecordingChange", { bRecording: !!on }); },
  _me() { const b = this._behaviour; return { userId: 16777216, userName: "Room 1", muted: this._muted, bVideoOn: this._videoOn,
           isHost: b.role === "host", isCoHost: b.role === "co-host", bRaiseHand: this._hand }; },
  _labelVideo(button) {
    const on = this._videoOn;
    if (this._behaviour.labelStyle === "client") {
      button.setAttribute("aria-label", on ? "stop my video" : "start my video");
      button.textContent = on ? "Stop Video" : "Start Video";
    } else {
      button.setAttribute("aria-label", on ? "Stop Video" : "Start Video");
    }
  },
  _button(footer, id, label, onClick) {
    const b = document.createElement("button"); b.id = id; b.setAttribute("aria-label", label); b.textContent = label;
    b.addEventListener("click", () => { window.__presses.push([id, b.getAttribute("aria-label")]); onClick(b); });
    footer.appendChild(b); return b;
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
    const footer = document.createElement("div"); footer.className = "footer";
    const video = document.createElement("button"); video.id = "stub-video"; this._labelVideo(video);
    video.addEventListener("click", () => { window.__videoClicks += 1; if (b.videoStuckOff) return; this._videoOn = !this._videoOn; this._labelVideo(video); });
    footer.appendChild(video);
    this._button(footer, "stub-hand", "Raise Hand", (btn) => { this._hand = !this._hand; btn.setAttribute("aria-label", this._hand ? "Lower Hand" : "Raise Hand"); btn.textContent = btn.getAttribute("aria-label"); });
    this._button(footer, "stub-reactions", "Reactions", () => this._menu("stub-reaction-menu",
      [["thumbsup", "👍"], ["clap", "👏"], ["heart", "❤️"], ["joy", "😂"], ["open_mouth", "😮"], ["tada", "🎉"]],
      (value) => window.__zoomCalls.push(["reaction", value])));
    this._button(footer, "stub-captions", "Show Captions", (btn) => { this._captions = !this._captions; btn.setAttribute("aria-label", this._captions ? "Hide Captions" : "Show Captions"); btn.textContent = btn.getAttribute("aria-label"); });
    this._button(footer, "stub-view", "View", () => this._menu("stub-view-menu", [["speaker", "Speaker"], ["gallery", "Gallery"]],
      (value) => { this._layout = value; }));
    this._button(footer, "stub-self", "Hide Self View", (btn) => { this._selfHidden = !this._selfHidden; btn.setAttribute("aria-label", this._selfHidden ? "Show Self View" : "Hide Self View"); btn.textContent = btn.getAttribute("aria-label"); });
    document.body.appendChild(footer);
  },
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
  getCurrentUser(opts) { opts.success && opts.success({ result: { currentUser: this._me() } }); },
  getAttendeeslist(opts) { opts.success && opts.success({ result: { attendeesList: [this._me()].concat(this._attendees) } }); },
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
```

Update the module docstring: add "role (participant, co-host, host), attendees, recording, refuseHostCalls; `_setAttendee(id, patch)`, `_setRecording(on)`; the client's hand, reactions, captions, view and self-view buttons under `.footer`, with `window.__presses` logging each press; host calls are recorded and refused for a participant."

- [ ] **Step 2: Write the failing page tests**

Append to `tests/unit/meeting/test_zoom_sdk_page.py`:

```python
async def test_read_meeting_reports_me_the_room_and_the_clients_buttons():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.role = 'host';") as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["me"] == {"userId": 16777216, "muted": False, "videoOn": True, "isHost": True, "isCoHost": False, "raisedHand": False}
        assert m["role"] == "host" and m["count"] == 3 and m["recording"] is False
        assert [p["name"] for p in m["participants"]] == ["Room 1", "Ann Lee", "Raj Patel"]
        assert m["participants"][2] == {"id": 16777218, "name": "Raj Patel", "muted": True, "video": False, "hand": True, "host": False, "waiting": False, "me": False}
        assert m["participants"][0]["me"] is True
        assert m["buttons"] == {"camera": {"on": True}, "raise_hand": {"on": False}, "reactions": {}, "captions": {"on": False},
                                "layout": {"value": "speaker"}, "hide_self": {"on": False}}


async def test_without_a_toolbar_no_buttons_are_reported_and_the_rest_still_reads():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;") as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.readMeeting()")
        assert m["buttons"] == {} and m["count"] == 3 and m["me"]["videoOn"] is True   # video from the user's record


async def test_a_change_to_another_participant_reaches_the_device_with_the_whole_model():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("window.ZoomMtg._setAttendee(16777218, {bRaiseHand: false})")
        await run.wait_for("state")
        import json
        detail = json.loads(next(d for s, d in run.events if s == "state"))
        assert detail["participants"][2]["hand"] is False and detail["me"]["muted"] is False
```

- [ ] **Step 3: Run them to see them fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider -W error tests/unit/meeting/test_zoom_sdk_page.py -k "read_meeting or no_buttons_are_reported or another_participant"`
Expected: FAIL (`readMeeting` is not a function; the state detail lacks `participants`).

- [ ] **Step 4: Implement `readMeeting()` in the bridge**

In `meeting.js`, after `bridge.readState`, add the attendee reading and the button table, and make `syncState` compare and send the whole model:

```js
  function attendees() {
    return new Promise((resolve) => {
      ZoomMtg.getAttendeeslist({
        success: (result) => resolve((result && result.result && result.result.attendeesList) || []),
        error: () => resolve([]),
      });
    });
  }
  // The client's buttons the table may press, each found by what it says. The inventory run on the device
  // (spec section 7) confirms these words; a button not found is simply not offered.
  const BUTTONS = {
    raise_hand: { words: [["raise hand"], ["lower hand"]], on: (label) => label.includes("lower") },
    reactions: { words: [["reaction"]], on: null },
    captions: { words: [["caption", "show"], ["caption", "hide"], ["closed caption"]], on: (label) => label.includes("hide") },
    layout: { words: [["view"]], on: null },
    hide_self: { words: [["self view"]], on: (label) => label.includes("show") },
  };
  function labelOf(b) { return ((b.getAttribute("aria-label") || b.textContent || "")).trim(); }
  function findButton(name) {
    const spec = BUTTONS[name];
    for (const b of document.querySelectorAll("button")) {
      if (b.closest('[role="menu"]')) continue;
      const label = labelOf(b).toLowerCase();
      if (spec.words.some((all) => all.every((w) => label.includes(w)))) return b;
    }
    return null;
  }
  function buttonsSeen() {
    const out = {};
    const video = bridge.video();
    if (video.on !== null) out.camera = { on: video.on };
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
  bridge.readMeeting = async function () {
    const user = await currentUser();
    const list = await attendees();
    const me = {
      userId: pick(user, ["userId"]) === undefined ? bridge.userId : pick(user, ["userId"]),
      muted: !!pick(user, ["muted", "bMuted", "isMuted"]),
      videoOn: (bridge.video().on !== null) ? bridge.video().on : !!pick(user, ["bVideoOn", "videoOn", "isVideoOn"]),
      isHost: !!pick(user, ["isHost", "bHost"]),
      isCoHost: !!pick(user, ["isCoHost", "isCohost", "bCoHost"]),
      raisedHand: !!pick(user, ["bRaiseHand", "raiseHand", "isHandRaised", "bHandRaised"]),
    };
    if (user && pick(user, ["muted", "bMuted", "isMuted"]) !== undefined) bridge.muted = me.muted;
    const participants = list.map((a) => ({
      id: pick(a, ["userId"]), name: String(pick(a, ["userName", "displayName", "name"]) || ""),
      muted: !!pick(a, ["muted", "bMuted"]), video: !!pick(a, ["bVideoOn", "videoOn"]),
      hand: !!pick(a, ["bRaiseHand", "raiseHand", "bHandRaised"]), host: !!pick(a, ["isHost", "bHost"]),
      waiting: !!pick(a, ["waiting", "bHold", "isInWaitingRoom"]), me: pick(a, ["userId"]) === me.userId,
    }));
    return {
      me: me, role: me.isHost ? "host" : (me.isCoHost ? "co-host" : "participant"),
      buttons: buttonsSeen(), participants: participants, count: participants.length,
      recording: !!bridge._recording, keys: user ? Object.keys(user) : [],
    };
  };
  bridge._recording = false;
  bridge._layout = null;
```

Replace `syncState` so it compares the whole model (minus `keys`) and sends it:

```js
  let lastSeen = null;
  async function syncState() {
    if (bridge.state !== "connected") return;
    const now = await bridge.readMeeting();
    const copy = Object.assign({}, now); delete copy.keys;
    const key = JSON.stringify(copy);
    if (key === lastSeen) return;
    const first = lastSeen === null;
    lastSeen = key;
    if (!first && typeof window.crystalMeetEvent === "function") {
      try { window.crystalMeetEvent("state", JSON.stringify(now)); } catch (e) { /* the device is not listening */ }
    }
  }
```

Register the recording listener next to `onUserUpdate` in `main()`:

```js
    ZoomMtg.inMeetingServiceListener("onRecordingChange", (data) => { bridge._recording = !!(data && (data.bRecording !== undefined ? data.bRecording : data.recording)); syncState(); });
    ZoomMtg.inMeetingServiceListener("onUserJoin", () => { syncState(); });
    ZoomMtg.inMeetingServiceListener("onUserLeave", () => { syncState(); });
```

Keep `readState()` as a thin wrapper for the `meeting-state` tests: `bridge.readState = async () => { const m = await bridge.readMeeting(); return Object.assign({}, m.me, { keys: m.keys }); };` placed after `readMeeting` (and delete the older `readState` body).

- [ ] **Step 5: Run the page tests**

Run: `.venv/bin/pytest -q -p no:cacheprovider -W error tests/unit/meeting/test_zoom_sdk_page.py`
Expected: PASS (the `meeting-state` tests included: `test_the_bridge_reads_mute_and_video_from_zoom` still reads `muted`, `videoOn`, `isHost`, `raisedHand`, `keys` through the wrapper).

- [ ] **Step 6: Commit**

```bash
git add tests/unit/meeting/zoom_stub.py src/croom/meeting/providers/zoom_sdk_page/meeting.js tests/unit/meeting/test_zoom_sdk_page.py
git commit -m "feat(meeting): the TV page reads the whole meeting from Zoom: the room, every participant, the client's buttons and recording"
```

---

### Task 2: The bridge carries out button controls

**Files:**
- Modify: `src/croom/meeting/providers/zoom_sdk_page/meeting.js`
- Test: `tests/unit/meeting/test_zoom_sdk_page.py`

**Interfaces:**
- Produces: `window.crystalMeet.control(name, value)` for `camera`, `raise_hand`, `reactions` (value: a key from the stub's menu such as `thumbsup`, or an emoji; matched against the menu items' text or data attribute), `captions`, `layout` (value `speaker`/`gallery`), `hide_self`. Resolves with the new `readMeeting()`; rejects with `Error("… is not on the screen yet; try again in a moment")` when the button is missing, `Error("Zoom offers no <item> here")` when a menu lacks the item.

- [ ] **Step 1: Failing tests**

```python
async def test_button_controls_press_the_clients_buttons_and_settle_on_the_new_state():
    async with PageRun() as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.control('raise_hand')")
        assert m["buttons"]["raise_hand"] == {"on": True} and m["me"]["raisedHand"] is True    # the button and the room's record agree
        m = await run.page.evaluate("window.crystalMeet.control('captions')")
        assert m["buttons"]["captions"] == {"on": True}
        m = await run.page.evaluate("window.crystalMeet.control('hide_self')")
        assert m["buttons"]["hide_self"] == {"on": True}
        m = await run.page.evaluate("window.crystalMeet.control('camera')")
        assert m["buttons"]["camera"] == {"on": False} and m["me"]["videoOn"] is False
        assert [p[0] for p in await run.page.evaluate("window.__presses")] == ["stub-hand", "stub-captions", "stub-self"]


async def test_menus_open_pick_and_close_within_one_call():
    async with PageRun() as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.control('layout', 'gallery')")
        assert m["buttons"]["layout"] == {"value": "gallery"}
        assert await run.page.evaluate("document.getElementById('stub-view-menu')") is None
        await run.page.evaluate("window.crystalMeet.control('reactions', '👏')")
        assert ["reaction", "clap"] in await run.page.evaluate("window.__zoomCalls")
        assert await run.page.evaluate("document.getElementById('stub-reaction-menu')") is None
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.control('reactions', '🦄')")
        assert "offers no" in str(failure.value)
        assert await run.page.evaluate("document.getElementById('stub-reaction-menu')") is None   # closed again after the miss


async def test_a_missing_button_says_so():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.noToolbar = true;") as run:
        await run.wait_for("connected")
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.control('raise_hand')")
        assert "not on the screen yet" in str(failure.value)
```

- [ ] **Step 2: Run to see them fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider -W error tests/unit/meeting/test_zoom_sdk_page.py -k "button_controls or menus_open or missing_button"`
Expected: FAIL (`control` is not a function).

- [ ] **Step 3: Implement**

```js
  const settle = (done, ms) => new Promise((resolve) => {
    let tries = 0;
    const tick = () => { if (done() || tries++ >= Math.ceil(ms / 100)) resolve(); else setTimeout(tick, 100); };
    setTimeout(tick, 100);
  });
  function pressButton(name) {
    const b = findButton(name);
    if (!b) throw new Error("Zoom's " + name.replace("_", " ") + " button is not on the screen yet; try again in a moment");
    const before = labelOf(b);
    b.click();
    return settle(() => labelOf(b) !== before, 2000);
  }
  async function pickFromMenu(name, value) {
    const b = findButton(name);
    if (!b) throw new Error("Zoom's " + name + " button is not on the screen yet; try again in a moment");
    b.click();
    await settle(() => document.querySelector('[role="menu"], [role="menuitem"]'), 2000);
    const wanted = String(value).toLowerCase();
    const items = Array.from(document.querySelectorAll('[role="menuitem"], [role="menu"] button'));
    const item = items.find((i) => {
      const text = (i.textContent || "").trim().toLowerCase();
      const data = Object.values(i.dataset || {}).map((v) => String(v).toLowerCase());
      return text === wanted || data.includes(wanted) || (i.getAttribute("aria-label") || "").toLowerCase() === wanted;
    });
    if (!item) {
      for (const m of document.querySelectorAll('[role="menu"]')) m.remove();   // never leave a menu open on the TV
      throw new Error("Zoom offers no " + value + " here");
    }
    item.click();
    await settle(() => !document.querySelector('[role="menu"]'), 1000);
    if (name === "layout") bridge._layout = String(value);
  }
  bridge.control = async function (name, value) {
    switch (name) {
      case "camera": await bridge.setVideo(!(bridge.video().on)); break;
      case "raise_hand": case "captions": case "hide_self": await pressButton(name); break;
      case "reactions": await pickFromMenu("reactions", value); break;
      case "layout": await pickFromMenu("layout", value); break;
      default: throw new Error("Unknown control " + name);
    }
    await syncState();
    return bridge.readMeeting();
  };
```

- [ ] **Step 4: Run the page tests** — `.venv/bin/pytest -q -p no:cacheprovider -W error tests/unit/meeting/test_zoom_sdk_page.py` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/croom/meeting/providers/zoom_sdk_page/meeting.js tests/unit/meeting/test_zoom_sdk_page.py
git commit -m "feat(meeting): the TV page presses the client's own buttons for the hand, reactions, captions, the view and self view"
```

---

### Task 3: Host calls and the SDK init flags

**Files:**
- Modify: `src/croom/meeting/providers/zoom_sdk_page/meeting.js`
- Test: `tests/unit/meeting/test_zoom_sdk_page.py`

**Interfaces:**
- Produces: `window.crystalMeet.control("host.mute_all")`, `"host.ask_unmute"`, `"host.record"`, `"host.lock"`, `"host.admit"` (value: participant id), `"host.hold"` (id), `"host.remove"` (id), `"host.mute_one"` (id), `"host.end"`, `"rename"` (value: new name). Each resolves with `readMeeting()` or rejects with `Error(Zoom's words)`. `init` sets `isSupportChat: true`, `isSupportCC: true`, `disableRecord: false`.

- [ ] **Step 1: Failing tests**

```python
async def test_host_calls_go_to_the_sdk_and_a_participant_gets_zooms_refusal():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.role = 'host';") as run:
        await run.wait_for("connected")
        m = await run.page.evaluate("window.crystalMeet.control('host.mute_all')")
        assert all(p["muted"] for p in m["participants"] if not p["me"])
        m = await run.page.evaluate("window.crystalMeet.control('host.record')")
        assert m["recording"] is True
        m = await run.page.evaluate("window.crystalMeet.control('host.remove', 16777218)")
        assert [p["name"] for p in m["participants"]] == ["Room 1", "Ann Lee"]
        await run.page.evaluate("window.crystalMeet.control('host.hold', 16777217)")
        m = await run.page.evaluate("window.crystalMeet.control('host.admit', 16777217)")
        assert m["participants"][1]["waiting"] is False
        calls = [c[0] for c in await run.page.evaluate("window.__zoomCalls")]
        assert calls[-5:] == ["muteAll", "record", "expel", "putOnHold", "putOnHold"]
    async with PageRun() as run:                                                  # a participant
        await run.wait_for("connected")
        with pytest.raises(Exception) as failure:
            await run.page.evaluate("window.crystalMeet.control('host.lock')")
        assert "Not the host" in str(failure.value)


async def test_end_for_everyone_ends_the_meeting():
    async with PageRun(stub=STUB_JS + "window.ZoomMtg._behaviour.role = 'host';") as run:
        await run.wait_for("connected")
        await run.page.evaluate("window.crystalMeet.control('host.end')")
        await run.wait_for("left")


async def test_init_asks_for_chat_captions_and_recording():
    async with PageRun() as run:
        await run.wait_for("connected")
        [init] = await run.calls("init")
        assert init[1]["isSupportChat"] is True and init[1]["isSupportCC"] is True and init[1]["disableRecord"] is False
```

Update `test_joins_with_the_one_time_parameters_and_reports_connected` if it asserts `disableRecord` or `isSupportChat` (it asserts `leaveUrl`, `disablePreview`, `showMeetingHeader` only; leave it).

- [ ] **Step 2: Run to see them fail** — same command with `-k "host_calls or end_for_everyone or init_asks"` — Expected: FAIL.

- [ ] **Step 3: Implement**

```js
  const sdk = (method, args) => new Promise((resolve, reject) => {
    ZoomMtg[method](Object.assign({}, args, { success: () => resolve(true), error: (e) => reject(new Error(words(e))) }));
  });
  const HOST = {
    "host.mute_all": () => sdk("muteAll", { muteAll: true }),
    "host.ask_unmute": () => sdk("muteAll", { muteAll: false }),
    "host.record": async () => sdk("record", { record: !bridge._recording }),
    "host.lock": async () => { const r = await sdk("lockMeeting", { lockMeeting: !bridge._locked }); bridge._locked = !bridge._locked; return r; },
    "host.admit": (id) => sdk("putOnHold", { userId: Number(id), hold: false }),
    "host.hold": (id) => sdk("putOnHold", { userId: Number(id), hold: true }),
    "host.remove": (id) => sdk("expel", { userId: Number(id) }),
    "host.mute_one": (id) => sdk("mute", { userId: Number(id), mute: true }),
    "host.end": () => sdk("endMeeting", {}),
    "rename": (name) => sdk("rename", { userId: bridge.userId, oldName: bridge.userName || "", newName: String(name) }),
  };
  bridge._locked = false;
```

In `bridge.control`, before `default:` add `default: if (HOST[name]) { await HOST[name](value); break; } throw new Error("Unknown control " + name);` (replace the existing default). `readMeeting()` reports `locked: !!bridge._locked` beside `recording`. In `connected()`'s `getCurrentUser` success, keep `bridge.userName = user.userName` for `rename`. In `ZoomMtg.init`, set `disableRecord: false`, `isSupportChat: true`, `isSupportCC: true`.

- [ ] **Step 4: Run the page tests** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/croom/meeting/providers/zoom_sdk_page/meeting.js tests/unit/meeting/test_zoom_sdk_page.py
git commit -m "feat(meeting): host controls through the SDK from the TV page, and the init asks for chat, captions and recording"
```

---

### Task 4: Dialogs and chat in the page

**Files:**
- Modify: `src/croom/meeting/providers/zoom_sdk_page/meeting.js`
- Modify: `tests/unit/meeting/zoom_stub.py` (`_showDialog`, `_chat`)
- Test: `tests/unit/meeting/test_zoom_sdk_page.py`

**Interfaces:**
- Produces: events `("dialog", JSON{id, title, text, buttons})`, `("dialog-closed", JSON{id})`, `("chat", JSON{from, text, at})`; `window.crystalMeet.answerDialog(id, label)` → resolves `true` when pressed, rejects when the dialog or the button is gone; `window.crystalMeet.dialogs()` → the open dialogs (for diagnostics).

- [ ] **Step 1: Stub additions**

In `zoom_stub.py`'s `window.ZoomMtg` add:

```js
  _dialogs: 0,
  _showDialog(title, text, buttons) {
    const id = "stub-dialog-" + (++this._dialogs);
    const d = document.createElement("div"); d.id = id; d.setAttribute("role", "dialog"); d.className = "zm-modal";
    const h = document.createElement("h2"); h.textContent = title; d.appendChild(h);
    const p = document.createElement("p"); p.textContent = text; d.appendChild(p);
    const footer = document.createElement("div");
    for (const label of buttons) { const b = document.createElement("button"); b.textContent = label;
      b.addEventListener("click", () => { window.__zoomCalls.push(["dialog", title, label]); d.remove(); }); footer.appendChild(b); }
    d.appendChild(footer); document.body.appendChild(d); return id;
  },
  _closeDialog(id) { const d = document.getElementById(id); if (d) d.remove(); },
  _chat(from, text) { this._fire("onReceiveChatMsg", { sender: { name: from }, content: text, timestamp: Date.now() }); },
```

- [ ] **Step 2: Failing tests**

```python
async def test_a_dialog_is_reported_with_its_words_and_buttons_and_answered_by_label():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("window.ZoomMtg._showDialog('Recording consent', 'This meeting is being recorded.', ['Got it', 'Leave meeting'])")
        await run.wait_for("dialog")
        import json
        d = json.loads(next(x for s, x in run.events if s == "dialog"))
        assert d["title"] == "Recording consent" and d["buttons"] == ["Got it", "Leave meeting"] and "being recorded" in d["text"]
        assert not any(c[0] == "dialog" for c in await run.page.evaluate("window.__zoomCalls"))   # the page presses nothing on its own
        assert await run.page.evaluate(f"window.crystalMeet.answerDialog({d['id']}, 'got it')") is True
        assert ["dialog", "Recording consent", "Got it"] in await run.page.evaluate("window.__zoomCalls")
        await run.wait_for("dialog-closed")


async def test_a_dialog_that_goes_away_on_its_own_is_reported_closed_and_cannot_be_answered():
    async with PageRun() as run:
        await run.wait_for("connected")
        sid = await run.page.evaluate("window.ZoomMtg._showDialog('Unmute?', 'The host would like you to unmute.', ['Unmute', 'Stay muted'])")
        await run.wait_for("dialog")
        import json
        d = json.loads(next(x for s, x in run.events if s == "dialog"))
        await run.page.evaluate(f"window.ZoomMtg._closeDialog('{sid}')")
        await run.wait_for("dialog-closed")
        with pytest.raises(Exception):
            await run.page.evaluate(f"window.crystalMeet.answerDialog({d['id']}, 'Unmute')")


async def test_chat_messages_reach_the_device():
    async with PageRun() as run:
        await run.wait_for("connected")
        await run.page.evaluate("window.ZoomMtg._chat('Ann Lee', 'Can you hear us?')")
        await run.wait_for("chat")
        import json
        c = json.loads(next(x for s, x in run.events if s == "chat"))
        assert c["from"] == "Ann Lee" and c["text"] == "Can you hear us?" and c["at"]
```

- [ ] **Step 3: Run to see them fail** — `-k "dialog or chat_messages"` — Expected: FAIL.

- [ ] **Step 4: Implement**

```js
  // ---- Dialogs: everything the client draws over the meeting is reported; nothing is pressed here on its own ----
  const DIALOG_SELECTOR = '[role="dialog"], [role="alertdialog"], .zm-modal, .zm-modal-legacy';
  const dialogs = new Map();   // element -> id
  let dialogIds = 0;
  function dialogInfo(el, id) {
    const buttons = Array.from(el.querySelectorAll("button")).map((b) => labelOf(b)).filter(Boolean);
    const heading = el.querySelector("h1, h2, h3, [class*='title']");
    const title = heading ? heading.textContent.trim() : "";
    const text = (el.innerText || "").replace(/\s+/g, " ").trim();
    const body = title && text.startsWith(title) ? text.slice(title.length).trim() : text;
    return { id: id, title: title, text: body.slice(0, 400), buttons: buttons };
  }
  function scanDialogs() {
    const seen = new Set();
    for (const el of document.querySelectorAll(DIALOG_SELECTOR)) {
      if (el.closest(DIALOG_SELECTOR) !== el) continue;   // nested parts of one modal
      seen.add(el);
      if (!dialogs.has(el)) {
        const id = ++dialogIds;
        dialogs.set(el, id);
        reportJson("dialog", dialogInfo(el, id));
      }
    }
    for (const [el, id] of Array.from(dialogs.entries())) {
      if (!seen.has(el) || !el.isConnected) { dialogs.delete(el); reportJson("dialog-closed", { id: id }); }
    }
  }
  function reportJson(state, data) {
    if (typeof window.crystalMeetEvent === "function") {
      try { window.crystalMeetEvent(state, JSON.stringify(data)); } catch (e) { /* the device is not listening */ }
    }
  }
  bridge.dialogs = () => Array.from(dialogs.entries()).map(([el, id]) => dialogInfo(el, id));
  bridge.answerDialog = function (id, label) {
    const entry = Array.from(dialogs.entries()).find(([, i]) => i === Number(id));
    if (!entry) throw new Error("That dialog is no longer on the screen");
    const wanted = String(label).trim().toLowerCase();
    const button = Array.from(entry[0].querySelectorAll("button")).find((b) => labelOf(b).toLowerCase() === wanted);
    if (!button) throw new Error("The dialog has no button called " + label);
    button.click();
    return true;
  };
  new MutationObserver(() => scanDialogs()).observe(document.documentElement, { childList: true, subtree: true });
  // ---- Chat ----
  function chatListener(data) {
    const from = pick(data, ["senderName", "from"]) || (data && data.sender && (data.sender.name || data.sender.userName)) || "";
    const text = pick(data, ["content", "message", "text", "msg"]) || "";
    const at = new Date(Number(pick(data, ["timestamp", "time"])) || Date.now()).toISOString();
    reportJson("chat", { from: String(from), text: String(text), at: at });
  }
```

Register in `main()`: `ZoomMtg.inMeetingServiceListener("onReceiveChatMsg", chatListener);`. Call `scanDialogs()` once after the observer is created, and after `report("connected", "")`.

- [ ] **Step 5: Run the page tests** — Expected: PASS. Also run `tests/unit/meeting/test_zoom_sdk_provider.py` to see nothing on the provider side broke (the provider ignores unknown events in `_wait_for_connection`; `_watch_meeting` ignores them too until Task 5).

- [ ] **Step 6: Commit**

```bash
git add src/croom/meeting/providers/zoom_sdk_page/meeting.js tests/unit/meeting/zoom_stub.py tests/unit/meeting/test_zoom_sdk_page.py
git commit -m "feat(meeting): the TV page reports every dialog Zoom draws and every chat message, and answers a dialog by label on request"
```

---

### Task 5: The provider's model, controls and dialog policy

**Files:**
- Modify: `src/croom/meeting/providers/base.py` (`MeetingInfo` fields, `control()`, `DialogPolicy` helper)
- Modify: `src/croom/core/config.py` (`DialogsConfig`, `ControlsConfig`, `MeetingConfig.dialogs/controls`, `to_dict`)
- Modify: `src/croom/meeting/providers/zoom_sdk.py`
- Modify: `src/croom/meeting/service.py` (`control()` passthrough; the provider gets `config.meeting`)
- Test: `tests/unit/meeting/test_zoom_sdk_provider.py`, `tests/unit/core/test_config.py` (or the existing config test module; find it with `ls tests/unit/core`)

**Interfaces:**
- Produces: `MeetingInfo.controls: Dict`, `.people: Dict ({"count", "list"})`, `.chat: Dict ({"unread", "messages"})`, `.notice: str`, `.dialog: Optional[Dict]`, `.locked: bool`, `.recording: bool`; `MeetingProvider.control(name, value=None) -> Dict` (base raises `MeetingControlError("Not available in this meeting")`); `ZoomSdkProvider.control()`; `DialogsConfig(acknowledge: List[str], ask_everything: bool)`, `ControlsConfig(reactions: List[str])`; `MeetingService.control(name, value)`; `croom.meeting.providers.base.controls_from(reading: dict, reactions: List[str]) -> Dict` (pure mapping, tested without a browser).

- [ ] **Step 1: Config**

In `core/config.py` before `MeetingConfig`:

```python
DEFAULT_ACKNOWLEDGE = ["is being recorded", "is being transcribed", "AI Companion", "Zoom AI is on",
                       "has been muted", "has been unmuted", "recording has started", "recording has stopped"]
DEFAULT_REACTIONS = ["👍", "👏", "❤️", "😂", "😮", "🎉"]


@dataclass
class DialogsConfig:
    """What the agent does with the dialogs Zoom draws over the TV (spec 2026-10-13, section 5)."""
    acknowledge: List[str] = field(default_factory=lambda: list(DEFAULT_ACKNOWLEDGE))   # one-button notices pressed at once
    ask_everything: bool = False   # every dialog waits for a tap on the table, the acknowledgeable ones too


@dataclass
class ControlsConfig:
    reactions: List[str] = field(default_factory=lambda: list(DEFAULT_REACTIONS))
```

In `MeetingConfig` add `dialogs: DialogsConfig = field(default_factory=DialogsConfig)` and `controls: ControlsConfig = field(default_factory=ControlsConfig)` and:

```python
    def __post_init__(self):
        if isinstance(self.dialogs, dict):
            self.dialogs = DialogsConfig(**self.dialogs)
        if isinstance(self.controls, dict):
            self.controls = ControlsConfig(**self.controls)
```

In `to_dict()`'s meeting block add `"dialogs": {"acknowledge": list(self.meeting.dialogs.acknowledge), "ask_everything": self.meeting.dialogs.ask_everything}, "controls": {"reactions": list(self.meeting.controls.reactions)}`.

Test (in the config test module):

```python
def test_dialog_and_control_settings_round_trip_and_default():
    config = Config()
    assert "is being transcribed" in config.meeting.dialogs.acknowledge and config.meeting.dialogs.ask_everything is False
    assert config.meeting.controls.reactions[0] == "👍"
    loaded = Config.from_dict({"meeting": {"dialogs": {"acknowledge": ["is being recorded"], "ask_everything": True}, "controls": {"reactions": ["👍"]}}})
    assert loaded.meeting.dialogs.acknowledge == ["is being recorded"] and loaded.meeting.dialogs.ask_everything is True
    assert Config.from_dict(loaded.to_dict()).meeting.controls.reactions == ["👍"]
```

- [ ] **Step 2: The model and the pure mapping in `providers/base.py`**

Add to `MeetingInfo` (after `error_message`): `controls: Dict[str, Any] = field(default_factory=dict)`, `people: Dict[str, Any] = field(default_factory=lambda: {"count": 0, "list": []})`, `chat: Dict[str, Any] = field(default_factory=lambda: {"unread": 0, "messages": []})`, `notice: str = ""`, `dialog: Optional[Dict[str, Any]] = None`, `recording: bool = False`, `locked: bool = False`. `to_dict()` emits them under the same names.

```python
HOST_ROLES = ("host", "co-host")


def controls_from(reading: Dict[str, Any], reactions: List[str]) -> Dict[str, Any]:
    """The table's control model (spec section 3) from the page's reading of the meeting."""
    buttons = reading.get("buttons") or {}
    me = reading.get("me") or {}
    role = reading.get("role") or "participant"
    hosting = role in HOST_ROLES
    waiting = [p for p in (reading.get("participants") or []) if p.get("waiting")]

    def button(name, **extra):
        state = buttons.get(name)
        return dict({"available": state is not None}, **(state or {}), **extra)

    return {
        "mute": {"available": True, "on": not bool(me.get("muted"))},
        "camera": button("camera"),
        "raise_hand": button("raise_hand"),
        "reactions": dict(button("reactions"), choices=list(reactions)),
        "captions": button("captions"),
        "layout": dict(button("layout"), choices=["speaker", "gallery"]),
        "hide_self": button("hide_self"),
        "pin": {"available": False},
        "leave": {"available": True},
        "host": {
            "role": role,
            "mute_all": {"available": hosting},
            "ask_unmute": {"available": hosting},
            "record": {"available": hosting, "on": bool(reading.get("recording"))},
            "lock": {"available": hosting, "on": bool(reading.get("locked"))},
            "waiting_room": {"available": hosting, "waiting": [{"id": p.get("id"), "name": p.get("name", "")} for p in waiting]},
            "mute_one": {"available": hosting},
            "remove": {"available": hosting},
            "end": {"available": role == "host"},
        },
    }
```

(`pin` stays unavailable in this version: the client's per-participant menu is not driven; the spec's table lists it and the inventory may add it later.)

`MeetingProvider` gains:

```python
    async def control(self, name: str, value: Any = None) -> Dict[str, Any]:
        """Carry out a control from the table (spec 2026-10-13, section 3); the base offers none."""
        raise MeetingControlError("Not available in this meeting")
```

Tests for `controls_from` (pure):

```python
def test_controls_from_a_participant_reading_hides_host_items_and_absent_buttons():
    reading = {"me": {"muted": True}, "role": "participant", "buttons": {"camera": {"on": False}, "captions": {"on": True}},
               "participants": [], "recording": False}
    c = controls_from(reading, ["👍"])
    assert c["mute"] == {"available": True, "on": False} and c["camera"] == {"available": True, "on": False}
    assert c["raise_hand"] == {"available": False} and c["captions"] == {"available": True, "on": True}
    assert c["reactions"] == {"available": False, "choices": ["👍"]}
    assert c["host"]["role"] == "participant" and c["host"]["record"] == {"available": False, "on": False} and c["host"]["end"]["available"] is False


def test_controls_from_a_host_reading_offers_the_host_items_and_the_waiting_list():
    reading = {"me": {"muted": False}, "role": "host", "buttons": {}, "recording": True, "locked": False,
               "participants": [{"id": 5, "name": "Guest", "waiting": True}, {"id": 6, "name": "Ann", "waiting": False}]}
    c = controls_from(reading, [])
    assert c["host"]["record"] == {"available": True, "on": True} and c["host"]["end"]["available"] is True
    assert c["host"]["waiting_room"] == {"available": True, "waiting": [{"id": 5, "name": "Guest"}]}
    assert controls_from(dict(reading, role="co-host"), [])["host"]["end"]["available"] is False
```

- [ ] **Step 3: The dialog policy as a small pure class in `providers/base.py`**

```python
class DialogPolicy:
    """Acknowledge one-button notices on the list; everything else waits for a tap (spec section 5)."""

    STATE_ONLY = ("has been muted", "has been unmuted")   # a notice about the room's own state: no disclosure line

    def __init__(self, acknowledge: List[str], ask_everything: bool = False):
        self.acknowledge = [p.lower() for p in acknowledge]
        self.ask_everything = ask_everything

    def decide(self, dialog: Dict[str, Any]) -> str:
        """'acknowledge' or 'ask'."""
        words = f"{dialog.get('title', '')} {dialog.get('text', '')}".lower()
        buttons = dialog.get("buttons") or []
        if self.ask_everything or len(buttons) != 1:
            return "ask"
        return "acknowledge" if any(p in words for p in self.acknowledge) else "ask"

    def disclosure(self, dialog: Dict[str, Any]) -> str:
        """The sentence the table keeps for an acknowledged notice; empty for a state-only one."""
        words = f"{dialog.get('title', '')} {dialog.get('text', '')}".lower()
        if any(p in words for p in self.STATE_ONLY):
            return ""
        text = (dialog.get("text") or dialog.get("title") or "").strip()
        first = text.split(". ")[0].rstrip(".")
        return first + "." if first else ""
```

Tests:

```python
def test_the_policy_acknowledges_only_one_button_notices_on_the_list():
    policy = DialogPolicy(["is being transcribed", "AI Companion"])
    assert policy.decide({"title": "", "text": "This meeting is being transcribed and Zoom AI is on.", "buttons": ["OK"]}) == "acknowledge"
    assert policy.decide({"title": "Recording consent", "text": "This meeting is being recorded.", "buttons": ["Got it", "Leave meeting"]}) == "ask"
    assert policy.decide({"title": "Breakout rooms", "text": "Join Room 1?", "buttons": ["Join"]}) == "ask"
    assert DialogPolicy(["is being transcribed"], ask_everything=True).decide({"text": "This meeting is being transcribed.", "buttons": ["OK"]}) == "ask"
    assert policy.disclosure({"text": "This meeting is being transcribed and Zoom AI is on. Anyone with access may share it.", "buttons": ["OK"]}) == "This meeting is being transcribed and Zoom AI is on."
    assert policy.disclosure({"text": "You have been muted by the host", "buttons": ["OK"]}) == ""
```

- [ ] **Step 4: Run the pure tests** (they need no browser): `.venv/bin/pytest -q -p no:cacheprovider -W error tests/unit/meeting/test_zoom_sdk_provider.py -k "controls_from or the_policy" tests/unit/core` — Expected: PASS after Steps 1 to 3 (write them first and see them fail on import).

- [ ] **Step 5: Failing provider tests (with the browser)**

```python
class TestControlsAndDialogs:
    async def test_the_status_model_fills_after_join_and_follows_events(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.role = 'host';")
        try:
            info = await provider.join_meeting(LINK)
            assert info.controls["host"]["role"] == "host" and info.controls["captions"] == {"available": True, "on": False}
            assert info.people["count"] == 3 and info.people["list"][2]["name"] == "Raj Patel"
            await provider._page.evaluate("window.ZoomMtg._setAttendee(16777218, {bRaiseHand: false})")
            for _ in range(40):
                if info.people["list"][2]["hand"] is False:
                    break
                await asyncio.sleep(0.05)
            assert info.people["list"][2]["hand"] is False
        finally:
            await shutdown(provider)

    async def test_events_before_connected_are_ignored_and_the_model_fills_after(self):
        provider = await provider_for(api=FakeApi(), stub=STUB_JS + "window.ZoomMtg._behaviour.delayMs = 400;")
        try:
            info = await provider.join_meeting(LINK)                       # dialog scans and syncs may fire during the wait
            assert provider.state == MeetingState.CONNECTED and info.controls["mute"]["available"] is True
        finally:
            await shutdown(provider)

    async def test_control_presses_and_reports_and_a_missing_button_says_so(self):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            state = await provider.control("raise_hand")
            assert state["raise_hand"]["on"] is True and info.controls["raise_hand"]["on"] is True
            state = await provider.control("layout", "gallery")
            assert state["layout"]["value"] == "gallery"
            await provider.control("reactions", "👏")
            assert ["reaction", "clap"] in await provider._page.evaluate("window.__zoomCalls")
            with pytest.raises(MeetingControlError) as failure:
                await provider.control("reactions", "🦄")
            assert "offers no" in str(failure.value)
        finally:
            await shutdown(provider)

    async def test_a_host_call_refused_by_zoom_answers_in_words(self):
        provider = await provider_for(api=FakeApi())
        try:
            await provider.join_meeting(LINK)
            with pytest.raises(MeetingControlError) as failure:
                await provider.control("host.mute_all")
            assert "Not the host" in str(failure.value) and provider.state == MeetingState.CONNECTED
            with pytest.raises(MeetingControlError):
                await provider.control("no_such_control")
        finally:
            await shutdown(provider)

    async def test_an_acknowledged_dialog_is_pressed_once_and_disclosed(self):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("window.ZoomMtg._showDialog('', 'This meeting is being transcribed and Zoom AI is on. Anyone with access to the transcript may view or share it with others.', ['OK'])")
            for _ in range(40):
                if info.notice:
                    break
                await asyncio.sleep(0.05)
            assert info.notice == "This meeting is being transcribed and Zoom AI is on."
            assert [c for c in await provider._page.evaluate("window.__zoomCalls") if c[0] == "dialog"] == [["dialog", "", "OK"]]
            assert info.dialog is None
            await provider._page.evaluate("window.ZoomMtg._showDialog('', 'This meeting is being recorded.', ['OK'])")
            for _ in range(40):
                if "recorded" in info.notice:
                    break
                await asyncio.sleep(0.05)
            assert info.notice == "This meeting is being transcribed and Zoom AI is on. This meeting is being recorded."
        finally:
            await shutdown(provider)

    async def test_a_dialog_with_a_choice_waits_on_the_table_and_a_tap_answers_it(self):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("window.ZoomMtg._showDialog('Recording consent', 'This meeting is being recorded.', ['Got it', 'Leave meeting'])")
            for _ in range(40):
                if info.dialog:
                    break
                await asyncio.sleep(0.05)
            assert info.dialog["buttons"] == ["Got it", "Leave meeting"] and info.dialog["title"] == "Recording consent"
            assert not [c for c in await provider._page.evaluate("window.__zoomCalls") if c[0] == "dialog"]
            await provider.control("dialog", {"id": info.dialog["id"], "button": "Got it"})
            for _ in range(40):
                if info.dialog is None:
                    break
                await asyncio.sleep(0.05)
            assert info.dialog is None
            assert ["dialog", "Recording consent", "Got it"] in await provider._page.evaluate("window.__zoomCalls")
        finally:
            await shutdown(provider)

    async def test_a_dialog_that_goes_away_clears(self):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            sid = await provider._page.evaluate("window.ZoomMtg._showDialog('Unmute?', 'The host would like you to unmute.', ['Unmute', 'Stay muted'])")
            for _ in range(40):
                if info.dialog:
                    break
                await asyncio.sleep(0.05)
            await provider._page.evaluate(f"window.ZoomMtg._closeDialog('{sid}')")
            for _ in range(40):
                if info.dialog is None:
                    break
                await asyncio.sleep(0.05)
            assert info.dialog is None
        finally:
            await shutdown(provider)

    async def test_ask_everything_makes_a_notice_wait(self):
        provider = await provider_for(api=FakeApi())
        provider.dialog_policy = DialogPolicy(DEFAULT_ACKNOWLEDGE, ask_everything=True)
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("window.ZoomMtg._showDialog('', 'This meeting is being transcribed.', ['OK'])")
            for _ in range(40):
                if info.dialog:
                    break
                await asyncio.sleep(0.05)
            assert info.dialog["buttons"] == ["OK"] and info.notice == ""
        finally:
            await shutdown(provider)

    async def test_chat_is_kept_with_an_unread_count_the_table_can_clear(self):
        provider = await provider_for(api=FakeApi())
        try:
            info = await provider.join_meeting(LINK)
            await provider._page.evaluate("window.ZoomMtg._chat('Ann Lee', 'Can you hear us?')")
            for _ in range(40):
                if info.chat["messages"]:
                    break
                await asyncio.sleep(0.05)
            assert info.chat == {"unread": 1, "messages": [dict(info.chat["messages"][0], **{"from": "Ann Lee", "text": "Can you hear us?"})]}
            await provider.control("chat.seen")
            assert info.chat["unread"] == 0 and len(info.chat["messages"]) == 1
            for i in range(25):
                await provider._page.evaluate(f"window.ZoomMtg._chat('Raj', 'm{i}')")
            for _ in range(60):
                if len(info.chat["messages"]) == 20 and info.chat["messages"][-1]["text"] == "m24":
                    break
                await asyncio.sleep(0.05)
            assert len(info.chat["messages"]) == 20 and info.chat["unread"] == 25
        finally:
            await shutdown(provider)
```

Imports at the top of the test module: `from croom.meeting.providers.base import DialogPolicy, MeetingControlError, MeetingState, controls_from` and `from croom.core.config import DEFAULT_ACKNOWLEDGE`.

- [ ] **Step 6: Run to see them fail** — `-k "TestControlsAndDialogs"` — Expected: FAIL (no `control`, no `controls` on the info).

- [ ] **Step 7: Implement in `zoom_sdk.py`**

Constructor: `self.dialog_policy = DialogPolicy(DEFAULT_ACKNOWLEDGE)`, `self.reactions = list(DEFAULT_REACTIONS)`, `self._reading: Dict[str, Any] = {}`. `from_config` sets `provider.dialog_policy = DialogPolicy(config.meeting.dialogs.acknowledge, config.meeting.dialogs.ask_everything)` and `provider.reactions = list(config.meeting.controls.reactions)`.

`_take_state(reading)` becomes the model update:

```python
    def _take_state(self, reading: Any) -> None:
        if not isinstance(reading, dict):
            return
        me = reading.get("me") if isinstance(reading.get("me"), dict) else reading   # readState() shape or readMeeting() shape
        if isinstance(me.get("muted"), bool):
            self._muted = me["muted"]
        if isinstance(me.get("videoOn"), bool):
            self._camera_on = me["videoOn"]
        if "participants" in reading:
            self._reading = reading
        if self._current_meeting is not None:
            info = self._current_meeting
            info.is_muted, info.is_camera_on = self._muted, self._camera_on
            if self._reading:
                info.controls = controls_from(self._reading, self.reactions)
                people = self._reading.get("participants") or []
                info.people = {"count": int(self._reading.get("count") or len(people)), "list": people[:50]}
                info.recording = bool(self._reading.get("recording"))
                info.locked = bool(self._reading.get("locked"))
```

`_read_state_from_zoom()` evaluates `window.crystalMeet.readMeeting()` (falls back to `readState()` when `readMeeting` is undefined, for the Task 1 transition: `"() => (window.crystalMeet.readMeeting || window.crystalMeet.readState)()"`).

`_watch_meeting()` handles the new events:

```python
            if state == "state":
                self._take_json(detail, self._take_state)
                continue
            if state == "dialog":
                self._take_json(detail, self._on_dialog)
                continue
            if state == "dialog-closed":
                self._take_json(detail, self._on_dialog_closed)
                continue
            if state == "chat":
                self._take_json(detail, self._on_chat)
                continue
```

with

```python
    def _take_json(self, detail: str, handler) -> None:
        try:
            handler(json.loads(detail))
        except ValueError:
            logger.debug(f"Unreadable event from the page: {detail!r}")

    def _on_dialog(self, dialog: Dict[str, Any]) -> None:
        info = self._current_meeting
        logger.info(f"Zoom dialog: {dialog.get('title')!r} {dialog.get('text')!r} buttons {dialog.get('buttons')}")
        if self.dialog_policy.decide(dialog) == "acknowledge":
            label = dialog["buttons"][0]
            asyncio.ensure_future(self._answer_dialog(dialog["id"], label, acknowledged=True))
            sentence = self.dialog_policy.disclosure(dialog)
            if info is not None and sentence and sentence not in info.notice:
                info.notice = (info.notice + " " + sentence).strip()
            return
        if info is not None:
            info.dialog = {"id": dialog["id"], "title": dialog.get("title", ""), "text": dialog.get("text", ""), "buttons": list(dialog.get("buttons") or [])}
            logger.info("The dialog waits on the table")

    def _on_dialog_closed(self, data: Dict[str, Any]) -> None:
        info = self._current_meeting
        if info is not None and info.dialog and info.dialog.get("id") == data.get("id"):
            info.dialog = None
        asyncio.ensure_future(self._read_state_from_zoom())   # the host may have muted the room with that dialog

    async def _answer_dialog(self, dialog_id: int, label: str, acknowledged: bool = False) -> None:
        try:
            await self._page.evaluate("([id, label]) => window.crystalMeet.answerDialog(id, label)", [dialog_id, label])
            logger.info(f"{'Acknowledged' if acknowledged else 'Answered'} Zoom's dialog with {label!r}")
        except Exception as e:  # noqa: BLE001 - it may have gone on its own
            logger.info(f"Could not press {label!r} on Zoom's dialog: {str(e).splitlines()[0]}")

    def _on_chat(self, message: Dict[str, Any]) -> None:
        info = self._current_meeting
        if info is None:
            return
        info.chat["messages"] = (info.chat["messages"] + [{"from": str(message.get("from", "")), "text": str(message.get("text", "")), "at": str(message.get("at", ""))}])[-20:]
        info.chat["unread"] += 1
```

Acknowledged dialogs are pressed once because the page reports each dialog element once (the observer keys by element); a second report of the same text is a new element and a new dialog.

`control()`:

```python
    async def control(self, name: str, value: Any = None) -> Dict[str, Any]:
        self._require_meeting()
        info = self._current_meeting
        if name == "chat.seen":
            info.chat["unread"] = 0
            return {"chat": info.chat}
        if name == "dialog":
            if not isinstance(value, dict) or "id" not in value or "button" not in value:
                raise MeetingControlError("Say which dialog and which button")
            await self._answer_dialog(int(value["id"]), str(value["button"]))
            if info.dialog and info.dialog.get("id") == int(value["id"]):
                info.dialog = None
            return {"dialog": None}
        if name == "mute":
            await self.toggle_mute()
        elif name == "leave":
            await self.leave_meeting()
            return {}
        else:
            try:
                reading = await self._page.evaluate("([name, value]) => window.crystalMeet.control(name, value)", [name, value])
            except Exception as e:  # noqa: BLE001
                text = str(e).splitlines()[0] if str(e) else type(e).__name__
                raise MeetingControlError(text.replace("Error: ", "", 1)) from e
            self._take_state(reading)
            return info.controls
        await self._read_state_from_zoom()
        return info.controls
```

`MeetingService.control(name, value)` → `self._active_provider.control(name, value)` (raise `MeetingControlError("Not in a meeting")` without one). `MeetingService` passes `config` to `ZoomSdkProvider.from_config` already; make `from_config` read the two new config blocks.

- [ ] **Step 8: Run the provider tests** — `.venv/bin/pytest -q -p no:cacheprovider -W error tests/unit/meeting/test_zoom_sdk_provider.py tests/unit/meeting/test_zoom_sdk_page.py tests/unit/core` — Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/croom/core/config.py src/croom/meeting/providers/base.py src/croom/meeting/providers/zoom_sdk.py src/croom/meeting/service.py tests/unit/meeting/test_zoom_sdk_provider.py tests/unit/core
git commit -m "feat(meeting): the provider keeps the meeting as a model for the table, carries out controls, and answers Zoom's dialogs by policy"
```

---

### Task 6: The control service routes and the status shape

**Files:**
- Modify: `src/croom/control/service.py`
- Modify: `installer/install.sh` (the stock config's `meeting:` block gains `dialogs:` and `controls:`)
- Test: `tests/unit/control/test_service.py`

**Interfaces:**
- Produces: `/api/status` → `meeting.controls`, `meeting.participants` (`{"count", "list"}`), `meeting.chat`, `meeting.notice`, `meeting.dialog`; `POST /api/meeting/control {"name", "value"?}` → 200 `{"controls": ...}`; `POST /api/meeting/dialog {"id", "button"}` → 200 `{"dialog": null}`; `POST /api/meeting/chat/seen` → 200 `{"chat": ...}`. 400 for a missing name or a bad body, 409 with words from `MeetingControlError` or any failure, 409 "Not in a meeting", 503 without the meeting service.

- [ ] **Step 1: Failing tests** (`StubMeeting` gains `controls`, `people`, `chat`, `notice`, `dialog` on its `MeetingInfo` at join, and `async def control(self, name, value=None)` that records `(name, value)` in `self.controls_called`, raises `self.fail_control` when set, and returns `self.current_meeting.controls`):

```python
    async def test_the_status_carries_the_control_model(self, client_factory):
        meeting = StubMeeting()
        client = await client_factory(make_service(meeting=meeting))
        await client.post("/api/meeting/join", json={"url": "https://zoom.us/j/98765432100"})
        await wait_until(lambda: meeting.state == MeetingState.CONNECTED)
        meeting.current_meeting.controls = {"mute": {"available": True, "on": True}, "host": {"role": "host"}}
        meeting.current_meeting.people = {"count": 2, "list": [{"id": 1, "name": "Ann"}]}
        meeting.current_meeting.chat = {"unread": 1, "messages": [{"from": "Ann", "text": "hi", "at": "2026-10-13T20:00:00Z"}]}
        meeting.current_meeting.notice = "This meeting is being recorded."
        meeting.current_meeting.dialog = {"id": 3, "title": "Unmute?", "text": "The host would like you to unmute.", "buttons": ["Unmute", "Stay muted"]}
        data = (await (await client.get("/api/status")).json())["meeting"]
        assert data["controls"]["host"]["role"] == "host" and data["participants"]["count"] == 2
        assert data["chat"]["unread"] == 1 and data["notice"].startswith("This meeting") and data["dialog"]["buttons"] == ["Unmute", "Stay muted"]

    async def test_a_control_is_carried_out_and_its_failure_answers_in_words(self, client_factory):
        meeting = StubMeeting()
        client = await client_factory(make_service(meeting=meeting))
        assert (await client.post("/api/meeting/control", json={"name": "raise_hand"})).status == 409   # not in a meeting
        await client.post("/api/meeting/join", json={"url": "https://zoom.us/j/98765432100"})
        await wait_until(lambda: meeting.state == MeetingState.CONNECTED)
        resp = await client.post("/api/meeting/control", json={"name": "layout", "value": "gallery"})
        assert resp.status == 200 and meeting.controls_called == [("layout", "gallery")]
        assert (await client.post("/api/meeting/control", json={})).status == 400
        meeting.fail_control = MeetingControlError("Zoom's raise hand button is not on the screen yet; try again in a moment")
        resp = await client.post("/api/meeting/control", json={"name": "raise_hand"})
        assert resp.status == 409 and "raise hand" in (await resp.json())["error"]
        meeting.fail_control = RuntimeError("page closed")
        assert (await client.post("/api/meeting/control", json={"name": "raise_hand"})).status == 409

    async def test_dialog_and_chat_seen_routes(self, client_factory):
        meeting = StubMeeting()
        client = await client_factory(make_service(meeting=meeting))
        await client.post("/api/meeting/join", json={"url": "https://zoom.us/j/98765432100"})
        await wait_until(lambda: meeting.state == MeetingState.CONNECTED)
        resp = await client.post("/api/meeting/dialog", json={"id": 3, "button": "Got it"})
        assert resp.status == 200 and meeting.controls_called == [("dialog", {"id": 3, "button": "Got it"})]
        assert (await client.post("/api/meeting/dialog", json={"id": 3})).status == 400
        resp = await client.post("/api/meeting/chat/seen", json={})
        assert resp.status == 200 and meeting.controls_called[-1] == ("chat.seen", None)
```

- [ ] **Step 2: Run to see them fail** — Expected: FAIL (404s, missing keys).

- [ ] **Step 3: Implement**

Routes:

```python
        app.router.add_post("/api/meeting/control", self._handle_control)
        app.router.add_post("/api/meeting/dialog", self._handle_dialog)
        app.router.add_post("/api/meeting/chat/seen", self._handle_chat_seen)
```

In `_status()`'s meeting block add:

```python
                "controls": dict(current.controls) if current is not None else {},
                "participants": dict(current.people) if current is not None else {"count": 0, "list": []},
                "chat": dict(current.chat) if current is not None else {"unread": 0, "messages": []},
                "notice": current.notice if current is not None else "",
                "dialog": dict(current.dialog) if current is not None and current.dialog else None,
```

Handlers:

```python
    async def _run_control(self, name: str, value: Any) -> web.Response:
        if self._meeting is None:
            return self._error_response("Meeting service not available", 503)
        if self._meeting_state() != "connected":
            return self._error_response("Not in a meeting", 409)
        try:
            result = await self._meeting.control(name, value)
        except MeetingControlError as e:
            return self._error_response(str(e), 409)
        except Exception as e:  # noqa: BLE001 - a fault in the browser answers with its words, never a 500
            logger.warning(f"Could not carry out {name}: {e}")
            return self._error_response(f"Could not carry out {name.replace('_', ' ')}: {e}", 409)
        return web.json_response({"controls": result} if name not in ("dialog", "chat.seen") else result)

    async def _handle_control(self, request: web.Request) -> web.Response:
        rejection = self._require_json(request)
        if rejection is not None:
            return rejection
        data = await self._read_object(request)
        if not data or not isinstance(data.get("name"), str) or not data["name"]:
            return self._error_response("Send a JSON object with name and, for some controls, value", 400)
        return await self._run_control(data["name"], data.get("value"))

    async def _handle_dialog(self, request: web.Request) -> web.Response:
        rejection = self._require_json(request)
        if rejection is not None:
            return rejection
        data = await self._read_object(request)
        if not data or "id" not in data or not isinstance(data.get("button"), str):
            return self._error_response("Send a JSON object with the dialog's id and the button's label", 400)
        return await self._run_control("dialog", {"id": data["id"], "button": data["button"]})

    async def _handle_chat_seen(self, request: web.Request) -> web.Response:
        rejection = self._require_json(request)
        if rejection is not None:
            return rejection
        return await self._run_control("chat.seen", None)
```

Installer stock config, under `meeting:` after `mic_default_on: true`:

```yaml
  # Dialogs Zoom draws over the TV: a one-button notice whose words match a phrase here is acknowledged at once and
  # disclosed on the table; every other dialog waits for a tap on the table. ask_everything: true makes them all wait.
  dialogs:
    acknowledge:
      - "is being recorded"
      - "is being transcribed"
      - "AI Companion"
      - "Zoom AI is on"
      - "has been muted"
      - "has been unmuted"
      - "recording has started"
      - "recording has stopped"
    ask_everything: false
  controls:
    reactions: ["👍", "👏", "❤️", "😂", "😮", "🎉"]
```

(`tests/unit/installer/` may parse the stock config; run that folder too.)

- [ ] **Step 4: Run** — `.venv/bin/pytest -q -p no:cacheprovider -W error tests/unit/control/test_service.py tests/unit/installer` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/croom/control/service.py installer/install.sh tests/unit/control/test_service.py
git commit -m "feat(control): the status carries the meeting's control model, and the table's controls, dialog answers and chat go through three routes"
```

---

### Task 7: Icons on every button

**Files:**
- Create: `src/croom/control/static/icons.svg`, `src/croom/control/static/LICENSES.md`
- Modify: `src/croom/control/static/app.js` (`button()` takes an icon; the sprite is loaded once), `index.html` (icons on the static buttons), `style.css`
- Test: `tests/unit/control/test_page.py`

**Interfaces:**
- Produces: `button(label, className, onClick, disabled, icon)`; a spec may carry `icon: "mic"`; `window.croomPage.iconsReady` (a promise); icon ids `icon-mic`, `icon-mic-off`, `icon-video`, `icon-video-off`, `icon-hand`, `icon-smile`, `icon-captions`, `icon-layout`, `icon-user`, `icon-users`, `icon-message`, `icon-phone-off`, `icon-record`, `icon-lock`, `icon-unlock`, `icon-door`, `icon-x-circle`, `icon-volume`, `icon-move`, `icon-monitor`, `icon-settings`, `icon-calendar`, `icon-back`, `icon-check`.

- [ ] **Step 1: Failing test**

```python
def test_every_button_on_the_meeting_screen_has_an_icon_and_the_label_stays_its_name(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-actions .button svg use')", timeout=5000)
        for b in page.locator("#meeting-actions .button").all():
            assert b.locator("svg.icon use").count() == 1 and b.locator("svg").get_attribute("aria-hidden") == "true"
            assert b.inner_text().strip() in ("Mute", "Unmute", "Turn camera off", "Turn camera on", "Leave")
        assert page.locator("#open-options svg use").count() == 0 or page.locator("#open-options").is_hidden()
        page.close()


def test_the_idle_buttons_carry_icons_too(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.wait_for_function("document.querySelector('#open-options svg use')", timeout=5000)
        assert page.locator("#open-options").inner_text().strip() == "Options"
        assert page.locator("#link-form button[type=submit] svg use").count() == 1
        page.close()
```

- [ ] **Step 2: Run to see them fail** — `-k "has_an_icon or carry_icons"` — Expected: FAIL.

- [ ] **Step 3: The sprite**

`icons.svg` holds `<svg xmlns="http://www.w3.org/2000/svg"><symbol id="icon-mic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">…</symbol>…</svg>` with Lucide's paths for: `mic`, `mic-off`, `video`, `video-off`, `hand`, `smile`, `captions`, `layout-grid`, `user`, `users`, `message-square`, `phone-off`, `circle` (record), `lock`, `lock-open`, `log-in` (door), `x-circle`, `volume-2`, `move`, `monitor`, `settings`, `calendar`, `arrow-left` (back), `check`. Copy each path from the Lucide repository (ISC) and name the symbols as listed under Interfaces. `LICENSES.md`: "Icons in icons.svg are from Lucide (https://lucide.dev), ISC License, Copyright (c) for portions of Lucide are held by Cole Bemis 2013-2022 as part of Feather (MIT), and Lucide Contributors 2022."

- [ ] **Step 4: Load the sprite once and draw icons**

In `app.js` near the top:

```js
  // Icons: one sprite of Lucide symbols, fetched once and kept in the document, so <use href="#icon-…"> resolves
  // without a request per button. A page without the sprite keeps its labels (the fetch may fail offline).
  const iconsReady = fetch("/static/icons.svg").then((r) => (r.ok ? r.text() : "")).then((svg) => {
    if (!svg) return false;
    const holder = document.createElement("div");
    holder.hidden = true;
    holder.innerHTML = svg;
    document.body.prepend(holder);
    return true;
  }).catch(() => false);
  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#icon-" + name);
    svg.append(use);
    return svg;
  }
  function labelWithIcon(node, label, name) {
    node.replaceChildren();
    if (name) node.append(icon(name));
    const text = document.createElement("span");
    text.textContent = label;
    node.append(text);
  }
```

`button(label, className, onClick, disabled, iconName)` calls `labelWithIcon(b, label, iconName)` instead of `b.textContent = label`; `setActions` passes `s.icon` and includes it in the key. The three meeting specs get icons: mute `m.muted ? "mic-off" : "mic"`, camera `m.camera_on ? "video-off" : "video"`, leave `"phone-off"`; the idle Join buttons `"check"`? (no: Join stays an arrow) use `"video"` for "Join now"; Dismiss `"x-circle"`; Cancel `"x-circle"`. Static buttons in `index.html` get their icons once at load: `labelWithIcon(el("open-options"), "Options", "settings")`, `open-camera-setup` → `move`, `open-screensaver` → `monitor`, `open-camera-live` → `move`, `open-upcoming` → `calendar`, every `.back` → `back`, `#link-form button[type=submit]` → `video` (its text is "Join"), `quieter`/`louder` → `volume`, `speaker-mute` → `volume`. Export `iconsReady` and `icon`, `labelWithIcon` on `window.croomPage` for devices.js.

CSS:

```css
.button { display: inline-flex; align-items: center; justify-content: center; gap: 10px; }
.button .icon { width: 1.3em; height: 1.3em; flex: 0 0 auto; }
.button.menu-item .icon { width: 1.6em; height: 1.6em; }
```

- [ ] **Step 5: Run the page tests** — `.venv/bin/pytest -q -p no:cacheprovider -W error tests/unit/control/test_page.py -k "icon or fits or kiosk_bar or mute_and_leave"` then the whole file — Expected: PASS (`fits()` on the kiosk must still hold: icons add width, not height; check `test_a_long_next_booking_title…` and the kiosk layout tests).

- [ ] **Step 6: Commit**

```bash
git add src/croom/control/static/icons.svg src/croom/control/static/LICENSES.md src/croom/control/static/app.js src/croom/control/static/index.html src/croom/control/static/style.css tests/unit/control/test_page.py
git commit -m "feat(control): an icon before every button's label on the room page, from one Lucide sprite"
```

---

### Task 8: The meeting screen drawn from the control model

**Files:**
- Modify: `src/croom/control/static/app.js`, `index.html`, `style.css`
- Modify: `tests/unit/control/test_service.py` (`StubMeeting` default `controls` for a Zoom join: the full participant model with everything available; `host.role` settable through `StubMeeting(role="host")`)
- Test: `tests/unit/control/test_page.py`

**Interfaces:**
- Consumes: `meeting.controls` from the status.
- Produces: `#meeting-actions` (primary row: Mute, Camera, Raise hand, Reactions, Leave), `#meeting-secondary` (Captions, View, Self view, Participants (count), Chat (unread), Host), reaction picker `#reaction-row` (replaces the secondary row while open, 10 s), `control(name, value)` → `act(() => post("/api/meeting/control", {name, value}))`.

- [ ] **Step 1: Failing tests**

```python
def test_the_meeting_screen_offers_every_available_control_from_the_model(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        primary = [b.inner_text().strip() for b in page.locator("#meeting-actions .button").all()]
        assert primary == ["Mute", "Turn camera off", "Raise hand", "Reactions", "Leave"]
        secondary = [b.inner_text().strip() for b in page.locator("#meeting-secondary .button").all()]
        assert secondary == ["Captions on", "Gallery view", "Hide self view", "Participants (3)", "Chat"]   # a participant: no Host
        page.locator("#meeting-secondary .button", has_text="Captions on").click()
        page.wait_for_function("document.querySelector('#meeting-secondary .button').textContent.includes('Captions off')", timeout=5000)
        assert server.meeting.controls_called == [("captions", None)]
        page.locator("#meeting-actions .button", has_text="Reactions").click()
        page.wait_for_function("document.querySelector('#reaction-row')", timeout=5000)
        page.locator("#reaction-row .button", has_text="👏").click()
        page.wait_for_function("!document.querySelector('#reaction-row')", timeout=5000)
        assert server.meeting.controls_called[-1] == ("reactions", "👏")
        assert fits(page)
        page.close()


def test_the_meeting_screen_with_everything_still_fits_and_hides_the_unavailable(browser):
    with PageServer(role="host") as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-secondary .button')", timeout=5000)
        server.meeting.current_meeting.notice = "This meeting is being recorded and transcribed."
        server.meeting.current_meeting.dialog = {"id": 1, "title": "Unmute?", "text": "The host would like you to unmute.", "buttons": ["Unmute", "Stay muted"]}
        server.meeting.current_meeting.chat = {"unread": 3, "messages": []}
        server.meeting.current_meeting.controls["raise_hand"] = {"available": False}
        page.wait_for_function("document.querySelector('#meeting-dialog:not([hidden])')", timeout=5000)
        assert [b.inner_text().strip() for b in page.locator("#meeting-actions .button").all()] == ["Mute", "Turn camera off", "Reactions", "Leave"]
        assert "Host" in [b.inner_text().strip() for b in page.locator("#meeting-secondary .button").all()]
        assert page.locator("#meeting-secondary .button", has_text="Chat (3)").count() == 1
        assert page.locator("#meeting-notice").inner_text() == "This meeting is being recorded and transcribed."
        assert fits(page)
        page.close()


def test_a_status_without_controls_keeps_the_three_buttons(browser):
    with PageServer(controls=False) as server:
        page = open_page(browser, server)
        join_by_id(page)
        assert [b.inner_text().strip() for b in page.locator("#meeting-actions .button").all()] == ["Mute", "Turn camera off", "Leave"]
        assert page.locator("#meeting-secondary .button").count() == 0
        page.close()


def test_a_controls_words_show_on_the_meeting_screen(browser):
    from croom.meeting.providers.base import MeetingControlError
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.wait_for_function("document.querySelector('#meeting-actions .button')", timeout=5000)
        server.meeting.fail_control = MeetingControlError("Zoom's raise hand button is not on the screen yet; try again in a moment")
        page.locator("#meeting-actions .button", has_text="Raise hand").click()
        page.wait_for_function("document.getElementById('meeting-message').textContent.includes('raise hand')", timeout=5000)
        page.close()
```

`PageServer` gains `role="participant"` and `controls=True` arguments passed to `StubMeeting(role=…, controls=…)`; `StubMeeting` builds `MeetingInfo.controls` at join as the full model (`controls_from({"me": {"muted": False}, "role": role, "buttons": {"camera": {"on": True}, "raise_hand": {"on": False}, "reactions": {}, "captions": {"on": False}, "layout": {"value": "speaker"}, "hide_self": {"on": False}}, "participants": [...3 entries...], "recording": False}, DEFAULT_REACTIONS)` when `controls` else `{}`), `people` with 3 entries, and `control()` flips the obvious states (`captions` on/off, `layout`, `raise_hand`, `hide_self`) so the page can see a change.

- [ ] **Step 2: Run to see them fail** — Expected: FAIL.

- [ ] **Step 3: Implement**

`index.html` meeting view, after `#meeting-message`:

```html
    <p id="meeting-notice" class="notice"></p>
    <section id="meeting-dialog" class="dialog-card" hidden>
      <p id="dialog-title" class="kicker"></p>
      <p id="dialog-text" class="detail"></p>
      <div id="dialog-actions" class="actions"></div>
    </section>
    <div id="meeting-actions" class="actions meeting-actions"></div>
    <div id="meeting-secondary" class="actions meeting-secondary"></div>
```

(`.view[data-view="meeting"]` gets one more `auto` row; `.notice { margin: 8px 0 0; color: var(--periwinkle-300); max-width: 60ch; } .notice:empty { display: none; } .dialog-card { padding: 14px 18px; border-radius: var(--radius); background: var(--warn-fill); color: var(--ink-900); } .dialog-card .detail { color: inherit; } .meeting-secondary .button { min-height: 52px; }`.)

`app.js`: `const control = (name, value) => act(() => post("/api/meeting/control", { name: name, value: value }));` and in `render()`'s connected branch:

```js
      const c = m.controls || {};
      const has = (name) => c[name] && c[name].available;
      specs = [
        { label: m.muted ? "Unmute" : "Mute", icon: m.muted ? "mic-off" : "mic", className: "", onClick: () => toggle("mute") },
        { label: m.camera_on ? "Turn camera off" : "Turn camera on", icon: m.camera_on ? "video-off" : "video", className: "", onClick: () => toggle("camera") },
      ];
      if (has("raise_hand")) specs.push({ label: c.raise_hand.on ? "Lower hand" : "Raise hand", icon: "hand", className: "", onClick: () => control("raise_hand") });
      if (has("reactions")) specs.push({ label: "Reactions", icon: "smile", className: "", onClick: openReactions });
      specs.push(leaveSpec());
      secondary = [];
      if (has("captions")) secondary.push({ label: c.captions.on ? "Captions off" : "Captions on", icon: "captions", className: "quiet", onClick: () => control("captions") });
      if (has("layout")) secondary.push({ label: c.layout.value === "gallery" ? "Speaker view" : "Gallery view", icon: "layout", className: "quiet", onClick: () => control("layout", c.layout.value === "gallery" ? "speaker" : "gallery") });
      if (has("hide_self")) secondary.push({ label: c.hide_self.on ? "Show self view" : "Hide self view", icon: "user", className: "quiet", onClick: () => control("hide_self") });
      if (m.participants && m.participants.count) secondary.push({ label: "Participants (" + m.participants.count + ")", icon: "users", className: "quiet", onClick: () => setView("participants") });
      if (m.chat) secondary.push({ label: "Chat" + (m.chat.unread ? " (" + m.chat.unread + ")" : ""), icon: "message", className: "quiet", onClick: () => setView("chat") });
      if (c.host && c.host.role !== "participant") secondary.push({ label: "Host", icon: "lock", className: "quiet", onClick: () => setView("host") });
```

Draw `secondary` with `setActions(secondary, "meeting-secondary", secondaryKey)` in meeting mode (`[]` otherwise). `openReactions()` sets `model.reactionsUntil = Date.now() + 10000` and renders `#reaction-row` (a div inserted before `#meeting-secondary`, with one button per `c.reactions.choices` and a Cancel) while `Date.now() < model.reactionsUntil`; a pick calls `control("reactions", choice)` and clears it; `tick()` re-renders when it lapses. The notice: `el("meeting-notice").textContent = m.notice || ""`. The dialog card (Task 9 fills it; here it stays hidden: `el("meeting-dialog").hidden = !m.dialog`).

Views `participants`, `chat`, `host` are added to `VIEW_MODES` as `"meeting"` now with empty sections and Back buttons, so `setView` works; their contents come in Task 10.

- [ ] **Step 4: Run** — `tests/unit/control/test_page.py` whole file — Expected: PASS (every earlier meeting test still passes: the three-button layout when `controls` is `{}`).

- [ ] **Step 5: Commit**

```bash
git add src/croom/control/static/app.js src/croom/control/static/index.html src/croom/control/static/style.css tests/unit/control/test_service.py tests/unit/control/test_page.py
git commit -m "feat(control): the meeting screen draws its controls from the agent's model: a primary row, a secondary row, reactions and the notice line"
```

---

### Task 9: The dialog card

**Files:**
- Modify: `src/croom/control/static/app.js`
- Test: `tests/unit/control/test_page.py`

- [ ] **Step 1: Failing test**

```python
def test_a_waiting_dialog_is_answered_from_the_table(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        server.meeting.current_meeting.dialog = {"id": 7, "title": "Recording consent", "text": "This meeting is being recorded.", "buttons": ["Got it", "Leave meeting"]}
        page.wait_for_function("document.querySelector('#meeting-dialog:not([hidden])')", timeout=5000)
        assert page.locator("#dialog-title").inner_text() == "Recording consent"
        buttons = [b.inner_text().strip() for b in page.locator("#dialog-actions .button").all()]
        assert buttons == ["Got it", "Leave meeting"]
        assert "primary" in page.locator("#dialog-actions .button").first.get_attribute("class")
        page.locator("#dialog-actions .button", has_text="Got it").click()
        page.wait_for_function("document.querySelector('#meeting-dialog').hidden", timeout=5000)
        assert server.meeting.controls_called == [("dialog", {"id": 7, "button": "Got it"})]
        page.close()
```

(`StubMeeting.control("dialog", …)` clears `current_meeting.dialog`.)

- [ ] **Step 2: Run to see it fail** — Expected: FAIL.

- [ ] **Step 3: Implement**

```js
  const answerDialog = (id, label) => act(() => post("/api/meeting/dialog", { id: id, button: label }));
  function renderDialog(m) {
    const d = m.dialog;
    el("meeting-dialog").hidden = !d;
    if (!d) { setActions([], "dialog-actions", dialogKey); return; }
    el("dialog-title").textContent = d.title || "Zoom asks";
    el("dialog-text").textContent = d.text || "";
    setActions(d.buttons.map((label, i) => ({ label: label, className: i === 0 ? "primary" : "quiet", onClick: () => answerDialog(d.id, label), target: String(d.id) })), "dialog-actions", dialogKey);
  }
```

Called from `render()` in meeting mode (and with `m.dialog` null otherwise).

- [ ] **Step 4: Run** — Expected: PASS. Confirm `fits(page)` in the "everything" test still holds (Task 8's test).

- [ ] **Step 5: Commit**

```bash
git add src/croom/control/static/app.js tests/unit/control/test_page.py
git commit -m "feat(control): a dialog Zoom leaves waiting is answered from the table with its own buttons"
```

---

### Task 10: Participants, Chat and Host sub-screens

**Files:**
- Modify: `src/croom/control/static/app.js`, `index.html`, `style.css`
- Test: `tests/unit/control/test_page.py`

**Interfaces:**
- Produces: views `participants` (`#participant-list` rows: name, marks for muted/video off/hand/host, and for a host Mute and Remove buttons; `#waiting-list` rows with Admit), `chat` (`#chat-list`, newest last; opening it posts `/api/meeting/chat/seen`), `host` (`#host-actions`: Mute all, Ask all to unmute, Start/Stop recording, Lock/Unlock, End meeting for everyone with a second tap); `MEETING_RETURN_MS = 2 * 60 * 1000` for `upcoming`, `participants`, `chat`, `host`.

- [ ] **Step 1: Failing tests**

```python
def test_the_participants_screen_lists_the_room_and_lets_a_host_act(browser):
    with PageServer(role="host") as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.locator("#meeting-secondary .button", has_text="Participants").click()
        page.wait_for_function("document.body.dataset.view === 'participants'", timeout=5000)
        rows = page.locator("#participant-list .participant")
        assert rows.count() == 3 and "Raj Patel" in rows.nth(2).inner_text()
        assert rows.nth(2).locator(".mark-hand").count() == 1 and rows.nth(2).locator(".mark-muted").count() == 1
        rows.nth(2).locator(".button", has_text="Mute").click()
        page.wait_for_function("window.croomPage.model.busy === false", timeout=5000)
        assert server.meeting.controls_called[-1] == ("host.mute_one", 16777218)
        server.meeting.current_meeting.controls["host"]["waiting_room"] = {"available": True, "waiting": [{"id": 9, "name": "Guest"}]}
        page.wait_for_function("document.querySelector('#waiting-list .participant')", timeout=5000)
        page.locator("#waiting-list .button", has_text="Admit").click()
        page.wait_for_function("window.croomPage.model.busy === false", timeout=5000)
        assert server.meeting.controls_called[-1] == ("host.admit", 9)
        page.locator("#back-from-participants").click()
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        page.close()


def test_a_participant_sees_no_host_buttons_on_the_participants_screen(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.locator("#meeting-secondary .button", has_text="Participants").click()
        page.wait_for_function("document.body.dataset.view === 'participants'", timeout=5000)
        assert page.locator("#participant-list .button").count() == 0
        page.close()


def test_the_chat_screen_shows_messages_and_clears_the_unread_count(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        join_by_id(page)
        server.meeting.current_meeting.chat = {"unread": 2, "messages": [{"from": "Ann", "text": "Can you hear us?", "at": "2026-10-13T20:00:00Z"}, {"from": "Raj", "text": "Yes", "at": "2026-10-13T20:00:30Z"}]}
        page.wait_for_function("document.querySelector('#meeting-secondary .button').parentElement.textContent.includes('Chat (2)')", timeout=5000)
        page.locator("#meeting-secondary .button", has_text="Chat").click()
        page.wait_for_function("document.body.dataset.view === 'chat'", timeout=5000)
        assert [m.inner_text() for m in page.locator("#chat-list .chat-message .chat-text").all()] == ["Can you hear us?", "Yes"]
        page.wait_for_function("window.croomPage.model.busy === false", timeout=5000)
        assert ("chat.seen", None) in server.meeting.controls_called
        page.close()


def test_the_host_screen_offers_the_host_actions_with_a_second_tap_to_end(browser):
    with PageServer(role="host") as server:
        page = open_page(browser, server)
        join_by_id(page)
        page.locator("#meeting-secondary .button", has_text="Host").click()
        page.wait_for_function("document.body.dataset.view === 'host'", timeout=5000)
        labels = [b.inner_text().strip() for b in page.locator("#host-actions .button").all()]
        assert labels == ["Mute all", "Ask all to unmute", "Start recording", "Lock meeting", "End meeting for everyone"]
        page.locator("#host-actions .button", has_text="Start recording").click()
        page.wait_for_function("document.querySelector('#host-actions').textContent.includes('Stop recording')", timeout=5000)
        page.locator("#host-actions .button", has_text="End meeting").click()
        assert page.locator("#host-actions .button", has_text="Tap again to end").count() == 1
        assert ("host.end", None) not in server.meeting.controls_called
        page.locator("#host-actions .button", has_text="Tap again to end").click()
        page.wait_for_function("window.croomPage.model.busy === false", timeout=5000)
        assert server.meeting.controls_called[-1] == ("host.end", None)
        page.close()


def test_meeting_sub_screens_return_on_their_own_after_two_idle_minutes(browser):
    with PageServer() as server:
        page = open_page(browser, server)
        page.clock.install()
        join_by_id(page)
        page.locator("#meeting-secondary .button", has_text="Participants").click()
        page.wait_for_function("document.body.dataset.view === 'participants'", timeout=5000)
        page.clock.run_for(2 * 60 * 1000 + 500)
        page.wait_for_function("document.body.dataset.view === 'meeting'", timeout=5000)
        page.close()
```

(`page.clock` is Playwright's clock API; the existing idle-return tests show the pattern to copy, under `page_on_a_stopped_clock` or similar helpers in this file: use the same helper.)

- [ ] **Step 2: Run to see them fail** — Expected: FAIL.

- [ ] **Step 3: Implement**

`index.html` adds three views:

```html
  <section class="view" data-view="participants">
    <button type="button" id="back-from-participants" class="button quiet back">Back</button>
    <section class="list-box">
      <p class="kicker">Participants</p>
      <ul id="participant-list" class="participants"></ul>
      <p id="waiting-kicker" class="kicker" hidden>Waiting room</p>
      <ul id="waiting-list" class="participants"></ul>
    </section>
  </section>
  <section class="view" data-view="chat">
    <button type="button" id="back-from-chat" class="button quiet back">Back</button>
    <section class="list-box">
      <p class="kicker">Chat</p>
      <ul id="chat-list" class="chat"></ul>
      <p id="chat-empty" class="note" hidden>No messages yet</p>
    </section>
  </section>
  <section class="view" data-view="host">
    <button type="button" id="back-from-host" class="button quiet back">Back</button>
    <section class="list-box">
      <p class="kicker">Host controls</p>
      <div id="host-actions" class="actions"></div>
    </section>
  </section>
```

CSS: `.list-box { width: 100%; max-height: 100%; overflow: auto; } .participants { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; } .participant { display: flex; align-items: center; gap: 12px; padding: 8px 12px; border-radius: var(--radius-sm); background: rgba(255,255,255,0.06); } .participant .name { flex: 1 1 auto; } .participant .mark { color: var(--periwinkle-300); font-size: 0.9em; } .chat { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; } .chat-message .chat-from { color: var(--periwinkle-300); font-size: 0.9em; margin: 0; } .chat-message .chat-text { margin: 2px 0 0; overflow-wrap: anywhere; }`; the three views share `.view[data-view="options"]…` grid rule (`auto 1fr`).

`app.js`: `VIEW_MODES` adds `participants: "meeting", chat: "meeting", host: "meeting"`; `armIdleReturn` adds `if (mode() === "meeting" && ["upcoming", "participants", "chat", "host"].includes(view())) idleReturnTimer = setTimeout(() => setView("meeting", true), MEETING_RETURN_MS);`. Renderers, keyed like `renderUpcoming`:

```js
  function participantRow(p, hosting, waiting) {
    const li = document.createElement("li");
    li.className = "participant";
    const name = document.createElement("span"); name.className = "name"; name.textContent = p.name + (p.me ? " (this room)" : "");
    li.append(name);
    const marks = [["host", "Host", "mark-host"], ["hand", "Hand raised", "mark-hand"], ["muted", "Muted", "mark-muted"]];
    for (const [key, text, cls] of marks) if (p[key]) { const s = document.createElement("span"); s.className = "mark " + cls; s.textContent = text; li.append(s); }
    if (p.video === false) { const s = document.createElement("span"); s.className = "mark mark-video-off"; s.textContent = "Video off"; li.append(s); }
    if (hosting && waiting) li.append(button("Admit", "primary", () => control("host.admit", p.id), false, "door"));
    else if (hosting && !p.me) {
      if (!p.muted) li.append(button("Mute", "quiet", () => control("host.mute_one", p.id), false, "mic-off"));
      li.append(button("Remove", "quiet", () => control("host.remove", p.id), false, "x-circle"));
    }
    return li;
  }
  function renderParticipants(m) {
    const c = m.controls || {}; const host = c.host || {}; const hosting = host.role && host.role !== "participant";
    const list = (m.participants && m.participants.list) || [];
    const waiting = (host.waiting_room && host.waiting_room.waiting) || [];
    const key = JSON.stringify([list, waiting, hosting, model.busy]);
    if (key === lastParticipantsKey) return;
    lastParticipantsKey = key;
    el("participant-list").replaceChildren(...list.map((p) => participantRow(p, hosting, false)));
    el("waiting-kicker").hidden = waiting.length === 0;
    el("waiting-list").replaceChildren(...waiting.map((p) => participantRow(p, hosting, true)));
  }
  function renderChat(m) {
    const messages = (m.chat && m.chat.messages) || [];
    const key = JSON.stringify(messages);
    if (key === lastChatKey) return;
    lastChatKey = key;
    el("chat-empty").hidden = messages.length > 0;
    el("chat-list").replaceChildren(...messages.map((msg) => {
      const li = document.createElement("li"); li.className = "chat-message";
      const from = document.createElement("p"); from.className = "chat-from"; from.textContent = msg.from + (msg.at ? ", " + fmtTime(new Date(msg.at)) : "");
      const text = document.createElement("p"); text.className = "chat-text"; text.textContent = msg.text;
      li.append(from, text); return li;
    }));
    el("chat-list").scrollTop = el("chat-list").scrollHeight;
  }
  function hostSpecs(m) {
    const h = (m.controls && m.controls.host) || {};
    const specs = [];
    if (h.mute_all && h.mute_all.available) specs.push({ label: "Mute all", icon: "mic-off", className: "", onClick: () => control("host.mute_all") });
    if (h.ask_unmute && h.ask_unmute.available) specs.push({ label: "Ask all to unmute", icon: "mic", className: "", onClick: () => control("host.ask_unmute") });
    if (h.record && h.record.available) specs.push({ label: h.record.on ? "Stop recording" : "Start recording", icon: "record", className: "", onClick: () => control("host.record") });
    if (h.lock && h.lock.available) specs.push({ label: h.lock.on ? "Unlock meeting" : "Lock meeting", icon: h.lock.on ? "unlock" : "lock", className: "", onClick: () => control("host.lock") });
    if (h.end && h.end.available) specs.push(model.confirmEnd
      ? { label: "Tap again to end", icon: "x-circle", className: "danger", onClick: () => { clearTimeout(confirmTimer); model.confirmEnd = false; control("host.end"); } }
      : { label: "End meeting for everyone", icon: "x-circle", className: "danger", onClick: () => { model.confirmEnd = true; clearTimeout(confirmTimer); confirmTimer = setTimeout(() => { model.confirmEnd = false; render(); }, CONFIRM_MS); render(); } });
    return specs;
  }
```

`render()` calls `renderParticipants(m)`, `renderChat(m)`, `setActions(hostSpecs(m), "host-actions", hostKey)` in meeting mode. A view hook: when the view becomes `chat` and `m.chat.unread > 0`, `post("/api/meeting/chat/seen")` through `act`. Back buttons wire to `setView("meeting")`. `landOnMode()` resets `model.confirmEnd`.

- [ ] **Step 4: Run** — the whole `tests/unit/control/test_page.py` — Expected: PASS, `fits()` on every screen (add `assert fits(page)` on each new screen in the tests above).

- [ ] **Step 5: Commit**

```bash
git add src/croom/control/static/app.js src/croom/control/static/index.html src/croom/control/static/style.css tests/unit/control/test_page.py
git commit -m "feat(control): Participants, Chat and Host screens on the table, each returning to the meeting screen after two idle minutes"
```

---

### Task 11: Google Meet answers the same controls where its page allows

**Files:**
- Modify: `src/croom/meeting/providers/google_meet.py`
- Test: `tests/unit/meeting/test_meet_controls.py` (new, with a local replica page in the style of `test_meet_prejoin.py`)

**Interfaces:**
- Produces: `GoogleMeetProvider.control()` for `camera` (Ctrl+E), `raise_hand` (Ctrl+Alt+H), `captions` (the `c` key); `mute` through `toggle_mute`; `MeetingInfo.controls` built from Meet's buttons (`[aria-label*="microphone" i]`, `[aria-label*="camera" i]`, `[aria-label*="raise hand" i]`/`[aria-label*="lower hand" i]`, `[aria-label*="captions" i]`), state from the label ("Turn off …" → on, "Lower hand" → raised); `host.role` `participant` and every host item unavailable; `people.count` from `[aria-label*="participants" i]` when present (a number in the label or the text), otherwise 0; `chat` unavailable (`{"unread": 0, "messages": []}`).

- [ ] **Step 1: Failing tests** with a replica page:

```python
MEET_IN_CALL = """
<!DOCTYPE html><html><body>
<div id="meeting">
  <button aria-label="Turn off microphone (ctrl + d)">mic</button>
  <button aria-label="Turn on camera (ctrl + e)">cam</button>
  <button aria-label="Raise hand (ctrl + alt + h)">hand</button>
  <button aria-label="Turn on captions (c)">cc</button>
  <button aria-label="Show everyone (3)">people</button>
  <button aria-label="Leave call">Leave</button>
</div>
<script>
const flip = (b, a, c) => { b.setAttribute('aria-label', b.getAttribute('aria-label').startsWith(a) ? c : a); };
const by = (s) => document.querySelector(s);
document.addEventListener('keydown', (e) => {
  if (e.ctrlKey && e.key === 'd') flip(by('[aria-label*="microphone"]'), 'Turn off microphone (ctrl + d)', 'Turn on microphone (ctrl + d)');
  if (e.ctrlKey && e.key === 'e') flip(by('[aria-label*="camera"]'), 'Turn off camera (ctrl + e)', 'Turn on camera (ctrl + e)');
  if (e.ctrlKey && e.altKey && e.key === 'h') flip(by('[aria-label*="hand"]'), 'Raise hand (ctrl + alt + h)', 'Lower hand (ctrl + alt + h)');
  if (!e.ctrlKey && e.key === 'c') flip(by('[aria-label*="captions"]'), 'Turn on captions (c)', 'Turn off captions (c)');
});
</script></body></html>
"""


async def test_meet_reports_its_buttons_as_controls_and_presses_them_with_shortcuts(meet_page):
    provider, info = meet_page(MEET_IN_CALL)                   # a fixture that serves the replica, joins, returns the provider in CONNECTED
    assert info.controls["camera"] == {"available": True, "on": False} and info.controls["captions"] == {"available": True, "on": False}
    assert info.controls["raise_hand"] == {"available": True, "on": False} and info.controls["host"]["role"] == "participant"
    assert info.controls["reactions"]["available"] is False and info.people["count"] == 3
    state = await provider.control("camera")
    assert state["camera"]["on"] is True and info.is_camera_on is True
    state = await provider.control("raise_hand")
    assert state["raise_hand"]["on"] is True
    state = await provider.control("captions")
    assert state["captions"]["on"] is True
    with pytest.raises(MeetingControlError):
        await provider.control("host.mute_all")
```

Build the `meet_page` fixture from `test_meet_prejoin.py`'s pattern (serve the HTML on a loopback aiohttp site, `GoogleMeetProvider` with `FakeDisplay`, `join_meeting` against the replica; look at how those tests reach `CONNECTED` and reuse it).

- [ ] **Step 2: Run to see them fail** — Expected: FAIL.

- [ ] **Step 3: Implement** `_read_controls()` (called after join, after every `control()`, and every 5 s by a small task while connected) that queries the four buttons and builds `info.controls` through `controls_from({"me": {"muted": not mic_on}, "role": "participant", "buttons": {...}, "participants": [], "recording": False}, [])` with the layout/reactions/hide_self keys absent; `control()`:

```python
    SHORTCUTS = {"camera": "Control+e", "raise_hand": "Control+Alt+h", "captions": "c"}

    async def control(self, name: str, value: Any = None) -> Dict[str, Any]:
        if self._page is None or self._state != MeetingState.CONNECTED:
            raise MeetingControlError("Not in a meeting")
        if name == "mute":
            await self.toggle_mute()
        elif name in self.SHORTCUTS:
            before = await self._read_controls()
            if not before.get(name, {}).get("available"):
                raise MeetingControlError(f"Meet shows no {name.replace('_', ' ')} button right now")
            await self._page.keyboard.press(self.SHORTCUTS[name])
            await asyncio.sleep(0.5)
        elif name == "chat.seen":
            return {"chat": self._current_meeting.chat}
        else:
            raise MeetingControlError("Not available in a Meet call")
        return await self._read_controls()
```

`toggle_camera`/`toggle_mute` read the state back from the labels instead of flipping an assumption.

- [ ] **Step 4: Run** — `tests/unit/meeting/test_meet_controls.py tests/unit/meeting/test_meet_prejoin.py tests/unit/meeting/test_meet_selection.py` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/croom/meeting/providers/google_meet.py tests/unit/meeting/test_meet_controls.py
git commit -m "feat(meeting): Google Meet reports its buttons as controls and presses them from the table"
```

---

### Task 12: Diagnostics, docs and the README

**Files:**
- Modify: `src/croom/meeting/providers/zoom_sdk_page/meeting.js` (`diagnostics()` adds `dialogs`, `attendee_keys`, `sdk`), `src/croom/meeting/providers/zoom_sdk.py` (no change unless `diagnostics()` needs the SDK version: add `"sdk_version": self.SDK_VERSION`)
- Modify: `README.md` (the room page row, the features table rows for this spec and plan, the config table row for `dialogs`/`controls`), `docs/guides/crystal-meet-zoom/index.html` (+ rebuilt PDF: a new section "In the meeting" after the join section: the controls, the dialog policy with the config, the inventory run, the diagnostics route), `docs/guides/crystal-meet-google-meet/index.html` (+ PDF: one paragraph on the controls Meet offers), `docs/guides/crystal-meet-room-setup/index.html` (+ PDF: the meeting screen's rows and sub-screens, with the icons)
- Test: `tests/unit/docs/*` (the README test must see the spec and plan linked; the guide tests build the PDFs)

- [ ] **Step 1: Failing docs tests** — add to `tests/unit/docs/test_readme.py`'s needle list: `"docs/superpowers/specs/2026-10-13-meeting-controls-and-dialogs-design.md"`, `"ask_everything"`, `"/api/meeting/diagnostics"`; to the Zoom guide test: the guide names "In the meeting", "ask_everything", "Participants", "Host". Run them: FAIL.

- [ ] **Step 2: Write the docs**, rebuild the PDFs (`.venv/bin/python docs/guides/crystal-meet-zoom/build.py` etc.), extend `diagnostics()`:

```js
  bridge.diagnostics = async function () {
    const meeting = await bridge.readMeeting();
    const buttons = Array.from(document.querySelectorAll("button")).map((b) => ({
      label: (b.getAttribute("aria-label") || "").trim(), text: (b.textContent || "").trim().slice(0, 40),
    })).filter((b) => b.label || b.text);
    const list = await attendees();
    return { user_keys: meeting.keys, attendee_keys: list.length ? Object.keys(list[0]) : [], state: meeting.me, meeting: meeting,
             buttons: buttons, dialogs: bridge.dialogs(), dialogs_seen: dialogsSeen, page: document.title };
  };
```

with `dialogsSeen` an array `scanDialogs()` appends each new dialog's info to (capped at 50).

- [ ] **Step 3: Run the docs tests** — `.venv/bin/pytest -q -p no:cacheprovider -W error tests/unit/docs tests/unit/meeting/test_zoom_sdk_page.py -k "diagnostics or readme or guide"` — Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/guides src/croom/meeting/providers/zoom_sdk_page/meeting.js tests/unit/docs tests/unit/meeting/test_zoom_sdk_page.py
git commit -m "docs: the meeting's controls, dialogs and the inventory run in the guides and README; diagnostics list the dialogs seen"
```

---

## Self-review

- **Spec coverage:** section 3 model → Tasks 5, 6, 8; section 4.1–4.4 → Tasks 1–3, 5; 4.5 Meet → Task 11 (scoped to what Meet's shortcuts allow; `pin`, `layout`, `hide_self`, `reactions` report unavailable, which the spec permits with "where its page allows"); section 5 → Tasks 4, 5, 9; section 6.1 → Tasks 8, 10; 6.2 icons → Task 7; section 7 → Task 12; section 8 config → Tasks 5, 6; section 9 → the tests in each task. `pin` is unavailable in this version (noted in Task 5).
- **Placeholders:** none; every step carries its code or the exact file edit.
- **Type consistency:** `control(name, value)` everywhere; `MeetingInfo.people` (not `participants`, which exists as a list of names) on the model and `participants` on the wire; `controls_from(reading, reactions)`; `DialogPolicy.decide/disclosure`; `window.crystalMeet.readMeeting/control/answerDialog/dialogs/diagnostics`.
- **Review Focus:** each of the five lines names its test and task.
