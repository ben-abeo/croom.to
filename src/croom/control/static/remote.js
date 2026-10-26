// The TV view's remote on the table (spec 2026-10-13 TV remote and touchpad, section 5): the page's own part for LG and Samsung TVs, with the modes that
// turn its d-pad into a trackpad. app.js draws the rest of the page and shares it through window.croomPage (as devices.js does): the model, the
// requests, the buttons, the render and view hooks. touchpad.js reads the fingers for the trackpad and for the full-screen surface.
(function () {
  "use strict";
  const P = window.croomPage;
  const el = P.el;
  const model = P.model;
  const api = P.api;
  const post = P.post;
  const render = P.render;
  const button = P.button;
  const icon = P.icon;
  const labelWithIcon = P.labelWithIcon;
  const refreshStatus = P.refreshStatus;
  const deviceEpoch = P.deviceEpoch;
  const keepFocus = P.keepFocus;
  const view = P.view;
  const CONFIRM_MS = P.CONFIRM_MS;
  const T = window.croomTouchpad;

  // ===========================================================================================================================
  // The TV view: the remote on the table (spec 2026-10-13 TV remote and touchpad, section 5). The agent's status carries the TV's
  // block (status.tv) and /api/tv/* are its controls: this section draws the one and sends the other. Which of three cards (no TV
  // set up, not paired, no longer accepted) or the remote itself shows follows that block alone. Whatever the TV gave (its name, its
  // inputs, the app and the channel it shows) and whatever the agent said it refused goes onto the page as text, never as markup.
  // ===========================================================================================================================
  const TV_CONTROL_MS = 30000;    // the agent waits up to 23 s for a TV to turn on, and a control behind that one waits its turn
  const TV_SEARCH_MS = 10000;     // Find TVs takes five or six seconds
  const TV_PAIR_MS = 70000;       // the agent gives the person at the TV a minute, and the TV five seconds more to finish
  const TV_INPUTS_MS = 10000;     // the row of inputs puts itself away when nobody picks
  const TV_FLASH_MS = 900;        // a key the TV refused is amber this long
  const TV_PI = "Crystal Meet";   // what the table calls the input the room Pi is on
  const CONFIRM_GUARD_MS = 500;   // a button that has just asked for a second tap (Power, Forget TV) ignores a tap this long: a touch that a screen reports twice, a moment apart (about 150 ms), would otherwise be both the question and the answer, and a person's own second tap comes after more than half a second
  // The apps the status line names in words; any other is "an app": the TV's id for it is not a word for a person.
  const TV_APPS = { "com.webos.app.livetv": "Live TV", "com.webos.app.home": "Home screen", "com.webos.app.settings": "Settings",
    netflix: "Netflix", "youtube.leanback.v4": "YouTube", amazon: "Prime Video" };
  const TV_PAIR_WAIT = "Accept the request on the TV with its own remote. The table waits a minute for it.";
  const TV_REFUSED_TEXT = el("tv-refused-text").textContent;   // the card's own words, put back when a pairing from it is over
  const TV_ASKING = "The TV is asking to pair again \u2014 Forget TV, then Pair";   // the status line of a TV that shows its pairing prompt for the key this table keeps (status.tv.prompting)
  const TV_FOUND_NONE = "No TVs answered. Check that the TV is on, and on the same network as this table.";

  const tvView = {
    words: "",           // the last refusal or failure in words, under the status line until the next tap
    turningOn: false,    // a Turn on is with the TV (up to 23 s): the remote waits for it
    armed: false,        // Power has had its first tap and waits for the second
    armTimer: null,
    armedAt: 0,          // ... and when it had it (Date.now())
    forgetArmed: false,  // the head's Forget TV has had its first tap and waits for the second
    forgetTimer: null,
    forgetArmedAt: 0,
    inputsOpen: false,   // the row of inputs is where the top keys are
    inputsTimer: null,
    inputsKey: null,
    pairing: false,      // a pairing waits for the person at the TV
    cancelling: false,   // ... and its Cancel is with the agent
    cancelled: false,    // ... and has been given up, so how it ends is no news
    forgetting: false,
    searching: false,    // Find TVs is looking
    choosing: false,
    selecting: null,     // the discovery item while saving and pairing, before status has caught up
    saving: false,
    found: null,         // what the last search found, or null before there was one
    factsKey: null,
    foundKey: null,
  };
  const tvKeyNodes = Array.from(document.querySelectorAll("#tv-remote [data-tv]"));
  const tvBlock = () => (model.status && model.status.tv) || { configured: false, paired: false };
  const tvSupports = (t, feature) => !t.capabilities || t.capabilities[feature] !== false;
  const tvResponding = (t) => Boolean(t.on || (t.reachable && t.power_known === false));
  // The inputs the TV lists, as far as they can be drawn: what a device on the network says is not trusted to have the shape it should.
  const tvInputs = (t) => (Array.isArray(t.inputs) ? t.inputs.filter((i) => i && (typeof i.id === "string" || typeof i.id === "number")) : []);
  let tvVisit = 0;   // counts the times the view has been left: what comes back from an earlier visit has nothing to say to this one
  const tvSay = (visit, words) => { if (visit === tvVisit) tvView.words = words; };

  // What the view shows in the remote's place: "not-set-up", "pairing" or "refused", and "" for the remote. Refused is read with
  // paired: the agent leaves it set after Forget TV, until the next pairing.
  function tvCardOf(t) {
    if (tvView.selecting) return "pairing";
    if (tvView.choosing) return "choose";
    if (!t.configured) return "not-set-up";
    if (!t.paired) return "pairing";
    return t.refused ? "refused" : "";
  }

  // A request to the TV's routes, given up on after `ms`: a TV that does not answer must not leave a key waiting for good.
  async function tvAsk(ms, what, request) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ms);
    try {
      return await request({ signal: controller.signal });
    } catch (e) {
      if (e && e.name === "AbortError") throw new Error(what + " did not answer in time");
      if (e instanceof TypeError) throw new Error("Can't reach the room");   // fetch's own failure: the agent is not there
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }

  // An answer carries the TV's block as it stands after the request, and replaces the model's at once. A poll that was already on its way
  // is older than it, so deviceEpoch tells refreshStatus to keep this block, as it does for the devices.
  function tvTake(answer) {
    if (!answer || !answer.tv || !model.status) return;
    model.status.tv = answer.tv;
    deviceEpoch.tv += 1;
  }

  // The key the TV refused flashes amber once. A key refused twice running flashes twice: the class comes off, and goes on again.
  function tvFlash(node) {
    if (!node) return;
    clearTimeout(node.tvFlashTimer);
    if (node.classList.contains("refused")) {
      node.classList.remove("refused");
      void node.getBoundingClientRect();
    }
    node.classList.add("refused");
    node.tvFlashTimer = setTimeout(() => node.classList.remove("refused"), TV_FLASH_MS);
  }

  // One control: {"name", "value"?}. The TV's answer is its block after the control, which replaces the model's at once. A refusal comes
  // back in words for the status line, and the key that asked flashes.
  async function tvControl(name, value, source) {
    const body = value === undefined ? { name: name } : { name: name, value: value };
    const visit = tvVisit;
    tvView.words = "";
    render();
    try {
      tvTake(await tvAsk(TV_CONTROL_MS, "The TV", (extra) => post("/api/tv/control", body, extra)));
    } catch (e) {
      tvSay(visit, e.message);
      tvFlash(source);
    }
    render();
  }

  // Turn on takes a while (the agent wakes the TV and waits for it), and every control behind it waits too: the remote's keys wait with it.
  async function tvTurnOn() {
    if (tvView.turningOn) return;
    tvView.turningOn = true;
    try {
      await tvControl("power", "on", el("tv-power"));
    } finally {
      tvView.turningOn = false;
      render();
    }
  }

  function tvDisarm() {
    tvView.armed = false;
    clearTimeout(tvView.armTimer);
  }

  // A TV that is on is turned off by two taps on Power, the second after half a second (CONFIRM_GUARD_MS) and within five seconds, as Leave is
  // (without the half second); a TV that is not on is turned on by one.
  function tvPower() {
    if (!tvResponding(tvBlock())) { tvTurnOn(); return; }
    if (!tvView.armed) {
      tvView.armed = true;
      tvView.armedAt = Date.now();
      clearTimeout(tvView.armTimer);
      tvView.armTimer = setTimeout(() => { tvView.armed = false; render(); }, CONFIRM_MS);
      render();
      return;
    }
    if (Date.now() - tvView.armedAt < CONFIRM_GUARD_MS) return;   // the first touch again, not the second tap
    tvDisarm();
    tvControl("power", "off", el("tv-power"));
  }

  function tvOpenInputs() {
    tvView.inputsOpen = true;
    clearTimeout(tvView.inputsTimer);
    tvView.inputsTimer = setTimeout(() => { tvCloseInputs(); render(); }, TV_INPUTS_MS);
    render();
  }
  function tvCloseInputs() {
    tvView.inputsOpen = false;
    clearTimeout(tvView.inputsTimer);
  }
  function tvPickInput(id) {
    tvCloseInputs();
    tvControl("input", id, el("tv-input"));
  }

  // A key of the remote: its data-tv is "name:value", or the name alone. Power's tap and Input's are the table's own business.
  function tvKey(node) {
    if (node.matches("button") ? node.disabled : node.getAttribute("aria-disabled") === "true") return;
    if (node.closest("#tv-dpad") && el("tv-dpad").dataset.mode === "pointer") return;   // the disc is the trackpad: its sectors and OK take no press
    const spec = node.dataset.tv;
    const cut = spec.indexOf(":");
    const name = cut < 0 ? spec : spec.slice(0, cut);
    const value = cut < 0 ? undefined : spec.slice(cut + 1);
    if (name === "power") tvPower();
    else if (name === "input" && value === "menu") {
      if (tvSupports(tvBlock(), "direct_inputs")) tvOpenInputs();
      else tvControl("key", "INPUT_HUB", node);
    }
    else if (name === "mute" && !tvSupports(tvBlock(), "mute_state")) tvControl("key", "MUTE", node);
    else tvControl(name, value, node);
  }

  // Pair: the TV shows its prompt and a person accepts it with the TV's own remote, so the request waits for them. Cancel is the agent's
  // Forget, which ends a waiting pairing at once; the pairing's own answer then says it was stopped, which is no news to the one who stopped it.
  async function tvPair(selection, mac) {
    if (tvView.pairing || tvView.saving || tvView.forgetting) return;
    // click listeners also pass an Event; only discovery items select a TV.
    const selected = selection && typeof selection.host === "string" ? selection : null;
    const visit = tvVisit;
    tvView.pairing = true;
    tvView.selecting = selected;
    tvView.choosing = false;
    tvView.cancelled = false;
    tvView.words = "";
    render();
    try {
      const body = selected ? { host: selected.host } : {};
      if (selected && mac) body.mac = mac;
      const answer = await tvAsk(TV_PAIR_MS, "The TV", (extra) => post(selected ? "/api/tv/select" : "/api/tv/pair", body, extra));
      if (!tvView.cancelled) tvTake(answer);
      if (selected && !tvView.cancelled) tvView.found = null;
    } catch (e) {
      if (!tvView.cancelled) tvSay(visit, e.message);
      if (selected) await refreshStatus();   // a declined pairing still leaves the selection saved
    } finally {
      tvView.pairing = false;
      tvView.selecting = null;
      tvView.cancelling = false;
    }
    render();
  }

  async function tvCancelPair() {
    if (!tvView.pairing || tvView.cancelling) return;
    const visit = tvVisit;
    tvView.cancelling = true;
    tvView.cancelled = true;
    render();
    try {
      tvTake(await tvAsk(TV_CONTROL_MS, "The TV", (extra) => post("/api/tv/forget", {}, extra)));
    } catch (e) {
      tvView.cancelled = false;   // it did not end: the pairing is still waiting
      tvSay(visit, e.message);
    }
    tvView.cancelling = false;
    render();
  }

  // Forget TV: the agent lets go of the TV and deletes its key. A pairing that is waiting for the person at the TV ends with it, and says
  // in words that it was stopped, which is no news to the one who did the stopping.
  async function tvForget(source) {
    if (tvView.forgetting) return;
    const visit = tvVisit;
    const ending = tvView.pairing;
    tvView.forgetting = true;
    if (ending) tvView.cancelled = true;
    tvView.words = "";
    render();
    try {
      tvTake(await tvAsk(TV_CONTROL_MS, "The TV", (extra) => post("/api/tv/forget", {}, extra)));
    } catch (e) {
      if (ending) tvView.cancelled = false;   // it did not end: the pairing is still waiting
      tvSay(visit, e.message);
      tvFlash(source);
    }
    tvView.forgetting = false;
    render();
  }

  function tvDisarmForget() {
    tvView.forgetArmed = false;
    clearTimeout(tvView.forgetTimer);
  }

  // The head's Forget TV asks for a second tap, after half a second and within five seconds, as Power does to turn the TV off: forgetting
  // costs the table its TV until the TV's own remote has accepted a new pairing.
  function tvForgetTap() {
    if (el("tv-forget-head").disabled) return;
    if (!tvView.forgetArmed) {
      tvView.forgetArmed = true;
      tvView.forgetArmedAt = Date.now();
      clearTimeout(tvView.forgetTimer);
      tvView.forgetTimer = setTimeout(() => { tvView.forgetArmed = false; render(); }, CONFIRM_MS);
      render();
      return;
    }
    if (Date.now() - tvView.forgetArmedAt < CONFIRM_GUARD_MS) return;   // the first touch again, not the second tap
    tvDisarmForget();
    tvForget(el("tv-forget-head"));
  }

  async function tvFind() {
    if (tvView.searching || tvView.pairing || tvView.saving) return;
    const visit = tvVisit;
    tvView.searching = true;
    tvView.words = "";
    render();
    try {
      const answer = await tvAsk(TV_SEARCH_MS, "The search for TVs", (extra) => api("/api/tv/discover", extra));
      if (visit === tvVisit) tvView.found = Array.isArray(answer.tvs) ? answer.tvs : [];
    } catch (e) {
      if (visit === tvVisit) tvView.found = null;
      tvSay(visit, e.message);
    }
    tvView.searching = false;
    render();
  }

  function tvChoose() {
    if (tvView.pairing || tvView.turningOn || tvView.forgetting) return;
    const t = tvBlock();
    tvView.choosing = true;
    el("tv-settings-mac").value = t.mac || "";
    const select = el("tv-settings-input");
    const input = t.pi_input || "HDMI_1";
    if (!Array.from(select.options).some((o) => o.value === input)) select.add(new Option(input, input));
    select.value = input;
    el("tv-settings-result").textContent = "";
    tvFind();
  }

  async function tvSaveSettings(event) {
    event.preventDefault();
    if (tvView.saving || tvView.pairing) return;
    tvView.saving = true;
    el("tv-settings-result").textContent = "";
    render();
    try {
      tvTake(await tvAsk(TV_CONTROL_MS, "The TV", (extra) => post("/api/tv/settings", {
        mac: el("tv-settings-mac").value.trim(), pi_input: el("tv-settings-input").value,
      }, extra)));
      el("tv-settings-result").textContent = "Saved";
    } catch (e) {
      el("tv-settings-result").textContent = e.message;
    } finally {
      tvView.saving = false;
      render();
    }
  }

  // --- the pointer modes (spec section 5.2) ---
  // Pointer and Room Pi mouse turn the disc's place, the whole middle of the remote, into a trackpad for the TV's cursor or the room Pi's
  // mouse (touchpad.js reads the fingers and carries them over /ws/pointer). A second tap on the mode that is on brings the d-pad back, and a
  // tap on the other moves the trackpad to its target; the keys above and the modes below stay where they are. Full-screen touchpad hands the
  // whole screen to the pointer, on the target of the mode that is on, else the room Pi. The page leaving the view puts both away.
  const pointer = { target: null };   // "tv", "pi", or null while the middle is the d-pad
  const trackpad = T.reader(el("tv-trackpad"), () => pointer.target);
  const TRACKPAD_WORDS = {
    tv: ["Move one finger to move the TV's cursor", "Tap to click \u00B7 Two fingers to scroll \u00B7 Press and hold, then move, to drag"],
    pi: ["Move one finger to move the mouse on the room's screen",
         "Tap to click \u00B7 Two-finger tap to right-click \u00B7 Two fingers to scroll \u00B7 Press and hold, then move, to drag"],
  };
  const TRACKPAD_LOST = "Reconnecting\u2026";   // in the how-line while the socket is lost: a finger moves nothing until it is back
  let pointerWordsTimer = null;

  function stopPointer() {
    trackpad.cancel();   // a drag that is held goes up first
    pointer.target = null;
    T.link.want("disc", false);
  }
  function setPointer(target) {
    trackpad.cancel();   // a drag ends on the target it began on
    pointer.target = pointer.target === target ? null : target;
    T.link.want("disc", pointer.target !== null);
    render();
  }
  // What the surface needs to know of the TV: its name, whether it can be moved, and whether the room has one set up (the words differ).
  const surfaceTv = (t, card) => ({ usable: card === "" && tvSupports(t, "pointer"), pointer: tvSupports(t, "pointer"),
    name: t.name || "", configured: Boolean(t.configured) });
  function openTouchpad() {
    const t = tvBlock();
    T.surface.show({ target: pointer.target || "pi", tv: surfaceTv(t, tvCardOf(t)), opener: el("tv-fullscreen") });
  }

  // What the agent says it cannot do (the TV is not answering, the room Pi's mouse is not allowed) goes where a refused control's words go. A move
  // that cannot be taken comes back for each frame, so the same words in a burst are drawn once, and they go five quiet seconds after the last.
  T.link.onError((words) => {
    if (!pointer.target || T.surface.isOpen()) return;   // (the surface shows what it is told in its own strip)
    clearTimeout(pointerWordsTimer);
    pointerWordsTimer = setTimeout(() => { if (tvView.words === words) { tvView.words = ""; render(); } }, T.WORDS_MS);
    if (tvView.words === words) return;
    tvView.words = words;
    render();
  });
  T.link.watch(drawTrackpadWords);   // the how-line follows the socket at once: not through render(), which stops short while the page's own status poll fails

  // The modes row: under the cards (no TV to point at) it is Full-screen touchpad alone, for the room Pi's mouse.
  function renderTvPointer(card, t) {
    const target = pointer.target;
    el("tv-dpad").dataset.mode = target ? "pointer" : "keys";
    el("tv-middle").hidden = Boolean(target);
    el("tv-trackpad").hidden = !target;
    for (const [id, who] of [["tv-pointer", "tv"], ["tv-pi-mouse", "pi"]]) {
      const b = el(id), on = target === who;
      b.hidden = card !== "" || (who === "tv" && !tvSupports(t, "pointer"));
      b.setAttribute("aria-pressed", String(on));
      b.classList.toggle("filled", on);
      b.classList.toggle("quiet", !on);
    }
    drawTrackpadWords();
    T.surface.sync(surfaceTv(t, card));
  }

  // What the trackpad says: whom it moves and how, or that the socket is lost.
  function drawTrackpadWords() {
    const target = pointer.target;
    if (!target) return;
    const [lead, how] = TRACKPAD_WORDS[target];
    const line = T.link.state() === "down" ? TRACKPAD_LOST : how;
    el("tv-trackpad").dataset.target = target;
    if (el("tv-trackpad-lead").textContent !== lead) el("tv-trackpad-lead").textContent = lead;
    if (el("tv-trackpad-how").textContent !== line) el("tv-trackpad-how").textContent = line;
  }

  // The view is left: nothing of it waits for the next person. (A pairing or a Turn on that is with the agent goes on, and its state stays.)
  function tvLeft() {
    stopPointer();
    T.surface.hide();
    clearTimeout(pointerWordsTimer);
    tvVisit += 1;
    tvDisarm();
    tvDisarmForget();
    tvCloseInputs();
    tvView.words = "";
    tvView.found = null;
    tvView.choosing = false;
  }

  // --- drawing the view from the status (keyed, so that a button is never replaced under a finger) ---

  // Where the TV is: the input it shows (the room Pi's says so), else the app, and the channel it is on.
  function tvWhere(t) {
    const listed = tvInputs(t).find((i) => i.id === t.input);
    let where = "";
    if (t.input) where = (listed && listed.label) || t.input;
    else if (t.app) where = TV_APPS[t.app] || "an app";
    if (where && t.input && t.input === t.pi_input) where += " (" + TV_PI + ")";
    return where;
  }

  // The status line: "Boardroom TV \u00B7 On \u00B7 HDMI 1 (Crystal Meet)". A TV that is not on has Turn on in place of its state, unless it is
  // asking this table to pair again: it has forgotten the table, and the line says what to do about it (Forget TV is on the head row).
  function renderTvStatus(t, card) {
    const parts = [["name", t.name || (card === "pairing" && t.host ? "TV at " + t.host : "TV")]];
    let turnOn = false;
    if (card === "not-set-up") parts.push(["text", "Not set up"]);
    else if (card === "choose") parts.push(["text", "Choose a TV"]);
    else if (card === "pairing") parts.push(["text", tvView.pairing ? "Pairing\u2026" : "Not paired yet"]);
    else if (card === "refused") parts.push(["text", tvView.pairing ? "Pairing\u2026" : "Not accepting this table"]);
    else if (tvView.turningOn) parts.push(["text", "Turning on\u2026"]);
    else if (t.reachable && t.power_known === false) {
      parts.push(["text", "Power state unavailable"]);
      turnOn = Boolean(t.mac);
    }
    else if (t.on) {
      parts.push(["on", "On"]);
      const where = tvWhere(t);
      if (where) parts.push(["text", where]);
      if (t.channel) parts.push(["text", "Channel " + t.channel]);
    } else if (t.prompting) parts.push(["text", TV_ASKING]);
    else if (t.mac === "") parts.push(["text", "Wake unavailable \u00B7 Add a MAC address in Change TV"]);
    else turnOn = true;
    if (card === "" && pointer.target === "tv") parts.push(["text", "Pointer on"]);
    const key = JSON.stringify([parts, turnOn]);
    if (key !== tvView.factsKey) {
      tvView.factsKey = key;
      const nodes = [];
      const separator = () => { const s = document.createElement("span"); s.className = "tv-sep"; s.setAttribute("aria-hidden", "true"); s.textContent = " \u00B7 "; return s; };
      parts.forEach(([kind, text], i) => {
        if (i) nodes.push(separator());
        const node = document.createElement(kind === "name" ? "strong" : "span");
        if (kind === "on") {
          node.className = "tv-on";
          const dot = document.createElement("i");
          dot.className = "tv-dot";
          node.append(dot);
        }
        node.append(text);
        nodes.push(node);
      });
      if (turnOn) nodes.push(separator());
      el("tv-facts").replaceChildren(...nodes);
    }
    el("tv-turn-on").hidden = !turnOn;
    el("tv-words").hidden = !tvView.words;
    if (el("tv-words").textContent !== tvView.words) el("tv-words").textContent = tvView.words;
  }

  // The remote's keys. A TV that is not on can only be turned on, and one that is turning on is not asked anything: every key waits.
  function renderTvKeys(t, live) {
    const off = !tvResponding(t) && !tvView.turningOn;
    for (const node of tvKeyNodes) {
      const spec = node.dataset.tv;
      const keys = t.capabilities && t.capabilities.supported_keys;
      const supported = !spec.startsWith("key:") || !Array.isArray(keys) || keys.includes(spec.slice(4));
      const allowed = supported && (live || (off && node.id === "tv-power" && t.mac !== ""));
      if (node.matches("button")) node.disabled = !allowed;
      else node.setAttribute("aria-disabled", allowed ? "false" : "true");
    }
    const armed = tvView.armed && live;
    const power = el("tv-power");
    labelWithIcon(power, armed ? "Tap again to turn off" : "Power", armed ? "" : "power");
    power.classList.toggle("danger", armed);
    power.classList.toggle("quiet", !armed && tvResponding(t));
    power.classList.toggle("filled", !tvResponding(t));   // the one filled key, while the TV is not on (and while it is turning on)
    // Mute follows the speaker's button on the meeting screen: the picture is the TV's sound (a speaker while it is live, a crossed one
    // while it is muted) and the words are what a tap does. The key is as wide as "Unmute" in both states (style.css), so nothing moves;
    // a sound that is off also shows as a key that is down, as the input showing does.
    const mute = el("tv-mute");
    const knowsMute = tvSupports(t, "mute_state");
    const muted = knowsMute && Boolean(t.muted);
    labelWithIcon(mute, muted ? "Unmute" : "Mute", muted ? "volume-x" : "volume");
    if (knowsMute) mute.setAttribute("aria-pressed", muted ? "true" : "false");
    else mute.removeAttribute("aria-pressed");
    mute.classList.toggle("current", muted);
    const level = t.volume === null || t.volume === undefined ? "\u2013" : String(t.volume);
    if (el("tv-level").textContent !== level) el("tv-level").textContent = level;
    el("tv-level").classList.toggle("muted", muted);
  }

  // A pick's check: its picture goes on and off in place.
  function tvSetCheck(b, on) {
    const picture = b.querySelector("svg.icon");
    if (on && !picture) b.prepend(icon("check"));
    else if (!on && picture) picture.remove();
  }

  // The row of inputs takes the top keys' place while it is open. The one the TV shows carries a check; the room Pi's says whose it is.
  // The picks are drawn again only when the list itself changes. When only the input showing changes (somebody used the TV's own remote)
  // the marks move in place, so that a finger that is down on a pick finds it still there at the lift.
  function renderTvInputs(t, live) {
    const open = tvView.inputsOpen && live && tvSupports(t, "direct_inputs");
    el("tv-inputs").hidden = !open;
    el("tv-keys").hidden = open;
    el("tv-input").setAttribute("aria-expanded", open ? "true" : "false");
    if (!open) return;
    const inputs = tvInputs(t);
    const key = JSON.stringify([inputs, t.pi_input, model.busy]);
    if (key !== tvView.inputsKey) {
      tvView.inputsKey = key;
      const none = document.createElement("span");
      none.className = "tv-label";
      none.textContent = "The TV lists no inputs";
      el("tv-input-list").replaceChildren(...(inputs.length ? inputs.map((input) => {
        const pi = input.id === t.pi_input;
        const b = button(String(input.label || input.id), "quiet" + (pi ? " pi" : ""), () => tvPickInput(input.id), false, null);
        b.dataset.input = String(input.id);
        if (pi) {
          const sub = document.createElement("span");
          sub.className = "tv-sub";
          sub.textContent = TV_PI;
          b.querySelector("span").append(" ", sub);
        }
        return b;
      }) : [none]));
    }
    for (const b of el("tv-input-list").querySelectorAll("button[data-input]")) {
      const current = t.input !== null && t.input !== undefined && b.dataset.input === String(t.input);
      b.classList.toggle("current", current);
      if (b.getAttribute("aria-pressed") !== String(current)) b.setAttribute("aria-pressed", String(current));
      tvSetCheck(b, current);
    }
  }

  // Discovery text is untrusted. Connect sends only its cached host; optional MAC text is validated by the server.
  function renderTvFound(thisRoom) {
    const key = JSON.stringify([tvView.found, thisRoom]);
    const changed = key !== tvView.foundKey;
    tvView.foundKey = key;
    if (changed) for (const box of document.querySelectorAll(".tv-found")) {
      const found = tvView.found;
      box.hidden = found === null;
      if (found === null) continue;
      const mine = box.closest("#tv-not-set-up") ? "" : thisRoom;
      box.querySelector(".tv-found-label").textContent = found.length ? "Found on the network" : TV_FOUND_NONE;
      const list = box.querySelector(".tv-found-list");
      list.hidden = found.length === 0;
      list.replaceChildren(...found.filter((tv) => tv && typeof tv.host === "string" && tv.host).map((tv) => {
        const li = document.createElement("li");
        const who = document.createElement("span");
        who.className = "tv-who";
        who.append((tv.name || (tv.brand === "samsung" ? "Samsung TV" : "LG TV")) + " ");
        if (mine && tv.host === mine) {
          const tag = document.createElement("span");
          tag.className = "tv-tag";
          tag.textContent = "This room's TV";
          who.append(tag);
        }
        const address = document.createElement("span");
        address.className = "tv-addr";
        address.textContent = tv.host || "";
        who.append(address);
        let mac = null;
        if (!tv.mac) {
          const details = document.createElement("details");
          details.className = "tv-mac";
          const summary = document.createElement("summary");
          summary.textContent = "Wake unavailable \u00B7 Add MAC";
          const label = document.createElement("label");
          label.append("MAC address (optional)");
          mac = document.createElement("input");
          mac.type = "text";
          mac.dataset.keyboard = "mac";
          mac.autocomplete = "off";
          mac.spellcheck = false;
          mac.placeholder = "AA:BB:CC:DD:EE:FF";
          label.append(mac);
          details.append(summary, label);
          who.append(details);
        }
        const connect = button("Connect", "quiet", () => tvPair(tv, mac ? mac.value.trim() : ""), false, null);
        connect.classList.add("tv-connect");
        li.append(who, connect);
        return li;
      }));
    }
    for (const b of document.querySelectorAll(".tv-connect")) b.disabled = tvView.pairing || tvView.searching || tvView.saving;
    for (const box of document.querySelectorAll(".tv-found")) box.hidden = tvView.found === null || tvView.pairing;
  }

  function renderTvCard(t, card) {
    if (card === "pairing") {
      const text = tvView.pairing ? TV_PAIR_WAIT :
        tvView.found && tvView.found.length ? "Tap Pair for this TV, or Connect beside another TV below." :
        "This room is set up for the TV at " + (t.host || "its address") + ". Tap Pair, then accept the request that appears on the TV, using the TV's own remote. You only do this once.";
      if (el("tv-pairing-text").textContent !== text) el("tv-pairing-text").textContent = text;
      el("tv-pair").hidden = tvView.pairing;
      el("tv-find").hidden = tvView.pairing;
      el("tv-pair-cancel").hidden = !tvView.pairing;
      el("tv-pair-cancel").disabled = tvView.cancelling;
    }
    if (card === "refused") {
      const waiting = tvView.pairing;
      const text = waiting ? TV_PAIR_WAIT : TV_REFUSED_TEXT;
      if (el("tv-refused-text").textContent !== text) el("tv-refused-text").textContent = text;
      el("tv-refused-more").hidden = waiting;
      el("tv-refused-pair").hidden = waiting;
      el("tv-refused-cancel").hidden = !waiting;
      el("tv-refused-cancel").disabled = tvView.cancelling;
      el("tv-forget").disabled = tvView.forgetting;   // (Forget TV still ends the wait: it is what Cancel does)
    }
    for (const b of document.querySelectorAll(".tv-find")) {
      labelWithIcon(b, tvView.searching ? "Looking\u2026" : "Find TVs", "locate");
      b.disabled = tvView.searching || tvView.pairing || tvView.saving;
    }
    el("tv-settings-save").disabled = tvView.saving;
    el("tv-settings-mac").disabled = tvView.saving;
    el("tv-settings-input").disabled = tvView.saving;
    el("tv-choose-done").disabled = tvView.saving;
    renderTvFound(t.host || "");
  }

  // The head's Forget TV is on the remote only; the cards that come before a pairing have their own words and buttons for it.
  function renderTvForget(card) {
    const b = el("tv-forget-head");
    b.hidden = card !== "";
    if (card !== "") { tvDisarmForget(); return; }
    const armed = tvView.forgetArmed;
    labelWithIcon(b, armed ? "Tap again to forget the TV" : "Forget TV", armed ? "" : "x-circle");
    b.classList.toggle("danger", armed);
    b.classList.toggle("quiet", !armed);
    b.disabled = tvView.forgetting || tvView.turningOn || tvView.pairing;
  }

  function renderTv() {
    if (view() !== "tv") return;
    const t = tvView.selecting ? { ...tvBlock(), ...tvView.selecting, configured: true, paired: false } : tvBlock();
    const card = tvCardOf(t);
    const on = card === "" && tvResponding(t);
    const live = on && !tvView.turningOn;
    if (!on) tvDisarm();             // a Power waiting for its second tap, and the row of inputs, go when the TV is not on
    if (!live) tvCloseInputs();
    el("tv-remote").hidden = card !== "";
    el("tv-not-set-up").hidden = card !== "not-set-up";
    el("tv-pairing").hidden = card !== "pairing";
    el("tv-refused").hidden = card !== "refused";
    el("tv-choose").hidden = card !== "choose";
    el("tv-change").hidden = !t.configured || tvView.pairing || card === "choose";
    el("tv-change").disabled = tvView.turningOn || tvView.forgetting;
    if ((card !== "" || (pointer.target === "tv" && !tvSupports(t, "pointer"))) && pointer.target) stopPointer();
    renderTvStatus(t, card);
    renderTvPointer(card, t);
    renderTvForget(card);
    if (card === "") {
      renderTvKeys(t, live);
      renderTvInputs(t, live);
    } else {
      renderTvCard(t, card);
    }
  }

  for (const node of tvKeyNodes) {
    keepFocus(node);
    node.addEventListener("click", () => tvKey(node));
    if (node.getAttribute("role") === "button") {   // the d-pad's sectors are not buttons to the browser: Enter and Space press them
      node.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); tvKey(node); } });
    }
  }
  el("tv-remote").addEventListener("contextmenu", (e) => e.preventDefault());   // a long press on a touch screen must not open a menu, as on the camera's pad
  keepFocus(el("tv-turn-on"));
  el("tv-turn-on").addEventListener("click", tvTurnOn);
  el("tv-inputs-close").addEventListener("click", () => { tvCloseInputs(); render(); });
  for (const b of document.querySelectorAll(".tv-find")) b.addEventListener("click", tvFind);
  el("tv-pair").addEventListener("click", tvPair);
  el("tv-refused-pair").addEventListener("click", tvPair);
  el("tv-pair-cancel").addEventListener("click", tvCancelPair);
  el("tv-refused-cancel").addEventListener("click", tvCancelPair);
  el("tv-forget").addEventListener("click", () => tvForget(el("tv-forget")));
  keepFocus(el("tv-forget-head"));
  el("tv-forget-head").addEventListener("click", tvForgetTap);
  el("tv-pointer").addEventListener("click", () => setPointer("tv"));
  el("tv-pi-mouse").addEventListener("click", () => setPointer("pi"));
  el("tv-fullscreen").addEventListener("click", openTouchpad);
  el("tv-change").addEventListener("click", tvChoose);
  el("tv-choose-done").addEventListener("click", () => { tvView.choosing = false; render(); });
  el("tv-settings-form").addEventListener("submit", tvSaveSettings);
  P.onViewChange((name) => {
    if (name === "tv") refreshStatus();   // the TV as it is now, not as of the last poll
    else tvLeft();
  });
  P.onRender(renderTv);   // after every status the page draws (and once now, for a status that came before this script did)
  // The meeting screen's Touchpad (app.js): the full-screen touchpad on a target, with the TV as the remote knows it; Done gives the focus back to
  // the button that opened it.
  window.croomRemote = { openTouchpad: (target, opener) => { const t = tvBlock(); T.surface.show({ target: target, tv: surfaceTv(t, tvCardOf(t)), opener: opener }); } };
})();
