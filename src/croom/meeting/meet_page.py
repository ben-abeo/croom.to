"""
The page scripts Google Meet's call page is read and driven with (spec 2026-10-30 Meet controls, section 4), in one place so that a change
Meet makes to its page is fixed here. Each is a function of the page, run with `page.evaluate`, that returns plain data.

Phase A: the boxes Meet draws over the call (its notices, "Someone wants to join", "Leave call?").
Phase B: the call's buttons (READ_CALL), the panels the room opens (people, chat, host controls), the reactions picker, and the drivers
that press by label (PRESS_BY_LABEL, and STATE_BY_LABEL to look without pressing), press inside the menu a person's own row opened
(PERSON_MENU), press a reaction (PRESS_REACTION), and give the chat box the keyboard (FOCUS_CHAT_BOX, CHAT_BOX_STATE).
"""

import json

# Every word and shortcut the room drives Meet with (Meet's English, not yet confirmed on a room Pi: the capture is what corrects it, here
# and nowhere else). It is a page script's argument, as `MEET` or as the "meet" of a dict that carries the script's own arguments.
# A list of words is a list of alternatives: an element matches when its accessibility label (its aria-label, else what its aria-labelledby
# names, else its drawn text, else its title) starts with one of them, in any case and at a word's end ("Pin" is "Pin Pat to your main
# screen" and not "Pinned"). A label is looked for in the order its alternatives are listed.
MEET = {
    "chat": {"labels": ["Chat with everyone", "Chat"], "shortcut": "Control+Alt+c"},             # the button that opens the chat panel
    "people": {"labels": ["Show everyone", "People"], "shortcut": "Control+Alt+p"},             # the button that opens the people panel
    "host_controls": ["Host controls"],         # the button that opens the host panel (drawn only for a host or co-host)
    "reactions": ["Send a reaction", "Reactions"],
    "more_options": ["More options"],
    "change_layout": ["Change layout"],        # the more-options menu's item
    "layouts": {"auto": ["Auto", "Auto (dynamic)"], "tiled": ["Tiled"], "spotlight": ["Spotlight"], "sidebar": ["Sidebar"]},
    "hide_self": ["Hide self view", "Hide yourself"],
    "show_self": ["Show self view", "Show yourself"],
    "pin": ["Pin"],
    "unpin": ["Unpin"],
    "chat_box": ["Send a message", "Send a message to everyone"],            # the message box (a text field) ...
    "chat_send": ["Send a message", "Send message", "Send"],                 # ... and its button (a button: the box has the same words)
    "admit": ["Admit"],
    "deny": ["Deny", "Deny entry"],
    "admit_all": ["Admit all"],
    "remove": ["Remove from the call", "Remove"],
    "mute_all": ["Mute all", "Mute everyone"],
    "end_for_all": ["End the call for everyone", "End call for everyone"],
    "waiting": ["Waiting to join", "Waiting to be admitted"],              # the heading or label of the people panel's section of knocks
    "host_switches": {"share_screen": ["Share their screen"], "chat": ["Send chat messages"], "reactions": ["Send reactions"],
                      "microphone": ["Turn on their microphone"], "video": ["Turn on their video"], "quick_access": ["Quick access"]},
    # What names a side panel, kept to what only it is called: the whole title, label or first heading of a panel's container (Meet's
    # "In-call messages", a trailing " panel" ignored). A container that is an <aside> or a [role=complementary] is a panel whatever it is called.
    "panel_names": {"chat": ["In-call messages", "Chat"], "people": ["People", "Participants"], "host": ["Host controls"], "activities": ["Activities"]},
    # What marks a person's row in the people panel: its microphone is off (a label or an icon's word on it), its tile is pinned.
    "muted": ["Muted", "Microphone is off", "Mic is off", "mic_off"],
    "pinned": ["Pinned"],
    # Phase B, Task 3: what the controls press on the way to their button.
    "person_menu": ["More actions", "More options"],   # a people panel row's own menu button (or any button of the row that opens a menu)
    "tile_menu": ["More options for"],                 # a tile's menu button: the room's own is the one naming the room or "you"
    "leave": ["Leave call"],                           # opens Meet's leave menu, which has End the call for everyone, for a host
    # The button of the box Meet asks for a confirmation with ("Mute all participants?", "End the call for everyone?", "Remove … from the call?").
    "confirm": {"mute_all": ["Mute all"], "end_for_all": ["End call"], "remove": ["Remove"]},
}

# The side panels the room opens (chat, people, host controls, activities), known by their structure and never taken for a box (Review Focus 2,
# Meet controls plan Phase B): a container (an <aside>, a [role=complementary], a dialog, a region, a tab panel) is a panel when it is an <aside>
# or [role=complementary], or when its aria-labelledby, aria-label or first drawn heading is the name of one (PANEL_NAMES: the whole title, in
# any case, a trailing " panel" left off). `insideSide(el)` says an element is, or is inside, a panel; `openPanels()` gives the drawn panel
# that is the chat, the people or the host controls, by kind (the first of each). It needs `clean`, `drawn` and PANEL_NAMES.
_SIDE = r"""
  const CONTAINERS = 'aside, [role="complementary"], [role="dialog"], [role="alertdialog"], [role="region"], [role="tabpanel"]';
  const SIDE_TAGS = 'aside, [role="complementary"]';
  const titlesOf = (el) => {
    const by = (el.getAttribute("aria-labelledby") || "").split(/\s+/).map((id) => id && document.getElementById(id)).filter(Boolean)
      .map((n) => clean(n.innerText || n.textContent));
    const heading = [...el.querySelectorAll('h1, h2, h3, h4, [role="heading"]')].find((h) => drawn(h) && !h.closest("button"));
    return [...by, clean(el.getAttribute("aria-label")), heading ? clean(heading.innerText || heading.textContent) : ""].filter(Boolean);
  };
  const panelKindOf = (el) => {
    if (!el.matches(CONTAINERS)) return null;
    for (const title of titlesOf(el)) {
      const bare = title.toLowerCase().replace(/ panel$/, "");
      for (const [kind, words] of Object.entries(PANEL_NAMES)) if (words.some((w) => w.toLowerCase() === bare)) return kind;
    }
    return null;
  };
  const isSide = (el) => el.matches(SIDE_TAGS) || panelKindOf(el) !== null;
  const insideSide = (el) => { for (let p = el; p; p = p.parentElement) if (isSide(p)) return true; return false; };
  const sidePanels = () => [...document.querySelectorAll(CONTAINERS)].filter((el) => drawn(el) && isSide(el));
  const openPanels = () => {
    const found = {};
    for (const el of sidePanels()) { const kind = panelKindOf(el); if (kind && !found[kind]) found[kind] = el; }
    return found;
  };
"""

