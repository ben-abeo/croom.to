// The Settings pages that edit the room's config (spec 2026-10-29, sections 3 and 4). The agent gives a page its values and fields
// (GET /api/settings/config/<page>); this file draws a form from the fields, one row each: text (with the page's own keyboard on the
// kiosk), a number, a switch, a choice, a list, a time, a secret (never shown, only whether one is set) and a key file. Save sends what
// changed; a value the agent refuses says why beside its field and nothing is saved. A setting the room applies at once says "Saved";
// one that waits for the room software to restart says "Saved: applies after a restart" and the page offers Restart now. When the last
// restart put the settings before a change back, the page says so at its top.
//
// A page is drawn with croomSettings.form(host, key, options); the Room page is one, and settings-pages.js draws the others with it.
// options.after(box) fills a box under the form (the Laptop switch's Test the switch). Later pages may add kinds of field to
// croomSettings.fieldKinds (kind -> function (field, value, page) giving { node, read, readOnly?, focus? }); `page` lets a field that
// saves on its own (a key file) say so as the form does: page.saved(answer) and page.say(words, ok).
(function () {
  "use strict";
  const S = window.croomSettings;
  const P = window.croomPage;

  const SAVED = "Saved";
  const SAVED_RESTART = "Saved: applies after a restart";
  const RESTART_NOW = "Restart now";
  const RESTARTING = "Restarting the room software\u2026";
  const BACK = "The room software has restarted";
  const NOT_BACK = "The room software has not come back yet";
  const NOT_RESTARTED = "The room software did not restart";
  const RETURN_POLL_MS = 2000, RETURN_WAIT_MS = 5 * 60 * 1000;   // the helper's watch is 2 minutes, and an undo restarts it again
  const UNDONE = "Your last change was undone: the room did not start with it";
  const NOTHING_CHANGED = "Nothing to save: nothing has changed";

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  // A press on one of the form's buttons leaves the focus where it is: on the kiosk the keys go when a field loses it, the page
  // shrinks, and the button would move from under the finger before it lifts.
  function button(label, className) {
    const b = make("button", "button " + (className || "quiet"), label);
    b.type = "button";
    P.keepFocus(b);
    return b;
  }
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

  // A field that types on the page's keyboard on the kiosk (keyboard.js comes up for input[data-keyboard]); a laptop types as usual.
  function textInput(field, value, type) {
    const input = make("input", "setting-input");
    input.type = type || "text";
    input.dataset.keyboard = "setting";
    input.autocomplete = "off";
    input.spellcheck = false;
    if (field.max_length) input.maxLength = field.max_length;
    input.value = value === null || value === undefined ? "" : String(value);
    return input;
  }

  const kinds = {
    text: (field, value) => {
      const input = textInput(field, value);
      return { node: input, read: () => input.value.trim(), focus: input };
    },
    // Never shown again: the field is empty, and says whether one is set. Left empty, the secret stays as it is.
    secret: (field) => {
      const input = textInput(field, "", "password");
      input.placeholder = field.set ? "Set: type a new one to replace it" : "Not set";
      return { node: input, read: () => input.value.trim(), focus: input };
    },
    // A text box rather than type=number, so that the page's keyboard can type in it; what is not a number goes as it is, for the
    // agent to refuse in words.
    number: (field, value) => {
      const input = textInput(field, value);
      input.inputMode = "decimal";
      input.classList.add("setting-number");
      return {
        node: input, focus: input,
        read: () => {
          const text = input.value.trim();
          const n = Number(text);
          return text !== "" && Number.isFinite(n) ? n : text;
        },
      };
    },
    switch: (field, value) => {
      const b = button(value ? "On" : "Off", "quiet setting-switch");
      b.setAttribute("role", "switch");
      let on = Boolean(value);
      const draw = () => { b.setAttribute("aria-checked", String(on)); b.textContent = on ? "On" : "Off"; };
      b.addEventListener("click", () => { on = !on; draw(); });
      draw();
      return { node: b, read: () => on, focus: b };
    },
    choice: (field, value) => {
      const select = make("select", "setting-input");
      for (const c of field.choices || []) {
        const option = make("option", "", c.label);
        option.value = c.value;
        select.append(option);
      }
      if (!(field.choices || []).some((c) => c.value === value)) {   // a value the file has that is not offered: shown, to be put right
        const option = make("option", "", String(value));
        option.value = String(value);
        select.prepend(option);
      }
      select.value = String(value);
      return { node: select, read: () => select.value, focus: select };
    },
    // On the quarter hour (or the field's step): a list of the times, not a clock face the kiosk cannot type in.
    time: (field, value) => {
      const select = make("select", "setting-input setting-time");
      const step = field.step || 15;
      for (let m = 0; m < 24 * 60; m += step) {
        const text = String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0");
        const option = make("option", "", text);
        option.value = text;
        select.append(option);
      }
      if (![...select.options].some((o) => o.value === value)) {
        const option = make("option", "", String(value));
        option.value = String(value);
        select.prepend(option);
      }
      select.value = String(value);
      return { node: select, read: () => select.value, focus: select };
    },
    // A list with choices is a button for each, pressed or not, in the choices' order; a free list is a line for each item, with
    // Remove, and Add for another while there is room.
    list: (field, value) => {
      const items = Array.isArray(value) ? value.slice() : [];
      if (field.choices) {
        const box = make("div", "setting-choices");
        const buttons = field.choices.map((c) => {
          const b = button(c.label, "quiet setting-choice");
          b.dataset.value = c.value;
          b.setAttribute("aria-pressed", String(items.includes(c.value)));
          b.addEventListener("click", () => b.setAttribute("aria-pressed", String(b.getAttribute("aria-pressed") !== "true")));
          return b;
        });
        box.append(...buttons);
        return {
          node: box, focus: buttons[0],
          read: () => buttons.filter((b) => b.getAttribute("aria-pressed") === "true").map((b) => b.dataset.value),
        };
      }
      const box = make("div", "setting-list");
      const lines = make("div", "setting-lines");
      const add = button("Add", "quiet setting-add");
      const max = field.max_items || 20;
      function line(text) {
        const row = make("div", "setting-line");
        const input = textInput(field, text);
        const remove = button("Remove", "quiet setting-remove");
        remove.setAttribute("aria-label", "Remove " + (text || "this line"));
        remove.addEventListener("click", () => { row.remove(); room(); });
        row.append(input, remove);
        lines.append(row);
        return input;
      }
      function room() { add.disabled = lines.children.length >= max; }
      items.forEach((text) => line(text));
      add.addEventListener("click", () => { if (lines.children.length < max) { line("").focus(); room(); } });
      room();
      box.append(lines, add);
      return {
        node: box, focus: add,
        read: () => [...lines.querySelectorAll("input")].map((i) => i.value.trim()).filter((t) => t),
      };
    },
    // A key file's place: what the page shows of it until the credential pages draw their own (Upload, From USB stick).
    credential: (field, value) => {
      const note = make("p", "note setting-credential", value ? "Saved" : "Not set");
      return { node: note, read: () => value, readOnly: true };
    },
  };

  // Draw the form of page `key` into `host`. Gives back the function that ends it (a page left while its values are on their way is
  // not drawn into afterwards).
  function form(host, key, options) {
    let gone = false;
    host.replaceChildren();
    const root = make("div", "settings-form");
    root.dataset.form = key;
    const banner = make("p", "settings-banner", UNDONE);
    banner.setAttribute("role", "status");
    banner.hidden = true;
    const loading = make("p", "note settings-loading", "Loading");
    root.append(banner, loading);
    host.append(root);
    if (options && options.after) {          // drawn whether or not the form can be: it does not need the root helper
      const after = make("div", "settings-form settings-after");
      host.append(after);
      options.after(after, () => gone);
    }

    (async () => {
      let data;
      try {
        data = await S.api("/api/settings/config/" + encodeURIComponent(key));
      } catch (e) {
        if (gone || e.locked) return;
        loading.textContent = e.message;
        loading.classList.add("settings-unavailable");
        return;
      }
      if (gone) return;
      loading.remove();
      draw(data);
      showUndone();
    })();

    async function showUndone() {
      try {
        const status = await S.api("/api/settings/restart");
        if (!gone && status.undone) {
          banner.hidden = false;
          if (status.why) banner.title = status.why;
        }
      } catch (e) { /* no record to show */ }
    }

    function draw(data) {
      const rows = new Map();
      const original = {};
      const list = make("div", "setting-rows");
      // What a field that saves on its own (a key file) tells the page: said as a save of the form is, and Restart now offered.
      const page = {
        saved: (answer) => { if (gone) return; say(answer.restart ? SAVED_RESTART : SAVED, true); restart.hidden = !answer.pending_restart; },
        say: (words, ok) => { if (!gone) say(words, ok); },
        gone: () => gone,
      };
      for (const field of data.fields) {
        const draw = kinds[field.kind] || kinds.text;
        const value = data.values[field.path];
        const control = draw(field, value, page);
        const row = make("div", "setting-row");
        row.dataset.path = field.path;
        const id = "setting-" + field.path.replace(/[^a-z0-9]+/gi, "-");
        const label = make("label", "setting-label", field.label);
        const body = make("div", "setting-body");
        control.node.id = id;
        if (control.node.matches("input, select")) label.htmlFor = id;
        else control.node.setAttribute("aria-label", field.label);
        body.append(control.node);
        if (field.help) body.append(make("p", "note setting-help", field.help));
        const error = make("p", "setting-error");
        error.setAttribute("role", "alert");
        body.append(error);
        row.append(label, body);
        list.append(row);
        rows.set(field.path, { field: field, control: control, error: error, row: row });
        original[field.path] = control.readOnly ? value : control.read();
      }
      const actions = make("div", "settings-actions");
      const save = button("Save", "primary settings-save");
      const restart = button(RESTART_NOW, "quiet settings-restart");
      restart.hidden = !data.pending_restart;
      const message = make("p", "message settings-message");
      message.setAttribute("role", "status");
      actions.append(save, restart, message);
      root.append(list, actions);
      const say = (words, ok) => { message.textContent = words; message.classList.toggle("ok", Boolean(ok)); };

      function clearErrors() {
        for (const r of rows.values()) { r.error.textContent = ""; r.row.classList.remove("refused"); }
      }

      save.addEventListener("click", async () => {
        clearErrors();
        const changes = {};
        for (const [path, r] of rows) {
          if (r.control.readOnly) continue;
          const now = r.control.read();
          if (!same(now, original[path]) || (r.field.kind === "secret" && now)) changes[path] = now;
        }
        if (!Object.keys(changes).length) { say(NOTHING_CHANGED, true); return; }
        save.disabled = true;
        say("");
        try {
          const answer = await S.post("/api/settings/config/" + encodeURIComponent(key), { values: changes });
          if (gone) return;
          for (const path of Object.keys(changes)) {
            if (rows.get(path).field.kind === "secret") continue;
            original[path] = changes[path];
          }
          for (const r of rows.values()) {
            if (r.field.kind === "secret" && changes[r.field.path]) { r.control.node.value = ""; r.control.node.placeholder = "Set: type a new one to replace it"; }
          }
          say(answer.restart ? SAVED_RESTART : SAVED, true);
          restart.hidden = !answer.pending_restart;
        } catch (e) {
          if (gone || e.locked) return;
          const errors = (e.data && e.data.errors) || {};
          let placed = false;
          for (const [path, words] of Object.entries(errors)) {
            const r = rows.get(path);
            if (r) { r.error.textContent = words; r.row.classList.add("refused"); placed = true; }
          }
          // A refusal of a setting on another page (a value in the file that is not right) is said here, in full.
          const pages = (e.data && e.data.pages) || {};
          const pageLabel = (path) => { const p = S.PAGES.find((page) => page.key === pages[path]); return p ? p.label + ": " : ""; };
          const elsewhere = Object.entries(errors).filter(([path]) => !rows.has(path)).map(([path, words]) => pageLabel(path) + words);
          say(elsewhere.length ? elsewhere.join(". ") : placed ? "Not saved: see the settings marked" : e.message);
        } finally {
          save.disabled = false;
        }
      });

      // Restart now: the room software goes away and comes back. The page says so, and asks the room whether it answers until it has
      // been away and is back (a request that fails meanwhile is the restart, not an error); then it reads the restart's record, as a
      // page does when it opens: the undo banner, or, since a restarted room has forgotten every unlock, the PIN pad.
      restart.addEventListener("click", async () => {
        restart.disabled = true;
        try {
          await S.post("/api/settings/restart", {});
        } catch (e) {
          restart.disabled = false;
          if (gone || e.locked) return;
          if (e.status) { say(e.message); return; }   // refused in words ("Leave the meeting first")
        }
        if (gone) return;
        say(RESTARTING, true);
        restart.hidden = true;
        restart.disabled = false;
        awaitReturn();
      });

      function awaitReturn() {
        const until = Date.now() + RETURN_WAIT_MS;
        let away = false;
        const look = async () => {
          if (gone) return;
          let up = false;
          try { up = (await fetch("/api/status", { cache: "no-store" })).ok; } catch (e) { up = false; }
          if (gone) return;
          if (!up) away = true;
          else if (away) { say(BACK, true); showUndone(); return; }
          else {
            // Not gone yet: a look at the restart's record keeps this screen unlocked while it waits (the PIN's 5 minutes would end
            // with the wait). A refused token is a room that restarted between the two looks: the PIN pad is up, as it is after one.
            try { await S.api("/api/settings/restart"); } catch (e) { if (e.locked) return; }
            if (gone) return;
          }
          if (Date.now() > until) {
            if (away) { say(NOT_BACK); return; }
            // Never away in all that time: the restart did not happen (the helper's systemctl failed, or it never ran). The button comes
            // back for another go, and the record may say why.
            say(NOT_RESTARTED);
            restart.hidden = false;
            showUndone();
            return;
          }
          setTimeout(look, RETURN_POLL_MS);
        };
        setTimeout(look, RETURN_POLL_MS);
      }
    }

    return () => { gone = true; };
  }

  S.form = form;
  S.fieldKinds = kinds;
  S.define("room", (host) => form(host, "room"));
})();
