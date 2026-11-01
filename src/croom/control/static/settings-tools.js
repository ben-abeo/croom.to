// Settings › Check and Logs (spec 2026-10-29, section 3), behind the PIN as every Settings page is.
//
// Check: one button for each of the room's checks (the `croom --check-…` commands, run inside the agent). A press runs the check and
// shows what it says in a block of its own, which scrolls inside itself; each check's last answer is kept while the page is open, and a
// press on a check that is running shows it running. Check needs no root helper.
//
// Capture the next Google Meet call (spec 2026-10-30 Meet controls, section 3), on the Check page. Settings closes when a meeting starts
// (the screen is shared), so the capture is armed beforehand: once the room has been in a Meet call for the delay chosen (1, 3 or 5
// minutes), the TV opens Meet's panels one after another, records them and closes them, once. The arm lasts two hours and is forgotten when
// the room software restarts. The page says whether it is armed, and what the last capture found, step by step; Download takes the newest
// capture's zip. The Screenshots switch (on unless turned off) leaves the screenshots out.
//
// Logs: the room software's log, newest first, through the root helper: All, or Warnings and errors; Load more adds the page before
// the oldest shown. A room without the helper says "Not available on this device".
(function () {
  "use strict";
  const S = window.croomSettings;
  const P = window.croomPage;

  const CHECKS = [
    { key: "calendar", label: "Calendar", icon: "calendar" },
    { key: "tv", label: "TV", icon: "tv" },
    { key: "laptop", label: "Laptop switch", icon: "laptop" },
    { key: "google", label: "Google sign-in", icon: "user" },
    { key: "zoom", label: "Zoom keys", icon: "video" },
  ];
  const DELAYS = [1, 3, 5];
  const CAPTURE_STEPS = { call: "The call", chat: "Chat", people: "People", reactions: "Reactions", more_options: "More options",
    layout: "Change layout", own_tile: "The room's tile menu", host_controls: "Host controls", dialogs: "Boxes on the screen" };
  const LEVELS = [
    { key: "all", label: "All" },
    { key: "warning", label: "Warnings and errors" },
  ];
  const LEVEL_WORDS = { critical: "Critical", error: "Error", warning: "Warning", info: "Info", debug: "Debug" };
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function button(label, className, icon) {
    const b = make("button", "button " + (className || "quiet"));
    b.type = "button";
    if (icon) P.labelWithIcon(b, label, icon);
    else b.textContent = label;
    P.keepFocus(b);
    return b;
  }

  // --- Check
  function drawCheck(host) {
    let gone = false;
    const results = {};      // check -> {running} | {ok, text} | {error}
    let shown = null;
    const root = make("div", "tool-page check-page");
    const help = make("p", "tool-note", "Each check looks at one part of the room and says what it finds. None of them changes anything.");
    const bar = make("div", "tool-bar check-buttons");
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-label", "Checks");
    const verdict = make("p", "check-verdict");
    verdict.setAttribute("role", "status");
    const output = make("pre", "tool-output check-output");
    output.tabIndex = 0;
    output.hidden = true;
    const buttons = {};
    for (const check of CHECKS) {
      const b = button(check.label, "quiet check-run", check.icon);
      b.dataset.check = check.key;
      b.setAttribute("aria-pressed", "false");
      b.addEventListener("click", () => run(check));
      buttons[check.key] = b;
      bar.append(b);
    }
    // Capture the next Google Meet call: the arm, its Screenshots switch and delay, Cancel and Download; what it says, and the last capture.
    const captureHelp = make("p", "tool-note", "Capture the next Google Meet call: a few minutes into the room's next Meet call, the TV opens " +
      "Meet's panels one by one and records them. Names, email addresses and chat are replaced in the recording; the screenshots show the TV " +
      "as it is. Armed for 2 hours; a restart of the room software forgets it.");
    const captureBar = make("div", "tool-bar capture-bar");
    captureBar.setAttribute("role", "group");
    captureBar.setAttribute("aria-label", "Capture the next Google Meet call");
    const arm = button("Capture the next Google Meet call", "quiet capture-arm", "record");
    let screenshots = true;
    let delay = 3;
    const shots = button("", "quiet capture-screenshots");
    shots.setAttribute("role", "switch");
    const delayButtons = DELAYS.map((minutes) => {
      const b = button(minutes + " min", "quiet capture-delay");
      b.dataset.minutes = String(minutes);
      b.addEventListener("click", () => { delay = minutes; drawChoices(); });
      return b;
    });
    const drawChoices = () => {
      shots.setAttribute("aria-checked", String(screenshots));
      shots.textContent = "Screenshots: " + (screenshots ? "On" : "Off");
      for (const b of delayButtons) b.setAttribute("aria-pressed", String(Number(b.dataset.minutes) === delay));
    };
    shots.addEventListener("click", () => { screenshots = !screenshots; drawChoices(); });
    drawChoices();
    const cancel = button("Cancel", "quiet capture-cancel", "x-circle");
    const download = button("Download", "quiet capture-download", "save");
    captureBar.append(arm, shots, ...delayButtons, cancel, download);
    const captureState = make("p", "check-verdict capture-state");
    captureState.setAttribute("role", "status");
    const captureOutput = make("pre", "tool-output capture-output");
    captureOutput.tabIndex = 0;
    captureOutput.hidden = true;
    arm.addEventListener("click", () => captureAsk({ arm: true, after_minutes: delay, screenshots: screenshots }));
    cancel.addEventListener("click", () => captureAsk({ arm: false }));
    download.addEventListener("click", runDownload);
    root.append(help, bar, verdict, output, captureHelp, captureBar, captureState, captureOutput);
    host.append(root);

    function show(check) {
      shown = check;
      for (const key in buttons) buttons[key].setAttribute("aria-pressed", String(key === check.key));
      const result = results[check.key];
      verdict.classList.remove("ok", "failed");
      if (!result) { verdict.textContent = ""; output.hidden = true; return; }
      if (result.running) {
        verdict.textContent = check.label + ": " + (check.busy || "checking…");
        output.hidden = true;
        return;
      }
      if (result.error) {
        verdict.textContent = check.label + ": " + result.error;
        verdict.classList.add("failed");
        output.hidden = true;
        return;
      }
      verdict.textContent = check.label + ": " + (result.verdict || (result.ok ? "passed" : "did not pass"));
      verdict.classList.add(result.ok ? "ok" : "failed");
      output.textContent = result.text || "";
      output.hidden = !result.text;
      output.scrollTop = 0;
    }

    async function run(check, work) {
      if (results[check.key] && results[check.key].running) { show(check); return; }
      results[check.key] = { running: true };
      buttons[check.key].classList.add("running");
      show(check);
      let result;
      try {
        result = await (work ? work() : S.post("/api/settings/checks/" + encodeURIComponent(check.key), {}));
      } catch (e) {
        if (gone || e.locked) return;
        result = { error: e.message };
      }
      if (gone) return;
      results[check.key] = result;
      buttons[check.key].classList.remove("running");
      if (shown === check) show(check);
    }

    // What the arm and the last capture are, in words: the arm, then the last capture with a line for each step.
    function drawCapture(state, said) {
      if (state.armed) { delay = state.after_minutes; screenshots = state.screenshots; drawChoices(); }   // the arm as it was made
      const armed = state.running ? "Capturing Meet's page now."
        : state.armed && state.runs_at ? "Armed: runs at " + state.runs_at + ", " + state.after_minutes + " min into this Google Meet call."
        : state.armed ? "Armed until " + state.until + ": runs " + state.after_minutes + " min into the next Google Meet call" +
                        (state.screenshots ? "." : ", without screenshots.")
        : "Not armed.";
      const last = state.last;
      let summary = "", lines = [];
      if (last && last.error) summary = " Last capture, " + last.at + ": " + last.error + ".";
      else if (last) {
        const steps = last.steps || [];
        const count = (test) => steps.filter(test).length;
        const open = count((s) => s.closed === false), unknown = count((s) => s.closed === "unknown");
        summary = " Last capture, " + last.at + ": " + count((s) => s.opened) + " opened, " + count((s) => s.opened && s.closed === true) + " closed" +
          (unknown ? ", " + unknown + " unknown — look at the TV" : "") + (open ? ", " + open + " left open on the TV" : "") + ".";
        // closed: true, false (a panel is still open), "unknown" (the page did not come back as it was: someone looks at the TV)
        const after = (step) => step.closed === false ? ", and left open on the TV" : step.closed === "unknown" ? "; unknown — look at the TV" : "";
        lines = ["File: " + last.file].concat(steps.map((step) => (CAPTURE_STEPS[step.name] || step.name) + ": " + (
          !step.opened ? "not opened" + (step.why ? " (" + step.why + ")" : "") + after(step)
            : step.closed === true ? "opened and closed" : "closed" in step ? "opened" + after(step) : "recorded")));
      } else if (state.newest) summary = " Newest file: " + state.newest + ".";
      captureState.textContent = (said ? said + " " : "") + armed + summary;
      captureState.classList.toggle("failed", Boolean(last && (last.error || (last.steps || []).some((s) => s.closed !== undefined && s.closed !== true))));
      cancel.disabled = !state.armed;
      captureOutput.textContent = lines.join("\n");
      captureOutput.hidden = !lines.length;
    }

    async function captureAsk(body) {
      try {
        drawCapture(body ? await S.post("/api/settings/meet-capture", body) : await S.api("/api/settings/meet-capture/state"));
      } catch (e) {
        if (gone || e.locked) return;
        captureState.textContent = e.message;
        captureState.classList.add("failed");
      }
    }

    // The newest capture's zip, with this screen's unlock (a plain link would not carry it).
    async function runDownload() {
      let words;
      try {
        const response = await fetch("/api/settings/meet-capture", { headers: { "x-croom-unlock": S.token() || "" } });
        if (!response.ok) {
          try { words = (await response.json()).error || ""; } catch (e) { words = ""; }
          words = words || "Request failed (" + response.status + ")";
        } else {
          const name = (/filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "") || [])[1] || "meet-capture.zip";
          const link = make("a");
          link.href = URL.createObjectURL(await response.blob());
          link.download = name;
          document.body.append(link);
          link.click();
          link.remove();
          setTimeout(() => URL.revokeObjectURL(link.href), 60000);
          words = "Download: saved as " + name + ".";
        }
      } catch (e) {
        words = e.message;
      }
      if (!gone) captureState.textContent = words;
    }

    captureAsk(null);
    return () => { gone = true; };
  }

  // --- Logs

  // "2026-10-30T10:42:13-05:00" (the TV Pi's own time) as "Oct 30 10:42:13", whatever this screen's time zone is.
  function when(iso) {
    const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d:\d\d:\d\d)/.exec(iso || "");
    return m ? MONTHS[Number(m[2]) - 1] + " " + Number(m[3]) + " " + m[4] : "";
  }

  function drawLogs(host) {
    let gone = false;
    let level = "all";
    let asked = 0;           // counts the readings: an answer to one the person has moved on from is not drawn
    let cursor = null;
    const root = make("div", "tool-page logs-page");
    const bar = make("div", "tool-bar logs-levels");
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-label", "Which lines");
    const levelButtons = LEVELS.map((l) => {
      const b = button(l.label, "quiet logs-level");
      b.dataset.level = l.key;
      b.addEventListener("click", () => { if (level !== l.key) { level = l.key; read(false); } });
      bar.append(b);
      return b;
    });
    const note = make("p", "note settings-loading", "Loading");
    const list = make("div", "tool-output logs-list");
    list.setAttribute("role", "log");
    list.tabIndex = 0;
    list.hidden = true;
    const more = button("Load more", "quiet logs-more");
    more.addEventListener("click", () => read(true));
    root.append(bar, note, list);
    host.append(root);

    function pressed() {
      for (const b of levelButtons) b.setAttribute("aria-pressed", String(b.dataset.level === level));
    }

    function row(entry) {
      const line = make("div", "log-entry");
      line.dataset.level = entry.level || "info";
      line.append(make("span", "log-time", when(entry.time)), make("span", "log-level", LEVEL_WORDS[entry.level] || entry.level || ""),
        make("span", "log-text", entry.text || ""));
      return line;
    }

    async function read(further) {
      const mine = ++asked;
      pressed();
      more.disabled = true;
      if (!further) {
        cursor = null;
        note.hidden = false;
        note.classList.remove("settings-unavailable");
        note.textContent = "Loading";
        list.hidden = true;
        list.replaceChildren();
      } else {
        more.textContent = "Loading";
      }
      let answer;
      try {
        const query = "?level=" + encodeURIComponent(level) + (further && cursor ? "&before=" + encodeURIComponent(cursor) : "");
        answer = await S.api("/api/settings/logs" + query);
      } catch (e) {
        if (gone || e.locked || mine !== asked) return;
        if (further) {
          more.textContent = "Load more";
          more.disabled = false;
          list.append(make("p", "setting-error logs-error", e.message));
          return;
        }
        note.textContent = e.message;
        note.classList.add("settings-unavailable");
        return;
      }
      if (gone || mine !== asked) return;
      more.remove();
      more.textContent = "Load more";
      more.disabled = false;
      for (const stale of list.querySelectorAll(".logs-error, .logs-end")) stale.remove();
      const entries = answer.entries || [];
      if (!further && !entries.length) {
        note.textContent = level === "warning" ? "No warnings or errors in the log" : "Nothing in the log";
        return;
      }
      note.hidden = true;
      list.hidden = false;
      list.append(...entries.map(row));
      cursor = answer.cursor || cursor;
      if (answer.more && cursor) list.append(more);
      else list.append(make("p", "tool-note logs-end", "The start of the log"));
      if (!further) list.scrollTop = 0;
    }

    read(false);
    return () => { gone = true; };
  }

  S.define("check", drawCheck);
  S.define("logs", drawLogs);
})();
