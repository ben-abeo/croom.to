(function () {
  "use strict";

  window.__tvLoadedAt = Date.now(); // a style change re-renders; it never reloads the page

  const SOON_MS = 10 * 60 * 1000;
  const STATUS_EVERY_MS = 2000;
  const EVENTS_EVERY_MS = 60000;
  const IN_PROGRESS = ["joining", "in_lobby", "connected", "leaving"];
  const STYLES = ["info", "quiet", "brand", "bounce"];
  const PALETTE = ["#1B52E5", "#BDCEFF", "#FFFFFF", "#6C92F5", "#1FA971", "#E8A013"];
  const SPEED = 140; // pixels per second

  const el = (id) => document.getElementById(id);
  const model = { status: null, events: [], offline: false, style: "info" };
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
      if (STYLES.includes(model.status.screensaver)) model.style = model.status.screensaver;
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

  // The idle camera preview (spec 2026-10-08, section 4.7): on while the room page's Camera panel is
  // open and no meeting runs. One stream at a time, and every way out stops its tracks, so the camera
  // is free again before a meeting provider needs it.
  const preview = (function () {
    const video = el("camera-preview");
    const caption = el("preview-caption");
    let wanted = false;   // the preview is asked for right now
    let opening = false;  // a getUserMedia call is pending
    let stream = null;    // the live stream
    let failed = false;   // the camera did not open, and the preview is still asked for

    function setFailed(value) {
      failed = value;
      caption.textContent = failed ? "Camera preview unavailable" : "Camera preview";
    }

    function release(s) {
      s.getTracks().forEach((t) => t.stop());
    }

    function stop() {
      if (stream) release(stream);
      stream = null;
      if (video.srcObject) video.srcObject = null;
    }

    // A camera that goes away mid-preview ends its tracks, and the picture would freeze under a caption that
    // still says "Camera preview": let the stream go, say so, and let the next poll try again.
    function adopt(opened) {
      stream = opened;
      video.srcObject = opened;
      opened.getTracks().forEach((t) => t.addEventListener("ended", () => {
        if (stream !== opened) return;   // the track of a stream that was already let go
        stop();
        setFailed(true);
      }));
    }

    async function start() {
      opening = true;
      let opened = null;
      try {
        opened = await navigator.mediaDevices.getUserMedia({ video: true });
      } catch (e) {
        // no camera, no permission or a busy camera: the caption says so, and the next poll tries again
      }
      opening = false;
      if (!wanted) {
        // The preview ended while the camera was opening: nobody is waiting for this stream.
        if (opened) release(opened);
        return;
      }
      if (opened) adopt(opened);
      setFailed(!opened);
    }

    // Leaving the page, as a meeting provider takes the browser over, lets the camera go too.
    window.addEventListener("pagehide", () => {
      wanted = false;
      stop();
    });

    return {
      // Shows or ends the preview for this status; true while it is shown
      update(status) {
        wanted = Boolean(status && status.camera && status.camera.preview && !IN_PROGRESS.includes(status.meeting.state));
        video.hidden = !wanted;
        caption.hidden = !wanted;
        if (wanted) {
          document.body.dataset.preview = "on";
          if (!stream && !opening) start();
        } else {
          delete document.body.dataset.preview;
          if (failed) setFailed(false);
          stop();
        }
        return wanted;
      },
    };
  })();

  function render() {
    document.body.dataset.style = model.style;
    el("brand-logo").hidden = model.style !== "brand";
    el("bounce-logo").hidden = model.style !== "bounce";
    const previewing = preview.update(model.offline ? null : model.status);
    bounce.setActive(model.style === "bounce" && !previewing);   // no animation frames under the preview, which hides the logo
    if (!model.status && !model.offline) return; // still loading: wait for the first status result
    if (model.offline) {
      setStatus("offline", "Not connected", "Not connected", "Crystal Meet is not answering on this device.");
      return;
    }
    const s = model.status;
    el("room-name").textContent = s.room.name;
    const m = s.meeting;
    const cal = s.calendar;
    const now = Date.now();
    // The API's "current" only covers bookings with a video link; an in-person
    // booking still occupies the room, so fall back to today's event list.
    const current = cal.current || ongoingEvent(now);
    const next = earliest(cal.next, upcomingEvent(now));

    if (IN_PROGRESS.includes(m.state)) {
      setStatus("occupied", "In use", "In use", m.title || "");
    } else if (current) {
      setStatus("occupied", "Booked", "Booked until " + fmtTime(new Date(current.end_time)), current.title);
    } else if (next && startMs(next) - now <= SOON_MS) {
      const minutes = Math.max(0, Math.round((startMs(next) - now) / 60000));
      setStatus("soon", "Starting soon", minutes === 0 ? next.title + " is starting" : next.title + " starts in " + plural(minutes, "minute"), fmtTime(new Date(next.start_time)) + " to " + fmtTime(new Date(next.end_time)));
    } else if (next) {
      setStatus("free", "Available", "Free until " + fmtTime(new Date(next.start_time)), "Next: " + next.title);
    } else {
      setStatus("free", "Available", cal.connected ? "Free for the rest of the day" : "Free", cal.connected ? "Nothing else is booked in here today." : "");
    }
  }

  // The DVD-style bounce: constant speed, a new brand colour at every edge.
  const bounce = (function () {
    const box = el("bounce-logo");
    let active = false, x = 40, y = 40, dx = 1, dy = 1, colour = 0, last = null, frame = null;
    function paint() {
      box.style.transform = "translate(" + Math.round(x) + "px, " + Math.round(y) + "px)";
      box.style.color = PALETTE[colour];
    }
    function step(ts) {
      if (!active) return;
      const dt = last === null ? 0 : Math.min(0.05, (ts - last) / 1000);
      last = ts;
      const maxX = Math.max(0, window.innerWidth - box.offsetWidth);
      const maxY = Math.max(0, window.innerHeight - box.offsetHeight);
      x += dx * SPEED * dt;
      y += dy * SPEED * dt;
      let hit = false;
      if (x <= 0) { x = 0; dx = 1; hit = true; } else if (x >= maxX) { x = maxX; dx = -1; hit = true; }
      if (y <= 0) { y = 0; dy = 1; hit = true; } else if (y >= maxY) { y = maxY; dy = -1; hit = true; }
      if (hit) colour = (colour + 1) % PALETTE.length;
      paint();
      frame = requestAnimationFrame(step);
    }
    return {
      setActive(on) {
        if (on === active) return;
        active = on;
        last = null;
        if (frame) cancelAnimationFrame(frame);
        frame = null;
        if (active) {
          paint();
          frame = requestAnimationFrame(step);
        }
      },
    };
  })();

  // The logo file is white; inlined with currentColor it can take the bounce colours.
  async function inlineLogo() {
    try {
      const response = await fetch("/static/crystalpm-logo-white.svg");
      if (!response.ok) return;
      const svg = (await response.text()).replace(/<\?xml[^>]*\?>/, "").replace(/#ffffff\b|#fff\b/gi, "currentColor");
      el("bounce-logo").innerHTML = svg;
    } catch (e) {
      // the empty placeholder stays; the page still works
    }
  }

  function tick() {
    el("clock").textContent = fmtTime(new Date());
  }

  inlineLogo();
  tick();
  setInterval(tick, 10000);
  refreshStatus();
  refreshEvents();
  setInterval(refreshStatus, STATUS_EVERY_MS);
  setInterval(refreshEvents, EVENTS_EVERY_MS);
})();