# What the two scripts below share: the boxes on the screen, each with its words and its buttons.
#
# A box is a drawn [role=dialog] or [role=alertdialog] with at least one drawn button (one with none asks nothing of anyone), and not one
# inside another (that is a part of the outer box), and not a side panel or inside one (_SIDE: a panel the room opened is never a box).
# Drawn: it takes up room on the screen, is not made invisible, and is not inside something the page hides from assistive technology. Meet gives its boxes no ids: a box is known by its title and its words.
#
# Its title is what its aria-labelledby names, else its first heading, else its aria-label. Its words are what its aria-describedby names,
# else its drawn text without the title's and the buttons'. A button's label is its aria-label, else its drawn text.
_BOXES = r"""
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const drawn = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden" && !el.closest('[aria-hidden="true"]');
  const BOX = '[role="dialog"], [role="alertdialog"]';
  const labelOf = (b) => clean(b.getAttribute("aria-label") || b.innerText || b.getAttribute("title"));
  const buttonsOf = (box) => [...box.querySelectorAll('button, [role="button"]')]
    .filter((b) => drawn(b) && !(b.parentElement && b.parentElement.closest('button, [role="button"]') && box.contains(b.parentElement.closest('button, [role="button"]'))))
    .filter((b) => labelOf(b));
  const named = (box, attribute) => (box.getAttribute(attribute) || "").split(/\s+/).map((id) => id && document.getElementById(id)).filter(Boolean);
  const textOf = (root, without) => {
    const found = [], walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode, owner = node.parentElement;
      if (!owner || /^(script|style|noscript)$/i.test(owner.tagName) || !drawn(owner)) continue;
      if (owner.closest('button, [role="button"]') || without.some((w) => w.contains(node))) continue;
      found.push(clean(node.nodeValue));
    }
    return found.filter(Boolean).join(" ");
  };
  const boxes = () => [...document.querySelectorAll(BOX)]
    .filter((box) => !(box.parentElement && box.parentElement.closest(BOX)) && drawn(box) && !insideSide(box))
    .map((box) => {
      const buttons = buttonsOf(box);
      const labelledBy = named(box, "aria-labelledby"), describedBy = named(box, "aria-describedby");
      const heading = labelledBy.length ? null : [...box.querySelectorAll('h1, h2, h3, h4, [role="heading"]')].find((h) => drawn(h) && !h.closest("button"));
      const titled = labelledBy.length ? labelledBy : heading ? [heading] : [];
      const title = titled.length ? clean(titled.map((el) => el.innerText || el.textContent).join(" ")) : clean(box.getAttribute("aria-label"));
      const text = describedBy.length ? clean(describedBy.map((el) => el.innerText || el.textContent).join(" ")) : textOf(box, titled);
      return { el: box, buttons: buttons, info: { title: title, text: text.slice(0, 400), buttons: buttons.map(labelOf) } };
    })
    .filter((box) => box.buttons.length > 0);
""" + "  const PANEL_NAMES = " + json.dumps(MEET["panel_names"]) + ";\n" + _SIDE

# Every box on the screen now, as [{"title", "text", "buttons": [labels, in order]}].
READ_DIALOGS = "() => {" + _BOXES + "  return boxes().map((box) => box.info);\n}"

# Press the button labelled `label` in the box whose title and words are `title` and `text`: "pressed", or "gone" when no such box is on the
# screen, or "no button" when it has no drawn button by that label.
PRESS_DIALOG = "([title, text, label]) => {" + _BOXES + r"""
  const box = boxes().find((b) => b.info.title === title && b.info.text === text);
  if (!box) return "gone";
  const button = box.buttons.find((b) => labelOf(b) === label);
  if (!button) return "no button";
  button.click();
  return "pressed";
}"""


# --- The capture (spec 2026-10-30 Meet controls, section 3): what Meet draws, recorded so the Phase B controls are built against it.

_DRAWN = r"""
  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const drawn = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden" && !el.closest('[aria-hidden="true"]');
"""

# Every drawn element with a role, an accessibility label, or the semantics of a button, a text field or a list item: its tag, role, label,
# states (pressed, expanded, checked, selected, modal, haspopup, disabled), placeholder and tooltip, the names of its data-* attributes (not
# their values), its drawn text (its pieces of text with a space between; cut at 4000 characters here: the capture redacts it whole, then
# keeps the first 120), its box on the screen
# and the index of the nearest recorded element it is inside (null at the top). With them the document's title, its URL's path (no query),
# its <html> lang and the browser's user agent.
RECORD_ELEMENTS = "() => {" + _DRAWN + r"""
  const SEMANTIC = new Set(["BUTTON", "INPUT", "TEXTAREA", "LI"]);
  const all = [...document.querySelectorAll("body *")]
    .filter((el) => (el.hasAttribute("role") || el.hasAttribute("aria-label") || SEMANTIC.has(el.tagName)) && drawn(el));
  const index = new Map(all.map((el, i) => [el, i]));
  const parentOf = (el) => { for (let p = el.parentElement; p; p = p.parentElement) if (index.has(p)) return index.get(p); return null; };
  // The drawn text inside el, each piece of text apart (a name beside an icon's ligature or a button's word is not run into it).
  const shown = new Map();
  const isDrawn = (el) => { if (!shown.has(el)) shown.set(el, drawn(el)); return shown.get(el); };
  const textOf = (el) => {
    const parts = [], walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let size = 0;
    while (size < 4000 && walker.nextNode()) {
      const owner = walker.currentNode.parentElement, piece = clean(walker.currentNode.nodeValue);
      if (!piece || !owner || /^(SCRIPT|STYLE|NOSCRIPT)$/.test(owner.tagName) || !isDrawn(owner)) continue;
      parts.push(piece);
      size += piece.length + 1;
    }
    return parts.join(" ").slice(0, 4000);
  };
  const STATES = { "aria-pressed": "pressed", "aria-expanded": "expanded", "aria-checked": "checked", "aria-selected": "selected",
                   "aria-modal": "modal", "aria-haspopup": "haspopup", "aria-disabled": "disabled", "placeholder": "placeholder", "title": "tooltip" };
  return {
    title: document.title, path: location.pathname, lang: document.documentElement.lang || "", user_agent: navigator.userAgent,
    elements: all.map((el) => {
      const r = el.getBoundingClientRect();
      const e = { tag: el.tagName.toLowerCase(), role: el.getAttribute("role") || "", label: el.getAttribute("aria-label") || "",
                  text: textOf(el), box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
                  parent: parentOf(el), data: el.getAttributeNames().filter((n) => n.startsWith("data-")) };
      for (const [attribute, key] of Object.entries(STATES)) if (el.hasAttribute(attribute)) e[key] = el.getAttribute(attribute);
      return e;
    }),
  };
}"""

