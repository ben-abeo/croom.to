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
  let lastCameraKey = null;
  let presetSaveMode = false;
  let setupOpen = false;

  // --- Sound and Camera: the commands behind both panels (spec 2026-10-08, section 4.6) ---
  // A success answers with the device's own block, which replaces the model's at once (no waiting for the next poll).
  // Any refusal (a non-2xx) puts its reason in that panel's note and leaves the model as it was. A long move that a
  // newer command cut short answers 409 "interrupted": the newer tap is the normal cause, so that says nothing at all.
  // options.long: Home, a preset recall and Find the stops run for seconds inside the request. options.background: a
  // call the page makes on its own (the preview's renewal): its answer is applied, but it neither clears the note the
  // user last got nor writes one, and a refusal of it is not shown or scrolled to.
  const INTERRUPTED = "the camera move was interrupted";
  const deviceNotes = { audio: "", camera: "" };

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
        if (kind === "audio") lastSoundKey = null; else lastCameraKey = null;   // drawn again from the model: a slider the finger moved goes back
        render();
        // The note sits above its panel: if the controls have scrolled it out of sight, bring it back, and no further.
        el(kind === "audio" ? "sound-note" : "camera-note").scrollIntoView({ block: "nearest" });
      }
      return false;
    }
    if (!background) deviceNotes[kind] = "";
    if (model.status) model.status[kind] = block;
    deviceEpoch[kind] += 1;
    render();
    return true;
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
    el("speaker-mute").textContent = audio.muted ? "Unmute speaker" : "Mute speaker";
  }

  const setVolume = (body) => deviceCall("audio", "/api/audio/volume", body);
  el("volume-slider").addEventListener("change", (e) => setVolume({ level: Number(e.target.value) }));
  el("quieter").addEventListener("click", () => setVolume({ step: -5 }));
  el("louder").addEventListener("click", () => setVolume({ step: 5 }));
  el("speaker-mute").addEventListener("click", () => {
    const muted = el("speaker-mute").getAttribute("aria-pressed") === "true";
    setVolume({ muted: !muted });
  });

  // --- Camera ---
  let previewTimer = null;

  function renderCamera(camera) {
    // Keyed like the other controls, so a button is never replaced under a finger when nothing about it changed.
    // model.busy is in the key because button() reads it.
    const key = JSON.stringify([camera && camera.available, camera && camera.busy, camera && camera.position_known,
      camera && camera.home_saved, camera && camera.zoom, camera && camera.presets, presetSaveMode, setupOpen, model.busy, deviceNotes.camera, P.cameraMode()]);
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
    el("camera-status").textContent = camera.busy ? "Moving\u2026" : "Zoom " + camera.zoom.level + " of " + camera.zoom.min + " to " + camera.zoom.max;
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
    el("preset-save-mode").setAttribute("aria-pressed", presetSaveMode ? "true" : "false");
    el("preset-save-mode").textContent = presetSaveMode ? "Tap a preset to save it here" : "Save";
    el("preset-row").replaceChildren(...camera.presets.map((p) => {
      const b = button(p.name, p.saved ? "quiet saved" : "quiet", () => presetAction(p.slot), camera.busy);
      b.dataset.slot = String(p.slot);
      if (!p.saved && !presetSaveMode) b.title = "Nothing saved yet";
      return b;
    }));
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
  P.onViewChange((name, automatic) => {
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
  });
  window.addEventListener("pagehide", () => {
    if (!previewing) return;
    fetch("/api/camera/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ on: false }), keepalive: true }).catch(() => {});
  });

  P.onRender((s) => {
    renderSound(s.audio);
    renderCamera(s.camera);
  });
})();
