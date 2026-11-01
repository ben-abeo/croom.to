"""
The page scripts Google Meet's call page is read and driven with (spec 2026-10-30 Meet controls, section 4), in one place so that a change
Meet makes to its page is fixed here. Each is a function of the page, run with `page.evaluate`, that returns plain data.

Phase A: the boxes Meet draws over the call (its notices, "Someone wants to join", "Leave call?").
"""

# What the two scripts below share: the boxes on the screen, each with its words and its buttons.
#
# A box is a drawn [role=dialog] or [role=alertdialog] with at least one drawn button (one with none asks nothing of anyone), and not one
# inside another (that is a part of the outer box). Drawn: it takes up room on the screen, is not made invisible, and is not inside
# something the page hides from assistive technology. Meet gives its boxes no ids: a box is known by its title and its words.
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
    .filter((box) => !(box.parentElement && box.parentElement.closest(BOX)) && drawn(box))
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
"""

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
  const icon = (line) => /^[a-z0-9_]+$/.test(line);   // a Material icon's ligature (mic_off, more_vert): Meet's own word, not a person's
  const nameLike = (line) => line.length >= 2 && line.length <= 80 && !icon(line) && !/^you$/i.test(line) && !/^\d{1,2}:\d\d/.test(line);
  const lines = (el) => String(el.innerText || "").split("\n").map(clean).filter(Boolean);
  // Each piece of text drawn inside el and not on a button, on its own (a name beside an icon's ligature is not run into it).
  const pieces = (el) => {
    const found = [], walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const owner = walker.currentNode.parentElement, piece = clean(walker.currentNode.nodeValue);
      if (piece && owner && drawn(owner) && !owner.closest('button, [role="button"]')) found.push(piece);
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
    for (const el of found) messages.push(...lines(el).filter((line) => !icon(line) && !/^\d{1,2}:\d\d/.test(line)));
    if (kind === "chat" && !found.length) {
      for (const p of opened) {
        const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
          const owner = walker.currentNode.parentElement, line = clean(walker.currentNode.nodeValue);
          if (!line || icon(line) || !owner || !drawn(owner)) continue;
          if (owner.closest('button, [role="button"], h1, h2, h3, h4, [role="heading"], label, input, textarea')) continue;
          messages.push(line);
        }
      }
    }
  }
  return { names: names.map(clean).filter(Boolean), messages: messages.map(clean).filter(Boolean) };
}"""