# What is open on Meet's page, to see a panel open and close: the drawn panels (dialogs, menus, lists that pop up, side panels, regions),
# each as {role, label, modal}; the pressed and expanded states of the first drawn element whose label matches `source` (a regular
# expression, any case), or null without one; and `count`, how many drawn elements have a role, a label or are buttons (what a press
# opened that is none of those panels, a bar of plain divs with its buttons, changes it).
_PANELS = r"""
  const PANEL = '[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"], [role="complementary"], [role="region"], [role="tabpanel"], aside';
  const panels = () => [...document.querySelectorAll(PANEL)].filter(drawn);
  const panelOf = (el) => ({ role: el.getAttribute("role") || el.tagName.toLowerCase(), label: clean(el.getAttribute("aria-label")),
                             modal: el.getAttribute("aria-modal") || "" });
  const keyOf = (p) => JSON.stringify([p.role, p.label, p.modal]);
"""
PAGE_STATE = "(source) => {" + _DRAWN + _PANELS + r"""
  const pattern = source ? new RegExp(source, "i") : null;
  const opener = pattern && [...document.querySelectorAll("[aria-label]")].find((el) => drawn(el) && pattern.test(clean(el.getAttribute("aria-label"))));
  return { panels: panels().map(panelOf),
           opener: opener ? { pressed: opener.getAttribute("aria-pressed") || "", expanded: opener.getAttribute("aria-expanded") || "" } : null,
           count: [...document.querySelectorAll("[role], [aria-label], button")].filter(drawn).length };
}"""

# The name an element is pressed by, read the way it was matched: what its aria-labelledby names, else its aria-label, else its drawn text,
# else its title; and every one of those words, for the never-pressed check.
NAMES_OF = "(el) => {" + _DRAWN + r"""
  const by = (el.getAttribute("aria-labelledby") || "").split(/\s+/).map((id) => id && document.getElementById(id)).filter(Boolean)
    .map((n) => clean(n.innerText || n.textContent)).join(" ");
  const words = [by, clean(el.getAttribute("aria-label")), clean(el.innerText), clean(el.getAttribute("title"))];
  return { name: words.find(Boolean) || "", words: words.filter(Boolean) };
}"""

# The Close button inside the panels `keys` names (as PAGE_STATE gives them, JSON [role, label, modal]), marked with
# data-croom-capture="close" for the capture to press: its name, or null when those panels have none. UNMARK_CLOSE takes the mark off.
MARK_CLOSE = "(keys) => {" + _DRAWN + _PANELS + r"""
  for (const old of document.querySelectorAll("[data-croom-capture]")) old.removeAttribute("data-croom-capture");
  const wanted = new Set(keys || []);
  for (const panel of panels().filter((el) => wanted.has(keyOf(panelOf(el))))) {
    for (const b of panel.querySelectorAll('button, [role="button"]')) {
      const name = clean(b.getAttribute("aria-label") || b.innerText || b.getAttribute("title"));
      if (drawn(b) && /^close\b/i.test(name)) { b.setAttribute("data-croom-capture", "close"); return name; }
    }
  }
  return null;
}"""
UNMARK_CLOSE = '() => { for (const el of document.querySelectorAll("[data-croom-capture]")) el.removeAttribute("data-croom-capture"); }'

