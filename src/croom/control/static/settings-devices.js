// Settings › Software: the devices that check in with the TV Pi (spec 2026-10-29, sections 3, 6 and 7), drawn under the TV Pi's card
// through croomSettings.softwareCard (settings-software.js).
//
// One card per device that has joined (the touch Pi; the door sign in a later phase): its name, when it last checked in ("Not seen since
// <time>" when it has gone quiet: its jobs wait for it), the version it runs, the newest on GitHub and the changes between, Update, Reboot
// (a second tap) and Forget (a second tap: a device replaced by another), the job under way with the device's own words, and what came
// of the last one. Then "Devices waiting to join", each with Allow and Refuse (a second tap: a refused device is not listed again).
(function () {
  "use strict";
  const S = window.croomSettings;
  const P = window.croomPage;
  const W = S.softwareWords;
  const CONFIRM_MS = 4000;
  const TITLES = { touch: "Touch Pi", sign: "Door sign" };
  const JOB_WORDS = { update: "Updating", reboot: "Rebooting", wifi_scan: "Looking for networks", wifi_connect: "Joining a network",
                      wifi_forget: "Forgetting a network" };

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
  const title = (device) => TITLES[device.kind] || "Device";
  // "10:42 AM" today, "28 Oct, 10:42 AM" before
  function when(iso) {
    const d = iso ? new Date(iso) : null;
    if (!d || isNaN(d)) return "";
    const time = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true });
    const today = new Date();
    if (d.toDateString() === today.toDateString()) return time;
    return W.day(iso).replace(/ \d{4}$/, "") + ", " + time;
  }

  // A button that asks for a second tap, then does `act`
  function twoTaps(b, label, icon, ask, act) {
    let timer = null;
    const words = () => P.labelWithIcon(b, label, icon);
    b.addEventListener("click", () => {
      if (!b.classList.contains("confirm")) {
        b.classList.add("confirm");
        P.labelWithIcon(b, ask, icon);
        timer = setTimeout(() => { b.classList.remove("confirm"); words(); }, CONFIRM_MS);
        return;
      }
      clearTimeout(timer);
      b.classList.remove("confirm");
      words();
      act();
    });
    return () => clearTimeout(timer);
  }

  function drawDevices(box, answer, again) {
    const tidies = [];
    let gone = false;
    const devices = answer.devices || [];
    const waiting = answer.waiting || [];

    async function ask(path, body, say, done) {
      say("");
      try {
        await S.post(path, body);
      } catch (e) {
        if (gone || e.locked) return;
        say(e.message);
        return;
      }
      if (gone) return;
      if (done) done();
      again();
    }

    for (const device of devices) {
      const card = make("section", "software-card device-card");
      card.dataset.device = device.id;
      card.dataset.kind = device.kind;
      const head = make("div", "software-head");
      const name = make("h3", "software-title", title(device));
      if (device.name) name.append(make("span", "device-name", device.name));
      const actions = make("div", "software-actions");
      const reboot = button("Reboot", "quiet device-reboot", "power");
      const forget = button("Forget", "quiet device-forget", "x-circle");
      actions.append(reboot, forget);
      head.append(name, actions);
      const seen = make("p", "device-seen" + (device.online ? " online" : ""),
        device.online ? "Checked in " + (device.seen_ago <= 5 ? "just now" : device.seen_ago + " seconds ago") : "Not seen since " + when(device.last_seen));
      const version = make("p", "software-version", W.versionWords(device.version, device.running && device.running.date, null));
      const newest = make("p", "software-newest");
      if (device.error) newest.textContent = device.error;
      else if (device.up_to_date) newest.textContent = "Up to date";
      else if (device.newest) newest.textContent = "Newest: " + W.short(device.newest.commit) + (W.day(device.newest.date) ? ", " + W.day(device.newest.date) : "");
      newest.classList.toggle("software-current", Boolean(device.up_to_date));
      newest.hidden = !newest.textContent;
      const changes = make("ul", "software-changes");
      changes.setAttribute("aria-label", "Changes");
      changes.tabIndex = 0;
      if (device.changes && device.changes.length && !device.up_to_date) {
        for (const change of device.changes) {
          const item = make("li", "software-change");
          item.append(make("span", "software-change-id", W.short(change.commit)), make("span", "software-change-title", change.title || ""));
          changes.append(item);
        }
        if (device.more) changes.append(make("li", "software-change software-more", "and " + device.more + " more"));
      }
      changes.hidden = !changes.children.length;
      // What came of the last job (the device's own words), and the one under way
      const notice = make("p", "settings-banner software-notice device-last");
      notice.setAttribute("role", "status");
      const last = device.last;
      notice.hidden = !(last && last.words);
      if (last && last.words) {
        notice.textContent = last.words + (when(last.at) ? " (" + when(last.at) + ")" : "");
        notice.classList.toggle("ok", last.state === "done");
      }
      const job = make("p", "device-job");
      job.setAttribute("role", "status");
      if (device.job) {
        const doing = JOB_WORDS[device.job.type] || "Working";
        const target = device.job.type === "update" && device.job.args && device.job.args.commit ? " to " + W.short(device.job.args.commit) : "";
        job.textContent = device.job.state === "running" ? doing + target + (device.job.words ? ": " + device.job.words : "")
          : doing + target + ": waiting for the " + device.label + " to take it" + (device.online ? "" : " (it has not checked in since " + when(device.last_seen) + ")");
      }
      job.hidden = !device.job;
      const row = make("div", "software-update-row");
      const update = button("Update", "primary software-update device-update", "refresh-cw");
      update.hidden = !(device.newest && device.up_to_date === false) || Boolean(device.job);
      const message = make("p", "message software-message");
      message.setAttribute("role", "status");
      row.append(update, message);
      const say = (words) => { message.textContent = words || ""; };
      card.append(head, seen, version, newest, changes, notice, job, row);
      box.append(card);
      reboot.disabled = forget.disabled = Boolean(device.job);

      update.addEventListener("click", () => {
        update.disabled = true;
        ask("/api/settings/software/update", { device: device.id, commit: device.newest.commit }, say).finally(() => { update.disabled = false; });
      });
      tidies.push(twoTaps(reboot, "Reboot", "power", "Tap again to reboot",
        () => ask("/api/settings/reboot", { device: device.id }, say)));
      tidies.push(twoTaps(forget, "Forget", "x-circle", "Tap again to forget",
        () => ask("/api/settings/devices/" + encodeURIComponent(device.id) + "/forget", {}, say)));
    }

    if (waiting.length) {
      const card = make("section", "software-card software-waiting");
      card.append(make("h3", "software-title", "Devices waiting to join"),
        make("p", "note", "Allow only a device you know: the touch Pi set up for this room, or its door sign."));
      const list = make("ul", "waiting-list");
      const message = make("p", "message software-message");
      message.setAttribute("role", "status");
      const say = (words) => { message.textContent = words || ""; };
      for (const device of waiting) {
        const item = make("li", "waiting-device");
        item.dataset.device = device.id;
        const what = make("div", "waiting-what");
        what.append(make("span", "waiting-title", title(device) + (device.name ? " " + device.name : "")),
          make("span", "waiting-detail", [device.address, "asked at " + when(device.first_seen)].filter(Boolean).join(" · ")));
        const allow = button("Allow", "primary waiting-allow", "check");
        const refuse = button("Refuse", "quiet waiting-refuse", "x-circle");
        allow.addEventListener("click", () => {
          allow.disabled = true;
          ask("/api/settings/devices/" + encodeURIComponent(device.id) + "/allow", {}, say).finally(() => { allow.disabled = false; });
        });
        tidies.push(twoTaps(refuse, "Refuse", "x-circle", "Tap again to refuse",
          () => ask("/api/settings/devices/" + encodeURIComponent(device.id) + "/refuse", {}, say)));
        item.append(what, allow, refuse);
        list.append(item);
      }
      card.append(list, message);
      box.append(card);
    }
    box.hidden = !box.children.length;
    return () => { gone = true; for (const tidy of tidies) tidy(); };
  }

  S.softwareCard(drawDevices);
})();
