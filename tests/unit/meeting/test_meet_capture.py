"""
The capture of Google Meet's page (spec 2026-10-30 Meet controls, section 3) against a fake Meet page built here (buttons with Meet's labels
opening fake panels, each closed its own way: Escape, a Close button, or its button again): every panel opens, is recorded and closes; one
that cannot open is recorded and the rest go on; nothing is pressed but what opens and closes; names, email addresses and chat are
redacted. Headless Chromium from the venv's Playwright; nothing reaches meet.google.com.
"""

import json
import re
import zipfile
from datetime import datetime, timezone

import pytest

from croom.meeting.meet_capture import MeetCapture, save_capture

playwright = pytest.importorskip("playwright.async_api")

MENU_ITEMS = '<div role="menuitem" id="layout-item">Change layout</div><div role="menuitem">Report a problem</div>'

# The call: two tiles (Pat Doe's, the room's), a knock waiting, Meet's bar, and the panels the bar opens. The chat closes by its Close
# button, the people panel only by its button again, the menus and Change layout by Escape, the host controls by their Close button. The
# reactions button opens a bar of plain divs (no role, no pressed state: only the count of labelled elements shows it), which Escape closes
# unless window.__sticky. Every click and key is kept in window.__clicks and window.__keys.
FAKE_MEET = """<!DOCTYPE html><html lang="en"><head><title>Meet - abc-defg-hij</title></head><body>
<div id="tiles">
  <div data-participant-id="p1"><span>Pat Doe</span><i>mic_off</i><button aria-label="More options for Pat Doe">more_vert</button></div>
  <div data-participant-id="p2"><span>Room 1</span><button aria-label="More options for Room 1 (You)" data-panel="tile-menu">more_vert</button></div>
</div>
<div role="alertdialog"><h2>Someone wants to join this call</h2><div>Sam Knock</div><button>Deny</button><button>Admit</button></div>
<div id="bar">
  <button aria-label="Chat with everyone" aria-pressed="false" data-panel="chat">chat</button>
  <button aria-label="Show everyone (3)" aria-pressed="false" data-panel="people">people</button>
  <button aria-label="Send a reaction" id="react">mood</button>
  <button aria-label="More options" aria-expanded="false" data-panel="menu">more_vert</button>
  <button aria-label="Host controls" aria-pressed="false" data-panel="host">shield</button>
  <button aria-label="Leave call">call_end</button>
</div>
<aside id="chat" class="panel" role="complementary" aria-label="Chat panel" hidden>
  <h2>In-call messages</h2><button aria-label="Close">close</button>
  <div data-message-id="m1"><div>Pat Doe</div><div>10:42 AM</div><div data-message-text>Hello from the laptop, write to pat.doe@example.org</div></div>
  <textarea aria-label="Send a message to everyone"></textarea><button aria-label="Send a message">send</button>
</aside>
<div id="people" class="panel" role="complementary" aria-label="People" hidden>
  <button>Mute everyone</button>
  <div role="list" aria-label="In the meeting">
    <div role="listitem" aria-label="Pat Doe">Pat Doe<button aria-label="Remove Pat Doe from the call">remove</button></div>
    <div role="listitem" aria-label="Lee Roe">Lee Roe<br>lee.roe@example.org</div>
  </div>
</div>
<div id="reactions-bar" hidden><div><button aria-label="Thumbs up">1</button><button aria-label="Heart">2</button></div></div>
<div id="menu" class="panel" role="menu" hidden>MENU_ITEMS</div>
<div id="layout" class="panel" role="dialog" aria-modal="true" aria-label="Change layout" hidden>
  <label><input type="radio" name="l" aria-label="Auto">Auto</label><label><input type="radio" name="l" aria-label="Tiled">Tiled</label>
  <button aria-label="Close">close</button>
</div>
<div id="tile-menu" class="panel" role="menu" hidden><div role="menuitem">Pin Room 1 to your main screen</div></div>
<div id="host" class="panel" role="dialog" aria-label="Host controls" hidden>
  <button>End call for everyone</button><button aria-label="Close">close</button>
</div>
<script>
const panel = (id) => document.getElementById(id);
function setOpen(id, on) {
  panel(id).hidden = !on;
  const opener = document.querySelector('[data-panel="' + id + '"]');
  if (opener) opener.setAttribute(opener.hasAttribute('aria-expanded') ? 'aria-expanded' : 'aria-pressed', String(on));
}
window.__clicks = []; window.__keys = [];
document.addEventListener('click', (e) => {
  const b = e.target.closest('button, [role=menuitem], [role=button]');
  if (b) window.__clicks.push(b.getAttribute('aria-label') || b.innerText.trim());
}, true);
document.querySelectorAll('[data-panel]').forEach((b) => b.addEventListener('click', () => setOpen(b.dataset.panel, panel(b.dataset.panel).hidden)));
document.querySelectorAll('#chat [aria-label="Close"], #layout [aria-label="Close"], #host [aria-label="Close"]')
  .forEach((b) => b.addEventListener('click', () => setOpen(b.closest('.panel').id, false)));
document.getElementById('react').addEventListener('click', () => { if (window.__inert) return; const bar = panel('reactions-bar'); bar.hidden = !bar.hidden; });
const item = document.getElementById('layout-item');
if (item) item.addEventListener('click', () => { setOpen('menu', false); setOpen('layout', true); });
document.addEventListener('keydown', (e) => {
  window.__keys.push(e.key);
  if (e.key === 'Escape' && !window.__sticky && !panel('reactions-bar').hidden) { panel('reactions-bar').hidden = true; return; }
  if (e.key === 'Escape') for (const id of ['layout', 'menu', 'tile-menu']) if (!panel(id).hidden) { setOpen(id, false); break; }
});
</script></body></html>"""

