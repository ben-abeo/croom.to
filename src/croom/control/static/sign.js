(function () {
  "use strict";

  const SOON_MS = 10 * 60 * 1000;
  const STATUS_EVERY_MS = 5000;
  const EVENTS_EVERY_MS = 60000;
  const IN_PROGRESS = ["joining", "in_lobby", "connected", "leaving"];
  const TODAY_ROWS = 3;

  // The scenery along the bottom of the sign: a silhouette in inline SVG, so the page loads nothing but its own files.
  // sign.css tints it in the status colour (.fill, .fill2 and .line take var(--status)); body[data-bg] picks which one.
  // The drawings are the ones in docs/superpowers/mockups/2026-10-13-door-sign-mockup.html.
  const SCENES = {
    // The Austin skyline as it stands from Lady Bird Lake, with the lake's faint reflection of it.
    skyline: `<svg viewBox="0 0 1280 300" preserveAspectRatio="none">
      <defs><clipPath id="water"><rect x="0" y="254" width="1280" height="46"/></clipPath></defs>
      <g id="city"><path class="fill" d="M0 236 L0 198.8 L70 198.8 L70 236 Z M60 236 L60 183.92 L110 183.92 L110 236 Z M118 236 L118 189.5 L178 189.5 L178 236 Z M186 236 L186 133.7 L209.0 107.7 L232 133.7 L232 236 Z M240 236 L240 165.32 L298 165.32 L298 236 Z M302 236 L302 150.44 L342 150.44 L342 236 Z M346 236.0 L346 216.7 L400 216.7 L400 236.0 Z M362 216.7 L362 197.3 L416 197.3 L416 216.7 Z M346 197.3 L346 178.0 L400 178.0 L400 197.3 Z M362 178.0 L362 158.6 L416 158.6 L416 178.0 Z M346 158.6 L346 139.3 L392 139.3 L392 158.6 Z M420 236 L420 170.9 L486 170.9 L486 236 Z M492 236 L492 150.7 L483 126.7 L483 116.7 L511.8 116.7 L518.0 98.7 L524.2 116.7 L553 116.7 L553 126.7 L544 150.7 L544 236 Z M552 236 L552 157.88 L608 157.88 L608 236 Z M612 236 L612 112.08000000000001 L654 112.08000000000001 L654 236 Z M620 236 L620 102.08000000000001 L646 102.08000000000001 L646 236 Z M660 236 L660 143.0 L740 143.0 L740 236 Z M746 236 L746 87.19999999999999 L788 87.19999999999999 L788 236 Z M792 236 L792 79.76000000000002 L828 79.76000000000002 L828 236 Z M838 236 L838 96.0 Q 870.4 60.0 910 50.0 L910 236 Z M916 236 L916 98.36000000000001 L966 98.36000000000001 L966 236 Z M972 236 L972 128.12 L1032 128.12 L1032 236 Z M1036 236 L1036 165.32 L1086 165.32 L1086 236 Z M1092 236 L1092 180.2 L1162 180.2 L1162 236 Z M1168 236 L1168 191.36 L1228 191.36 L1228 236 Z M1232 236 L1232 198.8 L1280 198.8 L1280 236 Z"/><path class="fill2" d="M0 236 L0 202 L60 202 L60 236 Z M60 236 L60 192 L120 192 L120 236 Z M120 236 L120 196 L180 196 L180 236 Z M180 236 L180 200 L240 200 L240 236 Z M240 236 L240 190 L300 190 L300 236 Z M300 236 L300 196 L360 196 L360 236 Z M360 236 L360 192 L420 192 L420 236 Z M420 236 L420 198 L480 198 L480 236 Z M480 236 L480 190 L540 190 L540 236 Z M540 236 L540 196 L600 196 L600 236 Z M600 236 L600 192 L660 192 L660 236 Z M660 236 L660 198 L720 198 L720 236 Z M720 236 L720 190 L780 190 L780 236 Z M780 236 L780 194 L840 194 L840 236 Z M840 236 L840 190 L900 190 L900 236 Z M900 236 L900 196 L960 196 L960 236 Z M960 236 L960 192 L1020 192 L1020 236 Z M1020 236 L1020 198 L1080 198 L1080 236 Z M1080 236 L1080 194 L1140 194 L1140 236 Z M1140 236 L1140 200 L1200 200 L1200 236 Z M1200 236 L1200 196 L1260 196 L1260 236 Z M1260 236 L1260 202 L1320 202 L1320 236 Z"/></g>
      <path class="fill2" d="M0 300 L0 248 Q 15 236 30 248 Q 45 239 60 248 Q 75 242 90 248 Q 105 236 120 248 Q 135 239 150 248 Q 165 242 180 248 Q 195 236 210 248 Q 225 239 240 248 Q 255 242 270 248 Q 285 236 300 248 Q 315 239 330 248 Q 345 242 360 248 Q 375 236 390 248 Q 405 239 420 248 Q 435 242 450 248 Q 465 236 480 248 Q 495 239 510 248 Q 525 242 540 248 Q 555 236 570 248 Q 585 239 600 248 Q 615 242 630 248 Q 645 236 660 248 Q 675 239 690 248 Q 705 242 720 248 Q 735 236 750 248 Q 765 239 780 248 Q 795 242 810 248 Q 825 236 840 248 Q 855 239 870 248 Q 885 242 900 248 Q 915 236 930 248 Q 945 239 960 248 Q 975 242 990 248 Q 1005 236 1020 248 Q 1035 239 1050 248 Q 1065 242 1080 248 Q 1095 236 1110 248 Q 1125 239 1140 248 Q 1155 242 1170 248 Q 1185 236 1200 248 Q 1215 239 1230 248 Q 1245 242 1260 248 Q 1275 236 1290 248 L1280 300 Z"/>
      <g clip-path="url(#water)" opacity="0.4" transform="translate(0 506) scale(1 -1)"><use href="#city"/></g>
      <path class="line" style="stroke-opacity: 0.35" d="M0 254 L1280 254"/>
    </svg>`,
    // The UT Tower rising from the Main Building, the campus roofs beside it.
    tower: `<svg viewBox="0 0 1280 300" preserveAspectRatio="none">
      <path class="fill2" d="M40 236 L40 214 L70 200 L100 214 L100 236 Z M110 236 L110 214 L140 200 L170 214 L170 236 Z M180 236 L180 214 L210 200 L240 214 L240 236 Z M250 236 L250 214 L280 200 L310 214 L310 236 Z M320 236 L320 214 L350 200 L380 214 L380 236 Z M390 236 L390 214 L420 200 L450 214 L450 236 Z M830 236 L830 214 L860 200 L890 214 L890 236 Z M900 236 L900 214 L930 200 L960 214 L960 236 Z M970 236 L970 214 L1000 200 L1030 214 L1030 236 Z M1040 236 L1040 214 L1070 200 L1100 214 L1100 236 Z M1110 236 L1110 214 L1140 200 L1170 214 L1170 236 Z M1180 236 L1180 214 L1210 200 L1240 214 L1240 236 Z"/>
      <path class="fill" d="M470 236 L470 196 L810 196 L810 236 Z M560 236 L560 180 L720 180 L720 236 Z M614 236 L614 70 L666 70 L666 236 Z M606 236 L606 56 L674 56 L674 236 Z M606 236 L606 70 L674 70 L674 236 Z M618 236 L618 36 L662 36 L662 236 Z M626 236 L626 28 L654 28 L654 236 Z M626 28 L640 18 L654 28 Z"/>
      <path d="M622 236 L622 36 L628 36 L628 236 Z M634 236 L634 36 L640 36 L640 236 Z M646 236 L646 36 L652 36 L652 236 Z M656 236 L656 36 L662 36 L662 236 Z  M626 104 L626 170 L634 170 L634 104 Z M646 104 L646 170 L654 170 L654 104 Z" style="fill: var(--navy-900); opacity: 0.7"/>
      <circle cx="640" cy="88" r="8" style="fill: var(--navy-900); opacity: 0.8"/>
      <path class="fill" d="M0 300 L0 236 L1280 236 L1280 300 Z"/>
    </svg>`,
    // Three ridges of the hill country and the road that winds between them.
    hills: `<svg viewBox="0 0 1280 300" preserveAspectRatio="none">
      <path class="fill2" d="M0 300 L0 150 Q 160 110 320 140 T 640 118 T 960 150 T 1280 122 L1280 300 Z"/>
      <path class="fill2" d="M0 300 L0 198 Q 200 160 400 194 T 800 180 T 1280 198 L1280 300 Z"/>
      <path class="fill" d="M0 300 L0 246 Q 240 206 480 240 T 960 232 T 1280 246 L1280 300 Z"/>
      <path class="line" style="stroke-width: 3; stroke-opacity: 0.7" d="M1060 300 Q 1000 250 900 244 T 720 220 T 580 190"/>
    </svg>`,
    plain: ``,   // no scenery: the glow in the corner is all (see sign.css)
  };

  const el = (id) => document.getElementById(id);
  const model = { status: null, events: [], offline: false };

  const platformNames = { zoom: "Zoom", google_meet: "Google Meet", teams: "Teams", webex: "Webex" };
  const platformName = (key) => platformNames[key] || key || "";
  // Times read the American way whatever the browser's locale: 12-hour with AM or PM, and the date as "October 13, 2026"
  // (the Pi's Chromium would otherwise follow the system locale and show 24-hour times).
  const fmtTime = (d) => d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true }).replace(/\s+(?=[AP]M$)/, "\u00A0");   // no-break space: a time never splits across lines
  const fmtDate = (d) => d.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });
  const plural = (n, word) => n + " " + word + (n === 1 ? "" : "s");

  async function getJson(path) {
    const options = typeof AbortSignal !== "undefined" && AbortSignal.timeout ? { signal: AbortSignal.timeout(4000) } : {};
    const response = await fetch(path, options);
    if (!response.ok) throw new Error("Request failed (" + response.status + ")");
    return response.json();
  }

  async function refreshStatus() {
    try {
      model.status = await getJson("/api/status");
      model.offline = false;
    } catch (e) {
      model.offline = true;
    }
    render();
  }

  async function refreshEvents() {
    try {
      model.events = (await getJson("/api/calendar/events")).events || [];
    } catch (e) {
      // keep the last list
    }
    render();
  }

  // The scenery is drawn once, from body[data-bg]: the skyline unless the page says otherwise.
  let drawnScene = null;
  function drawScene() {
    const name = document.body.dataset.bg || "skyline";
    if (name === drawnScene) return;
    drawnScene = name;
    el("scene").innerHTML = SCENES[name] || "";
  }

  // The state (free, soon, occupied, offline, loading) colours the rail, the chip and the scenery through the CSS;
  // the chip says it in words and the detail line adds what the room is for.
  function setStatus(state, chipText, detail) {
    document.body.dataset.state = state;
    el("chip-text").textContent = chipText;
    el("detail").textContent = detail || "";
  }

  const startMs = (ev) => new Date(ev.start_time).getTime();
  const endMs = (ev) => new Date(ev.end_time).getTime();
  const ongoingEvent = (now) => model.events.filter((ev) => startMs(ev) <= now && endMs(ev) > now).sort((a, b) => endMs(a) - endMs(b))[0] || null;
  const upcomingEvent = (now) => model.events.filter((ev) => startMs(ev) > now).sort((a, b) => startMs(a) - startMs(b))[0] || null;
  const earliest = (a, b) => (!a ? b : !b ? a : startMs(a) <= startMs(b) ? a : b);

  function render() {
    if (!model.status && !model.offline) return; // still loading: wait for the first status result
    if (model.offline) {
      setStatus("offline", "Sign not connected", "Check that Crystal Meet is running on the room's device.");
      return;
    }
    const s = model.status;
    el("room-name").textContent = s.room.name;
    el("room-location").textContent = s.room.location;
    el("room-location").hidden = !s.room.location;
    el("company").textContent = s.room.company || "";
    const m = s.meeting;
    const cal = s.calendar;
    const now = Date.now();
    el("today").hidden = !cal.connected;
    // The API's "current" only covers bookings with a video link; an in-person
    // booking still occupies the room, so fall back to today's event list.
    const current = cal.current || ongoingEvent(now);
    const next = earliest(cal.next, upcomingEvent(now));

    if (IN_PROGRESS.includes(m.state)) {
      const until = current ? " until " + fmtTime(new Date(current.end_time)) : "";
      setStatus("occupied", "In use" + until, m.title || (current ? current.title : platformName(m.platform)));
    } else if (current) {
      setStatus("occupied", "Booked until " + fmtTime(new Date(current.end_time)), current.title);
    } else if (next && new Date(next.start_time).getTime() - now <= SOON_MS) {
      const minutes = Math.max(0, Math.round((new Date(next.start_time).getTime() - now) / 60000));
      setStatus("soon", minutes === 0 ? next.title + " is starting" : next.title + " starts in " + plural(minutes, "minute"), fmtTime(new Date(next.start_time)) + " to " + fmtTime(new Date(next.end_time)));
    } else if (next) {
      setStatus("free", "Free until " + fmtTime(new Date(next.start_time)), "Next: " + next.title);
    } else {
      setStatus("free", cal.connected ? "Free for the rest of the day" : "Free", cal.connected ? "Nothing else is booked in here today." : "");
    }

    renderEvents(cal, now, current);
  }

  // The next bookings as rows: the time, the title and, for a video meeting, the platform. The one in progress is ringed
  // in the status colour (sign.css).
  function renderEvents(cal, now, current) {
    const list = el("events");
    const note = el("calendar-note");
    list.replaceChildren();
    if (!cal.connected) {
      note.textContent = "";
      return;
    }
    const upcoming = model.events.filter((ev) => new Date(ev.end_time).getTime() > now).slice(0, TODAY_ROWS);
    note.textContent = upcoming.length ? "" : "Nothing else scheduled today.";
    for (const ev of upcoming) {
      const li = document.createElement("li");
      li.className = "event";
      li.classList.toggle("now", !!current && ev.id === current.id);
      const time = document.createElement("time");
      time.dateTime = ev.start_time;
      time.textContent = fmtTime(new Date(ev.start_time));
      const text = document.createElement("div");
      const title = document.createElement("p");
      title.className = "title";
      title.textContent = ev.title;
      text.append(title);
      if (platformNames[ev.meeting_platform]) {
        const who = document.createElement("p");
        who.className = "who";
        who.textContent = platformNames[ev.meeting_platform];
        text.append(who);
      }
      li.append(time, text);
      list.append(li);
    }
  }

  function tick() {
    el("clock").textContent = fmtTime(new Date());
    el("date").textContent = fmtDate(new Date());
  }

  drawScene();
  tick();
  setInterval(tick, 1000);
  refreshStatus();
  refreshEvents();
  setInterval(refreshStatus, STATUS_EVERY_MS);
  setInterval(refreshEvents, EVENTS_EVERY_MS);
})();