# The words on Meet's page that are people's and must not leave the room in a capture: {"names", "messages"}. `kind` is the step ("people",
# "chat" or another) and `keys` the panels it opened (as PAGE_STATE gives them, JSON [role, label, modal]): the people panel's list items
# and the chat panel's messages are read inside those (or, when the step opened none that the page names, where Meet keeps them; a
# message Meet marks as one, with data-message-id or data-message-text, is read on every step). Names
# are also read, on every step, from the participant tiles and from labels that hold one ("More options for …", "Pin … to your main
# screen", the Google account's), and from a box that says someone wants to join.
GATHER_PRIVATE = "([kind, keys]) => {" + _DRAWN + _PANELS + r"""
  const names = [], messages = [];
  // An icon is known by its element, never by the shape of its word ("ben" and "hello" look like a ligature): an <i>, an <svg>, something
  // hidden from assistive technology, or text in Google's icon fonts (Material Icons / Symbols, Google Symbols).
  const ICON = 'i, svg, [aria-hidden="true"], [class*="material-icons"], [class*="material-symbols"], [class*="google-material-icons"], [class*="google-symbols"]';
  const nameLike = (line) => line.length >= 2 && line.length <= 80 && !/^you$/i.test(line) && !/^\d{1,2}:\d\d/.test(line);
  // Each piece of text drawn inside el, on its own (a name beside an icon's ligature is not run into it): not an icon's, and not on a
  // button unless `all` (a chat message's every word is taken, whatever it is on or looks like).
  const pieces = (el, all) => {
    const found = [], walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const owner = walker.currentNode.parentElement, piece = clean(walker.currentNode.nodeValue);
      if (!piece || !owner || !drawn(owner)) continue;
      if (all || (!owner.closest(ICON) && !owner.closest('button, [role="button"]'))) found.push(piece);
    }
    return found;
  };
  const wanted = new Set(keys || []);
  const opened = panels().filter((el) => wanted.has(keyOf(panelOf(el))));
  const within = (selector) => opened.length ? opened.flatMap((p) => [...p.querySelectorAll(selector)]) : [];
  const LABELS = [/^more options for (.+)$/i, /^pin (.+?) (?:to|on) /i, /^unpin (.+?)(?: from |$)/i, /^remove (.+?) from /i,
                  /^mute (.+?)'s (?:microphone|mic)/i, /^google account: (.+?)\s*\(/i];
  for (const el of document.querySelectorAll("[aria-label]")) {
    const label = clean(el.getAttribute("aria-label"));
    for (const pattern of LABELS) { const m = label.match(pattern); if (m) names.push(m[1]); }
  }
  for (const tile of document.querySelectorAll("[data-participant-id], [data-requested-participant-id]")) {
    if (drawn(tile)) names.push(...pieces(tile).filter(nameLike).slice(0, 2));
  }
  for (const box of document.querySelectorAll('[role="dialog"], [role="alertdialog"]')) {
    if (drawn(box) && /wants? to join|asking to join|waiting to join/i.test(box.innerText || "")) {
      const words = [...box.querySelectorAll("*")].filter((el) => !el.closest('button, [role="button"], h1, h2, h3, h4, [role="heading"]'));
      for (const el of words) for (const node of el.childNodes) {
        const line = node.nodeType === 3 ? clean(node.nodeValue) : "";
        if (line && nameLike(line) && !/wants? to join|asking to join|waiting to join/i.test(line)) names.push(line);
      }
    }
  }
  if (kind === "people") {
    let items = within('[role="listitem"], li');
    if (!items.length) items = [...document.querySelectorAll('[role="list"], ul')]
      .filter((list) => /participant|people|in call|in the call|contributors|waiting/i.test(list.getAttribute("aria-label") || ""))
      .flatMap((list) => [...list.querySelectorAll('[role="listitem"], li')]);
    for (const item of items) {
      const label = clean(item.getAttribute("aria-label"));
      const first = pieces(item).find(nameLike);
      if (label && nameLike(label)) names.push(label);
      if (first) names.push(first);
    }
  }
  {
    let found = [...document.querySelectorAll("[data-message-text], [data-message-id]")].filter(drawn);
    if (kind === "chat" && !found.length) found = within('[role="listitem"], li');
    for (const el of found) messages.push(...pieces(el, true));   // fails closed: every word in a message item, its time too
    if (kind === "chat" && !found.length) {
      for (const p of opened) {
        const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
          const owner = walker.currentNode.parentElement, line = clean(walker.currentNode.nodeValue);
          if (!line || !owner || !drawn(owner) || owner.closest(ICON)) continue;
          if (owner.closest('button, [role="button"], h1, h2, h3, h4, [role="heading"], label, input, textarea')) continue;
          messages.push(line);
        }
      }
    }
  }
  return { names: names.map(clean).filter(Boolean), messages: messages.map(clean).filter(Boolean) };
}"""


# --- Phase B (spec 2026-10-30 Meet controls, sections 4 and 5): the call's buttons and panels, read and driven by MEET's words.

# What the scripts below share, on top of _DRAWN and _SIDE (and `M`, the MEET table, which each script is given): the words an element is
# known by (`nameOf`: its aria-label, else what its aria-labelledby names, else its drawn text, else its title), `starts` (whether a text
# starts with one of a list of words, in any case and at a word's end), the elements a press is made on, and the text drawn in an element
# piece by piece (not an icon's, not a button's, not what is inside `skip`).
_WORDS = r"""
  const PRESSABLE = 'button, [role="button"], [role="menuitem"], [role="menuitemradio"], [role="menuitemcheckbox"], [role="option"], [role="switch"], [role="checkbox"], [role="radio"], input[type="radio"], input[type="checkbox"]';
  const ICONS = 'i, svg, [aria-hidden="true"], [class*="material-icons"], [class*="material-symbols"], [class*="google-material-icons"], [class*="google-symbols"]';
  const nameOf = (el) => {
    const by = (el.getAttribute("aria-labelledby") || "").split(/\s+/).map((id) => id && document.getElementById(id)).filter(Boolean)
      .map((n) => clean(n.innerText || n.textContent)).join(" ");
    const labelled = el.labels && el.labels.length ? clean(el.labels[0].innerText) : "";
    return clean(el.getAttribute("aria-label")) || by || clean(el.innerText) || labelled || clean(el.getAttribute("title"));
  };
  const starts = (text, words) => {
    const t = clean(text).toLowerCase();
    return Boolean(t) && words.some((w) => { const x = w.toLowerCase(); return t.startsWith(x) && !/[a-z0-9]/.test(t.charAt(x.length)); });
  };
  const shown = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  const enabled = (el) => !el.disabled && el.getAttribute("aria-disabled") !== "true";
  const pieces = (root, skip) => {
    const found = [], walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const owner = walker.currentNode.parentElement, piece = clean(walker.currentNode.nodeValue);
      if (!piece || !owner || !drawn(owner) || owner.closest(ICONS) || owner.closest('button, [role="button"]')) continue;
      if ((skip || []).some((el) => el.contains(owner))) continue;
      found.push(piece);
    }
    return found;
  };
"""