OPEN_OR_CLOSE = {"Chat with everyone", "Show everyone (3)", "Send a reaction", "More options", "Change layout", "More options for Room 1 (You)",
                 "Host controls", "Close"}


@pytest.fixture
async def fake_meet():
    """A factory: `await fake_meet(html)` is a headless page showing it."""
    async with playwright.async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as e:  # noqa: BLE001 - browser missing on this machine
            pytest.skip(f"Chromium not available: {e}")

        async def show(html):
            page = await browser.new_page(viewport={"width": 1280, "height": 720})
            await page.set_content(html)
            return page

        try:
            yield show
        finally:
            await browser.close()


def quick(page, **options):
    capture = MeetCapture(page, room_name="Room 1", **options)
    capture.OPEN_WAIT_S, capture.CLOSE_WAIT_S, capture.POLL_S = 0.5, 0.4, 0.05
    return capture


async def open_panels(page):
    return await page.evaluate("() => [...document.querySelectorAll('.panel')].filter((p) => !p.hidden).map((p) => p.id)")


async def test_every_panel_opens_is_recorded_and_closes_and_nothing_else_is_pressed(fake_meet, tmp_path):
    page = await fake_meet(FAKE_MEET.replace("MENU_ITEMS", MENU_ITEMS))
    capture = quick(page)
    result = await capture.run()
    steps = {step["name"]: step for step in result["steps"]}

    assert list(steps) == ["call", "chat", "people", "reactions", "more_options", "layout", "own_tile", "host_controls", "dialogs"]
    for name in ("chat", "people", "reactions", "more_options", "layout", "own_tile", "host_controls"):
        assert steps[name]["opened"] is True and steps[name]["closed"] is True, steps[name]
        assert steps[name]["elements"] and steps[name]["screenshot"] == f"{list(steps).index(name) + 1:02d}-{name}.png"
    assert steps["reactions"]["panels"] == []                                              # seen by the count alone, and closed
    assert await page.evaluate("document.getElementById('reactions-bar').hidden")
    assert steps["layout"]["by"] == "More options › Change layout"
    assert steps["layout"]["panels"] == [{"role": "dialog", "label": "Change layout", "modal": "true"}]   # its role and aria-modal
    assert "More options for Room 1 (You)" in capture.pressed and "More options for Pat Doe" not in capture.pressed   # the room's own
    assert steps["dialogs"]["boxes"][0]["buttons"] == ["Deny", "Admit"]
    assert await open_panels(page) == []

    clicks = await page.evaluate("window.__clicks")
    assert set(clicks) <= OPEN_OR_CLOSE, clicks
    assert set(await page.evaluate("window.__keys")) <= {"Escape"}
    assert await page.locator('[role="alertdialog"]').is_visible()                         # the knock still waits: not answered

    path = save_capture(result, capture.images, tmp_path)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert names[0] == "capture.json" and "02-chat.png" in names and "04-reactions.png" in names
        assert json.loads(archive.read("capture.json"))["versions"]["meet_lang"] == "en"


