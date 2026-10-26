// Book the room and Use now (spec 2026-10-25), the same steps on the door sign and the room page: each page gives the steps a
// place of its own (a host element) and styles them, and this draws them there.
//
// Book: a day (today and the six after it), then one of its open start times, then a name and a length from a list in steps of
// 15 minutes, as long as the time allows. Use now: a name and a length, from this minute. The room keeps what is booked here
// and sends it to Google Calendar, so a room with Google out of reach books all the same, and says so.
//
// Left alone for a minute the steps close; the page decides what shows then. Cancel is the pages' own button, and End now the room
// page's alone, each with a second tap to confirm (confirmButton).
(function () {
  "use strict";

  const IDLE_MS = 60 * 1000;
  const DONE_MS = 4000;          // how long "Booked" stays before the steps close by themselves
  const CONFIRM_MS = 4000;       // how long End now and Cancel wait for their second tap

  const fmtTime = (d) => d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true }).replace(/\s+(?=[AP]M$)/, " ");
  const fmtDay = (d) => d.toLocaleDateString("en-US", { weekday: "long", month: "short", day: "numeric" });
  const dayOf = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d); };
  function fmtLength(minutes) {
    const h = Math.floor(minutes / 60), m = minutes % 60;
    if (!h) return m + " min";
    if (!m) return h === 1 ? "1 hour" : h + " hours";
    return h + " hr " + m + " min";
  }

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  async function call(path, options) {
    const init = Object.assign({ headers: { "Content-Type": "application/json" } }, options || {});
    const response = await fetch(path, init);
    let data = {};
    try { data = await response.json(); } catch (e) { data = {}; }
    if (!response.ok) throw new Error(data.error || "Something went wrong (" + response.status + "). Try again.");
    return data;
  }

  // The steps, in `host`. options: mode ("book" or "now"), source ("door sign" or "room page"), now (the status's booking.now: what
  // Use now offers and until when), onClose(changed) (the steps are done, or given up: changed says a booking was made), and
  // field(input) (the page's hold on the name field, for its own keyboard).
  function open(host, options) {
    const state = { mode: options.mode, days: [], date: null, starts: null, chosen: null, busy: false, closed: false };
    let idleTimer = null;
    let doneTimer = null;

    const root = make("div", "bk");
    root.dataset.mode = state.mode;
    const head = make("div", "bk-head");
    const title = make("h2", "bk-title", state.mode === "now" ? "Use the room now" : "Book the room");
    const back = make("button", "button quiet bk-back", "Back");
    back.type = "button";
    head.append(title, back);
    const days = make("div", "bk-days");
    days.setAttribute("role", "group");
    days.setAttribute("aria-label", "Day");
    const times = make("div", "bk-times");
    times.setAttribute("role", "group");
    times.setAttribute("aria-label", "Start time");
    const note = make("p", "bk-note");
    note.setAttribute("role", "status");

    const form = make("form", "bk-form");
    form.hidden = true;
    const when = make("p", "bk-when");
    const nameLabel = make("label", "bk-label", "Your name or the meeting's name");
    const name = make("input", "bk-name");
    Object.assign(name, { type: "text", maxLength: 60, autocomplete: "off", spellcheck: false });
    name.setAttribute("autocapitalize", "words");
    name.dataset.keyboard = "name";
    nameLabel.append(name);
    const lengthLabel = make("label", "bk-label", "How long");
    const length = make("select", "bk-length");
    lengthLabel.append(length);
    const actions = make("div", "bk-actions");
    const submit = make("button", "button primary bk-submit", state.mode === "now" ? "Start" : "Book");
    submit.type = "submit";
    const change = make("button", "button quiet bk-change", "Change time");
    change.type = "button";
    change.hidden = state.mode === "now";
    actions.append(submit, change);
    const error = make("p", "bk-error");
    error.setAttribute("role", "alert");
    form.append(when, nameLabel, lengthLabel, actions, error);
    const done = make("p", "bk-done");
    done.setAttribute("role", "status");
    done.hidden = true;
    root.append(head, days, times, note, form, done);
    host.replaceChildren(root);
    host.hidden = false;
    if (options.field) options.field(name);

    function close(changed) {
      if (state.closed) return;
      state.closed = true;
      clearTimeout(idleTimer);
      clearTimeout(doneTimer);
      host.removeEventListener("pointerdown", arm, true);
      host.replaceChildren();
      host.hidden = true;
      if (options.onClose) options.onClose(Boolean(changed));
    }
    function arm() {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(() => close(false), IDLE_MS);
    }
    host.addEventListener("pointerdown", arm, true);
    arm();
    back.addEventListener("click", () => {
      if (!done.hidden) close(true);                                                    // Done, after a booking
      else if (state.mode === "book" && !form.hidden && !state.busy) showTimes();      // from the name and length, back to the times
      else close(false);
    });
    change.addEventListener("click", showTimes);

    function fillLengths(minutes) {
      length.replaceChildren(...minutes.map((m) => { const o = make("option", "", fmtLength(m)); o.value = String(m); return o; }));
      length.value = String(minutes.includes(30) ? 30 : minutes[minutes.length - 1]);
    }

    function showForm() {
      days.hidden = times.hidden = true;
      note.textContent = "";
      form.hidden = false;
      error.textContent = "";
      name.focus({ preventScroll: true });
    }

    function showTimes() {
      form.hidden = true;
      days.hidden = times.hidden = false;
      error.textContent = "";
      loadDay(state.date);
    }

    function drawDays() {
      days.replaceChildren(...state.days.map((day) => {
        const b = make("button", "button quiet bk-day", day.label === "Today" || day.label === "Tomorrow" ? day.label : day.label.slice(0, 3));
        b.type = "button";
        b.setAttribute("aria-label", day.label + ", " + fmtDay(dayOf(day.date)));
        b.setAttribute("aria-pressed", String(day.date === state.date));
        const date = make("span", "bk-date", dayOf(day.date).toLocaleDateString("en-US", { month: "short", day: "numeric" }));
        b.append(date);
        b.addEventListener("click", () => { if (day.date !== state.date) loadDay(day.date); });
        return b;
      }));
    }

    function drawTimes() {
      const starts = state.starts || [];
      times.replaceChildren(...starts.map((slot) => {
        const b = make("button", "button quiet bk-time", fmtTime(new Date(slot.start)));
        b.type = "button";
        b.addEventListener("click", () => {
          state.chosen = slot;
          when.textContent = fmtDay(new Date(slot.start)) + " at " + fmtTime(new Date(slot.start));
          fillLengths(slot.durations);
          showForm();
        });
        return b;
      }));
      if (state.starts === null) note.textContent = "Loading the open times.";
      else if (!starts.length) note.textContent = state.date === (state.days[0] || {}).date ? "No open times left today." : "No open times this day.";
      else note.textContent = "";
    }

    async function loadDay(date) {
      state.starts = null;
      if (date) state.date = date;
      drawDays();
      drawTimes();
      try {
        const data = await call("/api/bookings/slots" + (state.date ? "?date=" + encodeURIComponent(state.date) : ""));
        if (state.closed) return;
        state.days = data.days || [];
        state.date = data.date;
        state.starts = data.starts || [];
      } catch (e) {
        if (state.closed) return;
        state.starts = [];
        note.textContent = e.message;
        drawDays();
        times.replaceChildren();
        return;
      }
      drawDays();
      drawTimes();
    }

    function finish(data) {
      const b = data.booking;
      const range = fmtTime(new Date(b.start_time)) + " to " + fmtTime(new Date(b.end_time));
      let words = (state.mode === "now" ? "The room is yours until " + fmtTime(new Date(b.end_time)) : "Booked: " + b.title + ", " + range) + ".";
      if (data.warning) words += " " + data.warning;
      else if (data.pending) words += " Google Calendar can't be reached right now; the room will send it when it can.";
      form.hidden = true;
      done.textContent = words;
      done.hidden = false;
      back.textContent = "Done";
      clearTimeout(idleTimer);
      doneTimer = setTimeout(() => close(true), DONE_MS + (data.warning || data.pending ? 4000 : 0));
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (state.busy) return;
      const words = name.value.trim();
      if (!words) { error.textContent = "Type your name or the meeting's name."; name.focus({ preventScroll: true }); return; }
      const body = { title: words, minutes: Number(length.value), source: options.source };
      if (state.mode === "book") body.start = state.chosen.start;
      state.busy = true;
      submit.disabled = true;
      error.textContent = "";
      try {
        finish(await call(state.mode === "now" ? "/api/bookings/now" : "/api/bookings", { method: "POST", body: JSON.stringify(body) }));
      } catch (err) {
        error.textContent = err.message;
      } finally {
        state.busy = false;
        submit.disabled = false;
      }
    });

    if (state.mode === "now") {
      const now = options.now || { minutes: [], until: null };
      days.hidden = times.hidden = true;
      when.textContent = now.until ? "Free until " + fmtTime(new Date(now.until)) : "From now";
      fillLengths(now.minutes);
      form.hidden = false;
      name.focus({ preventScroll: true });
    } else {
      loadDay(null);
    }
    return { close: close, root: root };
  }

  // Cancel (and End now): the first tap asks ("Tap again to cancel"), a second within four seconds does it, saying which screen asked
  // (source). The button says what went wrong when the room refuses, and onDone runs after a change was made.
  function confirmButton(button, label, ask, key, change, source, onDone) {
    let armedUntil = 0;
    let resetTimer = null;
    button.textContent = label;
    button.addEventListener("click", async () => {
      if (Date.now() > armedUntil) {
        armedUntil = Date.now() + CONFIRM_MS;
        button.textContent = ask;
        button.classList.add("armed");
        clearTimeout(resetTimer);
        resetTimer = setTimeout(() => { button.textContent = label; button.classList.remove("armed"); }, CONFIRM_MS);
        return;
      }
      clearTimeout(resetTimer);
      armedUntil = 0;
      button.disabled = true;
      try {
        await call("/api/bookings/" + encodeURIComponent(key) + "/" + change, { method: "POST", body: JSON.stringify({ source: source }) });
        if (onDone) onDone();
      } catch (err) {
        button.textContent = err.message;
        button.classList.remove("armed");
        resetTimer = setTimeout(() => { button.textContent = label; button.disabled = false; }, CONFIRM_MS);
      }
    });
    return button;
  }

  window.CroomBooking = { open: open, confirmButton: confirmButton, fmtLength: fmtLength };
})();
