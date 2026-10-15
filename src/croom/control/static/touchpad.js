// The touchpad (spec 2026-10-13 TV remote and touchpad, sections 3 to 5): what a finger on the table's screen does to the TV's cursor or to the
// room Pi's mouse. Three parts, in this order: the touch reader, which turns the fingers on one surface into pointer messages; the pointer
// socket's client (/ws/pointer), which carries them; and the full-screen surface, one more place for a reader to listen. remote.js puts a reader
// on the TV view's trackpad and opens the surface; they share the one socket, and either can send to either target.
(function () {
  "use strict";
  const P = window.croomPage;
  const el = P.el;

  // How the pad feels: the plan's values, tuned on the table's own screen at Pi acceptance. Each says why it is the number it is.
  const GAIN = 1.6;                          // the cursor goes a little further than the finger: the table's screen is small and the TV's is wide
  const ACCEL_PX = 40;                       // the travel in one frame at which the acceleration tops out, at twice the gain: a flick crosses the screen, a slow drag is exact
  const TAP_MS = 180;                        // a touch shorter than this that stayed within TAP_PX is a click
  const TAP_PX = 8;                          // how far a fingertip may roll and still be still
  const SLOP_PX = 3;                         // how far it may wander and still not have moved at all: the noise of a resting finger and of the screen under it
  const HOLD_MS = 350;                       // a finger that rested this long before it first moved, and then goes, drags
  const PI_NOTCH_PX = 30;                    // two fingers' travel that turns the room Pi's wheel one notch
  const TOUCHPAD_IDLE_MS = 10 * 60 * 1000;   // the surface puts itself away after this long with no touch on it
  const RECONNECT_MS = [1000, 2000, 5000];   // the socket's waits before it tries again: after the first loss, after the second, then every five seconds
  const WORDS_MS = 5000;                     // what the TV or the mouse said it could not do stays this long after the last time it said it

  // --- the socket ---
  // One /ws/pointer socket, open while somebody needs it (the TV view's trackpad or the full-screen surface) and closed when nobody does. A
  // socket that is lost (the agent restarted, the network blinked) comes back by itself. A message is for the moment: one that is sent while the
  // socket is down is dropped, never kept for later, because a cursor that catches up on its own is worse than one that waits.
  const link = (function () {
    const wants = new Set();   // who needs it just now: "disc" or "surface"
    const watchers = [];       // fn(state), on every change of the state
    const speakers = [];       // fn(words), for every {"error": words} the agent sends back
    let socket = null;
    let state = "closed";      // "closed" (nobody needs it), "connecting", "up", or "down" (lost: it comes back by itself)
    let retry = null;
    let losses = 0;            // the losses in a row: how long to wait this time

    function set(next) {
      if (state === next) return;
      state = next;
      for (const fn of watchers) fn(state);
    }
    function connect() {
      retry = null;
      const mine = new WebSocket((location.protocol === "https:" ? "wss:" : "ws:") + "//" + location.host + "/ws/pointer");
      socket = mine;
      if (state !== "down") set("connecting");
      mine.onopen = () => {
        if (socket !== mine) return;
        losses = 0;
        set("up");
      };
      mine.onmessage = (e) => {
        let data = null;
        try { data = JSON.parse(e.data); } catch (err) { return; }
        if (data && typeof data.error === "string") for (const fn of speakers) fn(data.error);
      };
      mine.onclose = () => {
        if (socket !== mine) return;   // one that was let go of on purpose
        socket = null;
        set("down");
        retry = setTimeout(connect, RECONNECT_MS[Math.min(losses, RECONNECT_MS.length - 1)]);
        losses += 1;
      };
    }
    function want(who, on) {
      if (on) wants.add(who); else wants.delete(who);
      if (wants.size) {
        if (!socket && retry === null) connect();
        return;
      }
      clearTimeout(retry);
      retry = null;
      losses = 0;
      const gone = socket;
      socket = null;
      if (gone) gone.close(1000, "done");   // a normal closure, with its reason
      set("closed");
    }
    function send(message) {
      if (!socket || socket.readyState !== WebSocket.OPEN) return false;
      socket.send(JSON.stringify(message));
      return true;
    }
    return { want: want, send: send, watch: (fn) => watchers.push(fn), onError: (fn) => speakers.push(fn), state: () => state };
  })();

  // --- the touch reader ---
  // The fingers on one surface. `node` is where they go down; target() says whom a message is for, "tv" or "pi", each time one is sent.
  // A gesture runs from the first finger down to the last finger up. One finger moves the cursor; a short quick touch is a click; a press and
  // hold drags, with the button down until it lifts. What a touch is is decided once, when its finger first really moves (past SLOP_PX): before
  // HOLD_MS it is a mover for good, however slowly it goes on, and never drags; from HOLD_MS on it is a press and hold, however gently it starts.
  // A finger that never passes the slop is a click, if it was quick, and nothing otherwise. A second finger makes it a two-finger gesture,
  // which stays one until every finger is up: the fingers scroll, and a short quick touch of both is a right click (the room Pi's; the TV's
  // cursor has no right button). A third finger is ignored. The travel is gathered for each animation frame and sent as one message of whole
  // numbers; what the whole numbers leave behind is carried into the next, so a slow drag loses nothing. The travel of a touch is held back
  // until it has said what it is: a mover's by going TAP_PX or by lasting TAP_MS, a press and hold's by going TAP_PX, when the button goes down
  // and the held travel after it. A click sends only the click, and a still finger moves nothing.
  function reader(node, target) {
    const fingers = new Map();   // pointerId -> { x0, y0: where it landed, x, y: where it is, lx, ly: where the last frame left it }
    let g = null;                // the gesture: { at: when it began, two, far: how far a finger has been from where it landed, resting, released, dragging, carry }
    let frame = 0;
    let armTimer = null;
    const disarm = () => {       // the pad stops showing that a hold is ready
      clearTimeout(armTimer);
      armTimer = null;
      node.classList.remove("armed");
    };

    const send = (type, more) => link.send(Object.assign({ target: target(), type: type }, more));
    const whole = (carry) => {   // the whole numbers of the carry, which keeps what is left
      const x = Math.trunc(carry.x), y = Math.trunc(carry.y);
      carry.x -= x;
      carry.y -= y;
      return [x, y];
    };
    const released = (t) => {    // the touch has said what it is, so its travel goes
      if (!g.released && (g.far >= TAP_PX || (g.resting === false && t - g.at >= TAP_MS))) g.released = true;
      return g.released;
    };

    function travel(t) {
      const f = fingers.values().next().value;
      const dx = f.x - f.lx, dy = f.y - f.ly;
      f.lx = f.x;
      f.ly = f.y;
      const k = GAIN * (1 + Math.min(Math.hypot(dx, dy), ACCEL_PX) / ACCEL_PX);   // the faster the frame's travel, the further the cursor goes
      g.carry.x += dx * k;
      g.carry.y += dy * k;
      if (!released(t)) return;
      const [x, y] = whole(g.carry);
      if (x || y) send("move", { dx: x, dy: y });
    }

    function scroll(t) {
      let dx = 0, dy = 0;
      for (const f of fingers.values()) { dx += f.x - f.lx; dy += f.y - f.ly; f.lx = f.x; f.ly = f.y; }
      if (fingers.size < 2) return;   // one of the two has lifted: what is left of the gesture scrolls nothing
      const pi = target() === "pi";
      if (!pi) g.carry.x += dx / 2;
      g.carry.y += dy / 2 / (pi ? PI_NOTCH_PX : 1);   // the room Pi's wheel turns in notches; the TV takes the pixels
      if (!released(t)) return;
      const [x, y] = whole(g.carry);
      if (x || y) send("scroll", { dx: x, dy: y });
    }

    function flush(t) {          // t is the time of the frame, or of the event that asks for the flush: the panel's clock, like the stamps of the events
      frame = 0;
      if (g) (g.two ? scroll : travel)(t);
    }
    function cancelFrame() {
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
    }

    node.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      e.preventDefault();
      if (fingers.has(e.pointerId) || (g && g.two)) return;   // a third finger, or one that lands after a finger of two has lifted: a gesture with two stays that
      try { node.setPointerCapture(e.pointerId); } catch (err) { /* a synthetic event has no pointer to capture */ }
      const t = e.timeStamp;   // when the panel saw the finger land: a page that is slow to get to the event does not make a tap into something else
      if (g) {   // the second finger: the first one's travel so far was the cursor's, and the two begin together from here
        disarm();
        cancelFrame();
        flush(t);
        if (g.dragging) { send("up"); g.dragging = false; }
        g.two = true;
        g.at = t;
        g.far = 0;
        g.released = false;
        g.resting = null;
        g.carry.x = g.carry.y = 0;
        for (const f of fingers.values()) { f.x0 = f.lx = f.x; f.y0 = f.ly = f.y; }
      } else {
        g = { at: t, two: false, far: 0, released: false, dragging: false, resting: null, carry: { x: 0, y: 0 } };
        armTimer = setTimeout(() => node.classList.add("armed"), HOLD_MS);   // a finger that has rested: its next move drags, and the pad says so (a second finger, a mover's first travel, a lift or a cancel stop the timer)
      }
      fingers.set(e.pointerId, { x0: e.clientX, y0: e.clientY, x: e.clientX, y: e.clientY, lx: e.clientX, ly: e.clientY });
      node.classList.add("touching");
    });

    node.addEventListener("pointermove", (e) => {
      const f = fingers.get(e.pointerId);
      if (!f) return;
      f.x = e.clientX;
      f.y = e.clientY;
      const away = Math.hypot(f.x - f.x0, f.y - f.y0);
      if (away > g.far) {
        // What the touch is is decided once, when the finger first really moves: one that rested HOLD_MS before that is a press and hold, and
        // goes TAP_PX before it drags (the button goes down, then its travel); one that moved sooner is a mover and never a drag.
        if (g.resting === null && away >= SLOP_PX) {
          g.resting = !g.two && e.timeStamp - g.at >= HOLD_MS;
          if (g.resting) node.classList.add("armed"); else disarm();
        }
        if (g.resting && g.far < TAP_PX && away >= TAP_PX && send("down")) g.dragging = true;
        g.far = away;
      }
      if (!frame) frame = requestAnimationFrame(flush);
    });

    function lift(e, cancelled) {
      if (!fingers.has(e.pointerId)) return;
      cancelFrame();
      flush(e.timeStamp);                       // the travel that is still waiting goes first
      fingers.delete(e.pointerId);
      try { node.releasePointerCapture(e.pointerId); } catch (err) { /* gone already */ }
      if (fingers.size) return;                 // one finger of two: the gesture goes on until the last one is up
      const done = g, t = e.timeStamp;
      g = null;
      disarm();
      node.classList.remove("touching");
      if (done.dragging) send("up");            // a drag ends where the finger lifted, or where the browser took the touch away
      else if (!cancelled && t - done.at < TAP_MS && done.far < TAP_PX && (!done.two || target() === "pi")) send(done.two ? "right" : "tap");
    }
    node.addEventListener("pointerup", (e) => lift(e, false));
    node.addEventListener("pointercancel", (e) => lift(e, true));
    node.addEventListener("lostpointercapture", (e) => lift(e, true));   // (after a lift or a cancel the finger is gone already, and this does nothing)
    node.addEventListener("contextmenu", (e) => e.preventDefault());   // a long press on a touch screen must not open a menu

    // A drag the socket lost is not the page's to finish: the agent let the button go when the socket ended.
    link.watch((state) => { if (g && state !== "up") g.dragging = false; });

    // Everything stops, and nothing more is sent for what the fingers do from here: a held drag lets go first.
    function cancel() {
      cancelFrame();
      disarm();
      if (g && g.dragging) send("up");
      for (const id of fingers.keys()) { try { node.releasePointerCapture(id); } catch (err) { /* gone already */ } }
      fingers.clear();
      g = null;
      node.classList.remove("touching");
    }
    return { cancel: cancel };
  }

  // --- the full-screen surface ---
  // The whole touchscreen as a touchpad (spec 5.3): a strip that names the target, a switch between the two targets, Done, and nothing else
  // on it to be tapped by mistake. It opens over the TV view and the page's own idle return waits while it is up; Done, or ten minutes with
  // no touch on it, puts it away and the page is on the TV view again.
  const surface = (function () {
    const root = el("touchpad");
    const title = el("touchpad-title");
    const small = el("touchpad-sub");
    const how = el("touchpad-how");
    const mark = el("touchpad-link");
    const halves = { tv: el("touchpad-tv"), pi: el("touchpad-pi") };
    const HOW = {
      tv: "One finger moves \u00B7 Tap to click\nTwo fingers scroll \u00B7 Press and hold, then move, to drag",
      pi: "One finger moves \u00B7 Tap to click \u00B7 Two-finger tap to right-click\nTwo fingers scroll \u00B7 Press and hold, then move, to drag",
    };
    let target = "pi";
    let tv = { usable: false, name: "", configured: false };   // the TV's name, whether there is a TV to move, and whether the room has one set up at all
    let opener = null;
    let idle = null;
    let said = "";                          // what the agent last said it could not do, until it has been quiet for WORDS_MS
    let saidTimer = null;
    const read = reader(el("touchpad-surface"), () => target);

    function draw() {
      title.textContent = target === "tv" ? "Moving the TV's cursor" : "Moving the mouse on the room's screen";
      const line = said || (target === "tv" ? tv.name || "The TV" : tv.usable ? "The room Pi behind the TV"
        : tv.configured ? "Pair the TV to move its cursor" : "No TV is set up for this room");
      if (small.textContent !== line) small.textContent = line;
      how.textContent = HOW[target];
      for (const key of Object.keys(halves)) halves[key].setAttribute("aria-pressed", String(key === target));
      halves.tv.disabled = !tv.usable;
      mark.hidden = link.state() !== "down";
    }
    function touched() {
      clearTimeout(idle);
      idle = setTimeout(hide, TOUCHPAD_IDLE_MS);
    }
    function choose(next) {
      if (next === target) return;
      read.cancel();            // a drag ends on the target it began on
      target = next;
      said = "";
      draw();
    }
    // What the surface covers takes no focus while it is up, so that no key can leave it for a button that is out of sight.
    function coverPage(on) {
      for (const node of document.querySelectorAll("header.top, main.screen")) {
        if (on) node.setAttribute("inert", ""); else node.removeAttribute("inert");
      }
    }
    function show(options) {
      tv = options.tv;
      target = options.target;   // (remote.js asks for the TV only while there is one to move: the pointer mode goes off under the cards)
      opener = options.opener || null;
      said = "";
      root.hidden = false;
      coverPage(true);
      P.holdIdleReturn(true);
      link.want("surface", true);
      touched();
      draw();
      el("touchpad-done").focus({ preventScroll: true });
    }
    function hide() {
      if (root.hidden) return;
      read.cancel();            // a held drag goes up before the socket closes
      root.hidden = true;
      clearTimeout(idle);
      clearTimeout(saidTimer);
      said = "";
      link.want("surface", false);
      P.holdIdleReturn(false);
      coverPage(false);                                                  // (before the focus goes back: a node that is inert takes none)
      if (opener && opener.isConnected) opener.focus({ preventScroll: true });
      opener = null;
    }
    // The TV changed under the surface (it was forgotten, it was named): the strip follows, and a TV that cannot be moved any more is let go of.
    function sync(next) {
      tv = next;
      if (root.hidden) return;
      if (target === "tv" && !tv.usable) { read.cancel(); target = "pi"; said = ""; }
      draw();
    }

    for (const key of Object.keys(halves)) halves[key].addEventListener("click", () => choose(key));
    el("touchpad-done").addEventListener("click", hide);
    for (const type of ["pointerdown", "pointermove", "pointerup", "pointercancel"]) root.addEventListener(type, () => { if (!root.hidden) touched(); });
    link.watch(() => { if (!root.hidden) draw(); });
    link.onError((words) => {
      if (root.hidden) return;
      clearTimeout(saidTimer);
      saidTimer = setTimeout(() => { said = ""; draw(); }, WORDS_MS);
      if (said === words) return;   // the same words in a burst are drawn once
      said = words;
      draw();
    });
    return { show: show, hide: hide, sync: sync, isOpen: () => !root.hidden };
  })();

  window.croomTouchpad = { link: link, reader: reader, surface: surface, WORDS_MS: WORDS_MS };
})();