async def test_a_step_that_fails_is_recorded_its_panel_closed_and_the_run_goes_on(fake_meet):
    page = await fake_meet(FAKE_MEET.replace("MENU_ITEMS", '<div role="menuitem">Report a problem</div>'))   # no Change layout
    await page.evaluate("window.__sticky = true")                     # the reactions bar does not go: nothing says it went
    capture = quick(page, screenshots=False)
    gather = capture._gather

    async def people_breaks(step, keys):
        if step.name == "people":
            raise RuntimeError("the page went away")
        return await gather(step, keys)

    capture._gather = people_breaks
    result = await capture.run()
    steps = {step["name"]: step for step in result["steps"]}

    # Redaction fails closed: the people panel's names could not be read, so none of its elements is kept.
    assert steps["people"]["why"] == "Could not read the names on it (the page went away); its elements are left out"
    assert "elements" not in steps["people"] and "Lee Roe" not in json.dumps(result) and steps["people"]["closed"] is True
    assert steps["reactions"]["opened"] is True and steps["reactions"]["closed"] == "unknown"   # Ben is told to look at the TV
    assert steps["layout"]["opened"] is False and steps["layout"]["why"] == "Meet's More options menu has no Change layout item"
    assert steps["layout"]["closed"] is True                                                # the menu it opened is closed
    assert steps["host_controls"]["opened"] is True and steps["host_controls"]["closed"] is True
    assert await open_panels(page) == [] and capture.images == {}


async def test_names_email_addresses_and_chat_are_redacted_in_labels_and_texts(fake_meet):
    page = await fake_meet(FAKE_MEET.replace("MENU_ITEMS", MENU_ITEMS))
    result = await quick(page, screenshots=False).run()
    words = json.dumps(result, ensure_ascii=False)

    for private in ("Pat Doe", "Lee Roe", "Sam Knock", "pat.doe@example.org", "lee.roe@example.org", "Hello from the laptop"):
        assert private not in words, private
    labels = {e["label"] for step in result["steps"] for e in step.get("elements", [])}
    texts = {e["text"] for step in result["steps"] for e in step.get("elements", [])}
    pat = next(label[len("More options for "):] for label in labels if label.startswith("More options for Person") and "(You)" not in label)
    assert f"Remove {pat} from the call" in labels                                         # the same placeholder throughout
    assert any("person1@example.com" in text or "person2@example.com" in text for text in texts)
    assert any(re.search(r"Message \d", text) for text in texts)
    assert "Chat with everyone" in labels and "Change layout" in labels                     # Meet's own words stay


async def test_a_count_that_moves_with_nothing_opened_presses_nothing(fake_meet):
    """Someone joins (a labelled element appears) after Send a reaction opened nothing: the capture cannot say the page is as it was, but
    nothing of its own is open, so it presses no Escape for it, then or at the end."""
    page = await fake_meet(FAKE_MEET.replace("MENU_ITEMS", MENU_ITEMS))
    await page.evaluate("window.__inert = true")
    capture = quick(page, screenshots=False)
    real_open = capture._open

    async def someone_joins(step, before):
        answer = await real_open(step, before)
        if step.name == "reactions":
            await page.evaluate("""() => { const toast = document.createElement('div'); toast.setAttribute('role', 'status');
                                           toast.textContent = 'Lee Roe joined'; document.body.append(toast); }""")
        return answer

    capture._open = someone_joins
    steps = {step["name"]: step for step in (await capture.run())["steps"]}

    assert steps["reactions"]["opened"] is False and steps["reactions"]["closed"] == "unknown"
    after = capture.pressed[capture.pressed.index("Send a reaction") + 1:]
    assert after[0] == "More options"                                   # no Escape for the step that opened nothing
    assert capture.pressed[-1] == "Close"                               # nor at the end (the host controls' Close was the last press)
