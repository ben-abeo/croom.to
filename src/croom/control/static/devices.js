// Sound and Camera: the controls behind both panels and the TV preview (spec 2026-10-08, section 4.6).
(function () {
  "use strict";
  const P = window.croomPage;
  const el = P.el;
  const model = P.model;
  const post = P.post;
  const render = P.render;
  const button = P.button;
  const refreshStatus = P.refreshStatus;
  const deviceEpoch = P.deviceEpoch;
  const IN_MEETING = P.IN_MEETING;

  const SLIDE_OFF_PX = 16;   // how far outside a pressed pad button a finger may drift before that counts as letting go
  let lastSoundKey = null;
  let lastMicKey = null;
  let lastCameraKey = null;
  let presetSaveMode = false;
  let setupOpen = false;
  const FRAMING = "Framing\u2026";
  let framingHere = false;   // this screen's Frame people is under way (the status's camera.framing says it for every screen)

  // --- Sound and Camera: the commands behind both panels (spec 2026-10-08, section 4.6) ---
  // A success answers with the device's own block, which replaces the model's at once (no waiting for the next poll).
  // Any refusal (a non-2xx) puts its reason in that panel's note and leaves the model as it was. A long move that a
  // newer command cut short answers 409 "interrupted": the newer tap is the normal cause, so that says nothing at all.
  // options.long: Home, a preset recall and Find the stops run for seconds inside the request. options.background: a
  // call the page makes on its own (the preview's renewal): its answer is applied, but it neither clears the note the
  // user last got nor writes one, and a refusal of it is not shown or scrolled to.
  const INTERRUPTED = "the camera move was interrupted";
  const deviceNotes = { audio: "", mic: "", camera: "" };
  const NOTE_OF = { audio: "sound-note", mic: "mic-note", camera: "camera-note" };

  async function deviceCall(kind, path, body, options) {
    const long = Boolean(options && options.long);
    const background = Boolean(options && options.background);
    if (long) {
      // Home, a preset recall and Find the stops run for seconds inside the request: ask the status for the camera's
      // "busy" now, so "Moving..." shows without waiting for the poll's turn.
      deviceNotes[kind] = "";
      render();
      setTimeout(refreshStatus, 300);
    }
    let block;
    try {
      block = await post(path, body);
    } catch (e) {
      if (!background && e.message !== INTERRUPTED) {
        deviceNotes[kind] = e.message;
        // drawn again from the model: a slider the finger moved goes back
        if (kind === "audio") lastSoundKey = null; else if (kind === "mic") lastMicKey = null; else lastCameraKey = null;
        render();
        // The note sits above its panel: if the controls have scrolled it out of sight, bring it back, and no further.
        el(NOTE_OF[kind]).scrollIntoView({ block: "nearest" });
      }
      return false;
    }
    if (!background) deviceNotes[kind] = "";
    if (model.status) {
      // The camera's own answer has no "view" (who may see the picture: the status says it for the asking address): the page keeps it.
      const was = model.status[kind];
      model.status[kind] = kind === "camera" && was && was.view && block && !block.view ? Object.assign({}, block, { view: was.view }) : block;
    }
    deviceEpoch[kind] += 1;
    render();
    return block || true;   // the device's answer, for a caller that has words to take from it (Frame people)
  }

  // --- Sound ---
  function renderSound(audio) {
    const key = JSON.stringify([audio && audio.available, audio && audio.device, audio && audio.level, audio && audio.muted, deviceNotes.audio]);
    if (key === lastSoundKey) return;
    lastSoundKey = key;
    const panel = el("sound-panel");
    if (!audio || !audio.available) {
      panel.hidden = true;
      el("sound-note").textContent = "No speaker found";
      return;
    }
    panel.hidden = false;
    el("sound-note").textContent = deviceNotes.audio;
    el("sound-device").textContent = audio.device;
    el("volume-slider").value = audio.level;
    el("volume-level").textContent = String(audio.level);
    el("speaker-mute").setAttribute("aria-pressed", audio.muted ? "true" : "false");
    P.labelWithIcon(el("speaker-mute"), audio.muted ? "Unmute speaker" : "Mute speaker", audio.muted ? "volume-x" : "volume");   // the picture is the speaker's state, the words are the tap's action
  }

  const setVolume = (body) => deviceCall("audio", "/api/audio/volume", body);
  el("volume-slider").addEventListener("change", (e) => setVolume({ level: Number(e.target.value) }));
  el("quieter").addEventListener("click", () => setVolume({ step: -5 }));
  el("louder").addEventListener("click", () => setVolume({ step: 5 }));
  el("speaker-mute").addEventListener("click", () => {
    const muted = el("speaker-mute").getAttribute("aria-pressed") === "true";
    setVolume({ muted: !muted });
  });

  // --- Mic level (spec 2026-10-29): its own screen, from Options and from the meeting screen's dock. The room microphone, 0 to 150 where 100
  // is the device's normal level, Quieter / Louder 5 at a time. The level shown is the one the room keeps: the devices service puts it back
  // during a meeting, whatever the call's gain control does. ---
  function renderMic(mic) {
    const key = JSON.stringify([mic && mic.available, mic && mic.device, mic && mic.level, deviceNotes.mic]);
    if (key === lastMicKey) return;
    lastMicKey = key;
    const panel = el("mic-panel");
    if (!mic || !mic.available) {
      panel.hidden = true;
      el("mic-note").textContent = "No microphone found";
      return;
    }
    panel.hidden = false;
    el("mic-note").textContent = deviceNotes.mic;
    el("mic-device").textContent = mic.device;
    el("mic-slider").value = mic.level;
    el("mic-level").textContent = String(mic.level);
  }

  const setMic = (body) => deviceCall("mic", "/api/audio/mic", body);
  el("mic-slider").addEventListener("change", (e) => setMic({ level: Number(e.target.value) }));
  el("mic-quieter").addEventListener("click", () => setMic({ step: -5 }));
  el("mic-louder").addEventListener("click", () => setMic({ step: 5 }));

  // --- Camera ---
  let previewTimer = null;

  function renderCamera(camera) {
    // Keyed like the other controls, so a button is never replaced under a finger when nothing about it changed.
    // model.busy is in the key because button() reads it.
    const key = JSON.stringify([camera && camera.available, camera && camera.busy, camera && camera.framing, camera && camera.auto_framing,
      framingHere, camera && camera.position_known,
      camera && camera.home_saved, camera && camera.zoom, camera && camera.presets, camera && camera.limits, camera && camera.picture, presetSaveMode,
      setupOpen, model.busy, deviceNotes.camera, P.cameraMode()]);
    if (key === lastCameraKey) return;
    lastCameraKey = key;
    const panel = el("camera-panel");
    if (!camera || !camera.available) {
      panel.hidden = true;
      el("camera-note").textContent = "No controllable camera found";
      return;
    }
    panel.hidden = false;
    el("camera-note").textContent = deviceNotes.camera;
    el("camera-status").textContent = camera.framing || framingHere ? FRAMING : camera.busy ? "Moving\u2026"
      : "Zoom " + camera.zoom.level + " of " + camera.zoom.min + " to " + camera.zoom.max;
    el("frame-people").disabled = camera.busy || framingHere;
    // Auto framing (the room's setting, during a call): on, or paused while a person has the camera
    el("auto-framing-note").hidden = !camera.auto_framing;
    el("auto-framing-note").textContent = camera.auto_framing === "paused" ? "Auto framing is paused while the camera is moved by hand: "
      + "Frame people gives it back" : camera.auto_framing === "on" ? "Auto framing is on" : "";
    for (const b of document.querySelectorAll("#arrow-pad .pad")) b.disabled = camera.busy;
    el("camera-home").disabled = camera.busy || !camera.home_saved;
    const ready = camera.home_saved;
    el("preset-row").hidden = !ready;
    const setupAllowed = P.cameraMode() === "idle";   // during a meeting: recall and nudge only
    el("preset-save-mode").hidden = !ready || !setupAllowed;
    el("setup-link").hidden = !ready || !setupAllowed;
    el("camera-setup").hidden = !setupAllowed || (ready && !setupOpen);
    el("save-home").disabled = !camera.position_known || camera.busy;
    el("find-stops").disabled = camera.busy;
    // A camera that says where it points (camera.limits, the Rally) has no end stops to find, and keeps to limits instead
    const limits = camera.limits || null;
    el("find-stops").hidden = !!limits;
    el("camera-limits").hidden = !limits;
    el("setup-note").textContent = limits ? LIMITS_NOTE : STOPS_NOTE;
    for (const b of el("camera-limits").querySelectorAll("button")) {
      const set = !!(limits && limits[b.dataset.edge]);
      b.textContent = (set ? "Clear " : "Set ") + EDGE_WORDS[b.dataset.edge];
      b.setAttribute("aria-pressed", set ? "true" : "false");
      b.classList.toggle("saved", set);
      b.disabled = camera.busy;
    }
    renderPicture(camera.picture || []);
    el("preset-save-mode").setAttribute("aria-pressed", presetSaveMode ? "true" : "false");
    P.labelWithIcon(el("preset-save-mode"), presetSaveMode ? "Tap a preset to save it here" : "Save", el("preset-save-mode").dataset.icon);
    el("preset-row").replaceChildren(...camera.presets.map((p) => {
      const b = button(p.name, p.saved ? "quiet saved" : "quiet", () => presetAction(p.slot), camera.busy);
      b.dataset.slot = String(p.slot);
      if (!p.saved && !presetSaveMode) b.title = "Nothing saved yet";
      return b;
    }));
  }

  const STOPS_NOTE = "Set up once: find the camera's end stops, nudge it to the view you want, then save that as Home.";
  const LIMITS_NOTE = "Nudge the camera to the view you want, then save that as Home. To keep it from showing too much, point it as far as it "
    + "should go and tap that limit; tap it again to clear it.";
  const EDGE_WORDS = { down: "lowest", up: "highest", left: "leftmost", right: "rightmost" };
  // The picture: one row per setting the camera has, made once and then kept up to date (a slider under a finger is left alone). The
  // white balance's temperature shows only with the automatic one off: the camera takes it only then.
  const SETTING_WORDS = { brightness: "Brightness", contrast: "Contrast", saturation: "Colour", sharpness: "Sharpness",
    backlight_compensation: "Bright window behind people", white_balance_automatic: "Automatic white balance",
    white_balance_temperature: "White balance, warm to cool" };
  let pictureNames = null;

  function pictureRow(setting) {
    const row = document.createElement("label");
    row.className = "picture-setting";
    const words = document.createElement("span");
    words.textContent = SETTING_WORDS[setting.name] || setting.name;
    const input = document.createElement("input");
    input.dataset.picture = setting.name;
    if (setting.min === 0 && setting.max === 1) {
      input.type = "checkbox";
    } else {
      input.type = "range";
      input.min = String(setting.min);
      input.max = String(setting.max);
      input.step = String(setting.step || 1);
    }
    input.addEventListener("change", () => deviceCall("camera", "/api/camera/picture",
      { name: setting.name, value: input.type === "checkbox" ? (input.checked ? 1 : 0) : Number(input.value) }));
    row.append(words, input);
    return row;
  }

  function renderPicture(settings) {
    el("camera-picture").hidden = settings.length === 0;
    const box = el("picture-controls");
    const names = settings.map((s) => s.name).join(",");
    if (names !== pictureNames) {
      pictureNames = names;
      box.replaceChildren(...settings.map(pictureRow));
    }
    const automatic = settings.find((s) => s.name === "white_balance_automatic");
    for (const s of settings) {
      const input = box.querySelector("[data-picture='" + s.name + "']");
      if (!input) continue;
      if (input.type === "checkbox") input.checked = s.value !== 0;
      else if (document.activeElement !== input) input.value = String(s.value);
      if (s.name === "white_balance_temperature") input.closest(".picture-setting").hidden = !automatic || automatic.value !== 0;
    }
  }
  el("picture-reset").addEventListener("click", () => deviceCall("camera", "/api/camera/picture", { reset: true }));

  for (const b of el("camera-limits").querySelectorAll("button")) {
    b.addEventListener("click", () => deviceCall("camera", "/api/camera/setup",
      { action: b.getAttribute("aria-pressed") === "true" ? "clear_limit" : "set_limit", edge: b.dataset.edge }));
  }

  function presetAction(slot) {
    // Save belongs to Set up, which a meeting hides: a Save left on before the meeting must not turn a recall into one.
    const action = presetSaveMode && P.cameraMode() === "idle" ? "save" : "recall";
    presetSaveMode = false;
    lastCameraKey = null;
    deviceCall("camera", "/api/camera/presets/" + slot, { action: action }, { long: action === "recall" });
  }

  el("preset-save-mode").addEventListener("click", () => { presetSaveMode = !presetSaveMode; lastCameraKey = null; render(); });
  el("setup-link").addEventListener("click", () => { setupOpen = !setupOpen; lastCameraKey = null; render(); });
  el("find-stops").addEventListener("click", () => deviceCall("camera", "/api/camera/setup", { action: "find_stops" }, { long: true }));
  el("save-home").addEventListener("click", () => {
    deviceCall("camera", "/api/camera/setup", { action: "save_home" }).then((saved) => {
      if (saved) { setupOpen = false; render(); }   // Set up closes only once the home is really saved
    });
  });
  el("camera-home").addEventListener("click", () => deviceCall("camera", "/api/camera/home", {}, { long: true }));

  // Frame people (spec 2026-11-01, section 5): one tap zooms and turns toward the faces the camera finds, for some seconds inside the request.
  // "Framing..." shows at once here (and on every other screen from the status's camera.framing); the outcome is the camera's note.
  // A refusal ("Already framing", "Moving...", the picture's words) is the note as any refusal is.
  function framedWords(block) {
    const n = block.framed;
    if (!n) return "No one found: the camera stayed where it was";
    const words = n === 1 ? "Framed 1 person" : "Framed " + n + " people";
    return block.turned === false ? words + ". Set up the camera to let it turn too" : words;
  }
  el("frame-people").addEventListener("click", async () => {
    if (framingHere) return;
    framingHere = true;
    lastCameraKey = null;
    const block = await deviceCall("camera", "/api/camera/frame", {}, { long: true });
    framingHere = false;
    if (block && typeof block === "object" && "framed" in block) deviceNotes.camera = framedWords(block);
    lastCameraKey = null;
    render();
  });

  // Hold to move: press starts, release stops; re-sent every 750 ms while held so the agent's watchdog stays quiet.
  // Sliding the finger off the button is a release too (spec 2026-10-08, section 4.6): the pointer stays captured so
  // that a lift is always seen, and a move more than SLIDE_OFF_PX outside the button's box (a finger resting on the
  // edge jitters) ends the hold the same way a lift does.
  function holdToMove(buttonEl, start, repeatMs, stop) {
    let timer = null;
    const release = () => {
      if (timer === null) return;
      clearInterval(timer);
      timer = null;
      // A pad that has gone disabled (a Home or a preset from another screen made the camera busy) sends no stop: it
      // would cut that long move short.
      if (stop && !buttonEl.disabled) stop();
    };
    buttonEl.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      if (buttonEl.disabled || timer !== null) return;
      buttonEl.setPointerCapture(e.pointerId);
      start();
      timer = setInterval(() => {
        // The camera went busy under the finger (a Home or a preset from another screen): stop repeating, and do not
        // send the stop either, which would cut that move short.
        if (buttonEl.disabled) { clearInterval(timer); timer = null; return; }
        start();
      }, repeatMs);
    });
    for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) buttonEl.addEventListener(type, release);
    buttonEl.addEventListener("pointermove", (e) => {
      if (timer === null) return;   // no hold: nothing to end, and the rest of a press that already ended is ignored
      const box = buttonEl.getBoundingClientRect();
      const slack = SLIDE_OFF_PX;
      if (e.clientX >= box.left - slack && e.clientX <= box.right + slack && e.clientY >= box.top - slack && e.clientY <= box.bottom + slack) return;
      release();
      if (buttonEl.hasPointerCapture(e.pointerId)) buttonEl.releasePointerCapture(e.pointerId);
    });
    buttonEl.addEventListener("contextmenu", (e) => e.preventDefault());   // a long press on a touch screen must not open a menu
  }
  const moveCamera = (pan, tilt) => deviceCall("camera", "/api/camera/move", { pan: pan, tilt: tilt });
  for (const b of document.querySelectorAll("#arrow-pad [data-pan]")) {
    const pan = Number(b.dataset.pan), tilt = Number(b.dataset.tilt);
    holdToMove(b, () => moveCamera(pan, tilt), 750, () => moveCamera(0, 0));
  }
  for (const b of document.querySelectorAll("[data-zoom]")) {
    holdToMove(b, () => deviceCall("camera", "/api/camera/zoom", { step: Number(b.dataset.zoom) }), 400, null);
  }

  // The idle preview on the TV follows the Camera framing screen (spec 2026-10-12, section 4.2): asked for while the
  // camera view is shown in idle mode and no meeting runs, renewed every 30 s, and turned off once when the page
  // leaves that view: by Back, or on its own when a meeting begins or the idle return fires (app.js, ten minutes
  // without a tap), which is nobody's action (a background call: no note, no scroll if it fails). The camera view
  // opened from a meeting never asks for it.
  function previewWanted() {
    const s = model.status;
    return Boolean(P.view() === "camera" && P.cameraMode() === "idle" && s && s.camera && s.camera.available && !IN_MEETING.includes(s.meeting.state));
  }
  const setPreview = (on, options) => deviceCall("camera", "/api/camera/preview", { on: on }, options);
  let previewing = false;
  let onCameraView = false;
  P.onViewChange((name, automatic) => {
    if (onCameraView && name !== "camera") {
      // Leaving the camera view, by Back or on its own, puts Save and Set up away: a Save left on is never still armed
      // for the next person to open the screen.
      presetSaveMode = false;
      setupOpen = false;
      lastCameraKey = null;   // the panel is drawn again from the model
    }
    onCameraView = name === "camera";
    if (previewing) {
      clearInterval(previewTimer);
      previewTimer = null;
      previewing = false;
      setPreview(false, automatic ? { background: true } : undefined);   // the page left the screen on its own: nobody's action
    }
    if (name === "camera" && P.cameraMode() === "idle") {
      previewing = true;
      if (previewWanted()) setPreview(true);
      previewTimer = setInterval(() => { if (previewWanted()) setPreview(true, { background: true }); }, 30000);
    }
    syncPicture();
  });
  window.addEventListener("pagehide", () => {
    if (!previewing) return;
    fetch("/api/camera/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ on: false }), keepalive: true }).catch(() => {});
  });

  // --- The live picture on the touch Pi (spec 2026-11-01, section 3) ---
  // On the touch Pi the room lets see it (camera.view.allowed) the camera screen has the picture on its left: frames come over the
  // WebSocket GET /api/camera/view while the screen is shown (and the page is), and the socket is closed the moment it is not, which is
  // what lets the TV Pi stop its tap. One finger dragging in the picture turns the camera as the arrow pad does, against the finger (the
  // picture follows it); two fingers pinching scale the picture at once and zoom the camera. Every other page has no picture and no socket.
  const DEAD_ZONE_PX = 30;        // a drag this near its touch point moves nothing
  const MOVE_REPEAT_MS = 750;     // as the arrow pad: the agent's watchdog stays quiet while a finger holds
  const ZOOM_EVERY_MS = 100;      // a pinch sends the zoom level at most this often (and once more at its end)
  const NO_FRAME_MS = 5000;       // no picture this long: "No picture from the camera"
  const ZOOM_MIN = 100, ZOOM_MAX = 500;
  const PICTURE_WORDS = {
    starting: "Starting the picture\u2026", camera_off: "Your camera is off in the call", laptop: "The camera is on the laptop",
    failed: "No picture from the camera",
  };
  const pictureBox = el("picture");
  const pictureImg = el("picture-img");
  let pictureOn = false;          // the picture is wanted: a socket is open (or was and ended, to be tried again)
  let pictureSocket = null;
  let pictureState = "starting";  // what the room last said (CameraView.state()), "starting" until it speaks
  let pictureFrame = false;       // a frame is on show
  let pictureLost = false;        // the socket ended or no frame came for NO_FRAME_MS
  let pictureUrl = null;          // the frame's object URL: the one before is revoked as the next replaces it
  let pictureTimer = null;

  function drawPicture() {
    let words = "";
    if (pictureState === "camera_off" || pictureState === "laptop") words = PICTURE_WORDS[pictureState];
    else if (pictureLost || pictureState === "failed") words = PICTURE_WORDS.failed;
    else if (!pictureFrame) words = PICTURE_WORDS.starting;
    pictureImg.hidden = words !== "" || !pictureFrame;
    el("picture-words").hidden = words === "";
    el("picture-message").textContent = words;
    el("picture-retry").hidden = words !== PICTURE_WORDS.failed;
  }

  function dropFrame() {
    pictureFrame = false;
    if (pictureUrl !== null) URL.revokeObjectURL(pictureUrl);
    pictureUrl = null;
    pictureImg.removeAttribute("src");
  }

  // The wait for a frame starts again with each frame and each word from the room, and does not run while the room says why there is none.
  function armNoFrame() {
    clearTimeout(pictureTimer);
    pictureTimer = null;
    pictureLost = false;
    if (pictureSocket === null || pictureState === "camera_off" || pictureState === "laptop" || pictureState === "failed") return;
    pictureTimer = setTimeout(() => { pictureLost = true; drawPicture(); }, NO_FRAME_MS);
  }

  function showFrame(blob) {
    if (pictureState === "camera_off" || pictureState === "laptop" || pictureState === "failed") return;   // the room says there is none
    if (pictureState === "off") pictureState = "starting";
    const url = URL.createObjectURL(blob);
    const before = pictureUrl;
    pictureUrl = url;
    pictureImg.src = url;
    if (before !== null) URL.revokeObjectURL(before);
    pictureFrame = true;
    armNoFrame();
    drawPicture();
  }

  function openPicture() {
    pictureOn = true;
    pictureState = "starting";
    pictureLost = false;
    const mine = new WebSocket((location.protocol === "https:" ? "wss:" : "ws:") + "//" + location.host + "/api/camera/view");
    pictureSocket = mine;
    mine.onmessage = (e) => {
      if (pictureSocket !== mine) return;
      if (typeof e.data !== "string") { showFrame(e.data); return; }
      let said;
      try { said = JSON.parse(e.data).state; } catch (err) { return; }
      if (!Object.prototype.hasOwnProperty.call(PICTURE_WORDS, said) && said !== "on" && said !== "off") return;
      pictureState = said;
      if (said !== "on") dropFrame();      // starting again (the TV changed page), or no picture to show
      armNoFrame();
      drawPicture();
    };
    mine.onclose = () => {
      if (pictureSocket !== mine) return;   // closed by the page itself
      pictureSocket = null;
      clearTimeout(pictureTimer);
      pictureLost = true;
      dropFrame();
      drawPicture();
    };
    armNoFrame();
    drawPicture();
  }

  function closePicture() {
    pictureOn = false;
    const socket = pictureSocket;
    pictureSocket = null;       // (before the close, so that its onclose is not taken for the room's)
    clearTimeout(pictureTimer);
    pictureTimer = null;
    if (socket !== null) socket.close();
    dropFrame();
  }

  el("picture-retry").addEventListener("click", () => { closePicture(); openPicture(); });

  // Where the picture is: shown whenever the room lets this page see it; the socket only while the camera screen is up and the page is seen.
  function syncPicture() {
    const s = model.status;
    const allowed = Boolean(s && s.camera && s.camera.available && s.camera.view && s.camera.view.allowed);
    el("camera-view").hidden = !allowed;
    if (allowed) el("camera-grid").dataset.picture = ""; else delete el("camera-grid").dataset.picture;
    const wanted = allowed && P.view() === "camera" && !document.hidden;
    if (wanted && !pictureOn) openPicture();
    else if (!wanted && pictureOn) { closePicture(); endGestures(); }
    else if (!wanted) endGestures();
  }

  // One finger drags, two pinch. A gesture ends when its finger lifts, and as well when the browser cancels it, when it loses its capture,
  // when the page is hidden or loses focus and when the screen is left: the finger that never seems to lift must not leave the camera turning.
  const fingers = new Map();      // pointer id -> its last place
  let gesture = "none";           // "drag", "pinch", or "spent": a pinch ended with a finger still down, which starts nothing new
  let drag = null;
  let pinch = null;
  let zoomWanted = null;
  let zoomBusy = false;

  const cameraBusy = () => Boolean(model.status && model.status.camera && model.status.camera.busy);

  function turn(pan, tilt) {
    if (drag.timer !== null) clearInterval(drag.timer);
    drag.timer = null;
    if (!pan && !tilt) {
      if (drag.moving) { drag.moving = false; moveCamera(0, 0); }
      return;
    }
    const send = () => {
      // The camera went busy under the finger (a Home or a preset from another screen): no more moves, and no stop that would cut it short.
      if (cameraBusy()) { clearInterval(drag.timer); drag.timer = null; drag.moving = false; return; }
      drag.moving = true;
      moveCamera(pan, tilt);
    };
    send();
    if (drag.moving) drag.timer = setInterval(send, MOVE_REPEAT_MS);
  }

  function dragTo(place) {
    const dx = place.x - drag.x, dy = place.y - drag.y;
    // The picture follows the finger: right turns the camera left (pan -1), and down turns it up (tilt +1: the screen's y grows downward).
    const pan = Math.abs(dx) > DEAD_ZONE_PX ? -Math.sign(dx) : 0;
    const tilt = Math.abs(dy) > DEAD_ZONE_PX ? Math.sign(dy) : 0;
    if (pan === drag.pan && tilt === drag.tilt) return;
    drag.pan = pan;
    drag.tilt = tilt;
    turn(pan, tilt);
  }

  function endDrag() {
    if (drag === null) return;
    if (drag.timer !== null) clearInterval(drag.timer);
    if (drag.moving) moveCamera(0, 0);
    drag = null;
  }

  function sendZoom(level) {
    zoomWanted = level;
    if (zoomBusy) return;     // one request at a time, the newest level next: they cannot arrive out of order
    zoomBusy = true;
    (async () => {
      while (zoomWanted !== null) {
        const next = zoomWanted;
        zoomWanted = null;
        await deviceCall("camera", "/api/camera/zoom", { level: next });
      }
      zoomBusy = false;
    })();
  }

  const spread = () => { const [a, b] = [...fingers.values()]; return Math.hypot(a.x - b.x, a.y - b.y) || 1; };

  function startPinch() {
    const [a, b] = [...fingers.values()];
    const box = pictureBox.getBoundingClientRect();
    const level = model.status && model.status.camera && model.status.camera.zoom ? model.status.camera.zoom.level : ZOOM_MIN;
    pinch = { spread: spread(), level: level, now: null, sent: null, sentAt: 0, timer: null };
    pictureImg.style.transformOrigin = ((a.x + b.x) / 2 - box.left) + "px " + ((a.y + b.y) / 2 - box.top) + "px";
  }

  function pinchSend() {
    pinch.timer = null;
    if (pinch.now === null || pinch.now === pinch.sent) return;
    pinch.sent = pinch.now;
    pinch.sentAt = performance.now();
    sendZoom(pinch.now);
  }

  function pinchTo() {
    const ratio = spread() / pinch.spread;
    pictureImg.style.transform = "scale(" + ratio + ")";      // at once: the camera's own frames catch up
    pinch.now = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, Math.round(pinch.level * ratio)));
    const wait = ZOOM_EVERY_MS - (performance.now() - pinch.sentAt);
    if (wait <= 0) { clearTimeout(pinch.timer); pinchSend(); }
    else if (pinch.timer === null) pinch.timer = setTimeout(pinchSend, wait);
  }

  function endPinch() {
    if (pinch === null) return;
    clearTimeout(pinch.timer);
    pinchSend();                                              // the level the fingers ended on, once
    pinch = null;
    pictureImg.style.transform = "";
    pictureImg.style.transformOrigin = "";
  }

  function endGestures() {
    if (gesture === "drag") endDrag(); else if (gesture === "pinch") endPinch();
    for (const id of fingers.keys()) { try { pictureBox.releasePointerCapture(id); } catch (err) { /* already gone */ } }
    fingers.clear();
    gesture = "none";
  }

  function liftFinger(e) {
    if (!fingers.delete(e.pointerId)) return;
    if (gesture === "drag") endDrag(); else if (gesture === "pinch") endPinch();
    gesture = fingers.size ? "spent" : "none";
  }

  pictureBox.addEventListener("pointerdown", (e) => {
    if (e.target.closest("button")) return;       // Try again is a button, not a place to drag from
    e.preventDefault();
    if (fingers.size >= 2) return;
    fingers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    pictureBox.setPointerCapture(e.pointerId);
    if (fingers.size === 2) {
      if (gesture === "drag") endDrag();
      gesture = "pinch";
      startPinch();
    } else {
      gesture = "drag";
      drag = { x: e.clientX, y: e.clientY, pan: 0, tilt: 0, moving: false, timer: null };
    }
  });
  pictureBox.addEventListener("pointermove", (e) => {
    const place = fingers.get(e.pointerId);
    if (!place) return;
    place.x = e.clientX;
    place.y = e.clientY;
    if (gesture === "drag") dragTo(place); else if (gesture === "pinch") pinchTo();
  });
  for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) pictureBox.addEventListener(type, liftFinger);
  pictureBox.addEventListener("contextmenu", (e) => e.preventDefault());
  window.addEventListener("blur", endGestures);
  document.addEventListener("visibilitychange", syncPicture);
  window.addEventListener("pagehide", () => { closePicture(); endGestures(); });
  window.addEventListener("pageshow", syncPicture);

  P.onRender((s) => {
    renderSound(s.audio);
    renderMic(s.mic);
    renderCamera(s.camera);
    syncPicture();
  });
})();
