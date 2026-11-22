// Magnify (Ben, 2026-11-22): in a meeting, the TV's picture made larger from the table, to read a shared screen from the far end of the room.
// The agent magnifies the page the TV shows (Zoom's or Meet's alike, croom/meeting/magnify.py) and says how in the meeting's model:
// controls.magnify is {available, zoom, x, y, max}, where zoom 1 is the whole picture and (x, y) the point of it at the middle of the TV, as
// fractions of its width and height. This screen draws that as a box on a pad that stands for the whole picture, and sends what the fingers
// do as the "magnify" control: {zoom, x, y}. app.js shows the button that opens it (in the top bar, beside the Touchpad and the TV).
(function () {
  "use strict";
  const P = window.croomPage;
  const el = P.el;

  const STEPS = [1, 1.5, 2, 3, 4];     // the zoom's steps: each has a button, and Larger and Smaller go from one to the next
  const TAP_ZOOM = 2;                  // a tap on the whole picture magnifies to this, about the point tapped
  const TAP_MS = 300;                  // a touch shorter than this that stayed within TAP_PX is a tap
  const TAP_PX = 10;
  const SEND_MS = 50;                  // the TV is told of a move at most this often: twenty times a second follows a finger, and more only queues
  const ANSWER_MS = 10 * 1000;         // the agent answers at once (nothing of the platform's is pressed): longer than this is a room that is not answering
  const NO_ANSWER = "The room's screen did not answer in time; try again in a moment";

  const pad = el("magnify-pad");
  const box = el("magnify-window");

  // What the fingers have asked for and the TV has not answered yet, or is still being asked for by a finger that is down. While it is set
  // the screen draws it, and not the model: a poll that was already on its way would otherwise pull the box back under the finger.
  let wanted = null;
  let busy = false;          // a request is out: the next goes when it is answered, with what is wanted then
  let sentAt = 0;
  let message = "";          // a refusal, in the agent's words
  const fingers = new Map(); // the pointers down on the pad, by id: where each is now, as fractions of the pad
  let grab = null;           // one finger's hold on the box: how far the box's middle is from it
  let pinch = null;          // two fingers: how far apart they were, and the zoom, when the second came down
  let press = null;          // the touch that may yet be a tap: where and when it began, and whether the picture was whole

  const clamp = (value, low, high) => Math.min(Math.max(value, low), high);
  const most = () => { const state = P.magnifyState(); return state && state.max > 1 ? state.max : STEPS[STEPS.length - 1]; };
  // Within the bounds the TV's page keeps: the zoom between 1 and the most, and the box wholly inside the picture.
  function within(v) {
    const zoom = clamp(v.zoom, 1, most()), half = 0.5 / zoom;
    return { zoom: zoom, x: clamp(v.x, half, 1 - half), y: clamp(v.y, half, 1 - half) };
  }
  function shown() {
    const state = P.magnifyState();
    return state ? within({ zoom: Number(state.zoom) || 1, x: Number(state.x), y: Number(state.y) }) : { zoom: 1, x: 0.5, y: 0.5 };
  }
  const current = () => wanted || shown();
  const same = (a, b) => Math.abs(a - b) < 0.005;

  function draw() {
    const v = current(), side = 100 / v.zoom;
    box.style.width = box.style.height = side + "%";
    box.style.left = (v.x * 100 - side / 2) + "%";
    box.style.top = (v.y * 100 - side / 2) + "%";
    pad.dataset.zoom = String(Math.round(v.zoom * 100) / 100);
    el("magnify-level").textContent = P.zoomWords(v.zoom);
    el("magnify-out").disabled = v.zoom <= 1;
    el("magnify-in").disabled = v.zoom >= most();
    el("magnify-reset").disabled = v.zoom <= 1;
    for (const b of el("magnify-presets").children) b.setAttribute("aria-pressed", same(Number(b.dataset.zoom), v.zoom) ? "true" : "false");
    el("magnify-message").textContent = message;
  }

  // Ask the TV for a view. One request is out at a time: what is asked for meanwhile replaces what waited, so the TV goes to where the
  // finger is now and never plays back where it has been.
  function ask(v) {
    wanted = within(v);
    message = "";
    draw();
    if (!busy) send();
  }
  async function send() {
    busy = true;
    const wait = sentAt + SEND_MS - Date.now();
    if (wait > 0) await new Promise((done) => setTimeout(done, wait));
    const sent = wanted;
    if (!sent) { busy = false; return; }
    sentAt = Date.now();
    const cutoff = new AbortController();
    const timer = setTimeout(() => cutoff.abort(), ANSWER_MS);
    try {
      P.takeMeeting(await P.post("/api/meeting/control", { name: "magnify", value: sent }, { signal: cutoff.signal }));
    } catch (e) {
      message = e.name === "AbortError" ? NO_ANSWER : e.message;
      wanted = null;         // the TV is as the model says, not as the finger asked
    } finally {
      clearTimeout(timer);
    }
    if (wanted && wanted !== sent) { send(); return; }
    busy = false;
    if (!fingers.size) wanted = null;   // answered, and no finger is down: the model is what the TV has (with the bounds its page kept)
    P.render();              // the top bar's button says the zoom; the render draws this screen again too
  }

  // --- the pad: one finger moves the box, two pinch the zoom ---
  // Where a finger is, as fractions of the pad inside its border: the box is drawn in the same space.
  function place(e) {
    const r = pad.getBoundingClientRect();
    return { x: clamp((e.clientX - r.left - pad.clientLeft) / pad.clientWidth, 0, 1), y: clamp((e.clientY - r.top - pad.clientTop) / pad.clientHeight, 0, 1),
             px: e.clientX, py: e.clientY };
  }
  const apart = () => { const [a, b] = Array.from(fingers.values()); return Math.hypot(a.px - b.px, a.py - b.py); };

  pad.addEventListener("pointerdown", (e) => {
    if (fingers.size >= 2) return;
    e.preventDefault();
    try { pad.setPointerCapture(e.pointerId); } catch (err) { /* a pointer the browser has already let go of */ }
    const at = place(e), v = current();
    fingers.set(e.pointerId, at);
    if (fingers.size === 2) {
      pinch = { apart: Math.max(apart(), 1), zoom: v.zoom };
      grab = press = null;
      return;
    }
    press = { px: at.px, py: at.py, at: Date.now(), whole: v.zoom <= 1 };
    // A finger that lands on the box takes it where it holds it; one that lands outside brings the box's middle to itself.
    const half = 0.5 / v.zoom, inside = Math.abs(at.x - v.x) <= half && Math.abs(at.y - v.y) <= half;
    grab = inside ? { dx: v.x - at.x, dy: v.y - at.y } : { dx: 0, dy: 0 };
    if (v.zoom > 1 && !inside) ask({ zoom: v.zoom, x: at.x, y: at.y });
  });
  pad.addEventListener("pointermove", (e) => {
    if (!fingers.has(e.pointerId)) return;
    const at = place(e), v = current();
    fingers.set(e.pointerId, at);
    if (press && Math.hypot(at.px - press.px, at.py - press.py) > TAP_PX) press = null;
    if (pinch && fingers.size === 2) { ask({ zoom: pinch.zoom * apart() / pinch.apart, x: v.x, y: v.y }); return; }
    if (grab && v.zoom > 1) ask({ zoom: v.zoom, x: at.x + grab.dx, y: at.y + grab.dy });
  });
  function lift(e) {
    if (!fingers.has(e.pointerId)) return;
    const at = fingers.get(e.pointerId);
    fingers.delete(e.pointerId);
    pinch = null;
    grab = null;             // the finger left of a pinch does not drag the box: it lifts, and the next touch begins anew
    const tapped = e.type === "pointerup" && press && press.whole && Date.now() - press.at < TAP_MS;
    press = null;
    if (tapped) { ask({ zoom: TAP_ZOOM, x: at.x, y: at.y }); return; }
    if (!fingers.size && !busy) { wanted = null; draw(); }
  }
  pad.addEventListener("pointerup", lift);
  pad.addEventListener("pointercancel", lift);
  pad.addEventListener("lostpointercapture", lift);   // a finger the browser took away without saying it lifted: the box must not stay held (after a lift this finds nothing to do)

  // --- the buttons ---
  const zoomTo = (zoom) => { const v = current(); ask({ zoom: zoom, x: v.x, y: v.y }); };
  el("magnify-in").addEventListener("click", () => zoomTo(STEPS.find((step) => step > current().zoom + 0.005) || most()));
  el("magnify-out").addEventListener("click", () => zoomTo(STEPS.slice().reverse().find((step) => step < current().zoom - 0.005) || 1));
  el("magnify-reset").addEventListener("click", () => zoomTo(1));
  el("magnify-presets").replaceChildren(...STEPS.map((step) => {
    const b = P.button(P.zoomWords(step), "quiet", () => zoomTo(step));
    b.dataset.zoom = String(step);
    return b;
  }));

  P.onViewChange((name) => { if (name === "magnify") { message = ""; draw(); } });
  P.onRender(draw);
  draw();
})();
