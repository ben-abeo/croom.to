(function () {
  "use strict";

  const SOON_MS = 10 * 60 * 1000;
  const STATUS_EVERY_MS = 5000;
  const EVENTS_EVERY_MS = 60000;
  const IN_PROGRESS = ["joining", "in_lobby", "connected", "leaving"];
  const TODAY_ROWS = 3;

  const LONG_NAME = 24;   // a room name longer than this takes smaller type (.room.long in sign.css)
  const PHOTO_TIMEOUT_MS = 20 * 1000;   // a photograph not decoded by then counts as failed: a connection gone half open neither answers nor fails

  // The backgrounds are photographs the room serves, static/backgrounds/<key>.jpg; "plain" has none. The sign shows the one the
  // room's settings name once it has loaded, and the navy with a glow when it cannot.
  const BACKGROUND = /^[a-z_]+$/;
  const photoUrl = (key) => "/static/backgrounds/" + key + ".jpg";

  const el = (id) => document.getElementById(id);
  const model = { status: null, events: [], offline: false };

  const platformNames = { zoom: "Zoom", google_meet: "Google Meet", teams: "Teams", webex: "Webex" };
  const platformName = (key) => platformNames[key] || key || "";
  // Times read the American way whatever the browser's locale: 12-hour with AM or PM, and the date as "Monday, October 13, 2026"
  // (the Pi's Chromium would otherwise follow the system locale and show 24-hour times).
  const fmtTime = (d) => d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true }).replace(/\s+(?=[AP]M$)/, "\u00A0");   // no-break space: a time never splits across lines
  const fmtDate = (d) => d.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric" });
  const plural = (n, word) => n + " " + word + (n === 1 ? "" : "s");

  const timeout = () => (typeof AbortSignal !== "undefined" && AbortSignal.timeout ? { signal: AbortSignal.timeout(4000) } : {});

  async function getJson(path) {
    const response = await fetch(path, timeout());
    if (!response.ok) throw new Error("Request failed (" + response.status + ")");
    return response.json();
  }

  async function refreshStatus() {
    try {
      model.status = await getJson("/api/status");
      model.offline = false;
    } catch (e) {
      model.offline = true;
      forgetFailedPhotos();   // the room may be restarting after an update, which may have brought a photograph
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

  // Writes only what changed: the status words are aria-live, where the same words written again are announced again, and
  // the backdrop is drawn when the room's choice first arrives or changes, not at every poll.
  const setText = (node, value) => { if (node.textContent !== value) node.textContent = value; };
  const setData = (name, value) => {   // value null removes the attribute
    const data = document.body.dataset;
    if (value === null) { if (name in data) delete data[name]; } else if (data[name] !== value) data[name] = value;
  };

  // The photograph behind the sign. body[data-bg] is the room's choice (settings.sign_background in the status). The photograph is
  // loaded once, as an image, and shown only when it has decoded: body[data-photo] names it and --photo holds its address, and sign.css
  // draws it under the navy overlay. Until then the sign keeps what it shows: a change of background keeps the old photograph until the
  // new one has decoded, but a first load, or a reload, shows navy for a moment first (about 0.3 s with the picture cached, 1.3 s cold on
  // a slow link). "plain", and a photograph that fails to load (the room has no file for it, it is not an image, or it has not decoded
  // in twenty seconds), show the navy with the glow instead (a failed load also sets body[data-photo-missing]).
  const photos = new Map();   // key -> the decoded Image (kept, so that the style sheet draws that one and the room is asked once), or false when it failed
  const loading = new Set();
  let wanted = "skyline";

  function showPhoto(key, missing) {
    setData("photo", key);
    setData("photoMissing", missing ? "" : null);
    if (key) document.body.style.setProperty("--photo", "url('" + photoUrl(key) + "')");
    else document.body.style.removeProperty("--photo");
  }

  // What the sign shows follows what it has loaded: a choice that is still loading leaves the sign as it is.
  function showBackground() {
    setData("bg", wanted);
    if (wanted === "plain") { showPhoto(null, false); return; }
    const photo = photos.get(wanted);
    if (photo !== undefined) showPhoto(photo ? wanted : null, !photo);
  }

  async function loadPhoto(key) {
    if (loading.has(key)) return;
    loading.add(key);
    const image = new Image();
    image.src = photoUrl(key);
    let timer = null;
    const giveUp = new Promise((resolve, reject) => { timer = setTimeout(() => reject(new Error("the photograph did not decode in time")), PHOTO_TIMEOUT_MS); });
    try {
      await Promise.race([image.decode(), giveUp]);
      photos.set(key, image);
    } catch (e) {
      image.src = "";   // lets go of a request still in flight
      photos.set(key, false);
    } finally {
      clearTimeout(timer);
    }
    loading.delete(key);
    showBackground();
  }

  function chooseBackground(key) {
    wanted = key;
    showBackground();
    if (key !== "plain" && !photos.has(key)) loadPhoto(key);
  }

  // A room that goes away and comes back may have been updated, with a photograph it lacked: what failed is asked for again, once,
  // when the room is back.
  function forgetFailedPhotos() {
    for (const [key, photo] of photos) if (!photo) photos.delete(key);
  }

  // The state (free, soon, occupied, offline, loading) colours the rail, the chip and the ring through the CSS;
  // the chip says it in words and the detail line adds what the room is for.
  function setStatus(state, chipText, detail) {
    setData("state", state);
    setText(el("chip-text"), chipText);
    setText(el("detail"), detail || "");
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
    setText(el("room-name"), s.room.name);
    el("room-name").classList.toggle("long", s.room.name.length > LONG_NAME);
    setText(el("room-location"), s.room.location);
    el("room-location").hidden = !s.room.location;
    setText(el("company"), s.room.company || "");
    chooseBackground(s.settings && BACKGROUND.test(s.settings.sign_background) ? s.settings.sign_background : "skyline");
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

  tick();
  setInterval(tick, 1000);
  refreshStatus();
  refreshEvents();
  setInterval(refreshStatus, STATUS_EVERY_MS);
  setInterval(refreshEvents, EVENTS_EVERY_MS);
})();