# One reading of Meet's call page (spec 2026-10-13, section 4.5; Phase B adds the last four). Each control's button is found by a label that
# names what pressing it does, and its state is what that label says: "Turn off camera" means the camera is on, "Lower hand" means the hand is
# up. A button the page does not have, or whose label names no such action, reads as null. `people` is the people button with the number in its
# label or its text; `taken` is whether something that takes typing has the keyboard (a text field, the chat box, an editable area, a frame,
# say); `chat_unread` is the number on the chat button's label or badge (0 without one); `hosting` is whether the host controls button is drawn
# (Meet draws it for a host and a co-host alike); `panels` is which of "chat", "people" and "host" is open now, in that order: a side panel drawn
# by its structure (_SIDE), or its button saying it is pressed or expanded; `drawn` says whether the chat, reactions and more options buttons are
# on the screen. Run with MEET (the table's words); without it, with this file's MEET.
READ_CALL = "(meet) => {  const M = meet || " + json.dumps(MEET) + ";\n  const PANEL_NAMES = M.panel_names;" + _DRAWN + _SIDE + _WORDS + r"""
  // Only what is drawn counts: a copy the page keeps out of sight is not the button. Two drawn buttons that say different things say nothing (null).
  const labelled = [...document.querySelectorAll("[aria-label]")].filter((el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden");
  const find = (pattern) => labelled.find((el) => pattern.test(el.getAttribute("aria-label")));
  const state = (pattern, said) => {
    const says = labelled.filter((el) => pattern.test(el.getAttribute("aria-label"))).map((el) => el.getAttribute("aria-label").match(pattern)[1].toLowerCase() === said);
    return says.length && says.every((on) => on === says[0]) ? { on: says[0] } : null;
  };
  const people = find(/show everyone|participants|^people\b/i);
  const count = people && (people.getAttribute("aria-label").match(/\d+/) || people.textContent.match(/\d+/));
  // Whether something that takes typing has the keyboard (the bare C would be typed into it): a text field, a list that selects by typing, an editable
  // area, an element that says it is one, or a frame (what it holds is not seen from here). A button, a link, a menu item, a container: the key goes on to the page.
  const held = document.activeElement;
  const kind = (held && held.getAttribute && (held.getAttribute("type") || "text").toLowerCase()) || "";
  const role = (held && held.getAttribute && (held.getAttribute("role") || "").toLowerCase()) || "";
  const typing = Boolean(held && held !== document.body && (
    held.isContentEditable || ["TEXTAREA", "SELECT", "IFRAME"].includes(held.tagName)
    || (held.tagName === "INPUT" && !["checkbox", "radio", "button", "submit", "reset", "image", "file", "range", "color", "hidden"].includes(kind))
    || ["textbox", "searchbox", "combobox", "spinbutton"].includes(role)));
  // Phase B: the buttons that open the panels, the unread count on the chat's, and which panels are open.
  const buttons = [...document.querySelectorAll('button, [role="button"]')].filter(drawn);
  const opener = (words) => buttons.find((b) => starts(nameOf(b), words));
  const openers = { chat: opener(M.chat.labels), people: opener(M.people.labels), host: opener(M.host_controls) };
  const chat = openers.chat;
  let unread = 0;
  if (chat) {
    const label = clean(chat.getAttribute("aria-label"));
    const said = label.match(/(\d+)\+?\s*(?:unread|new)/i) || label.match(/(?:unread|new)\D{0,12}(\d+)/i) || label.match(/\((\d+)\)/);
    const badge = [...chat.querySelectorAll("*")].filter((el) => shown(el) && !el.children.length).map((el) => clean(el.textContent))
      .find((piece) => /^\d{1,3}\+?$/.test(piece));
    unread = said ? Number(said[1]) : badge ? parseInt(badge, 10) : 0;
  }
  const open = openPanels();
  const pressed = (b) => Boolean(b) && (b.getAttribute("aria-pressed") === "true" || b.getAttribute("aria-expanded") === "true");
  return {
    mute: state(/turn (on|off) microphone/i, "off"),
    camera: state(/turn (on|off) camera/i, "off"),
    raise_hand: state(/(raise|lower) hand/i, "lower"),
    captions: state(/turn (on|off) captions?/i, "off"),
    people: people ? { count: count ? Number(count[0]) : null } : null,
    taken: typing,
    chat_unread: unread,
    hosting: Boolean(openers.host),
    panels: ["chat", "people", "host"].filter((name) => open[name] || pressed(openers[name])),
    drawn: { chat: Boolean(chat), reactions: Boolean(opener(M.reactions)), more_options: Boolean(opener(M.more_options)) },
  };
}"""

# Which of the side panels "chat", "people" and "host" is open, told apart the two ways it can be known: `open` by the panel's own structure
# (_SIDE: what is drawn), `pressed` by its button saying it is pressed or expanded (which Meet could leave stale). The provider decides a panel
# is closed only when neither says it is open, and presses to close only what its structure shows (Meet controls plan Phase B, Task 2).
# Run with MEET.
READ_PANELS = "(M) => {  const PANEL_NAMES = M.panel_names;" + _DRAWN + _SIDE + _WORDS + r"""
  const buttons = [...document.querySelectorAll('button, [role="button"]')].filter(drawn);
  const opener = (words) => buttons.find((b) => starts(nameOf(b), words));
  const openers = { chat: opener(M.chat.labels), people: opener(M.people.labels), host: opener(M.host_controls) };
  const pressed = (b) => Boolean(b) && (b.getAttribute("aria-pressed") === "true" || b.getAttribute("aria-expanded") === "true");
  const open = openPanels(), KINDS = ["chat", "people", "host"];
  return { open: KINDS.filter((k) => open[k]), pressed: KINDS.filter((k) => pressed(openers[k])) };
}"""

