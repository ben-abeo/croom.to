(function () {
  "use strict";

  const SOON_MS = 10 * 60 * 1000;
  const STATUS_EVERY_MS = 5000;
  const EVENTS_EVERY_MS = 60000;
  const IN_PROGRESS = ["joining", "in_lobby", "connected", "leaving"];
  const TODAY_ROWS = 3;
  const WEEK_IDLE_MS = 30 * 1000;   // the week view goes back to Today after this long without a tap inside it
  const SCORE_EVERY_MS = 8000;   // with several schools on, the scoreboard strip moves to the next one this often

  const LONG_NAME = 24;   // a room name longer than this takes smaller type (.room.long in sign.css) ...
  const NAME_LINES = 2;   // ... and so does a shorter one that would take more lines than this at the large size (fitRoomName),
  const SHORT_NAME_LINES = 1;   // or than this on a short sign
  const NAME_STEP_REM = 0.1;    // and on a short sign a name that still takes more than NAME_LINES lines in the smaller type goes down this much at a time ...
  const NAME_FLOOR_REM = 1.3;   // ... until it takes NAME_LINES or reaches this size, below which it is not set
  // A short sign, 560 px high or less (the 7-inch 800 by 480 panel is one): sign.css gives up detail there, the chip says only the status (the booking's
  // title is in the Today list beside it), and a room name takes the smaller type as soon as it would take two lines, so that nothing runs off the screen.
  const shortSign = window.matchMedia("(max-height: 560px)");
  // A landscape sign narrower than 1000 px names the days of its week by their first three letters ("Wed"): a seventh of its width is less than "Wednesday" asks for.
  const narrowWeek = window.matchMedia("(orientation: landscape) and (max-width: 999px)");
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

  // The room's name takes the smaller type (.room.long) when it is longer than LONG_NAME characters, and also when it would take more than NAME_LINES
  // lines at the large size: "Sales Conference Room" is three lines of it on a 1280 px sign, and on a short sign three lines push the chip, and the
  // scoreboard strip, off the bottom. How many lines it takes is measured, with the smaller type taken off for a moment, in the layout it has now, so
  // it is measured again when the name, the sign's size or the view changes (the week sets the name in a size of its own on a short sign), when a font
  // arrives (the first measure may have been in the fallback font) and when the words of the status or the bookings beside it change (a longer chip is
  // a taller status). A poll that changes none of these measures nothing and writes nothing.
  let nameFit = null;      // what the name was last fitted to
  let fontsArrived = 0;    // how many times a font has finished loading, which is part of what it was fitted to

  function fitRoomName() {
    const room = el("room-name");
    const name = room.textContent;
    const data = document.body.dataset;
    const fitted = [name, window.innerWidth, window.innerHeight, data.view, "strip" in data, fontsArrived,   // the height decides shortSign too
      el("chip-text").textContent, el("detail").textContent, el("room-location").textContent, el("today").hidden, el("events").textContent].join("|");
    if (fitted === nameFit) return;
    nameFit = fitted;
    room.style.removeProperty("font-size");   // the sizes the stylesheet gives, afresh: a sign that has grown is let go of its steps
    let long = name.length > LONG_NAME;
    if (!long) {
      room.classList.remove("long");
      long = linesOf(room) > (shortSign.matches ? SHORT_NAME_LINES : NAME_LINES);
    }
    room.classList.toggle("long", long);
    if (shortSign.matches) stepNameDown(room, () => linesOf(room) > NAME_LINES);   // a short sign: down to two lines, whatever the room left
    stepNameDown(room, statusRunsOff);                                              // any sign: and on down while its status runs off the screen
  }

  const linesOf = (node) => Math.round(node.getBoundingClientRect().height / parseFloat(getComputedStyle(node).lineHeight));

  // Whether the sign is taller than its screen because of the status, which is the name's to cure: the Today list beside the status, when it is
  // the taller of the two, is not (and with no list beside it, in the week or without a calendar, the status is the whole of the row). The status
  // column is stretched to the row, so its words end where the chip and the detail line under it do.
  function statusRunsOff() {
    const sign = document.querySelector(".sign");
    if (sign.scrollHeight <= sign.clientHeight) return false;
    const today = el("today");
    return today.hidden || document.querySelector(".status > div").getBoundingClientRect().bottom >= today.getBoundingClientRect().bottom - 0.5;
  }

  // A name that is still too big (on a short sign: one that takes more than NAME_LINES lines even in the smaller type, "Executive Conference Room North Wing" on an
  // 800 px sign; on any sign: one with a status that runs off the screen) goes on down, NAME_STEP_REM at a time, each size on the 0.1 rem grid, until it is not
  // or has reached NAME_FLOOR_REM. Whole, as ever, and the status under it keeps its room.
  function stepNameDown(room, tooBig) {
    const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
    let size = parseFloat(getComputedStyle(room).fontSize) / rem;
    while (size > NAME_FLOOR_REM && tooBig()) {
      size = Math.max(NAME_FLOOR_REM, Math.round((size - NAME_STEP_REM) * 10) / 10);
      room.style.fontSize = size + "rem";
    }
  }

  // The state (free, soon, occupied, offline, loading) colours the rail, the chip and the ring through the CSS;
  // the chip says it in words and the detail line adds what the room is for.
  // On the SMT1019 panel the door sign app (android/door-sign) puts window.CrystalPanel on the page and colours the light bar
  // round the panel from the state; a browser has no CrystalPanel, and the sign is the same without it.
  let lightState = null;
  function setLight(state) {
    if (state === lightState || !window.CrystalPanel) return;
    lightState = state;
    window.CrystalPanel.setLight(state);
  }

  function setStatus(state, chipText, detail) {
    setData("state", state);
    setLight(state);
    setText(el("chip-text"), chipText);
    setText(el("detail"), detail || "");
  }

  const startMs = (ev) => new Date(ev.start_time).getTime();
  const endMs = (ev) => new Date(ev.end_time).getTime();
  const ongoingEvent = (now) => model.events.filter((ev) => startMs(ev) <= now && endMs(ev) > now).sort((a, b) => endMs(a) - endMs(b))[0] || null;
  const upcomingEvent = (now) => model.events.filter((ev) => startMs(ev) > now).sort((a, b) => startMs(a) - startMs(b))[0] || null;
  const earliest = (a, b) => (!a ? b : !b ? a : startMs(a) <= startMs(b) ? a : b);
  // The room's day is shown while Google answers, and while it does not from what the room last read (spec 2026-10-25): the status says
  // since when, in calendar.booking.offline_since.
  const booking = (cal) => (cal && cal.booking) || null;
  const scheduled = (cal) => !!(cal && (cal.connected || (booking(cal) && booking(cal).offline_since)));

  function render() {
    if (!model.status && !model.offline) return; // still loading: wait for the first status result
    if (model.offline) {
      setStatus("offline", "Sign not connected", "Check that Crystal Meet is running on the room's device.");
      el("sign-actions").hidden = true;   // nothing to book with: the room cannot be reached
      fitRoomName();   // its two lines of detail are a taller status than the one before
      return;
    }
    const s = model.status;
    setText(el("room-name"), s.room.name);
    fitRoomName();
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
      const when = minutes === 0 ? "is starting" : "starts in " + plural(minutes, "minute");
      const chip = shortSign.matches ? (minutes === 0 ? "Starting now" : "Starts in " + plural(minutes, "minute")) : next.title + " " + when;   // a short sign: no title in the chip
      setStatus("soon", chip, fmtTime(new Date(next.start_time)) + " to " + fmtTime(new Date(next.end_time)));
    } else if (next) {
      setStatus("free", "Free until " + fmtTime(new Date(next.start_time)), "Next: " + next.title);
    } else {
      setStatus("free", scheduled(cal) ? "Free for the rest of the day" : "Free", scheduled(cal) ? "Nothing else is booked in here today." : "");
    }

    // A booking made, ended or cancelled at either screen, or a new read of the calendar, moves the revision: read the day again now, not
    // at the minute's poll.
    const revision = booking(cal) ? booking(cal).revision : null;
    if (revision !== null && lastRevision !== null && revision !== lastRevision) refreshEvents();
    lastRevision = revision;
    renderEvents(cal, now, current);
    renderActions(cal, current, IN_PROGRESS.includes(m.state));
    drawView();
    drawScore();
  }

  // The next bookings as rows: the time, the title and, for a video meeting, the platform. The one in progress is ringed
  // in the status colour (sign.css).
  // A booking made at the room's screens (it has a booking_id) says when it has not reached Google yet, or what it is double-booked with,
  // and one still to start has Cancel (a second tap confirms it). The list is drawn again only when what it shows changes, so that a
  // poll between the two taps of a Cancel leaves the button as it is.
  let eventsKey = null;
  let lastRevision = null;
  function renderEvents(cal, now, current) {
    const list = el("events");
    const note = el("calendar-note");
    const upcoming = scheduled(cal) ? model.events.filter((ev) => new Date(ev.end_time).getTime() > now).slice(0, TODAY_ROWS) : [];
    const b = booking(cal);
    const offline = b && b.offline_since && !cal.connected ? "Calendar offline since " + fmtTime(new Date(b.offline_since)) + ". Bookings made here will sync." : "";
    const key = JSON.stringify([scheduled(cal), current && current.id, upcoming, offline, Boolean(b)]);
    if (key === eventsKey) return;
    eventsKey = key;
    list.replaceChildren();
    if (!scheduled(cal)) {
      note.textContent = "";
      return;
    }
    note.textContent = [upcoming.length ? "" : "Nothing else scheduled today.", offline].filter(Boolean).join(" ");
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
      const about = platformNames[ev.meeting_platform] || (ev.clash ? "Double-booked with " + ev.clash : ev.pending ? "Not in Google Calendar yet" : "");
      if (about) {
        const who = document.createElement("p");
        who.className = "who";
        who.textContent = about;
        text.append(who);
      }
      li.append(time, text);
      if (b && ev.booking_id && startMs(ev) > now) {
        li.classList.add("mine");
        const cancel = make("button", "button small cancel");
        cancel.type = "button";
        li.append(window.CroomBooking.confirmButton(cancel, "Cancel", "Tap again to cancel", ev.booking_id, "cancel", "door sign", bookingChanged));
      }
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
    const connected = scheduled(cal);
    const showBooking = bookingOpen;
    const showWeek = weekOpen && connected && !showBooking;
    el("today").hidden = !connected || showWeek || showBooking;
    el("week").hidden = !showWeek;
    setData("view", showBooking ? "book" : showWeek ? "week" : "today");
    fitRoomName();
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
    const name = make("h3", "day-name", narrowWeek.matches ? day.label.slice(0, 3) : day.label);
    if (name.textContent !== day.label) name.setAttribute("aria-label", day.label);   // the whole name for a screen reader
    head.append(name, date);
    li.append(head);
    if (!events.length) {
      li.classList.add("empty");   // an upright sign gives it a row of its own height (sign.css)
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
    const key = JSON.stringify([drawn, current ? current.id : null, narrowWeek.matches]);
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
      fitRoomName();
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
    fitRoomName();
  }

  function rotateScore() {
    const { usable } = scoreEntries();
    if (usable.length < 2) return;
    const at = usable.findIndex((e) => e.school === scoreShown);
    scoreShown = usable[(at + 1) % usable.length].school;
    drawScore();
  }

  // Book and Use now (spec 2026-10-25), under the room's status: Use now while the room is free long enough for 15 minutes, Book whenever
  // the room has a calendar to book in. A meeting in progress is ended at the room's own touch screen, never from the hallway: the sign has
  // no End now. Drawn again only when which of them show changes.
  let actionsKey = null;
  function renderActions(cal, current, inMeeting) {
    const b = booking(cal);
    const box = el("sign-actions");
    const useNow = !!b && !current && !inMeeting && b.now.minutes.length > 0;
    const key = JSON.stringify([Boolean(b), useNow]);
    if (key === actionsKey) return;
    actionsKey = key;
    const buttons = [];
    if (useNow) buttons.push(actionButton("Use now", "use-now", "primary", () => openBooking("now")));
    if (b) buttons.push(actionButton("Book", "book", "", () => openBooking("book")));
    box.replaceChildren(...buttons);
    box.hidden = !buttons.length;
    fitRoomName();
  }

  function actionButton(label, id, kind, onClick) {
    const button = make("button", "button" + (kind ? " " + kind : ""), label);
    button.type = "button";
    button.id = id;
    button.addEventListener("click", onClick);
    return button;
  }

  // The booking steps (booking.js) take the sign's width while they are up; they close by themselves after a minute left alone.
  let bookingOpen = false;
  function openBooking(mode) {
    const b = booking(model.status && model.status.calendar);
    if (!b) return;
    if (weekOpen) closeWeek();
    bookingOpen = true;
    drawView();
    window.CroomBooking.open(el("booking"), {
      mode: mode, source: "door sign", now: b.now,
      onClose: (changed) => { bookingOpen = false; drawView(); if (changed) bookingChanged(); },
    });
  }

  function bookingChanged() {
    eventsKey = actionsKey = null;
    refreshStatus();
    refreshEvents();
  }

  function tick() {
    el("clock").textContent = fmtTime(new Date());
    el("date").textContent = fmtDate(new Date());
  }

  window.addEventListener("resize", fitRoomName);
  if (shortSign.addEventListener) shortSign.addEventListener("change", render);   // the chip's words and the name's type follow the sign across 560 px
  if (narrowWeek.addEventListener) narrowWeek.addEventListener("change", drawView);   // and the days' names across 1000 px, and when it is turned
  if (document.fonts && document.fonts.addEventListener) document.fonts.addEventListener("loadingdone", () => { fontsArrived += 1; fitRoomName(); });
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
