// Settings › Software (spec 2026-10-29, sections 3, 6 and 8), behind the PIN as every Settings page is.
//
// One card per device. The TV Pi's: the version it runs (and when), the newest on GitHub and the changes between, Update with the
// helper's progress lines while it works, what came of the last update (a version that did not start is undone by itself, and the card
// says so), Switch back to the version kept before, Restart room software and Reboot. Restart, reboot and update are refused during a
// meeting ("Leave the meeting first", the agent's words). A room without the root helper says "Not available on this device" on the TV
// Pi's card.
//
// The other devices' cards (the touch Pi, the door sign, which check in with the TV Pi) go in the box under it: a file of their own
// hands a draw over with croomSettings.softwareCard(draw), and draw(box, answer, again) is called with GET /api/settings/software's
// answer each time the page reads it (again() reads it once more, after a change); it may give back a function that tidies up when the
// page is left or read again. While a device has a job under way the page reads again every 2 seconds, as while the TV Pi updates; while
// the devices with a job are all away (not checking in), every 30 seconds, and once they have been away 10 minutes not at all (Check again
// reads once more): an open page must not keep Settings unlocked for ever.
//
// Update all, over the cards, updates every device that is not on the newest version (the TV Pi last: its update restarts the room
// software); it waits while a meeting is on ("Leave the meeting first") and while anything is under way.
(function () {
  "use strict";
  const S = window.croomSettings;
  const P = window.croomPage;

  const UNDER_WAY = ["building", "switching", "restarting", "watching", "rolling_back"];
  const POLL_MS = 2000;
  const OFFLINE_POLL_MS = 30000;             // a device with a job that is not checking in
  const GIVE_UP_S = 10 * 60;                 // ... and after it has been away this long, the page stops reading by itself
  const RETURN_WAIT_MS = 10 * 60 * 1000;     // a build takes minutes; the restart and its watch 3 more; a reboot about 2
  const CONFIRM_MS = 4000;
  const NOT_AVAILABLE = "Not available on this device";
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const cards = [];                          // the other devices' draws (croomSettings.softwareCard)

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
  const short = (commit) => /^[0-9a-f]{40}$/.test(commit || "") ? commit.slice(0, 7) : (commit || "");   // a release not named by a commit keeps its name
  // What the card says while the helper works: an update, a switch back, or a going back after a version that did not start
  const working = (record) => record.state === "rolling_back" ? "Going back to " + short(record.from)
    : record.rollback ? "Switching back to " + short(record.commit) : "Updating";
  const WORKING = /^(Updating|Switching back to|Going back to)/;
  // "2026-10-28T14:02:11Z" (GitHub's) or "2026-10-28T10:02:11-04:00" (the TV Pi's) as "28 Oct 2026"
  function day(iso) {
    const d = iso ? new Date(iso) : null;
    return d && !isNaN(d) ? d.getDate() + " " + MONTHS[d.getMonth()] + " " + d.getFullYear() : "";
  }
  function versionWords(commit, iso, installed) {
    if (!commit || commit === "unknown") return "Version unknown";
    const when = day(iso) || (installed ? "installed " + day(installed) : "");
    return "Version " + short(commit) + (when ? ", " + when : "");
  }

  function drawSoftware(host) {
    let gone = false;
    let timer = null;
    let tidyCards = [];
    let waiting = false;                     // waiting for the room to come back (a restart, a reboot, an update's restart)
    const root = make("div", "tool-page software-page");
    const loading = make("p", "note settings-loading", "Loading");
    const bar = make("div", "software-bar");
    const updateAll = button("Update all", "primary software-update-all", "refresh-cw");
    const allMessage = make("p", "message software-all-message");
    allMessage.setAttribute("role", "status");
    bar.append(updateAll, allMessage);
    bar.hidden = true;
    const tv = make("section", "software-card");
    tv.dataset.device = "tv";
    tv.hidden = true;
    const others = make("div", "software-devices");   // the touch Pi's and the door sign's cards
    const stopped = make("div", "software-stopped");   // the page has stopped reading: the device it waits for has been away 10 minutes
    const stoppedWords = make("p", "note");
    const checkAgain = button("Check again", "quiet software-again", "refresh-cw");
    stopped.append(stoppedWords, checkAgain);
    stopped.hidden = true;
    checkAgain.addEventListener("click", () => { stopped.hidden = true; read(); });
    root.append(loading, bar, stopped, tv, others);
    host.append(root);

    // --- the TV Pi's card: built once, filled at each reading
    const head = make("div", "software-head");
    const title = make("h3", "software-title", "TV Pi");
    const actions = make("div", "software-actions");
    const restart = button("Restart room software", "quiet software-restart", "refresh-cw");
    const reboot = button("Reboot", "quiet software-reboot", "power");
    actions.append(restart, reboot);
    head.append(title, actions);
    const version = make("p", "software-version");
    const newest = make("p", "software-newest");
    const changes = make("ul", "software-changes");
    changes.setAttribute("aria-label", "Changes");
    changes.tabIndex = 0;
    const notice = make("p", "settings-banner software-notice");
    notice.setAttribute("role", "status");
    notice.hidden = true;
    const updateRow = make("div", "software-update-row");
    const update = button("Update", "primary software-update", "refresh-cw");
    const back = button("Switch back", "quiet software-back");
    const message = make("p", "message software-message");
    message.setAttribute("role", "status");
    updateRow.append(update, back, message);
    const progress = make("ol", "software-progress");
    progress.setAttribute("aria-label", "Progress");
    progress.tabIndex = 0;
    tv.append(head, version, newest, changes, notice, updateRow, progress);
    const say = (words, ok) => { message.textContent = words || ""; message.classList.toggle("ok", Boolean(ok)); };

    let card = null;   // the TV Pi's part of the last answer

    function fill(answer) {
      card = answer.tv || {};
      tv.hidden = false;
      tv.classList.toggle("unavailable", !card.available);
      if (!card.available) {
        version.textContent = card.error || NOT_AVAILABLE;
        version.classList.add("settings-unavailable");
        for (const node of [actions, newest, changes, notice, updateRow, progress]) node.hidden = true;
        return;
      }
      version.classList.remove("settings-unavailable");
      actions.hidden = false;
      updateRow.hidden = false;
      newest.hidden = false;
      version.textContent = versionWords(card.version, card.running && card.running.date, card.installed);
      const record = card.update || { state: "none" };
      const busy = UNDER_WAY.includes(record.state);
      // The newest and the changes between
      changes.replaceChildren();
      if (card.error) newest.textContent = card.error;
      else if (card.up_to_date) newest.textContent = "Up to date";
      else if (card.newest) newest.textContent = "Newest: " + short(card.newest.commit) + (day(card.newest.date) ? ", " + day(card.newest.date) : "");
      else newest.textContent = "";
      newest.classList.toggle("software-current", Boolean(card.up_to_date));
      if (card.changes && card.changes.length && !card.up_to_date) {
        for (const change of card.changes) {
          const item = make("li", "software-change");
          item.append(make("span", "software-change-id", short(change.commit)), make("span", "software-change-title", change.title || ""));
          changes.append(item);
        }
        if (card.more) changes.append(make("li", "software-change software-more", "and " + card.more + " more"));
      } else if (card.newest && !card.up_to_date && !card.error && card.version !== "unknown" && card.changes === null) {
        changes.append(make("li", "software-change software-more", "The changes since this version can't be listed"));
      }
      changes.hidden = !changes.children.length;
      // Update: to the newest, when there is a newer one and nothing is under way
      update.hidden = !(card.newest && !card.up_to_date) || busy;
      update.dataset.commit = card.newest ? card.newest.commit : "";
      back.hidden = !card.previous || busy;
      P.labelWithIcon(back, "Switch back to " + short(card.previous), "return");
      // What came of the last update, and its lines while it works (and when it did not work)
      notice.hidden = true;
      notice.classList.remove("ok");
      const target = short(record.commit);
      if (record.state === "rolled_back") {
        notice.textContent = "The update to " + target + " was undone: the room did not start with it. It is back on " + short(record.from) + ".";
        notice.title = record.why || "";
        notice.hidden = false;
      } else if (record.state === "failed") {
        notice.textContent = (record.rollback ? "Switching back to " : "The update to ") + target + " did not happen: " + (record.why || "it failed") +
          ". The room is still on " + short(record.from) + ".";
        notice.title = "";
        notice.hidden = false;
      } else if (record.state === "done" && record.commit === card.version) {
        notice.textContent = (record.rollback ? "Switched back to " : "Updated to ") + target + (day(record.at) ? " on " + day(record.at) : "");
        notice.classList.add("ok");
        notice.title = "";
        notice.hidden = false;
      }
      progress.replaceChildren(...(record.lines || []).map((line) => make("li", "software-line", line)));
      progress.hidden = !(busy || record.state === "failed" || record.state === "rolled_back") || !progress.children.length;
      if (busy) progress.scrollTop = progress.scrollHeight;
      if (busy && !waiting) say(working(record), true);
      else if (!busy && !waiting && WORKING.test(message.textContent)) say("");
    }

    // Update all: shown when a device is behind the newest; waits while a meeting is on or anything is under way
    let behind = [];
    let devicesRead = [];
    function fillBar(answer) {
      devicesRead = answer.devices || [];
      const tvCard = answer.tv || {};
      const devices = answer.devices || [];
      behind = [];
      if (tvCard.available && tvCard.newest && tvCard.up_to_date === false) behind.push("tv");
      for (const d of devices) if (d.newest && d.up_to_date === false && !(d.cannot || {}).update) behind.push(d.id);
      const busy = (tvCard.update && UNDER_WAY.includes(tvCard.update.state)) || devices.some((d) => d.job);
      bar.hidden = !devices.length || !(behind.length || allMessage.textContent);   // the TV Pi alone has its own Update
      updateAll.disabled = Boolean(answer.in_meeting) || busy || waiting;
      if (answer.in_meeting) allMessage.textContent = "Leave the meeting first";
      else if (allMessage.textContent === "Leave the meeting first") allMessage.textContent = "";
    }
    // When to read again for the devices' jobs: soon while one that has a job checks in; slowly while they are all away; not at all once
    // they have been away GIVE_UP_S
    function deviceDelay(answer) {
      const busy = (answer.devices || []).filter((d) => d.job);
      stopped.hidden = true;
      if (!busy.length) return null;
      if (busy.some((d) => d.online)) return POLL_MS;
      if (busy.every((d) => d.seen_ago === null || d.seen_ago > GIVE_UP_S)) {
        const name = busy[0].label || "device";
        stoppedWords.textContent = "The " + name + " has not checked in for 10 minutes: this page has stopped checking on it.";
        stopped.hidden = false;
        return null;
      }
      return OFFLINE_POLL_MS;
    }

    function drawOthers(answer) {
      for (const tidy of tidyCards) { try { tidy(); } catch (e) { console.warn("A Software card did not tidy up:", e); } }
      tidyCards = [];
      others.replaceChildren();
      for (const draw of cards) {
        const box = make("div", "software-device");
        others.append(box);
        const tidy = draw(box, answer, () => { if (!gone && !waiting) read(); });
        if (typeof tidy === "function") tidyCards.push(tidy);
      }
      others.hidden = !others.children.length;
    }

    // One reading of the page; while an update is under way, another in a moment. A reading that fails while one is under way is the
    // room software restarting for it: the page waits for it to come back.
    async function read() {
      clearTimeout(timer);
      let answer;
      try {
        answer = await S.api("/api/settings/software");
      } catch (e) {
        if (gone || e.locked) return;
        if (card && card.update && UNDER_WAY.includes(card.update.state) && !e.status) { awaitReturn("Restarting the room software…"); return; }
        loading.hidden = false;
        loading.textContent = e.message;
        loading.classList.add("settings-unavailable");
        return;
      }
      if (gone) return;
      loading.hidden = true;
      fill(answer);
      fillBar(answer);
      drawOthers(answer);
      const tvBusy = card.available && card.update && UNDER_WAY.includes(card.update.state);
      const devices = deviceDelay(answer);
      const delay = tvBusy ? POLL_MS : devices;
      if (delay !== null && !waiting) timer = setTimeout(read, delay);
    }

    // The room software (or the whole TV Pi) goes away and comes back: ask whether it answers until it has been away and is back, then
    // read the page again (a restarted room has forgotten every unlock: the PIN pad comes up, as after Restart now).
    function awaitReturn(words, back) {
      waiting = true;
      say(words, true);
      const until = Date.now() + RETURN_WAIT_MS;
      let away = false;
      const look = async () => {
        if (gone) return;
        let up = false;
        try { up = (await fetch("/api/status", { cache: "no-store" })).ok; } catch (e) { up = false; }
        if (gone) return;
        if (!up) away = true;
        else if (away) { waiting = false; say(back || "", true); read(); return; }
        else {
          // Not gone yet: a reading keeps this screen unlocked meanwhile (a refused token is a room that went and came back between two
          // looks: the PIN pad is up, as it is after one)
          try { await S.api("/api/settings/software"); } catch (e) { if (e.locked) return; }
          if (gone) return;
        }
        if (Date.now() > until) { waiting = false; say(away ? "The room has not come back yet" : "The room did not go away: it may not have restarted"); read(); return; }
        timer = setTimeout(look, POLL_MS);
      };
      timer = setTimeout(look, POLL_MS);
    }

    async function ask(path, body, then) {
      say("");
      try {
        await S.post(path, body);
      } catch (e) {
        if (gone || e.locked) return false;
        if (e.status) { say(e.message); return false; }   // refused in words ("Leave the meeting first")
        // no answer: the room is going away already (a reboot or a restart answered and went)
      }
      if (!gone) then();
      return true;
    }

    updateAll.addEventListener("click", async () => {
      updateAll.disabled = true;
      allMessage.textContent = "";
      let answer = null;
      try {
        answer = await S.post("/api/settings/software/update-all", {});
      } catch (e) {
        if (gone || e.locked) return;
        if (e.status) { allMessage.textContent = e.message; updateAll.disabled = false; return; }
      }
      if (gone) return;
      if (answer) {
        const title = (id) => { const d = devicesRead.find((x) => x.id === id); return d ? d.label.replace(/^touch/, "Touch").replace(/^door/, "Door") : id; };
        const names = answer.updating.map((id) => id === "tv" ? "TV Pi" : title(id));
        allMessage.textContent = (names.length ? "Updating " + names.join(", ") : "Nothing to update") +
          (answer.errors && answer.errors.length ? ". " + answer.errors.join(". ") : "");
        allMessage.classList.toggle("ok", !(answer.errors && answer.errors.length));
      }
      read();
    });

    update.addEventListener("click", async () => {
      if (!update.dataset.commit) return;
      update.disabled = true;
      await ask("/api/settings/software/update", { device: "tv", commit: update.dataset.commit }, () => { say("Updating", true); read(); });
      update.disabled = false;
    });
    back.addEventListener("click", async () => {
      back.disabled = true;
      await ask("/api/settings/software/rollback", { device: "tv" }, () => { say("Switching back to " + short(card.previous), true); read(); });
      back.disabled = false;
    });
    restart.addEventListener("click", async () => {
      restart.disabled = true;
      await ask("/api/settings/restart", {}, () => awaitReturn("Restarting the room software…", "The room software has restarted"));
      restart.disabled = false;
    });
    // Reboot asks for a second tap: the TV Pi (the room's meetings, its page) is gone for a couple of minutes
    let confirmTimer = null;
    const rebootWords = () => P.labelWithIcon(reboot, "Reboot", "power");
    reboot.addEventListener("click", async () => {
      if (!reboot.classList.contains("confirm")) {
        reboot.classList.add("confirm");
        P.labelWithIcon(reboot, "Tap again to reboot", "power");
        confirmTimer = setTimeout(() => { reboot.classList.remove("confirm"); rebootWords(); }, CONFIRM_MS);
        return;
      }
      clearTimeout(confirmTimer);
      reboot.classList.remove("confirm");
      rebootWords();
      reboot.disabled = true;
      await ask("/api/settings/reboot", { device: "tv" }, () => awaitReturn("Rebooting the TV Pi…", "The TV Pi has restarted"));
      reboot.disabled = false;
    });

    read();
    return () => {
      gone = true;
      clearTimeout(timer);
      clearTimeout(confirmTimer);
      for (const tidy of tidyCards) { try { tidy(); } catch (e) { /* the page is going */ } }
    };
  }

  S.define("software", drawSoftware);
  // The other devices' Software cards: draw(box, answer, again) at each reading of the page (see the top of this file)
  S.softwareCard = (draw) => { cards.push(draw); };
  // What those cards say as the TV Pi's does
  S.softwareWords = { short: short, day: day, versionWords: versionWords, UNDER_WAY: UNDER_WAY };
})();
