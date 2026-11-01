"""
Meet's boxes (spec 2026-10-30 Meet controls, section 3): the reader against pages built here (the photo's notice, a two-button box, "Leave
call?", hidden boxes), and the provider's policy against a stand-in page: a notice pressed once, a choice and a Leave never pressed for the
room, a box answered from the table, a press that fails on the table. Nothing here reaches meet.google.com.
"""

import asyncio

import pytest

from croom.core.config import Config
from croom.meeting.meet_page import PRESS_DIALOG, READ_DIALOGS
from croom.meeting.providers.base import MeetingControlError, MeetingInfo, MeetingState
from croom.meeting.providers.google_meet import GoogleMeetProvider
from tests.unit.meeting.test_meet_controls import BUTTONS, meet_page, replica  # noqa: F401 - meet_page: the replica of Meet's pages, a fixture

NOTICE = {"title": "Others may see your video differently",
          "text": "Your self view tile may show less of your background than others see. To view your entire video, click the icon button "
                  "on the bottom right of your tile.",
          "buttons": ["Got it"]}
KNOCK = {"title": "Someone wants to join this call", "text": "Pat Doe", "buttons": ["Deny", "Admit"]}
LEAVE = {"title": "Leave call?", "text": "You're the last one here.", "buttons": ["Cancel", "Leave call"]}

PAGE = """<!DOCTYPE html><html><body>
<div role="dialog" aria-labelledby="t1" aria-describedby="d1">
  <img alt="" src="data:,">
  <h2 id="t1">Others may see your video differently</h2>
  <div id="d1">Your self view tile may show less of your background than others see.
    To view your entire video, click the icon button on the bottom right of your tile.</div>
  <div><button onclick="this.closest('[role=dialog]').remove()"><span>Got it</span></button></div>
</div>
<div role="alertdialog"><h2>Someone wants to join this call</h2><div>Pat Doe</div><button>Deny</button><button>Admit</button></div>
<div role="dialog"><h2>Leave call?</h2><p>You're the last one here.</p><button>Cancel</button><button aria-label="Leave call">Leave</button></div>
<div role="dialog" style="display:none"><h2>Hidden</h2><button>OK</button></div>
<div role="dialog" style="visibility:hidden"><h2>Invisible</h2><button>OK</button></div>
<div aria-hidden="true"><div role="dialog"><h2>Kept away</h2><button>OK</button></div></div>
<div role="dialog"><h2>No buttons, nothing to answer</h2></div>
</body></html>"""


async def test_the_reader_finds_the_photos_notice_and_the_boxes_with_a_choice_and_ignores_hidden_ones():
    playwright = pytest.importorskip("playwright.async_api")
    async with playwright.async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as e:  # noqa: BLE001 - browser missing on this machine
            pytest.skip(f"Chromium not available: {e}")
        try:
            page = await browser.new_page()
            await page.set_content(PAGE)
            assert await page.evaluate(READ_DIALOGS) == [NOTICE, KNOCK, LEAVE]
            assert await page.evaluate(PRESS_DIALOG, [NOTICE["title"], NOTICE["text"], "Got it"]) == "pressed"
            assert await page.evaluate(READ_DIALOGS) == [KNOCK, LEAVE]
            assert await page.evaluate(PRESS_DIALOG, [NOTICE["title"], NOTICE["text"], "Got it"]) == "gone"
            assert await page.evaluate(PRESS_DIALOG, [KNOCK["title"], KNOCK["text"], "Maybe"]) == "no button"
        finally:
            await browser.close()


class StandInPage:
    """Meet's page as the provider reads it: the boxes on the screen, and the presses made on them. A box closes when pressed unless it is
    `stuck` (Meet did not take the press)."""

    def __init__(self, *boxes):
        self.boxes = [dict(box) for box in boxes]
        self.presses = []
        self.stuck = False

    async def evaluate(self, script, arg=None):
        if script == READ_DIALOGS:
            return [dict(box) for box in self.boxes]
        assert script == PRESS_DIALOG
        title, text, label = arg
        box = next((b for b in self.boxes if (b["title"], b["text"]) == (title, text)), None)
        if box is None:
            return "gone"
        self.presses.append((title, label))
        if not self.stuck:
            self.boxes.remove(box)
        return "pressed"


def in_a_call(page):
    provider = GoogleMeetProvider.from_config(Config())
    provider.PRESS_WAIT_S, provider.DIALOG_POLL_S, provider.ANSWER_SPACING_S = 0.1, 0.01, 0
    provider._page = page
    provider._current_meeting = MeetingInfo(platform="google_meet", meeting_id="abc-defg-hij", meeting_url="https://meet.google.com/abc-defg-hij")
    provider._state = MeetingState.CONNECTED
    return provider, provider._current_meeting


async def test_the_photos_notice_is_pressed_once_with_no_notice_line():
    assert "others may see your video differently" in Config().meeting.dialogs.acknowledge
    page = StandInPage(NOTICE)
    provider, info = in_a_call(page)
    await provider._read_dialogs()
    await provider._read_dialogs()
    assert page.presses == [(NOTICE["title"], "Got it")]
    assert info.notice == ""                           # a tip, not a disclosure: nothing for the notice line
    assert info.dialog is None


async def test_a_notice_meet_keeps_on_the_screen_is_not_pressed_again_and_waits_on_the_table():
    page = StandInPage(NOTICE)
    page.stuck = True
    provider, info = in_a_call(page)
    for _ in range(3):
        await provider._read_dialogs()
    assert page.presses == [(NOTICE["title"], "Got it")]
    assert info.dialog == {"id": info.dialog["id"], **NOTICE}


async def test_a_choice_and_a_leave_are_never_pressed_for_the_room_and_are_answered_from_the_table():
    # A one-button box whose words are on the list but whose button leaves: never pressed for the room either.
    lone_leave = {"title": "Others may see your video differently", "text": "", "buttons": ["Leave call"]}
    page = StandInPage(KNOCK, LEAVE, lone_leave)
    provider, info = in_a_call(page)
    await provider._read_dialogs()
    await provider._read_dialogs()
    assert page.presses == []
    assert info.dialog == {"id": info.dialog["id"], **KNOCK}

    await provider.control("dialog", {"id": info.dialog["id"], "button": "Admit"})
    assert page.presses == [(KNOCK["title"], "Admit")]
    assert info.dialog["title"] == LEAVE["title"]

    page.boxes = []                       # answered at the TV: forgotten, and a tap on it is refused in words
    gone = info.dialog["id"]
    await provider._read_dialogs()
    assert info.dialog is None
    with pytest.raises(MeetingControlError, match="no longer on Meet's screen"):
        await provider.control("dialog", {"id": gone, "button": "Cancel"})


async def test_the_notice_meet_shows_as_the_room_joins_is_pressed_right_after_the_join(meet_page):  # noqa: F811 - the fixture
    """End to end on the replica of Meet's call: the box reader runs from the join, and the notice goes from the page."""
    notice = PAGE.split("<body>")[1].split('<div role="alertdialog">')[0]

    def with_box_readings(provider):
        del provider._read_dialogs_regularly          # the fixture turns them off; this test wants the real ones

    provider, info = await meet_page(replica(BUTTONS + notice), setup=with_box_readings)
    for _ in range(100):
        if not await provider._page.evaluate(READ_DIALOGS):
            break
        await asyncio.sleep(0.05)
    assert await provider._page.evaluate(READ_DIALOGS) == []
    assert info.notice == ""                           # pressed, and a tip puts nothing on the notice line
    assert info.dialog is None
