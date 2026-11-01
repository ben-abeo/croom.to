"""
Meet's controls carried out by the provider (Meet controls plan Phase B, Task 3; Review Focus 3, 4 and 5), only where they guard a real risk:
Chat Send types nothing anywhere when Meet's message box is not there (keys typed elsewhere in Meet are its shortcuts), and no key at all
when it is; a person is acted on only when the id the touch Pi sent names exactly one row, and only inside that row or the menu that row's
own button just opened; End the call for everyone answers Meet's own confirmation box itself, after its own first press, while the box
reader queues nothing. Against the capture tests' fake Meet page in headless Chromium; nothing reaches meet.google.com.
"""

import asyncio

import pytest

from croom.meeting.providers.base import MeetingControlError, MeetingState
from tests.unit.meeting.test_meet_capture import FAKE_MEET, MENU_ITEMS, fake_meet  # noqa: F401 - fake_meet: a fixture
from tests.unit.meeting.test_meet_panels import in_a_call, shown

CALL = FAKE_MEET.replace("MENU_ITEMS", MENU_ITEMS)
CHAT_BOX = '<textarea aria-label="Send a message to everyone"></textarea>'

# The page's Send posts what the box holds and empties it; every input event anywhere is kept in window.__inputs.
CHAT = CALL.replace("</body>", """<script>
window.__inputs = [];
document.addEventListener('input', (e) => window.__inputs.push(e.target.tagName), true);
document.querySelector('#chat [aria-label="Send a message"]').addEventListener('click', () => {
  const box = document.querySelector('#chat textarea');
  if (!box || !box.value.trim()) return;
  const sent = document.createElement('div'); sent.setAttribute('data-message-id', 'm9'); sent.className = 'sent';
  sent.innerHTML = '<div>Room 1</div><div>10:50 AM</div><div data-message-text></div>';
  sent.querySelector('[data-message-text]').textContent = box.value;
  box.before(sent); box.value = '';
});
</script></body>""")

# The people panel: Pat Doe and Lee Roe (Meet's own ids), each with a menu button that opens one menu shared by every row (Meet's way, as
# far as anyone knows before the capture); and two people called Sam Same, with no id, each with a Pin button of their own. The menu's Pin
# pins the row whose button opened it last. window.__rowClicks keeps each click with the id of the row it was in ("" outside the rows).
ROWS = """
    <div role="listitem" data-participant-id="p1"><span>Pat Doe</span><button aria-label="More actions for Pat Doe" aria-haspopup="menu" aria-expanded="false" aria-controls="person-menu">more_vert</button></div>
    <div role="listitem" data-participant-id="p2"><span>Lee Roe</span><button aria-label="More actions for Lee Roe" aria-haspopup="menu" aria-expanded="false" aria-controls="person-menu">more_vert</button></div>
    <div role="listitem"><span>Sam Same</span><button aria-label="Pin Sam Same to your main screen">push_pin</button></div>
    <div role="listitem"><span>Sam Same</span><button aria-label="Pin Sam Same to your main screen">push_pin</button></div>
"""
PEOPLE = CALL.replace("""
    <div role="listitem" aria-label="Pat Doe">Pat Doe<button aria-label="Remove Pat Doe from the call">remove</button></div>
    <div role="listitem" aria-label="Lee Roe">Lee Roe<br>lee.roe@example.org</div>
""", ROWS).replace("</body>", """
<div id="person-menu" role="menu" hidden><div role="menuitem" id="pin-item">Pin to screen</div><div role="menuitem">Remove from the call</div></div>
<script>
window.__rowClicks = []; window.__pinned = [];
document.addEventListener('click', (e) => {
  const b = e.target.closest('button, [role=menuitem]');
  const row = b && b.closest('[data-participant-id], [role=listitem]');
  if (b) window.__rowClicks.push([row ? (row.dataset.participantId || row.innerText.split('\\n')[0]) : '', b.getAttribute('aria-label') || b.innerText.trim()]);
}, true);
const menu = document.getElementById('person-menu');
document.querySelectorAll('[aria-haspopup="menu"]').forEach((b) => b.addEventListener('click', () => {
  menu.dataset.for = b.closest('[data-participant-id]').dataset.participantId; menu.hidden = false;
  if (!window.__untied) b.setAttribute('aria-expanded', 'true');
}));
document.getElementById('pin-item').addEventListener('click', () => {
  window.__pinned.push(menu.dataset.for); menu.hidden = true;
  const row = document.querySelector('#people [data-participant-id="' + menu.dataset.for + '"]');
  row.querySelector('button').setAttribute('aria-expanded', 'false');
  const mark = document.createElement('span'); mark.setAttribute('aria-label', 'Pinned'); mark.textContent = 'keep'; row.append(mark);
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { menu.hidden = true; document.querySelectorAll('[aria-haspopup="menu"]').forEach((b) => b.setAttribute('aria-expanded', 'false')); } });
</script></body>""")
# The same, but the menu cannot be told to be the row's: its buttons name no menu and do not say they opened one.
UNTIED = PEOPLE.replace(' aria-controls="person-menu"', "").replace("window.__rowClicks = [];", "window.__untied = true; window.__rowClicks = [];")

