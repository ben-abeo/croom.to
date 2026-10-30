// The Settings pages that edit the rest of the room's config (spec 2026-10-29, section 3): Calendar, Meetings, Devices, Laptop switch and
// AI & dashboard, each drawn by settings-form.js from the fields the agent gives (the Devices page's choices are the speakers,
// microphones and cameras the room has now). The TV page is Options > TV (settings.js).
//
// A key file (the Google service-account key on Calendar, the Zoom SDK keys on Meetings) is never shown: its row says what it is and
// when it was saved ("Key for <client_email>, saved <date>"), or what is wrong with the file the config names, and offers Upload (from
// this browser: a laptop's own files), From USB stick (a stick in the TV Pi) and, for Zoom, Type the keys. A key saved applies after a
// restart, as the form says.
//
// The Laptop switch page has Test the switch under its form: the room presses the switch, the MeetUp goes to the laptop side and back,
// and the page shows what the check says.
(function () {
  "use strict";
  const S = window.croomSettings;
  const P = window.croomPage;

  const MAX_KEY_BYTES = 64 * 1024;
  const KIND_OF = { "calendar.google_credentials_path": "google", "meeting.zoom_credentials_path": "zoom" };
  const NOT_SET = "Not set";
  const NO_FILES = "No key files on a USB stick in the TV Pi: put the stick in the TV Pi and press From USB stick again";
  const TOO_LARGE = "This file is too large for a key file: a key is the few-KB .json file the console downloads";
  const TESTING = "Testing the switch… the MeetUp goes to the laptop side and back";

  const fmtDate = (iso) => new Date(iso).toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function button(label, className) {
    const b = make("button", "button " + (className || "quiet"), label);
    b.type = "button";
    P.keepFocus(b);
    return b;
  }
  function keyInput(placeholder, type) {
    const input = make("input", "setting-input");
    input.type = type || "text";
    input.dataset.keyboard = "setting";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.maxLength = 200;
    input.placeholder = placeholder;
    return input;
  }

  // What a key file is, in the row's first line.
  function describe(saved) {
    if (!saved) return NOT_SET;
    if (saved.problem) return saved.problem;
    return saved.summary + (saved.saved_at ? ", saved " + fmtDate(saved.saved_at) : "");
  }

  // --- A key file's row: what it is, and the three ways to give the room a new one.
  S.fieldKinds.credential = (field, value, page) => {
    const kind = KIND_OF[field.path] || "google";
    const box = make("div", "setting-credential");
    box.dataset.kind = kind;
    const summary = make("p", "credential-summary", value ? "" : NOT_SET);
    const actions = make("div", "credential-actions");
    const file = make("input", "visually-hidden credential-file");
    file.type = "file";
    file.accept = ".json,application/json";
    file.tabIndex = -1;
    file.setAttribute("aria-hidden", "true");
    const uploadButton = button("Upload", "quiet credential-upload");
    const usbButton = button("From USB stick", "quiet credential-usb");
    actions.append(uploadButton, usbButton);
    const usbList = make("div", "credential-list");
    usbList.hidden = true;
    const typed = make("div", "credential-typed");
    typed.hidden = true;
    let typedButton = null;
    if (kind === "zoom") {
      typedButton = button("Type the keys", "quiet credential-type");
      actions.append(typedButton);
      const keyField = keyInput("SDK key (Client ID)");
      keyField.classList.add("credential-key");
      keyField.setAttribute("aria-label", "Zoom SDK key");
      const secretField = keyInput("SDK secret (Client secret)", "password");
      secretField.classList.add("credential-secret");
      secretField.setAttribute("aria-label", "Zoom SDK secret");
      const saveKeys = button("Save the keys", "primary credential-save-typed");
      typed.append(keyField, secretField, saveKeys);
      typedButton.addEventListener("click", () => {
        typed.hidden = !typed.hidden;
        usbList.hidden = true;
        if (!typed.hidden) keyField.focus();
      });
      saveKeys.addEventListener("click", () => {
        send(() => S.post("/api/settings/credentials/zoom/typed", { key: keyField.value, secret: secretField.value }), () => {
          keyField.value = "";
          secretField.value = "";
        });
      });
    }
    const message = make("p", "setting-error credential-message");
    message.setAttribute("role", "alert");
    box.append(summary, actions, file, usbList, typed, message);

    const busy = (on) => { for (const b of box.querySelectorAll("button")) b.disabled = on; };

    // One save of a key, by whichever way: the row says what it is now and the page that it applies after a restart; a refusal is said
    // under the row, in the agent's words.
    async function send(request, after) {
      message.textContent = "";
      busy(true);
      try {
        const answer = await request();
        if (page.gone()) return;
        summary.textContent = describe(answer);
        box.classList.remove("credential-problem");
        usbList.hidden = true;
        typed.hidden = true;
        if (after) after();
        page.saved(answer);
      } catch (e) {
        if (page.gone() || e.locked) return;
        const errors = (e.data && e.data.errors) || {};
        message.textContent = Object.values(errors).join(". ") || e.message;
      } finally {
        busy(false);
      }
    }

    uploadButton.addEventListener("click", () => { file.value = ""; file.click(); });
    file.addEventListener("change", () => {
      const chosen = file.files && file.files[0];
      if (!chosen) return;
      if (chosen.size > MAX_KEY_BYTES) { message.textContent = TOO_LARGE; return; }
      const form = new FormData();
      form.append("file", chosen, chosen.name);
      send(() => S.upload("/api/settings/credentials/" + kind, form));
    });

    usbButton.addEventListener("click", async () => {
      message.textContent = "";
      typed.hidden = true;
      usbList.hidden = false;
      usbList.replaceChildren(make("p", "note", "Looking at the USB sticks…"));
      let listing;
      try {
        listing = await S.api("/api/settings/usb-files");
      } catch (e) {
        if (page.gone() || e.locked) return;
        usbList.replaceChildren(make("p", "note", e.message));
        return;
      }
      if (page.gone()) return;
      const files = listing.files || [];
      if (!files.length) { usbList.replaceChildren(make("p", "note", NO_FILES)); return; }
      usbList.replaceChildren(...files.map((f) => {
        const b = button(f.name + " · " + f.stick, "quiet credential-pick");
        b.dataset.path = f.path;
        b.addEventListener("click", () => send(() => S.post("/api/settings/credentials/" + kind + "/usb", { path: f.path })));
        return b;
      }));
    });

    // What the room's key is now: read from the agent, never the key itself.
    (async () => {
      try {
        const keys = await S.api("/api/settings/credentials");
        if (page.gone()) return;
        const saved = keys[kind];
        summary.textContent = describe(saved);
        box.classList.toggle("credential-problem", Boolean(saved && saved.problem));
      } catch (e) {
        if (!page.gone() && !e.locked) summary.textContent = value ? value : NOT_SET;
      }
    })();

    return { node: box, read: () => value, readOnly: true, focus: uploadButton };
  };

  // --- Meetings: the room's Google sign-in (spec 2026-10-29, section 3). The row says whom the room is signed in to Google as, and Sign in
  // to Google closes the TV's meeting browser and opens Google's sign-in there: this screen becomes the touchpad (the room Pi's mouse), with
  // Type on the TV (the keyboard, typing on the room Pi), Cancel and Done. Done checks that Google kept the sign-in and says as whom; the
  // TV has its browser back whatever ends the sign-in (Cancel, Done, or the agent after 15 minutes without a touch). A room with no Google
  // profile folder is offered the room's own (it applies after a restart).
  const SIGN_IN = "/api/settings/google/sign-in";
  const SIGN_IN_POLL_MS = 3000;
  const SIGN_IN_TITLE = "Sign in to Google on the TV";
  const SIGN_IN_LINE = "Google's sign-in is on the TV: move the mouse here, and Type on the TV for the address and password";
  const NOT_SIGNED_IN = "Not signed in";
  const SET_UP_FIRST = "Set up Google Meet first: the room keeps its Google sign-in in a folder of its own";

  S.fieldKinds.signin = (field, value, page) => {
    const box = make("div", "setting-signin");
    const status = make("p", "signin-status", "Looking at the sign-in…");
    const actions = make("div", "signin-actions");
    const start = button("Sign in to Google", "quiet signin-start");
    const setUp = button("Use the room's folder", "quiet signin-setup");
    start.hidden = setUp.hidden = true;
    actions.append(start, setUp);
    const message = make("p", "setting-error signin-message");
    message.setAttribute("role", "status");
    box.append(status, actions, message);
    const T = window.croomTouchpad;
    let folder = value || "";
    let offered = "";        // the folder the agent offers to a room that has none
    let poller = null;

    const say = (words, ok) => { message.textContent = words || ""; message.classList.toggle("ok", Boolean(ok)); };

    function drawState(state) {
      offered = state.default_profile_dir || offered;
      start.hidden = setUp.hidden = true;
      if (!state.configured) {
        if (folder) {
          status.textContent = "The room starts using " + folder + " for its Google sign-in after a restart";
        } else {
          status.textContent = SET_UP_FIRST;
          setUp.textContent = "Use " + offered;
          setUp.hidden = !offered;
        }
        return;
      }
      if (state.active) {
        status.textContent = "Signing in on the TV";
        start.textContent = "Back to the sign-in";
      } else {
        status.textContent = state.signed_in ? "Signed in as " + state.address : NOT_SIGNED_IN;
        start.textContent = "Sign in to Google";
      }
      start.hidden = false;
    }

    async function load() {
      try {
        const state = await S.api(SIGN_IN + "/state");
        if (!page.gone()) drawState(state);
      } catch (e) {
        if (!page.gone() && !e.locked) status.textContent = e.message;
      }
    }

    function stopPolling() {
      clearInterval(poller);
      poller = null;
    }
    // The sign-in has ended (Done, Cancel, or by itself): the touchpad goes, and the row says how it ended.
    function closed(words, ok) {
      stopPolling();
      if (T.surface.signingIn()) T.surface.hide();
      if (page.gone()) return;
      if (words) say(words, ok);
      load();
    }
    // While the touchpad is up, the agent is asked how the sign-in is: one that it ended (15 minutes without a touch, the browser closed by
    // itself) closes the touchpad. Each look also keeps Settings unlocked for as long as the sign-in goes on.
    function poll() {
      stopPolling();
      poller = setInterval(async () => {
        if (!T.surface.signingIn()) { stopPolling(); load(); return; }   // put away by something else (a view left)
        try {
          const state = await S.api(SIGN_IN + "/state");
          if (!state.active) closed(state.last && state.last.words, true);
        } catch (e) {
          if (e.locked) closed();
        }
      }, SIGN_IN_POLL_MS);
    }

    async function finish(done) {
      T.surface.update({ busy: true, line: done ? "Checking the sign-in, then the TV's browser comes back…" : "Closing the sign-in…" });
      let answer;
      try {
        answer = await S.post(SIGN_IN + (done ? "/done" : "/cancel"), {});
      } catch (e) {
        if (e.locked) { closed(); return; }
        if (e.status === 409) { closed(e.message); return; }   // ended already (the agent closed it): the row says how
        T.surface.update({ busy: false, line: e.message });
        return;
      }
      closed(answer.words, answer.signed_in !== false);   // (Google kept no sign-in: said as a problem)
    }

    function openTouchpad() {
      T.surface.show({
        tv: { usable: false, name: "", configured: false }, target: "pi", opener: start,
        signIn: { title: SIGN_IN_TITLE, line: SIGN_IN_LINE, busy: false, done: () => finish(true), cancel: () => finish(false), token: S.token },
      });
      poll();
    }

    start.addEventListener("click", async () => {
      say("");
      start.disabled = true;
      status.textContent = "Opening Google's sign-in on the TV…";
      try {
        await S.post(SIGN_IN, {});
      } catch (e) {
        if (page.gone() || e.locked) return;
        say(e.message);
        load();
        return;
      } finally {
        start.disabled = false;
      }
      if (page.gone()) return;
      status.textContent = "Signing in on the TV";
      openTouchpad();
    });

    setUp.addEventListener("click", async () => {
      say("");
      setUp.disabled = true;
      try {
        const answer = await S.post("/api/settings/config/meetings", { values: { "meeting.google_profile_dir": offered } });
        if (page.gone()) return;
        folder = offered;
        page.saved(answer);
        load();
      } catch (e) {
        if (page.gone() || e.locked) return;
        const errors = (e.data && e.data.errors) || {};
        say(Object.values(errors).join(". ") || e.message);
      } finally {
        setUp.disabled = false;
      }
    });

    load();
    return { node: box, read: () => folder, readOnly: true, focus: start };
  };

  // --- Laptop switch: Test the switch, under the form.
  function testTheSwitch(box, gone) {
    const title = make("p", "kicker", "Test the switch");
    const help = make("p", "note setting-help",
      "The room presses the switch: the MeetUp goes to the laptop side and comes back. Not during a meeting.");
    const run = button("Test the switch", "quiet laptop-test");
    const result = make("div", "laptop-test-result");
    result.setAttribute("role", "status");
    box.append(title, help, run, result);
    run.addEventListener("click", async () => {
      run.disabled = true;
      result.classList.remove("ok", "failed");
      result.replaceChildren(make("p", "note", TESTING));
      try {
        const answer = await S.post("/api/settings/laptop/test", {});
        if (gone()) return;
        result.classList.add(answer.ok ? "ok" : "failed");
        result.replaceChildren(make("p", "laptop-test-verdict", answer.ok ? "The switch works" : "The switch did not pass"),
          ...(answer.lines || []).map((line) => make("p", "laptop-test-line", line)));
      } catch (e) {
        if (gone() || e.locked) return;
        result.classList.add("failed");
        result.replaceChildren(make("p", "laptop-test-verdict", e.message));
      } finally {
        run.disabled = false;
      }
    });
  }

  S.define("calendar", (host) => S.form(host, "calendar"));
  S.define("meetings", (host) => S.form(host, "meetings"));
  S.define("devices", (host) => S.form(host, "devices"));
  S.define("laptop", (host) => S.form(host, "laptop", { after: testTheSwitch }));
  S.define("ai", (host) => S.form(host, "ai"));
})();