# The people panel, as READ_PEOPLE and PRESS_BY_LABEL both find a person in it: `peopleRows()` is each drawn row of the people panel
# ([role=listitem] or <li> with none inside it, not in a menu) as {el, id, name, me, waiting, pinned, muted}. A row's `id` is Meet's own
# participant id (data-participant-id, or data-requested-participant-id, on the row, its parent or something inside it), else "name:" + the name.
# Its name is its first piece of text that is neither "(You)" nor an email address, with "(You)" taken off; `me` is "(You)" anywhere in it.
# A row is `waiting` when the nearest heading before it (or the label of a section around it) starts with MEET.waiting: Meet's section of knocks.
_ROWS = r"""
  const HEADING = 'h1, h2, h3, h4, [role="heading"]';
  const waitingRow = (row, panel) => {
    for (let a = row.parentElement; a && panel.contains(a); a = a.parentElement) {
      if (starts(a.getAttribute("aria-label"), M.waiting)) return true;
      const before = [...a.children].filter((c) => c.matches(HEADING) && (c.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING));
      if (before.length) return starts(before[before.length - 1].innerText || before[before.length - 1].textContent, M.waiting);
    }
    return false;
  };
  const peopleRows = () => {
    const panel = openPanels().people;
    if (!panel) return [];
    const ROW = '[role="listitem"], li';
    return [...panel.querySelectorAll(ROW)]
      .filter((row) => drawn(row) && !row.querySelector(ROW) && !row.closest('[role="menu"], [role="listbox"]'))
      .map((row) => {
        const texts = pieces(row), label = clean(row.getAttribute("aria-label"));
        const me = /\(you\)/i.test(label) || texts.some((t) => /\(you\)/i.test(t) || /^you$/i.test(t));
        const bare = (t) => clean(t.replace(/\(you\)/gi, ""));
        const name = bare(texts.find((t) => bare(t) && !/^you$/i.test(t) && !/@/.test(t)) || label);
        const idEl = row.closest("[data-participant-id], [data-requested-participant-id]") || row.querySelector("[data-participant-id], [data-requested-participant-id]");
        const own = idEl && (idEl.getAttribute("data-participant-id") || idEl.getAttribute("data-requested-participant-id"));
        const inside = [...row.querySelectorAll("*")];
        return {
          el: row, name: name, me: me, id: own || "name:" + name, waiting: waitingRow(row, panel),
          pinned: inside.some((el) => shown(el) && (starts(el.getAttribute("aria-label"), M.pinned) || (el.matches(ICONS) && starts(el.textContent, M.pinned)) || (el.matches(PRESSABLE) && drawn(el) && starts(nameOf(el), M.unpin)))),
          muted: inside.some((el) => shown(el) && (starts(el.getAttribute("aria-label"), M.muted) || (el.matches(ICONS) && starts(el.textContent, M.muted)))),
        };
      })
      .filter((row) => row.name);
  };
"""

# With the people panel open: {"people": [{"id", "name", "me", "pinned", "muted"}], "waiting": [{"id", "name"}]} (see _ROWS), in the order
# the panel draws them; empty lists when it is not open. Two people with one name have two ids (Meet's own, else "name:" + the name, which
# is the same for both: whoever acts on such an id must see that more than one row has it). Run with MEET.
READ_PEOPLE = "(M) => {  const PANEL_NAMES = M.panel_names;" + _DRAWN + _SIDE + _WORDS + _ROWS + r"""
  const rows = peopleRows();
  return {
    people: rows.filter((r) => !r.waiting).map((r) => ({ id: r.id, name: r.name, me: r.me, pinned: r.pinned, muted: r.muted })),
    waiting: rows.filter((r) => r.waiting).map((r) => ({ id: r.id, name: r.name })),
  };
}"""

# With the chat panel open: its last 20 messages [{"from", "text", "at"}], oldest first. A message is what Meet marks as one
# (data-message-id), else a list item; its text is what Meet marks (data-message-text), else what is left of its drawn text after the time
# and the sender; `at` is the time it shows ("10:42 AM"), or "". Empty when the panel is not open. Run with MEET.
READ_CHAT = "(M) => {  const PANEL_NAMES = M.panel_names;" + _DRAWN + _SIDE + _WORDS + r"""
  const panel = openPanels().chat;
  if (!panel) return [];
  const TIME = /^\d{1,2}:\d{2}(?:\s?[ap]\.?m\.?)?$/i;
  let items = [...panel.querySelectorAll("[data-message-id]")].filter(drawn);
  if (!items.length) items = [...panel.querySelectorAll('[role="listitem"], li')].filter((el) => drawn(el) && !el.querySelector('[role="listitem"], li'));
  const messages = [];
  for (const item of items) {
    const marked = [...item.querySelectorAll("[data-message-text]")].filter(drawn);
    const rest = pieces(item, marked).filter((piece) => !TIME.test(piece));
    const at = pieces(item, marked).find((piece) => TIME.test(piece)) || "";
    const text = marked.length ? marked.map((el) => clean(el.innerText || el.textContent)).join("\n") : rest.slice(1).join(" ");
    if (text) messages.push({ from: rest[0] || "", text: text, at: at });
  }
  return messages.slice(-20);
}"""

# With the reactions picker open: the emoji it holds, in order, as strings. An emoji is a drawn button (or option, or menu item) that
# is nothing but one (data-emoji, an image's alt, or its text), not one inside a side panel. Empty when the picker is not open. Run with MEET.
_EMOJI = r"""
  const ONLY = /^[\p{Extended_Pictographic}‍️\u{1F3FB}-\u{1F3FF}\s]+$/u;
  const emojiButtons = () => {
    const found = [];
    for (const el of document.querySelectorAll(PRESSABLE)) {
      if (!drawn(el) || insideSide(el)) continue;
      const image = el.querySelector("img[alt]");
      const emoji = [el.getAttribute("data-emoji"), image && image.getAttribute("alt"), clean(el.innerText)].map(clean).find((t) => t && ONLY.test(t));
      if (emoji && !found.some((f) => f.emoji === emoji)) found.push({ el: el, emoji: emoji });
    }
    return found;
  };
"""
READ_REACTIONS = "(M) => {  const PANEL_NAMES = M.panel_names;" + _DRAWN + _SIDE + _WORDS + _EMOJI + r"""
  return emojiButtons().map((b) => b.emoji);
}"""

# Press the picker's button for `emoji` (as READ_REACTIONS gives it): true, or false when the picker shows no such one (nothing is pressed).
# It takes {"meet": MEET, "emoji": "…"}.
PRESS_REACTION = "({ meet, emoji }) => {  const M = meet;\n  const PANEL_NAMES = M.panel_names;" + _DRAWN + _SIDE + _WORDS + _EMOJI + r"""
  const button = emojiButtons().find((b) => b.emoji === emoji);
  if (!button || !enabled(button.el)) return false;
  button.el.click();
  return true;
}"""