# Meet's leave menu for a host, and its confirmation box, which comes a moment after "End the call for everyone" is pressed.
END = CALL.replace("</body>", """
<div id="leave-menu" role="menu" hidden><div role="menuitem">Just leave the call</div><div role="menuitem" id="end-all">End the call for everyone</div></div>
<div id="end-box" role="alertdialog" hidden><h2>End the call for everyone?</h2><div>Everyone will be removed from the call.</div><button>Cancel</button><button id="end-call">End call</button></div>
<script>
document.querySelector('[aria-label="Leave call"]').addEventListener('click', () => { document.getElementById('leave-menu').hidden = false; });
document.getElementById('end-all').addEventListener('click', () => {
  document.getElementById('leave-menu').hidden = true;
  setTimeout(() => { document.getElementById('end-box').hidden = false; }, 300);
});
document.getElementById('end-call').addEventListener('click', () => { document.getElementById('end-box').hidden = true; window.__ended = true; });
</script></body>""")


async def stay_on_the_page():
    return None


async def test_chat_send_types_nothing_anywhere_without_the_message_box_and_no_key_with_it(fake_meet):  # noqa: F811
    provider, page, clock = await in_a_call(fake_meet, CHAT.replace(CHAT_BOX, ""))
    with pytest.raises(MeetingControlError, match="^Meet's message box isn't ready; nothing was sent$"):
        await provider.control("chat.send", "hello C")
    assert await page.evaluate("window.__keys") == [] and await page.evaluate("window.__inputs") == []
    assert "hello C" not in await page.evaluate("document.body.innerText")
    assert await shown(page) == []                                  # the chat panel the room opened for it is closed again

    provider, page, clock = await in_a_call(fake_meet, CHAT)
    await provider.control("chat.send", "  hello C  ")
    assert await page.evaluate("window.__keys") == []               # typed without a key: no shortcut of Meet's can fire
    assert await page.evaluate("[...document.querySelectorAll('.sent [data-message-text]')].map((m) => m.textContent)") == ["hello C"]
    assert await page.evaluate("document.querySelector('#chat textarea').value") == ""
    assert await shown(page) == []


async def test_a_name_that_two_rows_have_or_none_is_refused_and_nobody_is_pressed_for(fake_meet):  # noqa: F811
    provider, page, clock = await in_a_call(fake_meet, PEOPLE)
    with pytest.raises(MeetingControlError, match="^Two people in the call are called Sam Same$"):
        await provider.control("pin", "name:Sam Same")
    with pytest.raises(MeetingControlError, match="^Nobody Here isn't in the call any more$"):
        await provider.control("pin", "name:Nobody Here")
    assert [click for click in await page.evaluate("window.__rowClicks") if click[0]] == []   # no row's button pressed
    assert await shown(page) == []


async def test_a_person_action_presses_only_in_that_persons_row_and_the_menu_it_just_opened(fake_meet):  # noqa: F811
    provider, page, clock = await in_a_call(fake_meet, PEOPLE)
    await provider.control("pin", "p2")
    assert await page.evaluate("window.__pinned") == ["p2"]
    clicks = [click for click in await page.evaluate("window.__rowClicks") if click[1] != "Show everyone (3)"]
    assert clicks == [["p2", "More actions for Lee Roe"], ["", "Pin to screen"]]
    assert await shown(page) == []

    # A menu that cannot be told to be that row's (it may be another row's): nothing is pressed in it, and it is closed.
    provider, page, clock = await in_a_call(fake_meet, UNTIED)
    with pytest.raises(MeetingControlError, match="Lee Roe"):
        await provider.control("pin", "p2")
    assert await page.evaluate("window.__pinned") == []
    assert [click[1] for click in await page.evaluate("window.__rowClicks") if click[1] != "Show everyone (3)"] == ["More actions for Lee Roe"]
    assert await page.evaluate("document.getElementById('person-menu').hidden") and await shown(page) == []


async def test_end_for_everyone_answers_meets_confirmation_itself_and_the_box_reader_queues_nothing(fake_meet, monkeypatch):  # noqa: F811
    provider, page, clock = await in_a_call(fake_meet, END)
    monkeypatch.setattr(provider, "_show_idle", stay_on_the_page)
    seen = []
    take = provider._on_dialog

    async def watched(key, dialog):
        seen.append(key[0])
        await take(key, dialog)

    monkeypatch.setattr(provider, "_on_dialog", watched)

    async def box_reader():                                      # the 2 s box loop, much faster
        while provider.state == MeetingState.CONNECTED:
            await provider._read_dialogs()
            await asyncio.sleep(0.02)

    reader = asyncio.create_task(box_reader())
    await asyncio.sleep(0.1)
    assert seen == ["Someone wants to join this call"]          # the reader is running
    assert await provider.control("host.end") == {}
    await reader

    clicks = await page.evaluate("window.__clicks")
    assert clicks == ["Leave call", "End the call for everyone", "End call", "Leave call"]   # the room leaves afterwards, as leave does
    assert await page.evaluate("window.__ended") is True
    assert seen == ["Someone wants to join this call"]          # Meet's confirmation was never read as a box of its own
    assert provider.state == MeetingState.IDLE
