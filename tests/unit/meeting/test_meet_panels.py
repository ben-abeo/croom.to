"""
The touch Pi's Meet screens on the TV (Meet controls plan Phase B, Task 2; Review Focus 1 and 2): the panel the room opens for a screen is
closed when the screen closes, when the touch Pi stops renewing it (180 s, a fake clock), when the call ends and before the capture (and
opened again after it); a panel someone at the TV opened is never closed; the room's open chat panel is never one of Meet's boxes. Against
the capture tests' fake Meet page in headless Chromium; nothing reaches meet.google.com.
"""

from croom.meeting.providers import google_meet
from croom.meeting.providers.base import MeetingInfo, MeetingState
from croom.meeting.providers.google_meet import GoogleMeetProvider
from tests.unit.meeting.test_meet_capture import FAKE_MEET, MENU_ITEMS, fake_meet  # noqa: F401 - fake_meet: a fixture

CALL = FAKE_MEET.replace("MENU_ITEMS", MENU_ITEMS)
# The same call with the chat panel drawn as a dialog, as a box would be (its title is the one Meet gives the panel).
CHAT_AS_DIALOG = CALL.replace('<aside id="chat" class="panel" role="complementary" aria-label="Chat panel" hidden>',
                              '<div id="chat" class="panel" role="dialog" aria-label="In-call messages" hidden>').replace("</aside>", "</div>")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def in_a_call(fake_meet, html=CALL):  # noqa: F811
    page = await fake_meet(html)
    clock = Clock()
    provider = GoogleMeetProvider(clock=clock)
    provider._page = page
    provider._current_meeting = MeetingInfo(platform="google_meet", meeting_id="abc-defg-hij", meeting_url="https://meet.google.com/abc-defg-hij")
    provider._set_state(MeetingState.CONNECTED)
    return provider, page, clock


async def shown(page):
    """The fake page's panels that are open."""
    return await page.evaluate("() => [...document.querySelectorAll('.panel')].filter((p) => !p.hidden).map((p) => p.id)")


async def test_the_panel_follows_the_screen_and_is_closed_when_the_screen_closes(fake_meet):  # noqa: F811
    provider, page, clock = await in_a_call(fake_meet)
    await provider.control("screen", "participants")
    assert await shown(page) == ["people"]
    assert [p["name"] for p in provider.current_meeting.people["list"]] == ["Pat Doe", "Lee Roe"]
    await provider.control("screen", "chat")                    # one side panel at a time: the people panel the room opened goes
    assert await shown(page) == ["chat"]
    await provider.control("screen", "")
    assert await shown(page) == []


async def test_the_panel_is_closed_when_the_touch_pi_stops_renewing_its_screen(fake_meet):  # noqa: F811
    provider, page, clock = await in_a_call(fake_meet)
    await provider.control("screen", "participants")
    clock.now += 120
    await provider.control("screen", "participants")            # the page's renewal
    clock.now += 179
    await provider._panel_turn()
    assert await shown(page) == ["people"]
    clock.now += 2                                              # 181 s since the last renewal: the touch Pi's page went away
    await provider._panel_turn()
    assert await shown(page) == []


async def test_the_panel_is_closed_when_the_call_ends(fake_meet, monkeypatch):  # noqa: F811
    provider, page, clock = await in_a_call(fake_meet)

    async def stay_on_the_page():
        return None

    monkeypatch.setattr(provider, "_show_idle", stay_on_the_page)   # the TV would go to the screensaver: this test looks at the page instead
    await provider.control("screen", "participants")
    await provider.leave_meeting()
    assert await shown(page) == []
    assert provider._room_panel is None and provider._screen == ""


async def test_the_panel_is_closed_before_the_capture_and_opened_again_after_it(fake_meet, monkeypatch, tmp_path):  # noqa: F811
    provider, page, clock = await in_a_call(fake_meet)
    await provider.control("screen", "participants")
    seen = []

    async def run(capture):
        seen.append(await shown(page))
        return {"steps": []}

    monkeypatch.setattr(google_meet.MeetCapture, "run", run)
    monkeypatch.setattr(google_meet, "save_capture", lambda result, images, directory: directory / "capture.zip")
    await provider.capture(tmp_path, screenshots=False)
    assert seen == [[]]                                          # the capture found Meet's page with no panel of the room's on it
    assert await shown(page) == ["people"]                       # the screen is still open on the touch Pi


async def test_a_panel_the_room_did_not_open_is_never_closed(fake_meet, monkeypatch):  # noqa: F811
    provider, page, clock = await in_a_call(fake_meet)

    async def stay_on_the_page():
        return None

    monkeypatch.setattr(provider, "_show_idle", stay_on_the_page)
    await page.click('[aria-label="Show everyone (3)"]')         # someone at the TV opened the people panel
    await provider.control("screen", "participants")
    assert [p["name"] for p in provider.current_meeting.people["list"]] == ["Pat Doe", "Lee Roe"]   # read, as it is open
    await provider.control("screen", "")
    assert await shown(page) == ["people"]
    await provider.control("screen", "participants")
    clock.now += 200
    await provider._panel_turn()
    assert await shown(page) == ["people"]
    await provider.leave_meeting()
    assert await shown(page) == ["people"]
    assert await page.evaluate("window.__clicks") == ["Show everyone (3)", "Leave call"]   # the TV's own press, then Leave: nothing else


async def test_the_rooms_open_chat_panel_is_not_one_of_meets_boxes(fake_meet):  # noqa: F811
    provider, page, clock = await in_a_call(fake_meet, CHAT_AS_DIALOG)
    await provider.control("screen", "chat")
    assert await shown(page) == ["chat"]
    clicks = await page.evaluate("window.__clicks.length")
    await provider._read_dialogs()
    info = provider.current_meeting
    assert info.dialog is not None and info.dialog["title"] == "Someone wants to join this call"      # the knock, and only the knock
    assert [key[0] for key in provider._on_screen] == ["Someone wants to join this call"]
    assert await page.evaluate("window.__clicks.length") == clicks                                     # nothing pressed in the panel
    assert await shown(page) == ["chat"]