# With the host controls panel open: [{"key", "label", "on"}] for each switch of MEET.host_switches drawn in it (a switch, a checkbox or a
# toggle button), in MEET's order: `label` is what Meet calls it (its label, else the label element or row of text it sits in), `on` whether
# it is on. Empty when the panel is not open. Run with MEET.
READ_HOST_SWITCHES = "(M) => {  const PANEL_NAMES = M.panel_names;" + _DRAWN + _SIDE + _WORDS + r"""
  const panel = openPanels().host;
  if (!panel) return [];
  const rowText = (el) => {
    for (let a = el.parentElement; a && panel.contains(a) && a !== panel; a = a.parentElement) {
      const text = clean(a.innerText);
      if (text && text.length <= 100) return text;
    }
    return "";
  };
  const nameOfSwitch = (el) => nameOf(el) || (el.labels && el.labels[0] ? clean(el.labels[0].innerText) : "") || rowText(el);
  const controls = [...panel.querySelectorAll('[role="switch"], [role="checkbox"], input[type="checkbox"], button[aria-pressed], [aria-checked]')]
    .filter(drawn).map((el) => ({ el: el, name: nameOfSwitch(el) }));
  const found = [];
  for (const [key, words] of Object.entries(M.host_switches)) {
    const control = controls.find((c) => starts(c.name, words));
    if (!control) continue;
    const el = control.el;
    found.push({ key: key, label: control.name, on: el.getAttribute("aria-checked") === "true" || el.getAttribute("aria-pressed") === "true" || el.checked === true });
  }
  return found;
}"""

# Press the drawn, enabled button, menu item, option or switch whose label starts with one of `labels` (looked for in the order given) and give
# back the label pressed, or null when there is none (nothing is pressed). It takes {"meet": MEET, "labels": [words], "container": ...} and
# the container, which says where to look, is one of:
#   null (or missing)     anywhere on the page;
#   "panel"               inside the open side panels (chat, people, host controls, activities, any <aside>);
#   "menu"                inside the open menus ([role=menu], [role=listbox]);
#   {"panel": kind}       inside the open side panel of that kind ("chat", "people", "host": by its structure, _SIDE), and nowhere else;
#   {"dialog": [words]}   inside a drawn dialog (not a side panel) whose title starts with one of the words (Change layout);
#   {"person": id}        inside the people panel's row with that id (as READ_PEOPLE gives it), and nowhere else: not even a menu its
#                         buttons name (one menu may serve every row; PERSON_MENU ties a menu to a row). An id that no row has, or that more
#                         than one row has ("name:" + a name two people share), presses nothing: the caller says so in words.
# A text field is never pressed (only the elements above are). A container with nothing in it, or nothing open, presses nothing. Two more
# keys narrow the match: "exact": true, the whole label is one of the words (the bar's "More options", not "More options for Pat");
# "own": the room's name, only an element whose label names the room or "you" (the room's own tile).
_FIND = r"""
  const DIALOGS = '[role="dialog"], [role="alertdialog"]';
  const rootsOf = (container) => {
    if (container === "panel") return sidePanels();
    if (container === "menu") return [...document.querySelectorAll('[role="menu"], [role="listbox"]')].filter(drawn);
    if (container && typeof container === "object" && container.panel) return [openPanels()[container.panel]].filter(Boolean);
    if (container && typeof container === "object" && container.dialog)
      return [...document.querySelectorAll(DIALOGS)].filter((d) => drawn(d) && !insideSide(d) && titlesOf(d).some((t) => starts(t, container.dialog)));
    if (container && typeof container === "object") {
      const rows = peopleRows().filter((row) => row.id === container.person);
      return rows.length === 1 ? [rows[0].el] : [];
    }
    return [document];
  };
  const findByLabel = (arg) => {
    let candidates = rootsOf(arg.container)
      .flatMap((root) => [...(root.matches && root.matches(PRESSABLE) ? [root] : []), ...root.querySelectorAll(PRESSABLE)])
      .filter((el) => drawn(el) && enabled(el));
    if (arg.own) {
      const room = String(arg.own).toLowerCase();
      candidates = candidates.filter((el) => { const n = nameOf(el).toLowerCase(); return (room && n.includes(room)) || /\byou\b/.test(n); });
    }
    for (const word of arg.labels) {
      const el = candidates.find((c) => arg.exact ? nameOf(c).toLowerCase() === word.toLowerCase() : starts(nameOf(c), [word]));
      if (el) return el;
    }
    return null;
  };
  const isOn = (el) => el.getAttribute("aria-checked") === "true" || el.getAttribute("aria-pressed") === "true"
    || el.getAttribute("aria-selected") === "true" || el.checked === true;
"""
PRESS_BY_LABEL = "(arg) => {  const M = arg.meet;\n  const PANEL_NAMES = M.panel_names;" + _DRAWN + _SIDE + _WORDS + _ROWS + _FIND + r"""
  const button = findByLabel(arg);
  if (!button) return null;
  const label = nameOf(button);
  button.click();
  return label;
}"""

# What PRESS_BY_LABEL would press for the same argument, without pressing it: {"label", "on"} (on: it says it is checked, pressed or
# selected), or null when there is none.
STATE_BY_LABEL = "(arg) => {  const M = arg.meet;\n  const PANEL_NAMES = M.panel_names;" + _DRAWN + _SIDE + _WORDS + _ROWS + _FIND + r"""
  const el = findByLabel(arg);
  return el ? { label: nameOf(el), on: isOn(el) } : null;
}"""

# The menus and the dialogs on the screen that are not side panels: {"menus": [role|label of each drawn menu or listbox], "dialogs": [the
# title of each drawn dialog]}. What an action opened is what it finds here that was not here before. Run with MEET.
OPEN_POPUPS = "(M) => {  const PANEL_NAMES = M.panel_names;" + _DRAWN + _SIDE + _WORDS + r"""
  const menus = [...document.querySelectorAll('[role="menu"], [role="listbox"]')].filter((m) => drawn(m) && !insideSide(m));
  const dialogs = [...document.querySelectorAll('[role="dialog"], [role="alertdialog"]')].filter((d) => drawn(d) && !insideSide(d));
  return { menus: menus.map((m) => m.getAttribute("role") + "|" + clean(m.getAttribute("aria-label"))), dialogs: dialogs.map((d) => titlesOf(d)[0] || "") };
}"""

