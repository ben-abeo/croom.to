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
