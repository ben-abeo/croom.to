(function () {
  "use strict";

  const SOON_MS = 10 * 60 * 1000;
  const STATUS_EVERY_MS = 5000;
  const EVENTS_EVERY_MS = 60000;
  const IN_PROGRESS = ["joining", "in_lobby", "connected", "leaving"];
  const TODAY_ROWS = 3;
  const WEEK_IDLE_MS = 30 * 1000;   // the week view goes back to Today after this long without a tap inside it
  const SCORE_EVERY_MS = 8000;   // with several schools on, the scoreboard strip moves to the next one this often

  const LONG_NAME = 24;   // a room name longer than this takes smaller type (.room.long in sign.css)
  const PHOTO_TIMEOUT_MS = 20 * 1000;   // a photograph not decoded by then counts as failed: a connection gone half open neither answers nor fails

  // The backgrounds are photographs the room serves, static/backgrounds/<key>.jpg; "plain" has none. The sign shows the one the
  // room's settings name once it has loaded, and the navy with a glow when it cannot.
  const BACKGROUND = /^[a-z_]+$/;
  const photoUrl = (key) => "/static/backgrounds/" + key + ".jpg";

  const el = (id) => document.getElementById(id);
  const model = { status: null, events: [], week: null, weekFailed: false, offline: false };

  const platformNames = { zoom: "Zoom", google_meet: "Google Meet", teams: "Teams", webex: "Webex" };
  const platformName = (key) => platformNames[key] || key || "";
  // Times read the American way whatever the browser's locale: 12-hour with AM or PM, and the date as "Monday, October 13, 2026"
  // (the Pi's Chromium would otherwise follow the system locale and show 24-hour times).
  const fmtTime = (d) => d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true }).replace(/\s+(?=[AP]M$)/, "\u00A0");   // no-break space: a time never splits across lines
  const fmtDate = (d) => d.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric" });
  // A day the room's calendar names ("2026-10-14") is a date, not an instant: it is read as the browser's own day, so no time zone moves it.
  const fmtDay = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d).toLocaleDateString("en-US", { month: "short", day: "numeric" }); };
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

  // A failed request leaves the week as it was; the next opening asks again.
  async function refreshWeek() {
    try {
      const body = await getJson("/api/calendar/week");
      if (!Array.isArray(body.days) || !body.days.every((day) => day && Array.isArray(day.events))) throw new Error("the week is not a list of days");
      model.week = body.days;
      model.weekFailed = false;
    } catch (e) {
      model.weekFailed = true;
    }
    drawView();
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
    drawView();
    drawScore();
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

  // The week view (spec 2026-10-13, section 2.3). This week takes the Today list's place with the next seven days, today first, from
  // /api/calendar/week; Back, or thirty seconds without a tap inside it, brings the Today list back. Asked for when it opens, and shown
  // from what came back: a day with nothing booked says so. The sign never scrolls, so a day with more bookings than its box holds
  // shows the ones that fit and "+N more" (fitWeek).
  let weekOpen = false;
  let weekTimer = null;
  let weekKey = null;   // what the days were last drawn from, so that a poll that changes nothing draws nothing

  function openWeek() {
    weekOpen = true;
    model.week = null;
    model.weekFailed = false;
    armWeekReturn();
    drawView();
    el("week-back").focus({ preventScroll: true });
    refreshWeek();
  }

  function closeWeek() {
    const hadFocus = el("week").contains(document.activeElement);
    weekOpen = false;
    clearTimeout(weekTimer);
    weekTimer = null;
    weekKey = null;
    drawView();
    if (hadFocus) el("this-week").focus({ preventScroll: true });
  }

  // Thirty seconds from now on the page's own clock; every tap inside the week view starts them again.
  function armWeekReturn() {
    clearTimeout(weekTimer);
    weekTimer = weekOpen ? setTimeout(closeWeek, WEEK_IDLE_MS) : null;
  }

  // Which of the Today list and the week shows, and the week's days when it is the week. Both need the calendar. Called at the end of
  // every render, and when the week opens or closes, so that those work while the room cannot be reached (render returns early then).
  function drawView() {
    const cal = model.status && model.status.calendar;
    const connected = !!(cal && cal.connected);
    const showWeek = weekOpen && connected;
    el("today").hidden = !connected || showWeek;
    el("week").hidden = !showWeek;
    setData("view", showWeek ? "week" : "today");
    if (showWeek) renderWeek(Date.now(), cal.current || ongoingEvent(Date.now()));
  }

  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  function slotElement(ev, current) {
    const li = make("li", "slot");
    li.classList.toggle("now", !!current && ev.id === current.id);   // ringed in the status colour, as on the Today list
    const time = make("time", "", fmtTime(new Date(ev.start_time)));
    time.dateTime = ev.start_time;
    li.append(time, make("p", "title", ev.title));
    if (platformNames[ev.meeting_platform]) li.append(make("p", "who", platformNames[ev.meeting_platform]));
    return li;
  }

  function dayElement(day, events, isToday, current) {
    const li = make("li", "day");
    if (isToday) li.setAttribute("aria-current", "date");
    const head = make("div", "day-head");
    const date = make("time", "day-date", fmtDay(day.date));
    date.dateTime = day.date;
    head.append(make("h3", "day-name", day.label), date);
    li.append(head);
    if (!events.length) {
      li.append(make("p", "none", "Nothing booked"));
      return li;
    }
    const slots = make("ul", "slots");
    slots.append(...events.map((ev) => slotElement(ev, current)));
    const more = make("li", "more");
    more.hidden = true;
    slots.append(more);
    li.append(slots);
    return li;
  }

  // Today's own column leaves out what is over, as the Today list does.
  function renderWeek(now, current) {
    const days = model.week;
    setText(el("week-note"), days ? "" : model.weekFailed ? "The week could not be loaded." : "Loading the week.");
    const drawn = (days || []).map((day, n) => ({ day, events: n === 0 ? day.events.filter((ev) => endMs(ev) > now) : day.events }));
    const key = JSON.stringify([drawn, current ? current.id : null]);
    if (key === weekKey) return;
    weekKey = key;
    el("week-days").replaceChildren(...drawn.map(({ day, events }, n) => dayElement(day, events, n === 0, current)));
    fitWeek();
  }

  // Of a day's bookings, the ones that fit its box show, and "+N more" counts the rest. Run when the days are drawn and whenever the
  // box they sit in changes size (the sign turned on its stand, a different screen, a taller status when the room's state changes, the
  // scoreboard strip coming up); each day starts from all its bookings, so a bigger box shows more again.
  function fitWeek() {
    if (el("week").hidden) return;
    for (const slots of el("week-days").querySelectorAll(".slots")) {
      const rows = [...slots.querySelectorAll(".slot")];
      const more = slots.querySelector(".more");
      for (const row of rows) row.hidden = false;
      more.hidden = true;
      let shown = rows.length;
      while (shown > 0 && slots.scrollHeight > slots.clientHeight) {
        shown -= 1;
        rows[shown].hidden = true;
        more.textContent = "+" + (rows.length - shown) + " more";
        more.hidden = false;
      }
    }
  }

  // The scoreboard strip (spec 2026-10-13, section 2.5), along the bottom: the last game of a school the room has on at the left, its next
  // game at the right, the ranks in burnt orange (sign.css). The status carries "scores" only while a school is on, one entry for each:
  // {school, name, last, next}, where last and next are null when the feed has told the room nothing yet. An entry with neither is left
  // out; with none left, or none sent, the strip is hidden, and nothing else on the sign moves for it. With several schools on, the strip
  // shows one at a time, its name leading the line, and moves to the next every eight seconds on the page's own clock. The status is the
  // room's own, but what it carries is checked all the same: an entry that is not a whole game is left out and is not an error.
  let scoreShown = null;   // the school whose lines are on the strip
  let scoreKey = null;     // what they were drawn from, so that a poll that changes nothing writes nothing

  const isName = (value) => typeof value === "string" && value.trim() !== "";
  const isScore = (value) => typeof value === "number" && Number.isFinite(value);
  const rankOf = (value) => (Number.isInteger(value) && value >= 1 && value <= 25 ? value : null);   // a place in the Top 25, or no rank
  const dayText = (iso) => { const [, month, day] = iso.split("-").map(Number); return month + "/" + day; };

  // The last game as its two sides in the order they are printed: the school's own first, whether it was at home or away.
  function lastGame(game) {
    if (!game || !isName(game.home) || !isName(game.away) || !isScore(game.home_score) || !isScore(game.away_score)) return null;
    const home = { name: game.home, rank: rankOf(game.home_rank), score: game.home_score };
    const away = { name: game.away, rank: rankOf(game.away_rank), score: game.away_score };
    return game.school_home === false ? [away, home] : [home, away];
  }

  function nextGame(game) {
    if (!game || !isName(game.home) || !isName(game.away) || !/^\d{4}-\d{2}-\d{2}$/.test(game.date)) return null;
    return {
      date: game.date, neutral: game.neutral === true,   // at a neutral site the side listed home is no host: "vs", not "@"
      away: { name: game.away, rank: rankOf(game.away_rank) }, home: { name: game.home, rank: rankOf(game.home_rank) },
    };
  }

  function scoreEntries() {
    const sent = model.status && Array.isArray(model.status.scores) ? model.status.scores : [];
    const entries = sent.filter((entry) => entry && typeof entry.school === "string");
    const usable = [];
    for (const entry of entries) {
      const last = lastGame(entry.last), next = nextGame(entry.next);
      if (last || next) usable.push({ school: entry.school, name: isName(entry.name) ? entry.name : entry.school, last, next });
    }
    return { usable, several: entries.length > 1 };
  }

  // "Texas (#4) 45": a team, its rank in orange and its score, one unit that does not split across lines.
  function side(team, score) {
    const unit = make("span", "side");
    unit.append(team.name);
    if (team.rank) unit.append(" ", make("span", "rank", "(#" + team.rank + ")"));
    if (score !== undefined) unit.append(" " + score);
    return unit;
  }

  function drawScore() {
    const { usable, several } = scoreEntries();
    const strip = el("score");
    if (!usable.length) {
      if (!strip.hidden) strip.hidden = true;
      setData("strip", null);
      scoreShown = scoreKey = null;
      return;
    }
    const entry = usable.find((e) => e.school === scoreShown) || usable[0];
    scoreShown = entry.school;
    const lead = several ? entry.name : null;
    const key = JSON.stringify([lead, entry]);
    if (key !== scoreKey) {
      scoreKey = key;
      const last = [];
      if (lead) last.push(lead + (entry.last ? ": " : ""));
      if (entry.last) last.push(side(entry.last[0], entry.last[0].score), " \u2013 ", side(entry.last[1], entry.last[1].score));
      el("score-last").replaceChildren(...last);
      const next = entry.next;
      el("score-next").replaceChildren(...(next ? ["Next: " + dayText(next.date) + " \u00b7 ", side(next.away), next.neutral ? " vs " : " @ ", side(next.home)] : []));
    }
    if (strip.hidden) strip.hidden = false;
    setData("strip", "");   // body[data-strip]: the strip is up, which a short sign's rules (sign.css) count in the height they have to share
  }

  function rotateScore() {
    const { usable } = scoreEntries();
    if (usable.length < 2) return;
    const at = usable.findIndex((e) => e.school === scoreShown);
    scoreShown = usable[(at + 1) % usable.length].school;
    drawScore();
  }

  function tick() {
    el("clock").textContent = fmtTime(new Date());
    el("date").textContent = fmtDate(new Date());
  }

  el("this-week").addEventListener("click", openWeek);
  el("week-back").addEventListener("click", closeWeek);
  el("week").addEventListener("pointerdown", armWeekReturn);
  if (typeof ResizeObserver === "function") new ResizeObserver(fitWeek).observe(el("week-days"));
  tick();
  setInterval(tick, 1000);
  refreshStatus();
  refreshEvents();
  setInterval(refreshStatus, STATUS_EVERY_MS);
  setInterval(refreshEvents, EVENTS_EVERY_MS);
  setInterval(rotateScore, SCORE_EVERY_MS);
})();
