"""
Meet's page scripts for the Phase B controls (meet_page: READ_DIALOGS, READ_PEOPLE, PRESS_BY_LABEL, FOCUS_CHAT_BOX) against a fake Meet page
with the chat and people panels open and drawn as role=dialog, as a box would be: a panel is never taken for one of Meet's boxes, two people
with one name are kept apart, a press scoped to one person's row never reaches another's, and the message box is only ever focused when it is
drawn. Headless Chromium from the venv's Playwright; nothing reaches meet.google.com.
"""

import pytest

from croom.meeting.meet_page import FOCUS_CHAT_BOX, MEET, PRESS_BY_LABEL, READ_DIALOGS, READ_PEOPLE
from tests.unit.meeting.test_meet_capture import fake_meet  # noqa: F401 - a fixture: headless pages built here

KNOCK = '<div role="alertdialog"><h2>Someone wants to join this call</h2><div>Sam Knock</div><button>Deny</button><button>Admit</button></div>'
CHAT_BOX = '<textarea aria-label="Send a message to everyone"></textarea>'

# The call: the chat panel (a dialog by its title), the people panel (a dialog inside an <aside>, by its structure) and a second <aside> whose
# dialog has a title no table knows. The people panel holds one person waiting, two people called Pat Doe (each with Meet's own id), the room's
# own row ("(You)") and a person Meet gives no id. Every click is kept in window.__clicks as the id of the row it was in and its label.
PAGE = """<!DOCTYPE html><html><body>
{knock}
<div id="chat" role="dialog" aria-label="Chat"><h2>In-call messages</h2><button aria-label="Close">close</button>
  <div data-message-id="m1"><div>Pat Doe</div><div>10:42 AM</div><div data-message-text>Hello</div></div>{box}<button aria-label="Send a message">send</button></div>
<aside><div role="dialog" aria-label="People"><h2>People</h2><button>Mute all</button>
  <div role="list" aria-label="Waiting to join"><div role="listitem" data-participant-id="w1">Sam Knock<button aria-label="Admit Sam Knock">Admit</button><button aria-label="Deny Sam Knock">Deny</button></div></div>
  <div role="list" aria-label="In the meeting">
    <div role="listitem" data-participant-id="p1"><span>Pat Doe</span><i>mic_off</i><button aria-label="Remove Pat Doe from the call">remove</button></div>
    <div role="listitem" data-participant-id="p2"><span>Pat Doe</span><button aria-label="Remove Pat Doe from the call">remove</button></div>
    <div role="listitem" data-participant-id="p3"><span>Room 1</span><span>(You)</span></div>
    <div role="listitem"><span>Lee Roe</span><button aria-label="Remove Lee Roe from the call">remove</button></div>
    <div role="listitem"><span>Dup Name</span><button aria-label="Remove Dup Name from the call">remove</button></div>
    <div role="listitem"><span>Dup Name</span><button aria-label="Remove Dup Name from the call">remove</button></div>
  </div></div></aside>
<aside><div role="dialog" aria-label="Brainstorm"><h2>Brainstorm</h2><button>Start</button></div></aside>
<script>
window.__clicks = [];
document.addEventListener('click', (e) => {{
  const b = e.target.closest('button');
  if (b) window.__clicks.push([(b.closest('[data-participant-id]') || {{getAttribute: () => ''}}).getAttribute('data-participant-id'), b.getAttribute('aria-label') || b.innerText.trim()]);
}}, true);
</script></body></html>""".format(knock=KNOCK, box=CHAT_BOX)


async def test_the_chat_and_people_panels_are_not_boxes_but_a_real_box_beside_them_is(fake_meet):  # noqa: F811
    page = await fake_meet(PAGE)
    boxes = await page.evaluate(READ_DIALOGS)
    assert [box["title"] for box in boxes] == ["Someone wants to join this call"]


async def test_two_people_with_one_name_are_kept_apart_and_the_rooms_own_row_is_marked(fake_meet):  # noqa: F811
    page = await fake_meet(PAGE)
    reading = await page.evaluate(READ_PEOPLE, MEET)
    assert reading["waiting"] == [{"id": "w1", "name": "Sam Knock"}]
    assert [(p["id"], p["name"], p["me"], p["muted"]) for p in reading["people"]] == [
        ("p1", "Pat Doe", False, True), ("p2", "Pat Doe", False, False), ("p3", "Room 1", True, False), ("name:Lee Roe", "Lee Roe", False, False),
        ("name:Dup Name", "Dup Name", False, False), ("name:Dup Name", "Dup Name", False, False)]


async def test_a_press_scoped_to_one_persons_row_never_presses_another_rows_button(fake_meet):  # noqa: F811
    page = await fake_meet(PAGE)
    remove = ["Remove from the call", "Remove"]
    assert await page.evaluate(PRESS_BY_LABEL, {"meet": MEET, "labels": remove, "container": {"person": "p2"}}) == "Remove Pat Doe from the call"
    assert await page.evaluate("window.__clicks") == [["p2", "Remove Pat Doe from the call"]]
    # p3 has no such button: nothing is pressed, though p1's, p2's and Lee Roe's are on the screen
    assert await page.evaluate(PRESS_BY_LABEL, {"meet": MEET, "labels": remove, "container": {"person": "p3"}}) is None
    # a person nobody has, and a name two rows share: nobody is pressed for
    assert await page.evaluate(PRESS_BY_LABEL, {"meet": MEET, "labels": remove, "container": {"person": "p9"}}) is None
    assert await page.evaluate(PRESS_BY_LABEL, {"meet": MEET, "labels": remove, "container": {"person": "name:Dup Name"}}) is None
    assert await page.evaluate("window.__clicks") == [["p2", "Remove Pat Doe from the call"]]
    # a row Meet gives no id is found by its name alone
    assert await page.evaluate(PRESS_BY_LABEL, {"meet": MEET, "labels": remove, "container": {"person": "name:Lee Roe"}}) == "Remove Lee Roe from the call"


async def test_the_message_box_is_focused_only_when_it_is_drawn_and_never_the_send_button(fake_meet):  # noqa: F811
    page = await fake_meet(PAGE)
    assert await page.evaluate(FOCUS_CHAT_BOX, MEET) is True
    assert await page.evaluate("document.activeElement.tagName") == "TEXTAREA"

    page = await fake_meet(PAGE.replace(CHAT_BOX, ""))                      # no box: false, and nothing has the keyboard
    assert await page.evaluate(FOCUS_CHAT_BOX, MEET) is False
    assert await page.evaluate("document.activeElement.tagName") == "BODY"
    page = await fake_meet(PAGE.replace(CHAT_BOX, CHAT_BOX.replace("<textarea", '<textarea style="display:none"')))   # a box that is not drawn
    assert await page.evaluate(FOCUS_CHAT_BOX, MEET) is False
