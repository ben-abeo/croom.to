// Settings › Network (spec 2026-10-29, sections 3 and 7), behind the PIN as every Settings page is.
//
// One card per device: the TV Pi (through the root helper) and each device that checks in (the touch Pi; the door sign's card comes with
// it in a later phase, drawn the same way). Each says how the device is connected (Wi-Fi or Ethernet, the network, its signal, its
// address), what came of the last Wi-Fi change in the device's own words, and offers Change Wi-Fi (the networks in range, a password
// field that the page's keyboard types in, a network that hides its name by its typed name, Join) and Forget (the networks it knows).
// A change keeps the device's network before it and goes back to it by itself when the device can't do its job on the new one within
// 90 seconds; the card follows it. A change is refused during a meeting ("Leave the meeting first"); looking for networks is not.
//
// While a change is under way the page reads again every 2 seconds. A device that is not checking in is read every 30 seconds, and after
// it has been away 10 minutes the page stops reading by itself (Check again reads once more): an open page must not keep Settings
// unlocked for ever. While the TV Pi itself changes network the page may lose it for a moment; it keeps asking for up to 3 minutes.
(function () {
  "use strict";
  const S = window.croomSettings;
  const P = window.croomPage;

  const POLL_MS = 2000;
  const OFFLINE_POLL_MS = 30000;
  const GIVE_UP_S = 10 * 60;
  const AWAY_MS = 3 * 60 * 1000;
  const CONFIRM_MS = 4000;
  const UNDER_WAY = ["trying", "proven", "undoing"];
  const WIFI_JOBS = ["wifi_scan", "wifi_connect", "wifi_forget"];
  const TITLES = { touch: "Touch Pi", sign: "Door sign" };
  const JOB_WORDS = { wifi_scan: "Looking for networks", wifi_connect: "Joining", wifi_forget: "Forgetting" };
  const OPEN_WARNING = "This network is open: anyone nearby can see what the device sends over it.";
  const PASSWORD_WORDS = "A Wi-Fi password is 8 to 63 letters, digits and symbols, or a key of 64 characters 0 to 9 and A to F";
  const LEAVE_FIRST = "Leave the meeting first";

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
  function field(className, type, placeholder) {
    const input = make("input", "setting-input " + className);
    input.type = type;
    input.dataset.keyboard = "setting";       // the page's keyboard on the kiosk
    input.autocomplete = "off";
    input.spellcheck = false;
    input.placeholder = placeholder || "";
    return input;
  }
  // "10:42 AM" today, "28 Oct, 10:42 AM" before
  function when(iso) {
    const d = iso ? new Date(iso) : null;
    if (!d || isNaN(d)) return "";
    const time = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true });
    if (d.toDateString() === new Date().toDateString()) return time;
    return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" }) + ", " + time;
  }
  const passwordOk = (p) => !p || /^[0-9A-Fa-f]{64}$/.test(p) || /^[\x20-\x7e]{8,63}$/.test(p);
  const bytes = (text) => new TextEncoder().encode(text).length;

  function drawNetwork(host) {
    let gone = false;
    let timer = null;
    let awaySince = 0;                         // when the page lost the room (the TV Pi changing network), 0 when it has not
    let last = null;                           // the last answer
    let joining = null;                        // a TV Pi change asked for that the page has not seen begin: {before, until}
    const root = make("div", "tool-page network-page");
    const loading = make("p", "note settings-loading", "Loading");
    const stopped = make("div", "network-stopped");
    const stoppedWords = make("p", "note");
    const again = button("Check again", "quiet network-again", "refresh-cw");
    stopped.append(stoppedWords, again);
    stopped.hidden = true;
    const box = make("div", "network-cards");
    root.append(loading, stopped, box);
    host.append(root);
    const cards = new Map();                   // device key ("tv" or an id) -> its card, built once and filled at each reading

    function stopReading(words) {
      clearTimeout(timer);
      stoppedWords.textContent = words;
      stopped.hidden = false;
    }
    again.addEventListener("click", () => { stopped.hidden = true; awaySince = 0; read(); });

    // --- one card
    function makeCard(key) {
      const c = { key: key, scan: null, chosen: null, hidden: false, tidy: [] };
      c.root = make("section", "software-card network-card");
      c.root.dataset.device = key;
      const head = make("div", "software-head");
      c.title = make("h3", "software-title");
      const actions = make("div", "software-actions");
      c.change = button("Change Wi-Fi", "quiet network-change", "wifi");
      c.forget = button("Forget", "quiet network-forget", "x-circle");
      actions.append(c.change, c.forget);
      head.append(c.title, actions);
      c.seen = make("p", "device-seen network-seen");
      c.connection = make("p", "software-version network-connection");
      c.address = make("p", "software-newest network-address");
      c.result = make("p", "settings-banner software-notice network-result");
      c.result.setAttribute("role", "status");
      c.job = make("p", "device-job network-job");
      c.job.setAttribute("role", "status");
      c.message = make("p", "message software-message network-message");
      c.message.setAttribute("role", "status");

      // Change Wi-Fi: the networks in range, then the chosen one's password and Join
      c.panel = make("div", "network-panel");
      const bar = make("div", "network-panel-bar");
      c.look = button("Look again", "quiet network-look", "refresh-cw");
      c.hiddenButton = button("Hidden network", "quiet network-hidden");
      c.close = button("Close", "quiet network-close");
      c.scanNote = make("p", "note network-scan-note");
      bar.append(c.look, c.hiddenButton, c.close, c.scanNote);
      c.list = make("ul", "network-list");
      c.list.setAttribute("aria-label", "Networks in range");
      c.join = make("div", "network-join");
      c.joinTitle = make("p", "network-join-title");
      c.ssidLabel = make("label", "network-label", "Network name");
      c.ssid = field("network-ssid", "text", "The network's name");
      c.ssid.maxLength = 32;
      c.ssidLabel.append(c.ssid);
      c.passwordLabel = make("label", "network-label", "Password");
      const passwordRow = make("div", "network-password-row");
      c.password = field("network-password", "password", "Password");
      c.password.maxLength = 64;
      c.show = button("Show", "quiet network-show");
      passwordRow.append(c.password, c.show);
      c.passwordLabel.append(passwordRow);
      c.warning = make("p", "network-warning", OPEN_WARNING);
      const joinRow = make("div", "software-update-row");
      c.joinButton = button("Join", "primary network-join-button", "check");
      c.cancel = button("Cancel", "quiet network-cancel");
      joinRow.append(c.joinButton, c.cancel);
      c.join.append(c.joinTitle, c.ssidLabel, c.passwordLabel, c.warning, joinRow);
      c.panel.append(bar, c.list, c.join);
      c.panel.hidden = true;
      c.join.hidden = true;

      // Forget: the networks the device knows, each with a second tap
      c.forgets = make("div", "network-forgets");
      c.forgets.hidden = true;

      c.root.append(head, c.seen, c.connection, c.address, c.result, c.job, c.panel, c.forgets, c.message);
      box.append(c.root);

      const say = (words, ok) => { c.message.textContent = words || ""; c.message.classList.toggle("ok", Boolean(ok)); };
      c.say = say;
      const path = (action) => "/api/settings/network/" + encodeURIComponent(key) + "/" + action;

      async function scan() {
        c.look.disabled = true;
        c.scanNote.textContent = "Looking for networks…";
        try {
          const answer = await S.post(path("scan"), {});
          if (gone) return;
          if (answer.networks) { c.scan = answer; fillList(c); }      // the TV Pi answers with them; a device later (its job)
          else read();
        } catch (e) {
          if (gone || e.locked) return;
          c.scanNote.textContent = e.message;
        } finally {
          c.look.disabled = false;
        }
      }
      c.change.addEventListener("click", () => {
        const opening = c.panel.hidden;
        c.forgets.hidden = true;
        c.panel.hidden = !opening;
        say("");
        if (opening) { closeJoin(c); fillList(c); if (!c.scan || !c.scan.networks) scan(); }
      });
      c.look.addEventListener("click", scan);
      c.close.addEventListener("click", () => { c.panel.hidden = true; closeJoin(c); });
      c.hiddenButton.addEventListener("click", () => openJoin(c, null));
      c.cancel.addEventListener("click", () => closeJoin(c));
      c.show.addEventListener("click", () => {
        const showing = c.password.type === "text";
        c.password.type = showing ? "password" : "text";
        c.show.textContent = showing ? "Show" : "Hide";
      });
      c.password.addEventListener("input", () => { c.warning.hidden = !(c.hidden && !c.password.value); });
      c.joinButton.addEventListener("click", async () => {
        const ssid = c.hidden ? c.ssid.value : c.chosen && c.chosen.ssid;
        const password = c.password.value;
        if (!ssid || !ssid.trim() || bytes(ssid) > 32) { say("A network's name is 1 to 32 characters"); return; }
        if (!passwordOk(password)) { say(PASSWORD_WORDS); return; }
        c.joinButton.disabled = true;
        say("");
        try {
          await S.post(path("connect"), { ssid: ssid, password: password, hidden: c.hidden });
        } catch (e) {
          c.joinButton.disabled = false;
          if (gone || e.locked) return;
          if (e.status) { say(e.message); return; }
          // no answer: the TV Pi is changing network already
        }
        if (gone) return;
        if (key === "tv") joining = { before: JSON.stringify((last && last.tv && last.tv.change) || null), until: Date.now() + 15000 };
        c.joinButton.disabled = false;
        c.panel.hidden = true;
        closeJoin(c);                            // the password goes from the page at once
        c.askedAfter = c.resultAt;               // the result before this one: a new one ends "Joining"
        say("Joining " + ssid + "…", true);
        read();
      });
      c.forget.addEventListener("click", () => {
        const opening = c.forgets.hidden;
        c.panel.hidden = true;
        closeJoin(c);
        c.forgets.hidden = !opening;
        say("");
      });
      c.forgetOne = async (ssid, b) => {
        b.disabled = true;
        say("");
        try {
          const answer = await S.post(path("forget"), { ssid: ssid });
          if (gone) return;
          say(answer.words || "Forgetting " + ssid + "…", true);
          read();
        } catch (e) {
          b.disabled = false;
          if (gone || e.locked) return;
          say(e.message);
        }
      };
      cards.set(key, c);
      return c;
    }

    function closeJoin(c) {
      c.chosen = null;
      c.hidden = false;
      c.password.value = "";
      c.password.type = "password";
      c.show.textContent = "Show";
      c.ssid.value = "";
      c.join.hidden = true;
      for (const b of c.list.querySelectorAll(".network-choice")) b.classList.remove("chosen");
    }

    // The chosen network's join: its password (none for an open network, which is warned of; a saved one may keep its own), or a
    // hidden network's typed name and its password
    function openJoin(c, network) {
      closeJoin(c);
      c.chosen = network;
      c.hidden = !network;
      c.join.hidden = false;
      c.ssidLabel.hidden = Boolean(network);
      c.joinTitle.textContent = network ? network.ssid : "A network that hides its name";
      const open = network ? network.open : false;
      c.passwordLabel.hidden = open;
      c.password.placeholder = network && network.saved ? "Leave empty to use the saved password" : "Password";
      c.warning.hidden = network ? !open : false;     // a hidden network with no password typed is joined as an open one
      for (const b of c.list.querySelectorAll(".network-choice")) b.classList.toggle("chosen", Boolean(network) && b.dataset.ssid === network.ssid);
      if (c.hidden) c.ssid.focus();
      else if (!open) c.password.focus();
    }

    function fillList(c) {
      const networks = (c.scan && c.scan.networks) || [];
      const chosen = c.chosen && c.chosen.ssid;
      c.list.replaceChildren();
      for (const n of networks) {
        const item = make("li", "network-item");
        const b = button("", "quiet network-choice");
        b.dataset.ssid = n.ssid;
        b.append(make("span", "network-name", n.ssid),
          make("span", "network-detail", [n.signal + "%", n.open ? "Open" : "Secured", n.saved ? "Saved" : "", n.in_use ? "Connected" : ""].filter(Boolean).join(" · ")));
        if (n.ssid === chosen) b.classList.add("chosen");
        b.addEventListener("click", () => openJoin(c, n));
        item.append(b);
        c.list.append(item);
      }
      c.list.hidden = !networks.length;
      if (c.scan && c.scan.networks) c.scanNote.textContent = networks.length ? "Found at " + when(c.scan.at) : "No networks found";
    }

    function fillForgets(c, saved, inMeeting) {
      const shown = Array.from(c.forgets.querySelectorAll(".network-forget-one")).map((b) => b.dataset.ssid).join("\n");
      if (shown === saved.join("\n") && c.forgets.children.length) {
        for (const b of c.forgets.querySelectorAll(".network-forget-one")) b.disabled = inMeeting;
        return;
      }
      for (const tidy of c.tidy) tidy();
      c.tidy = [];
      c.forgets.replaceChildren();
      if (!saved.length) { c.forgets.append(make("p", "note", "No networks are saved")); return; }
      const list = make("ul", "network-list network-saved");
      for (const ssid of saved) {
        const item = make("li", "network-item");
        item.append(make("span", "network-name", ssid));
        const b = button("Forget", "quiet network-forget-one", "x-circle");
        b.dataset.ssid = ssid;
        b.disabled = inMeeting;
        let confirmTimer = null;
        b.addEventListener("click", () => {
          if (!b.classList.contains("confirm")) {
            b.classList.add("confirm");
            P.labelWithIcon(b, "Tap again to forget", "x-circle");
            confirmTimer = setTimeout(() => { b.classList.remove("confirm"); P.labelWithIcon(b, "Forget", "x-circle"); }, CONFIRM_MS);
            return;
          }
          clearTimeout(confirmTimer);
          b.classList.remove("confirm");
          P.labelWithIcon(b, "Forget", "x-circle");
          c.forgetOne(ssid, b);
        });
        c.tidy.push(() => clearTimeout(confirmTimer));
        item.append(b);
        list.append(item);
      }
      c.forgets.append(list);
    }

    // How a device is connected, in words: "Wi-Fi: Office, signal 70%", "Ethernet", "Not connected"
    function connectionWords(n) {
      if (n.connection === "ethernet") return "Ethernet" + (n.ssid ? " (Wi-Fi: " + n.ssid + ")" : "");
      if (n.connection === "wifi") return "Wi-Fi: " + (n.ssid || "connected") + (typeof n.signal === "number" ? ", signal " + n.signal + "%" : "");
      if (n.wifi === false) return "No Wi-Fi on this device";
      return n.connection === "none" ? "Not connected" : "";
    }

    function fillCard(c, d, inMeeting) {
      // d: {title, name, online?, seen?, available, error, network, change, job, last, scan, saved}
      c.title.replaceChildren(d.title);
      if (d.name) c.title.append(make("span", "device-name", d.name));
      c.seen.hidden = d.seen === undefined;
      c.seen.textContent = d.seen || "";
      c.seen.classList.toggle("online", Boolean(d.online));
      c.root.classList.toggle("unavailable", !d.available);
      if (!d.available) {
        c.connection.textContent = d.error || "Not available on this device";
        c.connection.classList.add("settings-unavailable");
        for (const n of [c.change, c.forget, c.address, c.result, c.job, c.panel, c.forgets]) n.hidden = true;
        return;
      }
      c.connection.classList.remove("settings-unavailable");
      const n = d.network || {};
      c.connection.textContent = connectionWords(n) || "Connection unknown";
      c.address.textContent = n.address ? "Address " + n.address : "";
      c.address.hidden = !n.address;
      const wifi = n.wifi !== false;
      c.change.hidden = !wifi;
      c.forget.hidden = !wifi || !(d.saved && d.saved.length);
      if (!wifi) { c.panel.hidden = true; c.forgets.hidden = true; }
      if (d.scan && d.scan !== c.scan && (!c.scan || d.scan.at !== c.scan.at)) { c.scan = d.scan; fillList(c); }
      fillForgets(c, d.saved || [], inMeeting);
      // What came of the last change; the one under way
      const result = d.result;
      c.result.hidden = !(result && result.words);
      if (result && result.words) {
        c.result.textContent = result.words + (when(result.at) ? " (" + when(result.at) + ")" : "");
        c.result.classList.toggle("ok", result.ok);
      }
      c.job.hidden = !d.doing;
      c.job.textContent = d.doing || "";
      const busy = Boolean(d.doing);
      c.resultAt = result ? result.at : null;
      if (/^Joining .*…$/.test(c.message.textContent) && (busy || (result && result.at !== c.askedAfter))) c.say("");   // the card says it now
      c.change.disabled = busy;
      c.forget.disabled = busy;
      c.joinButton.disabled = busy || inMeeting;
      c.look.disabled = busy;
      if (inMeeting) { if (!c.message.textContent || c.message.textContent === LEAVE_FIRST) c.say(LEAVE_FIRST); }
      else if (c.message.textContent === LEAVE_FIRST) c.say("");
    }

    // The TV Pi's card from the helper's status; a device's from its check-ins and its jobs
    function tvCard(tv) {
      const change = tv.change || null;
      const busy = change && UNDER_WAY.includes(change.state);
      return { title: "TV Pi", available: tv.available, error: tv.error, network: tv, saved: tv.saved || [], scan: null,
               doing: busy ? change.words : "", result: change && !busy ? { words: change.words, at: change.at, ok: change.state === "done" } : null };
    }
    function deviceCard(d) {
      const n = d.network || {};
      const job = d.job && WIFI_JOBS.includes(d.job.type) ? d.job : null;
      let doing = "";
      if (job) {
        const what = JOB_WORDS[job.type] + (job.args && job.args.ssid ? " " + job.args.ssid : "");
        doing = job.state === "running" ? what + (job.words ? ": " + job.words : "")
          : what + ": waiting for the " + d.label + " to take it" + (d.online ? "" : " (it has not checked in since " + when(d.last_seen) + ")");
      }
      const lastJob = d.last && WIFI_JOBS.includes(d.last.type) ? d.last : null;
      const change = n.change && n.change.words ? n.change : null;
      let result = null;
      if (lastJob && lastJob.words && !(lastJob.type === "wifi_scan" && lastJob.state === "done")) result = { words: lastJob.words, at: lastJob.at, ok: lastJob.state === "done" };
      else if (change && !UNDER_WAY.includes(change.state)) result = { words: change.words, at: change.at, ok: change.state === "done" };
      return { title: TITLES[d.kind] || "Device", name: d.name, online: d.online, available: true, network: n,
               seen: d.online ? "Checked in " + (d.seen_ago <= 5 ? "just now" : d.seen_ago + " seconds ago") : "Not seen since " + when(d.last_seen),
               saved: n.saved || [], scan: d.scan, doing: doing, result: result };
    }

    function fill(answer) {
      last = answer;
      const keep = new Set(["tv"]);
      const tv = cards.get("tv") || makeCard("tv");
      fillCard(tv, tvCard(answer.tv || {}), answer.in_meeting);
      for (const d of answer.devices || []) {
        keep.add(d.id);
        fillCard(cards.get(d.id) || makeCard(d.id), deviceCard(d), answer.in_meeting);
      }
      for (const [key, c] of cards) if (!keep.has(key)) { for (const t of c.tidy) t(); c.root.remove(); cards.delete(key); }
    }

    // When to read again: soon while a change is under way; slowly while the device it waits for is away; not at all after 10 minutes
    function nextReading(answer) {
      const tv = answer.tv || {};
      if (tv.available && tv.change && UNDER_WAY.includes(tv.change.state)) { joining = null; return POLL_MS; }
      if (joining) {
        // The helper answers before its change begins: read again until the change shows (or has ended already)
        if (JSON.stringify(tv.change || null) === joining.before && Date.now() < joining.until) return POLL_MS;
        joining = null;
      }
      const waiting = (answer.devices || []).filter((d) => d.job && WIFI_JOBS.includes(d.job.type));
      if (!waiting.length) return null;
      if (waiting.some((d) => d.online)) return POLL_MS;
      if (waiting.every((d) => d.seen_ago === null || d.seen_ago > GIVE_UP_S)) {
        const names = waiting.map((d) => (TITLES[d.kind] || "device").replace(/^T/, "t").replace(/^D/, "d"));
        stopReading("The " + names[0] + " has not checked in for 10 minutes: this page has stopped checking on it.");
        return null;
      }
      return OFFLINE_POLL_MS;
    }

    async function read() {
      clearTimeout(timer);
      let answer;
      try {
        answer = await S.api("/api/settings/network");
      } catch (e) {
        if (gone || e.locked) return;
        const tv = last && last.tv;
        if (!e.status && tv && tv.change && UNDER_WAY.includes(tv.change.state)) {
          // The TV Pi is moving to the new network (or back): the page lost it for a moment
          if (!awaySince) awaySince = Date.now();
          if (Date.now() - awaySince > AWAY_MS) { stopReading("The room has not answered for 3 minutes."); return; }
          const c = cards.get("tv");
          if (c) { c.job.hidden = false; c.job.textContent = "Waiting for the TV Pi to answer on its network…"; }
          timer = setTimeout(read, POLL_MS);
          return;
        }
        loading.hidden = false;
        loading.textContent = e.message;
        loading.classList.add("settings-unavailable");
        return;
      }
      if (gone) return;
      awaySince = 0;
      loading.hidden = true;
      fill(answer);
      const delay = nextReading(answer);
      if (delay !== null) timer = setTimeout(read, delay);
    }

    read();
    return () => {
      gone = true;
      clearTimeout(timer);
      for (const c of cards.values()) for (const t of c.tidy) t();
    };
  }

  S.define("network", drawNetwork);
})();