# A person's own menu in the people panel (Pin, Remove, … behind a row's menu button), opened and pressed so that a menu that may be another
# row's is never pressed in (Review Focus 4). It takes {"meet": MEET, "id": the row's id, "labels": [words], "step": …}, in three steps:
#   "open"   the row (exactly one with that id, else "gone") has a menu button (one that says it opens a menu, or MEET.person_menu's words):
#            the menus on the screen now are marked as there before, the button is marked as the row's opener and pressed: "opened"; or
#            "none" when the row has no such button;
#   "press"  the menu that came after the press (not one marked as there before) is the row's when it is inside the row, or when the row's
#            opener names it (aria-controls, aria-owns), or when it is the only one that came and the opener says it is expanded:
#            {"pressed": label} after pressing the first item of `labels` in it; "waiting" while none has come; "untied" when the one that came
#            cannot be told to be the row's, or more than one came; "none" when the row's menu has no such item; "gone" when the row or its
#            opener is no longer there;
#   "done"   the marks are taken off: whether a menu that came after the press is still drawn (the caller closes it).
PERSON_MENU = "({ meet, id, labels, step }) => {  const M = meet;\n  const PANEL_NAMES = M.panel_names;" + _DRAWN + _SIDE + _WORDS + _ROWS + r"""
  const MENUS = '[role="menu"], [role="listbox"]';
  const menus = () => [...document.querySelectorAll(MENUS)].filter(drawn);
  const came = () => menus().filter((m) => !m.hasAttribute("data-croom-before"));
  const unmark = () => { for (const el of document.querySelectorAll("[data-croom-before], [data-croom-opener]")) { el.removeAttribute("data-croom-before"); el.removeAttribute("data-croom-opener"); } };
  if (step === "done") {
    const still = came().length > 0;
    unmark();
    return still;
  }
  const rows = peopleRows().filter((row) => row.id === id);
  if (rows.length !== 1) return "gone";
  const row = rows[0].el;
  if (step === "open") {
    const opener = [...row.querySelectorAll(PRESSABLE)].filter((el) => drawn(el) && enabled(el))
      .find((el) => ["menu", "true", "listbox"].includes((el.getAttribute("aria-haspopup") || "").toLowerCase()) || starts(nameOf(el), M.person_menu));
    if (!opener) return "none";
    unmark();   // what an earlier turn left, if it never got to "done"
    for (const m of menus()) m.setAttribute("data-croom-before", "");
    opener.setAttribute("data-croom-opener", "");
    opener.click();
    return "opened";
  }
  const opener = row.querySelector("[data-croom-opener]");
  if (!opener) return "gone";
  const fresh = came();
  if (!fresh.length) return "waiting";
  const named = ((opener.getAttribute("aria-controls") || "") + " " + (opener.getAttribute("aria-owns") || "")).split(/\s+/).filter(Boolean);
  let tied = fresh.filter((m) => row.contains(m) || (m.id && named.includes(m.id)));
  if (!tied.length && fresh.length === 1 && opener.getAttribute("aria-expanded") === "true") tied = fresh;
  if (tied.length !== 1) return "untied";
  const items = [...tied[0].querySelectorAll(PRESSABLE)].filter((el) => drawn(el) && enabled(el));
  for (const word of labels) {
    const item = items.find((el) => starts(nameOf(el), [word]));
    if (item) { const label = nameOf(item); item.click(); return { pressed: label }; }
  }
  return "none";
}"""

# Give Meet's chat message box the keyboard and say whether it has it now. The box is a drawn, enabled text field (a <textarea>, a text
# <input>, an editable area, a [role=textbox]) named by one of MEET.chat_box; with several, the one inside the open chat panel; with none, or
# no way to choose, nothing is focused and the answer is false: the caller types nothing (keys typed elsewhere in Meet are its shortcuts).
# What the box holds already is selected, so that what is typed next takes its place. Run with MEET.
_CHAT_BOX = r"""
  const FIELD = 'textarea, input, [contenteditable=""], [contenteditable="true"], [role="textbox"]';
  const fieldName = (el) => nameOf(el) || clean(el.getAttribute("placeholder"));
  const chatBox = () => {
    const boxes = [...document.querySelectorAll(FIELD)].filter((el) => drawn(el) && enabled(el) && !el.readOnly
      && !(el.tagName === "INPUT" && !["text", "search", ""].includes((el.getAttribute("type") || "").toLowerCase()))
      && starts(fieldName(el), M.chat_box));
    const chat = openPanels().chat;
    const inChat = chat ? boxes.filter((el) => chat.contains(el)) : [];
    return inChat.length === 1 ? inChat[0] : boxes.length === 1 ? boxes[0] : null;
  };
  const textIn = (box) => ["TEXTAREA", "INPUT"].includes(box.tagName) ? String(box.value) : String(box.innerText || box.textContent || "");
"""
FOCUS_CHAT_BOX = "(M) => {  const PANEL_NAMES = M.panel_names;" + _DRAWN + _SIDE + _WORDS + _CHAT_BOX + r"""
  const box = chatBox();
  if (!box) return false;
  box.focus();
  if (document.activeElement !== box) return false;
  if (typeof box.select === "function") box.select();
  else { const range = document.createRange(); range.selectNodeContents(box); const s = window.getSelection(); s.removeAllRanges(); s.addRange(range); }
  return true;
}"""

# Meet's chat message box as FOCUS_CHAT_BOX finds it: {"text": what it holds, "focused": whether it has the keyboard}, or null without one.
# Run with MEET.
CHAT_BOX_STATE = "(M) => {  const PANEL_NAMES = M.panel_names;" + _DRAWN + _SIDE + _WORDS + _CHAT_BOX + r"""
  const box = chatBox();
  return box ? { text: textIn(box), focused: document.activeElement === box } : null;
}"""
