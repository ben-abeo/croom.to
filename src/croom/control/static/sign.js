(function () {
  "use strict";

  const SOON_MS = 10 * 60 * 1000;
  const STATUS_EVERY_MS = 5000;
  const EVENTS_EVERY_MS = 60000;
  const IN_PROGRESS = ["joining", "in_lobby", "connected", "leaving"];

  const el = (id) => document.getElementById(id);
  const model = { status: null, events: [], offline: false };

  const platformNames = { zoom: "Zoom", google_meet: "Google Meet", teams: "Teams", webex: "Webex" };
  const platformName = (key) => platformNames[key] || key || "";
  const fmtTime = (d) => d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
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

  function setStatus(state, kicker, headline, detail) {
    document.body.dataset.state = state;
    el("kicker").textContent = kicker;
    el("headline").textContent = headline;
    el("headline").classList.toggle("long", headline.length > 34);
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
      setStatus("offline", "Not connected", "Sign not connected", "Check that Crystal Meet is running on the room's device.");
      return;
    }
    const s = model.status;
    el("room-name").textContent = s.room.name;
    el("room-location").textContent = s.room.location;
    const m = s.meeting;
    const cal = s.calendar;
    const now = Date.now();
    el("upcoming").hidden = !cal.connected;
    // The API's "current" only covers bookings with a video link; an in-person
    // booking still occupies the room, so fall back to today's event list.
    const current = cal.current || ongoingEvent(now);
    const next = earliest(cal.next, upcomingEvent(now));

    if (IN_PROGRESS.includes(m.state)) {
      const until = current ? " until " + fmtTime(new Date(current.end_time)) : "";
      setStatus("occupied", "In use", "In use" + until, m.title || (current ? current.title : platformName(m.platform)));
    } else if (current) {
      setStatus("occupied", "Booked", "Booked until " + fmtTime(new Date(current.end_time)), current.title);
    } else if (next && new Date(next.start_time).getTime() - now <= SOON_MS) {
      const minutes = Math.max(0, Math.round((new Date(next.start_time).getTime() - now) / 60000));
      setStatus("soon", "Starting soon", minutes === 0 ? next.title + " is starting" : next.title + " starts in " + plural(minutes, "minute"), fmtTime(new Date(next.start_time)) + " to " + fmtTime(new Date(next.end_time)));
    } else if (next) {
      setStatus("free", "Available", "Free until " + fmtTime(new Date(next.start_time)), "Next: " + next.title);
    } else {
      setStatus("free", "Available", cal.connected ? "Free for the rest of the day" : "Free", cal.connected ? "Nothing else is booked in here today." : "");
    }

    renderUpcoming(cal, now);
  }

  function renderUpcoming(cal, now) {
    const list = el("events");
    const note = el("calendar-note");
    list.replaceChildren();
    if (!cal.connected) {
      note.textContent = "";
      return;
    }
    const upcoming = model.events.filter((ev) => new Date(ev.end_time).getTime() > now).slice(0, 3);
    note.textContent = upcoming.length ? "" : "Nothing else scheduled today.";
    for (const ev of upcoming) {
      const li = document.createElement("li");
      li.className = "event";
      const time = document.createElement("time");
      time.dateTime = ev.start_time;
      time.textContent = fmtTime(new Date(ev.start_time));
      const title = document.createElement("p");
      title.className = "title";
      title.textContent = ev.title;
      li.append(time, title);
      list.append(li);
    }
  }

  function tick() {
    el("clock").textContent = fmtTime(new Date());
  }

  tick();
  setInterval(tick, 1000);
  refreshStatus();
  refreshEvents();
  setInterval(refreshStatus, STATUS_EVERY_MS);
  setInterval(refreshEvents, EVENTS_EVERY_MS);
})();
